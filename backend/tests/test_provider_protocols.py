"""Native protocol contracts; synthetic responses do not certify provider/model quality."""
import json
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi import api, catalog, gateway, protocols
from lingxi.api import app
from lingxi.config import settings
from lingxi.db import SessionLocal
from lingxi.models import Memory, Model, Policy, Provider, ModelCall
from lingxi.providers import ProviderProtocol, bind_protocol, protocol_for


def native(kind):
    with SessionLocal.begin() as db:
        db.merge(ProviderProtocol(provider_id='provider', protocol=kind))
        p = db.get(Provider, 'provider')
        p.base_url = 'https://fixture.example/native'


def transport(monkeypatch, handler, module=gateway):
    monkeypatch.setattr(module, 'client_for', lambda p: httpx.Client(
        base_url=p.base_url+'/', transport=httpx.MockTransport(handler)))


def answer(kind, text='你好', **updates):
    if kind == 'anthropic':
        return {'content': [{'type': 'thinking', 'thinking': 'PRIVATE'}, {'type': 'text', 'text': text}],
                'stop_reason': 'end_turn', 'usage': {'input_tokens': 3, 'output_tokens': 2}, **updates}
    return {'candidates': [{'content': {'parts': [{'thought': True, 'text': 'PRIVATE'}, {'text': text}]},
                            'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 2, 'thoughtsTokenCount': 1, 'totalTokenCount': 6}, **updates}


def stream_data(kind, text='你好'):
    if kind == 'anthropic':
        return [{'type': 'message_start', 'message': {'usage': {'input_tokens': 3}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'PRIVATE'}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': text}},
                {'type': 'content_block_stop', 'index': 1},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}},
                {'type': 'message_stop'}]
    return [{'candidates': [{'content': {'parts': [{'thought': True, 'text': 'PRIVATE'}]}}]}, answer(kind, text)]


def sse(rows, terminated=True):
    text = ''.join('event: ignored\ndata: '+json.dumps(row, ensure_ascii=False)+'\n\n' for row in rows)
    return text if terminated else text.rstrip('\n')


@pytest.mark.parametrize('kind', ['anthropic', 'gemini'])
def test_protocol_configuration_enforces_auth_version_and_new_key(configured, kind):
    assert configured.get('/api/providers').json()[0]['protocol'] == 'openai'
    body = {'name': 'Native', 'base_url': 'https://example.com/v1', 'version': 1, 'protocol': kind}
    assert configured.patch('/api/providers/provider', json=body).status_code == 422
    with TestClient(app) as anon:
        assert anon.patch('/api/providers/provider', json=body).status_code == 401
    response = configured.patch('/api/providers/provider', json=body | {'api_key': 'native-secret'})
    assert response.status_code == 200, response.text
    assert response.json()['protocol'] == kind and response.json()['version'] == 2
    assert 'native-secret' not in response.text
    assert configured.patch('/api/providers/provider', json=body).status_code == 409
    with SessionLocal() as db:
        assert db.get(Model, 'chat').status == 'unverified'
        assert protocol_for(db.get(Provider, 'provider'), db) == kind
    # Old clients omitting the new field preserve the native selection.
    response = configured.patch('/api/providers/provider', json={k:v for k,v in (body | {'version':2}).items() if k!='protocol'})
    assert response.json()['protocol'] == kind


def test_existing_embedding_index_cannot_change_protocol(configured):
    with SessionLocal.begin() as db:
        db.add(Model(id='embed', provider_id='provider', name='embed', purpose='embedding'))
        db.add(Policy(purpose='embedding', model_id='embed'))
        db.add(Memory(user_id='owner', content='现有记忆'))
    response = configured.patch('/api/providers/provider', json={'name':'Native', 'base_url':'https://example.com/v1',
        'protocol':'gemini', 'version':1, 'api_key':'new-key'})
    assert response.status_code == 409


@pytest.mark.parametrize('kind,purpose', [('anthropic','embedding'),('anthropic','rerank'),('gemini','rerank')])
def test_unsupported_purposes_rejected_before_model_creation(configured, kind, purpose):
    native(kind)
    response = configured.post('/api/models', json={'provider_id':'provider','name':'native-model','purpose':purpose,'context_window':8192})
    assert response.status_code == 422


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_transport_credentials_are_headers_only_and_reuse_policy(configured, monkeypatch, kind):
    native(kind); seen = []
    def outbound(base, **kwargs):
        seen.append((base, kwargs)); return object()
    monkeypatch.setattr(gateway, 'outbound_client', outbound)
    with SessionLocal() as db:
        gateway.client_for(bind_protocol(db.get(Provider,'provider'),db))
    base, args = seen[0]
    assert base == 'https://fixture.example/native'
    assert args['allow_private'] is False
    expected = 'x-api-key' if kind=='anthropic' else 'x-goog-api-key'
    assert args['headers'][expected] == 'secret-fixture-key'
    assert 'Authorization' not in args['headers'] and 'secret' not in base


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_catalog_reads_all_pages_without_changing_api_root(configured, monkeypatch, kind):
    native(kind); seen=[]
    def handler(req):
        seen.append(req)
        if kind=='anthropic':
            return httpx.Response(200,json={'data':[{'id':'b' if len(seen)==1 else 'a'}], 'has_more':len(seen)==1, 'last_id':'b'})
        return httpx.Response(200,json={'models':[{'name':'models/b' if len(seen)==1 else 'models/a',
            'supportedGenerationMethods':['generateContent'] if len(seen)==1 else ['embedContent']}],
            **({'nextPageToken':'opaque+/='} if len(seen)==1 else {})})
    transport(monkeypatch,handler,catalog)
    result=configured.get('/api/providers/provider/models')
    assert result.status_code==200, result.text
    assert len(result.json()['models'])==2 and len(seen)==2
    assert all(r.url.path=='/native/models' for r in seen)
    assert seen[1].url.params['after_id' if kind=='anthropic' else 'pageToken']==('b' if kind=='anthropic' else 'opaque+/=')
    assert all('purposes' in m for m in result.json()['models'])


@pytest.mark.parametrize('data', [
    {'data':[{'id':'https://evil/model'}]}, {'data':[{'id':'x'}],'has_more':True},
    {'data':[{'id':'x'}],'has_more':True,'last_id':'loop'}, {'data':'bad'},
])
def test_native_catalog_invalid_or_looped_pages_never_return_partial(configured, monkeypatch, data):
    native('anthropic'); transport(monkeypatch,lambda r:httpx.Response(200,json=data),catalog)
    assert configured.get('/api/providers/provider/models').status_code==502


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_message_mapping_preserves_system_turns_images_and_strict_schema(kind):
    schema={'type':'object','properties':{'fact':{'type':'string'}},'required':['fact'],'additionalProperties':False}
    route,payload=protocols.chat_request(kind,'models/native-model',[
        {'role':'system','content':'规则一'}, {'role':'developer','content':'规则二'},
        {'role':'user','content':'文字'}, {'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,YQ=='}}]},
    ],options={'response_format':{'type':'json_schema','json_schema':{'name':'memory','strict':True,'schema':schema}}})
    if kind=='anthropic':
        assert route=='messages' and payload['model']=='native-model'
        assert payload['system']=='规则一\n\n规则二'
        assert len(payload['messages'])==1 and payload['messages'][0]['content'][1]['type']=='image'
        assert payload['output_config']['format']['schema']==schema
    else:
        assert route=='models/native-model:generateContent'
        assert payload['generationConfig']['responseJsonSchema']==schema
        assert len(payload['contents'][0]['parts'])==2


