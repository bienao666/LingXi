"""OAuth PKCE, token isolation, lifecycle fencing and refresh interruption."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit, urlencode
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import LoginSession, User
from lingxi.plugin_manifest import Manifest, ApiEntry, McpEntry, digest
from lingxi.plugin_api import PluginConnection
from lingxi.plugin_oauth import OAuthFlow, OAuthGrant, ensure_access, sweep, redirect_uri
from lingxi.security import decrypt
from lingxi.worker import claim, perform
from test_plugins import add, install, action
from test_plugin_api import api_release, transport as api_transport, preview, confirm, current
from test_plugin_store import shop

OAUTH = dict(authorization_url='http://127.0.0.1:9191/authorize', token_url='http://127.0.0.1:9191/token',
    issuer='http://127.0.0.1:9191', revocation_url='http://127.0.0.1:9191/revoke',
    scopes=['files.read','profile.read'], client_auth='none', resource='')
ACCESS, REFRESH, CLIENT_SECRET = 'synthetic-oauth-access', 'synthetic-oauth-refresh', 'synthetic-client-secret'


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    from lingxi.config import settings
    monkeypatch.setattr(settings(), 'public_origin', 'https://testserver')


def transport(monkeypatch, handler):
    calls = api_transport(monkeypatch, handler)
    from lingxi.plugin_api import outbound_client
    monkeypatch.setattr('lingxi.plugin_oauth.outbound_client', outbound_client)
    return calls


def setup(client, **changes):
    oauth = OAUTH | changes
    add(api_release(auth='oauth2', oauth=oauth))
    row = install(client, plugin='sample.api')
    return row


def start(client, row, **changes):
    connections=client.get('/api/plugin-connections').json()
    body=dict(installation_id=row['id'], capability_id='sample.request', installation_version=row['version'],
        connection_version=connections[0]['version'] if connections else 0,
        client_id='synthetic-client', allow_private=True, acknowledged=True) | changes
    response=client.post('/api/plugin-oauth/start', json=body)
    assert response.status_code == 200, response.text
    return response.json()


def callback(client, started, **changes):
    params=dict(state=parse_qs(urlsplit(started['authorization_url']).query)['state'][0],
                code='synthetic-authorization-code', iss=OAUTH['issuer']) | changes
    return client.get('/api/plugin-oauth/callback?'+urlencode(params), follow_redirects=False)


def response(**changes):
    return dict(access_token=ACCESS, refresh_token=REFRESH, token_type='Bearer', expires_in=3600,
                scope='files.read profile.read') | changes


def connected(client, monkeypatch, **changes):
    row=setup(client, **changes)
    calls=transport(monkeypatch, lambda r:httpx.Response(200,json=response()))
    p=start(client,row,**({'client_secret':CLIENT_SECRET} if changes.get('client_auth','none')!='none' else {}))
    r=callback(client,p)
    assert r.status_code==303 and r.headers['location'].endswith('oauth=succeeded'), r.text
    return row,client.get('/api/plugin-connections').json()[0],calls


@pytest.mark.parametrize('changes',[
    {'authorization_url':'https://u:p@example.com/a'}, {'token_url':'https://example.com/t?secret=x'},
    {'revocation_url':'file:///tokens'}, {'issuer':'https://example.com/#token'},
    {'authorization_url':'https://example.com/\r\nX'}, {'scopes':['read','read']},
    {'scopes':['a b']}, {'scopes':['a"b']}, {'scopes':['a\\b']}, {'scopes':[]},
    {'client_auth':'client_credentials'}, {'script':'run'},
])
def test_contract_rejects_unsafe_oauth(changes):
    with pytest.raises(ValueError): ApiEntry(url='https://example.com/a', method='GET', effect='read',
        auth='oauth2', oauth=OAUTH|changes)


def test_contract_oauth_explicit_and_legacy_hash_unchanged():
    old=api_release().manifest
    assert 'oauth' not in old['capabilities'][0]['api']
    assert digest(Manifest(**old).model_dump())==digest(old)
    with pytest.raises(ValueError): ApiEntry(url='https://example.com/a',method='GET',effect='read',auth='oauth2')
    with pytest.raises(ValueError): McpEntry(url='https://example.com/mcp',oauth=OAUTH)
    assert McpEntry(url='https://example.com/mcp',auth='oauth2',oauth=OAUTH).model_dump()['oauth']['scopes']==OAUTH['scopes']


def test_start_requires_auth_scope_ack_and_correct_versions(client):
    row=setup(client)
    body=dict(installation_id=row['id'],capability_id='sample.request',installation_version=row['version'],client_id='client',acknowledged=True)
    assert client.post('/api/plugin-oauth/start',json=body).status_code==422  # private network not allowed
    assert client.post('/api/plugin-oauth/start',json=body|{'allow_private':True,'acknowledged':False}).status_code==422
    assert client.post('/api/plugin-oauth/start',json=body|{'allow_private':True,'installation_version':999}).status_code==409
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/plugin-oauth/start',json=body).status_code==403
    client.headers['X-CSRF-Token']=csrf
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/plugin-oauth/flows').status_code==401
    with SessionLocal.begin() as db:
        from lingxi.plugins import PluginInstallation
        db.get(PluginInstallation,row['id']).workspace_id='other'
    assert client.post('/api/plugin-oauth/start',json=body|{'allow_private':True}).status_code==404


def test_pkce_secret_is_encrypted_and_start_never_sends(client,monkeypatch):
    row=setup(client);calls=transport(monkeypatch,lambda r:pytest.fail('start must not send'))
    p=start(client,row);query=parse_qs(urlsplit(p['authorization_url']).query)
    assert calls==[] and query['response_type']==['code'] and query['code_challenge_method']==['S256']
    assert query['redirect_uri']==['https://testserver/api/plugin-oauth/callback']
    with SessionLocal() as db:
        f=db.get(OAuthFlow,p['id']);config=json.loads(decrypt(f.encrypted_config))
        expected=base64.urlsafe_b64encode(hashlib.sha256(config['verifier'].encode()).digest()).decode().rstrip('=')
        assert query['code_challenge']==[expected] and config['verifier'] not in json.dumps(p)
        assert query['state'][0] not in f.state_hash and len(f.state_hash)==64
    assert client.get('/api/plugin-connections').json()[0]['status']=='pending'
    assert 'encrypted_config' not in client.get('/api/plugin-oauth/flows').text


def test_callback_once_and_sensitive_material_never_returned(client,monkeypatch):
    row,c,calls=connected(client,monkeypatch)
    assert len(calls)==1 and c['available'] and c['oauth']['refreshable']
    assert c['oauth']['scopes']==['files.read','profile.read']
    fields=parse_qs(calls[0].content.decode());assert fields['code']==['synthetic-authorization-code']
    assert len(fields['code_verifier'][0])>=43 and fields['grant_type']==['authorization_code']
    with SessionLocal() as db:
        f=db.scalar(select(OAuthFlow));assert f.status=='succeeded' and f.encrypted_config==''
        g=db.scalar(select(OAuthGrant));assert REFRESH not in g.encrypted_config
        assert decrypt(db.get(PluginConnection,c['id']).encrypted_secret)==ACCESS
        state='unavailable'
    for endpoint in ('/api/plugin-connections','/api/plugin-oauth/flows','/api/plugin-catalog'):
        text=client.get(endpoint).text
        assert ACCESS not in text and REFRESH not in text and 'synthetic-authorization-code' not in text
    # Saved URL remains one-use even when the browser retries the same callback.
    p=start(client,row);r=callback(client,p);assert r.headers['referrer-policy']=='no-referrer'
    count=len(calls);assert callback(client,p).headers['location'].endswith('oauth=failed') and len(calls)==count


@pytest.mark.parametrize('change',[
    {'state':'wrong'}, {'iss':'https://different.example'}, {'error':'access_denied'}, {'code':''},
])
def test_callback_mismatch_or_denial_never_exchanges(client,monkeypatch,change):
    row=setup(client);p=start(client,row)
    calls=transport(monkeypatch,lambda r:pytest.fail('invalid callback cannot send'))
    r=callback(client,p,**change);assert r.status_code==303 and r.headers['location'].endswith('oauth=failed') and calls==[]


def test_duplicate_query_and_untrusted_errors_are_never_reflected(client,monkeypatch):
    p=start(client,setup(client));q=parse_qs(urlsplit(p['authorization_url']).query)
    r=client.get('/api/plugin-oauth/callback?'+urlencode({'state':q['state'][0],'code':'private-code'})+'&code=other',follow_redirects=False)
    assert r.status_code==303 and 'private-code' not in r.text+str(r.headers)
    transport(monkeypatch,lambda r:httpx.Response(400,json={'error_description':CLIENT_SECRET}))
    r=callback(client,p);assert CLIENT_SECRET not in r.text+str(r.headers)
    assert client.get('/api/plugin-oauth/flows').json()[0]['status']=='failed'


def test_different_login_cannot_consume_flow(client,monkeypatch):
    p=start(client,setup(client));calls=transport(monkeypatch,lambda r:httpx.Response(200,json=response()))
    with TestClient(app) as other:
        other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
        assert callback(other,p).headers['location'].endswith('oauth=failed')
    assert calls==[] and callback(client,p).headers['location'].endswith('oauth=succeeded')


def test_expired_flow_and_login_cannot_exchange(client,monkeypatch):
    row=setup(client);p=start(client,row)
    with SessionLocal.begin() as db: db.get(OAuthFlow,p['id']).expires_at=now()-1
    calls=transport(monkeypatch,lambda r:pytest.fail('expired cannot send'))
    assert callback(client,p).headers['location'].endswith('oauth=failed') and calls==[]
    assert client.get('/api/plugin-oauth/flows').json()[0]['status']=='failed'


def test_cancel_new_attempt_and_disable_prevent_late_callbacks(client,monkeypatch):
    row=setup(client);p=start(client,row);p2=start(client,row)
    calls=transport(monkeypatch,lambda r:httpx.Response(200,json=response()))
    assert callback(client,p).headers['location'].endswith('oauth=failed')
    assert client.post('/api/plugin-oauth/flows/'+p2['id']+'/cancel',json={}).status_code==200
    assert callback(client,p2).headers['location'].endswith('oauth=failed') and calls==[]
    p3=start(client,row)
    assert action(client,row,'disable').status_code==200
    assert callback(client,p3).headers['location'].endswith('oauth=failed') and calls==[]
    with SessionLocal() as db: assert db.get(OAuthFlow,p3['id']).encrypted_config==''


@pytest.mark.parametrize('change',[
    {'scope':'files.read secret.admin'}, {'token_type':'mac'}, {'access_token':'bad\ntoken'},
    {'expires_in':0}, {'expires_in':True}, {'expires_in':None}, {'refresh_token':123},
])
def test_token_contract_never_grants_unapproved_scope(client,monkeypatch,change):
    row=setup(client);p=start(client,row)
    transport(monkeypatch,lambda r:httpx.Response(200,json=response(**change)))
    assert callback(client,p).headers['location'].endswith('oauth=failed')
    assert not client.get('/api/plugin-connections').json()[0]['has_secret']


@pytest.mark.parametrize('auth',['client_secret_basic','client_secret_post'])
def test_confidential_client_auth_encoding_and_redaction(client,monkeypatch,auth):
    row=setup(client,client_auth=auth);calls=transport(monkeypatch,lambda r:httpx.Response(200,json=response()))
    p=start(client,row,client_id='id:+value',client_secret=CLIENT_SECRET)
    assert CLIENT_SECRET not in json.dumps(p)
    assert callback(client,p).headers['location'].endswith('oauth=succeeded')
    fields=parse_qs(calls[0].content.decode())
    if auth=='client_secret_basic':
        assert base64.b64decode(calls[0].headers['authorization'].split()[1]).decode()=='id%3A%2Bvalue:'+CLIENT_SECRET
        assert 'client_secret' not in fields
    else: assert fields['client_secret']==[CLIENT_SECRET]


def expire(connection):
    with SessionLocal.begin() as db:
        g=db.scalar(select(OAuthGrant).where(OAuthGrant.connection_id==connection['id']));g.expires_at=now()+15


def refresh(connection):
    with SessionLocal() as db: workspace = db.get(User, 'owner').workspace_id
    return ensure_access('owner', workspace, connection['id'])


def test_refresh_rotation_is_automatic_before_business_request(client,monkeypatch):
    row,c,_=connected(client,monkeypatch);p=preview(client,row).json();assert confirm(client,p).status_code==200
    expire(c)
    calls=transport(monkeypatch,lambda r:httpx.Response(200,json=response(access_token='rotated-access',refresh_token='rotated-refresh') if r.url.path=='/token' else {'ok':True}))
    perform(claim());r=current(client)
    assert r['status']=='succeeded' and [req.url.path for req in calls]==['/token','/action']
    assert parse_qs(calls[0].content.decode())['refresh_token']==[REFRESH]
    assert calls[1].headers['authorization']=='Bearer rotated-access'
    with SessionLocal() as db:
        g=db.scalar(select(OAuthGrant));assert json.loads(decrypt(g.encrypted_config))['refresh_token']=='rotated-refresh'
    assert client.get('/api/plugin-connections').json()[0]['version']==c['version']  # token refresh is not a new grant


def test_refresh_narrowed_scope_invalidates_old_previews(client,monkeypatch):
    row,c,_=connected(client,monkeypatch);p=preview(client,row).json();expire(c)
    transport(monkeypatch,lambda r:httpx.Response(200,json=response(scope='files.read')))
    refresh(c)
    assert confirm(client,p).status_code==409
    after=client.get('/api/plugin-connections').json()[0]
    assert after['version']==c['version']+1 and after['oauth']['scopes']==['files.read']


def test_refresh_timeout_is_not_replayed_and_no_business_send(client,monkeypatch):
    row,c,_=connected(client,monkeypatch);p=preview(client,row).json();confirm(client,p);expire(c)
    def fail(r): raise httpx.ReadTimeout('do not expose '+REFRESH)
    calls=transport(monkeypatch,fail)
    perform(claim());assert len(calls)==1 and calls[0].url.path=='/token'
    assert current(client)['status'] in ('failed','invalidated') and REFRESH not in json.dumps(current(client))
    with pytest.raises(HTTPException):refresh(c)
    assert len(calls)==1
    after=client.get('/api/plugin-connections').json()[0]
    assert after['status']=='revoked' and not after['has_secret']


def test_refresh_crash_marker_expires_without_retry(client,monkeypatch):
    _,c,_=connected(client,monkeypatch)
    with SessionLocal.begin() as db:
        g=db.scalar(select(OAuthGrant));g.status,g.operation,g.deadline='refreshing','interrupted',now()-1
    calls=transport(monkeypatch,lambda r:pytest.fail('interrupted token must not replay'))
    sweep();assert calls==[]
    with SessionLocal() as db:
        assert db.scalar(select(OAuthGrant)).encrypted_config==''
        assert db.get(PluginConnection,c['id']).status=='revoked'


def test_late_refresh_does_not_restore_revoked_grant(client,monkeypatch):
    _,c,_=connected(client,monkeypatch);expire(c)
    def revoke_during_request(request):
        from lingxi.plugin_oauth import revoke_local
        with SessionLocal.begin() as db:
            conn=db.get(PluginConnection,c['id']);revoke_local(db,conn)
            conn.status,conn.encrypted_secret='revoked','';conn.version+=1
        return httpx.Response(200,json=response(access_token='late-access'))
    transport(monkeypatch,revoke_during_request)
    with pytest.raises(HTTPException):refresh(c)
    after=client.get('/api/plugin-connections').json()[0]
    assert after['status']=='revoked' and not after['has_secret']


def test_revocation_is_local_first_and_remote_result_honest(client,monkeypatch):
    row,c,_=connected(client,monkeypatch)
    def revoke(request):
        with SessionLocal() as db:
            conn=db.get(PluginConnection,c['id']);assert conn.status=='revoked' and not conn.encrypted_secret
            assert db.scalar(select(OAuthGrant)).encrypted_config==''
        assert parse_qs(request.content.decode())['token']==[REFRESH]
        return httpx.Response(200)
    calls=transport(monkeypatch,revoke)
    r=client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':c['version']})
    assert r.status_code==200 and r.json()['oauth']['remote_status']=='accepted' and len(calls)==1
    assert r.json()['status']=='revoked' and not r.json()['has_secret']


def test_revocation_timeout_still_revokes_local_and_never_retries(client,monkeypatch):
    _,c,_=connected(client,monkeypatch)
    def timeout(request): raise httpx.ReadTimeout('private '+REFRESH)
    calls=transport(monkeypatch,timeout)
    r=client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':c['version']})
    assert r.status_code==200 and r.json()['oauth']['remote_status']=='unknown' and REFRESH not in r.text
    assert client.post('/api/plugin-connections/'+c['id']+'/revoke',json={'version':c['version']}).status_code==409
    assert len(calls)==1


def test_manual_token_override_rejected_and_lifecycle_clears_credentials(client,monkeypatch):
    row,c,_=connected(client,monkeypatch)
    body=dict(installation_id=row['id'],capability_id='sample.request',version=c['version'],secret='manual',acknowledged=True)
    assert client.put('/api/plugin-connections/'+c['id'],json=body).status_code==409
    assert action(client,row,'disable').status_code==200
    after=client.get('/api/plugin-connections').json()[0]
    assert after['status']=='revoked' and not after['has_secret'] and not after['oauth']['refreshable']
    with SessionLocal() as db: assert db.scalar(select(OAuthGrant)).encrypted_config==''


def test_validation_errors_do_not_echo_client_secret(client):
    r=client.post('/api/plugin-oauth/start',json={'client_secret':CLIENT_SECRET*400,'other':CLIENT_SECRET})
    assert r.status_code==422 and CLIENT_SECRET not in r.text


def test_public_origin_production_requires_https(monkeypatch):
    from lingxi.config import settings
    monkeypatch.setattr(settings(),'public_origin','http://public.example')
    with pytest.raises(HTTPException):redirect_uri()
    monkeypatch.setattr(settings(),'public_origin','https://lingxi.example')
    assert redirect_uri()=='https://lingxi.example/api/plugin-oauth/callback'


def test_backup_quarantine_clears_all_oauth_material(client,monkeypatch):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    _,c,_=connected(client,monkeypatch)
    with SessionLocal.begin() as db:schema(db)
    data=checkpoint();assert ACCESS not in json.dumps(data) and REFRESH not in json.dumps(data)
    quarantine(data)
    with SessionLocal() as db:
        assert db.scalar(select(OAuthGrant)).encrypted_config==''
        assert db.get(PluginConnection,c['id']).encrypted_secret==''
        assert all(not f.encrypted_config for f in db.scalars(select(OAuthFlow)))


def test_mcp_oauth_refresh_keeps_separate_tool_grants(client,monkeypatch):
    from test_plugin_mcp import mcp_release,Remote,discover,approve,preview as mcp_preview
    monkeypatch.setattr('test_plugin_mcp.SECRET', ACCESS)
    add(mcp_release(auth='oauth2',oauth=OAUTH));row=install(client,plugin='sample.mcp')
    calls=transport(monkeypatch,lambda r:httpx.Response(200,json=response()))
    p=start(client,row,capability_id='sample.remote')
    assert callback(client,p).headers['location'].endswith('oauth=succeeded')
    c=client.get('/api/plugin-connections').json()[0]
    remote=Remote(monkeypatch)
    cat=discover(client,c);assert cat.status_code==200,cat.text
    assert not remote.tool_calls and not cat.json()['approved_tools']
    assert approve(client,c,cat.json()).status_code==200
    p=mcp_preview(client,row).json();confirm(client,p);expire(c)
    perform(claim())
    assert current(client)['status']=='succeeded' and len(remote.tool_calls)==1
    assert len(calls)==2 and parse_qs(calls[-1].content.decode())['grant_type']==['refresh_token']


def test_late_callback_cannot_restore_disabled_connection(client,monkeypatch):
    row=setup(client);p=start(client,row)
    def disabled(request):
        assert action(client,row,'disable').status_code==200
        return httpx.Response(200,json=response())
    calls=transport(monkeypatch,disabled)
    assert callback(client,p).headers['location'].endswith('oauth=failed') and len(calls)==1
    after=client.get('/api/plugin-connections').json()[0]
    assert after['status']=='revoked' and not after['has_secret']


def test_concurrent_refresh_has_one_durable_send(client,monkeypatch):
    from lingxi.db import engine
    from concurrent.futures import ThreadPoolExecutor
    import threading
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row locks required')
    _,c,_=connected(client,monkeypatch);expire(c)
    entered,finish=threading.Event(),threading.Event()
    def send(request):
        entered.set();assert finish.wait(10)
        return httpx.Response(200,json=response())
    calls=transport(monkeypatch,send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(refresh,c)
        try:
            assert entered.wait(10)
            with pytest.raises(HTTPException) as failure:refresh(c)
            assert failure.value.status_code==409
        finally:finish.set()
        first.result(timeout=15)
    assert len(calls)==1


def test_concurrent_callback_consumes_one_code(client,monkeypatch):
    from lingxi.db import engine
    from concurrent.futures import ThreadPoolExecutor
    import threading
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row locks required')
    p=start(client,setup(client));entered,finish=threading.Event(),threading.Event()
    def send(request):
        entered.set();assert finish.wait(10)
        return httpx.Response(200,json=response())
    calls=transport(monkeypatch,send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(callback,client,p)
        try:
            assert entered.wait(10)
            assert callback(client,p).headers['location'].endswith('oauth=failed')
        finally:finish.set()
        assert first.result(timeout=15).headers['location'].endswith('oauth=succeeded')
    assert len(calls)==1


@pytest.mark.parametrize('mode',['redirect','large','duplicate','html'])
def test_token_response_bounds_and_redirects(client,monkeypatch,mode):
    p=start(client,setup(client))
    def invalid(request):
        if mode=='redirect':return httpx.Response(302,headers={'Location':'https://other.example/token'})
        if mode=='large':return httpx.Response(200,json={'access_token':'x'*17000})
        if mode=='duplicate':return httpx.Response(200,headers={'Content-Type':'application/json'},content=b'{"access_token":"one","access_token":"two"}')
        return httpx.Response(200,headers={'Content-Type':'text/html'},content=b'<html>not a token</html>')
    calls=transport(monkeypatch,invalid)
    assert callback(client,p).headers['location'].endswith('oauth=failed') and len(calls)==1
    assert client.get('/api/plugin-connections').json()[0]['has_secret']==False


def test_signed_oauth_release_revocation_stops_connection(client,shop,monkeypatch):
    from test_plugin_store import publish,core_config,decision
    published=publish(shop,data=api_release(auth='oauth2',oauth=OAUTH).manifest)
    core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    installed=install(client,plugin='sample.api')
    transport(monkeypatch,lambda r:httpx.Response(200,json=response()))
    assert callback(client,start(client,installed)).headers['location'].endswith('oauth=succeeded')
    assert decision(shop,published,'revoke').status_code==200
    assert client.post('/api/plugin-store/sync').status_code==200
    c=client.get('/api/plugin-connections').json()[0]
    assert c['status']=='revoked' and not c['has_secret']


def test_upgrade_from_oauth_to_manual_auth_does_not_block_new_connection(client,monkeypatch):
    row,c,_=connected(client,monkeypatch)
    changed=api_release()
    changed.version='1.1.0';changed.manifest=changed.manifest|{'version':'1.1.0'}
    changed.id=digest({'plugin_id':changed.plugin_id,'version':changed.version});changed.digest=digest(changed.manifest)
    add(changed);row=install(client,version='1.1.0',action='update',plugin='sample.api')
    c=client.get('/api/plugin-connections').json()[0]
    r=client.put('/api/plugin-connections/'+c['id'],json=dict(installation_id=row['id'],capability_id='sample.request',
        version=c['version'],secret='new-manual-key',allow_private=True,acknowledged=True))
    assert r.status_code==200 and r.json()['oauth'] is None
    transport(monkeypatch,lambda r:httpx.Response(200,json={'ok':True}))
    p=preview(client,row).json();assert confirm(client,p).status_code==200
    perform(claim());assert current(client)['status']=='succeeded'
