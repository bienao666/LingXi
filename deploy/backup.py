#!/usr/bin/env python3
"""Linux host backup runner. No Docker socket or archive download in the Web app."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time


class BackupError(Exception):
    pass


def identity(value):
    if not re.fullmatch('[a-f0-9]{32}',value):raise BackupError('Invalid archive identifier')
    return value


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024**2),b''):result.update(chunk)
    return result.hexdigest()


def atomic_json(path,data):
    temp=path.with_name(path.name+'.tmp')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def validate_tar(path,root):
    with tarfile.open(path,'r:') as archive:
        count=0
        for member in archive:
            count+=1
            parts=member.name.rstrip('/').split('/')
            if count>200000 or parts[0]!=root or any(p in ('','..','.') for p in parts) or '\\' in member.name or not (member.isfile() or member.isdir()):
                raise BackupError('Unsafe archive member')


def verify(directory):
    if directory.is_symlink() or not directory.is_dir():raise BackupError('Archive missing')
    manifest=directory/'manifest.json'
    if manifest.is_symlink() or manifest.stat().st_size>65536:raise BackupError('Invalid manifest')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('format')!=1 or data.get('archive_id')!=directory.name:raise BackupError('Archive manifest mismatch')
    required={'main.dump','hindsight.dump','runtime.tar','resources.tar'}
    allowed=required|{'qq.tar','qq-config.tar'}
    if not required.issubset(data['files']) or set(data['files'])-allowed:raise BackupError('Invalid archive scope')
    for name,record in data['files'].items():
        path=directory/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=record['bytes'] or digest(path)!=record['sha256']:raise BackupError('Archive digest mismatch')
        if name.endswith('.dump'):
            with path.open('rb') as stream:
                if stream.read(5)!=b'PGDMP':raise BackupError('Invalid PostgreSQL dump')
    for name,root in [('runtime.tar','runtime'),('resources.tar','resources'),('qq.tar','QQ'),('qq-config.tar','config')]:
        if name in data['files']:validate_tar(directory/name,root)
    return data


class Runner:
    def __init__(self,project,directory,project_name='lingxi'):
        self.project=Path(project).resolve();self.directory=Path(directory).resolve();self.project_name=project_name
        if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}',project_name):raise BackupError('Invalid compose project')
        if not (self.project/'compose.yaml').is_file():raise BackupError('Compose project missing')
        if self.directory==self.project or self.directory in self.project.parents:raise BackupError('Backup directory cannot replace project')
        if Path(directory).is_symlink():raise BackupError('Backup directory cannot be a symlink')
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.directory.stat().st_mode&0o077:raise BackupError('Backup directory must have mode 0700')

    def command(self,args,*,payload=None,output=None,timeout=900):
        try:
            result=subprocess.run(args,cwd=self.project,input=payload,stdin=subprocess.DEVNULL if payload is None else None,stdout=output or subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=timeout)
            return result.stdout if output is None else b''
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,OSError) as exc:
            # Underlying output can contain paths, SQL records or credentials.
            self.private_error(getattr(exc,'stderr',None))
            raise BackupError('Host operation failed; inspect private server logs') from None

    def private_error(self,details):
        if not details:return
        try:
            fd=os.open(self.directory/'runner-error.log',os.O_CREAT|os.O_TRUNC|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(details[-32768:])
        except OSError:pass

    def core(self,action,data=None,offline=False):
        module='lingxi.backups'
        if action.startswith('migration:'):
            module,action='lingxi.workspace_migrations',action.removeprefix('migration:')
        prefix=['docker','compose','-p',self.project_name,'-f','compose.yaml']
        with contextlib.ExitStack() as stack:
            if offline:
                image=getattr(self,'offline_image',None)
                if not image:raise BackupError('Offline helper image was not pinned')
                override=Path(stack.enter_context(tempfile.TemporaryDirectory(dir=self.directory)))/'compose.json'
                atomic_json(override,{'services':{'core':{'image':image,'pull_policy':'never'}}})
                prefix+=['-f',str(override),'run','--rm','-T','--no-deps','--pull','never','core']
            else:prefix+=['exec','-T','core']
            # Import once under its canonical name. Running a domain with ORM
            # tables as __main__ can register the same tables again when a
            # quarantine dependency imports the HTTP application.
            out=self.command(prefix+['python','-c','from '+module+' import main; main()',action],payload=json.dumps(data).encode() if data is not None else None)
        return json.loads(out)

    def containers(self):
        identifiers=self.command(['docker','ps','-aq','--filter','label=com.docker.compose.project='+self.project_name]).decode().split()
        if not identifiers:raise BackupError('Project containers missing')
        metadata=json.loads(self.command(['docker','inspect',*identifiers]))
        result={}
        for item in metadata:
            service=item['Config']['Labels'].get('com.docker.compose.service')
            if service in ('core','worker','web','hindsight','napcat','db'):
                if service in result:raise BackupError('Only one instance per service is supported')
                result[service]={'id':item['Id'],'running':item['State']['Running'],'image':item['Image']}
                if service=='core':
                    # containerd may expose an index digest that cannot be used
                    # as a docker-run image name. Resolve the installed name,
                    # reject moved tags, then pin a local alias before stopping.
                    installed=item['Config']['Image']
                    image=json.loads(self.command(['docker','image','inspect',installed]))[0]
                    if image['Id']!=item['Image']:
                        raise BackupError('Core image tag changed; recreate Core from the intended local image before backing up or restoring')
                    alias='lingxi-backup-runtime:'+image['Id'].removeprefix('sha256:')
                    self.command(['docker','image','tag',installed,alias])
                    result[service]['image']=alias
                    mounts={row['Destination']:row for row in item['Mounts']}
                    result[service]['volumes']={}
                    for target in ('runtime','resources'):
                        mounted=mounts.get('/app/'+target)
                        if not mounted or mounted['Type']!='volume' or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]*',mounted['Name']):raise BackupError('Backup restore requires named runtime and resource volumes')
                        result[service]['volumes'][target]=mounted['Name']
        if not {'core','worker','web','db','hindsight'}.issubset(result):raise BackupError('Workspace service set incomplete')
        self.offline_image=result['core']['image']
        return result

    def preflight_restore(self,services,manifest):
        if 'qq.tar' in manifest['files'] and ('napcat' not in services or 'qq-config.tar' not in manifest['files']):
            raise BackupError('Archive requires both NapCat directories and an existing NapCat target')
        # Never pull code during recovery. Test the exact local helper before
        # entering maintenance or overwriting a database.
        self.command(['docker','run','--rm','--pull','never','--network','none',
            '--entrypoint','python',services['core']['image'],'-c',
            'import lingxi.backup_files, lingxi.backups'])
        self.offline_image=services['core']['image']

    def stop(self,services):
        ids=[row['id'] for name,row in services.items() if name!='db' and row['running']]
        if ids:self.command(['docker','stop','--time','60',*ids],timeout=180)

    def resume(self,services):
        # Preserve services originally stopped; never start NapCat for the user.
        for name in ('core','worker','hindsight','napcat','web'):
            row=services.get(name)
            if not row or not row['running']:continue
            self.command(['docker','start',row['id']],timeout=180)
            if name=='core':
                for _ in range(60):
                    state=json.loads(self.command(['docker','inspect',row['id']]))[0]['State']
                    if state.get('Health',{}).get('Status')=='healthy':break
                    time.sleep(1)
                else:raise BackupError('Core did not become ready; dependent services remain stopped')

    def save(self,path,args):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as output:self.command(args,output=output)
        with path.open('rb') as stream:os.fsync(stream.fileno())

    def snapshot(self,request,services):
        archive_id=identity(request['archive_id'])
        path=self.directory/(archive_id+'.partial');path.mkdir(mode=0o700)
        checkpoint=self.core('checkpoint',offline=True)
        # Current safety data is separate from historical archives. It contains
        # encrypted secrets and must never be served or printed by the Web app.
        atomic_json(self.directory/'latest-safety.json',checkpoint)
        db=services['db']['id'];core=services['core']['id']
        for database,name in [('lingxi','main.dump'),('hindsight','hindsight.dump')]:
            self.save(path/name,['docker','exec',db,'pg_dump','-U','lingxi','-Fc','--no-owner','--no-acl','-d',database])
        self.save(path/'runtime.tar',['docker','cp',core+':/app/runtime','-'])
        self.save(path/'resources.tar',['docker','cp',core+':/app/resources','-'])
        if 'napcat' in services:
            for source,name in [('/app/.config/QQ','qq.tar'),('/app/napcat/config','qq-config.tar')]:
                self.save(path/name,['docker','cp',services['napcat']['id']+':'+source,'-'])
        files={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in path.iterdir()}
        manifest={'format':1,'archive_id':archive_id,'created_at':time.time(),'schema':checkpoint['schema'],
            'key_fingerprint':checkpoint['key_fingerprint'],'owner_id':checkpoint['users'][0]['id'],
            'images':{key:value['image'] for key,value in services.items()},'files':files,
            'excluded':['server master key and .env','independent plugin store','remote S3','model cache']}
        atomic_json(path/'manifest.json',manifest)
        target=self.directory/archive_id
        if target.exists():raise BackupError('Archive already exists')
        os.rename(path,target)
        verify(target)
        return {'digest':digest(target/'manifest.json'),'bytes':sum(r['bytes'] for r in files.values())}

    def once(self):
        if (self.directory/'in-progress.json').exists():raise BackupError('Run recover before starting another operation')
        request=self.core('claim')
        if not request:return False
        services=None;stopped=False;safe_resume=True
        try:
            if request['kind']=='verify':
                manifest=verify(self.directory/identity(request['archive_id']))
                self.core('finish',{'id':request['id'],'status':'succeeded','digest':digest(self.directory/request['archive_id']/'manifest.json'),'bytes':sum(r['bytes'] for r in manifest['files'].values())})
                return True
            for _ in range(60):
                if self.core('idle')['idle']:break
                time.sleep(1)
            else:
                self.core('finish',{'id':request['id'],'status':'failed','error':'busy'});return True
            services=self.containers()
            atomic_json(self.directory/'in-progress.json',{'request':request,'services':services,'phase':'backup'})
            stopped=True;self.stop(services)
            result=self.snapshot(request,services)
            self.core('finish',{'id':request['id'],'status':'succeeded',**result},offline=True)
        except Exception:
            try:self.core('finish',{'id':request['id'],'status':'failed','error':'runner_failed'},offline=stopped)
            except Exception:
                safe_resume=False
                raise BackupError('Maintenance retained; run recover from the server terminal') from None
            raise
        finally:
            if services and safe_resume:
                self.resume(services)
                (self.directory/'in-progress.json').unlink(missing_ok=True)
        return True

    def recover(self):
        path=self.directory/'in-progress.json'
        if not path.exists():raise BackupError('No interrupted host operation')
        data=json.loads(path.read_text())
        self.offline_image=data['services']['core']['image']
        if data['phase']=='restore':
            rollback_id=identity(data['rollback_id'])
            archive=self.directory/rollback_id;manifest=verify(archive)
            self.preflight_restore(data['services'],manifest)
            if input('Services remain stopped. Type ROLLBACK '+rollback_id+': ')!='ROLLBACK '+rollback_id:raise BackupError('Cancelled')
            safety=json.loads((self.directory/'latest-safety.json').read_text())
            self.apply_archive(archive,manifest,data['services'],{**safety,'restore_outcome':'interrupted'})
            self.resume({k:{**v,'running':False if k=='napcat' else v['running']} for k,v in data['services'].items()})
        elif data['phase'] in ('backup','restore_prepare'):
            self.core('release-maintenance',{'id':data['request']['id']},offline=True)
            self.resume(data['services'])
        else:raise BackupError('Unknown maintenance phase')
        path.unlink()

    def restore(self,archive_id):
        archive=self.directory/identity(archive_id);manifest=verify(archive)
        services=self.containers()
        self.preflight_restore(services,manifest)
        current=self.core('checkpoint')
        if current['schema']!=manifest['schema'] or current['key_fingerprint']!=manifest['key_fingerprint'] or current['users'][0]['id']!=manifest['owner_id']:
            raise BackupError('Restore requires matching schema, encryption key and owner; migrate in an isolated environment first')
        if input('Restore overwrites workspace data and quarantines external access. Type RESTORE '+archive_id+': ')!='RESTORE '+archive_id:raise BackupError('Cancelled')
        if (self.directory/'in-progress.json').exists():raise BackupError('Interrupted host operation exists')
        request=self.core('begin-restore',{'archive_id':archive_id})
        if not self.core('idle')['idle']:
            self.core('finish',{'id':request['id'],'status':'failed','error':'busy'});raise BackupError('Active work; retry after completion')
        atomic_json(self.directory/'in-progress.json',{'request':request,'services':services,'phase':'restore_prepare','archive_id':archive_id})
        self.stop(services)
        # Re-read after graceful shutdown, so accepted writes and security changes
        # that were finishing before maintenance cannot be rolled backwards.
        safety=self.core('checkpoint',offline=True)
        atomic_json(self.directory/'latest-safety.json',safety)
        rollback={'id':__import__('uuid').uuid4().hex,'archive_id':__import__('uuid').uuid4().hex}
        rollback['id']=rollback['archive_id']
        self.snapshot(rollback,services)
        print('Rollback archive:',rollback['archive_id'],flush=True)
        atomic_json(self.directory/'in-progress.json',{'request':request,'services':services,'phase':'restore','archive_id':archive_id,'rollback_id':rollback['archive_id']})
        try:self.apply_archive(archive,manifest,services,safety)
        except BackupError as exc:raise BackupError(str(exc)+'; run recover to apply the recorded rollback archive. Services remain stopped.') from None
        except Exception:raise BackupError('Restore interrupted during final verification; run recover. Services remain stopped.') from None
        self.resume({k:{**v,'running':False if k=='napcat' else v['running']} for k,v in services.items()})
        (self.directory/'in-progress.json').unlink()
        print('Restore verified and quarantined. Sessions revoked; reauthorize models, channels, plugins and contact settings before resuming work.')

    def apply_archive(self,archive,manifest,services,safety,quarantine_action='quarantine',replace_files=False):
        for database,name in [('lingxi','main.dump'),('hindsight','hindsight.dump')]:
            # Input is streamed; never interpolate SQL, paths or credentials.
            with (archive/name).open('rb') as stream:
                result=subprocess.run(['docker','exec','-i',services['db']['id'],'pg_restore','-U','lingxi','--clean','--if-exists','--no-owner','--no-acl','--exit-on-error','-d',database],stdin=stream,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=900)
            if result.returncode:
                self.private_error(result.stderr)
                raise BackupError('Database restore failed; services remain stopped')
        self.restore_hindsight_ownership(services['db']['id'])
        for name,target in [('runtime.tar','runtime'),('resources.tar','resources')]:
            with (archive/name).open('rb') as stream:
                result=subprocess.run(['docker','run','--rm','--pull','never','--network','none','-i','--mount','type=volume,src='+services['core']['volumes'][target]+',dst=/app/'+target,'--user','0','--entrypoint','python',services['core']['image'],'-m','lingxi.backup_files',target]+(['--replace'] if replace_files else []),stdin=stream,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=900)
            if result.returncode:
                self.private_error(result.stderr)
                raise BackupError('File restore failed; services remain stopped')
        if 'qq.tar' in manifest['files']:
            if 'napcat' not in services:raise BackupError('NapCat target missing; services remain stopped')
            for name,target in [('qq.tar','/app/.config'),('qq-config.tar','/app/napcat')]:
                with (archive/name).open('rb') as stream:
                    result=subprocess.run(['docker','cp','-',services['napcat']['id']+':'+target],stdin=stream,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=900)
                if result.returncode:
                    self.private_error(result.stderr)
                    raise BackupError('NapCat restore failed; services remain stopped')
        self.core(quarantine_action,safety,offline=True)

    def restore_hindsight_ownership(self,container):
        # Dumps deliberately omit roles/passwords. Restore application objects
        # only in this database; REASSIGN OWNED would also affect shared objects.
        # Extension objects must remain owned by their privileged installer.
        sql=b"""DO $$ DECLARE r record; BEGIN
