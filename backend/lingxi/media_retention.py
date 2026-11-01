"""Opt-in collection of old, unused Web uploads. Any possible reference retains a file."""
import hashlib
import json
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Float, Integer, String, Text, cast, exists, literal, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal, now, uid
from .models import Entity, User
from .media import MediaView
from .security import decrypt, encrypt
from .web import Context, audit, owned, view

router = APIRouter()
BATCH_LIMIT = 20
INTERVAL = 3600


class MediaRetentionPolicy(Base):
    __tablename__ = 'media_retention_policies'
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    days: Mapped[int] = mapped_column(Integer, default=7)
    last_checked: Mapped[float] = mapped_column(Float, default=0)
    last_marked: Mapped[int] = mapped_column(Integer, default=0)


class MediaProtection(Base):
    __tablename__ = 'media_protections'
    media_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    protected: Mapped[bool] = mapped_column(Boolean, default=True)


class MediaRetentionRun(Entity, Base):
    __tablename__ = 'media_retention_runs'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    marked: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RetentionSettings(Strict):
    enabled: bool = False
    days: int = Field(default=7, ge=1, le=365)


class RetentionInput(RetentionSettings):
    version: int = Field(ge=0)


class RetentionView(RetentionInput):
    last_checked: float
    last_marked: int


class RetentionPreview(BaseModel):
    before: RetentionView
    after: RetentionSettings
    token: str
    expires_at: float


class RetentionConfirm(Strict):
    token: str = Field(min_length=10, max_length=16384)
    acknowledgement: Literal['delete_old_unreferenced_web_uploads']


class ProtectionInput(Strict):
    version: int = Field(ge=0)
    media_version: int = Field(ge=1)
    protected: bool


class SweepItem(BaseModel):
    id: str
    filename: str
    version: int
    size: int
    created_at: float


class SweepPreview(BaseModel):
    id: str
    items: list[SweepItem]
    bytes: int
    has_more: bool
    token: str
    expires_at: float
    days: int


class SweepView(BaseModel):
    id: str
    kind: str
    marked: int
    skipped: int
    bytes: int
    created_at: float


def locked_owner(db, user):
    db.scalar(select(User).where(User.id == user.id, User.workspace_id == user.workspace_id).with_for_update())
    from .backups import paused
    if paused(db):
        raise HTTPException(503, '维护中，请稍后重试回收设置或确认')


def current(db, user):
    row = db.get(MediaRetentionPolicy, user.id, populate_existing=True)
    if row and row.workspace_id != user.workspace_id:
        raise HTTPException(409, '回收配置范围不一致，请检查工作空间')
    return RetentionView(**({key:getattr(row,key) for key in RetentionView.model_fields} if row else
        dict(version=0, enabled=False, days=7, last_checked=0., last_marked=0)))


def protection(db, item):
    row = db.get(MediaProtection, item.id)
    if row and row.user_id == item.user_id and row.workspace_id == item.workspace_id:
        return {'protected': row.protected, 'protection_version': row.version}
    return {'protected': False, 'protection_version': 0}


def ticket(user, login, purpose, data):
    return encrypt(json.dumps(dict(purpose=purpose, user=user.id, workspace=user.workspace_id,
        session=login.id, expires_at=now()+300, **data), sort_keys=True))


def unticket(token, user, login, purpose):
    try:
        data = json.loads(decrypt(token))
        assert data['purpose'] == purpose and data['user'] == user.id
        assert data['workspace'] == user.workspace_id and data['session'] == login.id
        assert now() <= data['expires_at'] <= now()+301
        return data
    except (InvalidToken, ValueError, KeyError, TypeError, AssertionError):
        raise HTTPException(409, '回收预览已过期或不属于当前登录，请重新预览') from None


def referenced(identity, user):
    """Correlated EXISTS only; no payloads loaded. Text matches deliberately retain too much.

    IDs are generated hexadecimal keys, so JSON formatting/escaping cannot conceal
    a reference. Historical, invalidated and deleted consumers still retain files.
    Matching text in one of these provenance columns also retains, never deletes.
    """
    from .models import Candidate, ChatRun, Job, Memory
    from .media import Media, MediaLink
    from .channel_media import ChannelAttachment
    from .channel_tasks import ChannelTask
    from .channel_artifacts import ArtifactReply
    from .delegation import AgentNode, AgentRun
    from .tasks import WorkTask
    from .plugins import PluginInvocation

    def has(entity, *columns, extra=()):
        conditions = [entity.workspace_id == user.workspace_id, *extra]
        if hasattr(entity, 'user_id'):
            conditions.append(entity.user_id == user.id)
        return select(literal(1)).select_from(entity).where(*conditions,
            or_(*(cast(column, Text).contains(identity) for column in columns))).exists().correlate(Media)

    return or_(
        exists(select(literal(1)).where(MediaLink.media_id == identity)).correlate(Media),
        exists(select(literal(1)).where(ChannelAttachment.media_id == identity)).correlate(Media),
        has(ChatRun, ChatRun.config), has(AgentRun, AgentRun.config),
        select(literal(1)).select_from(AgentNode).join(AgentRun, AgentRun.id == AgentNode.run_id).where(
            AgentRun.user_id == user.id, AgentRun.workspace_id == user.workspace_id,
            AgentNode.workspace_id == user.workspace_id,
            or_(cast(AgentNode.config, Text).contains(identity), cast(AgentNode.context, Text).contains(identity)))
            .exists().correlate(Media),
        has(WorkTask, WorkTask.config, WorkTask.input, WorkTask.result),
        has(ChannelTask, ChannelTask.media_refs), has(ArtifactReply, ArtifactReply.plan), has(Memory, Memory.provenance, Memory.source_ids),
        has(Candidate, Candidate.origin, Candidate.source_id),
        has(PluginInvocation, PluginInvocation.config, PluginInvocation.input, PluginInvocation.result),
        has(Job, Job.payload, extra=(Job.status.in_(('pending', 'running')),)))


