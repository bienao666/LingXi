"""Disposable Linux media spool, real DB/jobs and explicit permanent removal."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from lingxi.api import app
from lingxi.config import settings
from lingxi.db import SessionLocal, now
from lingxi.models import Job, LoginSession, User
from lingxi.media import Media
from lingxi import media_storage as store, worker

ROOT = '/api/admin/media-storage'


@pytest.fixture
def spool(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings(), 'runtime_dir', tmp_path)
    (tmp_path/'media').mkdir()
    monkeypatch.setattr(store, 'now', lambda: now()+3*86400)
    with SessionLocal.begin() as db:
        for login in db.scalars(select(LoginSession)): login.expires_at = now()+10*86400
    return tmp_path/'media'


def blob(spool, identity=None, ending='blob', content=b'disposable orphan bytes'):
    path = spool/((identity or uuid4().hex)+'.'+ending)
    path.write_bytes(content)
    return path


def drain(kind=None):
    job = worker.claim()
    assert job and (kind is None or job.kind == kind)
    worker.perform(job)
    return job


def scan(client, **extra):
    result = client.post(ROOT+'/scans', json={'request_id':uuid4().hex, **extra})
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    drain('media_storage_scan')
    return next(r for r in client.get(ROOT+'/runs').json() if r['id'] == identity)


def confirm(client, row):
    result = client.post(ROOT+'/runs/'+row['id']+'/confirm', json={
        'digest':row['digest'], 'acknowledgement':'remove_untracked_media_files'})
    assert result.status_code == 200, result.text
    return result.json()


def get(client, row):
    return next(r for r in client.get(ROOT+'/runs').json() if r['id'] == row['id'])


def test_scan_does_not_remove_and_confirmation_is_idempotent(client, spool):
    path = blob(spool)
    row = scan(client)
    assert row['status'] == 'review' and path.exists()
    assert row['items'] == [{'key':path.name, 'bytes':path.stat().st_size, 'modified_at':path.stat().st_mtime}]
    assert not any(s in str(row) for s in ('inode', 'fingerprint', str(spool), 'disposable orphan bytes'))
    confirm(client, row); confirm(client, row)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='media_storage_cleanup')) == 1
    drain('media_storage_cleanup')
    result = get(client, row)
    assert not path.exists() and result['status']=='completed' and result['results'][0]['status']=='removed'
    assert confirm(client, row)['status']=='completed'


@pytest.mark.parametrize('reference', ['media', 'deleted', 'other_workspace', 'job', 'link', 'protection', 'artifact', 'memory'])
def test_any_database_record_or_reference_preserves_orphan(client, spool, reference):
    identity = uuid4().hex; path = blob(spool, identity)
    with SessionLocal.begin() as db:
        if reference in ('media','deleted','other_workspace'):
            db.add(Media(id=identity,user_id='owner',workspace_id='other' if reference=='other_workspace' else 'personal',
                filename='known.md',request_id=uuid4().hex,suffix='.md',size=10,digest='h',status='deleted' if reference=='deleted' else 'ready'))
        elif reference=='job':
            db.add(Job(key=uuid4().hex,kind='media_parse',status='completed',payload={'media_id':identity}))
        elif reference=='link':
            from lingxi.media import MediaLink
            db.add(MediaLink(media_id=identity,message_id='historical-missing-message'))
        elif reference=='protection':
            from lingxi.media_retention import MediaProtection
            db.add(MediaProtection(media_id=identity,user_id='other',workspace_id='other'))
        elif reference=='artifact':
            from lingxi.channel_artifacts import ArtifactReply
            db.add(ArtifactReply(user_id='owner',channel_id='c',origin_id='o',login_id='l',request_id=uuid4().hex,
                request_hash='h',plan={'files':[{'id':identity}]},digest='h',expires_at=0))
        else:
            from lingxi.models import Memory
            db.add(Memory(user_id='other',workspace_id='other',content='not loaded',provenance={'media_id':identity}))
    row = scan(client)
    assert row['items']==[] and path.exists()
    assert row['summary']['tracked' if reference in ('media','deleted','other_workspace') else 'retained']==1


def test_fresh_files_unknown_names_directories_symlinks_and_hardlinks_excluded(client, spool, monkeypatch, tmp_path):
    fresh = blob(spool)
    unknown = spool/'private-backup.dump'; unknown.write_bytes(b'not our file')
    folder = spool/(uuid4().hex+'.blob');folder.mkdir()
    target = tmp_path/'outside.txt';target.write_text('outside')
    link = spool/(uuid4().hex+'.blob');link.symlink_to(target)
    hard = spool/(uuid4().hex+'.blob');os.link(target, hard)
    monkeypatch.setattr(store, 'now', now)
    row = scan(client)
    assert row['items']==[] and row['summary']['recent']==1 and row['summary']['ignored']==4
    assert fresh.exists() and unknown.exists() and folder.exists() and target.read_text()=='outside'


@pytest.mark.parametrize('change', ['rewrite','replace','symlink','hardlink','new_record','new_reference','missing','directory'])
def test_changes_after_preview_or_confirmation_are_not_removed(client, spool, change, tmp_path):
    path=blob(spool);identity=path.stem;row=scan(client);confirm(client,row)
    if change=='rewrite':path.write_bytes(b'new content')
    elif change=='replace':path.unlink();path.write_bytes(b'replaced')
    elif change=='symlink':path.unlink();path.symlink_to(tmp_path/'outside')
    elif change=='hardlink':os.link(path,tmp_path/'another-name')
    elif change=='directory':path.unlink();path.mkdir()
    elif change=='missing':path.unlink()
    elif change=='new_record':
        with SessionLocal.begin() as db:db.add(Media(id=identity,user_id='owner',request_id=uuid4().hex,filename='late.md',suffix='.md',digest='h'))
    else:
        from lingxi.models import Memory
        with SessionLocal.begin() as db:db.add(Memory(user_id='owner',content='late source',source_ids=[identity]))
    drain('media_storage_cleanup')
    r=get(client,row)
    assert r['results'][0]['status']==('missing' if change=='missing' else 'skipped')
    assert path.is_symlink() or change=='missing' or path.exists()


def test_scan_limits_and_batch_never_silently_expand(client, spool, monkeypatch):
    for _ in range(5):blob(spool)
    monkeypatch.setattr(store,'ENTRY_LIMIT',3);monkeypatch.setattr(store,'BATCH_LIMIT',2)
    row=scan(client)
    assert row['summary']['entries']==3 and row['summary']['partial'] and row['summary']['has_more']
    assert len(row['items'])==2
    confirm(client,row);drain('media_storage_cleanup')
    assert len(list(spool.iterdir()))==3


def test_no_client_paths_identity_or_scope_and_csrf_auth_required(client, spool):
    path=blob(spool)
    with TestClient(app) as stranger:assert stranger.get(ROOT).status_code==401
    for extra in ({'path':'/etc'},{'workspace_id':'other'},{'days':1},{'days':366}):
        assert client.post(ROOT+'/scans',json={'request_id':uuid4().hex}|extra).status_code==422
    row=scan(client)
    assert client.post(ROOT+'/runs/'+row['id']+'/confirm',json={'digest':row['digest']}).status_code==422
    assert client.post(ROOT+'/runs/'+row['id']+'/confirm',json={'digest':'0'*64,'acknowledgement':'remove_untracked_media_files'}).status_code==409
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post(ROOT+'/runs/'+row['id']+'/confirm',json={'digest':row['digest'],'acknowledgement':'remove_untracked_media_files'}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    with TestClient(app) as other:
        r=other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'});other.headers['X-CSRF-Token']=r.json()['csrf']
        assert other.post(ROOT+'/runs/'+row['id']+'/confirm',json={'digest':row['digest'],'acknowledgement':'remove_untracked_media_files'}).status_code==409
    assert path.exists()


def test_expiry_login_revocation_and_cancel_stop_cleanup(client, spool):
    path=blob(spool);row=scan(client)
    with SessionLocal.begin() as db:db.get(store.MediaStorageRun,row['id']).expires_at=0
    assert client.post(ROOT+'/runs/'+row['id']+'/confirm',json={'digest':row['digest'],'acknowledgement':'remove_untracked_media_files'}).status_code==409
    row=scan(client);confirm(client,row)
    client.post(ROOT+'/runs/'+row['id']+'/cancel');drain('media_storage_cleanup')
    assert path.exists() and get(client,row)['status']=='cancelled'
    row=scan(client);confirm(client,row)
    with SessionLocal.begin() as db:
        db.get(LoginSession,db.get(store.MediaStorageRun,row['id']).login_id).expires_at=0
    drain('media_storage_cleanup')
    with SessionLocal() as db:assert db.get(store.MediaStorageRun,row['id']).status=='failed'
    assert path.exists()


@pytest.mark.parametrize('already_unlinked',[False,True])
def test_uncertain_durable_intent_is_never_replayed(client, spool, monkeypatch, already_unlinked):
    path=blob(spool);row=scan(client);confirm(client,row)
    if already_unlinked:path.unlink()
    with SessionLocal.begin() as db:
        run=db.get(store.MediaStorageRun,row['id']);run.status='cleaning';run.results=[run.results[0]|{'status':'deleting'}]
    monkeypatch.setattr(store.os,'unlink',lambda *a,**k:pytest.fail('uncertain unlink must not replay'))
    # Preserve directory capability detection despite replacing the native function.
    monkeypatch.setattr(store.os,'supports_dir_fd',os.supports_dir_fd|{store.os.unlink})
    drain('media_storage_cleanup')
    assert path.exists() is not already_unlinked and get(client,row)['status']=='unknown'


def test_completed_receipts_survive_cancel_and_restore(client,spool):
    path=blob(spool);row=scan(client);confirm(client,row);drain('media_storage_cleanup')
    result=get(client,row)
    assert result['status']=='completed' and not path.exists()
    assert client.post(ROOT+'/runs/'+row['id']+'/cancel').json()['results']==result['results']
    with SessionLocal.begin() as db:
        saved=store.checkpoint(db)
        db.query(store.MediaStorageRun).delete();db.flush()
        store.restore(db,saved)
    assert get(client,row)['status']=='completed' and get(client,row)['results']==result['results']


def test_reference_budget_preserves_unexamined_files(client,spool,monkeypatch):
    monkeypatch.setattr(store,'REFERENCE_LIMIT',2)
    for _ in range(4):
        path=blob(spool)
        from lingxi.models import Memory
        with SessionLocal.begin() as db:db.add(Memory(user_id='owner',content='retained',source_ids=[path.stem]))
    row=scan(client)
    assert row['items']==[] and row['summary']['retained']==2
    assert row['summary']['reference_limited'] and row['summary']['has_more']
    assert len(list(spool.iterdir()))==4


def test_intent_committed_before_unlink_and_partial_failure(client, spool, monkeypatch):
    path=blob(spool);row=scan(client);confirm(client,row)
    def remove(key,**kwargs):
        with SessionLocal() as db:assert db.get(store.MediaStorageRun,row['id']).results[0]['status']=='deleting'
        raise OSError('synthetic disk error, private details not returned')
    monkeypatch.setattr(store.os,'unlink',remove)
    monkeypatch.setattr(store.os,'supports_dir_fd',os.supports_dir_fd|{remove})
    drain('media_storage_cleanup')
    assert path.exists() and get(client,row)['results'][0]['status']=='failed'
    assert 'private details' not in str(get(client,row))


def test_backup_restore_revokes_cleanup_and_preserves_receipts(client, spool):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    path=blob(spool);row=scan(client);confirm(client,row)
    with SessionLocal.begin() as db:schema(db)
    saved=checkpoint()
    with SessionLocal.begin() as db:db.query(store.MediaStorageRun).delete()
    quarantine(saved)
    with SessionLocal() as db:
        assert not db.scalar(select(Job.id).where(Job.kind=='media_storage_cleanup', Job.status.in_(('pending','running'))))
    with SessionLocal() as db:
        restored=db.get(store.MediaStorageRun,row['id'])
        assert restored.status=='cancelled' and restored.login_id=='revoked' and restored.results[0]['status']=='skipped'
    assert path.exists()


def test_idempotent_scan_and_active_scan_exclusion(client, spool):
    body={'request_id':uuid4().hex,'days':2}
    a=client.post(ROOT+'/scans',json=body);assert a.status_code==200
    assert client.post(ROOT+'/scans',json=body).json()['id']==a.json()['id']
    assert client.post(ROOT+'/scans',json={'request_id':uuid4().hex}).status_code==409
    client.post(ROOT+'/runs/'+a.json()['id']+'/cancel')
    drain('media_storage_scan')
    assert get(client,a.json())['status']=='cancelled'


def test_spool_symlink_is_rejected_without_following(client, spool, tmp_path):
    spool.rmdir();outside=tmp_path/'outside';outside.mkdir();path=blob(outside)
    spool.symlink_to(outside,target_is_directory=True)
    result=client.post(ROOT+'/scans',json={'request_id':uuid4().hex});identity=result.json()['id']
    job=worker.claim()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:store.execute_scan(job)
    assert error.value.status_code==503
    assert path.exists()

@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',reason='PostgreSQL serialization')
def test_concurrent_confirmations_queue_one_cleanup(client,spool):
    from concurrent.futures import ThreadPoolExecutor
    blob(spool);row=scan(client)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:confirm(client,row),range(2)))
    assert all(r['status']=='confirmed' for r in results)
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='media_storage_cleanup'))==1


@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',reason='PostgreSQL serialization')
def test_cancel_winning_owner_lock_stops_stale_cleanup(client,spool,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    path=blob(spool);row=scan(client);confirm(client,row)
    job=worker.claim();waiting=threading.Event();release=threading.Event();original=store.owner_lock
    def gate(db,user):
        if threading.current_thread().name.startswith('storage-cleaner'):
            record=db.get(store.MediaStorageRun,row['id'])
            if record and record.status=='cleaning':
                waiting.set();assert release.wait(10)
        return original(db,user)
    monkeypatch.setattr(store,'owner_lock',gate)
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='storage-cleaner') as pool:
        future=pool.submit(worker.perform,job)
        assert waiting.wait(10)
        result=client.post(ROOT+'/runs/'+row['id']+'/cancel');assert result.status_code==200
        release.set();future.result(timeout=10)
    assert path.exists() and get(client,row)['status']=='cancelled'
