import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from lingxi.api import app
from lingxi.config import settings
from lingxi.db import SessionLocal, now
from lingxi.models import Conversation, Job, LoginSession, Memory, User
from lingxi.security import encrypt
from lingxi import workspace_migrations as m


def schema():
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0050')"))


def ticket(**values):
    return encrypt(json.dumps(dict(purpose='migration-target', instance_id='b'*32,
        origin='https://new.example.com', label='新服务器', schema=m.SCHEMA,
        expires_at=now()+3600) | values))


def preview(client, value=None):
    schema()
    return client.post('/api/workspace-migrations/previews', json={'target_ticket': value or ticket()})


def plan(client):
    value=preview(client)
    assert value.status_code==200, value.text
    body={'request_id':'migration-request-01','token':value.json()['token'],
        'acknowledgement':'freeze_source_before_transfer'}
    response=client.post('/api/workspace-migrations',json=body)
    assert response.status_code==200,response.text
    return response.json(),body


def test_migration_auth_and_csrf(client):
    with TestClient(app) as stranger:
        assert stranger.get('/api/workspace-migrations').status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert preview(client).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.get('/api/workspace-migrations').json()['mode']=='normal'


@pytest.mark.parametrize('values',[
    {'expires_at':0}, {'schema':'0001'}, {'instance_id':'../bad'}, {'label':'bad\nlabel'},
    {'origin':'https://u:p@example.com'}, {'origin':'https://example.com/path'},
    {'origin':'http://testserver'}, {'purpose':'migration-handover'},
    {'origin':'https://example.com?key=secret'}, {'instance_id':'Z'*32},
])
def test_invalid_registrations_do_not_create_records(client,values):
    assert preview(client,ticket(**values)).status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(m.WorkspaceMigration)) is None
        assert m.site(db) is None


def test_tampered_ticket_and_wrong_server_key(client):
    token=ticket()
    assert preview(client,token[:45]+'A'+token[46:]).status_code==409
    from cryptography.fernet import Fernet
    assert preview(client,Fernet(Fernet.generate_key()).encrypt(b'secret').decode()).status_code==409


def test_preview_is_read_only_and_create_is_idempotent(client):
    row,body=plan(client)
    assert client.post('/api/workspace-migrations',json=body).json()['id']==row['id']
    assert client.post('/api/workspace-migrations',json=body|{'token':ticket()}).status_code==409
    data=client.get('/api/workspace-migrations').json()
    assert len(data['runs'])==1
    assert not {'target_ticket','ticket','request_digest','password_hash'} & set(data['runs'][0])
    with SessionLocal() as db:assert not m.paused(db)


def test_preview_bound_to_login_expiry_and_explicit_acknowledgement(client):
    value=preview(client).json()
    data=m.unpack(value['token'],'migration-preview')
    body={'request_id':'preview-login-01','token':value['token'],'acknowledgement':'freeze_source_before_transfer'}
    assert client.post('/api/workspace-migrations',json=body|{'acknowledgement':'yes'}).status_code==422
    for changes in ({'expires_at':now()-1},{'user':'other'},{'workspace':'other'},{'session':'other'}):
        token=encrypt(json.dumps(data|changes))
        assert client.post('/api/workspace-migrations',json=body|{'token':token}).status_code==409


def test_cancel_version_and_other_workspace(client):
    row,_=plan(client)
    path=f"/api/workspace-migrations/{row['id']}/cancel"
    assert client.post(path,json={'version':9}).status_code==409
    with SessionLocal.begin() as db:db.get(m.WorkspaceMigration,row['id']).workspace_id='other'
    assert client.get('/api/workspace-migrations').json()['runs']==[]
    assert client.post(path,json={'version':row['version']}).status_code==404
    with SessionLocal.begin() as db:db.get(m.WorkspaceMigration,row['id']).workspace_id='personal'
    assert client.post(path,json={'version':row['version']}).json()['status']=='cancelled'
    assert client.post(path,json={'version':row['version']}).status_code==409


