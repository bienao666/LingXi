"""Pinned NapCat document lookup; protocol fixtures do not prove real QQ IO."""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lingxi import channel_documents as cd
from lingxi.db import SessionLocal, now
from lingxi.channel_media import ChannelAttachment
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from lingxi.channels import Channel, ChannelInbound, IdentityLink
from lingxi.media import Media, MAX_FILE, MAX_TOTAL, storage
from lingxi.models import ChatRun, Job, Conversation
from lingxi.security import decrypt
from lingxi.worker import claim
from test_channels import event, connect, rules, create
from test_channel_media import setup, downloader, process, URL, HOST

KEY = '/synthetic-file-UUID_0123456789abcdef'


def incoming(channel, connection, *, data=None, **extra):
    e = event('请概括文档', **extra)
    e['message'].append({'type': 'file', 'data': data if data is not None else
        {'file': '证据.md', 'file_id': KEY, 'file_size': '4', 'path': '/never/read'}})
    receive_event(channel['id'], connection, e)
    return e


def response(channel, connection, action, **extra):
    packet = {'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'url': URL}, **extra}
    receive_event(channel['id'], connection, packet)
    return packet


@pytest.mark.parametrize('key', ['a'*32, '/aaa-bbb_ccc=', 'file_id_123'])
def test_bounded_opaque_protocol_key(key):
    assert cd.file_id(key) == key


@pytest.mark.parametrize('key', [None, 1, '', 'a'*513, 'https://test/a', 'file:///etc/passwd',
    'C:\\x', '../a', '//server/file', 'a b', 'a\n', 'a\x00', '[CQ:file,id=1]'])
def test_invalid_ids_never_become_protocol_search_or_local_io(key):
    with pytest.raises(HTTPException):
        cd.file_id(key)


@pytest.mark.parametrize('size', [0, -1, True, 4.0, '4.0', '1'*100, MAX_FILE+1, 'NaN'])
def test_invalid_declared_sizes_fail_before_lookup(client, size):
    channel, connection = setup(client)
    incoming(channel, connection, data={'file': 'a.txt', 'file_id': KEY, 'file_size': size})
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment)) and not db.scalar(select(Media))
        assert db.scalar(select(ChannelInbound)).error


def test_default_off_and_unbound_filter_before_key_retention(client):
    channel = rules(client, create(client), allow_unbound=True)
    connection = connect(channel['id'])
    incoming(channel, connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment))
    assert prepare_outbound(channel['id'], connection)['action'] == 'send_private_msg'


@pytest.mark.parametrize('group', [None, '12345'])
def test_exact_protocol_parameters_and_encrypted_transport(client, group):
    channel, connection = setup(client, group_enabled=True, group_ids=['12345'])
    e = incoming(channel, connection, group=group, mention=bool(group))
    receive_event(channel['id'], connection, e)
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert row.filename == '证据.md' and decrypt(row.encrypted_url) == KEY
        assert KEY not in row.encrypted_url and len(list(db.scalars(select(ChannelAttachment)))) == 1
    action = prepare_outbound(channel['id'], connection)
    assert action['action'] == ('get_group_file_url' if group else 'get_private_file_url')
    assert action['params'] == ({'file_id': KEY, 'group_id': group} if group else {'file_id': KEY})
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert row.status == 'resolving' and row.document_state['phase'] == 'requested'
        assert row.document_state['echo'] == action['echo']
        assert not db.scalar(select(Job).where(Job.kind == 'channel_media'))
    assert prepare_outbound(channel['id'], connection) is None
    response(channel, connection, action)
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert row.document_state == {'phase': 'url_ready', 'declared_size': 4}
        assert decrypt(row.encrypted_url) == URL and URL not in row.encrypted_url
    public = client.get('/api/channels/'+channel['id']+'/messages').text
    assert KEY not in public and 'signature' not in public and '证据' not in public


def test_protocol_to_worker_parser_chat_and_delete_fence(configured, monkeypatch):
    channel, connection = setup(configured, share=True)
    calls = downloader(monkeypatch, b'data')
    incoming(channel, connection, message_id=2)
    action = prepare_outbound(channel['id'], connection)
    packet = response(channel, connection, action)
    prepare_outbound(channel['id'], connection)
    assert process().kind == 'channel_media'
    assert process().kind == 'media_parse'
    assert calls == [URL]
    receive_event(channel['id'], connection, packet)
    prepare_outbound(channel['id'], connection)
    captured = []
    def stream(*args, **kwargs):
        captured.extend(args[2]); yield '文档写有 data。'
    monkeypatch.setattr('lingxi.worker.stream', stream)
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *a: [])
    process()
    assert 'data' in json.dumps(captured) and KEY not in json.dumps(captured) and URL not in json.dumps(captured)
    with SessionLocal() as db:
        media = db.scalar(select(Media))
        identity, version = media.id, media.version
        assert media.user_id == 'owner' and media.status == 'ready'
        assert db.scalar(select(ChatRun)).status == 'completed'
        assert storage(identity, 'blob').read_bytes() == b'data'
        assert db.scalar(select(ChannelAttachment)).encrypted_url == ''
    assert configured.request('DELETE', '/api/media/'+identity, json={'version': version}).status_code == 200
    assert prepare_outbound(channel['id'], connection) is None


