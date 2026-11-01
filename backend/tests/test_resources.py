"""Real policy persistence and revocation gates; model replies remain protocol fixtures."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.models import Audit, Candidate, ChatRun, Conversation, Job, Memory, Message, User
from lingxi.resources import ResourcePolicy, current, require_snapshot, snapshot, valid
from lingxi.tasks import WorkTask, list_resource
from lingxi import worker


def preview(client, roots, version=None):
    if version is None:
        version = client.get('/api/resource-policy').json()['version']
    return client.post('/api/admin/resource-policy/previews', json={'version':version, 'roots':roots})


def confirm(client, response):
    assert response.status_code == 200, response.text
    return client.put('/api/admin/resource-policy', json={'token':response.json()['token'],
        'acknowledgement':'invalidate_previous_resource_contexts'})


def grant(client, roots):
    r = confirm(client, preview(client, roots))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def mounted(monkeypatch):
    monkeypatch.setattr('lingxi.tasks.resource_available', lambda: True)
    monkeypatch.setattr('lingxi.resources.validate_directories', lambda roots: None)


def submit(client, path='projects', request_id='resource-task-001', **kw):
    return client.post('/api/tasks', json={'request_id':request_id,'title':'资料清单',
        'skill_id':'server.files','input':{'path':path}} | kw)


def drain():
    for _ in range(40):
        job = worker.claim()
        if not job:
            return
        worker.perform(job)
    raise AssertionError('not idle')


def test_mount_does_not_implicitly_authorize(client, mounted):
    policy = client.get('/api/resource-policy').json()
    assert policy['mounted'] and not policy['available'] and policy['roots'] == [] and policy['version'] == 0
    assert submit(client).status_code == 403
    skill = next(s for s in client.get('/api/skills').json() if s['id']=='server.files')
    assert not skill['available'] and '资源授权' in skill['reason']
    assert not next(s for s in client.get('/api/plugin-capabilities').json() if s['id']=='server.files')['available']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkTask)) == 0


def test_preview_confirm_replay_and_immutable_task_snapshot(client, mounted):
    p = preview(client, ['projects/./docs'])
    assert p.json()['after']['roots'] == ['projects/docs']
    assert client.get('/api/resource-policy').json()['roots'] == []
    saved = confirm(client, p).json()
    assert saved['version'] == 1 and saved['roots'] == ['projects/docs']
    assert confirm(client, p).status_code == 409
    task = submit(client, 'projects/docs/research').json()
    assert task['config']['resource_policy'] == {'version':1,'user_id':'owner','workspace_id':'personal','roots':['projects/docs']}
    duplicate = submit(client, 'projects/docs/research').json()
    assert duplicate['id'] == task['id']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='resource_policy_update')) == 1


@pytest.mark.parametrize('roots', [['/etc'], ['../private'], ['C:\\private'], ['a/../../private'], ['a\nsecret'],
    ['projects','projects/docs'], ['projects','projects'], ['.','projects'], ['x'*401], ['']])
def test_invalid_or_overlapping_scope_never_changes_policy(client, mounted, roots):
    assert preview(client, roots).status_code == 422
    assert client.get('/api/resource-policy').json()['version'] == 0


@pytest.mark.parametrize('path', ['.', 'projects-old', 'project', 'other', 'projects-old/secret'])
def test_prefix_and_parent_directory_do_not_expand_scope(client, mounted, path):
    grant(client, ['projects'])
    assert submit(client, path).status_code == 403


def test_auth_csrf_foreign_session_expiry_tamper_and_explicit_ack(client, mounted, monkeypatch):
    with TestClient(app) as anon:
        assert anon.get('/api/resource-policy').status_code == 401
        assert anon.post('/api/admin/resource-policy/previews',json={'version':0,'roots':['.']}).status_code == 401
    p = preview(client, ['projects'])
    assert client.put('/api/admin/resource-policy',json={'token':p.json()['token']}).status_code == 422
    assert client.put('/api/admin/resource-policy',json={'token':'invalid-token-xx','acknowledgement':'invalidate_previous_resource_contexts'}).status_code == 409
    with TestClient(app) as other:
        other.headers['X-CSRF-Token'] = other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'}).json()['csrf']
        assert confirm(other, p).status_code == 409
    clock = now()
    monkeypatch.setattr('lingxi.resources.now', lambda: clock+302)
    assert confirm(client, p).status_code == 409
    monkeypatch.setattr('lingxi.resources.now', lambda: clock)
    client.headers.pop('X-CSRF-Token')
    assert confirm(client, p).status_code == 403


def test_changed_scope_stale_preview_and_noop(client, mounted):
    p = preview(client, ['projects'])
    grant(client, ['shared'])
    assert confirm(client, p).status_code == 409
    assert preview(client, ['projects'],version=0).status_code == 409
    assert preview(client, ['shared']).status_code == 409


def test_volume_offline_can_revoke_but_cannot_expand(client, mounted, monkeypatch):
    grant(client, ['projects'])
    monkeypatch.setattr('lingxi.tasks.resource_available', lambda: False)
    from lingxi import resources
    def unavailable(roots):
        if roots:
            raise HTTPException(409,'资料卷不可用')
    monkeypatch.setattr(resources,'validate_directories',unavailable)
    policy=client.get('/api/resource-policy').json()
    assert policy['roots'] == ['projects'] and not policy['available']
    assert preview(client,['other']).status_code == 409
    assert grant(client,[])['roots'] == []


def test_policy_isolated_by_owner_workspace_and_not_inherited_by_qq_actor(client, mounted):
    grant(client, ['projects'])
    with SessionLocal.begin() as db:
        owner=db.get(User,'owner'); ref=snapshot(db,owner)
        actor=User(id='actor',username='channel-'+'a'*32,password_hash='')
        other=User(id='other',username='other',password_hash='x',workspace_id='other-space')
        db.add_all([actor,other]);db.flush()
        assert current(db,actor).roots == [] and current(db,other).roots == []
        assert not valid(db,ref,actor) and not valid(db,ref,other)
        assert not valid(db,ref|{'workspace_id':'other-space'})
        with pytest.raises(HTTPException):
            snapshot(db,actor)


def test_revoke_queued_and_completed_tasks_cancels_sources_and_later_answers(client, mounted):
    grant(client, ['projects'])
    c=client.post('/api/conversations',json={'title':'资源讨论'}).json()['id']
    queued=submit(client).json()
    complete=submit(client,request_id='resource-task-002',conversation_id=c).json()
    with SessionLocal.begin() as db:
        message=Message(id='file-result',conversation_id=c,role='assistant',status='completed',content='sensitive-filename.md')
        db.add(message)
        t=db.get(WorkTask,complete['id']);t.status='succeeded';t.result={'entries':[{'name':'sensitive-filename.md'}]};t.result_message_id=message.id
        db.add(ChatRun(id='follow-on',user_id='owner',conversation_id=c,request_id='follow-on',request_hash='x',model_id='chat',
            status='completed',source_message_ids=[message.id]))
        db.add(Message(id='derived-result',conversation_id=c,role='assistant',status='completed',run_id='follow-on',content='derived-sensitive.md'))
    grant(client, [])
    for task in (queued,complete):
        state=client.get('/api/tasks/'+task['id']).json()
        assert state['status']=='cancelled' and state['result']=={} and state['config']['resource_revoked']
        assert client.post('/api/tasks/'+task['id']+'/retry',json={'version':state['version']}).status_code==409
    with SessionLocal() as db:
        assert db.get(Message,'file-result').content == ''
        assert db.get(Message,'derived-result').content == '' and db.get(ChatRun,'follow-on').status=='invalidated'
    grant(client,['projects'])
    drain()
    assert client.get('/api/tasks/'+queued['id']).json()['status']=='cancelled'
    assert submit(client,request_id='resource-task-new').status_code==200


def test_revocation_during_file_read_discards_late_result(client, mounted, monkeypatch):
    grant(client,['projects']); row=submit(client).json()
    def late(*a,**kw):
        grant(client,[])
        return {'verification':'filesystem_metadata','path':'projects','entries':[{'name':'late-secret'}],'truncated':False}
    monkeypatch.setattr('lingxi.task_worker.list_resource',late)
    drain()
    detail=client.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='cancelled' and detail['result']=={} and detail['result_message_id'] is None


def test_legacy_and_creation_race_are_invalidated_before_reading_context(client, mounted):
    grant(client,['projects']); row=submit(client).json()
    with SessionLocal.begin() as db:
        t=db.get(WorkTask,row['id']);t.config={};t.status='succeeded';t.result={'entries':[{'name':'legacy'}]}
    assert client.get('/api/tasks/'+row['id']).json()['result']=={}
    with SessionLocal() as db:
        assert db.get(WorkTask,row['id']).status=='cancelled'


def test_resource_change_invalidates_agent_nodes_candidates_and_sources(configured,mounted,monkeypatch):
    from test_delegation import create,fixture_model,drain as drain_agents
    from lingxi.delegation import AgentNode,AgentRun
    grant(configured,['projects'])
    fixture_model(configured,monkeypatch)
    run=create(configured,allow_files=True)
    drain_agents()
    with SessionLocal.begin() as db:
        node=db.scalar(select(AgentNode).where(AgentNode.run_id==run['id']))
        db.add(Candidate(id='pending',user_id='owner',source_id=node.id,fingerprint='pending',content='from-resource',category='projects',reason='待确认来源',
            origin={'kind':'agent','run_id':run['id'],'node_id':node.id,'round':1}))
    grant(configured,['projects','shared'])  # Even an expansion invalidates old resource contexts.
    r=configured.get('/api/agent-runs/'+run['id']).json()
    assert r['status']=='invalidated' and r['result']=={} and all(n['result']=={} and n['context']=={} for n in r['nodes'])
    with SessionLocal() as db:
        assert db.get(Candidate,'pending').status=='invalidated' and db.get(Candidate,'pending').content==''
        assert db.get(AgentRun,run['id']).cancel_requested


def test_accepted_resource_memory_retained_but_not_retrieved_or_revived(client,mounted):
    from lingxi.memory import usable
    grant(client,['projects'])
    with SessionLocal.begin() as db:
        ref=snapshot(db,db.get(User,'owner'))
        db.add(Memory(id='derived',user_id='owner',category='projects',content='confirmed-resource-fact',subject='self',
            source_ids=[],provenance={'kind':'agent','resources':[ref],'user_id':'owner','workspace_id':'personal'}))
    with SessionLocal() as db:
        assert usable(db.get(Memory,'derived'))
    grant(client,[])
    with SessionLocal() as db:
        m=db.get(Memory,'derived');assert m.status=='active' and m.content=='confirmed-resource-fact' and not usable(m)
    grant(client,['projects'])
    with SessionLocal() as db:
        assert not usable(db.get(Memory,'derived'))


def test_restore_clears_grants_and_advances_current_version(client,mounted):
    from lingxi.resources import checkpoint,restore
    grant(client,['projects'])
    with SessionLocal() as db:
        old=checkpoint(db)
    grant(client,['shared'])
    with SessionLocal.begin() as db:
        restore(db,old,db.get(User,'owner'))
    assert client.get('/api/resource-policy').json()['roots']==[]
    assert client.get('/api/resource-policy').json()['version']==3


def test_actual_backup_quarantine_never_restores_previous_grants(client,mounted):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    with SessionLocal.begin() as db:
        schema(db)
    grant(client,['projects'])
    data=checkpoint()
    grant(client,['shared'])
    quarantine(data)
    with SessionLocal() as db:
        p=current(db,db.get(User,'owner'))
        assert p.roots==[] and p.version==3


def test_late_delegation_plan_cannot_reintroduce_revoked_resource_scope(configured,mounted,monkeypatch):
    from test_delegation import create,fixture_model,drain as drain_agents
    def revoke(stage,messages):
        if stage=='plan':
            grant(configured,[])
    grant(configured,['projects'])
    fixture_model(configured,monkeypatch,mutate=revoke)
    run=create(configured,allow_files=True)
    drain_agents()
    result=configured.get('/api/agent-runs/'+run['id']).json()
    assert result['status']=='invalidated' and result['nodes']==[] and result['result']=={}


def test_missing_owner_and_legacy_agent_scope_fail_closed(configured,mounted):
    from test_delegation import create
    from lingxi.delegation import AgentRun
    from lingxi.agent_worker import validate_sources,AgentStopped,fail_agent
    grant(configured,['projects'])
    run=create(configured,allow_files=True)
    with SessionLocal.begin() as db:
        row=db.get(AgentRun,run['id']);row.config={k:v for k,v in row.config.items() if k!='resource_policy'}
        with pytest.raises(AgentStopped):
            validate_sources(db,row)
        job=db.scalar(select(Job).where(Job.kind=='agent_plan'))
        fail_agent(db,job,'失效来源')
        assert row.status=='invalidated'
        with pytest.raises(HTTPException):
            require_snapshot(db,None,{'resource_policy':snapshot(db,db.get(User,'owner'))})


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL concurrent worker commit fence')
def test_concurrent_revocation_returns_before_late_task_result_and_blocks_it(client,mounted,monkeypatch):
    import threading
    entered,release=threading.Event(),threading.Event()
    grant(client,['projects']);row=submit(client).json()
    def delayed(*args,**kw):
        entered.set()
        assert release.wait(15)
        return {'path':'projects','entries':[{'name':'late'}],'truncated':False,'verification':'filesystem_metadata'}
    monkeypatch.setattr('lingxi.task_worker.list_resource',delayed)
    job=worker.claim()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(worker.perform,job)
        try:
            assert entered.wait(10)
            assert grant(client,[])['roots']==[]
        finally:
            release.set()
        running.result(timeout=15)
    detail=client.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='cancelled' and detail['result']=={} and detail['result_message_id'] is None


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL concurrent row locking')
def test_parallel_confirmation_only_commits_one_revision(client,mounted):
    p=preview(client,['projects'])
    token=p.json()['token']; headers=dict(client.headers); cookies=dict(client.cookies)
    def save(_):
        with TestClient(app) as c:
            c.headers.update(headers);c.cookies.update(cookies)
            return c.put('/api/admin/resource-policy',json={'token':token,'acknowledgement':'invalidate_previous_resource_contexts'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save,range(2)))==[200,409]
    assert client.get('/api/resource-policy').json()['version']==1


@pytest.mark.skipif(os.name!='posix',reason='Linux openat container validation')
def test_real_directory_validation_symlinks_fd_cleanup_and_authorized_task(client,tmp_path,monkeypatch):
    from lingxi.config import settings
    root=tmp_path/'resources';root.mkdir();(root/'projects').mkdir()
    (root/'projects'/'readme.md').write_text('NEVER READ FILE BODY')
    (root/'outside').symlink_to(tmp_path,target_is_directory=True)
    monkeypatch.setattr(settings(),'resource_dir',root)
    assert preview(client,['missing']).status_code==409
    assert preview(client,['outside']).status_code==409
    p=preview(client,['projects'])
    (root/'projects').rename(root/'moved');(root/'projects').symlink_to(tmp_path,target_is_directory=True)
    assert confirm(client,p).status_code==409
    (root/'projects').unlink();(root/'moved').rename(root/'projects')
    grant(client,['projects']); row=submit(client).json();drain()
    detail=client.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='succeeded' and detail['result']['entries'][0]['name']=='readme.md'
    assert 'NEVER READ FILE BODY' not in json.dumps(detail)
    before=len(os.listdir('/proc/self/fd'))
    def stopped(): raise HTTPException(410,'revoked')
    for _ in range(50):
        with pytest.raises(HTTPException):
            list_resource('projects',stopped,grant={'roots':['projects']})
    assert len(os.listdir('/proc/self/fd'))<=before+1
