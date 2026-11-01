"""Bounded, read-only execution relationships; never serialize business payloads."""
from pydantic import BaseModel
from sqlalchemy import select
from .models import ChatRun, Conversation, Memory, Message, ModelCall

NODE_LIMIT = 200
EDGE_LIMIT = 400
QUERY_LIMIT = 100
ROW_LIMIT = 50


class TraceNode(BaseModel):
    key: str
    kind: str
    id: str
    status: str
    created_at: float
    version: int | None = None
    sequence: int | None = None


class TraceEdge(BaseModel):
    source: str
    target: str
    relation: str
    recorded_version: int | None = None


class TraceGraph(BaseModel):
    root: str
    nodes: list[TraceNode]
    edges: list[TraceEdge]
    truncated: bool
    note: str


class Relationships:
    def __init__(self, db, user):
        self.db, self.user = db, user
        self.nodes, self.edges, self.visited, self.cache = {}, {}, set(), {}
        self.queries, self.truncated = 0, False

    def rows(self, query):
        if self.queries >= QUERY_LIMIT:
            self.truncated = True
            return []
        self.queries += 1
        rows = list(self.db.scalars(query.limit(ROW_LIMIT + 1)))
        self.truncated |= len(rows) > ROW_LIMIT
        return rows[:ROW_LIMIT]

    def scoped(self, entity):
        return select(entity).where(entity.workspace_id == self.user.workspace_id,
                                    entity.user_id == self.user.id)

    def get(self, entity, identity):
        if not isinstance(identity, str) or not identity or len(identity) > 64:
            return None
        key = (entity, identity)
        if key not in self.cache:
            self.cache[key] = next(iter(self.rows(self.scoped(entity).where(entity.id == identity))), None)
        return self.cache[key]

    def add(self, kind, row, *, status=None, sequence=None):
        key = kind + ':' + row.id
        if key not in self.nodes:
            if len(self.nodes) >= NODE_LIMIT:
                self.truncated = True
                return None
            self.nodes[key] = TraceNode(key=key, kind=kind, id=row.id,
                status=status if status is not None else row.status, created_at=row.created_at,
                version=getattr(row, 'version', None), sequence=sequence)
        return key

    def edge(self, source, target, relation, version=None):
        if not source or not target:
            return
        key = (source, target, relation, version)
        if key not in self.edges:
            if len(self.edges) >= EDGE_LIMIT:
                self.truncated = True
                return
            self.edges[key] = TraceEdge(source=source, target=target, relation=relation, recorded_version=version)

    def references(self, source, refs, entity, kind, relation):
        if not isinstance(refs, list):
            return
        self.truncated |= len(refs) > ROW_LIMIT
        for ref in refs[:ROW_LIMIT]:
            if not isinstance(ref, dict):
                continue
            row = self.get(entity, ref.get('id'))
            if row:
                version = ref.get('version')
                self.edge(source, self.add(kind, row), relation,
                          version if type(version) is int and version > 0 else None)

    def message(self, identity):
        # Message has no user_id. Ownership must be proved through its conversation.
        if not isinstance(identity, str) or not identity or len(identity) > 64:
            return None
        query = select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(
            Message.id == identity, Message.workspace_id == self.user.workspace_id,
            Conversation.workspace_id == self.user.workspace_id, Conversation.user_id == self.user.id)
        message = next(iter(self.rows(query)), None)
        return self.add('message', message) if message else None

    def incoming(self, row):
        from .channels import ChannelDelivery
        key = self.add('inbound', row)
        for delivery in self.rows(self.scoped(ChannelDelivery).where(ChannelDelivery.inbound_id == row.id)
                                  .order_by(ChannelDelivery.part, ChannelDelivery.id)):
            self.edge(key, self.add('delivery', delivery), 'protocol_delivery')
        return key

    def expand(self, kind, row):
        from .chat_actions import ChatAction
        from .delegation import AgentRun, AgentNode
        from .media import Media
        from .plugins import PluginInvocation
        from .tasks import WorkTask, TaskAttempt, Reminder, ReminderReceipt
        from .channels import ChannelInbound
        from .channel_tasks import ChannelTask

        key = self.add(kind, row)
        if not key or key in self.visited:
            return key
        self.visited.add(key)

        if getattr(row, 'conversation_id', None):
            conversation = self.get(Conversation, row.conversation_id)
            if conversation:
                self.edge(self.add('conversation', conversation, status='deleted' if conversation.deleted else 'active'), key, 'contains')
        if getattr(row, 'result_message_id', None):
            self.edge(key, self.message(row.result_message_id), 'result_message')
        self.references(key, getattr(row, 'memory_refs', []), Memory, 'memory', 'used_memory')
        self.references(key, row.config.get('media_refs', []), Media, 'media', 'used_media')

        condition = (ModelCall.run_id == row.id if kind == 'chat' else
            ModelCall.config[{'task':'task_id','agent':'agent_run_id','plugin':'invocation_id'}[kind]].as_string() == row.id)
        calls = self.rows(select(ModelCall).where(ModelCall.workspace_id == self.user.workspace_id, condition)
                          .order_by(ModelCall.created_at, ModelCall.id))
        for call in calls:
            self.edge(key, self.add('model_call', call), 'model_call')

        if kind == 'chat':
            self.truncated |= len(row.source_message_ids) > ROW_LIMIT
            for identity in row.source_message_ids[:ROW_LIMIT]:
                self.edge(self.message(identity), key, 'context_message')
            messages = self.rows(select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(
                Message.run_id == row.id, Message.conversation_id == row.conversation_id,
                Message.workspace_id == self.user.workspace_id, Conversation.workspace_id == self.user.workspace_id,
                Conversation.user_id == self.user.id).order_by(Message.created_at, Message.id))
            for message in messages:
                self.edge(key, self.add('message', message), 'run_message')
            for incoming in self.rows(self.scoped(ChannelInbound).where(ChannelInbound.run_id == row.id)
                                      .order_by(ChannelInbound.created_at, ChannelInbound.id)):
                self.edge(self.incoming(incoming), key, 'received_message')

        if kind in ('chat', 'task', 'agent'):
            # A shared conversation alone never establishes an execution relation.
            actions = self.rows(self.scoped(ChatAction).where(
                ChatAction.run_id == row.id if kind == 'chat' else ChatAction.target_id == row.id)
                .order_by(ChatAction.created_at, ChatAction.id))
            for action in actions:
                target_kind, entity = ({'agent.delegate': ('agent', AgentRun), 'project.plan': ('task', WorkTask),
                    'server.files': ('task', WorkTask), 'reminder.once': ('reminder', Reminder)}.get(action.capability, ('', None)))
                if entity is None or (kind != 'chat' and target_kind != kind):
                    continue
                parent, target = self.get(ChatRun, action.run_id), self.get(entity, action.target_id)
                if not parent or not target or parent.conversation_id != action.conversation_id or target.conversation_id != action.conversation_id:
                    continue
                parent_key = self.expand('chat', parent)
                if target_kind == 'reminder':
                    target_key = self.add('reminder', target)
                    for receipt in self.rows(self.scoped(ReminderReceipt).where(ReminderReceipt.reminder_id == target.id)
                                             .order_by(ReminderReceipt.created_at, ReminderReceipt.id)):
                        self.edge(target_key, self.add('reminder_receipt', receipt), 'reminder_receipt')
                else:
                    target_key = self.expand(target_kind, target)
                self.edge(parent_key, target_key, 'dispatched')

        if kind == 'task':
            for attempt in self.rows(select(TaskAttempt).where(TaskAttempt.task_id == row.id,
                TaskAttempt.workspace_id == self.user.workspace_id).order_by(TaskAttempt.number)):
                self.edge(key, self.add('attempt', attempt, sequence=attempt.number), 'attempted')
        elif kind == 'agent':
            nodes = self.rows(select(AgentNode).where(AgentNode.run_id == row.id,
                AgentNode.workspace_id == self.user.workspace_id).order_by(AgentNode.created_at, AgentNode.id))
            by_key = {node.key: self.add('agent_node', node) for node in nodes}
            for node in nodes:
                node_key = by_key[node.key]
                self.edge(key, node_key, 'delegated')
                self.truncated |= len(node.depends_on) > ROW_LIMIT
                for dependency in node.depends_on[:ROW_LIMIT]:
                    if isinstance(dependency, str):
                        self.edge(by_key.get(dependency), node_key, 'depends_on')
                self.references(node_key, node.context.get('memory_provenance', []), Memory, 'memory', 'used_memory')
                self.references(node_key, node.context.get('media_provenance', []), Media, 'media', 'used_media')
                for call in calls:
                    if call.config.get('agent_node_id') == node.id:
                        self.edge(node_key, self.add('model_call', call), 'model_call')
                invocation = self.get(PluginInvocation, node.context.get('invocation_id'))
                origin = invocation.config.get('agent_origin', {}) if invocation else {}
                if isinstance(origin, dict) and origin.get('run_id') == row.id and origin.get('node_id') == node.id:
                    self.edge(node_key, self.expand('plugin', invocation), 'tool_invocation')
            task = self.get(ChannelTask, row.config.get('qq_task_id'))
            if task and task.run_id == row.id:
                task_key = self.add('channel_task', task)
                self.edge(task_key, key, 'authorized_execution')
                incoming = self.get(ChannelInbound, task.inbound_id)
                if incoming and incoming.channel_id == task.channel_id:
                    self.edge(self.incoming(incoming), task_key, 'requested')
        elif kind == 'plugin':
            origin = row.config.get('agent_origin', {})
            parent = self.get(AgentRun, origin.get('run_id')) if isinstance(origin, dict) else None
            if parent:
                nodes = self.rows(select(AgentNode).where(AgentNode.id == origin.get('node_id'),
                    AgentNode.run_id == parent.id, AgentNode.workspace_id == self.user.workspace_id))
                if nodes and nodes[0].context.get('invocation_id') == row.id:
                    self.expand('agent', parent)
        return key


def trace_graph(db, user, kind, row):
    graph = Relationships(db, user)
    root = graph.expand(kind, row)
    return TraceGraph(root=root, nodes=list(graph.nodes.values()), edges=list(graph.edges.values()),
        truncated=graph.truncated,
        note='仅显示当前身份与工作区内的已记录关联，缺失关联不推测补全。'
             '节点显示当前记录状态，记忆关系标注当时引用版本；协议接受不代表已读。')
