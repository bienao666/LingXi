"""Named reminders are separate from unsolicited contacts and never replay unknown sends."""
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, delete
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Job, ModelCall, User
from lingxi import reminder_delivery as rd, task_worker, worker
from lingxi.tasks import Reminder, ReminderReceipt
from lingxi.channels import Channel, IdentityLink
from lingxi.channel_gateway import prepare_outbound, receive_event, close_connection
from test_channels import create, connect, bind
from test_tasks import reminder, drain


def setup(c):
    channel=create(c); connection=connect(channel['id'])
    bind(c,channel,connection,memory=True)
    target=c.get('/api/reminder-targets').json()[0]['target']
    row=reminder(c,due_at=now()+20)
    return channel,connection,target,row


def path(row): return '/api/reminders/'+row['id']+'/delivery'


def preview(c,row,target,**extra):
    version=c.get(path(row)).json()['version']
    r=c.post(path(row)+'/previews',json={'reminder_version':row['version'],'delivery_version':version,'target':target,'wait_hours':1}|extra)
    assert r.status_code==200,r.text
    return r.json()


def confirm(c,row,proposal):
    r=c.post(path(row)+'/confirm',json={'token':proposal['token'],'acknowledgement':'send_this_reminder_to_selected_qq' if proposal['after']['target']['kind']=='qq' else 'store_this_reminder_only'})
    assert r.status_code==200,r.text
    return r.json()


def queued(c,monkeypatch):
    channel,connection,target,row=setup(c)
    before=c.get('/api/contact-preferences').json()
    confirm(c,row,preview(c,row,target))
    fire(c,row,monkeypatch)
    assert c.get(path(row)).json()['status']=='pending'
    assert c.get('/api/contact-preferences').json()==before
    return channel,connection,row


def fire(c,row,monkeypatch):
    at=row['due_at']+1
    monkeypatch.setattr(rd,'now',lambda:at)
    monkeypatch.setattr(task_worker,'now',lambda:at)
    with SessionLocal.begin() as db:
        job=db.scalar(select(Job).where(Job.kind=='reminder'))
        job.available_at=0
    drain()


def current(c,row): return c.get(path(row)).json()


def receipt(channel,connection,action,**changes):
    receive_event(channel['id'],connection,{'echo':action['echo'],'retcode':0,'data':{'message_id':12345}}|changes)


def test_default_station_only_and_preview_has_no_send(client):
    channel,connection,target,row=setup(client)
    assert current(client,row)['status']=='station_only'
    p=preview(client,row,target)
    assert p['title']==row['title'] and p['content']==row['content']
    assert p['after']['target_label'].endswith('QQ 87654321')
    assert current(client,row)['version']==0 and prepare_outbound(channel['id'],connection) is None
    with SessionLocal() as db: assert db.scalar(select(func.count()).select_from(rd.ReminderDelivery))==0


def test_confirmation_is_explicit_and_idempotent(client):
    _,_,target,row=setup(client); p=preview(client,row,target)
    assert client.post(path(row)+'/confirm',json={'token':p['token'],'acknowledgement':'store_this_reminder_only'}).status_code==422
    first=confirm(client,row,p)
    assert first['status']=='configured' and confirm(client,row,p)==first
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(rd.ReminderDelivery))==1
        assert db.scalar(select(func.count()).select_from(ModelCall))==0


def test_switch_to_station_only_requires_confirmation_and_does_not_reenable_itself(client,monkeypatch):
    channel,connection,target,row=setup(client)
    confirm(client,row,preview(client,row,target))
    p=preview(client,row,{'kind':'web'})
    assert current(client,row)['status']=='configured'
    assert confirm(client,row,p)['status']=='cancelled'
    fire(client,row,monkeypatch)
    assert prepare_outbound(channel['id'],connection) is None
    assert client.get('/api/reminders').json()['items'][0]['status']=='fired'


