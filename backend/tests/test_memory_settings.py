"""Configuration, inheritance and recovery tests without external inference."""
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from lingxi.config import settings
from lingxi.db import SessionLocal, now
from lingxi.gateway import resolve_model
from lingxi.memory import HindsightAdapter, MemoryService, index_epoch
from lingxi.memory_settings import (MemoryConfiguration, MemoryFlight, configuration,
    extraction_signature, flight, inherited_verified, selection, tick)
from lingxi.models import Job, Memory, Model, Operation, Policy, Provider
from lingxi.security import encrypt


def chat():
    with SessionLocal.begin() as db:
        db.add(Provider(id='p', name='Fixture', base_url='https://fixture.test/v1', encrypted_key=encrypt('synthetic')))
        db.add(Model(id='chat', provider_id='p', name='fixture-chat', purpose='chat',
            status='verified', verified_provider_version=1))
        db.add(Policy(purpose='chat', model_id='chat'))


def verified():
    with SessionLocal.begin() as db:
        row = configuration(db, create=True)
        row.verification = {'signature': extraction_signature(db), 'status': 'verified'}


def independent():
    with SessionLocal.begin() as db:
        for purpose in ('extraction', 'embedding', 'rerank'):
            db.add(Model(id=purpose, provider_id='p', purpose=purpose, name='fixture-'+purpose,
                status='verified', verified_provider_version=1))


def save(client, body):
    preview = client.post('/api/memory-settings/preview', json=body)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    result = client.put('/api/memory-settings', json=body | {k: data[k] for k in ('preview_digest', 'preview_expires_at')})
    assert result.status_code == 200, result.text
    return result.json(), data


def test_simple_defaults_are_read_only_and_do_not_probe_paid_models(client, monkeypatch):
    monkeypatch.setattr('lingxi.memory_settings.builtin_request', lambda *args: {'models': {
        'embedding': {'state': 'not_downloaded', 'notice': '等待下载'}, 'rerank': {'state': 'ready', 'notice': '就绪'}}})
    response = client.get('/api/memory-settings')
    assert response.status_code == 200
    data = response.json()
    assert data['mode'] == 'simple' and data['version'] == 0
    assert [p['source'] for p in data['purposes']] == ['inherited', 'builtin', 'builtin']
    assert data['purposes'][1]['state'] == 'not_downloaded'
    with SessionLocal() as db:
        assert db.get(MemoryConfiguration, 'personal') is None


def test_legacy_policies_keep_advanced_and_epoch(configured):
    with SessionLocal.begin() as db:
        db.add(Policy(purpose='embedding', model_id='unused', version=7))
        assert configuration(db, create=True).mode == 'advanced'
        assert index_epoch(db) == 7


def test_inherited_model_needs_separate_verification_and_never_mutates_chat(client, monkeypatch):
    chat()
    with SessionLocal() as db:
        with pytest.raises(Exception, match='验证抽取'):
            resolve_model(db, 'extraction')
    monkeypatch.setattr('lingxi.gateway.complete', lambda *a, **k: 'not json')
    result = client.post('/api/memory-settings/verify-extraction')
    assert result.status_code == 200
    assert result.json()['verification_error']
    with SessionLocal() as db:
        assert db.get(Model, 'chat').status == 'verified'
        assert not inherited_verified(db)


def test_verified_inheritance_uses_fixed_model_not_automatic_or_persona(client, monkeypatch):
    chat(); seen = []
    def complete(model, provider, messages, **options):
        seen.append((model.id, options))
        return json.dumps({'items': [{'content': '我长期喜欢喝茶。', 'quote': '我长期喜欢喝茶。',
            'subject': 'self', 'category': 'preferences', 'evidence': 'explicit', 'sensitive': False}]})
    monkeypatch.setattr('lingxi.gateway.complete', complete)
    assert client.post('/api/memory-settings/verify-extraction').status_code == 200
    with SessionLocal.begin() as db:
        assert resolve_model(db, 'extraction')[0].id == 'chat'
        assert inherited_verified(db)
        db.get(Provider, 'p').version += 1
    with SessionLocal() as db:
        assert not inherited_verified(db)
    assert seen[0][1]['purpose'] == 'extraction'


def test_chat_policy_version_change_invalidates_extraction(client):
    chat(); verified()
    with SessionLocal.begin() as db:
        db.get(Policy, 'chat').version += 1
    with SessionLocal() as db:
        assert not inherited_verified(db)


def test_preview_cannot_be_confirmed_after_provider_change(client):
    chat()
    body = {'version': 0, 'mode': 'simple'}
    p = client.post('/api/memory-settings/preview', json=body).json()
    with SessionLocal.begin() as db:
        db.get(Provider, 'p').version += 1
    result = client.put('/api/memory-settings', json=body | {k: p[k] for k in ('preview_digest', 'preview_expires_at')})
    assert result.status_code == 409


