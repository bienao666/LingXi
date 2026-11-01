"""Real task/media lifecycle, separate grants, late-result fences and Linux file access."""
import hashlib
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from lingxi.db import SessionLocal
from lingxi.media import Media, MediaLink, content_for_items, storage
from lingxi.models import ChatRun, Message, User
from lingxi.plugin_manifest import Manifest, bundled_manifests
from lingxi.server_documents import FileReadInput, read_snapshot
from lingxi.tasks import WorkTask
from test_plugins import install, action
from test_resources import mounted, drain, grant as metadata_grant


def grant(client, roots=None, read_roots=None):
    body = {'version': client.get('/api/resource-policy').json()['version'],
        'roots': ['docs'] if roots is None else roots, 'read_roots': ['docs'] if read_roots is None else read_roots}
    response = client.post('/api/admin/resource-policy/previews', json=body)
    assert response.status_code == 200, response.text
    response = client.put('/api/admin/resource-policy', json={'token': response.json()['token'],
        'acknowledgement': 'invalidate_previous_resource_contexts'})
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, path='docs/readme.md', request='server-read-001', **kwargs):
    return client.post('/api/tasks', json={'request_id': request, 'title': '读取文档',
        'skill_id': 'server.read', 'input': {'path': path}} | kwargs)


@pytest.fixture
def reader(client, mounted, monkeypatch):
    plugin = install(client, plugin='lingxi.documents')
    grant(client)
    def read(path, check, **kwargs):
        check(True)
        data = '灵犀服务器文档正文\n只读快照。'.encode()
        return data, {'path': path, 'digest': hashlib.sha256(data).hexdigest(),
            'size': len(data), 'observed_at': 100., 'modified_at': 99.}
    monkeypatch.setattr('lingxi.server_documents.read_snapshot', read)
    return client, plugin


def imported(client):
    response = submit(client)
    assert response.status_code == 200, response.text
    drain()
    task = client.get('/api/tasks/' + response.json()['id']).json()
    assert task['status'] == 'succeeded', task
    media = client.get('/api/media/' + task['result']['media_id']).json()
    assert media['status'] == 'ready', media
    return task, media


def test_old_scope_and_catalog_do_not_grant_document_access(client, mounted):
    metadata_grant(client, ['docs'])
    before = client.get('/api/plugin-installations').json()
    assert len(before) == 1
    assert submit(client).status_code == 409
    preview = client.post('/api/plugin-installations/preview', json={'plugin_id': 'lingxi.documents',
        'plugin_version': '1.0.0', 'action': 'install'}).json()
    assert preview['plan']['permissions'] == ['resources.read']
    assert len(client.get('/api/plugin-installations').json()) == 1
    install(client, plugin='lingxi.documents')
    assert submit(client).status_code == 403
    assert client.get('/api/resource-policy').json()['read_roots'] == []
    assert not next(s for s in client.get('/api/skills').json() if s['id'] == 'server.read')['available']


def test_document_scope_requires_subset_and_signed_preview(client, mounted):
    assert client.post('/api/admin/resource-policy/previews', json={
        'version': 0, 'roots': ['docs'], 'read_roots': ['private']}).status_code == 422
    policy = grant(client, ['docs'], ['docs/public'])
    assert policy['mode'] == 'scoped_document_read'
    install(client, plugin='lingxi.documents')
    assert submit(client).status_code == 403
    assert submit(client, 'docs/public-old/a.md').status_code == 403
    assert submit(client, 'docs/public/a.md').status_code == 200


@pytest.mark.parametrize('path', ['.', '/etc/a.txt', '../a.txt', 'docs/../../a.md', 'C:\\a.txt',
    'docs/script.py', 'docs/audio.wav', 'docs/x\n.md', 'docs/' + 'a'*201 + '.txt'])
def test_document_input_rejects_invalid_or_unsupported_paths(path):
    with pytest.raises(ValueError):
        FileReadInput(path=path)


