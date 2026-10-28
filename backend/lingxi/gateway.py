"""OpenAI-compatible transport. No redirects, silent fallback, or automatic retries."""
import json
import time
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from .config import settings
from .db import SessionLocal, now
from .models import Model, ModelCall, Policy, Provider
from .security import decrypt, safe_error, validate_endpoint


def resolve_model(db, purpose: str, model_id: str | None = None, verified: bool = True):
    policy = db.get(Policy, purpose) if model_id is None else None
    model = db.get(Model, model_id or (policy.model_id if policy else ''))
    provider = db.get(Provider, model.provider_id) if model else None
    if not model or not provider or not provider.enabled:
        raise HTTPException(409, f'请先配置 {purpose} 用途的可用模型')
    if verified and (model.status != 'verified' or model.verified_provider_version != provider.version):
        raise HTTPException(409, '模型尚未通过连接验证')
    return model, provider, policy.version if policy else 0


def client_for(provider: Provider):
    address = validate_endpoint(provider.base_url, provider.allow_private)
    class PinnedTransport(httpx.HTTPTransport):
        def handle_request(self, request):
            # Pin the checked address. A second DNS lookup must not bypass the private-address policy.
            hostname = request.url.host
            request.headers['Host'] = request.url.netloc.decode()
            request.extensions['sni_hostname'] = hostname.encode()
            request.url = request.url.copy_with(host=address)
            return super().handle_request(request)
    return httpx.Client(base_url=provider.base_url.rstrip('/') + '/',
                        headers={'Authorization': 'Bearer ' + decrypt(provider.encrypted_key)},
                        timeout=httpx.Timeout(settings().provider_timeout, connect=10),
                        follow_redirects=False, trust_env=False, transport=PinnedTransport())


def call_config(model, provider, purpose):
    purpose = purpose.removeprefix('memory_')
    with SessionLocal() as db:
        policy = db.get(Policy, purpose)
        return {'model': model.name, 'provider_version': provider.version,
                'policy_version': policy.version if policy and policy.model_id == model.id else None,
                'prompt_version': 'extraction-v1' if purpose == 'extraction' else ('lingxi-companion-v1' if purpose == 'chat' else None)}


def record_call(model_id, purpose, started, status, usage=None, error=None, run_id=None, config=None):
    with SessionLocal.begin() as db:
        db.add(ModelCall(model_id=model_id, purpose=purpose, duration_ms=int((time.monotonic()-started)*1000),
                         status=status, usage=usage or {}, error=error, run_id=run_id,
                         config=config or {}))


def complete(model: Model, provider: Provider, messages: list, purpose='extraction') -> str:
    started = time.monotonic()
    config = call_config(model, provider, purpose)
    try:
        with client_for(provider) as client:
            response = client.post('chat/completions', json={'model': model.name, 'messages': messages,
                                    'stream': False, 'max_tokens': 2000})
            response.raise_for_status()
            data = response.json()
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise ValueError('Invalid completion')
        record_call(model.id, purpose, started, 'completed', data.get('usage'), config=config)
        return content
    except Exception as exc:
        record_call(model.id, purpose, started, 'failed', error=safe_error(exc), config=config)
        raise


def stream(model, provider, messages, run_id, check, config=None):
    started = time.monotonic()
    usage = {}
    terminal = False
    status = 'failed'
    error = None
    try:
        with client_for(provider) as client:
            with client.stream('POST', 'chat/completions', json={'model': model.name, 'messages': messages,
                               'stream': True, 'max_tokens': 2048}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    check()
                    if not line.startswith('data:'):
                        continue
                    raw = line[5:].strip()
                    if raw == '[DONE]':
                        terminal = True
                        break
                    data = json.loads(raw)
                    if data.get('error'):
                        raise ValueError('Upstream error')
                    usage = data.get('usage') or usage
                    for choice in data.get('choices', []):
                        text = choice.get('delta', {}).get('content')
                        if isinstance(text, str) and text:
                            yield text
                        if choice.get('finish_reason'):
                            terminal = True
                if not terminal:
                    raise ConnectionError('Stream ended without completion marker')
        status = 'completed'
    except GeneratorExit:
        status = 'cancelled'
        raise
    except Exception as exc:
        error = safe_error(exc)
        raise
    finally:
        record_call(model.id, 'chat', started, status, usage, error, run_id, config=config)


def test_model(db, model: Model):
    _, provider, _ = resolve_model(db, model.purpose, model.id, verified=False)
    if model.purpose in ('chat', 'extraction'):
        complete(model, provider, [{'role': 'user', 'content': '请只回复 OK'}], purpose='validation')
    else:
        with client_for(provider) as client:
            route = 'embeddings' if model.purpose == 'embedding' else 'rerank'
            payload = {'model': model.name, 'input': ['中文检索验证']} if route == 'embeddings' else {
                'model': model.name, 'query': '中文检索', 'documents': ['中文检索验证']}
            response = client.post(route, json=payload)
            response.raise_for_status()
            data = response.json()
            if route == 'embeddings' and not data.get('data', [{}])[0].get('embedding'):
                raise ValueError('Invalid embedding')
            if route == 'embeddings' and len(data['data'][0]['embedding']) != settings().memory_embedding_dimensions:
                raise HTTPException(422, f'嵌入维度须为部署配置的 {settings().memory_embedding_dimensions}；修改维度须重建记忆索引')
            if route == 'rerank' and not (data.get('results') or data.get('data')):
                raise ValueError('Invalid rerank')
    model.status = 'verified'
    model.verified_at = now()
    model.verified_provider_version = provider.version
