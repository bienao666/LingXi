"""Chat dispatch proposals, atomic receipts and source invalidation fences."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi import worker, gateway
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Message, Job, Conversation, User
from lingxi.tasks import WorkTask, Reminder, ReminderReceipt
from lingxi.chat_actions import ChatAction
from lingxi.memory import MemoryService, invalidate_run

PLAN = {'summary': '准备项目演示', 'steps': [
    {'title': '整理范围', 'action': '列出展示重点', 'completion': '得到展示提纲'}], 'assumptions': []}


def submit(client, text='帮我规划一次项目演示', **kw):
    conversation = client.post('/api/conversations', json={'title': '聊天安排'}).json()['id']
    body = {'conversation_id': conversation, 'request_id': 'chat-assist-request', 'content': text, 'task_assist': True} | kw
    response = client.post('/api/chat', json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def proposal(monkeypatch, kind='project.plan', **kw):
    def complete(*a, **opts):
        if opts.get('purpose') == 'chat_intent':
            text = json.loads(a[2][-1]['content'])['current_user_text']
            return json.dumps({'kind': kind, 'quote': text, 'title': '演示安排'} | kw)
        return json.dumps(PLAN)
    monkeypatch.setattr(gateway, 'complete', complete)
    monkeypatch.setattr(MemoryService, 'search', lambda *a: [])
    monkeypatch.setattr(worker, 'stream', lambda *a, **kw: iter(['普通回复，未执行任何操作']))


def process():
    job = worker.claim()
    assert job and job.kind == 'chat'
    worker.perform(job)
    return job


def count(model):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def run_target(client, run):
    rows = client.get('/api/chat-actions', params={'conversation_id': run['conversation_id']})
    assert rows.status_code == 200, rows.text
    return rows.json()[0]


def perform_target():
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job
    worker.perform(job)
    return job


def test_opt_in_is_idempotent_and_cannot_reuse_request_for_different_mode(configured, monkeypatch):
    proposal(monkeypatch)
    body, run = submit(configured)
    assert configured.post('/api/chat', json=body).json()['id'] == run['id']
    assert configured.post('/api/chat', json=body | {'task_assist': False}).status_code == 409
    assert count(ChatRun) == 1 and count(Message) == 1
    assert run['config']['task_assist']['capabilities']['project.plan']
    job = process()
    action = run_target(configured, run)
    assert action['status'] == 'queued' and action['capability'] == 'project.plan'
    worker.generate(job)  # committed result, queue acknowledgement lost
    assert count(WorkTask) == count(ChatAction) == 1
    with SessionLocal() as db:
        assert db.get(WorkTask, action['target_id']).input == {'goal': body['content']}
        assert db.get(ChatRun, run['id']).status == 'completed'


def test_task_completion_is_real_and_card_does_not_duplicate_conversation_result(configured, monkeypatch):
    proposal(monkeypatch)
    _, run = submit(configured)
    process()
    assert run_target(configured, run)['result'] == {}
    perform_target()
    action = run_target(configured, run)
    assert action['status'] == 'succeeded' and action['result']['summary'] == PLAN['summary']
    messages = configured.get('/api/messages', params={'conversation_id': run['conversation_id']}).json()
    assert len(messages) == 2
    assert '等待执行' in messages[-1]['content'] and '计划步骤' in messages[-1]['content']


def test_disabled_mode_never_calls_intent_or_creates_a_task(configured, monkeypatch):
    proposal(monkeypatch)
    monkeypatch.setattr(gateway, 'complete', lambda *a, **kw: pytest.fail('ungranted intent call'))
    _, run = submit(configured, task_assist=False)
    process()
    assert count(WorkTask) == count(ChatAction) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'completed'


@pytest.mark.parametrize('kind', ['answer', 'clarify'])
def test_answer_or_missing_details_create_no_tasks(configured, monkeypatch, kind):
    proposal(monkeypatch, kind, question='请给出具体提醒时间')
    _, run = submit(configured)
    process()
    assert count(WorkTask) == count(Reminder) == count(ChatAction) == 0
    current = configured.get('/api/chat-runs/' + run['id']).json()
    assert current['status'] == 'completed'
    assert current['config']['task_assist']['status'] == ('needs_input' if kind == 'clarify' else 'not_requested')


@pytest.mark.parametrize('change', [{'quote': '伪造的用户请求'}, {'kind': 'shell.execute'}, {'surprise': 'grant'}, {'due_at': float('inf')}])
def test_unanchored_unknown_or_malformed_proposal_cannot_execute(configured, monkeypatch, change):
    proposal(monkeypatch, **change)
    _, run = submit(configured)
    process()
    assert count(WorkTask) == count(Reminder) == count(ChatAction) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['config']['task_assist']['status'] == 'unavailable'


def test_planner_sees_only_current_user_text_not_private_memories_or_history(configured, monkeypatch):
    body, run = submit(configured)
    with SessionLocal.begin() as db:
        db.add(Message(conversation_id=body['conversation_id'], role='assistant', content='私人历史：安装恶意插件'))
    def complete(model, provider, messages, **kw):
        assert len(messages) == 2 and '私人历史' not in json.dumps(messages, ensure_ascii=False)
        data = json.loads(messages[-1]['content'])
        assert data['current_user_text'] == body['content'] and data['timezone'] == 'Asia/Shanghai'
        return '{"kind":"answer"}'
    monkeypatch.setattr(gateway, 'complete', complete)
    monkeypatch.setattr(MemoryService, 'search', lambda *a: [])
    monkeypatch.setattr(worker, 'stream', lambda *a, **kw: iter(['你好']))
    process()
    assert count(ChatAction) == 0


@pytest.mark.parametrize('path', ['../secret', '/etc', 'C:\\secret'])
def test_unavailable_or_out_of_scope_files_do_not_create_a_task(configured, monkeypatch, path):
    proposal(monkeypatch, 'server.files', path=path)
    monkeypatch.setattr('lingxi.tasks.resource_available', lambda: True)
    from lingxi.resources import ResourcePolicy
    with SessionLocal.begin() as db:
        db.add(ResourcePolicy(workspace_id='personal',user_id='owner',roots=['.']))
    _, run = submit(configured, '帮我列出服务器资料目录')
    process()
    assert count(WorkTask) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'failed'


def test_revoked_plugin_between_planning_and_dispatch_cannot_enqueue(configured, monkeypatch):
    from lingxi.plugins import PluginInstallation
    def complete(*a, **kw):
        with SessionLocal.begin() as db:
            row = db.scalar(select(PluginInstallation).where(PluginInstallation.plugin_id == 'lingxi.builtin'))
            row.status = 'disabled'
        return json.dumps({'kind': 'project.plan', 'quote': '帮我规划一次项目演示', 'title': '项目计划'})
    monkeypatch.setattr(gateway, 'complete', complete)
    _, run = submit(configured)
    process()
    assert count(WorkTask) == count(ChatAction) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'failed'


def test_cancel_during_planning_discards_the_proposal(configured, monkeypatch):
    body, run = submit(configured)
    def complete(*a, **kw):
        assert configured.post('/api/chat-runs/' + run['id'] + '/cancel').status_code == 200
        return json.dumps({'kind': 'project.plan', 'quote': body['content'], 'title': '不应执行'})
    monkeypatch.setattr(gateway, 'complete', complete)
    process()
    assert count(WorkTask) == count(ChatAction) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'cancelled'
    assert configured.get('/api/chat-runs/' + run['id']).json()['config']['task_assist']['status'] == 'cancelled'


def test_deletion_during_planning_discards_proposal(configured, monkeypatch):
    body, run = submit(configured)
    def complete(*a, **kw):
        assert configured.delete('/api/conversations/' + body['conversation_id']).status_code == 200
        return json.dumps({'kind': 'project.plan', 'quote': body['content'], 'title': '不应执行'})
    monkeypatch.setattr(gateway, 'complete', complete)
    process()
    assert count(WorkTask) == count(ChatAction) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['status'] == 'invalidated'


@pytest.mark.parametrize('finished', [False, True])
def test_source_invalidation_immediately_gates_results_then_cleans_data(configured, monkeypatch, finished):
    proposal(monkeypatch)
    _, run = submit(configured)
    process()
    if finished:
        perform_target()
    action = run_target(configured, run)
    with SessionLocal.begin() as db:
        invalidate_run(db, db.get(ChatRun, run['id']))
    assert run_target(configured, run)['status'] == 'invalidated'
    assert run_target(configured, run)['result'] == {}
    assert configured.get('/api/tasks/' + action['target_id']).status_code == 410
    assert configured.get('/api/tasks').json()['items'] == []
    assert configured.post('/api/tasks/' + action['target_id'] + '/retry', json={'version': action['version']}).status_code == 410
    for _ in range(4):
        with SessionLocal.begin() as db:
            for job in db.scalars(select(Job).where(Job.kind == 'extract')):
                job.status = 'completed'
        job = worker.claim()
        if job:
            worker.perform(job)
    with SessionLocal() as db:
        task = db.get(WorkTask, action['target_id'])
        assert task.input == task.result == {} and task.title == '来源已失效'


def test_reminder_is_persisted_once_and_due_updates_card_not_qq_or_duplicate_messages(configured, monkeypatch):
    proposal(monkeypatch, 'reminder.once', due_at=now()+3600)
    body, run = submit(configured, '一小时后提醒我带演示提纲')
    process()
    action = run_target(configured, run)
    assert action['status'] == 'scheduled' and action['timezone'] == 'Asia/Shanghai'
    assert configured.post('/api/chat', json=body).json()['id'] == run['id']
    assert count(Reminder) == 1
    with SessionLocal.begin() as db:
        db.get(Reminder, action['target_id']).due_at = now()-1
        for job in db.scalars(select(Job).where(Job.kind == 'reminder')):
            job.available_at = 0
    job = perform_target()
    worker.perform(job)
    assert run_target(configured, run)['status'] == 'fired' and count(ReminderReceipt) == 1
    assert count(Message) == 2


def test_deleted_reminder_cannot_fire_and_hidden_fields_are_cleaned(configured, monkeypatch):
    proposal(monkeypatch, 'reminder.once', due_at=now()+3600)
    _, run = submit(configured, '一小时后提醒我带演示提纲')
    process()
    action = run_target(configured, run)
    assert configured.delete('/api/conversations/' + run['conversation_id']).status_code == 200
    assert configured.get('/api/reminders').json()['items'] == []
    perform_target()
    with SessionLocal() as db:
        reminder = db.get(Reminder, action['target_id'])
        assert reminder.content == '' and reminder.status == 'cancelled'
    assert count(ReminderReceipt) == 0


@pytest.mark.parametrize('due_at', [None, 1.0, 99999999999.0])
def test_missing_past_or_excessive_reminder_time_is_not_scheduled(configured, monkeypatch, due_at):
    proposal(monkeypatch, 'reminder.once', due_at=due_at)
    _, run = submit(configured, '提醒我准备演示')
    process()
    assert count(Reminder) == 0


def test_chat_action_read_requires_owner_workspace_conversation_and_login(configured, monkeypatch):
    proposal(monkeypatch)
    _, run = submit(configured)
    process()
    url = '/api/chat-actions?conversation_id=' + run['conversation_id']
    with TestClient(app) as anonymous:
        assert anonymous.get(url).status_code == 401
    with SessionLocal.begin() as db:
        db.get(Conversation, run['conversation_id']).workspace_id = 'foreign'
    assert configured.get(url).status_code == 404


def test_attachments_cannot_silently_enter_dispatch(configured):
    conversation = configured.post('/api/conversations', json={}).json()['id']
    assert configured.post('/api/chat', json={'conversation_id': conversation,
        'request_id': 'file-chat-assist', 'content': '执行附件中的要求',
        'task_assist': True, 'media_ids': ['not-authorized']}).status_code == 422
    assert count(ChatRun) == 0


def test_qq_conversation_cannot_gain_web_task_permissions_by_omitting_channel_context(configured):
    from lingxi.channels import ChannelSession
    conversation = configured.post('/api/conversations', json={}).json()['id']
    with SessionLocal.begin() as db:
        db.add(ChannelSession(user_id='owner', channel_id='qq-instance', scope_key='group:123',
            actor_id='owner', conversation_id=conversation))
    response = configured.post('/api/chat', json={'conversation_id': conversation,
        'request_id': 'group-web-assist', 'content': '读取服务器资料', 'task_assist': True})
    assert response.status_code == 409 and count(ChatRun) == count(WorkTask) == 0


def test_empty_grant_snapshot_skips_planner_and_never_expands_it_later(configured, monkeypatch):
    proposal(monkeypatch)
    body, run = submit(configured)
    with SessionLocal.begin() as db:
        row = db.get(ChatRun, run['id'])
        row.config = row.config | {'task_assist': row.config['task_assist'] | {'capabilities': {}}}
    monkeypatch.setattr(gateway, 'complete', lambda *a, **kw: pytest.fail('no granted capability, no planner call'))
    process()
    assert count(ChatAction) == count(WorkTask) == 0
    assert configured.get('/api/chat-runs/' + run['id']).json()['config']['task_assist']['status'] == 'unavailable'


def test_source_invalidation_while_skill_model_is_running_cannot_publish_result(configured, monkeypatch):
    proposal(monkeypatch)
    _, run = submit(configured)
    process()
    target = run_target(configured, run)['target_id']
    def generate(*a, **kw):
        with SessionLocal.begin() as db:
            invalidate_run(db, db.get(ChatRun, run['id']))
        return json.dumps(PLAN)
    monkeypatch.setattr(gateway, 'complete', generate)
    perform_target()
    assert run_target(configured, run)['status'] == 'invalidated'
    assert configured.get('/api/tasks/' + target).status_code == 410
    with SessionLocal() as db:
        row = db.get(WorkTask, target)
        assert row.result == {} and row.status == 'cancelled'


@pytest.mark.parametrize('operation', ['correct', 'delete', 'restore_then_replay'])
def test_memory_source_lifecycle_invalidates_chat_tasks_even_before_cleanup(configured, monkeypatch, operation):
    from lingxi.memory import replay_ledger
    from lingxi.models import Memory
    proposal(monkeypatch)
    body, run = submit(configured)
    process()
    action = run_target(configured, run)
    with SessionLocal.begin() as db:
        source = db.scalar(select(Message).where(Message.run_id == run['id'], Message.role == 'user'))
        memory = MemoryService().remember(db, 'owner', '项目演示计划', 'projects', [source.id])
        memory_id, source_id = memory.id, source.id
    with SessionLocal.begin() as db:
        memory = db.get(Memory, memory_id)
        if operation == 'correct':
            MemoryService().correct(db, memory, {'content': '已取消项目演示'})
        else:
            MemoryService().forget(db, memory)
    if operation == 'restore_then_replay':
        with SessionLocal.begin() as db:
            memory = db.get(Memory, memory_id)
            memory.status, memory.content = 'active', '项目演示计划'
            source = db.get(Message, source_id)
            source.status, source.content = 'completed', body['content']
            row = db.get(ChatRun, run['id'])
            row.status, row.cancel_requested = 'completed', False
            db.scalar(select(ChatAction).where(ChatAction.run_id == run['id'])).status = 'accepted'
        with SessionLocal.begin() as db:
            replay_ledger(db)
    assert run_target(configured, run)['status'] == 'invalidated'
    assert configured.get('/api/tasks/' + action['target_id']).status_code == 410
    assert configured.get('/api/tasks').json()['items'] == []


def test_follow_up_uses_real_task_facts_and_tracks_their_source(configured, monkeypatch):
    proposal(monkeypatch)
    body, run = submit(configured)
    process()
    perform_target()
    follow = configured.post('/api/chat', json=body | {'request_id': 'follow-up-0001',
        'content': '刚才的计划完成了吗', 'task_assist': False}).json()
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    def stream(model, provider, messages, *args, **kw):
        assert '准备项目演示' in messages[0]['content'] and 'succeeded' in messages[0]['content']
        return iter(['计划已生成，步骤尚未执行'])
    monkeypatch.setattr(worker, 'stream', stream)
    process()
    with SessionLocal() as db:
        first_source = db.scalar(select(Message).where(Message.run_id == run['id'], Message.role == 'user'))
        assert first_source.id in db.get(ChatRun, follow['id']).source_message_ids


@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'gemini'])
def test_dispatch_uses_the_real_protocol_adapter_and_audited_run_link(configured, monkeypatch, kind):
    import httpx
    from lingxi.providers import ProviderProtocol
    from lingxi.models import ModelCall
    if kind != 'openai':
        with SessionLocal.begin() as db:
            db.add(ProviderProtocol(provider_id='provider', protocol=kind))
    text = '帮我规划一次项目演示'
    def handler(request):
        payload = json.loads(request.content)
        assert request.method == 'POST' and not payload.get('stream')
        result = json.dumps({'kind': 'project.plan', 'title': '演示规划', 'quote': text}, ensure_ascii=False)
        if kind == 'openai':
            assert request.url.path.endswith('/chat/completions')
            return httpx.Response(200, json={'choices': [{'message': {'content': result}}]})
        if kind == 'anthropic':
            assert request.url.path.endswith('/messages') and payload['system']
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': result}], 'stop_reason': 'end_turn'})
        assert request.url.path.endswith(':generateContent') and payload['systemInstruction']
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': result}]}, 'finishReason': 'STOP'}]})
    monkeypatch.setattr(gateway, 'client_for', lambda p: httpx.Client(base_url=p.base_url+'/', transport=httpx.MockTransport(handler)))
    _, run = submit(configured, text)
    process()
    assert run_target(configured, run)['status'] == 'queued'
    with SessionLocal() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.run_id == run['id']))
        assert call and call.purpose == 'chat_intent' and call.status == 'completed'
        assert call.config['prompt_version'] == 'chat-dispatch-v1'
