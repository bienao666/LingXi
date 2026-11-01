"""Attachment delegation against persisted sources; all model answers are protocol fixtures."""
import io
import json
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from lingxi.db import SessionLocal
from lingxi.models import Model, Message, ChatRun, Job
from lingxi.media import Media, VisionCapability
from lingxi.delegation import AgentRun, AgentNode
from lingxi import worker, gateway
from test_media import upload, process_media
from test_delegation import create, fetch, drain


def attachment(client, image=False, text='项目预算：42 万元\n未经确认的建议不等于已执行', key='attachment-one'):
    if image:
        buffer = io.BytesIO()
        Image.new('RGB', (240, 180), 'blue').save(buffer, format='PNG')
        data, name = buffer.getvalue(), '示意图.png'
    else:
        data, name = text.encode(), '项目.md'
    response = upload(client, name, data, request_id=key)
    assert response.status_code == 200, response.text
    process_media()
    return client.get('/api/media/' + response.json()['id']).json()


def vision_agent(client):
    # Main dispatcher stays text-only; only this explicitly configured assistant sees images.
    client.get('/api/agents')
    with SessionLocal.begin() as db:
        db.add(Model(id='visual', provider_id='provider', name='fixture-vision', purpose='chat',
                     status='verified', verified_provider_version=1))
        db.add(VisionCapability(model_id='visual', model_name='fixture-vision', provider_version=1, status='verified'))
    persona = client.get('/api/personas').json()[0]
    role_id = client.get('/api/agent-roles').json()[0]['id']
    response = client.post('/api/agent-profiles', json={'name':'图片分析', 'persona_id':persona['id'],
        'role_id':role_id, 'model_id':'visual'})
    assert response.status_code == 200, response.text
    return response.json()['id']


def protocol(client, monkeypatch, items, agent=None, mode='valid', mutate=None):
    identity = agent or client.get('/api/agents').json()[0]['id']
    calls = []
    def complete(model, provider, messages, purpose='chat', config=None):
        stage = config['stage']
        content = messages[-1]['content']
        data = json.loads(content[0]['text'] if isinstance(content, list) else content)
        calls.append((stage, model.id, data, content))
        if mutate:
            mutate(stage)
        if stage == 'plan':
            assert '项目预算' not in json.dumps(data['attachments'], ensure_ascii=False)
            ids = [m['id'] for m in items]
            if mode == 'omit': ids = ids[:-1]
            if mode == 'foreign': ids = ['outside-task']
            if mode == 'duplicate': ids += ids[:1]
            nodes = [{'key':'read', 'title':'资料分析', 'instruction':'分析资料并保留来源位置',
                'agent_id':identity, 'media_ids':ids}]
            if mode in ('split', 'partial'):
                nodes[0]['media_ids'] = ids[:1]
                nodes.append({'key':'other', 'title':'另一份', 'instruction':'分析第二份',
                    'agent_id':identity, 'media_ids':ids[1:]})
                nodes.append({'key':'join', 'title':'比较', 'instruction':'比较结论',
                    'agent_id':identity, 'depends_on':['read', 'other']})
            if mode == 'tool': nodes[0]['capability'] = 'plugin.call'
            return json.dumps({'mode':'direct' if len(nodes)==1 else 'delegate', 'reason':'按附件范围分配', 'nodes':nodes})
        if stage == 'node':
            if mode == 'partial' and data['instruction'] == '分析第二份':
                raise HTTPException(502, '协议测试中的节点失败')
            ids = [m['id'] for m in data['context_bundle'].get('attachments', [])]
            if mode == 'forged': ids = ['not-assigned']
            return json.dumps({'summary':'分析材料，未执行任何操作', 'findings':[{
                'text':'第 1 行给出的项目预算为 42 万元', 'evidence_ids':ids}], 'limitations':[]})
        return json.dumps({'summary':'汇总了可用节点，未执行外部操作', 'findings':[{
            'text':'请按来源核对预算', 'evidence_ids':[n['id'] for n in data['nodes'] if n['status']=='succeeded']}],
            'limitations':['失败节点未完成分析'] if mode == 'partial' else []})
    monkeypatch.setattr(gateway, 'complete', complete)
    return calls


