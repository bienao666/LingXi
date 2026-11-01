"""Explicitly authorized, signed inbound events. Never a notification bypass."""
import hashlib
import hmac
import json
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column
from starlette.concurrency import run_in_threadpool

from .config import settings
from .db import Base, SessionLocal, now
from .models import Entity, User
from .plugin_api import RedactedValidationRoute
from .plugin_manifest import digest
from .plugins import PluginInstallation, owner_lock, require_capability
from .security import encrypt, decrypt
from .web import Context, audit, owned, view

router = APIRouter(route_class=RedactedValidationRoute)
MAX_BODY = 12 * 1024


class EventSubscription(Entity, Base):
    __tablename__ = 'plugin_event_subscriptions'
    __table_args__ = (UniqueConstraint('installation_id', 'capability_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    capability_id: Mapped[str] = mapped_column(String(120))
    release_id: Mapped[str] = mapped_column(String(64))
    installation_version: Mapped[int] = mapped_column(Integer)
    binding_digest: Mapped[str] = mapped_column(String(64))
    dependency_lock: Mapped[dict] = mapped_column(JSON, default=dict)
    encrypted_secret: Mapped[str] = mapped_column(Text, default='')
    event_types: Mapped[list] = mapped_column(JSON, default=list)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=10)
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class PluginEvent(Entity, Base):
    __tablename__ = 'plugin_events'
    __table_args__ = (UniqueConstraint('subscription_id', 'external_id'),
                     UniqueConstraint('subscription_id', 'sequence'))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    release_id: Mapped[str] = mapped_column(String(64))
    subscription_version: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str] = mapped_column(String(100))
    body_digest: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200), default='')
    summary: Mapped[str] = mapped_column(Text, default='')
    occurred_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    status: Mapped[str] = mapped_column(String(30), default='accepted')
    version: Mapped[int] = mapped_column(Integer, default=1)


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    secret: str = Field(default='', max_length=256)
    event_types: list[str] = Field(min_length=1, max_length=16)
    hourly_limit: int = Field(default=10, ge=1, le=60)
    daily_limit: int = Field(default=50, ge=1, le=500)
    version: int = Field(default=0, ge=0)
    acknowledged: Literal[True]

    @field_validator('secret')
    @classmethod
    def secret_bytes(cls, value):
        if value and (not 32 <= len(value) <= 256 or not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError('签名密钥须为 32–256 位不含空白的 ASCII 字符')
        return value


class SubscriptionView(BaseModel):
    id: str
    installation_id: str
    capability_id: str
    plugin_id: str
    plugin_version: str
    status: str
    version: int
    event_types: list[str]
    hourly_limit: int
    daily_limit: int
    has_secret: bool
    available: bool
    reason: str | None
    endpoint: str
    updated_at: float


class EventView(BaseModel):
    id: str
    subscription_id: str
    event_type: str
    title: str
    summary: str
    occurred_at: float
    expires_at: float
    status: str
    version: int
    created_at: float
    sequence: int


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class EventPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    occurred_at: float = Field(gt=0, allow_inf_nan=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)

    @field_validator('title', 'summary')
    @classmethod
    def plain_text(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('事件内容必须为非空文本')
        return value.strip()


def event_capability(db, user, installation_id, capability_id):
    install = owned(db, PluginInstallation, installation_id, user)
    _, release, cap = require_capability(db, user, capability_id, install.plugin_id)
    if cap['kind'] != 'event':
        raise HTTPException(409, '此能力不是事件订阅')
    return install, release, cap['event']


def available_subscription(db, user, row):
    if row.status != 'active' or not row.encrypted_secret:
        raise HTTPException(409, '事件订阅未授权或已撤销')
    install, release, entry = event_capability(db, user, row.installation_id, row.capability_id)
    if (row.release_id != release.id or row.installation_version != install.version or
            row.binding_digest != digest(entry) or row.dependency_lock != install.dependency_lock):
        raise HTTPException(409, '插件版本、安装或授权已变化，请重新确认事件订阅')
    return release, entry


def subscription_view(db, user, row):
    from .plugins import PluginRelease
    install = db.get(PluginInstallation, row.installation_id)
    release = db.get(PluginRelease, row.release_id)
    reason = None
    try:
        available_subscription(db, user, row)
    except HTTPException as exc:
        reason = str(exc.detail)
    return {key: getattr(row, key) for key in ('id', 'installation_id', 'capability_id', 'status',
        'version', 'event_types', 'hourly_limit', 'daily_limit', 'updated_at')} | {
        'plugin_id': install.plugin_id, 'plugin_version': release.version,
        'has_secret': bool(row.encrypted_secret), 'available': reason is None, 'reason': reason,
        'endpoint': settings().public_origin.rstrip('/') + '/api/plugin-events/hooks/' + row.id}


def valid_event(db, user, row):
    if not row or row.user_id != user.id or row.workspace_id != user.workspace_id or row.status != 'accepted' or row.expires_at <= now():
        return False
    sub = db.get(EventSubscription, row.subscription_id, populate_existing=True)
    if not sub or sub.user_id != user.id or sub.workspace_id != user.workspace_id or sub.version != row.subscription_version:
        return False
    try:
        available_subscription(db, user, sub)
    except HTTPException:
        return False
    return True


def event_view(db, user, row):
    values = view(row)
    if not valid_event(db, user, row):
        values |= {'title': '', 'summary': '', 'status': 'deleted' if row.status == 'deleted' else
                   'expired' if row.expires_at <= now() else 'invalidated'}
    return values


def invalidate_subscription(db, row):
    from .proactive import invalidate_topics
    row.status, row.encrypted_secret, row.updated_at = 'revoked', '', now()
    row.version += 1
    # source_id denotes the subscription; source_version is the immutable event sequence.
    invalidate_topics(db, 'plugin_event', row.id)
    for event in db.scalars(select(PluginEvent).where(PluginEvent.subscription_id == row.id,
            PluginEvent.status == 'accepted')):
        event.status, event.title, event.summary = 'invalidated', '', ''
        event.version += 1


def invalidate_subscriptions(db, user, installation_id):
    install = db.get(PluginInstallation, installation_id)
    for sub in db.scalars(select(EventSubscription).where(EventSubscription.user_id == user.id,
            EventSubscription.workspace_id == user.workspace_id, EventSubscription.status == 'active')):
        if sub.installation_id == installation_id or (install and install.plugin_id in sub.dependency_lock):
            invalidate_subscription(db, sub)


def expire_events():
    """Clear expired bodies and derived conversation sources, keeping dedupe markers."""
    with SessionLocal() as db:
        identities = list(db.scalars(select(PluginEvent.id).where(PluginEvent.status == 'accepted',
            PluginEvent.expires_at <= now()).order_by(PluginEvent.expires_at).limit(100)))
    for identity in identities:
        with SessionLocal.begin() as db:
            initial = db.get(PluginEvent, identity)
            user = db.get(User, initial.user_id) if initial else None
            if not user or user.workspace_id != initial.workspace_id:
                continue
            owner_lock(db, user)
            row = owned(db, PluginEvent, identity, user, True)
            if row.status != 'accepted' or row.expires_at > now():
                continue
            row.status, row.title, row.summary = 'expired', '', ''
            row.version += 1
            from .proactive import invalidate_topics
            invalidate_topics(db, 'plugin_event', row.subscription_id, source_version=row.sequence)


@router.get('/api/plugin-event-subscriptions', response_model=list[SubscriptionView])
def subscriptions(ctx: Context):
    db, user, _ = ctx
    return [subscription_view(db, user, row) for row in db.scalars(select(EventSubscription).where(
        EventSubscription.user_id == user.id, EventSubscription.workspace_id == user.workspace_id)
        .order_by(EventSubscription.created_at.desc()).limit(200))]


@router.post('/api/plugin-event-subscriptions', response_model=SubscriptionView)
def save_subscription(body: SubscriptionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    install, release, entry = event_capability(db, user, body.installation_id, body.capability_id)
    if len(set(body.event_types)) != len(body.event_types) or not set(body.event_types) <= set(entry['event_types']):
        raise HTTPException(422, '仅能授权清单中明确声明且不重复的事件类型')
    row = db.scalar(select(EventSubscription).where(EventSubscription.installation_id == install.id,
        EventSubscription.capability_id == body.capability_id, EventSubscription.user_id == user.id,
        EventSubscription.workspace_id == user.workspace_id))
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '事件订阅已变化，请重新加载后确认')
    if not body.secret and (not row or not row.encrypted_secret or row.status != 'active'):
        raise HTTPException(422, '新建或撤销后重新授权必须填写签名密钥')
    if row:
        encrypted = encrypt(body.secret) if body.secret else row.encrypted_secret
        invalidate_subscription(db, row)
    else:
        encrypted = encrypt(body.secret)
        row = EventSubscription(user_id=user.id, workspace_id=user.workspace_id, installation_id=install.id,
                                capability_id=body.capability_id)
        db.add(row)
    row.release_id, row.installation_version = release.id, install.version
    row.binding_digest, row.dependency_lock = digest(entry), dict(install.dependency_lock)
    row.encrypted_secret, row.status = encrypted, 'active'
    row.event_types, row.hourly_limit, row.daily_limit = body.event_types, body.hourly_limit, body.daily_limit
    row.updated_at = now()
    db.flush()
    audit(db, user, 'plugin_event_authorize', row.id)
    return subscription_view(db, user, row)


@router.post('/api/plugin-event-subscriptions/{identity}/revoke', response_model=SubscriptionView)
def revoke_subscription(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, EventSubscription, identity, user, True)
    if row.version != body.version:
        raise HTTPException(409, '事件订阅已变化，请重新加载')
    invalidate_subscription(db, row)
    audit(db, user, 'plugin_event_revoke', row.id)
    return subscription_view(db, user, row)


def signing_data(subscription_id, timestamp, external_id, raw):
    return (subscription_id + '\n' + timestamp + '\n' + external_id + '\n').encode('ascii') + raw


def ingest(subscription_id, timestamp, external_id, signature, raw):
    with SessionLocal.begin() as db:
        row = db.get(EventSubscription, subscription_id)
        if not row:
            raise HTTPException(401, '事件签名无效或订阅不可用')
        user = db.get(User, row.user_id)
        if not user or user.workspace_id != row.workspace_id:
            raise HTTPException(401, '事件签名无效或订阅不可用')
        owner_lock(db, user)
        db.refresh(row)
        if row.status != 'active' or not row.encrypted_secret:
            raise HTTPException(401, '事件签名无效或订阅不可用')
        secret = decrypt(row.encrypted_secret)
        expected = hmac.new(secret.encode('ascii'), signing_data(subscription_id, timestamp, external_id, raw), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or abs(now() - int(timestamp)) > 300:
            raise HTTPException(401, '事件签名无效或已过期')
        _, entry = available_subscription(db, user, row)
        body_digest = hashlib.sha256(raw).hexdigest()
        existing = db.scalar(select(PluginEvent).where(PluginEvent.subscription_id == row.id,
                                                     PluginEvent.external_id == external_id))
        if existing:
            if existing.body_digest != body_digest:
                raise HTTPException(409, '同一事件标识的内容已变化，不能覆盖')
            return {'id': existing.id, 'status': 'duplicate', 'note': '仅接收回执，不表示已联系或已读'}
        # Retain dedupe tombstones; bound receipts per owner, including deleted/expired events.
        if db.scalar(select(func.count()).select_from(PluginEvent).where(PluginEvent.user_id == user.id,
                PluginEvent.workspace_id == user.workspace_id)) >= 10000:
            raise HTTPException(409, '事件留存达到上限，请联系管理员处理归档')
        for seconds, limit in ((3600, row.hourly_limit), (86400, row.daily_limit)):
            if db.scalar(select(func.count()).select_from(PluginEvent).where(PluginEvent.subscription_id == row.id,
                    PluginEvent.created_at > now() - seconds)) >= limit:
                raise HTTPException(429, '事件接收额度已用完，未生成新话题')
        # All subscriptions share a durable owner budget; installations cannot multiply the ceiling.
        if db.scalar(select(func.count()).select_from(PluginEvent).where(PluginEvent.user_id == user.id,
                PluginEvent.workspace_id == user.workspace_id, PluginEvent.created_at > now() - 86400)) >= 500:
            raise HTTPException(429, '工作区今日事件接收额度已用完')
        try:
            def pairs(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise ValueError('Duplicate key')
                    result[key] = value
                return result
            payload = EventPayload.model_validate(json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite'))))
        except (ValueError, RecursionError, UnicodeError, ValidationError):
            raise HTTPException(422, '事件正文须符合类型、标题、摘要和时间约定，不含重复字段或额外指令') from None
        instant = now()
        if (payload.type not in row.event_types or payload.type not in entry['event_types'] or
                payload.occurred_at > instant + 30 or payload.occurred_at < instant - entry['ttl_seconds'] or
                not instant < payload.expires_at <= payload.occurred_at + entry['ttl_seconds']):
            raise HTTPException(422, '事件类型未授权，或事件时间／有效期不符合清单')
        if secret in raw.decode('utf-8') or any(secret in value for value in (payload.title, payload.summary, payload.type, external_id)):
            raise HTTPException(422, '事件正文包含签名凭据，拒绝保存')
        row.sequence += 1
        event = PluginEvent(user_id=user.id, workspace_id=user.workspace_id, subscription_id=row.id,
            installation_id=row.installation_id, release_id=row.release_id, subscription_version=row.version,
            external_id=external_id, body_digest=body_digest, sequence=row.sequence, event_type=payload.type,
            title=payload.title, summary=payload.summary, occurred_at=payload.occurred_at, expires_at=payload.expires_at)
        db.add(event)
        db.flush()
        from .proactive import ConversationTopic
        db.add(ConversationTopic(user_id=user.id, workspace_id=user.workspace_id, source_type='plugin_event',
            source_id=row.id, source_version=event.sequence, dedupe_key=f'plugin_event:{row.id}:{event.sequence}',
            title='插件事件：' + payload.title[:190], kind='interest', not_before=instant,
            expires_at=payload.expires_at, origin='plugin'))
        audit(db, user, 'plugin_event_accept', event.id)
        return {'id': event.id, 'status': 'accepted', 'note': '仅接收回执，核心另行检查联系偏好与时机'}


@router.post('/api/plugin-events/hooks/{identity}')
async def event_hook(identity: str, request: Request):
    # Cookie/CSRF are not event authentication; no browser-origin or query-secret entry point.
    timestamp = request.headers.get('x-lingxi-timestamp', '')
    external_id = request.headers.get('x-lingxi-event-id', '')
    signature = request.headers.get('x-lingxi-signature', '')
    for name in (b'x-lingxi-timestamp', b'x-lingxi-event-id', b'x-lingxi-signature', b'content-type'):
        if sum(key.lower() == name for key, _ in request.scope['headers']) != 1:
            raise HTTPException(401, '事件签名头缺失或重复')
    if (request.headers.get('origin') or request.query_params or not re.fullmatch(r'[a-f0-9]{32}', identity) or
            not re.fullmatch(r'\d{10}', timestamp) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,100}', external_id) or
            not re.fullmatch(r'sha256=[a-f0-9]{64}', signature) or abs(now()-int(timestamp)) > 300):
        raise HTTPException(401, '事件签名无效或已过期')
    if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json' or request.headers.get('content-encoding'):
        raise HTTPException(415, '仅支持未压缩的 application/json')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, '事件正文超过 12 KiB')
    return await run_in_threadpool(ingest, identity, timestamp, external_id, signature[7:], bytes(raw))


@router.get('/api/plugin-events', response_model=list[EventView])
def events(ctx: Context, limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    rows = db.scalars(select(PluginEvent).where(PluginEvent.user_id == user.id,
        PluginEvent.workspace_id == user.workspace_id).order_by(PluginEvent.created_at.desc()).limit(limit))
    return [event_view(db, user, row) for row in rows]


@router.post('/api/plugin-events/{identity}/delete', response_model=EventView)
def delete_event(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginEvent, identity, user, True)
    if row.version != body.version:
        raise HTTPException(409, '事件记录已变化，请重新加载')
    from .proactive import invalidate_topics
    row.status, row.title, row.summary = 'deleted', '', ''
    row.version += 1
    invalidate_topics(db, 'plugin_event', row.subscription_id, source_version=row.sequence)
    audit(db, user, 'plugin_event_delete', row.id)
    return event_view(db, user, row)
