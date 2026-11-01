"""Delegation acceptance on real persistence; LLM protocol replies are fixtures."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now, engine
from lingxi.models import Job, Memory, Message, Conversation, Provider, User
from lingxi.assistants import AssistantProfile
from lingxi.delegation import AgentRun, AgentNode, DelegationPlan
from lingxi import worker, gateway
from lingxi.memory import MemoryService


def create(client, **kw):
    response = client.post('/api/agent-runs',json={'request_id':'delegation-request-001',
        'title':'准备项目评审','goal':'整理评审目标与行动建议'} | kw)
    assert response.status_code == 200, response.text
    return response.json()


def fetch(client, row):
    return client.get('/api/agent-runs/'+row['id']).json()


def drain():
    for _ in range(40):
        job = worker.claim()
        if not job:
            return
        worker.perform(job)
    raise AssertionError('not idle')


def fixture_model(client, monkeypatch, count=1, dependencies=None, mutate=None):
    agents = client.get('/api/agents').json()
    identity = agents[0]['id']
    calls = []
    plan = {'mode':'direct' if count==1 else 'delegate','reason':'按任务依赖分工','nodes':[
        {'key':f'n{i}','title':f'节点{i}','instruction':f'分析项目第{i}部分', 'agent_id':identity,
         'depends_on':(dependencies or {}).get(i,[])} for i in range(count)]}
    def complete(model,provider,messages,purpose='chat',config=None):
        stage = config['stage']
        calls.append((stage,messages))
        if mutate:
            replacement = mutate(stage,messages)
            if replacement is not None:
                return replacement
        if stage == 'plan':
            return json.dumps(plan,ensure_ascii=False)
        if stage == 'node':
            context = json.loads(messages[-1]['content'])['context_bundle']
            ids = [m['id'] for m in context['memories']] + [d['id'] for d in context['dependencies']]
            return json.dumps({'summary':'分析建议，尚未执行','findings':[{'text':'可以先确认评审范围','evidence_ids':ids}], 'limitations':['没有联网或外部执行']},ensure_ascii=False)
        data = json.loads(messages[-1]['content'])
        ids = [n['id'] for n in data['nodes'] if n['status']=='succeeded']
        return json.dumps({'summary':'整理了评审建议','findings':[{'text':'建议核对范围','evidence_ids':ids}], 'limitations':[]},ensure_ascii=False)
    monkeypatch.setattr(gateway,'complete',complete)
    return plan,calls


@pytest.mark.parametrize('path',['/api/agents','/api/agent-runs','/api/agent-runs/missing'])
def test_authentication_required(client,path):
    with TestClient(app) as anonymous:
        assert anonymous.get(path).status_code == 401


def test_csrf(client):
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/agent-runs',json={'request_id':'request-001','title':'x','goal':'x'}).status_code == 403


def test_no_model_is_not_fake_ready(client):
    assert client.get('/api/agents').json() == []
    assert client.post('/api/agent-runs',json={'request_id':'request-001','title':'x','goal':'x'}).status_code == 409


def test_idempotency_and_fixed_config(configured):
    row=create(configured)
    assert create(configured)['id']==row['id']
    assert row['config']['max_nodes']==8 and row['config']['max_depth']==1
    assert configured.post('/api/agent-runs',json={'request_id':'delegation-request-001','title':'changed','goal':'x'}).status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun))==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='agent_plan'))==1


def test_direct_run_and_single_main_output(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'原会话'}).json()
    row=create(configured,conversation_id=c['id'])
    drain();detail=fetch(configured,row)
    assert detail['status']=='completed' and detail['calls_used']==3
    assert detail['decision']['mode']=='direct' and len(detail['nodes'])==1
    assert detail['nodes'][0]['result']['external_writes'] is False
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id==c['id']))==1
    drain()
    assert fetch(configured,row)['result_message_id']==detail['result_message_id']


def test_dag_parallel_capacity_and_dependency_context(configured,monkeypatch):
    plan,calls=fixture_model(configured,monkeypatch,6,{5:['n0','n4']})
    row=create(configured);worker.perform(worker.claim())
    detail=fetch(configured,row)
    assert sum(n['status']=='queued' for n in detail['nodes'])==3
    assert next(n for n in detail['nodes'] if n['key']=='n5')['status']=='pending'
    drain();detail=fetch(configured,row)
    assert detail['status']=='completed' and detail['calls_used']==8
    bundle=next(n for n in detail['nodes'] if n['key']=='n5')['context']
    assert {d['id'] for d in bundle['dependencies']}=={'n0','n4'}
    assert bundle['delegation_depth']==1
    assert not any('history' in c for c in bundle)


@pytest.mark.parametrize('change',[
    lambda p:p['nodes'][0].update(depends_on=['missing']),
    lambda p:p['nodes'][0].update(depends_on=['n0']),
    lambda p:p['nodes'][0].update(agent_id='foreign'),
    lambda p:p['nodes'][0].update(capability='shell'),
    lambda p:p['nodes'][0].update(capability='server.files'),
    lambda p:p.update(nodes=p['nodes']*9),
    lambda p:p.update(extra_permission=True),
    lambda p:p.update(nodes=[]),
])
def test_untrusted_plans_cannot_expand_grants(configured,monkeypatch,change):
    plan,_=fixture_model(configured,monkeypatch)
    change(plan)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='failed' and detail['nodes']==[]


def test_cyclic_dependency_rejected(configured,monkeypatch):
    plan,_=fixture_model(configured,monkeypatch,2,{0:['n1'],1:['n0']})
    row=create(configured);drain()
    assert fetch(configured,row)['status']=='failed'


def test_budget_checked_before_any_nodes(configured,monkeypatch):
    fixture_model(configured,monkeypatch,3)
    row=create(configured,max_calls=3);drain()
    assert fetch(configured,row)['status']=='failed'
    assert fetch(configured,row)['calls_used']==1


def test_cancel_before_plan_prevents_model_call(configured,monkeypatch):
    _,calls=fixture_model(configured,monkeypatch)
    row=create(configured)
    result=configured.post('/api/agent-runs/'+row['id']+'/cancel',json={'version':row['version']})
    assert result.status_code==200
    drain();assert calls==[] and fetch(configured,row)['status']=='cancelled'


def test_stale_cancel_is_conflict(configured):
    row=create(configured)
    assert configured.post('/api/agent-runs/'+row['id']+'/cancel',json={'version':1}).status_code==409


def test_cancel_during_model_does_not_publish(configured,monkeypatch):
    row=None
    def mutate(stage,messages):
        if stage=='node':
            d=fetch(configured,row)
            assert configured.post('/api/agent-runs/'+row['id']+'/cancel',json={'version':d['version']}).status_code==200
    fixture_model(configured,monkeypatch,mutate=mutate)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='cancelled' and not detail['result']
    assert all(n['status']=='cancelled' for n in detail['nodes'])


def test_memory_opt_in_and_category_subject_scope(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    with SessionLocal.begin() as db:
        db.add_all([Memory(id='project',user_id='owner',content='我的项目用 Python',category='projects',subject='self',source_ids=['manual']),
                    Memory(id='private',user_id='owner',content='私人偏好',category='preferences',subject='self',source_ids=[]),
                    Memory(id='foreign',user_id='other',content='他人资料',category='projects',subject='self',source_ids=[])])
    monkeypatch.setattr(MemoryService,'search',lambda self,db,user,q:list(db.scalars(select(Memory))))
    row=create(configured,share_memory=True);drain();detail=fetch(configured,row)
    bundle=detail['nodes'][0]['context']
    assert [m['id'] for m in bundle['memories']]==['project']
    assert detail['memory_refs']==[{'id':'project','version':1}]
    assert bundle['memories'][0]['source_ids']==['manual']


def test_no_memory_permission_means_no_retrieval(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    monkeypatch.setattr(MemoryService,'search',lambda *a:pytest.fail('retrieval without opt in'))
    row=create(configured);drain()
    assert fetch(configured,row)['nodes'][0]['context']['memory_status']=='not_allowed'


def test_memory_outage_is_explicit(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    def fail(*a):raise RuntimeError('private error')
    monkeypatch.setattr(MemoryService,'search',fail)
    row=create(configured,share_memory=True);drain()
    assert fetch(configured,row)['nodes'][0]['context']['memory_status']=='unavailable'


@pytest.mark.parametrize('timing',['during_node','during_summary','after_complete'])
def test_memory_correction_invalidates_all_derivatives(configured,monkeypatch,timing):
    with SessionLocal.begin() as db:
        db.add(Memory(id='project',user_id='owner',content='项目旧事实',category='projects',subject='self',source_ids=[]))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    def correct():
        with SessionLocal.begin() as db:
            m=db.get(Memory,'project')
            m.version+=1;m.content='项目新事实'
            from lingxi.memory import invalidate_runs
            invalidate_runs(db,m.id)
    fixture_model(configured,monkeypatch,mutate=lambda stage,m:correct() if ((timing=='during_node' and stage=='node') or (timing=='during_summary' and stage=='summary')) else None)
    row=create(configured,share_memory=True);drain()
    if timing=='after_complete':correct()
    detail=fetch(configured,row)
    assert detail['status']=='invalidated' and detail['result']=={}
    assert all(n['context']=={} and n['result']=={} for n in detail['nodes'])


def test_reference_injection_fails_node_and_blocks_dependents(configured,monkeypatch):
    def mutate(stage,m):
        if stage=='node':return json.dumps({'summary':'x','findings':[{'text':'x','evidence_ids':['foreign-memory']}],'limitations':[]})
    fixture_model(configured,monkeypatch,2,{1:['n0']},mutate)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='failed'
    assert [n['status'] for n in detail['nodes']]==['failed','blocked']


def test_independent_success_is_partial_and_retry_preserves_success(configured,monkeypatch):
    failing=[True]
    def mutate(stage,m):
        if stage=='node' and '第0部分' in m[-1]['content'] and failing[0]:
            raise RuntimeError('private-token should not escape')
    _,calls=fixture_model(configured,monkeypatch,2,mutate=mutate)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='partial' and 'private-token' not in json.dumps(detail)
    succeeded=next(n for n in detail['nodes'] if n['status']=='succeeded')
    failing[0]=False
    response=configured.post('/api/agent-runs/'+row['id']+'/retry',json={'version':detail['version']})
    assert response.status_code==200,response.text
    drain();detail=fetch(configured,row)
    assert detail['status']=='completed'
    assert next(n for n in detail['nodes'] if n['id']==succeeded['id'])['attempt']==1


def test_role_disable_prevents_call(configured,monkeypatch):
    _,calls=fixture_model(configured,monkeypatch)
    row=create(configured);worker.perform(worker.claim())
    with SessionLocal.begin() as db:
        role_id=db.get(AgentRun,row['id']).config['agents'][0]['role']['id']
        db.get(AssistantProfile,role_id).enabled=False
    drain();assert len(calls)==1
    assert fetch(configured,row)['status']=='failed'


def test_provider_change_prevents_call(configured,monkeypatch):
    _,calls=fixture_model(configured,monkeypatch)
    row=create(configured)
    with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    drain();assert not calls and fetch(configured,row)['status']=='failed'


def test_configuration_versions_are_pinned(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    row=create(configured)
    with SessionLocal.begin() as db:
        for p in db.scalars(select(AssistantProfile).where(AssistantProfile.kind=='persona')):
            p.version+=1;p.spec=p.spec|{'style':'new-style'}
    drain();detail=fetch(configured,row)
    assert detail['status']=='completed'
    assert detail['nodes'][0]['config']['persona']['version']==1


def test_interrupted_node_not_automatically_replayed(configured,monkeypatch):
    _,calls=fixture_model(configured,monkeypatch)
    row=create(configured);worker.perform(worker.claim());job=worker.claim()
    with SessionLocal.begin() as db:db.get(AgentNode,job.payload['node_id']).status='running'
    worker.perform(job);drain()
    assert len(calls)==1 and fetch(configured,row)['status']=='failed'


def test_deleted_conversation_invalidates_delegation(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'x'}).json()
    row=create(configured,conversation_id=c['id']);drain()
    from lingxi.memory import delete_conversation
    with SessionLocal.begin() as db:delete_conversation(db,db.get(Conversation,c['id']),False)
    detail=fetch(configured,row)
    assert detail['status']=='invalidated' and not detail['result']
    with SessionLocal() as db:assert not db.get(Message,detail['result_message_id']).content


def test_cross_owner_and_workspace_access(configured):
    row=create(configured)
    with SessionLocal.begin() as db:db.get(AgentRun,row['id']).user_id='other'
    assert configured.get('/api/agent-runs/'+row['id']).status_code==404
    for action in ('retry','cancel'):
        assert configured.post('/api/agent-runs/'+row['id']+'/'+action,json={'version':row['version']}).status_code==404
    assert configured.get('/api/agent-runs').json()['items']==[]


def test_list_search_and_cursor(configured):
    first=create(configured,title='旧评审')
    second=create(configured,title='新评审',request_id='delegation-request-002')
    page=configured.get('/api/agent-runs?limit=1').json()
    assert page['next_cursor']==second['id']
    assert configured.get('/api/agent-runs?cursor='+second['id']).json()['items'][0]['id']==first['id']
    assert len(configured.get('/api/agent-runs?q=旧').json()['items'])==1


def test_expired_memory_redacts_published_conversation_on_read(configured,monkeypatch):
    with SessionLocal.begin() as db:
        db.add(Memory(id='project',user_id='owner',content='项目期限',category='projects',subject='self',source_ids=[],expires_at=now()+60))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    fixture_model(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'x'}).json()
    row=create(configured,share_memory=True,conversation_id=c['id']);drain()
    with SessionLocal.begin() as db:db.get(Memory,'project').expires_at=now()-1
    messages=configured.get('/api/messages?conversation_id='+c['id']).json()
    assert all(not m['content'] and m['status']=='invalidated' for m in messages)
    assert configured.get('/api/agent-runs').json()['items'][0]['status']=='invalidated'


def test_summary_rejects_fabricated_node_references(configured,monkeypatch):
    fixture_model(configured,monkeypatch,mutate=lambda stage,m:json.dumps({'summary':'x','findings':[{'text':'x','evidence_ids':['fake']}],'limitations':[]}) if stage=='summary' else None)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='failed' and not detail['result_message_id']


def test_retry_cannot_exceed_budget(configured,monkeypatch):
    fixture_model(configured,monkeypatch,mutate=lambda stage,m:'{}' if stage=='node' else None)
    row=create(configured,max_calls=3);drain();detail=fetch(configured,row)
    assert configured.post('/api/agent-runs/'+row['id']+'/retry',json={'version':detail['version']}).status_code==200
    drain();detail=fetch(configured,row)
    assert detail['calls_used']==3
    assert configured.post('/api/agent-runs/'+row['id']+'/retry',json={'version':detail['version']}).status_code==409


def test_context_over_budget_fails_without_silent_truncation(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    from lingxi.models import Model
    with SessionLocal.begin() as db:db.get(Model,'chat').context_window=2600
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='failed' and '上下文超出' in detail['error']


@pytest.mark.skipif(__import__('os').name!='posix',reason='Linux authorized openat resource acceptance')
def test_readonly_files_require_task_and_role_intersection(configured,monkeypatch,tmp_path):
    from lingxi.config import settings
    (tmp_path/'readme.md').write_text('not sent to model')
    monkeypatch.setattr(settings(),'resource_dir',tmp_path)
    from test_resources import grant
    grant(configured, ['.'])
    agent=configured.get('/api/agents').json()[0]
    role_id=agent['id'].removeprefix('preset:')
    with SessionLocal.begin() as db:
        role=db.get(AssistantProfile,role_id);role.spec=role.spec|{'tools':['server.files']}
    plan,calls=fixture_model(configured,monkeypatch)
    plan['nodes'][0]['capability']='server.files'
    row=create(configured,allow_files=True);drain();detail=fetch(configured,row)
    assert detail['status']=='completed',detail
    assert detail['calls_used']==2
    assert detail['nodes'][0]['result']['entries'][0]['name']=='readme.md'
    assert 'not sent to model' not in json.dumps(calls)


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL multi-worker row locking')
def test_three_parallel_slots_remain_bounded_across_workers(configured,monkeypatch):
    lock=threading.Lock();active=[0];peak=[0]
    def mutate(stage,m):
        if stage=='node':
            with lock:
                active[0]+=1;peak[0]=max(peak[0],active[0])
            time.sleep(.1)
            with lock:active[0]-=1
    fixture_model(configured,monkeypatch,7,mutate=mutate)
    row=create(configured);worker.perform(worker.claim())
    def work():
        for _ in range(15):
            job=worker.claim()
            if job:worker.perform(job)
            else:time.sleep(.03)
    with ThreadPoolExecutor(max_workers=5) as pool:list(pool.map(lambda _:work(),range(5)))
    drain();detail=fetch(configured,row)
    assert detail['status']=='completed',detail
    assert 1 < peak[0] <= 3
    with SessionLocal() as db:
        keys=list(db.scalars(select(Job.key).where(Job.kind=='agent_node')))
        assert len(keys)==len(set(keys))==7


def test_dependency_does_not_bypass_role_memory_scope(configured,monkeypatch):
    plan,_=fixture_model(configured,monkeypatch,2,{1:['n0']})
    agents=configured.get('/api/agents').json()
    second=agents[1]
    plan['nodes'][1]['agent_id']=second['id']
    with SessionLocal.begin() as db:
        db.add(Memory(id='project',user_id='owner',content='项目保密上下文',category='projects',subject='self',source_ids=[]))
        role=db.get(AssistantProfile,second['id'].removeprefix('preset:'))
        role.spec=role.spec|{'context_categories':['procedures']}
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    row=create(configured,share_memory=True);drain();detail=fetch(configured,row)
    assert detail['status']=='partial'
    failed=next(n for n in detail['nodes'] if n['key']=='n1')
    assert failed['status']=='failed' and not failed['context']
    assert '角色范围外' in failed['error']


def test_delete_ledger_replay_invalidates_old_task_results(configured,monkeypatch):
    with SessionLocal.begin() as db:
        db.add(Memory(id='project',user_id='owner',content='旧项目事实',category='projects',subject='self',source_ids=[]))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    fixture_model(configured,monkeypatch)
    row=create(configured,share_memory=True);drain()
    from lingxi.memory import append_ledger,replay_ledger
    append_ledger({'kind':'memory','id':'project','user_id':'owner'})
    with SessionLocal.begin() as db:replay_ledger(db)
    detail=fetch(configured,row)
    assert detail['status']=='invalidated' and not detail['result']
    assert all(not n['result'] and not n['context'] for n in detail['nodes'])


def test_role_memory_scope_revocation_fences_inflight_result(configured,monkeypatch):
    with SessionLocal.begin() as db:
        db.add(Memory(id='project',user_id='owner',content='项目资料',category='projects',subject='self',source_ids=[]))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    def mutate(stage,messages):
        if stage=='node':
            role_id=json.loads(messages[-1]['content'])['role']['id']
            with SessionLocal.begin() as db:
                role=db.get(AssistantProfile,role_id)
                role.spec=role.spec|{'context_categories':[]}
    fixture_model(configured,monkeypatch,mutate=mutate)
    row=create(configured,share_memory=True);drain();detail=fetch(configured,row)
    assert detail['status']=='failed' and not detail['result']
    assert '记忆范围已缩小' in detail['nodes'][0]['error']


def test_connection_change_during_generation_prevents_result_commit(configured,monkeypatch):
    def mutate(stage,m):
        if stage=='node':
            with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    fixture_model(configured,monkeypatch,mutate=mutate)
    row=create(configured);drain();detail=fetch(configured,row)
    assert detail['status']=='failed' and not detail['result_message_id']
    assert not detail['nodes'][0]['result']
