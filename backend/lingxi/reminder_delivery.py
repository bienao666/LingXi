"""Explicit per-reminder private QQ delivery; station receipt remains independent."""
import hashlib
import json
from math import isfinite
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal, now, uid
from .models import Entity, User
from .proactive_channels import ContactTarget, TargetOption, checked_target, targets
from .security import encrypt, decrypt
from .web import Context, owned, view, audit

router = APIRouter()
ACTIVE = ('configured', 'pending', 'sending')


class ReminderDelivery(Entity, Base):
    __tablename__ = 'reminder_deliveries'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    reminder_id: Mapped[str] = mapped_column(String(64), unique=True)
    reminder_version: Mapped[int] = mapped_column(Integer)
    source_hash: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30))
    expires_at: Mapped[float] = mapped_column(Float)
    confirmation_hash: Mapped[str] = mapped_column(String(64))
    echo: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    protocol_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reviewed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReminderDeliveryView(BaseModel):
    version: int = 0
    reminder_version: int
    target: ContactTarget = Field(default_factory=ContactTarget)
    target_label: str = '仅存站内'
    status: str = 'station_only'
    expires_at: float | None = None
    sent_at: float | None = None
    protocol_message_id: str | None = None
    reviewed_at: float | None = None
    error: str | None = None


class DeliveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    reminder_version: int = Field(ge=1)
    delivery_version: int = Field(ge=0)
    target: ContactTarget
    wait_hours: int = Field(default=1, ge=1, le=24)


class DeliveryPreview(BaseModel):
    token: str
    expires_at: float
    reminder_version: int
    title: str
    content: str
    due_at: float
    timezone: str
    after: ReminderDeliveryView


class DeliveryConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=10, max_length=12000)
    acknowledgement: Literal['send_this_reminder_to_selected_qq', 'store_this_reminder_only']


class DeliveryAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=1)
    acknowledgement: Literal['cancel_without_resend', 'checked_no_resend']


def record(db, reminder_id, lock=False):
    query = select(ReminderDelivery).where(ReminderDelivery.reminder_id == reminder_id).execution_options(populate_existing=True)
    return db.scalar(query.with_for_update() if lock else query)


def public(db, reminder):
    row = record(db, reminder.id)
    if not row:
        return ReminderDeliveryView(reminder_version=reminder.version)
    target = {k: v for k, v in row.target.items() if k in ContactTarget.model_fields}
    return ReminderDeliveryView(version=row.version, reminder_version=row.reminder_version,
        target=ContactTarget(**target), target_label=row.target.get('label', '仅存站内'),
        status=row.status, expires_at=row.expires_at, sent_at=row.sent_at,
        protocol_message_id=row.protocol_message_id, reviewed_at=row.reviewed_at, error=row.error)


def source_hash(reminder):
    from .tasks import digest
    return digest({k: getattr(reminder, k) for k in ('id', 'user_id', 'workspace_id', 'version',
        'title', 'content', 'due_at', 'timezone', 'conversation_id')})


def owner_lock(db, user):
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def reminder_source(db, user, identity, scheduled=False):
    from .tasks import Reminder
    from .chat_actions import require_origin
    row = owned(db, Reminder, identity, user, lock=True)
    require_origin(db, row.id)
    if scheduled and (row.status != 'scheduled' or row.due_at <= now()):
        raise HTTPException(409, '提醒已到期或已变化，请重新加载；已触发提醒不能更改投递目标')
    return row


def stop(row, reason):
    if row.status in ('configured', 'pending'):
        row.status, row.error, row.version = 'cancelled', reason, row.version + 1
    elif row.status == 'sending':
        row.status, row.error, row.version = 'unknown', reason + '；已经开始发送，结果待核对，不会重发', row.version + 1


def invalidate(db, *, reminder_id=None, channel_id=None, binding_id=None, user_id=None, reason='提醒或 QQ 授权已变化'):
    query = select(ReminderDelivery).where(ReminderDelivery.status.in_(ACTIVE))
    if reminder_id: query = query.where(ReminderDelivery.reminder_id == reminder_id)
    if channel_id: query = query.where(ReminderDelivery.channel_id == channel_id)
    if user_id: query = query.where(ReminderDelivery.user_id == user_id)
    for row in db.scalars(query.with_for_update()):
        if not binding_id or row.target.get('binding_id') == binding_id:
            stop(row, reason)


@router.get('/api/reminder-targets', response_model=list[TargetOption])
def reminder_targets(ctx: Context):
    return targets(ctx)


