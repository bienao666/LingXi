"""Synthetic protocol/source-safety coverage; not physical audio quality evidence."""
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from lingxi.db import SessionLocal
from lingxi.models import Candidate, ChatRun, Job, Memory, Message
from lingxi.voice import VoiceSession
from test_voice import opened, post_audio, voice_config, transport  # noqa: F401


def continuous(client):
    previous = opened(client)
    result = client.post('/api/voice/sessions', json={'conversation_id': previous['conversation_id'], 'mode': 'continuous'})
    assert result.status_code == 200, result.text
    return result.json()


def send(client, session, content='我喜欢喝茶。', request='automatic-voice-001', **extra):
    return client.post(f"/api/voice/sessions/{session['id']}/messages",
        json={'content': content, 'request_id': request, 'automatic': True, **extra})


def test_default_manual_and_explicit_continuous_mode(configured):
    s = opened(configured)
    assert s['mode'] == 'manual' and s['automatic_turns'] == 0
    assert send(configured, s).status_code == 409
    assert configured.post('/api/voice/sessions', json={'conversation_id': s['conversation_id'], 'mode': 'always-on'}).status_code == 422
    s = configured.post('/api/voice/sessions', json={'conversation_id': s['conversation_id'], 'mode': 'continuous'}).json()
    assert s['mode'] == 'continuous'
    assert configured.post(f"/api/voice/sessions/{s['id']}/heartbeat").json()['mode'] == 'continuous'


def test_automatic_transcript_has_source_marker_and_no_memory_outbox(configured):
    s = continuous(configured)
    first, replay = send(configured, s), send(configured, s)
    assert first.status_code == 200 and replay.json()['id'] == first.json()['id']
    with SessionLocal() as db:
        run = db.get(ChatRun, first.json()['id'])
        assert run.config['voice_input'] == {'automatic': True, 'confirmed': False}
        assert run.config['voice_session_id'] == s['id']
        assert 'task_assist' not in run.config
        assert len(list(db.scalars(select(Message)))) == 1
        assert [j.kind for j in db.scalars(select(Job))] == ['chat']
        assert db.get(VoiceSession, s['id']).automatic_turns == 1
        assert not db.scalar(select(Candidate)) and not db.scalar(select(Memory))


@pytest.mark.parametrize('start_automatic', [True, False])
def test_idempotency_cannot_reinterpret_confirmation(configured, start_automatic):
    s = continuous(configured)
    assert send(configured, s, automatic=start_automatic).status_code == 200
    assert send(configured, s, automatic=not start_automatic).status_code == 409
    assert send(configured, s, content='不同内容', automatic=start_automatic).status_code == 409
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChatRun)))) == 1
        assert db.get(VoiceSession, s['id']).automatic_turns == int(start_automatic)


def test_automatic_request_cannot_be_replayed_as_web_input(configured):
    s = continuous(configured)
    assert send(configured, s).status_code == 200
    result = configured.post('/api/chat', json={'conversation_id': s['conversation_id'],
        'content': '我喜欢喝茶。', 'request_id': 'automatic-voice-001'})
    assert result.status_code == 409


@pytest.mark.parametrize('text', ['先别聊', '这个以后别问', '直接告诉我结果', '帮我删除服务器文件'])
def test_automatic_input_cannot_change_preferences_or_enable_tools(configured, monkeypatch, text):
    s = continuous(configured)
    def forbidden(*args, **kwargs): raise AssertionError('unconfirmed transcript is not authorization')
    monkeypatch.setattr('lingxi.proactive.chat_control', forbidden)
    result = send(configured, s, content=text, task_assist=True, collaboration={'goal': 'execute'})
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        run = db.get(ChatRun, result.json()['id'])
        assert run.model_id != 'contact-control' and 'task_assist' not in run.config
        assert [j.kind for j in db.scalars(select(Job))] == ['chat']


def test_continuous_dialogue_has_no_fixed_turn_cap_and_replay_is_not_counted(configured, transport):
    s = continuous(configured)
    with SessionLocal.begin() as db: db.get(VoiceSession, s['id']).automatic_turns = 19
    result = send(configured, s)
    assert result.status_code == 200
    assert send(configured, s).json()['id'] == result.json()['id']
    with SessionLocal.begin() as db: db.get(ChatRun, result.json()['id']).status = 'completed'
    assert send(configured, s, request='automatic-voice-002').status_code == 200
    assert post_audio(configured, f"/api/voice/sessions/{s['id']}/transcriptions").status_code == 200
    assert len(transport) == 1
    with SessionLocal() as db: assert db.get(VoiceSession, s['id']).automatic_turns == 21


