"""Native message translation. No tools, fallback, remote media fetch or reasoning exposure."""
import base64
import json
import re
from fastapi import HTTPException


def failure(message='模型返回不兼容或不完整的原生协议响应'):
    raise HTTPException(502, message)


def native_name(name):
    if not isinstance(name, str):
        raise HTTPException(422, '模型标识须为文字')
    name = name.removeprefix('models/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,149}', name):
        raise HTTPException(422, '原生模型标识仅接受 models/ 前缀及字母、数字、点、下划线和连字符')
    return name


def content_parts(value, kind):
    if isinstance(value, str):
        return [{'type': 'text', 'text': value}] if kind == 'anthropic' else [{'text': value}]
    if not isinstance(value, list) or not value:
        raise HTTPException(422, '原生协议仅接受文字和已授权图片内容')
    result = []
    for block in value:
        if not isinstance(block, dict):
            raise HTTPException(422, '无效的消息内容块')
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            result.extend(content_parts(block['text'], kind))
        elif block.get('type') == 'image_url':
            image = block.get('image_url')
            value = image.get('url', '') if isinstance(image, dict) else ''
            if not isinstance(value, str):
                raise HTTPException(422, '图片地址格式无效')
            match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)', value)
            if not match or len(value) > 24 * 1024**2:
                raise HTTPException(422, '图片须来自已授权附件，不接受远程图片地址')
            try:
                base64.b64decode(match[2], validate=True)
            except ValueError:
                raise HTTPException(422, '图片编码无效') from None
            if kind == 'anthropic':
                result.append({'type': 'image', 'source': {'type': 'base64', 'media_type': match[1], 'data': match[2]}})
            else:
                result.append({'inlineData': {'mimeType': match[1], 'data': match[2]}})
        else:
            raise HTTPException(422, '此消息内容尚未接入原生协议，不会静默丢弃')
    return result


