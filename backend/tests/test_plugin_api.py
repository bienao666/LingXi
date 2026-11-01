"""API adapter confirmation, send fencing and credential isolation."""
import copy
import json
from contextlib import contextmanager
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Job
from lingxi.plugin_api import PluginConnection, input_value
from lingxi.plugin_manifest import Manifest, ApiEntry, digest
from lingxi.plugins import PluginInvocation, PluginRelease, PluginInstallation
from lingxi.worker import claim, perform
from test_plugins import release, add, install, action
from test_plugin_store import shop

SECRET = 'synthetic-api-credential'


def api_release(**changes):
    row = release('sample.api')
    entry = dict(url='http://127.0.0.1:9191/action', method='POST', effect='write', auth='bearer',
        parameters=[{'name':'title','type':'string'}], health_url='http://127.0.0.1:9191/health',
        idempotency_header='Idempotency-Key') | changes
    row.manifest = Manifest(**(row.manifest | {'capabilities':[dict(id='sample.request', name='远程请求',
        kind='api', permissions=['network.'+entry['effect']], api=entry)]})).model_dump()
    row.digest = digest(row.manifest)
    return row


def setup(client, **changes):
    add(api_release(**changes)); row = install(client, plugin='sample.api')
    result = client.post('/api/plugin-connections', json={'installation_id':row['id'],
        'capability_id':'sample.request', 'secret':SECRET if changes.get('auth')!='none' else '',
        'allow_private':True, 'acknowledged':True})
    assert result.status_code == 200, result.text
    return row, result.json()


def preview(client, row, request='api-request-001', content='{"title":"合成资料"}'):
    return client.post('/api/plugin-invocations/preview', json={'installation_id':row['id'],
        'capability_id':'sample.request', 'request_id':request, 'content':content})


def confirm(client, p):
    return client.post('/api/plugin-invocations/'+p['id']+'/confirm', json={'digest':p['digest']})


def current(client):
    return client.get('/api/plugin-invocations').json()[0]


def transport(monkeypatch, handler):
    calls=[]
    def send(request):
        calls.append(request)
        return handler(request)
    @contextmanager
    def client(*args, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(send), headers=kwargs.get('headers',{}),
                          follow_redirects=False) as conn: yield conn
    monkeypatch.setattr('lingxi.network.outbound_client',client)
    monkeypatch.setattr('lingxi.plugin_api.outbound_client',client)
    return calls


def test_legacy_manifest_roundtrip_preserves_digest():
    old=release('sample.skill').manifest
    assert 'api' not in old['capabilities'][0]
    assert digest(Manifest(**copy.deepcopy(old)).model_dump()) == digest(old)


@pytest.mark.parametrize('change',[
    {'url':'https://user:pass@example.com/a'}, {'url':'https://example.com/{id}'},
    {'url':'https://example.com/a?token=x'}, {'url':'https://example.com/a#x'},
    {'url':'file:///etc/passwd'}, {'method':'POST','effect':'read'},
    {'auth':'api_key','key_header':'Host'}, {'idempotency_header':'Authorization'},
    {'idempotency_header':'X-API-Key'}, {'health_url':'https://other.example/health'},
    {'parameters':[{'name':'x','type':'string'},{'name':'x','type':'string'}]},
    {'script':'print(1)'}])
def test_api_contract_rejects_unsafe_declarations(change):
    with pytest.raises(ValueError):ApiEntry(**({'url':'https://example.com/action','method':'GET','effect':'read'}|change))


@pytest.mark.parametrize('content',['[]','{"title":1}','{"title":"x","other":1}',
    '{}','{"title":"x","title":"y"}','{"title":NaN}'])
def test_input_contract_rejects_ambiguous_values(content):
    with pytest.raises(HTTPException):input_value(content,api_release().manifest['capabilities'][0]['api'])


def test_connection_encrypted_versioned_and_scoped(client):
    row,c=setup(client)
    assert c['has_secret'] and SECRET not in json.dumps(c)
    with SessionLocal() as db:
        encrypted=db.get(PluginConnection,c['id']).encrypted_secret
        assert encrypted and SECRET not in encrypted
    body={'installation_id':row['id'],'capability_id':'sample.request','version':c['version'],
          'acknowledged':True,'allow_private':True}
    changed=client.put('/api/plugin-connections/'+c['id'],json=body)
    assert changed.status_code==200 and changed.json()['has_secret']
    assert client.put('/api/plugin-connections/'+c['id'],json=body).status_code==409
    with SessionLocal.begin() as db:db.get(PluginConnection,c['id']).workspace_id='other'
    assert client.get('/api/plugin-connections').json()==[]
    assert client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':2}).status_code==404


