"""Versioned non-authorizing workspace defaults, with reviewed configuration changes."""
import json
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, Float, Integer, String, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import Entity, User
from .security import decrypt, encrypt
from .web import Context, audit, view

router = APIRouter()


class WorkspaceSettings(Base):
    __tablename__ = 'workspace_settings'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    values: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class SettingsRevision(Entity, Base):
    __tablename__ = 'settings_revisions'
    __table_args__ = (UniqueConstraint('workspace_id', 'version'),)
    user_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    values: Mapped[dict] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(String(20))
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Defaults(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    workspace_name: str = Field(default='个人工作空间', min_length=1, max_length=40)
    default_timezone: str = Field(default='Asia/Shanghai', min_length=1, max_length=100)
    default_persona_id: str | None = Field(default=None, min_length=1, max_length=64)
    history_budget_bytes: int = Field(default=48000, ge=1500, le=48000)

    @field_validator('workspace_name')
    @classmethod
    def name(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('工作空间名称须为可见文字，不能包含控制字符')
        return value.strip()

    @field_validator('default_timezone')
    @classmethod
    def timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('请选择有效的 IANA 时区，如 Asia/Shanghai') from None
        return value


class SettingsView(BaseModel):
    version: int
    config: Defaults
    updated_at: float | None


class SettingsDocument(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    format: Literal['lingxi-workspace-defaults/v1']
    config: Defaults


class SystemSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    action: Literal['edit', 'import', 'rollback']
    config: Defaults | None = None
    document: SettingsDocument | None = None
    source_version: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def exact_payload(self):
        expected = {'edit': 'config', 'import': 'document', 'rollback': 'source_version'}[self.action]
        if any((getattr(self, key) is not None) != (key == expected)
               for key in ('config', 'document', 'source_version')):
            raise ValueError('修改、导入和回退须使用各自唯一的配置来源')
        return self


class ConfigChange(BaseModel):
    field: str
    before: str | int | None
    after: str | int | None


class SettingsPreview(BaseModel):
    before: SettingsView
    after: Defaults
    changes: list[ConfigChange]
    action: Literal['edit', 'import', 'rollback']
    source_version: int | None
    token: str
    expires_at: float
    note: str


class SettingsConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=10, max_length=8192)
    acknowledgement: Literal['new_turns_only']


class SettingsRevisionView(BaseModel):
    version: int
    config: Defaults
    created_at: float
    action: str
    source_version: int | None


class SettingsHistory(BaseModel):
    items: list[SettingsRevisionView]
    next_before: int | None


def current(db, user, lock=False):
    query = select(WorkspaceSettings).where(WorkspaceSettings.workspace_id == user.workspace_id,
        WorkspaceSettings.user_id == user.id).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    # Credentialless QQ actors never inherit the owner's persona or settings text.
    return SettingsView(version=row.version if row else 0,
        config=Defaults.model_validate(row.values) if row else Defaults(),
        updated_at=row.updated_at if row else None)


def persona_reference(db, config, user, lock=False):
    if config.default_persona_id is None:
        return None
    from .assistants import AssistantProfile, validate_model
    query = select(AssistantProfile).where(AssistantProfile.id == config.default_persona_id,
        AssistantProfile.workspace_id == user.workspace_id, AssistantProfile.user_id == user.id,
        AssistantProfile.kind == 'persona').execution_options(populate_existing=True)
    profile = db.scalar(query.with_for_update() if lock else query)
    if not profile or not profile.enabled:
        raise HTTPException(409, '默认人格已停用、不存在或不属于当前账号，请重新选择')
    validate_model(db, profile.spec.get('model_id'), user)
    return {'id': profile.id, 'version': profile.version}


def run_snapshot(db, user):
    settings = current(db, user)
    return {'system_settings': {'version': settings.version,
        'history_budget_bytes': settings.config.history_budget_bytes}}


def history_budget(run, model):
    original = min(48000, max(1500, (model.context_window - 3000) // 2))
    return min(original, run.config.get('system_settings', {}).get('history_budget_bytes', 48000))


def checkpoint(db):
    return {'settings': [view(r) for r in db.scalars(select(WorkspaceSettings))],
        'revisions': [view(r) for r in db.scalars(select(SettingsRevision))]}


def validate_checkpoint(data, owner):
    if set(data) != {'settings', 'revisions'} or len(data['settings']) > 1:
        raise ValueError('Invalid settings checkpoint')
    versions = set()
    for record in data['settings'] + data['revisions']:
        if record['workspace_id'] != owner['workspace_id'] or record['user_id'] != owner['id']:
            raise ValueError('Invalid settings checkpoint owner')
        Defaults.model_validate(record['values'])
        if type(record['version']) is not int or record['version'] < 0:
            raise ValueError('Invalid settings checkpoint version')
    for revision in data['revisions']:
        if revision['version'] in versions:
            raise ValueError('Duplicate settings revision')
        versions.add(revision['version'])
    if data['settings']:
        row = data['settings'][0]
        latest = next((r for r in data['revisions'] if r['version'] == row['version']), None)
        if not latest or latest['values'] != row['values'] or max(versions) != row['version']:
            raise ValueError('Invalid settings checkpoint history')
    elif data['revisions']:
        raise ValueError('Orphan settings history')


def restore(db, data, user):
    # Retain latest reviewed defaults, never revive a persona from an old archive.
    prior = db.get(WorkspaceSettings, user.workspace_id)
    latest = data['settings'][0] if data['settings'] else None
    config = Defaults.model_validate(latest['values']) if latest else Defaults()
    config.default_persona_id = None
    version = max(prior.version if prior else 0, latest['version'] if latest else 0)+1
    db.execute(delete(SettingsRevision).where(SettingsRevision.workspace_id == user.workspace_id))
    for record in data['revisions']:
        db.add(SettingsRevision(**record))
    row = prior or WorkspaceSettings(workspace_id=user.workspace_id, user_id=user.id)
    row.version, row.values, row.updated_at = version, config.model_dump(), now()
    db.add(row)
    db.add(SettingsRevision(workspace_id=user.workspace_id, user_id=user.id, version=version,
        values=row.values, action='restore', source_version=latest['version'] if latest else None))
    audit(db, user, 'system_settings_restore', str(version))


@router.get('/api/system-settings', response_model=SettingsView)
def get_settings(ctx: Context):
    db, user, _ = ctx
    return current(db, user)


@router.get('/api/admin/system-settings/export', response_model=SettingsDocument)
def export_settings(ctx: Context):
    db, user, _ = ctx
    # Deliberate allowlist: no providers, network addresses, secrets or grants.
    return SettingsDocument(format='lingxi-workspace-defaults/v1', config=current(db, user).config)


@router.get('/api/admin/system-settings/revisions', response_model=SettingsHistory)
def revisions(ctx: Context, before: int | None = Query(None, ge=1)):
    db, user, _ = ctx
    query = select(SettingsRevision).where(SettingsRevision.workspace_id == user.workspace_id,
        SettingsRevision.user_id == user.id)
    if before is not None:
        query = query.where(SettingsRevision.version < before)
    rows = list(db.scalars(query.order_by(SettingsRevision.version.desc()).limit(21)))
    return {'items': [dict(version=r.version, config=r.values, created_at=r.created_at,
        action=r.action, source_version=r.source_version) for r in rows[:20]],
        'next_before': rows[19].version if len(rows) > 20 else None}


@router.post('/api/admin/system-settings/previews', response_model=SettingsPreview)
def preview(body: SystemSettingsInput, ctx: Context):
    db, user, session = ctx
    previous = current(db, user)
    if body.version != previous.version:
        raise HTTPException(409, '系统配置已变化，请重新加载后预览')
    if body.action == 'rollback':
        row = db.scalar(select(SettingsRevision).where(SettingsRevision.workspace_id == user.workspace_id,
            SettingsRevision.user_id == user.id, SettingsRevision.version == body.source_version))
        if not row:
            raise HTTPException(404, '配置版本不存在')
        after = Defaults.model_validate(row.values)
    else:
        after = body.document.config if body.action == 'import' else body.config
    ref = persona_reference(db, after, user)
    changes = [dict(field=key, before=getattr(previous.config, key), after=getattr(after, key))
        for key in Defaults.model_fields if getattr(previous.config, key) != getattr(after, key)]
    if not changes:
        raise HTTPException(422, '配置没有变化，无需提交')
    expiry = now()+300
    token = encrypt(json.dumps({'purpose': 'system-settings', 'user': user.id,
        'workspace': user.workspace_id, 'session': session.id, 'version': previous.version,
        'action': body.action, 'source_version': body.source_version, 'after': after.model_dump(),
        'persona': ref, 'expires_at': expiry}, sort_keys=True))
    return dict(before=previous, after=after, changes=changes, action=body.action,
        source_version=body.source_version, token=token, expires_at=expiry,
        note='确认后创建新版本。名称用于界面；默认时区只用于尚未单独设置的联系偏好；'
            '默认人格只用于没有显式人格选择的新回合；历史上下文预算按 UTF-8 字节计算，并受模型更低上限约束。'
            '已提交任务保持原配置。导入或回退不修改账号、模型密钥、代理、插件授权、联系许可或已有提醒。')


@router.put('/api/admin/system-settings', response_model=SettingsView)
def confirm(body: SettingsConfirm, ctx: Context):
    db, user, session = ctx
    try:
        data = json.loads(decrypt(body.token))
        assert data['purpose'] == 'system-settings' and data['user'] == user.id
        assert data['workspace'] == user.workspace_id and data['session'] == session.id
        assert now() <= data['expires_at'] <= now()+301
        after = Defaults.model_validate(data['after'])
        assert data['action'] in ('edit', 'import', 'rollback')
    except (InvalidToken, ValueError, KeyError, TypeError, AssertionError):
        raise HTTPException(409, '预览已失效或不属于当前登录，请重新预览') from None
    # Single-owner workspace lock also serializes initial creation with chat submission.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    previous = current(db, user, lock=True)
    if previous.version != data['version']:
        raise HTTPException(409, '系统配置已变化，请重新预览')
    if persona_reference(db, after, user, lock=True) != data['persona']:
        raise HTTPException(409, '默认人格在预览后已变化，请重新预览')
    row = db.get(WorkspaceSettings, user.workspace_id)
    if row is None:
        row = WorkspaceSettings(workspace_id=user.workspace_id, user_id=user.id)
        db.add(row)
        db.add(SettingsRevision(workspace_id=user.workspace_id, user_id=user.id, version=0,
            values=previous.config.model_dump(), action='initial'))
    elif row.user_id != user.id:
        raise HTTPException(403, '不能修改其他所有者的配置')
    row.version, row.values, row.updated_at = previous.version+1, after.model_dump(), now()
    db.add(SettingsRevision(workspace_id=user.workspace_id, user_id=user.id,
        version=row.version, values=row.values, action=data['action'], source_version=data['source_version']))
    audit(db, user, 'system_settings_'+data['action'], str(row.version))
    db.flush()
    return current(db, user)