def chat_request(kind, name, messages, stream=False, max_tokens=2000, options=None):
    options = dict(options or {})
    if kind == 'openai':
        return 'chat/completions', {'model': name, 'messages': messages, 'stream': stream, 'max_tokens': max_tokens, **options}
    name = native_name(name)
    if kind not in ('anthropic', 'gemini'):
        raise HTTPException(422, '不支持的模型协议')
    if type(max_tokens) is not int or not 1 <= max_tokens <= 1000000:
        raise HTTPException(422, '输出长度须为正整数')
    if set(options) - {'temperature', 'top_p', 'stop', 'response_format'}:
        raise HTTPException(422, '请求包含尚未适配的原生模型选项')
    if not isinstance(messages, list) or not messages:
        raise HTTPException(422, '对话消息不能为空')
    system, turns = [], []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(422, '消息格式无效')
        role, content = message.get('role'), message.get('content')
        if role in ('system', 'developer'):
            if not isinstance(content, str):
                raise HTTPException(422, '原生系统指令须为文字')
            system.append(content)
        elif role in ('user', 'assistant'):
            parts = content_parts(content, kind)
            target_role = role if kind == 'anthropic' else ('model' if role == 'assistant' else 'user')
            field = 'content' if kind == 'anthropic' else 'parts'
            # Native APIs allow consolidated adjacent turns. Preserve ordering.
            if turns and turns[-1]['role'] == target_role:
                turns[-1][field].extend(parts)
            else:
                turns.append({'role': target_role, field: parts})
        else:
            raise HTTPException(422, '此调用链未接入原生工具消息')
    if not turns or turns[0]['role'] != 'user':
        raise HTTPException(422, '原生对话必须包含用户消息并从用户开始')
    fmt = options.pop('response_format', None)
    schema = None
    if fmt is not None:
        if not isinstance(fmt, dict) or fmt.get('type') not in ('json_object', 'json_schema', 'text'):
            raise HTTPException(422, '不支持的输出格式')
        if fmt['type'] == 'json_schema':
            definition = fmt.get('json_schema')
            schema = definition.get('schema') if isinstance(definition, dict) else None
            if not isinstance(schema, dict) or not schema:
                raise HTTPException(422, 'JSON Schema 定义不能为空')
        if fmt['type'] == 'text':
            fmt = None
    if kind == 'anthropic':
        if fmt and schema is None:
            system.append('本次仅输出一个有效的 JSON 对象，不要 Markdown 围栏或额外解释。')
        payload = {'model': name, 'messages': turns, 'max_tokens': max_tokens, 'stream': stream}
        if schema is not None:
            payload['output_config'] = {'format': {'type': 'json_schema', 'schema': schema}}
        if system:
            payload['system'] = '\n\n'.join(system)
        payload.update({k: v for k, v in options.items() if k in ('temperature', 'top_p')})
        if options.get('stop'):
            payload['stop_sequences'] = [options['stop']] if isinstance(options['stop'], str) else options['stop']
        return 'messages', payload
    generation = {'maxOutputTokens': max_tokens}
    for source, target in [('temperature', 'temperature'), ('top_p', 'topP'), ('stop', 'stopSequences')]:
        if source in options:
            value = options[source]
            generation[target] = [value] if source == 'stop' and isinstance(value, str) else value
    if fmt:
        generation['responseMimeType'] = 'application/json'
        if schema is not None:
            generation['responseJsonSchema'] = schema
    payload = {'contents': turns, 'generationConfig': generation}
    if system:
        payload['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system)}]}
    method = 'streamGenerateContent?alt=sse' if stream else 'generateContent'
    return f'models/{native_name(name)}:{method}', payload


def usage(kind, raw):
    raw = raw if isinstance(raw, dict) else {}
    clean = {k: v for k, v in raw.items() if type(v) is int and v >= 0}
    result = {}
    if kind == 'anthropic':
        if 'input_tokens' in clean:
            result['prompt_tokens'] = clean['input_tokens'] + clean.get('cache_read_input_tokens', 0) + clean.get('cache_creation_input_tokens', 0)
        if 'output_tokens' in clean:
            result['completion_tokens'] = clean['output_tokens']
        result.update({k: clean[k] for k in ('cache_read_input_tokens', 'cache_creation_input_tokens') if k in clean})
    else:
        if 'promptTokenCount' in clean:
            result['prompt_tokens'] = clean['promptTokenCount']
        if 'candidatesTokenCount' in clean:
            result['completion_tokens'] = clean['candidatesTokenCount'] + clean.get('thoughtsTokenCount', 0)
        if 'totalTokenCount' in clean:
            result['total_tokens'] = clean['totalTokenCount']
        if 'cachedContentTokenCount' in clean:
            result['prompt_tokens_details'] = {'cached_tokens': clean['cachedContentTokenCount']}
    if 'total_tokens' not in result and all(k in result for k in ('prompt_tokens', 'completion_tokens')):
        result['total_tokens'] = result['prompt_tokens'] + result['completion_tokens']
    return result


def text_parts(parts, kind):
    if not isinstance(parts, list):
        failure()
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            failure()
        if kind == 'anthropic':
            if part.get('type') in ('thinking', 'redacted_thinking'):
                continue
            if part.get('type') != 'text' or not isinstance(part.get('text'), str):
                failure('原生模型返回尚未接入的工具或内容，未执行任何工具')
        else:
            if part.get('thought') is True:
                continue
            if not isinstance(part.get('text'), str):
                failure('原生模型返回尚未接入的工具或内容，未执行任何工具')
        texts.append(part['text'])
    return ''.join(texts)


def finish_reason(kind, reason):
    if reason not in (('end_turn', 'stop_sequence') if kind == 'anthropic' else ('STOP',)):
        failure('原生模型未正常完成（可能达到长度限制、内容拦截或请求工具），请核对结果后重试')


def completion(kind, data):
    if not isinstance(data, dict) or data.get('error'):
        failure()
    if kind == 'anthropic':
        finish_reason(kind, data.get('stop_reason'))
        content = text_parts(data.get('content'), kind)
        tokens = usage(kind, data.get('usage'))
    else:
        candidates = data.get('candidates')
        if not isinstance(candidates, list) or len(candidates) != 1:
            failure('原生模型未返回唯一有效回答，可能被服务拒绝或拦截')
        candidate = candidates[0]
        if not isinstance(candidate, dict) or not isinstance(candidate.get('content'), dict):
            failure()
        finish_reason(kind, candidate.get('finishReason'))
        content = text_parts(candidate.get('content', {}).get('parts'), kind)
        tokens = usage(kind, data.get('usageMetadata'))
    if not content.strip():
        failure('原生模型未返回有效文字')
    return {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop', 'index': 0}], 'usage': tokens}


def events(response, check):
    """Incremental SSE framing, including multiline data; bounded per event and total."""
    response.raise_for_status()
    lines, size, total = [], 0, 0
    for line in response.iter_lines():
        check()
        total += len(line)
        if total > 8 * 1024**2:
            failure('原生回复超过处理预算')
        if line.startswith('data:'):
            value = line[5:].lstrip(' ')
            size += len(value)
            if size > 1024**2:
                failure('原生回复单个事件超过处理预算')
            lines.append(value)
        elif not line and lines:
            yield json.loads('\n'.join(lines))
            lines, size = [], 0
    # EOF does not dispatch an unterminated SSE event.


def stream_chunks(kind, response, check):
    terminal, started, has_text, reason, raw_usage = False, False, False, None, {}
    blocks, closed = {}, set()
    for data in events(response, check):
        if not isinstance(data, dict) or data.get('error'):
            failure('原生模型流返回错误，请核对服务状态')
        text = ''
        if kind == 'anthropic':
            event = data.get('type')
            if event == 'message_start':
                if started:
                    failure()
                started = True
                raw_usage.update(data.get('message', {}).get('usage') or {})
            elif event in ('content_block_start', 'content_block_delta', 'content_block_stop', 'message_delta', 'message_stop') and not started:
                failure('原生模型流缺少开始事件')
            elif event == 'content_block_start':
                block = data.get('content_block', {})
                index = data.get('index')
                if type(index) is not int or index < 0 or index in blocks or not isinstance(block, dict):
                    failure()
                blocks[index] = block.get('type')
                text = text_parts([block], kind)
            elif event == 'content_block_delta':
                delta = data.get('delta', {})
                index = data.get('index')
                if index not in blocks or index in closed or not isinstance(delta, dict):
                    failure()
                if blocks[index] == 'text' and delta.get('type') == 'text_delta':
                    text = delta.get('text', '')
                elif blocks[index] not in ('thinking', 'redacted_thinking') or delta.get('type') not in ('thinking_delta', 'signature_delta'):
                    failure('原生模型流包含尚未接入的工具内容')
            elif event == 'content_block_stop':
                index = data.get('index')
                if index not in blocks or index in closed:
                    failure()
                closed.add(index)
            elif event == 'message_delta':
                reason = data.get('delta', {}).get('stop_reason') or reason
                raw_usage.update(data.get('usage') or {})
            elif event == 'message_stop':
                finish_reason(kind, reason)
                if set(blocks) != closed:
                    failure('原生模型内容块未完整结束')
                terminal = started
                yield None, usage(kind, raw_usage)
                break
            elif event != 'ping':
                failure('原生模型流包含未知事件')
        else:
            if data.get('promptFeedback', {}).get('blockReason'):
                failure('原生模型服务拦截了请求，未返回有效回答')
            if data.get('usageMetadata'):
                yield None, usage(kind, data['usageMetadata'])
            candidates = data.get('candidates', [])
            if not isinstance(candidates, list) or len(candidates) > 1:
                failure()
            if candidates:
                candidate = candidates[0]
                if not isinstance(candidate, dict) or terminal:
                    failure('原生模型流在完成后继续返回内容或格式无效')
                if candidate.get('index', 0) != 0:
                    failure()
                text = text_parts(candidate.get('content', {}).get('parts', []), kind)
                if candidate.get('finishReason'):
                    finish_reason(kind, candidate['finishReason'])
                    terminal = True
        if not isinstance(text, str):
            failure()
        if text:
            has_text = has_text or bool(text.strip())
            yield text, None
    if not terminal or not has_text:
        failure('原生流式回复提前断开或未返回有效文字')
