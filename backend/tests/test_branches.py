"""Fork history, idempotency, tool non-replay and evidence invalidation."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi import worker, gateway
from lingxi.models import ChatRun, Conversation, Job, Message, User
from lingxi.branches import ConversationBranch, history
from lingxi.memory import MemoryService, replay_ledger


def process(run_id):
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job and job.payload['run_id'] == run_id
    worker.perform(job)


def setup(client, monkeypatch):
    captured = []
    def stream(model, provider, messages, *args, **kwargs):
        captured.append(messages)
        return iter(['回答：' + str(messages[-1]['content'])])
    monkeypatch.setattr(worker, 'stream', stream)
    monkeypatch.setattr(MemoryService, 'search', lambda *a: [])
    c = client.post('/api/conversations', json={'title': '原始对话'}).json()['id']
    for i, content in enumerate(('我喜欢茶', '只在原分支的问题')):
        r = client.post('/api/chat', json={'conversation_id': c, 'request_id': f'original-request-{i}', 'content': content})
        assert r.status_code == 200, r.text
        process(r.json()['id'])
    return c, client.get('/api/messages', params={'conversation_id': c}).json(), captured


def fork(client, c, message, mode='regenerate', **kw):
    return client.post(f'/api/conversations/{c}/branches', json={
        'request_id': 'branch-request-one', 'message_id': message['id'], 'mode': mode} | kw)


def messages(client, c):
    return client.get('/api/messages', params={'conversation_id': c}).json()


def test_edit_forks_without_overwriting_or_inheriting_future(configured, monkeypatch):
    c, original, captured = setup(configured, monkeypatch)
    result = fork(configured, c, original[2], 'edit', content='修改后的提问')
    assert result.status_code == 200, result.text
    branch = result.json()
    assert messages(configured, c) == original
    before = messages(configured, branch['conversation_id'])
    assert [m['id'] for m in before[:2]] == [m['id'] for m in original[:2]]
    assert before[-1]['content'] == '修改后的提问'
    process(branch['run_id'])
    assert [m['content'] for m in captured[-1][2:]] == [original[0]['content'], original[1]['content'], '修改后的提问']
    assert '只在原分支' not in json.dumps(captured[-1], ensure_ascii=False)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == branch['conversation_id'])) == 2


def test_regenerate_references_original_user_without_reextracting(configured, monkeypatch):
    c, original, captured = setup(configured, monkeypatch)
    result = fork(configured, c, original[1]).json()
    process(result['run_id'])
    visible = messages(configured, result['conversation_id'])
    assert visible[0]['id'] == original[0]['id'] and len(visible) == 2
    assert messages(configured, c) == original
    with SessionLocal() as db:
        run = db.get(ChatRun, result['run_id'])
        assert run.status == 'completed'
        assert run.config['input_message_id'] == original[0]['id']
        assert 'task_assist' not in run.config
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'extract')) == 2
        assert db.scalar(select(func.count()).select_from(Message).where(Message.run_id == run.id, Message.role == 'user')) == 0


def test_retry_and_changed_payload_are_fenced(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    first = fork(configured, c, original[1])
    assert fork(configured, c, original[1]).json() == first.json()
    assert fork(configured, c, original[3]).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ConversationBranch)) == 1


def test_nested_branch_flattens_and_continues(configured, monkeypatch):
    c, original, captured = setup(configured, monkeypatch)
    first = fork(configured, c, original[1]).json()
    process(first['run_id'])
    edited = fork(configured, first['conversation_id'], original[0], 'edit', content='分支再编辑', request_id='branch-request-two').json()
    process(edited['run_id'])
    assert len(messages(configured, edited['conversation_id'])) == 2
    assert captured[-1][-1]['content'] == '分支再编辑'
    r = configured.post('/api/chat', json={'conversation_id': first['conversation_id'], 'request_id': 'branch-follow-up', 'content': '继续讨论'})
    process(r.json()['id'])
    assert len(messages(configured, first['conversation_id'])) == 4
    assert '只在原分支' not in json.dumps(captured[-1], ensure_ascii=False)


def test_delete_source_invalidates_completed_and_queued_forks(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    done = fork(configured, c, original[1]).json()
    process(done['run_id'])
    queued = fork(configured, c, original[3], request_id='branch-request-two').json()
    assert configured.delete('/api/conversations/' + c).status_code == 200
    for branch in (done, queued):
        assert configured.get('/api/chat-runs/' + branch['run_id']).json()['status'] == 'invalidated'
        assert all(m['content'] == '' for m in messages(configured, branch['conversation_id']))
        relation = configured.get('/api/conversations/' + branch['conversation_id'] + '/branches').json()
        assert relation['origin'] is None and relation['is_branch']
    with SessionLocal.begin() as db:
        assert replay_ledger(db) == 0
    process(queued['run_id'])
    assert configured.get('/api/chat-runs/' + queued['run_id']).json()['status'] == 'invalidated'


def test_delete_branch_keeps_parent_and_shared_evidence(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    result = fork(configured, c, original[1]).json()
    assert configured.delete('/api/conversations/' + result['conversation_id']).status_code == 200
    assert messages(configured, c) == original
    assert fork(configured, c, original[1]).status_code == 404


@pytest.mark.parametrize('mode,index,content,status', [
    ('edit', 1, '改写', 422), ('edit', 0, '   ', 422),
    ('regenerate', 0, None, 422), ('regenerate', 1, '多余内容', 422)])
def test_invalid_inputs(configured, monkeypatch, mode, index, content, status):
    c, original, _ = setup(configured, monkeypatch)
    extra = {'content': content} if content is not None else {}
    assert fork(configured, c, original[index], mode, **extra).status_code == status


def test_active_generation_rejected(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    configured.post('/api/chat', json={'conversation_id': c, 'request_id': 'pending-original', 'content': '尚在生成'})
    assert fork(configured, c, original[1]).status_code == 409


def test_cross_owner_and_workspace_and_login_are_checked(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    with TestClient(app) as anonymous:
        assert fork(anonymous, c, original[1]).status_code == 401
    with SessionLocal.begin() as db:
        other = Conversation(user_id='another-owner', title='不能访问')
        db.add(other); db.flush(); other_id = other.id
    assert fork(configured, other_id, original[1]).status_code == 404
    with SessionLocal.begin() as db:
        db.get(Conversation, c).workspace_id = 'other-space'
    assert fork(configured, c, original[1]).status_code == 404


def test_foreign_anchor_and_invalidated_anchor_rejected(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    other = configured.post('/api/conversations', json={'title': '另一个会话'}).json()['id']
    assert fork(configured, other, original[1]).status_code == 409
    with SessionLocal.begin() as db:
        db.get(Message, original[1]['id']).status = 'invalidated'
    assert fork(configured, c, original[1]).status_code == 409


def test_memory_delete_fences_shared_prefix_and_inflight(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    branch = fork(configured, c, original[3]).json()
    with SessionLocal.begin() as db:
        memory = MemoryService().remember(db, 'owner', '我喜欢茶', 'preferences', [original[0]['id']])
        MemoryService().forget(db, memory)
    process(branch['run_id'])
    assert configured.get('/api/chat-runs/' + branch['run_id']).json()['status'] == 'invalidated'
    assert '我喜欢茶' not in json.dumps(messages(configured, branch['conversation_id']), ensure_ascii=False)


def test_source_deleted_midstream_cannot_publish_late_reply(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    branch = fork(configured, c, original[1]).json()
    def stream(*args, **kw):
        yield '首段'
        assert configured.delete('/api/conversations/' + c).status_code == 200
        yield '迟到的原文内容'
    monkeypatch.setattr(worker, 'stream', stream)
    process(branch['run_id'])
    assert configured.get('/api/chat-runs/' + branch['run_id']).json()['status'] == 'invalidated'
    assert all(not m['content'] for m in messages(configured, branch['conversation_id']))


def test_export_is_branch_scoped_and_relations_are_live(configured, monkeypatch):
    c, original, _ = setup(configured, monkeypatch)
    branch = fork(configured, c, original[1]).json()
    process(branch['run_id'])
    data = configured.get(f"/api/conversations/{branch['conversation_id']}/export?format=json").json()
    assert [m['id'] for m in data['messages']][:1] == [original[0]['id']]
    assert '只在原分支' not in json.dumps(data, ensure_ascii=False)
    relation = configured.get('/api/conversations/' + c + '/branches').json()
    assert relation['children'][0]['id'] == branch['conversation_id']
    found = configured.get('/api/conversations', params={'q': '我喜欢茶'}).json()
    assert {c, branch['conversation_id']}.issubset({item['id'] for item in found})


def test_branch_attachment_reuse_and_deletion_propagation(configured, monkeypatch):
    from lingxi.media import Media, MediaLink, forget_media
    c, original, captured = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        media = Media(user_id='owner', request_id='branch-media-fixture', filename='分支资料.md',
            suffix='.md', size=10, digest='fixture', status='ready',
            parsed={'kind': 'document', 'notes': [], 'parts': [{'location': '第 1 行', 'text': '附件原文'}]})
        db.add(media); db.flush(); media_id = media.id
        db.add(MediaLink(media_id=media.id, message_id=original[0]['id']))
    edited = fork(configured, c, original[0], 'edit', content='重新解释附件').json()
    process(edited['run_id'])
    assert messages(configured, edited['conversation_id'])[0]['attachments'][0]['id'] == media_id
    assert '附件原文' in str(captured[-1][-1]['content'])
    with SessionLocal.begin() as db:
        forget_media(db, db.get(Media, media_id))
    assert configured.get('/api/chat-runs/' + edited['run_id']).json()['status'] == 'invalidated'


@pytest.mark.parametrize('kind', ['qq', 'voice'])
def test_channel_and_active_voice_rejected(configured, monkeypatch, kind):
    from lingxi.channels import ChannelSession
    from lingxi.voice import VoiceSession
    from lingxi.models import LoginSession
    from lingxi.db import now
    c, original, _ = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        if kind == 'qq':
            db.add(ChannelSession(user_id='owner', channel_id='fixture-channel', scope_key='private:123', actor_id='owner',
                conversation_id=c))
        else:
            login = db.scalar(select(LoginSession))
            db.add(VoiceSession(user_id='owner', login_session_id=login.id, conversation_id=c,
                config_version=1, expires_at=now()+300))
    assert fork(configured, c, original[1]).status_code == 409


def test_edit_does_not_replay_contact_controls(configured, monkeypatch):
    from lingxi.proactive import ContactPreference
    c, original, _ = setup(configured, monkeypatch)
    result = fork(configured, c, original[0], 'edit', content='先别聊')
    assert result.status_code == 200, result.text
    with SessionLocal() as db:
        run = db.get(ChatRun, result.json()['run_id'])
        assert run.model_id == 'chat' and run.status == 'queued'
        pref = db.get(ContactPreference, 'owner')
        assert pref is None or pref.enabled


def test_prefix_limit_and_later_parent_messages(configured, monkeypatch):
    from lingxi import branches
    c, original, captured = setup(configured, monkeypatch)
    monkeypatch.setattr(branches, 'MAX_PREFIX', 1)
    assert fork(configured, c, original[3]).status_code == 409
    monkeypatch.setattr(branches, 'MAX_PREFIX', 1000)
    result = fork(configured, c, original[1]).json()
    r = configured.post('/api/chat', json={'conversation_id': c, 'request_id': 'parent-later-input', 'content': '未来仅留在原对话'})
    # Only consume the fork job first. The other conversation may progress independently.
    process(result['run_id'])
    assert '未来仅留' not in json.dumps(captured[-1], ensure_ascii=False)
    assert '未来仅留' not in json.dumps(messages(configured, result['conversation_id']), ensure_ascii=False)


@pytest.mark.skipif(__import__('lingxi.db', fromlist=['engine']).engine.dialect.name != 'postgresql',
    reason='Requires PostgreSQL row locks')
def test_concurrent_fork_requests_create_once(configured, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    c, original, _ = setup(configured, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: fork(configured, c, original[1]), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ConversationBranch)) == 1


def test_regenerating_task_receipt_uses_facts_not_dispatch(configured, monkeypatch):
    from lingxi.chat_actions import ChatAction
    from lingxi.tasks import Reminder
    from lingxi.db import now
    c, original, captured = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        run = db.get(ChatRun, original[1]['run_id'])
        run.config = run.config | {'task_assist': {'status': 'accepted'}}
        reminder = Reminder(user_id='owner', title='已经安排', content='原始提醒事项', due_at=now()+7200, timezone='Asia/Shanghai',
            request_id='original-reminder', request_hash='fixture', status='scheduled')
        db.add(reminder); db.flush()
        db.add(ChatAction(user_id='owner', run_id=run.id, conversation_id=c, source_id=original[0]['id'],
            capability='reminder.once', target_id=reminder.id))
    monkeypatch.setattr(gateway, 'complete', lambda *a, **kw: pytest.fail('Must not re-plan or execute'))
    branch = fork(configured, c, original[1]).json()
    process(branch['run_id'])
    assert '已经安排' in captured[-1][0]['content']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 1
        assert db.scalar(select(func.count()).select_from(ChatAction)) == 1
