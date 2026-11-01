import base64
import json

import httpx
import pytest
from sqlalchemy import select

from lingxi import channel_speech as speech, worker
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from lingxi.channels import Channel, ChannelDelivery, ChannelInbound
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Job, Message, Provider, Memory
from lingxi.security import decrypt
from lingxi.voice import VoiceConfig, VoiceSession, VoicePlayback
from test_channels import create, rules, connect, event, bind
from test_voice import voice_config, wav


def setup(client, monkeypatch, text='这是完整的回答。', enabled=True, group=False, shared=False):
    voice_config(client)
    channel = rules(client, create(client), allow_unbound=True, voice_reply_enabled=enabled,
                    group_enabled=group, group_ids=['12345'] if group else [])
    connection = connect(channel['id'])
    if shared:
        bind(client, channel, connection, True)
    receive_event(channel['id'], connection, event(group='12345' if group else None, mention=group))
    prepare_outbound(channel['id'], connection)
    monkeypatch.setattr(worker, 'stream', lambda *a, **k: iter([text]))
    monkeypatch.setattr(worker.MemoryService, 'search', lambda *a: ([], False))
    # Shared memory extraction is unrelated to the reply synthesis job.
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job and job.kind == 'chat'
    worker.perform(job)
    action = prepare_outbound(channel['id'], connection)
    assert action['params']['message'][-1]['data']['text'] == text
    return channel, connection, action


def ack(channel, connection, action, retcode=0):
    receive_event(channel['id'], connection, {'echo': action['echo'], 'retcode': retcode,
                                             'data': {'message_id': -77}})
    with SessionLocal.begin() as db:
        db.get(Channel, channel['id']).last_sent = now()-3


def process():
    job = worker.claim()
    assert job and job.kind == 'channel_speech'
    worker.perform(job)
    return job


