import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from lingxi.api import app
from lingxi.db import SessionLocal,now
from lingxi.models import Audit,ChatRun,ModelCall,RunEvent,Job,Memory
from lingxi.operations import (DiagnosticBundle,OpsPreference,RuntimeEvent,WorkerState,event,heartbeat,maintenance,tokens)


@pytest.mark.parametrize('path',['/api/admin/audit','/api/admin/logs','/api/admin/traces','/api/admin/usage',
    '/api/admin/operations/health','/api/admin/operations/preferences','/api/admin/diagnostics'])
def test_operations_require_login(path):
    with TestClient(app) as client:assert client.get(path).status_code==401


def test_empty_worker_is_unavailable_not_invented_from_queue(client):
    data=client.get('/api/admin/operations/health').json()
    assert data['worker']['status']=='unavailable' and data['worker']['online']==0
    heartbeat('synthetic-worker',now(),0)
    data=client.get('/api/admin/operations/health').json()
    assert data['worker']['status']=='ready' and data['worker']['busy']==0
    with SessionLocal.begin() as db:db.get(WorkerState,'synthetic-worker').seen_at=now()-46
    assert client.get('/api/admin/operations/health').json()['worker']['status']=='unavailable'
    assert client.get('/api/admin/health').json()['worker']['status']=='unavailable'


def test_disk_failure_has_no_host_path_or_exception(client,monkeypatch):
    monkeypatch.setattr('lingxi.operations.shutil.disk_usage',lambda *a:(_ for _ in ()).throw(OSError('SECRET-host-path')))
    response=client.get('/api/admin/operations/health')
    assert response.status_code==200 and 'SECRET' not in response.text
    assert response.json()['disk']['status']=='unavailable'


def test_preferences_version_bounds_and_audit(client):
    initial=client.get('/api/admin/operations/preferences').json()
    assert initial=={'version':0,'log_retention_days':90,'daily_token_alert':0}
    body=dict(initial,log_retention_days=7,daily_token_alert=100)
    saved=client.put('/api/admin/operations/preferences',json=body)
    assert saved.status_code==200 and saved.json()['version']==1
    assert client.put('/api/admin/operations/preferences',json=body).status_code==409
    assert client.get('/api/admin/operations/preferences').json()['daily_token_alert']==100
    assert client.put('/api/admin/operations/preferences',json=dict(body,version=1,log_retention_days=0)).status_code==422
    assert client.put('/api/admin/operations/preferences',json=dict(body,version=1,secret='no')).status_code==422
    with SessionLocal() as db:assert db.scalar(select(Audit).where(Audit.action=='operations_preferences')).target_id


def test_audit_pagination_ties_scope_and_bad_cursor(client):
    at=now()-10
    with SessionLocal.begin() as db:
        for i in range(60):db.add(Audit(id=f'audit-{i:03}',user_id='owner',workspace_id='personal',created_at=at,action='test_action',target_id=f'object-{i}'))
        db.add(Audit(user_id='different',action='not-my-user'))
        db.add(Audit(user_id='owner',workspace_id='other',action='not-my-space'))
    first=client.get('/api/admin/audit?action=test_action').json()
    second=client.get('/api/admin/audit?action=test_action&cursor='+first['next_cursor']).json()
    assert len(first['items'])==50 and len(second['items'])==10
    assert len({x['id'] for x in first['items']+second['items']})==60
    assert client.get('/api/admin/audit?cursor=bad').status_code==422
    assert 'not-my-' not in client.get('/api/admin/audit').text


def test_structured_logs_never_serialize_jobs_errors_or_input(client):
    with SessionLocal.begin() as db:
        event(db,'job_attempt_failed',reference='safe-job')
        event(db,'store_sync_failed','store')
        event(db,'worker_interrupted',workspace='other')
        db.add(Job(key='secret-job',kind='chat',payload={'content':'SECRET-input'},error='SECRET-token-and-upstream-body',status='failed'))
    response=client.get('/api/admin/logs?level=error')
    assert len(response.json()['items'])==1
    assert response.json()['items'][0]['message']=='后台作业失败'
    assert 'SECRET' not in response.text
    assert client.get('/api/admin/logs?level=warning').json()['items'][0]['component']=='store'
    with SessionLocal() as db:
        with pytest.raises(ValueError):event(db,'arbitrary-content')


