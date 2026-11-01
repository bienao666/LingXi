"""History remains complete across ties, changing filters, inserts and credentials."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from lingxi.api import app
from lingxi.db import SessionLocal, now, engine
from lingxi.models import ModelCall, User
from lingxi.security import hasher, decrypt, encrypt


def seed(count=205, **changes):
    with SessionLocal.begin() as db:
        db.add_all([ModelCall(**(dict(id=f'call-{i:04}', model_id='former-model', purpose='chat',
            status='completed', duration_ms=10, created_at=10) | changes)) for i in range(count)])


def page(client, **params):
    response = client.get('/api/admin/model-call-pages', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_history_with_identical_timestamps(client):
    seed()
    ids, cursor = [], ''
    while True:
        data = page(client, cursor=cursor, limit=37)
        ids += [row['id'] for row in data['items']]
        cursor = data['next_cursor']
        if not cursor:
            break
    assert ids == [f'call-{i:04}' for i in range(204, -1, -1)]
    legacy = client.get('/api/admin/model-calls').json()
    assert len(legacy) == 200 and legacy[0]['id'] == 'call-0204'


def test_insert_and_removed_anchor_preserve_position(client):
    seed(5)
    first = page(client, limit=2)
    with SessionLocal.begin() as db:
        db.delete(db.get(ModelCall, 'call-0003'))
        db.add(ModelCall(id='new', model_id='former-model', purpose='chat', status='failed', duration_ms=1))
    second = page(client, cursor=first['next_cursor'])
    assert [r['id'] for r in second['items']] == ['call-0002', 'call-0001', 'call-0000']
    assert second['as_of'] == first['as_of']
    assert page(client)['items'][0]['id'] == 'new'


def test_scoped_records_and_cursor(client):
    seed(3)
    with SessionLocal.begin() as db:
        db.add(ModelCall(id='hidden', workspace_id='other', model_id='other', purpose='chat', status='completed', duration_ms=1))
        db.add(User(id='other-owner', workspace_id='other', username='other', password_hash=hasher.hash('other-password-123')))
    first = page(client, limit=1)
    assert all(row['id'] != 'hidden' for row in page(client)['items'])
    with TestClient(app) as other:
        assert other.get('/api/admin/model-call-pages').status_code == 401
        assert other.post('/api/auth/login', json={'username':'other','password':'other-password-123'}).status_code == 200
        assert [row['id'] for row in page(other)['items']] == ['hidden']
        assert other.get('/api/admin/model-call-pages', params={'cursor':first['next_cursor']}).status_code == 400


@pytest.mark.parametrize('change', [{'status':'failed'}, {'model_id':'different'}, {'purpose':'tts'}, {'days':7}])
def test_cursor_is_bound_to_filters(client, change):
    seed(3)
    token = page(client, limit=1)['next_cursor']
    assert client.get('/api/admin/model-call-pages', params={'cursor':token, **change}).status_code == 400


def test_filter_intersection_and_rolling_window(client):
    seed(1, created_at=now()-2*86400, status='failed', model_id='target', purpose='tts')
    with SessionLocal.begin() as db:
        db.add_all([ModelCall(id=identity, model_id=model, purpose=purpose, status=status, duration_ms=1)
            for identity, model, purpose, status in [('match','target','tts','failed'),
                ('different-model','else','tts','failed'), ('different-status','target','tts','completed'),
                ('different-purpose','target','chat','failed')]])
    data = page(client, days=1, model_id='target', purpose='tts', status='failed')
    assert [row['id'] for row in data['items']] == ['match']
    assert len(page(client, days=7, model_id='target', purpose='tts', status='failed')['items']) == 2
    assert page(client, model_id='absent')['items'] == []


def test_empty_and_last_page(client):
    assert page(client)['items'] == [] and page(client)['next_cursor'] is None
    seed(2)
    assert page(client, limit=2)['next_cursor'] is None


def test_expired_tampered_and_malformed_positions(client):
    seed(3)
    token = page(client, limit=1)['next_cursor']
    data = json.loads(decrypt(token))
    data['expires'] = now()-1
    expired = encrypt(json.dumps(data))
    for bad in [token[:-8]+'broken!!', 'not-a-token', expired]:
        assert client.get('/api/admin/model-call-pages', params={'cursor':bad}).status_code == 400
    for bad in [{'time':True,'id':'x','as_of':now()}, [], {'time':float('nan'),'id':'x','as_of':now()}]:
        data['expires'], data['position'] = now()+100, bad
        assert client.get('/api/admin/model-call-pages', params={'cursor':encrypt(json.dumps(data))}).status_code == 400


@pytest.mark.parametrize('params', [{'limit':0}, {'limit':201}, {'days':2}, {'days':'bad'},
    {'status':'unknown'}, {'purpose':'x'*31}, {'model_id':'x'*65}, {'cursor':'x'*4097}])
def test_invalid_inputs_are_rejected(client, params):
    assert client.get('/api/admin/model-call-pages', params=params).status_code == 422


def test_metadata_allowlists_and_unknown_cost_for_new_and_legacy(client):
    seed(1, usage={'prompt_tokens':12,'total_tokens':20,'debug':'secret',
        'completion_tokens':True, 'input_tokens':-1,
        'prompt_tokens_details':{'cached_tokens':3,'private':'secret'}},
        config={'model':'historic-model','provider_version':4,'prompt_version':'fixture-v1',
            'messages':['secret'], 'api_key':'secret', 'validation':True,
            'routing':{'version':2,'attempts':[{'prompt':'secret'}]}},
        billing={'status':'unknown','reason':'unpriced','extra':'secret',
            'price':{'version':2,'input_per_million':'2','api_key':'secret'}})
    for rows in [page(client)['items'], client.get('/api/admin/model-calls').json()]:
        row = rows[0]
        assert 'secret' not in json.dumps(row)
        assert row['usage'] == {'prompt_tokens':12,'total_tokens':20,'prompt_tokens_details':{'cached_tokens':3}}
        assert row['config']['model'] == 'historic-model'
        assert row['config']['routing'] == {'version':2,'attempt_count':1}
        assert row['billing']['status'] == 'unknown' and row['cost'] is None
        assert row['billing']['price'] == {'version':2,'input_per_million':'2'}


def test_estimate_uses_stored_price_not_current_configuration(client):
    seed(1, billing={'status':'estimated','currency':'CNY','amount':'0.0005','price':{'version':2}})
    row = page(client)['items'][0]
    assert row['billing']['amount'] == '0.0005' and row['billing']['price']['version'] == 2


def test_page_index_matches_query():
    index = next(i for i in inspect(engine).get_indexes('model_calls') if i['name'] == 'ix_model_calls_page')
    assert index['column_names'] == ['workspace_id','created_at','id']
