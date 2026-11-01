"""Cross-entry consent and provenance; synthetic models do not prove emotion quality."""
import json
import pytest
from sqlalchemy import select, func
from lingxi.api import submit_chat
from lingxi.assistants import expression_prompt
from lingxi.db import SessionLocal, now
from lingxi.emotions import EmotionObservation, analyze, source_kind, validate_expression
from lingxi.media import MediaChatInput
from lingxi.models import ChatRun, Conversation, Job, Message, User
from lingxi import gateway, worker
from test_channels import create, connect, bind, event, receive_event, prepare_outbound, rules
from test_emotions import enable, submit, observation, finish


def enable_channels(client, channels):
    return enable(client, observation_channels=channels)


def voice_submit(client, confirmed=True, automatic=False):
    conversation = client.post('/api/conversations', json={'title': '转写线索'}).json()['id']
    with SessionLocal.begin() as db:
        return submit_chat(MediaChatInput(conversation_id=conversation, request_id='confirmed-voice-source',
            content='我现在有些疲惫'), (db, db.get(User, 'owner'), None),
            voice_context={'confirmed': confirmed, 'automatic': automatic})


def qq_submit(client, *, memory=True, group=False, bound=True):
    channel = create(client)
    if group or not bound:
        channel = rules(client, channel, allow_unbound=True, group_enabled=group, group_ids=['12345'] if group else [])
    connection = connect(channel['id'])
    link = bind(client, channel, connection, memory) if bound else None
    receive_event(channel['id'], connection, event('我现在有些疲惫', group='12345' if group else None, mention=group))
    prepare_outbound(channel['id'], connection)
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun).where(ChatRun.request_id.like('onebot:%')))
        assert run is not None
    return channel, link, run


def test_existing_preferences_do_not_grant_new_entry_consent(configured):
    assert configured.get('/api/support-preferences').json()['observation_channels'] == ['web_text']
    enable(configured)
    voice_submit(configured)
    assert observation() is None
    qq_submit(configured)
    assert observation() is None


@pytest.mark.parametrize('channels', [[], ['unknown'], ['web_text', 'web_text']])
def test_invalid_or_empty_enabled_entry_selection_rejected(configured, channels):
    old = configured.get('/api/support-preferences').json()
    result = configured.put('/api/support-preferences', json=old | {'auto_observe': True, 'observation_channels': channels})
    assert result.status_code == 422
    assert configured.get('/api/support-preferences').json() == old


def test_deselected_web_text_is_not_analyzed(configured):
    enable_channels(configured, ['confirmed_voice'])
    submit(configured)
    assert observation() is None


@pytest.mark.parametrize('confirmed,automatic', [(False, True), (False, False), (True, True)])
def test_only_manually_confirmed_voice_is_eligible(configured, confirmed, automatic):
    enable_channels(configured, ['confirmed_voice'])
    voice_submit(configured, confirmed, automatic)
    assert observation() is None


def test_confirmed_voice_uses_text_not_acoustics_and_keeps_source(configured, monkeypatch):
    enable_channels(configured, ['confirmed_voice'])
    run = voice_submit(configured)
    row = finish(monkeypatch)
    assert row.config['source_kind'] == 'confirmed_voice'
    with SessionLocal.begin() as db:
        prompt = expression_prompt(db, db.get(ChatRun, run['id']))
        assert '"source_kind": "confirmed_voice"' in prompt
    view = configured.get('/api/emotion-observations').json()[0]
    assert view['source_kind'] == 'confirmed_voice' and view['evidence'] == 'reported'


def test_bound_private_requires_both_existing_identity_scope_and_selected_entry(configured, monkeypatch):
    enable_channels(configured, ['qq_private'])
    _, _, run = qq_submit(configured)
    row = finish(monkeypatch)
    assert row.status == 'ready' and row.config['source_kind'] == 'qq_private'
    with SessionLocal.begin() as db:
        prompt = expression_prompt(db, db.get(ChatRun, run.id))
        assert '"source_kind": "qq_private"' in prompt
        assert '"label": "fatigue"' in prompt
    assert configured.get('/api/emotion-observations').json()[0]['source_kind'] == 'qq_private'