def test_napcat_file_name_with_direct_url_does_not_resolve(client, monkeypatch):
    channel, connection = setup(client)
    calls = downloader(monkeypatch, b'data')
    incoming(channel, connection, data={'file': '原文件.txt', 'file_id': KEY, 'url': URL, 'file_size': 4})
    assert prepare_outbound(channel['id'], connection) is None
    process(); process()
    assert calls == [URL]


@pytest.mark.parametrize('variant', ['retcode', 'bool', 'missing_status', 'wrong_status', 'missing_url',
    'local_path', 'http', 'unallowed', 'url_userinfo', 'fragment', 'bad_data', 'expired'])
def test_failed_lookup_clears_transient_data_and_never_downloads(client, variant):
    channel, connection = setup(client)
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    extra = {}
    if variant == 'retcode': extra['retcode'] = 1400
    if variant == 'bool': extra['retcode'] = False
    if variant == 'missing_status': extra['status'] = None
    if variant == 'wrong_status': extra['status'] = 'failed'
    urls = {'missing_url': None, 'local_path': 'file:///private/file.txt', 'http': 'http://'+HOST+'/a',
        'unallowed': 'https://elsewhere.example.com/a', 'url_userinfo': 'https://user:pass@'+HOST+'/a',
        'fragment': URL+'#x'}
    if variant in urls: extra['data'] = {'url': urls[variant], 'file': '/never/read.txt', 'base64': 'ZGF0YQ=='}
    if variant == 'bad_data': extra['data'] = []
    if variant == 'expired':
        with SessionLocal.begin() as db:
            row = db.scalar(select(ChannelAttachment))
            row.document_state = row.document_state | {'sent_at': now()-61}
    response(channel, connection, action, **extra)
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert row.status == 'failed' and not row.encrypted_url and 'echo' not in row.document_state
        assert db.scalar(select(Media)).status == 'deleted'
        assert not db.scalar(select(Job).where(Job.kind.in_(['channel_media', 'chat', 'media_parse'])))
    assert URL not in client.get('/api/channels/'+channel['id']+'/messages').text


@pytest.mark.parametrize('variant', ['wrong_echo', 'wrong_connection', 'other_channel'])
def test_foreign_reply_cannot_resolve_input(client, variant):
    channel, connection = setup(client)
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    if variant == 'wrong_echo': action = action | {'echo': connection+':document:foreign'}
    if variant == 'wrong_connection': connection = 'foreign'
    if variant == 'other_channel': channel = create(client, bot_id='99999999')
    response(channel, connection, action)
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert row.document_state['phase'] == 'requested' and decrypt(row.encrypted_url) == KEY
        assert not db.scalar(select(Job).where(Job.kind.in_(['channel_media', 'chat', 'media_parse'])))


@pytest.mark.parametrize('change', ['disable', 'rules', 'binding', 'conversation', 'media'])
def test_source_invalidations_before_receipt_do_not_resurrect(configured, change):
    channel, connection = setup(configured, share=True)
    incoming(channel, connection, message_id=2)
    action = prepare_outbound(channel['id'], connection)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ChannelAttachment))
        if change == 'disable':
            db.get(Channel, channel['id']).enabled = False
            from lingxi.channels import invalidate_channel_runs
            invalidate_channel_runs(db, channel['id'])
        if change == 'rules': db.get(Channel, channel['id']).version += 1
        if change == 'binding': db.scalar(select(IdentityLink)).version += 1
        if change == 'conversation': db.get(Conversation, row.conversation_id).deleted = True
        if change == 'media': db.get(Media, row.media_id).status = 'deleted'
    response(channel, connection, action)
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert not db.scalar(select(Job).where(Job.kind.in_(['channel_media', 'chat', 'media_parse']))) and not db.scalar(select(ChatRun))
        assert not row.encrypted_url
        if change != 'disable': assert db.scalar(select(Media)).status == 'deleted'


@pytest.mark.parametrize('after_resolution', [False, True])
def test_disconnect_does_not_repeat_lookup_but_completed_lookup_can_resume(client, monkeypatch, after_resolution):
    channel, connection = setup(client)
    calls = downloader(monkeypatch, b'data')
    e = incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    if after_resolution: response(channel, connection, action)
    close_connection(channel['id'], connection)
    connection = connect(channel['id'])
    receive_event(channel['id'], connection, e)
    action = prepare_outbound(channel['id'], connection)
    if after_resolution:
        assert action is None
        process(); process()
        assert calls == [URL]
    else:
        assert action['action'] == 'send_private_msg'
        assert calls == []
        with SessionLocal() as db:
            assert not db.scalar(select(Job).where(Job.kind.in_(['channel_media', 'chat'])))


