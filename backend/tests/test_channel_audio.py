import base64
import io
import json
import wave

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from lingxi import channel_audio as ca
from lingxi.db import SessionLocal, now
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from lingxi.channel_media import ChannelAttachment, cancel
from lingxi.channels import Channel, ChannelInbound, ChannelDelivery
from lingxi.media import Media, MediaLink, storage, forget_media
from lingxi.models import Audit, ChatRun, Job, Message, Provider, Conversation
from lingxi.security import decrypt
from lingxi.voice import VoiceConfig, VoiceSession, VoicePlayback
from lingxi.worker import claim, perform
from test_channels import create, rules, connect, bind, event, TOKEN
from test_voice import voice_config, wav

KEY = '0123456789abcdef0123456789abcdef.amr'


def setup(client, *, share=False, **extra):
    voice_config(client)
    channel = rules(client, create(client), voice_enabled=True, **({'allow_unbound': True} | extra))
    connection = connect(channel['id'])
    if share:
        bind(client, channel, connection, True)
    return channel, connection


def incoming(channel, connection, *, key=KEY, text='', **extra):
    e = event(text, **extra)
    e['message'].append({'type': 'record', 'data': {'file': key, 'path': '/private/ignored.amr',
                                                 'url': 'file:///never-read'}})
    receive_event(channel['id'], connection, e)
    return e


def response(channel, connection, action, *, raw=None, **extra):
    packet = {'echo': action['echo'], 'retcode': 0,
        'data': {'base64': base64.b64encode(wav() if raw is None else raw).decode(),
                 'file': '/never/read/server.wav', 'url': 'file:///never-read'}, **extra}
    receive_event(channel['id'], connection, packet)
    return packet


def received(client, **extra):
    channel, connection = setup(client, **extra)
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    assert action['action'] == 'get_record'
    response(channel, connection, action)
    return channel, connection, action


def asr(monkeypatch, text='明天的演示该怎样准备？'):
    calls = []
    def respond(request):
        calls.append(request)
        assert request.url.path.endswith('/audio/transcriptions')
        assert b'asr-model' in request.content and b'RIFF' in request.content
        return httpx.Response(200, json={'text': text})
    monkeypatch.setattr('lingxi.voice.client_for', lambda p: httpx.Client(base_url=p.base_url+'/',
                        transport=httpx.MockTransport(respond)))
    return calls


def process():
    job = claim()
    assert job
    perform(job)
    return job


@pytest.mark.parametrize('key', ['file:///etc/passwd', '/home/qq/a.amr', 'C:\\qq\\a.wav', 'https://cdn.qq.com/a',
    'base64://UklGRg==', '../a.amr', 'q'*32+'.amr', 'a'*32+'.txt', 'a'*31, 'a'*33, None, 1, 'a'*32+'.amr\n'])
def test_reject_paths_urls_and_arbitrary_record_keys(key):
    with pytest.raises(HTTPException):
        ca.record_id(key)


@pytest.mark.parametrize('key', ['a'*32, 'A'*32+'.amr', '1'*32+'.silk', 'f'*32+'.slk', '2'*32+'.wav'])
def test_accept_pinned_protocol_opaque_cache_keys(key):
    assert ca.record_id(key) == key


def test_default_off_no_audio_retention_or_asr(client):
    channel = rules(client, create(client), allow_unbound=True)
    connection = connect(channel['id'])
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    assert action['action'] == 'send_private_msg'
    assert '尚未开启' in action['params']['message'][0]['data']['text']
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment))
        assert not db.scalar(select(Media))
        assert not db.scalar(select(Job))


def test_no_conversion_without_verified_asr(configured):
    channel, connection = setup(configured)
    with SessionLocal.begin() as db:
        db.get(VoiceConfig, 'owner').checks = {}
    incoming(channel, connection)
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChannelInbound)).status == 'ready'
        assert db.scalar(select(ChannelAttachment)).encrypted_url == ''
        assert db.scalar(select(Media)).status == 'deleted'
        assert not db.scalar(select(Job).where(Job.kind == 'channel_audio'))


