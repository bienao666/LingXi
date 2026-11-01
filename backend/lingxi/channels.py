"""Owned OneBot instances and durable identity, inbox and delivery contracts."""
import hashlib
import secrets
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Entity, User, ChatRun, Message
from .security import encrypt
from .web import Context, owned, view, audit

router = APIRouter()


class Channel(Entity, Base):
    __tablename__ = 'channels'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(100))
    bot_id: Mapped[str] = mapped_column(String(30))
    encrypted_token: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    last_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    login_state: Mapped[str] = mapped_column(String(30), default='unknown')
    protocol_version: Mapped[str] = mapped_column(String(100), default='')
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    last_sent: Mapped[float] = mapped_column(Float, default=0)


class IdentityLink(Entity, Base):
    __tablename__ = 'identity_links'
    __table_args__ = (UniqueConstraint('channel_id', 'external_user_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    external_user_id: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='pending')
    challenge_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)
    allow_memory: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class ChannelSession(Entity, Base):
    __tablename__ = 'channel_sessions'
    __table_args__ = (UniqueConstraint('channel_id', 'scope_key'),)
    user_id: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    scope_key: Mapped[str] = mapped_column(String(200))
    actor_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64))


class ChannelInbound(Entity, Base):
    __tablename__ = 'channel_inbounds'
    __table_args__ = (UniqueConstraint('channel_id', 'target_key', 'external_message_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_version: Mapped[int] = mapped_column(Integer)
    external_message_id: Mapped[str] = mapped_column(String(60))
    target_key: Mapped[str] = mapped_column(String(100), index=True)
    sender_id: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    binding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binding_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeliveryReview(BaseModel):
    acknowledgement: Literal['checked_no_resend']


class ChannelDelivery(Entity, Base):
    __tablename__ = 'channel_deliveries'
    __table_args__ = (UniqueConstraint('inbound_id', 'part'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    inbound_id: Mapped[str] = mapped_column(String(64), index=True)
    part: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default='text')
    speech: Mapped[dict] = mapped_column(JSON, default=dict)
    encrypted_audio: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_info: Mapped[dict] = mapped_column(JSON, default=dict)
    protocol_file_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    echo: Mapped[str] = mapped_column(String(100), unique=True)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    protocol_message_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ChannelRules(BaseModel):
    private_enabled: bool = True
    allow_unbound: bool = False
    group_enabled: bool = False
    group_ids: list[str] = Field(default_factory=list, max_length=100)
    command: str = Field(default='/灵犀', min_length=2, max_length=20, pattern=r'^/[^\s]+$')
    attachments_enabled: bool = False
    voice_enabled: bool = False
    voice_reply_enabled: bool = False
    file_reply_enabled: bool = False
    attachment_hosts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('attachment_hosts')
    @classmethod
    def attachment_domains(cls, values):
        from .channel_media import valid_host
        return list(dict.fromkeys(valid_host(value) for value in values))

    @field_validator('group_ids')
    @classmethod
    def group_numbers(cls, values):
        if any(not v.isascii() or not v.isdigit() or not 4 <= len(v) <= 20 for v in values):
            raise ValueError('群号必须为 4–20 位数字')
        return list(dict.fromkeys(values))


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    bot_id: str = Field(pattern=r'^[0-9]{4,20}$')
    access_token: str = Field(default='', max_length=500)
    enabled: bool = False
    version: int | None = None

    @field_validator('access_token')
    @classmethod
    def token(cls, value):
        if value and (len(value) < 32 or any(c.isspace() for c in value) or not value.isascii()):
            raise ValueError('令牌至少 32 位，使用无空白 ASCII 字符')
        return value


class ChannelView(BaseModel):
    id: str
    name: str
    bot_id: str
    version: int
    enabled: bool
    rules: ChannelRules
    connection_state: str
    login_state: str
    last_seen: float | None
    protocol_version: str
    capabilities: dict
    gateway_path: str
    token_mask: str


class RulesUpdate(ChannelRules):
    version: int = Field(ge=1)


class LinkInput(BaseModel):
    channel_id: str
    external_user_id: str = Field(pattern=r'^[0-9]{4,20}$')
    allow_memory: bool = False


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class LinkView(BaseModel):
    id: str
    channel_id: str
    external_user_id: str
    status: str
    allow_memory: bool
    version: int
    expires_at: float


class LinkChallenge(BaseModel):
    link: LinkView
    command: str


class DeliveryView(BaseModel):
    id: str
    inbound_id: str
    part: int
    kind: str = 'text'
    speech: dict = Field(default_factory=dict)
    file_info: dict = Field(default_factory=dict)
    protocol_file_id: str | None = None
    status: str
    sent_at: float | None
    protocol_message_id: str | None
    error: str | None
    reviewed_at: float | None


class InboundView(BaseModel):
    id: str
    sender_id: str
    target_key: str
    external_message_id: str
    status: str
    created_at: float
    run_id: str | None
    error: str | None
    deliveries: list[DeliveryView]
    attachments: list['ChannelAttachmentView']


from .channel_media import ChannelAttachmentView
InboundView.model_rebuild()


def channel_view(row):
    return {**view(row, ('encrypted_token', 'connection_id', 'lease_until', 'last_sent', 'workspace_id', 'user_id', 'created_at')),
            'connection_state': 'connected' if row.connection_id and row.lease_until > now() else 'offline',
            'gateway_path': '/gateway/onebot/v11/ws/'+row.id, 'token_mask': '••••••••'}


def delivery_view(row):
    result = view(row, ('encrypted_audio', 'speech', 'content', 'file_info'))
    result['file_info'] = {k: v for k, v in row.file_info.items() if k in ('name', 'bytes')}
    result['speech'] = {k: v for k, v in row.speech.items() if k in (
        'phase', 'voice', 'source', 'persona_name', 'persona_version', 'voice_version',
        'provider_id', 'provider_version', 'model', 'characters', 'bytes')}
    return result


def check_version(row, version):
    if row.version != version:
        raise HTTPException(409, '配置已经变化，请重新加载后编辑')


def invalidate_channel_runs(db, channel_id, binding_id=None):
    from .emotions import EmotionObservation, discard, owner_lock
    # Clear short-lived excerpts even for completed runs. Neither a revoked
    # identity nor a changed channel may leave a ready hint behind.
    channel = db.get(Channel, channel_id)
    if channel:
        # Channel -> owner is the same order as receive/submit. Serialize with
        # the analyzer's final owner fence so a late result cannot overwrite
        # invalidation after a channel or binding change.
        owner_lock(db, channel.user_id)
    observations = db.execute(select(EmotionObservation, ChatRun).join(Message, Message.id == EmotionObservation.source_id)
        .join(ChatRun, ChatRun.id == Message.run_id).where(EmotionObservation.status.in_(['queued', 'running', 'ready']),
            EmotionObservation.user_id == channel.user_id, EmotionObservation.workspace_id == channel.workspace_id)) if channel else []
    for observation, source_run in observations:
        scope = source_run.config.get('channel', {})
        if scope.get('id') == channel_id and (not binding_id or scope.get('binding_id') == binding_id):
            discard(db, observation)
    from .channel_tasks import invalidate as invalidate_tasks
    invalidate_tasks(db, channel_id=channel_id, binding_id=binding_id)
    from .proactive_channels import invalidate
    invalidate(db,channel_id=channel_id,binding_id=binding_id,reason='QQ 平台或身份授权已修改或撤销')
    from .reminder_delivery import invalidate as invalidate_reminders
    invalidate_reminders(db, channel_id=channel_id, binding_id=binding_id)
    from .memory import invalidate_run
    for run in db.scalars(select(ChatRun).where(ChatRun.status.in_(['queued', 'running']))):
        scope = run.config.get('channel', {})
        if scope.get('id') == channel_id and (not binding_id or scope.get('binding_id') == binding_id):
            invalidate_run(db, run)
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == channel_id,
                                                           ChannelInbound.status.in_(['queued', 'ingesting', 'running', 'ready']))):
        if not binding_id or incoming.binding_id == binding_id:
            incoming.status, incoming.error = 'cancelled', '渠道或身份授权已变更'
            from .channel_media import cancel
            cancel(db, incoming, incoming.error)
            for output in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == incoming.id,
                                                                  ChannelDelivery.status == 'pending')):
                output.status, output.content, output.error = 'cancelled', '', incoming.error
                output.encrypted_audio = None


