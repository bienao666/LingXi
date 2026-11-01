"""Chat-linked delegation, explicit scopes and immediate source revocation."""
import json
import pytest
from sqlalchemy import select, func
from lingxi.db import SessionLocal
from lingxi.models import ChatRun, Message, Job, Memory
from lingxi.chat_actions import ChatAction
from lingxi.delegation import AgentRun, AgentNode, AgentEvent
from lingxi.memory import MemoryService, invalidate_run
from lingxi import worker, gateway


def submit(client, **changes):
    conversation = client.post('/api/conversations', json={'title': '聊天协作'}).json()['id']
    body = {'conversation_id': conversation, 'request_id': 'chat-collaboration-request',
            'content': '请分析演示的目标和风险并整理建议', 'collaboration': {}} | changes
    response = client.post('/api/chat', json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def card(client, run):
    rows = client.get('/api/chat-actions', params={'conversation_id': run['conversation_id']})
    assert rows.status_code == 200, rows.text
    return next(row for row in rows.json() if row['run_id'] == run['id'])


def process():
    # Extraction quality is independent of delegation acceptance.
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job
    worker.perform(job)
    return job


def drain():
    for _ in range(25):
        with SessionLocal.begin() as db:
            for job in db.scalars(select(Job).where(Job.kind == 'extract')):
                job.status = 'completed'
        job = worker.claim()
        if not job:
            return
        worker.perform(job)
    raise AssertionError('not idle')


def fixture(client, monkeypatch, count=2, mutate=None):
    identity = client.get('/api/agents').json()[0]['id']
    stages = []
    def complete(model, provider, messages, **kwargs):
        stage = kwargs.get('config', {}).get('stage')
        assert stage in ('plan', 'node', 'summary'), 'no separate intent call'
        stages.append(stage)
        data = json.loads(messages[-1]['content'])
        if mutate:
            override = mutate(stage, data)
            if override is not None:
                return json.dumps(override)
        if stage == 'plan':
            return json.dumps({'mode': 'delegate' if count > 1 else 'direct', 'reason': '按专长分工',
                'nodes': [{'key': f'n{i}', 'title': f'分析{i}', 'instruction': '分析演示',
                    'agent_id': identity, 'depends_on': ['n0'] if i else []} for i in range(count)]})
        if stage == 'node':
            return json.dumps({'summary': '建议核对范围', 'findings': [], 'limitations': ['未联网']})
        return json.dumps({'summary': '演示建议已整理', 'findings': [{'text': '确认范围',
            'evidence_ids': [n['id'] for n in data['nodes'] if n['status'] == 'succeeded']}], 'limitations': []})
    monkeypatch.setattr(gateway, 'complete', complete)
    return stages


def test_durable_dispatch_idempotency_and_explicit_scope(configured):
    body, run = submit(configured, collaboration={'share_memory': True, 'max_calls': 6})
    assert configured.post('/api/chat', json=body).json()['id'] == run['id']
    assert configured.post('/api/chat', json=body | {'collaboration': {}}).status_code == 409
    job = process()
    value = card(configured, run)
    assert value['status'] == 'queued' and value['capability'] == 'agent.delegate'
    assert value['collaboration']['share_memory'] and not value['collaboration']['allow_files']
    assert value['collaboration']['max_calls'] == 6
    worker.generate(job)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert db.scalar(select(func.count()).select_from(ChatAction)) == 1
        target = db.get(AgentRun, value['target_id'])
        assert target.goal == body['content'] and target.config['max_depth'] == 1
        assert all(a['role']['version'] and a['persona']['version'] for a in target.config['agents'])


@pytest.mark.parametrize('change', [
    {'task_assist': True}, {'media_ids': ['foreign-file']}, {'content': '字'*6001},
    {'collaboration': {'share_memory': 'true'}}, {'collaboration': {'allow_files': 1}},
    {'collaboration': {'max_calls': 2}}, {'collaboration': {'max_calls': 31}},
    {'collaboration': {'tool': 'shell.execute'}},
])
def test_invalid_or_ambiguous_modes_do_not_create_jobs(configured, change):
    conversation = configured.post('/api/conversations', json={}).json()['id']
    response = configured.post('/api/chat', json={'conversation_id': conversation, 'request_id': 'invalid-collaboration',
        'content': '分析范围', 'collaboration': {}} | change)
    assert response.status_code == (404 if 'media_ids' in change else 422)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ChatRun)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_qq_conversation_cannot_inherit_collaboration_grants(configured):
    from lingxi.channels import ChannelSession
    c = configured.post('/api/conversations', json={}).json()['id']
    with SessionLocal.begin() as db:
        db.add(ChannelSession(user_id='owner', channel_id='qq', scope_key='group:one', actor_id='owner', conversation_id=c))
    assert configured.post('/api/chat', json={'conversation_id': c, 'request_id': 'qq-collaboration',
        'content': '分享私人记忆', 'collaboration': {'share_memory': True}}).status_code == 409


