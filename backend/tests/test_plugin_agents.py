"""Role packages execute through the real fenced delegation persistence."""
import copy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import User, Memory
from lingxi.assistants import AssistantProfile
from lingxi.plugin_agents import PluginAgentBinding
from lingxi.plugin_manifest import Manifest, digest
from lingxi import gateway, worker
from lingxi.memory import MemoryService
from test_plugins import release, add, install, action
from test_delegation import create, fetch, drain
from test_plugin_store import shop


def agent_release(version='1.0.0', deps=(), **changes):
    row = release('sample.roles', version, deps)
    entry = dict(expertise='审阅项目交付风险', instructions='区分建议与已执行行动。',
                 completion='提供带证据的风险清单', context_categories=['projects', 'procedures'], tools=['reason']) | changes
    permissions = ['model.chat'] + (['resources.list'] if 'server.files' in entry['tools'] else [])
    row.manifest = Manifest(**(row.manifest | {'capabilities': [dict(id='sample.reviewer', name='风险审阅',
        kind='agent', permissions=permissions, agent=entry)]})).model_dump()
    row.digest = digest(row.manifest)
    return row


def bind(client, installed, binding=None, **changes):
    persona = next(p for p in client.get('/api/personas').json() if p['name'] == '并肩伙伴')
    return client.post('/api/plugin-agent-bindings', json=dict(installation_id=installed['id'],
        installation_version=installed['version'], capability_id='sample.reviewer', persona_id=persona['id'],
        version=binding['version'] if binding else 0, model_id=None, tools=['reason'],
        context_categories=[], acknowledged=True) | changes)


def setup(client, **kw):
    add(agent_release(**kw)); installed = install(client, plugin='sample.roles')
    r = bind(client, installed); assert r.status_code == 200, r.text
    return installed, r.json()


def model_fixture(client, monkeypatch, mutate=None):
    identity = next(a['id'] for a in client.get('/api/agents').json() if a.get('plugin_id') == 'sample.roles')
    calls = []
    def complete(model, provider, messages, purpose='chat', config=None):
        stage = config['stage']; calls.append((stage, messages))
        if mutate: mutate(stage, messages)
        if stage == 'plan':
            return json.dumps({'mode':'direct', 'reason':'使用用户授权的项目风险角色', 'nodes':[
                dict(key='risk', title='核对风险', instruction='审阅项目风险', agent_id=identity)]})
        data = json.loads(messages[-1]['content'])
        ids = ([m['id'] for m in data['context_bundle']['memories']] if stage == 'node'
               else [n['id'] for n in data['nodes'] if n['status'] == 'succeeded'])
        return json.dumps(dict(summary='风险建议，尚未执行', findings=[dict(text='核对交付范围', evidence_ids=ids)], limitations=[]))
    monkeypatch.setattr(gateway, 'complete', complete)
    return calls


def test_legacy_manifest_has_identical_bytes_and_agent_scope_is_bounded():
    old = release('sample.old').manifest
    assert Manifest(**old).model_dump() == old
    spec = agent_release().manifest
    for patch in ({'agent':None}, {'instruction':'execute'}, {'permissions':['network.write']},
                  {'agent':dict(spec['capabilities'][0]['agent'], tools=['shell'])},
                  {'agent':dict(spec['capabilities'][0]['agent'], context_categories=['all'])},
                  {'agent':dict(spec['capabilities'][0]['agent'], tools=['reason','reason'])}):
        broken = copy.deepcopy(spec); broken['capabilities'][0].update(patch)
        with pytest.raises(ValueError): Manifest(**broken)


@pytest.mark.parametrize('path', ['/api/plugin-agent-bindings','/api/agents'])
def test_role_management_requires_auth(client, path):
    with TestClient(app) as c: assert c.get(path).status_code == 401


