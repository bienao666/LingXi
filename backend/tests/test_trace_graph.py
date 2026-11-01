import pytest
from sqlalchemy import event
from fastapi.testclient import TestClient
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.models import ChatRun, Conversation, Memory, Message, ModelCall, User
from lingxi.chat_actions import ChatAction
from lingxi.delegation import AgentRun, AgentNode
from lingxi.plugins import PluginInvocation
from lingxi.tasks import WorkTask, TaskAttempt, Reminder, ReminderReceipt
from lingxi.media import Media
from lingxi.channels import ChannelInbound, ChannelDelivery
from lingxi.channel_tasks import ChannelTask
from lingxi.trace_graph import trace_graph


def chat(identity='chat', **values):
    return ChatRun(id=identity, user_id='owner', conversation_id='conversation', request_id=identity,
        request_hash='hash', model_id='model', **values)


def agent(identity='agent', **values):
    return AgentRun(id=identity, user_id='owner', request_id=identity, request_hash='hash',
        title='SECRET title', goal='SECRET goal', conversation_id='conversation', **values)


def plugin(identity='plugin', **values):
    return PluginInvocation(id=identity, user_id='owner', request_id=identity, request_hash='hash',
        installation_id='install', release_id='release', capability_id='capability', input='SECRET input',
        result='SECRET output', receipt={'body':'SECRET receipt'}, **values)


def detail(client, kind='chat', identity='chat'):
    response = client.get(f'/api/admin/traces/{kind}/{identity}')
    assert response.status_code == 200, response.text
    assert 'SECRET' not in response.text
    graph = response.json()['graph']
    keys = {node['key'] for node in graph['nodes']}
    assert graph['root'] == kind+':'+identity
    assert all(edge['source'] in keys and edge['target'] in keys for edge in graph['edges'])
    return graph, {node['key']: node for node in graph['nodes']}


def test_trace_graph_requires_auth_and_owned_root(client):
    with SessionLocal.begin() as db:
        db.add(chat(workspace_id='other'))
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/admin/traces/chat/chat').status_code == 401
    assert client.get('/api/admin/traces/chat/chat').status_code == 404


def test_graph_connects_messages_versions_media_and_actual_calls_without_payload(client):
    with SessionLocal.begin() as db:
        db.add(Conversation(id='conversation', user_id='owner', title='SECRET title'))
        db.add(chat(memory_refs=[{'id':'memory','version':1}], source_message_ids=['source'],
            config={'media_refs':[{'id':'media','version':2}], 'prompt':'SECRET prompt'}))
        db.add(Message(id='source', conversation_id='conversation', role='user', run_id='chat', content='SECRET user'))
        db.add(Message(id='output', conversation_id='conversation', role='assistant', run_id='chat', content='SECRET answer'))
        db.add(Memory(id='memory', user_id='owner', content='SECRET fact', version=3, status='deleted'))
        db.add(Media(id='media', user_id='owner', request_id='media', filename='SECRET.txt', suffix='.txt', digest='hash', version=2))
        db.add(ModelCall(id='call', model_id='model', run_id='chat', purpose='chat', status='completed', duration_ms=2,
            config={'prompt':'SECRET prompt'}, error='SECRET error'))
        db.add(chat('unrelated'))
    graph, nodes = detail(client)
    assert set(nodes) == {'chat:chat','conversation:conversation','message:source','message:output',
                          'memory:memory','media:media','model_call:call'}
    assert nodes['memory:memory']['version'] == 3 and nodes['memory:memory']['status'] == 'deleted'
    assert next(e for e in graph['edges'] if e['relation']=='used_memory')['recorded_version'] == 1
    assert graph['truncated'] is False


def test_shared_conversation_never_implies_an_execution_relation(client):
    with SessionLocal.begin() as db:
        db.add(chat())
        db.add(agent())
        db.add(WorkTask(id='task',user_id='owner',request_id='task',request_hash='hash',title='SECRET',skill_id='project.plan',input={},conversation_id='conversation'))
    _, nodes = detail(client)
    assert set(nodes) == {'chat:chat'}