@pytest.mark.parametrize('options', [{'tools':[]},{'seed':1},{'reasoning_effort':'low'}, {'response_format':{'type':'json_schema'}}])
def test_unadapted_options_fail_explicitly(options):
    with pytest.raises(HTTPException) as exc:
        protocols.chat_request('gemini','native',[{'role':'user','content':'hi'}],options=options)
    assert exc.value.status_code==422


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_native_complete_and_stream_hide_private_reasoning_and_keep_usage(kind):
    result=protocols.completion(kind,answer(kind))
    assert result['choices'][0]['message']['content']=='你好' and 'PRIVATE' not in str(result)
    assert result['usage']['total_tokens']==(5 if kind=='anthropic' else 6)
    checks=[]
    response=httpx.Response(200,content=sse(stream_data(kind)),request=httpx.Request('POST','https://fixture/'))
    chunks=list(protocols.stream_chunks(kind,response,lambda:checks.append(1)))
    assert ''.join(text or '' for text,_ in chunks)=='你好' and checks
    assert [u for _,u in chunks if u][-1]['total_tokens']==(5 if kind=='anthropic' else 6)


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_stream_disconnect_and_unterminated_event_fail(kind):
    for content in [sse(stream_data(kind)[:-1]),sse(stream_data(kind),terminated=False)]:
        with pytest.raises(HTTPException):
            list(protocols.stream_chunks(kind,httpx.Response(200,content=content,request=httpx.Request('POST','https://fixture/')),lambda:None))


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_stream_checks_cancellation_before_consuming_more_content(kind):
    class Cancelled(Exception):pass
    def check():raise Cancelled()
    with pytest.raises(Cancelled):
        list(protocols.stream_chunks(kind,httpx.Response(200,content=sse(stream_data(kind)),request=httpx.Request('POST','https://fixture/')),check))


