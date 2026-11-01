import base64
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Audit, LoginSession, User
from lingxi.auth_totp import TwoFactor, totp
from lingxi.security import decrypt

PASSWORD = 'test-password-123'


def setup(client, key='synthetic-enrollment'):
    version = client.get('/api/auth/security').json()['version']
    response = client.post('/api/auth/totp/setup', json={'version':version,'password':PASSWORD,'request_id':key})
    assert response.status_code == 200, response.text
    return response.json()


def enroll(client, monkeypatch=None):
    pending = setup(client)
    response = client.post('/api/auth/totp/confirm', json={'version':pending['version'], 'code':totp(pending['secret'], int(now()//30))})
    assert response.status_code == 200, response.text
    return pending, response.json()


def login(client, code=''):
    return client.post('/api/auth/login',json={'username':'owner','password':PASSWORD,'code':code})


@pytest.mark.parametrize('timestamp,code',[(59,'287082'),(1111111109,'081804'),(1111111111,'050471'),
    (1234567890,'005924'),(2000000000,'279037'),(20000000000,'353130')])
def test_rfc6238_sha1_vectors(timestamp, code):
    secret = base64.b32encode(b'12345678901234567890').decode()
    assert totp(secret, timestamp//30) == code


@pytest.mark.parametrize('path,body',[
    ('/api/auth/security',None),
    ('/api/auth/totp/setup',{'version':0,'password':'x','request_id':'synthetic'}),
    ('/api/auth/totp/confirm',{'version':1,'code':'123456'}),
    ('/api/auth/totp/cancel',{'version':1}),
    ('/api/auth/totp/disable',{'version':1,'password':'x','code':'123456'}),
    ('/api/auth/totp/recovery',{'version':1,'password':'x','code':'123456'})])
def test_totp_requires_authenticated_context(path,body):
    with TestClient(app) as anonymous:
        response=anonymous.get(path) if body is None else anonymous.post(path,json=body)
        assert response.status_code==401


def test_status_never_exposes_pending_or_active_secrets(client):
    assert client.get('/api/auth/security').json()=={'version':0,'enabled':False,'recovery_remaining':0,'pending':False,'pending_expires_at':None}
    pending=setup(client)
    assert pending['secret'] in pending['otpauth_uri'] and 'algorithm=SHA1' in pending['otpauth_uri']
    with SessionLocal()as db:
        config=db.scalar(select(TwoFactor))
        assert config.pending_secret != pending['secret'] and decrypt(config.pending_secret)==pending['secret']
    response=client.get('/api/auth/security')
    assert response.json()['pending'] is True and pending['secret'] not in response.text and 'otpauth' not in response.text


def test_setup_retry_is_idempotent_but_changed_version_is_not(client):
    body={'version':0,'password':PASSWORD,'request_id':'same-enrollment-request'}
    first=client.post('/api/auth/totp/setup',json=body).json()
    second=client.post('/api/auth/totp/setup',json=body)
    assert second.status_code==200 and second.json()==first
    assert client.post('/api/auth/totp/setup',json=dict(body,request_id='different-request')).status_code==409
    with SessionLocal()as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='totp_setup'))==1


def test_setup_password_csrf_and_origin_cannot_be_bypassed(client):
    body={'version':0,'password':'incorrect','request_id':'synthetic-password-failure'}
    assert client.post('/api/auth/totp/setup',json=body).status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/auth/totp/setup',json=dict(body,password=PASSWORD)).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.post('/api/auth/totp/setup',json=dict(body,password=PASSWORD),headers={'origin':'https://other.example'}).status_code==403
    with SessionLocal()as db:assert db.scalar(select(func.count()).select_from(TwoFactor))==0


def test_confirmation_expires_and_is_bound_to_original_session(client):
    pending=setup(client)
    fresh=login(client).json();client.headers['X-CSRF-Token']=fresh['csrf']
    assert client.post('/api/auth/totp/confirm',json={'version':pending['version'],'code':totp(pending['secret'],int(now()//30))}).status_code==410
    assert client.get('/api/auth/security').json()['pending'] is False


def test_confirm_rejects_expired_and_invalid_code(client):
    pending=setup(client)
    valid=totp(pending['secret'],int(now()//30))
    invalid='000000' if valid!='000000' else '111111'
    assert client.post('/api/auth/totp/confirm',json={'version':pending['version'],'code':invalid}).status_code==401
    with SessionLocal.begin()as db:db.scalar(select(TwoFactor)).pending_expires=now()-1
    assert client.post('/api/auth/totp/confirm',json={'version':pending['version'],'code':valid}).status_code==410
    assert client.get('/api/auth/security').json()['enabled'] is False


def test_enable_encrypts_secret_hashes_codes_and_revokes_other_sessions(client):
    with TestClient(app)as other:
        assert login(other).status_code==200
        pending,enabled=enroll(client)
        assert other.get('/api/auth/me').status_code==401
    assert client.get('/api/auth/me').status_code==200
    assert len(set(enabled['recovery_codes']))==10 and enabled['status']['recovery_remaining']==10
    with SessionLocal()as db:
        config=db.scalar(select(TwoFactor))
        assert config.pending_secret=='' and decrypt(config.encrypted_secret)==pending['secret']
        assert all(code not in json.dumps(config.recovery_hashes) for code in enabled['recovery_codes'])
    assert pending['secret'] not in client.get('/api/auth/security').text


def test_password_only_login_cannot_issue_session_when_enabled(client):
    enroll(client)
    with TestClient(app)as anonymous:
        result=login(anonymous)
        assert result.status_code==401 and '双重验证' in result.text
        assert 'lingxi_session' not in result.cookies
        assert anonymous.get('/api/auth/me').status_code==401


def test_totp_login_accepts_small_skew_once_and_rejects_replay(client,monkeypatch):
    pending,_=enroll(client)
    current=int(now()//30)
    monkeypatch.setattr('lingxi.auth_totp.now',lambda:(current+1)*30+5)
    # Enrollment already consumed the current counter.
    with TestClient(app)as anonymous:
        assert login(anonymous,totp(pending['secret'],current)).status_code==401
        assert login(anonymous,totp(pending['secret'],current+1)).status_code==200
        assert login(anonymous,totp(pending['secret'],current+1)).status_code==401


def test_recovery_is_single_use_and_never_returned_from_read_api(client):
    _,enabled=enroll(client)
    code=enabled['recovery_codes'][0]
    with TestClient(app)as other:
        assert login(other,code.upper()).status_code==200
        assert login(other,code).status_code==401
    assert client.get('/api/auth/security').json()['recovery_remaining']==9
    assert code not in client.get('/api/admin/audit').text
    with SessionLocal()as db:assert db.scalar(select(Audit).where(Audit.action=='totp_recovery_used'))


def test_rotation_requires_both_proofs_invalidates_old_codes_and_other_sessions(client):
    _,enabled=enroll(client)
    codes=enabled['recovery_codes']
    with TestClient(app)as other:
        assert login(other,codes[0]).status_code==200
        body={'version':enabled['status']['version'],'password':'wrong','code':codes[1]}
        assert client.post('/api/auth/totp/recovery',json=body).status_code==401
        result=client.post('/api/auth/totp/recovery',json=dict(body,password=PASSWORD))
        assert result.status_code==200 and result.json()['status']['recovery_remaining']==10
        assert other.get('/api/auth/me').status_code==401
        assert login(other,codes[2]).status_code==401
        assert login(other,result.json()['recovery_codes'][0]).status_code==200


def test_disable_clears_secrets_codes_pending_and_revokes_others(client):
    _,enabled=enroll(client)
    with TestClient(app)as other:
        login(other,enabled['recovery_codes'][0])
        body={'version':enabled['status']['version'],'password':PASSWORD,'code':enabled['recovery_codes'][1]}
        result=client.post('/api/auth/totp/disable',json=body)
        assert result.status_code==200 and result.json()['enabled'] is False
        assert other.get('/api/auth/me').status_code==401
        assert login(other).status_code==200
    with SessionLocal()as db:
        config=db.scalar(select(TwoFactor))
        assert config.encrypted_secret=='' and config.recovery_hashes==[] and config.pending_secret==''


def test_cancel_preserves_login_and_requires_version(client):
    pending=setup(client)
    assert client.post('/api/auth/totp/cancel',json={'version':pending['version']+1}).status_code==409
    result=client.post('/api/auth/totp/cancel',json={'version':pending['version']})
    assert result.status_code==200 and result.json()['pending'] is False
    assert client.get('/api/auth/me').status_code==200
    with SessionLocal()as db:assert db.scalar(select(TwoFactor)).pending_secret==''


def test_enrollment_never_replaces_enabled_factor(client):
    pending,enabled=enroll(client)
    assert client.post('/api/auth/totp/setup',json={'version':enabled['status']['version'],'password':PASSWORD,'request_id':'replacement-request'}).status_code==409
    with SessionLocal()as db:assert decrypt(db.scalar(select(TwoFactor)).encrypted_secret)==pending['secret']


def test_management_throttle_is_durable_on_failed_passwords(client):
    for _ in range(5):
        assert client.post('/api/auth/totp/setup',json={'version':0,'password':'bad','request_id':'bad-password-request'}).status_code==401
    assert client.post('/api/auth/totp/setup',json={'version':0,'password':PASSWORD,'request_id':'valid-after-errors'}).status_code==429
    with SessionLocal()as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='security_attempt'))==5


def test_security_scope_never_reads_or_mutates_other_workspace(client):
    with SessionLocal.begin()as db:
        db.add(TwoFactor(user_id='owner',workspace_id='other',enabled=True,encrypted_secret='unreadable'))
    assert client.get('/api/auth/security').json()['enabled'] is False
    pending,_=enroll(client)
    with SessionLocal()as db:
        rows=list(db.scalars(select(TwoFactor)))
        assert len(rows)==2 and next(r for r in rows if r.workspace_id=='other').encrypted_secret=='unreadable'


def test_operator_reset_requires_password_confirmation_and_revokes_all(client,monkeypatch):
    enroll(client)
    from lingxi.cli import main
    monkeypatch.setattr('sys.argv',['lingxi.cli','reset-totp'])
    answers=iter(['owner','NO'])
    monkeypatch.setattr('builtins.input',lambda *_:next(answers))
    monkeypatch.setattr('getpass.getpass',lambda *_:PASSWORD)
    with pytest.raises(SystemExit):main()
    assert client.get('/api/auth/security').json()['enabled'] is True
    answers=iter(['owner','RESET owner'])
    main()
    assert client.get('/api/auth/me').status_code==401
    assert login(client).status_code==200
    with SessionLocal()as db:assert db.scalar(select(Audit).where(Audit.action=='totp_operator_reset'))


@pytest.mark.parametrize('factor',['totp','recovery'])
def test_concurrent_factor_replay_allows_exactly_one_login(client,monkeypatch,factor):
    from concurrent.futures import ThreadPoolExecutor
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('Real PostgreSQL row-lock concurrency validation')
    pending,enabled=enroll(client)
    current=int(now()//30)+1
    monkeypatch.setattr('lingxi.auth_totp.now',lambda:current*30+5)
    code=totp(pending['secret'],current) if factor=='totp' else enabled['recovery_codes'][0]
    def attempt(_):
        with TestClient(app)as other:return login(other,code).status_code
    with ThreadPoolExecutor(max_workers=2)as pool:results=list(pool.map(attempt,range(2)))
    assert sorted(results)==[200,401]


def test_account_factor_throttle_survives_client_address_change(client):
    enroll(client)
    for i in range(6):
        with TestClient(app,client=(f'127.0.0.{i+2}',50000))as other:
            result=login(other,'invalid-factor')
            assert result.status_code==(401 if i<5 else 429)


def test_authenticator_and_recovery_never_enter_diagnostics(client):
    pending,enabled=enroll(client)
    result=client.post('/api/admin/diagnostics/preview',json={'request_id':'totp-diagnostics-check','sections':['health','usage','logs','audit']})
    assert result.status_code==200
    assert pending['secret'] not in result.text and all(code not in result.text for code in enabled['recovery_codes'])


def test_expired_pending_cleanup_never_clears_active_factor(client):
    from lingxi.auth_totp import prune_pending
    pending,enabled=enroll(client)
    with SessionLocal.begin()as db:
        db.add(TwoFactor(user_id='expired-user',pending_secret='expired-ciphertext',pending_expires=now()-1))
        prune_pending(db)
    with SessionLocal()as db:
        assert decrypt(db.scalar(select(TwoFactor).where(TwoFactor.user_id=='owner')).encrypted_secret)==pending['secret']
        assert db.scalar(select(TwoFactor).where(TwoFactor.user_id=='expired-user')).pending_secret==''
