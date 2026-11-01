import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lingxi.api import app
from lingxi import configuration_bundles as b, model_budgets, operations
from lingxi.db import SessionLocal, engine
from lingxi.models import Audit, ModelCall, Provider, User
from lingxi.security import decrypt, encrypt
from lingxi.system_settings import SettingsRevision

BASE = '/api/admin/configuration-bundles'


def proposed(client, names=None):
    state = client.get(BASE).json()
    modules = state['document']['modules']
    modules['workspace']['workspace_name'] = '合成配置空间'
    modules['media'].update(max_files=3, allowed_types=['text', 'markdown'])
    modules['operations'].update(log_retention_days=7, daily_token_alert=100)
    modules['model_budget'].update(daily_limit=5, execution_limit=2)
    names = names or list(modules)
    state['document']['modules'] = {key: value for key, value in modules.items() if key in names}
    state['versions'] = {key: value for key, value in state['versions'].items() if key in names}
    return state


def preview(client, payload=None):
    response = client.post(BASE+'/previews', json=payload or proposed(client))
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, p):
    return client.put(BASE, json={'token': p['token'], 'acknowledgement': 'atomic_configuration_changes'})


def test_complete_allowlisted_export_no_business_payloads_or_secrets(configured):
    with SessionLocal.begin() as db:
        db.add(ModelCall(model_id='secret-model', purpose='chat', status='completed', duration_ms=2,
            config={'secret': 'SECRET-prompt'}))
    doc = configured.get(BASE+'/export').json()
    assert doc['format'] == 'lingxi-configuration-bundle/v1'
    assert set(doc) == {'format', 'modules'}
    assert set(doc['modules']) == {'workspace', 'media', 'operations', 'model_budget'}
    assert doc['modules']['workspace']['default_persona_id'] is None
    assert not any(s in json.dumps(doc) for s in ('secret-fixture', 'provider', 'SECRET', 'owner'))
    state = configured.get(BASE).json()
    assert state['document'] == doc
    assert state['versions'] == dict(workspace=0, media=1, operations=0, model_budget=0)


def test_preview_is_read_only_then_atomic_apply_and_shared_revision_history(client):
    before = client.get(BASE).json()
    payload = proposed(client)
    p = preview(client, payload)
    assert set(c['module'] for c in p['changes']) == set(payload['versions'])
    assert client.get(BASE).json() == before
    response = confirm(client, p)
    assert response.status_code == 200, response.text
    after = response.json()
    assert after['document'] == payload['document']
    assert after['versions'] == {name: version+1 for name, version in before['versions'].items()}
    assert confirm(client, p).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SettingsRevision)) == 2
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='configuration_bundle_import')) == 1


@pytest.mark.parametrize('name', ['workspace', 'media', 'operations', 'model_budget'])
def test_single_module_apply_leaves_other_versions_and_values_intact(client, name):
    before = client.get(BASE).json()
    assert confirm(client, preview(client, proposed(client, [name]))).status_code == 200
    after = client.get(BASE).json()
    for other in set(before['versions'])-{name}:
        assert after['versions'][other] == before['versions'][other]
        assert after['document']['modules'][other] == before['document']['modules'][other]


def test_import_can_restore_exported_values_but_versions_keep_increasing(client):
    original = client.get(BASE).json()
    assert confirm(client, preview(client)).status_code == 200
    versions = client.get(BASE).json()['versions']
    p = preview(client, dict(document=original['document'], versions=versions))
    response = confirm(client, p)
    assert response.status_code == 200
    assert response.json()['document'] == original['document']
    assert response.json()['versions'] == {key: version+1 for key, version in versions.items()}


def test_selected_but_unchanged_modules_are_version_guarded_not_rewritten(client):
    original = client.get(BASE).json()
    payload = copy.deepcopy(original)
    payload['document']['modules']['workspace']['workspace_name'] = '仅修改名称'
    assert confirm(client, preview(client, payload)).status_code == 200
    after = client.get(BASE).json()
    assert after['versions'] == original['versions'] | {'workspace': 1}
    assert client.post(BASE+'/previews', json=after).status_code == 422


@pytest.mark.parametrize('mutation', ['unknown', 'empty', 'null', 'partial', 'versions', 'negative', 'coercion', 'secret', 'format', 'duplicate_type'])
def test_reject_unknown_partial_coerced_or_malformed_configuration(client, mutation):
    body = proposed(client)
    modules = body['document']['modules']
    if mutation == 'unknown': modules['plugins'] = {}
    elif mutation == 'empty': body['document']['modules'] = {}; body['versions'] = {}
    elif mutation == 'null': modules['workspace'] = None
    elif mutation == 'partial': del modules['workspace']['default_persona_id']
    elif mutation == 'versions': del body['versions']['media']
    elif mutation == 'negative': body['versions']['media'] = -1
    elif mutation == 'coercion': modules['model_budget']['daily_limit'] = True
    elif mutation == 'secret': modules['workspace']['key'] = 'SECRET'
    elif mutation == 'format': body['document']['format'] = 'foreign/v1'
    else: modules['media']['allowed_types'] = ['text', 'text']
    before = client.get(BASE).json()
    assert client.post(BASE+'/previews', json=body).status_code == 422
    assert client.get(BASE).json() == before


@pytest.mark.parametrize('name', ['workspace', 'media', 'operations', 'model_budget'])
def test_individual_edit_invalidates_whole_bundle_without_partial_apply(client, name):
    payload = proposed(client)
    p = preview(client, payload)
    selected = proposed(client, [name])
    assert confirm(client, preview(client, selected)).status_code == 200
    before = client.get(BASE).json()
    assert confirm(client, p).status_code == 409
    assert client.get(BASE).json() == before


