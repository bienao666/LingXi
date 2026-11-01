import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lingxi.api import app
from lingxi.db import SessionLocal, engine
from lingxi.media import Media, storage, validate_attachments
from lingxi.media_policy import MediaLimits, MediaPolicy, MIB, check_sizes
from lingxi.models import Audit, ChatRun, User
from test_media import upload


def preview(client, **changes):
    current = client.get('/api/media-policy').json()
    return client.post('/api/admin/media-policy/previews', json=current | changes)


def save(client, **changes):
    p = preview(client, **changes)
    assert p.status_code == 200, p.text
    return client.put('/api/admin/media-policy', json={'token': p.json()['token'],
        'acknowledgement': 'new_submissions_only'})


def test_defaults_preview_and_confirm_are_separate(client):
    before = client.get('/api/media-policy').json()
    assert (before['max_files'], before['max_file_mib'], before['max_total_mib']) == (10, 20, 50)
    p = preview(client, max_files=2, max_file_mib=3, max_total_mib=5)
    assert p.status_code == 200 and p.json()['before'] == before
    assert client.get('/api/media-policy').json() == before
    assert '不删除' in p.json()['note']
    result = client.put('/api/admin/media-policy', json={'token': p.json()['token'], 'acknowledgement': 'new_submissions_only'})
    assert result.status_code == 200
    assert result.json() == dict(version=before['version']+1, max_files=2, max_file_mib=3, max_total_mib=5,
        allowed_types=before['allowed_types'])
    assert client.put('/api/admin/media-policy', json={'token': p.json()['token'], 'acknowledgement': 'new_submissions_only'}).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'media_policy_update')) == 1


@pytest.mark.parametrize('changes', [dict(max_files=0), dict(max_files=11), dict(max_file_mib=21),
    dict(max_total_mib=51), dict(max_total_mib=0), dict(max_file_mib=5, max_total_mib=4),
    dict(max_files=True), dict(max_file_mib=1.5), dict(max_files='2'), dict(extra='untrusted')])
def test_invalid_policy_cannot_raise_hard_caps_or_coerce(client, changes):
    assert preview(client, **changes).status_code == 422


def test_version_scope_expiry_session_and_acknowledgement(client, monkeypatch):
    body = preview(client, max_files=1).json()
    confirm = {'token': body['token'], 'acknowledgement': 'new_submissions_only'}
    assert client.put('/api/admin/media-policy', json={'token': body['token']}).status_code == 422
    assert client.put('/api/admin/media-policy', json=confirm | {'token': body['token'][:-5]+'wrong'}).status_code == 409
    from lingxi import media_policy
    clock = media_policy.now
    monkeypatch.setattr(media_policy, 'now', lambda: clock()+301)
    assert client.put('/api/admin/media-policy', json=confirm).status_code == 409
    monkeypatch.setattr(media_policy, 'now', clock)
    with TestClient(app) as second:
        session = second.post('/api/auth/login', json={'username': 'owner', 'password': 'test-password-123'}).json()
        second.headers['X-CSRF-Token'] = session['csrf']
        assert second.put('/api/admin/media-policy', json=confirm).status_code == 409
    with SessionLocal.begin() as db:
        db.get(User, 'owner').workspace_id = 'different'
    assert client.put('/api/admin/media-policy', json=confirm).status_code == 409
    assert client.get('/api/media-policy').json()['max_files'] == 10


def test_policy_auth_csrf_and_stale_preview(client):
    before = client.get('/api/media-policy').json()
    assert save(client, max_files=2).status_code == 200
    assert client.post('/api/admin/media-policy/previews', json=before).status_code == 409
    token = client.headers.pop('X-CSRF-Token')
    assert preview(client).status_code == 403
    client.headers['X-CSRF-Token'] = token
    client.cookies.clear()
    assert client.get('/api/media-policy').status_code == 401
    assert client.put('/api/admin/media-policy', json={'token':'bad-token-value', 'acknowledgement':'new_submissions_only'}).status_code == 401


