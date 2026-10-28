"""Protocol and lifecycle regression tests; no claims about model quality."""
import httpx
from sqlalchemy import select
from lingxi import gateway
from lingxi.config import settings
from lingxi.db import SessionLocal
from lingxi.memory import MemoryService
from lingxi.models import Candidate, Conversation, Memory, Message, Model, Policy


def add_index_models():
    with SessionLocal.begin() as db:
        for purpose in ('embedding', 'rerank'):
            db.add(Model(id=purpose, provider_id='provider', name='fixture-'+purpose, purpose=purpose,
                         status='verified', verified_provider_version=1))
            db.add(Policy(purpose=purpose, model_id=purpose))


def test_private_gateway_authentication_and_model_override(configured, monkeypatch):
    from lingxi import api
    monkeypatch.setattr(settings(), 'memory_gateway_token', 'internal-test-token')
    seen = []
    def request(req):
        import json
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}]})
    monkeypatch.setattr(api, 'client_for', lambda p: httpx.Client(base_url='https://fixture/v1/', transport=httpx.MockTransport(request)))
    path = '/internal/model/v1/chat/completions'
    assert configured.post(path, json={}).status_code == 401
    response = configured.post(path, json={'model':'untrusted-model', 'messages':[]}, headers={'Authorization':'Bearer internal-test-token'})
    assert response.status_code == 200
    assert seen[0]['model'] == 'fixture-model'
    assert configured.post('/internal/model/v1/arbitrary', json={}, headers={'Authorization':'Bearer internal-test-token'}).status_code == 422
    assert configured.post(path, json={'stream':True}, headers={'Authorization':'Bearer internal-test-token'}).status_code == 422


def test_embedding_dimension_validation(configured, monkeypatch):
    add_index_models()
    monkeypatch.setattr(gateway, 'client_for', lambda p: httpx.Client(base_url='https://fixture/', transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={'data':[{'embedding':[0.1, 0.2]}]}))))
    result = configured.post('/api/models/embedding/test')
    assert result.status_code == 502 and '1024' in result.json()['detail']
    with SessionLocal() as db:
        assert db.get(Model, 'embedding').status == 'failed'


def test_policy_noop_does_not_create_new_bank(configured):
    add_index_models()
    configured.post('/api/memories', json={'content':'记忆已存在'})
    assert configured.put('/api/model-policies/embedding', json={'model_id':'embedding','version':1}).json()['version'] == 1
    with SessionLocal.begin() as db:
        db.add(Model(id='new-embedding', provider_id='provider', name='different', purpose='embedding', status='verified', verified_provider_version=1))
    assert configured.put('/api/model-policies/embedding', json={'model_id':'new-embedding','version':1}).status_code == 409


def test_recall_never_mixes_embedding_generations(configured):
    add_index_models()
    with SessionLocal.begin() as db:
        db.add_all([Memory(id='old',user_id='owner',content='旧索引',index_status='ready',index_epoch=0),
                    Memory(id='current',user_id='owner',content='当前索引',index_status='ready',index_epoch=1)])
    class Adapter:
        def recall(self, *args):
            return {'results':[{'document_id':'old-v1'},{'document_id':'current-v1'}]}
    with SessionLocal() as db:
        assert [m.id for m in MemoryService(Adapter()).search(db, 'owner', 'query')] == ['current']


def test_superseded_source_cannot_be_confirmed(client):
    with SessionLocal.begin() as db:
        db.add(Conversation(id='conversation',user_id='owner'))
        db.add(Message(id='source',conversation_id='conversation',role='user',content='旧事实',status='superseded'))
        db.add(Candidate(id='candidate',user_id='owner',source_id='source',fingerprint='x',content='旧事实',category='profile',reason='需核对'))
    assert client.post('/api/memory-candidates/candidate/actions', json={'action':'confirm'}).status_code == 409
    with SessionLocal() as db:
        assert not db.scalars(select(Memory)).all()
