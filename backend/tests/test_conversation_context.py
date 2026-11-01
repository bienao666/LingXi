"""Context compaction uses exact evidence, bounded reads, and ordinary source fences."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import Conversation, Message, ChatRun, Job, Model, Candidate
from lingxi import worker, gateway
from lingxi.memory import MemoryService, replay_ledger
from lingxi.conversation_context import ConversationContext


def setup(client, monkeypatch, mode='valid'):
    captured = {'compact': [], 'chat': []}
    monkeypatch.setattr(MemoryService, 'search', lambda *a: [])
    def complete(model, provider, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        captured['compact'].append((data, kwargs))
        source = data['candidates'][0]
        if callable(mode):
            return mode(data)
        if mode == 'failure':
            raise RuntimeError('synthetic upstream failure')
        if mode == 'empty':
            return '{"quotes":[],"reads":[]}'
        value = {'quotes': [{'message_id': source['message_id'], 'quote': source['text'][:40]}],
                 'reads': [{'message_id': source['message_id'], 'start': 10, 'length': 80}]}
        if mode == 'fabricated': value['quotes'][0]['quote'] = '不是原文的新事实'
        if mode == 'foreign': value['reads'][0]['message_id'] = 'foreign-message'
        if mode == 'range': value['reads'][0]['start'] = source['total'] + 1
        if mode == 'tool': value['tool'] = 'run this command'
        return json.dumps(value, ensure_ascii=False)
    def stream(model, provider, messages, *args, **kw):
        captured['chat'].append(messages)
        return iter(['合成最终回复'])
    monkeypatch.setattr(gateway, 'complete', complete)
    monkeypatch.setattr(worker, 'stream', stream)
    cid = client.post('/api/conversations', json={'title': '上下文评测'}).json()['id']
    with SessionLocal.begin() as db:
        db.get(Model, 'chat').context_window = 20000
        ids = []
        for i in range(15):
            m = Message(conversation_id=cid, role='user' if i % 2 == 0 else 'assistant',
                content=f'第{i}条约定：蓝桥项目使用茶色界面，项目编号{i}。' + ('原始上下文资料。' * 120),
                created_at=i+1, status='completed')
            db.add(m); db.flush(); ids.append(m.id)
    return cid, ids, captured


def submit(client, cid):
    response = client.post('/api/chat', json={'conversation_id': cid, 'request_id': 'context-request-001', 'content': '蓝桥项目原先有什么约定？'})
    assert response.status_code == 200, response.text
    return response.json()['id']


def process(run_id):
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job and job.payload['run_id'] == run_id
    worker.perform(job)


def test_long_history_compaction_and_read_enter_final_prompt(configured, monkeypatch):
    cid, ids, captured = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    run = configured.get('/api/chat-runs/' + rid).json()
    assert run['status'] == 'completed'
    assert len(captured['compact']) == 1 and captured['compact'][0][1]['purpose'] == 'context_summary'
    assert captured['compact'][0][1]['max_tokens'] <= 1000
    evidence = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert evidence['status'] in ('ready', 'partial')
    assert evidence['quotes'] and evidence['reads'] and evidence['counts']['recent'] >= 1
    prompt = json.loads(captured['chat'][0][2]['content'].split('\n', 1)[1])
    assert prompt['quotes'][0]['content'] == evidence['quotes'][0]['content']
    original = configured.get(f"/api/conversations/{cid}/history/{evidence['reads'][0]['message_id']}").json()
    assert evidence['reads'][0]['content'] == original['content'][10:90]
    assert captured['chat'][0][-1]['content'] == '蓝桥项目原先有什么约定？'
    assert sum(len(json.dumps(prompt, ensure_ascii=False).encode()) for _ in [0]) <= 2833
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Candidate)) == 0
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'extract')) == 1
        row = db.scalar(select(ConversationContext))
        assert set(row.source_hashes).issubset(set(db.get(ChatRun, rid).source_message_ids))
        assert 'content' not in json.dumps(run['config']['context_summary'])


@pytest.mark.parametrize('mode', ['failure', 'fabricated', 'foreign', 'range', 'tool'])
def test_bad_compaction_falls_back_without_guessing(configured, monkeypatch, mode):
    cid, ids, captured = setup(configured, monkeypatch, mode)
    rid = submit(configured, cid); process(rid)
    assert configured.get('/api/chat-runs/' + rid).json()['status'] == 'completed'
    evidence = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert evidence['status'] == 'unavailable' and not evidence['quotes'] and not evidence['reads']
    assert '不是原文' not in json.dumps(captured['chat'], ensure_ascii=False)
    assert len(captured['compact']) == 1


def test_short_history_does_not_add_a_model_call(configured, monkeypatch):
    cid, _, captured = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        db.query(Message).filter(Message.conversation_id == cid).delete()
    rid = submit(configured, cid); process(rid)
    assert not captured['compact']
    assert configured.get('/api/chat-runs/' + rid + '/context').json()['status'] == 'not_needed'


def test_no_room_for_excerpts_does_not_drop_current_question(configured, monkeypatch):
    cid, _, captured = setup(configured, monkeypatch)
    with SessionLocal.begin() as db: db.get(Model, 'chat').context_window = 2048
    r = configured.post('/api/chat', json={'conversation_id': cid, 'request_id': 'context-big-question', 'content': '原文' * 200}).json()
    process(r['id'])
    assert not captured['compact']
    assert captured['chat'][0][-1]['content'] == '原文' * 200
    assert configured.get('/api/chat-runs/' + r['id'] + '/context').json()['status'] == 'unavailable'


def test_history_search_and_read_are_bounded(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    page = configured.post(f'/api/conversations/{cid}/history/search', json={'query': '项目编号', 'limit': 2}).json()
    assert page['total'] == 15 and len(page['items']) == 2 and page['next_offset'] == 2
    value = configured.get(f'/api/conversations/{cid}/history/{ids[0]}?start=9&length=30').json()
    assert value['start'] == 9 and value['end'] == 39 and len(value['content']) == 30
    assert configured.get(f'/api/conversations/{cid}/history/{ids[0]}?start=99999').status_code == 422
    assert configured.get(f'/api/conversations/{cid}/history/{ids[0]}?length=5001').status_code == 422
    assert configured.post(f'/api/conversations/{cid}/history/search', json={'limit': 51}).status_code == 422


def test_foreign_history_and_deleted_sources_are_hidden(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        c = Conversation(user_id='someone-else', title='private')
        db.add(c); db.flush(); foreign = c.id
        m = Message(conversation_id=foreign, role='user', content='secret', status='completed')
        db.add(m); db.flush(); mid = m.id
    assert configured.get(f'/api/conversations/{cid}/history/{mid}').status_code == 404
    assert configured.post(f'/api/conversations/{foreign}/history/search', json={}).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.post(f'/api/conversations/{cid}/history/search', json={}).status_code == 401
    with SessionLocal.begin() as db: db.get(Conversation, cid).workspace_id = 'other'
    assert configured.get(f'/api/conversations/{cid}/history/{ids[0]}').status_code == 404


def test_branch_only_reads_frozen_prefix(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    result = configured.post(f'/api/conversations/{cid}/branches', json={'request_id': 'context-branch-001', 'mode': 'edit', 'message_id': ids[4], 'content': '分支提问'}).json()
    branch = result['conversation_id']
    assert configured.get(f'/api/conversations/{branch}/history/{ids[0]}').status_code == 200
    assert configured.get(f'/api/conversations/{branch}/history/{ids[6]}').status_code == 404
    page = configured.post(f'/api/conversations/{branch}/history/search', json={}).json()
    assert page['total'] == 5


def test_source_deleted_during_summary_cannot_publish(configured, monkeypatch):
    def delete(data):
        assert configured.delete('/api/conversations/' + cid).status_code == 200
        source = data['candidates'][0]
        return json.dumps({'quotes': [{'message_id': source['message_id'], 'quote': source['text'][:10]}], 'reads': []})
    cid, _, captured = setup(configured, monkeypatch, delete)
    rid = submit(configured, cid); process(rid)
    assert configured.get('/api/chat-runs/' + rid).json()['status'] == 'invalidated'
    assert not captured['chat']
    with SessionLocal() as db:
        row = db.scalar(select(ConversationContext)); assert row.status == 'invalidated' and row.data == {}


def test_cancel_during_summary_cannot_publish(configured, monkeypatch):
    def cancel(data):
        assert configured.post('/api/chat-runs/' + rid + '/cancel').status_code == 200
        return '{"quotes":[],"reads":[]}'
    cid, _, captured = setup(configured, monkeypatch, cancel)
    rid = submit(configured, cid); process(rid)
    assert configured.get('/api/chat-runs/' + rid + '/context').json()['status'] == 'cancelled'
    assert not captured['chat']


def test_correction_and_deletion_scrub_completed_snapshot(configured, monkeypatch):
    cid, ids, captured = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ConversationContext))
        # A personal fact must be anchored in user evidence, not an assistant quote.
        source_id = next(identity for identity in row.source_hashes if db.get(Message, identity).role == 'user')
        memory = MemoryService().remember(db, 'owner', '蓝桥项目使用茶色界面', 'projects', [source_id])
        MemoryService().forget(db, memory)
    evidence = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert evidence['status'] == 'invalidated' and not evidence['quotes'] and not evidence['reads']
    with SessionLocal.begin() as db: assert replay_ledger(db) == 0
    assert configured.get(f'/api/conversations/{cid}/history/{source_id}').status_code == 404


def test_raw_hash_change_invalidates_context_on_read(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ConversationContext)); db.get(Message, next(iter(row.source_hashes))).content = '被修改的原文'
    result = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert result['status'] == 'invalidated' and not result['quotes']
    assert configured.get('/api/chat-runs/' + rid).json()['status'] == 'invalidated'


def test_model_failure_erases_prepared_excerpts(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    def fail(*a, **k): raise RuntimeError('synthetic chat failure')
    monkeypatch.setattr(worker, 'stream', fail)
    rid = submit(configured, cid); process(rid)
    result = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert result['status'] == 'failed' and not result['quotes']


def test_get_context_requires_owner(configured, monkeypatch):
    cid, ids, _ = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db: db.get(ChatRun, rid).user_id = 'another'
    assert configured.get('/api/chat-runs/' + rid + '/context').status_code == 404

def test_voice_compaction_does_not_recover_unplayed_words(configured, monkeypatch):
    from lingxi.voice import VoicePlayback
    from lingxi.conversation_context import visible_history, prepare
    cid, ids, _ = setup(configured, monkeypatch)
    with SessionLocal.begin() as db:
        old = ChatRun(user_id='owner', conversation_id=cid, request_id='old-voice', request_hash='fixture', model_id='chat',
            status='completed', config={'voice_session_id': 'voice-old'})
        db.add(old); db.flush()
        message = db.get(Message, ids[1]); message.run_id = old.id
        message.content = '已播放的约定。未播放的秘密内容。'
        db.add(VoicePlayback(user_id='owner', session_id='voice-old', message_id=message.id,
            digest='fixture', confirmed_parts=1))
        run = ChatRun(user_id='owner', conversation_id=cid, request_id='new-voice', request_hash='fixture', model_id='chat',
            config={'voice_session_id': 'voice-new'}, source_message_ids=[])
        db.add(run); db.flush()
        history = visible_history(db, run)
        draft = prepare(db, run, history, {history[-1].id}, '已播放的约定', 3000, db.get(Model, 'chat'))
        value = json.dumps(draft['payload'], ensure_ascii=False)
        assert '未播放的秘密内容' not in value
        assert draft['projections'][ids[1]] == '已播放的约定。'
        assert db.get(Message, ids[1]).content.endswith('未播放的秘密内容。')


def test_channel_compaction_excludes_unaccepted_assistant_reply(configured, monkeypatch):
    from lingxi.conversation_context import visible_history
    cid, ids, _ = setup(configured, monkeypatch)
    with SessionLocal() as db:
        run = ChatRun(conversation_id=cid, config={'channel': {'id': 'fixture'}})
        history = visible_history(db, run)
        assert all(m.role == 'user' for m in history)


def test_cancelled_generation_is_not_replayed_after_restart(configured, monkeypatch):
    cid, ids, captured = setup(configured, monkeypatch)
    rid = submit(configured, cid)
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')): job.status = 'completed'
    job = worker.claim()
    with SessionLocal.begin() as db: db.get(ChatRun, rid).status = 'running'
    worker.perform(job)
    assert configured.get('/api/chat-runs/' + rid).json()['status'] == 'failed'
    assert not captured['compact'] and not captured['chat']


def test_empty_selection_is_not_a_fabricated_summary(configured, monkeypatch):
    cid, _, captured = setup(configured, monkeypatch, 'empty')
    rid = submit(configured, cid); process(rid)
    value = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert value['status'] in ('partial', 'ready') and not value['quotes'] and not value['reads']
    assert len(captured['compact']) == 1


def test_correction_scrubs_snapshot_and_future_context(configured, monkeypatch):
    cid, _, _ = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db:
        row = db.scalar(select(ConversationContext))
        source_id = next(identity for identity in row.source_hashes if db.get(Message, identity).role == 'user')
        memory = MemoryService().remember(db, 'owner', '蓝桥项目旧约定', 'projects', [source_id])
        MemoryService().correct(db, memory, {'content': '蓝桥项目新约定', 'category': 'projects', 'subject': 'self'})
    result = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert result['status'] == 'invalidated' and not result['quotes']


def test_attachment_deletion_invalidates_derived_summary(configured, monkeypatch):
    from lingxi.media import Media, forget_media
    cid, _, _ = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db:
        item = Media(user_id='owner', filename='fixture.txt', suffix='.txt', request_id='fixture-media', digest='fixture',
            size=10, status='ready', parsed={'kind':'text','parts':[],'notes':[]})
        # Use a ready media evidence ref without reading any real attachment.
        db.add(item); db.flush()
        run = db.get(ChatRun, rid); run.config = run.config | {'media_refs': [{'id':item.id,'version':item.version}]}
        forget_media(db, item, journal=False)
    result = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert result['status'] == 'invalidated' and not result['quotes']

def test_memory_expiry_invalidates_open_evidence_snapshot(configured, monkeypatch):
    from lingxi.models import Memory
    from lingxi.db import now
    cid, _, _ = setup(configured, monkeypatch)
    rid = submit(configured, cid); process(rid)
    with SessionLocal.begin() as db:
        memory = Memory(user_id='owner', content='临时有效的项目资料', category='projects', expires_at=now()-1)
        db.add(memory); db.flush()
        db.get(ChatRun, rid).memory_refs = [{'id':memory.id,'version':memory.version}]
    result = configured.get('/api/chat-runs/' + rid + '/context').json()
    assert result['status'] == 'invalidated' and not result['quotes'] and not result['reads']
