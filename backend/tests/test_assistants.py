"""Real persistence, version boundaries, authorization and prompt integration."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.api import app
from lingxi.assistants import (AssistantProfile, ProfileRevision, SupportPreference,
    seed_defaults, support_view)
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Job, Memory, Model, Provider
from lingxi import worker


def create(client, kind='personas', **values):
    response = client.post('/api/'+kind, json={'name':'测试配置'} | values)
    assert response.status_code == 200, response.text
    return response.json()


def conversation(client):
    return client.post('/api/conversations', json={'title':'人格验收'}).json()['id']


def select_persona(client, identity, profile, version=0):
    return client.put(f'/api/conversations/{identity}/persona', json={'persona_id':profile,'version':version})


def chat(client, identity, request_id='assistant-request-001'):
    return client.post('/api/chat', json={'conversation_id':identity,'content':'你好','request_id':request_id})


def test_defaults_are_persisted_once_and_human_edit_survives_restart(client):
    personas = client.get('/api/personas').json()
    roles = client.get('/api/agent-roles').json()
    assert {p['name'] for p in personas} == {'并肩伙伴','资料研究','项目规划','文件执行'}
    assert len(roles) == 4
    p = personas[0]
    response = client.put('/api/personas/'+p['id'], json=p['spec'] | {'name':'我修改的人格','version':1})
    assert response.status_code == 200
    with SessionLocal.begin() as db:
        seed_defaults(db)
        assert len(db.scalars(select(AssistantProfile)).all()) == 8
        assert db.get(AssistantProfile,p['id']).name == '我修改的人格'


@pytest.mark.parametrize('kind', ['personas','agent-roles','agent-profiles'])
def test_profiles_require_session_and_csrf(client,kind):
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/'+kind).status_code == 401
        assert anonymous.post('/api/'+kind,json={'name':'x'}).status_code == 401
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/'+kind,json={'name':'x'}).status_code == 403


def test_profile_versions_copy_history_and_audit(client):
    item = create(client, style='用短句')
    update = client.put('/api/personas/'+item['id'],json={'name':'第二版','style':'先给结论','version':1})
    assert update.status_code == 200 and update.json()['version'] == 2
    assert client.put('/api/personas/'+item['id'],json={'name':'陈旧编辑','version':1}).status_code == 409
    history = client.get('/api/personas/'+item['id']+'/revisions').json()
    assert [r['version'] for r in history] == [2,1]
    assert history[1]['snapshot']['spec']['style'] == '用短句'
    assert client.get('/api/personas/'+item['id']+'/revisions?before=2').json()[0]['version'] == 1
    copy = client.post('/api/personas/'+item['id']+'/copy').json()
    assert copy['id'] != item['id'] and not copy['enabled'] and copy['version'] == 1
    assert copy['spec']['style'] == '先给结论'
    from lingxi.models import Audit
    with SessionLocal() as db:
        assert db.scalar(select(Audit).where(Audit.action=='persona_copy')).target_id == copy['id']


def test_foreign_user_workspace_and_wrong_kind_are_never_resolved(client):
    with SessionLocal.begin() as db:
        db.add(AssistantProfile(id='foreign',user_id='someone',workspace_id='other',kind='persona',name='private',spec={}))
        db.add(AssistantProfile(id='other-owner',user_id='someone',workspace_id='personal',kind='persona',name='private',spec={}))
    identity = conversation(client)
    for profile in ('foreign','other-owner'):
        for route in ('/revisions','/copy'):
            call = client.get if route=='/revisions' else client.post
            assert call('/api/personas/'+profile+route).status_code == 404
        assert select_persona(client,identity,profile).status_code == 404
        assert client.put('/api/personas/'+profile,json={'name':'x','version':1}).status_code == 404
    role = create(client,'agent-roles')
    assert select_persona(client,identity,role['id']).status_code == 404
    assert client.get('/api/conversations/foreign/persona').status_code == 404


def test_models_are_only_verified_authorized_chat_models(configured):
    assert create(configured,model_id='chat')['spec']['model_id'] == 'chat'
    for model_id in ('missing','extraction'):
        assert configured.post('/api/personas',json={'name':'bad','model_id':model_id}).status_code == 409
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').version += 1
    assert configured.post('/api/personas',json={'name':'stale','model_id':'chat'}).status_code == 409


def test_agent_configuration_checks_dependencies_and_does_not_claim_dispatch(client):
    persona,role = create(client),create(client,'agent-roles',tools=['server.files.read'],context_categories=['projects'])
    agent = create(client,'agent-profiles',persona_id=persona['id'],role_id=role['id'])
    assert agent['readiness'] == 'unavailable' and '尚未验证或选用' in agent['issues'][0]
    assert client.post('/api/agent-profiles',json={'name':'bad','persona_id':role['id'],'role_id':role['id']}).status_code == 404
    assert client.put('/api/personas/'+persona['id'],json={'name':persona['name'],'version':1,'enabled':False}).status_code == 200
    response = client.get('/api/agent-profiles').json()
    assert '人格不可用' in response[0]['issues']
    assert client.post('/api/agent-profiles',json={'name':'bad','persona_id':persona['id'],'role_id':role['id']}).status_code == 409


def test_role_scope_and_examples_have_bounded_validated_inputs(client):
    assert client.post('/api/agent-roles',json={'name':'bad','context_categories':['all']}).status_code == 422
    assert client.post('/api/agent-roles',json={'name':'bad','tools':['x'*151]}).status_code == 422
    assert client.post('/api/personas',json={'name':'bad','examples':['x'*1001]}).status_code == 422
    assert client.post('/api/personas',json={'name':'bad','examples':['x']*9}).status_code == 422


def test_selection_versions_disable_and_same_memory_identity(configured):
    identity = conversation(configured)
    assert configured.get(f'/api/conversations/{identity}/persona').json()['version'] == 0
    p = create(configured)
    assert select_persona(configured,identity,p['id']).json()['version'] == 1
    assert select_persona(configured,identity,None).status_code == 409
    assert select_persona(configured,identity,None,1).json()['persona_id'] is None
    configured.put('/api/personas/'+p['id'],json={'name':p['name'],'enabled':False,'version':1})
    assert select_persona(configured,identity,p['id'],2).status_code == 409
    with SessionLocal() as db:
        assert not db.scalars(select(Memory)).all()


def test_chat_freezes_persona_version_and_preserves_idempotency(configured):
    identity = conversation(configured)
    p = create(configured,style='first style',model_id='chat')
    assert select_persona(configured,identity,p['id']).status_code == 200
    result = chat(configured,identity)
    assert result.status_code == 200 and result.json()['config']['persona']['version'] == 1
    configured.put('/api/personas/'+p['id'],json={'name':'new','style':'second style','model_id':'chat','version':1})
    assert chat(configured,identity).json()['config']['persona']['spec']['style'] == 'first style'
    configured.post('/api/chat-runs/'+result.json()['id']+'/cancel')
    second = chat(configured,identity,'assistant-request-002').json()
    assert second['config']['persona']['version'] == 2
    assert second['config']['persona']['spec']['style'] == 'second style'


def test_disabled_selection_prevents_new_chat_but_can_be_explicitly_reset(configured):
    identity = conversation(configured)
    p = create(configured)
    select_persona(configured,identity,p['id'])
    configured.put('/api/personas/'+p['id'],json={'name':'off','enabled':False,'version':1})
    assert chat(configured,identity).status_code == 409
    assert select_persona(configured,identity,None,1).status_code == 200
    assert chat(configured,identity).status_code == 200


def test_support_expiry_correction_and_disable_do_not_create_memories(client):
    assert client.get('/api/support-preferences').json()['version'] == 0
    saved = client.put('/api/support-preferences',json={'mode':'listen','self_report':'有些疲惫','report_ttl_hours':1}).json()
    assert saved['evidence'] == 'user_report' and saved['report_expires_at'] > now()
    updated = client.put('/api/support-preferences',json={'version':1,'mode':'advice','self_report':'有些疲惫','report_ttl_hours':24}).json()
    assert updated['report_expires_at'] == saved['report_expires_at']
    assert client.put('/api/support-preferences',json={'version':1}).status_code == 409
    with SessionLocal.begin() as db:
        row = db.get(SupportPreference,'owner')
        row.spec = row.spec | {'report_expires_at':now()-1}
    assert client.get('/api/support-preferences').json()['self_report'] == ''
    cleared = client.put('/api/support-preferences',json={'version':2,'emotion_enabled':False,'self_report':'不要保存'}).json()
    assert not cleared['self_report'] and cleared['report_expires_at'] is None
    with SessionLocal() as db:
        assert db.get(SupportPreference,'owner').spec['self_report'] == ''
        assert not db.scalars(select(Memory)).all()


def test_worker_uses_frozen_expression_and_current_support_without_granting_tools(configured,monkeypatch):
    identity = conversation(configured)
    p = create(configured,style='固定的风格',instructions='声称拥有所有工具')
    select_persona(configured,identity,p['id'])
    result = chat(configured,identity).json()
    configured.put('/api/personas/'+p['id'],json={'name':'changed','style':'不应出现的新风格','version':1})
    configured.put('/api/support-preferences',json={'mode':'listen','notes':'不要连续提问','self_report':'今天疲惫'})
    monkeypatch.setattr(worker.MemoryService,'search',lambda *a:[])
    seen=[]
    def stream(model,provider,messages,run_id,check,**kwargs):
        seen.extend(messages)
        check()
        yield '收到，我在听。'
    monkeypatch.setattr(worker,'stream',stream)
    job = worker.claim()
    assert job.kind == 'chat'
    worker.perform(job)
    with SessionLocal() as db:
        assert db.get(ChatRun,result['id']).status == 'completed'
    expression = seen[1]['content']
    assert '固定的风格' in expression and '不应出现的新风格' not in expression
    assert '不要连续提问' in expression and '今天疲惫' in expression
    assert '不授予工具权限' in expression
    assert seen[-1] == {'role':'user','content':'你好'}