def delete(client, item):
    result = client.request('DELETE', '/api/media/' + item['id'], json={'version':item['version']})
    assert result.status_code == 200, result.text


def test_document_node_receives_locations_without_persisting_body(configured, monkeypatch):
    item = attachment(configured)
    calls = protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'completed'
    assert detail['config']['media_refs'] == [{'id':item['id'], 'version':item['version']}]
    node = next(c for c in calls if c[0] == 'node')
    assert '项目预算' in json.dumps(node[3], ensure_ascii=False)
    assert '第 1 行' in json.dumps(node[3], ensure_ascii=False)
    assert detail['nodes'][0]['context']['attachments'][0]['id'] == item['id']
    assert '项目预算' not in json.dumps(detail['nodes'][0]['context'], ensure_ascii=False)
    assert 'data:image' not in json.dumps(detail)


def test_text_dispatcher_assigns_image_to_verified_visual_assistant(configured, monkeypatch):
    item = attachment(configured, image=True)
    agent = vision_agent(configured)
    calls = protocol(configured, monkeypatch, [item], agent=agent)
    row = create(configured, media_ids=[item['id']]); drain()
    assert fetch(configured, row)['status'] == 'completed'
    assert calls[0][1] == 'chat'
    node = next(c for c in calls if c[0] == 'node')
    assert node[1] == 'visual' and node[3][-1]['type'] == 'image_url'
    assert node[3][-1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert 'data:image' not in json.dumps(fetch(configured, row))


@pytest.mark.parametrize('mode', ['omit', 'foreign', 'duplicate', 'tool'])
def test_invalid_attachment_plan_executes_no_node(configured, monkeypatch, mode):
    item = attachment(configured)
    calls = protocol(configured, monkeypatch, [item], mode=mode)
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'failed' and not detail['nodes']
    assert [c[0] for c in calls] == ['plan']


def test_unverified_image_node_does_not_silently_drop_image(configured, monkeypatch):
    item = attachment(configured, image=True)
    calls = protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'failed' and not detail['nodes']
    assert '视觉验证' in detail['error'] and len(calls) == 1


def test_assigned_files_are_scoped_and_dependency_provenance_is_inherited(configured, monkeypatch):
    items = [attachment(configured), attachment(configured, key='attachment-two', text='另一份材料')]
    calls = protocol(configured, monkeypatch, items, mode='split')
    row = create(configured, media_ids=[m['id'] for m in items]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'completed'
    nodes = {n['key']:n for n in detail['nodes']}
    assert [m['id'] for m in nodes['read']['context']['attachments']] == [items[0]['id']]
    assert [m['id'] for m in nodes['other']['context']['attachments']] == [items[1]['id']]
    assert not nodes['join']['context']['attachments']
    assert {m['id'] for m in nodes['join']['context']['media_provenance']} == {m['id'] for m in items}
    join = next(c for c in calls if c[0]=='node' and c[2]['instruction']=='比较结论')
    assert isinstance(join[3], str), 'downstream receives cited results, not attachment bytes'


@pytest.mark.parametrize('change,status', [('foreign',404), ('workspace',404), ('not_ready',409),
                                         ('deleted',409), ('audio',422), ('duplicate',422), ('limit',413)])
def test_submission_rejects_unavailable_or_out_of_scope_media(configured, change, status):
    item = attachment(configured)
    with SessionLocal.begin() as db:
        obj = db.get(Media, item['id'])
        if change == 'workspace': obj.workspace_id = 'other'
        if change == 'not_ready': obj.status = 'processing'
        if change == 'deleted': obj.status = 'deleted'
        if change == 'audio': obj.parsed = {'kind':'audio', 'parts':[], 'notes':[]}
        if change == 'limit': obj.size = 51*1024**2
    ids = ['unknown'] if change == 'foreign' else [item['id']] * (2 if change=='duplicate' else 1)
    r = configured.post('/api/agent-runs', json={'request_id':'invalid-media-run', 'title':'测试', 'goal':'分析', 'media_ids':ids})
    assert r.status_code == status, r.text
    with SessionLocal() as db:
        assert not db.scalar(select(AgentRun))


def test_idempotency_includes_attachment_scope_and_preserves_old_empty_hash(configured):
    a, b = attachment(configured), attachment(configured, key='attachment-two')
    row = create(configured, media_ids=[a['id']])
    assert create(configured, media_ids=[a['id']])['id'] == row['id']
    r = configured.post('/api/agent-runs', json={'request_id':'delegation-request-001', 'title':row['title'],
        'goal':row['goal'], 'media_ids':[b['id']]})
    assert r.status_code == 409


@pytest.mark.parametrize('stage', ['queued', 'plan', 'node', 'summary', 'completed'])
def test_deletion_fences_all_phases_and_late_model_results(configured, monkeypatch, stage):
    item = attachment(configured)
    deleted = False
    def mutate(current):
        nonlocal deleted
        if current == stage and not deleted:
            deleted = True; delete(configured, item)
    calls = protocol(configured, monkeypatch, [item], mutate=mutate)
    c = configured.post('/api/conversations', json={'title':'来源会话'}).json()
    row = create(configured, media_ids=[item['id']], conversation_id=c['id'])
    if stage == 'queued': delete(configured, item)
    drain()
    if stage == 'completed':
        assert fetch(configured, row)['status'] == 'completed'
        delete(configured, item); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'invalidated' and detail['result'] == {}
    assert all(not n['result'] and not n['context'] for n in detail['nodes'])
    with SessionLocal() as db:
        messages = list(db.scalars(select(Message).where(Message.conversation_id == c['id'])))
        assert all(m.status == 'invalidated' and not m.content for m in messages)


def test_unassigned_evidence_does_not_become_success(configured, monkeypatch):
    item = attachment(configured)
    protocol(configured, monkeypatch, [item], mode='forged')
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'failed' and not detail['result']


def test_oversized_document_is_not_silently_truncated(configured, monkeypatch):
    item = attachment(configured, text='文'*40000)
    calls = protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'failed'
    assert '预算' in detail['nodes'][0]['error']
    assert [c[0] for c in calls] == ['plan']


def test_image_transport_budget_is_independent_of_base64_text(configured):
    from lingxi.agent_media import text_budget
    messages = [{'role':'user','content':[{'type':'text','text':'中文'},
        {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+'A'*200000}}]}]
    assert text_budget(messages) == 8006


def test_vision_revoked_during_model_call_blocks_publication(configured, monkeypatch):
    item = attachment(configured, image=True)
    agent = vision_agent(configured)
    def mutate(stage):
        if stage == 'node':
            with SessionLocal.begin() as db:
                db.get(VisionCapability, 'visual').status = 'failed'
    protocol(configured, monkeypatch, [item], agent=agent, mutate=mutate)
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] != 'completed' and not detail['result']