def test_postgres_concurrent_confirmation_and_dispatch_are_single_attempt(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from lingxi.db import engine
    if engine.dialect.name!='postgresql': pytest.skip('PostgreSQL row locking')
    channel,connection,target,row=setup(client);p=preview(client,row,target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:confirm(client,row,p),range(2)))
    assert results[0]==results[1]
    fire(client,row,monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        actions=list(pool.map(lambda _:prepare_outbound(channel['id'],connection),range(2)))
    assert len([a for a in actions if isinstance(a,dict)])==1


def test_postgres_edit_before_worker_owner_lock_does_not_fire_stale_source(client,monkeypatch):
    from lingxi import plugins
    from lingxi.db import engine
    if engine.dialect.name!='postgresql': pytest.skip('PostgreSQL concurrent writer')
    channel,connection,target,row=setup(client)
    confirm(client,row,preview(client,row,target))
    with SessionLocal.begin() as db:
        db.scalar(select(Job).where(Job.kind=='reminder')).available_at=0
    job=worker.claim()
    assert job and job.kind=='reminder'
    original=plugins.owner_lock
    changed=False
    def edit_before_lock(db,user):
        nonlocal changed
        if not changed:
            changed=True
            result=client.put('/api/reminders/'+row['id'],json={k:row[k] for k in
                ['title','content','due_at','timezone','conversation_id','version']}|{'title':'新的提醒','due_at':now()+600})
            assert result.status_code==200,result.text
        return original(db,user)
    monkeypatch.setattr(plugins,'owner_lock',edit_before_lock)
    monkeypatch.setattr(task_worker,'now',lambda:row['due_at']+1)
    task_worker.fire_reminder(job)
    with SessionLocal() as db:
        latest=db.get(Reminder,row['id'])
        assert latest.status=='scheduled' and latest.version==2
        assert db.scalar(select(func.count()).select_from(ReminderReceipt))==0
    assert current(client,row)['status']=='cancelled'
    assert prepare_outbound(channel['id'],connection) is None


def test_real_binding_revoke_hook_stops_configuration_immediately(client):
    _,_,target,row=setup(client);confirm(client,row,preview(client,row,target))
    r=client.post('/api/identity-links/'+target['binding_id']+'/revoke',json={'version':target['binding_version']})
    assert r.status_code==200,r.text
    assert current(client,row)['status']=='cancelled'


@pytest.mark.parametrize('change',['version','content','binding','session','expired'])
def test_preview_staleness_cannot_expand_authorization(client,monkeypatch,change):
    channel,_,target,row=setup(client); p=preview(client,row,target)
    if change=='version':
        with SessionLocal.begin() as db: db.get(Reminder,row['id']).version+=1
    elif change=='content':
        with SessionLocal.begin() as db: db.get(Reminder,row['id']).content='changed'
    elif change=='binding':
        with SessionLocal.begin() as db: db.get(IdentityLink,target['binding_id']).version+=1
    elif change=='session':
        login=client.post('/api/auth/login',json={'username':'owner','password':'test-password-123'}).json()
        client.headers['X-CSRF-Token']=login['csrf']
    else: monkeypatch.setattr(rd,'now',lambda:p['expires_at']+1)
    r=client.post(path(row)+'/confirm',json={'token':p['token'],'acknowledgement':'send_this_reminder_to_selected_qq'})
    assert r.status_code==409,r.text
    assert current(client,row)['version']==0


def test_management_auth_csrf_workspace_and_strict_input(client):
    _,_,target,row=setup(client)
    with TestClient(app) as anon:
        assert anon.get('/api/reminder-targets').status_code==401
        assert anon.get(path(row)).status_code==401
        assert anon.post(path(row)+'/previews',json={}).status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post(path(row)+'/previews',json={}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.post(path(row)+'/previews',json={'reminder_version':True,'delivery_version':0,'target':target}).status_code==422
    with SessionLocal.begin() as db: db.get(Reminder,row['id']).workspace_id='foreign'
    assert client.get(path(row)).status_code==404


def test_unbound_and_group_receivers_cannot_be_invented(client):
    row=reminder(client)
    assert client.get('/api/reminder-targets').json()==[]
    assert client.post(path(row)+'/previews',json={'reminder_version':1,'delivery_version':0,
        'target':{'kind':'qq','binding_id':'group:123','binding_version':1,'channel_version':1}}).status_code==404


def test_due_qq_reminder_has_one_station_receipt_and_one_protocol_attempt(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch)
    a=prepare_outbound(channel['id'],connection)
    assert a['action']=='send_private_msg' and a['params']['user_id']==87654321
    assert row['title'] in a['params']['message'][0]['data']['text']
    assert prepare_outbound(channel['id'],connection) is None
    receipt(channel,connection,a)
    assert current(client,row)['status']=='accepted_by_protocol'
    assert current(client,row)['protocol_message_id']=='12345'
    assert client.get('/api/reminders').json()['items'][0]['status']=='fired'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderReceipt))==1
        assert db.scalar(select(func.count()).select_from(ModelCall))==0


def test_named_reminder_ignores_unsolicited_contact_limits_and_quiet_hours(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch)
    prefs=client.get('/api/contact-preferences').json()
    assert client.put('/api/contact-preferences',json=prefs|{'enabled':False,'daily_limit':0}).status_code==200
    assert prepare_outbound(channel['id'],connection)['action']=='send_private_msg'


