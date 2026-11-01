"""Durable, bounded delegation. Model plans are data, never permission grants."""
import json
import re
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Entity, User, Conversation, Memory, Message
from .assistants import AssistantProfile, snapshot, seed_defaults, validate_model
from .web import Context, owned, view, audit
from .tasks import digest, scheduled_job
from .task_routes import serial_owner, matching, check_conversation

DONE = {'completed', 'partial', 'failed', 'cancelled', 'invalidated'}
NODE_DONE = {'succeeded', 'failed', 'blocked', 'cancelled'}


class AgentRun(Entity, Base):
    __tablename__ = 'agent_runs'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    round: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    memory_refs: Mapped[list] = mapped_column(JSON, default=list)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)
    max_calls: Mapped[int] = mapped_column(Integer, default=12)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class AgentNode(Entity, Base):
    __tablename__ = 'agent_nodes'
    __table_args__ = (UniqueConstraint('run_id', 'key'),)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    instruction: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(String(100))
    capability: Mapped[str] = mapped_column(String(60))
    resource_path: Mapped[str] = mapped_column(String(400), default='.')
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class AgentEvent(Entity, Base):
    __tablename__ = 'agent_events'
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class DelegationInput(Strict):
    request_id: str = Field(min_length=8, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=6000)
    conversation_id: str | None = None
    share_memory: bool = False
    propose_memory: bool = False
    allow_files: bool = False
    max_calls: int = Field(default=12, ge=3, le=30)
    tool_keys: list[str] = Field(default_factory=list, max_length=16)
    media_ids: list[str] = Field(default_factory=list, max_length=10)


class NodePlan(Strict):
    key: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,29}$')
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=2000)
    agent_id: str = Field(min_length=1, max_length=100)
    capability: Literal['reason', 'server.files', 'plugin.call'] = 'reason'
    tool_key: str = Field(default='', max_length=64)
    resource_path: str = Field(default='.', max_length=400)
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    media_ids: list[str] = Field(default_factory=list, max_length=10)


class DelegationPlan(Strict):
    mode: Literal['direct', 'delegate']
    reason: str = Field(min_length=1, max_length=800)
    nodes: list[NodePlan] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def graph(self):
        keys = {n.key for n in self.nodes}
        if len(keys) != len(self.nodes) or (self.mode == 'direct' and len(keys) != 1):
            raise ValueError('Invalid node keys or direct plan')
        pending = {n.key: set(n.depends_on) for n in self.nodes}
        if any(not deps <= keys or key in deps for key, deps in pending.items()):
            raise ValueError('Invalid dependencies')
        visited = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= visited}
            if not ready:
                raise ValueError('Cyclic graph')
            visited |= ready
            pending = {k: v for k, v in pending.items() if k not in ready}
        return self


class Finding(Strict):
    text: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class AnalysisResult(Strict):
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)


class MemoryProposal(Strict):
    content: str = Field(min_length=1, max_length=2000)
    category: Literal['profile', 'preferences', 'experiences', 'projects', 'relationships', 'procedures', 'support']
    subject: str = Field(default='unknown', min_length=1, max_length=150)
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class NodeAnalysis(AnalysisResult):
    memory_candidates: list[MemoryProposal] = Field(default_factory=list, max_length=3)


class Version(Strict):
    version: int = Field(ge=1)


class AgentRunView(BaseModel):
    id: str
    title: str
    goal: str
    status: str
    version: int
    decision: dict
    calls_used: int
    max_calls: int
    config: dict
    memory_refs: list[dict]
    conversation_id: str | None
    result_message_id: str | None
    result: dict
    error: str | None
    created_at: float
    updated_at: float


class AgentNodeView(BaseModel):
    id: str
    key: str
    title: str
    instruction: str
    agent_id: str
    capability: str
    depends_on: list[str]
    config: dict
    context: dict
    status: str
    attempt: int
    result: dict
    error: str | None


class AgentRunDetail(AgentRunView):
    nodes: list[AgentNodeView]
    events: list[dict]


class AgentRunPage(BaseModel):
    items: list[AgentRunView]
    next_cursor: str | None


def event(db, run, kind, node=None, **data):
    run.version += 1
    run.updated_at = now()
    db.add(AgentEvent(workspace_id=run.workspace_id, run_id=run.id,
                      node_id=node.id if node else None, kind=kind, data=data))


