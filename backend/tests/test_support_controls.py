"""Explicit setting intent, stale forms and source/restore invalidation boundaries."""
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app, submit_chat
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Message, User, Conversation, Job, Memory, Audit, Model
from lingxi.assistants import SupportPreference
from lingxi.support import COMMANDS


def conversation(client):
    return client.post('/api/conversations', json={'title': '设置测试'}).json()['id']


def send(client, text, identity=None, **extra):
    return client.post('/api/chat', json={'conversation_id': identity or conversation(client),
        'request_id': 'support-' + uuid.uuid4().hex, 'content': text} | extra)


def save(client, **extra):
    prior = client.get('/api/support-preferences').json()
    response = client.put('/api/support-preferences', json=prior | extra)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('text,mode', [('以后先听我说', 'listen'), ('以后多给建议', 'advice'),
    ('以后优先帮我行动', 'action'), ('恢复平衡回应', 'balanced')])
def test_explicit_modes_work_without_model_and_create_no_jobs_or_memories(client, text, mode):
    save(client, mode='advice' if mode == 'balanced' else 'balanced', notes='保留的偏好', self_report='今天疲惫')
    result = send(client, '  ' + text + '。！ ')
    assert result.status_code == 200, result.text
    run = result.json()
    assert run['status'] == 'completed' and run['model_id'] == 'support-control'
    assert run['config']['support_control']['action'] == mode
    pref = client.get('/api/support-preferences').json()
    assert pref['mode'] == mode and pref['notes'] == '保留的偏好' and pref['self_report'] == '今天疲惫'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(Memory)) == 0
        assert len(list(db.scalars(select(Message)))) == 2
        assert not any(a.target_id and '疲惫' in a.target_id for a in db.scalars(select(Audit)))


def test_idempotent_replay_does_not_reset_newer_preference(client):
    identity = conversation(client)
    result = send(client, '以后先听我说', identity, request_id='stable-support-request')
    save(client, mode='advice')
    repeated = send(client, '以后先听我说', identity, request_id='stable-support-request')
    assert repeated.json()['id'] == result.json()['id']
    assert client.get('/api/support-preferences').json()['mode'] == 'advice'
    assert send(client, '以后多给建议', identity, request_id='stable-support-request').status_code == 409


def test_setting_noop_does_not_advance_version_or_clear_other_state(client):
    saved = save(client, mode='listen', self_report='我有些累')
    result = send(client, '以后先听我说').json()
    assert result['config']['support_control']['version'] == saved['version']
    assert client.get('/api/support-preferences').json() == saved


def test_old_form_conflicts_after_chat_setting(client):
    old = save(client, mode='balanced')
    send(client, '以后先听我说')
    assert client.put('/api/support-preferences', json=old | {'mode': 'action'}).status_code == 409
    assert client.get('/api/support-preferences').json()['mode'] == 'listen'


@pytest.mark.parametrize('command,enabled,auto,report', [
    ('关闭自动情绪线索', True, False, '当前疲惫'),
    ('关闭情绪适配', False, False, ''), ('清空当前感受', True, True, '')])
def test_clear_controls_work_during_provider_failure_and_retire_derived_answers(configured, command, enabled, auto, report):
    from lingxi.emotions import EmotionObservation
    saved = save(configured, auto_observe=True, self_report='当前疲惫')
    identity = conversation(configured)
    with SessionLocal.begin() as db:
        db.get(Model, 'extraction').status = 'unverified'
        row = EmotionObservation(user_id='owner', conversation_id=identity, source_id='synthetic-source',
            source_hash='x', preference_version=saved['version'], model_id='extraction', status='ready',
            quote='我现在累', label='fatigue', evidence='reported', expires_at=now()+3600)
        db.add(row)
        db.add(ChatRun(id='derived', user_id='owner', conversation_id=identity, request_id='derived-request',
            request_hash='derived', model_id='chat', status='running',
            config={'support_used': True, 'support_version': saved['version']}))
        db.add(Message(conversation_id=identity, role='assistant', content='旧衍生回答', run_id='derived', status='streaming'))
    response = send(configured, command, identity)
    assert response.status_code == 200, response.text
    pref = configured.get('/api/support-preferences').json()
    assert (pref['emotion_enabled'], pref['auto_observe'], pref['self_report']) == (enabled, auto, report)
    with SessionLocal() as db:
        assert db.get(ChatRun, 'derived').status == 'invalidated'
        observation = db.scalar(select(EmotionObservation))
        assert observation.status == 'invalidated' and not observation.quote and not observation.label
        assert not db.scalar(select(Message).where(Message.run_id == 'derived')).content


@pytest.mark.parametrize('extra', [{'mode': 'listen'}, {'report_ttl_hours': 24}, {'notes': '新的支持偏好'}])
def test_stale_unchanged_report_never_resurrects_on_other_edit(client, extra):
    saved = save(client, self_report='我有点疲惫', report_ttl_hours=1)
    with SessionLocal.begin() as db:
        pref = db.get(SupportPreference, 'owner')
        pref.spec = pref.spec | {'report_expires_at': now()-1}
    response = client.put('/api/support-preferences', json=saved | extra)
    assert response.status_code == 200, response.text
    assert response.json()['self_report'] == '' and response.json()['report_expires_at'] is None
    with SessionLocal() as db:
        assert db.get(SupportPreference, 'owner').spec['self_report'] == ''