@router.get('/api/channels', response_model=list[ChannelView])
def channel_list(ctx: Context):
    db, user, _ = ctx
    return [channel_view(c) for c in db.scalars(select(Channel).where(Channel.user_id == user.id,
                                                                   Channel.workspace_id == user.workspace_id).order_by(Channel.created_at))]


@router.post('/api/channels', response_model=ChannelView)
def channel_create(body: ChannelInput, ctx: Context):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not body.access_token:
        raise HTTPException(422, '首次配置必须填写与 NapCat 相同的访问令牌')
    if len(list(db.scalars(select(Channel).where(Channel.user_id == user.id)))) >= 10:
        raise HTTPException(409, '最多配置 10 个平台实例')
    row = Channel(user_id=user.id, workspace_id=user.workspace_id, name=body.name, bot_id=body.bot_id,
                  encrypted_token=encrypt(body.access_token), enabled=body.enabled, rules=ChannelRules().model_dump())
    db.add(row)
    db.flush()
    audit(db, user, 'channel_create', row.id)
    return channel_view(row)


@router.put('/api/channels/{identity}', response_model=ChannelView)
def channel_update(identity: str, body: ChannelInput, ctx: Context):
    db, user, _ = ctx
    row = owned(db, Channel, identity, user, lock=True)
    check_version(row, body.version)
    if body.bot_id != row.bot_id:
        raise HTTPException(422, '机器人账号不能变更，请停用后新建实例')
    row.name, row.enabled = body.name, body.enabled
    if body.access_token:
        row.encrypted_token = encrypt(body.access_token)
    row.version += 1
    row.connection_id, row.lease_until, row.login_state = None, 0, 'unknown'
    invalidate_channel_runs(db, row.id)
    audit(db, user, 'channel_update', row.id)
    return channel_view(row)


