"""Evaluation tooling protocol and scoring tests; these never measure real model quality."""
from copy import deepcopy
import json
from pathlib import Path
import httpx
import pytest
from evals import report as scoring, run as runner
from evals.dataset import build


def evidence():
    return {'schema_version': 2, 'scope': scoring.SCOPE, 'run_id': 'isolated-fixture',
        'dataset_sha256': scoring.dataset_digest(), 'planned_count': 150, 'status': 'awaiting_review',
        'results': [{'id': case['id'], 'group': case['group'], 'status': 'needs_review',
            'answer': '|'.join(case['expected']) or '没有依据，无法确定。', 'source_check': {'passed': True, 'expected_retrieved':True},
            'cleanup': {'passed': True}} for case in build()]}


def reviewed(data):
    reviews = scoring.template(data)
    reviews['reviewer'] = '隔离夹具，不是真实质量测量'
    for row in reviews['reviews']:
        row.update(correct=True, unsupported_assertion=False, blocking_issue=False, reason='单元测试合成复核')
    return reviews


def test_all_150_answers_never_prove_extraction_restore_or_full_product():
    data = evidence(); result = scoring.summarize(data, reviewed(data))
    assert result['status'] == 'answers_passed_system_checks_pending'
    assert result['first_five'] == {'correct':120, 'total':120, 'rate':1, 'passed':True}
    assert not result['full_product_verified']
    assert result['system_lifecycle_gate'] == result['extraction_quality'] == 'not_covered'
    assert '系统断言待验收' in scoring.markdown(result)


def test_template_never_fills_in_human_judgements():
    data = evidence(); template = scoring.template(data)
    assert all(row['correct'] is None for row in template['reviews'])
    result = scoring.summarize(data, template)
    assert result['status'] == 'incomplete' and len(result['pending_reviews']) == 150
    assert not result['unknown_disputed']['passed']


@pytest.mark.parametrize('change', ['answer', 'cleanup', 'model', 'question', 'run', 'dataset'])
def test_changed_results_or_review_display_invalidates_review(change):
    data = evidence(); reviews = reviewed(data)
    if change == 'answer': data['results'][0]['answer'] = 'changed'
    elif change == 'cleanup': data['results'][0]['cleanup']['passed'] = False
    elif change == 'model': data['results'][0]['model_id'] = 'new-model'
    elif change == 'question': reviews['reviews'][0]['question'] = 'changed'
    elif change == 'run': reviews['run_id'] = 'other-run'
    else: data['dataset_sha256'] = '0'*64
    with pytest.raises(ValueError): scoring.summarize(data, reviews)


@pytest.mark.parametrize('change', ['duplicate', 'missing', 'group', 'foreign', 'legacy', 'source_type'])
def test_invalid_or_incomplete_raw_result_structure_rejected(change):
    data = evidence()
    if change == 'duplicate': data['results'].append(data['results'][0])
    elif change == 'missing': data['results'].pop()
    elif change == 'group': data['results'][0]['group'] = 'deletion'
    elif change == 'foreign': data['results'][0]['id'] = 'M999'
    elif change == 'legacy': data['schema_version'] = 1
    else: data['results'][0]['source_check'] = []
    with pytest.raises(ValueError): scoring.template(data)


@pytest.mark.parametrize('field', ['correct','unsupported_assertion','blocking_issue'])
def test_review_booleans_are_strict_and_unexplained_reviews_stay_pending(field):
    data = evidence(); reviews = reviewed(data); reviews['reviews'][0][field] = 'false'
    with pytest.raises(ValueError): scoring.summarize(data, reviews)
    reviews = reviewed(data); reviews['reviews'][0]['reason'] = ' '
    assert 'M001' in scoring.summarize(data, reviews)['pending_reviews']