@pytest.mark.parametrize('reason', ['max_tokens','tool_use','refusal',None])
def test_incomplete_or_tool_answers_are_not_success(reason):
    with pytest.raises(HTTPException):protocols.completion('anthropic',answer('anthropic',stop_reason=reason))


def test_thinking_block_cannot_disguise_text_delta():
    rows=stream_data('anthropic');rows[2]['delta']={'type':'text_delta','text':'PRIVATE'}
    with pytest.raises(HTTPException):
        list(protocols.stream_chunks('anthropic',httpx.Response(200,content=sse(rows),request=httpx.Request('POST','https://fixture/')),lambda:None))


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_private_memory_gateway_translates_strict_schema_and_fixed_model(configured, monkeypatch, kind):
    native(kind);monkeypatch.setattr(settings(),'memory_gateway_token','fixture-token');seen=[]
    def handler(req):
        seen.append((req.url.path,json.loads(req.content)));return httpx.Response(200,json=answer(kind,'{"items":[]}'))
    transport(monkeypatch,handler,api)
    result=configured.post('/internal/model/v1/chat/completions',headers={'Authorization':'Bearer fixture-token'},json={
        'model':'untrusted-model','messages':[{'role':'user','content':'提取'}],'max_completion_tokens':500,
        'response_format':{'type':'json_schema','json_schema':{'name':'memory','strict':True,'schema':{'type':'object'}}}})
    assert result.status_code==200,result.text
    assert result.json()['choices'][0]['message']['content']=='{"items":[]}'
    assert 'untrusted-model' not in str(seen) and 'fixture-model' in str(seen)
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall));assert call.config['provider_protocol']==kind and call.status=='completed'


def test_gemini_embedding_batch_maps_dimensions_and_order(configured,monkeypatch):
    native('gemini');seen=[]
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200,json={'embeddings':[{'values':[v]*settings().memory_embedding_dimensions} for v in (0.1,0.2)]})
    with SessionLocal() as db:
        p=bind_protocol(db.get(Provider,'provider'),db)
        with httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(handler)) as client:
            data=gateway.compatible_json(client,p,'embeddings',{'model':'models/embed','input':['一','二']})
    assert [r['index'] for r in data['data']]==[0,1]
    assert [r['embedding'][0] for r in data['data']]==[0.1,0.2]
    assert seen[0]['requests'][0]['embedContentConfig']=={'outputDimensionality':1024,'autoTruncate':False}
    assert 'usage' not in data