def test_gates_dedup_and_encrypted_key_precede_conversion(configured):
    channel, connection = setup(configured, group_enabled=True, group_ids=['12345'])
    incoming(channel, connection, group='12345')
    incoming(channel, connection, user='12345678')
    e = incoming(channel, connection, group='12345', mention=True, message_id=2)
    receive_event(channel['id'], connection, e)
    with SessionLocal() as db:
        rows = list(db.scalars(select(ChannelAttachment)))
        assert len(rows) == 1 and decrypt(rows[0].encrypted_url) == KEY
        assert KEY not in rows[0].encrypted_url
        assert not db.scalar(select(Media))
    action = prepare_outbound(channel['id'], connection)
    assert action['params'] == {'file': KEY, 'out_format': 'wav'}
    assert prepare_outbound(channel['id'], connection) is None  # no duplicate conversion


def test_unbound_gate_does_not_retain_record(configured):
    channel, connection = setup(configured, allow_unbound=False)
    incoming(channel, connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment))


def test_multiple_records_fail_without_partial_processing(configured):
    channel, connection = setup(configured)
    e = event('')
    e['message'] += [{'type': 'record', 'data': {'file': KEY}}] * 2
    receive_event(channel['id'], connection, e)
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment))
        assert '一段' in db.scalar(select(ChannelInbound)).error


def test_audio_to_scoped_media_to_actual_chat_no_memory_candidate(configured, monkeypatch):
    channel, connection, action = received(configured, share=True)
    calls = asr(monkeypatch)
    assert process().kind == 'channel_audio'
    assert len(calls) == 1
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment)); media = db.get(Media, row.media_id)
        assert row.encrypted_url == '' and 'echo' not in row.audio_state
        assert row.audio_state['phase'] == 'completed'
        assert media.user_id == 'owner' and media.status == 'ready'
        assert media.parsed['kind'] == 'audio'
        assert media.parsed['asr']['model'] == 'asr-model'
        assert storage(media.id, 'blob').read_bytes() == wav()
        assert not db.scalar(select(VoiceSession)) and not db.scalar(select(VoicePlayback))
    assert prepare_outbound(channel['id'], connection) is None
    captured, searches = [], []
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda self, db, user, q: searches.append(q) or [])
    def stream(*args, **kwargs):
        captured.extend(args[2]); yield '可以先准备演示大纲。'
    monkeypatch.setattr('lingxi.worker.stream', stream)
    assert process().kind == 'chat'
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun))
        assert run.status == 'completed' and run.user_id == 'owner'
        assert run.config['channel']['audio_unconfirmed'] is True
        assert run.config['media_refs'] and db.scalar(select(MediaLink))
        assert not db.scalar(select(Job).where(Job.kind == 'extract'))
        assert '明天' not in db.scalar(select(Message).where(Message.role == 'user')).content
    assert '明天的演示' in searches[0]
    assert '未经确认' in json.dumps(captured, ensure_ascii=False)
    result = prepare_outbound(channel['id'], connection)
    assert result['action'] == 'send_private_msg'
    assert '演示大纲' in result['params']['message'][0]['data']['text']
    receive_event(channel['id'], connection, {'echo': result['echo'], 'retcode': 0, 'data': {'message_id': 91}})
    assert configured.get('/api/channels/'+channel['id']+'/messages').json()[0]['status'] == 'completed'
    # An unsolicited replay cannot enqueue a second ASR or modify the file.
    response(channel, connection, action)
    assert len(calls) == 1


