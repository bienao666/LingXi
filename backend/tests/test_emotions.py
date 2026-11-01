"""Evidence, consent, scope, cost and invalidation boundaries (synthetic providers only)."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Conversation, Job, Memory, Message, Provider, User
from lingxi.assistants import SupportPreference, expression_prompt, support_view
from lingxi.emotions import EmotionObservation, analyze, sweep, validate_expression
from lingxi import worker, gateway


def enable(client, **extra):
    old = client.get('/api/support-preferences').json()
    response = client.put('/api/support-preferences', json=old | {'auto_observe': True} | extra)
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, content='我现在有些疲惫，先陪我聊聊。', conversation_id=None, **extra):
    if not conversation_id:
        conversation_id = client.post('/api/conversations', json={'title': '短期线索'}).json()['id']
    response = client.post('/api/chat', json={'conversation_id': conversation_id,
        'content': content, 'request_id': 'emotion-test-' + __import__('uuid').uuid4().hex} | extra)
    assert response.status_code == 200, response.text
    return response.json()


def observation():
    with SessionLocal() as db:
        return db.scalar(select(EmotionObservation))


def job_for(row):
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == 'emotion'))
        job.status, job.attempts, job.lease_token, job.lease_until = 'running', 1, 'test-token', now() + 600
        return job


def finish(monkeypatch, row=None, result=None, hook=None):
    row = row or observation()
    def complete(*args, **kwargs):
        assert kwargs['purpose'] == 'emotion'
        assert kwargs['max_tokens'] == 400
        if hook:
            hook()
        return json.dumps(result if result is not None else {'label': 'fatigue', 'evidence': 'reported', 'quote': '我现在有些疲惫'})
    monkeypatch.setattr(gateway, 'complete', complete)
    analyze(job_for(row))
    return observation()


def test_default_off_no_cost_and_enable_requires_verified_extraction(configured, client):
    assert not client.get('/api/support-preferences').json()['auto_observe']
    submit(configured)
    assert observation() is None
    with SessionLocal.begin() as db:
        from lingxi.models import Model
        db.get(Model, 'extraction').status = 'unverified'
    assert client.put('/api/support-preferences', json={'auto_observe': True}).status_code == 409
    assert client.put('/api/support-preferences', json={'auto_observe': True, 'emotion_enabled': False}).status_code == 422


def test_new_message_only_idempotency_cost_bound_and_source_contract(configured):
    enable(configured)
    request = {'request_id': 'stable-emotion-request'}
    first = submit(configured, **request)
    row = observation()
    assert row and row.status == 'queued' and row.preference_version == 1
    assert not row.quote and not row.label
    repeated = submit(configured, conversation_id=first['conversation_id'], **request)
    assert repeated['id'] == first['id']
    submit(configured)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(EmotionObservation)) == 1
        assert db.get(ChatRun, first['id']).config['emotion_source'] == row.id


@pytest.mark.parametrize('text', [
    '你好', '朋友今天很疲惫', '我朋友说他很累', '如果我很累怎么办', '假设我很开心',
    '请扮演我很累的角色', '他说“我很开心”', '翻译：我很难过', '昨天我很难过',
    '我曾经感到压力', '我有焦虑症', '我不想活了', '我很累' + '。' * 4000,
])
def test_ambiguous_historical_third_party_and_no_signal_do_not_call(configured, text):
    enable(configured)
    submit(configured, text)
    assert observation() is None


@pytest.mark.parametrize('flag,value', [('channel', {'memory_allowed': True}), ('voice_input', {'automatic': True}),
    ('voice_input', {'automatic': False}), ('task_assist', {'mode': 'collaboration'}), ('media_refs', [{'id': 'file'}])])
def test_non_web_text_modes_never_analyze_or_receive_hints(configured, monkeypatch, flag, value):
    enable(configured)
    run = submit(configured)
    with SessionLocal.begin() as db:
        stored = db.get(ChatRun, run['id'])
        stored.config = stored.config | {flag: value}
    finish(monkeypatch)
    assert observation().status == 'invalidated'
    with SessionLocal.begin() as db:
        assert '"temporary_observation": null' in expression_prompt(db, db.get(ChatRun, run['id']))


def test_reported_and_inferred_are_not_memories_or_cross_conversation_profile(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch, result={'label': 'stress', 'evidence': 'inferred', 'quote': '我现在有些疲惫'})
    assert row.status == 'ready'
    other = submit(configured, '帮我比较两个方案。')
    with SessionLocal.begin() as db:
        same = expression_prompt(db, db.get(ChatRun, run['id']))
        assert '"uncertain": true' in same and '"evidence": "inferred"' in same
        assert '"temporary_observation": null' in expression_prompt(db, db.get(ChatRun, other['id']))
        assert not db.scalars(select(Memory)).all()
        assert db.get(ChatRun, other['id']).config.get('emotion_ref') is None


@pytest.mark.parametrize('result', [
    {'label': 'diagnosis', 'evidence': 'reported', 'quote': '我现在有些疲惫'},
    {'label': ['fatigue'], 'evidence': 'reported', 'quote': '我现在有些疲惫'},
    {'label': 'fatigue', 'evidence': 'certain', 'quote': '我现在有些疲惫'},
    {'label': 'fatigue', 'evidence': 'reported', 'quote': '我患有疾病'},
    {'label': 'fatigue', 'evidence': 'reported', 'quote': '有些疲惫'},
])
def test_unverifiable_model_output_is_not_used(configured, monkeypatch, result):
    enable(configured)
    submit(configured)
    row = finish(monkeypatch, result=result)
    assert row.status == 'failed' and not row.quote


def test_unknown_emotion_does_not_become_a_claim(configured, monkeypatch):
    enable(configured)
    submit(configured)
    row = finish(monkeypatch, result={'label': None})
    assert row.status == 'none' and not row.quote and not row.label


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_partial_failed_and_cancelled_answers_are_also_erased_on_withdrawal(configured, monkeypatch, status):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        stored = db.get(ChatRun, run['id'])
        expression_prompt(db, stored)
        stored.status = status
        db.add(Message(conversation_id=run['conversation_id'], role='assistant', content='包含旧线索的半段回答', run_id=stored.id, status=status))
    response = configured.post(f'/api/emotion-observations/{row.id}/feedback', json={'version': 1, 'action': 'reject'})
    assert response.status_code == 200
    with SessionLocal() as db:
        assert db.get(ChatRun, run['id']).status == 'invalidated'
        assert not db.scalar(select(Message).where(Message.role == 'assistant')).content


def test_self_report_priority_and_ordinary_reply_integration(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    monkeypatch.setattr(worker.MemoryService, 'search', lambda *args: [])
    seen = []
    def stream(model, provider, messages, run_id, check, **kwargs):
        seen.extend(messages)
        check()
        yield '我在听。'
    monkeypatch.setattr(worker, 'stream', stream)
    with SessionLocal.begin() as db:
        job = db.scalar(select(Job).where(Job.kind == 'chat'))
        job.status, job.lease_token, job.lease_until = 'running', 'test-chat', now() + 600
    worker.generate(job)
    assert any('"label": "fatigue"' in m['content'] for m in seen)
    with SessionLocal() as db:
        assert db.get(ChatRun, run['id']).status == 'completed'
    enable(configured, self_report='我现在已经轻松了。')
    with SessionLocal.begin() as db:
        current = db.get(ChatRun, run['id'])
        assert current.status == 'invalidated'
        prompt = expression_prompt(db, current)
        assert '我现在已经轻松了' in prompt and '"temporary_observation": null' in prompt
        assert db.get(EmotionObservation, row.id).status == 'invalidated'


@pytest.mark.parametrize('change', ['disable', 'delete_source', 'provider', 'expire'])
def test_inflight_fence_drops_late_results(configured, monkeypatch, change):
    enable(configured)
    run = submit(configured)
    row = observation()
    def hook():
        if change == 'disable':
            enable(configured, auto_observe=False)
        elif change == 'delete_source':
            assert configured.delete('/api/conversations/' + run['conversation_id']).status_code == 200
        else:
            with SessionLocal.begin() as db:
                if change == 'provider':
                    db.get(Provider, 'provider').version += 1
                else:
                    db.get(EmotionObservation, row.id).expires_at = now() - 1
    result = finish(monkeypatch, hook=hook)
    assert result.status in ('invalidated', 'expired') and not result.quote


def test_feedback_conflict_rejection_correction_ledger_and_cleanup(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        generated = db.get(ChatRun, run['id'])
        expression_prompt(db, generated)
        generated.status = 'completed'
        db.add(Message(conversation_id=run['conversation_id'], role='assistant', content='依据线索的回答', run_id=generated.id))
    endpoint = '/api/emotion-observations/' + row.id
    assert configured.post(endpoint + '/feedback', json={'version': 9, 'action': 'reject'}).status_code == 409
    assert configured.post(endpoint + '/feedback', json={'version': 1, 'action': 'correct'}).status_code == 422
    corrected = configured.post(endpoint + '/feedback', json={'version': 1, 'action': 'correct', 'label': 'calm'}).json()
    assert corrected['label'] == 'calm' and corrected['evidence'] == 'user_correction'
    assert corrected['expires_at'] == row.expires_at
    with SessionLocal() as db:
        assert db.get(ChatRun, run['id']).status == 'invalidated'
        assert not db.scalar(select(Message).where(Message.role == 'assistant')).content
    # Replaying the old correction barrier must not erase the newer correction.
    from lingxi.memory import replay_ledger
    with SessionLocal.begin() as db:
        replay_ledger(db)
        assert db.get(EmotionObservation, row.id).label == 'calm'
    rejected = configured.post(endpoint + '/feedback', json={'version': 2, 'action': 'reject'}).json()
    assert rejected['status'] == 'rejected' and not rejected['quote']
    with SessionLocal.begin() as db:
        stored = db.get(EmotionObservation, row.id)
        stored.version, stored.status, stored.quote, stored.label = 1, 'ready', '我现在有些疲惫', 'fatigue'
        replay_ledger(db)
        assert stored.status == 'invalidated' and not stored.quote


def test_expiry_and_delete_stop_inflight_expression_and_clear_evidence(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        expression_prompt(db, db.get(ChatRun, run['id']))
        db.get(EmotionObservation, row.id).expires_at = now() - 1
    with SessionLocal() as db:
        with pytest.raises(worker.Cancelled):
            validate_expression(db, db.get(ChatRun, run['id']))
    sweep()
    view = configured.get('/api/emotion-observations').json()[0]
    assert view['status'] == 'expired' and view['quote'] == ''
    assert configured.delete(f'/api/emotion-observations/{row.id}?version={view["version"]}').json()['status'] == 'deleted'


def test_unavailable_analysis_is_non_replayed_and_does_not_fail_chat(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    job = job_for(observation())
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('synthetic-timeout')
    monkeypatch.setattr(gateway, 'complete', fail)
    worker.perform(job)
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == 'failed'
        assert db.get(ChatRun, run['id']).status == 'queued'
        assert db.scalar(select(EmotionObservation)).status == 'failed'


def test_interrupted_analysis_never_replays_paid_work(configured, monkeypatch):
    enable(configured)
    submit(configured)
    row = observation()
    job = job_for(row)
    with SessionLocal.begin() as db:
        db.get(EmotionObservation, row.id).status = 'running'
    monkeypatch.setattr(gateway, 'complete', lambda *a, **kw: pytest.fail('must not replay'))
    analyze(job)
    assert observation().status == 'failed'


def test_new_negation_retires_old_hint_even_within_call_budget(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        stored = db.get(ChatRun, run['id'])
        stored.status = 'completed'
        expression_prompt(db, stored)
    submit(configured, '我现在不累，已经好了。', conversation_id=run['conversation_id'])
    with SessionLocal() as db:
        assert db.get(EmotionObservation, row.id).status == 'invalidated'
        assert db.get(ChatRun, run['id']).status == 'invalidated'
        assert db.scalar(select(func.count()).select_from(EmotionObservation)) == 1


def test_reported_label_without_matching_emotion_word_is_marked_inference(configured, monkeypatch):
    enable(configured)
    submit(configured)
    row = finish(monkeypatch, result={'label': 'anger', 'evidence': 'reported', 'quote': '我现在有些疲惫'})
    assert row.evidence == 'inferred'


def test_auth_scope_source_erasure_and_preference_restore(configured, monkeypatch):
    enable(configured)
    run = submit(configured)
    row = finish(monkeypatch)
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/emotion-observations').status_code == 401
        assert anonymous.delete('/api/emotion-observations/x?version=1').status_code == 401
    with SessionLocal.begin() as db:
        stored = db.get(EmotionObservation, row.id)
        stored.user_id = 'other-user'
    assert configured.get('/api/emotion-observations').json() == []
    assert configured.post(f'/api/emotion-observations/{row.id}/feedback', json={'version': 1, 'action': 'reject'}).status_code == 404
    with SessionLocal.begin() as db:
        stored = db.get(EmotionObservation, row.id)
        stored.user_id = 'owner'
    enable(configured, auto_observe=False, self_report='')
    from lingxi.memory import replay_ledger
    with SessionLocal.begin() as db:
        pref = db.get(SupportPreference, 'owner')
        pref.version, pref.spec = 1, {'auto_observe': True, 'emotion_enabled': True, 'self_report': '旧感受'}
        replay_ledger(db)
        assert not pref.spec['auto_observe'] and not pref.spec['self_report']
    configured.headers.pop('X-CSRF-Token')
    assert configured.delete(f'/api/emotion-observations/{row.id}?version=1').status_code == 403