@pytest.mark.parametrize('vector', [[0]*1024,[float('nan')]*1024,[0.1]*3,None])
def test_gemini_embedding_rejects_bad_vectors(configured,vector):
    native('gemini')
    # NaN is intentionally malformed upstream JSON, encoded manually.
    with SessionLocal() as db:
        p=bind_protocol(db.get(Provider,'provider'),db)
        with httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda r:httpx.Response(200,content=json.dumps({'embeddings':[{'values':vector}]})))) as client:
            with pytest.raises(HTTPException):gateway.compatible_json(client,p,'embeddings',{'model':'embed','input':'一'})


@pytest.mark.parametrize('kind', ['anthropic','gemini'])
def test_real_http_native_catalog_validation_and_durable_chat(configured, monkeypatch, kind):
    """Real TCP/HTTP, auth headers and worker persistence; no provider/model quality claim."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from lingxi import worker
    from lingxi.memory import MemoryService
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            data={'data':[{'id':'fixture-model'}]} if kind=='anthropic' else {'models':[{'name':'models/fixture-model','supportedGenerationMethods':['generateContent']}]}
            self.reply(json.dumps(data).encode())
        def reply(self,body,mime='application/json'):
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path,dict(self.headers),body))
            assert self.headers['x-api-key' if kind=='anthropic' else 'x-goog-api-key']=='secret-fixture-key'
            self.reply(sse(stream_data(kind,'HTTP 协议测试回复')).encode(),'text/event-stream')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        native(kind)
        with SessionLocal.begin() as db:
            p=db.get(Provider,'provider');p.base_url=f'http://127.0.0.1:{server.server_port}/native';p.allow_private=True
        result=configured.get('/api/providers/provider/models')
        assert result.status_code==200,result.text
        assert configured.post('/api/models/chat/test').status_code==200
        monkeypatch.setattr(MemoryService,'search',lambda *a:[])
        conversation=configured.post('/api/conversations',json={'title':'原生 HTTP 测试'}).json()['id']
        payload={'conversation_id':conversation,'content':'你好','request_id':'native-http-request'}
        run=configured.post('/api/chat',json=payload).json()
        assert configured.post('/api/chat',json=payload).json()['id']==run['id']
        job=worker.claim();assert job.kind=='chat';worker.perform(job)
        assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='completed'
        assert configured.get('/api/messages',params={'conversation_id':conversation}).json()[-1]['content']=='HTTP 协议测试回复'
        assert len(requests)==2
        assert all('key=' not in path and 'secret' not in path for path,_,_ in requests)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_provider_change_fences_queued_generation(configured):
    from lingxi import worker
    conversation=configured.post('/api/conversations',json={'title':'配置变更'}).json()['id']
    run=configured.post('/api/chat',json={'conversation_id':conversation,'content':'你好','request_id':'changed-provider'}).json()
    native('gemini')
    with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    job=worker.claim();worker.perform(job)
    assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='failed'


def test_gemini_embedding_supports_hindsight_sdk_base64_encoding(configured):
    import base64,struct
    native('gemini')
    with SessionLocal() as db:
        p=bind_protocol(db.get(Provider,'provider'),db)
        with httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'embeddings':[{'values':[0.25]*1024}]}))) as client:
            data=gateway.compatible_json(client,p,'embeddings',{'model':'embed','input':['一'],'encoding_format':'base64'})
    assert struct.unpack('<1024f',base64.b64decode(data['data'][0]['embedding']))==(0.25,)*1024


def test_gemini_embedding_usage_uses_only_real_metadata(configured):
    native('gemini')
    with SessionLocal() as db:
        p=bind_protocol(db.get(Provider,'provider'),db)
        with httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
                'embeddings':[{'values':[0.25]*1024}],'usageMetadata':{'promptTokenCount':7}}))) as client:
            data=gateway.compatible_json(client,p,'embeddings',{'model':'embed','input':['一']})
    assert data['usage']=={'prompt_tokens':7,'total_tokens':7}
