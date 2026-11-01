"""Bounded inspection and explicitly confirmed removal in the private media spool.

No recursion, paths supplied by users, content reads, or automatic collection.
Unknown unlink outcomes are durable barriers and are never replayed.
"""
from contextlib import contextmanager
import os
import re
import shutil
import stat
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, cast, func, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal, now
from .models import Entity, Job, LoginSession, User
from .web import Context, audit, owned, view
from .tasks import digest

router = APIRouter()
ENTRY_LIMIT, BATCH_LIMIT = 10000, 20
REFERENCE_LIMIT = 100
BYTE_LIMIT = 200 * 1024**2
KEY = re.compile(r'([a-f0-9]{32})\.(blob|preview|json)\Z')
ACTIVE = ('queued', 'scanning', 'confirmed', 'cleaning')


class MediaStorageRun(Entity, Base):
    __tablename__ = 'media_storage_runs'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    login_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='queued')
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    results: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class StorageScanInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=8, max_length=100)
    days: int = Field(default=2, ge=2, le=365)


class StorageConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    digest: str = Field(pattern='^[a-f0-9]{64}$')
    acknowledgement: Literal['remove_untracked_media_files']


class StorageItem(BaseModel):
    key: str
    bytes: int
    modified_at: float


class StorageResult(StorageItem):
    status: str


class StorageSummary(BaseModel):
    entries: int = 0
    recognized: int = 0
    tracked: int = 0
    retained: int = 0
    recent: int = 0
    ignored: int = 0
    recognized_bytes: int = 0
    candidate_bytes: int = 0
    partial: bool = False
    has_more: bool = False
    reference_limited: bool = False


class StorageRunView(BaseModel):
    id: str
    status: str
    days: int
    created_at: float
    expires_at: float
    finished_at: float | None
    digest: str
    items: list[StorageItem]
    results: list[StorageResult]
    summary: StorageSummary
    error: str | None


class StorageOverview(BaseModel):
    records: int
    logical_bytes: int
    deleted_records: int
    disk_status: str
    free_bytes: int | None
    total_bytes: int | None


def owner_lock(db, user):
    db.scalar(select(User).where(User.id == user.id, User.workspace_id == user.workspace_id).with_for_update())
    from .backups import paused
    if paused(db):
        raise HTTPException(503, '维护或迁移中，暂不扫描或清理存储')