def eligible(user, cutoff):
    from .media import Media
    held = select(literal(1)).where(MediaProtection.media_id == Media.id,
        MediaProtection.user_id == user.id, MediaProtection.workspace_id == user.workspace_id,
        MediaProtection.protected.is_(True)).exists().correlate(Media)
    return select(Media).where(Media.user_id == user.id, Media.workspace_id == user.workspace_id,
        Media.status.in_(('ready', 'failed')), Media.created_at <= cutoff,
        cast(Media.source, Text) == '{}', ~held, ~referenced(Media.id, user))


@router.get('/api/admin/media-retention', response_model=RetentionView)
def get_policy(ctx: Context):
    db, user, _ = ctx
    return current(db, user)


@router.post('/api/admin/media-retention/previews', response_model=RetentionPreview)
def policy_preview(body: RetentionInput, ctx: Context):
    db, user, login = ctx
    before = current(db, user)
    if before.version != body.version:
        raise HTTPException(409, '回收配置已变化，请重新加载')
    after = body.model_dump(exclude={'version'})
    token = ticket(user, login, 'media-retention-policy', dict(version=body.version, after=after))
    return dict(before=before, after=after, token=token, expires_at=now()+300)


@router.put('/api/admin/media-retention', response_model=RetentionView)
def save_policy(body: RetentionConfirm, ctx: Context):
    db, user, login = ctx
    data = unticket(body.token, user, login, 'media-retention-policy')
    after = RetentionSettings.model_validate(data['after'])
    locked_owner(db, user)
    before = current(db, user)
    if before.version != data['version']:
        raise HTTPException(409, '回收配置已变化，请重新预览')
    row = db.get(MediaRetentionPolicy, user.id)
    if not row:
        row = MediaRetentionPolicy(user_id=user.id, workspace_id=user.workspace_id)
        db.add(row)
    row.version, row.enabled, row.days = before.version+1, after.enabled, after.days
    # Changing a rule starts a fresh interval; saving never deletes inline.
    row.last_checked, row.last_marked = now(), 0
    db.flush()
    audit(db, user, 'media_retention_policy')
    return current(db, user)


@router.put('/api/media/{identity}/protection', response_model=MediaView)
def set_protection(identity: str, body: ProtectionInput, ctx: Context):
    from .media import Media, public_media
    db, user, _ = ctx
    locked_owner(db, user)
    item = owned(db, Media, identity, user, lock=True)
    before = protection(db, item)
    if item.status == 'deleted' or item.version != body.media_version or before['protection_version'] != body.version:
        raise HTTPException(409, '附件或保留设置已变化，请重新加载')
    row = db.get(MediaProtection, identity)
    if row and (row.user_id != user.id or row.workspace_id != user.workspace_id):
        raise HTTPException(409, '附件保留范围不一致')
    if not row:
        row = MediaProtection(media_id=identity, user_id=user.id, workspace_id=user.workspace_id)
        db.add(row)
    row.version, row.protected = before['protection_version']+1, body.protected
    db.flush()
    audit(db, user, 'media_protection', identity)
    return public_media(item)


@router.post('/api/admin/media-retention/sweep-previews', response_model=SweepPreview)
def sweep_preview(ctx: Context):
    db, user, login = ctx
    policy = current(db, user)
    cutoff = now()-policy.days*86400
    from .media import Media
    rows = list(db.scalars(eligible(user, cutoff).order_by(Media.created_at, Media.id).limit(BATCH_LIMIT+1)))
    selected = rows[:BATCH_LIMIT]
    identity = uid()
    token = ticket(user, login, 'media-retention-sweep', dict(id=identity, version=policy.version, cutoff=cutoff,
        items=[{'id':r.id, 'version':r.version} for r in selected]))
    return dict(id=identity, items=[{key:getattr(row,key) for key in SweepItem.model_fields} for row in selected],
        bytes=sum(row.size for row in selected), has_more=len(rows)>BATCH_LIMIT, token=token, expires_at=now()+300, days=policy.days)


