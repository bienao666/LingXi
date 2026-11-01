"""Temporal evidence and PATCH semantics; no real model quality claims."""
import json
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import Memory, Job, ChatRun, Conversation, Message, Operation
from lingxi.memory import MemoryService, HindsightAdapter
from lingxi import worker
from test_core import create_conversation, submit
from test_delegation import create, drain, fixture_model, fetch


def saved(client, **extra):
    response = client.post('/api/memories', json={'content':'小林曾在杭州工作', 'category':'experiences',
        'subject':'小林', 'event_at':'2024-02-29', 'valid_from':'2024-01-01', 'valid_to':'2024-12-31'} | extra)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('value', ['2026-02-30', '2026-11', '昨天', '', '2026-11-01T09:00:00',
    '2026-11-01T09:00:00+25:00', '2026-11-01T09:00:00Z\n'])
def test_invalid_new_time_rejected_without_creating_jobs(client, value):
    response = client.post('/api/memories', json={'content':'时间资料', 'event_at':value})
    assert response.status_code == 422
    with SessionLocal() as db:
        assert not db.scalars(select(Memory)).all()
        assert not db.scalars(select(Job).where(Job.kind == 'index')).all()


def test_timezones_compare_actual_instants_and_same_day_is_allowed(client):
    memory = saved(client, valid_from='2026-11-01T09:00:00+08:00', valid_to='2026-11-01T01:00:00Z')
    assert memory['valid_from'].endswith('+08:00')  # Keep source precision and offset.
    assert saved(client, valid_from='2026-11-01', valid_to='2026-11-01')['valid_to']=='2026-11-01'
    assert client.post('/api/memories', json={'content':'顺序错误', 'valid_from':'2026-11-02',
        'valid_to':'2026-11-01'}).status_code == 422


def test_content_correction_preserves_subject_category_times_and_full_revision(client):
    memory = saved(client)
    response = client.patch('/api/memories/'+memory['id'], json={'content':'小林曾在杭州实习', 'version':1})
    assert response.status_code == 200, response.text
    changed = response.json()
    for key in ('subject','category','event_at','valid_from','valid_to'):
        assert changed[key] == memory[key]
        assert changed['revisions'][0][key] == memory[key]
    assert changed['version']==2 and changed['index_status']=='pending'
    assert client.patch('/api/memories/'+memory['id'],json={'content':'旧提交','version':1}).status_code==409


def test_one_bound_is_checked_against_retained_bound_and_null_explicitly_clears(client):
    memory = saved(client)
    path = '/api/memories/'+memory['id']
    assert client.patch(path,json={'content':memory['content'],'version':1,'valid_from':'2025-01-01'}).status_code==422
    assert client.get(path).json()['version']==1
    response = client.patch(path,json={'content':memory['content'],'version':1,'valid_to':None})
    assert response.status_code==200 and response.json()['valid_to'] is None
    assert response.json()['valid_from']=='2024-01-01'


def test_legacy_freeform_times_remain_readable_and_unchanged_until_explicitly_edited(client):
    with SessionLocal.begin() as db:
        db.add(Memory(id='legacy-time',user_id='owner',content='历史记录',event_at='去年春天',valid_from='入职时'))
    assert client.get('/api/memory-pages').json()['items'][0]['event_at']=='去年春天'
    path='/api/memories/legacy-time'
    assert client.patch(path,json={'content':'修正文字','version':1}).json()['event_at']=='去年春天'
    assert client.patch(path,json={'content':'修正文字','version':2,'valid_to':'2026-11-01'}).status_code==422
    response=client.patch(path,json={'content':'修正文字','version':2,'event_at':None,'valid_from':None,'valid_to':'2026-11-01'})
    assert response.status_code==200 and response.json()['event_at'] is None


def test_untrusted_client_cannot_edit_other_owner_or_bypass_authentication(client):
    memory=saved(client)
    with SessionLocal.begin() as db: db.get(Memory,memory['id']).user_id='other'
    assert client.patch('/api/memories/'+memory['id'],json={'content':'窃取','version':1,'event_at':'2026-11-01'}).status_code==404
    with TestClient(app) as anonymous:
        assert anonymous.post('/api/memories',json={'content':'未授权','event_at':'2026-11-01'}).status_code==401


