"""Unified transport. No redirects, silent fallback, or automatic retries."""
import json
import base64
import struct
import math
import time
import httpx
from fastapi import HTTPException
from sqlalchemy import select
from .config import settings
from .db import SessionLocal, now
from .models import Model, ModelCall, Policy, Provider
from .security import decrypt, safe_error, validate_endpoint
from .network import outbound_client
from .providers import bind_protocol, protocol_for, require_capability
from . import protocols
from .model_budgets import client as budget_client, link as link_dispatches


def resolve_model(db, purpose: str, model_id: str | None = None, verified: bool = True):
    policy = db.get(Policy, purpose) if model_id is None else None
    model = db.get(Model, model_id or (policy.model_id if policy else ''))
    provider = db.get(Provider, model.provider_id) if model else None
    if not model or not provider or not provider.enabled:
        raise HTTPException(409, f'请先配置 {purpose} 用途的可用模型')
    if verified and (model.status != 'verified' or model.verified_provider_version != provider.version):
        raise HTTPException(409, '模型尚未通过连接验证')
    bind_protocol(provider, db)
    require_capability(protocol_for(provider), model.purpose)
    return model, provider, policy.version if policy else 0


def client_for(provider: Provider):
    kind = protocol_for(provider)
    key = decrypt(provider.encrypted_key)
    headers = ({'x-api-key': key, 'anthropic-version': '2023-06-01'} if kind == 'anthropic' else
               {'x-goog-api-key': key} if kind == 'gemini' else {'Authorization': 'Bearer ' + key})
    return outbound_client(provider.base_url, allow_private=provider.allow_private,
                           headers=headers,
                           timeout=httpx.Timeout(settings().provider_timeout, connect=10))


def call_config(model, provider, purpose):
    purpose = purpose.removeprefix('memory_')
    with SessionLocal() as db:
        policy = db.get(Policy, purpose)
        from .usage_costs import snapshot
        return {'model': model.name, 'provider_id': provider.id, 'provider_version': provider.version,
                '_price_snapshot': snapshot(db, model, provider),
                'provider_protocol': protocol_for(provider),
                'policy_version': policy.version if policy and policy.model_id == model.id else None,
                'prompt_version': 'extraction-v1' if purpose == 'extraction' else ('lingxi-companion-v1' if purpose == 'chat' else None)}


def record_call(model_id, purpose, started, status, usage=None, error=None, run_id=None, config=None):
    with SessionLocal.begin() as db:
        from .usage_costs import cost, attribution
        config = dict(config or {})
        dispatches = config.pop('_dispatch_ids', [])
        price = config.pop('_price_snapshot', None)
        model = db.get(Model, model_id)
        provider = db.get(Provider, config.get('provider_id', '')) if not model else None
        workspace = model.workspace_id if model else provider.workspace_id if provider else 'personal'
        billing = cost(price, usage) | {'attribution': attribution(db, model_id, run_id, config, workspace)}
        call = ModelCall(model_id=model_id, purpose=purpose, duration_ms=int((time.monotonic()-started)*1000),
                         status=status, usage=usage or {}, error=error, run_id=run_id,
                         config=config, billing=billing, workspace_id=workspace)
        db.add(call); db.flush()
        link_dispatches(db, call, dispatches)


def complete(model: Model, provider: Provider, messages: list, purpose='extraction', config=None, max_tokens=2000) -> str:
    if type(max_tokens) is not int or not 1 <= max_tokens <= 2000:
        raise ValueError('Invalid completion output budget')
    started = time.monotonic()
    base = call_config(model, provider, purpose)
    config = base | (config or {}) | {'_price_snapshot': base['_price_snapshot']}
    usage = {}
    try:
        with budget_client(provider, client_for, config) as client:
            data = compatible_json(client, provider, 'chat/completions',
                                   {'model': model.name, 'messages': messages, 'stream': False, 'max_tokens': max_tokens})
            usage = data.get('usage') or {}
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise HTTPException(502, '模型未返回有效文字，请检查模型类型和服务协议')
        record_call(model.id, purpose, started, 'completed', data.get('usage'), run_id=config.get('run_id'), config=config)
        return content
    except Exception as exc:
        record_call(model.id, purpose, started, 'failed', usage, error=safe_error(exc), run_id=config.get('run_id'), config=config)
        raise


def bounded_json(client, route, payload):
    with client.stream('POST', route, json=payload) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 8 * 1024**2:
                raise HTTPException(502, '原生模型响应超过处理预算')
        return json.loads(raw)


