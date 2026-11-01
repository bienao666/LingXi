"""Opt-in real-service benchmark; interrupted/uncertain requests are never replayed."""
import argparse
import getpass
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import httpx
try:
    from .dataset import build
    from .report import SCOPE, dataset_digest, save_json
except ImportError:
    from dataset import build
    from report import SCOPE, dataset_digest, save_json


def timestamp(): return datetime.now(timezone.utc).isoformat()


def base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or
        parsed.query or parsed.fragment or parsed.path not in ('', '/') or
        (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('使用 HTTPS 或本机 HTTP 入口；地址不能含凭据、子路径、参数或片段')
    return value.rstrip('/')


class API:
    def __init__(self, client):
        self.client, self.checkpoint, self.pending, self.last_receipt = client, None, None, None

    def __call__(self, method, path, **kwargs):
        write = method != 'GET'
        if write:
            self.pending = {'method': method, 'path': path}
            if self.checkpoint: self.checkpoint()
        response = self.client.request(method, path, **kwargs)
        if 400 <= response.status_code < 500: self.pending = None
        response.raise_for_status()
        value = response.json()
        needs_id = write and (path in ('/api/memories', '/api/conversations', '/api/chat') or
            path.startswith('/api/memories/') and method in ('PATCH','DELETE'))
        if needs_id:
            if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
                raise ValueError('创建结果没有可追踪编号')
            self.last_receipt = {'method':method, 'path':path, 'id':value['id']}
            if self.checkpoint: self.checkpoint()
        if write: self.pending = None
        return value


def wait_for(read, predicate, terminal_error=lambda row: False, timeout=180, pause=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if predicate(value): return value
        if terminal_error(value): raise RuntimeError('后台处理失败；停止本次评测，不自动重试')
        if time.monotonic() >= deadline: raise TimeoutError('等待后台处理超时')
        pause(min(1, max(0, deadline - time.monotonic())))


def run_ready(api, identity, **kwargs):
    row = wait_for(lambda: api('GET', '/api/chat-runs/' + identity),
        lambda value: value['status'] == 'completed',
        lambda value: value['status'] not in ('queued', 'running'), **kwargs)
    if row['memory_status'] != 'ready': raise RuntimeError('长期记忆服务不可用，不能作为检索质量结果')
    return row


def indexed(api, identity, **kwargs):
    return wait_for(lambda: api('GET', '/api/memories/' + identity),
        lambda value: value['index_status'] == 'ready',
        lambda value: value['index_status'] in ('failed', 'deleted', 'deleting'), **kwargs)


def source_check(case, memories, run):
    refs = run.get('memory_refs')
    allowed = {row['id']: row['version'] for row in memories}
    if not isinstance(refs, list): return {'passed': False, 'expected_retrieved':False, 'reason': '缺少实际检索来源'}
    valid = all(isinstance(ref, dict) and ref.get('id') in allowed and
        ref.get('version') == allowed.get(ref.get('id')) for ref in refs)
    expected = not (case['expected'] and memories and not refs)
    return {'passed': valid, 'expected_retrieved':expected,
        'reason': '来源均为本题有效版本' if valid and expected else '没有召回预期依据' if valid else '失效版本或其他题目资料'}


def preflight(api):
    user = api('GET', '/api/auth/me')
    if user['username'] != 'eval-lingxi' or user['auto_memory'] is not False:
        raise ValueError('必须使用专用 eval-lingxi，且先在隔离环境关闭自动保存记忆')
    for path in ('/api/conversations', '/api/memories', '/api/memory-candidates'):
        if api('GET', path): raise ValueError('仅允许空的隔离评测账号，不修改已有数据')
    return user


def evaluate(api, output, limit=150, restart=input, pause=time.sleep, timeout=180):
    if type(limit) is not int or not 1 <= limit <= 150: raise ValueError('题数范围为 1–150')
    owner = preflight(api)
    report = {'schema_version': 2, 'scope': SCOPE, 'dataset_sha256': dataset_digest(),
        'run_id': uuid.uuid4().hex, 'planned_count': limit, 'started_at': timestamp(),
        'status': 'running', 'results': [{'id': case['id'], 'group': case['group'], 'status': 'not_run'}
            for case in build()[:limit]]}
    save_json(output, report)
    def checkpoint():
        report['pending_request'] = api.pending
        report['last_write_receipt'] = api.last_receipt
        save_json(output, report, replace=True)
    api.checkpoint = checkpoint
    interrupted = False
    try:
        for case, outcome in zip(build()[:limit], report['results']):
            memories, conversations, runs, deleted = [], [], [], {}
            api.last_receipt = None
            outcome.update(status='running', started_at=timestamp()); checkpoint()
            try:
                current = api('GET', '/api/auth/me')
                if current['id'] != owner['id'] or current['auto_memory'] is not False:
                    raise RuntimeError('评测身份或自动保存设置已变化')
                for fact in case['facts']:
                    memory = api('POST', '/api/memories', json={'content': fact, 'category': 'experiences'})
                    memories.append(memory); outcome['memory_ids'] = [row['id'] for row in memories]; checkpoint()
                # Keep every created ID even if a later index read fails.
                for i, memory in enumerate(memories): memories[i] = indexed(api, memory['id'], timeout=timeout, pause=pause)
                kind = (case['action'] or {}).get('kind')
                if kind == 'correct':
                    memory = api('PATCH', '/api/memories/' + memories[0]['id'], json={
                        'content': case['action']['content'], 'version': memories[0]['version'], 'category': 'experiences'})
                    memories[0] = indexed(api, memory['id'], timeout=timeout, pause=pause)
                if kind == 'delete':
                    for memory in memories: deleted[memory['id']] = api('DELETE', '/api/memories/' + memory['id'])['id']
                    outcome['deletion_operations'] = list(deleted.values())
                if kind == 'restart':
                    note = restart(case['id'] + '：实际重启 Core/worker/Hindsight 并核对就绪，输入说明（留空停止）：').strip()
                    if not note: raise RuntimeError('未确认实际重启')
                    outcome['restart_evidence'] = {'type': 'operator_attestation', 'note': note[:2000]}
                    for memory in memories: indexed(api, memory['id'], timeout=timeout, pause=pause)
                conversation = api('POST', '/api/conversations', json={'title': 'EVAL ' + case['id']})
                conversations.append(conversation['id']); outcome['conversation_ids'] = conversations[:]; checkpoint()
                body = {'conversation_id': conversation['id'], 'request_id': 'eval-' + report['run_id'] + '-' + case['id'], 'content': case['query']}
                run = api('POST', '/api/chat', json=body)
                runs.append(run['id']); outcome['run_ids'] = runs[:]; checkpoint()
                if kind == 'redelivery':
                    same = api('POST', '/api/chat', json=body)
                    if same['id'] != run['id']: raise RuntimeError('重复提交产生了不同执行')
                    outcome['redelivery_same_run'] = True
                result = run_ready(api, run['id'], timeout=timeout, pause=pause)
                messages = api('GET', '/api/messages', params={'conversation_id': conversation['id']})
                answers = [row for row in messages if row['role'] == 'assistant' and row.get('run_id') == run['id'] and row['status'] == 'completed']
                if len(answers) != 1: raise RuntimeError('缺少对应执行的唯一最终回答')
                answer = answers[0]['content']
                outcome.update(status='needs_review', answer=answer, memory_refs=result['memory_refs'],
                    model_id=result['model_id'], memory_status=result['memory_status'],
                    model_config={key:result.get('config', {}).get(key) for key in
                        ('model','provider_id','provider_version','provider_protocol','policy_version','prompt_version')},
                    source_check=source_check(case, [] if kind == 'delete' else memories, result),
                    automated_assertions_passed=all(value in answer for value in case['expected']) and not any(value in answer for value in case['forbidden']))
            except KeyboardInterrupt:
                outcome.update(status='interrupted', error='KeyboardInterrupt'); interrupted = True
            except Exception as exc:
                outcome.update(status='error', error=type(exc).__name__)
            finally:
                uncertain, cleanup_errors = api.pending, []
                # An interrupt between receiving a create response and appending its ID is recoverable.
                receipt = api.last_receipt or {}
                if receipt.get('method') == 'POST':
                    identity = receipt['id']
                    if receipt['path'] == '/api/memories' and identity not in {row['id'] for row in memories}:
                        memories.append({'id':identity})
                    elif receipt['path'] == '/api/conversations' and identity not in conversations: conversations.append(identity)
                    elif receipt['path'] == '/api/chat' and identity not in runs: runs.append(identity)
                elif receipt.get('method') == 'DELETE' and receipt.get('path','').startswith('/api/memories/'):
                    deleted[receipt['path'].rsplit('/',1)[1]] = receipt['id']
                # Only known IDs from this case. Never sweep by title or enumerate-and-delete user data.
                for identity in runs:
                    try: api('POST', '/api/chat-runs/' + identity + '/cancel')
                    except Exception: cleanup_errors.append('cancel_run:' + identity)
                for identity in conversations:
                    try: api('DELETE', '/api/conversations/' + identity, params={'delete_memories': 'true'})
                    except Exception: cleanup_errors.append('conversation:' + identity)
                for memory in memories:
                    try:
                        if uncertain == {'method':'DELETE','path':'/api/memories/' + memory['id']}:
                            raise RuntimeError('删除结果未知，不重复提交')
                        if memory['id'] not in deleted: deleted[memory['id']] = api('DELETE', '/api/memories/' + memory['id'])['id']
                        wait_for(lambda: api('GET', '/api/memory-operations/' + deleted[memory['id']]),
                            lambda value: value['status'] == 'completed', lambda value: value['status'] == 'failed', timeout=timeout, pause=pause)
                    except Exception: cleanup_errors.append('memory:' + memory['id'])
                outcome['cleanup'] = {'passed': not cleanup_errors and uncertain is None,
                    'failed_targets': cleanup_errors, 'uncertain_request': uncertain}
                outcome['finished_at'] = timestamp(); checkpoint()
            print(outcome['id'], outcome['status'])
            if outcome['status'] != 'needs_review' or not outcome['cleanup']['passed']:
                report['status'] = 'interrupted' if interrupted else 'stopped'; break
        else: report['status'] = 'awaiting_review'
    finally:
        report['finished_at'] = timestamp(); checkpoint(); api.checkpoint = None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立中文记忆检索评测，可能产生模型和索引费用')
    parser.add_argument('--base-url', default='http://localhost:8080')
    parser.add_argument('--output', default='.preview/memory-eval/results.json')
    parser.add_argument('--limit', type=int, choices=range(1, 151), default=150, metavar='1..150')
    parser.add_argument('--allow-model-calls', action='store_true', help='允许隔离部署调用已配置的模型、嵌入、重排和抽取服务')
    args = parser.parse_args(argv)
    if not args.allow_model_calls: parser.error('真实运行必须明确指定 --allow-model-calls；离线报告无需此选项')
    if Path(args.output).exists(): parser.error('结果已存在；不覆盖或自动重放，请先核对原执行和清理状态，再使用新输出路径')
    try:
        url = base_url(args.base_url)
        with httpx.Client(base_url=url, timeout=30, trust_env=False, follow_redirects=False, headers={'Origin': url}) as client:
            api = API(client)
            auth = api('POST', '/api/auth/login', json={'username': 'eval-lingxi', 'password': getpass.getpass('独立 eval-lingxi 密码：'), 'code': getpass.getpass('TOTP（未启用则留空）：')})
            client.headers['X-CSRF-Token'] = auth['csrf']
            try: report = evaluate(api, args.output, args.limit)
            finally:
                api.checkpoint = None
                try: api('POST', '/api/auth/logout')
                except Exception: pass
        return 0 if report['status'] == 'awaiting_review' else 2
    except (ValueError, OSError, httpx.HTTPError, KeyError):
        parser.exit(1, '评测未完成，请核对隔离账号、自动保存开关、连接、输出和检查点；不自动重试。\n')


if __name__ == '__main__': raise SystemExit(main())
