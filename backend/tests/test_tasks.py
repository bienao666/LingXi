"""Task contracts, fencing, recovery, cancellation and reminder delivery facts."""
import json
import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Job, Message, User, Provider
from lingxi.tasks import WorkTask, TaskAttempt, TaskEvent, Reminder, ReminderReceipt, list_resource
from lingxi import worker, gateway
from lingxi.task_worker import execute_task

PLAN = {'summary':'准备一次演示','steps':[{'title':'整理内容','action':'梳理需要展示的功能','completion':'列出清晰的展示提纲'}],
        'assumptions':['演示时间需要用户确认']}


def task(client, request_id='task-request-0001', **values):
    response = client.post('/api/tasks', json={'request_id':request_id,'title':'演示准备',
        'skill_id':'project.plan','input':{'goal':'准备一次演示'}} | values)
    assert response.status_code == 200, response.text
    return response.json()


def reminder(client, request_id='reminder-request-0001', **values):
    response = client.post('/api/reminders', json={'request_id':request_id,'title':'准备演示',
        'content':'带上展示提纲','due_at':now()+600,'timezone':'Asia/Shanghai'} | values)
    assert response.status_code == 200, response.text
    return response.json()


def ready_reminders():
    with SessionLocal.begin() as db:
        for row in db.scalars(select(Reminder)):
            row.due_at = now()-1
        for job in db.scalars(select(Job).where(Job.kind == 'reminder')):
            job.available_at = 0


def drain():
    for _ in range(30):
        job = worker.claim()
        if not job:
            return
        worker.perform(job)
    raise AssertionError('worker did not reach idle')


@pytest.mark.parametrize('route',['/api/tasks','/api/reminders','/api/skills'])
def test_management_requires_login_and_csrf(client,route):
    with TestClient(app) as anonymous:
        assert anonymous.get(route).status_code == 401
    if route != '/api/skills':
        client.headers.pop('X-CSRF-Token')
        body = {'request_id':'auth-request-0001','title':'x','due_at':now()+60,
                'skill_id':'project.plan','input':{'goal':'x'}}
        assert client.post(route,json=body).status_code == 403


def test_registry_reports_real_prerequisites_and_bounded_read_only_capabilities(client):
    entries = {item['id']:item for item in client.get('/api/skills').json()}
    assert set(entries) == {'project.plan','server.files','server.read','reminder.once'}
    assert not entries['project.plan']['available']
    assert entries['reminder.once']['available']
    assert entries['project.plan']['plugin_id'] == 'lingxi.builtin'
    assert client.post('/api/tasks',json={'request_id':'needs-model-0001','title':'x',
        'skill_id':'project.plan','input':{'goal':'x'}}).status_code == 409


def test_task_creation_is_transactional_idempotent_and_snapshots_model(configured):
    row = task(configured)
    duplicate = task(configured)
    assert duplicate['id'] == row['id']
    assert row['config']['model_id'] == 'chat'
    assert row['config']['provider_version'] == 1
    assert row['status'] == 'queued'
    assert configured.post('/api/tasks',json={'request_id':'task-request-0001','title':'changed',
        'skill_id':'project.plan','input':{'goal':'x'}}).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkTask)) == 1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'task')) == 1
        assert db.scalar(select(func.count()).select_from(TaskEvent)) == 1


def test_tasks_and_reminders_filter_and_paginate_without_cross_owner_access(configured):
    first = task(configured,'task-00000001',title='早期计划')
    second = task(configured,'task-00000002',title='新计划')
    page = configured.get('/api/tasks?limit=1').json()
    assert page['items'][0]['id'] == second['id'] and page['next_cursor'] == second['id']
    assert configured.get('/api/tasks?limit=1&cursor='+page['next_cursor']).json()['items'][0]['id'] == first['id']
    assert configured.get('/api/tasks?q=早期').json()['items'][0]['id'] == first['id']
    assert configured.get('/api/tasks?status=failed').json()['items'] == []
    reminder(configured,'reminder-0000001')
    with SessionLocal.begin() as db:
        db.get(WorkTask,first['id']).workspace_id = 'other'
        db.add(WorkTask(id='foreign-owner',workspace_id='personal',user_id='foreign',request_id='foreign',
                       request_hash='x',title='private',skill_id='project.plan',input={}))
        db.add(Reminder(id='foreign-reminder',workspace_id='other',user_id='owner',request_id='other',
                        request_hash='x',title='private',content='',due_at=now()+60,timezone='UTC'))
    for identity in (first['id'],'foreign-owner'):
        assert configured.get('/api/tasks/'+identity).status_code == 404
        for action in ('cancel','retry'):
            assert configured.post('/api/tasks/'+identity+'/'+action,json={'version':1}).status_code == 404
    assert configured.get('/api/tasks?cursor=foreign-owner').status_code == 404
    assert len(configured.get('/api/tasks').json()['items']) == 1
    assert len(configured.get('/api/reminders').json()['items']) == 1
    assert configured.post('/api/reminders/foreign-reminder/cancel',json={'version':1}).status_code == 404


