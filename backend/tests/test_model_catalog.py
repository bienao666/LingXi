"""Provider catalog authentication, normalization and failure behavior."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi import catalog
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import Model, Provider
from lingxi.security import encrypt


def transport(monkeypatch, handler):
    monkeypatch.setattr(catalog, 'client_for', lambda p: httpx.Client(
        base_url=p.base_url.rstrip('/')+'/', headers={'Authorization': 'Bearer fixture-only-key'},
        transport=httpx.MockTransport(handler)))


def test_catalog_uses_selected_connection_without_creating_or_verifying_models(configured, monkeypatch):
    seen = []
    def request(req):
        seen.append((req.method, str(req.url), req.headers['Authorization']))
        return httpx.Response(200, json={'data': [{'id':'z-model','secret':'never-return'}, {'id':'a-model'}, {'id':'z-model'}]})
    transport(monkeypatch, request)
    result = configured.get('/api/providers/provider/models')
    assert result.status_code == 200, result.text
    assert result.json() == {'provider_id':'provider','provider_version':1,'models':[{'id':'a-model'},{'id':'z-model'}]}
    assert seen == [('GET','https://example.com/v1/models','Bearer fixture-only-key')]
    assert 'never-return' not in result.text and 'fixture-only-key' not in result.text
    with SessionLocal() as db:
        assert len(db.scalars(select(Model)).all()) == 2


def test_catalog_requires_identity_workspace_and_enabled_connection(configured, monkeypatch):
    transport(monkeypatch, lambda req: pytest.fail('Unauthorized catalog must not be called'))
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/providers/provider/models').status_code == 401
    with SessionLocal.begin() as db:
        db.add(Provider(id='foreign', workspace_id='another', name='Other',base_url='https://example.com/v1',encrypted_key=encrypt('other-key')))
        db.get(Provider,'provider').enabled = False
    assert configured.get('/api/providers/foreign/models').status_code == 404
    assert configured.get('/api/providers/unknown/models').status_code == 404
    assert configured.get('/api/providers/provider/models').status_code == 409


@pytest.mark.parametrize('status', [401,404,429])
def test_catalog_redacts_provider_errors(configured, monkeypatch, status):
    transport(monkeypatch, lambda req: httpx.Response(status,text='fixture-only-key private-data'))
    result = configured.get('/api/providers/provider/models')
    assert result.status_code == 502 and str(status) in result.json()['detail']
    assert 'fixture-only-key' not in result.text and 'private-data' not in result.text


@pytest.mark.parametrize('data', [{'data':'bad'}, {'data':[{}]}, {'data':[{'id':''}]},
                                {'data':[{'id':'x'}],'has_more':True}])
def test_catalog_rejects_incompatible_or_incomplete_response(configured, monkeypatch, data):
    transport(monkeypatch, lambda req: httpx.Response(200,json=data))
    result = configured.get('/api/providers/provider/models')
    assert result.status_code == 502 and '/models' in result.json()['detail']


def test_empty_catalog_is_real_empty_success(configured, monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200,json={'data':[]}))
    assert configured.get('/api/providers/provider/models').json()['models'] == []


def test_catalog_is_bounded(configured, monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200,content=b' '*(2*1024*1024+1)))
    result = configured.get('/api/providers/provider/models')
    assert result.status_code == 502 and '过大' in result.json()['detail']


def test_catalog_network_error_is_actionable(configured, monkeypatch):
    def request(req):
        raise httpx.ReadTimeout('credential-bearing-debug-info', request=req)
    transport(monkeypatch, request)
    result = configured.get('/api/providers/provider/models')
    assert result.status_code == 502 and '超时' in result.json()['detail']
    assert 'credential-bearing' not in result.text


@pytest.mark.parametrize('base,path', [
    ('https://example.com','/models'), ('https://example.com/V1','/V1/models'),
    ('https://example.com/v1/','/v1/models'), ('https://example.com/gateway/v1','/gateway/v1/models'),
])
def test_catalog_uses_configured_root_without_probing_another_path(configured, monkeypatch, base, path):
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').base_url=base
    seen=[]
    def request(req):
        seen.append(req.url.path)
        return httpx.Response(200,json={'data':[{'id':'real-model'}]})
    transport(monkeypatch,request)
    result=configured.get('/api/providers/provider/models')
    assert result.status_code==200
    assert seen==[path]


def test_catalog_does_not_probe_host_root_when_api_root_contains_v1(configured,monkeypatch):
    seen=[]
    def request(req):
        seen.append(req.url.path)
        return httpx.Response(200,json={'data':[{'id':'api-model'}]})
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').base_url='https://example.com/v1'
    transport(monkeypatch,request)
    result=configured.get('/api/providers/provider/models')
    assert result.status_code==200 and result.json()['models']==[{'id':'api-model'}]
    assert seen==['/v1/models']


@pytest.mark.parametrize('status',[401,429])
def test_catalog_does_not_retry_auth_or_rate_errors(configured,monkeypatch,status):
    seen=[]
    def request(req):
        seen.append(req.url.path)
        return httpx.Response(status)
    transport(monkeypatch,request)
    assert configured.get('/api/providers/provider/models').status_code==502
    assert seen==['/v1/models']


def test_catalog_reports_html_without_exposing_page_content(configured,monkeypatch):
    transport(monkeypatch,lambda req:httpx.Response(200,text='<!doctype html><html>private-secret</html>'))
    result=configured.get('/api/providers/provider/models')
    assert result.status_code==502 and 'JSON' in result.json()['detail']
    assert 'private-secret' not in result.text and '手动填写' not in result.text
