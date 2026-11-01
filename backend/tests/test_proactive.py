"""Proactive source integrity, feedback, timing, replay and audience boundaries."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Memory, Message, User, Conversation, ChatRun, Job
from lingxi.tasks import WorkTask
from lingxi import proactive as pro


def prefs(client, **values):
    current = client.get('/api/contact-preferences').json()
    response = client.put('/api/contact-preferences', json=current | values)
    assert response.status_code == 200, response.text
    return response.json()


def memory(client, **values):
    response = client.post('/api/memories', json={'content':'我在准备一个中文项目演示。','category':'preferences'} | values)
    assert response.status_code == 200, response.text
    return response.json()


def topic(client, source=None, **values):
    source = source or memory(client)
    response = client.post('/api/conversation-topics', json={'source_type':'memory','source_id':source['id'],
        'kind':'follow_up','title':'接着聊项目','not_before':now()-1,'expires_at':now()+86400} | values)
    assert response.status_code == 200, response.text
    return response.json()


def contacts(client):
    response = client.get('/api/proactive-contacts')
    assert response.status_code == 200, response.text
    return response.json()['items']


def ready(client):
    prefs(client, quiet_start='00:00', quiet_end='00:00')
    row = topic(client)
    pro.deliver(row['id'])
    assert len(contacts(client)) == 1
    return contacts(client)[0]


@pytest.mark.parametrize('path',['/api/contact-preferences','/api/conversation-topics','/api/proactive-contacts'])
def test_authentication_and_csrf(client, path):
    with TestClient(app) as anonymous:
        assert anonymous.get(path).status_code == 401
    client.headers.pop('X-CSRF-Token')
    assert client.put('/api/contact-preferences', json={'version':0}).status_code == 403


def test_default_limits_and_optimistic_update(client):
    original = client.get('/api/contact-preferences').json()
    assert (original['daily_limit'], original['min_gap_hours'], original['quiet_start'],original['quiet_end']) == (2,3,'22:00','08:00')
    assert original['version'] == 0
    updated = prefs(client, enabled=False)
    assert updated['version'] == 1 and not updated['enabled']
    assert client.put('/api/contact-preferences', json=original).status_code == 409


@pytest.mark.parametrize('values',[{'timezone':'somewhere/invalid'},{'quiet_start':'25:00'},
    {'quiet_end':'8:00'},{'daily_limit':11},{'min_gap_hours':0},{'allowed_kinds':['fiction']},
    {'paused_until':-1},{'blocked_sources':['user:all']}])
def test_invalid_preferences_rejected(client, values):
    assert client.put('/api/contact-preferences', json={'version':0} | values).status_code == 422


def test_explicit_project_memory_auto_creates_one_delayed_topic(client):
    source = memory(client,category='projects')
    rows = client.get('/api/conversation-topics').json()['items']
    assert len(rows) == 1 and rows[0]['origin'] == 'core'
    assert rows[0]['not_before'] > now()+86300
    with SessionLocal.begin() as db:
        pro.automatic_topic(db, db.get(Memory, source['id']))
    assert len(client.get('/api/conversation-topics').json()['items']) == 1


@pytest.mark.parametrize('extra',[{'subject':'friend'},{'evidence':'inferred'},{'category':'support'}])
def test_no_automatic_friend_inferred_or_emotion_topics(client,extra):
    with SessionLocal.begin() as db:
        source = Memory(user_id='owner',content='朋友的经历或推测，不是本人的稳定事实',category='projects')
        for key,value in extra.items():
            setattr(source,key,value)
        db.add(source)
        db.flush()
        pro.automatic_topic(db,source)
    assert client.get('/api/conversation-topics').json()['items'] == []


def test_plan_suggestion_uses_verified_structure_without_claiming_execution(client):
    prefs(client,quiet_start='00:00',quiet_end='00:00')
    with SessionLocal.begin() as db:
        source = WorkTask(user_id='owner',request_id='pro-plan-0001',request_hash='hash',title='演示计划',
            skill_id='project.plan',input={'goal':'准备演示'},status='succeeded',
            result={'steps':[{'title':'提纲','action':'整理提纲','completion':'列出三个重点'}]})
        db.add(source);db.flush()
        pro.automatic_topic(db,source)
        row = db.scalar(select(pro.ConversationTopic))
        row.not_before=now()-1
        identity=row.id
    pro.deliver(identity)
    content=contacts(client)[0]['content']
    assert '整理提纲' in content and '列出三个重点' in content and '步骤是否完成' in content
    assert '已完成演示' not in content


def test_grounded_share_once_without_a_user_question_and_no_read_claim(client):
    row=ready(client)
    assert '我在准备一个中文项目演示' in row['content']
    assert '还不知道后来的进展' in row['content']
    assert row['status']=='stored' and row['feedback'] is None
    assert '来源版本' in row['reason']
    for _ in range(3):
        pro.deliver(row['topic_id'])
    assert len(contacts(client))==1
    assert contacts(client)[0]['feedback'] is None


@pytest.mark.parametrize('values,reason',[
    ({'enabled':False},'已关闭'),({'mode':'results_only'},'仅接收'),({'daily_limit':0},'已关闭'),
    ({'allowed_kinds':[]},'未获允许'),({'paused_until':now()+86400},'暂缓')])
def test_disabled_paused_and_restricted_topics_never_send(client,values,reason):
    prefs(client,quiet_start='00:00',quiet_end='00:00',**values)
    row=topic(client)
    pro.deliver(row['id'])
    assert contacts(client)==[]
    assert reason in client.get('/api/conversation-topics').json()['items'][0]['reason']


@pytest.mark.parametrize('instant,expected',[
    ('2026-10-29T21:59:00',False),('2026-10-29T22:00:00',True),
    ('2026-10-30T07:59:00',True),('2026-10-30T08:00:00',False)])
def test_quiet_boundary_in_local_timezone(instant,expected):
    epoch=datetime.fromisoformat(instant).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    assert pro.quiet(pro.PreferenceValues().model_dump(),epoch)==expected


def test_quiet_deferral_rechecks_and_drops_expired_topic(client,monkeypatch):
    prefs(client)
    row=topic(client)
    evening=datetime(2026,10,29,23,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    with SessionLocal.begin() as db:
        entry=db.get(pro.ConversationTopic,row['id']);entry.not_before=evening-10;entry.expires_at=evening+60
    monkeypatch.setattr(pro,'now',lambda:evening)
    pro.deliver(row['id'])
    assert not contacts(client)
    monkeypatch.setattr(pro,'now',lambda:evening+9*3600)
    pro.deliver(row['id'])
    assert not contacts(client)
    assert client.get('/api/conversation-topics').json()['items'][0]['status']=='expired'


def test_daily_quota_and_minimum_gap_across_distinct_sources(client):
    first=ready(client)
    second=topic(client)
    pro.deliver(second['id'])
    assert len(contacts(client))==1
    with SessionLocal.begin() as db:
        db.get(pro.ProactiveContact,first['id']).created_at=now()-4*3600
    pro.deliver(second['id'])
    assert len(contacts(client))==2
    third=topic(client)
    with SessionLocal.begin() as db:
        for row in db.scalars(select(pro.ProactiveContact)):
            row.created_at=now()-4*3600
    # Anchor all records inside a fixed local day so midnight cannot make this flaky.
    instant=datetime(2026,10,29,16,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    with SessionLocal.begin() as db:
        for row in db.scalars(select(pro.ProactiveContact)):
            row.created_at=instant-4*3600
        item=db.get(pro.ConversationTopic,third['id']);item.not_before=instant-5;item.expires_at=instant+3600
    from unittest.mock import patch
    with patch.object(pro,'now',return_value=instant):
        pro.deliver(third['id'])
    assert len(contacts(client))==2
    assert '额度' in client.get('/api/conversation-topics').json()['items'][0]['reason']


def test_busy_conversation_defers_without_inventing_emotion(client):
    prefs(client,quiet_start='00:00',quiet_end='00:00')
    row=topic(client)
    with SessionLocal.begin() as db:
        db.add(ChatRun(user_id='owner',conversation_id='busy',request_id='busy-request-001',request_hash='h',model_id='x'))
    pro.deliver(row['id'])
    assert not contacts(client)
    reason=client.get('/api/conversation-topics').json()['items'][0]['reason']
    assert '会话正在处理' in reason and '心情' not in reason


def test_contact_continuation_is_idempotent_and_uses_one_history(client):
    row=ready(client)
    first=client.post('/api/proactive-contacts/'+row['id']+'/continue').json()
    assert client.post('/api/proactive-contacts/'+row['id']+'/continue').json()==first
    messages=client.get('/api/messages?conversation_id='+first['conversation_id']).json()
    assert len(messages)==1 and messages[0]['content']==row['content']


def test_target_conversation_and_cross_browser_delivery_are_atomic(client):
    conversation=client.post('/api/conversations',json={'title':'同一会话'}).json()
    prefs(client,quiet_start='00:00',quiet_end='00:00',conversation_id=conversation['id'])
    row=topic(client)
    pro.deliver(row['id']);pro.deliver(row['id'])
    messages=client.get('/api/messages?conversation_id='+conversation['id']).json()
    assert len(messages)==1
    assert contacts(client)[0]['message_id']==messages[0]['id']


def test_closed_topic_blocks_recreated_source_and_correction_version(client):
    row=topic(client)
    assert client.post('/api/conversation-topics/'+row['id']+'/close',json={'version':1}).status_code==200
    pro.deliver(row['id'])
    assert not contacts(client)
    assert f"memory:{row['source_id']}" in client.get('/api/contact-preferences').json()['blocked_sources']
    assert client.post('/api/conversation-topics/'+row['id']+'/close',json={'version':1}).status_code==409


@pytest.mark.parametrize('action',['helpful','stop_topic','pause','results_only'])
def test_feedback_is_explicit_versioned_and_immediate(client,action):
    row=ready(client)
    response=client.post('/api/proactive-contacts/'+row['id']+'/feedback',json={'version':row['version'],'feedback':action})
    assert response.status_code==200
    assert response.json()['feedback']==action
    assert client.post('/api/proactive-contacts/'+row['id']+'/feedback',json={'version':row['version'],'feedback':action}).status_code==409
    preferences=client.get('/api/contact-preferences').json()
    if action=='pause':assert preferences['paused_until']>now()+86300
    if action=='results_only':assert preferences['mode']=='results_only'
    if action=='stop_topic':assert preferences['blocked_sources']


@pytest.mark.parametrize('command',['先别聊','直接告诉我结果','以后别主动联系','这个以后别问'])
def test_chat_feedback_needs_no_model_and_is_not_extracted_as_user_fact(client,command):
    row=ready(client)
    conversation=client.post('/api/proactive-contacts/'+row['id']+'/continue').json()['conversation_id']
    body={'conversation_id':conversation,'request_id':'control-request-0001','content':command}
    response=client.post('/api/chat',json=body)
    assert response.status_code==200,response.text
    assert response.json()['status']=='completed'
    assert client.post('/api/chat',json=body).json()['id']==response.json()['id']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='chat'))==0
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='extract'))==0


@pytest.mark.parametrize('operation',['correct','delete'])
def test_source_changes_redact_contact_history_and_cancel_followups(client,operation):
    source=memory(client)
    prefs(client,quiet_start='00:00',quiet_end='00:00')
    row=topic(client,source)
    pro.deliver(row['id'])
    contact=contacts(client)[0]
    conversation=client.post('/api/proactive-contacts/'+contact['id']+'/continue').json()['conversation_id']
    if operation=='correct':
        assert client.patch('/api/memories/'+source['id'],json={'content':'已更正的项目说明','version':1}).status_code==200
    else:
        assert client.delete('/api/memories/'+source['id']).status_code==200
    result=contacts(client)[0]
    assert result['status']=='invalidated' and result['content']==''
    messages=client.get('/api/messages?conversation_id='+conversation).json()
    assert all('中文项目演示' not in m['content'] for m in messages)
    assert client.post('/api/proactive-contacts/'+contact['id']+'/continue').status_code==409
    pro.deliver(row['id'])
    assert len(contacts(client))==1


def test_deleting_target_conversation_cancels_topic_without_fallback(client):
    conversation=client.post('/api/conversations',json={'title':'授权目标'}).json()
    row=topic(client,conversation_id=conversation['id'])
    assert client.delete('/api/conversations/'+conversation['id']).status_code==200
    pro.deliver(row['id'])
    assert not contacts(client)
    assert client.get('/api/conversation-topics').json()['items'][0]['status']=='invalidated'


def test_cross_user_and_workspace_access_is_denied(client):
    row=ready(client)
    with SessionLocal.begin() as db:
        db.get(User,'owner').workspace_id='another-workspace'
    assert client.get('/api/proactive-contacts').json()['items']==[]
    assert client.post('/api/proactive-contacts/'+row['id']+'/continue').status_code==404
    assert client.post('/api/conversation-topics/'+row['topic_id']+'/close',json={'version':2}).status_code==404


def test_expired_or_wrong_subject_memory_cannot_become_topic(client):
    source=memory(client,subject='friend')
    response=client.post('/api/conversation-topics',json={'source_type':'memory','source_id':source['id'],
        'title':'朋友不是本人','not_before':now()-1,'expires_at':now()+3600})
    assert response.status_code==409


def test_quiet_unanswered_restarts_do_not_schedule_repeated_contact(client):
    row=ready(client)
    for _ in range(3):pro.tick()
    assert len(contacts(client))==1 and contacts(client)[0]['feedback'] is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Message))==0


def test_reconnecting_after_topic_expiry_does_not_prompt_old_idle_contact(client):
    row=ready(client)
    assert len(client.get('/api/proactive-contacts?unreviewed=true').json()['items'])==1
    with SessionLocal.begin() as db:
        db.get(pro.ConversationTopic,row['topic_id']).expires_at=now()-1
    assert client.get('/api/proactive-contacts?unreviewed=true').json()['items']==[]
    assert len(contacts(client))==1  # Historical station receipt remains inspectable.


@pytest.mark.parametrize('values',[{'enabled':False},{'daily_limit':0},{'mode':'results_only'},{'paused_until':now()+86400},
                                  {'allowed_kinds':[]}])
def test_unreviewed_browser_prompt_respects_new_contact_preferences(client,values):
    ready(client)
    prefs(client,**values)
    assert client.get('/api/proactive-contacts?unreviewed=true').json()['items']==[]
    assert len(contacts(client))==1


def test_browser_prompt_stops_in_quiet_hours_and_after_source_block(client,monkeypatch):
    row=ready(client)
    with SessionLocal() as db:
        source=pro.source_key(db.get(pro.ConversationTopic,row['topic_id']))
    prefs(client,blocked_sources=[source])
    assert not client.get('/api/proactive-contacts?unreviewed=true').json()['items']
    prefs(client,blocked_sources=[])
    monkeypatch.setattr(pro,'quiet',lambda *args:True)
    assert not client.get('/api/proactive-contacts?unreviewed=true').json()['items']


def test_quoted_feedback_is_data_not_a_command(client,configured):
    conversation=client.post('/api/conversations',json={'title':'引用'}).json()
    response=client.post('/api/chat',json={'conversation_id':conversation['id'],'content':'请解释“先别聊”这句话',
        'request_id':'quote-request-0001'})
    assert response.status_code==200
    assert response.json()['status']=='queued'
    assert client.get('/api/contact-preferences').json()['paused_until'] is None


def test_explicit_pause_applies_even_when_same_conversation_is_generating(configured):
    conversation=configured.post('/api/conversations',json={'title':'仍在生成'}).json()['id']
    pending=configured.post('/api/chat',json={'conversation_id':conversation,'content':'分析这个项目',
        'request_id':'busy-pro-request-01'}).json()
    reply=configured.post('/api/chat',json={'conversation_id':conversation,'content':'先别聊',
        'request_id':'busy-control-request-01'})
    assert reply.status_code==200 and reply.json()['status']=='completed'
    assert configured.get('/api/contact-preferences').json()['paused_until']>now()+86300
    assert configured.get('/api/chat-runs/'+pending['id']).json()['status']=='queued'


def test_pending_topic_schedule_can_change_with_version_guard(client):
    row=topic(client)
    body={'version':row['version'],'title':'改到明天','not_before':now()+86400,
          'expires_at':now()+2*86400,'conversation_id':None}
    response=client.put('/api/conversation-topics/'+row['id'],json=body)
    assert response.status_code==200,response.text
    assert response.json()['title']=='改到明天' and response.json()['version']==2
    assert client.put('/api/conversation-topics/'+row['id'],json=body).status_code==409
    pro.deliver(row['id'])
    assert contacts(client)==[]


def test_current_source_view_never_returns_stale_versions(client):
    source=memory(client)
    row=topic(client,source)
    response=client.get('/api/conversation-topics/'+row['id']+'/source')
    assert response.status_code==200 and response.json()['version']==1
    client.patch('/api/memories/'+source['id'],json={'content':'新的事实','version':1})
    assert client.get('/api/conversation-topics/'+row['id']+'/source').status_code==409


def test_stale_topic_cannot_invalidate_a_rearranged_current_source_version(client):
    source=memory(client)
    old=topic(client,source)
    client.patch('/api/memories/'+source['id'],json={'content':'纠正后的当前项目','version':1})
    current=topic(client,{'id':source['id']})
    # Simulate a queued old candidate surviving a restore; only that source version is invalid.
    with SessionLocal.begin() as db:
        db.get(pro.ConversationTopic,old['id']).status='pending'
    pro.deliver(old['id'])
    rows={item['id']:item for item in client.get('/api/conversation-topics').json()['items']}
    assert rows[old['id']]['status']=='invalidated'
    assert rows[current['id']]['status']=='pending' and rows[current['id']]['source_version']==2


def test_old_backup_deletion_replay_invalidates_proactive_content(client):
    from lingxi.memory import replay_ledger
    source=memory(client)
    prefs(client,quiet_start='00:00',quiet_end='00:00')
    row=topic(client,source)
    pro.deliver(row['id'])
    contact=contacts(client)[0]
    client.delete('/api/memories/'+source['id'])
    with SessionLocal.begin() as db:
        old=db.get(Memory,source['id']);old.status='active';old.content=source['content']
        stale=db.get(pro.ProactiveContact,contact['id']);stale.status='stored';stale.content=contact['content']
        db.get(pro.ConversationTopic,row['id']).status='contacted'
        assert replay_ledger(db)==1
    assert contacts(client)[0]['content']==''
    assert contacts(client)[0]['status']=='invalidated'


def test_correction_during_reply_invalidates_proactive_evidence_and_partial_output(configured,monkeypatch):
    from lingxi import worker
    source=memory(configured)
    prefs(configured,quiet_start='00:00',quiet_end='00:00')
    row=topic(configured,source);pro.deliver(row['id'])
    contact=contacts(configured)[0]
    conversation=configured.post('/api/proactive-contacts/'+contact['id']+'/continue').json()['conversation_id']
    with SessionLocal.begin() as db:
        for job in db.scalars(select(Job)):job.status='completed'
    run=configured.post('/api/chat',json={'conversation_id':conversation,'content':'接着梳理这个项目',
        'request_id':'pro-context-request-01'}).json()
    monkeypatch.setattr(worker.MemoryService,'search',lambda *args:[])
    def stream(model,provider,messages,run_id,check,**kwargs):
        check();yield '先前的记录'
        result=configured.patch('/api/memories/'+source['id'],json={'content':'已纠正的项目事实','version':1})
        assert result.status_code==200
        check();yield '这段不应送达'
    monkeypatch.setattr(worker,'stream',stream)
    job=worker.claim();assert job.kind=='chat'
    worker.perform(job)
    with SessionLocal() as db:
        current=db.get(ChatRun,run['id'])
        assert current.memory_refs==[{'id':source['id'],'version':1}]
        assert current.status=='invalidated'
        assert all(not msg.content for msg in db.scalars(select(Message).where(Message.run_id==current.id,Message.role=='assistant')))


@pytest.mark.parametrize('same_source',[True,False])
def test_postgres_concurrent_delivery_keeps_one_owner_budget(client,same_source):
    from concurrent.futures import ThreadPoolExecutor
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('Row locking verified on actual PostgreSQL')
    prefs(client,quiet_start='00:00',quiet_end='00:00',daily_limit=1)
    first=topic(client);second=first if same_source else topic(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(pro.deliver,[first['id'],second['id']]))
    assert len(results)==2 and len(contacts(client))==1
