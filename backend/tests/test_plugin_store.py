"""Publication, independent identity, signed distribution and scope boundaries."""
import base64
import copy
import json
import secrets
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.plugin_manifest import Manifest, digest
from lingxi.plugin_signing import canonical, key_id, load_private, public_key, sha, signed
from lingxi_store import data as sd
from lingxi_store.api import app as store_app
from lingxi_store.cli import keys
from lingxi_store import artifacts


@pytest.fixture
def shop(tmp_path,monkeypatch):
    monkeypatch.setenv('LINGXI_STORE_DATABASE_URL',__import__('os').environ.get('LINGXI_TEST_STORE_DATABASE_URL','sqlite:///'+str(tmp_path/'store.db')))
    monkeypatch.setenv('LINGXI_STORE_COOKIE_SECURE','false')
    monkeypatch.setenv('LINGXI_STORE_PUBLIC_ORIGIN','http://testserver')
    monkeypatch.setenv('LINGXI_STORE_SIGNING_KEY',str(tmp_path/'keys/signing.key'))
    monkeypatch.setenv('LINGXI_STORE_TRUST_BUNDLE',str(tmp_path/'keys/trust.json'))
    monkeypatch.setenv('LINGXI_STORE_ARTIFACT_DIR',str(tmp_path/'objects'))
    sd.settings.cache_clear();sd.engine.cache_clear()
    keys(tmp_path/'keys')
    sd.Base.metadata.drop_all(sd.engine());sd.Base.metadata.create_all(sd.engine())
    from argon2 import PasswordHasher
    password=PasswordHasher().hash('store-test-password-123')
    with sd.session()as db,db.begin():
        db.add(sd.State(id='catalog',revision=0))
        db.add_all([sd.Account(id='pub',username='publisher',password_hash=password,role='publisher',namespace='sample',audiences=['space-a']),
            sd.Account(id='reviewer',username='reviewer',password_hash=password,role='reviewer',audiences=[]),
            sd.Account(id='other',username='other',password_hash=password,role='publisher',namespace='other',audiences=['space-b'])])
    with TestClient(store_app)as client:
        client.headers['Origin']='http://testserver'
        def login(name):
            r=client.post('/store/v1/auth/login',json={'username':name,'password':'store-test-password-123'})
            assert r.status_code==200,r.text
            client.headers['X-Store-CSRF']=r.json()['csrf']
            return r.json()
        client.login=login
        client.directory=tmp_path
        client.login('publisher')
        yield client
    sd.engine().dispose();sd.engine.cache_clear();sd.settings.cache_clear()


def manifest(version='1.0.0',**extra):
    return Manifest(plugin_id='sample.writer',publisher='sample',version=version,name='测试资料整理',description='Only synthetic input.',license='MIT',
        costs='May use configured paid model.',data_scope='Only explicit input.',capabilities=[{'id':'sample.outline','name':'整理','kind':'skill','permissions':['model.chat'],'instruction':'整理明确输入，不虚构行动。'}],**extra).model_dump()


def submit(shop,version='1.0.0',visibility='public',audience='',data=None):
    r=shop.post('/store/v1/submissions',json={'manifest':data or manifest(version),'visibility':visibility,'audience':audience})
    assert r.status_code==200,r.text
    return r.json()


def decision(shop,row,action='approve'):
    return shop.post('/store/v1/releases/'+row['id']+'/decision',json={'version':row['version_number'],'action':action,'note':'Synthetic review evidence.'})


def publish(shop,**kwargs):
    shop.login('publisher');row=submit(shop,**kwargs);shop.login('reviewer')
    r=decision(shop,row);assert r.status_code==200,r.text
    return r.json()


def core_config(client,shop,monkeypatch,audience='',token=''):
    monkeypatch.setattr('lingxi.plugin_store.validate_endpoint',lambda *a:None)
    pub=json.loads((shop.directory/'keys/trust.json').read_text())['root_public_key']
    r=client.put('/api/plugin-store',json={'version':0,'url':'http://store.example','enabled':True,'allow_private':True,'root_keys':[pub],'audience':audience,'token':token})
    assert r.status_code==200,r.text
    # HTTP fixture preserves service request/response behavior; no Core cookie or
    # model credentials are sent into the independent store.
    class Transport:
        def __init__(self,headers):self.headers=headers
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,method,path):
            headers=dict(self.headers);headers['Cookie']=''
            return shop.stream(method,'/'+path,headers=headers)
    monkeypatch.setattr('lingxi.plugin_store.outbound_client',lambda url,**kw:Transport(kw.get('headers',{})))
    return r.json()


