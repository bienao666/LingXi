"""Owner-scoped, versioned read-only directory grants. A mount is not a grant."""
import json
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, Float, Integer, String, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import LoginSession, User
from .security import decrypt, encrypt
from .web import Context, audit, view

router = APIRouter()
REVOKED = '服务器资源授权已变化；原任务与派生上下文停止使用，请核对范围后新建任务'


class ResourcePolicy(Base):
    __tablename__ = 'resource_policies'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    roots: Mapped[list] = mapped_column(JSON, default=list)
    read_roots: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


def within(path, root):
    return root == '.' or path == root or path.startswith(root + '/')


class ResourceScope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    roots: list[str] = Field(max_length=20)
    read_roots: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('roots', 'read_roots')
    @classmethod
    def directories(cls, values):
        from .tasks import FileListInput
        paths = [FileListInput(path=v).path for v in values]
        if any(any(ord(c) < 32 or ord(c) == 127 for c in p) for p in paths):
            raise ValueError('目录不能包含控制字符')
        if any(within(p, q) or within(q, p) for i, p in enumerate(paths) for q in paths[i+1:]):
            raise ValueError('请移除重复或互相包含的目录')
        return sorted(paths)

    @model_validator(mode='after')
    def read_subset(self):
        if any(not any(within(path, root) for root in self.roots) for path in self.read_roots):
            raise ValueError('正文目录必须位于允许列出元数据的目录之内')
        return self


class ResourcePolicyView(ResourceScope):
    version: int
    mounted: bool
    available: bool
    reason: str | None
    updated_at: float | None
    mode: Literal['directory_metadata_only', 'scoped_document_read'] = 'directory_metadata_only'


class ResourcePolicyInput(ResourceScope):
    version: int = Field(ge=0)


class ResourcePolicyPreview(BaseModel):
    before: ResourcePolicyView
    after: ResourceScope
    token: str
    expires_at: float
    note: str


class ResourcePolicyConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=10, max_length=32768)
    acknowledgement: Literal['invalidate_previous_resource_contexts']


def current(db, user, lock=False):
    from .tasks import resource_available
    query = select(ResourcePolicy).where(ResourcePolicy.workspace_id == user.workspace_id,
        ResourcePolicy.user_id == user.id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    mounted = resource_available()
    roots = list(row.roots) if row and user.password_hash else []
    reason = ('请先在服务器部署受控资料卷；当前不能安全访问目录' if not mounted else
              '请在系统设置的资源授权中明确允许目录' if not roots else None)
    return ResourcePolicyView(roots=roots, read_roots=list(row.read_roots) if roots else [],
        mode="scoped_document_read" if row and row.read_roots and roots else "directory_metadata_only", version=row.version if row else 0,
        mounted=mounted, available=mounted and bool(roots), reason=reason,
        updated_at=row.updated_at if row else None)


def snapshot(db, user, path=None, *, read=False):
    policy = current(db, user)
    if not policy.available:
        raise HTTPException(403 if policy.mounted else 409, policy.reason)
    ref = {'version': policy.version, 'user_id': user.id, 'workspace_id': user.workspace_id,
           'roots': policy.roots}
    if read:
        if not policy.read_roots:
            raise HTTPException(403, '请先在资源授权中单独允许正文目录；目录清单授权不包含正文')
        ref |= {'mode': 'document_read', 'read_roots': policy.read_roots}
    if path is not None:
        check_path(ref, path)
    return ref


def valid(db, ref, user=None):
    if not ref or not isinstance(ref, dict):
        return False
    owner = user or db.get(User, ref.get('user_id'))
    if not owner or not owner.password_hash or owner.id != ref.get('user_id') or owner.workspace_id != ref.get('workspace_id'):
        return False
    policy = current(db, owner)
    # A temporary unmounted volume does not rewrite a historical observation.
    return bool(policy.roots and policy.version == ref.get('version') and policy.roots == ref.get('roots')
        and (ref.get('mode') != 'document_read' or (policy.read_roots and policy.read_roots == ref.get('read_roots'))))


def check_path(ref, path):
    from .tasks import FileListInput
    path = FileListInput(path=path).path
    if not any(within(path, root) for root in ref.get('read_roots' if ref.get('mode') == 'document_read' else 'roots', [])):
        raise HTTPException(403, '目录不在本次已授权范围内；请在资源授权中核对目录，不能列出其父目录或同名前缀目录')
    return path


def require_snapshot(db, user, config, path=None):
    ref = config.get('resource_policy')
    if user is None or not valid(db, ref, user):
        raise HTTPException(410, REVOKED)
    if path is not None:
        check_path(ref, path)
    return ref


def invalidate_task(db, task, reason=REVOKED):
    from .tasks import TaskAttempt, state
    from .models import ChatRun, Message
    from .memory import invalidate_source, invalidate_run
    from .chat_actions import for_target
    if task.config.get('resource_revoked'):
        return
    from .media import Media, forget_media
    if identity := task.result.get('media_id'):
        if (item := db.get(Media, identity)) and item.status != 'deleted':
            forget_media(db, item)
    task.result, task.error, task.cancel_requested = {}, reason, True
    task.config = task.config | {'resource_revoked': True}
    state(db, task, 'cancelled', reason=reason)
    for attempt in db.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task.id)):
        attempt.receipt = {}
        if attempt.status == 'running':
            attempt.status, attempt.finished_at, attempt.error = 'cancelled', now(), reason
    if task.result_message_id and (message := db.get(Message, task.result_message_id)):
        message.status, message.content = 'invalidated', ''
        invalidate_source(db, message.id)
    if (action := for_target(db, task.id)) and (parent := db.get(ChatRun, action.run_id)):
        invalidate_run(db, parent)