def test_install_does_not_enable_role_or_create_local_profiles(configured):
    before = len(configured.get('/api/agent-roles').json())
    add(agent_release()); installed = install(configured, plugin='sample.roles')
    assert configured.get('/api/plugin-agent-bindings').json() == []
    assert not any(a.get('plugin_id') for a in configured.get('/api/agents').json())
    cap = next(c for c in configured.get('/api/plugin-capabilities').json() if c['kind'] == 'agent')
    assert not cap['available'] and '配置' in cap['reason']
    assert len(configured.get('/api/agent-roles').json()) == before
    assert configured.post('/api/plugin-invocations', json=dict(installation_id=installed['id'],
        capability_id=cap['id'],content='x', request_id='not-a-skill-role')).status_code == 409


@pytest.mark.parametrize('patch, status', [({'acknowledged':False},422),({'version':2},409),
    ({'installation_version':9},409),({'tools':['reason','network.write']},422),
    ({'context_categories':['preferences']},422),({'context_categories':['projects','projects']},422),
    ({'persona_id':'unknown'},404),({'model_id':'unknown'},409)])
def test_binding_checks_user_confirmation_versions_and_scope(configured,patch,status):
    add(agent_release()); installed=install(configured,plugin='sample.roles')
    assert bind(configured,installed,**patch).status_code==status
    assert configured.get('/api/plugin-agent-bindings').json()==[]


def test_no_verified_model_is_not_fake_ready(client):
    add(agent_release()); installed=install(client,plugin='sample.roles')
    assert bind(client,installed).status_code==409
    assert client.get('/api/plugin-agent-bindings').json()==[]


def test_role_run_has_pinned_template_and_one_main_reply(configured,monkeypatch):
    installed,binding=setup(configured);calls=model_fixture(configured,monkeypatch)
    conv=configured.post('/api/conversations',json={'title':'评审'}).json()
    run=create(configured,conversation_id=conv['id']);drain();detail=fetch(configured,run)
    assert detail['status']=='completed' and detail['calls_used']==3
    node=detail['nodes'][0]
    assert node['config']['plugin_role']['binding_id']==binding['id']
    assert node['config']['plugin_version']=='1.0.0' and node['config']['role']['spec']['completion']=='提供带证据的风险清单'
    assert node['context']['memory_status']=='not_allowed'
    assert len(configured.get('/api/messages?conversation_id='+conv['id']).json())==1
    assert [s for s,_ in calls]==['plan','node','summary']


def test_role_context_intersects_manifest_binding_and_task(configured,monkeypatch):
    installed,binding=setup(configured)
    binding=bind(configured,installed,binding,context_categories=['projects']).json()
    model_fixture(configured,monkeypatch)
    with SessionLocal.begin() as db:
        db.add_all([Memory(id='project',user_id='owner',content='项目资料',category='projects',subject='self',source_ids=[]),
                    Memory(id='private',user_id='owner',content='私人偏好',category='preferences',subject='self',source_ids=[]),
                    Memory(id='foreign',user_id='other',content='他人项目',category='projects',subject='self',source_ids=[])])
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:list(db.scalars(select(Memory))))
    run=create(configured,share_memory=True);drain();detail=fetch(configured,run)
    assert detail['status']=='completed'
    assert [m['id'] for m in detail['nodes'][0]['context']['memories']]==['project']


@pytest.mark.parametrize('when',['plan','node','summary'])
def test_revoke_fences_each_stage_even_after_reenable(configured,monkeypatch,when):
    installed,binding=setup(configured)
    def mutate(stage,messages):
        if stage==when:
            r=configured.post('/api/plugin-agent-bindings/'+binding['id']+'/revoke',json={'version':binding['version']})
            assert r.status_code==200
            assert bind(configured,installed,r.json()).status_code==200
    model_fixture(configured,monkeypatch,mutate)
    run=create(configured);drain();detail=fetch(configured,run)
    assert detail['status']=='failed' and not detail['result']
    assert not detail['result_message_id']


