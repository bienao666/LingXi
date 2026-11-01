"""Read-only local evidence must not be confused with live service verification."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now
from lingxi.models import Provider, Model, Policy, Job
from lingxi.channels import Channel
from lingxi.plugins import PluginInstallation, PluginRelease, seed_bundled
from lingxi.voice import VoiceConfig, audio_signature
from lingxi.plugin_store import StoreConnection
from lingxi.operations import heartbeat

URL='/api/admin/operations/readiness'


def group(client, name):
    response=client.get(URL)
    assert response.status_code==200,response.text
    return next(row for row in response.json()['groups'] if row['id']==name)


def test_requires_authentication():
    with TestClient(app) as client: assert client.get(URL).status_code==401


def test_empty_snapshot_is_read_only_and_never_probes_remote(client,monkeypatch):
    def forbidden(*a,**kw): raise AssertionError('No network during snapshot')
    monkeypatch.setattr('httpx.Client.send',forbidden)
    monkeypatch.setattr('httpx.AsyncClient.send',forbidden)
    writes=[]
    def inspect(conn,cursor,statement,*args):
        if statement.lstrip().lower().startswith(('insert','update','delete')):writes.append(statement)
    event.listen(engine,'before_cursor_execute',inspect)
    try:
        # Calling through ASGI avoids blocking the TestClient transport itself.
        with SessionLocal() as db:
            from lingxi.readiness import readiness
            from lingxi.models import User
            data=readiness((db,db.get(User,'owner'),None))
        assert len(data.groups)==5 and not writes
        assert [x.state for x in data.groups[0].items] == ['unconfigured', 'unconfigured', 'configured', 'configured']
        assert 'worker_unavailable' in [x.code for x in data.alerts]
    finally:event.remove(engine,'before_cursor_execute',inspect)


def test_model_evidence_invalidates_with_provider_version(configured):
    assert group(configured,'models')['items'][0]['state']=='configured'
    with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    assert group(configured,'models')['items'][0]['state']=='attention'
    with SessionLocal.begin() as db:db.get(Provider,'provider').enabled=False
    assert group(configured,'models')['items'][0]['state']=='disabled'


@pytest.mark.parametrize('change',['model_workspace','provider_workspace','purpose'])
def test_default_policy_never_leaks_foreign_or_wrong_purpose(configured,change):
    with SessionLocal.begin() as db:
        if change=='provider_workspace':db.get(Provider,'provider').workspace_id='other'
        elif change=='purpose':db.get(Model,'chat').purpose='embedding'
        else:db.get(Model,'chat').workspace_id='other'
    response=configured.get(URL)
    assert group(configured,'models')['items'][0]['state']=='unconfigured'
    assert 'secret-fixture' not in response.text and 'example.com' not in response.text


@pytest.mark.parametrize('enabled,lease,login,expected',[
    (False,100,'online','disabled'),(True,-1,'online','attention'),
    (True,100,'online','online'),(True,100,'login_required','attention'),(True,100,'checking','unknown')])
def test_protocol_evidence_is_not_delivery(client,enabled,lease,login,expected):
    with SessionLocal.begin() as db:
        db.add(Channel(user_id='owner',name='QA',bot_id='1',encrypted_token='SECRET',
            enabled=enabled,connection_id='socket',lease_until=now()+lease,login_state=login,last_seen=123))
    row=group(client,'channels')['items'][0]
    assert row['state']==expected and row['observed_at']==123
    assert 'SECRET' not in client.get(URL).text


def test_channel_scope_and_bound_are_honest(client):
    with SessionLocal.begin() as db:
        for i in range(23):db.add(Channel(user_id='owner',name=f'row{i}',bot_id=str(i),encrypted_token='secret'))
        db.add(Channel(user_id='different',name='FOREIGN',bot_id='88',encrypted_token='secret'))
        db.add(Channel(user_id='owner',workspace_id='elsewhere',name='FOREIGN',bot_id='89',encrypted_token='secret'))
    data=group(client,'channels')
    assert data['total']==23 and len(data['items'])==20 and data['truncated']
    assert 'FOREIGN' not in client.get(URL).text


def test_plugins_only_observe_manifest_and_reject_tampering(client):
    with SessionLocal.begin() as db:
        seed_bundled(db)
        release=db.query(PluginRelease).first()
        identity=release.id
    assert group(client,'plugins')['items'][0]['state']=='observed'
    with SessionLocal.begin() as db:db.get(PluginRelease,identity).digest='invalid'
    assert group(client,'plugins')['items'][0]['state']=='attention'
    with SessionLocal.begin() as db:db.query(PluginInstallation).first().status='uninstalled'
    assert group(client,'plugins')['total']==0


def test_voice_signature_changes_are_not_physical_device_checks(configured):
    with SessionLocal.begin() as db:
        row=VoiceConfig(id='owner',spec={'enabled':True,'asr':{'provider_id':'provider','model':'speech'}})
        row.checks={'asr':audio_signature(row,db.get(Provider,'provider'),'asr')}
        db.add(row)
    assert group(configured,'voice')['items'][0]['state']=='configured'
    with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    assert group(configured,'voice')['items'][0]['state']=='attention'
    with SessionLocal.begin() as db:db.get(VoiceConfig,'owner').spec={'enabled':True,'asr':[]}
    assert group(configured,'voice')['items'][0]['state']=='attention'


@pytest.mark.parametrize('enabled,expires,error,expected',[(False,100,None,'disabled'),(True,-1,None,'attention'),
    (True,100,'SECRET-upstream-error','attention'),(True,100,None,'observed')])
def test_store_uses_only_sanitized_cached_evidence(client,enabled,expires,error,expected):
    with SessionLocal.begin() as db:
        db.add(StoreConnection(user_id='owner',url='https://SECRET.example',root_keys=['SECRET-key'],
            encrypted_token='SECRET-token',enabled=enabled,expires_at=now()+expires,error=error))
    assert group(client,'store')['items'][0]['state']==expected
    assert 'SECRET' not in client.get(URL).text


def test_idle_worker_future_jobs_and_disk_failure(client,monkeypatch):
    heartbeat('qa-worker',now(),0)
    with SessionLocal.begin() as db:
        db.add(Job(key='future',kind='qa',status='pending',available_at=now()+600,payload={}))
        db.add(Job(key='just-due',kind='qa',status='pending',created_at=now()-86400,available_at=now()-1,payload={}))
        db.add(Job(key='foreign',workspace_id='other',kind='qa',status='pending',available_at=now()-600,payload={}))
    monkeypatch.setattr('lingxi.operations.shutil.disk_usage',lambda *a:(_ for _ in ()).throw(OSError('SECRET-path')))
    data=client.get(URL).json()
    codes=[x['code'] for x in data['alerts']]
    assert 'worker_unavailable' not in codes and 'queue_backlog' not in codes and 'disk_unavailable' in codes
    with SessionLocal.begin() as db:db.add(Job(key='due',kind='qa',status='pending',available_at=now()-601,created_at=now()-601,payload={}))
    assert 'queue_backlog' in [x['code'] for x in client.get(URL).json()['alerts']]
    assert 'SECRET' not in client.get(URL).text