def test_late_failure_rolls_back_all_modules_revisions_and_audit(client, monkeypatch):
    p = preview(client)
    before = client.get(BASE).json()
    def fail(*_): raise HTTPException(409, '模拟最后一个模块冲突')
    monkeypatch.setattr(model_budgets, 'confirm', fail)
    response = confirm(client, p)
    assert response.status_code == 409
    assert client.get(BASE).json() == before
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SettingsRevision)) == 0
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action.in_(
            ['configuration_bundle_import', 'media_policy_update', 'system_settings_import', 'operations_preferences']))) == 0


def test_expiry_tamper_ack_login_and_workspace_binding(client, monkeypatch):
    p = preview(client)
    assert client.put(BASE, json={'token': p['token']}).status_code == 422
    assert confirm(client, {'token': 'tampered-token-value'}).status_code == 409
    data = json.loads(decrypt(p['token']))
    data['expires_at'] -= 301
    assert confirm(client, {'token': encrypt(json.dumps(data))}).status_code == 409
    with TestClient(app) as other:
        login = other.post('/api/auth/login', json={'username': 'owner', 'password': 'test-password-123'}).json()
        other.headers['X-CSRF-Token'] = login['csrf']
        assert confirm(other, p).status_code == 409
    with SessionLocal.begin() as db: db.get(User, 'owner').workspace_id = 'other-space'
    assert confirm(client, p).status_code == 409


def test_persona_change_between_preview_and_confirm_prevents_import(client):
    from lingxi.assistants import AssistantProfile
    persona = client.post('/api/personas', json={'name': '合成人格', 'instructions': '清晰表达', 'version': 0}).json()
    payload = proposed(client)
    payload['document']['modules']['workspace']['default_persona_id'] = persona['id']
    p = preview(client, payload)
    with SessionLocal.begin() as db: db.get(AssistantProfile, persona['id']).version += 1
    before = client.get(BASE).json()
    assert confirm(client, p).status_code == 409
    assert client.get(BASE).json() == before
    with SessionLocal.begin() as db: db.get(AssistantProfile, persona['id']).user_id = 'other'
    assert client.post(BASE+'/previews', json=payload).status_code == 409


def test_budget_attempts_are_not_reset_by_import_or_restoring_export(configured):
    with SessionLocal() as db: provider = db.get(Provider, 'provider')
    model_budgets.reserve(provider, {})
    assert confirm(configured, preview(configured)).status_code == 200
    budget = configured.get('/api/admin/model-budgets').json()
    assert budget['today_attempts'] == 1 and budget['daily_limit'] == 5
    for _ in range(4): model_budgets.reserve(provider, {})
    with pytest.raises(model_budgets.BudgetExceeded): model_budgets.reserve(provider, {})


def test_locked_identity_is_rechecked_before_any_module_write(client, monkeypatch):
    from lingxi import auth_totp
    p = preview(client)
    original = auth_totp.lock_owner
    def moved(db, user, login=None):
        result = original(db, user, login)
        user.workspace_id = 'simulated-concurrent-move'
        return result
    monkeypatch.setattr(auth_totp, 'lock_owner', moved)
    assert confirm(client, p).status_code == 409
    with SessionLocal() as db:
        assert db.get(User, 'owner').workspace_id == 'personal'
        assert db.scalar(select(func.count()).select_from(SettingsRevision)) == 0


def test_authentication_and_csrf(client):
    body = proposed(client)
    token = client.headers.pop('X-CSRF-Token')
    assert client.post(BASE+'/previews', json=body).status_code == 403
    client.headers['X-CSRF-Token'] = token
    client.cookies.clear()
    for path in (BASE, BASE+'/export'):
        assert client.get(path).status_code == 401
    assert client.post(BASE+'/previews', json=body).status_code == 401
    assert confirm(client, {'token': 'invalid-preview-value'}).status_code == 401


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Requires PostgreSQL row locks')
def test_concurrent_confirm_applies_once(client):
    p = preview(client)
    cookies, headers = dict(client.cookies), dict(client.headers)
    def execute(_):
        with TestClient(app) as parallel:
            parallel.cookies.update(cookies); parallel.headers.update(headers)
            return confirm(parallel, p).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(execute, range(2)))
    assert sorted(statuses) == [200, 409]
    after = client.get(BASE).json()
    assert after['versions'] == dict(workspace=1, media=2, operations=1, model_budget=1)


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Requires PostgreSQL row locks')
def test_bundle_and_individual_budget_confirmation_serialize_without_overwrite(client):
    p = preview(client)
    budget = client.post('/api/admin/model-budgets/previews', json={
        'version': 0, 'daily_limit': 10, 'execution_limit': 3}).json()
    cookies, headers = dict(client.cookies), dict(client.headers)
    def execute(which):
        with TestClient(app) as parallel:
            parallel.cookies.update(cookies); parallel.headers.update(headers)
            return (confirm(parallel, p) if which == 0 else parallel.put('/api/admin/model-budgets',
                json={'token': budget['token'], 'acknowledgement': 'request_attempts_not_money'})).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(execute, range(2)))
    assert sorted(statuses) == [200, 409]
    final = client.get(BASE).json()
    assert final['versions']['model_budget'] == 1
    if statuses[0] == 200:
        assert final['document']['modules']['model_budget']['daily_limit'] == 5
        assert final['versions']['workspace'] == 1
    else:
        assert final['document']['modules']['model_budget']['daily_limit'] == 10
        assert final['versions']['workspace'] == 0 and final['versions']['media'] == 1