def test_chat_receives_canonical_subject_times_including_past_future_and_unknown(configured, monkeypatch):
    saved(configured)
    saved(configured, content='未来安排', subject='self', valid_from='2030-01-01', valid_to=None)
    saved(configured, content='未注明日期', event_at=None, valid_from=None, valid_to=None)
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*args:list(db.scalars(select(Memory))))
    seen=[]
    def stream(model,provider,messages,*args,**kwargs):
        seen.extend(messages)
        yield '这些记录有不同主体和期间。'
    monkeypatch.setattr(worker,'stream',stream)
    conversation=create_conversation(configured)
    run=submit(configured,conversation,'谁在什么时间工作？')
    # API-created index jobs precede the chat; claim only the chat for this fixture.
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job).where(Job.kind=='index')):job.status='completed'
    worker.generate(worker.claim())
    payload=json.loads(seen[0]['content'].split('\n')[-1])
    memory_payload=payload['memories']
    assert '不要把过去或未来' in memory_payload and '空日期' in memory_payload
    evidence=json.loads(memory_payload.split('\n',1)[1])
    assert evidence[0]['subject']=='小林' and evidence[0]['event_at']=='2024-02-29'
    assert any(m['valid_from']=='2030-01-01' for m in evidence)
    assert any(m['event_at'] is None for m in evidence)
    assert configured.get('/api/chat-runs/'+run['id']).json()['config']['memory_context_version']=='temporal-evidence-v1'


def test_subassistant_receives_same_temporal_evidence_without_full_history(configured, monkeypatch):
    fixture_model(configured,monkeypatch)
    with SessionLocal.begin() as db:
        db.add(Memory(id='project-time',user_id='owner',content='过去的项目',category='projects',subject='团队甲',
            event_at='2025-01-01',valid_from='2025-01-01',valid_to='2025-03-01'))
    monkeypatch.setattr(MemoryService,'search',lambda self,db,*args:list(db.scalars(select(Memory))))
    row=create(configured,share_memory=True);drain()
    context=fetch(configured,row)['nodes'][0]['context']
    assert context['memories'][0]['valid_to']=='2025-03-01'
    assert context['memories'][0]['subject']=='团队甲'
    assert '不同期间的事实可并存' in context['memory_time_note']


def test_index_receives_subject_and_time_metadata_without_rewriting_canonical_fact(client,monkeypatch):
    memory=saved(client)
    writes=[]
    monkeypatch.setattr(HindsightAdapter,'retain',lambda self,*args:writes.append(args))
    worker.perform(worker.claim())
    assert len(writes)==1
    assert '小林' in writes[0][2] and '2024-02-29' in writes[0][2] and 'valid_to' in writes[0][2]
    canonical=client.get('/api/memories/'+memory['id']).json()
    assert canonical['content']==memory['content'] and canonical['index_status']=='ready'


def test_temporal_correction_invalidates_old_answers_and_deletion_erases_revision(client):
    memory=saved(client)
    with SessionLocal.begin() as db:
        db.add(Conversation(id='previous',user_id='owner'))
        db.add(ChatRun(id='previous-answer',user_id='owner',conversation_id='previous',request_id='previous-answer',
            request_hash='x',model_id='fixture',status='completed',memory_refs=[{'id':memory['id'],'version':1}]))
        db.add(Message(id='answer',conversation_id='previous',role='assistant',run_id='previous-answer',
            status='completed',content='基于旧日期的回答'))
    result=client.patch('/api/memories/'+memory['id'],json={'content':memory['content'],'version':1,'event_at':'2024-03-01'})
    assert result.status_code==200 and result.json()['revisions'][0]['event_at']=='2024-02-29'
    with SessionLocal() as db:
        assert db.get(ChatRun,'previous-answer').status=='invalidated'
        assert db.get(Message,'answer').content==''
    assert client.delete('/api/memories/'+memory['id']).status_code==200
    assert client.get('/api/memories/'+memory['id']).status_code==404
    with SessionLocal() as db:
        assert db.get(Memory,memory['id']).revisions==[]
        assert db.get(Memory,memory['id']).event_at is None
        assert db.get(Memory,memory['id']).subject=='unknown'


@pytest.mark.parametrize('format', ['markdown','json'])
def test_exports_preserve_current_temporal_evidence_and_exclude_deleted_records(client,format):
    memory=saved(client)
    response=client.post('/api/memory-exports',json={'format':format})
    identity=response.json()['id']
    with SessionLocal.begin() as db:
        row=db.get(Operation,identity);row.status='completed';row.result={'format':format}
    download=client.get('/api/memory-exports/'+identity+'/download')
    assert download.status_code==200
    assert '2024-02-29' in download.text and '2024-12-31' in download.text and '小林' in download.text
    client.delete('/api/memories/'+memory['id'])
    assert '2024-02-29' not in client.get('/api/memory-exports/'+identity+'/download').text