def test_native_document_entry_cannot_be_published_by_other_packages():
    spec = next(m for m in bundled_manifests() if m.plugin_id == 'lingxi.documents').model_dump()
    for changes in ({'plugin_id': 'lingxi.other'}, {'publisher': 'evil', 'plugin_id': 'evil.documents'},
        {'capabilities': [{'id': 'server.read', 'name': 'read', 'kind': 'native', 'permissions': []}]}):
        with pytest.raises(ValueError):
            Manifest.model_validate(spec | changes)


def test_import_parses_sources_and_can_be_attached_without_duplicate_on_retry(reader):
    client, _ = reader
    task, media = imported(client)
    assert task['config']['plugin_id'] == 'lingxi.documents'
    assert media['source']['kind'] == 'server_document'
    assert media['source']['path'] == 'docs/readme.md' and media['parts_count'] > 0
    assert '服务器文档正文' in str(media['parts'])
    assert submit(client).json()['id'] == task['id']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 1
        value, refs = content_for_items('请总结', [db.get(Media, media['id'])])
        assert '服务器文档正文' in str(value) and refs[0]['id'] == media['id']
    assert client.get('/api/media/' + media['id'] + '/content').content == storage(media['id'], 'blob').read_bytes()


def test_revoke_during_read_discards_late_snapshot(reader, monkeypatch):
    client, _ = reader
    original = __import__('lingxi.server_documents', fromlist=['read_snapshot']).read_snapshot
    def delayed(*args, **kwargs):
        data = original(*args, **kwargs)
        grant(client, [], [])
        return data
    monkeypatch.setattr('lingxi.server_documents.read_snapshot', delayed)
    response = submit(client).json()
    drain()
    task = client.get('/api/tasks/' + response['id']).json()
    assert task['status'] == 'cancelled' and task['result'] == {}
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Media)) == 0


def test_scope_revoke_invalidates_download_context_and_derivative_answers(reader):
    client, _ = reader
    task, media = imported(client)
    conversation = client.post('/api/conversations', json={'title': '文档分析'}).json()['id']
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='document-run', user_id='owner', conversation_id=conversation, request_id='document-run',
            request_hash='hash', model_id='chat', status='completed', config={'media_refs': [{'id': media['id'], 'version': media['version']}]}))
        db.add(Message(id='document-answer', conversation_id=conversation, run_id='document-run', role='assistant', status='completed', content='派生正文'))
    grant(client, ['docs'], [])
    assert client.get('/api/media/' + media['id']).json()['parts'] == []
    assert client.get('/api/media/' + media['id'] + '/content').status_code == 410
    with SessionLocal() as db:
        assert db.get(ChatRun, 'document-run').status == 'invalidated'
        assert db.get(Message, 'document-answer').content == ''
    grant(client)
    assert client.get('/api/media/' + media['id']).json()['status'] == 'deleted'
    assert submit(client).json()['id'] == task['id']  # replay cannot recreate a deleted source


def test_plugin_disable_stops_queued_and_completed_snapshots_and_enable_does_not_revive(reader):
    client, plugin = reader
    task, media = imported(client)
    pending = submit(client, request='server-read-pending').json()
    assert action(client, plugin, 'disable').status_code == 200
    drain()
    assert client.get('/api/tasks/' + pending['id']).json()['status'] == 'cancelled'
    assert client.get('/api/tasks/' + task['id']).json()['result'] == {}
    assert client.get('/api/media/' + media['id'] + '/content').status_code == 410
    plugin = next(p for p in client.get('/api/plugin-installations').json() if p['plugin_id'] == 'lingxi.documents')
    assert action(client, plugin, 'enable').status_code == 200
    assert client.get('/api/media/' + media['id']).json()['status'] == 'deleted'


