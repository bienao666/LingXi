"""Fixed-endpoint API plugins. Credentials and human confirmation stay in Core."""
import hashlib
import json
import math
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Audit, Entity, LoginSession, User
from .plugins import (PluginInstallation, PluginInvocation, PluginRelease, owner_lock,
                      require_capability)
from .plugin_manifest import digest
from .plugin_routes import InvocationView
from .security import encrypt, decrypt, safe_error, validate_endpoint
from .network import outbound_client
from .web import Context, audit, owned, view

class RedactedValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def redacted(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Validation errors must not echo submitted credential fields.
                return JSONResponse(status_code=422, content={'detail': [
                    {key: error[key] for key in ('loc', 'msg', 'type')} for error in exc.errors()]})
        return redacted


router = APIRouter(route_class=RedactedValidationRoute)
MAX_RESPONSE = 256 * 1024


class PluginConnection(Entity, Base):
    __tablename__ = 'plugin_connections'
    __table_args__ = (UniqueConstraint('installation_id', 'capability_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    capability_id: Mapped[str] = mapped_column(String(120))
    binding_digest: Mapped[str] = mapped_column(String(64))
    encrypted_secret: Mapped[str] = mapped_column(Text, default='')
    allow_private: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    secret: str = Field(default='', max_length=4000)
    allow_private: bool = False
    version: int = Field(default=0, ge=0)
    acknowledged: Literal[True]


class ConnectionView(BaseModel):
    id: str
    installation_id: str
    capability_id: str
    plugin_id: str
    status: str
    version: int
    allow_private: bool
    has_secret: bool
    available: bool
    reason: str | None
    url: str
    method: str
    auth: str
    health_url: str
    checked_at: float | None
    oauth: dict | None = None


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class ApiPreviewInput(BaseModel):
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=8, max_length=100)


class ApiPreviewView(BaseModel):
    id: str
    digest: str
    expires_at: float
    status: str
    version: int
    plan: dict


class ApiConfirm(BaseModel):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class Resolution(BaseModel):
    version: int = Field(ge=1)
    outcome: Literal['applied', 'not_applied']
    evidence: str = Field(min_length=5, max_length=500)
    acknowledged: Literal[True]


def api_capability(db, user, installation_id, capability_id, release_id=None, dependency_lock=None):
    installation = owned(db, PluginInstallation, installation_id, user)
    _, release, cap = require_capability(db, user, capability_id, installation.plugin_id, release_id,
                                       dependency_lock=dependency_lock)
    if cap['kind'] not in ('api', 'mcp'):
        raise HTTPException(409, '此能力不是远程 API / MCP 插件')
    return installation, release, cap[cap['kind']]


def connection_for(db, user, installation_id, capability_id, entry):
    connection = db.scalar(select(PluginConnection).where(PluginConnection.installation_id == installation_id,
        PluginConnection.capability_id == capability_id, PluginConnection.user_id == user.id,
        PluginConnection.workspace_id == user.workspace_id))
    if not connection or connection.status != 'active' or connection.binding_digest != digest(entry):
        raise HTTPException(409, '请先在账号授权中配置此 API 的有效连接；入口变化后需重新授权')
    if entry['auth'] != 'none' and not connection.encrypted_secret:
        raise HTTPException(409, '连接缺少凭据，请重新授权')
    if entry['auth'] == 'oauth2':
        from .plugin_oauth import usable
        usable(db, connection)
    return connection


def connection_view(db, user, row):
    from .plugin_oauth import grant_view
    installation = db.get(PluginInstallation, row.installation_id)
    entry, reason = {}, None
    try:
        _, _, entry = api_capability(db, user, row.installation_id, row.capability_id)
        connection_for(db, user, row.installation_id, row.capability_id, entry)
    except HTTPException as exc:
        reason = str(exc.detail)
    return view(row, ('encrypted_secret', 'binding_digest')) | {
        'plugin_id': installation.plugin_id, 'has_secret': bool(row.encrypted_secret),
        'available': reason is None, 'reason': reason,
        'oauth': grant_view(db, row) if entry.get('auth', 'oauth2') == 'oauth2' else None,
        **{k: entry.get(k, '') for k in ('url', 'method', 'auth', 'health_url')}}


def auth_headers(connection, entry):
    if entry['auth'] == 'oauth2':
        from .plugin_oauth import access_header
        return access_header(connection)
    if entry['auth'] == 'none':
        return {}
    secret = decrypt(connection.encrypted_secret)
    return {'Authorization': 'Bearer ' + secret} if entry['auth'] == 'bearer' else {entry['key_header']: secret}


def login_valid(db, identity):
    login = db.get(LoginSession, identity, populate_existing=True)
    if not login or login.expires_at <= now():
        raise HTTPException(401, '登录已失效，请重新确认身份')


def reserve(db, user):
    if db.scalar(select(func.count()).select_from(Audit).where(Audit.user_id == user.id,
            Audit.workspace_id == user.workspace_id, Audit.action == 'plugin_api_request',
            Audit.created_at > now() - 60)) >= 30:
        raise HTTPException(429, 'API 插件请求过多，请一分钟后重试')
    audit(db, user, 'plugin_api_request')


@router.get('/api/plugin-connections', response_model=list[ConnectionView])
def connections(ctx: Context):
    db, user, _ = ctx
    return [connection_view(db, user, row) for row in db.scalars(select(PluginConnection).where(
        PluginConnection.user_id == user.id, PluginConnection.workspace_id == user.workspace_id)
        .order_by(PluginConnection.created_at.desc()).limit(200))]


def save_connection(db, user, body, identity=None):
    owner_lock(db, user)
    _, _, entry = api_capability(db, user, body.installation_id, body.capability_id)
    if entry['auth'] == 'oauth2':
        raise HTTPException(409, 'OAuth 连接须通过授权码＋PKCE 完成，不能手工填写访问令牌')
    row = owned(db, PluginConnection, identity, user, True) if identity else None
    if row and (row.installation_id != body.installation_id or row.capability_id != body.capability_id):
        raise HTTPException(409, '不能将已有账号连接移动到其他插件能力')
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '账号连接已变化，请重新加载')
    if not row and db.scalar(select(PluginConnection).where(PluginConnection.installation_id == body.installation_id,
            PluginConnection.capability_id == body.capability_id)):
        raise HTTPException(409, '此能力已有连接，请编辑原连接')
    validate_endpoint(entry['url'], body.allow_private)
    if entry.get('health_url'):
        validate_endpoint(entry['health_url'], body.allow_private)
    secret = body.secret
    if any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise HTTPException(422, '凭据须为无空白的可打印 ASCII 标识')
    same_binding = row is not None and row.binding_digest == digest(entry)
    encrypted = encrypt(secret) if secret else (row.encrypted_secret if same_binding and row.status == 'active' else '')
    if entry['auth'] != 'none' and not encrypted:
        raise HTTPException(422, '请提供当前 API 所需凭据；重新授权时不能恢复已撤销凭据')
    if entry['auth'] == 'none':
        if secret:
            raise HTTPException(422, '此接口未声明凭据，不接收密钥')
        encrypted = ''
    if not row:
        if db.scalar(select(func.count()).select_from(PluginConnection).where(PluginConnection.user_id == user.id)) >= 200:
            raise HTTPException(409, '连接数量达到 200 项，请先整理插件能力')
        row = PluginConnection(user_id=user.id, workspace_id=user.workspace_id,
            installation_id=body.installation_id, capability_id=body.capability_id)
        db.add(row)
    else:
        row.version += 1
        invalidate_api(db, user, row.installation_id, 'API 连接已变化', row.capability_id)
        from .plugin_oauth import revoke_local
        revoke_local(db, row)
    row.binding_digest, row.encrypted_secret, row.allow_private = digest(entry), encrypted, body.allow_private
    row.status, row.checked_at, row.updated_at = 'active', None, now()
    db.flush()
    audit(db, user, 'plugin_connection_save', row.id)
    return connection_view(db, user, row)


@router.post('/api/plugin-connections', response_model=ConnectionView)
def create_connection(body: ConnectionInput, ctx: Context):
    return save_connection(ctx[0], ctx[1], body)


@router.put('/api/plugin-connections/{identity}', response_model=ConnectionView)
def update_connection(identity: str, body: ConnectionInput, ctx: Context):
    return save_connection(ctx[0], ctx[1], body, identity)


@router.post('/api/plugin-connections/{identity}/revoke', response_model=ConnectionView)
def revoke_connection(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginConnection, identity, user, True)
    if row.version != body.version:
        raise HTTPException(409, '连接已变化，请重新加载')
    from .plugin_oauth import grant_for, revoke_remote, revoke_local
    if grant_for(db, row):
        try:
            _, _, entry = api_capability(db, user, row.installation_id, row.capability_id)
        except HTTPException:
            entry = {}
        if entry.get('auth') == 'oauth2' and digest(entry) == row.binding_digest:
            revoke_remote(db, user, row, entry)
            return connection_view(db, user, row)
    revoke_local(db, row)
    row.status, row.encrypted_secret, row.checked_at = 'revoked', '', None
    row.version += 1
    invalidate_api(db, user, row.installation_id, '账号连接已撤销', row.capability_id)
    audit(db, user, 'plugin_connection_revoke', row.id)
    return connection_view(db, user, row)


@router.post('/api/plugin-connections/{identity}/test', response_model=ConnectionView)
def test_connection(identity: str, body: VersionInput, ctx: Context):
    db, user, login = ctx
    from .plugin_oauth import ensure_request
    ensure_request(db, user, identity, body.version)
    owner_lock(db, user)
    row = owned(db, PluginConnection, identity, user)
    _, _, entry = api_capability(db, user, row.installation_id, row.capability_id)
    connection_for(db, user, row.installation_id, row.capability_id, entry)
    if row.version != body.version:
        raise HTTPException(409, '连接已变化，请重新加载')
    if not entry.get('health_url'):
        raise HTTPException(409, '插件未声明只读健康接口；可通过请求预览执行并查看回执')
    reserve(db, user)
    version = row.version
    db.commit()
    try:
        with outbound_client(entry['health_url'], allow_private=row.allow_private,
                             headers=auth_headers(row, entry), timeout=15) as client:
            with client.stream('GET', entry['health_url']) as result:
                if not 200 <= result.status_code < 300:
                    raise HTTPException(502, f'健康接口返回 HTTP {result.status_code}；未跟随重定向')
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from None
    db.expire_all()
    owner_lock(db, user)
    login_valid(db, login.id)
    row = owned(db, PluginConnection, identity, user, True)
    _, _, current = api_capability(db, user, row.installation_id, row.capability_id)
    connection_for(db, user, row.installation_id, row.capability_id, current)
    if row.version != version or digest(entry) != digest(current):
        raise HTTPException(409, '连接或入口已变化，测试结果已丢弃')
    row.checked_at = now()
    audit(db, user, 'plugin_connection_test', row.id)
    return connection_view(db, user, row)


def input_value(content, entry):
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError()
                result[key] = value
            return result
        value = json.loads(content, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or len(content.encode()) > 24000:
            raise ValueError()
        fields = {p['name']: p for p in entry['parameters']}
        if set(value) - set(fields) or any(p['required'] and p['name'] not in value for p in entry['parameters']):
            raise ValueError()
        kinds = {'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
                 'number': lambda v: type(v) in (int, float) and math.isfinite(v),
                 'boolean': lambda v: type(v) is bool, 'object': lambda v: isinstance(v, dict),
                 'array': lambda v: isinstance(v, list), 'null': lambda v: v is None}
        if any(not kinds[fields[k]['type']](v) for k, v in value.items()):
            raise ValueError()
        def depth(v, level=0):
            if level > 8: raise ValueError()
            if type(v) in (int, float) and (not math.isfinite(v) or abs(v) > 1e100): raise ValueError()
            if isinstance(v, (dict, list)):
                if len(v) > 128: raise ValueError()
                for child in (v.values() if isinstance(v, dict) else v): depth(child, level+1)
        depth(value)
        return value
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise HTTPException(422, 'API 输入须为符合已声明字段及类型的 JSON 对象；最多 24 KiB、嵌套 8 层') from None


def invalidate_api(db, user, installation_id, reason, capability_id=None):
    for row in db.scalars(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
            PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.installation_id == installation_id,
            PluginInvocation.status.in_(['preview', 'queued', 'running']))):
        fixed = row.config.get('api') or row.config.get('mcp')
        if not fixed or (capability_id and row.capability_id != capability_id): continue
        sent = fixed.get('dispatch') == 'in_flight'
        row.status = 'unknown' if sent else 'invalidated'
        row.result, row.error, row.finished_at = '', reason + ('；外部结果待核对，不会重发' if sent else ''), now()
        row.version += 1


def preview_view(row):
    preview = row.config['api_preview']
    return {'id': row.id, 'status': row.status, 'version': row.version,
            'digest': preview['digest'], 'expires_at': preview['expires_at'], 'plan': preview['plan']}


@router.post('/api/plugin-invocations/preview', response_model=ApiPreviewView)
def preview_api(body: ApiPreviewInput, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    fingerprint = digest(body.model_dump())
    previous = db.scalar(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.request_id == body.request_id))
    if previous:
        if previous.request_hash != fingerprint or not previous.config.get('api_preview'):
            raise HTTPException(409, '请求编号已用于其他调用')
        return preview_view(previous)
    for old in db.scalars(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
            PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.status == 'preview')):
        if (old.config.get('api_preview') or old.config.get('mcp_preview') or {}).get('expires_at', 0) <= now():
            old.status, old.input = 'cancelled', ''
    pending = db.scalar(select(func.count()).select_from(PluginInvocation).where(PluginInvocation.user_id == user.id,
        PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.status.in_(['preview', 'queued', 'running'])))
    if pending >= 16:
        raise HTTPException(429, '等待确认或执行的插件调用已达 16 项')
    installation, release, entry = api_capability(db, user, body.installation_id, body.capability_id)
    if entry.get('transport'):
        raise HTTPException(409, 'MCP 请先发现并授权工具，再生成工具调用预览')
    connection = connection_for(db, user, installation.id, body.capability_id, entry)
    if entry['effect'] == 'write' and db.scalar(select(PluginInvocation.id).where(
            PluginInvocation.installation_id == installation.id, PluginInvocation.capability_id == body.capability_id,
            PluginInvocation.status == 'unknown')):
        raise HTTPException(409, '此 API 存在待核对的外部结果；先在运行记录核对，不得盲目再次操作')
    value = input_value(body.content, entry)
    plan = {'plugin_id': release.plugin_id, 'plugin_version': release.version, 'release_digest': release.digest,
        'capability_id': body.capability_id, 'url': entry['url'], 'method': entry['method'],
        'effect': entry['effect'], 'input': value, 'auth': entry['auth'], 'allow_private': connection.allow_private,
        'connection_id': connection.id, 'connection_version': connection.version,
        'costs': release.manifest['costs'], 'data_scope': release.manifest['data_scope']}
    preview = {'plan': plan, 'digest': digest(plan), 'login_session_id': login.id, 'expires_at': now()+300}
    row = PluginInvocation(user_id=user.id, workspace_id=user.workspace_id, installation_id=installation.id,
        release_id=release.id, capability_id=body.capability_id, request_id=body.request_id,
        request_hash=fingerprint, input=body.content, status='preview',
        config={'release_digest': release.digest, 'dependency_lock': dict(installation.dependency_lock),
                'api_preview': preview, 'api': {'connection_id': connection.id, 'connection_version': connection.version,
                                              'binding_digest': digest(entry), 'dispatch': 'not_sent'}})
    db.add(row); db.flush()
    audit(db, user, 'plugin_api_preview', row.id)
    return preview_view(row)


