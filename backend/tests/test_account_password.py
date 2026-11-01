import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from lingxi.api import app
from lingxi.auth_totp import TwoFactor, new_recovery, totp
from lingxi.db import SessionLocal, engine, now
from lingxi.models import Audit, Job, LoginSession, User
from lingxi.security import encrypt, hasher

OLD = 'test-password-123'
NEW = 'new-password-for-rotation-2026'
PATH = '/api/auth/password'
SECRET = 'JBSWY3DPEHPK3PXP'


def body(**values):
    return dict(version=0,password=OLD,new_password=NEW,confirmation=NEW,
        acknowledgement='revoke_all_logins',**values)


def factor():
    with SessionLocal.begin() as db:
        row=TwoFactor(user_id='owner',version=2,enabled=True,encrypted_secret=encrypt(SECRET))
        codes=new_recovery(db.get(User,'owner'),row);db.add(row)
    return codes


def changed_count():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Audit).where(Audit.action=='password_changed'))


def login(client,password=OLD,code=''):
    return client.post('/api/auth/login',json={'username':'owner','password':password,'code':code})


def test_rotation_revokes_all_sessions_preserves_jobs_and_requires_new_login(client):
    with TestClient(app) as other:
        assert login(other).status_code==200
        with SessionLocal.begin() as db:db.add(Job(key='keep-background',kind='extract',payload={},status='pending'))
        response=client.put(PATH,json=body())
        assert response.status_code==200,response.text
        assert response.json()=={'status':'reauthentication_required'}
        assert 'Max-Age=0' in response.headers['set-cookie']
        assert client.get('/api/auth/me').status_code==401
        assert other.get('/api/auth/me').status_code==401
        assert login(other).status_code==401
        assert login(other,NEW).status_code==200
    with SessionLocal() as db:
        user=db.get(User,'owner');assert hasher.verify(user.password_hash,NEW)
        assert user.password_hash not in response.text and OLD not in response.text and NEW not in response.text
        assert db.scalar(select(Job).where(Job.key=='keep-background')).status=='pending'
    assert changed_count()==1


def test_authentication_csrf_origin_and_identity_are_required(client):
    with TestClient(app) as stranger:assert stranger.put(PATH,json=body()).status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.put(PATH,json=body()).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.put(PATH,json=body(),headers={'Origin':'https://foreign.example'}).status_code==403
    assert client.put(PATH,json=body()|{'user_id':'another'}).status_code==422
    assert changed_count()==0


@pytest.mark.parametrize('values',[
    {'new_password':'short','confirmation':'short'},
    {'new_password':'x'*301,'confirmation':'x'*301},
    {'new_password':' '*12,'confirmation':' '*12},
    {'confirmation':'different-confirmation'},
    {'new_password':OLD,'confirmation':OLD},
    {'acknowledgement':'not-approved'},
    {'version':True},
    {'password':None},
])
def test_invalid_password_input_never_changes_credentials_or_echoes_values(client,values):
    result=client.put(PATH,json=body()|values)
    assert result.status_code==422,result.text
    assert OLD not in result.text and NEW not in result.text and 'different-confirmation' not in result.text
    assert changed_count()==0 and client.get('/api/auth/me').status_code==200


def test_wrong_password_and_stale_factor_version_preserve_credentials(client):
    assert client.put(PATH,json=body()|{'password':'incorrect-secret'}).status_code==401
    assert client.put(PATH,json=body()|{'version':99}).status_code==409
    assert changed_count()==0 and client.get('/api/auth/me').status_code==200
    with SessionLocal() as db:assert hasher.verify(db.get(User,'owner').password_hash,OLD)


def test_disabled_pending_enrollment_is_cleared_without_enabling_factor(client):
    with SessionLocal.begin() as db:
        db.add(TwoFactor(user_id='owner',version=1,pending_secret=encrypt(SECRET),
            pending_session='old-session',pending_request='pending-enrollment',pending_expires=now()+300))
    result=client.put(PATH,json=body()|{'version':1});assert result.status_code==200,result.text
    with SessionLocal() as db:
        row=db.scalar(select(TwoFactor));assert row.version==2 and not row.enabled and not row.pending_secret
        assert row.pending_session=='' and row.pending_request=='' and row.pending_expires==0