@pytest.mark.parametrize('group', [None, '12345'])
def test_nonsharing_and_group_audio_stay_isolated(configured, monkeypatch, group):
    channel, connection = setup(configured, group_enabled=True, group_ids=['12345'], share=bool(group))
    incoming(channel, connection, group=group, mention=bool(group))
    action = prepare_outbound(channel['id'], connection)
    response(channel, connection, action)
    asr(monkeypatch); process(); prepare_outbound(channel['id'], connection)
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun)); media = db.scalar(select(Media))
        assert run.user_id != 'owner' and media.user_id == run.user_id
        assert run.config['channel']['memory_allowed'] is False
        assert configured.get('/api/media/'+media.id).status_code == 404


@pytest.mark.parametrize('raw', [b'', b'SILK bad', b'RIFF1234WAVE', wav(60.1), wav(.05), b'x'*(6*1024**2+1)], ids=['empty','silk','incomplete','too-long','too-short','too-large'])
def test_invalid_or_overbudget_waveform_never_calls_asr(configured, raw):
    channel, connection = setup(configured)
    incoming(channel, connection); action = prepare_outbound(channel['id'], connection)
    response(channel, connection, action, raw=raw)
    with SessionLocal() as db:
        assert not db.scalar(select(Job).where(Job.kind == 'channel_audio'))
        assert db.scalar(select(ChannelInbound)).error
        assert db.scalar(select(Media)).status == 'deleted'


@pytest.mark.parametrize('variant', ['retcode', 'missing_base64', 'invalid_base64', 'wrong_echo', 'old_connection'])
def test_protocol_reply_validation(configured, variant):
    channel, connection = setup(configured)
    incoming(channel, connection); action = prepare_outbound(channel['id'], connection)
    packet = {'echo': action['echo'], 'retcode': 0, 'data': {'base64': base64.b64encode(wav()).decode()}}
    if variant == 'retcode': packet['retcode'] = 1400
    if variant == 'missing_base64': packet['data'] = {'url': 'file:///etc/passwd', 'file': '/server/file'}
    if variant == 'invalid_base64': packet['data'] = {'base64': '!*'}
    if variant == 'wrong_echo': packet['echo'] = connection+':record:other'
    receive_event(channel['id'], 'stale' if variant == 'old_connection' else connection, packet)
    with SessionLocal() as db:
        assert not db.scalar(select(Job).where(Job.kind == 'channel_audio'))
        assert not storage(db.scalar(select(Media)).id, 'blob').exists()


@pytest.mark.parametrize('change', ['disable', 'revoke', 'delete_conversation', 'delete_media', 'voice_config', 'provider'])
def test_source_change_before_and_during_paid_call_blocks_results(configured, monkeypatch, change):
    channel, connection, _ = received(configured, share=True)
    def mutate(*args, **kwargs):
        with SessionLocal.begin() as db:
            if change == 'disable': db.get(Channel, channel['id']).enabled = False
            elif change == 'revoke':
                from lingxi.channels import IdentityLink
                link = db.scalar(select(IdentityLink)); link.status = 'revoked'; link.version += 1
            elif change == 'delete_conversation': db.scalar(select(Conversation)).deleted = True
            elif change == 'delete_media': forget_media(db, db.scalar(select(Media)))
            elif change == 'voice_config': db.get(VoiceConfig, 'owner').version += 1
            else: db.get(Provider, 'provider').version += 1
        return '不应继续使用的转写'
    monkeypatch.setattr(ca, 'audio_call', mutate)
    process()
    prepare_outbound(channel['id'], connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert db.scalar(select(Media)).status == 'deleted'
        assert '不应继续' not in json.dumps(db.scalar(select(Media)).parsed)


def test_deleted_before_worker_does_not_start_asr(configured, monkeypatch):
    channel, connection, _ = received(configured)
    calls = asr(monkeypatch)
    with SessionLocal.begin() as db:
        forget_media(db, db.scalar(select(Media)))
    process()
    assert not calls


def test_deleted_after_chat_invalidates_unsent_reply(configured, monkeypatch):
    channel, connection, _ = received(configured, share=True)
    asr(monkeypatch); process(); prepare_outbound(channel['id'], connection)
    monkeypatch.setattr('lingxi.worker.stream', lambda *a, **k: iter(['不应发送的旧结果']))
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *a, **k: [])
    process()
    with SessionLocal.begin() as db:
        media = db.scalar(select(Media)); forget_media(db, media)
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChatRun)).status == 'invalidated'
        assert not db.scalar(select(ChannelDelivery).where(ChannelDelivery.status == 'pending'))