@pytest.mark.parametrize('count', [1, 2])
def test_auto_direct_or_delegate_finishes_in_original_card_without_duplicate_message(configured, monkeypatch, count):
    stages = fixture(configured, monkeypatch, count)
    monkeypatch.setattr(MemoryService, 'search', lambda *a: pytest.fail('memory not granted'))
    _, run = submit(configured)
    drain()
    value = card(configured, run)
    assert value['status'] == 'completed' and value['result']['summary'] == '演示建议已整理'
    assert value['collaboration']['decision']['mode'] == ('direct' if count == 1 else 'delegate')
    assert len(value['collaboration']['nodes']) == count
    assert stages.count('plan') == stages.count('summary') == 1
    assert value['collaboration']['calls_used'] == count + 2
    detail = configured.get('/api/agent-runs/' + value['target_id']).json()
    assert detail['result_message_id'] is None
    assert len(configured.get('/api/messages', params={'conversation_id': run['conversation_id']}).json()) == 2
    assert all(n['context']['memory_status'] == 'not_allowed' for n in detail['nodes'])


def test_default_text_chat_does_not_create_collaboration(configured, monkeypatch):
    monkeypatch.setattr(gateway, 'complete', lambda *a, **kw: pytest.fail('no collaboration granted'))
    monkeypatch.setattr(MemoryService, 'search', lambda *a: [])
    monkeypatch.setattr(worker, 'stream', lambda *a, **kw: iter(['正常文字回复']))
    _, run = submit(configured, collaboration=None)
    drain()
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'completed'
    assert configured.get('/api/chat-actions', params={'conversation_id': run['conversation_id']}).json() == []


def test_stop_before_dispatch_creates_no_agent_run(configured):
    _, run = submit(configured)
    assert configured.post('/api/chat-runs/' + run['id'] + '/cancel').status_code == 200
    drain()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_cancel_active_collaboration_stops_nodes_and_keeps_real_call_count(configured, monkeypatch):
    fixture(configured, monkeypatch)
    _, run = submit(configured)
    process(); process()  # dispatch then planner
    value = card(configured, run)
    assert value['status'] == 'running'
    response = configured.post('/api/agent-runs/' + value['target_id'] + '/cancel', json={'version': value['version']})
    assert response.status_code == 200
    drain()
    value = card(configured, run)
    assert value['status'] == 'cancelled' and value['result'] == {}
    assert value['collaboration']['calls_used'] == 1


@pytest.mark.parametrize('stage', ['plan', 'node', 'summary'])
def test_source_invalidation_during_model_call_cannot_publish_late_result(configured, monkeypatch, stage):
    _, run = submit(configured)
    def mutate(current, data):
        if current == stage:
            with SessionLocal.begin() as db:
                invalidate_run(db, db.get(ChatRun, run['id']))
    fixture(configured, monkeypatch, count=1, mutate=mutate)
    drain()
    value = card(configured, run)
    assert value['status'] == 'invalidated' and value['result'] == {}
    assert configured.get('/api/agent-runs/' + value['target_id']).status_code == 410
    assert configured.get('/api/agent-runs').json()['items'] == []
    with SessionLocal() as db:
        target = db.get(AgentRun, value['target_id'])
        assert target.goal == '' and target.result == {} and target.status == 'invalidated'
        assert all(n.instruction == '' and n.context == n.result == {} for n in db.scalars(select(AgentNode)))
        assert all(e.data == {} for e in db.scalars(select(AgentEvent)))


