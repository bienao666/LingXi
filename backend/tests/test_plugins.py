"""Plugin lifecycle, authority boundaries and real durable-worker integration."""
import copy
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Job, User
from lingxi.plugin_manifest import Manifest, digest, resolve
from lingxi.plugins import (PluginRelease, PluginInstallation, PluginSnapshot, PluginPreview, PluginInvocation,
    seed_bundled, check_installation, require_capability)
from lingxi.worker import claim, perform


def preview(client, version='1.0.0', action='install', plugin='lingxi.writing'):
    response=client.post('/api/plugin-installations/preview',json={'plugin_id':plugin,'plugin_version':version,'action':action})
    assert response.status_code==200,response.text
    return response.json()


def confirm(client, p):
    return client.post(f"/api/plugin-installations/previews/{p['id']}/confirm",json={
        'digest':p['digest'],'approved_permissions':p['plan']['permissions']})


def install(client,version='1.0.0',action='install',plugin='lingxi.writing'):
    r=confirm(client,preview(client,version,action,plugin))
    assert r.status_code==200,r.text
    return next(row for row in r.json() if row['plugin_id']==plugin)


def action(client,row,kind):
    return client.post(f"/api/plugin-installations/{row['id']}/{kind}",json={'version':row['version']})


def invocation(client,row,request='plugin-request-001'):
    r=client.post('/api/plugin-invocations',json={'installation_id':row['id'],
        'capability_id':'writing.outline','content':'资料：下周进行项目演示，时间尚未确定。','request_id':request})
    assert r.status_code==200,r.text
    return r.json()


def release(plugin,version='1.0.0',deps=(),status='published',core='>=0.2.0,<0.3.0'):
    spec=Manifest(plugin_id=plugin,publisher=plugin.split('.')[0],version=version,name=plugin,
        description='隔离测试清单',license='MIT',core=core,costs='模型费用未知',data_scope='明确输入',
        capabilities=[{'id':'skill.process','name':'处理资料','kind':'skill','permissions':['model.chat'],'instruction':'整理提纲'}],
        dependencies=[{'plugin_id':p,'constraint':c}for p,c in deps]).model_dump()
    return PluginRelease(id=digest({'plugin_id':plugin,'version':version}),plugin_id=plugin,version=version,
        digest=digest(spec),manifest=spec,status=status,source='bundled')


def add(*rows):
    with SessionLocal.begin() as db:db.add_all(rows)


def test_bundled_catalog_separates_installed_from_available(client):
    rows=client.get('/api/plugin-catalog').json()
    assert {(r['plugin_id'],r['version']) for r in rows}=={('lingxi.documents','1.0.0'),('lingxi.builtin','1.0.0'),('lingxi.writing','1.0.0'),('lingxi.writing','1.1.0')}
    assert len(client.get('/api/plugin-installations').json())==1
    assert all(r['source']=='bundled' and r['digest']for r in rows)
    assert client.get('/api/plugin-catalog?q=写作').json()[0]['name']=='写作伙伴'


def test_preview_has_all_data_and_no_installation_side_effect(client):
    p=preview(client)
    assert p['plan']['entries'][0]['new_permissions']==['model.chat']
    assert p['plan']['entries'][0]['release']['costs']
    assert p['plan']['location']=='服务器 Core／worker'
    assert len(client.get('/api/plugin-installations').json())==1


def test_confirmation_is_atomic_idempotent_and_creates_snapshot(client):
    p=preview(client)
    a=confirm(client,p);b=confirm(client,p)
    assert a.status_code==200 and a.json()==b.json()
    row=a.json()[0]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PluginSnapshot).where(PluginSnapshot.installation_id==row['id']))==1