@pytest.mark.parametrize('value,expected',[({'total_tokens':7},7),({'prompt_tokens':3,'completion_tokens':4},7),
    ({'input_tokens':5,'output_tokens':0},5),({},None),({'total_tokens':True},None),({'total_tokens':-5},None),({'input_tokens':5},None),
    (None,None),([],None),('invalid',None)])
def test_usage_accepts_only_complete_nonnegative_integer_accounting(value,expected):assert tokens(value)==expected


def test_usage_unknown_not_zero_and_utc_alert(client):
    with SessionLocal.begin() as db:
        for data in [dict(usage={'total_tokens':120},status='completed'),dict(usage={},status='failed')]:
            db.add(ModelCall(model_id='model-ref',purpose='chat',duration_ms=200,**data))
        db.add(ModelCall(workspace_id='other',model_id='other-model',purpose='chat',status='completed',duration_ms=1,usage={'total_tokens':900000}))
        db.add(OpsPreference(user_id='owner',daily_token_alert=100))
    data=client.get('/api/admin/usage?days=1').json()
    assert data['calls']==2 and data['known_tokens']==120 and data['unknown_usage_calls']==1 and data['cost'] is None
    assert data['daily']['alert'] is True and data['daily']['enforcement']=='alert_only'
    assert data['daily']['timezone']=='UTC' and data['groups'][0]['failed']==1
    assert client.get('/api/admin/usage?days=31').status_code==422


def run(identity,**values):
    return ChatRun(id=identity,user_id='owner',conversation_id='synthetic-conversation',request_id='request-'+identity,
        request_hash='hash',model_id='model-ref',**values)


def test_traces_stable_pagination_and_owner_scope(client):
    at=now()-5
    with SessionLocal.begin() as db:
        for i in range(60):db.add(run(f'trace-{i:03}',created_at=at))
        db.add(run('foreign-space',workspace_id='other'))
    first=client.get('/api/admin/traces?kind=chat').json()
    assert len(first['items'])==50
    second=client.get('/api/admin/traces?kind=chat&cursor='+first['next_cursor']).json()
    assert len(second['items'])==10 and second['next_cursor'] is None
    assert len({r['id']for r in first['items']+second['items']})==60
    assert client.get('/api/admin/traces/chat/foreign-space').status_code==404
    assert client.get('/api/admin/traces/chat/missing').status_code==404


def test_trace_detail_only_whitelisted_metadata(client):
    with SessionLocal.begin() as db:
        db.add(run('trace',config={'provider_version':3,'prompt_version':'v1','SECRET':'SECRET-persona-content'}))
        db.add(RunEvent(run_id='trace',seq=1,kind='delta',data={'text':'SECRET-stream-content'}))
        db.add(RunEvent(run_id='trace',seq=2,kind='done',data={'error':'SECRET-upstream'}))
        db.add(ModelCall(model_id='model-ref',run_id='trace',purpose='chat',status='failed',duration_ms=25,
            error='SECRET-credentials',usage={},config={'provider_version':3,'headers':'SECRET-key','persona':'SECRET-persona'}))
    response=client.get('/api/admin/traces/chat/trace')
    assert response.status_code==200 and 'SECRET' not in response.text
    data=response.json()
    assert data['events']==[{'seq':2,'created_at':None,'kind':'done'}]
    assert data['calls'][0]['known_tokens'] is None and data['versions']['provider_version']==3


def preview(client,key='synthetic-diagnostic',sections=None):
    response=client.post('/api/admin/diagnostics/preview',json={'request_id':key,'days':7,'sections':sections or ['health','usage','logs','audit']})
    assert response.status_code==200,response.text
    return response.json()