def compatible_json(client, provider, route, body):
    """Translate only explicitly supported requests for the private memory gateway."""
    kind = protocol_for(provider)
    if kind == 'openai':
        response = client.post(route, json=body)
        response.raise_for_status()
        return response.json()
    purpose = {'chat/completions': 'extraction', 'embeddings': 'embedding', 'rerank': 'rerank'}.get(route)
    require_capability(kind, purpose)
    if route == 'chat/completions':
        if 'max_tokens' in body and 'max_completion_tokens' in body:
            raise HTTPException(422, '输出长度参数不能重复指定')
        options = {k: v for k, v in body.items() if k not in ('model', 'messages', 'max_tokens', 'max_completion_tokens', 'stream')}
        target, payload = protocols.chat_request(kind, body['model'], body.get('messages', []),
                                                max_tokens=body.get('max_completion_tokens', body.get('max_tokens', 2000)), options=options)
        return protocols.completion(kind, bounded_json(client, target, payload))
    if set(body) - {'model', 'input', 'dimensions', 'encoding_format'} or body.get('encoding_format', 'float') not in ('float', 'base64'):
        raise HTTPException(422, '此嵌入请求包含尚未适配的原生选项')
    inputs = body.get('input')
    inputs = [inputs] if isinstance(inputs, str) else inputs
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 100 or not all(isinstance(s, str) and s for s in inputs):
        raise HTTPException(422, '原生嵌入仅支持每批 1–100 条非空文字')
    dimensions = body.get('dimensions', settings().memory_embedding_dimensions)
    if dimensions != settings().memory_embedding_dimensions:
        raise HTTPException(422, '嵌入维度与部署配置不一致，不允许混用索引')
    model = 'models/' + protocols.native_name(body['model'])
    requests = [{'model': model, 'content': {'parts': [{'text': text}]},
                 'embedContentConfig': {'outputDimensionality': dimensions, 'autoTruncate': False}} for text in inputs]
    data = bounded_json(client, model + ':batchEmbedContents', {'requests': requests})
    rows = data.get('embeddings') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != len(inputs):
        raise HTTPException(502, '原生嵌入返回数量不匹配')
    result = []
    for i, row in enumerate(rows):
        vector = row.get('values') if isinstance(row, dict) else None
        validate_capability('embedding', {'data': [{'embedding': vector}]})
        if body.get('encoding_format') == 'base64':
            try:
                vector = base64.b64encode(struct.pack('<'+'f'*len(vector), *vector)).decode('ascii')
            except (OverflowError, struct.error):
                raise HTTPException(502, '原生嵌入数值超出 float32 范围') from None
        result.append({'index': i, 'embedding': vector, 'object': 'embedding'})
    count = data.get('usageMetadata', {}).get('promptTokenCount') if isinstance(data.get('usageMetadata'), dict) else None
    # Omit unknown usage: an empty object becomes an SDK Usage with None fields,
    # which Hindsight cannot add. Never manufacture a zero token count.
    tokens = {'usage': {'prompt_tokens': count, 'total_tokens': count}} if type(count) is int and count >= 0 else {}
    return {'object': 'list', 'model': body['model'], 'data': result, **tokens}


def stream_chunks(response, check):
    """Use the same protocol checks for validation and production generation."""
    response.raise_for_status()
    terminal = False
    has_text = False
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
            raise HTTPException(502, '模型流返回错误，请查看提供商状态后重试')
        if data.get('usage'):
            yield None, data['usage']
        for choice in data.get('choices', []):
            text = choice.get('delta', {}).get('content')
            if isinstance(text, str) and text:
                has_text = has_text or bool(text.strip())
                yield text, None
            if choice.get('finish_reason'):
                terminal = True
    if not terminal:
        raise HTTPException(502, '流式回复提前断开或协议不兼容，请检查服务是否支持流式 Chat Completions')
    if not has_text:
        raise HTTPException(502, '模型流没有有效文字，请检查模型类型和服务协议')


def stream(model, provider, messages, run_id, check, config=None):
    started = time.monotonic()
    base = call_config(model, provider, 'chat')
    config = base | (config or {}) | {'_price_snapshot': base['_price_snapshot']}
    usage = {}
    status = 'failed'
    error = None
    try:
        # Fence invalidated sources before opening the upstream request, as well as per chunk.
        check()
        kind = protocol_for(provider)
        route, payload = protocols.chat_request(kind, model.name, messages, stream=True, max_tokens=2048)
        with budget_client(provider, client_for, config, run_id) as client:
            with client.stream('POST', route, json=payload) as response:
                chunks = stream_chunks(response, check) if kind == 'openai' else protocols.stream_chunks(kind, response, check)
                for text, tokens in chunks:
                    usage = tokens or usage
                    if text is not None:
                        yield text
        status = 'completed'
    except GeneratorExit:
        status = 'cancelled'
        raise
    except Exception as exc:
        error = safe_error(exc)
        raise
    finally:
        record_call(model.id, 'chat', started, status, usage, error, run_id,
                    config=(config or {}) | {'provider_protocol': protocol_for(provider)})


