"""Plugin lifecycle is an authenticated management surface, not a model tool."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from .db import now
from .models import User
from .web import Context, audit, owned
from .tasks import scheduled_job
from .plugins import (PluginRelease, PluginInstallation, PluginSnapshot, PluginPreview, PluginInvocation,
    installations, owner_lock, release_checked, snapshot, installation_view, require_capability,
    invalidate_invocations)
from .plugin_manifest import resolve, digest, permissions, CORE_VERSION

router = APIRouter()


class CatalogItem(BaseModel):
    release_id: str
    plugin_id: str
    version: str
    name: str
    description: str
    publisher: str
    source: str
    digest: str
    permissions: list[str]
    capabilities: list[dict]
    dependencies: list[dict]
    external_services: list[str]
    costs: str
    data_scope: str
    license: str
    core: str
    compatible: bool


class InstallationView(BaseModel):
    id: str
    plugin_id: str
    name: str
    plugin_version: str
    release_id: str
    version: int
    status: str
    grants: list[str]
    dependency_lock: dict[str, str]
    config: dict
    available: bool
    reason: str | None
    digest: str
    source: str
    updated_at: float


class PreviewInput(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=120)
    plugin_version: str = Field(pattern=r'^\d+\.\d+\.\d+$', max_length=30)
    action: Literal['install', 'update', 'rollback'] = 'install'


class PreviewView(BaseModel):
    id: str
    digest: str
    expires_at: float
    status: str
    plan: dict


class Confirmation(BaseModel):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved_permissions: list[str] = Field(max_length=20)


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class PluginImpact(BaseModel):
    dependents: list[str]
    active_invocations: int
    pending_tasks: int
    scheduled_reminders: int
    delegated_file_nodes: int
    plugin_roles: int
    role_delegations: int


class InvocationInput(BaseModel):
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=8, max_length=100)


class InvocationView(BaseModel):
    id: str
    installation_id: str
    plugin_id: str
    plugin_version: str
    capability_id: str
    adapter: str
    status: str
    version: int
    created_at: float
    finished_at: float | None
    result: str
    receipt: dict
    error: str | None


def catalog_view(row):
    m = row.manifest
    # Instructions remain source material; they are never rendered as HTML.
    return {k: m[k] for k in ('plugin_id', 'version', 'name', 'description', 'publisher', 'capabilities',
        'dependencies', 'external_services', 'costs', 'data_scope', 'license', 'core')} | {
        'release_id': row.id, 'source': row.source, 'digest': row.digest, 'permissions': permissions(m),
        'compatible': Version(CORE_VERSION) in SpecifierSet(m['core'])}


@router.get('/api/plugin-catalog', response_model=list[CatalogItem])
def catalog(ctx: Context, q: str = Query(default='', max_length=100)):
    db, user, _ = ctx
    from .plugin_store import visible_releases
    rows = [r for r in visible_releases(db, user) if r.status == 'published']
    result = []
    for row in rows:
        release_checked(db, row.id, user=user)
        if q.casefold() in (row.plugin_id + row.manifest['name'] + row.manifest['description']).casefold():
            result.append(catalog_view(row))
    return sorted(result, key=lambda r: (r['plugin_id'], Version(r['version'])), reverse=True)


@router.get('/api/plugin-installations', response_model=list[InstallationView])
def installed(ctx: Context):
    db, user, _ = ctx
    return [installation_view(db, user, row) for row in installations(db, user)]


def baseline(rows):
    return {r.plugin_id: {'id': r.id, 'version': r.version, 'release_id': r.release_id,
            'status': r.status, 'grants': sorted(r.grants), 'dependency_lock': r.dependency_lock,
            'config': r.config} for r in rows}


def make_plan(db, user, body):
    rows = installations(db, user)
    current = {row.plugin_id: row for row in rows}
    root = current.get(body.plugin_id)
    if body.action == 'install' and root:
        raise HTTPException(409, '插件已安装，请使用更新或回滚预览')
    if body.action != 'install' and not root:
        raise HTTPException(409, '插件尚未安装')
    from .plugin_store import visible_releases, connection
    releases = visible_releases(db, user)
    selected = resolve(releases, body.plugin_id, body.plugin_version,
                       {r.plugin_id: r.release_id for r in rows})
    if root and root.release_id == selected[body.plugin_id].id:
        raise HTTPException(409, '已经使用此版本，无需变更')
    if root and body.action == 'update' and Version(body.plugin_version) <= Version(db.get(PluginRelease, root.release_id).version):
        raise HTTPException(409, '更新应选择更高版本；降级请使用已安装历史中的回滚预览')
    rollback = None
    if body.action == 'rollback':
        rollback = db.scalar(select(PluginSnapshot).where(PluginSnapshot.installation_id == root.id,
            PluginSnapshot.release_id == selected[body.plugin_id].id).order_by(PluginSnapshot.number.desc()))
        if not rollback:
            raise HTTPException(409, '只能回滚到此安装已使用的版本')
        if rollback.config != root.config:
            raise HTTPException(409, '配置快照不兼容，不能直接回滚')
        fixed = rollback.dependency_lock
        selected = resolve([r for r in releases if r.plugin_id not in fixed or r.id == fixed[r.plugin_id]],
            body.plugin_id, body.plugin_version, {r.plugin_id: r.release_id for r in rows})
    # Updating a dependency cannot break an installed dependent outside this graph.
    for row in rows:
        if row.plugin_id in selected:
            continue
        try:
            m = release_checked(db, row.release_id, user=user).manifest
        except HTTPException:
            # An already blocked installation cannot execute. It must not make
            # unrelated installations unavailable while the owner resolves it.
            continue
        for dep in m['dependencies']:
            if dep['plugin_id'] in selected and Version(selected[dep['plugin_id']].version) not in SpecifierSet(dep['constraint']):
                raise HTTPException(409, f'版本变更与已安装依赖者 {row.plugin_id} 不兼容')
    entries = []
    def closure(identity, found=None):
        found = {} if found is None else found
        for dep in selected[identity].manifest['dependencies']:
            key = dep['plugin_id']
            if key not in found:
                found[key] = selected[key].id
                closure(key, found)
        return found
    for identity, release in sorted(selected.items()):
        release_checked(db, release.id, True, user=user)
        if any(c['kind'] not in ('skill', 'native', 'api', 'mcp', 'event', 'agent') for c in release.manifest['capabilities']):
            raise HTTPException(409, '此插件需要尚未接入的执行适配器，暂不能安装')
        row = current.get(identity)
        required = permissions(release.manifest)
        entries.append({'release': catalog_view(release), 'previous_version':
            db.get(PluginRelease, row.release_id).version if row else None,
            'action': 'install' if row is None else 'enable' if row.status != 'enabled' and row.release_id == release.id
                else 'keep' if row.release_id == release.id else 'change',
            'new_permissions': sorted(set(required) - set(row.grants if row else [])),
            'grants': required, 'dependency_lock': closure(identity),
            'config': rollback.config if rollback and identity == body.plugin_id else row.config if row else {}})
    store = connection(db, user) if any(r.source == 'store' for r in selected.values()) else None
    return {'request': body.model_dump(), 'baseline': baseline(rows), 'entries': entries,
            'permissions': sorted({p for entry in entries for p in entry['grants']}),
            'location': '服务器 Core／worker', 'catalog': '内置目录＋已验证的商店快照',
            'store_trust': {'version': store.version, 'revision': store.revision, 'epoch': store.trust_epoch} if store else None}


@router.post('/api/plugin-installations/preview', response_model=PreviewView)
def preview(body: PreviewInput, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    # Bound retention/work per owner; previews are short-lived management artifacts.
    for prior in db.scalars(select(PluginPreview).where(PluginPreview.user_id == user.id,
            PluginPreview.workspace_id == user.workspace_id, PluginPreview.status == 'pending')):
        prior.status = 'superseded'
    plan = make_plan(db, user, body)
    row = PluginPreview(user_id=user.id, workspace_id=user.workspace_id, login_session_id=login.id,
        plan=plan, digest=digest(plan), expires_at=now()+600)
    db.add(row)
    db.flush()
    audit(db, user, 'plugin_preview', row.id)
    return {'id': row.id, 'plan': row.plan, 'digest': row.digest, 'expires_at': row.expires_at, 'status': row.status}


@router.post('/api/plugin-installations/previews/{identity}/reject')
def reject(identity: str, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginPreview, identity, user, True)
    if row.status == 'pending':
        row.status = 'rejected'
        audit(db, user, 'plugin_reject', row.id)
    return {'status': row.status}


@router.post('/api/plugin-installations/previews/{identity}/confirm', response_model=list[InstallationView])
def confirm(identity: str, body: Confirmation, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    row = owned(db, PluginPreview, identity, user, True)
    if row.login_session_id != login.id or body.digest != row.digest:
        raise HTTPException(409, '确认与当前登录或预览不匹配，请重新预览')
    if row.status == 'confirmed':
        return row.result
    if row.status != 'pending' or row.expires_at <= now():
        raise HTTPException(409, '预览已失效或已拒绝，请重新预览')
    plan = make_plan(db, user, PreviewInput(**row.plan['request']))
    if digest(plan) != row.digest or digest(row.plan) != row.digest:
        raise HTTPException(409, '目录、安装或授权已变化，请重新预览')
    if sorted(set(body.approved_permissions)) != plan['permissions']:
        raise HTTPException(422, '请明确确认预览中的完整权限；未确认不安装')
    # Root owners first registered above can depend on another entry; commit only
    # after all installations, snapshots and locks are present in this transaction.
    result = []
    for entry in plan['entries']:
        spec = entry['release']
        existing = db.scalar(select(PluginInstallation).where(PluginInstallation.user_id == user.id,
            PluginInstallation.workspace_id == user.workspace_id, PluginInstallation.plugin_id == spec['plugin_id']))
        if not existing:
            existing = PluginInstallation(user_id=user.id, workspace_id=user.workspace_id,
                plugin_id=spec['plugin_id'], release_id=spec['release_id'])
            db.add(existing)
            db.flush()
        else:
            from .plugin_events import invalidate_subscriptions
            invalidate_subscriptions(db, user, existing.id)
            if existing.plugin_id == 'lingxi.documents' and ('resources.read' not in entry['grants'] or
                    not any(c['id'] == 'server.read' and c['kind'] == 'native' for c in spec['capabilities'])):
                from .server_documents import invalidate_plugin
                invalidate_plugin(db, user, existing.id)
            existing.version += 1
        existing.release_id, existing.grants = spec['release_id'], entry['grants']
        existing.dependency_lock, existing.config = entry['dependency_lock'], entry['config']
        existing.status, existing.updated_at = 'enabled', now()
        snapshot(db, existing)
    db.flush()
    for entry in plan['entries']:
        existing = db.scalar(select(PluginInstallation).where(PluginInstallation.user_id == user.id,
            PluginInstallation.workspace_id == user.workspace_id, PluginInstallation.plugin_id == entry['release']['plugin_id']))
        result.append(installation_view(db, user, existing))
    row.status, row.result = 'confirmed', result
    audit(db, user, 'plugin_confirm', row.id)
    return result


@router.get('/api/plugin-installations/{identity}/history', response_model=list[InstallationView])
def history(identity: str, ctx: Context):
    db, user, _ = ctx
    row = owned(db, PluginInstallation, identity, user)
    records = db.scalars(select(PluginSnapshot).where(PluginSnapshot.installation_id == row.id,
        PluginSnapshot.user_id == user.id, PluginSnapshot.workspace_id == user.workspace_id)
        .order_by(PluginSnapshot.number.desc()).limit(100)).all()
    result = []
    for record in records:
        release = db.get(PluginRelease, record.release_id)
        result.append(installation_view(db, user, row) | {'version': record.number,
            'release_id': record.release_id, 'plugin_version': release.version, 'digest': release.digest,
            'config': record.config, 'grants': record.grants, 'dependency_lock': record.dependency_lock,
            'updated_at': record.created_at})
    return result


@router.post('/api/plugin-installations/{identity}/{action}', response_model=InstallationView)
def lifecycle(identity: str, action: Literal['enable', 'disable', 'uninstall'], body: VersionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginInstallation, identity, user, True)
    if row.version != body.version or row.status == 'uninstalled':
        raise HTTPException(409, '安装状态已变化，请重新加载')
    if action == 'uninstall':
        dependents = [other.plugin_id for other in installations(db, user) if other.id != row.id and
                      row.plugin_id in other.dependency_lock]
        if dependents:
            raise HTTPException(409, '存在依赖者，先单独确认卸载：' + '、'.join(dependents))
    if action == 'enable':
        from .plugins import check_installation
        previous = row.status
        row.status = 'enabled'
        try:
            check_installation(db, user, row)
        except HTTPException:
            row.status = previous
            raise
    else:
        row.status = 'disabled' if action == 'disable' else 'uninstalled'
        invalidate_invocations(db, user, row.id, '插件已停用或卸载，结果未提交')
        if action == 'uninstall':
            row.config, row.grants = {}, []
            from .plugin_api import PluginConnection
            for connection in db.scalars(select(PluginConnection).where(PluginConnection.installation_id == row.id)):
                connection.status, connection.encrypted_secret, connection.checked_at = 'revoked', '', None
                connection.version += 1
    row.version += 1
    row.updated_at = now()
    audit(db, user, 'plugin_' + action, row.id)
    return installation_view(db, user, row)


@router.get('/api/plugin-installations/{identity}/impact', response_model=PluginImpact)
def impact(identity: str, ctx: Context):
    db, user, _ = ctx
    row = owned(db, PluginInstallation, identity, user)
    dependents = [other.plugin_id for other in installations(db, user) if other.id != row.id and row.plugin_id in other.dependency_lock]
    calls = db.scalar(select(func.count()).select_from(PluginInvocation).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.installation_id == row.id,
        PluginInvocation.status.in_(['preview', 'queued', 'running', 'unknown'])))
    tasks = reminders = nodes = 0
    if row.plugin_id in ('lingxi.builtin', 'lingxi.documents'):
        from .tasks import WorkTask, Reminder
        from .delegation import AgentNode, AgentRun
        tasks = db.scalar(select(func.count()).select_from(WorkTask).where(WorkTask.user_id == user.id,
            WorkTask.workspace_id == user.workspace_id, WorkTask.config['plugin_id'].as_string() == row.plugin_id,
            WorkTask.status.in_(['queued', 'running'])))
        reminders = db.scalar(select(func.count()).select_from(Reminder).where(Reminder.user_id == user.id,
            Reminder.workspace_id == user.workspace_id, Reminder.status == 'scheduled')) if row.plugin_id == 'lingxi.builtin' else 0
        nodes = db.scalar(select(func.count()).select_from(AgentNode).join(AgentRun, AgentRun.id == AgentNode.run_id)
            .where(AgentRun.user_id == user.id, AgentRun.workspace_id == user.workspace_id,
                AgentNode.capability == 'server.files', AgentNode.status.in_(['pending', 'queued', 'running']))) if row.plugin_id == 'lingxi.builtin' else 0
    from .plugin_agents import affected_bindings
    from .delegation import AgentRun, AgentNode, DONE
    affected = {b.id for b in affected_bindings(db, user, row.id)}
    role_runs = 0
    if affected:
        for run in db.scalars(select(AgentRun).where(AgentRun.user_id == user.id,
                AgentRun.workspace_id == user.workspace_id, AgentRun.status.not_in(DONE))):
            assigned = [n.config for n in db.scalars(select(AgentNode).where(AgentNode.run_id == run.id))]
            if any(c.get('plugin_role', {}).get('binding_id') in affected for c in assigned or run.config.get('agents', [])):
                role_runs += 1
    return dict(dependents=dependents, active_invocations=calls, pending_tasks=tasks,
                scheduled_reminders=reminders, delegated_file_nodes=nodes, plugin_roles=len(affected), role_delegations=role_runs)


@router.get('/api/plugin-capabilities')
def capabilities(ctx: Context):
    db, user, _ = ctx
    result = []
    for row in installations(db, user):
        info = installation_view(db, user, row)
        release = db.get(PluginRelease, row.release_id)
        for cap in release.manifest['capabilities']:
            available, reason = info['available'], info['reason']
            if available and cap['kind'] in ('api', 'mcp'):
                from .plugin_api import connection_for
                try:
                    connection = connection_for(db, user, row.id, cap['id'], cap[cap['kind']])
                    if cap['kind'] == 'mcp':
                        from .plugin_mcp import catalog_for
                        catalog_for(db, user, connection)
                except HTTPException as exc:
                    available, reason = False, str(exc.detail)
            if available and cap['kind'] == 'event':
                from .plugin_events import EventSubscription, available_subscription
                sub = db.scalar(select(EventSubscription).where(EventSubscription.installation_id == row.id,
                    EventSubscription.capability_id == cap['id'], EventSubscription.user_id == user.id,
                    EventSubscription.workspace_id == user.workspace_id))
                try:
                    if not sub:
                        raise HTTPException(409, '请先在账号授权中确认事件订阅')
                    available_subscription(db, user, sub)
                except HTTPException as exc:
                    available, reason = False, str(exc.detail)
            if available and cap['kind'] == 'agent':
                from .plugin_agents import PluginAgentBinding, candidate
                binding = db.scalar(select(PluginAgentBinding).where(PluginAgentBinding.installation_id == row.id,
                    PluginAgentBinding.capability_id == cap['id'], PluginAgentBinding.user_id == user.id,
                    PluginAgentBinding.workspace_id == user.workspace_id))
                try:
                    if not binding:
                        raise HTTPException(409, '请在助手配置中选择人格、模型及相关记忆范围')
                    candidate(db, user, binding)
                except HTTPException as exc:
                    available, reason = False, str(exc.detail)
            if available and 'model.chat' in cap['permissions'] and cap['kind'] != 'agent':
                try:
                    from .gateway import resolve_model
                    model, provider, _ = resolve_model(db, 'chat')
                    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
                        raise HTTPException(409, '模型不属于当前工作区')
                except HTTPException as exc:
                    available, reason = False, str(exc.detail)
            if available and cap['id'] in ('server.files', 'server.read'):
                from .resources import current as resource_policy
                resources = resource_policy(db, user)
                if not resources.available or (cap['id'] == 'server.read' and not resources.read_roots):
                    available, reason = False, resources.reason or '请在资源授权中单独允许正文目录'
            result.append({**cap, 'installation_id': row.id, 'plugin_id': row.plugin_id,
                'plugin_version': release.version, 'available': available, 'reason': reason})
    return result


def invocation_view(db, row):
    release = db.get(PluginRelease, row.release_id)
    return {k: getattr(row, k) for k in InvocationView.model_fields if k not in ('plugin_id', 'plugin_version', 'adapter')} | {
        'plugin_id': release.plugin_id, 'plugin_version': release.version, 'adapter': 'mcp' if row.config.get('mcp') else 'api' if row.config.get('api') else 'skill'}


@router.get('/api/plugin-invocations', response_model=list[InvocationView])
def invocations(ctx: Context, limit: int = Query(default=50, ge=1, le=100)):
    db, user, _ = ctx
    return [invocation_view(db, row) for row in db.scalars(select(PluginInvocation).where(
        PluginInvocation.user_id == user.id, PluginInvocation.workspace_id == user.workspace_id)
        .order_by(PluginInvocation.created_at.desc(), PluginInvocation.id.desc()).limit(limit))]


@router.post('/api/plugin-invocations', response_model=InvocationView)
def invoke(body: InvocationInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    fingerprint = digest(body.model_dump())
    previous = db.scalar(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.request_id == body.request_id))
    if previous:
        if previous.request_hash != fingerprint:
            raise HTTPException(409, '请求编号已用于其他调用')
        return invocation_view(db, previous)
    queued = db.scalars(select(PluginInvocation.id).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.status.in_(['queued', 'running'])).limit(16)).all()
    if len(queued) >= 16:
        raise HTTPException(429, '同时等待或执行的插件调用已达 16 项，请等待或取消后重试')
    installation = owned(db, PluginInstallation, body.installation_id, user)
    _, release, cap = require_capability(db, user, body.capability_id, installation.plugin_id)
    if cap['kind'] != 'skill':
        raise HTTPException(409, '内置能力请通过任务中心或待办提醒提交')
    if not body.content.strip():
        raise HTTPException(422, '请输入需要处理的资料')
    from .gateway import resolve_model
    model, provider, _ = resolve_model(db, 'chat')
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(409, '模型不属于当前工作区')
    if len(body.content) + len(cap['instruction']) + 2000 > model.context_window:
        raise HTTPException(422, '输入超过当前模型的保守上下文预算，请缩小范围')
    row = PluginInvocation(user_id=user.id, workspace_id=user.workspace_id,
        installation_id=installation.id, release_id=release.id, capability_id=cap['id'],
        request_id=body.request_id, request_hash=fingerprint, input=body.content,
        config={'model_id': model.id, 'model': model.name, 'provider_version': provider.version,
                'release_digest': release.digest, 'dependency_lock': dict(installation.dependency_lock),
                'prompt_version': 'declarative-skill-v1'})
    db.add(row)
    db.flush()
    scheduled_job(db, 'plugin:' + row.id, 'plugin', {'invocation_id': row.id}, workspace_id=user.workspace_id)
    audit(db, user, 'plugin_invoke', row.id)
    return invocation_view(db, row)


@router.post('/api/plugin-invocations/{identity}/cancel', response_model=InvocationView)
def cancel(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginInvocation, identity, user, True)
    if row.version != body.version:
        raise HTTPException(409, '调用状态已变化，请重新加载')
    if (row.config.get('api') or row.config.get('mcp')) and row.status in ('preview', 'queued', 'running'):
        # Only this invocation is cancelled; account permissions remain unchanged.
        sent = (row.config.get('api') or row.config['mcp']).get('dispatch') == 'in_flight'
        row.status, row.result = ('unknown' if sent else 'cancelled'), ''
        row.error = '用户停止等待；外部结果待核对，不会重发' if sent else '用户取消，尚未发送'
        row.finished_at, row.version = now(), row.version+1
        audit(db, user, 'plugin_cancel', row.id)
    elif row.status in ('queued', 'running'):
        row.status, row.finished_at, row.error = 'cancelled', now(), '用户取消；上游费用不能撤销'
        row.version += 1
        audit(db, user, 'plugin_cancel', row.id)
    return invocation_view(db, row)
