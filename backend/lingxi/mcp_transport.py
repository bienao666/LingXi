"""Bounded Streamable HTTP MCP client. No local processes or implicit capabilities.

One ephemeral session per discovery/execution; interrupted calls are never replayed.
Session identifiers, remote errors and server instructions never leave this client.
"""
import json
import math
import re
import time
from contextlib import AbstractContextManager
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from referencing import Registry
from .network import outbound_client

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
LIMIT = 256 * 1024


def invalid(message):
    raise HTTPException(502, 'MCP ' + message)


def loads(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: invalid('JSON 包含重复字段')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: invalid('JSON 包含非有限数值'))
        def bounded(v, depth=0):
            if depth > 24: invalid('JSON 嵌套超过预算')
            if isinstance(v, float) and not math.isfinite(v): invalid('JSON 包含非有限数值')
            if isinstance(v, (list, dict)):
                for child in (v.values() if isinstance(v, dict) else v): bounded(child, depth+1)
        bounded(value)
        return value
    except (ValueError, RecursionError, UnicodeError):
        invalid('返回无效 JSON')


def schema_valid(schema):
    """2020-12, bounded schemas and local acyclic $refs only; no network resolver."""
    if not isinstance(schema, dict) or schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema') not in (
            'https://json-schema.org/draft/2020-12/schema', 'https://json-schema.org/draft/2020-12/schema#'):
        invalid('仅支持 JSON Schema 2020-12 对象')
    count = 0
    maps = {'$defs', 'definitions', 'properties', 'dependentSchemas'}
    singles = {'additionalProperties', 'items', 'contains', 'not', 'if', 'then', 'else',
               'propertyNames', 'unevaluatedProperties', 'unevaluatedItems', 'contentSchema'}
    arrays = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
    def walk(value, ancestors=(), depth=0, schema_node=True):
        nonlocal count
        count += 1
        if count > 2048 or depth > 20: invalid('工具 Schema 超出预算或循环引用')
        if isinstance(value, dict) and schema_node:
            # Regex evaluation and dynamic reference scope require a separate
            # sandbox; do not silently accept unenforced validation keywords.
            if any(k in value for k in ('$dynamicRef', '$dynamicAnchor', 'pattern', 'patternProperties')):
                invalid('工具 Schema 含当前不支持的动态引用或正则约束')
            if '$ref' in value:
                ref = value['$ref']
                if not isinstance(ref, str) or not ref.startswith('#/') or ref in ancestors:
                    invalid('工具 Schema 仅允许无环的本地引用')
                target = schema
                try:
                    for part in ref[2:].split('/'):
                        key = part.replace('~1', '/').replace('~0', '~')
                        target = target[int(key)] if isinstance(target, list) else target[key]
                except (KeyError, TypeError, ValueError, IndexError): invalid('工具 Schema 本地引用不存在')
                walk(target, ancestors+(ref,), depth+1)
            for key, child in value.items():
                if key in maps and isinstance(child, dict):
                    for item in child.values(): walk(item, ancestors, depth+1)
                elif key in arrays and isinstance(child, list):
                    for item in child: walk(item, ancestors, depth+1)
                else: walk(child, ancestors, depth+1, schema_node=key in singles)
        elif isinstance(value, dict):
            # Annotation/default data and property names are not schema keywords.
            for child in value.values(): walk(child, ancestors, depth+1, schema_node=False)
        elif isinstance(value, list):
            for child in value: walk(child, ancestors, depth+1, schema_node=False)
    walk(schema)
    try: Draft202012Validator.check_schema(schema)
    except Exception: invalid('工具 Schema 无效')


def validate_input(value, schema):
    schema_valid(schema)
    try:
        if not Draft202012Validator(schema, registry=Registry()).is_valid(value):
            raise ValueError()
    except Exception:
        raise HTTPException(422, '输入不符合已授权工具的 JSON Schema；请查看工具字段约束') from None


