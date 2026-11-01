import importlib.util
import io
import json
from pathlib import Path
import tarfile
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from lingxi.api import app
from lingxi.backups import BackupAgent, BackupPolicy, BackupRun, agent_claim, checkpoint, finish, paused, quarantine
from lingxi.db import SessionLocal, now
from lingxi.models import Job, LoginSession, Memory, Provider, User
from lingxi.security import encrypt


def request(client,**kwargs):
    return client.post('/api/admin/backups',json={'request_id':'backup-request-0001','confirmed':True,**kwargs})


def test_backup_auth_csrf_confirmation(client):
    with TestClient(app) as stranger:assert stranger.get('/api/admin/backups').status_code==401
    assert request(client,confirmed=False).status_code==422
    csrf=client.headers.pop('X-CSRF-Token');assert request(client).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert request(client,archive_id='../bad').status_code==422


def test_backup_creation_idempotence_and_queue_cancel(client):
    first=request(client);assert first.status_code==200
    assert request(client).json()['id']==first.json()['id']
    assert request(client,request_id='other-backup-request').status_code==409
    assert client.post('/api/admin/backups/'+first.json()['id']+'/cancel').json()['status']=='cancelled'
    assert request(client,request_id='other-backup-request').status_code==200


def test_backup_does_not_expose_credentials_or_files(client):
    request(client)
    result=client.get('/api/admin/backups').json()
    assert not result['agent_online'] and not result['maintenance']
    assert not any(k in result['runs'][0] for k in ('path','payload','key','users','factors'))


def test_policy_versions_schedule_catches_up_once(client):
    body={'version':0,'enabled':True,'interval_hours':24}
    assert client.put('/api/admin/backups/policy',json=body).status_code==200
    assert client.put('/api/admin/backups/policy',json=body).status_code==409
    with SessionLocal.begin() as db:db.scalar(select(BackupPolicy)).next_at=now()-86400*10
    claimed=agent_claim();assert claimed['kind']=='backup'
    assert agent_claim() is None
    finish({'id':claimed['id'],'status':'succeeded','digest':'a'*64,'bytes':123})
    assert agent_claim() is None
    data=client.get('/api/admin/backups').json();assert len(data['runs'])==1 and data['agent_online']


def test_maintenance_stops_writes_and_worker_claims(client):
    result=request(client).json();claimed=agent_claim();assert claimed['id']==result['id']
    assert client.post('/api/conversations',json={'title':'blocked'}).status_code==503
    assert client.get('/api/conversations').status_code==200
    assert client.post('/api/admin/backups/'+result['id']+'/cancel').status_code==409
    from lingxi.worker import claim
    with SessionLocal.begin() as db:db.add(Job(key='test-pause',kind='extract',payload={}))
    assert claim() is None
    finish({'id':result['id'],'status':'failed','error':'busy'})
    assert client.post('/api/conversations',json={'title':'resumed'}).status_code==200


def test_verify_only_accepts_owned_successful_archive(client):
    first=request(client).json()
    assert request(client,kind='verify',archive_id=first['id'],request_id='verify-request-0001').status_code==409
    agent_claim();finish({'id':first['id'],'status':'succeeded','digest':'f'*64,'bytes':12})
    assert request(client,kind='verify',archive_id=first['id'],request_id='verify-request-0001').status_code==200
    agent_claim()
    with SessionLocal() as db:assert not paused(db)
    assert client.post('/api/conversations',json={'title':'verification permits chat'}).status_code==200


def test_backup_workspace_isolation(client):
    with SessionLocal.begin() as db:
        db.add(BackupRun(id='1'*32,user_id='owner',workspace_id='other',request_id='other-request',kind='backup',status='succeeded',archive_id='1'*32))
    assert client.get('/api/admin/backups').json()['runs']==[]
    assert client.post('/api/admin/backups/'+'1'*32+'/cancel').status_code==404
    assert request(client,kind='verify',archive_id='1'*32).status_code==404


def schema(db):
    db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
    db.execute(text('DELETE FROM alembic_version'));db.execute(text("INSERT INTO alembic_version VALUES ('0015')"))


