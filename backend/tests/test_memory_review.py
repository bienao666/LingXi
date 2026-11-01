import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import Candidate, ChatRun, Conversation, Job, Memory, Message, User
from lingxi.memory import fingerprint


def proposal(subject='self', content='我现在喜欢喝茶', category='preferences', **values):
    with SessionLocal.begin() as db:
        db.add(Conversation(id='review-conversation', user_id='owner'))
        db.add(Message(id='review-source', conversation_id='review-conversation', role='user',
            status='completed', content=content+'。这是我的原话。'))
        db.add(Candidate(id='review-candidate', user_id='owner', source_id='review-source',
            fingerprint=fingerprint(content), subject=subject, content=content, category=category,
            evidence='explicit', reason='请核对可能的冲突', **values))
    return '/api/memory-candidates/review-candidate'


def old(client, **values):
    response=client.post('/api/memories',json={'content':'我喜欢喝咖啡','subject':'self',
        'category':'preferences','event_at':'2020-01-01',**values})
    assert response.status_code==200,response.text
    return response.json()


def preview(client, path, target=None, **values):
    body={'resolution':'replace','target_memory_id':target['id'],'target_version':target['version']} if target else {'resolution':'keep'}
    response=client.post(path+'/review',json=body|values)
    assert response.status_code==200,response.text
    return response.json()


def confirm(client,path,data):
    return client.post(path+'/actions',json={'action':'confirm','review_token':data['token']})


def test_source_and_related_records_are_evidence_not_an_automatic_conflict(client):
    prior=old(client);path=proposal()
    result=client.get(path+'/sources').json()
    assert result['source']['content'].endswith('这是我的原话。')
    assert result['related'][0]['id']==prior['id'] and result['related_count']==1
    assert '不代表事实矛盾' in result['warning']
    assert client.post(path+'/actions',json={'action':'confirm'}).status_code==409


def test_keep_preserves_different_periods_and_does_not_grant_auto_save(client):
    prior=old(client);path=proposal()
    data=preview(client,path,valid_from='2026-11-01',valid_to='2026-11-03')
    assert data['before'] is None and data['after']['event_at'] is None
    result=confirm(client,path,data);assert result.status_code==200,result.text
    memory=client.get('/api/memories/'+result.json()['memory_id']).json()
    assert memory['id']!=prior['id'] and memory['valid_to']=='2026-11-03'
    assert memory['evidence']=='user_confirmed'
    assert client.get('/api/memories/'+prior['id']).json()['version']==1
    with SessionLocal() as db:assert db.get(User,'owner').auto_memory is True
    assert confirm(client,path,data).json()['memory_id']==memory['id']
    assert len(client.get('/api/memories').json())==2


def test_replace_preserves_history_invalidates_answers_and_never_inherits_dates(client):
    prior=old(client);path=proposal()
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='old-run',user_id='owner',conversation_id='review-conversation',request_id='old-request',
            request_hash='x',model_id='fixture',status='completed',memory_refs=[{'id':prior['id'],'version':1}]))
        db.add(Message(id='old-answer',conversation_id='review-conversation',role='assistant',run_id='old-run',
            content='旧事实的回答',status='completed'))
    data=preview(client,path,prior)
    assert data['before']['event_at']=='2020-01-01' and data['after']['event_at'] is None
    response=confirm(client,path,data);assert response.status_code==200,response.text
    memory=client.get('/api/memories/'+prior['id']).json()
    assert memory['version']==2 and memory['event_at'] is None and memory['content']=='我现在喜欢喝茶'
    assert memory['revisions'][0]['content']==prior['content'] and memory['revisions'][0]['event_at']=='2020-01-01'
    assert 'review-source' in memory['source_ids'] and memory['evidence']=='user_confirmed'
    assert confirm(client,path,data).json()['memory_id']==prior['id']
    with SessionLocal() as db:
        assert db.get(ChatRun,'old-run').status=='invalidated'
        assert db.get(Message,'old-answer').content==''
        assert len(list(db.scalars(select(Job).where(Job.key=='index:'+prior['id']+':2'))))==1


