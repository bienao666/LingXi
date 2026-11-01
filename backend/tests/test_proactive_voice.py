"""Active-session consent, grounded speech, freshness, interruption and receipts."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now, engine
from lingxi.models import ChatRun, LoginSession, Memory, Message, Provider, User
from lingxi.proactive import ProactiveContact, ConversationTopic, ContactPreference, deliver
from lingxi.proactive_voice import VoiceContact
from lingxi.voice import VoiceSession, VoicePlayback, heard_history
from test_voice import opened, transport, wav
from test_proactive import prefs, topic, memory, contacts


def setup(client, enabled=True, **preferences):
    session = opened(client)
    prefs(client, quiet_start='00:00', quiet_end='00:00', conversation_id=session['conversation_id'], **preferences)
    if enabled:
        assert client.put(f"/api/voice/sessions/{session['id']}/proactive", json={'enabled': True}).status_code == 200
    row = topic(client)
    deliver(row['id'])
    contact = contacts(client)[0]
    return session, contact


def claim(client, session, idle=4000):
    result = client.post(f"/api/voice/sessions/{session['id']}/proactive-playback", json={'idle_ms': idle})
    assert result.status_code == 200, result.text
    return result.json()['playback']


def test_consent_default_off_and_no_old_contact_replay(configured):
    session, contact = setup(configured, False)
    assert session['allow_proactive'] is False and contact['voice'] is None
    assert claim(configured, session) is None
    configured.put(f"/api/voice/sessions/{session['id']}/proactive", json={'enabled': True})
    assert claim(configured, session) is None


def test_explicit_consent_at_open_and_live_switch(configured):
    s = opened(configured)
    new = configured.post('/api/voice/sessions', json={'conversation_id': s['conversation_id'], 'allow_proactive': True})
    assert new.status_code == 200 and new.json()['allow_proactive'] is True
    assert configured.put(f"/api/voice/sessions/{new.json()['id']}/proactive", json={'enabled': False}).json() == {'enabled': False}


def test_claim_is_once_and_renders_only_verified_contact_text(configured, transport):
    s, contact = setup(configured)
    assert contact['voice']['status'] == 'candidate'
    plan = claim(configured, s)
    assert plan['proactive'] is True and plan['contact_id'] == contact['id']
    assert claim(configured, s) is None
    assert len(transport) == 0
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0")
    assert result.status_code == 200 and result.content[:4] == b'RIFF'
    assert plan['parts'][0] in transport[0].content.decode()
    assert 'secret-fixture-key' not in configured.get(f"/api/voice/playbacks/{plan['id']}").text
    assert configured.post(f"/api/voice/sessions/{s['id']}/playbacks", json={'message_id': contact['message_id']}).status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(VoicePlayback)) == 1


def test_short_pause_or_busy_core_defers_without_claiming(configured):
    s, _ = setup(configured)
    assert claim(configured, s, 2999) is None
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='busy', user_id='owner', conversation_id=s['conversation_id'],
            request_id='busy-request', request_hash='x', model_id='chat', status='running'))
    assert claim(configured, s) is None
    with SessionLocal.begin() as db:
        db.get(ChatRun, 'busy').status = 'completed'
    assert claim(configured, s) is not None


def test_expired_candidates_are_dropped_not_replayed(configured):
    s, _ = setup(configured)
    with SessionLocal.begin() as db:
        db.scalar(select(VoiceContact)).expires_at = now()-1
    assert claim(configured, s) is None
    assert contacts(configured)[0]['voice']['status'] == 'expired'


def test_voice_topics_have_no_fixed_external_count_or_gap(configured):
    s, _ = setup(configured, daily_limit=0, min_gap_hours=24)
    for i in range(3):
        row = topic(configured, memory(configured, content=f'我的项目线索{i}'))
        deliver(row['id'])
    assert len(contacts(configured)) == 4
    assert all(c['voice'] for c in contacts(configured))
    from lingxi.proactive_channels import quota_times
    with SessionLocal() as db:
        assert quota_times(db, db.get(User, 'owner')) == []


def test_ordinary_quota_still_applies_after_voice_close(configured):
    s, _ = setup(configured, daily_limit=1)
    configured.delete(f"/api/voice/sessions/{s['id']}")
    first = topic(configured, memory(configured, content='普通交流线索一'))
    deliver(first['id'])
    second = topic(configured, memory(configured, content='普通交流线索二'))
    deliver(second['id'])
    rows = contacts(configured)
    assert len(rows) == 2 and sum(c['voice'] is None for c in rows) == 1


@pytest.mark.parametrize('change', ['enabled', 'pause', 'quiet', 'mode', 'kind', 'blocked', 'source', 'topic', 'message', 'new_input'])
def test_contact_constraints_rechecked_before_claim(configured, change):
    s, contact = setup(configured)
    with SessionLocal.begin() as db:
        p, t = db.get(ContactPreference, 'owner'), db.get(ConversationTopic, contact['topic_id'])
        if change == 'enabled': p.enabled = False
        if change == 'pause': p.paused_until = now()+600
        if change == 'quiet': p.quiet_start, p.quiet_end = '00:00', '23:59'
        if change == 'mode': p.mode = 'results_only'
        if change == 'kind': p.allowed_kinds = []
        if change == 'blocked': p.blocked_sources = ['memory:'+t.source_id]
        if change == 'source': db.get(Memory, t.source_id).version += 1
        if change == 'topic': t.status = 'closed'
        if change == 'message': db.get(Message, contact['message_id']).status = 'invalidated'
        if change == 'new_input': db.add(Message(conversation_id=s['conversation_id'], role='user', content='我有新问题'))
    assert claim(configured, s) is None


@pytest.mark.parametrize('change', ['closed', 'expired', 'heartbeat', 'login', 'config', 'other_login'])
def test_voice_liveness_and_login_scope_are_required(configured, transport, change):
    s, _ = setup(configured)
    with SessionLocal.begin() as db:
        row = db.get(VoiceSession, s['id'])
        if change == 'closed': row.status = 'closed'
        if change == 'expired': row.expires_at = now()-1
        if change == 'heartbeat': row.heartbeat_at = now()-61
        if change == 'login': db.get(LoginSession, row.login_session_id).expires_at = now()-1
        if change == 'config': row.config_version += 1
        if change == 'other_login': row.login_session_id = 'unrelated-login'
    result = configured.post(f"/api/voice/sessions/{s['id']}/proactive-playback", json={'idle_ms': 4000})
    assert result.status_code in (401, 404, 409) and transport == []


def test_feedback_and_consent_stop_active_playback_without_replay(configured, transport):
    s, contact = setup(configured)
    plan = claim(configured, s)
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 200
    response = configured.post(f"/api/proactive-contacts/{contact['id']}/feedback", json={'version': contact['version'], 'feedback': 'pause'})
    assert response.status_code == 200
    assert configured.get(f"/api/voice/playbacks/{plan['id']}").status_code == 409
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 409
    configured.put(f"/api/voice/sessions/{s['id']}/proactive", json={'enabled': False})
    configured.put(f"/api/voice/sessions/{s['id']}/proactive", json={'enabled': True})
    assert claim(configured, s) is None


def test_delete_source_during_synthesis_discards_late_audio(configured, monkeypatch):
    s, contact = setup(configured)
    plan = claim(configured, s)
    def audio(*a, **kw):
        with SessionLocal.begin() as db:
            from lingxi.memory import MemoryService
            t = db.get(ConversationTopic, contact['topic_id'])
            MemoryService().forget(db, db.get(Memory, t.source_id))
        return wav()
    monkeypatch.setattr('lingxi.voice.audio_call', audio)
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0")
    assert result.status_code == 409 and not result.content.startswith(b'RIFF')


def test_only_confirmed_speech_enters_voice_history_and_web_text_is_unchanged(configured, transport):
    s, contact = setup(configured)
    with SessionLocal() as db:
        m = db.get(Message, contact['message_id'])
        run = ChatRun(user_id='owner', workspace_id='personal', config={'voice_session_id': s['id']})
        original = m.content
        assert heard_history(db, run, [m]) == []
    plan = claim(configured, s)
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 200
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/progress", json={'completed_parts': 1, 'stop': True}).status_code == 200
    with SessionLocal() as db:
        m = db.get(Message, contact['message_id'])
        assert heard_history(db, run, [m])[0].content == plan['parts'][0]
        assert m.content == original
    assert claim(configured, s) is None


def test_candidate_does_not_reroute_qq_or_another_web_conversation(configured):
    s, _ = setup(configured)
    other = configured.post('/api/conversations', json={'title': '其他窗口'}).json()
    prefs(configured, conversation_id=other['id'])
    row = topic(configured, memory(configured, content='其他窗口的线索'))
    deliver(row['id'])
    assert contacts(configured)[0]['voice'] is None
    from lingxi.proactive_voice import selected_session
    with SessionLocal() as db:
        assert selected_session(db, db.get(User, 'owner'), db.get(ConversationTopic, row['id']),
            {'delivery': {'kind': 'qq'}, 'conversation_id': None}) is None


def test_auth_csrf_and_owner_isolation(configured):
    s, _ = setup(configured)
    path = f"/api/voice/sessions/{s['id']}/proactive-playback"
    csrf = configured.headers.pop('X-CSRF-Token')
    assert configured.post(path, json={'idle_ms': 4000}).status_code == 403
    configured.headers['X-CSRF-Token'] = csrf
    with SessionLocal.begin() as db:
        db.get(VoiceSession, s['id']).workspace_id = 'other'
    assert configured.post(path, json={'idle_ms': 4000}).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.post(path, json={'idle_ms': 4000}).status_code == 401


def test_switching_off_and_back_on_never_revives_old_candidate(configured):
    s, _ = setup(configured)
    path = f"/api/voice/sessions/{s['id']}/proactive"
    assert configured.put(path, json={'enabled': False}).status_code == 200
    assert configured.put(path, json={'enabled': True}).status_code == 200
    assert claim(configured, s) is None
    with SessionLocal() as db:
        assert db.scalar(select(VoiceContact)).status == 'invalidated'


def test_new_session_cannot_replay_old_session_contact(configured):
    s, _ = setup(configured)
    newer = configured.post('/api/voice/sessions', json={
        'conversation_id': s['conversation_id'], 'allow_proactive': True}).json()
    assert claim(configured, newer) is None
    with SessionLocal() as db:
        assert db.get(VoiceSession, s['id']).status == 'closed'
        assert db.scalar(select(VoiceContact)).status == 'invalidated'


def test_restore_quarantine_closes_voice_and_fences_unfinished_contact(configured, transport):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    s, _ = setup(configured)
    plan = claim(configured, s)
    with SessionLocal.begin() as db:
        schema(db)
    latest = checkpoint()
    quarantine(latest)
    with SessionLocal() as db:
        assert db.get(VoiceSession, s['id']).status == 'closed'
        assert db.scalar(select(VoiceContact)).status == 'invalidated'
        assert db.get(VoicePlayback, plan['id']).status == 'stopped'
        assert db.scalar(select(LoginSession)) is None
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 401
    assert transport == []


@pytest.mark.parametrize('change', ['source', 'new_input', 'consent', 'session'])
def test_active_playback_rechecks_invalidation_on_status_and_each_part(configured, transport, change):
    s, contact = setup(configured)
    plan = claim(configured, s)
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 200
    with SessionLocal.begin() as db:
        if change == 'source':
            t = db.get(ConversationTopic, contact['topic_id'])
            db.get(Memory, t.source_id).version += 1
        if change == 'new_input':
            db.add(Message(conversation_id=s['conversation_id'], role='user', content='换个话题'))
        if change == 'consent':
            db.get(VoiceSession, s['id']).allow_proactive = False
        if change == 'session':
            db.get(VoiceSession, s['id']).status = 'closed'
    assert configured.get(f"/api/voice/playbacks/{plan['id']}").status_code == 409
    assert configured.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code == 409
    assert len(transport) == 1


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Requires production row locks')
def test_concurrent_claims_create_one_playback(configured):
    s, _ = setup(configured)
    def request(_):
        with TestClient(app) as c:
            c.cookies.update(configured.cookies)
            c.headers['X-CSRF-Token'] = configured.headers['X-CSRF-Token']
            return claim(c, s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert sum(r is not None for r in results) == 1