@contextmanager
def directory():
    """Linux dirfds pin the service spool; never follow a media/root symlink."""
    if os.name != 'posix' or os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
        raise HTTPException(503, '存储巡检只在受支持的 Linux 服务器上运行')
    rootfd = mediafd = None
    try:
        rootfd = os.open(settings().runtime_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.mkdir('media', mode=0o700, dir_fd=rootfd)
        except FileExistsError: pass
        mediafd = os.open('media', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        yield mediafd, (os.fstat(rootfd).st_dev, os.fstat(rootfd).st_ino,
                        os.fstat(mediafd).st_dev, os.fstat(mediafd).st_ino)
    except OSError:
        raise HTTPException(503, '附件专用目录不可用或不是受控目录，未进行清理') from None
    finally:
        if mediafd is not None: os.close(mediafd)
        if rootfd is not None: os.close(rootfd)


def fingerprint(s):
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size,
                modified_ns=s.st_mtime_ns, changed_ns=s.st_ctime_ns, links=s.st_nlink)


def candidate(s, cutoff):
    return (stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and 0 <= s.st_size <= BYTE_LIMIT
            and max(s.st_mtime, s.st_ctime) <= cutoff)


def referenced(db, identity):
    """Any workspace or historical reference retains the file, without reading payloads."""
    from .models import Candidate, ChatRun, Memory
    from .media import Media, MediaLink
    from .media_retention import MediaProtection
    from .channel_media import ChannelAttachment
    from .channel_tasks import ChannelTask
    from .channel_artifacts import ArtifactReply
    from .delegation import AgentNode, AgentRun
    from .tasks import WorkTask
    from .plugins import PluginInvocation
    direct = ((Media, Media.id), (MediaLink, MediaLink.media_id),
              (MediaProtection, MediaProtection.media_id), (ChannelAttachment, ChannelAttachment.media_id))
    if any(db.scalar(select(col).where(col == identity).limit(1)) is not None for _, col in direct):
        return True
    for entity, columns in ((ChatRun, (ChatRun.config,)), (AgentRun, (AgentRun.config,)),
            (AgentNode, (AgentNode.config, AgentNode.context)),
            (WorkTask, (WorkTask.config, WorkTask.input, WorkTask.result)),
            (ChannelTask, (ChannelTask.media_refs,)), (ArtifactReply, (ArtifactReply.plan,)),
            (Memory, (Memory.provenance, Memory.source_ids)), (Candidate, (Candidate.origin, Candidate.source_id)),
            (PluginInvocation, (PluginInvocation.config, PluginInvocation.input, PluginInvocation.result)),
            (Job, (Job.payload,))):
        if db.scalar(select(entity.id).where(or_(*(cast(c, Text).contains(identity) for c in columns))).limit(1)):
            return True
    return False


def public(row):
    status = 'expired' if row.status == 'review' and row.expires_at <= now() else row.status
    return dict(id=row.id, status=status, days=row.days, created_at=row.created_at, expires_at=row.expires_at,
        finished_at=row.finished_at, digest=digest(row.plan),
        items=[{k:item[k] for k in StorageItem.model_fields} for item in row.plan.get('items', [])],
        results=row.results, summary=row.plan.get('summary', {}), error=row.error)


@router.get('/api/admin/media-storage', response_model=StorageOverview)
def overview(ctx: Context):
    from .media import Media
    db, user, _ = ctx
    scope = (Media.user_id == user.id, Media.workspace_id == user.workspace_id)
    count, size = db.execute(select(func.count(), func.coalesce(func.sum(Media.size), 0)).where(*scope)).one()
    deleted = db.scalar(select(func.count()).select_from(Media).where(*scope, Media.status == 'deleted'))
    try:
        usage = shutil.disk_usage(settings().runtime_dir)
        state, free, total = ('warning' if usage.free < max(1024**3, usage.total//20) else 'ready'), usage.free, usage.total
    except OSError:
        state, free, total = 'unavailable', None, None
    return dict(records=count, logical_bytes=size, deleted_records=deleted, disk_status=state, free_bytes=free, total_bytes=total)


@router.get('/api/admin/media-storage/runs', response_model=list[StorageRunView])
def runs(ctx: Context):
    db, user, _ = ctx
    return [public(r) for r in db.scalars(select(MediaStorageRun).where(MediaStorageRun.user_id == user.id,
        MediaStorageRun.workspace_id == user.workspace_id).order_by(MediaStorageRun.created_at.desc()).limit(20))]


@router.post('/api/admin/media-storage/scans', response_model=StorageRunView)
def start(body: StorageScanInput, ctx: Context):
    from .memory import enqueue
    db, user, login = ctx
    owner_lock(db, user)
    old = db.scalar(select(MediaStorageRun).where(MediaStorageRun.user_id == user.id, MediaStorageRun.request_id == body.request_id))
    if old:
        if old.workspace_id != user.workspace_id or old.login_id != login.id or old.request_hash != digest(body.model_dump()):
            raise HTTPException(409, '扫描编号已用于其他登录或范围')
        return public(old)
    if db.scalar(select(MediaStorageRun.id).where(MediaStorageRun.user_id == user.id, MediaStorageRun.status.in_(ACTIVE))):
        raise HTTPException(409, '已有存储巡检或清理正在执行，请等待真实结果')
    row = MediaStorageRun(user_id=user.id, workspace_id=user.workspace_id, login_id=login.id,
        request_id=body.request_id, request_hash=digest(body.model_dump()), days=body.days)
    db.add(row); db.flush()
    enqueue(db, 'media-storage-scan:'+row.id, 'media_storage_scan', {'storage_run_id': row.id})
    audit(db, user, 'media_storage_scan', row.id)
    return public(row)


@router.post('/api/admin/media-storage/runs/{identity}/confirm', response_model=StorageRunView)
def confirm(identity: str, body: StorageConfirm, ctx: Context):
    from .memory import enqueue
    db, user, login = ctx
    owner_lock(db, user)
    row = owned(db, MediaStorageRun, identity, user, lock=True)
    if row.login_id != login.id or digest(row.plan) != body.digest:
        raise HTTPException(409, '扫描不属于当前登录或内容已变化')
    if row.status in ('confirmed', 'cleaning', 'completed', 'partial', 'unknown'):
        return public(row)
    if row.status != 'review' or row.expires_at <= now() or not row.plan.get('items'):
        raise HTTPException(409, '扫描已过期、无候选或不可清理，请重新扫描')
    row.status, row.expires_at = 'confirmed', now()+3600
    row.results = [{k:item[k] for k in StorageItem.model_fields} | {'status':'pending'} for item in row.plan['items']]
    enqueue(db, 'media-storage-cleanup:'+row.id, 'media_storage_cleanup', {'storage_run_id': row.id})
    audit(db, user, 'media_storage_confirm', row.id)
    return public(row)


@router.post('/api/admin/media-storage/runs/{identity}/cancel', response_model=StorageRunView)
def cancel(identity: str, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, MediaStorageRun, identity, user, lock=True)
    if row.status not in (*ACTIVE, 'review'): return public(row)
    row.status, row.expires_at = 'cancelled', 0
    row.results = [r | {'status':'unknown' if r['status']=='deleting' else 'skipped'}
        if r['status'] in ('deleting','pending') else r for r in row.results]
    audit(db, user, 'media_storage_cancel', row.id)
    return public(row)


def valid(db, row):
    user = db.get(User, row.user_id)
    if not user or user.workspace_id != row.workspace_id:
        raise HTTPException(409, '巡检主体已失效')
    owner_lock(db, user)
    db.refresh(row)
    login = db.get(LoginSession, row.login_id, populate_existing=True)
    if not login or login.user_id != user.id or login.workspace_id != user.workspace_id or login.expires_at <= now():
        raise HTTPException(409, '巡检登录已撤销或失效，未继续清理')
    return user


def execute_scan(job):
    from .worker import owns
    with SessionLocal.begin() as db:
        owns(db, job)
        row = db.get(MediaStorageRun, job.payload['storage_run_id'])
        if not row or row.status not in ('queued', 'scanning'): return
        valid(db, row)
        if row.status not in ('queued', 'scanning'): return
        row.status = 'scanning'
    with SessionLocal.begin() as db:
        owns(db, job)
        row = db.get(MediaStorageRun, job.payload['storage_run_id'])
        if not row or row.status not in ('queued', 'scanning'): return
        valid(db, row)
        if row.status not in ('queued', 'scanning'): return
        cutoff = now()-row.days*86400
        summary = dict(entries=0, recognized=0, tracked=0, retained=0, recent=0, ignored=0,
                       recognized_bytes=0, candidate_bytes=0, partial=False, has_more=False, reference_limited=False)
        items = []
        with directory() as (fd, location):
            inventory = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if summary['entries'] >= ENTRY_LIMIT:
                        summary['partial'] = True; break
                    summary['entries'] += 1
                    match = KEY.fullmatch(entry.name)
                    try: s = entry.stat(follow_symlinks=False)
                    except OSError: summary['ignored'] += 1; continue
                    if not match or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                        summary['ignored'] += 1; continue
                    inventory.append((entry.name, match[1], s))
            from .media import Media
            identities = list({identity for _, identity, _ in inventory})
            tracked = set()
            for offset in range(0, len(identities), 500):
                tracked.update(db.scalars(select(Media.id).where(Media.id.in_(identities[offset:offset+500]))))
            checked = 0
            for key, identity, s in inventory:
                summary['recognized'] += 1; summary['recognized_bytes'] += s.st_size
                if identity in tracked: summary['tracked'] += 1; continue
                if not candidate(s, cutoff): summary['recent'] += 1; continue
                if len(items) >= BATCH_LIMIT or summary['candidate_bytes']+s.st_size > BYTE_LIMIT:
                    summary['has_more'] = True; continue
                if checked >= REFERENCE_LIMIT:
                    summary['reference_limited'] = summary['has_more'] = True; continue
                checked += 1
                if referenced(db, identity): summary['retained'] += 1; continue
                items.append(dict(key=key, bytes=s.st_size, modified_at=s.st_mtime, fingerprint=fingerprint(s)))
                summary['candidate_bytes'] += s.st_size
            row.plan = dict(location=list(location), cutoff=cutoff, items=items, summary=summary)
        row.status, row.expires_at = 'review', now()+300


def execute_cleanup(job):
    from .worker import owns
    for index in range(BATCH_LIMIT):
        with directory() as (fd, location):
            with SessionLocal.begin() as db:
                owns(db, job)
                row = db.get(MediaStorageRun, job.payload['storage_run_id'], populate_existing=True)
                if not row or row.status not in ('confirmed', 'cleaning'): return
                valid(db, row)
                owns(db, job)
                if row.status not in ('confirmed', 'cleaning'): return
                if index >= len(row.results): break
                results = [dict(v) for v in row.results]
                if results[index]['status'] == 'deleting':
                    results[index]['status'] = 'unknown'; row.results = results; continue
                if results[index]['status'] != 'pending': continue
                item = row.plan['items'][index]
                match = KEY.fullmatch(item['key'])
                if row.expires_at <= now() or not match or referenced(db, match[1]):
                    results[index]['status'] = 'skipped'; row.results = results; continue
                try: snapshot = os.stat(item['key'], dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    results[index]['status'] = 'missing'; row.results = results; continue
                if list(location) != row.plan['location'] or not candidate(snapshot, row.plan['cutoff']) or fingerprint(snapshot) != item['fingerprint']:
                    results[index]['status'] = 'skipped'; row.results = results; continue
                results[index]['status'] = 'deleting'; row.results = results; row.status = 'cleaning'
                # Commit intent before filesystem I/O; an uncertain result is never replayed.
            with SessionLocal.begin() as db:
                owns(db, job)
                row = db.get(MediaStorageRun, job.payload['storage_run_id'], populate_existing=True)
                if not row or row.status != 'cleaning': return
                user = valid(db, row)
                owns(db, job)
                if row.status != 'cleaning': return
                results = [dict(v) for v in row.results]
                if results[index]['status'] != 'deleting': continue
                if row.expires_at <= now() or referenced(db, match[1]): outcome = 'skipped'
                else:
                    try:
                        latest = os.stat(item['key'], dir_fd=fd, follow_symlinks=False)
                        if fingerprint(latest) != item['fingerprint'] or not candidate(latest, row.plan['cutoff']): outcome = 'skipped'
                        else: os.unlink(item['key'], dir_fd=fd); outcome = 'removed'
                    except FileNotFoundError: outcome = 'missing'
                    except OSError: outcome = 'failed'
                results[index]['status'] = outcome; row.results = results
                audit(db, user, 'media_storage_'+outcome, row.id)
    with SessionLocal.begin() as db:
        owns(db, job)
        row = db.get(MediaStorageRun, job.payload['storage_run_id'])
        if row and row.status in ('confirmed', 'cleaning'):
            states = {r['status'] for r in row.results}
            row.status = 'unknown' if states & {'deleting', 'unknown'} else 'completed' if states <= {'removed','missing'} else 'partial'
            row.finished_at = now()


def fail(db, job):
    row = db.get(MediaStorageRun, job.payload['storage_run_id'])
    if row and row.status in ACTIVE:
        row.results = [r | {'status':'unknown' if r['status']=='deleting' else 'skipped'}
            if r['status'] in ('deleting','pending') else r for r in row.results]
        row.status = 'unknown' if any(r['status']=='unknown' for r in row.results) else 'failed'
        row.error, row.finished_at = '巡检或清理中断，请查看逐项结果；未自动重试删除', now()


def checkpoint(db):
    return [view(r) | {'login_id':'revoked', 'status':'cancelled' if r.status in (*ACTIVE, 'review') else r.status, 'expires_at':0,
        'results':[i | {'status':'unknown' if i['status']=='deleting' else 'skipped'}
            if i['status'] in ('deleting','pending') else i for i in r.results]}
        for r in db.scalars(select(MediaStorageRun))]


def restore(db, records):
    for data in records:
        status = 'cancelled' if data['status'] in (*ACTIVE, 'review') else data['status']
        db.merge(MediaStorageRun(**(data | {'login_id':'revoked','status':status,'expires_at':0})))
