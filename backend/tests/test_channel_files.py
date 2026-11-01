import base64
import json

import pytest
from sqlalchemy import select

from lingxi import channel_files as files, worker
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from lingxi.channels import Channel, ChannelDelivery, ChannelInbound
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Conversation, Memory, Message
from test_channels import create, rules, connect, event, bind

LONG = '# 完整回答\n' + '中文 Markdown 内容，不包含额外历史。\n' * 320


def setup(client, monkeypatch, text=LONG, enabled=True, group=False, shared=False):
    channel = rules(client, create(client), allow_unbound=True, file_reply_enabled=enabled,
                    group_enabled=group, group_ids=['12345'] if group else [])
    connection = connect(channel['id'])
    if shared:
        bind(client, channel, connection, True)
    receive_event(channel['id'], connection, event(group='12345' if group else None, mention=group))
    prepare_outbound(channel['id'], connection)
    monkeypatch.setattr(worker, 'stream', lambda *a, **k: iter([text]))
    monkeypatch.setattr(worker.MemoryService, 'search', lambda *a: ([], False))
    with SessionLocal.begin() as db:
        from lingxi.models import Job
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job and job.kind == 'chat'
    worker.perform(job)
    action = prepare_outbound(channel['id'], connection)
    assert action and action['action'] == ('send_group_msg' if group else 'send_private_msg')
    return channel, connection, action


def output(db):
    return db.scalar(select(ChannelDelivery).where(ChannelDelivery.kind == 'file'))


def ack(channel, connection, action, **extra):
    receive_event(channel['id'], connection, {'echo': action['echo'], 'status': 'ok', 'retcode': 0,
        'data': {'file_id': '/synthetic-file-uuid'} if action['action'].startswith('upload_') else {'message_id': -7}, **extra})
    with SessionLocal.begin() as db:
        db.get(Channel, channel['id']).last_sent = now()-3


@pytest.mark.parametrize('group', [False, True])
def test_file_bytes_and_upload_receipt_are_distinct_from_message_id(configured, monkeypatch, group):
    channel, connection, intro = setup(configured, monkeypatch, group=group)
    assert '完整内容将另附' in intro['params']['message'][-1]['data']['text']
    assert prepare_outbound(channel['id'], connection) is None
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    assert upload['action'] == ('upload_group_file' if group else 'upload_private_file')
    assert upload['params']['group_id' if group else 'user_id'] == ('12345' if group else '87654321')
    assert base64.b64decode(upload['params']['file'][9:]).decode() == LONG
    assert upload['params']['upload_file'] is True
    assert upload['params']['name'].startswith('lingxi-answer-') and upload['params']['name'].endswith('.md')
    assert not any(k in upload['params'] for k in ('url', 'folder', 'message'))
    ack(channel, connection, upload)
    assert prepare_outbound(channel['id'], connection) is None
    records = configured.get('/api/channels/'+channel['id']+'/messages')
    assert records.json()[0]['status'] == 'completed'
    row = records.json()[0]['deliveries'][1]
    assert row['protocol_message_id'] is None and row['protocol_file_id'] == '/synthetic-file-uuid'
    assert row['file_info']['bytes'] == len(LONG.encode())
    assert not any(value in records.text for value in ('base64', '完整回答', 'digest', 'message_id": "'))
    assert configured.get('/api/channels').json()[0]['capabilities']['file_send'] == 'accepted_by_protocol'
    with SessionLocal() as db:
        assert output(db).content == '' and output(db).encrypted_audio is None


@pytest.mark.parametrize('text,enabled', [('短回答', True), ('中'*6000, True), (LONG, False)], ids=['short','boundary','disabled'])
def test_default_off_and_short_replies_keep_text(configured, monkeypatch, text, enabled):
    setup(configured, monkeypatch, text=text, enabled=enabled)
    with SessionLocal() as db:
        assert not output(db)
        assert ''.join(d.content for d in db.scalars(select(ChannelDelivery)))