def test_restore_keeps_latest_auth_memory_deletions_and_quarantines(client):
    from lingxi.auth_totp import TwoFactor
    from lingxi.memory import MemoryService
    with SessionLocal.begin() as db:
        schema(db)
        db.add(Memory(id='latest-memory',user_id='owner',content='当前纠正事实',category='preferences',source_ids=[]))
        db.add(Memory(id='forgotten-memory',user_id='owner',content='应当删除',category='preferences',source_ids=[]))
        db.add(Provider(id='provider',name='provider',base_url='https://example.com',encrypted_key=encrypt('test-key')))
        db.add(TwoFactor(user_id='owner',enabled=True,encrypted_secret=encrypt('test-secret'),recovery_hashes=['hashed-code'],last_counter=123))
    with SessionLocal.begin() as db:MemoryService().forget(db,db.get(Memory,'forgotten-memory'))
    latest=checkpoint()
    with SessionLocal.begin() as db:
        db.get(Memory,'latest-memory').content='旧事实'
        db.get(Memory,'forgotten-memory').content='旧备份内容';db.get(Memory,'forgotten-memory').status='active'
        db.scalar(select(TwoFactor)).enabled=False
        db.add(Job(key='do-not-repeat',kind='task',payload={}))
    quarantine(latest)
    with SessionLocal() as db:
        assert db.get(Memory,'latest-memory').content=='当前纠正事实'
        assert db.get(Memory,'forgotten-memory').status=='deleted'
        assert db.scalar(select(TwoFactor)).enabled and db.scalar(select(TwoFactor)).last_counter==123
        assert not db.scalar(select(Provider)).enabled
        assert list(db.scalars(select(LoginSession)))==[]
        assert db.scalar(select(Job).where(Job.key=='do-not-repeat')).status=='failed'
        assert all(job.kind in ('index','cleanup','media_cleanup') for job in db.scalars(select(Job).where(Job.status.in_(['pending','running']))))


def test_restore_rejects_key_and_owner_mismatch_before_writes(client):
    with SessionLocal.begin() as db:schema(db)
    data=checkpoint();data['key_fingerprint']='wrong'
    with pytest.raises(ValueError,match='key mismatch'):quarantine(data)
    data=checkpoint();data['users'][0]['id']='stranger'
    with pytest.raises(ValueError,match='Owner mismatch'):quarantine(data)
    with SessionLocal() as db:assert db.scalar(select(LoginSession)) is not None