@pytest.mark.parametrize('change',['expired','rejected','superseded','wrong-digest','permissions','catalog-changed','tampered-plan','other-session'])
def test_stale_or_unapproved_previews_never_install(client,change):
    p=preview(client)
    body={'digest':p['digest'],'approved_permissions':p['plan']['permissions']}
    if change=='rejected':client.post(f"/api/plugin-installations/previews/{p['id']}/reject")
    elif change=='superseded':preview(client,'1.1.0')
    elif change=='wrong-digest':body['digest']='0'*64
    elif change=='permissions':body['approved_permissions']=[]
    else:
        with SessionLocal.begin() as db:
            row=db.get(PluginPreview,p['id'])
            if change=='expired':row.expires_at=now()-1
            if change=='other-session':row.login_session_id='another-session'
            if change=='tampered-plan':row.plan={**row.plan,'location':'changed'}
            if change=='catalog-changed':db.get(PluginRelease,p['plan']['entries'][0]['release']['release_id']).status='revoked'
    result=client.post(f"/api/plugin-installations/previews/{p['id']}/confirm",json=body)
    assert result.status_code in (409,422)
    assert len(client.get('/api/plugin-installations').json())==1


def test_install_change_invalidates_preview(client):
    p=preview(client)
    native=client.get('/api/plugin-installations').json()[0]
    assert action(client,native,'disable').status_code==200
    assert confirm(client,p).status_code==409


def test_disable_enable_and_restart_do_not_resurrect_disabled_install(client):
    native=client.get('/api/plugin-installations').json()[0]
    disabled=action(client,native,'disable').json()
    with SessionLocal.begin() as db:seed_bundled(db)
    assert client.get('/api/skills').json()[0]['available'] is False
    assert action(client,disabled,'enable').json()['available'] is True
    assert action(client,native,'disable').status_code==409


def test_uninstall_retains_invocation_history_and_does_not_reseed(configured):
    row=install(configured);inv=invocation(configured,row)
    assert action(configured,row,'uninstall').status_code==200
    with SessionLocal.begin() as db:seed_bundled(db)
    assert len(configured.get('/api/plugin-installations').json())==1
    assert configured.get('/api/plugin-invocations').json()[0]['id']==inv['id']
    assert configured.get('/api/plugin-invocations').json()[0]['status']=='invalidated'
    again=install(configured)
    assert again['id']==row['id'] and again['version']>row['version']


def test_updates_and_rollback_keep_immutable_version_history(client):
    one=install(client)
    two=install(client,'1.1.0','update')
    assert two['id']==one['id'] and two['version']>one['version']
    old=install(client,'1.0.0','rollback')
    assert old['plugin_version']=='1.0.0'
    history=client.get(f"/api/plugin-installations/{one['id']}/history").json()
    assert [r['plugin_version']for r in history]==['1.0.0','1.1.0','1.0.0']


def test_cannot_rollback_to_never_installed_version(client):
    install(client,'1.1.0')
    r=client.post('/api/plugin-installations/preview',json={'plugin_id':'lingxi.writing','plugin_version':'1.0.0','action':'rollback'})
    assert r.status_code==409


def test_dependency_install_is_previewed_and_cannot_be_uninstalled_alone(client):
    a=release('test.root',deps=[('test.leaf','>=1.0.0,<2.0.0')]);b=release('test.leaf')
    add(a,b)
    p=preview(client,plugin='test.root')
    assert {e['release']['plugin_id']for e in p['plan']['entries']}=={'test.root','test.leaf'}
    rows=confirm(client,p).json()
    root=next(r for r in rows if r['plugin_id']=='test.root');leaf=next(r for r in rows if r['plugin_id']=='test.leaf')
    assert root['available'] and root['dependency_lock']=={'test.leaf':b.id}
    assert action(client,leaf,'uninstall').status_code==409
    assert action(client,leaf,'disable').status_code==200
    assert next(r for r in client.get('/api/plugin-installations').json() if r['id']==root['id'])['available'] is False


def test_shared_dependency_solver_backtracks_and_detects_cycles():
    root=release('test.root',deps=[('test.left','>=1.0.0'),('test.right','>=1.0.0')])
    left=release('test.left',deps=[('test.shared','>=1.0.0,<3.0.0')])
    right=release('test.right',deps=[('test.shared','<2.0.0')])
    selected=resolve([root,left,right,release('test.shared'),release('test.shared','2.0.0')],'test.root','1.0.0',{})
    assert selected['test.shared'].version=='1.0.0'
    with pytest.raises(HTTPException):resolve([release('test.root',deps=[('test.leaf','>=1.0.0')]),release('test.leaf',deps=[('test.root','>=1.0.0')])],'test.root','1.0.0',{})