@pytest.mark.parametrize('root', ['chat','agent','plugin'])
def test_bidirectional_dispatch_node_dependencies_and_reciprocal_tools(client, root):
    with SessionLocal.begin() as db:
        db.add(chat())
        db.add(agent())
        db.add(ChatAction(user_id='owner',run_id='chat',conversation_id='conversation',source_id='source',capability='agent.delegate',target_id='agent'))
        db.add(AgentNode(id='first',run_id='agent',key='first',title='SECRET',instruction='SECRET',agent_id='research',capability='reason'))
        db.add(AgentNode(id='second',run_id='agent',key='second',title='SECRET',instruction='SECRET',agent_id='executor',
            capability='plugin.call',depends_on=['first'],context={'invocation_id':'plugin'}))
        db.add(plugin(config={'agent_origin':{'run_id':'agent','node_id':'second'},'token':'SECRET'}))
        db.add(ModelCall(id='nodecall',model_id='model',purpose='chat',status='completed',duration_ms=2,
            config={'agent_run_id':'agent','agent_node_id':'first'}))
        db.add(ModelCall(id='plugincall',model_id='model',purpose='plugin',status='completed',duration_ms=2,config={'invocation_id':'plugin'}))
    graph, nodes = detail(client,root,root)
    assert {'chat:chat','agent:agent','agent_node:first','agent_node:second','plugin:plugin','model_call:nodecall','model_call:plugincall'} <= set(nodes)
    relationships = {(e['source'],e['target'],e['relation']) for e in graph['edges']}
    assert ('chat:chat','agent:agent','dispatched') in relationships
    assert ('agent_node:first','agent_node:second','depends_on') in relationships
    assert ('agent_node:second','plugin:plugin','tool_invocation') in relationships
    assert ('agent_node:first','model_call:nodecall','model_call') in relationships
    assert len(relationships) == len(graph['edges'])


def test_scope_checks_on_every_relation_and_nonreciprocal_plugin_origin(client):
    with SessionLocal.begin() as db:
        db.add(chat(memory_refs=[{'id':'foreign-memory','version':1}], source_message_ids=['foreign-message'],
            config={'media_refs':[{'id':'foreign-media','version':1}]}))
        db.add(Conversation(id='foreign-conversation', user_id='other'))
        db.add(Message(id='foreign-message',conversation_id='foreign-conversation',role='user',content='SECRET'))
        db.add(Memory(id='foreign-memory',user_id='other',content='SECRET'))
        db.add(Media(id='foreign-media',workspace_id='other',user_id='owner',request_id='media',filename='SECRET',suffix='.txt',digest='hash'))
        db.add(ModelCall(id='foreign-call',workspace_id='other',model_id='model',run_id='chat',purpose='chat',status='completed',duration_ms=1))
        db.add(agent())
        db.add(AgentNode(id='node',run_id='agent',key='node',title='SECRET',instruction='SECRET',agent_id='research',capability='reason'))
        db.add(plugin(config={'agent_origin':{'run_id':'agent','node_id':'node'}}))
        db.add(ChatAction(user_id='other',run_id='chat',conversation_id='conversation',source_id='source',capability='agent.delegate',target_id='agent'))
    _, nodes = detail(client)
    assert set(nodes) == {'chat:chat'}
    _, nodes = detail(client,'plugin','plugin')
    assert set(nodes) == {'plugin:plugin'}


def test_agent_omits_foreign_nodes_tools_and_evidence(client):
    with SessionLocal.begin() as db:
        db.add(agent())
        db.add(AgentNode(id='foreign-node',workspace_id='other',run_id='agent',key='foreign',title='SECRET',instruction='SECRET',agent_id='x',capability='reason'))
        db.add(AgentNode(id='node',run_id='agent',key='node',title='SECRET',instruction='SECRET',agent_id='x',capability='plugin.call',
            context={'invocation_id':'foreign-plugin','memory_provenance':[{'id':'foreign-memory','version':1}]}))
        db.add(plugin('foreign-plugin',workspace_id='other',config={'agent_origin':{'run_id':'agent','node_id':'node'}}))
        db.add(Memory(id='foreign-memory',user_id='different',content='SECRET'))
    _, nodes = detail(client,'agent','agent')
    assert set(nodes) == {'agent:agent','agent_node:node'}


def test_task_attempts_receipts_are_metadata_only_and_can_find_origin(client):
    with SessionLocal.begin() as db:
        db.add(chat())
        db.add(WorkTask(id='task',user_id='owner',request_id='task',request_hash='hash',title='SECRET',skill_id='project.plan',input={},conversation_id='conversation'))
        db.add(ChatAction(user_id='owner',run_id='chat',conversation_id='conversation',source_id='source',capability='project.plan',target_id='task'))
        db.add(TaskAttempt(id='attempt',task_id='task',number=1,lease_token='SECRET',status='succeeded',receipt={'content':'SECRET'}))
    graph, nodes = detail(client,'task','task')
    assert nodes['attempt:attempt']['sequence'] == 1
    assert {'task:task','chat:chat','attempt:attempt'} == set(nodes)