def parse_json(text, schema):
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
    try:
        return schema.model_validate(json.loads(text))
    except (ValueError, TypeError):
        raise HTTPException(502, '模型返回的协作结构无效，未记为成功，请检查结构化输出能力') from None


def registry(db, user):
    """Config selection narrows grants. Defaults combine matching preset roles/personas."""
    from .gateway import resolve_model
    seed_defaults(db)
    profiles = list(db.scalars(select(AssistantProfile).where(AssistantProfile.user_id == user.id,
        AssistantProfile.workspace_id == user.workspace_id, AssistantProfile.enabled == True).order_by(AssistantProfile.id)))
    roles = [p for p in profiles if p.kind == 'role']
    personas = [p for p in profiles if p.kind == 'persona']
    specs = []
    for agent in [p for p in profiles if p.kind == 'agent']:
        role = next((r for r in roles if r.id == agent.spec['role_id']), None)
        persona = next((p for p in personas if p.id == agent.spec['persona_id']), None)
        if role and persona:
            specs.append((agent.id, agent.name, role, persona, agent))
    for role in roles:
        persona = next((p for p in personas if p.name == role.name), None)
        if persona:
            specs.append(('preset:'+role.id, role.name, role, persona, None))
    result = []
    for identity, name, role, persona, agent in specs[:30]:
        model_id = (agent.spec.get('model_id') if agent else None) or persona.spec.get('model_id')
        try:
            validate_model(db, model_id, user)
            model, provider, policy = resolve_model(db, 'chat', model_id)
            if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
                continue
        except HTTPException:
            continue
        files_allowed = False
        if 'server.files' in role.spec.get('tools', []):
            try:
                from .plugins import require_capability
                _, files_release, _ = require_capability(db, user, 'server.files')
                from .resources import current as resource_policy
                files_allowed = resource_policy(db, user).available
            except HTTPException:
                pass
        result.append({'id': identity, 'name': name, 'role': snapshot(role), 'persona': snapshot(persona),
            'agent': snapshot(agent) if agent else None, 'model_id': model.id, 'model': model.name,
            'provider_version': provider.version, 'policy_version': policy,
            'capabilities': ['reason'] + (['server.files'] if files_allowed else [])
                + (['plugin.call'] if 'plugin.call' in role.spec.get('tools', []) else []),
            'plugin_id': 'lingxi.builtin', 'plugin_version': '1.0.0',
            'plugin_release_id': files_release.id if files_allowed else None})
    from .plugin_agents import registry as plugin_registry
    return result + plugin_registry(db, user)


def revoke_run(db, run, reason, invalidated=False):
    if run.status == 'invalidated' or (run.status == 'cancelled' and not invalidated):
        return
    run.cancel_requested, run.status, run.error = True, 'invalidated' if invalidated else 'cancelled', reason
    from .agent_memory import invalidate_pending
    invalidate_pending(db,run.id)
    from .agent_tools import cancel_calls
    cancel_calls(db, run, reason, invalidated)
    if invalidated:
        run.result = {}
    for node in db.scalars(select(AgentNode).where(AgentNode.run_id == run.id).with_for_update()):
        if node.status not in NODE_DONE or invalidated:
            node.status, node.error = 'cancelled', reason
        if invalidated:
            node.context, node.result = {}, {}
    if invalidated and run.result_message_id:
        message = db.get(Message, run.result_message_id)
        if message:
            message.status, message.content = 'invalidated', ''
            from .memory import invalidate_source
            invalidate_source(db, message.id)
    event(db, run, run.status)
    if invalidated:
        from .chat_actions import for_target
        action = for_target(db, run.id)
        if action:
            from .models import ChatRun
            from .memory import invalidate_run
            parent = db.get(ChatRun, action.run_id)
            if parent:
                invalidate_run(db, parent)


def invalidate_delegations(db, memory_id=None, conversation_id=None, media_id=None):
    if media_id:
        from .agent_memory import invalidate_derived
        invalidate_derived(db,'media',media_id)
    query = select(AgentRun).where(AgentRun.status != 'invalidated')
    if conversation_id:
        query = query.where(AgentRun.conversation_id == conversation_id)
    for run in db.scalars(query.with_for_update()):
        if (conversation_id or any(ref['id'] == memory_id for ref in run.memory_refs) or
                any(ref['id'] == media_id for ref in run.config.get('media_refs', []))):
            revoke_run(db, run, '任务依据已纠正、删除或到期，请重新提交', invalidated=True)