@router.put('/api/channels/{identity}/rules', response_model=ChannelView)
def rules_update(identity: str, body: RulesUpdate, ctx: Context):
    db, user, _ = ctx
    row = owned(db, Channel, identity, user, lock=True)
    check_version(row, body.version)
    row.rules, row.version = body.model_dump(exclude={'version'}), row.version+1
    invalidate_channel_runs(db, row.id)
    audit(db, user, 'channel_rules', row.id)
    return channel_view(row)


@router.get('/api/channels/{identity}/messages', response_model=list[InboundView])
def inbound_list(identity: str, ctx: Context, limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    owned(db, Channel, identity, user)
    rows = db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == identity)
                     .order_by(ChannelInbound.created_at.desc(), ChannelInbound.id.desc()).limit(limit))
    from .channel_media import public_attachments
    return [view(r, ('content', 'binding_id', 'binding_version', 'channel_version', 'user_id', 'workspace_id', 'channel_id')) |
            {'attachments': public_attachments(db, r.id), 'deliveries': [delivery_view(d) for d in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == r.id)
                                                       .order_by(ChannelDelivery.part))]} for r in rows]


@router.post('/api/channels/{identity}/deliveries/{delivery_id}/review', response_model=DeliveryView)
def delivery_review(identity: str, delivery_id: str, body: DeliveryReview, ctx: Context):
    db, user, _ = ctx
    owned(db, Channel, identity, user, lock=True)
    row = owned(db, ChannelDelivery, delivery_id, user, lock=True)
    if row.channel_id != identity:
        raise HTTPException(404, '记录不存在')
    if row.status not in ('unknown', 'failed'):
        raise HTTPException(409, '只可结束结果未知或失败的等待，请重新加载')
    row.reviewed_at = now()
    incoming = db.get(ChannelInbound, row.inbound_id)
    incoming.status = 'failed' if row.status == 'failed' else 'unknown'
    for part in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == incoming.id,
                                                         ChannelDelivery.status == 'pending')):
        part.status, part.content, part.error = 'cancelled', '', '人工结束本条投递；剩余分段不再发送'
        part.encrypted_audio = None
    audit(db, user, 'channel_delivery_review', row.id)
    return delivery_view(row)


@router.get('/api/identity-links', response_model=list[LinkView])
def link_list(ctx: Context):
    db, user, _ = ctx
    return [view(r) for r in db.scalars(select(IdentityLink).where(IdentityLink.user_id == user.id,
                                                                IdentityLink.workspace_id == user.workspace_id))]


@router.post('/api/identity-links', response_model=LinkChallenge)
def link_request(body: LinkInput, ctx: Context):
    db, user, _ = ctx
    channel = owned(db, Channel, body.channel_id, user, lock=True)
    if not channel.enabled:
        raise HTTPException(409, '请先启用平台实例')
    row = db.scalar(select(IdentityLink).where(IdentityLink.channel_id == channel.id,
                                             IdentityLink.external_user_id == body.external_user_id).with_for_update())
    if row and row.status != 'revoked':
        raise HTTPException(409, '这个账号已有绑定申请，请先撤销后重新申请')
    code = secrets.token_urlsafe(24)
    if not row:
        row = IdentityLink(user_id=user.id, workspace_id=user.workspace_id, channel_id=channel.id,
                           external_user_id=body.external_user_id, version=0)
        db.add(row)
    row.status, row.allow_memory = 'pending', body.allow_memory
    row.version += 1
    row.expires_at, row.challenge_hash = now()+600, hashlib.sha256(code.encode()).hexdigest()
    db.flush()
    audit(db, user, 'identity_request', row.id)
    return {'link': view(row), 'command': '/bind '+code}


@router.post('/api/identity-links/{identity}/confirm', response_model=LinkView)
def link_confirm(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    pre = owned(db, IdentityLink, identity, user)
    owned(db, Channel, pre.channel_id, user, lock=True)
    row = owned(db, IdentityLink, identity, user, lock=True)
    check_version(row, body.version)
    if row.status != 'proved' or row.expires_at <= now():
        raise HTTPException(409, '请在十分钟内先由指定 QQ 私聊发送验证命令，再确认绑定')
    row.status, row.version, row.challenge_hash = 'active', row.version+1, ''
    audit(db, user, 'identity_confirm', row.id)
    return view(row)


@router.post('/api/identity-links/{identity}/revoke', response_model=LinkView)
def link_revoke(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    pre = owned(db, IdentityLink, identity, user)
    owned(db, Channel, pre.channel_id, user, lock=True)
    row = owned(db, IdentityLink, identity, user, lock=True)
    check_version(row, body.version)
    row.status, row.version, row.challenge_hash = 'revoked', row.version+1, ''
    invalidate_channel_runs(db, row.channel_id, row.id)
    audit(db, user, 'identity_revoke', row.id)
    return view(row)