def test_private_requires_explicit_allow_and_auth_csrf(client):
    add(api_release());row=install(client,plugin='sample.api')
    body={'installation_id':row['id'],'capability_id':'sample.request','secret':SECRET,'acknowledged':True}
    assert client.post('/api/plugin-connections',json=body).status_code==422
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/plugin-connections',json=body).status_code==403
    client.headers['X-CSRF-Token']=token
    with TestClient(app) as anonymous: assert anonymous.get('/api/plugin-connections').status_code==401


def test_invalid_connection_never_echoes_credentials(client):
    response=client.post('/api/plugin-connections',json={'installation_id':'x','capability_id':'sample.request',
        'secret':SECRET*300,'acknowledged':True,'unknown':SECRET})
    assert response.status_code==422 and SECRET not in response.text


def test_preview_never_sends_and_confirm_is_idempotent(client,monkeypatch):
    row,_=setup(client);calls=transport(monkeypatch,lambda r:httpx.Response(200,json={'ok':True}))
    p=preview(client,row).json();assert p==preview(client,row).json() and calls==[]
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='plugin'))==0
    assert p['plan']['url'].endswith('/action') and p['plan']['effect']=='write'
    one=confirm(client,p);assert one.status_code==200 and confirm(client,p).json()==one.json()
    perform(claim());r=current(client)
    assert r['status']=='succeeded' and r['receipt']['verification']=='http_2xx_json' and len(calls)==1
    assert calls[0].headers['authorization']=='Bearer '+SECRET
    assert calls[0].headers['idempotency-key']==p['id'] and json.loads(calls[0].content)['title']=='合成资料'
    assert SECRET not in json.dumps(r)


@pytest.mark.parametrize('change',['expired','input','digest','session','connection','disable','cancel'])
def test_invalid_preview_never_schedules(client,change):
    row,c=setup(client);p=preview(client,row).json()
    if change=='connection':client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':1})
    elif change=='disable':action(client,row,'disable')
    elif change=='cancel':client.post('/api/plugin-invocations/'+p['id']+'/cancel',json={'version':p['version']})
    else:
        with SessionLocal.begin() as db:
            invocation=db.get(PluginInvocation,p['id']);config=copy.deepcopy(invocation.config)
            if change=='expired':config['api_preview']['expires_at']=now()-1
            elif change=='session':config['api_preview']['login_session_id']='other-login'
            elif change=='input':invocation.input='{"title":"changed"}'
            else:p['digest']='0'*64
            invocation.config=config
    assert confirm(client,p).status_code==409
    with SessionLocal() as db:assert not db.scalar(select(Job).where(Job.kind=='plugin'))


@pytest.mark.parametrize('failure',['timeout','redirect','error','invalid-json','huge','empty'])
def test_sent_failure_is_unknown_not_retried(client,monkeypatch,failure):
    row,_=setup(client)
    def result(request):
        if failure=='timeout':raise httpx.ReadTimeout('do not expose '+SECRET)
        if failure=='redirect':return httpx.Response(302,headers={'Location':'http://untrusted.example'})
        if failure=='error':return httpx.Response(500,text=SECRET)
        return httpx.Response(200,content={'invalid-json':b'not json','huge':b'x'*270000,'empty':b''}[failure])
    calls=transport(monkeypatch,result);p=preview(client,row).json();confirm(client,p);perform(claim())
    r=current(client);assert r['status']=='unknown' and r['result']=='' and len(calls)==1
    assert SECRET not in json.dumps(r) and claim() is None
    assert preview(client,row,'api-second-request').status_code==409
    resolved=client.post('/api/plugin-invocations/'+r['id']+'/resolve',json={'version':r['version'],
        'outcome':'not_applied','evidence':'已在合成服务查询，未发现对应记录','acknowledged':True})
    assert resolved.status_code==200 and resolved.json()['status']=='resolved' and len(calls)==1


@pytest.mark.parametrize('change',['cancel','revoke','disable','uninstall','release'])
def test_revocation_during_send_retains_receipt_discards_body(client,monkeypatch,change):
    row,c=setup(client);p=preview(client,row).json();confirm(client,p)
    def result(request):
        if change=='cancel':
            r=current(client);assert client.post('/api/plugin-invocations/'+p['id']+'/cancel',json={'version':r['version']}).status_code==200
        elif change=='revoke':assert client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':1}).status_code==200
        elif change=='release':
            with SessionLocal.begin() as db:db.get(PluginRelease,row['release_id']).status='revoked'
        else:assert action(client,row,change).status_code==200
        return httpx.Response(200,json={'private':'must not publish'})
    calls=transport(monkeypatch,result);perform(claim());r=current(client)
    assert r['status']=='unknown' and r['result']=='' and r['receipt']['http_status']==200 and len(calls)==1