def test_scope_reduction_during_generation_rejects_old_result(configured,monkeypatch):
    installed,binding=setup(configured)
    binding=bind(configured,installed,binding,context_categories=['projects']).json()
    def mutate(stage,messages):
        if stage=='node': assert bind(configured,installed,binding,context_categories=[]).status_code==200
    model_fixture(configured,monkeypatch,mutate)
    run=create(configured,share_memory=True);drain();detail=fetch(configured,run)
    assert detail['status']=='failed' and not detail['result']


def test_update_requires_review_for_new_tasks_but_keeps_pinned_old_role(configured,monkeypatch):
    installed,binding=setup(configured)
    add(agent_release('1.1.0',instructions='新版本风险核对要求。'))
    calls=model_fixture(configured,monkeypatch)
    run=create(configured);worker.perform(worker.claim())
    updated=install(configured,version='1.1.0',action='update',plugin='sample.roles')
    assert not configured.get('/api/plugin-agent-bindings').json()[0]['available']
    assert not any(a.get('plugin_id')=='sample.roles' for a in configured.get('/api/agents').json())
    rebound=bind(configured,updated,binding);assert rebound.status_code==200,rebound.text
    drain();detail=fetch(configured,run)
    assert detail['status']=='completed' and detail['nodes'][0]['config']['plugin_version']=='1.0.0'
    assert '区分建议' in detail['nodes'][0]['config']['role']['spec']['instructions']
    assert any(a['plugin_version']=='1.1.0' for a in configured.get('/api/agents').json() if a.get('plugin_id'))


@pytest.mark.parametrize('kind',['disable','uninstall'])
def test_plugin_lifecycle_revokes_role_and_stops_queued_run(configured,monkeypatch,kind):
    installed,binding=setup(configured);calls=model_fixture(configured,monkeypatch)
    run=create(configured)
    impact=configured.get('/api/plugin-installations/'+installed['id']+'/impact').json()
    assert impact['plugin_roles']==1 and impact['role_delegations']==1
    assert action(configured,installed,kind).status_code==200
    drain();assert fetch(configured,run)['status']=='failed' and calls==[]
    assert configured.get('/api/plugin-agent-bindings').json()[0]['status']=='revoked'


def test_dependency_disable_revokes_dependent_binding(configured):
    add(release('sample.dep'),agent_release(deps=[('sample.dep','==1.0.0')]))
    installed=install(configured,plugin='sample.roles');assert bind(configured,installed).status_code==200
    dep=next(r for r in configured.get('/api/plugin-installations').json() if r['plugin_id']=='sample.dep')
    assert action(configured,dep,'disable').status_code==200
    assert configured.get('/api/plugin-agent-bindings').json()[0]['status']=='revoked'


def test_cross_workspace_cannot_inherit_or_edit_binding(configured):
    installed,binding=setup(configured)
    with SessionLocal.begin() as db: db.get(User,'owner').workspace_id='different'
    assert configured.get('/api/plugin-agent-bindings').json()==[]
    assert configured.post('/api/plugin-agent-bindings/'+binding['id']+'/revoke',json={'version':1}).status_code==404
    assert configured.post('/api/plugin-agent-bindings',json=dict(installation_id=installed['id'],
        installation_version=installed['version'],capability_id='sample.reviewer',persona_id=binding['persona_id'],
        acknowledged=True)).status_code==404


def test_local_persona_disable_removes_plugin_candidate(configured):
    _,binding=setup(configured)
    with SessionLocal.begin() as db:db.get(AssistantProfile,binding['persona_id']).enabled=False
    assert not configured.get('/api/plugin-agent-bindings').json()[0]['available']
    assert not any(a.get('plugin_id') for a in configured.get('/api/agents').json())


def test_restore_carries_latest_role_revocation_and_does_not_reenable(configured):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    _,binding=setup(configured)
    with SessionLocal.begin() as db:schema(db)
    data=checkpoint();assert len(data['plugin_agent_bindings'])==1
    with SessionLocal.begin() as db:db.delete(db.get(PluginAgentBinding,binding['id']))
    quarantine(data)
    with SessionLocal() as db:
        row=db.get(PluginAgentBinding,binding['id']);assert row.status=='revoked' and row.epoch==2