@pytest.mark.parametrize('boundary', ['group', 'no_memory', 'unbound'])
def test_group_isolated_identity_and_strangers_do_not_inherit_owner_consent(configured, boundary):
    enable_channels(configured, ['web_text', 'confirmed_voice', 'qq_private'])
    _, _, run = qq_submit(configured, group=boundary == 'group', memory=boundary != 'no_memory', bound=boundary != 'unbound')
    assert observation() is None and run.user_id != 'owner'
    with SessionLocal.begin() as db:
        assert '"temporary_observation": null' in expression_prompt(db, db.get(ChatRun, run.id))


@pytest.mark.parametrize('change', ['revoke', 'disable_channel', 'deselect', 'delete_source'])
def test_qq_inflight_analysis_drops_results_after_scope_changes(configured, monkeypatch, change):
    enable_channels(configured, ['qq_private'])
    channel, link, run = qq_submit(configured)
    def hook():
        if change == 'revoke':
            assert configured.post('/api/identity-links/' + link['id'] + '/revoke', json={'version': link['version']}).status_code == 200
        elif change == 'disable_channel':
            assert configured.put('/api/channels/' + channel['id'], json={'version': channel['version'],
                'name': channel['name'], 'bot_id': channel['bot_id'], 'enabled': False}).status_code == 200
        elif change == 'deselect':
            enable_channels(configured, ['web_text'])
        else:
            assert configured.delete('/api/conversations/' + run.conversation_id).status_code == 200
    row = finish(monkeypatch, hook=hook)
    assert row.status == 'invalidated' and not row.quote


def test_revoke_clears_ready_qq_hint_and_prevents_pending_delivery(configured, monkeypatch):
    enable_channels(configured, ['qq_private'])
    channel, link, run = qq_submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        stored = db.get(ChatRun, run.id)
        expression_prompt(db, stored)
        stored.status = 'completed'
        db.add(Message(conversation_id=run.conversation_id, role='assistant', content='依据旧线索的回答', run_id=run.id))
    assert configured.post('/api/identity-links/' + link['id'] + '/revoke', json={'version': link['version']}).status_code == 200
    with SessionLocal() as db:
        assert db.get(EmotionObservation, row.id).status == 'invalidated'
        assert db.get(ChatRun, run.id).status == 'invalidated'
    view = configured.get('/api/emotion-observations').json()[0]
    assert not view['quote'] and view['source_id'] is None


def test_qq_conversation_opened_on_web_does_not_change_source_audience(configured, monkeypatch):
    enable_channels(configured, ['qq_private', 'web_text'])
    _, _, run = qq_submit(configured)
    finish(monkeypatch)
    with SessionLocal.begin() as db:
        db.get(ChatRun, run.id).status = 'completed'
    view = submit(configured, conversation_id=run.conversation_id)
    with SessionLocal.begin() as db:
        current = db.get(ChatRun, view['id'])
        assert source_kind(db, current) is None
        assert '"temporary_observation": null' in expression_prompt(db, current)
        assert db.scalar(select(func.count()).select_from(EmotionObservation)) == 1


def test_all_entries_share_paid_budget_and_hints_do_not_cross_conversations(configured, monkeypatch):
    enable_channels(configured, ['web_text', 'confirmed_voice', 'qq_private'])
    run = voice_submit(configured)
    finish(monkeypatch)
    _, _, qq_run = qq_submit(configured)
    with SessionLocal.begin() as db:
        assert db.scalar(select(func.count()).select_from(EmotionObservation)) == 1
        assert '"temporary_observation": null' in expression_prompt(db, db.get(ChatRun, qq_run.id))


