from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func,delete
from lingxi.api import app
from lingxi.db import SessionLocal,now,engine
from lingxi.models import Memory,User,Message,Conversation
from lingxi.browser_notifications import BrowserNotice,BrowserNoticePreference
from lingxi import proactive as pro
from test_proactive import ready,prefs

ROOT='/api/browser-notifications'


def consent(client,enabled=True):
    p=client.get(ROOT+'/state').json()['preference']
    r=client.put(ROOT+'/preferences',json={'version':p['version'],'enabled':enabled,'acknowledgement':'browser_running_only'})
    assert r.status_code==200,r.text
    return r.json()


def claim(client):
    p=client.get(ROOT+'/state').json()['preference']
    r=client.post(ROOT+'/claims',json={'consent_version':p['version'],'background':True})
    assert r.status_code==200,r.text
    return r.json()['id']


def opened(client):
    consent(client)
    return ready(client)


def test_authentication_and_csrf(client):
    with TestClient(app) as anon:assert anon.get(ROOT+'/state').status_code==401
    client.headers.pop('X-CSRF-Token')
    assert client.put(ROOT+'/preferences',json={'version':0,'enabled':True,'acknowledgement':'browser_running_only'}).status_code==403


def test_default_off_explicit_consent_and_no_backfill(client):
    assert client.get(ROOT+'/state').json()['preference']['enabled'] is False
    ready(client)
    assert client.put(ROOT+'/preferences',json={'version':0,'enabled':True}).status_code==422
    consent(client)
    assert claim(client) is None
    assert client.get(ROOT+'/state').json()['receipts']==[]


def test_consent_version_change_and_noop_second_browser_enrollment(client):
    first=consent(client)
    assert consent(client)==first
    closed=consent(client,False)
    assert closed['version']==first['version']+1
    assert client.put(ROOT+'/preferences',json={'version':first['version'],'enabled':True,'acknowledgement':'browser_running_only'}).status_code==409
    assert consent(client)['version']==closed['version']+1


def test_one_contact_one_attempt_and_no_personal_text(client):
    opened(client)
    identity=claim(client)
    assert identity and claim(client) is None
    r=client.get(ROOT+'/state')
    assert r.json()['receipts'][0]['status']=='attempting'
    assert '中文项目演示' not in r.text and 'content' not in r.text
    assert client.post(ROOT+'/'+identity+'/ack',json={'status':'requested'}).json()['status']=='requested'
    assert client.post(ROOT+'/'+identity+'/ack',json={'status':'clicked'}).json()['status']=='clicked'
    assert client.post(ROOT+'/'+identity+'/ack',json={'status':'requested'}).json()['status']=='clicked'
    assert claim(client) is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(pro.ProactiveContact))==1
        assert db.scalar(select(func.count()).select_from(BrowserNotice))==1


def test_failed_or_lost_ack_never_retries_and_not_read_feedback(client):
    contact=opened(client);identity=claim(client)
    assert client.post(ROOT+'/'+identity+'/ack',json={'status':'failed'}).json()['status']=='failed'
    assert claim(client) is None
    with SessionLocal() as db:assert db.get(pro.ProactiveContact,contact['id']).feedback is None


@pytest.mark.parametrize('change',['disabled','results_only','pause','quiet','blocked','kind','limit','pref_version',
    'expired','source_deleted','source_corrected','feedback','closed','consent','maintenance'])
def test_changes_prevent_claim_and_invalidate_active_presentation(client,change,monkeypatch):
    contact=opened(client)
    with SessionLocal.begin() as db:
        notice=db.scalar(select(BrowserNotice));topic=db.get(pro.ConversationTopic,contact['topic_id'])
        pref=db.get(pro.ContactPreference,'owner')
        if change=='disabled':pref.enabled=False
        elif change=='results_only':pref.mode='results_only'
        elif change=='pause':pref.paused_until=now()+600
        elif change=='quiet':pref.quiet_start='00:00';pref.quiet_end='23:59'
        elif change=='blocked':pref.blocked_sources=[pro.source_key(topic)]
        elif change=='kind':pref.allowed_kinds=[]
        elif change=='limit':pref.daily_limit=0
        elif change=='pref_version':pref.version+=1
        elif change=='expired':notice.expires_at=now()-1
        elif change=='source_deleted':db.get(Memory,topic.source_id).status='deleted'
        elif change=='source_corrected':db.get(Memory,topic.source_id).version+=1
        elif change=='feedback':db.get(pro.ProactiveContact,contact['id']).feedback='helpful'
        elif change=='closed':topic.status='closed'
        elif change=='consent':db.get(BrowserNoticePreference,'owner').enabled=False
        elif change=='maintenance':monkeypatch.setattr('lingxi.backups.paused',lambda db:True)
    if change=='maintenance':
        assert client.post(ROOT+'/claims',json={'consent_version':1,'background':True}).status_code==503
    else:assert claim(client) is None
    assert client.get(ROOT+'/state').json()['receipts'][0]['eligible'] is False


def test_new_user_message_prevents_stale_notice(client):
    opened(client)
    with SessionLocal.begin() as db:
        conversation=Conversation(user_id='owner');db.add(conversation);db.flush()
        db.add(Message(conversation_id=conversation.id,role='user',content='新的发言'))
    assert claim(client) is None


def test_no_voice_contact_double_notification(configured):
    from test_proactive_voice import setup
    consent(configured)
    setup(configured)
    assert configured.get(ROOT+'/state').json()['receipts']==[]


def test_original_login_binds_ack_and_other_id_is_hidden(client):
    opened(client);identity=claim(client)
    with TestClient(app) as second:
        r=second.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
        second.headers['X-CSRF-Token']=r.json()['csrf']
        assert second.post(ROOT+'/'+identity+'/ack',json={'status':'requested'}).status_code==403
        assert claim(second) is None
    with SessionLocal.begin() as db:
        db.add(BrowserNotice(id='foreign',user_id='different',workspace_id='elsewhere',contact_id='foreign',
            preference_version=1,consent_version=1,expires_at=now()+120))
    assert 'foreign' not in client.get(ROOT+'/state').text
    assert client.post(ROOT+'/foreign/ack',json={'status':'requested'}).status_code==404


def test_restore_preserves_attempt_barrier_and_requires_fresh_consent(client):
    from lingxi.backups import checkpoint,quarantine
    opened(client);identity=claim(client)
    assert client.post(ROOT+'/'+identity+'/ack',json={'status':'requested'}).status_code==200
    from sqlalchemy import text
    with engine.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)'))
        db.execute(text('DELETE FROM alembic_version'))
        db.execute(text("INSERT INTO alembic_version VALUES ('0055')"))
    safety=checkpoint()
    with SessionLocal.begin() as db:db.execute(delete(BrowserNotice));db.execute(delete(BrowserNoticePreference))
    quarantine(safety)
    with SessionLocal() as db:
        assert not db.get(BrowserNoticePreference,'owner').enabled
        assert db.get(BrowserNotice,identity).status=='requested'
        assert db.get(BrowserNotice,identity).login_id==''


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='Row-lock concurrency requires PostgreSQL')
def test_parallel_browsers_claim_exactly_one_durable_attempt(client):
    opened(client)
    def request(_):
        with TestClient(app) as browser:
            r=browser.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
            browser.headers['X-CSRF-Token']=r.json()['csrf']
            return claim(browser)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(request,range(2)))
    assert len([r for r in results if r])==1