def test_reminder_dispatch_and_receipt(client):
    with SessionLocal.begin() as db:
        db.add(chat())
        db.add(Reminder(id='reminder',user_id='owner',request_id='reminder',request_hash='hash',title='SECRET',content='SECRET',due_at=now()+100,timezone='UTC',conversation_id='conversation'))
        db.add(ChatAction(user_id='owner',run_id='chat',conversation_id='conversation',source_id='source',capability='reminder.once',target_id='reminder'))
        db.add(ReminderReceipt(id='receipt',user_id='owner',reminder_id='reminder',reminder_version=1))
    graph, nodes = detail(client)
    assert {'reminder:reminder','reminder_receipt:receipt'} <= set(nodes)
    assert any(e['relation']=='reminder_receipt' for e in graph['edges'])


def test_qq_protocol_acceptance_is_not_read_confirmation(client):
    with SessionLocal.begin() as db:
        db.add(agent(config={'qq_task_id':'channel-task'}))
        db.add(ChannelTask(id='channel-task',user_id='owner',channel_id='channel',inbound_id='inbound',actor_id='qq-actor',
            conversation_id='qq-conversation',source_id='source',source_hash='hash',goal='SECRET',run_id='agent',expires_at=now()+100))
        db.add(ChannelInbound(id='inbound',user_id='owner',channel_id='channel',channel_version=1,external_message_id='SECRET',
            target_key='SECRET',sender_id='SECRET',content='SECRET',run_id='actor-run'))
        db.add(ChannelDelivery(id='delivery',user_id='owner',channel_id='channel',inbound_id='inbound',part=1,content='SECRET',
            status='accepted',echo='SECRET',protocol_message_id='SECRET',encrypted_audio='SECRET'))
        db.add(chat('actor-run'))
    graph, nodes = detail(client,'agent','agent')
    assert {'channel_task:channel-task','inbound:inbound','delivery:delivery'} <= set(nodes)
    assert 'chat:actor-run' not in nodes
    assert nodes['delivery:delivery']['status'] == 'accepted' and '已读' in graph['note']


def test_bounds_are_explicit_stable_and_queries_never_exceed_budget(client):
    with SessionLocal.begin() as db:
        db.add(WorkTask(id='task',user_id='owner',request_id='task',request_hash='hash',title='SECRET',skill_id='project.plan',input={}))
        for i in range(65):
            db.add(TaskAttempt(id=f'attempt-{i:03}',task_id='task',number=i+1,lease_token='SECRET'))
        db.add(chat(memory_refs=[{'id':f'memory-{i}','version':1} for i in range(200)]))
    graph, nodes = detail(client,'task','task')
    assert graph['truncated'] and len([n for n in nodes.values() if n['kind']=='attempt']) == 50
    assert detail(client,'task','task')[0] == graph
    queries=[]
    def record(*args): queries.append(1)
    with SessionLocal() as db:
        row, user = db.get(ChatRun,'chat'), db.get(User,'owner')
        event.listen(engine,'before_cursor_execute',record)
        try:
            result=trace_graph(db,user,'chat',row)
        finally:
            event.remove(engine,'before_cursor_execute',record)
    assert result.truncated and len(queries) <= 100
    assert len(result.nodes) <= 200 and len(result.edges) <= 400


def test_wrong_capability_or_conversation_does_not_create_false_dispatch(client):
    with SessionLocal.begin() as db:
        db.add(chat())
        db.add(agent())
        db.add(ChatAction(user_id='owner',run_id='chat',conversation_id='different',source_id='source',capability='agent.delegate',target_id='agent'))
    _, nodes = detail(client)
    assert set(nodes) == {'chat:chat'}


@pytest.mark.parametrize('constant,limit', [('QUERY_LIMIT',2),('NODE_LIMIT',2),('EDGE_LIMIT',1)])
def test_each_graph_budget_is_enforced_without_dangling_edges(client, monkeypatch, constant, limit):
    import importlib
    module = importlib.import_module('lingxi.trace_graph')
    monkeypatch.setattr(module,constant,limit)
    with SessionLocal.begin() as db:
        db.add(Conversation(id='conversation', user_id='owner'))
        db.add(chat())
        for i in range(5):
            db.add(ModelCall(id=f'call-{i}',model_id='model',run_id='chat',purpose='chat',status='completed',duration_ms=1))
    graph, nodes = detail(client)
    assert graph['truncated']
    if constant == 'NODE_LIMIT': assert len(nodes) <= limit
    if constant == 'EDGE_LIMIT': assert len(graph['edges']) <= limit