def test_answer_threshold_boundary_and_unknown_denominator_are_fixed():
    data = evidence(); reviews = reviewed(data)
    for row in reviews['reviews'][:12]: row['correct'] = False
    reviews['reviews'][100]['unsupported_assertion'] = True
    result = scoring.summarize(data, reviews)
    assert result['first_five']['correct'] == 107 and not result['first_five']['passed']
    assert result['unknown_disputed']['rate'] == .05 and result['unknown_disputed']['passed']
    reviews['reviews'][12]['correct'] = False
    reviews['reviews'][101]['unsupported_assertion'] = True
    result = scoring.summarize(data, reviews)
    assert not result['first_five']['passed'] and not result['unknown_disputed']['passed']
    assert result['status'] == 'failed'


def test_ordinary_recall_misses_reduce_accuracy_without_becoming_privacy_blockers():
    data = evidence()
    for row in data['results'][:12]: row['source_check']['expected_retrieved'] = False
    result = scoring.summarize(data, reviewed(data))
    assert not result['blocking_cases'] and result['first_five']['correct'] == 108
    assert result['first_five']['passed'] and result['status'] == 'answers_passed_system_checks_pending'


@pytest.mark.parametrize('case', ['source','blocking','cleanup','partial','execution_error','not_finished'])
def test_small_safety_failure_cannot_be_hidden_by_high_average(case):
    data = evidence()
    if case == 'source': data['results'][0]['source_check']['passed'] = False
    elif case == 'cleanup': data['results'][0]['cleanup']['passed'] = False
    elif case == 'partial': data['planned_count'] = 1; data['results'] = data['results'][:1]
    elif case == 'execution_error': data['results'][0]['status'] = 'error'
    elif case == 'not_finished': data['status'] = 'running'
    reviews = reviewed(data)
    if case == 'blocking': reviews['reviews'][0]['blocking_issue'] = True
    result = scoring.summarize(data, reviews)
    assert result['status'] in ('blocked','incomplete') and not result['full_product_verified']


def test_reports_are_atomic_do_not_overwrite_and_reject_duplicate_json_fields(tmp_path):
    target = tmp_path/'evidence.json'; scoring.save_json(target, {'a':1})
    with pytest.raises(ValueError): scoring.save_json(target, {'a':2})
    assert scoring.read_json(target) == {'a':1}
    scoring.save_json(target, {'a':3}, replace=True)
    assert scoring.read_json(target) == {'a':3}
    target.write_text('{"a":1,"a":2}')
    with pytest.raises(ValueError): scoring.read_json(target)


