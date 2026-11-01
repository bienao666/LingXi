"""Reviewed, atomic transport of non-secret configuration across domains.

Use each domain's own apply path so validation, revision and audit behavior stay
identical to an individual change. A failure rolls back the entire HTTP request.
Neither this document nor its preview is an authorization or credential backup.
"""
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import media_policy, model_budgets, operations, system_settings
from .db import now
from .security import decrypt, encrypt
from .web import Context, audit

router = APIRouter()
ModuleName = Literal['workspace', 'media', 'operations', 'model_budget']
NAMES = ('workspace', 'media', 'operations', 'model_budget')


class OperationalValues(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    log_retention_days: int = Field(ge=7, le=365)
    daily_token_alert: int = Field(ge=0, le=2_000_000_000)


class ConfigurationModules(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    workspace: system_settings.Defaults | None = None
    media: media_policy.MediaLimits | None = None
    operations: OperationalValues | None = None
    model_budget: model_budgets.Limits | None = None

    @model_validator(mode='after')
    def present_modules(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('至少选择一个配置模块，模块不能为 null')
        # An included module is a complete snapshot, never a partial PATCH whose
        # omitted keys accidentally reset the other fields to defaults.
        for key in self.model_fields_set:
            value = getattr(self, key)
            if value.model_fields_set != set(type(value).model_fields):
                raise ValueError('所选模块必须包含完整字段，请重新导出配置包')
        return self


class ConfigurationDocument(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    format: Literal['lingxi-configuration-bundle/v1']
    modules: ConfigurationModules


class ConfigurationState(BaseModel):
    document: ConfigurationDocument
    versions: dict[ModuleName, int]


class ConfigurationImport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document: ConfigurationDocument
    versions: dict[ModuleName, int] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def exact_versions(self):
        if set(self.versions) != self.document.modules.model_fields_set or any(v < 0 for v in self.versions.values()):
            raise ValueError('版本须与所选模块逐一对应')
        return self


class ConfigurationDifference(BaseModel):
    module: ModuleName
    field: str
    before: str | int | list[str] | None
    after: str | int | list[str] | None


NOTE = ('所有差异在同一事务中生效；任一配置变化则全部停止，请重新预览。'
        '空间默认值用于新回合；媒体类型及大小用于新提交，不删除已有文件；'
        '缩短日志保留期会在后续清理时永久删除过期的结构化运行日志；'
        'token 阈值只告警，模型次数限额不清空已计次数、不撤回已发请求，也不是金额封顶。'
        '配置包不含密钥、代理、模型连接、插件、资源授权、联系许可或原始业务数据。')


class ConfigurationPreview(BaseModel):
    versions: dict[ModuleName, int]
    changes: list[ConfigurationDifference]
    token: str
    expires_at: float
    note: str


class ConfigurationConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    token: str = Field(min_length=10, max_length=32768)
    acknowledgement: Literal['atomic_configuration_changes']


def state(db, user):
    workspace = system_settings.current(db, user)
    media = media_policy.current(db, user.workspace_id)
    prefs = operations.prefs(db, user)
    budget = db.get(model_budgets.ModelBudget, user.workspace_id, populate_existing=True)
    return ConfigurationState(document=ConfigurationDocument(
        format='lingxi-configuration-bundle/v1', modules=ConfigurationModules(
            workspace=system_settings.Defaults(**workspace.config.model_dump()), media=media_policy.MediaLimits(**media.model_dump(exclude={'version'})),
            operations=OperationalValues(**{k: prefs[k] for k in OperationalValues.model_fields}),
            model_budget=model_budgets.Limits(**model_budgets.values(budget)))),
        versions={'workspace': workspace.version, 'media': media.version,
            'operations': prefs['version'], 'model_budget': budget.version if budget else 0})


@router.get('/api/admin/configuration-bundles', response_model=ConfigurationState)
def get_state(ctx: Context):
    db, user, _ = ctx
    # Do not exclude nullable fields inside a module (default_persona_id).
    return state(db, user)


@router.get('/api/admin/configuration-bundles/export', response_model=ConfigurationDocument)
def export(ctx: Context):
    db, user, _ = ctx
    return state(db, user).document


def check_versions(actual, requested):
    if any(actual[name] != version for name, version in requested.items()):
        raise HTTPException(409, '所选模块的配置已变化，请重新加载并预览；未应用任何更改')


@router.post('/api/admin/configuration-bundles/previews', response_model=ConfigurationPreview)
def preview(body: ConfigurationImport, ctx: Context):
    db, user, session = ctx
    previous = state(db, user)
    check_versions(previous.versions, body.versions)
    modules = body.document.modules
    persona = system_settings.persona_reference(db, modules.workspace, user) if modules.workspace else None
    changes = []
    for name in NAMES:
        after = getattr(modules, name)
        if after is None:
            continue
        before = getattr(previous.document.modules, name)
        for field in type(after).model_fields:
            if getattr(before, field) != getattr(after, field):
                changes.append(dict(module=name, field=field, before=getattr(before, field), after=getattr(after, field)))
    if not changes:
        raise HTTPException(422, '所选配置没有变化，无需导入')
    expires = now()+300
    # Preserve intentional null persona references in the nested document.
    data = body.model_dump()
    data['document']['modules'] = {name: getattr(modules, name).model_dump() for name in body.versions}
    token = encrypt(json.dumps({'purpose': 'configuration-bundle', 'user': user.id,
        'workspace': user.workspace_id, 'session': session.id, 'expires_at': expires,
        'input': data, 'persona': persona}, sort_keys=True))
    return dict(versions=body.versions, changes=changes, token=token, expires_at=expires, note=NOTE)


@router.put('/api/admin/configuration-bundles', response_model=ConfigurationState)
def confirm(body: ConfigurationConfirm, ctx: Context):
    db, user, session = ctx
    try:
        data = json.loads(decrypt(body.token))
        if (data['purpose'] != 'configuration-bundle' or data['user'] != user.id or
            data['workspace'] != user.workspace_id or data['session'] != session.id or
            type(data['expires_at']) not in (int, float) or not now() <= data['expires_at'] <= now()+301):
            raise ValueError('Invalid configuration preview')
        request = ConfigurationImport.model_validate(data['input'])
    except Exception:
        raise HTTPException(409, '配置包预览已失效或不属于当前登录，请重新预览') from None
    # Same lock order as individual configuration edits. Budget admission can
    # happen without the owner lock; hold its own row before checking versions.
    from .auth_totp import lock_owner
    lock_owner(db, user, session)
    if data['workspace'] != user.workspace_id:
        raise HTTPException(409, '工作空间已变化，请重新预览；未应用任何更改')
    if 'model_budget' in request.versions:
        model_budgets.locked(db, user.workspace_id)
    previous = state(db, user)
    check_versions(previous.versions, request.versions)
    modules = request.document.modules
    if modules.workspace and system_settings.persona_reference(db, modules.workspace, user, lock=True) != data['persona']:
        raise HTTPException(409, '默认人格已变化，请重新预览；未应用任何更改')
    # Keep each module's own validations, revisions and audits. There are no
    # commits here: web.database owns the single transaction and rollback.
    for name in NAMES:
        value = getattr(modules, name)
        if value is None or value == getattr(previous.document.modules, name):
            continue
        version = request.versions[name]
        if name == 'workspace':
            checked = system_settings.preview(system_settings.SystemSettingsInput(
                version=version, action='import', document=system_settings.SettingsDocument(
                    format='lingxi-workspace-defaults/v1', config=value)), ctx)
            system_settings.confirm(system_settings.SettingsConfirm(
                token=checked['token'], acknowledgement='new_turns_only'), ctx)
        elif name == 'media':
            checked = media_policy.preview(media_policy.MediaPolicyInput(version=version, **value.model_dump()), ctx)
            media_policy.save(media_policy.MediaPolicyConfirm(token=checked['token'],
                acknowledgement='new_submissions_only'), ctx)
        elif name == 'operations':
            operations.save_preferences(operations.OpsPreferenceInput(version=version, **value.model_dump()), ctx)
        else:
            checked = model_budgets.preview(model_budgets.BudgetPreview(version=version, **value.model_dump()), ctx)
            model_budgets.confirm(model_budgets.BudgetConfirm(token=checked['token'],
                acknowledgement='request_attempts_not_money'), ctx)
    audit(db, user, 'configuration_bundle_import')
    db.flush()
    return state(db, user)