class McpClient(AbstractContextManager):
    def __init__(self, entry, headers, allow_private):
        self.url, self.headers, self.allow_private = entry['url'], headers, allow_private
        self.version, self.session, self.sequence = None, None, 0
        self.changed, self.deadline = False, time.monotonic()+45

    def __enter__(self):
        self.manager = outbound_client(self.url, headers=self.headers, allow_private=self.allow_private, timeout=15)
        self.client = self.manager.__enter__()
        try:
            result = self.request('initialize', {'protocolVersion': VERSIONS[0], 'capabilities': {},
                'clientInfo': {'name': 'lingxi', 'version': '0.2.0'}})
            if result.get('protocolVersion') not in VERSIONS: invalid('协议版本不受支持')
            if not isinstance(result.get('capabilities'), dict) or not isinstance(result['capabilities'].get('tools'), dict):
                invalid('服务未声明 tools 能力')
            self.version = result['protocolVersion']
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        # Closing transport terminates our usage. No DELETE: failed/unknown calls
        # retain their remote session for operator investigation until server expiry.
        return self.manager.__exit__(*args)

    def send(self, message):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0: invalid('会话超过 45 秒预算；不会自动重发')
        headers = {'Accept': 'application/json, text/event-stream'}
        if self.version: headers['MCP-Protocol-Version'] = self.version
        if self.session: headers['MCP-Session-Id'] = self.session
        with self.client.stream('POST', self.url, json=message, headers=headers, timeout=min(15, remaining)) as response:
            if response.status_code not in (200, 202):
                invalid(f'返回 HTTP {response.status_code}；未跟随跳转或重发')
            if message.get('method') == 'initialize':
                session = response.headers.get('mcp-session-id')
                if session and (len(session) > 1024 or any(ord(c)<33 or ord(c)>126 for c in session)):
                    invalid('会话标识无效')
                self.session = session
            if 'id' not in message or 'method' not in message:
                if response.status_code != 202: invalid('通知未得到 202 接受回执')
                return None
            kind = response.headers.get('content-type', '').split(';')[0].lower()
            raw, buffer, data, events = bytearray(), '', [], 0
            def event():
                nonlocal data, events
                payload = '\n'.join(data); data = []
                if not payload: return None
                events += 1
                if events > 128: invalid('事件数量超过预算')
                return self.envelope(loads(payload), message['id'])
            def drain(final=False):
                nonlocal buffer
                while True:
                    separator = re.search(r'\r\n|\r|\n', buffer)
                    if not separator: return None
                    if not final and separator.end() == len(buffer) and buffer.endswith('\r'): return None
                    line = buffer[:separator.start()].encode('latin1').decode('utf-8', errors='strict')
                    buffer = buffer[separator.end():]
                    if line == '':
                        result = event()
                        if result is not None: return result
                    elif line.startswith('data:'):
                        data.append(line[5:].removeprefix(' '))
            for chunk in response.iter_bytes():
                if time.monotonic() > self.deadline: invalid('响应超过会话预算')
                raw.extend(chunk)
                if len(raw) > LIMIT: invalid('响应超过 256 KiB；未截断冒充完整结果')
                if kind == 'text/event-stream':
                    # Decode only complete lines, so UTF-8 and JSON may cross chunks.
                    buffer += chunk.decode('latin1')
                    result = drain()
                    if result is not None: return result
            if kind == 'application/json': return self.envelope(loads(raw), message['id'], final=True)
            if kind == 'text/event-stream':
                result = drain(final=True)
                if result is not None: return result
                invalid('事件流中断且未收到结果；不会重放调用')
            invalid('响应类型必须是 JSON 或 SSE')

    def envelope(self, value, identity, final=False):
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0': invalid('JSON-RPC 信封无效')
        if 'method' in value:
            if 'id' in value:
                # No roots, sampling or elicitation capabilities were negotiated.
                result = {'result': {}} if value['method'] == 'ping' else {'error': {
                    'code': -32601, 'message': 'Client capability not supported'}}
                self.send({'jsonrpc': '2.0', 'id': value['id']} | result)
            elif value['method'] == 'notifications/tools/list_changed': self.changed = True
            if final: invalid('请求没有对应结果')
            return None
        if type(value.get('id')) is not type(identity) or value.get('id') != identity:
            invalid('响应 ID 与请求不匹配')
        if 'error' in value: invalid('返回协议错误；远端错误正文未保存，执行结果需核对')
        if not isinstance(value.get('result'), dict): invalid('缺少结果对象')
        return value['result']

    def request(self, method, params):
        self.sequence += 1
        return self.send({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params})

    def tools(self):
        result, cursor, seen = [], None, set()
        for _ in range(8):
            page = self.request('tools/list', {'cursor': cursor} if cursor else {})
            tools = page.get('tools')
            if not isinstance(tools, list): invalid('工具目录不是列表')
            for tool in tools:
                if not isinstance(tool, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', tool.get('name', '')):
                    invalid('工具名称无效')
                if any(k in tool and not isinstance(tool[k], str) for k in ('title', 'description')):
                    invalid('工具名称及说明必须是文字')
                if not isinstance(tool.get('execution', {}), dict): invalid('工具执行声明无效')
                if any(t['name'] == tool['name'] for t in result): invalid('工具名称重复')
                if tool.get('execution', {}).get('taskSupport') == 'required': invalid('不支持要求异步 task 的工具')
                schema_valid(tool.get('inputSchema'))
                if 'outputSchema' in tool: schema_valid(tool['outputSchema'])
                if len(json.dumps(tool, ensure_ascii=False)) > 16000: invalid('单个工具定义超过预算')
                # Keep descriptions and hints as untrusted display data, never instructions.
                result.append({k: tool[k] for k in ('name', 'title', 'description', 'inputSchema', 'outputSchema', 'annotations') if k in tool})
                if len(result) > 128: invalid('工具超过 128 项')
            cursor = page.get('nextCursor')
            if cursor is None:
                if self.changed: invalid('发现期间工具发生变化，请重新发现')
                return sorted(result, key=lambda t:t['name'])
            if not isinstance(cursor, str) or not cursor or len(cursor)>1000 or cursor in seen:
                invalid('分页游标无效或循环')
            seen.add(cursor)
        invalid('工具目录超过 8 页')

    def call(self, name, arguments, tool):
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        if self.changed: invalid('调用期间工具目录变化；结果待核对')
        if not isinstance(result.get('content'), list) or type(result.get('isError', False)) is not bool:
            invalid('工具返回无效结果')
        if len(result['content']) > 128 or ('structuredContent' in result and not isinstance(result['structuredContent'], dict)):
            invalid('工具内容数量或结构化结果无效')
        for block in result['content']:
            if not isinstance(block, dict): invalid('工具内容块无效')
            kind = block.get('type')
            if kind == 'text': valid = isinstance(block.get('text'), str)
            elif kind in ('image', 'audio'):
                valid = isinstance(block.get('data'), str) and isinstance(block.get('mimeType'), str)
            elif kind == 'resource_link':
                valid = isinstance(block.get('uri'), str) and isinstance(block.get('name'), str)
            elif kind == 'resource':
                resource = block.get('resource')
                valid = isinstance(resource, dict) and isinstance(resource.get('uri'), str) and (
                    isinstance(resource.get('text'), str) or isinstance(resource.get('blob'), str))
            else: valid = False
            if not valid: invalid('工具内容块不符合协议；不会自动获取资源链接')
        if tool.get('outputSchema') and not result.get('isError'):
            if 'structuredContent' not in result: invalid('缺少工具声明的结构化结果')
            validate_input(result['structuredContent'], tool['outputSchema'])
        return result
