"""Remote MCP discovery snapshots, explicit tool grants and durable invocations."""
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import JSON, Float, Integer, String, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Entity
from .plugins import PluginInvocation, owner_lock
from .plugin_manifest import digest
from .plugin_api import (PluginConnection, RedactedValidationRoute, VersionInput, ApiPreviewView,
    api_capability, connection_for, auth_headers, login_valid, reserve, invalidate_api, write_barrier)
from .plugin_routes import invocation_view
from .web import Context, owned, audit
from .mcp_transport import McpClient, loads, validate_input

router = APIRouter(route_class=RedactedValidationRoute)


class McpCatalog(Entity, Base):
    __tablename__ = 'plugin_mcp_catalogs'
    __table_args__ = (UniqueConstraint('connection_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    digest: Mapped[str] = mapped_column(String(64), default='')
    approved_tools: Mapped[list] = mapped_column(JSON, default=list)
    approved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    discovered_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class McpCatalogView(BaseModel):
    connection_id: str
    version: int
    digest: str
    tools: list[dict]
    approved_tools: list[str]
    available: bool
    discovered_at: float | None


class ToolApproval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    tools: list[str] = Field(max_length=128)
    acknowledged: Literal[True]


class McpPreviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    tool: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=8, max_length=100)


def mcp_connection(db, user, identity):
    connection = owned(db, PluginConnection, identity, user)
    installation, release, entry = api_capability(db, user, connection.installation_id, connection.capability_id)
    if entry.get('transport') != 'streamable-http': raise HTTPException(409, '此连接不是远程 MCP')
    connection_for(db, user, installation.id, connection.capability_id, entry)
    return connection, release, entry


def catalog_for(db, user, connection, require_approved=True):
    row = db.scalar(select(McpCatalog).where(McpCatalog.connection_id == connection.id,
        McpCatalog.user_id == user.id, McpCatalog.workspace_id == user.workspace_id))
    if not row or row.connection_version != connection.version or not row.discovered_at:
        raise HTTPException(409, '请先重新发现 MCP 工具；账号、入口或目录变化后需重新授权')
    if require_approved and (not row.approved_at or not row.approved_tools):
        raise HTTPException(409, '请先明确授权 MCP 工具，发现目录不代表授权执行')
    return row


def catalog_view(row, connection):
    valid = bool(row.discovered_at and row.connection_version == connection.version)
    return {'connection_id': connection.id, 'version': row.version, 'digest': row.digest,
        'tools': row.tools if valid else [], 'approved_tools': row.approved_tools if valid else [],
        'available': valid and bool(row.approved_at and row.approved_tools), 'discovered_at': row.discovered_at}


def contains_token(value, tokens):
    if isinstance(value, str): return any(token and token in value for token in tokens)
    if isinstance(value, list): return any(contains_token(child, tokens) for child in value)
    if isinstance(value, dict):
        return any(contains_token(key, tokens) or contains_token(child, tokens) for key, child in value.items())
    return False


@router.get('/api/plugin-connections/{identity}/tools', response_model=McpCatalogView)
def tools(identity: str, ctx: Context):
    db, user, _ = ctx
    connection, _, _ = mcp_connection(db, user, identity)
    row = db.scalar(select(McpCatalog).where(McpCatalog.connection_id == identity,
        McpCatalog.user_id == user.id, McpCatalog.workspace_id == user.workspace_id))
    return catalog_view(row, connection) if row else dict(connection_id=identity, version=0, digest='',
        tools=[], approved_tools=[], available=False, discovered_at=None)


@router.post('/api/plugin-connections/{identity}/discover', response_model=McpCatalogView)
def discover(identity: str, body: VersionInput, ctx: Context):
    db, user, login = ctx
    from .plugin_oauth import ensure_request
    ensure_request(db, user, identity, body.version)
    owner_lock(db, user)
    connection, release, entry = mcp_connection(db, user, identity)
    if connection.version != body.version: raise HTTPException(409, '连接已变化，请重新加载')
    reserve(db, user)
    row = db.scalar(select(McpCatalog).where(McpCatalog.connection_id == identity))
    if not row:
        row = McpCatalog(user_id=user.id, workspace_id=user.workspace_id, connection_id=identity,
                         connection_version=connection.version)
        db.add(row); db.flush()
    else: row.version += 1
    row.discovered_at, row.approved_at, row.approved_tools, row.tools, row.digest = None, None, [], [], ''
    invalidate_api(db, user, connection.installation_id, 'MCP 工具正在重新发现', connection.capability_id)
    token, fixed_release, fixed_connection = row.version, release.digest, connection.version
    headers, allow_private = auth_headers(connection, entry), connection.allow_private
    db.commit()
    try:
        with McpClient(entry, headers, allow_private) as remote:
            found = remote.tools()
    except HTTPException as exc:
        raise HTTPException(502, str(exc.detail) + '；旧工具授权已失效，请核对后重新发现。') from None
    except Exception:
        # Never echo untrusted metadata or remote authentication errors.
        raise HTTPException(502, 'MCP 工具发现失败；检查协议、服务状态及受支持的 Schema。旧工具授权已失效。') from None
    db.expire_all(); owner_lock(db, user); login_valid(db, login.id)
    connection, release, current = mcp_connection(db, user, identity)
    row = db.scalar(select(McpCatalog).where(McpCatalog.connection_id == identity).with_for_update()
                    .execution_options(populate_existing=True))
    if connection.version != fixed_connection or release.digest != fixed_release or row.version != token or digest(current) != digest(entry):
        raise HTTPException(409, '发现期间连接、版本或目录已变化，结果未提交')
    from .security import decrypt
    secret = decrypt(connection.encrypted_secret) if connection.encrypted_secret else ''
    if contains_token(found, (secret, remote.session)):
        raise HTTPException(502, 'MCP 工具元数据包含连接或会话凭据，结果未保存')
    row.tools, row.digest, row.connection_version, row.discovered_at = found, digest(found), connection.version, now()
    connection.checked_at = now()
    audit(db, user, 'plugin_mcp_discover', connection.id)
    return catalog_view(row, connection)


@router.post('/api/plugin-connections/{identity}/tools/approve', response_model=McpCatalogView)
def approve(identity: str, body: ToolApproval, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    connection, _, _ = mcp_connection(db, user, identity)
    row = catalog_for(db, user, connection, False)
    if row.version != body.version or row.digest != body.digest:
        raise HTTPException(409, '工具目录已变化，请重新核对')
    if len(set(body.tools)) != len(body.tools) or not set(body.tools) <= {t['name'] for t in row.tools}:
        raise HTTPException(422, '只能授权当前目录中不重复的工具')
    invalidate_api(db, user, connection.installation_id, 'MCP 工具授权已变化', connection.capability_id)
    row.approved_tools, row.approved_at, row.version = sorted(body.tools), now(), row.version+1
    audit(db, user, 'plugin_mcp_approve', connection.id)
    return catalog_view(row, connection)


def arguments(content, tool):
    from .plugin_api import input_value
    # Reuse strict duplicate/NaN/depth/collection/size guards before JSON Schema.
    try: value = loads(content)
    except HTTPException:
        raise HTTPException(422, 'MCP 输入须为无重复字段、无歧义的有效 JSON') from None
    if not isinstance(value, dict): raise HTTPException(422, 'MCP 输入须为 JSON 对象')
    value = input_value(content, {'parameters': [{'name': k, 'required': False,
        'type': 'boolean' if type(v) is bool else 'string' if isinstance(v, str) else
                'null' if v is None else 'object' if isinstance(v, dict) else 'array' if isinstance(v, list) else 'number'}
        for k, v in value.items()]})
    validate_input(value, tool['inputSchema'])
    return value


def runtime(db, row, user):
    from .agent_tools import require_origin
    require_origin(db, row, user)
    _, release, entry = api_capability(db, user, row.installation_id, row.capability_id,
                                      row.release_id, row.config['dependency_lock'])
    if entry.get('transport') != 'streamable-http': raise HTTPException(409, '固定 MCP 入口不可用')
    connection = connection_for(db, user, row.installation_id, row.capability_id, entry)
    catalog = catalog_for(db, user, connection)
    fixed = row.config['mcp']
    if (release.digest != row.config['release_digest'] or connection.id != fixed['connection_id'] or
            connection.version != fixed['connection_version'] or digest(entry) != fixed['binding_digest'] or
            catalog.digest != fixed['catalog_digest'] or catalog.version != fixed['catalog_version'] or
            fixed['tool'] not in catalog.approved_tools):
        raise HTTPException(409, '固定 MCP 版本、连接、目录或工具授权已变化')
    tool = next(t for t in catalog.tools if t['name'] == fixed['tool'])
    return entry, connection, catalog, tool


def preview_view(row):
    p = row.config['mcp_preview']
    return {'id': row.id, 'status': row.status, 'version': row.version,
            'digest': p['digest'], 'expires_at': p['expires_at'], 'plan': p['plan']}


@router.post('/api/plugin-invocations/mcp-preview', response_model=ApiPreviewView)
def preview(body: McpPreviewInput, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    fingerprint = digest(body.model_dump())
    previous = db.scalar(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.request_id == body.request_id))
    if previous:
        if previous.request_hash != fingerprint or not previous.config.get('mcp_preview'):
            raise HTTPException(409, '请求编号已用于其他调用')
        return preview_view(previous)
    for old in db.scalars(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
            PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.status == 'preview')):
        if (old.config.get('mcp_preview') or old.config.get('api_preview') or {}).get('expires_at', 0) <= now():
            old.status, old.input = 'cancelled', ''
    if db.scalar(select(func.count()).select_from(PluginInvocation).where(PluginInvocation.user_id == user.id,
            PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.status.in_(['preview', 'queued', 'running']))) >= 16:
        raise HTTPException(429, '等待确认或执行的插件调用已达 16 项')
    installation, release, entry = api_capability(db, user, body.installation_id, body.capability_id)
    if entry.get('transport') != 'streamable-http': raise HTTPException(409, '此能力不是远程 MCP')
    connection = connection_for(db, user, installation.id, body.capability_id, entry)
    catalog = catalog_for(db, user, connection)
    if body.tool not in catalog.approved_tools: raise HTTPException(403, '此工具尚未获得明确授权')
    if db.scalar(select(PluginInvocation.id).where(PluginInvocation.installation_id == installation.id,
            PluginInvocation.capability_id == body.capability_id, PluginInvocation.status == 'unknown')):
        raise HTTPException(409, '此 MCP 存在待核对结果；先核对第三方记录')
    tool = next(t for t in catalog.tools if t['name'] == body.tool)
    value = arguments(body.content, tool)
    plan = {'adapter': 'mcp', 'plugin_id': release.plugin_id, 'plugin_version': release.version,
        'release_digest': release.digest, 'capability_id': body.capability_id, 'url': entry['url'],
        'transport': entry['transport'], 'tool': body.tool, 'tool_digest': digest(tool), 'catalog_digest': catalog.digest,
        'effect': 'write', 'input': value, 'connection_id': connection.id, 'connection_version': connection.version,
        'allow_private': connection.allow_private, 'auth': entry['auth'],
        'costs': release.manifest['costs'], 'data_scope': release.manifest['data_scope']}
    row = PluginInvocation(user_id=user.id, workspace_id=user.workspace_id, installation_id=installation.id,
        release_id=release.id, capability_id=body.capability_id, request_id=body.request_id,
        request_hash=fingerprint, input=body.content, status='preview', config={
            'release_digest': release.digest, 'dependency_lock': dict(installation.dependency_lock),
            'mcp_preview': {'plan': plan, 'digest': digest(plan), 'expires_at': now()+300, 'login_session_id': login.id},
            'mcp': {'connection_id': connection.id, 'connection_version': connection.version,
                'binding_digest': digest(entry), 'catalog_digest': catalog.digest, 'catalog_version': catalog.version,
                'tool': body.tool, 'dispatch': 'not_sent'}})
    db.add(row); db.flush()
    audit(db, user, 'plugin_mcp_preview', row.id)
    return preview_view(row)


def confirm_mcp(db, user, login, row, body):
    p = row.config['mcp_preview']
    if p['login_session_id'] != login.id or p['digest'] != body.digest:
        raise HTTPException(409, '确认与当前登录及工具预览不匹配')
    if row.status != 'preview':
        if row.config['mcp'].get('confirmed_at'): return invocation_view(db, row)
        raise HTTPException(409, '工具预览已关闭')
    if p['expires_at'] <= now() or digest(p['plan']) != body.digest:
        raise HTTPException(409, '工具预览已过期或变化')
    _, _, _, tool = runtime(db, row, user)
    write_barrier(db, row, {'effect': 'write'})
    if arguments(row.input, tool) != p['plan']['input']: raise HTTPException(409, '工具输入已变化')
    row.status, row.version = 'queued', row.version+1
    row.config = row.config | {'mcp': row.config['mcp'] | {'confirmed_at': now()}}
    from .tasks import scheduled_job
    scheduled_job(db, 'plugin:'+row.id, 'plugin', {'invocation_id': row.id}, workspace_id=user.workspace_id)
    audit(db, user, 'plugin_mcp_confirm', row.id)
    return invocation_view(db, row)
