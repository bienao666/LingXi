"""Long-lived memory management must remain traversable without freezing stale data."""
import json
import pytest
from fastapi.testclient import TestClient
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Candidate, Memory, Operation, User
from lingxi.security import decrypt, encrypt

ENDPOINTS=[('/api/memory-pages',Memory,{}),('/api/memory-candidate-pages',Candidate,{}),
           ('/api/memory-operation-pages',Operation,{'kind':'export'})]


def seed(model, count=231, **values):
    timestamp=now()-100
    with SessionLocal.begin() as db:
        for i in range(count):
            common={'id':f'row-{i:04}','user_id':'owner','created_at':timestamp}
            fields=({'content':f'记忆 {i}'} if model is Memory else
                    {'source_id':f'source-{i}','fingerprint':f'finger-{i}','content':f'候选 {i}',
                     'category':'preferences','reason':'核对主体'} if model is Candidate else {'kind':'export'})
            db.add(model(**(common|fields|values)))


def page(client, endpoint, **params):
    response=client.get(endpoint,params=params)
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('endpoint,model,params',ENDPOINTS)
def test_all_records_beyond_old_cap_with_same_timestamps(client,endpoint,model,params):
    seed(model)
    cursor=None
    seen=[]
    while True:
        data=page(client,endpoint,**params,**({'cursor':cursor} if cursor else {}))
        assert len(data['items'])<=50
        seen.extend(row['id'] for row in data['items'])
        cursor=data['next_cursor']
        if not cursor:break
    assert seen==[f'row-{i:04}' for i in range(230,-1,-1)]


@pytest.mark.parametrize('endpoint,model,params',ENDPOINTS)
def test_auth_scope_and_cursor_cannot_be_reused_for_other_workspace(client,endpoint,model,params):
    seed(model,3)
    with SessionLocal.begin() as db:
        db.get(model,'row-0000').workspace_id='foreign'
        db.get(model,'row-0001').user_id='stranger'
    data=page(client,endpoint,**params)
    assert [row['id'] for row in data['items']]==['row-0002']
    with TestClient(app) as anonymous:
        assert anonymous.get(endpoint,params=params).status_code==401
    seed_token=page(client,endpoint,**params,limit=1)
    assert seed_token['next_cursor'] is None
    # A correctly encrypted token bound to another workspace is still refused.
    from lingxi.conversation_pages import seal
    with SessionLocal() as db:
        user=db.get(User,'owner')
        token=seal(user,['memory-records','',''],{'time':now()-2,'id':'x','as_of':now()})
    data=json.loads(decrypt(token));data['workspace']='foreign'
    assert client.get(endpoint,params=params|{'cursor':encrypt(json.dumps(data))}).status_code==400


def test_insert_delete_and_correction_between_pages_do_not_freeze_content(client):
    seed(Memory,4)
    first=page(client,'/api/memory-pages',limit=1)
    with SessionLocal.begin() as db:
        db.get(Memory,'row-0002').status='deleted'
        db.get(Memory,'row-0001').content='纠正后的事实'
        db.add(Memory(id='new',user_id='owner',content='新记录'))
    second=page(client,'/api/memory-pages',cursor=first['next_cursor'])
    assert [row['id'] for row in second['items']]==['row-0001','row-0000']
    assert second['items'][0]['content']=='纠正后的事实'
    assert second['as_of']==first['as_of']
    assert page(client,'/api/memory-pages')['items'][0]['id']=='new'


def test_memory_filters_treat_wildcards_literally_and_cursor_binds_filters(client):
    seed(Memory,3,content='项目_100% Python',category='projects')
    assert len(page(client,'/api/memory-pages',q='_100%',category='projects')['items'])==3
    assert page(client,'/api/memory-pages',q='missing%')['items']==[]
    assert page(client,'/api/memory-pages',category='preferences')['items']==[]
    token=page(client,'/api/memory-pages',limit=1,q='_100%',category='projects')['next_cursor']
    for change in [{'q':'项目'},{'category':'preferences'}]:
        params={'q':'_100%','category':'projects','cursor':token}|change
        assert client.get('/api/memory-pages',params=params).status_code==400


def test_candidate_pending_filter_is_not_hidden_by_newer_reviewed_rows(client):
    seed(Candidate,202,status='confirmed')
    with SessionLocal.begin() as db:
        db.get(Candidate,'row-0000').status='pending'
        db.get(Candidate,'row-0001').status='invalidated'
    data=page(client,'/api/memory-candidate-pages',status='pending')
    assert [row['id'] for row in data['items']]==['row-0000']
    assert data['next_cursor'] is None
    token=page(client,'/api/memory-candidate-pages',limit=1)['next_cursor']
    assert client.get('/api/memory-candidate-pages',params={'cursor':token,'status':'pending'}).status_code==400
    assert all(row['id']!='row-0001' for row in page(client,'/api/memory-candidate-pages')['items'])


def test_operations_filter_in_database_before_paging(client):
    seed(Operation,204)
    with SessionLocal.begin() as db:
        db.get(Operation,'row-0000').kind='delete'
    assert [row['id'] for row in page(client,'/api/memory-operation-pages',kind='delete')['items']]==['row-0000']
    token=page(client,'/api/memory-operation-pages',kind='export',limit=1)['next_cursor']
    assert client.get('/api/memory-operation-pages',params={'kind':'delete','cursor':token}).status_code==400
    assert client.get('/api/memory-candidate-pages',params={'cursor':token}).status_code==400


@pytest.mark.parametrize('endpoint,model,params',ENDPOINTS)
def test_tampered_expired_and_malformed_positions_rejected(client,endpoint,model,params):
    seed(model,3)
    token=page(client,endpoint,**params,limit=1)['next_cursor']
    data=json.loads(decrypt(token));data['expires']=now()-1
    for bad in ['broken',token[:-6]+'broken',encrypt(json.dumps(data))]:
        assert client.get(endpoint,params=params|{'cursor':bad}).status_code==400
    data['expires']=now()+100
    for position in [[],None,{'time':True,'id':'x','as_of':now()},
                     {'time':float('nan'),'id':'x','as_of':now()},
                     {'time':now(),'id':'','as_of':now()}]:
        data['position']=position
        # A null position must not be treated as an absent cursor.
        assert client.get(endpoint,params=params|{'cursor':encrypt(json.dumps(data))}).status_code==400


@pytest.mark.parametrize('endpoint,params',[
    ('/api/memory-pages',{'limit':0}),('/api/memory-pages',{'limit':101}),
    ('/api/memory-pages',{'q':'x'*201}),('/api/memory-pages',{'category':'bad'}),
    ('/api/memory-candidate-pages',{'status':'invalidated'}),
    ('/api/memory-operation-pages',{}),('/api/memory-operation-pages',{'kind':'bad'}),
    ('/api/memory-pages',{'cursor':'x'*4097})])
def test_invalid_inputs(client,endpoint,params):
    assert client.get(endpoint,params=params).status_code==422