def test_dependency_conflict_and_missing_versions_are_rejected():
    root=release('test.root',deps=[('test.leaf','>=2.0.0')])
    with pytest.raises(HTTPException):resolve([root,release('test.leaf')],'test.root','1.0.0',{})
    with pytest.raises(HTTPException):resolve([root],'test.root','1.0.0',{})


def test_transitive_locks_and_rollback_restore_exact_dependency_versions(client):
    add(release('test.root',deps=[('test.leaf','>=1.0.0')]),
        release('test.root','2.0.0',deps=[('test.leaf','>=2.0.0')]),
        release('test.leaf'),release('test.leaf','2.0.0'))
    # Install leaf v1 first to verify preference and stable lock.
    install(client,plugin='test.leaf')
    root=install(client,plugin='test.root')
    install(client,'2.0.0','update','test.root')
    restored=install(client,'1.0.0','rollback','test.root')
    assert restored['dependency_lock']==root['dependency_lock']
    leaf=next(r for r in client.get('/api/plugin-installations').json()if r['plugin_id']=='test.leaf')
    assert leaf['plugin_version']=='1.0.0'


def test_cannot_break_an_installed_dependent(client):
    add(release('test.root',deps=[('test.leaf','<2.0.0')]),release('test.leaf'),release('test.leaf','2.0.0'))
    install(client,plugin='test.root')
    result=client.post('/api/plugin-installations/preview',json={'plugin_id':'test.leaf','plugin_version':'2.0.0','action':'update'})
    assert result.status_code==409 and '依赖者' in result.text


@pytest.mark.parametrize('state',['revoked','delisted'])
def test_revocation_and_delisting_have_different_runtime_semantics(configured,state):
    row=install(configured)
    with SessionLocal.begin() as db:db.get(PluginRelease,row['release_id']).status=state
    assert not any(r['release_id']==row['release_id']for r in configured.get('/api/plugin-catalog').json())
    r=configured.post('/api/plugin-invocations',json={'installation_id':row['id'],'capability_id':'writing.outline','content':'资料','request_id':'catalog-state-test'})
    assert r.status_code==(409 if state=='revoked'else 200)


def test_manifest_tamper_and_unsupported_adapter_block_install(client):
    row=release('test.api');spec=copy.deepcopy(row.manifest);spec['capabilities'][0]['kind']='script';spec['capabilities'][0]['instruction']=''
    row.manifest=spec;row.digest=digest(row.manifest);add(row)
    assert client.post('/api/plugin-installations/preview',json={'plugin_id':'test.api','plugin_version':'1.0.0'}).status_code==409
    with SessionLocal.begin() as db:
        release_=db.scalar(select(PluginRelease).where(PluginRelease.plugin_id=='lingxi.writing',PluginRelease.version=='1.0.0'))
        release_.manifest={**release_.manifest,'description':'tampered'}
    assert client.get('/api/plugin-catalog').status_code==409


@pytest.mark.parametrize('value',['^1.0.0','*','==1.*','>=1.0.0;python_version<"4"'])
def test_manifest_range_is_explicit_and_not_executable(value):
    data=release('test.root').manifest
    with pytest.raises(ValueError):Manifest(**(data|{'core':value}))


def test_plugin_cannot_declare_native_entry_or_unknown_permissions():
    data=release('test.root').manifest
    with pytest.raises(ValueError):Manifest(**(data|{'capabilities':[{'id':'server.files','name':'escape','kind':'native','permissions':['resources.list']}]}))
    with pytest.raises(ValueError):Manifest(**(data|{'capabilities':[{'id':'skill.test','name':'escape','kind':'skill','permissions':['memory.all'],'instruction':'ignore'}]}))


def test_no_login_csrf_and_cross_workspace_access(client):
    with TestClient(app)as anon:
        assert anon.get('/api/plugin-catalog').status_code==401
    p=preview(client)
    token=client.headers.pop('X-CSRF-Token')
    assert confirm(client,p).status_code==403
    client.headers['X-CSRF-Token']=token
    row=install(client)
    with SessionLocal.begin()as db:db.get(PluginInstallation,row['id']).workspace_id='another'
    assert action(client,row,'disable').status_code==404
    assert client.get(f"/api/plugin-installations/{row['id']}/history").status_code==404


