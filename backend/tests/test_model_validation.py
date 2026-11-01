"""Capability protocol tests, not provider integration or Chinese quality evaluation."""
import json
import httpx
import pytest
from sqlalchemy import select
from lingxi import gateway
from lingxi.config import settings
from lingxi.db import SessionLocal
from lingxi.models import Model, ModelCall, Provider


def transport(monkeypatch, handler):
    monkeypatch.setattr(gateway, 'client_for', lambda p: httpx.Client(
        base_url='https://fixture/v1/', transport=httpx.MockTransport(handler)))


def model_id(client, purpose):
    return client.post('/api/models', json={'provider_id': 'provider', 'name': 'fixture-' + purpose,
                                           'purpose': purpose}).json()['id']


def extraction():
    return {'choices': [{'message': {'content': json.dumps({'items': [{
        'content': gateway.PROBE_SOURCE, 'quote': gateway.PROBE_SOURCE, 'subject': 'self',
        'category': 'preferences', 'evidence': 'explicit', 'sensitive': False}]})}}]}


@pytest.mark.parametrize('purpose', ['chat', 'extraction', 'embedding', 'rerank'])
def test_success_is_use_specific_and_logged(configured, monkeypatch, purpose):
    identity = model_id(configured, purpose)
    def request(req):
        body = json.loads(req.content)
        assert body['model'] == 'fixture-' + purpose
        if purpose == 'chat':
            assert body['stream'] is True
            return httpx.Response(200, text=': heartbeat\n\ndata: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
                                  'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":4}}\n\n')
        if purpose == 'extraction':
            assert body['stream'] is False and body['messages'][-1]['content'] == gateway.PROBE_SOURCE
            data = extraction()
        elif purpose == 'embedding':
            assert req.url.path.endswith('/embeddings')
            data = {'data': [{'embedding': [0.1] * settings().memory_embedding_dimensions}]}
        else:
            assert req.url.path.endswith('/rerank') and len(body['documents']) == 2
            data = {'results': [{'index': 1, 'relevance_score': 0.2}, {'index': 0, 'relevance_score': 0.9}]}
        return httpx.Response(200, json=data)
    transport(monkeypatch, request)
    result = configured.post(f'/api/models/{identity}/test')
    assert result.status_code == 200 and result.json()['status'] == 'verified', result.text
    with SessionLocal() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.model_id == identity))
        assert call.purpose == purpose and call.status == 'completed'
        assert call.config['validation'] is True and call.config['probe_version'] == 'capabilities-v2'
        assert db.get(Model, identity).verified_provider_version == 1


@pytest.mark.parametrize('body', [
    'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    'data: [DONE]\n\n',
    '{"choices":[{"message":{"content":"ordinary completion"}}]}',
    'data: {"choices":[{"delta":{"content":" "},"finish_reason":"stop"}]}\n\n',
])
def test_chat_rejects_broken_empty_or_nonstream_response(configured, monkeypatch, body):
    transport(monkeypatch, lambda req: httpx.Response(200, text=body))
    result = configured.post('/api/models/chat/test')
    assert result.status_code == 502
    with SessionLocal() as db:
        assert db.get(Model, 'chat').status == 'failed'
        assert db.scalar(select(ModelCall)).status == 'failed'
    assert configured.put('/api/model-policies/chat', json={'model_id': 'chat', 'version': 1}).status_code == 409


@pytest.mark.parametrize('kind', ['plain', 'empty', 'invented', 'wrong_subject', 'sensitive'])
def test_extraction_requires_structured_source_evidence(configured, monkeypatch, kind):
    data = extraction()
    content = json.loads(data['choices'][0]['message']['content'])
    if kind == 'empty':
        content['items'] = []
    elif kind == 'invented':
        content['items'][0]['quote'] = content['items'][0]['content'] = '我喜欢咖啡'
    elif kind == 'wrong_subject':
        content['items'][0]['subject'] = 'friend'
    elif kind == 'sensitive':
        content['items'][0]['sensitive'] = True
    data['choices'][0]['message']['content'] = 'OK' if kind == 'plain' else json.dumps(content)
    transport(monkeypatch, lambda req: httpx.Response(200, json=data))
    result = configured.post('/api/models/extraction/test')
    assert result.status_code == 502 and '原文证据' in result.json()['detail']
    with SessionLocal() as db:
        assert db.get(Model, 'extraction').status == 'failed'
        assert db.scalar(select(ModelCall)).status == 'failed'


@pytest.mark.parametrize('value', ['0.1', True, None, 0])
def test_embedding_requires_numeric_nonzero_vector(configured, monkeypatch, value):
    identity = model_id(configured, 'embedding')
    transport(monkeypatch, lambda req: httpx.Response(200, json={
        'data': [{'embedding': [value] * settings().memory_embedding_dimensions}]}))
    result = configured.post(f'/api/models/{identity}/test')
    assert result.status_code == 502 and '有限数值' in result.json()['detail']


@pytest.mark.parametrize('results', [
    [{'index': 2, 'relevance_score': 0.5}],
    [{'index': 0, 'relevance_score': 0.5}, {'index': 0, 'relevance_score': 0.1}],
    [{'index': 0, 'relevance_score': '0.5'}],
    [{'index': True, 'relevance_score': 0.5}],
    ['not-a-result'], [],
])
def test_rerank_requires_valid_indices_and_scores(configured, monkeypatch, results):
    identity = model_id(configured, 'rerank')
    transport(monkeypatch, lambda req: httpx.Response(200, json={'results': results}))
    assert configured.post(f'/api/models/{identity}/test').status_code == 502


@pytest.mark.parametrize('status,hint', [(401, '密钥'), (403, '权限'), (404, '根地址'), (429, '额度'), (503, '兼容性')])
def test_actionable_http_errors_redact_upstream_data(configured, monkeypatch, status, hint):
    transport(monkeypatch, lambda req: httpx.Response(status, text='secret-fixture-key private-prompt'))
    result = configured.post('/api/models/chat/test')
    assert result.status_code == 502 and hint in result.json()['detail']
    calls = configured.get('/api/admin/model-calls').json()
    assert len(calls) == 1 and calls[0]['status'] == 'failed'
    assert 'secret-fixture-key' not in result.text + json.dumps(calls)
    assert 'private-prompt' not in result.text + json.dumps(calls)


def test_disabled_provider_cannot_validate(configured, monkeypatch):
    with SessionLocal.begin() as db:
        db.get(Provider, 'provider').enabled = False
    transport(monkeypatch, lambda req: pytest.fail('Disabled provider must not be called'))
    assert configured.post('/api/models/chat/test').status_code == 502
