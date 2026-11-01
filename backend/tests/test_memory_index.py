"""Real authority/worker transitions; upstream retain is a protocol fixture."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.memory import HindsightAdapter, MemoryService, enqueue
from lingxi.models import Job, Memory, Policy, User
from lingxi import worker


def saved(client):
    row = client.post('/api/memories', json={'content': '我喜欢红茶'}).json()
    return row['id']


def state(client, identity):
    r = client.get(f'/api/memories/{identity}/index')
    assert r.status_code == 200, r.text
    return r.json()


def retry(client, identity, **extra):
    return client.post(f'/api/memories/{identity}/retry', json={'version': 1, 'epoch': 0} | extra)


@pytest.mark.parametrize('key', ['index:{id}:1', 'reindex:0:{id}:1', 'restore-index:backup:{id}', 'index-recovery:0:{id}:1'])
def test_all_creation_paths_retry_one_current_job_and_clear_old_lease(client, key):
    identity = saved(client)
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == 'index'))
        job.key, job.status, job.attempts = key.format(id=identity), 'failed', 5
        job.error, job.lease_token, job.lease_until = 'private-service-error', 'old-worker', now()-10
        db.get(Memory, identity).index_status = 'failed'
    before = state(client, identity)
    assert before['can_retry'] and before['status'] == 'failed' and before['attempts'] == 5
    assert 'private-service-error' not in str(before)
    assert retry(client, identity).json()['status'] == 'pending'
    assert retry(client, identity).json()['status'] == 'pending'
    with SessionLocal() as db:
        jobs = db.scalars(select(Job).where(Job.kind == 'index')).all()
        assert len(jobs) == 1
        assert jobs[0].attempts == 0 and jobs[0].error is None and jobs[0].lease_token is None


def test_missing_job_can_be_recovered_without_changing_fact(client, monkeypatch):
    identity = saved(client)
    with SessionLocal.begin() as db:
        db.execute(delete(Job).where(Job.kind == 'index'))
    assert state(client, identity)['status'] == 'missing'
    assert retry(client, identity).json()['status'] == 'pending'
    calls = []
    monkeypatch.setattr(HindsightAdapter, 'retain', lambda *a: calls.append(a[1:]))
    worker.perform(worker.claim())
    assert calls == [('owner', identity+'-v1', '我喜欢红茶', 0)]
    assert state(client, identity)['status'] == 'ready'
    assert not state(client, identity)['can_retry']
    assert retry(client, identity).json()['status'] == 'ready'
    with SessionLocal() as db:
        assert db.get(Memory, identity).content == '我喜欢红茶'
        assert db.scalar(select(Job)).status == 'completed'


def test_new_index_jobs_pin_bank_at_enqueue_and_old_bank_never_runs(client, monkeypatch):
    identity = saved(client)
    with SessionLocal.begin() as db:
        assert db.scalar(select(Job)).payload['epoch'] == 0
        db.add(Policy(purpose='embedding', model_id='unused', version=2))
        enqueue(db, 'reindex:2:'+identity+':1', 'index', {'memory_id': identity, 'version': 1})
    calls = []
    monkeypatch.setattr(HindsightAdapter, 'retain', lambda *a: calls.append(a[-1]))
    worker.perform(worker.claim())
    assert not calls
    worker.perform(worker.claim())
    assert calls == [2]
    assert state(client, identity)['status'] == 'ready'


def test_old_legacy_reindex_cannot_fail_new_generation(client):
    identity = saved(client)
    with SessionLocal.begin() as db:
        db.add(Policy(purpose='embedding', model_id='unused', version=3))
        job = db.scalar(select(Job))
        job.payload = {'memory_id': identity, 'version': 1}
        job.key, job.status, job.attempts = 'reindex:1:'+identity+':1', 'failed', 5
        worker.finish_failed(db, job, 'synthetic failure')
        assert db.get(Memory, identity).index_status == 'pending'
    assert state(client, identity)['status'] == 'missing'
    assert retry(client, identity, epoch=3).json()['status'] == 'pending'
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.key.startswith('reindex:'))).status == 'failed'


def test_terminal_duplicate_does_not_mark_current_ready_index_failed(client):
    identity = saved(client)
    with SessionLocal.begin() as db:
        memory = db.get(Memory, identity)
        memory.index_status = 'ready'
        worker.finish_failed(db, db.scalar(select(Job)), 'late failure')
        assert memory.index_status == 'ready'


@pytest.mark.parametrize('change', ['correct', 'delete', 'expire', 'policy'])
def test_stale_retry_cannot_revive_or_replace_changed_authority(client, change):
    identity = saved(client)
    with SessionLocal.begin() as db:
        db.scalar(select(Job)).status = 'failed'
        db.get(Memory, identity).index_status = 'failed'
        if change == 'expire':
            db.get(Memory, identity).expires_at = now()-1
        elif change == 'policy':
            db.add(Policy(purpose='embedding', model_id='unused', version=2))
    if change == 'correct':
        assert client.patch('/api/memories/'+identity, json={'content':'我喜欢绿茶', 'version':1}).status_code == 200
    elif change == 'delete':
        assert client.delete('/api/memories/'+identity).status_code == 200
    assert retry(client, identity).status_code == 409
    with SessionLocal() as db:
        old = db.scalar(select(Job).where(Job.payload['version'].as_integer() == 1, Job.kind == 'index'))
        assert old.status == 'failed'


def test_running_job_keeps_lease_and_backoff_is_visible(client):
    identity = saved(client)
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job))
        job.available_at, job.attempts = now()+100, 2
    assert state(client, identity)['retry_at'] > now()
    assert not state(client, identity)['can_retry']
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job))
        job.status, job.lease_token, job.lease_until = 'running', 'keep-lease', now()-1
    assert retry(client, identity).json()['status'] == 'running'
    with SessionLocal() as db:
        assert db.scalar(select(Job)).lease_token == 'keep-lease'


def test_legacy_creation_job_pins_epoch_before_network_and_retries(client, monkeypatch):
    identity = saved(client)
    with SessionLocal.begin() as db:
        db.scalar(select(Job)).payload = {'memory_id': identity, 'version': 1}
    def unavailable(*args):
        with SessionLocal() as db:
            assert db.scalar(select(Job)).payload['epoch'] == 0
        raise RuntimeError('private data must not escape')
    monkeypatch.setattr(HindsightAdapter, 'retain', unavailable)
    job = worker.claim()
    worker.perform(job)
    value = state(client, identity)
    assert value['status'] == 'pending' and value['attempts'] == 1 and value['retry_at'] > now()
    with SessionLocal.begin() as db:
        current = db.get(Job, job.id)
        current.available_at, current.attempts = now(), 4
    worker.perform(worker.claim())
    assert state(client, identity)['status'] == 'failed'


def test_policy_changes_during_retain_queue_old_bank_cleanup(client, monkeypatch):
    identity = saved(client)
    def changed(*a):
        with SessionLocal.begin() as db:
            db.add(Policy(purpose='embedding', model_id='unused', version=7))
    monkeypatch.setattr(HindsightAdapter, 'retain', changed)
    job = worker.claim()
    worker.perform(job)
    assert state(client, identity)['status'] == 'missing'
    with SessionLocal() as db:
        cleanup = db.scalar(select(Job).where(Job.key == 'late-cleanup:'+job.id))
        assert cleanup.payload['epochs'] == [0]
        assert db.get(Memory, identity).index_status != 'ready'


def test_auth_csrf_owner_workspace_and_malformed_retry(client):
    identity = saved(client)
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/memories/'+identity+'/index').status_code == 401
        assert retry(anonymous, identity).status_code == 401
    assert client.post('/api/memories/'+identity+'/retry', json={}).status_code == 422
    assert client.post('/api/memories/'+identity+'/retry', json={'version':1, 'epoch':-1}).status_code == 422
    token = client.headers.pop('X-CSRF-Token')
    assert retry(client, identity).status_code == 403
    client.headers['X-CSRF-Token'] = token
    with SessionLocal.begin() as db:
        db.get(Memory, identity).workspace_id = 'foreign'
    assert client.get('/api/memories/'+identity+'/index').status_code == 404
    assert retry(client, identity).status_code == 404


def test_foreign_job_cannot_publish_or_expose_attempts(client, monkeypatch):
    identity = saved(client)
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job))
        job.workspace_id, job.attempts = 'foreign', 97
    assert state(client, identity)['status'] == 'missing'
    calls = []
    monkeypatch.setattr(HindsightAdapter, 'retain', lambda *a: calls.append(a))
    worker.perform(worker.claim())
    assert not calls
    assert state(client, identity)['attempts'] == 0


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='PostgreSQL row locking')
def test_parallel_retry_serializes_one_job(client):
    identity = saved(client)
    with SessionLocal.begin() as db:
        db.execute(delete(Job).where(Job.kind == 'index'))
    gate = threading.Barrier(2)
    def invoke(_):
        from lingxi.memory_index import IndexRetry, retry as restart
        from lingxi.web import owned
        with SessionLocal.begin() as db:
            gate.wait(timeout=10)
            memory = owned(db, Memory, identity, db.get(User, 'owner'), lock=True)
            return restart(db, memory, IndexRetry(version=1, epoch=0)).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(invoke, range(2))) == ['pending', 'pending']
    with SessionLocal() as db:
        assert len(db.scalars(select(Job).where(Job.kind == 'index')).all()) == 1