FOR r IN SELECT n.oid,n.nspname FROM pg_namespace n
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
 AND n.nspname<>'public' AND NOT EXISTS
 (SELECT 1 FROM pg_depend d WHERE d.classid='pg_namespace'::regclass AND d.objid=n.oid AND d.deptype='e')
 LOOP EXECUTE format('ALTER SCHEMA %I OWNER TO hindsight',r.nspname); END LOOP;
FOR r IN SELECT c.oid,n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','S','v','m','f')
 AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
 AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')))
 LOOP EXECUTE format('ALTER %s %I.%I OWNER TO hindsight', CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,r.nspname,r.relname); END LOOP;
FOR r IN SELECT p.oid,n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) args FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
 AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
 LOOP EXECUTE format('ALTER ROUTINE %I.%I(%s) OWNER TO hindsight',r.nspname,r.proname,r.args); END LOOP;
FOR r IN SELECT t.oid,n.nspname,t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
 WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND t.typtype IN ('e','d')
 AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e')
 LOOP EXECUTE format('ALTER TYPE %I.%I OWNER TO hindsight',r.nspname,r.typname); END LOOP;
END $$;"""
        self.command(['docker','exec','-i',container,'psql','-U','lingxi','-v','ON_ERROR_STOP=1','-d','hindsight'],payload=sql)


@contextlib.contextmanager
def exclusive(directory):
    import fcntl
    fd=os.open(directory/'.runner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise BackupError('Another host runner is active') from None
        yield
    finally:os.close(fd)


@contextlib.contextmanager
def project_exclusive(runner):
    # Independent archive directories must not allow two operators to replace
    # the same Compose project's database concurrently. Run as the same host
    # account; the directory is local, private and independent of archive paths.
    folder=Path('/tmp')/('lingxi-host-locks-'+str(os.getuid()))
    folder.mkdir(mode=0o700,exist_ok=True)
    if folder.is_symlink() or folder.stat().st_uid!=os.getuid() or folder.stat().st_mode&0o077:
        raise BackupError('Unsafe project lock directory')
    key=hashlib.sha256(runner.project_name.encode()).hexdigest()
    lock=folder/key
    lock.mkdir(mode=0o700,exist_ok=True)
    if lock.is_symlink() or lock.stat().st_mode&0o077:raise BackupError('Unsafe project lock')
    with exclusive(lock):
        with exclusive(runner.directory):yield


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['serve','verify','restore','recover']);parser.add_argument('archive_id',nargs='?')
    parser.add_argument('--project-dir',default='.');parser.add_argument('--backup-dir',required=True);parser.add_argument('--project-name',default='lingxi');parser.add_argument('--once',action='store_true');args=parser.parse_args()
    try:
        runner=Runner(args.project_dir,args.backup_dir,args.project_name)
        with project_exclusive(runner):
            if args.action=='verify':verify(runner.directory/identity(args.archive_id or ''));print('Archive hashes, scope, format and safe file entries verified.')
            elif args.action=='restore':runner.restore(args.archive_id or '')
            elif args.action=='recover':runner.recover()
            else:
                while True:
                    try:runner.once()
                    except BackupError as exc:
                        print(str(exc),flush=True)
                        if args.once:raise
                    if args.once:break
                    time.sleep(15)
    except (BackupError,ValueError,KeyError,OSError,EOFError) as exc:
        raise SystemExit('Backup operation failed: '+str(exc)) from None


if __name__=='__main__':main()