def test_response_redacts_credential_in_values_and_keys(client,monkeypatch):
    row,_=setup(client);transport(monkeypatch,lambda r:httpx.Response(200,json={SECRET:SECRET,'token':'hidden','data':'x'+SECRET}))
    confirm(client,preview(client,row).json());perform(claim())
    r=current(client);assert r['status']=='succeeded' and SECRET not in json.dumps(r) and 'hidden' not in r['result']


def test_two_previews_cannot_bypass_unknown_barrier(client,monkeypatch):
    row,_=setup(client);a=preview(client,row).json();b=preview(client,row,'api-request-002').json()
    confirm(client,a);assert confirm(client,b).status_code==409
    transport(monkeypatch,lambda r:httpx.Response(500));perform(claim())
    assert confirm(client,b).status_code==409


def test_crash_after_send_marker_never_replays(client,monkeypatch):
    row,_=setup(client);p=preview(client,row).json();confirm(client,p)
    job=claim()
    with SessionLocal.begin() as db:
        r=db.get(PluginInvocation,p['id']);r.status='running';r.config=r.config|{'api':r.config['api']|{'dispatch':'in_flight'}}
        db.get(Job,job.id).lease_until=now()-1
    calls=transport(monkeypatch,lambda r:httpx.Response(200,json={'ok':True}))
    perform(claim());assert current(client)['status']=='unknown' and calls==[]


def test_health_is_separate_read_only_and_no_body_persisted(client,monkeypatch):
    _,c=setup(client);calls=transport(monkeypatch,lambda r:httpx.Response(200,text=SECRET))
    r=client.post('/api/plugin-connections/'+c['id']+'/test',json={'version':1})
    assert r.status_code==200 and r.json()['checked_at'] and len(calls)==1
    assert calls[0].method=='GET' and calls[0].url.path=='/health' and SECRET not in r.text


def test_api_uses_actual_http_transport_and_get_encoding(client):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    observed=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path,self.headers.get('X-Test-Key')))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"checked":true}')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        row,_=setup(client,url=f'http://127.0.0.1:{server.server_port}/read',health_url='',
            method='GET',effect='read',auth='api_key',key_header='X-Test-Key')
        confirm(client,preview(client,row).json());perform(claim())
        assert current(client)['status']=='succeeded' and len(observed)==1
        assert observed[0][1]==SECRET and observed[0][0].startswith('/read?title=')
    finally:server.shutdown();server.server_close();thread.join()


def test_reviewed_api_is_signed_synced_then_installed(client,shop,monkeypatch):
    from test_plugin_store import publish, core_config, install as store_install
    published=publish(shop,data=api_release().manifest)
    assert published['checks']['supported_by_current_core'] is True
    core_config(client,shop,monkeypatch)
    assert client.post('/api/plugin-store/sync').status_code==200
    row=store_install(client,published)
    assert row.status_code==200 and row.json()[0]['grants']==['network.write']
    cap=next(c for c in client.get('/api/plugin-capabilities').json() if c['kind']=='api')
    assert cap['available'] is False and cap['api']['method']=='POST'


def test_update_pins_existing_request_and_new_endpoint_needs_authorization(client,monkeypatch):
    row,_=setup(client);p=preview(client,row).json();confirm(client,p)
    new=api_release(url='http://127.0.0.1:9191/new')
    new.version='1.1.0';new.id=digest({'plugin_id':new.plugin_id,'version':new.version})
    new.manifest=new.manifest|{'version':new.version};new.digest=digest(new.manifest);add(new)
    install(client,'1.1.0','update',plugin='sample.api')
    calls=transport(monkeypatch,lambda r:httpx.Response(200,json={'ok':True}));perform(claim())
    assert current(client)['status']=='succeeded' and calls[0].url.path=='/action'
    assert preview(client,row,'api-new-version').status_code==409


def test_restore_replays_latest_send_markers_and_revokes_connections(client):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    row,c=setup(client);p=preview(client,row).json();confirm(client,p)
    with SessionLocal.begin() as db:
        schema(db);r=db.get(PluginInvocation,p['id']);r.status='running'
        r.config=r.config|{'api':r.config['api']|{'dispatch':'in_flight'}}
    data=checkpoint()
    with SessionLocal.begin() as db:
        r=db.get(PluginInvocation,p['id']);r.status='queued';r.config=r.config|{'api':r.config['api']|{'dispatch':'not_sent'}}
    quarantine(data)
    with SessionLocal() as db:
        assert db.get(PluginInvocation,p['id']).status=='unknown'
        assert db.get(PluginConnection,c['id']).status=='revoked' and not db.get(PluginConnection,c['id']).encrypted_secret
        assert db.get(PluginInstallation,row['id']).status=='disabled'
