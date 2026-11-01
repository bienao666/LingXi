"""Request admission evidence; synthetic transports never claim real billing caps."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lingxi import gateway, model_budgets as b
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.models import ChatRun, Conversation, Model, ModelCall, Provider


def save(client, daily=0, execution=0, version=None):
    if version is None:version=client.get('/api/admin/model-budgets').json()['version']
    p=client.post('/api/admin/model-budgets/previews',json={
        'version':version,'daily_limit':daily,'execution_limit':execution})
    assert p.status_code==200,p.text
    body={'token':p.json()['token'],'acknowledgement':'request_attempts_not_money'}
    r=client.put('/api/admin/model-budgets',json=body)
    assert r.status_code==200,r.text
    return r.json(),body


def pair():
    with SessionLocal() as db:return db.get(Model,'chat'),db.get(Provider,'provider')


def transport(monkeypatch, outcome=None):
    seen=[]
    def send(req):
        seen.append(req)
        if isinstance(outcome,Exception):raise outcome
        return outcome or httpx.Response(200,json={'choices':[{'message':{'content':'合成结果'}}]})
    monkeypatch.setattr(gateway,'client_for',lambda p:httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(send)))
    return seen


def count():
    with SessionLocal() as db:return db.scalar(select(func.count()).select_from(b.ModelDispatch))


def test_defaults_auth_and_confirmation(client):
    value=client.get('/api/admin/model-budgets').json()
    assert value['daily_limit']==value['execution_limit']==value['today_attempts']==0
    with TestClient(app) as anon:assert anon.get('/api/admin/model-budgets').status_code==401
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/admin/model-budgets/previews',json={'version':0}).status_code==403
    client.headers['X-CSRF-Token']=token
    value,body=save(client,2,1)
    assert value['version']==1
    assert client.put('/api/admin/model-budgets',json=body).json()['version']==1
    assert client.post('/api/admin/model-budgets/previews',json={'version':0}).status_code==409
    assert client.put('/api/admin/model-budgets',json=body|{'acknowledgement':'money'}).status_code==422
    with TestClient(app) as other:
        response=other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
        other.headers['X-CSRF-Token']=response.json()['csrf']
        assert other.put('/api/admin/model-budgets',json=body).status_code==409


@pytest.mark.parametrize('changes',[{'daily_limit':-1},{'execution_limit':10001},{'daily_limit':True},
    {'daily_limit':1.5},{'daily_limit':'10'},{'unexpected':1}])
def test_invalid_config(client,changes):
    assert client.post('/api/admin/model-budgets/previews',json={'version':0}|changes).status_code==422


def test_preview_expiry_tamper_and_concurrent_version(client,monkeypatch):
    p=client.post('/api/admin/model-budgets/previews',json={'version':0,'daily_limit':3}).json()
    body={'token':p['token'],'acknowledgement':'request_attempts_not_money'}
    assert client.put('/api/admin/model-budgets',json=body|{'token':'x'*40}).status_code==409
    save(client,5)
    assert client.put('/api/admin/model-budgets',json=body).status_code==409
    monkeypatch.setattr(b,'now',lambda:p['expires_at']+1)
    assert client.put('/api/admin/model-budgets',json=body).status_code==409


@pytest.mark.parametrize('outcome',[None,httpx.ConnectError('secret'),httpx.Response(429),httpx.Response(503)])
def test_transport_precharge_failure_counts_and_no_network_after_limit(configured,monkeypatch,outcome):
    save(configured,1)
    seen=transport(monkeypatch,outcome)
    m,p=pair()
    try:gateway.complete(m,p,[{'role':'user','content':'fixture'}])
    except Exception:pass
    assert count()==len(seen)==1
    with pytest.raises(b.BudgetExceeded):gateway.complete(m,p,[])
    assert len(seen)==count()==1
    with SessionLocal() as db:
        attempt=db.scalar(select(b.ModelDispatch))
        assert attempt.call_id and attempt.status in ('returned','interrupted')
        calls=list(db.scalars(select(ModelCall)))
        assert len(calls)==2 and '_dispatch_ids' not in repr([c.config for c in calls])
        assert any(c.billing['status']=='unknown' for c in calls)


def test_daily_utc_reset_config_change_never_resets_usage(configured,monkeypatch):
    save(configured,1);m,p=pair();seen=transport(monkeypatch)
    t=now();day=int(t//86400)*86400
    monkeypatch.setattr(b,'now',lambda:day+1)
    gateway.complete(m,p,[])
    save(configured,0);gateway.complete(m,p,[])
    save(configured,1)
    with pytest.raises(b.BudgetExceeded):gateway.complete(m,p,[])
    monkeypatch.setattr(b,'now',lambda:day+86401)
    gateway.complete(m,p,[])
    assert len(seen)==count()==3
    assert configured.get('/api/admin/model-budgets').json()['today_attempts']==1


def chat_root(client):
    c=client.post('/api/conversations',json={}).json()['id']
    r=client.post('/api/chat',json={'conversation_id':c,'request_id':'budget-request-0001','content':'你好'})
    assert r.status_code==200,r.text
    return r.json()['id']


def test_execution_root_lifetime_and_background(configured,monkeypatch):
    rid=chat_root(configured);save(configured,20,1);transport(monkeypatch);m,p=pair()
    gateway.complete(m,p,[],config={'run_id':rid})
    # Crossing the UTC day does not reset this execution's count.
    stamp=now()+86400;monkeypatch.setattr(b,'now',lambda:stamp)
    with pytest.raises(b.BudgetExceeded):gateway.complete(m,p,[],config={'run_id':rid})
    gateway.complete(m,p,[]);gateway.complete(m,p,[])
    v=configured.get('/api/admin/model-budgets').json()
    assert v['recent_executions'][0]['attempts']==1 and count()==3


def test_invalid_root_and_provider_cannot_dispatch(configured,monkeypatch):
    seen=transport(monkeypatch);m,p=pair()
    with pytest.raises(HTTPException):gateway.complete(m,p,[],config={'run_id':'not-owned'})
    p.workspace_id='different'
    with pytest.raises(HTTPException):gateway.complete(m,p,[])
    assert not seen and not count()


@pytest.mark.parametrize('change',['disabled','version'])
def test_last_admission_rechecks_provider_config(configured,change):
    _,p=pair()
    with SessionLocal.begin() as db:
        row=db.get(Provider,p.id)
        if change=='disabled':row.enabled=False
        else:row.version+=1
    with pytest.raises(HTTPException):b.reserve(p,{})
    assert count()==0


def test_stream_cancel_and_interrupted_reservation_remain(configured,monkeypatch):
    rid=chat_root(configured);save(configured,2)
    raw='data: '+json.dumps({'choices':[{'delta':{'content':'合成'}}]})+'\n\ndata: [DONE]\n\n'
    seen=transport(monkeypatch,httpx.Response(200,text=raw))
    m,p=pair();stream=gateway.stream(m,p,[],rid,lambda:None)
    assert next(stream)=='合成';stream.close()
    # A process can disappear after this committed reservation.
    b.reserve(p,{})
    with pytest.raises(b.BudgetExceeded):gateway.complete(m,p,[])
    assert len(seen)==1 and count()==2
    assert configured.get('/api/admin/model-budgets').json()['unsettled_attempts']==1
    from lingxi.model_routing import retryable
    assert not retryable(b.BudgetExceeded('daily'))


@pytest.mark.parametrize('method,headers,expected',[('GET',{},0),('POST',{},1),('GET',{'Upgrade':'websocket'},1)])
def test_admission_hook_post_and_real_upgrade_request(configured,method,headers,expected):
    _,p=pair();seen=[]
    factory=lambda _:httpx.Client(transport=httpx.MockTransport(lambda req:(seen.append(req) or httpx.Response(200))))
    with b.client(p,factory,{}) as upstream:upstream.request(method,'https://example.com/protocol',headers=headers)
    assert count()==expected and len(seen)==1


def test_validation_block_preserves_verified_model(configured,monkeypatch):
    save(configured,1);m,p=pair();b.reserve(p,{})
    seen=transport(monkeypatch)
    response=configured.post('/api/models/chat/test')
    assert response.status_code==429,response.text
    with SessionLocal() as db:assert db.get(Model,'chat').status=='verified'
    assert not seen


@pytest.mark.parametrize('route,purpose',[('chat/completions','extraction'),('embeddings','embedding'),('rerank','rerank')])
def test_internal_memory_proxy_blocked_before_network(configured,monkeypatch,route,purpose):
    from lingxi.config import settings
    from lingxi.models import Policy
    if purpose!='extraction':
        with SessionLocal.begin() as db:
            db.add(Model(id=purpose,provider_id='provider',name='fixture',purpose=purpose,status='verified',verified_provider_version=1))
            db.add(Policy(purpose=purpose,model_id=purpose))
    save(configured,1);_,p=pair();b.reserve(p,{})
    monkeypatch.setattr(settings(),'memory_gateway_token','fixture-service-token')
    seen=transport(monkeypatch)
    monkeypatch.setattr('lingxi.api.client_for',gateway.client_for)
    r=configured.post('/internal/model/v1/'+route,json={},headers={'Authorization':'Bearer fixture-service-token'})
    assert r.status_code==429,r.text
    assert not seen and count()==1


def test_audio_and_native_websocket_blocked_at_transport(configured,monkeypatch):
    from lingxi.voice import audio_call
    from test_voice import wav
    from lingxi.voice_native_output import native_tts
    save(configured,1);_,p=pair();b.reserve(p,{})
    seen=transport(monkeypatch)
    for target in ('lingxi.voice.client_for','lingxi.voice_native_output.client_for'):
        monkeypatch.setattr(target,gateway.client_for)
    row=SimpleNamespace(version=1,spec={'language':'zh','voice':'fixture','speed':1})
    for kind in ('asr','tts'):
        with pytest.raises(b.BudgetExceeded):audio_call(row,p,kind,'fixture',data=wav(),text='fixture')
    with pytest.raises(HTTPException) as error:native_tts(row,p,'fixture','合成','fixture',1)
    assert error.value.status_code==429 and not seen and count()==1


def test_realtime_relay_reports_quota_before_upstream_upgrade(configured,monkeypatch):
    import queue,threading
    from test_voice_realtime import setup,ticket
    from lingxi.voice_realtime import claim,relay
    setup(configured)
    token=ticket(configured)
    identity,login=claim(token,configured.cookies,configured.headers)
    save(configured,1);_,p=pair();b.reserve(p,{})
    seen=transport(monkeypatch)
    monkeypatch.setattr('lingxi.voice_realtime.client_for',gateway.client_for)
    outgoing=queue.Queue();stop=threading.Event()
    relay(identity,login,queue.Queue(),outgoing,stop)
    event=outgoing.get_nowait()
    assert event['type']=='error' and event['status']==429
    assert not seen and count()==1 and stop.is_set()


def test_parallel_reservations_cannot_overspend(configured):
    save(configured,3);_,p=pair()
    def attempt(_):
        try:return b.reserve(p,{})
        except b.BudgetExceeded:return None
    with ThreadPoolExecutor(max_workers=6) as pool:result=list(pool.map(attempt,range(12)))
    assert len([v for v in result if v])==count()==3


def test_restore_union_current_policy_old_preview_invalid(configured):
    from lingxi.backups import checkpoint,quarantine
    from sqlalchemy import text
    from lingxi.workspace_migrations import SCHEMA
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)'))
        db.execute(text('DELETE FROM alembic_version'))
        db.execute(text('INSERT INTO alembic_version VALUES (:v)'),{'v':SCHEMA})
    save(configured,6);_,p=pair();first=b.reserve(p,{})
    with SessionLocal() as db:old=b.checkpoint(db)
    save(configured,2);second=b.reserve(p,{})
    preview=configured.post('/api/admin/model-budgets/previews',json={'version':2,'daily_limit':100}).json()
    latest=checkpoint()
    # Restore the older budget rows to emulate a database restore.
    with SessionLocal.begin() as db:
        from sqlalchemy import delete
        db.execute(delete(b.ModelDispatch));db.execute(delete(b.ModelBudget))
        for row in old['policies']:db.add(b.ModelBudget(**row))
        for row in old['attempts']:db.add(b.ModelDispatch(**row))
    quarantine(latest)
    with SessionLocal() as db:
        assert db.get(b.ModelBudget,'personal').daily_limit==2
        assert db.get(b.ModelBudget,'personal').version==3
        assert db.get(b.ModelDispatch,first) and db.get(b.ModelDispatch,second)
    # Quarantine revokes sessions; old confirmation remains invalid after login.
    response=configured.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
    configured.headers['X-CSRF-Token']=response.json()['csrf']
    assert configured.put('/api/admin/model-budgets',json={'token':preview['token'],'acknowledgement':'request_attempts_not_money'}).status_code==409


@pytest.mark.parametrize('mutation',['workspace','nan','duplicate','reference','limit','type'])
def test_checkpoint_rejects_invalid_before_restore(configured,mutation):
    _,p=pair();b.reserve(p,{})
    with SessionLocal() as db:data=b.checkpoint(db)
    row=data['attempts'][0]
    if mutation=='workspace':row['workspace_id']='other'
    elif mutation=='nan':row['created_at']=float('nan')
    elif mutation=='duplicate':data['attempts'].append(copy.deepcopy(row))
    elif mutation=='reference':row['root_id']='invalid-background'
    elif mutation=='limit':data['policies'][0]['daily_limit']=-1
    else:data['attempts']=[None]
    with pytest.raises((ValueError,TypeError)):b.validate_checkpoint(data,{'workspace_id':'personal'})


def migration():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('budget_migration',Path(__file__).parents[1]/'migrations/versions/0052_model_budgets.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('populated',['policy','attempt'])
def test_migration_cannot_lose_governance_state(configured,populated):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    if populated=='policy':save(configured,2)
    else:
        _,p=pair();b.reserve(p,{})
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError,match='调用限额'):migration().downgrade()


def test_empty_migration_roundtrip(client):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            m=migration();m.downgrade()
            assert 'model_dispatches' not in inspect(connection).get_table_names()
            m.upgrade()
            assert 'model_dispatches' in inspect(connection).get_table_names()


@pytest.mark.parametrize('kind',['task','agent','plugin'])
def test_durable_execution_scopes_obey_lifetime_limit(configured,kind):
    from lingxi.tasks import WorkTask
    from lingxi.delegation import AgentRun
    from lingxi.plugins import PluginInvocation
    common={'request_id':'budget-scope-01','request_hash':'fixture','config':{}}
    models={'task':(WorkTask,'task_id',common|{'title':'合成','skill_id':'project.plan','input':{}}),
        'agent':(AgentRun,'agent_run_id',common|{'title':'合成','goal':'合成计划'}),
        'plugin':(PluginInvocation,'invocation_id',common|{'installation_id':'fixture','release_id':'fixture','capability_id':'fixture','input':'合成'})}
    entity,key,fields=models[kind]
    with SessionLocal.begin() as db:
        row=entity(user_id='owner',**fields);db.add(row);db.flush();identity=row.id
    save(configured,20,1);_,p=pair()
    b.reserve(p,{key:identity})
    with pytest.raises(b.BudgetExceeded):b.reserve(p,{key:identity})
    assert configured.get('/api/admin/model-budgets').json()['recent_executions'][0]['kind']==kind