def expire_delegations(db, user_id):
    from .resources import expire
    if user := db.get(User, user_id):
        expire(db, user)
    from .agent_tools import expire_runs
    expire_runs(db, user_id)
    from .memory import usable
    for run in db.scalars(select(AgentRun).where(AgentRun.user_id == user_id,
                                                AgentRun.status.not_in(['invalidated','cancelled']))):
        from .agent_media import load
        from .agent_worker import AgentStopped
        try:
            load(db, run)
        except AgentStopped:
            revoke_run(db, run, '协作附件已删除、变化或超出授权范围', invalidated=True)
            continue
        for ref in run.memory_refs:
            memory = db.get(Memory, ref['id'], populate_existing=True)
            if not usable(memory) or memory.version != ref['version']:
                invalidate_delegations(db, memory_id=ref['id'])
                break


router = APIRouter()


@router.get('/api/agents')
def available_agents(ctx: Context):
    db, user, _ = ctx
    return [{'id': a['id'], 'name': a['name'], 'role': a['role']['name'],
             'model': a['model'], 'capabilities': a['capabilities'],
             'plugin_id': a['plugin_id'] if a.get('plugin_role') else None,
             'plugin_version': a['plugin_version'] if a.get('plugin_role') else None} for a in registry(db, user)]


@router.post('/api/agent-runs', response_model=AgentRunView)
def create_run(body: DelegationInput, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    hashed = digest(body.model_dump(exclude=({'propose_memory'} if not body.propose_memory else set()) | ({'tool_keys'} if not body.tool_keys else set()) |
        ({'media_ids'} if not body.media_ids else set())))
    existing = db.scalar(select(AgentRun).where(AgentRun.user_id == user.id, AgentRun.request_id == body.request_id,
                                               AgentRun.workspace_id == user.workspace_id))
    if existing:
        return view(matching(existing, hashed))
    check_conversation(db, body.conversation_id, user)
    if body.conversation_id:
        from .channels import ChannelSession
        if db.scalar(select(ChannelSession).where(ChannelSession.conversation_id == body.conversation_id)):
            raise HTTPException(409, '本批协作仅回传 Web 会话，QQ 回传尚未接入')
    from .agent_media import select_media, refs_for
    attachments = select_media(db, body.media_ids, user)
    agents = registry(db, user)
    if not agents:
        raise HTTPException(409, '没有可用助手，请先验证并选用对话模型，启用人格与执行角色')
    from .agent_tools import select_tools
    tools = select_tools(db, user, body.tool_keys)
    if tools and not any('plugin.call' in a['capabilities'] for a in agents):
        raise HTTPException(409, '所选工具需要至少一个启用 plugin.call 候选工具的执行角色')
    from .gateway import resolve_model
    model, provider, policy = resolve_model(db, 'chat')
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(409, '主助手模型不属于当前工作区')
    from .resources import snapshot as resource_snapshot
    resources = {'resource_policy': resource_snapshot(db, user)} if body.allow_files else {}
    run = AgentRun(user_id=user.id, workspace_id=user.workspace_id, request_id=body.request_id,
        request_hash=hashed, title=body.title, goal=body.goal, conversation_id=body.conversation_id,
        max_calls=body.max_calls, config={'agents': agents, 'share_memory': body.share_memory, 'propose_memory':body.propose_memory,
            'allow_files': body.allow_files, **resources, 'model_id': model.id, 'model': model.name,
            'provider_version': provider.version, 'policy_version': policy, 'prompt_version': 'delegation-v1',
            'max_nodes': 8, 'max_parallel': 3, 'max_depth': 1, 'tools': tools,
            **({'media_refs':refs_for(attachments), 'media_contract':'delegation-media-v1'} if attachments else {})})
    db.add(run)
    db.flush()
    event(db, run, 'queued')
    scheduled_job(db, 'agent-plan:'+run.id, 'agent_plan', {'run_id':run.id}, workspace_id=user.workspace_id)
    audit(db, user, 'delegation_create', run.id)
    return view(run)


@router.get('/api/agent-runs', response_model=AgentRunPage)
def list_runs(ctx: Context, q: str = Query('', max_length=200), cursor: str | None = None,
              limit: int = Query(30, ge=1, le=100)):
    from .task_routes import page_query
    db, user, _ = ctx
    expire_delegations(db, user.id)
    rows, cursor = page_query(db, AgentRun, user, cursor,
        select(AgentRun).where(AgentRun.title.icontains(q, autoescape=True)), limit)
    from .chat_actions import for_target, valid_origin
    return {'items':[view(row) for row in rows
                     if not (action := for_target(db, row.id)) or valid_origin(db, action)], 'next_cursor':cursor}


@router.get('/api/agent-runs/{identity}', response_model=AgentRunDetail)
def detail(identity: str, ctx: Context):
    db, user, _ = ctx
    from .agent_tools import expire_runs
    expire_runs(db, user.id)
    run = owned(db, AgentRun, identity, user)
    from .chat_actions import require_origin
    require_origin(db, run.id)
    from .agent_media import load
    from .agent_worker import AgentStopped
    try:
        load(db, run)
    except AgentStopped:
        revoke_run(db, run, '协作附件已删除、变化或超出授权范围', invalidated=True)
    # Expiry also redacts completed derivative results when viewed.
    from .memory import usable
    if any(not usable(m := db.get(Memory, r['id'])) or m.version != r['version'] for r in run.memory_refs):
        invalidate_delegations(db, memory_id=next(r['id'] for r in run.memory_refs
            if not usable(m := db.get(Memory, r['id'])) or m.version != r['version']))
    require_origin(db, run.id)
    return view(run) | {'nodes':[view(n) for n in db.scalars(select(AgentNode).where(AgentNode.run_id == run.id).order_by(AgentNode.created_at, AgentNode.key))],
        'events':[view(e) for e in db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.created_at, AgentEvent.id))]}


