"""Collection never outruns consent, provenance, file protection or restore barriers."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, engine, now, uid
from lingxi.media import Media, MediaLink, storage, cleanup_media
from lingxi.media_retention import MediaRetentionPolicy, MediaProtection, MediaRetentionRun, sweep
from lingxi.models import Audit, Job, User

ROOT = '/api/admin/media-retention'
ACK = 'delete_old_unreferenced_web_uploads'


def file(*, age=8, **values):
    identity = uid()
    with SessionLocal.begin() as db:
        item = Media(id=identity, user_id='owner', workspace_id='personal', request_id=identity,
            filename='闲置资料.txt', suffix='.txt', size=3, digest='a'*64,
            status='ready', created_at=now()-age*86400, **values)
        db.add(item)
    storage(identity, 'blob').write_bytes(b'abc')
    return identity


def preview(client, **changes):
    p = client.get(ROOT).json()
    return client.post(ROOT+'/previews',json={k:p[k] for k in ('version','enabled','days')} | changes)


def save(client, **changes):
    p = preview(client, **changes)
    assert p.status_code == 200, p.text
    return client.put(ROOT,json={'token':p.json()['token'],'acknowledgement':ACK})


def collect(client, p=None):
    p = p or client.post(ROOT+'/sweep-previews').json()
    return client.post(ROOT+'/sweeps',json={'token':p['token'],'acknowledgement':ACK})


def age_policy():
    with SessionLocal.begin() as db:
        db.get(MediaRetentionPolicy,'owner').last_checked = now()-3601


def status(identity):
    with SessionLocal() as db:
        return db.get(Media,identity).status


def test_default_off_and_configuration_preview_never_deletes(client):
    identity = file()
    assert client.get(ROOT).json()['enabled'] is False
    sweep()
    assert status(identity) == 'ready'
    p = preview(client,enabled=True).json()
    assert status(identity) == 'ready'
    assert client.put(ROOT,json={'token':p['token'],'acknowledgement':ACK}).status_code == 200
    sweep()  # Saving does not turn a background interval into an immediate purge.
    assert status(identity) == 'ready'
    age_policy(); sweep()
    assert status(identity) == 'deleted'


@pytest.mark.parametrize('changes',[{'days':0},{'days':366},{'enabled':'true'},{'version':-1},{'other':True}])
def test_strict_policy_validation(client, changes):
    assert preview(client,**changes).status_code == 422


def test_confirmation_and_concurrent_policy_version(client):
    a,b = preview(client,enabled=True).json(),preview(client,days=10).json()
    assert client.put(ROOT,json={'token':a['token'],'acknowledgement':'yes'}).status_code == 422
    assert client.put(ROOT,json={'token':a['token'],'acknowledgement':ACK}).status_code == 200
    assert client.put(ROOT,json={'token':b['token'],'acknowledgement':ACK}).status_code == 409


def test_scope_and_authentication(client):
    with TestClient(app) as stranger:
        assert stranger.get(ROOT).status_code == 401
        assert stranger.get(ROOT+'/runs').status_code == 401
    token = client.headers.pop('X-CSRF-Token')
    assert client.post(ROOT+'/sweep-previews').status_code == 403
    client.headers['X-CSRF-Token'] = token
    with SessionLocal.begin() as db:
        db.add(User(id='other',username='other',password_hash=''))
    identity=file()
    with SessionLocal.begin() as db:
        row=db.get(Media,identity); row.user_id='other'
    assert client.put('/api/media/'+identity+'/protection',json={'version':0,'media_version':1,'protected':True}).status_code == 404
    assert client.post(ROOT+'/sweep-previews').json()['items'] == []


@pytest.mark.parametrize('change',[{'source':{'kind':'server_document'}},{'source':{'kind':'qq'}},
    {'status':'pending'},{'status':'processing'},{'status':'fetching'},{'status':'transcribing'},{'workspace_id':'elsewhere'}])
def test_non_web_or_non_terminal_or_other_workspace_is_retained(client,change):
    identity=file()
    with SessionLocal.begin() as db:
        for key,value in change.items():setattr(db.get(Media,identity),key,value)
    assert client.post(ROOT+'/sweep-previews').json()['items'] == []


def test_failed_web_upload_is_eligible_and_recent_upload_is_not(client):
    recent=file(age=6); old=file()
    with SessionLocal.begin() as db:db.get(Media,old).status='failed'
    assert [r['id'] for r in client.post(ROOT+'/sweep-previews').json()['items']] == [old]
    assert status(recent)=='ready'


@pytest.mark.parametrize('kind',['message','chat','agent','node','task','channel','memory','candidate','plugin','job'])
def test_every_reference_family_protects_even_historical_files(client,kind):
    from lingxi.models import ChatRun, Memory, Candidate
    from lingxi.delegation import AgentRun, AgentNode
    from lingxi.tasks import WorkTask
    from lingxi.channel_media import ChannelAttachment
    from lingxi.plugins import PluginInvocation
    identity=file()
    with SessionLocal.begin() as db:
        if kind=='message':db.add(MediaLink(media_id=identity,message_id='deleted-message'))
        if kind=='channel':db.add(ChannelAttachment(inbound_id='i',media_id=identity,part=0,
            kind='file',filename='old.txt',expires_at=now()))
        if kind=='chat':db.add(ChatRun(user_id='owner',conversation_id='c',request_id='r',model_id='m',
            request_hash='h',status='invalidated',config={'media_refs':[{'id':identity}]}))
        if kind in ('agent','node'):
            db.add(AgentRun(id='agent',user_id='owner',request_id='r',request_hash='h',title='t',goal='g',
                config={'media_refs':[{'id':identity}]} if kind=='agent' else {}))
            if kind=='node':db.add(AgentNode(run_id='agent',agent_id='a',key='a',title='t',instruction='g',capability='reason',context={'media_refs':[{'id':identity}]}))
        if kind=='task':db.add(WorkTask(user_id='owner',request_id='r',request_hash='h',skill_id='test',title='t',input={'media_id':identity}))
        if kind=='memory':db.add(Memory(user_id='owner',content='derived',category='profile',provenance={'media':identity}))
        if kind=='candidate':db.add(Candidate(user_id='owner',content='derived',category='profile',source_id=identity,fingerprint='f',reason='r'))
        if kind=='plugin':db.add(PluginInvocation(user_id='owner',installation_id='p',release_id='r',capability_id='a',
            request_id='req',request_hash='h',input=json.dumps({'media_id':identity})))
        if kind=='job':db.add(Job(key='in-flight',kind='future',payload={'nested':{'media_id':identity}}))
    assert client.post(ROOT+'/sweep-previews').json()['items']==[]
    assert status(identity)=='ready'


def test_referenced_prefix_does_not_starve_other_candidates_and_batch_is_bounded(client):
    held=[file(age=10) for _ in range(25)]
    with SessionLocal.begin() as db:
        db.add_all(MediaLink(media_id=i,message_id='historical') for i in held)
    unused=[file() for _ in range(21)]
    p=client.post(ROOT+'/sweep-previews').json()
    assert len(p['items'])==20 and p['has_more'] and p['bytes']==60
    assert set(r['id'] for r in p['items'])<=set(unused)
    result=collect(client,p)
    assert result.status_code==200,result.text
    assert result.json()['marked']==20
    assert len(client.post(ROOT+'/sweep-previews').json()['items'])==1
    assert all(status(i)=='ready' for i in held)


@pytest.mark.parametrize('change',['reference','protection','version','policy'])
def test_preview_is_rechecked_and_never_expands(client,change):
    identity=file(); p=client.post(ROOT+'/sweep-previews').json(); newer=file()
    with SessionLocal.begin() as db:
        if change=='reference':db.add(MediaLink(media_id=identity,message_id='just-used'))
        if change=='protection':db.add(MediaProtection(media_id=identity,user_id='owner',workspace_id='personal'))
        if change=='version':db.get(Media,identity).version+=1
    if change=='policy':assert save(client,days=10).status_code==200
    r=collect(client,p)
    if change=='policy':assert r.status_code==409
    else:assert r.status_code==200 and r.json()['marked']==0 and r.json()['skipped']==1
    assert status(identity)=='ready' and status(newer)=='ready'


def test_expired_or_different_login_cannot_confirm(client,monkeypatch):
    from lingxi import media_retention as mr
    file(); p=client.post(ROOT+'/sweep-previews').json()
    actual=now();monkeypatch.setattr(mr,'now',lambda:actual+302)
    assert collect(client,p).status_code==409
    monkeypatch.undo()
    login=client.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
    client.headers['X-CSRF-Token']=login.json()['csrf']
    assert collect(client,p).status_code==409


def test_repeated_confirmation_is_idempotent_and_physical_cleanup_is_deferred(client):
    identity=file(); p=client.post(ROOT+'/sweep-previews').json()
    first=collect(client,p);assert first.status_code==200,first.text
    assert collect(client,p).json()==first.json()
    assert storage(identity,'blob').exists()
    assert client.get('/api/media/'+identity+'/content').status_code==410
    with SessionLocal.begin() as db:
        job=db.scalar(select(Job).where(Job.kind=='media_cleanup'))
        job.status,job.lease_token,job.lease_until='running',uid(),now()+60
    cleanup_media(job)
    assert not storage(identity,'blob').exists()
    with SessionLocal() as db:
        assert db.get(Media,identity).cleanup_status=='completed'
        assert db.scalar(select(func.count()).select_from(MediaRetentionRun))==1
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='media_recycled'))==1
    assert client.get(ROOT+'/runs').json()[0]['marked']==1


def test_protection_versions_are_independent_and_manual_delete_still_allowed(client):
    identity=file()
    p=client.put('/api/media/'+identity+'/protection',json={'version':0,'media_version':1,'protected':True})
    assert p.status_code==200,p.text
    assert p.json()['version']==1 and p.json()['protection_version']==1 and p.json()['protected']
    assert client.put('/api/media/'+identity+'/protection',json={'version':0,'media_version':1,'protected':False}).status_code==409
    assert client.post(ROOT+'/sweep-previews').json()['items']==[]
    assert client.request('DELETE','/api/media/'+identity,json={'version':1}).status_code==200


def test_disable_and_backup_maintenance_stop_background_collection(client):
    from lingxi.backups import BackupRun
    identity=file();assert save(client,enabled=True).status_code==200;age_policy()
    with SessionLocal.begin() as db:
        db.add(BackupRun(id='backup',user_id='owner',request_id='r',kind='backup',status='running'))
    sweep(); assert status(identity)=='ready'
    with SessionLocal.begin() as db:db.get(BackupRun,'backup').status='succeeded'
    assert save(client,enabled=False).status_code==200;age_policy();sweep()
    assert status(identity)=='ready'


def test_restore_keeps_latest_protection_closes_policy_and_replays_deletions(client):
    from lingxi.backups import checkpoint, quarantine
    from test_backups import schema
    with SessionLocal.begin() as db:schema(db)
    held=file(); deleted=file()
    assert save(client,enabled=True).status_code==200
    assert client.put('/api/media/'+held+'/protection',json={'version':0,'media_version':1,'protected':True}).status_code==200
    p=client.post(ROOT+'/sweep-previews').json(); assert collect(client,p).json()['marked']==1
    latest=checkpoint()
    with SessionLocal.begin() as db:
        db.get(MediaProtection,held).protected=False
        item=db.get(Media,deleted);item.status='ready';item.version=1
    quarantine(latest)
    with SessionLocal() as db:
        assert db.get(MediaProtection,held).protected
        assert not db.get(MediaRetentionPolicy,'owner').enabled
        assert db.get(MediaRetentionPolicy,'owner').version>latest['media_retention']['policies'][0]['version']
        assert db.get(Media,deleted).status=='deleted'


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL serialization')
def test_concurrent_confirmations_record_one_deletion(client):
    file();p=client.post(ROOT+'/sweep-previews').json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:collect(client,p),range(2)))
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    assert results[0].json()==results[1].json()
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(MediaRetentionRun))==1


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL serialization')
def test_concurrent_workers_collect_once_per_interval(client):
    identity=file();assert save(client,enabled=True).status_code==200;age_policy()
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:sweep(),range(2)))
    assert status(identity)=='deleted'
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(MediaRetentionRun))==1


@pytest.mark.parametrize('table',['media_retention_policies','media_protections','media_retention_runs'])
def test_migration_refuses_loss_of_any_new_user_state(client,table):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path=Path(__file__).parents[1]/'migrations/versions/0051_media_retention.py'
    spec=importlib.util.spec_from_file_location('retention_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    identity=file()
    if table=='media_retention_policies':assert save(client).status_code==200
    if table=='media_protections':
        assert client.put('/api/media/'+identity+'/protection',json={'version':0,'media_version':1,'protected':True}).status_code==200
    if table=='media_retention_runs':assert collect(client).status_code==200
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError,match='回收配置'):migration.downgrade()


def test_empty_migration_downgrade_upgrade_roundtrip(client):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec=importlib.util.spec_from_file_location('retention_roundtrip',Path(__file__).parents[1]/'migrations/versions/0051_media_retention.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert 'media_protections' not in inspect(connection).get_table_names()
            migration.upgrade()
            assert 'media_protections' in inspect(connection).get_table_names()


def test_restore_rejects_changed_owner_before_any_mutation(client):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    with SessionLocal.begin() as db:schema(db)
    identity=file();save(client,enabled=True)
    assert client.put('/api/media/'+identity+'/protection',json={'version':0,'media_version':1,'protected':True}).status_code==200
    data=checkpoint();data['media_retention']['protections'][0]['user_id']='different'
    with pytest.raises(ValueError,match='protection'):quarantine(data)
    assert client.get(ROOT).json()['enabled']
    assert client.get('/api/auth/me').status_code==200


def test_restore_closes_policy_present_only_in_restored_snapshot(client):
    from lingxi.media_retention import restore
    save(client,enabled=True)
    before=client.get(ROOT).json()['version']
    with SessionLocal.begin() as db:restore(db,{'policies':[],'protections':[]})
    after=client.get(ROOT).json()
    assert not after['enabled'] and after['version']>before