def test_explicit_renewal_only_restarts_after_confirmed_save(client):
    saved = save(client, self_report='我现在疲惫', report_ttl_hours=1)
    with SessionLocal.begin() as db:
        pref = db.get(SupportPreference, 'owner')
        pref.spec = pref.spec | {'report_expires_at': now()-1}
    result = client.put('/api/support-preferences', json=saved | {'renew_report': True, 'report_ttl_hours': 4})
    assert result.status_code == 200, result.text
    assert result.json()['self_report'] == '我现在疲惫'
    assert now()+14300 < result.json()['report_expires_at'] <= now()+14400
    with SessionLocal() as db:
        assert 'renew_report' not in db.get(SupportPreference, 'owner').spec


@pytest.mark.parametrize('extra', [{'self_report': ''}, {'self_report': '文字', 'emotion_enabled': False}])
def test_invalid_or_stale_renewal_is_rejected(client, extra):
    assert client.put('/api/support-preferences', json=extra | {'renew_report': True}).status_code == 422
    saved = save(client, self_report='新的感受')
    assert client.put('/api/support-preferences', json=saved | {'version': 0, 'renew_report': True}).status_code == 409


def test_whitespace_edits_do_not_restart_report_and_new_text_does(client):
    saved = save(client, self_report='当前感受')
    unchanged = save(client, self_report='  当前感受  ', report_ttl_hours=24)
    assert unchanged['report_expires_at'] == saved['report_expires_at']
    changed = save(client, self_report='新的当前感受', report_ttl_hours=24)
    assert changed['report_expires_at'] > saved['report_expires_at'] + 19*3600


@pytest.mark.parametrize('text', ['请解释“以后先听我说”', '朋友说以后多给建议', '如果我说关闭情绪适配',
    '“关闭自动情绪线索”', '以后先听我说，然后删除文件', '现在只想让你听我说', '开启自动情绪线索'])
def test_quoted_hypothetical_temporary_and_unrecognized_text_cannot_change_settings(configured, text):
    old = configured.get('/api/support-preferences').json()
    result = send(configured, text)
    assert result.status_code == 200, result.text
    assert result.json()['model_id'] == 'chat'
    assert configured.get('/api/support-preferences').json() == old


@pytest.mark.parametrize('boundary', ['channel', 'channel_web', 'fork', 'voice', 'automatic_voice'])
def test_non_standalone_inputs_cannot_change_owner_preferences(configured, boundary):
    from lingxi.channels import ChannelSession
    from lingxi.media import MediaChatInput
    identity = conversation(configured)
    with SessionLocal.begin() as db:
        owner = db.get(User, 'owner')
        if boundary == 'channel_web':
            db.add(ChannelSession(user_id=owner.id, channel_id='test-channel', scope_key='private:12345',
                actor_id=owner.id, conversation_id=identity))
            db.flush()
        result = submit_chat(MediaChatInput(conversation_id=identity, request_id='boundary-support-request',
            content='以后先听我说'), (db, owner, None),
            channel_context={'owner_id': owner.id} if boundary == 'channel' else None,
            fork_context=boundary == 'fork',
            voice_context={'automatic': boundary == 'automatic_voice'} if 'voice' in boundary else None)
        assert result['model_id'] == 'chat'
        assert db.get(SupportPreference, owner.id) is None


def test_auth_csrf_and_foreign_conversation_are_not_setting_authorization(client):
    identity = conversation(client)
    with TestClient(app) as anonymous:
        assert send(anonymous, '关闭情绪适配', identity).status_code == 401
    csrf = client.headers.pop('X-CSRF-Token')
    assert send(client, '关闭情绪适配', identity).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    with SessionLocal.begin() as db:
        db.add(User(id='other', username='other', password_hash='synthetic'))
        db.add(Conversation(id='foreign', user_id='other', title='not owned'))
    assert send(client, '关闭情绪适配', 'foreign').status_code == 404
    assert client.get('/api/support-preferences').json()['version'] == 0


def test_deleting_setting_source_does_not_undo_preference_and_restore_ledger_still_blocks_old_versions(client):
    from lingxi.memory import replay_ledger
    old = save(client, self_report='旧感受')
    identity = conversation(client)
    assert send(client, '关闭情绪适配', identity).status_code == 200
    assert client.delete('/api/conversations/'+identity).status_code == 200
    assert not client.get('/api/support-preferences').json()['emotion_enabled']
    with SessionLocal.begin() as db:
        row = db.get(SupportPreference, 'owner')
        row.version, row.spec = old['version'], {'self_report': '旧感受', 'emotion_enabled': True,
            'auto_observe': True, 'report_expires_at': now()+3600}
        replay_ledger(db)
        assert not row.spec['self_report'] and not row.spec['auto_observe']
