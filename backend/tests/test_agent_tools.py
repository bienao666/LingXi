"""A real persistent DAG, human confirmations and one-shot remote adapters."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from lingxi.api import app
from lingxi.db import SessionLocal,now
from lingxi.models import Job,User,Message
from lingxi.assistants import AssistantProfile
from lingxi.delegation import AgentRun,AgentNode
from lingxi.plugins import PluginInvocation
from lingxi.plugin_api import PluginConnection
from lingxi.agent_tools import reconcile
from lingxi.worker import claim,perform
from test_plugin_api import setup as api_setup,transport
from test_plugin_mcp import ready as mcp_setup
from test_delegation import drain


def enable_role(client):
    client.get('/api/agents')
    with SessionLocal.begin() as db:
        for role in db.scalars(select(AssistantProfile).where(AssistantProfile.kind=='role')):
            role.spec=role.spec|{'tools':['reason','plugin.call']};role.version+=1


def fixture(client,monkeypatch,adapter='api',depend=True,content='{"title":"本次已确认资料"}'):
    if adapter=='api':
        install,connection=api_setup(client)
        remote=transport(monkeypatch,lambda r:httpx.Response(200,json={'accepted':True}))
    else:
        remote,install,connection,_=mcp_setup(client,monkeypatch)
    enable_role(client)
    tools=client.get('/api/agent-tools').json();assert len(tools)==1
    tool=tools[0];calls=[]
    def complete(model,provider,messages,**kwargs):
        stage=kwargs.get('config',{}).get('stage');calls.append((stage,messages))
        if stage=='plan':
            data=json.loads(messages[-1]['content'])
            chosen=next(a for a in data['agents'] if 'plugin.call' in a['capabilities'])
            nodes=[dict(key='send',title='本次远程请求',instruction='只提交用户本次指定资料',
                agent_id=chosen['id'],capability='plugin.call',tool_key=tool['key'])]
            if depend:nodes.append(dict(key='review',title='核对结果',instruction='根据真实回执说明限制',
                agent_id=chosen['id'],depends_on=['send']))
            return json.dumps(dict(mode='delegate' if depend else 'direct',reason='远程完成后再分析',nodes=nodes))
        if stage=='agent_tool_parameters':return content
        if stage=='node':
            context=json.loads(messages[-1]['content'])['context_bundle']
            return json.dumps(dict(summary='已参考远程回执',findings=[dict(text='远端报告接受，业务效果待核',evidence_ids=[d['id'] for d in context['dependencies']])],limitations=[]))
        if stage=='summary':
            data=json.loads(messages[-1]['content'])
            return json.dumps(dict(summary='按真实节点形成汇总',findings=[dict(text='请核对服务端业务记录',evidence_ids=[n['id'] for n in data['nodes'] if n['status']=='succeeded'])],limitations=[]))
        return '[]'
    monkeypatch.setattr('lingxi.gateway.complete',complete)
    return tool,connection,remote,calls


def create(client,tool,**changes):
    r=client.post('/api/agent-runs',json=dict(request_id='agent-tools-request-001',title='远程协作',goal='提交指定资料后核对回执',tool_keys=[tool['key']])|changes)
    assert r.status_code==200,r.text
    return r.json()


def detail(client,row):
    r=client.get('/api/agent-runs/'+row['id']);assert r.status_code==200,r.text
    return r.json()


def preview(client,row):
    d=detail(client,row);n=next(n for n in d['nodes'] if n['capability']=='plugin.call')
    return client.post(f"/api/agent-runs/{row['id']}/nodes/{n['id']}/preview",json={'version':d['version']})


def confirm(client,p):
    return client.post('/api/plugin-invocations/'+p['id']+'/confirm',json={'digest':p['digest']})


@pytest.mark.parametrize('adapter',['api','mcp'])
def test_wait_confirm_then_dependency_and_summary(configured,monkeypatch,adapter):
    tool,_,remote,calls=fixture(configured,monkeypatch,adapter)
    row=create(configured,tool);drain()
    d=detail(configured,row)
    assert [n['status'] for n in sorted(d['nodes'],key=lambda n:n['key'])]==['pending','waiting_confirmation']
    assert d['calls_used']==2
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(PluginInvocation))==0
    p=preview(configured,row);assert p.status_code==200,p.text
    p=p.json();assert p['plan']['input']['title']=='本次已确认资料'
    assert p['plan']['effect']=='write'
    assert confirm(configured,p).status_code==200
    assert confirm(configured,p).status_code==200
    drain();reconcile();drain()
    d=detail(configured,row);assert d['status']=='completed' and d['calls_used']==4
    assert d['result']['external_writes'] is True
    received=[json.loads(ms[-1]['content'])['context_bundle'] for stage,ms in calls if stage=='node']
    assert received[0]['dependencies'][0]['result']['status']=='succeeded'
    if adapter=='api':assert len(remote)==1
    else:assert len([c for c in remote.calls if c.get('method')=='tools/call'])==1
    reconcile();drain()
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(PluginInvocation))==1


def test_scope_requires_role_task_and_connection(configured,monkeypatch):
    _,connection=api_setup(configured)
    tool=configured.get('/api/agent-tools').json()[0]
    body=dict(request_id='tools-denied-001',title='x',goal='x',tool_keys=[tool['key']])
    assert configured.post('/api/agent-runs',json=body).status_code==409
    enable_role(configured)
    assert configured.post('/api/agent-runs',json=body|{'tool_keys':['unknown']}).status_code==409
    assert configured.post('/api/agent-runs',json=body|{'tool_keys':[tool['key'],tool['key']]}).status_code==422
    configured.post('/api/plugin-connections/'+connection['id']+'/revoke',json={'version':connection['version']})
    assert configured.get('/api/agent-tools').json()==[]


@pytest.mark.parametrize('content',['null','[]','{"title":1}','{"title":"a","title":"b"}','{"title":"x","extra":"y"}'])
def test_invalid_parameters_do_not_propose_or_execute(configured,monkeypatch,content):
    tool,_,remote,_=fixture(configured,monkeypatch,content=content)
    row=create(configured,tool);drain()
    assert detail(configured,row)['status']=='failed' and not remote
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(PluginInvocation))==0


@pytest.mark.parametrize('phase',['before_preview','after_preview','after_confirm'])
def test_cancel_prevents_send_and_old_confirmation(configured,monkeypatch,phase):
    tool,_,remote,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=None
    if phase!='before_preview':p=preview(configured,row).json()
    if phase=='after_confirm':assert confirm(configured,p).status_code==200
    d=detail(configured,row)
    assert configured.post('/api/agent-runs/'+row['id']+'/cancel',json={'version':d['version']}).status_code==200
    if p:assert confirm(configured,p).status_code in (200,409,410)
    drain();reconcile();assert not remote
    assert detail(configured,row)['status']=='cancelled'


@pytest.mark.parametrize('change',['connection','role','plugin'])
def test_revocation_after_preview_blocks_direct_plugin_confirm(configured,monkeypatch,change):
    tool,connection,remote,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=preview(configured,row).json()
    if change=='connection':
        configured.post('/api/plugin-connections/'+connection['id']+'/revoke',json={'version':connection['version']})
    elif change=='role':
        with SessionLocal.begin() as db:
            for role in db.scalars(select(AssistantProfile).where(AssistantProfile.kind=='role')):
                role.spec=role.spec|{'tools':['reason']};role.version+=1
    else:
        configured.post('/api/plugin-installations/'+tool['installation_id']+'/disable',json={'version':tool['installation_version']})
    assert confirm(configured,p).status_code in (403,409,410)
    reconcile();drain();assert not remote
    assert detail(configured,row)['status']=='invalidated'


def test_timeout_is_unknown_no_dependency_no_retry(configured,monkeypatch):
    tool,_,_,_=fixture(configured,monkeypatch)
    sent=[]
    def fail(r):sent.append(r);raise httpx.ReadTimeout('synthetic timeout')
    transport(monkeypatch,fail)
    row=create(configured,tool);drain();p=preview(configured,row).json();confirm(configured,p)
    drain();reconcile();drain()
    d=detail(configured,row);assert d['status']=='failed'
    assert next(n for n in d['nodes'] if n['key']=='send')['result']['status']=='unknown'
    assert next(n for n in d['nodes'] if n['key']=='review')['status']=='blocked'
    assert configured.post('/api/agent-runs/'+row['id']+'/retry',json={'version':d['version']}).status_code==409
    assert len(sent)==1


def test_delete_conversation_clears_proposal_and_invocation_input(configured,monkeypatch):
    tool,_,remote,_=fixture(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'来源'}).json()
    row=create(configured,tool,conversation_id=c['id']);drain();p=preview(configured,row).json()
    assert configured.delete('/api/conversations/'+c['id']).status_code==200
    assert confirm(configured,p).status_code in (409,410)
    drain();reconcile();assert not remote
    with SessionLocal() as db:
        inv=db.get(PluginInvocation,p['id']);assert not inv.input and not inv.result
        assert not inv.config['api_preview']['plan']['input']
        assert all(not n.context for n in db.scalars(select(AgentNode).where(AgentNode.run_id==row['id'])))


def test_preview_original_session_and_expiry(configured,monkeypatch):
    tool,_,_,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=preview(configured,row).json()
    with TestClient(app) as other:
        login=other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
        other.headers['X-CSRF-Token']=login.json()['csrf']
        assert confirm(other,p).status_code==409
        fresh=preview(other,row);assert fresh.status_code==200
        fresh=fresh.json();assert fresh['id']!=p['id']
        assert confirm(configured,fresh).status_code==409


def test_task_owner_and_csrf_isolation(configured,monkeypatch):
    tool,_,_,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();d=detail(configured,row);n=d['nodes'][0]
    endpoint=f"/api/agent-runs/{row['id']}/nodes/{n['id']}/preview"
    with TestClient(app) as anon:
        assert anon.get('/api/agent-tools').status_code==401
        assert anon.post(endpoint,json={'version':d['version']}).status_code==401
    csrf=configured.headers.pop('X-CSRF-Token');assert configured.post(endpoint,json={'version':d['version']}).status_code==403
    configured.headers['X-CSRF-Token']=csrf
    with SessionLocal.begin() as db:db.get(AgentRun,row['id']).workspace_id='other'
    assert configured.post(endpoint,json={'version':d['version']}).status_code==404


def test_chat_collaboration_uses_same_scoped_pipeline(configured,monkeypatch):
    tool,_,remote,_=fixture(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'工具对话'}).json()
    body=dict(conversation_id=c['id'],request_id='chat-tools-request-001',content='提交指定资料后说明回执',
              collaboration={'tool_keys':[tool['key']]})
    r=configured.post('/api/chat',json=body);assert r.status_code==200,r.text
    assert configured.post('/api/chat',json=body).json()['id']==r.json()['id']
    drain()
    actions=configured.get('/api/chat-actions',params={'conversation_id':c['id']}).json()
    row={'id':actions[0]['target_id']};p=preview(configured,row).json();confirm(configured,p)
    drain();reconcile();drain()
    actions=configured.get('/api/chat-actions',params={'conversation_id':c['id']}).json()
    assert actions[0]['status']=='completed' and actions[0]['result']['external_writes']
    assert len(remote)==1


def test_no_task_scope_cannot_be_added_by_model(configured,monkeypatch):
    tool,_,remote,_=fixture(configured,monkeypatch)
    row=create(configured,tool,tool_keys=[]);drain()
    assert detail(configured,row)['status']=='failed' and not remote
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(PluginInvocation))==0


def test_expired_preview_replacement_never_confirms_old_input(configured,monkeypatch):
    tool,_,remote,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=preview(configured,row).json()
    with SessionLocal.begin() as db:
        inv=db.get(PluginInvocation,p['id'])
        inv.config=inv.config|{'api_preview':inv.config['api_preview']|{'expires_at':now()-1}}
    assert confirm(configured,p).status_code==409
    fresh=preview(configured,row).json();assert fresh['id']!=p['id']
    assert confirm(configured,p).status_code==409 and not remote
    assert confirm(configured,fresh).status_code==200
    drain();reconcile();drain();assert len(remote)==1


def test_memory_correction_cancels_unconfirmed_tool_and_clears_input(configured,monkeypatch):
    from lingxi.models import Memory
    tool,_,remote,_=fixture(configured,monkeypatch)
    with SessionLocal.begin() as db:
        for role in db.scalars(select(AssistantProfile).where(AssistantProfile.kind=='role')):
            role.spec=role.spec|{'context_categories':['preferences']}
        m=Memory(user_id='owner',content='评审偏好蓝色',category='preferences',source_ids=[])
        db.add(m);db.flush();memory_id=m.id
    monkeypatch.setattr('lingxi.memory.MemoryService.search',lambda self,db,user,q:list(db.scalars(select(Memory))))
    row=create(configured,tool,share_memory=True);drain();p=preview(configured,row).json()
    with SessionLocal.begin() as db:
        from lingxi.memory import MemoryService
        MemoryService().correct(db,db.get(Memory,memory_id),{'content':'评审偏好绿色'})
    assert confirm(configured,p).status_code in (409,410)
    reconcile();assert not remote and detail(configured,row)['status']=='invalidated'
    with SessionLocal() as db:assert not db.get(PluginInvocation,p['id']).input


def test_cancellation_during_remote_response_never_publishes_late_body(configured,monkeypatch):
    tool,_,_,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=preview(configured,row).json()
    def cancel_during_send(request):
        d=detail(configured,row)
        assert configured.post('/api/agent-runs/'+row['id']+'/cancel',json={'version':d['version']}).status_code==200
        return httpx.Response(200,json={'private_result':'late body'})
    calls=transport(monkeypatch,cancel_during_send)
    confirm(configured,p);drain();reconcile()
    with SessionLocal() as db:
        inv=db.get(PluginInvocation,p['id']);assert inv.status=='unknown' and not inv.result
        assert inv.receipt.get('late_response')
    assert len(calls)==1 and detail(configured,row)['status']=='cancelled'


def test_tool_revoked_during_downstream_model_does_not_publish(configured,monkeypatch):
    tool,connection,remote,_=fixture(configured,monkeypatch)
    row=create(configured,tool);drain();p=preview(configured,row).json();confirm(configured,p)
    drain();reconcile()
    from lingxi.gateway import complete as original
    def revoke(model,provider,messages,**kwargs):
        result=original(model,provider,messages,**kwargs)
        if kwargs.get('config',{}).get('stage')=='node':
            configured.post('/api/plugin-connections/'+connection['id']+'/revoke',json={'version':connection['version']})
        return result
    monkeypatch.setattr('lingxi.gateway.complete',revoke)
    drain()
    d=detail(configured,row)
    assert d['status']=='invalidated' and not d['result']
    assert all(not n['result'] for n in d['nodes']) and len(remote)==1