class Fixture:
    def __init__(self, mode='normal'):
        self.mode, self.requests, self.memories, self.runs, self.bodies = mode, [], {}, {}, {}
        self.cases = {case['id']:case for case in build()}
        self.operations = {}; self.conversations = []; self.owner = {'id':'eval-owner','username':'eval-lingxi','auto_memory':False}

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        def reply(value, code=200): return httpx.Response(code, json=value)
        if path == '/api/auth/me': return reply(self.owner)
        if method == 'GET' and path in ('/api/conversations','/api/memories','/api/memory-candidates'): return reply([])
        if path == '/api/memories' and method == 'POST':
            if self.mode == 'uncertain': return reply({'error':'fixture'},500)
            identity = 'memory-'+str(len(self.memories))
            self.memories[identity] = body|{'id':identity,'version':1,'index_status':'ready'}
            return reply(self.memories[identity])
        if path.startswith('/api/memories/'):
            identity = path.rsplit('/',1)[1]; row = self.memories[identity]
            if method == 'GET':
                if self.mode == 'index_failed': return reply(row|{'index_status':'failed'})
                return reply(row)
            if method == 'PATCH': row.update(body|{'version':row['version']+1}); return reply(row)
            if self.mode == 'cleanup_failed': return reply({'error':'fixture'},500)
            if self.mode == 'malformed_delete': return reply({'status':'accepted'})
            row['deleted'] = True
            self.operations[identity] = {'id':identity,'status':'completed'}
            return reply(self.operations[identity])
        if path.startswith('/api/memory-operations/'): return reply(self.operations[path.rsplit('/',1)[1]])
        if path == '/api/conversations' and method == 'POST':
            identity = 'conversation-'+str(len(self.conversations)); self.conversations.append(identity)
            return reply({'id':identity})
        if path.startswith('/api/conversations/') and method == 'DELETE': return reply({'status':'deleted'})
        if path == '/api/chat':
            if self.mode == 'interrupt': raise KeyboardInterrupt()
            if body['request_id'] in self.bodies: return reply(self.runs[self.bodies[body['request_id']]])
            case = self.cases[body['request_id'].rsplit('-',1)[1]]
            identity = 'run-'+str(len(self.runs))
            self.bodies[body['request_id']] = identity
            self.runs[identity] = {'id':identity,'status':'completed','memory_status':'ready','model_id':'protocol-fixture',
                'config':{'model':'fixture-model','provider_id':'p','provider_version':7,'api_key':'must-not-be-exported'},
                'memory_refs':[{'id':row['id'],'version':row['version']} for row in self.memories.values() if not row.get('deleted')],
                'answer':'|'.join(case['expected']) or '没有依据，无法确定。'}
            return reply(self.runs[identity])
        if path.startswith('/api/chat-runs/'):
            return reply(self.runs[path.split('/')[3]])
        if path == '/api/messages':
            identity = next(reversed(self.runs)); row = self.runs[identity]
            return reply([{'role':'assistant','run_id':'other','status':'completed','content':'错误的旧回答'},
                {'role':'assistant','run_id':identity,'status':'completed','content':row['answer']}])
        raise AssertionError((method,path))


def perform(tmp_path, fixture, limit=1):
    with httpx.Client(base_url='https://isolated.test', transport=httpx.MockTransport(fixture)) as client:
        return runner.evaluate(runner.API(client), tmp_path/'results.json', limit,
            restart=lambda _: '合成协议测试，未实际重启服务器', pause=lambda _:None, timeout=1)


def test_protocol_all_150_results_are_reviewable_and_select_exact_run_answer(tmp_path):
    fixture = Fixture(); data = perform(tmp_path, fixture, 150)
    assert data['status'] == 'awaiting_review'
    assert all(row['source_check']['passed'] and row['cleanup']['passed'] for row in data['results'])
    assert all(row['status'] == 'needs_review' for row in data['results'])
    assert all('错误的旧回答' not in row['answer'] for row in data['results'])
    assert all(row['model_config']['provider_version'] == 7 for row in data['results'])
    assert 'must-not-be-exported' not in json.dumps(data)
    assert all(row.get('deleted') for row in fixture.memories.values())
    assert scoring.summarize(data, scoring.template(data))['status'] == 'incomplete'


@pytest.mark.parametrize('mode', ['uncertain','index_failed','cleanup_failed','interrupt'])
def test_runner_stops_and_records_partial_work_without_replaying(tmp_path, mode):
    fixture = Fixture(mode); data = perform(tmp_path, fixture, 2)
    assert data['status'] in ('stopped','interrupted') and data['results'][1]['status'] == 'not_run'
    assert len([request for request in fixture.requests if request == ('POST','/api/memories')]) == 1
    assert Path(tmp_path/'results.json').exists()
    if mode in ('uncertain','cleanup_failed','interrupt'): assert not data['results'][0]['cleanup']['passed']
    if mode == 'index_failed': assert ('DELETE','/api/memories/memory-0') in fixture.requests


@pytest.mark.parametrize('change', ['owner','auto','not_empty'])
def test_preflight_performs_no_business_writes_when_owner_or_scope_wrong(tmp_path, change):
    fixture = Fixture()
    if change == 'owner': fixture.owner['username'] = 'daily-owner'
    if change == 'auto': fixture.owner['auto_memory'] = True
    original = fixture.__call__
    def handle(request):
        if change == 'not_empty' and request.url.path == '/api/conversations': return httpx.Response(200,json=[{'id':'existing'}])
        return original(request)
    with httpx.Client(base_url='https://isolated.test',transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError): runner.evaluate(runner.API(client), tmp_path/'results.json')
    assert all(method == 'GET' for method,_ in fixture.requests)
    assert not (tmp_path/'results.json').exists()


