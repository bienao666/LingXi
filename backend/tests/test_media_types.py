import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from lingxi.db import SessionLocal
from lingxi.media import Media, validate_attachments
from lingxi.media_policy import DEFAULT_TYPES, MediaLimits, MediaPolicy, check_types
from lingxi.models import ChatRun
from test_media import upload
from test_media_policy import save, preview


def test_default_formats_and_explicit_empty_switch(client):
    assert client.get('/api/media-policy').json()['allowed_types'] == DEFAULT_TYPES
    assert save(client, allowed_types=[]).status_code == 200
    assert upload(client).status_code == 415
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 0


@pytest.mark.parametrize('formats', [['script'], ['image', 'image'], ['.txt'], 'text', [None], [True]])
def test_unknown_duplicate_and_untyped_formats_rejected(client, formats):
    assert preview(client, allowed_types=formats).status_code == 422


def test_formats_canonicalized_and_older_size_only_client_cannot_expand(client):
    assert save(client, allowed_types=['image', 'text']).json()['allowed_types'] == ['text', 'image']
    current = client.get('/api/media-policy').json()
    current.pop('allowed_types')
    p = client.post('/api/admin/media-policy/previews', json=current | {'max_files': 1}).json()
    assert p['after']['allowed_types'] == ['text', 'image']
    assert client.put('/api/admin/media-policy', json={'token': p['token'],
        'acknowledgement': 'new_submissions_only'}).json()['allowed_types'] == ['text', 'image']


def test_restricted_formats_preserve_history_but_block_new_selection(configured):
    item = upload(configured).json()
    with SessionLocal.begin() as db:
        row = db.get(Media, item['id'])
        row.status, row.parsed = 'ready', {'kind': 'document', 'parts': [{'location': '第 1 行', 'text': '测试资料'}]}
    conversation = configured.post('/api/conversations', json={'title': '媒体策略'}).json()
    run = configured.post('/api/chat', json={'conversation_id': conversation['id'], 'content': '阅读',
        'request_id': 'before-format-restriction', 'media_ids': [item['id']]}).json()
    assert save(configured, allowed_types=['image']).status_code == 200
    assert configured.get('/api/media/'+item['id']+'/content').status_code == 200
    another = configured.post('/api/conversations', json={'title': '新附件选择'}).json()
    result = configured.post('/api/chat', json={'conversation_id': another['id'], 'content': '再次阅读',
        'request_id': 'after-format-restriction', 'media_ids': [item['id']]})
    assert result.status_code == 415
    with SessionLocal() as db:
        assert db.get(ChatRun, run['id']).status == 'queued'
        assert db.get(Media, item['id']).status == 'ready'


def test_change_during_upload_rechecked_and_partial_file_discarded(client, monkeypatch):
    from starlette.requests import Request
    original = Request.stream
    async def stream(request):
        async for chunk in original(request):
            if request.url.path == '/api/media' and chunk:
                with SessionLocal.begin() as db:
                    row = db.get(MediaPolicy, 'personal')
                    row.allowed_types, row.version = [], row.version+1
            yield chunk
    monkeypatch.setattr(Request, 'stream', stream)
    assert upload(client).status_code == 415
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 0


def test_extension_mapping_and_qq_audio_separate_from_images():
    policy = MediaLimits(allowed_types=['image'])
    check_types(policy, ['.PNG', '.jpg', '.jpeg', '.webp'])
    for suffix in ('.wav', '.gif', '.svg', '.html'):
        with pytest.raises(HTTPException):
            check_types(policy, [suffix])


def test_qq_declaration_blocks_disabled_audio_and_documents():
    from lingxi.channel_media import declarations
    from lingxi.channels import ChannelRules
    rule = ChannelRules(attachments_enabled=True, voice_enabled=True, attachment_hosts=['cdn.example.com'])
    for item in [('file', {'name': '资料.txt', 'url': 'https://cdn.example.com/a'}),
                 ('image', {'url': 'https://cdn.example.com/a'}),
                 ('record', {'file': 'a'*32})]:
        with pytest.raises(HTTPException) as exc:
            declarations([item], rule, MediaLimits(allowed_types=[]))
        assert exc.value.status_code == 415