def runtime(db, row, user):
    from .agent_tools import require_origin
    require_origin(db, row, user)
    installation, release, entry = api_capability(db, user, row.installation_id, row.capability_id,
                                                row.release_id, row.config['dependency_lock'])
    connection = connection_for(db, user, installation.id, row.capability_id, entry)
    fixed = row.config['api']
    if (release.digest != row.config['release_digest'] or connection.id != fixed['connection_id'] or
            connection.version != fixed['connection_version'] or digest(entry) != fixed['binding_digest']):
        raise HTTPException(409, '固定版本、API 入口或账号连接已变化')
    return entry, connection


def write_barrier(db, row, entry):
    # The owner lock serializes confirmations and worker send boundaries.
    if entry['effect'] == 'write' and db.scalar(select(PluginInvocation.id).where(
            PluginInvocation.user_id == row.user_id, PluginInvocation.workspace_id == row.workspace_id,
            PluginInvocation.installation_id == row.installation_id,
            PluginInvocation.capability_id == row.capability_id, PluginInvocation.id != row.id,
            PluginInvocation.status.in_(['queued', 'running', 'unknown']))):
        raise HTTPException(409, '此 API 已有在途或待核对请求；请先查看运行记录，不得重复发送外部操作')


@router.post('/api/plugin-invocations/{identity}/confirm', response_model=InvocationView)
def confirm_api(identity: str, body: ApiConfirm, ctx: Context):
    from .plugin_routes import invocation_view
    from .tasks import scheduled_job
    db, user, login = ctx
    owner_lock(db, user)
    row = owned(db, PluginInvocation, identity, user, True)
    if row.config.get('mcp'):
        from .plugin_mcp import confirm_mcp
        return confirm_mcp(db, user, login, row, body)
    preview = row.config.get('api_preview', {})
    if not preview or preview['login_session_id'] != login.id or body.digest != preview['digest']:
        raise HTTPException(409, '确认与当前登录及请求预览不匹配')
    if row.status != 'preview':
        if row.config['api'].get('confirmed_at'): return invocation_view(db, row)
        raise HTTPException(409, '请求预览已关闭')
    if preview['expires_at'] <= now() or digest(preview['plan']) != body.digest:
        raise HTTPException(409, '请求预览已过期或变化，请重新预览')
    entry, connection = runtime(db, row, user)
    write_barrier(db, row, entry)
    if input_value(row.input, entry) != preview['plan']['input']:
        raise HTTPException(409, '输入已变化，请重新预览')
    row.status, row.version = 'queued', row.version+1
    row.config = row.config | {'api': row.config['api'] | {'confirmed_at': now()}}
    scheduled_job(db, 'plugin:'+row.id, 'plugin', {'invocation_id': row.id}, workspace_id=user.workspace_id)
    audit(db, user, 'plugin_api_confirm', row.id)
    return invocation_view(db, row)


@router.post('/api/plugin-invocations/{identity}/resolve', response_model=InvocationView)
def resolve_api(identity: str, body: Resolution, ctx: Context):
    from .plugin_routes import invocation_view
    db, user, _ = ctx
    owner_lock(db, user)
    row = owned(db, PluginInvocation, identity, user, True)
    if row.status != 'unknown' or row.version != body.version:
        raise HTTPException(409, '仅可核对当前版本的未知结果')
    row.status, row.version, row.finished_at = 'resolved', row.version+1, now()
    row.receipt = row.receipt | {'user_reported_outcome': body.outcome, 'evidence': body.evidence,
        'note': '用户在第三方服务核对后登记；系统未独立证明外部执行结果，不会自动重发。'}
    audit(db, user, 'plugin_api_resolve', row.id)
    return invocation_view(db, row)