def install(client,row):
    p=client.post('/api/plugin-installations/preview',json={'plugin_id':row['plugin_id'],'plugin_version':row['version']})
    assert p.status_code==200,p.text
    data=p.json()
    return client.post('/api/plugin-installations/previews/'+data['id']+'/confirm',json={'digest':data['digest'],'approved_permissions':data['plan']['permissions']})


def test_pending_never_public_and_publisher_cannot_approve(shop):
    row=submit(shop)
    assert decision(shop,row).status_code==403
    assert shop.get('/store/v1/catalog').json()==[]
    assert shop.get('/store/v1/feed').json()['snapshot']['payload']['releases']==[]
    assert shop.get('/store/v1/artifacts/'+row['digest']).status_code==404


def test_published_artifact_is_immutable_and_signed(shop):
    row=publish(shop)
    blob=shop.get('/store/v1/artifacts/'+row['digest'])
    assert blob.status_code==200 and sha(blob.content)==row['digest']
    from lingxi.plugin_signing import verify
    bundle=json.loads((shop.directory/'keys/trust.json').read_text())
    pub=bundle['payload']['keys'][0]['public_key']
    verify(pub,row['artifact']['payload'],row['artifact']['signature'])
    shop.login('publisher')
    assert shop.post('/store/v1/submissions',json={'manifest':manifest()}).status_code==409


def test_reject_requires_new_version_and_note(shop):
    row=submit(shop);shop.login('reviewer');r=decision(shop,row,'reject')
    assert r.status_code==200 and r.json()['status']=='rejected'
    assert decision(shop,r.json(),'approve').status_code==409
    assert shop.get('/store/v1/catalog').json()==[]


def test_review_optimistic_version_and_self_review(shop):
    row=submit(shop)
    with sd.session()as db,db.begin():db.get(sd.Account,'pub').role='reviewer'
    assert decision(shop,row).status_code==403
    shop.login('reviewer');assert decision(shop,row).status_code==200
    assert decision(shop,row,'withdraw').status_code==409


@pytest.mark.parametrize('kind',['cookie','csrf','origin'])
def test_independent_auth_and_csrf(shop,kind):
    if kind=='cookie':shop.cookies.clear()
    elif kind=='csrf':shop.headers['X-Store-CSRF']='wrong'
    else:shop.headers['Origin']='http://wrong.example'
    assert shop.post('/store/v1/submissions',json={'manifest':manifest()}).status_code in (401,403)


def test_login_errors_rate_limit_and_session_disable(shop):
    for _ in range(8):
        assert shop.post('/store/v1/auth/login',json={'username':'ghost','password':'bad-password'}).status_code==401
    assert shop.post('/store/v1/auth/login',json={'username':'ghost','password':'bad-password'}).status_code==429
    with sd.session()as db,db.begin():db.get(sd.Account,'pub').enabled=False
    assert shop.get('/store/v1/auth/me').status_code==401


def test_namespace_and_private_audience_authorization(shop):
    bad=manifest()|{'publisher':'other','plugin_id':'other.test'}
    assert shop.post('/store/v1/submissions',json={'manifest':bad}).status_code==403
    assert shop.post('/store/v1/submissions',json={'manifest':manifest(),'visibility':'private','audience':'space-b'}).status_code==403
    submit(shop,visibility='private',audience='space-a')
    shop.login('other')
    assert shop.get('/store/v1/submissions').json()==[]


def test_private_catalog_and_artifact_require_matching_reader(shop):
    row=publish(shop,visibility='private',audience='space-a')
    assert shop.get('/store/v1/catalog').json()==[]
    assert shop.get('/store/v1/artifacts/'+row['digest']).status_code==404
    with sd.session()as db,db.begin():db.add(sd.Reader(id='read',token_hash=sha(b'reader-token'),audience='space-a',expires_at=sd.now()+300))
    headers={'Authorization':'Bearer reader-token'}
    assert len(shop.get('/store/v1/catalog',headers=headers).json())==1
    assert shop.get('/store/v1/artifacts/'+row['digest'],headers=headers).status_code==200
    with sd.session()as db,db.begin():db.get(sd.Reader,'read').enabled=False
    assert shop.get('/store/v1/catalog',headers=headers).status_code==401


def test_dependencies_cannot_reference_private_from_public(shop):
    publish(shop,visibility='private',audience='space-a')
    shop.login('publisher')
    data=manifest('1.1.0',dependencies=[{'plugin_id':'sample.writer','constraint':'==1.0.0'}])
    data['plugin_id']='sample.dependent'
    row=submit(shop,data=data);shop.login('reviewer')
    assert decision(shop,row).status_code==409


def test_circular_dependency_and_missing_version_rejected(shop):
    row=submit(shop,data=manifest(dependencies=[{'plugin_id':'sample.writer','constraint':'==1.0.0'}]))
    shop.login('reviewer');assert decision(shop,row).status_code==409