def test_freeze_fences_http_worker_existing_qq_and_backup_claims(client):
    from lingxi.worker import claim
    from lingxi.backups import agent_claim
    from lingxi.channel_gateway import authorized_channel, open_connection
    row,_=plan(client)
    with SessionLocal.begin() as db:db.add(Job(key='pending-before-freeze',kind='extract',payload={}))
    m.freeze(row['id'])
    for method,path,body in [('POST','/api/conversations',{'title':'blocked'}),
        ('GET','/api/conversations',None),('GET','/api/providers',None),
        ('POST',f"/api/workspace-migrations/{row['id']}/cancel",{'version':2})]:
        assert client.request(method,path,json=body).status_code==503
    assert client.get('/api/auth/me').status_code==200
    assert client.get('/api/workspace-migrations').json()['mode']=='source_frozen'
    assert claim() is None and agent_claim() is None
    with SessionLocal() as db:assert authorized_channel(db,'any','old-socket') is None
    assert open_connection('any','token','123') is None
    # Idempotent freezing, not an escape hatch back to normal.
    assert m.freeze(row['id'])['status']=='freezing'
    assert client.post('/api/auth/logout').status_code==200
    assert client.post('/api/auth/login',json={'username':'owner','password':'test-password-123'}).status_code==200


def test_pending_backup_prevents_freeze(client):
    row,_=plan(client)
    assert client.post('/api/admin/backups',json={'request_id':'backup-before-migration','confirmed':True}).status_code==200
    with pytest.raises(ValueError,match='backup'):m.freeze(row['id'])
    with SessionLocal() as db:assert not m.paused(db)


def test_prepare_empty_target_is_idempotent_and_pauses_business(client):
    schema()
    data=m.prepare_target('新服务器')
    assert m.prepare_target('same target')['ticket']==data['ticket']
    assert m.target_info(data['ticket'])['origin']=='http://testserver'
    assert client.get('/api/workspace-migrations').json()['mode']=='prepared'
    assert client.post('/api/conversations',json={'title':'blocked'}).status_code==503


@pytest.mark.parametrize('asset',['conversation','profile','settings','connection','resource','media-policy','plugin'])
def test_target_rejects_existing_assets_even_disabled(client,asset):
    schema()
    with SessionLocal.begin() as db:
        if asset=='conversation':db.add(Conversation(user_id='owner',title='existing'))
        elif asset=='profile':
            from lingxi.assistants import AssistantProfile
            db.scalar(select(AssistantProfile)).name='customized'
        elif asset=='settings':
            from lingxi.system_settings import WorkspaceSettings
            db.add(WorkspaceSettings(user_id='owner',workspace_id='personal',version=1,values={}))
        elif asset=='connection':
            from lingxi.network import SystemProxy
            db.add(SystemProxy(id='system',enabled=False,encrypted_config=encrypt('{}')))
        elif asset=='resource':
            from lingxi.resources import ResourcePolicy
            db.add(ResourcePolicy(user_id='owner',workspace_id='personal',roots=[],version=1))
        elif asset=='media-policy':
            from lingxi.media_policy import MediaPolicy
            db.get(MediaPolicy,'personal').max_files=3
        else:
            from lingxi.plugins import PluginInstallation
            db.scalar(select(PluginInstallation)).status='disabled'
    with pytest.raises(ValueError,match='Target'):m.prepare_target('refuse nonempty')
    with SessionLocal() as db:assert m.site(db) is None


def test_checkpoint_restore_never_releases_source_gate(client):
    from lingxi.backups import checkpoint,quarantine
    row,_=plan(client);m.freeze(row['id'])
    saved=checkpoint()
    quarantine(saved)
    with SessionLocal.begin() as db:
        m.restore(db,None)
        assert m.site(db).mode=='source_frozen'
    with TestClient(app) as again:
        assert again.get('/health/ready').status_code==200
        assert again.get('/api/conversations').status_code==503