def test_preview_does_not_install_or_switch_and_stale_versions_rejected(client):
    chat(); independent()
    body = {'version': 0, 'mode': 'advanced', 'models': {p: p for p in ('extraction', 'embedding', 'rerank')}}
    assert client.post('/api/memory-settings/preview', json=body).status_code == 200
    with SessionLocal() as db:
        assert not db.get(MemoryConfiguration, 'personal')
    data, _ = save(client, body)
    assert data['phase'] == 'draining'
    assert client.post('/api/memory-settings/preview', json=body).status_code == 409


def test_same_configuration_does_not_change_generation(client):
    chat(); verified()
    with SessionLocal() as db:
        original = index_epoch(db)
    data, preview = save(client, {'version': 1, 'mode': 'simple'})
    assert not preview['rebuild_required'] and data['epoch'] == original
    assert data['version'] == 1


def start_rebuild(client):
    chat(); independent(); verified()
    with SessionLocal.begin() as db:
        db.add(Memory(id='memory', user_id='owner', content='我喜欢茶', index_status='ready', index_epoch=0))
    data, preview = save(client, {'version': 1, 'mode': 'advanced',
        'models': {p: p for p in ('extraction', 'embedding', 'rerank')}})
    assert preview['rebuild_required'] and data['phase'] == 'draining'
    return data


def test_rebuild_drains_isolates_epoch_and_stays_paused_until_all_current_records_ready(client):
    data = start_rebuild(client)
    with pytest.raises(Exception, match='维护'):
        with flight(0, recall=True):
            pass
    tick()
    with SessionLocal.begin() as db:
        row = configuration(db)
        assert row.mode == 'advanced' and row.epoch == 1 and row.phase == 'rebuilding'
        assert db.get(Memory, 'memory').index_status == 'pending'
        assert db.scalar(select(Job).where(Job.key == 'reindex:1:memory:1'))
        db.add(Memory(id='new', user_id='owner', content='新增记忆', index_status='pending'))
        db.get(Memory, 'memory').index_status, db.get(Memory, 'memory').index_epoch = 'ready', 1
    tick()
    assert client.get('/api/memory-rebuilds').json()[0]['pending'] == 1
    with SessionLocal.begin() as db:
        db.get(Memory, 'new').status = 'deleted'
    tick()
    with SessionLocal() as db:
        assert configuration(db).phase == 'ready'
        assert db.get(Operation, data['rebuild_id']).status == 'completed'


def test_unknown_old_request_blocks_cutover_and_requires_explicit_recovery(client):
    data = start_rebuild(client)
    with SessionLocal.begin() as db:
        db.add(MemoryFlight(workspace_id='personal', epoch=0, status='running'))
    tick()
    with SessionLocal() as db:
        row = configuration(db)
        assert row.phase == 'blocked' and row.epoch == 0 and row.mode == 'simple'
    path = '/api/memory-rebuilds/'+data['rebuild_id']+'/retry'
    assert client.post(path, json={'version': 2}).status_code == 409
    assert client.post(path, json={'version': 2, 'acknowledge_service_restarted': True}).status_code == 200
    tick()
    with SessionLocal() as db:
        assert configuration(db).epoch == 1


def test_no_old_generation_recall_can_be_accepted_after_switch(client):
    chat(); verified()
    with SessionLocal.begin() as db:
        db.add(Memory(id='m', user_id='owner', content='有效事实', index_status='ready'))
    class Adapter:
        def recall(self, *args):
            with SessionLocal.begin() as db:
                configuration(db).epoch = 1
                m = db.get(Memory, 'm');m.index_epoch = 1
            return {'results': [{'document_id': 'm-v1'}]}
    with SessionLocal() as db:
        with pytest.raises(ValueError, match='generation changed'):
            MemoryService(Adapter()).search(db, 'owner', '事实')


def test_late_recall_is_rejected_as_soon_as_maintenance_starts(client):
    chat(); verified()
    class Adapter:
        def recall(self, *args):
            with SessionLocal.begin() as db:
                configuration(db).phase = 'draining'
            return {'results': []}
    with SessionLocal() as db:
        with pytest.raises(ValueError, match='maintenance started'):
            MemoryService(Adapter()).search(db, 'owner', '事实')


def test_target_connection_drift_blocks_pending_cutover_and_can_be_cancelled(client):
    data = start_rebuild(client)
    with SessionLocal.begin() as db:
        db.get(Provider, 'p').version += 1
    tick()
    current = client.get('/api/memory-rebuilds').json()[0]
    assert current['phase'] == 'blocked' and '目标配置已变化' in current['error']
    result = client.post('/api/memory-rebuilds/'+data['rebuild_id']+'/cancel', json={'version': 2})
    assert result.status_code == 200
    assert result.json()['phase'] == 'ready' and result.json()['mode'] == 'simple'
    assert result.json()['epoch'] == 0


def test_explicit_rebuild_requires_confirmation_and_cannot_cancel_after_cutover(client):
    chat(); verified()
    with SessionLocal.begin() as db:
        db.add(Memory(id='m', user_id='owner', content='当前事实', index_status='ready'))
    assert client.post('/api/memory-rebuilds', json={'version': 1, 'confirmation': 'yes'}).status_code == 422
    result = client.post('/api/memory-rebuilds', json={'version': 1, 'confirmation': 'rebuild_current_index'})
    assert result.status_code == 202
    tick()
    assert client.post('/api/memory-rebuilds/'+result.json()['id']+'/cancel', json={'version': 2}).status_code == 409