@pytest.mark.parametrize('tamper', ['target', 'sender', 'owner', 'binding', 'workspace', 'session', 'content', 'audio'])
def test_stale_or_forged_qq_provenance_never_becomes_expression_evidence(configured, monkeypatch, tamper):
    from lingxi.channels import ChannelInbound, ChannelSession, IdentityLink
    enable_channels(configured, ['qq_private'])
    _, link, run = qq_submit(configured)
    with SessionLocal.begin() as db:
        current = db.get(ChatRun, run.id)
        scope = dict(current.config['channel'])
        if tamper == 'target': scope['target_key'] = 'group:12345'
        if tamper == 'sender': scope['sender_id'] = '33333333'
        if tamper == 'owner': scope['owner_id'] = 'foreign'
        if tamper == 'audio': scope['audio_unconfirmed'] = True
        current.config = current.config | {'channel': scope}
        if tamper == 'binding': db.get(IdentityLink, link['id']).allow_memory = False
        if tamper == 'workspace': db.get(IdentityLink, link['id']).workspace_id = 'foreign'
        if tamper == 'session': db.scalar(select(ChannelSession)).actor_id = 'foreign'
        if tamper == 'content': db.scalar(select(ChannelInbound)).content = '另一个消息'
    row = finish(monkeypatch)
    assert row.status == 'invalidated' and not row.quote


def test_voice_feedback_and_preference_revoke_invalidate_inflight_expression(configured, monkeypatch):
    enable_channels(configured, ['confirmed_voice'])
    run = voice_submit(configured)
    row = finish(monkeypatch)
    with SessionLocal.begin() as db:
        expression_prompt(db, db.get(ChatRun, run['id']))
    response = configured.post('/api/emotion-observations/' + row.id + '/feedback',
        json={'version': row.version, 'action': 'correct', 'label': 'calm'})
    assert response.status_code == 200 and response.json()['evidence'] == 'user_correction'
    assert response.json()['expires_at'] == row.expires_at
    enable_channels(configured, ['web_text'])
    with SessionLocal() as db:
        with pytest.raises(worker.Cancelled):
            validate_expression(db, db.get(ChatRun, run['id']))


def test_adding_entry_requires_valid_extraction_but_removing_it_does_not(configured):
    from lingxi.models import Model
    enable_channels(configured, ['web_text'])
    with SessionLocal.begin() as db:
        db.get(Model, 'extraction').status = 'unverified'
    old = configured.get('/api/support-preferences').json()
    assert configured.put('/api/support-preferences', json=old | {'observation_channels': ['web_text', 'qq_private']}).status_code == 409
    assert configured.put('/api/support-preferences', json=old | {'auto_observe': False, 'observation_channels': []}).status_code == 200


def test_pg_revoke_serializes_with_final_analysis_write(configured, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from lingxi.api import app
    from lingxi import channels
    from lingxi.emotions import owner_lock
    with SessionLocal() as db:
        if db.bind.dialect.name != 'postgresql':
            pytest.skip('PostgreSQL row-lock concurrency')
    enable_channels(configured, ['qq_private'])
    _, link, _ = qq_submit(configured)
    row = observation()
    entered = threading.Event()
    original = channels.invalidate_channel_runs
    def invalidating(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(channels, 'invalidate_channel_runs', invalidating)
    # Complete lifespan seeding before taking the analyzer lock; startup itself
    # legitimately locks the owner and is not the race under test.
    with TestClient(app) as revoker, ThreadPoolExecutor(max_workers=1) as executor:
        revoker.cookies.update(configured.cookies)
        revoker.headers['X-CSRF-Token'] = configured.headers['X-CSRF-Token']
        def revoke():
            return revoker.post('/api/identity-links/' + link['id'] + '/revoke', json={'version': link['version']})
        with SessionLocal.begin() as db:
            owner_lock(db, 'owner')
            future = executor.submit(revoke)
            assert entered.wait(10)
            assert not future.done()
            # The final analysis transaction was already authorized before the
            # concurrent revocation; it must be cleared once that commit wins.
            current = db.get(EmotionObservation, row.id)
            current.status, current.label, current.quote, current.evidence = 'ready', 'fatigue', '我现在有些疲惫', 'reported'
        response = future.result(timeout=15)
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        stored = db.get(EmotionObservation, row.id)
        assert stored.status == 'invalidated' and not stored.quote and not stored.label