def test_failed_submission_does_not_consume_turn(configured):
    s = continuous(configured)
    result = send(configured, s)
    assert result.status_code == 200
    assert send(configured, s, request='automatic-voice-002').status_code == 409
    with SessionLocal() as db: assert db.get(VoiceSession, s['id']).automatic_turns == 1


def test_explicitly_confirmed_input_retains_memory_path(configured):
    s = continuous(configured)
    assert send(configured, s, automatic=False).status_code == 200
    with SessionLocal() as db:
        assert db.get(VoiceSession, s['id']).automatic_turns == 0
        assert sorted(j.kind for j in db.scalars(select(Job))) == ['chat', 'extract']


@pytest.mark.parametrize('automatic', [False, True])
def test_voice_retains_fixed_model_policy(configured, monkeypatch, automatic):
    from lingxi.model_routing import select_chat
    observed = []
    def selection(*a, **k):
        observed.append(k['assisted'])
        return select_chat(*a, **k)
    monkeypatch.setattr('lingxi.model_routing.select_chat', selection)
    s = continuous(configured)
    assert send(configured, s, automatic=automatic).status_code == 200
    assert observed == [True]


def test_restore_closes_continuous_session_and_invalidates_login(configured):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    from lingxi.models import LoginSession
    s = continuous(configured)
    with SessionLocal.begin() as db: schema(db)
    quarantine(checkpoint())
    with SessionLocal() as db:
        assert db.get(VoiceSession, s['id']).status == 'closed'
        assert not db.scalar(select(LoginSession))


def test_rogue_retried_extraction_rejects_unconfirmed_source_before_model(configured, monkeypatch):
    s = continuous(configured)
    assert send(configured, s).status_code == 200
    with SessionLocal() as db: source = db.scalar(select(Message).where(Message.role == 'user'))
    def forbidden(*a, **k): raise AssertionError('must not call extraction model')
    monkeypatch.setattr('lingxi.worker.complete', forbidden)
    from lingxi.worker import extract
    extract(SimpleNamespace(payload={'message_id': source.id}))
    with SessionLocal() as db: assert not db.scalar(select(Candidate))


def test_final_extraction_fence_rechecks_unconfirmed_marker(configured, monkeypatch):
    from lingxi.worker import claim, extract
    s = continuous(configured)
    result = send(configured, s, automatic=False)
    with SessionLocal.begin() as db:
        db.scalar(select(Job).where(Job.kind == 'chat')).status = 'completed'
    job = claim()
    assert job.kind == 'extract'
    def upstream(*a, **k):
        with SessionLocal.begin() as db:
            run = db.get(ChatRun, result.json()['id'])
            run.config = run.config | {'voice_input': {'automatic': True, 'confirmed': False}}
        return '{"items":[{"content":"我喜欢喝茶。","quote":"我喜欢喝茶。","subject":"self","category":"preferences","evidence":"explicit","sensitive":false}]}'
    monkeypatch.setattr('lingxi.worker.complete', upstream)
    extract(job)
    with SessionLocal() as db:
        assert not db.scalar(select(Candidate)) and not db.scalar(select(Memory))


def test_worker_labels_transcript_uncertainty_and_only_generates_chat(configured, monkeypatch):
    from lingxi.worker import claim, perform
    s = continuous(configured)
    result = send(configured, s)
    observed = []
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *a: [])
    def stream(model, provider, messages, *a, **k):
        observed.extend(messages)
        yield '好的。'
    monkeypatch.setattr('lingxi.worker.stream', stream)
    perform(claim())
    content = [m['content'] for m in observed if m['role'] == 'user']
    assert len(content) == 1 and '未经逐句确认' in content[0] and '我喜欢喝茶。' in content[0]
    with SessionLocal() as db:
        assert db.get(ChatRun, result.json()['id']).status == 'completed'
        assert db.scalar(select(Message).where(Message.role == 'user')).content == '我喜欢喝茶。'
        assert not db.scalar(select(Job).where(Job.kind != 'chat'))


@pytest.mark.parametrize('change', ['closed', 'workspace', 'csrf'])
def test_automatic_send_rechecks_session_and_auth(configured, change):
    s = continuous(configured)
    with SessionLocal.begin() as db:
        row = db.get(VoiceSession, s['id'])
        if change == 'closed': row.status = 'closed'
        if change == 'workspace': row.workspace_id = 'other'
    if change == 'csrf': configured.headers.pop('X-CSRF-Token')
    assert send(configured, s).status_code in (403, 404, 409)
    with SessionLocal() as db: assert not db.scalar(select(ChatRun))