def handover(client,monkeypatch):
    from lingxi.backups import checkpoint
    row,_=plan(client);m.freeze(row['id']);cold=checkpoint()
    sealed=m.seal({'id':row['id'],'manifest_digest':'a'*64,'checkpoint':cold})
    payload={'handover':sealed['handover'],'manifest_digest':'a'*64}
    bundle=m.unpack(payload['handover'],'migration-handover')
    payload['target']={'id':'local','mode':'prepared','instance_id':'b'*32,'origin':'https://new.example.com',
        'ticket':bundle['target_ticket'],'version':2,'migration_id':None}
    monkeypatch.setattr(settings(),'public_origin','https://new.example.com')
    return row,payload


def test_import_activation_quarantines_grants_and_preserves_authority(client,monkeypatch):
    with SessionLocal.begin() as db:db.add(Memory(user_id='owner',content='中文记忆',category='preferences',source_ids=[]))
    row,payload=handover(client,monkeypatch)
    result=m.imported(payload)
    assert result['status']=='awaiting_activation' and result['direction']=='inbound'
    with SessionLocal() as db:
        assert m.site(db).mode=='target_pending'
        assert db.scalar(select(LoginSession)) is None
        assert db.scalar(select(Memory)).content=='中文记忆'
        from lingxi.plugins import PluginInstallation
        assert db.scalar(select(PluginInstallation)).status=='disabled'
    assert m.activate(row['id'])['status']=='completed'
    with SessionLocal() as db:assert not m.paused(db)
    with pytest.raises(ValueError):m.activate(row['id'])


@pytest.mark.parametrize('change',['digest','target','origin','counts'])
def test_import_rejects_mismatched_bundle(client,monkeypatch,change):
    _,payload=handover(client,monkeypatch)
    if change=='digest':payload['manifest_digest']='c'*64
    elif change=='target':payload['target']['instance_id']='c'*32
    elif change=='origin':monkeypatch.setattr(settings(),'public_origin','https://wrong.example.com')
    else:
        data=m.unpack(payload['handover'],'migration-handover');data['counts']['users']=100
        payload['handover']=encrypt(json.dumps(data))
    with pytest.raises(ValueError):m.imported(payload)
    with SessionLocal() as db:assert m.paused(db)


def test_media_hash_and_staged_failed_files(client):
    from lingxi.media import Media,storage
    content=b'authority attachment'
    with SessionLocal.begin() as db:
        db.add(Media(id='a'*32,user_id='owner',request_id='media',filename='a.txt',suffix='.txt',size=len(content),digest=hashlib.sha256(content).hexdigest(),status='ready'))
        db.add(Media(id='staged-failed',user_id='owner',request_id='failed',filename='f.txt',suffix='.txt',size=0,digest='',status='failed'))
    path=storage('a'*32,'blob');path.write_bytes(content)
    try:
        with SessionLocal() as db:m.verify_media(db)
        path.write_bytes(b'X'*len(content))
        with SessionLocal() as db:
            with pytest.raises(ValueError,match='digest'):m.verify_media(db)
    finally:path.unlink(missing_ok=True)