@pytest.mark.parametrize('change',['content','subject','source','delete','target'])
def test_changed_candidate_source_or_target_requires_a_new_review(client,change):
    prior=old(client);path=proposal();data=preview(client,path,prior)
    if change=='delete':client.delete('/api/memories/'+prior['id'])
    else:
        with SessionLocal.begin() as db:
            if change=='source':db.get(Message,'review-source').content='别的原文'
            elif change=='target':db.get(Memory,prior['id']).version+=1
            else:setattr(db.get(Candidate,'review-candidate'),change,'changed')
    assert confirm(client,path,data).status_code==409
    with SessionLocal() as db:assert db.get(Candidate,'review-candidate').memory_id is None


@pytest.mark.parametrize('field,value',[('event_at','2026-02-30'),('valid_from','昨天'),('valid_to','2026-01-01T09:00:00')])
def test_review_rejects_invalid_time_fields(client,field,value):
    path=proposal()
    assert client.post(path+'/review',json={'resolution':'keep',field:value}).status_code==422


def test_combined_period_validation_and_source_time_are_explicit(client):
    path=proposal()
    assert client.post(path+'/review',json={'resolution':'keep','valid_from':'2026-11-03','valid_to':'2026-11-01'}).status_code==422
    assert preview(client,path,event_at='2026-11-01T09:00:00+08:00')['after']['event_at']=='2026-11-01T09:00:00+08:00'


def test_review_token_is_bound_to_the_login_and_expires(client,monkeypatch):
    path=proposal();data=preview(client,path)
    with TestClient(app) as other:
        login=other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'}).json()
        other.headers['X-CSRF-Token']=login['csrf']
        assert confirm(other,path,data).status_code==409
    monkeypatch.setattr('lingxi.memory_review.now',lambda:data['expires_at']+1)
    assert confirm(client,path,data).status_code==409


def test_unknown_subject_agent_candidate_and_invalid_choices_cannot_be_accepted(client):
    path=proposal(subject='unknown')
    assert client.post(path+'/review',json={'resolution':'keep'}).status_code==409
    assert client.post(path+'/review',json={'resolution':'replace','target_memory_id':'missing'}).status_code==422
    assert client.post(path+'/review',json={'resolution':'keep','target_version':1}).status_code==422
    with SessionLocal.begin() as db:db.get(Candidate,'review-candidate').origin={'kind':'agent'}
    assert client.post(path+'/review',json={'resolution':'keep'}).status_code==409


@pytest.mark.parametrize('variation',['user','workspace','subject','category','same-source'])
def test_replacement_never_crosses_a_scope_or_overwrites_shared_source(client,variation):
    prior=old(client);path=proposal()
    with SessionLocal.begin() as db:
        memory=db.get(Memory,prior['id'])
        if variation=='same-source':memory.source_ids=['review-source']
        else:setattr(memory,{'user':'user_id','workspace':'workspace_id','subject':'subject','category':'category'}[variation],'other')
    result=client.post(path+'/review',json={'resolution':'replace','target_memory_id':prior['id'],'target_version':1})
    assert result.status_code in (404,409)
    assert client.get(path+'/sources').json()['related_count']==(1 if variation=='same-source' else 0)


def test_source_quotes_are_user_owned_and_not_assistant_facts(client):
    path=proposal()
    with SessionLocal.begin() as db:db.get(Message,'review-source').role='assistant'
    assert client.get(path+'/sources').status_code==409
    assert client.post(path+'/review',json={'resolution':'keep'}).status_code==409