def test_timeout_leaves_explicit_failure_instead_of_retry(client):
    channel, connection = setup(client)
    incoming(channel, connection)
    prepare_outbound(channel['id'], connection)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ChannelAttachment))
        row.document_state = row.document_state | {'sent_at': now()-61}
    assert prepare_outbound(channel['id'], connection) is None
    assert prepare_outbound(channel['id'], connection)['action'] == 'send_private_msg'
    with SessionLocal() as db:
        assert db.scalar(select(ChannelAttachment)).encrypted_url == ''
        assert '超时' in db.scalar(select(ChannelInbound)).error


def test_mixed_documents_wait_for_all_urls_before_single_download_job(client, monkeypatch):
    channel, connection = setup(client)
    calls = downloader(monkeypatch, b'data')
    e = event('概括三个文件')
    e['message'] += [{'type': 'file', 'data': {'file': 'a.md', 'file_id': KEY, 'file_size': 4}},
        {'type': 'file', 'data': {'name': 'b.txt', 'url': URL}},
        {'type': 'file', 'data': {'file': 'c.csv', 'file_id': 'another-key', 'size': 4}}]
    receive_event(channel['id'], connection, e)
    first = prepare_outbound(channel['id'], connection)
    second = prepare_outbound(channel['id'], connection)
    assert first['params']['file_id'] == KEY and second['params']['file_id'] == 'another-key'
    response(channel, connection, first)
    assert prepare_outbound(channel['id'], connection) is None
    assert claim() is None
    response(channel, connection, second)
    prepare_outbound(channel['id'], connection)
    process()
    assert calls == [URL]*3
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Job).where(Job.kind == 'channel_media')))) == 1


def test_declared_total_and_actual_size_mismatch_never_chat(configured, monkeypatch):
    channel, connection = setup(configured)
    e = event()
    e['message'] += [{'type': 'file', 'data': {'file': 'a.txt', 'file_id': KEY, 'file_size': MAX_FILE}}]*3
    receive_event(channel['id'], connection, e)
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelAttachment))
        assert '50 MiB' in db.scalar(select(ChannelInbound)).error
    downloader(monkeypatch, b'not-four')
    incoming(channel, connection, message_id=2)
    action = prepare_outbound(channel['id'], connection)
    # First error may be delivered while the other input is staged.
    if action['action'] != 'get_private_file_url':
        receive_event(channel['id'], connection, {'echo': action['echo'], 'retcode': 0, 'data': {'message_id': 5}})
        action = prepare_outbound(channel['id'], connection)
    response(channel, connection, action)
    prepare_outbound(channel['id'], connection); process()
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert db.scalar(select(Media)).status == 'deleted'
        assert not db.scalar(select(Job).where(Job.kind == 'media_parse'))


def test_groups_notices_and_unbound_private_do_not_retain_ids(client):
    channel, connection = setup(client, group_enabled=True, group_ids=['12345'])
    channel = rules(client, channel, allow_unbound=False)
    incoming(channel, connection, group='12345')
    incoming(channel, connection)
    receive_event(channel['id'], connection, {'self_id': 12345678, 'post_type': 'notice', 'notice_type': 'group_upload',
        'group_id': 12345, 'user_id': 87654321, 'file': {'name': '私密.md', 'id': KEY, 'url': URL}})
    assert prepare_outbound(channel['id'], connection) is None
    with SessionLocal() as db:
        assert not db.scalar(select(ChannelInbound)) and not db.scalar(select(ChannelAttachment))


def test_large_document_budget_stays_twenty_mib(client, monkeypatch):
    channel, connection = setup(client)
    seen = []
    def fetch(url, hosts, path, limit, fence, deadline):
        seen.append(limit)
        raise HTTPException(413, 'fixture: stop without allocating a large body')
    monkeypatch.setattr('lingxi.channel_media.download', fetch)
    incoming(channel, connection, data={'file': 'large.txt', 'file_id': KEY, 'file_size': MAX_FILE})
    response(channel, connection, prepare_outbound(channel['id'], connection))
    prepare_outbound(channel['id'], connection); process()
    assert seen == [20*1024**2]


@pytest.mark.parametrize('resolved', [False, True])
def test_restore_quarantine_scrubs_lookup_and_prevents_old_receive(client, resolved):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    channel, connection = setup(client)
    incoming(channel, connection)
    action = prepare_outbound(channel['id'], connection)
    if resolved:
        response(channel, connection, action)
    with SessionLocal.begin() as db:
        schema(db)
    quarantine(checkpoint())
    with SessionLocal() as db:
        row = db.scalar(select(ChannelAttachment))
        assert not row.encrypted_url and 'echo' not in row.document_state and 'connection' not in row.document_state
        assert row.status == 'cancelled' and db.scalar(select(ChannelInbound)).status == 'cancelled'
        assert db.scalar(select(Media)).status == 'deleted'
        assert not db.get(Channel, channel['id']).enabled
    response(channel, connection, action)
    with SessionLocal() as db:
        assert db.scalar(select(ChannelAttachment)).encrypted_url == ''
        assert not db.scalar(select(Job).where(Job.kind.in_(['channel_media', 'chat'])))
