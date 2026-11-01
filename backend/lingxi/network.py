"""Authoritative system egress policy, shared by Core and workers."""
import ipaddress
import json
from urllib.parse import urlsplit
import httpcore
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now
from .security import decrypt, encrypt, validate_endpoint

INTERNAL_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', 'core', 'db', 'worker', 'hindsight', 'web', 'memory-inference'})


class SystemProxy(Base):
    __tablename__ = 'system_proxy'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_config: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class ProxyInput(BaseModel):
    enabled: bool = False
    http_proxy: str = Field(default='', max_length=2000)
    https_proxy: str = Field(default='', max_length=2000)
    all_proxy: str = Field(default='', max_length=2000)
    no_proxy: list[str] = Field(default_factory=list, max_length=100)
    version: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def validate_settings(self):
        for key in ('http_proxy', 'https_proxy', 'all_proxy'):
            value = getattr(self, key).strip()
            setattr(self, key, value)
            if not value:
                continue
            try:
                url = urlsplit(value)
                valid = (url.scheme in ('http', 'https', 'socks5', 'socks5h') and url.hostname and
                         url.port and url.path in ('', '/') and not url.query and not url.fragment and
                         not url.username and not url.password and not any(c.isspace() for c in value))
            except ValueError:
                valid = False
            if not valid:
                raise HTTPException(422, '代理须为含端口的 HTTP(S) 或 SOCKS5 地址，暂不支持内嵌账号密码')
        if self.enabled and not ((self.http_proxy or self.all_proxy) and (self.https_proxy or self.all_proxy)):
            raise HTTPException(422, '启用代理须覆盖 HTTP 和 HTTPS，或填写 ALL 代理作为通用后备')
        self.no_proxy = list(dict.fromkeys(rule.strip().lower().rstrip('.') for rule in self.no_proxy if rule.strip()))
        for rule in self.no_proxy:
            if len(rule) > 253 or any(c.isspace() for c in rule) or '://' in rule:
                raise HTTPException(422, '直连规则仅支持主机名、域名后缀、IP、CIDR 或 *')
            try:
                ipaddress.ip_network(rule, strict=False)
            except ValueError:
                if rule != '*' and (not rule.lstrip('.').replace('-', '').replace('.', '').isalnum() or ':' in rule or '/' in rule):
                    raise HTTPException(422, '直连规则仅支持主机名、域名后缀、IP、CIDR 或 *')
        return self


class ProxyView(ProxyInput):
    protected_no_proxy: list[str] = Field(default_factory=lambda: sorted(INTERNAL_HOSTS))


class ProxyTestInput(ProxyInput):
    test_url: str = Field(default='https://example.com', min_length=1, max_length=1000)


class ProxyTestResult(BaseModel):
    ok: bool
    status_code: int
    duration_ms: int
    route: str


def config_view(row):
    if not row:
        return ProxyView()
    return ProxyView(**json.loads(decrypt(row.encrypted_config)), enabled=row.enabled, version=row.version)


def load_proxy():
    with SessionLocal() as db:
        return config_view(db.get(SystemProxy, 'system'))


def save_proxy(db, body):
    row = db.query(SystemProxy).filter_by(id='system').with_for_update().one_or_none()
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '代理配置已变化，请重新加载后再保存')
    values = body.model_dump(include={'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'})
    if row is None:
        row = SystemProxy(id='system', enabled=body.enabled, encrypted_config=encrypt(json.dumps(values)))
        db.add(row)
    else:
        row.enabled, row.encrypted_config = body.enabled, encrypt(json.dumps(values))
        row.version += 1
    row.updated_at = now()
    db.flush()
    return config_view(row)


def proxy_route(url, address, config):
    target = urlsplit(url)
    host = target.hostname.lower().rstrip('.')
    if host in INTERNAL_HOSTS or ipaddress.ip_address(address).is_loopback:
        return None, 'internal_direct'
    for rule in config.no_proxy:
        if rule == '*' or host == rule.lstrip('.') or (rule.startswith('.') and host.endswith(rule)):
            return None, 'no_proxy'
        try:
            if ipaddress.ip_address(address) in ipaddress.ip_network(rule, strict=False):
                return None, 'no_proxy'
        except ValueError:
            pass
    if not config.enabled:
        return None, 'direct'
    key = target.scheme + '_proxy'
    value = getattr(config, key, '')
    if not value:
        key, value = 'all_proxy', config.all_proxy
    if not value:
        raise HTTPException(409, '代理未覆盖当前请求，请补全代理配置')
    return value, key


class _TLSStream(httpcore.NetworkStream):
    def __init__(self, stream, hostname, proxy_tls=False):
        self.stream, self.hostname, self.proxy_tls = stream, hostname, proxy_tls

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout)

    def close(self):
        return self.stream.close()

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # HTTPS proxies have their own TLS hop; never substitute its identity.
        hostname = self.proxy_tls or self.hostname
        wrapped = self.stream.start_tls(ssl_context, hostname, timeout)
        return _TLSStream(wrapped, self.hostname)


class _TLSBackend(httpcore.SyncBackend):
    def __init__(self, hostname, proxy_tls=False):
        self.hostname, self.proxy_tls = hostname, proxy_tls

    def connect_tcp(self, *args, **kwargs):
        return _TLSStream(super().connect_tcp(*args, **kwargs), self.hostname, self.proxy_tls)


class PinnedTransport(httpx.HTTPTransport):
    def __init__(self, address, hostname, proxy=None, **kwargs):
        super().__init__(proxy=proxy, trust_env=False, **kwargs)
        self.address, self.hostname = address, hostname
        # httpcore 1.0.9's HTTP CONNECT path ignores sni_hostname. Keep the checked
        # IP for CONNECT while validating origin TLS against its original DNS name.
        # This adapter relies on the pinned HTTPX/httpcore versions and proxy TLS tests.
        proxy_url = str(proxy.url) if isinstance(proxy, httpx.Proxy) else proxy
        proxy_tls = urlsplit(proxy_url).hostname if proxy_url and urlsplit(proxy_url).scheme == 'https' else None
        self._pool._network_backend = _TLSBackend(hostname, proxy_tls)

    def handle_request(self, request):
        if request.url.host != self.hostname:
            raise HTTPException(422, '网络客户端不允许切换目标主机')
        request.headers['Host'] = request.url.netloc.decode()
        request.extensions['sni_hostname'] = self.hostname.encode()
        request.url = request.url.copy_with(host=self.address)
        return super().handle_request(request)


def outbound_client(url, *, allow_private=False, headers=None, timeout=45, config=None):
    address = validate_endpoint(url, allow_private)
    policy = config if config is not None else load_proxy()
    proxy, _ = proxy_route(url, address, policy)
    transport = PinnedTransport(address, httpx.URL(url).host, proxy=proxy)
    return httpx.Client(base_url=url.rstrip('/')+'/', headers=headers, timeout=timeout,
                        follow_redirects=False, trust_env=False, transport=transport)