def expire(db, user):
    """Also catches legacy jobs and creations racing a scope change; no implicit regrant."""
    from .tasks import WorkTask
    from .delegation import AgentRun, revoke_run
    policy = current(db, user)
    task_version = WorkTask.config['resource_policy']['version'].as_integer()
    query = select(WorkTask).where(WorkTask.user_id == user.id, WorkTask.workspace_id == user.workspace_id,
        WorkTask.skill_id.in_(['server.files', 'server.read']),
        or_(WorkTask.config['resource_revoked'].as_boolean().is_(None), WorkTask.config['resource_revoked'].as_boolean() == False),
        or_(task_version.is_(None), task_version != policy.version))
    for task in db.scalars(query.with_for_update().execution_options(populate_existing=True)):
        if not valid(db, task.config.get('resource_policy'), user):
            invalidate_task(db, task)
    run_version = AgentRun.config['resource_policy']['version'].as_integer()
    query = select(AgentRun).where(AgentRun.user_id == user.id, AgentRun.workspace_id == user.workspace_id,
        AgentRun.config['allow_files'].as_boolean() == True, AgentRun.status != 'invalidated',
        or_(run_version.is_(None), run_version != policy.version))
    for run in db.scalars(query.with_for_update().execution_options(populate_existing=True)):
        if not valid(db, run.config.get('resource_policy'), user):
            revoke_run(db, run, REVOKED, invalidated=True)
            if identity := run.config.get('qq_task_id'):
                from .channel_tasks import ChannelTask, discard
                if task := db.get(ChannelTask, identity):
                    discard(db, task, REVOKED)


def expire_all(db):
    for user in db.scalars(select(User).where(User.password_hash != '')):
        expire(db, user)


def validate_directories(roots):
    from .tasks import open_resource_directory
    import os
    for root in roots:
        descriptor = open_resource_directory(root, lambda: None)
        os.close(descriptor)


@router.get('/api/resource-policy', response_model=ResourcePolicyView)
def resource_policy(ctx: Context):
    db, user, _ = ctx
    return current(db, user)


