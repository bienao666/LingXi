import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lingxi.api import app
from lingxi.assistants import AssistantProfile, ConversationPersona, chat_profile
from lingxi.db import SessionLocal, engine
from lingxi.models import Audit, ChatRun, Conversation, Model, User
from lingxi.system_settings import SettingsRevision, WorkspaceSettings, current, history_budget


def preview(client, changes=None, **payload):
    current = client.get('/api/system-settings').json()
    return client.post('/api/admin/system-settings/previews', json={
        'version': current['version'], 'action': 'edit', 'config': current['config'] | (changes or {})} | payload)


def confirm(client, response):
    assert response.status_code == 200, response.text
    return client.put('/api/admin/system-settings', json={'token': response.json()['token'],
        'acknowledgement': 'new_turns_only'})


def save(client, **changes):
    result = confirm(client, preview(client, changes))
    assert result.status_code == 200, result.text
    return result.json()


def test_reviewed_change_replay_and_audit(client):
    initial = client.get('/api/system-settings').json()
    assert initial['version'] == 0 and initial['config']['history_budget_bytes'] == 48000
    p = preview(client, {'workspace_name': '研究空间'})
    assert p.json()['changes'] == [{'field': 'workspace_name', 'before': '个人工作空间', 'after': '研究空间'}]
    assert client.get('/api/system-settings').json() == initial
    assert confirm(client, p).json()['version'] == 1
    assert confirm(client, p).status_code == 409
    history = client.get('/api/admin/system-settings/revisions').json()
    assert [r['version'] for r in history['items']] == [1, 0]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'system_settings_edit')) == 1


@pytest.mark.parametrize('changes', [dict(workspace_name=' '), dict(workspace_name='x\nsecret'),
    dict(workspace_name='a'*41), dict(default_timezone='Mars/Invalid'), dict(default_timezone='../localtime'),
    dict(history_budget_bytes=True), dict(history_budget_bytes='3000'), dict(history_budget_bytes=1499),
    dict(history_budget_bytes=48001), dict(history_budget_bytes=2000.5), dict(api_key='secret'),
    dict(default_persona_id='')])
def test_schema_rejects_invalid_unknown_and_coerced_fields(client, changes):
    assert preview(client, changes).status_code == 422
    assert client.get('/api/system-settings').json()['version'] == 0


def test_noop_and_exact_action_shape(client):
    assert preview(client).status_code == 422
    assert preview(client, {'workspace_name':'changed'}, action='rollback', source_version=0).status_code == 422
    assert client.post('/api/admin/system-settings/previews', json={'version':0,'action':'import'}).status_code == 422
    assert client.post('/api/admin/system-settings/previews', json={'version':0,'action':'edit','config':{}}).status_code == 422


def test_expiry_tamper_ack_version_session_and_workspace(client, monkeypatch):
    from lingxi import system_settings
    p = preview(client, {'workspace_name':'审核空间'})
    assert client.put('/api/admin/system-settings', json={'token':p.json()['token']}).status_code == 422
    assert client.put('/api/admin/system-settings', json={'token':'invalid-token-value','acknowledgement':'new_turns_only'}).status_code == 409
    clock = system_settings.now
    monkeypatch.setattr(system_settings, 'now', lambda: clock()+301)
    assert confirm(client, p).status_code == 409
    monkeypatch.setattr(system_settings, 'now', clock)
    with TestClient(app) as second:
        second.headers['X-CSRF-Token'] = second.post('/api/auth/login', json={'username':'owner','password':'test-password-123'}).json()['csrf']
        assert confirm(second, p).status_code == 409
    save(client, workspace_name='更新空间')
    assert confirm(client, p).status_code == 409
    assert preview(client, {'workspace_name':'stale'}, version=0).status_code == 409
    p = preview(client, {'workspace_name':'待确认'})
    with SessionLocal.begin() as db:
        db.get(User, 'owner').workspace_id = 'different'
    assert confirm(client, p).status_code == 409
    assert client.get('/api/system-settings').json()['version'] == 0
    assert client.get('/api/admin/system-settings/revisions').json()['items'] == []