def test_storage_failure_does_not_publish_or_leak_error(shop,monkeypatch):
    row=submit(shop);shop.login('reviewer')
    monkeypatch.setattr(artifacts,'put',lambda *a:(_ for _ in ()).throw(RuntimeError('SECRET-storage-details')))
    r=decision(shop,row)
    assert r.status_code==503 and 'SECRET'not in r.text
    assert shop.get('/store/v1/submissions').json()[0]['status']=='pending'


def test_corrupt_artifact_refused(shop):
    row=publish(shop)
    (shop.directory/'objects/artifacts'/f"{row['digest']}.json").write_bytes(b'changed')
    assert shop.get('/store/v1/artifacts/'+row['digest']).status_code==503


def test_untrusted_large_body_rejected(shop):
    r=shop.post('/store/v1/submissions',content=b'x'*(301*1024),headers={'Content-Type':'application/json'})
    assert r.status_code==413


def test_core_sync_install_uses_signed_artifact_and_explicit_confirmation(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch)
    r=client.post('/api/plugin-store/sync');assert r.status_code==200,r.text
    catalog=client.get('/api/plugin-catalog').json()
    assert any(x['source']=='store'and x['plugin_id']=='sample.writer'for x in catalog)
    assert not any(x['plugin_id']=='sample.writer'for x in client.get('/api/plugin-installations').json())
    assert install(client,row).status_code==200
    assert 'encrypted_token'not in client.get('/api/plugin-store').text