@router.get('/api/reminders/{identity}/delivery', response_model=ReminderDeliveryView)
def get_delivery(identity: str, ctx: Context):
    db, user, _ = ctx
    return public(db, reminder_source(db, user, identity))


@router.post('/api/reminders/{identity}/delivery/previews', response_model=DeliveryPreview)
def preview(identity: str, body: DeliveryInput, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    reminder = reminder_source(db, user, identity, scheduled=True)
    row = record(db, reminder.id)
    if reminder.version != body.reminder_version or (row.version if row else 0) != body.delivery_version:
        raise HTTPException(409, '提醒或投递设置已变化，请重新加载后核对')
    from .plugins import require_capability
    require_capability(db, user, 'reminder.once')
    target = body.target.model_dump()
    channel, link = checked_target(db, user, target)
    if channel:
        target |= {'external_user_id': link.external_user_id, 'label': channel.name + ' · QQ ' + link.external_user_id}
    expiry = min(now() + 300, reminder.due_at)
    after = ReminderDeliveryView(version=body.delivery_version + 1, reminder_version=reminder.version,
        target=body.target, target_label=target.get('label', '仅存站内'),
        status='configured' if channel else 'cancelled', expires_at=reminder.due_at + body.wait_hours * 3600)
    payload = {'purpose':'reminder-delivery', 'user':user.id, 'workspace':user.workspace_id,
        'session':login.id, 'reminder':reminder.id, 'source_hash':source_hash(reminder),
        'reminder_version':reminder.version, 'delivery_version':body.delivery_version,
        'target':target, 'channel':channel.id if channel else None,
        'deadline':after.expires_at, 'expires_at':expiry}
    return DeliveryPreview(token=encrypt(json.dumps(payload, sort_keys=True)), expires_at=expiry,
        reminder_version=reminder.version, title=reminder.title, content=reminder.content,
        due_at=reminder.due_at, timezone=reminder.timezone, after=after)


@router.post('/api/reminders/{identity}/delivery/confirm', response_model=ReminderDeliveryView)
def confirm(identity: str, body: DeliveryConfirm, ctx: Context):
    db, user, login = ctx
    try:
        data = json.loads(decrypt(body.token))
        if (data['purpose'] != 'reminder-delivery' or data['user'] != user.id or
            data['workspace'] != user.workspace_id or data['session'] != login.id or data['reminder'] != identity):
            raise ValueError()
        target = ContactTarget(**{k:v for k,v in data['target'].items() if k in ContactTarget.model_fields})
    except Exception:
        raise HTTPException(409, '投递预览无效，请重新预览') from None
    expected = 'send_this_reminder_to_selected_qq' if target.kind == 'qq' else 'store_this_reminder_only'
    if body.acknowledgement != expected:
        raise HTTPException(422, '请确认此条提醒的接收目标和内容')
    owner_lock(db, user)
    from .tasks import Reminder
    reminder = owned(db, Reminder, identity, user, lock=True)
    row = record(db, reminder.id, lock=True)
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    # A lost confirmation response may be retried with this exact request. No send is repeated.
    if row and row.confirmation_hash == token_hash and row.version == data['delivery_version'] + 1:
        return public(db, reminder)
    reminder = reminder_source(db, user, identity, scheduled=True)
    if (data['expires_at'] <= now() or reminder.version != data['reminder_version'] or
        source_hash(reminder) != data['source_hash'] or (row.version if row else 0) != data['delivery_version']):
        raise HTTPException(409, '投递预览过期或内容已变化，请重新核对')
    from .plugins import require_capability
    require_capability(db, user, 'reminder.once')
    channel, _ = checked_target(db, user, target.model_dump())
    row = row or ReminderDelivery(reminder_id=reminder.id, user_id=user.id, workspace_id=user.workspace_id)
    row.reminder_version, row.source_hash, row.channel_id = reminder.version, source_hash(reminder), channel.id if channel else None
    row.target, row.status, row.expires_at = data['target'], 'configured' if channel else 'cancelled', data['deadline']
    row.confirmation_hash, row.version = token_hash, data['delivery_version'] + 1
    row.error, row.echo, row.sent_at, row.protocol_message_id, row.reviewed_at = None, None, None, None, None
    db.add(row); db.flush()
    audit(db, user, 'reminder_delivery_configure', reminder.id)
    return public(db, reminder)


@router.post('/api/reminders/{identity}/delivery/action', response_model=ReminderDeliveryView)
def action(identity: str, body: DeliveryAction, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    reminder = reminder_source(db, user, identity)
    row = record(db, reminder.id, lock=True)
    if not row or row.version != body.version:
        raise HTTPException(409, '投递状态已变化，请重新加载后核对')
    if body.acknowledgement == 'cancel_without_resend':
        if row.status not in ACTIVE: raise HTTPException(409, '没有可以停止的投递；已发起操作不能撤回')
        stop(row, '用户停止此条 QQ 提醒投递')
    else:
        if row.status not in ('unknown', 'failed') or row.reviewed_at:
            raise HTTPException(409, '没有需要核对的未知或失败投递')
        row.reviewed_at, row.version = now(), row.version + 1
    audit(db, user, 'reminder_delivery_' + body.acknowledgement, reminder.id)
    return public(db, reminder)


def valid(db, row):
    from .tasks import Reminder
    from .chat_actions import require_origin
    from .plugins import require_capability
    user, reminder = db.get(User, row.user_id), db.get(Reminder, row.reminder_id, populate_existing=True)
    if (not user or not user.password_hash or user.workspace_id != row.workspace_id or not reminder or
        reminder.user_id != user.id or reminder.workspace_id != user.workspace_id or
        reminder.status not in ('scheduled', 'fired', 'acknowledged') or reminder.version != row.reminder_version or source_hash(reminder) != row.source_hash):
        raise HTTPException(409, '提醒已取消、修改或来源失效，未继续发送')
    require_origin(db, reminder.id)
    require_capability(db, user, 'reminder.once')
    target = {k:v for k,v in row.target.items() if k in ContactTarget.model_fields}
    channel, link = checked_target(db, user, target)
    if not channel or channel.id != row.channel_id or row.target.get('external_user_id') != link.external_user_id:
        raise HTTPException(409, '提醒的 QQ 身份或平台授权已变化')
    return reminder


def enqueue(db, reminder):
    row = record(db, reminder.id, lock=True)
    if row and row.status == 'configured':
        try: valid(db, row)
        except HTTPException:
            stop(row, '提醒或接收授权已失效，仍保留站内触发记录'); return
        row.status, row.version = 'pending', row.version + 1


def reconcile_row(db, row):
    if row.status not in ACTIVE: return
    try: valid(db, row)
    except HTTPException:
        stop(row, '提醒来源、插件或 QQ 授权已变化'); return
    if row.status == 'sending':
        if row.sent_at < now() - 30: stop(row, 'QQ 提醒回执超时')
    elif row.expires_at <= now():
        row.status, row.error, row.version = 'expired', '超过用户允许的等待期限，不补发或改投', row.version + 1


def reconcile():
    with SessionLocal.begin() as db:
        for row in db.scalars(select(ReminderDelivery).where(ReminderDelivery.status.in_(ACTIVE)).with_for_update()):
            reconcile_row(db, row)


def prepare(identity, connection):
    from .channel_gateway import authorized_channel
    from .channels import ChannelInbound, ChannelDelivery
    with SessionLocal.begin() as db:
        channel = authorized_channel(db, identity, connection)
        if not channel: return False
        owner = db.get(User, channel.user_id)
        if not owner or owner.workspace_id != channel.workspace_id: return False
        owner_lock(db, owner)
        rows = list(db.scalars(select(ReminderDelivery).where(ReminderDelivery.channel_id == identity,
            ReminderDelivery.status.in_(ACTIVE)).order_by(ReminderDelivery.created_at).with_for_update()))
        for row in rows:
            reconcile_row(db, row)
            if row.status != 'pending' or channel.login_state != 'online' or now() - channel.last_sent < 2: continue
            reminder = valid(db, row)
            if reminder.status not in ('fired', 'acknowledged'): continue
            # Respect protocol pacing and unresolved writes, independently of unsolicited contact quotas.
            if db.scalar(select(ReminderDelivery.id).where(ReminderDelivery.channel_id == identity,
                ReminderDelivery.id != row.id, ReminderDelivery.status.in_(['sending','unknown','failed']),
                ReminderDelivery.reviewed_at.is_(None))): continue
            if db.scalar(select(ChannelDelivery.id).join(ChannelInbound, ChannelInbound.id == ChannelDelivery.inbound_id).where(
                ChannelDelivery.channel_id == identity, ChannelInbound.target_key == 'private:' + row.target['external_user_id'],
                ChannelDelivery.status.in_(['sending','unknown','failed']), ChannelDelivery.reviewed_at.is_(None))): continue
            row.status, row.sent_at, row.echo, row.version = 'sending', now(), connection + ':reminder:' + uid(), row.version + 1
            channel.last_sent = now()
            return {'action':'send_private_msg', 'params':{'user_id':int(row.target['external_user_id']),
                'message':[{'type':'text','data':{'text':f'待办提醒：{reminder.title}\n\n{reminder.content}\n\n此消息是你设置的一次性提醒。'}}]}, 'echo':row.echo}
    return None


def interrupted(db, channel_id, connection=None):
    for row in db.scalars(select(ReminderDelivery).where(ReminderDelivery.channel_id == channel_id,
            ReminderDelivery.status == 'sending').with_for_update()):
        if connection is None or (row.echo or '').startswith(connection + ':'):
            stop(row, 'QQ 连接中断')


def receive_receipt(db, channel, echo, event):
    from .channel_gateway import numeric
    row = db.scalar(select(ReminderDelivery).where(ReminderDelivery.channel_id == channel.id,
        ReminderDelivery.echo == echo).with_for_update())
    if not row or row.status not in ('sending', 'unknown'): return
    code, data = event.get('retcode'), event.get('data')
    message_id = numeric(data.get('message_id'), 30, negative=True) if isinstance(data, dict) else None
    if type(code) is not int: return
    if code == 0 and message_id:
        row.status, row.protocol_message_id, row.error = 'accepted_by_protocol', message_id, None
    elif code != 0:
        row.status, row.error = 'failed', 'QQ 协议端拒绝提醒请求；未自动重发'
    else: return
    row.version += 1


def checkpoint(db):
    return [view(row) for row in db.scalars(select(ReminderDelivery))]


def validate_checkpoint(rows, user):
    if not isinstance(rows, list): raise ValueError('Invalid reminder delivery checkpoint')
    ids, reminders = set(), set()
    expected = {c.name for c in ReminderDelivery.__table__.columns}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != expected or row['user_id'] != user['id'] or row['workspace_id'] != user['workspace_id'] or
            any(not isinstance(row[k], str) or not 1 <= len(row[k]) <= 64 for k in ('id','user_id','workspace_id','reminder_id')) or
            row['id'] in ids or row['reminder_id'] in reminders or
            any(type(row[k]) is not int or row[k] < 1 for k in ('version','reminder_version')) or
            row['status'] not in (*ACTIVE, 'accepted_by_protocol','unknown','failed','cancelled','expired') or
            any(not isinstance(row[k], str) or len(row[k]) != 64 or any(c not in '0123456789abcdef' for c in row[k]) for k in ('source_hash','confirmation_hash')) or
            any(type(row[k]) not in (float,int) or not isfinite(row[k]) or row[k] <= 0 for k in ('created_at','expires_at')) or
            any(row[k] is not None and (type(row[k]) not in (float,int) or not isfinite(row[k]) or row[k] <= 0) for k in ('sent_at','reviewed_at'))):
            raise ValueError('Invalid reminder delivery checkpoint')
        target = row['target']
        if not isinstance(target, dict) or set(target) - {*ContactTarget.model_fields, 'external_user_id','label'}:
            raise ValueError('Invalid reminder delivery target')
        parsed = ContactTarget.model_validate({k:v for k,v in target.items() if k in ContactTarget.model_fields})
        if parsed.kind == 'qq':
            from .channel_gateway import numeric
            if not numeric(target.get('external_user_id')) or not isinstance(target.get('label'), str) or len(target['label']) > 150 or not isinstance(row['channel_id'],str) or len(row['channel_id']) > 64:
                raise ValueError('Invalid reminder delivery target')
        elif row['channel_id'] is not None or row['status'] != 'cancelled':
            raise ValueError('Invalid station-only reminder delivery')
        if row['echo'] is not None and (not isinstance(row['echo'],str) or len(row['echo']) > 100):
            raise ValueError('Invalid reminder delivery receipt')
        if row['protocol_message_id'] is not None:
            from .channel_gateway import numeric
            if not numeric(row['protocol_message_id'],30,negative=True): raise ValueError('Invalid reminder delivery receipt')
        if row['error'] is not None and (not isinstance(row['error'],str) or len(row['error']) > 1000):
            raise ValueError('Invalid reminder delivery checkpoint')
        ids.add(row['id']); reminders.add(row['reminder_id'])


def restore(db, rows):
    for values in rows:
        existing = record(db, values['reminder_id'])
        if existing and existing.id != values['id']:
            db.delete(existing); db.flush()
        if not existing or existing.version <= values['version']:
            db.merge(ReminderDelivery(**values))
    db.flush()
    for row in db.scalars(select(ReminderDelivery)):
        stop(row, '恢复隔离：旧提醒投递不自动重放，需重新核对授权')