def test_upload_uses_lower_limit_even_without_content_length(client):
    assert save(client, max_file_mib=1, max_total_mib=1).status_code == 200
    assert upload(client, data=b'x'*(MIB+1)).status_code == 413
    # Streaming bodies have no Content-Length. Per-chunk admission still applies.
    result = client.post('/api/media', params={'filename':'stream.txt', 'request_id':'stream-limit-test'},
        content=iter([b'x'*MIB, b'y']), headers={'Content-Type':'application/octet-stream'})
    assert result.status_code == 413
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 0


def test_lowering_policy_during_upload_rechecks_before_commit(client, monkeypatch):
    from starlette.requests import Request
    original = Request.stream
    async def stream(request):
        async for chunk in original(request):
            if request.url.path == '/api/media' and chunk:
                with SessionLocal.begin() as db:
                    row = db.get(MediaPolicy, 'personal')
                    row.max_file_mib, row.max_total_mib, row.version = 1, 1, row.version+1
            yield chunk
    monkeypatch.setattr(Request, 'stream', stream)
    response = upload(client, data=b'x'*(MIB+1))
    assert response.status_code == 413
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 0


def test_existing_assets_download_but_new_selection_obeys_count_size_and_total(client):
    ids = []
    for i in range(3):
        row = upload(client, filename=f'{i}.txt', data=b'x'*(MIB+1 if i == 0 else MIB), request_id=f'asset-size-{i}')
        assert row.status_code == 200
        ids.append(row.json()['id'])
    assert save(client, max_files=2, max_file_mib=1, max_total_mib=1).status_code == 200
    assert client.get('/api/media/'+ids[0]+'/content').status_code == 200
    with SessionLocal.begin() as db:
        for identity in ids:
            row = db.get(Media, identity)
            row.status, row.parsed = 'ready', {'kind':'document','parts':[]}
        user = db.get(User,'owner')
        for choice in ([ids[0]], ids[1:], ids):
            with pytest.raises(HTTPException) as exc:
                validate_attachments(db, choice, user, None, None)
            assert exc.value.status_code == 413
        assert len(validate_attachments(db, [ids[1]], user, None, None)) == 1
    assert client.get('/api/media/'+ids[0]).json()['status'] == 'ready'


def test_workspace_policy_not_sender_policy_on_qq_declarations(client):
    from lingxi.channel_media import declarations
    from lingxi.channels import ChannelRules
    rule = ChannelRules(attachments_enabled=True, attachment_hosts=['cdn.example.com'])
    item = ('file', {'name':'资料.txt','url':'https://cdn.example.com/a','size':MIB})
    low = MediaLimits(max_files=1,max_file_mib=1,max_total_mib=1)
    assert len(declarations([item],rule,low)) == 1
    for inputs in ([item,item], [('file', item[1] | {'size':MIB+1})]):
        with pytest.raises(HTTPException) as exc:
            declarations(inputs,rule,low)
        assert exc.value.status_code == 413


def test_qq_download_rechecks_changed_policy_before_commit(configured, monkeypatch):
    import hashlib
    from lingxi import channel_media as cm
    from test_channel_media import setup, inbound, process
    from lingxi.channel_gateway import prepare_outbound
    channel, connection = setup(configured)
    inbound(channel, connection)
    prepare_outbound(channel['id'], connection)
    def fetch(url, hosts, path, limit, fence, deadline):
        assert limit == 20*MIB
        fence()
        with SessionLocal.begin() as db:
            row = db.get(MediaPolicy,'personal')
            row.max_file_mib, row.max_total_mib, row.version = 1,1,row.version+1
        data = b'x'*(MIB+1)
        path.write_bytes(data)
        return len(data), hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(cm,'download',fetch)
    assert process().kind == 'channel_media'
    with SessionLocal() as db:
        media = db.scalar(select(Media))
        assert media.status == 'deleted'
        assert not storage(media.id,'blob').exists()
        assert db.scalar(select(ChatRun)) is None