def test_related_search_is_bounded_and_escapes_wildcards(client):
    path=proposal()
    with SessionLocal.begin() as db:
        for index in range(25):db.add(Memory(user_id='owner',content=f'不同偏好{index}'))
        db.add(Memory(user_id='owner',content='包含%符号'))
    data=client.get(path+'/sources').json()
    assert data['related_count']==26 and len(data['related'])==20 and data['truncated']
    match=client.get(path+'/sources',params={'q':'%'}).json()
    assert match['related_count']==26 and match['matched_count']==1 and match['related'][0]['content']=='包含%符号'


def test_no_model_call_or_write_happens_during_review_and_rejection_stays_available(client):
    path=proposal();preview(client,path)
    with SessionLocal() as db:
        assert list(db.scalars(select(Memory)))==[] and list(db.scalars(select(Job)))==[]
    assert client.post(path+'/actions',json={'action':'reject'}).json()['status']=='rejected'
    assert client.post(path+'/review',json={'resolution':'keep'}).status_code==409


def test_no_auth_cannot_inspect_or_submit_a_review(client):
    path=proposal()
    with TestClient(app) as anonymous:
        assert anonymous.get(path+'/sources').status_code==401
        assert anonymous.post(path+'/review',json={'resolution':'keep'}).status_code==401


def test_index_status_change_is_not_a_change_to_the_fact(client):
    prior=old(client);path=proposal();data=preview(client,path,prior)
    with SessionLocal.begin() as db:db.get(Memory,prior['id']).index_status='ready'
    assert confirm(client,path,data).status_code==200


@pytest.mark.parametrize('kind',['deleted-conversation','foreign-conversation','revoked-channel','automatic-transcript'])
def test_review_rechecks_dialogue_and_channel_authority(client,kind):
    path=proposal();data=preview(client,path)
    with SessionLocal.begin() as db:
        if kind=='deleted-conversation':db.get(Conversation,'review-conversation').deleted=True
        elif kind=='foreign-conversation':db.get(Conversation,'review-conversation').user_id='other'
        else:
            db.add(ChatRun(id='source-run',user_id='owner',conversation_id='review-conversation',request_id='source-request',
                request_hash='x',model_id='fixture',status='completed',config={'channel':{'memory_allowed':False}} if kind=='revoked-channel' else {'voice_input':{'automatic':True}}))
            db.get(Message,'review-source').run_id='source-run'
    assert confirm(client,path,data).status_code==409
    assert client.get('/api/memories').json()==[]


@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',reason='Row locks require PostgreSQL')
def test_concurrent_replacement_submissions_share_one_receipt(client):
    from concurrent.futures import ThreadPoolExecutor
    prior=old(client);path=proposal();data=preview(client,path,prior)
    with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(lambda _:confirm(client,path,data),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert {r.json()['memory_id'] for r in responses}=={prior['id']}
    assert client.get('/api/memories/'+prior['id']).json()['version']==2


@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',reason='Row locks require PostgreSQL')
def test_concurrent_conversation_deletion_cannot_miss_the_newly_confirmed_memory(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from lingxi.memory import MemoryService
    import lingxi.api as api_module
    path=proposal();data=preview(client,path)
    entered,release,deleted=Event(),Event(),Event()
    original=MemoryService.remember
    original_delete=api_module.delete_conversation
    def remember(*args,**kwargs):
        entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    def deleting(*args,**kwargs):
        deleted.set();return original_delete(*args,**kwargs)
    monkeypatch.setattr(MemoryService,'remember',remember)
    monkeypatch.setattr(api_module,'delete_conversation',deleting)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving=pool.submit(confirm,client,path,data)
        assert entered.wait(5)
        removing=pool.submit(client.delete,'/api/conversations/review-conversation?delete_memories=true')
        try:assert not deleted.wait(0.2),'Deletion must wait until confirmation releases its conversation fence'
        finally:release.set()
        assert saving.result(timeout=10).status_code==200
        assert removing.result(timeout=10).status_code==200
    assert client.get('/api/memories').json()==[]
    with SessionLocal() as db:assert db.get(Candidate,'review-candidate').status=='invalidated'