def test_core_scope_is_not_global_release_visibility(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    from lingxi.db import SessionLocal
    from lingxi.plugin_store import StoreAccess
    with SessionLocal.begin()as db:
        item=db.scalar(select(StoreAccess));item.workspace_id='other'
    assert not any(r['plugin_id']==row['plugin_id']for r in client.get('/api/plugin-catalog').json())
    assert client.post('/api/plugin-installations/preview',json={'plugin_id':row['plugin_id'],'plugin_version':row['version']}).status_code==409


@pytest.mark.parametrize('change',['signature','root','expiry','audience','revision','artifact'])
def test_core_rejects_untrusted_feed(client,shop,monkeypatch,change):
    row=publish(shop);core_config(client,shop,monkeypatch)
    from lingxi.plugin_store import verified_feed,connection
    from lingxi.db import SessionLocal
    from lingxi.models import User
    data=shop.get('/store/v1/feed').json();private=load_private(shop.directory/'keys/signing.key')
    if change=='signature':data['snapshot']['signature']='invalid'
    elif change=='root':data['trust']['root_public_key']=base64.b64encode(b'x'*32).decode()
    else:
        p=data['snapshot']['payload']
        if change=='expiry':p['expires_at']=0
        elif change=='audience':p['audience']='other'
        elif change=='revision':p['revision']=-10
        else:p['releases'][0]['artifact']['payload']['digest']='0'*64
        data['snapshot']=signed(private,p)
    with SessionLocal()as db:
        cfg=connection(db,db.get(User,'owner'))
        with pytest.raises(Exception):verified_feed(data,cfg)


def test_withdraw_keeps_existing_install_but_blocks_new_install(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    assert install(client,row).status_code==200
    assert decision(shop,row,'withdraw').status_code==200
    assert client.post('/api/plugin-store/sync').status_code==200
    installed=next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')
    assert installed['available']is True
    assert not any(r['plugin_id']=='sample.writer'for r in client.get('/api/plugin-catalog').json())


def test_revocation_invalidates_pending_call_and_old_preview(configured,shop,monkeypatch):
    row=publish(shop);core_config(configured,shop,monkeypatch);assert configured.post('/api/plugin-store/sync').status_code==200
    item=install(configured,row).json()[0]
    inv=configured.post('/api/plugin-invocations',json={'installation_id':item['id'],'capability_id':'sample.outline','content':'Synthetic','request_id':'store-revoke-fixture'}).json()
    assert decision(shop,row,'revoke').status_code==200
    assert configured.post('/api/plugin-store/sync').status_code==200
    result=configured.get('/api/plugin-invocations').json()[0]
    assert result['id']==inv['id']and result['status']=='invalidated'
    item=next(r for r in configured.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')
    assert item['available']is False


def test_offline_store_keeps_installed_but_expired_snapshot_blocks_install(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    assert install(client,row).status_code==200
    from lingxi.db import SessionLocal
    from lingxi.plugin_store import StoreConnection
    with SessionLocal.begin()as db:
        cfg=db.scalar(select(StoreConnection));cfg.expires_at=0;cfg.error='offline'
    assert next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')['available']is True
    # Existing exact version cannot be installed again; fresh access still rejects.
    from lingxi.plugin_store import access
    from lingxi.models import User
    from lingxi.plugins import PluginRelease
    with SessionLocal()as db:
        with pytest.raises(HTTPException):access(db,db.get(User,'owner'),db.get(PluginRelease,row['id']),True)


def test_signing_key_rotation_and_revocation_stops_old_artifacts(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    assert install(client,row).status_code==200
    old=key_id(public_key(load_private(shop.directory/'keys/signing.key')))
    keys(shop.directory/'keys',shop.directory/'keys/offline-root.key',old)
    assert client.post('/api/plugin-store/sync').status_code==200
    installed=next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')
    assert installed['available']is False


def test_signed_revocation_survives_another_artifact_download_failure(configured,shop,monkeypatch):
    row=publish(shop);core_config(configured,shop,monkeypatch);assert configured.post('/api/plugin-store/sync').status_code==200
    install(configured,row)
    assert decision(shop,row,'revoke').status_code==200
    other=publish(shop,version='1.1.0')
    (shop.directory/'objects/artifacts'/f"{other['digest']}.json").write_bytes(b'broken')
    assert configured.post('/api/plugin-store/sync').status_code==503
    item=next(r for r in configured.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')
    assert item['available']is False


def test_signing_rotation_retains_nonrevoked_history(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    install(client,row)
    keys(shop.directory/'keys',shop.directory/'keys/offline-root.key')
    assert client.post('/api/plugin-store/sync').status_code==200
    assert next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')['available']is True


def test_s3_transport_is_signed_and_revalidates_content(shop,monkeypatch):
    import httpx
    s=sd.settings();s.s3_endpoint='https://objects.example';s.s3_bucket='plugin-artifacts';s.s3_region='us-east-1';s.s3_access_key='fixture-access';s.s3_secret_key='fixture-secret'
    stored={};calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.host=='objects.example'
        assert request.headers['authorization'].startswith('AWS4-HMAC-SHA256 Credential=fixture-access/')
        assert request.headers['x-amz-content-sha256']==sha(request.content)
        assert request.headers.get('cookie')is None
        if request.method=='PUT':stored[request.url.path]=request.content;return httpx.Response(200)
        return httpx.Response(200,content=stored[request.url.path])
    original=httpx.Client
    monkeypatch.setattr(artifacts.httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    blob=canonical(manifest());checksum=sha(blob)
    artifacts.put(checksum,blob)
    assert artifacts.get(checksum)==blob and len(calls)==3
    stored[next(iter(stored))]=b'changed'
    with pytest.raises(ValueError):artifacts.get(checksum)


def test_prior_signed_snapshot_cannot_undo_new_revision(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch);assert client.post('/api/plugin-store/sync').status_code==200
    old=shop.get('/store/v1/feed').json()
    assert decision(shop,row,'revoke').status_code==200
    assert client.post('/api/plugin-store/sync').status_code==200
    from lingxi.db import SessionLocal
    from lingxi.models import User
    from lingxi.plugin_store import connection,verified_feed
    with SessionLocal()as db:
        with pytest.raises(ValueError):verified_feed(old,connection(db,db.get(User,'owner')))


def test_revoked_unrelated_plugin_does_not_block_new_install(client,shop,monkeypatch):
    row=publish(shop);core_config(client,shop,monkeypatch)
    assert client.post('/api/plugin-store/sync').status_code==200
    assert install(client,row).status_code==200
    assert decision(shop,row,'revoke').status_code==200
    assert client.post('/api/plugin-store/sync').status_code==200
    installed=next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='sample.writer')
    assert installed['available']is False
    preview=client.post('/api/plugin-installations/preview',json={'plugin_id':'lingxi.writing','plugin_version':'1.1.0'})
    assert preview.status_code==200,preview.text


def test_slow_store_sync_does_not_block_worker_job_claims(monkeypatch):
    import threading
    from lingxi import worker
    started,release,claimed=threading.Event(),threading.Event(),threading.Event()
    def slow_sync():
        started.set()
        release.wait(5)
    def claim():
        assert started.wait(2)
        claimed.set()
        raise StopIteration()
    monkeypatch.setattr('lingxi.plugin_store.tick',slow_sync)
    monkeypatch.setattr('lingxi.proactive.tick',lambda:None)
    monkeypatch.setattr('lingxi.memory.replay_ledger',lambda db:None)
    monkeypatch.setattr(worker,'claim',claim)
    errors=[]
    def run():
        try:worker.main()
        except StopIteration:pass
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run)
    thread.start()
    try:
        assert claimed.wait(2),'Store sync blocked job scheduling'
    finally:
        release.set();thread.join(timeout=5)
    assert not thread.is_alive() and errors==[]