def test_diagnostic_preview_confirmation_idempotency_and_download(client):
    first=preview(client)
    second=preview(client)
    assert first['id']==second['id'] and first['digest']==second['digest'] and not first['download_url']
    url=f"/api/admin/diagnostics/{first['id']}"
    assert client.get(url+'/download').status_code==409
    assert client.post(url+'/confirm',json={'digest':'0'*64}).status_code==409
    confirmed=client.post(url+'/confirm',json={'digest':first['digest']})
    assert confirmed.status_code==200
    assert client.post(url+'/confirm',json={'digest':first['digest']}).status_code==200
    response=client.get(confirmed.json()['download_url'])
    assert response.status_code==200 and hashlib.sha256(response.content).hexdigest()==first['digest']
    assert 'attachment' in response.headers['content-disposition']
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='diagnostic_confirm'))==1


def test_diagnostic_allowlist_omits_all_content_credentials_and_endpoints(client):
    with SessionLocal.begin() as db:
        db.add(ModelCall(model_id='SECRET-model-name',purpose='chat',status='failed',duration_ms=1,
            error='SECRET-upstream',config={'url':'https://SECRET-host','key':'SECRET-key','content':'SECRET-chat'}))
        db.add(Audit(user_id='owner',action='memory_delete',target_id='SECRET-memory-ref'))
        event(db,'job_attempt_failed',reference='SECRET-job-ref')
    data=preview(client)
    assert 'SECRET' not in json.dumps(data)
    assert data['preview']['redaction'].startswith('metadata_only')
    assert len(data['preview']['usage']['groups'][0]['model_id'])==16


def test_diagnostics_bound_to_session_and_identity(client):
    data=preview(client)
    login=client.post('/api/auth/login',json={'username':'owner','password':'test-password-123'}).json()
    client.headers['X-CSRF-Token']=login['csrf']
    assert client.post(f"/api/admin/diagnostics/{data['id']}/confirm",json={'digest':data['digest']}).status_code==403
    assert client.get('/api/admin/diagnostics/missing/download').status_code==404


def test_expiration_and_tamper_block_confirmation(client):
    data=preview(client)
    with SessionLocal.begin() as db:db.get(DiagnosticBundle,data['id']).payload='changed'
    assert client.post(f"/api/admin/diagnostics/{data['id']}/confirm",json={'digest':data['digest']}).status_code==409
    with SessionLocal.begin() as db:db.get(DiagnosticBundle,data['id']).expires_at=now()-1
    assert client.get(f"/api/admin/diagnostics/{data['id']}/download").status_code==410


def test_maintenance_only_prunes_expired_operational_data(client):
    data=preview(client)
    with SessionLocal.begin() as db:
        db.get(DiagnosticBundle,data['id']).expires_at=now()-1
        db.add(OpsPreference(user_id='owner',log_retention_days=7))
        db.add(RuntimeEvent(id='old-log',workspace_id='personal',created_at=now()-8*86400,component='worker',code='job_attempt_failed',level='error'))
        db.add(RuntimeEvent(id='other-log',workspace_id='other',created_at=now()-8*86400,component='worker',code='job_attempt_failed',level='error'))
        db.add(Audit(id='old-audit',user_id='owner',created_at=now()-100*86400,action='memory_delete'))
        db.add(Job(id='old-job',key='historical-job',kind='cleanup',payload={'deletion':'record'},created_at=now()-100*86400,status='completed'))
    maintenance()
    with SessionLocal() as db:
        assert db.get(RuntimeEvent,'old-log') is None and db.get(RuntimeEvent,'other-log')
        assert db.get(Audit,'old-audit') and db.get(Job,'old-job')
        assert db.get(DiagnosticBundle,data['id']).payload==''


def test_diagnostic_rejects_invalid_scope_and_request_reuse(client):
    assert client.post('/api/admin/diagnostics/preview',json={'request_id':'synthetic','sections':['secrets']}).status_code==422
    data=preview(client)
    assert client.post('/api/admin/diagnostics/preview',json={'request_id':'synthetic-diagnostic','sections':['health'],'days':1}).status_code==409
    assert client.get('/api/admin/diagnostics').json()[0]['id']==data['id']
