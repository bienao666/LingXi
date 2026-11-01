"""Synthetic protocol and authorization evidence; no real model quality claims."""
import json
import httpx
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from fastapi.testclient import TestClient
from lingxi import gateway, worker
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Conversation, Job, Model, ModelCall, Policy, Provider, User
from lingxi.model_routing import ModelRouting, select_chat
from lingxi.network import SystemProxy
from lingxi.security import encrypt


def candidates(*, cross=False, context=8192):
    with SessionLocal.begin() as db:
        if cross:
            db.add(Provider(id='backup-service', name='备用服务', base_url='https://backup.example/api',
                            encrypted_key=encrypt('DO-NOT-EXPOSE')))
        db.add(Model(id='backup', provider_id='backup-service' if cross else 'provider', name='backup-model',
            purpose='chat', context_window=context, status='verified', verified_provider_version=1))


def body(**changes):
    return {'version': 0, 'mode': 'automatic', 'candidate_ids': ['chat', 'backup'],
            'allow_fallback': True, 'cross_provider': False, 'minimum_context': 8192} | changes


def confirm(client, **changes):
    value = body(**changes)
    preview = client.post('/api/model-routing/preview', json=value)
    assert preview.status_code == 200, preview.text
    receipt = preview.json()
    saved = client.put('/api/model-routing', json=value | {k: receipt[k] for k in ('preview_digest', 'preview_expires_at')})
    assert saved.status_code == 200, saved.text
    return saved.json()


def submit(client, **changes):
    c = client.post('/api/conversations', json={'title': '路由测试'}).json()
    value = {'conversation_id': c['id'], 'request_id': 'routing-request-01', 'content': '你好'} | changes
    result = client.post('/api/chat', json=value)
    assert result.status_code == 200, result.text
    return result.json(), value


def process():
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status = 'completed'
    job = worker.claim()
    assert job and job.kind == 'chat'
    worker.perform(job)
    return job