def test_paid_boundary_survives_worker_crash_without_replay(configured, monkeypatch):
    channel, connection, _ = received(configured)
    calls = asr(monkeypatch)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ChannelAttachment)); row.audio_state = row.audio_state | {'phase': 'calling'}
    process()
    assert not calls
    with SessionLocal() as db:
        assert '结果未知' in db.scalar(select(ChannelInbound)).error
        assert db.scalar(select(Media)).status == 'deleted'


def test_asr_timeout_not_retried_or_disclosed(configured, monkeypatch):
    channel, connection, _ = received(configured)
    calls = []
    def error(*a, **k):
        calls.append(1); raise RuntimeError('secret URL /path transcript')
    monkeypatch.setattr(ca, 'audio_call', error)
    job = process()
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == 'completed'  # handled domain failure
        assert '未自动重试' in db.scalar(select(ChannelInbound)).error
    assert len(calls) == 1
    assert 'secret' not in json.dumps(configured.get('/api/channels/'+channel['id']+'/messages').json())


def test_global_audio_budget_covers_browser_and_qq(configured, monkeypatch):
    channel, connection, _ = received(configured)
    calls = asr(monkeypatch)
    with SessionLocal.begin() as db:
        for _ in range(30): db.add(Audit(user_id='owner', action='voice_request', target_id=''))
    process()
    assert not calls


@pytest.mark.parametrize('condition', ['connection', 'timeout', 'expired_key'])
def test_conversion_deadline_or_disconnect_never_replays(configured, condition):
    channel, connection = setup(configured)
    incoming(channel, connection)
    if condition == 'expired_key':
        with SessionLocal.begin() as db: db.scalar(select(ChannelAttachment)).expires_at = now()-1
        assert prepare_outbound(channel['id'], connection) is None
    else:
        action = prepare_outbound(channel['id'], connection)
        if condition == 'connection': close_connection(channel['id'], connection)
        else:
            with SessionLocal.begin() as db:
                row = db.scalar(select(ChannelAttachment)); row.audio_state = row.audio_state | {'sent_at': now()-61}
            prepare_outbound(channel['id'], connection)
        response(channel, connection, action)
    with SessionLocal() as db:
        assert db.scalar(select(ChannelAttachment)).encrypted_url == ''
        assert not db.scalar(select(Job).where(Job.kind == 'channel_audio'))


def test_authenticated_socket_large_record_reply_but_not_large_messages(configured):
    voice_config(configured)
    channel = rules(configured, create(configured), voice_enabled=True, allow_unbound=True)
    with configured.websocket_connect('/gateway/onebot/v11/ws/'+channel['id'], headers={
            'Authorization': 'Bearer '+TOKEN, 'X-Self-ID': '12345678'}) as ws:
        status = ws.receive_json(); ws.receive_json()
        ws.send_json({'echo': status['echo'], 'retcode': 0, 'data': {'online': True}})
        e = event(''); e['message'].append({'type': 'record', 'data': {'file': KEY}})
        ws.send_json(e)
        action = ws.receive_json(); assert action['action'] == 'get_record'
        ws.send_json({'echo': action['echo'], 'retcode': 0, 'data': {'base64': base64.b64encode(wav(2)).decode()}})
        ws.send_json(event('a'*65536, message_id=2))
        with pytest.raises(WebSocketDisconnect) as exc: ws.receive_json()
        assert exc.value.code == 1009
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.kind == 'channel_audio'))
        assert db.scalar(select(Media)).size == len(wav(2))