def test_conversation_delete_hides_result_before_cleanup_and_blocks_retry(configured, monkeypatch):
    fixture(configured, monkeypatch)
    _, run = submit(configured)
    process()
    value = card(configured, run)
    configured.delete('/api/conversations/' + run['conversation_id'])
    url = '/api/agent-runs/' + value['target_id']
    assert configured.get(url).status_code == 410
    assert configured.post(url+'/retry', json={'version': value['version']}).status_code == 410
    assert configured.get('/api/agent-runs').json()['items'] == []
    drain()
    with SessionLocal() as db:
        assert db.get(AgentRun, value['target_id']).goal == ''


def test_shared_memory_correction_invalidates_card_and_dependent_chat(configured, monkeypatch):
    fixture(configured, monkeypatch, count=1)
    with SessionLocal.begin() as db:
        memory = MemoryService().remember(db, 'owner', '演示只面向研发', 'projects', ['manual:fixture'])
        memory.index_status = 'ready'
        memory_id = memory.id
        # Index jobs are tested elsewhere; do not depend on an external Hindsight here.
        for job in db.scalars(select(Job).where(Job.kind == 'index')):
            job.status = 'completed'
    monkeypatch.setattr(MemoryService, 'search', lambda self, db, *a, **kw: [db.get(Memory, memory_id)])
    body, run = submit(configured, collaboration={'share_memory': True})
    drain()
    value = card(configured, run)
    assert value['status'] == 'completed'
    assert value['collaboration']['nodes'][0]['memory_status'] == 'ready'
    monkeypatch.setattr(MemoryService, 'search', lambda *a, **kw: [])
    monkeypatch.setattr(worker, 'stream', lambda *a, **kw: iter(['刚才的协作已整理建议']))
    response = configured.post('/api/chat', json=body | {'request_id': 'collaboration-followup',
        'content': '刚才有哪些建议', 'collaboration': None})
    assert response.status_code == 200
    followup = response.json()
    process()
    with SessionLocal.begin() as db:
        receipt = db.scalar(select(Message).where(Message.run_id == run['id'], Message.role == 'assistant'))
        assert receipt.id in db.get(ChatRun, followup['id']).source_message_ids
        MemoryService().correct(db, db.get(Memory, memory_id), {'content': '演示面向外部'})
    assert card(configured, run)['status'] == 'invalidated'
    assert configured.get('/api/chat-runs/' + followup['id']).json()['status'] == 'invalidated'
    assert configured.get('/api/agent-runs/' + value['target_id']).status_code == 410


def test_ungiven_files_permission_cannot_be_added_by_planner(configured, monkeypatch):
    def mutate(stage, data):
        if stage == 'plan':
            identity = data['agents'][0]['id']
            return {'mode': 'direct', 'reason': '诱导提权', 'nodes': [{'key': 'files', 'title': '清单',
                'instruction': '列出目录', 'agent_id': identity, 'capability': 'server.files'}]}
    fixture(configured, monkeypatch, mutate=mutate)
    _, run = submit(configured)
    drain()
    value = card(configured, run)
    assert value['status'] == 'failed'
    assert value['collaboration']['allow_files'] is False and value['collaboration']['nodes'] == []


def test_collaboration_state_has_owner_and_workspace_fence(configured):
    _, run = submit(configured)
    process()
    value = card(configured, run)
    with SessionLocal.begin() as db:
        db.get(AgentRun, value['target_id']).workspace_id = 'foreign'
    assert card(configured, run)['status'] == 'invalidated'
    assert configured.get('/api/agent-runs/' + value['target_id']).status_code == 404