def test_update_removing_document_capability_cannot_revive_snapshot_after_rollback(reader):
    from lingxi.plugins import PluginRelease
    from lingxi.plugin_manifest import digest
    client, _ = reader
    _, media = imported(client)
    spec = next(m for m in bundled_manifests() if m.plugin_id == 'lingxi.documents').model_dump()
    spec.update(version='1.1.0', capabilities=[{'id': 'document.outline', 'kind': 'skill', 'name': '文字提纲',
        'permissions': ['model.chat'], 'instruction': '整理用户提供的文字'}])
    spec = Manifest.model_validate(spec).model_dump()
    with SessionLocal.begin() as db:
        db.add(PluginRelease(id='new-document-release', plugin_id='lingxi.documents', version='1.1.0',
            manifest=spec, digest=digest(spec)))
    install(client, plugin='lingxi.documents', version='1.1.0', action='update')
    assert client.get('/api/media/' + media['id']).json()['status'] == 'deleted'
    install(client, plugin='lingxi.documents', version='1.0.0', action='rollback')
    assert client.get('/api/media/' + media['id'] + '/content').status_code == 410


def test_delete_snapshot_keeps_resource_grant_and_does_not_repeat_task(reader):
    client, _ = reader
    task, media = imported(client)
    assert client.request('DELETE', '/api/media/' + media['id'], json={'version': media['version']}).status_code == 200
    drain()
    assert client.get('/api/resource-policy').json()['read_roots'] == ['docs']
    assert submit(client).json()['id'] == task['id']
    assert client.get('/api/media/' + media['id']).json()['status'] == 'deleted'


def test_restore_clears_both_scopes_and_invalidates_snapshots(reader):
    from lingxi.resources import checkpoint, restore
    client, _ = reader
    _, media = imported(client)
    with SessionLocal.begin() as db:
        records = checkpoint(db)
        restore(db, records, db.get(User, 'owner'))
    policy = client.get('/api/resource-policy').json()
    assert policy['roots'] == [] and policy['read_roots'] == []
    assert client.get('/api/media/' + media['id'] + '/content').status_code == 410


def test_parser_revocation_discards_already_computed_body(reader, monkeypatch):
    import subprocess
    from lingxi import worker
    client, _ = reader
    task = submit(client).json()
    worker.perform(worker.claim())  # import commits, parse remains queued
    real_run = subprocess.run
    def delayed(*args, **kwargs):
        result = real_run(*args, **kwargs)
        grant(client, [], [])
        return result
    monkeypatch.setattr('lingxi.media.subprocess.run', delayed)
    drain()
    media_id = task['config']['media_id']
    detail = client.get('/api/media/' + media_id).json()
    assert detail['status'] == 'deleted' and detail['parts'] == []


def test_missing_source_grant_is_scrubbed_even_without_invalidation_hook(reader):
    from lingxi.resources import ResourcePolicy
    from lingxi.agent_memory import roots_valid
    client, _ = reader
    _, media = imported(client)
    with SessionLocal.begin() as db:
        db.get(ResourcePolicy,
            ('personal', 'owner')).version += 1
    detail = client.get('/api/media/' + media['id']).json()
    assert detail['status'] == 'revoked' and detail['parts'] == []
    assert client.get('/api/media/' + media['id'] + '/content').status_code == 410
    with SessionLocal() as db:
        item = db.get(Media, media['id'])
        value, refs = content_for_items('继续聊天', [item])
        assert not refs and '服务器文档正文' not in str(value)
        assert not roots_valid(db, {'workspace_id': 'personal', 'media': [
            {'id': item.id, 'version': item.version, 'user_id': 'owner'}]})


def test_deleted_snapshot_cannot_be_selected_and_other_owner_cannot_read(reader):
    from lingxi.media import validate_attachments
    client, _ = reader
    _, media = imported(client)
    with SessionLocal.begin() as db:
        user = User(id='other', username='other', password_hash='not-empty', workspace_id='other-space')
        db.add(user)
        db.flush()
        with pytest.raises(HTTPException):
            validate_attachments(db, [media['id']], user, None, None)
    assert client.request('DELETE', '/api/media/' + media['id'], json={'version': media['version']}).status_code == 200
    with SessionLocal() as db:
        with pytest.raises(HTTPException):
            validate_attachments(db, [media['id']], db.get(User, 'owner'), None, None)


@pytest.mark.skipif(__import__('lingxi.db', fromlist=['engine']).engine.dialect.name != 'postgresql',
    reason='PostgreSQL concurrent final commit fence')