@router.post('/api/agent-runs/{identity}/cancel', response_model=AgentRunView)
def cancel_run(identity: str, body: Version, ctx: Context):
    db, user, _ = ctx
    run = owned(db, AgentRun, identity, user, lock=True)
    from .chat_actions import require_origin
    require_origin(db, run.id)
    if run.version != body.version:
        raise HTTPException(409, '任务状态已变化，请重新加载后操作')
    if run.status not in DONE:
        revoke_run(db, run, '用户取消协作；已产生的模型费用无法撤销')
        audit(db, user, 'delegation_cancel', run.id)
    return view(run)


@router.post('/api/agent-runs/{identity}/retry', response_model=AgentRunView)
def retry_run(identity: str, body: Version, ctx: Context):
    db, user, _ = ctx
    run = owned(db, AgentRun, identity, user, lock=True)
    from .chat_actions import require_origin
    require_origin(db, run.id)
    if run.version != body.version:
        raise HTTPException(409, '任务状态已变化，请重新加载后操作')
    nodes = list(db.scalars(select(AgentNode).where(AgentNode.run_id == run.id)))
    if run.status not in {'failed', 'partial'} or not nodes:
        raise HTTPException(409, '仅已形成依赖图的失败协作可重试；规划失败请新建任务')
    if run.calls_used >= run.max_calls:
        raise HTTPException(409, '模型调用预算已耗尽，请缩小目标后新建任务')
    from .agent_worker import validate_sources, validate_profiles, schedule
    from .agent_worker import AgentStopped
    try:
        validate_sources(db, run, lock=False)
        validate_profiles(db, run)
    except AgentStopped:
        raise HTTPException(409, '依据或助手配置已失效，请重新创建任务') from None
    if any(n.capability == 'plugin.call' and n.context.get('invocation_id') for n in nodes):
        raise HTTPException(409, '协作已有关联远程调用，不能重试并重放外部操作；请先核对运行记录')
    for node in nodes:
        if node.status in {'failed', 'blocked', 'cancelled'}:
            node.status, node.error, node.result = 'pending', None, {}
    run.status, run.error, run.result = 'running', None, {}
    from .agent_memory import invalidate_pending
    invalidate_pending(db,run.id)
    if run.result_message_id:
        message = db.get(Message, run.result_message_id)
        if message:
            message.status = 'superseded'
        run.result_message_id = None
    run.round += 1
    event(db, run, 'retry', note='保留已成功节点与固定版本；只读操作，模型调用可能产生费用')
    schedule(db, run)
    audit(db, user, 'delegation_retry', run.id)
    return view(run)