@pytest.mark.parametrize('change',['channel','binding','plugin','source','cancel','edit'])
def test_revocation_and_changes_cancel_delivery_without_revival(client,monkeypatch,change):
    channel,connection,target,row=setup(client);confirm(client,row,preview(client,row,target))
    if change=='cancel':
        assert client.post('/api/reminders/'+row['id']+'/cancel',json={'version':1}).status_code==200
    elif change=='edit':
        assert client.put('/api/reminders/'+row['id'],json={k:row[k] for k in ['title','content','due_at','timezone','conversation_id','version']}|{'title':'new title'}).status_code==200
    else:
        with SessionLocal.begin() as db:
            if change=='channel': db.get(Channel,channel['id']).version+=1
            elif change=='binding': db.get(IdentityLink,target['binding_id']).status='revoked'
            elif change=='plugin':
                from lingxi.plugins import PluginInstallation, invalidate_invocations
                install=db.scalar(select(PluginInstallation).where(PluginInstallation.plugin_id=='lingxi.builtin'))
                install.status='disabled';invalidate_invocations(db,db.get(User,'owner'),install.id,'disabled')
            else: db.get(Reminder,row['id']).content='changed outside route'
    rd.reconcile()
    assert current(client,row)['status']=='cancelled'
    assert prepare_outbound(channel['id'],connection) is None


def test_offline_waits_then_expires_without_changing_station_result(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch)
    with SessionLocal.begin() as db: db.get(Channel,channel['id']).login_state='login_required'
    assert prepare_outbound(channel['id'],connection) is None and current(client,row)['status']=='pending'
    expiry=current(client,row)['expires_at'];monkeypatch.setattr(rd,'now',lambda:expiry+1)
    rd.reconcile()
    assert current(client,row)['status']=='expired'
    assert client.get('/api/reminders').json()['items'][0]['status']=='fired'


@pytest.mark.parametrize('mode',['disconnect','timeout','cancel','malformed','failure'])
def test_dispatched_unknown_is_not_replayed_and_late_receipt_is_truthful(client,monkeypatch,mode):
    channel,connection,row=queued(client,monkeypatch);a=prepare_outbound(channel['id'],connection)
    if mode=='disconnect': close_connection(channel['id'],connection)
    elif mode=='timeout':
        at=row['due_at']+40;monkeypatch.setattr(rd,'now',lambda:at);rd.reconcile()
    elif mode=='cancel':
        assert client.post(path(row)+'/action',json={'version':current(client,row)['version'],'acknowledgement':'cancel_without_resend'}).status_code==200
    elif mode=='malformed': receipt(channel,connection,a,data={})
    else: receipt(channel,connection,a,retcode=100)
    assert current(client,row)['status']==('failed' if mode=='failure' else 'sending' if mode=='malformed' else 'unknown')
    assert prepare_outbound(channel['id'],connection) in (None,False)
    if mode!='disconnect':
        receipt(channel,connection,a)
        expected='failed' if mode=='failure' else 'accepted_by_protocol'
        assert current(client,row)['status']==expected


def test_review_does_not_resend_or_fabricate_success(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch);prepare_outbound(channel['id'],connection)
    r=client.post(path(row)+'/action',json={'version':current(client,row)['version'],'acknowledgement':'cancel_without_resend'})
    assert r.status_code==200
    r=client.post(path(row)+'/action',json={'version':r.json()['version'],'acknowledgement':'checked_no_resend'})
    assert r.status_code==200 and r.json()['status']=='unknown' and r.json()['reviewed_at']
    assert prepare_outbound(channel['id'],connection) is None


def test_old_backup_restore_keeps_send_barrier_and_cancels_old_queue(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch);prepare_outbound(channel['id'],connection)
    with SessionLocal() as db: data=rd.checkpoint(db)
    rd.validate_checkpoint(data,{'id':'owner','workspace_id':'personal'})
    with SessionLocal.begin() as db:
        db.execute(delete(rd.ReminderDelivery));rd.restore(db,data)
    assert current(client,row)['status']=='unknown'
    assert prepare_outbound(channel['id'],connection) is None


def test_full_backup_quarantine_carries_latest_reminder_barrier(client,monkeypatch):
    channel,connection,row=queued(client,monkeypatch);prepare_outbound(channel['id'],connection)
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    with SessionLocal.begin() as db: schema(db)
    data=checkpoint()
    assert len(data['reminder_deliveries'])==1
    with SessionLocal.begin() as db: db.execute(delete(rd.ReminderDelivery))
    quarantine(data)
    with SessionLocal() as db:
        entry=db.scalar(select(rd.ReminderDelivery))
        assert entry.status=='unknown' and entry.echo==data['reminder_deliveries'][0]['echo']


@pytest.mark.parametrize('change',['owner','content','time','hash'])
def test_malformed_restore_metadata_rejected(client,change):
    _,_,target,row=setup(client);confirm(client,row,preview(client,row,target))
    with SessionLocal() as db: data=deepcopy(rd.checkpoint(db))
    if change=='owner': data[0]['workspace_id']='other'
    elif change=='content': data[0]['body']='untrusted'
    elif change=='time': data[0]['expires_at']=float('nan')
    else: data[0]['source_hash']='x'*64
    with pytest.raises(ValueError): rd.validate_checkpoint(data,{'id':'owner','workspace_id':'personal'})