def test_signed_store_role_can_install_and_trust_revocation_stops_it(configured,shop,monkeypatch):
    from test_plugin_store import publish,core_config,decision
    published=publish(shop,data=agent_release().manifest)
    assert published['checks']['supported_by_current_core']
    core_config(configured,shop,monkeypatch);assert configured.post('/api/plugin-store/sync').status_code==200
    installed=install(configured,plugin='sample.roles');assert bind(configured,installed).status_code==200
    assert decision(shop,published,'revoke').status_code==200
    assert configured.post('/api/plugin-store/sync').status_code==200
    assert configured.get('/api/plugin-agent-bindings').json()[0]['status']=='revoked'


def test_file_template_still_needs_native_resource_authorization(configured,monkeypatch):
    monkeypatch.setattr('lingxi.tasks.resource_available',lambda:True)
    from lingxi.resources import ResourcePolicy
    with SessionLocal.begin() as db:
        db.add(ResourcePolicy(workspace_id='personal',user_id='owner',roots=['.']))
    installed,binding=setup(configured,tools=['reason','server.files'])
    r=bind(configured,installed,binding,tools=['reason','server.files']);assert r.status_code==200,r.text
    native=next(r for r in configured.get('/api/plugin-installations').json() if r['plugin_id']=='lingxi.builtin')
    assert action(configured,native,'disable').status_code==200
    assert not configured.get('/api/plugin-agent-bindings').json()[0]['available']
    assert not any(a.get('plugin_id') for a in configured.get('/api/agents').json())


def test_file_declaration_without_resources_permission_is_rejected():
    data=agent_release(tools=['reason','server.files']).manifest
    data['capabilities'][0]['permissions']=['model.chat']
    with pytest.raises(ValueError):Manifest(**data)


def test_correcting_shared_memory_invalidates_role_result(configured,monkeypatch):
    installed,binding=setup(configured)
    assert bind(configured,installed,binding,context_categories=['projects']).status_code==200
    with SessionLocal.begin() as db:db.add(Memory(id='project',user_id='owner',content='旧项目资料',category='projects',subject='self',source_ids=[]))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*a:[db.get(Memory,'project')])
    model_fixture(configured,monkeypatch);run=create(configured,share_memory=True);drain()
    assert fetch(configured,run)['status']=='completed'
    with SessionLocal.begin() as db:
        from lingxi.memory import invalidate_runs
        db.get(Memory,'project').version+=1;invalidate_runs(db,'project')
    detail=fetch(configured,run)
    assert detail['status']=='invalidated' and not detail['result'] and not detail['nodes'][0]['context']


def test_csrf_is_required_for_binding_changes(configured):
    installed,binding=setup(configured)
    configured.headers.pop('X-CSRF-Token')
    assert configured.post('/api/plugin-agent-bindings/'+binding['id']+'/revoke',json={'version':1}).status_code==403


def test_concurrent_role_activation_requires_one_version_winner(configured):
    from concurrent.futures import ThreadPoolExecutor
    from lingxi.db import engine
    from lingxi.plugin_agents import configure,BindingInput
    from fastapi import HTTPException
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row locks required')
    add(agent_release());installed=install(configured,plugin='sample.roles')
    persona=configured.get('/api/personas').json()[0]
    body=BindingInput(installation_id=installed['id'],installation_version=installed['version'],
        capability_id='sample.reviewer',persona_id=persona['id'],acknowledged=True)
    def activate(_):
        try:
            with SessionLocal.begin() as db:configure(body,(db,db.get(User,'owner'),None))
            return 200
        except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(activate,range(2)))
    assert sorted(results)==[200,409]
    assert len(configured.get('/api/plugin-agent-bindings').json())==1
