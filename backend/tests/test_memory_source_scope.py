"""Memory source IDs never confer read/write access, including restore anomalies."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from lingxi.db import SessionLocal, engine, now
from lingxi.models import Candidate, ChatRun, Conversation, Job, Memory, Message, Model, User
from lingxi.memory import MemoryService
from lingxi import worker


def seed(variation='message-workspace'):
    with SessionLocal.begin() as db:
        db.add(User(id='other-owner', username='other-owner', password_hash='unused'))
        db.add_all([Conversation(id='own-c', user_id='owner'),
            Conversation(id='foreign-c', user_id='other-owner' if variation == 'owner' else 'owner',
                workspace_id='foreign' if variation == 'conversation-workspace' else 'personal')])
        db.add_all([Message(id='own-source', conversation_id='own-c', role='user', content='我喜欢茶。'),
            Message(id='foreign-source', conversation_id='foreign-c', role='user', content='PRIVATE-SOURCE',
                workspace_id='foreign' if variation == 'message-workspace' else 'personal')])
        db.add(Memory(id='anomalous-memory', user_id='owner', content='权威事实', source_ids=['own-source', 'foreign-source']))


@pytest.mark.parametrize('variation', ['message-workspace', 'conversation-workspace', 'owner'])
def test_read_correct_delete_do_not_disclose_or_modify_foreign_source(client, variation):
    seed(variation)
    path='/api/memories/anomalous-memory'
    r=client.get(path+'/sources');assert r.status_code==200
    assert '我喜欢茶。' in r.text and 'PRIVATE-SOURCE' not in r.text
    assert client.patch(path,json={'content':'纠正后的事实', 'version':1}).status_code==200
    with SessionLocal() as db:
        assert db.get(Message,'own-source').status=='superseded'
        assert db.get(Message,'foreign-source').status=='completed'
    assert client.delete(path).status_code==200
    with SessionLocal() as db:
        own,foreign=db.get(Message,'own-source'),db.get(Message,'foreign-source')
        assert own.content=='' and own.status=='redacted'
        assert foreign.content=='PRIVATE-SOURCE' and foreign.status=='completed'


@pytest.mark.parametrize('status', ['deleted','redacted','invalidated','failed','streaming'])
def test_unusable_source_body_is_not_shown(client, status):
    seed()
    with SessionLocal.begin() as db:db.get(Message,'own-source').status=status
    assert '我喜欢茶。' not in client.get('/api/memories/anomalous-memory/sources').text


def test_historical_source_remains_visible_but_assistant_is_not_a_fact(client):
    seed()
    with SessionLocal.begin() as db:db.get(Message,'own-source').status='superseded'
    assert '我喜欢茶。' in client.get('/api/memories/anomalous-memory/sources').text
    with SessionLocal.begin() as db:
        db.get(Message,'own-source').role='assistant'
        db.get(Message,'own-source').status='completed'
    with SessionLocal.begin() as db, pytest.raises(HTTPException):
        MemoryService().remember(db,'owner','不能采纳助手回答','profile',['own-source'])


@pytest.mark.parametrize('variation', ['message-workspace','conversation-workspace','owner'])
def test_candidates_cannot_review_preview_confirm_or_reject_foreign_source(client, variation):
    seed(variation)
    with SessionLocal.begin() as db:
        db.add(Candidate(id='bad-candidate',user_id='owner',source_id='foreign-source',fingerprint='scope',
            content='PRIVATE-SOURCE',category='profile',reason='invalid source pointer'))
    path='/api/memory-candidates/bad-candidate'
    assert client.get(path+'/sources').status_code==409
    assert client.post(path+'/review',json={'resolution':'keep'}).status_code==409
    assert client.post(path+'/actions',json={'action':'reject'}).status_code==409
    with SessionLocal() as db:assert db.get(Candidate,'bad-candidate').status=='pending'


def test_source_run_must_match_conversation_owner_and_workspace(client):
    seed()
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='foreign-run',workspace_id='foreign',user_id='owner',conversation_id='own-c',
            request_id='foreign-run',request_hash='x',model_id='unused'))
        db.get(Message,'own-source').run_id='foreign-run'
        db.add(Candidate(id='bad-candidate',user_id='owner',source_id='own-source',fingerprint='scope',
            content='我喜欢茶。',category='preferences',reason='mismatched run'))
    assert client.get('/api/memory-candidates/bad-candidate/sources').status_code==409


def test_service_binds_workspace_and_rejects_foreign_ids_before_enqueue(client):
    seed()
    with SessionLocal.begin() as db,pytest.raises(HTTPException):
        MemoryService().remember(db,'owner','bad','profile',['foreign-source'])
    with SessionLocal.begin() as db:
        db.get(User,'owner').workspace_id='dedicated'
    with SessionLocal.begin() as db:
        row=MemoryService().remember(db,'owner','手动记录','preferences',['user-entry:fixture'])
        assert row.workspace_id=='dedicated'
        job=db.scalar(select(Job).where(Job.kind=='index'))
        assert job.workspace_id=='dedicated'
    with SessionLocal.begin() as db,pytest.raises(HTTPException):
        MemoryService().remember(db,'owner','bad','profile',['user-entry:fixture'],workspace_id='personal')


def test_forget_does_not_invalidate_foreign_run_or_candidate_from_bad_pointer(client):
    seed()
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='foreign-run',user_id='other-owner',conversation_id='foreign-c',request_id='foreign-run',
            request_hash='x',model_id='unused',status='completed',source_message_ids=['foreign-source']))
        db.add(Candidate(id='foreign-candidate',user_id='other-owner',source_id='foreign-source',fingerprint='scope',
            memory_id='anomalous-memory',content='PRIVATE-CANDIDATE',category='profile',reason='not owned'))
    assert client.delete('/api/memories/anomalous-memory').status_code==200
    with SessionLocal() as db:
        assert db.get(ChatRun,'foreign-run').status=='completed'
        assert db.get(Candidate,'foreign-candidate').content=='PRIVATE-CANDIDATE'


def extraction_job(db, identity='own-source'):
    job=Job(key='scope-extract',kind='extract',payload={'message_id':identity},status='running',
        lease_token='scope-test',lease_until=now()+120)
    db.add(job);db.flush();return job


@pytest.mark.parametrize('variation', ['message-workspace','conversation-workspace','owner-workspace','run-workspace','model-workspace'])
def test_extraction_does_not_send_out_of_scope_data(configured,monkeypatch,variation):
    seed()
    with SessionLocal.begin() as db:
        if variation=='message-workspace':db.get(Message,'own-source').workspace_id='foreign'
        if variation=='conversation-workspace':db.get(Conversation,'own-c').workspace_id='foreign'
        if variation=='owner-workspace':db.get(User,'owner').workspace_id='foreign'
        if variation=='model-workspace':db.get(Model,'extraction').workspace_id='foreign'
        if variation=='run-workspace':
            db.add(ChatRun(id='bad-run',user_id='owner',workspace_id='foreign',conversation_id='own-c',
                request_id='bad-run',request_hash='x',model_id='extraction'))
            db.get(Message,'own-source').run_id='bad-run'
        job=extraction_job(db)
    calls=[]
    monkeypatch.setattr(worker,'complete',lambda *a,**k:calls.append(a))
    worker.extract(job);assert calls==[]
    with SessionLocal() as db:assert not db.scalar(select(Candidate))


def extracted():
    return json.dumps({'items':[{'content':'我喜欢茶。','quote':'我喜欢茶。','subject':'self',
        'category':'preferences','evidence':'explicit','sensitive':False}]},ensure_ascii=False)


@pytest.mark.parametrize('change', ['message-workspace','conversation-workspace','source-run','delete'])
def test_source_changes_during_model_request_block_late_auto_save(configured,monkeypatch,change):
    seed()
    with SessionLocal.begin() as db:job=extraction_job(db)
    def complete(*a,**k):
        if change=='delete':
            assert configured.delete('/api/conversations/own-c?delete_memories=true').status_code==200
        else:
            with SessionLocal.begin() as db:
                if change=='message-workspace':db.get(Message,'own-source').workspace_id='foreign'
                if change=='conversation-workspace':db.get(Conversation,'own-c').workspace_id='foreign'
                if change=='source-run':db.get(Message,'own-source').run_id='new-run'
        return extracted()
    monkeypatch.setattr(worker,'complete',complete)
    worker.extract(job)
    with SessionLocal() as db:
        assert not db.scalar(select(Candidate))
        assert len(db.scalars(select(Memory).where(Memory.status!='deleted')).all())==(0 if change=='delete' else 1)


def test_conversation_delete_does_not_touch_foreign_rows_sharing_bad_references(client):
    seed()
    with SessionLocal.begin() as db:
        db.add(Message(id='wrong-message',workspace_id='foreign',conversation_id='own-c',role='user',content='PRIVATE-WRONG-MESSAGE'))
        db.add(Memory(id='wrong-memory',workspace_id='foreign',user_id='owner',content='PRIVATE-WRONG-MEMORY',source_ids=['own-source']))
        db.add(ChatRun(id='wrong-run',workspace_id='foreign',conversation_id='own-c',user_id='owner',request_id='wrong-run',request_hash='x',model_id='unused',status='completed'))
    linked=client.get('/api/conversations/own-c/memories')
    assert linked.status_code==200 and 'PRIVATE-WRONG-MEMORY' not in linked.text
    assert client.delete('/api/conversations/own-c?delete_memories=true').status_code==200
    with SessionLocal() as db:
        assert db.get(Message,'wrong-message').content=='PRIVATE-WRONG-MESSAGE'
        assert db.get(Memory,'wrong-memory').status=='active'
        assert db.get(ChatRun,'wrong-run').status=='completed'


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='Conversation deletion requires PostgreSQL row locks')
def test_auto_save_and_conversation_delete_share_the_same_fence(configured,monkeypatch):
    import lingxi.api as api_module
    seed()
    with SessionLocal.begin() as db:
        # No existing same-category fact, so this low-risk source can auto-save.
        db.get(Memory,'anomalous-memory').category='projects'
        job=extraction_job(db)
    entered,release,deleting,attempted=Event(),Event(),Event(),Event()
    original,original_delete,original_owned=MemoryService.remember,api_module.delete_conversation,api_module.owned
    def remember(*args,**kw):
        entered.set();assert release.wait(10)
        return original(*args,**kw)
    def remove(*args,**kw):
        deleting.set();return original_delete(*args,**kw)
    def owned(db,entity,identity,user,lock=False):
        if entity is Conversation and identity=='own-c' and lock:
            attempted.set()
        return original_owned(db,entity,identity,user,lock=lock)
    monkeypatch.setattr(MemoryService,'remember',remember)
    monkeypatch.setattr(api_module,'delete_conversation',remove)
    monkeypatch.setattr(api_module,'owned',owned)
    monkeypatch.setattr(worker,'complete',lambda *a,**k:extracted())
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving=pool.submit(worker.extract,job);assert entered.wait(10)
        removing=pool.submit(configured.delete,'/api/conversations/own-c?delete_memories=true')
        try:
            assert attempted.wait(10),'Deletion must reach the actual conversation lock'
            assert not deleting.wait(.3),'Deletion must wait for auto-save before scanning memories'
        finally:release.set()
        saving.result(timeout=15);assert removing.result(timeout=15).status_code==200
    with SessionLocal() as db:
        assert not db.scalar(select(Memory).where(Memory.status!='deleted'))
        assert db.scalar(select(Candidate)).status=='invalidated'