def test_source_scope_changed_is_redacted_on_read(configured, monkeypatch):
    item = attachment(configured)
    protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']]); drain()
    with SessionLocal.begin() as db:
        db.get(Media, item['id']).workspace_id = 'other'
    assert fetch(configured, row)['status'] == 'invalidated'
    assert fetch(configured, row)['result'] == {}


def test_chat_collaboration_attaches_and_invalidates_one_unified_result(configured, monkeypatch):
    from test_chat_collaboration import submit, drain as drain_chat, card
    item = attachment(configured)
    calls = protocol(configured, monkeypatch, [item])
    body, run = submit(configured, media_ids=[item['id']])
    assert configured.post('/api/chat', json=body).json()['id'] == run['id']
    drain_chat()
    value = card(configured, run)
    assert value['status'] == 'completed'
    assert [c[0] for c in calls] == ['plan','node','summary']
    with SessionLocal() as db:
        target = db.get(AgentRun, value['target_id'])
        assert target.config['media_refs'][0]['id'] == item['id']
        assert not target.result_message_id, 'chat card remains the only result outlet'
    delete(configured, item); drain_chat()
    assert card(configured, run)['status'] == 'invalidated'
    with SessionLocal() as db:
        assert db.get(ChatRun, run['id']).status == 'invalidated'