def fixture(monkeypatch, responses):
    seen = []
    def handle(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload))
        response = responses[len(seen) - 1]
        if isinstance(response, Exception):
            raise response
        if type(response) is int:
            return httpx.Response(response, json={'error': {'secret': 'DO-NOT-EXPOSE'}})
        return httpx.Response(200, content=response, headers={'content-type': 'text/event-stream'})
    monkeypatch.setattr(gateway, 'client_for', lambda p: httpx.Client(
        base_url=p.base_url.rstrip('/')+'/', transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(worker.MemoryService, 'search', lambda *args: [])
    return seen


DONE = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'


def test_default_fixed_and_preview_never_enable_without_confirmation(configured):
    candidates()
    state = configured.get('/api/model-routing').json()
    assert state['version'] == 0 and state['mode'] == 'fixed' and not state['allow_fallback']
    preview = configured.post('/api/model-routing/preview', json=body())
    assert preview.status_code == 200
    assert 'secret-fixture-key' not in preview.text and 'DO-NOT-EXPOSE' not in preview.text
    assert configured.get('/api/model-routing').json()['version'] == 0
    run, value = submit(configured)
    assert 'routing' not in run['config'] and run['model_id'] == 'chat'
    assert configured.post('/api/chat', json=value).json()['id'] == run['id']


def test_preview_binds_endpoint_version_owner_expiry_and_form(configured):
    candidates()
    value = body()
    preview = configured.post('/api/model-routing/preview', json=value).json()
    receipt = {k: preview[k] for k in ('preview_digest', 'preview_expires_at')}
    assert configured.put('/api/model-routing', json=value | receipt | {'cross_provider': True}).status_code == 409
    assert configured.put('/api/model-routing', json=value | receipt | {'preview_expires_at': int(now())-1}).status_code == 409
    assert configured.put('/api/model-routing', json=value | receipt | {'preview_digest': '0'*64}).status_code == 409
    with SessionLocal.begin() as db:
        p = db.get(Provider, 'provider'); p.version += 1; p.base_url = 'https://changed.example'
    assert configured.put('/api/model-routing', json=value | receipt).status_code == 409


@pytest.mark.parametrize('changes', [
    {'candidate_ids': ['chat','chat']}, {'candidate_ids': ['backup','chat']},
    {'candidate_ids': ['chat']}, {'candidate_ids': ['chat','extraction']},
    {'minimum_context': 2047}, {'minimum_context': 2000001}, {'mode': 'smart'},
    {'allow_fallback': 'true'}, {'unknown': True}, {'version': -1},
])
def test_invalid_policy_never_installs(configured, changes):
    candidates()
    assert configured.post('/api/model-routing/preview', json=body(**changes)).status_code == 422
    assert configured.get('/api/model-routing').json()['version'] == 0


def test_cross_connection_requires_explicit_consent_and_lists_addresses(configured):
    candidates(cross=True)
    assert configured.post('/api/model-routing/preview', json=body()).status_code == 422
    saved = confirm(configured, cross_provider=True)
    assert [r['base_url'] for r in saved['recipients']] == ['https://example.com/v1', 'https://backup.example/api']
    assert saved['cross_provider']


def test_workspace_and_nonchat_candidates_fail_closed(configured):
    candidates()
    with SessionLocal.begin() as db:
        db.get(Model, 'backup').workspace_id = 'other'
    assert configured.post('/api/model-routing/preview', json=body()).status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/model-routing').status_code == 401
        assert anonymous.post('/api/model-routing/preview', json=body()).status_code == 401
    assert configured.put('/api/model-routing', json=body()).status_code == 422


def test_save_version_conflict_and_disabling_route_clears_recipients(configured):
    candidates(); saved = confirm(configured)
    assert saved['version'] == 1
    assert configured.post('/api/model-routing/preview', json=body()).status_code == 409
    state = confirm(configured, version=1, mode='fixed', allow_fallback=False, candidate_ids=[])
    assert state['recipients'] == [] and state['version'] == 2
    run, _ = submit(configured)
    assert 'routing' not in run['config']


@pytest.mark.parametrize('mutation', ['provider', 'model', 'policy', 'proxy', 'workspace'])
def test_changed_authorization_cannot_silently_return_to_fixed(configured, mutation):
    candidates(); confirm(configured)
    with SessionLocal.begin() as db:
        if mutation == 'provider': db.get(Provider, 'provider').version += 1
        elif mutation == 'model': db.get(Model, 'backup').name = 'unapproved'
        elif mutation == 'policy': db.get(Policy, 'chat').version += 1
        elif mutation == 'workspace': db.get(Model, 'backup').workspace_id = 'other'
        else: db.add(SystemProxy(id='system', encrypted_config=encrypt('{}')))
    assert configured.get('/api/model-routing').json()['current'] is False
    conversation = configured.post('/api/conversations', json={'title':'变更'}).json()['id']
    assert configured.post('/api/chat', json={'conversation_id':conversation, 'request_id':'changed-route', 'content':'你好'}).status_code == 409
    # Disabling does not require the old endpoint to work.
    confirm(configured, version=1, mode='fixed', candidate_ids=[], allow_fallback=False)


def test_automatic_selection_filters_context_and_keeps_fixed_policy(configured):
    candidates(context=32768); confirm(configured, minimum_context=16000)
    run, value = submit(configured, content='较长的问题')
    assert run['model_id'] == 'backup'
    assert len(run['config']['routing']['candidates']) == 1
    assert configured.get('/api/model-policies').json()[0]['model_id'] == 'chat'
    assert configured.post('/api/chat', json=value).json()['id'] == run['id']


def test_fixed_mode_does_not_silently_select_backup_on_capability_failure(configured):
    candidates(context=32768); confirm(configured, mode='fixed', minimum_context=16000)
    c = configured.post('/api/conversations', json={'title':'固定'}).json()['id']
    assert configured.post('/api/chat', json={'conversation_id':c,'request_id':'fixed-insufficient','content':'你好'}).status_code == 409


def test_vision_filters_selection_and_no_smaller_context_fallback(configured):
    from lingxi.media import Media, MediaLink, VisionCapability
    from lingxi.models import Message
    candidates(context=16384); confirm(configured)
    with SessionLocal.begin() as db:
        conversation=Conversation(user_id='owner'); db.add(conversation); db.flush()
        message=Message(conversation_id=conversation.id,role='user',content='图');db.add(message);db.flush()
        image=Media(id='image',user_id='owner',request_id='routing-image-request',filename='a.png',suffix='.png',size=100,digest='x',status='ready',parsed={'kind':'image'})
        db.add(image);db.add(MediaLink(media_id=image.id,message_id=message.id))
        db.add(VisionCapability(model_id='backup',status='verified',provider_version=1,model_name='backup-model'))
        db.flush()
        result=select_chat(db,db.get(User,'owner'),conversation,'图片',[])
        assert result[0].id=='backup' and result[3]['requirement']['vision']
        assert len(result[3]['candidates'])==1


@pytest.mark.parametrize('status', [429,502,503,504])
def test_pretext_fallback_pins_actual_model_and_calls_once_each(configured, monkeypatch, status):
    candidates(cross=True);confirm(configured,cross_provider=True)
    seen=fixture(monkeypatch,[status,DONE]);run,_=submit(configured)
    job=process()
    result=configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status']=='completed' and result['model_id']=='backup'
    assert [a['state'] for a in result['config']['routing']['attempts']]==['failed','completed']
    assert len(seen)==2 and seen[0][1]['messages']==seen[1][1]['messages']
    assert seen[0][1]['model']=='fixture-model' and seen[1][1]['model']=='backup-model'
    with SessionLocal() as db:
        logs=list(db.scalars(select(ModelCall).where(ModelCall.run_id==run['id']).order_by(ModelCall.created_at)))
        assert [r.model_id for r in logs]==['chat','backup']
        assert [r.status for r in logs]==['failed','completed']
        assert 'DO-NOT-EXPOSE' not in json.dumps([r.config for r in logs])
    worker.generate(job)
    assert len(seen)==2


@pytest.mark.parametrize('status', [400,401,403,404,422,500])
def test_auth_input_and_unclassified_errors_never_fallback(configured,monkeypatch,status):
    candidates();confirm(configured);seen=fixture(monkeypatch,[status]);run,_=submit(configured);process()
    result=configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status']=='failed' and len(seen)==1
    assert 'DO-NOT-EXPOSE' not in result['error']


def test_partial_stream_and_whitespace_are_not_replayed(configured,monkeypatch):
    candidates();confirm(configured)
    seen=fixture(monkeypatch,[b'data: {"choices":[{"delta":{"content":" "}}]}\n\n'])
    run,_=submit(configured);process()
    assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='failed'
    assert len(seen)==1


def test_fallback_bounded_at_two_attempts(configured,monkeypatch):
    candidates();confirm(configured);seen=fixture(monkeypatch,[503,429]);run,_=submit(configured);process()
    result=configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status']=='failed' and len(seen)==2
    assert len(result['config']['routing']['attempts'])==2


@pytest.mark.parametrize('kind',['anthropic','gemini'])
def test_cross_protocol_fallback_preserves_content_and_actual_protocol(configured,monkeypatch,kind):
    from lingxi.providers import ProviderProtocol
    candidates(cross=True)
    with SessionLocal.begin() as db:
        db.add(ProviderProtocol(provider_id='backup-service',protocol=kind))
    confirm(configured,cross_provider=True)
    rows=([{'type':'message_start','message':{'usage':{'input_tokens':3}}},
           {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
           {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'OK'}},
           {'type':'content_block_stop','index':0},
           {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':2}},
           {'type':'message_stop'}] if kind=='anthropic' else
          [{'candidates':[{'content':{'parts':[{'text':'OK'}]},'finishReason':'STOP'}]}])
    data=''.join('data: '+json.dumps(r)+'\n\n' for r in rows).encode()
    seen=fixture(monkeypatch,[503,data]);run,_=submit(configured);process()
    result=configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status']=='completed' and result['model_id']=='backup'
    assert len(seen)==2
    assert seen[1][0].endswith('/messages' if kind=='anthropic' else '/backup-model:streamGenerateContent?alt=sse')
    with SessionLocal() as db:
        log=db.scalar(select(ModelCall).where(ModelCall.model_id=='backup'))
        assert log.config['provider_protocol']==kind


def test_deleted_memory_between_failed_and_backup_request_is_not_reused(configured,monkeypatch):
    from lingxi.models import Memory
    from lingxi.memory import MemoryService
    candidates();confirm(configured)
    with SessionLocal.begin() as db:
        memory=Memory(user_id='owner',content='我喜欢喝茶',category='preferences',index_status='ready');db.add(memory);db.flush();identity=memory.id
    def retrieve(self,db,*args):return [db.get(Memory,identity)]
    monkeypatch.setattr(worker.MemoryService,'search',retrieve)
    seen=[]
    def stream(model,provider,messages,run_id,check,config=None):
        seen.append(model.id)
        with SessionLocal.begin() as db:
            MemoryService().forget(db,db.get(Memory,identity))
        raise httpx.ReadTimeout('unknown')
        yield ''
    monkeypatch.setattr(worker,'stream',stream);run,_=submit(configured);process()
    assert seen==['chat']
    assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='invalidated'


@pytest.mark.parametrize('exc', [httpx.ConnectError('secret'),httpx.ConnectTimeout('secret'),httpx.ReadTimeout('secret'),httpx.RemoteProtocolError('secret')])
def test_pretext_network_failure_is_bounded(configured,monkeypatch,exc):
    candidates();confirm(configured);seen=fixture(monkeypatch,[exc,DONE]);run,_=submit(configured);process()
    assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='completed'
    assert len(seen)==2


@pytest.mark.parametrize('change', ['cancel','delete','route','provider','lease'])
def test_midfailure_invalidation_prevents_backup_request(configured,monkeypatch,change):
    candidates();confirm(configured)
    seen=[]
    def stream(model,provider,messages,run_id,check,config=None):
        seen.append(model.id)
        with SessionLocal.begin() as db:
            if change=='cancel': db.get(ChatRun,run_id).cancel_requested=True
            elif change=='delete': db.get(Conversation,db.get(ChatRun,run_id).conversation_id).deleted=True;db.get(ChatRun,run_id).status='invalidated'
            elif change=='route': db.get(ModelRouting,'personal').version+=1
            elif change=='provider': db.get(Provider,'provider').version+=1
            else: db.scalar(select(Job).where(Job.kind=='chat')).lease_token='lost'
        raise httpx.ReadTimeout('unknown')
        yield ''
    monkeypatch.setattr(worker,'stream',stream);monkeypatch.setattr(worker.MemoryService,'search',lambda *args:[])
    run,_=submit(configured);process()
    assert seen==['chat']


def test_running_recovery_never_restarts_call_or_dispatch(configured,monkeypatch):
    candidates();confirm(configured);run,_=submit(configured)
    with SessionLocal.begin() as db:
        db.get(ChatRun,run['id']).status='running'
    seen=fixture(monkeypatch,[DONE]);process()
    assert not seen


def test_persona_override_and_task_flows_bypass_routing(configured):
    candidates();confirm(configured)
    with SessionLocal.begin() as db:
        conversation=Conversation(user_id='owner');db.add(conversation);db.flush();user=db.get(User,'owner')
        assert select_chat(db,user,conversation,'你好',[],explicit_id='backup')[0].id=='backup'
        assert select_chat(db,user,conversation,'你好',[],explicit_id='backup')[3] is None
        assert select_chat(db,user,conversation,'你好',[],assisted=True)[3] is None
        assert gateway.resolve_model(db,'extraction')[0].id=='extraction'


def test_fallback_reuses_context_without_dispatch_or_compaction_replay(configured,monkeypatch):
    import lingxi.chat_actions as actions
    import lingxi.conversation_context as context
    candidates();confirm(configured)
    dispatches=[];compactions=[]
    original=actions.dispatch
    def dispatch(*args):dispatches.append(1);return original(*args)
    def compact(*args):compactions.append(1);return None
    monkeypatch.setattr(actions,'dispatch',dispatch);monkeypatch.setattr(context,'compact',compact)
    seen=fixture(monkeypatch,[503,DONE]);run,_=submit(configured);process()
    assert configured.get('/api/chat-runs/'+run['id']).json()['status']=='completed'
    assert len(seen)==2 and dispatches==[1] and compactions==[1]


def test_channel_actor_uses_only_owner_routing_not_owner_memory(configured):
    from lingxi.api import submit_chat
    from lingxi.media import MediaChatInput
    candidates();confirm(configured)
    with SessionLocal.begin() as db:
        actor=User(username='channel-actor',password_hash='');db.add(actor);db.flush()
        conversation=Conversation(user_id=actor.id);db.add(conversation);db.flush()
        result=submit_chat(MediaChatInput(conversation_id=conversation.id,request_id='qq-route-request',content='你好'),
            (db,actor,None),channel_context={'owner_id':'owner','memory_allowed':False})
        assert result['config']['routing']['user_id']=='owner'
        assert result['config']['routing']['subject_user_id']==actor.id
        assert result['user_id']==actor.id and not result['config']['channel']['memory_allowed']
