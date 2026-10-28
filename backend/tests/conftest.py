import os
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
from cryptography.fernet import Fernet

TEMP = tempfile.TemporaryDirectory(prefix='lingxi-tests-')
os.environ['LINGXI_DATABASE_URL'] = os.environ.get('LINGXI_TEST_DATABASE_URL', 'sqlite:///' + str(Path(TEMP.name) / 'tests.db'))
os.environ['LINGXI_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['LINGXI_RUNTIME_DIR'] = TEMP.name
os.environ['LINGXI_COOKIE_SECURE'] = 'false'
os.environ['LINGXI_PUBLIC_ORIGIN'] = 'http://testserver'
os.environ['LINGXI_HINDSIGHT_URL'] = 'http://127.0.0.1:1'

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.db import Base, SessionLocal, engine
from lingxi.models import User
from lingxi.security import hasher
from lingxi.api import app


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
    TEMP.cleanup()


@pytest.fixture(autouse=True)
def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    for file in Path(TEMP.name).glob('*.jsonl'):
        file.unlink()
    with SessionLocal.begin() as db:
        db.add(User(id='owner', username='owner', password_hash=hasher.hash('test-password-123')))


@pytest.fixture
def client():
    with TestClient(app) as client:
        login = client.post('/api/auth/login', json={'username':'owner', 'password':'test-password-123'})
        assert login.status_code == 200, login.text
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        yield client


@pytest.fixture
def configured(client):
    from lingxi.models import Provider, Model, Policy
    from lingxi.security import encrypt
    with SessionLocal.begin() as db:
        p = Provider(id='provider', name='Protocol fixture', base_url='https://example.com/v1', encrypted_key=encrypt('secret-fixture-key'))
        db.add(p)
        for purpose in ('chat', 'extraction'):
            db.add(Model(id=purpose, provider_id=p.id, name='fixture-model', purpose=purpose, status='verified', verified_provider_version=1))
            db.add(Policy(purpose=purpose, model_id=purpose))
    return client
