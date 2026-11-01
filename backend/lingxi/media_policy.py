"""Workspace upload limits. Settings never delete assets or rewrite past runs."""
import json
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import User
from .configuration_history import tracked
from .security import decrypt, encrypt
from .web import Context, audit

router = APIRouter()
MIB = 1024**2
MediaType = Literal['text', 'markdown', 'csv', 'pdf', 'docx', 'xlsx', 'image', 'qq_audio']
TYPE_SUFFIXES = {'text': ('.txt',), 'markdown': ('.md',), 'csv': ('.csv',), 'pdf': ('.pdf',),
    'docx': ('.docx',), 'xlsx': ('.xlsx',), 'image': ('.png', '.jpg', '.jpeg', '.webp'), 'qq_audio': ('.wav',)}
TYPE_NAMES = {'text': '纯文本', 'markdown': 'Markdown', 'csv': 'CSV', 'pdf': '文本 PDF',
    'docx': 'Word', 'xlsx': 'Excel', 'image': '静态图片', 'qq_audio': 'QQ 语音'}
DEFAULT_TYPES = list(TYPE_SUFFIXES)


class MediaPolicy(Base):
    __tablename__ = 'media_policies'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    max_files: Mapped[int] = mapped_column(Integer, default=10)
    max_file_mib: Mapped[int] = mapped_column(Integer, default=20)
    max_total_mib: Mapped[int] = mapped_column(Integer, default=50)
    allowed_types: Mapped[list] = mapped_column(JSON, default=lambda: list(DEFAULT_TYPES))


class MediaLimits(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_files: int = Field(default=10, ge=1, le=10)
    max_file_mib: int = Field(default=20, ge=1, le=20)
    max_total_mib: int = Field(default=50, ge=1, le=50)
    allowed_types: list[MediaType] = Field(default_factory=lambda: list(DEFAULT_TYPES), max_length=8)

    @field_validator('allowed_types')
    @classmethod
    def distinct_types(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('附件类型不能重复')
        return [key for key in DEFAULT_TYPES if key in values]

    @model_validator(mode='after')
    def check_total(self):
        if self.max_file_mib > self.max_total_mib:
            raise ValueError('单个附件上限不能高于单条合计上限')
        return self


class MediaPolicyView(MediaLimits):
    version: int


class MediaPolicyInput(MediaLimits):
    version: int = Field(ge=0)


class MediaPolicyPreview(BaseModel):
    before: MediaPolicyView
    after: MediaLimits
    token: str
    expires_at: float
    note: str


class MediaPolicyConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=10, max_length=4096)
    acknowledgement: Literal['new_submissions_only']


def current(db, workspace, lock=False):
    query = select(MediaPolicy).where(MediaPolicy.workspace_id == workspace).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    return MediaPolicyView(version=row.version if row else 0,
        **({key: getattr(row, key) for key in MediaLimits.model_fields} if row else {}))


def seed_defaults(db):
    for workspace in db.scalars(select(User.workspace_id).distinct()):
        if db.get(MediaPolicy, workspace) is None:
            db.add(MediaPolicy(workspace_id=workspace))
    db.flush()


def check_sizes(policy, sizes):
    # Zero-sized placeholders are allowed while a QQ download is staged. Actual
    # nonempty content validation remains in each receiver, before committing.
    if (len(sizes) > policy.max_files or any(size > policy.max_file_mib*MIB for size in sizes)
            or sum(sizes) > policy.max_total_mib*MIB):
        raise HTTPException(413, f'当前附件策略：每条最多 {policy.max_files} 个，'
            f'单个 {policy.max_file_mib} MiB、合计 {policy.max_total_mib} MiB；请减少或缩小附件')


def check_items(db, workspace, items, lock=False):
    policy = current(db, workspace, lock=lock)
    check_sizes(policy, [item.size for item in items])
    check_types(policy, [item.suffix for item in items])
    return policy


def check_types(policy, suffixes):
    allowed = {suffix for kind in policy.allowed_types for suffix in TYPE_SUFFIXES[kind]}
    if any(suffix.lower() not in allowed for suffix in suffixes):
        raise HTTPException(415, '当前媒体配置未允许此附件类型；请调整媒体配置或改用允许的格式')


@router.get('/api/media-policy', response_model=MediaPolicyView)
def policy(ctx: Context):
    db, user, _ = ctx
    return current(db, user.workspace_id)


@router.post('/api/admin/media-policy/previews', response_model=MediaPolicyPreview)
def preview(body: MediaPolicyInput, ctx: Context):
    db, user, session = ctx
    previous = current(db, user.workspace_id)
    if body.version != previous.version:
        raise HTTPException(409, '附件策略已变化，请重新加载后核对')
    after = body.model_dump(exclude={'version'})
    # Older callers adjusting sizes cannot silently re-enable excluded formats.
    if 'allowed_types' not in body.model_fields_set:
        after['allowed_types'] = previous.allowed_types
    expiry = now()+300
    token = encrypt(json.dumps({'purpose': 'media-policy', 'user': user.id, 'workspace': user.workspace_id,
        'session': session.id, 'version': body.version, 'after': after, 'expires_at': expiry}, sort_keys=True))
    return dict(before=previous, after=after, token=token, expires_at=expiry,
        note='类型及大小限制用于新的上传、附件选择、协作提交和 QQ 接收。已有文件与历史内容不删除；'
            '已经提交的模型任务继续使用原附件。下载中的 QQ 附件在提交前重新检查；降低上限可能终止该次接收。')


@router.put('/api/admin/media-policy', response_model=MediaPolicyView)
@tracked
def save(body: MediaPolicyConfirm, ctx: Context):
    db, user, session = ctx
    try:
        data = json.loads(decrypt(body.token))
        assert data['purpose'] == 'media-policy' and data['user'] == user.id
        assert data['workspace'] == user.workspace_id and data['session'] == session.id
        assert now() <= data['expires_at'] <= now()+301
        assert 'allowed_types' in data['after']  # Re-preview pre-upgrade tokens.
        after = MediaLimits.model_validate(data['after'])
    except (InvalidToken, ValueError, KeyError, TypeError, AssertionError):
        raise HTTPException(409, '预览已失效或不属于当前登录，请重新预览') from None
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    previous = current(db, user.workspace_id, lock=True)
    if data['version'] != previous.version:
        raise HTTPException(409, '附件策略已变化，请重新预览')
    row = db.get(MediaPolicy, user.workspace_id)
    if row is None:
        row = MediaPolicy(workspace_id=user.workspace_id)
        db.add(row)
    row.version = previous.version+1
    for key, value in after.model_dump().items():
        setattr(row, key, value)
    db.flush()
    audit(db, user, 'media_policy_update')
    return current(db, user.workspace_id)
