"""Persona speech uses verified server settings and immutable turn snapshots."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.api import app
from lingxi.assistants import AssistantProfile
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, LoginSession, Message, ModelCall, Provider
from lingxi.voice import VoiceConfig, VoiceSession
from test_voice import opened, reply, transport, voice_config, wav


def persona(client, voice='nova', **extra):
    response = client.post('/api/personas', json={'name': '合成语音人格', 'voice': voice} | extra)
    assert response.status_code == 200, response.text
    return response.json()


def select_persona(client, session, profile, version=0):
    result = client.put(f"/api/conversations/{session['conversation_id']}/persona",
                       json={'persona_id': profile['id'], 'version': version})
    assert result.status_code == 200, result.text


def follow(client):
    config = client.get('/api/voice/config').json()
    result = client.put('/api/voice/config', json=config | {'follow_persona': True})
    assert result.status_code == 200


def following_session(client):
    voice_config(client)
    follow(client)
    conversation = client.post('/api/conversations', json={'title': '人格语音'}).json()
    result = client.post('/api/voice/sessions', json={'conversation_id': conversation['id']})
    assert result.status_code == 200, result.text
    return result.json()


def verify(client, profile):
    return client.post(f"/api/voice/personas/{profile['id']}/test", json={'version': profile['version']})


def test_link_is_explicit_opt_in_and_default_voice_unchanged(configured, transport):
    s = opened(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    p = reply(configured, s)
    assert p['voice'] == 'alloy' and p['voice_source'] == 'system'
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 200
    assert json.loads(transport[-1].content)['voice'] == 'alloy'
    assert configured.get('/api/voice/config').json()['follow_persona'] is False


def test_verified_persona_voice_used_and_persisted_per_turn(configured, transport):
    s = following_session(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    assert verify(configured, profile).status_code == 200
    p = reply(configured, s)
    assert (p['voice'], p['voice_source'], p['persona_version']) == ('nova', 'persona', 1)
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 200
    assert json.loads(transport[-1].content)['voice'] == 'nova'
    with SessionLocal() as db:
        run = db.get(ChatRun, db.get(Message, p['message_id']).run_id)
        assert run.config['speech']['persona_id'] == profile['id']
        assert db.get(VoiceConfig, 'owner').spec['voice'] == 'alloy'
        calls = list(db.scalars(select(ModelCall).where(ModelCall.purpose == 'tts')))
        assert len(calls) == 2 and all(c.config['voice'] == 'nova' for c in calls)


def test_unverified_voice_keeps_text_without_silent_fallback(configured, transport):
    s = following_session(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    result = configured.post(f"/api/voice/sessions/{s['id']}/messages",
        json={'content': '请回答', 'request_id': 'unverified-persona'})
    assert result.status_code == 200
    with SessionLocal.begin() as db:
        run = db.get(ChatRun, result.json()['id'])
        run.status = 'completed'
        message = Message(conversation_id=s['conversation_id'], role='assistant', content='文字仍保留。', run_id=run.id)
        db.add(message)
        db.flush()
        identity = message.id
    result = configured.post(f"/api/voice/sessions/{s['id']}/playbacks", json={'message_id': identity})
    assert result.status_code == 409 and '音色未验证' in result.text
    with SessionLocal() as db:
        assert db.get(Message, identity).content == '文字仍保留。'
    assert transport == []
    assert verify(configured, profile).status_code == 200
    assert configured.post(f"/api/voice/sessions/{s['id']}/playbacks", json={'message_id': identity}).status_code == 200


@pytest.mark.parametrize('voice', ['', 'alloy'])
def test_empty_persona_or_same_default_reuses_system_validation(configured, transport, voice):
    s = following_session(configured)
    profile = persona(configured, voice)
    select_persona(configured, s, profile)
    p = reply(configured, s)
    assert p['voice'] == 'alloy'
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 200


def test_edit_and_switch_only_affect_new_turns_idempotent_retry_keeps_snapshot(configured, transport):
    s = following_session(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    assert verify(configured, profile).status_code == 200
    p = reply(configured, s)
    updated = configured.put('/api/personas/' + profile['id'],
        json={'name': '人格新版', 'voice': 'echo', 'version': 1}).json()
    assert updated['version'] == 2
    retry = configured.post(f"/api/voice/sessions/{s['id']}/messages",
        json={'content': '请回答', 'request_id': 'voice-message-001'})
    assert retry.status_code == 200 and retry.json()['config']['speech']['voice'] == 'nova'
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 200
    assert json.loads(transport[-1].content)['voice'] == 'nova'
    new = configured.post(f"/api/voice/sessions/{s['id']}/messages",
        json={'content': '下一轮', 'request_id': 'new-persona-voice-round'})
    assert new.status_code == 200 and new.json()['config']['speech']['voice'] == 'echo'
    assert new.json()['config']['speech']['persona_version'] == 2


@pytest.mark.parametrize('change', ['disabled', 'deleted', 'workspace', 'owner', 'kind'])
def test_persona_revocation_prevents_audio(configured, transport, change):
    s = following_session(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    assert verify(configured, profile).status_code == 200
    p = reply(configured, s)
    with SessionLocal.begin() as db:
        row = db.get(AssistantProfile, profile['id'])
        if change == 'disabled': row.enabled = False
        if change == 'deleted': db.delete(row)
        if change == 'workspace': row.workspace_id = 'other'
        if change == 'owner': row.user_id = 'other'
        if change == 'kind': row.kind = 'role'
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code in (404, 409)
    assert len(transport) == 1


@pytest.mark.parametrize('change', ['persona', 'disabled', 'provider', 'config', 'login', 'close', 'source'])
def test_late_audio_discarded_after_changes(configured, transport, monkeypatch, change):
    s = following_session(configured)
    profile = persona(configured)
    select_persona(configured, s, profile)
    assert verify(configured, profile).status_code == 200
    p = reply(configured, s)
    def upstream(*args, **kwargs):
        with SessionLocal.begin() as db:
            if change == 'persona':
                # Editing expression/voice is for NEW rounds, not a revocation.
                item = db.get(AssistantProfile, profile['id'])
                item.version += 1
                item.spec = item.spec | {'voice': 'echo'}
            if change == 'disabled': db.get(AssistantProfile, profile['id']).enabled = False
            if change == 'provider': db.get(Provider, 'provider').version += 1
            if change == 'config': db.get(VoiceConfig, 'owner').version += 1
            if change == 'login': db.get(LoginSession, db.get(VoiceSession, s['id']).login_session_id).expires_at = now() - 1
            if change == 'close': db.get(VoiceSession, s['id']).status = 'closed'
            if change == 'source': db.get(Message, p['message_id']).status = 'invalidated'
        return wav()
    monkeypatch.setattr('lingxi.voice.audio_call', upstream)
    result = configured.post(f"/api/voice/playbacks/{p['id']}/audio/0")
    assert result.status_code == (200 if change == 'persona' else 401 if change == 'login' else 409)
    if change != 'persona': assert result.content[:4] != b'RIFF'


@pytest.mark.parametrize('change', ['persona', 'disabled', 'provider', 'config', 'login'])
def test_persona_test_rechecks_before_marking_verified(configured, monkeypatch, change):
    voice_config(configured)
    profile = persona(configured)
    def upstream(*args, **kwargs):
        with SessionLocal.begin() as db:
            if change == 'persona': db.get(AssistantProfile, profile['id']).version += 1
            if change == 'disabled': db.get(AssistantProfile, profile['id']).enabled = False
            if change == 'provider': db.get(Provider, 'provider').version += 1
            if change == 'config': db.get(VoiceConfig, 'owner').version += 1
            if change == 'login': db.scalar(select(LoginSession)).expires_at = now() - 1
        return wav()
    monkeypatch.setattr('lingxi.voice.audio_call', upstream)
    result = verify(configured, profile)
    assert result.status_code in (401, 409) and result.content[:4] != b'RIFF'
    with SessionLocal() as db:
        assert not any(k.startswith('tts_voice:') for k in db.get(VoiceConfig, 'owner').checks)


@pytest.mark.parametrize('voice', ['a' * 81, 'voice instructions', 'https://voice.example', '音色'])
def test_invalid_legacy_persona_voice_reported_not_sent(configured, transport, voice):
    voice_config(configured)
    profile = persona(configured, voice)
    result = configured.get('/api/voice/personas').json()
    item = next(x for x in result if x['id'] == profile['id'])
    assert item['issue'] and not item['verified']
    assert verify(configured, profile).status_code == 422
    assert transport == []


def test_validation_tracks_effective_provider_model_speed_and_reuses_equal_voice(configured, transport):
    voice_config(configured)
    profile = persona(configured)
    assert verify(configured, profile).status_code == 200
    other = persona(configured)
    values = configured.get('/api/voice/personas').json()
    assert all(x['verified'] for x in values if x['id'] in (profile['id'], other['id']))
    config = configured.get('/api/voice/config').json()
    configured.put('/api/voice/config', json=config | {'speed': 1.5})
    assert not next(x for x in configured.get('/api/voice/personas').json() if x['id'] == profile['id'])['verified']


def test_auth_csrf_scope_stale_version_and_no_auto_calls(configured, transport):
    voice_config(configured)
    profile = persona(configured)
    assert configured.get('/api/voice/personas').status_code == 200 and transport == []
    assert configured.post(f"/api/voice/personas/{profile['id']}/test", json={'version': 99}).status_code == 409
    assert configured.post('/api/voice/personas/missing/test', json={'version': 1}).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/voice/personas').status_code == 401
        assert anonymous.post(f"/api/voice/personas/{profile['id']}/test", json={'version': 1}).status_code == 401
    configured.headers.pop('X-CSRF-Token')
    assert verify(configured, profile).status_code == 403
    assert transport == []