def test_qq_stage_count_policy_prevents_download(configured, monkeypatch):
    from test_channel_media import setup
    from test_channels import event
    from lingxi.channel_gateway import receive_event, prepare_outbound
    from lingxi.channel_media import ChannelAttachment
    from lingxi.models import Job
    channel, connection = setup(configured)
    # The message was accepted before a stricter setting was applied.
    message = event('阅读这两个附件')
    message['message'] += [{'type':'file','data':{'name':f'{i}.txt','url':'https://cdn.example.com/a'}} for i in range(2)]
    receive_event(channel['id'],connection,message)
    assert save(configured,max_files=1).status_code == 200
    action = prepare_outbound(channel['id'],connection)
    assert action and '附件' in json.dumps(action,ensure_ascii=False)
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.kind=='channel_media')) is None
        assert db.scalar(select(Media)) is None


def test_restore_preserves_latest_limits_and_invalidates_preview(client):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    with SessionLocal.begin() as db:
        schema(db)
    assert save(client, max_files=1,max_file_mib=1,max_total_mib=1).status_code == 200
    latest = checkpoint()
    assert latest['media_policies'][0]['max_files'] == 1
    with SessionLocal.begin() as db:
        row = db.get(MediaPolicy,'personal')
        row.max_files,row.max_file_mib,row.max_total_mib,row.version = 10,20,50,1
    quarantine(latest)
    with SessionLocal() as db:
        row = db.get(MediaPolicy,'personal')
        assert (row.max_files,row.max_file_mib,row.max_total_mib) == (1,1,1)
        assert row.version == latest['media_policies'][0]['version']+1


def test_qq_converted_audio_respects_workspace_limit(configured):
    import io, wave
    from test_channel_audio import setup, incoming, response
    from lingxi.channel_gateway import prepare_outbound
    channel, connection = setup(configured)
    incoming(channel,connection)
    action = prepare_outbound(channel['id'],connection)
    assert action['action'] == 'get_record'
    assert save(configured,max_file_mib=1,max_total_mib=1).status_code == 200
    output = io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
        wav.writeframes(b'\x00\x00'*(40*16000))
    response(channel,connection,action,raw=output.getvalue())
    with SessionLocal() as db:
        media = db.scalar(select(Media))
        assert media.status == 'deleted'
        assert not storage(media.id,'blob').exists()


def test_policy_update_does_not_cancel_previously_submitted_chat(configured):
    from lingxi.models import Conversation
    from lingxi.media import MediaLink
    item = upload(configured,data=b'x'*(MIB+1)).json()
    with SessionLocal.begin() as db:
        row = db.get(Media,item['id'])
        row.status,row.parsed = 'ready',{'kind':'document','parts':[{'location':'第 1 行','text':'授权资料'}]}
    conversation = configured.post('/api/conversations',json={'title':'已提交附件'}).json()
    submitted = configured.post('/api/chat',json={'request_id':'before-lower-policy','conversation_id':conversation['id'],
        'content':'阅读资料','media_ids':[item['id']]} )
    assert submitted.status_code == 200,submitted.text
    run_id = submitted.json()['id']
    assert save(configured,max_file_mib=1,max_total_mib=1).status_code == 200
    with SessionLocal() as db:
        run = db.get(ChatRun,run_id)
        assert run.status == 'queued' and not run.cancel_requested
        assert run.config['media_refs'] == [{'id':item['id'],'version':item['version']}]
        assert db.get(Media,item['id']).status == 'ready'


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='PostgreSQL policy row-lock concurrency')
def test_only_one_same_version_confirmation_wins(client):
    previews = [preview(client,max_files=i).json() for i in (1,2)]
    def confirm(p):
        return client.put('/api/admin/media-policy',json={'token':p['token'],'acknowledgement':'new_submissions_only'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(confirm,previews)) == [200,409]