def runner_module():
    path=Path(__file__).parents[2]/'deploy/backup.py'
    spec=importlib.util.spec_from_file_location('backup_host_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def tar(path,name,kind=None):
    with tarfile.open(path,'w') as archive:
        info=tarfile.TarInfo(name);info.size=0
        if kind:info.type=kind;info.linkname='/etc/passwd'
        archive.addfile(info,io.BytesIO())


@pytest.mark.parametrize('name,kind',[('../escape',None),('/runtime/file',None),('runtime/../escape',None),('runtime/link',tarfile.SYMTYPE),('runtime/link',tarfile.LNKTYPE),('runtime/device',tarfile.CHRTYPE),('other/file',None)])
def test_runner_rejects_unsafe_archive_members(tmp_path,name,kind):
    runner=runner_module();path=tmp_path/'runtime.tar';tar(path,name,kind)
    with pytest.raises(runner.BackupError):runner.validate_tar(path,'runtime')


def test_runner_verifies_hash_size_scope_and_pg_format(tmp_path):
    runner=runner_module();folder=tmp_path/uuid.uuid4().hex;folder.mkdir()
    for name in ('main.dump','hindsight.dump'):(folder/name).write_bytes(b'PGDMPsynthetic')
    for name in ('runtime','resources'):tar(folder/(name+'.tar'),name+'/test')
    manifest={'format':1,'archive_id':folder.name,'files':{p.name:{'bytes':p.stat().st_size,'sha256':runner.digest(p)} for p in folder.iterdir()}}
    (folder/'manifest.json').write_text(json.dumps(manifest))
    assert runner.verify(folder)==manifest
    (folder/'main.dump').write_bytes(b'PGDMPtampered')
    with pytest.raises(runner.BackupError,match='digest'):runner.verify(folder)


def test_runner_fails_without_resuming_when_completion_unknown(tmp_path,monkeypatch):
    runner=runner_module();instance=runner.Runner.__new__(runner.Runner);instance.directory=tmp_path
    calls=[]
    def core(action,data=None,offline=False):
        if action=='claim':return {'id':'a'*32,'kind':'backup','archive_id':'a'*32}
        if action=='idle':return {'idle':True}
        if action=='finish':raise runner.BackupError('database unavailable')
    instance.core=core;instance.containers=lambda:{'core':{'id':'synthetic','running':True}}
    instance.stop=lambda services:calls.append('stop')
    instance.resume=lambda services:calls.append('resume')
    instance.snapshot=lambda *args:(_ for _ in ()).throw(runner.BackupError('snapshot failure'))
    with pytest.raises(runner.BackupError,match='Maintenance retained'):instance.once()
    assert calls==['stop'] and (tmp_path/'in-progress.json').exists()


def test_runner_restore_missing_helper_fails_before_checkpoint_or_maintenance(tmp_path,monkeypatch):
    module=runner_module();instance=module.Runner.__new__(module.Runner);instance.directory=tmp_path
    monkeypatch.setattr(module,'verify',lambda path:{'files':{}})
    instance.containers=lambda:{'core':{'image':'missing'}}
    calls=[]
    def command(args,**kwargs):
        calls.append(args);raise module.BackupError('missing image')
    instance.command=command
    instance.core=lambda *a,**kw:pytest.fail('Must not read or write the database')
    instance.stop=lambda *a:pytest.fail('Must not stop services')
    with pytest.raises(module.BackupError,match='missing image'):instance.restore('a'*32)
    assert len(calls)==1 and '--pull' in calls[0] and 'never' in calls[0]
    assert not (tmp_path/'in-progress.json').exists()


def test_runner_resumes_core_before_web_and_preserves_stopped_worker(monkeypatch):
    module=runner_module();instance=module.Runner.__new__(module.Runner);calls=[]
    def command(args,**kwargs):
        calls.append(args)
        return json.dumps([{'State':{'Health':{'Status':'healthy'}}}]).encode() if 'inspect' in args else b''
    instance.command=command
    instance.resume({'web':{'id':'web','running':True},'worker':{'id':'worker','running':False},'core':{'id':'core','running':True}})
    assert calls==[['docker','start','core'],['docker','inspect','core'],['docker','start','web']]


def test_runner_does_not_inherit_confirmation_stdin(monkeypatch):
    module=runner_module();instance=module.Runner.__new__(module.Runner);instance.project=Path('.')
    seen=[]
    def run(args,**kwargs):
        seen.append(kwargs)
        return type('Result',(),{'stdout':b'{}'})()
    monkeypatch.setattr(module.subprocess,'run',run)
    instance.command(['example']);instance.command(['example'],payload=b'{}')
    assert seen[0]['stdin']==module.subprocess.DEVNULL and seen[1]['stdin'] is None


def test_runner_file_restore_mounts_only_explicit_target_without_network(tmp_path,monkeypatch):
    module=runner_module();instance=module.Runner.__new__(module.Runner)
    for name in ('main.dump','hindsight.dump','runtime.tar','resources.tar'):(tmp_path/name).write_bytes(b'fixture')
    calls=[]
    def run(args,**kwargs):
        calls.append(args);return type('Result',(),{'returncode':0})()
    monkeypatch.setattr(module.subprocess,'run',run)
    instance.restore_hindsight_ownership=lambda container:None;instance.core=lambda *a,**kw:{}
    instance.apply_archive(tmp_path,{'files':{}},{'db':{'id':'db'},'core':{'image':'pinned','volumes':{'runtime':'runtime-volume','resources':'resource-volume'}}},{})
    helpers=[args for args in calls if 'lingxi.backup_files' in args]
    assert len(helpers)==2
    assert all('--volumes-from' not in args and '--network' in args and '--pull' in args for args in helpers)
    assert 'type=volume,src=resource-volume,dst=/app/resources' in helpers[1]


def test_recovery_records_interruption_instead_of_success(client):
    with SessionLocal.begin() as db:
        schema(db)
        db.add(BackupRun(user_id='owner',request_id='restore-interrupted',kind='restore',status='running'))
    data=checkpoint();data['restore_outcome']='interrupted';quarantine(data)
    with SessionLocal() as db:
        assert db.scalar(select(BackupRun).where(BackupRun.request_id=='restore-interrupted')).status=='interrupted'


def test_runner_rejects_moved_image_tag_without_pinning_or_stopping():
    module=runner_module();instance=module.Runner.__new__(module.Runner);instance.project_name='fixture'
    calls=[]
    def command(args,**kwargs):
        calls.append(args)
        if args[1]=='ps':return b'core'
        if args[1]=='inspect':return json.dumps([{'Config':{'Labels':{'com.docker.compose.service':'core'},'Image':'core:latest'},'Id':'core','Image':'old-digest','State':{'Running':True}}]).encode()
        if args[1:3]==['image','inspect']:return json.dumps([{'Id':'new-digest'}]).encode()
        pytest.fail('No tag mutation or stopping is allowed after mismatch')
    instance.command=command
    with pytest.raises(module.BackupError,match='tag changed'):instance.containers()


def test_offline_core_uses_pinned_override_and_cleans_file(tmp_path):
    module=runner_module();instance=module.Runner.__new__(module.Runner)
    instance.directory=tmp_path;instance.project_name='fixture';instance.offline_image='pinned-runtime:exact'
    calls=[]
    def command(args,**kwargs):
        path=Path(args[args.index('run')-1])
        assert json.loads(path.read_text())=={'services':{'core':{'image':'pinned-runtime:exact','pull_policy':'never'}}}
        assert '--pull' in args and 'never' in args
        calls.append(path);return b'{}'
    instance.command=command
    assert instance.core('checkpoint',offline=True)=={}
    assert len(calls)==1 and not calls[0].exists()