@pytest.fixture
def tts(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.path.endswith('/audio/speech')
        assert json.loads(request.content)['response_format'] == 'wav'
        return httpx.Response(200, content=wav())
    monkeypatch.setattr('lingxi.voice.client_for', lambda p: httpx.Client(base_url=p.base_url+'/',
                        transport=httpx.MockTransport(respond)))
    return calls


def record(db):
    return db.scalar(select(ChannelDelivery).where(ChannelDelivery.kind == 'record'))


def test_default_off_keeps_text_only(configured, monkeypatch, tts):
    channel, connection, action = setup(configured, monkeypatch, enabled=False)
    ack(channel, connection, action)
    assert prepare_outbound(channel['id'], connection) is None
    assert not tts
    with SessionLocal() as db:
        assert not record(db)
        assert not db.scalar(select(Job).where(Job.kind == 'channel_speech'))


@pytest.mark.parametrize('group', [False, True])
def test_full_text_then_one_encrypted_record_and_truthful_receipt(configured, monkeypatch, tts, group):
    channel, connection, text = setup(configured, monkeypatch, group=group)
    process()
    assert len(tts) == 1
    with SessionLocal() as db:
        output = record(db)
        assert base64.b64decode(decrypt(output.encrypted_audio)) == wav()
        # Random ciphertext may contain a short WAV/base64 marker by chance.
        # Check the complete stored representation rather than a four-byte fragment.
        assert output.encrypted_audio != base64.b64encode(wav()).decode('ascii')
        assert not db.scalar(select(VoiceSession)) and not db.scalar(select(VoicePlayback))
    # Audio never overtakes the accompanying text's receipt.
    assert prepare_outbound(channel['id'], connection) is None
    ack(channel, connection, text)
    audio = prepare_outbound(channel['id'], connection)
    assert audio['action'] == ('send_group_msg' if group else 'send_private_msg')
    segments = audio['params']['message']
    assert len(segments) == 1 and segments[0]['type'] == 'record'
    assert base64.b64decode(segments[0]['data']['file'][9:]) == wav()
    with SessionLocal() as db:
        assert record(db).encrypted_audio is None
        assert db.scalar(select(ChannelInbound)).status == 'ready'
    ack(channel, connection, audio)
    assert prepare_outbound(channel['id'], connection) is None
    records = configured.get('/api/channels/'+channel['id']+'/messages')
    assert records.json()[0]['status'] == 'completed'
    assert 'encrypted_audio' not in records.text and 'digest' not in records.text
    assert 'base64' not in records.text and '这是完整' not in records.text
    assert records.json()[0]['deliveries'][1]['kind'] == 'record'
    assert configured.get('/api/channels').json()[0]['capabilities']['record_send'] == 'accepted_by_protocol'
    assert len(tts) == 1


def test_long_reply_keeps_full_text_without_paid_truncation(configured, monkeypatch, tts):
    channel, connection, action = setup(configured, monkeypatch, text='长'*601)
    ack(channel, connection, action)
    assert not tts
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'skipped'
        assert '未截断' in record(db).error
        assert db.scalar(select(ChannelInbound)).status == 'completed'


@pytest.mark.parametrize('change', ['unverified', 'disabled', 'provider'])
def test_changed_config_before_paid_call_uses_text(configured, monkeypatch, tts, change):
    channel, connection, action = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        if change == 'unverified':
            db.get(VoiceConfig, 'owner').checks = {}
        elif change == 'disabled':
            row = db.get(VoiceConfig, 'owner')
            row.spec = row.spec | {'enabled': False}
        else:
            db.get(Provider, 'provider').version += 1
    process()
    ack(channel, connection, action)
    assert prepare_outbound(channel['id'], connection) is None
    assert not tts
    with SessionLocal() as db:
        assert record(db).status == 'cancelled' and record(db).encrypted_audio is None


@pytest.mark.parametrize('change', ['source', 'voice', 'rule'])
def test_inflight_source_or_config_change_discards_audio(configured, monkeypatch, change):
    channel, connection, action = setup(configured, monkeypatch)
    def call(*a, **k):
        with SessionLocal.begin() as db:
            if change == 'source':
                run = db.scalar(select(ChatRun))
                from lingxi.memory import invalidate_run
                invalidate_run(db, run)
            elif change == 'voice':
                db.get(VoiceConfig, 'owner').version += 1
            else:
                row = db.get(Channel, channel['id'])
                row.rules = row.rules | {'voice_reply_enabled': False}
        return wav()
    monkeypatch.setattr(speech, 'audio_call', call)
    process()
    with SessionLocal() as db:
        assert record(db).encrypted_audio is None and record(db).status == 'cancelled'


def test_recovered_calling_job_never_repeats_paid_call(configured, monkeypatch, tts):
    setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        row = record(db)
        row.speech = row.speech | {'phase': 'calling'}
    process()
    assert not tts
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'unknown'
        assert record(db).status == 'cancelled'


@pytest.mark.parametrize('failure', ['http', 'invalid', 'oversize', 'timeout'])
def test_tts_failure_keeps_text_no_retry_or_false_receipt(configured, monkeypatch, failure):
    channel, connection, action = setup(configured, monkeypatch)
    calls = []
    def respond(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('private-upstream-body')
        return httpx.Response(429 if failure == 'http' else 200,
            content=b'private-upstream-body' if failure != 'oversize' else b'x'*(6*1024**2+1))
    monkeypatch.setattr('lingxi.voice.client_for', lambda p: httpx.Client(base_url=p.base_url+'/',
                        transport=httpx.MockTransport(respond)))
    job = process()
    ack(channel, connection, action)
    worker.perform(job)
    assert len(calls) == 1
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'failed'
        assert record(db).encrypted_audio is None
        assert db.scalar(select(ChannelInbound)).status == 'completed'
    assert 'private-upstream-body' not in configured.get('/api/channels/'+channel['id']+'/messages').text


def test_audio_unknown_after_disconnect_never_resends(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    ack(channel, connection, text)
    audio = prepare_outbound(channel['id'], connection)
    close_connection(channel['id'], connection)
    replacement = connect(channel['id'])
    assert prepare_outbound(channel['id'], replacement) is None
    with SessionLocal() as db:
        assert record(db).status == 'unknown' and record(db).encrypted_audio is None
    # Only a receipt on the current connection can resolve current sends.
    ack(channel, connection, audio)
    with SessionLocal() as db:
        assert record(db).status == 'unknown'
    assert len(tts) == 1


@pytest.mark.parametrize('change', ['rule', 'source', 'memory', 'message', 'provider'])
def test_ready_audio_rechecks_at_send_boundary(configured, monkeypatch, tts, change):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    ack(channel, connection, text)
    with SessionLocal.begin() as db:
        run = db.scalar(select(ChatRun))
        if change == 'rule':
            row = db.get(Channel, channel['id'])
            row.rules = row.rules | {'voice_reply_enabled': False}
        elif change == 'source':
            from lingxi.memory import invalidate_run
            invalidate_run(db, run)
        elif change == 'memory':
            run.memory_refs = [{'id': 'deleted-memory', 'version': 1}]
        elif change == 'message':
            db.get(Message, run.source_message_ids[0]).status = 'deleted'
        else:
            db.get(Provider, 'provider').version += 1
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert record(db).encrypted_audio is None
        assert record(db).status == 'cancelled'


def test_offline_audio_expires_without_replay(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    close_connection(channel['id'], connection)
    with SessionLocal.begin() as db:
        record(db).created_at = now()-3601
    speech.sweep()
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'expired'
        assert record(db).encrypted_audio is None


def test_disabling_channel_clears_pending_waveform(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    configured.put('/api/channels/'+channel['id']+'/rules', json={**channel['rules'],
                    'version': channel['version'], 'voice_reply_enabled': False}).raise_for_status()
    with SessionLocal() as db:
        assert record(db).status == 'cancelled' and record(db).encrypted_audio is None
    assert prepare_outbound(channel['id'], connection) is None


def test_shared_identity_revocation_stops_paid_call(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch, shared=True)
    link = configured.get('/api/identity-links').json()[0]
    configured.post('/api/identity-links/'+link['id']+'/revoke', json={'version': link['version']}).raise_for_status()
    process()
    assert not tts
    with SessionLocal() as db:
        assert record(db).status == 'cancelled' and record(db).encrypted_audio is None


def test_shared_browser_and_qq_audio_budget_blocks_before_tts(configured, monkeypatch, tts):
    from lingxi.models import Audit
    setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        for _ in range(30):
            db.add(Audit(user_id='owner', action='voice_request'))
    process()
    assert not tts
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'failed'


def test_receipt_boolean_does_not_claim_protocol_acceptance(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    ack(channel, connection, text)
    audio = prepare_outbound(channel['id'], connection)
    ack(channel, connection, audio, retcode=False)
    with SessionLocal() as db:
        assert record(db).status == 'sending'
        assert not db.get(Channel, channel['id']).capabilities.get('record_send')


def test_late_text_receipt_does_not_resurrect_deleted_source(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    with SessionLocal.begin() as db:
        from lingxi.memory import invalidate_run
        invalidate_run(db, db.scalar(select(ChatRun)))
    ack(channel, connection, text)
    with SessionLocal() as db:
        assert db.scalar(select(ChannelInbound)).status == 'cancelled'
        assert record(db).encrypted_audio is None


def test_unknown_text_review_clears_ready_audio_without_sending(configured, monkeypatch, tts):
    channel, connection, text = setup(configured, monkeypatch)
    process()
    close_connection(channel['id'], connection)
    data = configured.get('/api/channels/'+channel['id']+'/messages').json()[0]
    configured.post('/api/channels/'+channel['id']+'/deliveries/'+data['deliveries'][0]['id']+'/review',
                    json={'acknowledgement': 'checked_no_resend'}).raise_for_status()
    with SessionLocal() as db:
        assert record(db).status == 'cancelled' and record(db).encrypted_audio is None


def test_lost_worker_lease_cannot_publish_old_tts_result(configured, monkeypatch):
    setup(configured, monkeypatch)
    def call(*a, **k):
        with SessionLocal.begin() as db:
            job = db.scalar(select(Job).where(Job.kind == 'channel_speech'))
            job.lease_token, job.lease_until = 'replacement-owner', now()+300
        return wav()
    monkeypatch.setattr(speech, 'audio_call', call)
    job = process()
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'calling' and record(db).encrypted_audio is None
    # The recovering owner sees the durable boundary and cannot reissue TTS.
    job.lease_token = 'replacement-owner'
    monkeypatch.setattr(speech, 'audio_call', lambda *a, **k: pytest.fail('must not call twice'))
    worker.perform(job)
    with SessionLocal() as db:
        assert record(db).speech['phase'] == 'unknown' and record(db).encrypted_audio is None


@pytest.mark.parametrize('older_backup', [False, True])
def test_restore_keeps_audio_dedupe_without_waveform_or_replay(configured, monkeypatch, tts, older_backup):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    channel, connection, text = setup(configured, monkeypatch)
    process()
    with SessionLocal.begin() as db:
        schema(db)
    latest = checkpoint()  # Credentialless QQ actors are not login owners.
    serialized = json.dumps(latest['channel_speech_receipts'], ensure_ascii=False)
    assert '这是完整' not in serialized and 'UklG' not in serialized and 'digest' not in serialized
    assert 'encrypted_audio": null' in serialized
    if older_backup:
        with SessionLocal.begin() as db:
            db.query(ChannelDelivery).delete()
            db.query(ChannelInbound).delete()
    quarantine(latest)
    with SessionLocal() as db:
        assert record(db).status == 'cancelled' and record(db).encrypted_audio is None
        assert not db.get(Channel, channel['id']).enabled
        assert db.scalar(select(ChannelInbound)).status == 'cancelled'
    # After explicit platform reauthorization, the replayed external message ID
    # still cannot create another run, synthesis job or send.
    with SessionLocal.begin() as db:
        row = db.get(Channel, channel['id'])
        row.enabled, row.login_state = True, 'online'
        row.connection_id, row.lease_until = connection, now()+90
    receive_event(channel['id'], connection, event())
    assert prepare_outbound(channel['id'], connection) is None
    assert len(tts) == 1
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChannelInbound)))) == 1


def test_restore_still_rejects_real_second_owner(configured, monkeypatch):
    from lingxi.backups import checkpoint
    from lingxi.models import User
    setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        db.add(User(username='another-owner', password_hash='real-login-hash'))
    with pytest.raises(ValueError, match='single-owner'):
        checkpoint()