def mark(db, user, rows, *, identity, request_hash, kind, expected_count):
    from .media import forget_media
    for item in rows:
        forget_media(db, item)
        audit(db, user, 'media_recycled', item.id)
    run = MediaRetentionRun(id=identity, user_id=user.id, workspace_id=user.workspace_id, request_hash=request_hash,
        kind=kind, marked=len(rows), skipped=expected_count-len(rows), bytes=sum(item.size for item in rows))
    db.add(run)
    db.flush()
    return {key:getattr(run,key) for key in SweepView.model_fields}


@router.post('/api/admin/media-retention/sweeps', response_model=SweepView)
def confirm_sweep(body: RetentionConfirm, ctx: Context):
    db, user, login = ctx
    data = unticket(body.token, user, login, 'media-retention-sweep')
    locked_owner(db, user)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    previous = db.get(MediaRetentionRun, data['id'])
    if previous:
        if previous.user_id != user.id or previous.workspace_id != user.workspace_id or previous.request_hash != digest:
            raise HTTPException(409, '回收请求已使用，请重新预览')
        return {key:getattr(previous,key) for key in SweepView.model_fields}
    if current(db, user).version != data['version']:
        raise HTTPException(409, '回收配置已变化，请重新预览')
    from .media import Media
    versions = {item['id']:item['version'] for item in data['items']}
    rows = list(db.scalars(eligible(user, data['cutoff']).where(Media.id.in_(versions)).with_for_update())) if versions else []
    rows = [row for row in rows if row.version == versions[row.id]]
    return mark(db, user, rows, identity=data['id'], request_hash=digest, kind='manual', expected_count=len(versions))


@router.get('/api/admin/media-retention/runs', response_model=list[SweepView])
def runs(ctx: Context):
    db, user, _ = ctx
    rows = db.scalars(select(MediaRetentionRun).where(MediaRetentionRun.user_id == user.id,
        MediaRetentionRun.workspace_id == user.workspace_id).order_by(MediaRetentionRun.created_at.desc(), MediaRetentionRun.id.desc()).limit(20))
    return [{key:getattr(row,key) for key in SweepView.model_fields} for row in rows]


def sweep():
    from .backups import paused
    from .media import Media
    with SessionLocal() as db:
        identities = list(db.scalars(select(MediaRetentionPolicy.user_id).where(MediaRetentionPolicy.enabled.is_(True)).limit(100)))
    for identity in identities:
        with SessionLocal.begin() as db:
            if paused(db):
                return
            user = db.scalar(select(User).where(User.id == identity).with_for_update())
            if not user or paused(db):
                continue
            policy = db.get(MediaRetentionPolicy, identity, populate_existing=True)
            if (not policy or not policy.enabled or policy.workspace_id != user.workspace_id
                    or policy.last_checked > now()-INTERVAL):
                continue
            rows = list(db.scalars(eligible(user, now()-policy.days*86400).order_by(Media.created_at, Media.id)
                                   .limit(BATCH_LIMIT).with_for_update()))
            policy.last_checked, policy.last_marked = now(), len(rows)
            if rows:
                mark(db, user, rows, identity=uid(), request_hash='automatic', kind='automatic', expected_count=len(rows))


def checkpoint(db):
    return {'policies':[view(row) for row in db.scalars(select(MediaRetentionPolicy))],
            'protections':[view(row) for row in db.scalars(select(MediaProtection))]}


def validate_checkpoint(data, user):
    if not isinstance(data, dict) or set(data) != {'policies','protections'}:
        raise ValueError('Invalid media retention checkpoint')
    seen = set()
    for record in data.get('policies', []):
        if (set(record) != set(c.name for c in MediaRetentionPolicy.__table__.columns)
                or record['user_id'] != user['id'] or record['workspace_id'] != user['workspace_id']
                or type(record['version']) is not int or record['version'] < 1 or record['user_id'] in seen):
            raise ValueError('Invalid retention owner or version')
        seen.add(record['user_id'])
        RetentionSettings.model_validate({key:record[key] for key in RetentionSettings.model_fields})
    seen = set()
    for record in data.get('protections', []):
        if (set(record) != set(c.name for c in MediaProtection.__table__.columns)
                or record['user_id'] != user['id'] or record['workspace_id'] != user['workspace_id'] or type(record['version']) is not int
                or record['version'] < 1 or type(record['protected']) is not bool
                or not isinstance(record['media_id'], str) or record['media_id'] in seen):
            raise ValueError('Invalid media protection checkpoint')
        seen.add(record['media_id'])


def restore(db, data):
    for record in data.get('protections', []):
        db.merge(MediaProtection(**(record | {'version':record['version']+1})))
    for record in data.get('policies', []):
        db.merge(MediaRetentionPolicy(**(record | {'enabled':False,'version':record['version']+1,'last_checked':0.,'last_marked':0})))
    # Invalidate previews even for policies present only in the restored DB.
    db.flush()
    for row in db.scalars(select(MediaRetentionPolicy)):
        row.enabled = False
        row.version += 1
        row.last_checked, row.last_marked = 0., 0
