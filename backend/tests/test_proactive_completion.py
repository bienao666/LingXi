"""Explicit completion keeps history, stops contact and survives correction/restore."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, delete

from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Memory, User, ModelCall, Message, ChatRun
from lingxi import proactive as pro, proactive_completion as done
from test_proactive import memory, topic, prefs, contacts, ready


def resolve(c, row):
    r = c.post('/api/conversation-topics/' + row['id'] + '/resolve', json={'version': row['version']})
    assert r.status_code == 200, r.text
    return r.json()


def test_pending_completion_keeps_memory_preferences_and_other_topics(client):
    first, second = topic(client), topic(client)
    before = client.get('/api/contact-preferences').json()
    assert resolve(client, first)['status'] == 'resolved'
    assert client.get('/api/contact-preferences').json() == before
    assert client.get('/api/memories/' + first['source_id']).status_code == 200
    pro.deliver(first['id'])
    assert contacts(client) == []
    rows = client.get('/api/conversation-topics?status=resolved').json()['items']
    assert [r['id'] for r in rows] == [first['id']]
    with SessionLocal() as db:
        assert db.get(pro.ConversationTopic, second['id']).status == 'pending'
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_completion_requires_auth_csrf_version_and_scope(client):
    row = topic(client)
    path = '/api/conversation-topics/' + row['id'] + '/resolve'
    with TestClient(app) as anon: assert anon.post(path, json={'version': 1}).status_code == 401
    token = client.headers.pop('X-CSRF-Token')
    assert client.post(path, json={'version': 1}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    assert client.post(path, json={'version': 2}).status_code == 409
    assert client.post(path, json={'version': True}).status_code == 422
    with SessionLocal.begin() as db: db.get(pro.ConversationTopic, row['id']).workspace_id = 'foreign'
    assert client.post(path, json={'version': 1}).status_code == 404


def test_resolved_contact_keeps_history_and_is_not_an_unreviewed_notification(client):
    contact = ready(client)
    before = client.get('/api/contact-preferences').json()
    r = client.post('/api/proactive-contacts/' + contact['id'] + '/feedback',
        json={'version': contact['version'], 'feedback': 'resolved'})
    assert r.status_code == 200, r.text
    assert r.json()['topic_status'] == 'resolved' and r.json()['feedback'] == 'resolved'
    assert r.json()['content'] == contact['content']
    assert client.get('/api/proactive-contacts?unreviewed=true').json()['items'] == []
    assert client.get('/api/contact-preferences').json() == before
    assert client.post('/api/proactive-contacts/' + contact['id'] + '/continue').status_code == 200
    assert client.post('/api/proactive-contacts/' + contact['id'] + '/feedback',
        json={'version': contact['version'], 'feedback': 'resolved'}).status_code == 409


def test_correction_does_not_automatically_recreate_finished_subject_but_user_can_arrange_new_version(client):
    source = memory(client, category='projects')
    first = client.get('/api/conversation-topics').json()['items'][0]
    resolve(client, first)
    r = client.patch('/api/memories/' + source['id'], json={'version': 1, 'content': '项目已经有新的阶段'})
    assert r.status_code == 200, r.text
    assert len(client.get('/api/conversation-topics').json()['items']) == 1
    current = topic(client, source)
    assert current['source_version'] == 2 and current['origin'] == 'user'
    with SessionLocal() as db: assert db.scalar(select(func.count()).select_from(done.TopicCompletion)) == 1


@pytest.mark.parametrize('terminal', ['resolved', 'closed', 'invalidated'])
def test_terminal_or_invalid_source_cannot_be_resolved_again(client, terminal):
    row = topic(client)
    with SessionLocal.begin() as db: db.get(pro.ConversationTopic, row['id']).status = terminal
    assert client.post('/api/conversation-topics/' + row['id'] + '/resolve', json={'version': 1}).status_code == 409


@pytest.mark.parametrize('text', ['这个话题结束了', '这件事已经结束了。', '这件事已完成'])
def test_exact_chat_completion_needs_one_current_conversation_topic_and_no_model(client, text):
    contact = ready(client)
    conversation = client.post('/api/proactive-contacts/' + contact['id'] + '/continue').json()['conversation_id']
    r = client.post('/api/chat', json={'conversation_id': conversation, 'content': text, 'request_id': 'explicit-completion-control'})
    assert r.status_code == 200 and r.json()['status'] == 'completed', r.text
    assert contacts(client)[0]['topic_status'] == 'resolved'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert db.get(ChatRun, r.json()['id']).model_id == 'contact-control'
    assert client.post('/api/chat', json={'conversation_id': conversation, 'content': text,
        'request_id': 'explicit-completion-control'}).json()['id'] == r.json()['id']


@pytest.mark.parametrize('text', ['演示顺利完成了', '如果我说这个话题结束了', '“这个话题结束了”', '朋友说这件事已完成'])
def test_results_quotes_and_hypotheses_are_not_completion_commands(client, text):
    contact = ready(client)
    with SessionLocal.begin() as db:
        assert pro.chat_control(db, db.get(User, 'owner'), type('C', (), {'id': 'unknown'})(), text) is None
    assert contacts(client)[0]['topic_status'] == 'contacted'


def test_ambiguous_or_unrelated_conversation_does_not_pick_a_topic(client):
    first = ready(client)
    conv = client.post('/api/proactive-contacts/' + first['id'] + '/continue').json()['conversation_id']
    second = topic(client, conversation_id=conv)
    prefs(client, min_gap_hours=1)
    with SessionLocal.begin() as db:
        db.get(pro.ProactiveContact, first['id']).created_at = now() - 4000
        from lingxi.proactive_channels import ProactiveDelivery
        db.scalar(select(ProactiveDelivery)).sent_at = now() - 4000
    pro.deliver(second['id'])
    assert len(contacts(client)) == 2
    with SessionLocal.begin() as db:
        reply = pro.chat_control(db, db.get(User, 'owner'), type('C', (), {'id': conv})(), '这个话题结束了')
        assert '唯一' in reply
    assert all(c['topic_status'] == 'contacted' for c in contacts(client))


@pytest.mark.parametrize('sent', [False, True])
def test_qq_queued_cancelled_and_dispatched_kept_unknown_without_resend(client, sent):
    from test_proactive_channels import queued
    from lingxi.channel_gateway import prepare_outbound
    channel, connection, contact = queued(client)
    if sent:
        assert prepare_outbound(channel['id'], connection)
        contact = contacts(client)[0]
    r = client.post('/api/proactive-contacts/' + contact['id'] + '/feedback',
        json={'version': contact['version'], 'feedback': 'resolved'})
    assert r.status_code == 200, r.text
    assert r.json()['delivery']['status'] == ('unknown' if sent else 'cancelled')
    assert r.json()['content'] == contact['content'] and r.json()['topic_status'] == 'resolved'
    assert prepare_outbound(channel['id'], connection) is None


def test_browser_notification_and_active_voice_candidate_stop(client, configured):
    from test_browser_notifications import consent, claim
    consent(client)
    contact = ready(client)
    row = client.get('/api/conversation-topics').json()['items'][0]
    resolve(client, row)
    assert claim(client) is None
    assert not client.get('/api/browser-notifications/state').json()['receipts'][0]['eligible']


def test_active_voice_stops_without_fabricating_played_parts(configured):
    from test_proactive_voice import setup, claim
    from lingxi.voice import VoicePlayback
    session, _ = setup(configured)
    claim(configured, session)
    contact = contacts(configured)[0]
    r = configured.post('/api/proactive-contacts/' + contact['id'] + '/feedback',
        json={'version': contact['version'], 'feedback': 'resolved'})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        playback = db.scalar(select(VoicePlayback))
        assert playback.status == 'stopped' and playback.confirmed_parts == 0


def test_completing_plugin_event_preserves_other_events_from_same_subscription(client):
    from test_plugin_events import setup, signed
    _, subscription = setup(client)
    assert signed(client, subscription).status_code == 200
    first = client.get('/api/conversation-topics').json()['items'][0]
    assert signed(client, subscription, external='independent-event-002').status_code == 200
    second = client.get('/api/conversation-topics').json()['items'][0]
    assert first['source_id'] == second['source_id'] and first['source_version'] != second['source_version']
    resolve(client, first)
    with SessionLocal() as db:
        assert db.get(pro.ConversationTopic, second['id']).status == 'pending'

        saved = done.checkpoint(db)
    with SessionLocal.begin() as db:
        db.get(pro.ConversationTopic, first['id']).status = 'pending'
        done.restore(db, saved)
    with SessionLocal() as db:
        assert db.get(pro.ConversationTopic, first['id']).status == 'resolved'
        assert db.get(pro.ConversationTopic, second['id']).status == 'pending'


def test_completion_during_model_draft_rejects_late_text(configured, monkeypatch):
    from test_proactive_generation import setup, run, draft
    _, row = setup(configured)
    pro.deliver(row['id'])
    current = configured.get('/api/conversation-topics').json()['items'][0]
    calls = run(monkeypatch, hook=lambda: resolve(configured, current))
    assert len(calls) == 1
    assert draft(row).status == 'invalidated' and not draft(row).content
    pro.deliver(row['id'])
    assert contacts(configured) == []


def test_postgres_concurrent_completion_commits_one_barrier(client):
    from concurrent.futures import ThreadPoolExecutor
    from lingxi.db import engine
    if engine.dialect.name != 'postgresql': pytest.skip('PostgreSQL row locking')
    row = topic(client)
    def finish(_):
        return client.post('/api/conversation-topics/' + row['id'] + '/resolve', json={'version': row['version']}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: codes = list(pool.map(finish, [1, 2]))
    assert sorted(codes) == [200, 409]
    with SessionLocal() as db: assert db.scalar(select(func.count()).select_from(done.TopicCompletion)) == 1


def test_restore_latest_completion_over_old_pending_topic_does_not_revive(client):
    row = topic(client)
    resolve(client, row)
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    with SessionLocal.begin() as db: schema(db)
    data = checkpoint()
    assert len(data['topic_completions']) == 1
    with SessionLocal.begin() as db:
        db.execute(delete(done.TopicCompletion))
        db.get(pro.ConversationTopic, row['id']).status = 'pending'
    quarantine(data)
    with SessionLocal() as db:
        # Restore privacy quarantine invalidates source-bearing history as well.
        assert db.get(pro.ConversationTopic, row['id']).status == 'invalidated'
        assert done.completed(db, db.get(User, 'owner'), 'memory', row['source_id'])


@pytest.mark.parametrize('change', ['workspace', 'time', 'id', 'extra'])
def test_completion_checkpoint_rejects_malformed_or_foreign_metadata_before_restore(client, change):
    resolve(client, topic(client))
    with SessionLocal() as db:
        rows = done.checkpoint(db)
        owner = {'id': 'owner', 'workspace_id': 'personal'}
    altered = deepcopy(rows)
    if change == 'workspace': altered[0]['workspace_id'] = 'foreign'
    if change == 'time': altered[0]['completed_at'] = float('nan')
    if change == 'id': altered[0]['id'] = 'forged'
    if change == 'extra': altered[0]['content'] = 'not allowed'
    with pytest.raises(ValueError): done.validate_checkpoint(altered, owner)
    done.validate_checkpoint(rows, owner)
