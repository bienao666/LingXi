"""Read a provider's declared model IDs without trusting its capability claims."""
import json
import time
from urllib.parse import urlsplit, urlunsplit
from fastapi import HTTPException
from pydantic import BaseModel
from .gateway import client_for
from .providers import protocol_for
from .protocols import native_name


class DiscoveredModel(BaseModel):
    id: str
    purposes: list[str] | None = None


class ProviderModelCatalog(BaseModel):
    provider_id: str
    provider_version: int
    models: list[DiscoveredModel]


def catalog_urls(base_url):
    """Build the catalog endpoint from the configured API root exactly once.

    A configured ``/v1`` root maps to ``/v1/models``. We never probe the host
    root first or invent another version segment, because an unconfigured
    probe can fail authentication or rate limiting before the real endpoint.
    """
    url = urlsplit(base_url)
    path = url.path.rstrip('/')
    return [urlunsplit((url.scheme, url.netloc, path + '/models', '', ''))]


def discover_models(provider):
    if not provider.enabled:
        raise HTTPException(409, '此服务连接已停用，请启用后获取模型')
    if protocol_for(provider) != 'openai':
        return native_catalog(provider)
    started = time.monotonic()
    # Reuse credential handling, DNS pinning, private-network policy and no redirects.
    urls = catalog_urls(provider.base_url)
    received = 0
    with client_for(provider) as client:
        for url in urls:
            remaining = 15 - (time.monotonic() - started)
            if remaining <= 0:
                raise HTTPException(502, '模型目录读取超时，请检查服务连接')
            with client.stream('GET', url, timeout=remaining) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    received += len(chunk)
                    if received > 2 * 1024 * 1024 or time.monotonic() - started > 15:
                        raise HTTPException(502, '模型目录过大或读取超时，请检查服务连接')
            try:
                data = json.loads(content)
            except ValueError:
                raise HTTPException(502, '模型目录接口未返回 JSON，可能返回了网站页面，请检查服务连接地址与 /models 接口') from None
            break
    try:
        rows = data['data']
        if not isinstance(rows, list) or len(rows) > 5000 or data.get('has_more'):
            raise ValueError('Unsupported catalog')
        names = set()
        for row in rows:
            name = row['id']
            if not isinstance(name, str) or not name.strip() or len(name) > 150 or any(ord(c) < 32 for c in name):
                raise ValueError('Invalid model ID')
            names.add(name)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, '服务未返回兼容的完整模型目录，请检查服务连接地址与 /models 接口') from None
    return ProviderModelCatalog(provider_id=provider.id, provider_version=provider.version,
                                models=[DiscoveredModel(id=name) for name in sorted(names)])


def native_catalog(provider):
    kind = protocol_for(provider)
    started, received, names, seen, cursor = time.monotonic(), 0, {}, set(), None
    with client_for(provider) as client:
        for _ in range(50):
            remaining = 15 - (time.monotonic() - started)
            if remaining <= 0:
                raise HTTPException(502, '原生模型目录读取超时')
            params = {'limit': 1000} if kind == 'anthropic' else {'pageSize': 1000}
            if cursor:
                params['after_id' if kind == 'anthropic' else 'pageToken'] = cursor
            with client.stream('GET', 'models', params=params, timeout=remaining) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    received += len(chunk);raw.extend(chunk)
                    if received > 2 * 1024**2 or time.monotonic() - started > 15:
                        raise HTTPException(502, '原生模型目录过大或读取超时')
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or (kind == 'anthropic' and type(data.get('has_more', False)) is not bool):
                    raise ValueError()
                rows = data['data' if kind == 'anthropic' else 'models']
                if not isinstance(rows, list) or len(rows) > 5000:
                    raise ValueError()
                for row in rows:
                    name = row['id' if kind == 'anthropic' else 'name']
                    if not isinstance(name, str):
                        raise ValueError()
                    try:
                        native_name(name)
                    except HTTPException:
                        raise ValueError('Invalid native model ID') from None
                    purposes = ['chat', 'extraction']
                    if kind == 'gemini':
                        methods = row.get('supportedGenerationMethods', [])
                        if not isinstance(methods, list) or not all(isinstance(m, str) for m in methods):
                            raise ValueError()
                        purposes = (['chat', 'extraction'] if 'generateContent' in methods else []) + (['embedding'] if 'embedContent' in methods else [])
                    item = DiscoveredModel(id=name, purposes=purposes)
                    if name in names and names[name] != item:
                        raise ValueError('Conflicting model declarations')
                    names[name] = item
                if len(names) > 5000:
                    raise ValueError()
                cursor = data.get('last_id') if kind == 'anthropic' and data.get('has_more') else data.get('nextPageToken') if kind == 'gemini' else None
                if kind == 'anthropic' and data.get('has_more') and not cursor:
                    raise ValueError()
                if not cursor:
                    return ProviderModelCatalog(provider_id=provider.id, provider_version=provider.version, models=[names[k] for k in sorted(names)])
                if not isinstance(cursor, str) or len(cursor) > 4000 or cursor in seen or not rows:
                    raise ValueError()
                seen.add(cursor)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(502, '原生模型目录格式或分页无效，不会使用不完整目录') from None
    raise HTTPException(502, '原生模型目录超过分页预算，不会使用不完整目录')
