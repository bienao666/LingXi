import hashlib
import ipaddress
import secrets
import socket
import httpx
from urllib.parse import urlsplit
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from sqlalchemy import select
from .config import settings
from .db import now
from .models import LoginSession, User

hasher = PasswordHasher()


def encrypt(value: str) -> str:
    return Fernet(settings().encryption_key.encode()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return Fernet(settings().encryption_key.encode()).decrypt(value.encode()).decode()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(request: Request, db):
    token = request.cookies.get('lingxi_session', '')
    session = db.scalar(select(LoginSession).where(LoginSession.token_hash == token_hash(token),
                                                  LoginSession.expires_at > now()))
    if not session:
        raise HTTPException(401, '请先登录')
    user = db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, '登录已失效')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session.csrf):
            raise HTTPException(403, '请求校验失败，请刷新后重试')
        origin = request.headers.get('origin')
        if origin and origin != settings().public_origin:
            raise HTTPException(403, '请求来源不被允许')
    return user, session


def validate_endpoint(url: str, allow_private: bool):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, '服务地址必须是无凭据、查询参数的 HTTP(S) 地址')
    if not allow_private and parsed.scheme != 'https':
        raise HTTPException(422, '公网模型服务必须使用 HTTPS')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        if not allow_private and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise HTTPException(422, '此地址涉及内网，请明确授权内网连接')
    except socket.gaierror:
        raise HTTPException(422, '模型服务域名无法解析') from None
    return addresses[0][4][0]


def safe_error(exc: Exception) -> str:
    # Never surface upstream response bodies, prompts, URLs, or credential-bearing exception strings.
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        hint = {401: '密钥无效或已过期，请更新服务连接',
                403: '服务拒绝访问，请检查账号权限与模型授权',
                404: '接口或模型不存在，请检查接口根地址及模型标识',
                429: '请求受限或额度不足，请检查服务额度后重试'}.get(status, '请检查服务状态与协议兼容性后重试')
        return f'模型服务返回 HTTP {status}；{hint}'
    if isinstance(exc, httpx.TimeoutException):
        return '请求超时，请检查代理、网络与上游服务负载后重试'
    if isinstance(exc, httpx.ProxyError):
        return '代理连接失败，请检查代理地址与服务访问规则'
    if isinstance(exc, httpx.ConnectError):
        return '无法连接上游服务或代理，请检查地址、容器网络与证书'
    return f'服务请求失败（{type(exc).__name__}），请检查连接与服务状态'