def test_runner_requires_explicit_model_opt_in_before_password_or_network(monkeypatch):
    monkeypatch.setattr(runner.getpass,'getpass',lambda _:pytest.fail('must not ask password'))
    with pytest.raises(SystemExit) as error: runner.main([])
    assert error.value.code == 2


def test_interrupt_after_create_receipt_still_cleans_only_its_known_id(tmp_path):
    fixture = Fixture()
    class InterruptedAPI(runner.API):
        def __call__(self, method, path, **kwargs):
            result = super().__call__(method,path,**kwargs)
            if method == 'POST' and path == '/api/memories': raise KeyboardInterrupt()
            return result
    with httpx.Client(base_url='https://isolated.test',transport=httpx.MockTransport(fixture)) as client:
        data = runner.evaluate(InterruptedAPI(client),tmp_path/'results.json',1,pause=lambda _:None,timeout=1)
    assert data['status'] == 'interrupted'
    assert fixture.memories['memory-0']['deleted']
    assert ('DELETE','/api/memories/memory-0') in fixture.requests


def test_interrupt_after_delete_receipt_reuses_operation_without_second_delete(tmp_path,monkeypatch):
    fixture = Fixture()
    case = deepcopy(build()[120]); monkeypatch.setattr(runner,'build',lambda:[case])
    class InterruptedAPI(runner.API):
        def __call__(self, method, path, **kwargs):
            result = super().__call__(method,path,**kwargs)
            if method == 'DELETE' and path.startswith('/api/memories/'): raise KeyboardInterrupt()
            return result
    with httpx.Client(base_url='https://isolated.test',transport=httpx.MockTransport(fixture)) as client:
        data = runner.evaluate(InterruptedAPI(client),tmp_path/'results.json',1,pause=lambda _:None,timeout=1)
    assert data['status'] == 'interrupted' and data['results'][0]['cleanup']['passed']
    assert fixture.requests.count(('DELETE','/api/memories/memory-0')) == 1


def test_no_replay_when_delete_response_has_no_trackable_operation_id(tmp_path,monkeypatch):
    fixture = Fixture('malformed_delete')
    # Exercise the explicit deletion phase before the final cleanup.
    case = deepcopy(build()[120]); monkeypatch.setattr(runner,'build',lambda:[case])
    data = perform(tmp_path,fixture)
    assert data['status'] == 'stopped' and not data['results'][0]['cleanup']['passed']
    assert fixture.requests.count(('DELETE','/api/memories/memory-0')) == 1


def test_source_checks_separate_normal_miss_from_stale_or_foreign_memory():
    case = build()[0]; memories = [{'id':'m','version':2}]
    assert runner.source_check(case,memories,{'memory_refs':[]}) == {
        'passed':True,'expected_retrieved':False,'reason':'没有召回预期依据'}
    assert not runner.source_check(case,memories,{'memory_refs':[{'id':'m','version':1}]})['passed']
    assert not runner.source_check(case,[],{'memory_refs':[{'id':'foreign','version':1}]})['passed']


def test_run_waits_for_terminal_reply_and_rejects_memory_outage_or_cancellation():
    states = iter([{'status':'queued'},{'status':'running'}, {'status':'completed','memory_status':'ready'}])
    assert runner.run_ready(lambda *args:next(states),'r',pause=lambda _:None)['status'] == 'completed'
    for value in ({'status':'cancelled'}, {'status':'completed','memory_status':'unavailable'}):
        with pytest.raises(RuntimeError): runner.run_ready(lambda *args:value,'r',pause=lambda _:None)
    with pytest.raises(TimeoutError): runner.run_ready(lambda *args:{'status':'running'},'r',timeout=0)