def test_authentication_and_csrf(client):
    token = client.headers.pop('X-CSRF-Token')
    assert preview(client, {'workspace_name':'blocked'}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    client.cookies.clear()
    for url in ('/api/system-settings','/api/admin/system-settings/export','/api/admin/system-settings/revisions'):
        assert client.get(url).status_code == 401
    assert client.put('/api/admin/system-settings',json={'token':'invalid-token-value','acknowledgement':'new_turns_only'}).status_code == 401


def test_import_export_and_rollback_are_allowlisted_and_monotonic(configured):
    client = configured
    exported = client.get('/api/admin/system-settings/export').json()
    assert set(exported) == {'format','config'}
    assert 'secret-fixture-key' not in str(exported) and 'provider' not in str(exported)
    doc = copy.deepcopy(exported)
    doc['config']['workspace_name'] = '导入空间'
    body = {'version':0,'action':'import','document':doc}
    for bad in (doc | {'credentials':{}}, doc | {'format':'other'}, doc | {'config':doc['config'] | {'grants':['*']}}):
        assert client.post('/api/admin/system-settings/previews',json=body | {'document':bad}).status_code == 422
    assert confirm(client, client.post('/api/admin/system-settings/previews',json=body)).json()['version'] == 1
    rollback = client.post('/api/admin/system-settings/previews',json={'version':1,'action':'rollback','source_version':0})
    result = confirm(client, rollback).json()
    assert result['version'] == 2 and result['config'] == exported['config']
    history = client.get('/api/admin/system-settings/revisions').json()['items']
    assert [(r['version'],r['action'],r['source_version']) for r in history] == [(2,'rollback',0),(1,'import',None),(0,'initial',None)]
    assert client.post('/api/admin/system-settings/previews',json={'version':2,'action':'rollback','source_version':999}).status_code == 404


def test_history_pagination(client):
    for i in range(22):
        save(client, workspace_name=f'空间 {i}')
    page = client.get('/api/admin/system-settings/revisions').json()
    assert len(page['items']) == 20 and page['next_before'] == 3
    page2 = client.get('/api/admin/system-settings/revisions?before=3').json()
    assert [r['version'] for r in page2['items']] == [2,1,0]
    assert page2['next_before'] is None


def persona(client, name='专注伙伴'):
    response = client.post('/api/personas',json={'name':name,'instructions':'保持清晰','version':0})
    assert response.status_code == 200, response.text
    return response.json()


def test_persona_reference_validated_on_preview_and_commit_and_rollback(client):
    p = persona(client)
    previewed = preview(client, {'default_persona_id':p['id']})
    with SessionLocal.begin() as db:
        db.get(AssistantProfile, p['id']).version += 1
    assert confirm(client, previewed).status_code == 409
    save(client, default_persona_id=p['id'])
    save(client, default_persona_id=None)
    with SessionLocal.begin() as db:
        db.get(AssistantProfile, p['id']).enabled = False
    assert preview(client, {'default_persona_id':p['id']}).status_code == 409
    assert client.post('/api/admin/system-settings/previews',json={'version':2,'action':'rollback','source_version':1}).status_code == 409
    with SessionLocal.begin() as db:
        profile = db.get(AssistantProfile, p['id'])
        profile.enabled, profile.user_id = True, 'other-owner'
    assert preview(client, {'default_persona_id':p['id']}).status_code == 409
    assert preview(client, {'default_persona_id':'nonexistent'}).status_code == 409


def test_chat_snapshot_explicit_builtin_and_inheritance(configured):
    client = configured
    with SessionLocal.begin() as db:
        db.get(Model, 'chat').context_window = 20000
    p = persona(client)
    save(client, default_persona_id=p['id'], history_budget_bytes=6000)
    conversation = client.post('/api/conversations',json={'title':'配置固定'}).json()['id']
    selected = client.get(f'/api/conversations/{conversation}/persona').json()
    assert selected['inherit_default'] and selected['persona_name'] == p['name']
    run = client.post('/api/chat',json={'conversation_id':conversation,'content':'你好','request_id':'settings-chat-1'}).json()
    assert run['config']['system_settings'] == {'version':1,'history_budget_bytes':6000}
    assert run['config']['persona']['id'] == p['id']
    save(client, default_persona_id=None, history_budget_bytes=3000)
    with SessionLocal.begin() as db:
        r = db.get(ChatRun, run['id'])
        assert history_budget(r, db.get(Model, 'chat')) == 6000
        r.status = 'completed'
    # Explicit built-in remains built-in even when workspace default changes.
    assert client.put(f'/api/conversations/{conversation}/persona',json={'persona_id':None,'version':0}).status_code == 200
    save(client, default_persona_id=p['id'])
    with SessionLocal() as db:
        assert chat_profile(db, db.get(Conversation,conversation),db.get(User,'owner'))['id'] is None
    assert client.put(f'/api/conversations/{conversation}/persona',json={'inherit_default':True,'version':1}).json()['inherit_default']
    with SessionLocal() as db:
        assert chat_profile(db, db.get(Conversation,conversation),db.get(User,'owner'))['id'] == p['id']
        model = db.get(Model,'chat')
        model.context_window = 6000
        assert history_budget(db.get(ChatRun,run['id']), model) == 1500
    assert client.put(f'/api/conversations/{conversation}/persona',json={'inherit_default':True,'persona_id':p['id'],'version':2}).status_code == 422


def test_qq_actor_never_inherits_private_persona_or_config(client):
    p = persona(client)
    save(client, workspace_name='private workspace', default_persona_id=p['id'], history_budget_bytes=3000)
    with SessionLocal.begin() as db:
        actor = User(id='actor',username='channel-actor',password_hash='')
        conversation = Conversation(user_id=actor.id)
        db.add_all([actor,conversation]);db.flush()
        assert current(db, actor).version == 0
        assert chat_profile(db, conversation, actor)['id'] is None


def test_timezone_only_supplies_unset_preferences(client):
    save(client, default_timezone='Europe/London')
    assert client.get('/api/contact-preferences').json()['timezone'] == 'Europe/London'
    from lingxi.proactive import preference
    with SessionLocal.begin() as db:
        assert preference(db,db.get(User,'owner'),create=True).timezone == 'Europe/London'
    save(client, default_timezone='Asia/Tokyo')
    assert client.get('/api/contact-preferences').json()['timezone'] == 'Europe/London'


def test_worker_uses_submitted_budget_after_defaults_change(configured, monkeypatch):
    from test_conversation_context import setup, submit, process
    cid, _, captured = setup(configured, monkeypatch)
    save(configured, history_budget_bytes=6000)
    rid = submit(configured, cid)
    save(configured, history_budget_bytes=1500)
    process(rid)
    run = configured.get('/api/chat-runs/'+rid).json()
    assert run['status'] == 'completed'
    assert run['config']['system_settings']['version'] == 1
    history = [m for m in captured['chat'][0] if m['role'] != 'system']
    size = sum(len(m['content'].encode('utf-8')) for m in history)
    assert 1500 < size <= 6000
    assert history[-1]['content'] == '蓝桥项目原先有什么约定？'


def test_regeneration_gets_new_defaults_without_changing_original(configured, monkeypatch):
    from test_branches import setup, fork
    save(configured, history_budget_bytes=3000)
    cid, messages, _ = setup(configured, monkeypatch)
    original = configured.get('/api/chat-runs/'+messages[1]['run_id']).json()
    p = persona(configured)
    save(configured, default_persona_id=p['id'], history_budget_bytes=1500)
    child = fork(configured, cid, messages[1])
    assert child.status_code == 200, child.text
    generated = configured.get('/api/chat-runs/'+child.json()['run_id']).json()
    assert generated['config']['system_settings'] == {'version':2,'history_budget_bytes':1500}
    assert generated['config']['persona']['id'] == p['id']
    assert original['config']['system_settings'] == {'version':1,'history_budget_bytes':3000}


def test_restore_latest_defaults_history_and_clear_old_persona(client):
    from lingxi.backups import checkpoint, quarantine
    p = persona(client)
    save(client, workspace_name='最新空间', default_persona_id=p['id'], history_budget_bytes=8000)
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0044')"))
    data = checkpoint()
    with SessionLocal.begin() as db:
        db.get(WorkspaceSettings,'personal').values = {'workspace_name':'旧空间'}
    quarantine(data)
    with SessionLocal() as db:
        result = current(db,db.get(User,'owner'))
        assert result.version == 2 and result.config.workspace_name == '最新空间'
        assert result.config.default_persona_id is None
        assert db.scalar(select(func.count()).select_from(SettingsRevision)) == 3


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='row-lock concurrency requires PostgreSQL')
def test_concurrent_confirm_only_one_revision(client):
    p = preview(client, {'workspace_name':'唯一确认'})
    token = p.json()['token']
    from lingxi.system_settings import confirm as commit, SettingsConfirm
    from lingxi.models import LoginSession
    from fastapi import HTTPException
    def run():
        try:
            with SessionLocal.begin() as db:
                user,session = db.get(User,'owner'),db.scalar(select(LoginSession))
                return commit(SettingsConfirm(token=token,acknowledgement='new_turns_only'),(db,user,session)).version
        except HTTPException as e:
            return e.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:run(),range(2))) == [1,409]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SettingsRevision)) == 2
