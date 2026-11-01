"""Fixed OAuth authorization-code/PKCE connections. No discovery or token replay."""
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import Float, Integer, JSON, String, Text, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column, object_session
from .db import Base, SessionLocal, now
from .models import Entity, User, Audit, LoginSession
from .plugins import owner_lock, PluginInstallation, PluginInvocation
from .plugin_api import (PluginConnection, RedactedValidationRoute, api_capability,
                         invalidate_api, login_valid)
from .plugin_manifest import digest
from .security import encrypt, decrypt, token_hash, validate_endpoint
from .network import outbound_client
from .web import Context, owned, audit
from .config import settings

router = APIRouter(route_class=RedactedValidationRoute)


class OAuthGrant(Entity, Base):
    __tablename__ = 'plugin_oauth_grants'
    __table_args__ = (UniqueConstraint('connection_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_config: Mapped[str] = mapped_column(Text, default='')
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(30), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    operation: Mapped[str] = mapped_column(String(64), default='')
    deadline: Mapped[float] = mapped_column(Float, default=0)
    remote_status: Mapped[str] = mapped_column(String(30), default='not_requested')


class OAuthFlow(Entity, Base):
    __tablename__ = 'plugin_oauth_flows'
    __table_args__ = (UniqueConstraint('state_hash'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    login_id: Mapped[str] = mapped_column(String(64))
    state_hash: Mapped[str] = mapped_column(String(64))
    encrypted_config: Mapped[str] = mapped_column(Text, default='')
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    expires_at: Mapped[float] = mapped_column(Float)


class OAuthStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    installation_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=120)
    installation_version: int = Field(ge=1)
    connection_version: int = Field(default=0, ge=0)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(default='', max_length=4000)
    allow_private: bool = False
    acknowledged: Literal[True]


class OAuthStarted(BaseModel):
    id: str
    authorization_url: str
    redirect_uri: str
    expires_at: float
    scopes: list[str]


class OAuthFlowView(BaseModel):
    id: str
    connection_id: str
    status: str
    created_at: float
    expires_at: float


def grant_for(db, connection):
    return db.scalar(select(OAuthGrant).where(OAuthGrant.connection_id == connection.id,
        OAuthGrant.user_id == connection.user_id, OAuthGrant.workspace_id == connection.workspace_id))


def grant_view(db, connection):
    row = grant_for(db, connection)
    if not row: return None
    return {'status': row.status, 'scopes': row.scopes, 'expires_at': row.expires_at,
            'remote_status': row.remote_status,
            'refreshable': bool(row.status == 'active' and row.encrypted_config and
                               json.loads(decrypt(row.encrypted_config)).get('refresh_token'))}


def usable(db, connection):
    grant = grant_for(db, connection)
    if not grant or grant.status != 'active' or not grant.encrypted_config:
        raise HTTPException(409, 'OAuth 授权不可用或正在刷新，请重新加载；失效时需重新授权')
    if grant.expires_at <= now() + 5 and not json.loads(decrypt(grant.encrypted_config)).get('refresh_token'):
        raise HTTPException(409, 'OAuth 令牌已到期且不能刷新，请重新授权')
    return grant


def access_header(connection):
    grant = usable(object_session(connection), connection)
    if grant.expires_at <= now() + 5:
        raise HTTPException(409, 'OAuth 令牌需先刷新，业务请求尚未发送')
    return {'Authorization': 'Bearer ' + decrypt(connection.encrypted_secret)}


def fixed_entry(db, user, connection):
    installation, release, entry = api_capability(db, user, connection.installation_id, connection.capability_id)
    if (entry['auth'] != 'oauth2' or connection.binding_digest != digest(entry)):
        raise HTTPException(409, 'OAuth 入口已变化，请重新授权')
    return installation, release, entry


def check_urls(entry, allow_private):
    for value in (entry['url'], entry.get('health_url'), *[
            entry['oauth'].get(k) for k in ('authorization_url', 'token_url', 'revocation_url', 'issuer', 'resource')]):
        if value: validate_endpoint(value, allow_private)


def redirect_uri():
    origin = settings().public_origin
    p = urlsplit(origin)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or
            p.path not in ('', '/') or p.query or p.fragment or '\\' in origin or
            (p.scheme != 'https' and p.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise HTTPException(409, 'OAuth 需要可信的 PUBLIC_ORIGIN：生产 HTTPS，或本机回环 HTTP')
    return origin.rstrip('/') + '/api/plugin-oauth/callback'


def clear_flows(db, connection_id, status='cancelled'):
    for flow in db.scalars(select(OAuthFlow).where(OAuthFlow.connection_id == connection_id,
                                                  OAuthFlow.status.in_(['pending', 'exchanging']))):
        flow.status, flow.encrypted_config = status, ''


def revoke_local(db, connection):
    clear_flows(db, connection.id)
    grant = grant_for(db, connection)
    if grant:
        grant.status, grant.encrypted_config, grant.operation = 'revoked', '', ''
        grant.version += 1
        grant.expires_at, grant.deadline = 0, 0
        grant.remote_status = 'not_requested'


def invalidate_installation(db, user, installation_id):
    for connection in db.scalars(select(PluginConnection).where(
            PluginConnection.installation_id == installation_id, PluginConnection.user_id == user.id,
            PluginConnection.workspace_id == user.workspace_id)):
        if grant_for(db, connection) or db.scalar(select(OAuthFlow.id).where(OAuthFlow.connection_id == connection.id)):
            revoke_local(db, connection)
            connection.status, connection.encrypted_secret, connection.checked_at = 'revoked', '', None
            connection.version += 1


@router.post('/api/plugin-oauth/start', response_model=OAuthStarted)
def start(body: OAuthStart, ctx: Context):
    db, user, login = ctx
    owner_lock(db, user)
    installation, release, entry = api_capability(db, user, body.installation_id, body.capability_id)
    if entry['auth'] != 'oauth2': raise HTTPException(409, '此能力未声明 OAuth 连接')
    if installation.version != body.installation_version: raise HTTPException(409, '插件安装已变化，请重新加载')
    if db.scalar(select(func.count()).select_from(Audit).where(Audit.user_id == user.id,
            Audit.action == 'plugin_oauth_start', Audit.created_at > now()-60)) >= 10:
        raise HTTPException(429, '授权启动过于频繁，请一分钟后重试')
    check_urls(entry, body.allow_private)
    callback = redirect_uri()
    for value in (body.client_id, body.client_secret):
        if any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise HTTPException(422, '客户端标识与密钥须为无空白的可打印 ASCII')
    if (entry['oauth']['client_auth'] == 'none') == bool(body.client_secret):
        raise HTTPException(422, '请按插件声明填写专用客户端密钥；公共客户端不能提交密钥')
    connection = db.scalar(select(PluginConnection).where(PluginConnection.installation_id == installation.id,
        PluginConnection.capability_id == body.capability_id))
    if connection and (connection.user_id != user.id or connection.workspace_id != user.workspace_id):
        raise HTTPException(404, '连接不存在')
    if body.connection_version != (connection.version if connection else 0):
        raise HTTPException(409, '账号连接已变化，请重新加载')
    if connection:
        grant = grant_for(db, connection)
        if grant and grant.remote_status == 'in_flight' and grant.deadline > now():
            raise HTTPException(409, '服务方撤销请求仍在处理中，请稍后重新加载')
    if not connection:
        if db.scalar(select(func.count()).select_from(PluginConnection).where(PluginConnection.user_id == user.id)) >= 200:
            raise HTTPException(409, '连接数量达到 200 项，请先整理')
        connection = PluginConnection(user_id=user.id, workspace_id=user.workspace_id,
            installation_id=installation.id, capability_id=body.capability_id,
            binding_digest=digest(entry), status='pending', allow_private=body.allow_private)
        db.add(connection); db.flush()
    elif connection.binding_digest != digest(entry):
        revoke_local(db, connection)
        connection.status, connection.encrypted_secret, connection.checked_at = 'pending', '', None
        connection.binding_digest, connection.allow_private = digest(entry), body.allow_private
        connection.version += 1
        invalidate_api(db, user, connection.installation_id, 'OAuth 入口变化，等待重新授权', connection.capability_id)
    clear_flows(db, connection.id)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    flow = OAuthFlow(user_id=user.id, workspace_id=user.workspace_id, connection_id=connection.id,
        login_id=login.id, state_hash=token_hash(state), expires_at=now()+600,
        encrypted_config=encrypt(json.dumps({'client_id': body.client_id, 'client_secret': body.client_secret,
            'verifier': verifier})), snapshot={'installation_version': installation.version,
            'release_digest': release.digest, 'connection_version': connection.version,
            'binding_digest': digest(entry), 'entry': entry, 'allow_private': body.allow_private, 'redirect_uri': callback})
    db.add(flow); db.flush(); audit(db, user, 'plugin_oauth_start', connection.id)
    params = dict(response_type='code', client_id=body.client_id, redirect_uri=callback,
        scope=' '.join(entry['oauth']['scopes']), state=state, code_challenge_method='S256',
        code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
    if entry['oauth']['resource']: params['resource'] = entry['oauth']['resource']
    return {'id': flow.id, 'authorization_url': entry['oauth']['authorization_url']+'?'+urlencode(params),
            'redirect_uri': callback, 'expires_at': flow.expires_at, 'scopes': entry['oauth']['scopes']}


@router.get('/api/plugin-oauth/flows', response_model=list[OAuthFlowView])
def flows(ctx: Context):
    db, user, _ = ctx
    return [dict(id=f.id, connection_id=f.connection_id, status='expired' if f.status == 'pending' and
        f.expires_at <= now() else f.status, created_at=f.created_at, expires_at=f.expires_at)
        for f in db.scalars(select(OAuthFlow).where(OAuthFlow.user_id == user.id,
            OAuthFlow.workspace_id == user.workspace_id).order_by(OAuthFlow.created_at.desc()).limit(20))]


@router.post('/api/plugin-oauth/flows/{identity}/cancel', response_model=OAuthFlowView)
def cancel(identity: str, ctx: Context):
    db, user, _ = ctx
    owner_lock(db, user)
    flow = owned(db, OAuthFlow, identity, user, True)
    if flow.status in ('pending', 'exchanging'): flow.status, flow.encrypted_config = 'cancelled', ''
    return dict(id=flow.id, connection_id=flow.connection_id, status=flow.status,
                created_at=flow.created_at, expires_at=flow.expires_at)


def token_request(oauth, config, fields, allow_private, *, revocation=False):
    url = oauth['revocation_url'] if revocation else oauth['token_url']
    headers = {'Accept': 'application/json'}
    fields = dict(fields)
    if oauth['client_auth'] == 'client_secret_basic':
        raw = quote_plus(config['client_id']) + ':' + quote_plus(config['client_secret'])
        headers['Authorization'] = 'Basic '+base64.b64encode(raw.encode()).decode()
    else:
        fields['client_id'] = config['client_id']
        if oauth['client_auth'] == 'client_secret_post': fields['client_secret'] = config['client_secret']
    # Never use the normal model error helper on token payloads or exception strings.
    try:
        with outbound_client(url, allow_private=allow_private, headers=headers, timeout=20) as client:
            with client.stream('POST', url, data=fields) as response:
                if not 200 <= response.status_code < 300: raise ValueError('token status')
                if revocation: return {}
                if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    raise ValueError('token content type')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 16384: raise ValueError('token budget')
        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result: raise ValueError('duplicate')
                result[k] = v
            return result
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict): raise ValueError('token object')
        return result
    except Exception:
        raise HTTPException(502, 'OAuth 服务未返回可核验的成功响应；请检查服务与客户端配置，勿重放授权码或刷新请求') from None


def tokens(value, requested, *, previous_refresh=''):
    if 'error' in value: raise ValueError('token error')
    access, refresh = value.get('access_token'), value.get('refresh_token', previous_refresh)
    if str(value.get('token_type', '')).lower() != 'bearer': raise ValueError('token type')
    if not isinstance(access, str) or not re.fullmatch(r'[\x21-\x7e]{1,4000}', access): raise ValueError('access')
    if not isinstance(refresh, str) or (refresh and not re.fullmatch(r'[\x21-\x7e]{1,4000}', refresh)): raise ValueError('refresh')
    expiry = value.get('expires_in')
    if isinstance(expiry, str) and re.fullmatch(r'[0-9]{1,9}', expiry): expiry = int(expiry)
    if type(expiry) is not int or not 60 <= expiry <= 31536000: raise ValueError('expiry')
    scope = value.get('scope', ' '.join(requested))
    if not isinstance(scope, str) or not scope or len(scope) > 4000: raise ValueError('scope')
    scopes = scope.split(' ')
    if len(set(scopes)) != len(scopes) or not set(scopes) <= set(requested): raise ValueError('scope expansion')
    return access, refresh, sorted(scopes), now()+expiry


def callback_response(success):
    return RedirectResponse('/#/plugins/connections?oauth='+('succeeded' if success else 'failed'), status_code=303,
        headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
                 'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})


@router.get('/api/plugin-oauth/callback', include_in_schema=False)
def callback(request: Request):
    # Parse manually: never echo codes/state in validation errors or access logs.
    raw = request.scope.get('query_string', b'')
    request.scope['query_string'] = b''
    try:
        if len(raw) > 12000: return callback_response(False)
        query = parse_qs(raw.decode('ascii'), keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if any(len(v) != 1 for v in query.values()) or set(query)-{'state','code','error','error_description','error_uri','iss'}:
            return callback_response(False)
        state = query.get('state', [''])[0]
        if not re.fullmatch(r'[a-zA-Z0-9_-]{43}', state): return callback_response(False)
        from .security import authenticate
        with SessionLocal() as db:
            user, login = authenticate(request, db)
            owner_lock(db, user)
            flow = db.scalar(select(OAuthFlow).where(OAuthFlow.state_hash == token_hash(state),
                OAuthFlow.user_id == user.id, OAuthFlow.workspace_id == user.workspace_id).with_for_update())
            if not flow or flow.login_id != login.id or flow.status != 'pending': return callback_response(False)
            connection = owned(db, PluginConnection, flow.connection_id, user, True)
            installation, release, entry = fixed_entry(db, user, connection)
            fixed = dict(flow.snapshot)
            if (flow.expires_at <= now() or fixed['redirect_uri'] != redirect_uri() or
                    'error' in query or not query.get('code', [''])[0] or
                    ('iss' in query and query['iss'][0] != entry['oauth']['issuer']) or
                    len(query.get('code',[''])[0]) > 4000 or
                    any(ord(c) < 33 or ord(c) > 126 for c in query.get('code', [''])[0]) or
                    installation.version != fixed['installation_version'] or release.digest != fixed['release_digest'] or
                    connection.version != fixed['connection_version'] or digest(entry) != fixed['binding_digest']):
                flow.status, flow.encrypted_config = 'failed', ''; db.commit(); return callback_response(False)
            config = json.loads(decrypt(flow.encrypted_config))
            flow.status, flow.encrypted_config = 'exchanging', ''
            flow_id, user_id, login_id = flow.id, user.id, login.id
            db.commit()  # One use, even after process death or an ambiguous token response.
        result = token_request(entry['oauth'], config, dict(grant_type='authorization_code',
            code=query['code'][0], code_verifier=config['verifier'], redirect_uri=fixed['redirect_uri'],
            **({'resource':entry['oauth']['resource']} if entry['oauth']['resource'] else {})), fixed['allow_private'])
        access, refresh, scopes, expiry = tokens(result, entry['oauth']['scopes'])
        with SessionLocal() as db:
            user = db.get(User, user_id); owner_lock(db, user); login_valid(db, login_id)
            flow = owned(db, OAuthFlow, flow_id, user, True)
            connection = owned(db, PluginConnection, flow.connection_id, user, True)
            installation, release, current = fixed_entry(db, user, connection)
            if (flow.status != 'exchanging' or flow.expires_at <= now() or
                    installation.version != fixed['installation_version'] or release.digest != fixed['release_digest'] or
                    connection.version != fixed['connection_version'] or digest(current) != fixed['binding_digest']):
                raise ValueError('late callback')
            grant = grant_for(db, connection)
            if not grant:
                grant = OAuthGrant(user_id=user.id, workspace_id=user.workspace_id, connection_id=connection.id)
                db.add(grant)
            else: grant.version += 1
            grant.status, grant.scopes, grant.expires_at = 'active', scopes, expiry
            grant.operation, grant.deadline, grant.remote_status = '', 0, 'not_requested'
            grant.encrypted_config = encrypt(json.dumps({'client_id':config['client_id'],
                'client_secret':config['client_secret'], 'refresh_token':refresh}))
            connection.status, connection.encrypted_secret = 'active', encrypt(access)
            connection.allow_private, connection.checked_at = fixed['allow_private'], None
            connection.version += 1; connection.updated_at = now()
            invalidate_api(db, user, connection.installation_id, 'OAuth 账号授权已变化', connection.capability_id)
            flow.status = 'succeeded'; audit(db, user, 'plugin_oauth_connected', connection.id); db.commit()
        return callback_response(True)
    except Exception:
        # If an exchange was consumed, clear its status without storing upstream data.
        if 'flow_id' in locals():
            with SessionLocal.begin() as db:
                flow = db.get(OAuthFlow, flow_id)
                if flow and flow.status == 'exchanging': flow.status, flow.encrypted_config = 'failed', ''
        return callback_response(False)


def ensure_access(user_id, workspace_id, connection_id):
    """Refresh before acquiring business execution locks; never replay a rotating token."""
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or user.workspace_id != workspace_id: raise HTTPException(404, '连接不存在')
        owner_lock(db, user)
        connection = owned(db, PluginConnection, connection_id, user, True)
        _, _, entry = fixed_entry(db, user, connection)
        if connection.status != 'active': raise HTTPException(409, 'OAuth 连接已撤销，请重新授权')
        grant = usable(db, connection)
        if grant.expires_at > now()+30: return
        config = json.loads(decrypt(grant.encrypted_config))
        if not config.get('refresh_token'):
            if grant.expires_at > now()+5: return
            raise HTTPException(409, 'OAuth 令牌已到期，请重新授权')
        operation, version, binding, allow_private = secrets.token_hex(16), connection.version, connection.binding_digest, connection.allow_private
        grant.status, grant.operation, grant.deadline = 'refreshing', operation, now()+60
        db.commit()  # Crash/timeout is ambiguous; no automatic retry of this refresh token.
    try:
        fields = dict(grant_type='refresh_token', refresh_token=config['refresh_token'], scope=' '.join(grant.scopes))
        if entry['oauth']['resource']: fields['resource'] = entry['oauth']['resource']
        result = token_request(entry['oauth'], config, fields, allow_private)
        access, refresh, scopes, expiry = tokens(result, grant.scopes, previous_refresh=config['refresh_token'])
        with SessionLocal() as db:
            user = db.get(User, user_id); owner_lock(db, user)
            connection = owned(db, PluginConnection, connection_id, user, True)
            _, _, current = fixed_entry(db, user, connection)
            current_grant = grant_for(db, connection)
            if (connection.status != 'active' or connection.version != version or digest(current) != binding or
                    current_grant.status != 'refreshing' or current_grant.operation != operation or
                    current_grant.deadline <= now()): raise ValueError('late refresh')
            if scopes != current_grant.scopes:
                # A narrowed grant cannot silently retain old business/tool approvals.
                connection.version += 1
                invalidate_api(db, user, connection.installation_id, 'OAuth 范围缩小，请重新预览和确认工具', connection.capability_id)
            current_grant.encrypted_config = encrypt(json.dumps(config | {'refresh_token':refresh}))
            current_grant.scopes, current_grant.expires_at = scopes, expiry
            current_grant.status, current_grant.operation, current_grant.deadline = 'active', '', 0
            current_grant.version += 1
            connection.encrypted_secret = encrypt(access)
            audit(db, user, 'plugin_oauth_refresh', connection.id); db.commit()
    except Exception:
        with SessionLocal.begin() as db:
            user = db.get(User, user_id); owner_lock(db, user)
            connection = owned(db, PluginConnection, connection_id, user, True)
            current_grant = grant_for(db, connection)
            if current_grant and current_grant.status == 'refreshing' and current_grant.operation == operation:
                revoke_local(db, connection); current_grant.status = 'reauthorize'
                connection.encrypted_secret, connection.status = '', 'revoked'; connection.version += 1
                invalidate_api(db, user, connection.installation_id, 'OAuth 刷新结果不可核验，请重新授权', connection.capability_id)
                audit(db, user, 'plugin_oauth_refresh_failed', connection.id)
        raise HTTPException(409, 'OAuth 刷新未完成或授权已变化；业务请求未重发，请重新授权') from None


def ensure_job(job):
    with SessionLocal() as db:
        row = db.get(PluginInvocation, job.payload['invocation_id'])
        if not row or row.status not in ('queued', 'running'): return
        fixed = row.config.get('api') or row.config.get('mcp') or {}
        if fixed.get('dispatch') != 'not_sent': return
        connection = db.get(PluginConnection, fixed.get('connection_id'))
        if not connection or not grant_for(db, connection): return
        if connection.version != fixed.get('connection_version'): return
        user = db.get(User, row.user_id)
        _, _, entry = api_capability(db, user, connection.installation_id, connection.capability_id)
        if entry['auth'] != 'oauth2': return
        identity = (row.user_id, row.workspace_id, connection.id)
    ensure_access(*identity)


def ensure_request(db, user, connection_id, version):
    # Authenticated request context has made no changes before this call.
    connection = owned(db, PluginConnection, connection_id, user)
    if connection.version != version: raise HTTPException(409, '连接已变化，请重新加载')
    if not grant_for(db, connection): return
    _, _, entry = api_capability(db, user, connection.installation_id, connection.capability_id)
    if entry['auth'] != 'oauth2': return
    identity = (user.id, user.workspace_id, connection.id)
    db.commit()
    ensure_access(*identity)
    db.expire_all()


def sweep():
    with SessionLocal.begin() as db:
        for user in db.scalars(select(User)):
            owner_lock(db, user)
            for flow in db.scalars(select(OAuthFlow).where(OAuthFlow.user_id == user.id,
                    OAuthFlow.status.in_(['pending', 'exchanging']), OAuthFlow.expires_at <= now())):
                flow.status, flow.encrypted_config = 'expired', ''
            for grant in db.scalars(select(OAuthGrant).where(OAuthGrant.user_id == user.id,
                    OAuthGrant.status == 'refreshing', OAuthGrant.deadline <= now())):
                connection = db.get(PluginConnection, grant.connection_id)
                revoke_local(db, connection); grant.status = 'reauthorize'
                connection.status, connection.encrypted_secret = 'revoked', ''; connection.version += 1
                invalidate_api(db, user, connection.installation_id, 'OAuth 刷新被中断，请重新授权', connection.capability_id)


def revoke_remote(db, user, connection, entry):
    grant = grant_for(db, connection)
    config = json.loads(decrypt(grant.encrypted_config)) if grant and grant.encrypted_config else {}
    access = decrypt(connection.encrypted_secret) if connection.encrypted_secret else ''
    token = config.get('refresh_token') or access
    revoke_local(db, connection)
    connection.status, connection.encrypted_secret, connection.checked_at = 'revoked', '', None
    connection.version += 1
    invalidate_api(db, user, connection.installation_id, 'OAuth 账号连接已撤销', connection.capability_id)
    audit(db, user, 'plugin_connection_revoke', connection.id)
    if not grant: return
    grant.remote_status = 'in_flight' if entry['oauth']['revocation_url'] and token else 'not_supported'
    grant.deadline = now()+60 if grant.remote_status == 'in_flight' else 0
    version, identity, allow_private = connection.version, connection.id, connection.allow_private
    db.commit()  # Local revocation is effective even if the upstream never responds.
    status = grant.remote_status
    if status == 'in_flight':
        try:
            token_request(entry['oauth'], config, {'token':token,
                'token_type_hint':'refresh_token' if config.get('refresh_token') else 'access_token'}, allow_private, revocation=True)
            status = 'accepted'
        except Exception: status = 'unknown'
    db.expire_all(); owner_lock(db, user)
    connection = owned(db, PluginConnection, identity, user, True)
    grant = grant_for(db, connection)
    if connection.version == version and grant and grant.status == 'revoked':
        grant.remote_status, grant.deadline = status, 0
    # "accepted" only means RFC7009 response; provider/account sessions may still exist.
