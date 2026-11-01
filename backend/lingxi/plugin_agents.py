"""Plugin role templates bind to local personas; task snapshots never grant scope."""
from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import JSON, Float, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Entity
from .web import Context, audit, owned
from .plugins import PluginInstallation, PluginRelease, check_installation, owner_lock
from .plugin_manifest import Contract, AgentEntry, digest
from .assistants import profile_owned, validate_model, snapshot


class PluginAgentBinding(Entity, Base):
    __tablename__ = 'plugin_agent_bindings'
    __table_args__ = (UniqueConstraint('installation_id', 'capability_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    capability_id: Mapped[str] = mapped_column(String(120))
    release_id: Mapped[str] = mapped_column(String(64))
    persona_id: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    context_categories: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default='enabled')
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class BindingInput(Contract):
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    installation_version: int = Field(ge=1)
    version: int = Field(default=0, ge=0)
    persona_id: str = Field(min_length=1, max_length=64)
    model_id: str | None = Field(default=None, max_length=64)
    tools: list[str] = Field(default_factory=lambda: ['reason'], min_length=1, max_length=3)
    context_categories: list[str] = Field(default_factory=list, max_length=7)
    acknowledged: bool = False


class BindingVersion(Contract):
    version: int = Field(ge=1)


class BindingView(Contract):
    id: str
    installation_id: str
    capability_id: str
    plugin_id: str
    plugin_version: str
    role_name: str
    status: str
    version: int
    persona_id: str
    model_id: str | None
    tools: list[str]
    context_categories: list[str]
    available: bool
    reason: str | None
    updated_at: float


def template(db, user, install, capability_id, release_id=None, dependency_lock=None):
    release = check_installation(db, user, install, release_id, dependency_lock=dependency_lock)
    cap = next((c for c in release.manifest['capabilities'] if c['id'] == capability_id), None)
    if not cap or cap['kind'] != 'agent':
        raise HTTPException(409, '此版本未提供子助手角色')
    return release, cap


def candidate(db, user, row):
    if row.status != 'enabled' or row.user_id != user.id or row.workspace_id != user.workspace_id:
        raise HTTPException(409, '插件角色未启用或已撤销')
    install = db.get(PluginInstallation, row.installation_id)
    release, cap = template(db, user, install, row.capability_id)
    if row.release_id != release.id:
        raise HTTPException(409, '插件版本已变化，请核对新角色后重新确认')
    spec = cap['agent']
    if not set(row.tools) <= set(spec['tools']) or not set(row.context_categories) <= set(spec['context_categories']):
        raise HTTPException(409, '角色配置超出插件声明范围')
    persona = profile_owned(db, row.persona_id, user, 'persona', enabled=True)
    model_id = row.model_id or persona.spec.get('model_id')
    validate_model(db, model_id, user)
    from .gateway import resolve_model
    model, provider, policy = resolve_model(db, 'chat', model_id)
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(409, '模型不属于当前工作区')
    files_release = None
    if 'server.files' in row.tools:
        from .plugins import require_capability
        _, files_release, _ = require_capability(db, user, 'server.files')
        from .resources import snapshot as resource_snapshot
        resource_snapshot(db, user)
    role = {'id': 'plugin-role:'+row.id, 'name': cap['name'], 'kind': 'role', 'version': row.version,
        'spec': {k: spec[k] for k in ('expertise', 'instructions', 'completion')} |
                {'tools': list(row.tools), 'context_categories': list(row.context_categories)}}
    return {'id': 'plugin:'+row.id, 'name': cap['name'], 'role': role, 'persona': snapshot(persona), 'agent': None,
        'model_id': model.id, 'model': model.name, 'provider_version': provider.version, 'policy_version': policy,
        'capabilities': list(row.tools), 'plugin_id': install.plugin_id, 'plugin_version': release.version,
        # The existing file executor pins the separate first-party resource capability.
        'plugin_release_id': files_release.id if files_release else None,
        'plugin_role': {'binding_id': row.id, 'epoch': row.epoch, 'version': row.version,
            'installation_id': install.id, 'release_id': release.id, 'release_digest': release.digest,
            'capability_id': row.capability_id, 'entry_digest': digest(spec),
            'dependency_lock': dict(install.dependency_lock)}}


def validate_pinned(db, user, config):
    """Old tasks keep their template; live permission reductions still win."""
    ref = config['plugin_role']
    row = db.scalar(select(PluginAgentBinding).where(PluginAgentBinding.id == ref['binding_id'])
        .with_for_update().execution_options(populate_existing=True))
    if (not row or row.user_id != user.id or row.workspace_id != user.workspace_id or row.status != 'enabled'
            or row.epoch != ref['epoch'] or row.installation_id != ref['installation_id'] or row.capability_id != ref['capability_id']):
        raise HTTPException(409, '插件角色授权已撤销，请重新创建任务')
    install = db.get(PluginInstallation, row.installation_id, populate_existing=True)
    release, cap = template(db, user, install, row.capability_id, ref['release_id'], ref['dependency_lock'])
    if release.digest != ref['release_digest'] or digest(cap['agent']) != ref['entry_digest']:
        raise HTTPException(409, '固定角色版本校验失败')
    current = next((c for c in db.get(PluginRelease, install.release_id).manifest['capabilities']
                    if c['id'] == row.capability_id and c['kind'] == 'agent'), None)
    pinned = config['role']['spec']
    if (not current or row.persona_id != config['persona']['id'] or
            not set(pinned['tools']) <= set(row.tools) & set(current['agent']['tools']) or
            not set(pinned['tools']) <= set(cap['agent']['tools']) or
            not set(pinned['context_categories']) <= set(row.context_categories) & set(current['agent']['context_categories']) & set(cap['agent']['context_categories'])):
        raise HTTPException(409, '角色的人格、工具或记忆授权已变化，停止使用旧上下文')
    # Instructions are immutable source text; neither they nor a task can expand scope.
    if any(pinned[k] != cap['agent'][k] for k in ('expertise', 'instructions', 'completion')):
        raise HTTPException(409, '角色快照与固定版本不一致')
    return row


def affected_bindings(db, user, installation_id):
    install = db.get(PluginInstallation, installation_id)
    result = []
    for row in db.scalars(select(PluginAgentBinding).where(PluginAgentBinding.user_id == user.id,
            PluginAgentBinding.workspace_id == user.workspace_id, PluginAgentBinding.status == 'enabled')
            .order_by(PluginAgentBinding.id)):
        parent = db.get(PluginInstallation, row.installation_id)
        if row.installation_id == installation_id or (install and install.plugin_id in parent.dependency_lock):
            result.append(row)
    return result


def revoke_bindings(db, user, installation_id):
    for row in affected_bindings(db, user, installation_id):
        row.status, row.epoch, row.version, row.updated_at = 'revoked', row.epoch+1, row.version+1, now()


def view(db, user, row):
    release = db.get(PluginRelease, row.release_id)
    cap = next(c for c in release.manifest['capabilities'] if c['id'] == row.capability_id)
    reason = None
    try:
        candidate(db, user, row)
    except HTTPException as exc:
        reason = str(exc.detail)
    return {k: getattr(row, k) for k in ('id', 'installation_id', 'capability_id', 'status', 'version',
        'persona_id', 'model_id', 'tools', 'context_categories', 'updated_at')} | {
        'plugin_id': release.plugin_id, 'plugin_version': release.version, 'role_name': cap['name'],
        'available': reason is None, 'reason': reason}


def registry(db, user):
    result = []
    for row in db.scalars(select(PluginAgentBinding).where(PluginAgentBinding.user_id == user.id,
            PluginAgentBinding.workspace_id == user.workspace_id, PluginAgentBinding.status == 'enabled')
            .order_by(PluginAgentBinding.id).limit(30)):
        try:
            result.append(candidate(db, user, row))
        except HTTPException:
            continue
    return result


router = APIRouter()


@router.get('/api/plugin-agent-bindings', response_model=list[BindingView])
def listing(ctx: Context):
    db, user, _ = ctx
    return [view(db, user, row) for row in db.scalars(select(PluginAgentBinding).where(
        PluginAgentBinding.user_id == user.id, PluginAgentBinding.workspace_id == user.workspace_id)
        .order_by(PluginAgentBinding.updated_at.desc()).limit(100))]


@router.post('/api/plugin-agent-bindings', response_model=BindingView)
def configure(body: BindingInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    install = owned(db, PluginInstallation, body.installation_id, user)
    if install.version != body.installation_version:
        raise HTTPException(409, '插件安装已变化，请重新加载后核对')
    release, cap = template(db, user, install, body.capability_id)
    spec = cap['agent']
    if (not body.acknowledged or len(set(body.tools)) != len(body.tools) or 'reason' not in body.tools or
            not set(body.tools) <= set(spec['tools']) or len(set(body.context_categories)) != len(body.context_categories) or
            not set(body.context_categories) <= set(spec['context_categories'])):
        raise HTTPException(422, '请明确确认人格、模型及声明范围内的工具和记忆类别')
    row = db.scalar(select(PluginAgentBinding).where(PluginAgentBinding.installation_id == install.id,
        PluginAgentBinding.capability_id == body.capability_id))
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '角色配置已变化，请重新加载后编辑')
    if not row:
        if len(list(db.scalars(select(PluginAgentBinding.id).where(PluginAgentBinding.user_id == user.id,
                PluginAgentBinding.workspace_id == user.workspace_id).limit(100)))) >= 100:
            raise HTTPException(409, '插件角色配置已达 100 项，请先复用现有配置')
        row = PluginAgentBinding(user_id=user.id, workspace_id=user.workspace_id,
            installation_id=install.id, capability_id=body.capability_id, epoch=1, version=0)
        db.add(row)
    row.release_id, row.persona_id, row.model_id = release.id, body.persona_id, body.model_id
    row.tools, row.context_categories, row.status = body.tools, body.context_categories, 'enabled'
    row.version, row.updated_at = row.version+1, now()
    db.flush()
    candidate(db, user, row)
    audit(db, user, 'plugin_agent_configure', row.id)
    return view(db, user, row)


@router.post('/api/plugin-agent-bindings/{identity}/revoke', response_model=BindingView)
def revoke(identity: str, body: BindingVersion, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginAgentBinding, identity, user)
    if row.version != body.version:
        raise HTTPException(409, '角色配置已变化，请重新加载')
    row.status, row.epoch, row.version, row.updated_at = 'revoked', row.epoch+1, row.version+1, now()
    audit(db, user, 'plugin_agent_revoke', row.id)
    return view(db, user, row)