@pytest.mark.parametrize('proof',['totp','recovery'])
def test_enabled_factor_is_required_preserved_and_single_use(client,proof):
    codes=factor()
    request=body()|{'version':2}
    assert client.put(PATH,json=request).status_code==401
    request['code']=totp(SECRET,int(now()//30)) if proof=='totp' else codes[0]
    response=client.put(PATH,json=request);assert response.status_code==200,response.text
    with SessionLocal() as db:
        row=db.scalar(select(TwoFactor));assert row.enabled and row.version==3
        assert len(row.recovery_hashes)==(10 if proof=='totp' else 9)
    with TestClient(app) as other:
        assert login(other,NEW).status_code==401
        assert login(other,NEW,request['code']).status_code==401
        assert login(other,NEW,codes[1]).status_code==200
    assert changed_count()==1


def test_password_proof_failure_does_not_consume_recovery_code(client):
    codes=factor()
    assert client.put(PATH,json=body()|{'version':2,'password':'wrong','code':codes[0]}).status_code==401
    with SessionLocal() as db:assert len(db.scalar(select(TwoFactor)).recovery_hashes)==10


def test_new_password_preserves_exact_unicode_and_outer_spaces(client):
    value='  灵犀独立密码示例·2026  '
    response=client.put(PATH,json=body()|{'new_password':value,'confirmation':value})
    assert response.status_code==200,response.text
    with TestClient(app) as other:
        assert login(other,value.strip()).status_code==401
        assert login(other,value).status_code==200


def test_unrelated_workspace_factor_and_other_identity_are_unchanged(client):
    with SessionLocal.begin() as db:
        db.add(TwoFactor(user_id='owner',workspace_id='foreign',version=9,enabled=True,encrypted_secret='foreign-secret'))
        db.add(User(id='foreign-owner',workspace_id='foreign',username='foreign-user',password_hash=hasher.hash('foreign-password')))
    assert client.put(PATH,json=body()).status_code==200
    with SessionLocal() as db:
        row=db.scalar(select(TwoFactor));assert row.version==9 and row.encrypted_secret=='foreign-secret'
        assert hasher.verify(db.get(User,'foreign-owner').password_hash,'foreign-password')


def test_management_throttle_is_shared_and_survives_failure(client):
    for _ in range(5):assert client.put(PATH,json=body()|{'password':'wrong'}).status_code==401
    assert client.put(PATH,json=body()).status_code==429
    assert client.post('/api/auth/totp/setup',json={'version':0,'password':OLD,'request_id':'new-enrollment'}).status_code==429
    assert changed_count()==0


def test_repeated_request_from_revoked_session_cannot_apply_twice(client):
    assert client.put(PATH,json=body()).status_code==200
    assert client.put(PATH,json=body()).status_code==401
    assert changed_count()==1


def test_login_rechecks_password_after_factor_reservation_releases_lock(client,monkeypatch):
    import lingxi.api as module
    original=module.verify_login_factor
    def changed_during_factor(db,user,code):
        db.commit()
        with SessionLocal.begin() as other:other.get(User,user.id).password_hash=hasher.hash(NEW)
        original(db,user,code)
    monkeypatch.setattr(module,'verify_login_factor',changed_during_factor)
    with TestClient(app) as other:
        result=login(other);assert result.status_code==401,result.text
        assert 'lingxi_session' not in result.cookies
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(LoginSession))==1


def test_late_failure_rolls_back_password_factor_and_revocation(client,monkeypatch):
    import lingxi.account_password as module
    codes=factor()
    def failure(*args):raise RuntimeError('synthetic hashing failure')
    monkeypatch.setattr(module,'hasher',SimpleNamespace(hash=failure))
    with TestClient(app,raise_server_exceptions=False) as request:
        request.cookies.update(client.cookies);request.headers['X-CSRF-Token']=client.headers['X-CSRF-Token']
        result=request.put(PATH,json=body()|{'version':2,'code':codes[0]})
        assert result.status_code==500
    assert changed_count()==0 and client.get('/api/auth/me').status_code==200
    with SessionLocal() as db:
        assert hasher.verify(db.get(User,'owner').password_hash,OLD)
        row=db.scalar(select(TwoFactor));assert row.version==2 and len(row.recovery_hashes)==10


def test_changed_password_and_factor_survive_older_database_restore(client):
    from lingxi.backups import checkpoint,quarantine
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'));db.execute(text("INSERT INTO alembic_version VALUES ('0060')"))
    codes=factor()
    assert client.put(PATH,json=body()|{'version':2,'code':codes[0]}).status_code==200
    saved=checkpoint()
    with SessionLocal.begin() as db:
        db.get(User,'owner').password_hash=hasher.hash(OLD)
        row=db.scalar(select(TwoFactor));row.enabled=False;row.recovery_hashes=[]
    quarantine(saved)
    with TestClient(app) as other:
        assert login(other,OLD,codes[1]).status_code==401
        assert login(other,NEW,codes[0]).status_code==401
        assert login(other,NEW,codes[1]).status_code==200


def test_password_does_not_enter_read_metadata_or_diagnostics(client):
    assert client.put(PATH,json=body()).status_code==200
    with TestClient(app) as other:
        auth=login(other,NEW);other.headers['X-CSRF-Token']=auth.json()['csrf']
        for path in ('/api/auth/me','/api/auth/security','/api/admin/audit'):
            response=other.get(path);assert response.status_code==200
            assert OLD not in response.text and NEW not in response.text and 'password_hash' not in response.text
        result=other.post('/api/admin/diagnostics/preview',json={'request_id':'password-diagnostic','sections':['audit']})
        assert result.status_code==200 and OLD not in result.text and NEW not in result.text
    from lingxi.account_password import PasswordChangeInput
    assert NEW not in repr(PasswordChangeInput(**body()))


def test_auth_validation_never_echoes_login_or_second_factor_inputs():
    with TestClient(app) as request:
        result=request.post('/api/auth/login',json={'username':'owner','password':'private-too-long-'*30,'code':'private-factor-'*10})
        assert result.status_code==422
        assert 'private' not in result.text and 'input' not in result.text


def test_parallel_rotation_only_one_current_proof_can_win(client):
    if engine.dialect.name!='postgresql':pytest.skip('Requires PostgreSQL owner row locks')
    with TestClient(app) as second:
        auth=login(second);assert auth.status_code==200
        second.headers['X-CSRF-Token']=auth.json()['csrf']
        def rotate(request):return request.put(PATH,json=body()).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(rotate,[client,second]))
    assert sorted(results)==[200,401],results
    assert changed_count()==1