def test_typed_text_plus_unconfirmed_voice_does_not_auto_extract(configured, monkeypatch):
    channel, connection = setup(configured, share=True)
    incoming(channel, connection, text='我喜欢茶，请结合语音回答')
    action = prepare_outbound(channel['id'], connection)
    response(channel, connection, action)
    asr(monkeypatch); process(); prepare_outbound(channel['id'], connection)
    with SessionLocal() as db:
        assert db.scalar(select(ChatRun)).config['channel']['audio_unconfirmed']
        assert not db.scalar(select(Job).where(Job.kind == 'extract'))


def test_mixed_document_failure_never_ignores_audio(configured, monkeypatch):
    channel, connection = setup(configured, attachments_enabled=True, attachment_hosts=['cdn.example.com'])
    e = event('请综合回答')
    e['message'] += [{'type': 'record', 'data': {'file': KEY}},
                     {'type': 'file', 'data': {'name': '资料.md', 'url': 'https://cdn.example.com/a'}}]
    receive_event(channel['id'], connection, e)
    action = prepare_outbound(channel['id'], connection)
    assert action['action'] == 'get_record'
    response(channel, connection, action)
    def failed(*a, **k): raise RuntimeError('secret origin')
    monkeypatch.setattr('lingxi.channel_media.download', failed)
    assert process().kind == 'channel_media'
    asr_calls = asr(monkeypatch)
    while job := claim(): perform(job)
    prepare_outbound(channel['id'], connection)
    assert not asr_calls
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert all(m.status == 'deleted' for m in db.scalars(select(Media)))


def test_one_target_waits_for_audio_and_other_target_proceeds(configured):
    channel, connection = setup(configured)
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    receive_event(channel['id'], connection, event('后续同会话', message_id=2))
    receive_event(channel['id'], connection, event('另一位', user='98765432', message_id=3))
    prepare_outbound(channel['id'], connection)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChatRun)))) == 1
        assert db.scalar(select(ChannelInbound).where(ChannelInbound.external_message_id == '2')).status == 'queued'
        assert db.scalar(select(ChannelInbound).where(ChannelInbound.external_message_id == '3')).status == 'running'
    assert action['action'] == 'get_record'


def test_configuration_changed_before_paid_call_discards_audio(configured, monkeypatch):
    channel, connection, _ = received(configured)
    calls = asr(monkeypatch)
    with SessionLocal.begin() as db: db.get(Provider, 'provider').version += 1
    process()
    assert not calls
    with SessionLocal() as db:
        assert db.scalar(select(Media)).status == 'deleted'


def test_large_unrelated_echo_not_accepted_as_an_audio_source(configured):
    channel, connection = setup(configured)
    receive_event(channel['id'], connection, {'echo': connection+':record:unknown', 'retcode': 0,
        'data': {'base64': base64.b64encode(wav(2)).decode()}})
    with SessionLocal() as db:
        assert not db.scalar(select(Media))
        assert not db.scalar(select(Job))


def test_replayed_extraction_outbox_cannot_convert_asr_message_to_fact(configured, monkeypatch):
    channel, connection = setup(configured, share=True)
    incoming(channel, connection, text='我喜欢喝茶。')
    action = prepare_outbound(channel['id'], connection); response(channel, connection, action)
    asr(monkeypatch); process(); prepare_outbound(channel['id'], connection)
    with SessionLocal.begin() as db:
        message = db.scalar(select(Message).where(Message.role == 'user'))
        from lingxi.memory import enqueue
        enqueue(db, 'extract:'+message.id, 'extract', {'message_id': message.id})
    def forbidden(*a, **k): raise AssertionError('unconfirmed audio must not be extracted')
    monkeypatch.setattr('lingxi.worker.complete', forbidden)
    from lingxi.worker import extract
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == 'extract'))
    extract(job)
    from lingxi.models import Candidate
    with SessionLocal() as db: assert not db.scalar(select(Candidate))