def test_model(db, model: Model):
    _, provider, _ = resolve_model(db, model.purpose, model.id, verified=False)
    started = time.monotonic()
    config = call_config(model, provider, model.purpose) | {'validation': True, 'probe_version': 'capabilities-v2'}
    usage = {}
    try:
        with budget_client(provider, client_for, config) as client:
            if model.purpose == 'chat':
                kind = protocol_for(provider)
                route, payload = protocols.chat_request(kind, model.name, [{'role': 'user', 'content': '请用中文打个招呼。'}], stream=True, max_tokens=512)
                with client.stream('POST', route, json=payload) as response:
                    chunks = stream_chunks(response, lambda: None) if kind == 'openai' else protocols.stream_chunks(kind, response, lambda: None)
                    for _, tokens in chunks:
                        usage = tokens or usage
            else:
                route, payload = validation_request(model)
                data = compatible_json(client, provider, route, payload)
                usage = data.get('usage') or {}
                validate_capability(model.purpose, data)
        record_call(model.id, model.purpose, started, 'completed', usage, config=config)
    except Exception as exc:
        record_call(model.id, model.purpose, started, 'failed', usage, safe_error(exc), config=config)
        raise
    model.status = 'verified'
    model.verified_at = now()
    model.verified_provider_version = provider.version


PROBE_SOURCE = '我长期喜欢喝茶。'


def validation_request(model):
    if model.purpose == 'extraction':
        prompt = ('只分析用户原文，输出 JSON 对象 {"items": [...]}，不要代码围栏。'
                  '提取明确的长期偏好。每项必须包含 content、quote（均为原文完整引用）、'
                  'subject（self）、category（preferences）、evidence（explicit）、sensitive（boolean）。')
        return 'chat/completions', {'model': model.name, 'stream': False, 'max_tokens': 512,
                'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': PROBE_SOURCE}]}
    if model.purpose == 'embedding':
        return 'embeddings', {'model': model.name, 'input': ['中文检索验证']}
    if model.purpose == 'rerank':
        return 'rerank', {'model': model.name, 'query': '喜欢喝茶', 'documents': ['我喜欢喝茶。', '今天天气晴。']}
    raise HTTPException(422, '此用途尚未提供能力验证')


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_capability(purpose, data):
    if purpose == 'extraction':
        try:
            content = data['choices'][0]['message']['content'].strip()
            if content.startswith('```json\n') and content.endswith('```'):
                content = content[8:-3].strip()
            items = json.loads(content)['items']
            if not isinstance(items, list) or not 1 <= len(items) <= 5:
                raise ValueError('Missing extraction items')
            for item in items:
                quote = item['quote']
                if (not isinstance(quote, str) or not quote.strip() or quote not in PROBE_SOURCE or
                        item['content'] != quote or item['subject'] != 'self' or
                        item['category'] != 'preferences' or item['evidence'] != 'explicit' or
                        item['sensitive'] is not False):
                    raise ValueError('Invalid extraction evidence')
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise HTTPException(502, '记忆抽取验证未通过：需返回含原文证据的结构化 JSON，请更换模型或检查服务设置') from None
    elif purpose == 'embedding':
        rows = data.get('data')
        vector = rows[0].get('embedding') if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
        if not isinstance(vector, list) or len(vector) != settings().memory_embedding_dimensions:
            raise HTTPException(422, f'嵌入维度须为部署配置的 {settings().memory_embedding_dimensions}；修改维度须重建记忆索引')
        if not all(finite_number(v) for v in vector) or not any(v != 0 for v in vector):
            raise HTTPException(502, '嵌入响应须为有限数值组成的非零向量，请检查嵌入接口')
    elif purpose == 'rerank':
        results = data.get('results') or data.get('data')
        indices = set()
        if not isinstance(results, list) or not results:
            raise HTTPException(502, '重排响应缺少排序结果，请使用兼容 /rerank 的服务')
        for row in results:
            index = row.get('index') if isinstance(row, dict) else None
            if type(index) is not int or index not in (0, 1) or index in indices or not finite_number(row.get('relevance_score')):
                raise HTTPException(502, '重排结果须包含不重复的文档 index 和有效 relevance_score，请检查服务协议')
            indices.add(index)