def test_invocation_is_durable_deduplicated_and_runs_real_worker(configured,monkeypatch):
    row=install(configured);one=invocation(configured,row);two=invocation(configured,row)
    assert one==two
    observed=[]
    def model(m,p,messages,**kw):observed.append((messages,kw));return '## 建议提纲\n时间待确认。'
    monkeypatch.setattr('lingxi.gateway.complete',model)
    job=claim();assert job.kind=='plugin';perform(job)
    result=configured.get('/api/plugin-invocations').json()[0]
    assert result['status']=='succeeded' and result['receipt']['external_writes'] is False
    assert len(observed)==1 and 'secret-fixture-key'not in str(observed)
    assert len(observed[0][0])==2 and observed[0][1]['config']['plugin_version']=='1.0.0'
    with SessionLocal()as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='plugin'))==1


@pytest.mark.parametrize('change',['disable','cancel','revoke','permissions','provider'])
def test_authority_change_during_model_call_discards_result(configured,monkeypatch,change):
    row=install(configured);inv=invocation(configured,row)
    def model(*a,**kw):
        if change=='disable':assert action(configured,row,'disable').status_code==200
        elif change=='cancel':
            current=configured.get('/api/plugin-invocations').json()[0]
            assert configured.post(f"/api/plugin-invocations/{inv['id']}/cancel",json={'version':current['version']}).status_code==200
        else:
            with SessionLocal.begin()as db:
                if change=='revoke':db.get(PluginRelease,row['release_id']).status='revoked'
                elif change=='provider':
                    from lingxi.models import Provider
                    db.get(Provider,'provider').version+=1
                else:db.get(PluginInstallation,row['id']).grants=[]
        return 'MUST NOT COMMIT'
    monkeypatch.setattr('lingxi.gateway.complete',model)
    perform(claim())
    result=configured.get('/api/plugin-invocations').json()[0]
    assert result['status']in ('invalidated','cancelled','failed') and result['result']==''


def test_update_during_execution_preserves_pinned_old_version(configured,monkeypatch):
    row=install(configured);invocation(configured,row)
    def model(*args,**kwargs):
        install(configured,'1.1.0','update')
        assert kwargs['config']['plugin_version']=='1.0.0'
        return '固定旧版本结果'
    monkeypatch.setattr('lingxi.gateway.complete',model);perform(claim())
    result=configured.get('/api/plugin-invocations').json()[0]
    assert result['status']=='succeeded' and result['plugin_version']=='1.0.0'


def test_disable_blocks_native_tasks_and_reminder_delivery(configured,monkeypatch):
    native=configured.get('/api/plugin-installations').json()[0]
    r=configured.post('/api/reminders',json={'request_id':'native-reminder-001','title':'提醒','due_at':now()+20,'timezone':'Asia/Shanghai'})
    assert r.status_code==200
    assert action(configured,native,'disable').status_code==200
    r=configured.post('/api/tasks',json={'request_id':'blocked-native-task','title':'计划','skill_id':'project.plan','input':{'goal':'资料'}})
    assert r.status_code==409
    from lingxi.tasks import Reminder
    with SessionLocal.begin()as db:
        db.scalar(select(Job)).available_at=now()-1
        db.scalar(select(Reminder)).due_at=now()-1
    perform(claim())
    assert configured.get('/api/reminders').json()['items'][0]['status']=='scheduled'
    with SessionLocal()as db:
        from lingxi.tasks import ReminderReceipt
        assert db.scalar(select(func.count()).select_from(ReminderReceipt))==0


def test_invocation_failure_is_not_automatically_recharged(configured,monkeypatch):
    row=install(configured);invocation(configured,row)
    def fail(*a,**kw):raise RuntimeError('upstream-sensitive-details')
    monkeypatch.setattr('lingxi.gateway.complete',fail);perform(claim())
    result=configured.get('/api/plugin-invocations').json()[0]
    assert result['status']=='failed'and 'upstream-sensitive-details'not in result['error']
    with SessionLocal()as db:assert db.scalar(select(Job).where(Job.kind=='plugin')).status=='failed'