def test_task_result_attempt_events_and_original_conversation_are_atomic(configured,monkeypatch):
    conversation = configured.post('/api/conversations',json={'title':'演示'}).json()['id']
    row = task(configured,conversation_id=conversation)
    calls=[]
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:(calls.append(kw) or json.dumps(PLAN)))
    job = worker.claim()
    worker.perform(job)
    execute_task(job)  # crash after result commit but before queue acknowledgement
    detail = configured.get('/api/tasks/'+row['id']).json()
    assert detail['status'] == 'succeeded'
    assert detail['result']['verification'] == 'structure_only'
    assert detail['attempts'][0]['receipt']['external_writes'] is False
    assert [e['kind'] for e in detail['events']] == ['queued','running','succeeded']
    assert len(calls) == 1
    assert 'lease_token' not in json.dumps(detail)
    with SessionLocal() as db:
        messages=db.scalars(select(Message).where(Message.conversation_id == conversation)).all()
        assert len(messages) == 1 and '步骤尚未执行' in messages[0].content


def test_deleted_conversation_is_not_recreated_by_background_completion(configured,monkeypatch):
    from lingxi.models import Conversation
    conversation = configured.post('/api/conversations',json={'title':'演示'}).json()['id']
    row = task(configured,conversation_id=conversation)
    with SessionLocal.begin() as db:
        db.get(Conversation,conversation).deleted=True
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:json.dumps(PLAN))
    drain()
    assert configured.get('/api/tasks/'+row['id']).json()['result_message_id'] is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0


def test_queued_cancel_prevents_execution_and_version_conflicts(configured,monkeypatch):
    row = task(configured)
    assert configured.post('/api/tasks/'+row['id']+'/cancel',json={'version':99}).status_code == 409
    response=configured.post('/api/tasks/'+row['id']+'/cancel',json={'version':row['version']})
    assert response.json()['status'] == 'cancelled'
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:pytest.fail('cancelled task executed'))
    drain()
    detail=configured.get('/api/tasks/'+row['id']).json()
    assert detail['attempts'] == [] and detail['status'] == 'cancelled'


def test_running_cancel_discards_result_and_keeps_attempt_fact(configured,monkeypatch):
    row = task(configured)
    def generate(*args,**kw):
        current=configured.get('/api/tasks/'+row['id']).json()
        assert current['status']=='running'
        assert configured.post('/api/tasks/'+row['id']+'/cancel',json={'version':current['version']}).status_code == 200
        return json.dumps(PLAN)
    monkeypatch.setattr(gateway,'complete',generate)
    drain()
    detail=configured.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='cancelled' and detail['result']=={}
    assert detail['attempts'][0]['status']=='cancelled'


def test_malformed_output_has_bounded_retries_then_explicit_retry(configured,monkeypatch):
    row=task(configured)
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:'not a plan')
    for _ in range(3):
        with SessionLocal.begin() as db:
            for job in db.scalars(select(Job)):
                job.available_at=0
        drain()
    detail=configured.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='failed' and len(detail['attempts'])==3 and detail['result']=={}
    assert configured.post('/api/tasks/'+row['id']+'/retry',json={'version':1}).status_code==409
    assert configured.post('/api/tasks/'+row['id']+'/retry',json={'version':detail['version']}).json()['status']=='queued'
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:json.dumps(PLAN))
    drain()
    assert configured.get('/api/tasks/'+row['id']).json()['status']=='succeeded'


def test_lease_loss_cannot_commit_and_recovery_marks_interruption(configured,monkeypatch):
    row=task(configured)
    calls=[]
    def generate(*a,**kw):
        calls.append(1)
        if len(calls)==1:
            with SessionLocal.begin() as db:
                job=db.scalar(select(Job).where(Job.kind=='task'))
                job.lease_token,job.lease_until='another-worker',now()-1
        return json.dumps(PLAN)
    monkeypatch.setattr(gateway,'complete',generate)
    drain()
    detail=configured.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='succeeded' and len(calls)==2
    assert [a['status'] for a in detail['attempts']]==['interrupted','succeeded']


def test_changed_provider_is_not_silently_substituted(configured,monkeypatch):
    row=task(configured)
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').version+=1
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:pytest.fail('stale model used'))
    drain()
    detail=configured.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='queued' and detail['attempts'][0]['status']=='failed'


def test_registry_and_submission_reject_other_workspace_model(configured):
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').workspace_id='foreign'
    catalog={entry['id']:entry for entry in configured.get('/api/skills').json()}
    assert not catalog['project.plan']['available']
    assert configured.post('/api/tasks',json={'request_id':'foreign-model-0001','title':'private',
        'skill_id':'project.plan','input':{'goal':'x'}}).status_code==409


@pytest.mark.parametrize('path',['/etc','../private','sub/../../etc','C:\\Users','a\\b','\x00'])
def test_resource_paths_cannot_escape(configured,path):
    response=configured.post('/api/tasks',json={'request_id':'unsafe-path-0001','title':'目录',
        'skill_id':'server.files','input':{'path':path}})
    assert response.status_code==422


