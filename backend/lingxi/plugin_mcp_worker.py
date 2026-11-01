"""A fresh negotiated MCP session, directory revalidation, then a fenced tools/call."""
import json
import re
from fastapi import HTTPException
from .db import SessionLocal, now
from .plugin_worker import checkpoint, TERMINAL
from .plugin_mcp import runtime, arguments
from .plugin_api import auth_headers, reserve, write_barrier, invalidate_api
from .plugin_manifest import digest
from .mcp_transport import McpClient
from .security import decrypt, safe_error


def execute_mcp(job):
    from .plugin_oauth import ensure_job
    ensure_job(job)
    with SessionLocal.begin() as db:
        row, user = checkpoint(db, job)
        if row.status in TERMINAL: return
        if row.config['mcp'].get('dispatch') != 'not_sent':
            row.status, row.error, row.finished_at = 'unknown', '上次 MCP 调用可能已送出；先核对，不会重发', now()
            row.version += 1
            return
        entry, connection, catalog, tool = runtime(db, row, user)
        arguments(row.input, tool)
        write_barrier(db, row, {'effect': 'write'}); reserve(db, user)
        headers, allow_private = auth_headers(connection, entry), connection.allow_private
        secret = decrypt(connection.encrypted_secret) if connection.encrypted_secret else ''
    # initialize/list are metadata requests, not tools/call. No send marker yet.
    with McpClient(entry, headers, allow_private) as remote:
        found = remote.tools()
        with SessionLocal.begin() as db:
            row, user = checkpoint(db, job)
            if row.status in TERMINAL: return
            _, _, catalog, tool = runtime(db, row, user)
            if digest(found) != catalog.digest:
                catalog.discovered_at, catalog.approved_at, catalog.approved_tools = None, None, []
                catalog.version += 1
                invalidate_api(db, user, row.installation_id, '远端 MCP 工具目录变化，请重新发现并授权', row.capability_id)
                return
            value = arguments(row.input, tool)
            write_barrier(db, row, {'effect': 'write'})
            row.status, row.version = 'running', row.version+1
            row.config = row.config | {'mcp': row.config['mcp'] | {'dispatch': 'in_flight', 'dispatched_at': now()}}
            row.receipt = {'adapter': 'mcp', 'tool': tool['name'], 'tool_digest': digest(tool),
                'protocol_version': remote.version, 'external_writes': True,
                'request_key': row.id, 'job_attempt': job.attempts,
                'note': '已进入工具发送阶段；停止等待不能撤销远程操作。'}
        output, error, result_digest, is_error = '', None, None, False
        try:
            result = remote.call(tool['name'], value, tool)
            result_digest = digest(result)
            is_error = result.get('isError', False)
            def scrub(value, depth=0):
                if depth > 12: raise HTTPException(502, 'MCP 结果嵌套超过预算')
                def text(s):
                    for token in (secret, remote.session):
                        if token: s = s.replace(token, '[redacted]')
                    return s
                if isinstance(value, dict):
                    return {text(k): '[redacted]' if re.search(r'(?i)secret|password|token|authorization|cookie|api.?key', k)
                            else scrub(v, depth+1) for k,v in value.items()}
                if isinstance(value, list): return [scrub(v, depth+1) for v in value]
                return text(value) if isinstance(value, str) else value
            output = json.dumps(scrub(result), ensure_ascii=False, allow_nan=False)
            if len(output) > 30000: raise HTTPException(502, 'MCP 结果超过展示预算；未截断冒充完整结果')
        except Exception as exc:
            error = safe_error(exc)
        with SessionLocal.begin() as db:
            row, user = checkpoint(db, job)
            receipt = row.receipt | {'response_digest': result_digest, 'tool_error': is_error,
                                    'release_digest': row.config['release_digest']}
            if remote.changed:
                from .plugin_mcp import McpCatalog
                from sqlalchemy import select
                catalog = db.scalar(select(McpCatalog).where(McpCatalog.connection_id == row.config['mcp']['connection_id']))
                if catalog:
                    catalog.discovered_at, catalog.approved_at, catalog.approved_tools = None, None, []
                    catalog.version += 1
                invalidate_api(db, user, row.installation_id, 'MCP 通知工具目录变化，请重新发现并授权', row.capability_id)
            if row.status in TERMINAL:
                row.receipt = receipt | {'late_response': True}
                return
            try: runtime(db, row, user)
            except HTTPException:
                row.status, row.result, row.error = 'unknown', '', 'MCP 授权或版本变化；外部结果待核对'
            else:
                row.status = 'unknown' if error or is_error else 'succeeded'
                row.result = output if not error else ''
                row.error = error or ('MCP 工具报告错误；不能据此断言未产生副作用，请核对' if is_error else None)
            row.receipt = receipt | {'verification': 'mcp_result' if not error else 'incomplete',
                'note': '已收到 MCP 工具成功响应，未独立验证第三方业务结果。' if row.status == 'succeeded'
                        else '远程操作结果需核对，系统不会自动重发。'}
            row.config = row.config | {'mcp': row.config['mcp'] | {'dispatch': 'responded' if not error else 'unknown'}}
            row.finished_at, row.version = now(), row.version+1