@router.post('/api/admin/resource-policy/previews', response_model=ResourcePolicyPreview)
def preview(body: ResourcePolicyInput, ctx: Context):
    db, user, session = ctx
    before = current(db, user)
    if body.version != before.version:
        raise HTTPException(409, '资源授权已变化，请重新加载')
    if body.roots == before.roots and body.read_roots == before.read_roots:
        raise HTTPException(409, '目录范围没有变化，无需保存')
    validate_directories(body.roots + body.read_roots)
    expiry = now() + 300
    token = encrypt(json.dumps({'purpose': 'resource-policy', 'user': user.id, 'workspace': user.workspace_id,
        'session': session.id, 'version': before.version, 'roots': body.roots, 'read_roots': body.read_roots, 'expires_at': expiry}, sort_keys=True))
    return dict(before=before, after=ResourceScope(roots=body.roots, read_roots=body.read_roots), token=token, expires_at=expiry,
        note='清单目录仅授权元数据；正文目录另行允许读取支持文档并保存受控快照，需同时安装文档读取插件。'
        '不修改原文件或执行程序；不会自动发给模型。确认后使用新版本；'
        '所有使用旧资源授权的任务、协作、待确认记忆和派生上下文停止使用，即使此次只是增加目录。'
        '已保存的来源记忆保留，但不能继续召回；可核对后以用户纠正建立独立来源。'
        '已发往模型或 QQ 的内容无法撤回。其他任务与原始服务器文件不受影响。')


@router.put('/api/admin/resource-policy', response_model=ResourcePolicyView)
def save(body: ResourcePolicyConfirm, ctx: Context):
    db, user, session = ctx
    try:
        data = json.loads(decrypt(body.token))
        assert data['purpose'] == 'resource-policy' and data['user'] == user.id
        assert data['workspace'] == user.workspace_id and data['session'] == session.id
        assert now() <= data['expires_at'] <= now() + 301
        scope = ResourceScope(roots=data['roots'], read_roots=data.get('read_roots', []))
    except (InvalidToken, ValueError, KeyError, TypeError, AssertionError):
        raise HTTPException(409, '预览已过期或不属于当前登录，请重新预览') from None
    active = db.get(LoginSession, session.id, populate_existing=True)
    if not active or active.expires_at <= now():
        raise HTTPException(401, '登录已失效，请重新登录后预览')
    from .plugins import owner_lock
    owner_lock(db, user)
    before = current(db, user, lock=True)
    if data['version'] != before.version:
        raise HTTPException(409, '资源授权已变化，请重新预览')
    validate_directories(scope.roots + scope.read_roots)
    row = db.get(ResourcePolicy, (user.workspace_id, user.id))
    if row is None:
        row = ResourcePolicy(workspace_id=user.workspace_id, user_id=user.id)
        db.add(row)
    row.roots, row.read_roots, row.version, row.updated_at = scope.roots, scope.read_roots, before.version + 1, now()
    try:
        db.flush()
    except IntegrityError:
        raise HTTPException(409, '资源授权已并发变化，请重新加载与预览') from None
    expire(db, user)
    from .models import Memory
    from .memory import invalidate_runs
    for memory in db.scalars(select(Memory).where(Memory.user_id == user.id,
            Memory.workspace_id == user.workspace_id, Memory.status == 'active')):
        if memory.provenance.get('resources'):
            invalidate_runs(db, memory.id)
    audit(db, user, 'resource_policy_update', str(row.version))
    return current(db, user)


def checkpoint(db):
    return [view(row) for row in db.scalars(select(ResourcePolicy))]


def restore(db, records, owner):
    """Never restore an old grant. Retain a monotonic version across server moves."""
    versions = [r['version'] for r in records if r['user_id'] == owner.id and r['workspace_id'] == owner.workspace_id]
    row = db.get(ResourcePolicy, (owner.workspace_id, owner.id))
    if row:
        versions.append(row.version)
    else:
        row = ResourcePolicy(workspace_id=owner.workspace_id, user_id=owner.id)
        db.add(row)
    row.roots, row.read_roots, row.version, row.updated_at = [], [], max(versions, default=0) + 1, now()
    db.flush()
    expire(db, owner)