@pytest.mark.parametrize('data', [{}, {'file_id': None}, {'file_id': ''}, {'file_id': 123},
    {'message_id': 77}, {'file_id': 'bad\nfile'}, {'file_id': 'x'*201}])
def test_missing_file_id_is_unknown_not_success_or_retry(configured, monkeypatch, data):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    ack(channel, connection, upload, data=data)
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert output(db).status == 'unknown'
        assert db.scalar(select(ChannelInbound)).status == 'unknown'
    # A valid late receipt can resolve uncertainty without another upload.
    ack(channel, connection, upload)
    with SessionLocal() as db:
        assert output(db).status == 'accepted_by_protocol'
        assert db.scalar(select(ChannelInbound)).status == 'completed'


@pytest.mark.parametrize('change', ['reply', 'name', 'digest', 'source', 'conversation', 'scope', 'workspace'])
def test_rechecks_source_before_send(configured, monkeypatch, change):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    with SessionLocal.begin() as db:
        delivery = output(db)
        run = db.scalar(select(ChatRun))
        if change == 'reply':
            db.get(Message, delivery.file_info['message_id']).content += '伪造'
        elif change in ('name', 'digest'):
            delivery.file_info = delivery.file_info | {change: '../../private.key' if change == 'name' else '0'*64}
        elif change == 'source':
            db.get(Message, run.source_message_ids[0]).status = 'deleted'
        elif change == 'conversation':
            db.get(Conversation, run.conversation_id).deleted = True
        elif change == 'scope':
            run.config = run.config | {'channel': run.config['channel'] | {'target_key': 'private:99999999'}}
        else:
            delivery.workspace_id = 'another-workspace'
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert output(db).status == 'cancelled'
        assert db.scalar(select(ChannelInbound)).status == 'failed'


@pytest.mark.parametrize('change', ['memory', 'rules', 'binding'])
def test_revocation_cancels_pending_file(configured, monkeypatch, change):
    channel, connection, intro = setup(configured, monkeypatch, shared=change == 'binding')
    ack(channel, connection, intro)
    with SessionLocal.begin() as db:
        if change == 'memory':
            memory = Memory(user_id='owner', content='测试依据', category='preferences', source_ids=[])
            db.add(memory); db.flush()
            run = db.scalar(select(ChatRun)); run.memory_refs = [{'id': memory.id, 'version': memory.version}]
            from lingxi.memory import MemoryService
            MemoryService().forget(db, memory)
    if change == 'rules':
        rules(configured, channel, file_reply_enabled=False)
    elif change == 'binding':
        link = configured.get('/api/identity-links').json()[0]
        assert configured.post('/api/identity-links/'+link['id']+'/revoke', json={'version': link['version']}).status_code == 200
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert output(db).status == 'cancelled'


def test_disconnect_timeout_and_review_never_resend(configured, monkeypatch):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    with SessionLocal.begin() as db:
        output(db).sent_at = now()-31
    assert prepare_outbound(channel['id'], connection) is None
    close_connection(channel['id'], connection)
    connection = connect(channel['id'])
    receive_event(channel['id'], connection, event(message_id=2))
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        identity = output(db).id
        assert output(db).status == 'unknown'
    result = configured.post('/api/channels/'+channel['id']+'/deliveries/'+identity+'/review', json={'acknowledgement': 'checked_no_resend'})
    assert result.status_code == 200 and result.json()['status'] == 'unknown'
    assert prepare_outbound(channel['id'], connection) is None  # second message only starts generation
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChatRun)))) == 2
        assert output(db).status == 'unknown' and output(db).reviewed_at


def test_failure_does_not_claim_complete_and_duplicate_event_does_not_upload(configured, monkeypatch):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    ack(channel, connection, upload, retcode=1404, data={'message': 'private-service-url-secret'})
    receive_event(channel['id'], connection, event())
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert output(db).status == 'failed' and 'secret' not in output(db).error
        assert len(list(db.scalars(select(ChannelInbound)))) == 1
        assert db.scalar(select(ChannelInbound)).status == 'failed'