def test_existing_output_is_preserved_without_any_business_writes(tmp_path):
    fixture = Fixture(); target = tmp_path/'results.json'; target.write_text('previous evidence')
    with httpx.Client(base_url='https://isolated.test',transport=httpx.MockTransport(fixture)) as client:
        with pytest.raises(ValueError): runner.evaluate(runner.API(client),target,1)
    assert target.read_text() == 'previous evidence'
    assert all(method == 'GET' for method,_ in fixture.requests)


def test_duplicate_review_ids_cannot_inflate_reviewed_count():
    data = evidence(); reviews = reviewed(data); reviews['reviews'].append(reviews['reviews'][0])
    with pytest.raises(ValueError): scoring.summarize(data,reviews)


@pytest.mark.parametrize('url', ['http://remote.test','https://u:p@remote.test','https://remote.test/path','https://remote.test?token=secret'])
def test_runner_rejects_credential_leaks_and_insecure_remote_login(url):
    with pytest.raises(ValueError): runner.base_url(url)


def test_offline_cli_generates_bound_template_and_incomplete_json_markdown(tmp_path):
    raw, reviews = tmp_path/'raw.json', tmp_path/'reviews.json'
    scoring.save_json(raw,evidence())
    assert scoring.main(['prepare',str(raw),'--output',str(reviews)]) == 0
    assert scoring.main(['summarize',str(raw),'--reviews',str(reviews),'--json',str(tmp_path/'summary.json'),
        '--markdown',str(tmp_path/'summary.md')]) == 2
    assert '证据或复核未齐' in (tmp_path/'summary.md').read_text(encoding='utf-8')


def test_actual_local_http_login_csrf_collection_logout_and_offline_review(tmp_path,monkeypatch):
    """Real sockets; the HTTP fixture is not an actual model or Hindsight quality result."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    fixture = Fixture(); seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def dispatch(self):
            content = self.rfile.read(int(self.headers.get('Content-Length','0')))
            origin = 'http://127.0.0.1:'+str(self.server.server_port)
            assert self.headers.get('Origin') == origin
            if self.path == '/api/auth/login':
                assert json.loads(content) == {'username':'eval-lingxi','password':'fixture-password','code':''}
                result = httpx.Response(200,json=fixture.owner|{'csrf':'fixture-csrf'})
            else:
                assert self.headers.get('Cookie') == 'lingxi_session=fixture-cookie'
                if self.command != 'GET': assert self.headers.get('X-CSRF-Token') == 'fixture-csrf'
                if self.path == '/api/auth/logout': result = httpx.Response(200,json={'status':'logged_out'})
                else: result = fixture(httpx.Request(self.command,origin+self.path,content=content))
            seen.append(self.path)
            self.send_response(result.status_code)
            if self.path == '/api/auth/login': self.send_header('Set-Cookie','lingxi_session=fixture-cookie; Path=/; HttpOnly; SameSite=Strict')
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(result.content))); self.end_headers(); self.wfile.write(result.content)
        do_GET = do_POST = do_PATCH = do_DELETE = dispatch
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    passwords = iter(['fixture-password',''])
    monkeypatch.setattr(runner.getpass,'getpass',lambda _:next(passwords))
    target = tmp_path/'http-results.json'
    try:
        assert runner.main(['--allow-model-calls','--base-url','http://127.0.0.1:'+str(server.server_port),
            '--limit','1','--output',str(target)]) == 0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    assert seen[0] == '/api/auth/login' and seen[-1] == '/api/auth/logout'
    text = target.read_text(encoding='utf-8')
    assert 'fixture-password' not in text and 'fixture-cookie' not in text and 'fixture-csrf' not in text
    data = scoring.read_json(target); assert data['results'][0]['source_check']['passed']
    assert scoring.summarize(data,scoring.template(data))['status'] == 'incomplete'