def test_qq_staged_download_does_not_start_after_type_disabled(configured, monkeypatch):
    from test_channel_media import setup, inbound, process
    from lingxi.channel_gateway import prepare_outbound
    import lingxi.channel_media as cm
    channel, connection = setup(configured)
    inbound(channel, connection)
    prepare_outbound(channel['id'], connection)
    assert save(configured, allowed_types=[]).status_code == 200
    def no_download(*args):
        pytest.fail('Disabled type must not download')
    monkeypatch.setattr(cm, 'download', no_download)
    process()
    with SessionLocal() as db:
        assert db.scalar(select(Media)).status == 'deleted'
        assert db.scalar(select(ChatRun)) is None


def test_actual_backup_quarantine_preserves_latest_restrictions(client):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    with SessionLocal.begin() as db:
        schema(db)
    assert save(client, allowed_types=['markdown']).status_code == 200
    latest = checkpoint()
    with SessionLocal.begin() as db:
        db.get(MediaPolicy, 'personal').allowed_types = list(DEFAULT_TYPES)
    quarantine(latest)
    with SessionLocal() as db:
        policy = db.get(MediaPolicy, 'personal')
        assert policy.allowed_types == ['markdown']
        assert policy.version == latest['media_policies'][0]['version']+1


def test_pre_upgrade_preview_cannot_silently_reenable_formats(client):
    import json
    from lingxi.security import encrypt
    from lingxi.models import LoginSession
    from lingxi.db import now
    current = client.get('/api/media-policy').json()
    with SessionLocal() as db:
        session = db.scalar(select(LoginSession))
        token = encrypt(json.dumps({'purpose': 'media-policy', 'user': 'owner', 'workspace': 'personal',
            'session': session.id, 'version': current['version'], 'expires_at': now()+300,
            'after': {'max_files': 1, 'max_file_mib': 20, 'max_total_mib': 50}}))
    assert client.put('/api/admin/media-policy', json={'token': token,
        'acknowledgement': 'new_submissions_only'}).status_code == 409


def test_type_disabled_during_qq_download_discards_unsubmitted_result(configured, monkeypatch):
    import hashlib
    import lingxi.channel_media as cm
    from test_channel_media import setup, inbound, process
    from lingxi.channel_gateway import prepare_outbound
    channel, connection = setup(configured)
    inbound(channel, connection)
    prepare_outbound(channel['id'], connection)
    def fetch(url, hosts, path, limit, fence, deadline):
        fence()
        with SessionLocal.begin() as db:
            policy = db.get(MediaPolicy, 'personal')
            policy.allowed_types, policy.version = [], policy.version+1
        raw = b'pending content'
        path.write_bytes(raw)
        return len(raw), hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(cm, 'download', fetch)
    process()
    with SessionLocal() as db:
        from lingxi.media import storage
        media = db.scalar(select(Media))
        assert media.status == 'deleted' and not storage(media.id, 'blob').exists()
        assert db.scalar(select(ChatRun)) is None


def test_disabling_qq_audio_after_conversion_blocks_paid_asr(configured, monkeypatch):
    from test_channel_audio import received, process
    import lingxi.channel_audio as ca
    received(configured)
    assert save(configured, allowed_types=[]).status_code == 200
    def no_asr(*args, **kwargs):
        pytest.fail('Disabled QQ audio must not start ASR')
    monkeypatch.setattr(ca, 'audio_call', no_asr)
    assert process().kind == 'channel_audio'
    with SessionLocal() as db:
        assert db.scalar(select(Media)).status == 'deleted'
        assert db.scalar(select(ChatRun)) is None