def test_partial_run_reports_failed_attachment_analysis(configured, monkeypatch):
    items = [attachment(configured), attachment(configured, key='attachment-two')]
    protocol(configured, monkeypatch, items, mode='partial')
    row = create(configured, media_ids=[m['id'] for m in items]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'partial'
    assert detail['result']['limitations'] and any(n['status'] == 'failed' for n in detail['nodes'])


def test_restore_ledger_invalidates_recovered_attachment_results(configured, monkeypatch):
    from lingxi.memory import append_ledger, replay_ledger
    item = attachment(configured)
    protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']]); drain()
    assert fetch(configured, row)['status'] == 'completed'
    append_ledger({'kind':'media', 'id':item['id'], 'user_id':'owner'})
    with SessionLocal.begin() as db:
        replay_ledger(db)
    detail = fetch(configured, row)
    assert detail['status'] == 'invalidated' and not detail['result']
    assert all(not n['result'] and not n['context'] for n in detail['nodes'])


def test_committed_plan_resumes_without_planning_again(configured, monkeypatch):
    item = attachment(configured)
    calls = protocol(configured, monkeypatch, [item])
    row = create(configured, media_ids=[item['id']])
    worker.perform(worker.claim())
    from lingxi.db import engine
    engine.dispose()
    drain()
    assert fetch(configured, row)['status'] == 'completed'
    assert [c[0] for c in calls] == ['plan', 'node', 'summary']


def test_image_binary_budget_prevents_paid_call(configured, monkeypatch):
    from lingxi.media import storage
    item = attachment(configured, image=True)
    agent = vision_agent(configured)
    storage(item['id'], 'preview').write_bytes(b'x' * (12*1024**2+1))
    calls = protocol(configured, monkeypatch, [item], agent=agent)
    row = create(configured, media_ids=[item['id']]); drain()
    detail = fetch(configured, row)
    assert detail['status'] == 'failed' and '12 MiB' in detail['nodes'][0]['error']
    assert [c[0] for c in calls] == ['plan']


def test_completed_answer_descendants_are_invalidated(configured, monkeypatch):
    item = attachment(configured)
    protocol(configured, monkeypatch, [item])
    c = configured.post('/api/conversations', json={'title':'原会话'}).json()
    row = create(configured, media_ids=[item['id']], conversation_id=c['id']); drain()
    detail = fetch(configured, row)
    with SessionLocal.begin() as db:
        child = ChatRun(user_id='owner', workspace_id='default', conversation_id=c['id'],
            request_id='derived-answer', request_hash='derived', model_id='chat', status='completed',
            source_message_ids=[detail['result_message_id']])
        db.add(child);db.flush()
        reply = Message(workspace_id='default', conversation_id=c['id'], run_id=child.id,
            role='assistant', content='依据附件分析的后续回答')
        db.add(reply);db.flush();child_id=child.id;reply_id=reply.id
    delete(configured, item)
    with SessionLocal() as db:
        assert db.get(ChatRun, child_id).status == 'invalidated'
        assert not db.get(Message, reply_id).content


def test_selected_attachment_order_survives_stable_lock_order(configured, monkeypatch):
    items = [attachment(configured), attachment(configured, key='attachment-two')]
    ids = sorted([m['id'] for m in items], reverse=True)
    calls = protocol(configured, monkeypatch, items, mode='split')
    row = create(configured, media_ids=ids);drain()
    assert [r['id'] for r in row['config']['media_refs']] == ids
    assert [r['id'] for r in calls[0][2]['attachments']] == ids
    assert [r['id'] for r in fetch(configured,row)['result']['attachment_coverage']] == ids