def test_fingerprint_drift_stops_recall_and_same_mode_preview_requests_rebuild(client):
    chat(); verified()
    with SessionLocal.begin() as db:
        configuration(db).embedding_fingerprint = 'old-artifact'
        db.add(Memory(id='m', user_id='owner', content='有效事实', index_status='ready'))
    with pytest.raises(Exception, match='指纹变化'):
        with flight(0, recall=True):
            pass
    data, preview = save(client, {'version': 1, 'mode': 'simple'})
    assert preview['rebuild_required'] and data['phase'] == 'draining'
    tick()
    with SessionLocal() as db:
        assert index_epoch(db) == 1


def test_gateway_builtin_uses_authentication_and_never_provider_credentials(client, monkeypatch):
    chat(); verified()
    monkeypatch.setattr(settings(), 'memory_gateway_token', 'test-service-token')
    calls = []
    monkeypatch.setattr('lingxi.memory_settings.builtin_request', lambda route, body: calls.append((route, body)) or {'data': [{'embedding': [0.1]*1024}]})
    route = '/internal/model/v1/embeddings'
    assert client.post(route, json={'input': '中文'}).status_code == 401
    result = client.post(route, json={'input': '中文', 'model': 'malicious'}, headers={'Authorization': 'Bearer test-service-token'})
    assert result.status_code == 200 and calls[0][1]['model'] == 'BAAI/bge-m3'
    with SessionLocal() as db:
        from lingxi.models import ModelCall
        record = db.scalar(select(ModelCall))
        assert record.config['inference_location'] == 'server'
        assert record.billing['amount'] is None


def test_config_endpoints_reject_cross_workspace_and_browser_identity(client):
    chat(); independent()
    with SessionLocal.begin() as db:
        db.get(Model, 'embedding').workspace_id = 'other'
    body = {'version': 0, 'mode': 'advanced', 'models': {'embedding': 'embedding'}}
    assert client.post('/api/memory-settings/preview', json=body).status_code in (409, 422)
    assert client.get('/internal/memory-inference/config').status_code == 401
    assert client.put('/api/memory-settings', json={'workspace_id': 'other'}).status_code == 422


def test_simple_mode_rejects_non_1024_deployment_and_custom_models(client, monkeypatch):
    chat()
    monkeypatch.setattr(settings(), 'memory_embedding_dimensions', 384)
    assert client.post('/api/memory-settings/preview', json={'version': 0, 'mode': 'simple'}).status_code == 409
    monkeypatch.setattr(settings(), 'memory_embedding_dimensions', 1024)
    assert client.post('/api/memory-settings/preview', json={'version': 0, 'mode': 'simple', 'models': {'embedding': 'anything'}}).status_code == 422


def test_transport_timeout_is_durable_unknown_but_http_failure_is_known(client):
    chat()
    with pytest.raises(httpx.ReadTimeout):
        with flight(0):
            raise httpx.ReadTimeout('synthetic')
    with pytest.raises(httpx.HTTPStatusError):
        with flight(0):
            response = httpx.Response(503, request=httpx.Request('POST', 'http://fixture/'))
            response.raise_for_status()
    with SessionLocal() as db:
        assert sorted(db.scalars(select(MemoryFlight.status))) == ['failed', 'unknown']


def test_postgres_active_request_prevents_cutover_without_blocking_callback(client):
    data = start_rebuild(client)
    with SessionLocal.begin() as db:
        if db.bind.dialect.name != 'postgresql':
            pytest.skip('real PostgreSQL advisory lock test')
        row = configuration(db);row.phase = 'ready'
    with flight(0):
        with SessionLocal.begin() as db:
            configuration(db).phase = 'draining'
        tick()
        with SessionLocal() as db:
            assert configuration(db).epoch == 0
        # A callback still resolves the old generation while the switch waits.
        with SessionLocal() as db:
            assert resolve_model(db, 'embedding')[0].name == 'BAAI/bge-m3'
    tick()
    with SessionLocal() as db:
        assert configuration(db).epoch == 1


def test_backup_restore_keeps_latest_mode_and_uses_a_fresh_generation(client):
    from lingxi.backups import checkpoint, quarantine
    from sqlalchemy import text
    chat(); verified()
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0061')"))
        configuration(db).epoch = 9
        db.add(Memory(id='m', user_id='owner', content='恢复前纠正事实', index_epoch=9, index_status='ready'))
    latest = checkpoint()
    with SessionLocal.begin() as db:
        configuration(db).epoch = 2
        configuration(db).mode = 'advanced'
    quarantine(latest)
    with SessionLocal() as db:
        row = configuration(db)
        assert row.mode == 'simple' and row.epoch == 10 and not row.verification
        assert db.get(Memory, 'm').index_status == 'pending'
