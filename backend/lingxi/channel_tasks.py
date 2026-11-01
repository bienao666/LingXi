"""Per-request QQ delegation. Only an authenticated Web owner can authorize execution.

The channel actor owns the source and attachments; the owner owns the execution.
Those principals must never be conflated, particularly for group conversations.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now
from .models import Entity, User, Conversation, Message, ChatRun
from .channels import Channel, ChannelInbound, ChannelDelivery, ChannelSession, IdentityLink
from .web import Context, owned, audit
from .tasks import digest, scheduled_job, FileListInput

router = APIRouter()
STOPPED = {'invalidated', 'rejected', 'cancelled', 'expired'}


class ChannelTask(Entity, Base):
    __tablename__ = 'channel_tasks'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    inbound_id: Mapped[str] = mapped_column(String(64), unique=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    goal: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    media_refs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    notifications: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    agent_ids: list[str] = Field(min_length=1, max_length=8)
    tool_keys: list[str] = Field(default_factory=list, max_length=16)
    share_memory: bool = False
    resource_path: str | None = Field(default=None, max_length=300)
    max_calls: int = Field(default=12, ge=3, le=30)


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class TaskVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)


class ChannelTaskView(BaseModel):
    id: str
    channel_id: str
    target_key: str
    sender_id: str
    goal: str
    status: str
    version: int
    run_id: str | None
    run_status: str | None
    expires_at: float
    created_at: float
    error: str | None
    notifications: dict


class ChannelTaskPreviewView(BaseModel):
    version: int
    digest: str
    expires_at: float
    plan: dict


def command(text):
    """Only an explicit text command, never quoted prose or an ASR transcription."""
    if text == '/协作':
        return ''
    return text[3:].strip() if text.startswith(('/协作 ', '/协作\n')) else None


def source_valid(db, task):
    from .channel_gateway import channel_scope_valid
    if task.status in STOPPED or task.expires_at <= now() or not channel_scope_valid(db, task.scope):
        return False
    incoming = db.get(ChannelInbound, task.inbound_id, populate_existing=True)
    conversation = db.get(Conversation, task.conversation_id, populate_existing=True)
    source = db.get(Message, task.source_id, populate_existing=True)
    channel = db.get(Channel, task.channel_id)
    owner = db.get(User, task.user_id)
    if (not incoming or incoming.status == 'cancelled' or incoming.channel_id != task.channel_id or incoming.user_id != task.user_id or
            incoming.target_key != task.scope['target_key'] or incoming.sender_id != task.scope['sender_id'] or
            not channel or channel.user_id != task.user_id or channel.workspace_id != task.workspace_id or
            not owner or not owner.password_hash or owner.workspace_id != task.workspace_id or
            not conversation or conversation.deleted or conversation.user_id != task.actor_id or
            conversation.workspace_id != task.workspace_id or not source or source.role != 'user' or
            source.status != 'completed' or source.conversation_id != conversation.id or
            source.workspace_id != task.workspace_id or digest(source.content) != task.source_hash):
        return False
    if not db.scalar(select(ChannelSession.id).where(ChannelSession.channel_id == task.channel_id,
            ChannelSession.actor_id == task.actor_id, ChannelSession.conversation_id == task.conversation_id)):
        return False
    from .media import Media
    for ref in task.media_refs:
        item = db.get(Media, ref['id'], populate_existing=True)
        if (not item or item.version != ref['version'] or item.status != 'ready' or
                item.user_id != task.actor_id or item.workspace_id != task.workspace_id or
                item.parsed.get('kind') not in ('image', 'document')):
            return False
    return True


def require_origin(db, run):
    identity = run.config.get('qq_task_id')
    if not identity:
        return None
    task = db.get(ChannelTask, identity, populate_existing=True)
    if (not task or task.run_id != run.id or task.user_id != run.user_id or
            task.workspace_id != run.workspace_id or not source_valid(db, task) or
            run.config.get('media_actor_id') != task.actor_id or run.config.get('media_refs', []) != task.media_refs or
            (run.config.get('share_memory') and (task.scope['target_key'].startswith('group:') or
                task.actor_id != task.user_id or not task.scope.get('memory_allowed')))):
        raise HTTPException(410, 'QQ 协作来源、接收范围或授权已失效')
    return task


def delivery_valid(db, chat):
    ref = chat.config.get('qq_task')
    if not ref:
        return True
    task = db.get(ChannelTask, ref['id'], populate_existing=True)
    if not task or not source_valid(db, task):
        return False
    if ref['kind'] == 'request':
        return True
    from .delegation import AgentRun
    run = db.get(AgentRun, task.run_id, populate_existing=True) if task.run_id else None
    if not run or run.status == 'invalidated':
        return False
    try:
        from .agent_worker import validate_sources, validate_profiles
        validate_sources(db, run, lock=False)
        validate_profiles(db, run)
    except HTTPException:
        return False
    # A result notification is pinned to one round; a retry must not revive it.
    return run.round == ref['round']


def receive(db, channel, incoming, actor, conversation, share, media_ids, audio):
    goal = command(incoming.content)
    if goal is None:
        return False
    # Mixed speech is unconfirmed; it must not acquire a background task grant.
    if not goal or audio:
        text = '请以明确文字发送“/协作 任务目标”，不要混入未确认语音；任务需要在 Web 渠道规则中逐次确认。'
        make_reply(db, incoming, actor.id, conversation.id, text)
        return True
    count = len(list(db.scalars(select(ChannelTask.id).where(ChannelTask.channel_id == channel.id,
        ChannelTask.status == 'pending', ChannelTask.expires_at > now()).limit(20))))
    if count >= 20:
        make_reply(db, incoming, actor.id, conversation.id, '待确认协作已满，请管理员先在 Web 处理，未创建执行任务。')
        return True
    from .agent_media import select_media, refs_for
    media = select_media(db, media_ids, actor)
    source = Message(workspace_id=actor.workspace_id, conversation_id=conversation.id,
                     role='user', content=incoming.content, status='completed')
    db.add(source)
    db.flush()
    from .media import MediaLink
    for item in media:
        db.add(MediaLink(message_id=source.id, media_id=item.id))
    task = ChannelTask(user_id=channel.user_id, workspace_id=channel.workspace_id, channel_id=channel.id,
        inbound_id=incoming.id, actor_id=actor.id, conversation_id=conversation.id, source_id=source.id,
        source_hash=digest(source.content), goal=goal, media_refs=refs_for(media), expires_at=now()+86400,
        scope={'id':channel.id, 'version':incoming.channel_version, 'binding_id':incoming.binding_id,
            'binding_version':incoming.binding_version, 'target_key':incoming.target_key,
            'sender_id':incoming.sender_id, 'memory_allowed':share})
    db.add(task)
    db.flush()
    make_reply(db, incoming, actor.id, conversation.id,
        '协作请求已记录，等待管理员在 Web「渠道规则 → 后台协作」核对并确认；尚未调用模型或执行工具。'
        '请求 24 小时内有效，确认后的结果仅返回本会话。', task=task, kind='request', source=source)
    return True


def make_reply(db, incoming, actor_id, conversation_id, content, *, task=None, kind='', source=None, run=None):
    from .channel_gateway import add_deliveries
    chat = ChatRun(user_id=actor_id, workspace_id=incoming.workspace_id, conversation_id=conversation_id,
        request_id='qq-task:'+incoming.id, request_hash=digest(content), status='completed', model_id='qq-task-control',
        memory_status='disabled', memory_refs=list(run.memory_refs) if run else [],
        source_message_ids=[source.id] if source else [],
        config={'channel': {'id':incoming.channel_id, 'version':incoming.channel_version,
            'binding_id':incoming.binding_id, 'binding_version':incoming.binding_version},
            **({'qq_task':{'id':task.id,'kind':kind,'round':run.round if run else 0}} if task else {})})
    db.add(chat)
    db.flush()
    db.add(Message(workspace_id=incoming.workspace_id, conversation_id=conversation_id, role='assistant',
                   content=content, status='completed', run_id=chat.id))
    incoming.run_id, incoming.status = chat.id, 'ready'
    add_deliveries(db, incoming, content)
    return chat


def plan_for(db, task, selection):
    if not source_valid(db, task):
        raise HTTPException(410, '请求来源已删除、过期或渠道授权已变化')
    user = db.get(User, task.user_id)
    if selection.share_memory and (task.scope['target_key'].startswith('group:') or
            task.actor_id != task.user_id or not task.scope.get('memory_allowed')):
        raise HTTPException(403, '群聊及未允许个人记忆的私聊不能共享所有者记忆')
    from .delegation import registry
    directory = {a['id']:a for a in registry(db, user)}
    if len(set(selection.agent_ids)) != len(selection.agent_ids) or not set(selection.agent_ids) <= directory.keys():
        raise HTTPException(409, '所选助手已变化或不可用，请重新选择')
    agents = [directory[i] for i in selection.agent_ids]
    from .agent_tools import select_tools
    tools = select_tools(db, user, selection.tool_keys)
    if tools and not any('plugin.call' in a['capabilities'] for a in agents):
        raise HTTPException(409, '所选助手未声明远程工具能力')
    path = None
    if selection.resource_path is not None:
        try:
            path = FileListInput(path=selection.resource_path).path
        except ValueError:
            raise HTTPException(422, '共享目录须为服务器授权目录内的相对子目录') from None
        if path == '.':
            raise HTTPException(422, 'QQ 协作须选择明确的共享子目录，不允许共享服务器资料根目录')
        if not any('server.files' in a['capabilities'] for a in agents):
            raise HTTPException(409, '所选助手没有资料清单能力')
        from .resources import snapshot
        resource_grant = snapshot(db, user, path)
        from .tasks import list_resource
        def check_source():
            if not source_valid(db, task):
                raise HTTPException(410, 'QQ 请求来源或授权已变化')
        list_resource(path, check_source, grant=resource_grant)
    from .gateway import resolve_model
    model, provider, policy = resolve_model(db, 'chat')
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(409, '主助手模型不属于当前工作区')
    from .media import Media
    from .agent_media import describe
    return {'target_key':task.scope['target_key'], 'sender_id':task.scope['sender_id'], 'goal':task.goal,
        'scope':task.scope, 'source_hash':task.source_hash, 'media_refs':task.media_refs,
        'attachments':describe([db.get(Media, r['id']) for r in task.media_refs]),
        'agents':agents, 'tools':tools, 'share_memory':selection.share_memory, 'resource_path':path,
        **({'resource_policy':resource_grant} if path is not None else {}),
        'model_id':model.id, 'model':model.name, 'provider_version':provider.version, 'policy_version':policy,
        'max_calls':selection.max_calls, 'audience':'本群所有成员可见' if task.scope['target_key'].startswith('group:') else '仅原 QQ 私聊',
        'costs':'模型与已选外部服务可能收费，未知价格不计为零；每次远程请求仍须在 Web 单独确认'}


def task_view(db, task):
    from .delegation import AgentRun
    run = db.get(AgentRun, task.run_id) if task.run_id else None
    receipts = {}
    for key, identity in task.notifications.items():
        receipts[key] = [d.status for d in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == identity))]
    return {k:getattr(task,k) for k in ('id','channel_id','goal','status','version','run_id','expires_at','created_at','error')} | {
        'target_key':task.scope['target_key'], 'sender_id':task.scope['sender_id'],
        'run_status':run.status if run else None, 'notifications':receipts}


def task_lock(db, user, identity):
    row = owned(db, ChannelTask, identity, user)
    # Same order as the channel gateway. Source reads don't lock execution rows.
    owned(db, Channel, row.channel_id, user, lock=True)
    row = owned(db, ChannelTask, identity, user, lock=True)
    # The initial ownership read may predate another confirmation while this
    # request waits for the channel lock. FOR UPDATE alone doesn't refresh an
    # existing SQLAlchemy identity-map object.
    db.refresh(row)
    return row


@router.get('/api/channel-tasks', response_model=list[ChannelTaskView])
def listing(ctx: Context, channel_id: str = Query(...)):
    db, user, _ = ctx
    owned(db, Channel, channel_id, user, lock=True)
    rows = list(db.scalars(select(ChannelTask).where(ChannelTask.channel_id == channel_id,
        ChannelTask.user_id == user.id, ChannelTask.workspace_id == user.workspace_id).order_by(ChannelTask.created_at.desc()).limit(50)))
    for task in rows:
        if task.status not in STOPPED and not source_valid(db, task):
            discard(db, task, '来源、授权或有效期已失效')
    return [task_view(db, t) for t in rows]


@router.post('/api/channel-tasks/{identity}/preview', response_model=ChannelTaskPreviewView)
def preview(identity: str, body: Selection, ctx: Context):
    db, user, login = ctx
    task = task_lock(db, user, identity)
    if task.version != body.version or task.status != 'pending':
        raise HTTPException(409, '请求状态已经变化，请重新加载')
    plan = plan_for(db, task, body)
    task.version += 1
    task.preview = {'plan':plan, 'digest':digest(plan), 'expires_at':min(now()+300, task.expires_at),
                    'selection':body.model_dump(), 'login_id':login.id}
    audit(db, user, 'channel_task_preview', task.id)
    return {k:task.preview[k] for k in ('plan','digest','expires_at')} | {'version':task.version}


@router.post('/api/channel-tasks/{identity}/confirm', response_model=ChannelTaskView)
def confirm(identity: str, body: Confirm, ctx: Context):
    db, user, login = ctx
    task = task_lock(db, user, identity)
    p = task.preview
    # Lost HTTP responses can only return the already committed execution.
    if task.run_id and p.get('digest') == body.digest and p.get('login_id') == login.id and task.version == body.version+1:
        if not source_valid(db, task):
            raise HTTPException(410, '原请求已失效')
        return task_view(db, task)
    if (task.status != 'pending' or task.version != body.version or not p or p['expires_at'] <= now() or
            p['login_id'] != login.id or p['digest'] != body.digest):
        raise HTTPException(409, '预览已变化、过期或登录不同，请重新核对')
    plan = plan_for(db, task, Selection(**p['selection']))
    if digest(plan) != p['digest']:
        raise HTTPException(409, '助手、模型、工具或资料版本已变化，请重新预览')
    from .delegation import AgentRun, event
    config = {k:plan[k] for k in ('agents','tools','share_memory','model_id','model','provider_version','policy_version','media_refs')}
    if plan.get('resource_policy'):
        config['resource_policy'] = plan['resource_policy']
    config.update(allow_files=plan['resource_path'] is not None, resource_prefix=plan['resource_path'],
        qq_task_id=task.id, qq_audience=plan['target_key'], media_actor_id=task.actor_id,
        media_contract='delegation-media-v1', prompt_version='delegation-v1', max_nodes=8,max_parallel=3,max_depth=1)
    run = AgentRun(user_id=user.id, workspace_id=user.workspace_id, request_id='qq-task:'+task.id,
        request_hash=p['digest'], title='QQ 协作 · '+task.goal[:80], goal=task.goal, config=config,
        max_calls=plan['max_calls'])
    db.add(run)
    db.flush()
    task.run_id, task.status, task.version = run.id, 'running', task.version+1
    event(db, run, 'queued', qq_task_id=task.id, audience=plan['target_key'])
    scheduled_job(db,'agent-plan:'+run.id,'agent_plan',{'run_id':run.id},workspace_id=user.workspace_id)
    audit(db,user,'channel_task_confirm',task.id)
    return task_view(db,task)


def discard(db, task, reason, status='invalidated'):
    if task.status in STOPPED:
        return
    task.status, task.error, task.version, task.preview = status, reason, task.version+1, {}
    if status in ('invalidated', 'expired'):
        task.goal = ''
    from .delegation import AgentRun, revoke_run
    run = db.get(AgentRun, task.run_id) if task.run_id else None
    if run:
        revoke_run(db, run, reason, invalidated=True)
    from .memory import invalidate_run
    for chat in db.scalars(select(ChatRun).where(ChatRun.conversation_id == task.conversation_id)):
        if chat.config.get('qq_task', {}).get('id') == task.id:
            invalidate_run(db, chat)


def invalidate(db, *, channel_id=None, binding_id=None, source_id=None):
    query = select(ChannelTask).where(ChannelTask.status.not_in(STOPPED))
    if channel_id:
        query = query.where(ChannelTask.channel_id == channel_id)
    if source_id:
        query = query.where(ChannelTask.source_id == source_id)
    for task in db.scalars(query):
        if not binding_id or task.scope.get('binding_id') == binding_id:
            discard(db, task, 'QQ 协作来源或渠道授权已撤销')


@router.post('/api/channel-tasks/{identity}/cancel', response_model=ChannelTaskView)
def cancel(identity: str, body: TaskVersion, ctx: Context):
    db, user, _ = ctx
    task = task_lock(db,user,identity)
    if task.version != body.version:
        raise HTTPException(409,'请求状态已经变化，请重新加载')
    discard(db,task,'管理员拒绝或撤销了本次 QQ 协作；已发生的外部操作须核对',
            status='rejected' if not task.run_id else 'cancelled')
    audit(db,user,'channel_task_cancel',task.id)
    return task_view(db,task)


def notification(db, task, run, kind, content):
    key = f'{run.round}:{kind}'
    if key in task.notifications:
        return
    original = db.get(ChannelInbound, task.inbound_id)
    incoming = ChannelInbound(user_id=task.user_id,workspace_id=task.workspace_id,channel_id=task.channel_id,
        channel_version=original.channel_version, external_message_id='task:'+digest([task.id,key])[:45],
        target_key=original.target_key, sender_id=original.sender_id, content='',binding_id=original.binding_id,
        binding_version=original.binding_version,status='ready')
    db.add(incoming)
    db.flush()
    make_reply(db,incoming,task.actor_id,task.conversation_id,content,task=task,kind=kind,
               source=db.get(Message,task.source_id),run=run)
    task.notifications = task.notifications | {key:incoming.id}


def expire_notifications(db, task):
    """An offline target never receives an accumulated stale burst on reconnect."""
    for identity in task.notifications.values():
        incoming = db.get(ChannelInbound, identity)
        if not incoming or incoming.created_at > now()-3600:
            continue
        changed = False
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == identity,
                                                                  ChannelDelivery.status == 'pending')):
            delivery.status, delivery.content, delivery.error = 'cancelled', '', '离线通知已超过一小时投递期限，请在 Web 查看任务结果'
            changed = True
        if changed:
            incoming.status, incoming.error = 'cancelled', '通知过期，不重排、不更换接收者；任务结果仍在 Web'


def reconcile():
    """Durable, idempotent result projection. Never execute models or remote tools here."""
    from .delegation import AgentRun, AgentNode, DONE
    from .agent_worker import validate_sources, validate_profiles
    # Short independent transactions; a locked/slow channel doesn't hold other targets.
    with SessionLocal() as db:
        ids = list(db.scalars(select(ChannelTask.id).where(ChannelTask.status.not_in(STOPPED))))
    for identity in ids:
        with SessionLocal.begin() as db:
            initial = db.get(ChannelTask,identity)
            if not initial:
                continue
            db.scalar(select(Channel).where(Channel.id == initial.channel_id).with_for_update())
            task = db.scalar(select(ChannelTask).where(ChannelTask.id == identity).with_for_update().execution_options(populate_existing=True))
            if task.status in STOPPED:
                continue
            expire_notifications(db, task)
            if not source_valid(db,task):
                discard(db,task,'请求已过期、来源删除或渠道授权变化',status='expired' if task.expires_at<=now() else 'invalidated')
                continue
            if not task.run_id:
                continue
            run = db.get(AgentRun,task.run_id,populate_existing=True)
            if not run or run.status == 'invalidated':
                discard(db,task,'协作已失效，未发送旧结果')
                continue
            try:
                validate_sources(db,run,lock=False)
                validate_profiles(db,run)
            except HTTPException:
                discard(db,task,'协作来源、模型、角色或工具授权已变化，未发送旧结果')
                continue
            if run.status not in DONE:
                if db.scalar(select(AgentNode.id).where(AgentNode.run_id == run.id,AgentNode.status=='waiting_confirmation')):
                    notification(db,task,run,'confirmation','后台协作有远程工具提案，等待管理员在 Web「任务中心 → 协作任务」核对参数与费用；尚未执行该工具。')
                continue
            text = '后台协作'+{'completed':'已形成汇总','partial':'部分完成','failed':'未完成','cancelled':'已经停止'}.get(run.status,'已结束')+'。\n'
            if run.status in ('completed','partial'):
                text += run.result.get('summary','没有可发布汇总')
                text += '\n'+ '\n'.join('- '+f['text'] for f in run.result.get('findings',[]))
                text += '\n限制：'+ '；'.join(run.result.get('limitations',[]))
                text += '\n仅校验结构与引用范围；外部操作以真实回执为准，业务效果仍须核对。'
            else:
                text += '请管理员在 Web 查看真实状态；未将部分生成内容记为成功。外部操作如有结果未知，须先核对。'
            # Never silently truncate a result or turn it into an unreviewed file upload.
            if len(text)>17500:
                text='后台协作结果超出 QQ 文字预算，请管理员在 Web 查看完整结果；未截断后声称完整回传。'
            notification(db,task,run,'result',text)
            task.status = run.status