@pytest.mark.skipif(os.name!='posix',reason='Linux openat/NOFOLLOW acceptance runs in Docker')
def test_resource_openat_does_not_follow_symlinks_and_reports_limits(tmp_path,monkeypatch):
    from lingxi.config import settings
    root=tmp_path/'resources';root.mkdir()
    (root/'readme.txt').write_text('data')
    (root/'outside').symlink_to(tmp_path,target_is_directory=True)
    monkeypatch.setattr(settings(),'resource_dir',root)
    result=list_resource('.',lambda:None,grant={'roots':['.']})
    assert [x['name'] for x in result['entries']]==['readme.txt']
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        list_resource('outside',lambda:None,grant={'roots':['.']})
    for i in range(210):
        (root/f'file{i}').write_text('x')
    limited=list_resource('.',lambda:None,grant={'roots':['.']})
    assert limited['truncated'] and len(limited['entries'])==200


def test_reminder_uses_durable_schedule_and_idempotent_request(client):
    row=reminder(client)
    duplicate=client.post('/api/reminders',json={'request_id':'reminder-request-0001','title':row['title'],
        'content':row['content'],'due_at':row['due_at'],'timezone':row['timezone']}).json()
    assert duplicate['id']==row['id']
    assert worker.claim() is None
    ready_reminders();drain()
    fired=client.get('/api/reminders').json()['items'][0]
    assert fired['status']=='fired' and fired['receipt']['status']=='stored'
    assert fired['receipt']['acknowledged_at'] is None


@pytest.mark.skipif(os.name!='posix',reason='Linux secure resource task runs in Docker')
def test_resource_skill_runs_through_durable_worker(client,tmp_path,monkeypatch):
    from lingxi.config import settings
    root=tmp_path/'resources';root.mkdir();(root/'readme.md').write_text('metadata only')
    monkeypatch.setattr(settings(),'resource_dir',root)
    from test_resources import grant
    grant(client, ['.'])
    row=task(client,skill_id='server.files',input={'path':'.'})
    drain()
    detail=client.get('/api/tasks/'+row['id']).json()
    assert detail['status']=='succeeded'
    assert detail['result']['entries'][0]['name']=='readme.md'
    assert detail['result']['verification']=='filesystem_metadata'
    assert detail['attempts'][0]['receipt']['external_writes'] is False


def test_edited_reminder_fires_only_current_revision(client):
    row=reminder(client)
    update={'title':'新提醒','content':'仅发送新内容','due_at':now()+900,'timezone':'UTC','version':1}
    assert client.put('/api/reminders/'+row['id'],json=update).status_code==200
    ready_reminders();drain()
    current=client.get('/api/reminders').json()['items'][0]
    assert current['status']=='fired' and current['version']==2
    with SessionLocal() as db:
        receipts=db.scalars(select(ReminderReceipt)).all()
        assert len(receipts)==1 and receipts[0].reminder_version==2


def test_reminder_edit_invalidates_old_job_cancel_invalidates_new_job(client):
    row=reminder(client)
    update={'title':'更改后的提醒','content':'新内容','due_at':now()+1200,'timezone':'UTC',
            'version':row['version']}
    changed=client.put('/api/reminders/'+row['id'],json=update).json()
    assert changed['version']==2
    assert client.put('/api/reminders/'+row['id'],json=update).status_code==409
    assert client.post('/api/reminders/'+row['id']+'/cancel',json={'version':2}).json()['status']=='cancelled'
    ready_reminders();drain()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderReceipt))==0


def test_reminder_receipt_and_conversation_write_once_acknowledgement_explicit(client):
    conversation=client.post('/api/conversations',json={'title':'提醒'}).json()['id']
    row=reminder(client,conversation_id=conversation)
    ready_reminders()
    job=worker.claim();worker.perform(job)
    from lingxi.task_worker import fire_reminder
    fire_reminder(job)
    current=client.get('/api/reminders').json()['items'][0]
    assert current['receipt']['message_id']
    assert client.post('/api/reminders/'+row['id']+'/cancel',json={'version':1}).status_code==409
    response=client.post('/api/reminders/'+row['id']+'/acknowledge',json={'version':1})
    assert response.json()['receipt']['status']=='acknowledged'
    assert client.post('/api/reminders/'+row['id']+'/acknowledge',json={'version':1}).status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReminderReceipt))==1
        assert db.scalar(select(func.count()).select_from(Message))==1


@pytest.mark.parametrize('values',[{'timezone':'invalid/zone'},{'due_at':1},{'due_at':99999999999},
                                  {'conversation_id':'foreign'}])
def test_reminders_reject_invalid_time_zone_and_foreign_conversation(client,values):
    response=client.post('/api/reminders',json={'request_id':'invalid-reminder-001','title':'test',
        'due_at':now()+60,'timezone':'Asia/Shanghai'}|values)
    assert response.status_code in (404,422)