def host():
    sys.path.insert(0,str(Path(__file__).parents[2]/'deploy'))
    spec=importlib.util.spec_from_file_location('migration_host',Path(__file__).parents[2]/'deploy/migrate_workspace.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_host_export_failure_retains_journal_and_never_resumes(tmp_path,monkeypatch):
    module=host();runner=module.MigrationRunner.__new__(module.MigrationRunner);runner.directory=tmp_path
    services={'core':{'id':'c','image':'pinned','running':True}}
    monkeypatch.setattr('builtins.input',lambda prompt:'EXPORT '+'a'*32)
    monkeypatch.setattr(runner,'containers',lambda:services)
    monkeypatch.setattr(runner,'migration',lambda *a,**k:(_ for _ in ()).throw(ValueError('unknown')))
    monkeypatch.setattr(runner,'resume',lambda s:pytest.fail('Must not resume after unknown export'))
    with pytest.raises(module.BackupError):runner.export('a'*32)
    assert json.loads(runner.journal.read_text())['phase']=='export'


def test_host_retry_checks_real_container_identity_and_uses_offline_idle(tmp_path,monkeypatch):
    module=host();runner=module.MigrationRunner.__new__(module.MigrationRunner);runner.directory=tmp_path
    calls=[]
    monkeypatch.setattr(runner,'core',lambda action,**kw:(calls.append((action,kw)) or {'idle':True}))
    runner.wait_idle();assert calls==[('idle',{'offline':True})]
    monkeypatch.setattr(runner,'containers',lambda:{'core':{'id':'new','image':'pinned','running':False}})
    with pytest.raises(module.BackupError,match='changed'):runner.current_services({'core':{'id':'old','image':'pinned','running':True}})


def test_host_status_resume_does_not_start_old_workers_qq_or_hindsight(tmp_path,monkeypatch):
    module=host();runner=module.MigrationRunner.__new__(module.MigrationRunner);runner.directory=tmp_path
    seen=[];monkeypatch.setattr(runner,'resume',lambda services:seen.append(services))
    runner.status_services({name:{'running':True} for name in ('core','web','worker','hindsight','napcat')})
    assert {key for key,value in seen[0].items() if value['running']}=={'core','web'}


def test_rollback_recovers_prepared_target_without_destroying_defaults(client):
    schema();m.prepare_target('target')
    with SessionLocal() as db:data={'migrations':m.checkpoint(db)}
    assert m.recover_target(data)=={'mode':'prepared'}
    with SessionLocal() as db:
        m.pristine(db)
        assert db.scalar(select(LoginSession)) is None


def test_target_with_server_files_is_not_empty(client,tmp_path,monkeypatch):
    schema();monkeypatch.setattr(settings(),'resource_dir',tmp_path)
    (tmp_path/'existing.md').write_text('must not overwrite',encoding='utf-8')
    with pytest.raises(ValueError,match='resource volume'):m.prepare_target('nonempty-files')
    assert (tmp_path/'existing.md').read_text()=='must not overwrite'


def test_cli_helper_imports_orm_module_once_under_canonical_name(tmp_path,monkeypatch):
    module=host();runner=module.MigrationRunner.__new__(module.MigrationRunner)
    runner.directory=tmp_path;runner.project_name='qa'
    calls=[]
    monkeypatch.setattr(runner,'command',lambda args,**kw:(calls.append(args) or b'{}'))
    runner.migration('activate',{'id':'a'*32})
    assert calls[0][-4:]==['python','-c','from lingxi.workspace_migrations import main; main()','activate']


def test_host_import_failure_requires_recovery_before_retry(tmp_path,monkeypatch):
    module=host();runner=module.MigrationRunner.__new__(module.MigrationRunner);runner.directory=tmp_path
    services={key:{'id':key,'running':True,'image':'pinned'} for key in ('core','web','worker','db','hindsight')}
    payload={'handover':'encrypted','manifest_digest':'a'*64}
    monkeypatch.setattr(runner,'bundle',lambda identity:(tmp_path,{'images':{k:'pinned' for k in services}},payload))
    monkeypatch.setattr(runner,'migration',lambda *a,**k:{'id':'a'*32,'target':{'mode':'prepared'}})
    monkeypatch.setattr(runner,'containers',lambda:services)
    monkeypatch.setattr(runner,'preflight_restore',lambda *a:None)
    monkeypatch.setattr(runner,'core',lambda *a,**k:{'idle':True})
    monkeypatch.setattr(runner,'stop',lambda *a:None)
    monkeypatch.setattr(runner,'snapshot',lambda *a:None)
    monkeypatch.setattr('builtins.input',lambda *a:'IMPORT '+'a'*32)
    calls=[]
    def interrupted(*a,**kw):
        calls.append(kw);raise module.BackupError('injected interrupted restore')
    monkeypatch.setattr(runner,'apply_archive',interrupted)
    monkeypatch.setattr(runner,'resume',lambda *a:pytest.fail('unknown import cannot resume services'))
    with pytest.raises(module.BackupError,match='interrupted'):runner.import_archive('a'*32)
    assert calls[0]['replace_files'] is True and calls[0]['quarantine_action']=='migration:imported'
    assert json.loads(runner.journal.read_text())['phase']=='import'
    with pytest.raises(module.BackupError,match='Recover'):runner.import_archive('a'*32)