def test_over_budget_is_honest_and_does_not_truncate_or_upload(configured, monkeypatch):
    monkeypatch.setattr(files, 'MAX_BYTES', 100)
    channel, connection, intro = setup(configured, monkeypatch)
    assert '未完整发送' in intro['params']['message'][-1]['data']['text']
    ack(channel, connection, intro)
    with SessionLocal() as db:
        assert not output(db)
        assert db.scalar(select(ChannelInbound)).status == 'failed'


@pytest.mark.parametrize('sent', [False, True])
def test_old_backup_retains_file_receipt_and_external_message_dedupe(configured, monkeypatch, sent):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    if sent:
        upload = prepare_outbound(channel['id'], connection)
        ack(channel, connection, upload)
    with SessionLocal.begin() as db:
        schema(db)
    latest = checkpoint()
    serialized = json.dumps(latest['channel_file_receipts'], ensure_ascii=False)
    assert all(s not in serialized for s in ('base64', '完整回答', 'digest', 'run_id": "'))
    with SessionLocal.begin() as db:
        db.query(ChannelDelivery).delete(); db.query(ChannelInbound).delete()
    quarantine(latest)
    with SessionLocal.begin() as db:
        assert output(db).status == ('accepted_by_protocol' if sent else 'cancelled')
        assert not db.get(Channel, channel['id']).enabled
        row = db.get(Channel, channel['id'])
        row.enabled, row.login_state, row.connection_id, row.lease_until = True, 'online', connection, now()+90
    receive_event(channel['id'], connection, event())
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChannelInbound)))) == 1


def test_wrong_echo_boolean_retcode_and_non_ok_status_are_not_receipts(configured, monkeypatch):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    ack(channel, connection, upload, echo='old:'+upload['echo'])
    ack(channel, connection, upload, retcode=False)
    with SessionLocal() as db:
        assert output(db).status == 'sending'
    ack(channel, connection, upload, status='failed')
    with SessionLocal() as db:
        assert output(db).status == 'unknown'


def test_intro_acceptance_does_not_expose_unaccepted_answer_in_history(configured, monkeypatch):
    from lingxi.conversation_context import visible_history
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun))
        reply = db.get(Message, output(db).file_info['message_id'])
        assert reply not in visible_history(db, run)
    upload = prepare_outbound(channel['id'], connection)
    ack(channel, connection, upload)
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun))
        reply = db.get(Message, output(db).file_info['message_id'])
        assert reply in visible_history(db, run)


def test_expired_file_is_not_uploaded_on_reconnect(configured, monkeypatch):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    with SessionLocal.begin() as db:
        incoming = db.scalar(select(ChannelInbound))
        incoming.created_at = now()-3601
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert output(db).status == 'cancelled'


def test_inflight_upload_truth_survives_source_deletion(configured, monkeypatch):
    channel, connection, intro = setup(configured, monkeypatch)
    ack(channel, connection, intro)
    upload = prepare_outbound(channel['id'], connection)
    with SessionLocal.begin() as db:
        from lingxi.memory import invalidate_run
        invalidate_run(db, db.scalar(select(ChatRun)))
    ack(channel, connection, upload)
    with SessionLocal() as db:
        assert output(db).status == 'accepted_by_protocol'
        assert db.scalar(select(ChannelInbound)).status == 'cancelled'
        assert db.get(Message, output(db).file_info['message_id']).status == 'invalidated'


def test_public_endpoints_and_foreign_channel_cannot_observe_file_receipts(configured, monkeypatch):
    from fastapi.testclient import TestClient
    from lingxi.api import app
    channel, _, _ = setup(configured, monkeypatch)
    with TestClient(app) as stranger:
        assert stranger.get('/api/channels/'+channel['id']+'/messages').status_code == 401
    with SessionLocal() as db:
        identity = output(db).id
    assert configured.get('/api/channels/foreign/messages').status_code == 404
    assert configured.post('/api/channels/foreign/deliveries/'+identity+'/review',
        json={'acknowledgement': 'checked_no_resend'}).status_code == 404