@pytest.mark.parametrize('revoke', ['resource', 'plugin'])
def test_concurrent_revocation_cannot_be_overwritten_by_late_import(reader, monkeypatch, revoke):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from lingxi import worker
    client, plugin = reader
    entered, release = threading.Event(), threading.Event()
    real_read = __import__('lingxi.server_documents', fromlist=['read_snapshot']).read_snapshot
    def delayed(*args, **kwargs):
        result = real_read(*args, **kwargs)
        entered.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr('lingxi.server_documents.read_snapshot', delayed)
    task = submit(client).json()
    job = worker.claim()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.perform, job)
        try:
            assert entered.wait(10)
            if revoke == 'resource':
                grant(client, [], [])
            else:
                assert action(client, plugin, 'disable').status_code == 200
        finally:
            release.set()
        future.result(timeout=15)
    detail = client.get('/api/tasks/' + task['id']).json()
    assert detail['status'] == 'cancelled' and detail['result'] == {}
    with SessionLocal() as db:
        assert db.get(Media, task['config']['media_id']) is None


@pytest.mark.skipif(os.name != 'posix', reason='Linux openat and parser integration')
def test_real_file_import_parser_and_original_preserved(client, tmp_path, monkeypatch):
    from lingxi.config import settings
    root = tmp_path / 'resources'
    (root / 'docs').mkdir(parents=True)
    original = root / 'docs' / 'readme.md'
    original.write_text('服务器实读：灵犀文档。', encoding='utf-8')
    monkeypatch.setattr(settings(), 'resource_dir', root)
    install(client, plugin='lingxi.documents')
    grant(client)
    task, media = imported(client)
    assert '服务器实读' in str(media['parts'])
    original.write_text('后续修改', encoding='utf-8')
    assert '服务器实读' in str(client.get('/api/media/' + media['id']).json()['parts'])
    assert client.request('DELETE', '/api/media/' + media['id'], json={'version': media['version']}).status_code == 200
    drain()
    assert original.read_text(encoding='utf-8') == '后续修改'


@pytest.mark.skipif(os.name != 'posix', reason='Linux symlinks/special files and fd cleanup')
def test_real_file_links_limits_races_and_descriptor_cleanup(client, tmp_path, monkeypatch):
    from lingxi.config import settings
    root = tmp_path / 'resources'
    (root / 'docs').mkdir(parents=True)
    original = root / 'docs' / 'a.txt'
    original.write_text('正文', encoding='utf-8')
    (root / 'docs' / 'link.txt').symlink_to(original)
    os.link(original, root / 'docs' / 'hard.txt')
    os.mkfifo(root / 'docs' / 'pipe.txt')
    (root / 'docs' / 'empty.txt').touch()
    monkeypatch.setattr(settings(), 'resource_dir', root)
    ref = {'mode': 'document_read', 'roots': ['docs'], 'read_roots': ['docs']}
    for path in ('link.txt', 'hard.txt', 'a.txt', 'pipe.txt', 'empty.txt'):
        with pytest.raises(HTTPException):
            read_snapshot('docs/' + path, lambda: None, grant=ref, limit=1024)
    (root / 'docs' / 'hard.txt').unlink()
    with pytest.raises(HTTPException):
        read_snapshot('docs/a.txt', lambda: None, grant=ref, limit=1)
    count = len(os.listdir('/proc/self/fd'))
    def cancelled():
        raise HTTPException(410, '撤销')
    for _ in range(30):
        with pytest.raises(HTTPException):
            read_snapshot('docs/a.txt', cancelled, grant=ref, limit=1024)
    assert len(os.listdir('/proc/self/fd')) <= count + 1
    changed = False
    def mutate():
        nonlocal changed
        if not changed:
            return
        original.write_text('被修改的正文', encoding='utf-8')
    real_read = os.read
    def read(fd, size):
        nonlocal changed
        value = real_read(fd, size)
        changed = True
        return value
    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(HTTPException, match='409'):
        read_snapshot('docs/a.txt', mutate, grant=ref, limit=1024)
