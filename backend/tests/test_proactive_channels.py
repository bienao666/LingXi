"""Private QQ proactive consent, dispatch, receipts and restoration barriers."""
import concurrent.futures
import pytest
from sqlalchemy import select, delete
from lingxi.db import SessionLocal, engine, now
from lingxi.models import User, Message
from lingxi import proactive as pro
from lingxi import proactive_channels as pc
from lingxi.channels import Channel, IdentityLink, ChannelDelivery, ChannelInbound
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from test_channels import create, connect, bind, rules
from test_proactive import prefs, topic, contacts


def setup(client):
    channel=create(client);connection=connect(channel['id'])
    bind(client,channel,connection,memory=True)
    target=client.get('/api/proactive-targets').json()[0]['target']
    prefs(client,delivery=target,qq_acknowledged=True,quiet_start='00:00',quiet_end='00:00')
    return channel,connection,target


def queued(client):
    channel,connection,target=setup(client)
    row=topic(client);pro.deliver(row['id'])
    assert contacts(client)[0]['delivery']['status']=='pending'
    return channel,connection,contacts(client)[0]


def receipt(channel,connection,action,**event):
    receive_event(channel['id'],connection,{'echo':action['echo'],'retcode':0,'data':{'message_id':1234},**event})


def test_default_and_explicit_qq_consent(client):
    assert client.get('/api/contact-preferences').json()['delivery']['kind']=='web'
    channel=create(client);connection=connect(channel['id']);bind(client,channel,connection,memory=True)
    target=client.get('/api/proactive-targets').json()[0]
    assert target['online']
    body=client.get('/api/contact-preferences').json()|{'delivery':target['target']}
    assert client.put('/api/contact-preferences',json=body).status_code==422
    assert client.put('/api/contact-preferences',json=body|{'qq_acknowledged':True}).status_code==200
    assert client.put('/api/contact-preferences',json=body|{'qq_acknowledged':True}).status_code==409


def test_target_and_review_authentication_csrf(client):
    from fastapi.testclient import TestClient
    from lingxi.api import app
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/proactive-targets').status_code==401
        assert anonymous.post('/api/proactive-contacts/missing/delivery-review',json={'version':1,'acknowledgement':'checked_no_resend'}).status_code==401
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/proactive-contacts/missing/delivery-review',json={'version':1,'acknowledgement':'checked_no_resend'}).status_code==403


@pytest.mark.parametrize('memory',[False,True])
def test_unconfirmed_or_no_memory_binding_not_eligible(client,memory):
    channel=create(client);connection=connect(channel['id'])
    if not memory:bind(client,channel,connection,memory=False)
    else:client.post('/api/identity-links',json={'channel_id':channel['id'],'external_user_id':'87654321','allow_memory':True})
    assert client.get('/api/proactive-targets').json()==[]


@pytest.mark.parametrize('change',['binding','channel','owner','group'])
def test_changed_scope_or_forged_target_rejected(client,change):
    channel,connection,target=setup(client)
    if change=='group':target={'kind':'qq','binding_id':'group:123','binding_version':1,'channel_version':1}
    else:
        with SessionLocal.begin() as db:
            if change=='binding':db.get(IdentityLink,target['binding_id']).version+=1
            if change=='channel':db.get(Channel,channel['id']).version+=1
            if change=='owner':db.get(IdentityLink,target['binding_id']).workspace_id='foreign'
    body=client.get('/api/contact-preferences').json()|{'delivery':target,'qq_acknowledged':True}
    assert client.put('/api/contact-preferences',json=body).status_code in (404,409)


def test_cannot_mix_web_conversation_and_qq(client):
    _,_,target=setup(client)
    conversation=client.post('/api/conversations',json={'title':'Web'}).json()
    body=client.get('/api/contact-preferences').json()|{'conversation_id':conversation['id']}
    assert client.put('/api/contact-preferences',json=body).status_code==422


def test_offline_no_creation_no_fallback_and_expiry(client):
    channel,connection,_=setup(client);close_connection(channel['id'],connection)
    row=topic(client);pro.deliver(row['id'])
    assert not contacts(client)
    assert '离线' in client.get('/api/conversation-topics').json()['items'][0]['reason']
    with SessionLocal.begin() as db:db.get(pro.ConversationTopic,row['id']).expires_at=now()-1
    connect(channel['id']);pro.deliver(row['id'])
    assert not contacts(client)


def test_queued_is_not_web_notification_or_conversation_until_receipt(client):
    channel,connection,contact=queued(client)
    assert not contact['conversation_id']
    assert client.get('/api/proactive-contacts?unreviewed=true').json()['items']==[]
    assert client.post('/api/proactive-contacts/'+contact['id']+'/continue').status_code==409
    action=prepare_outbound(channel['id'],connection)
    assert action['action']=='send_private_msg' and action['params']['user_id']==87654321
    assert action['params']['message'][0]['data']['text']==contact['content']
    assert not contacts(client)[0]['message_id']
    receipt(channel,connection,action)
    result=contacts(client)[0]
    assert result['delivery']['status']=='accepted_by_protocol'
    assert result['delivery']['protocol_message_id']=='1234'
    assert result['conversation_id'] and result['message_id']
    assert client.post('/api/proactive-contacts/'+result['id']+'/continue').json()['conversation_id']==result['conversation_id']
    receipt(channel,connection,action)
    with SessionLocal() as db:assert len(list(db.scalars(select(Message).where(Message.role=='assistant'))))==1
    assert prepare_outbound(channel['id'],connection) is None


@pytest.mark.parametrize('event',[{'echo':'wrong'}, {'retcode':True}, {'retcode':0,'data':{}}, {'retcode':'0'}])
def test_bad_receipt_never_claims_acceptance(client,event):
    channel,connection,_=queued(client);action=prepare_outbound(channel['id'],connection)
    receipt(channel,connection,action,**event)
    assert contacts(client)[0]['delivery']['status']=='sending'


@pytest.mark.parametrize('cause',['pause','disabled','source_deleted','source_corrected','channel_changed','closed','expired'])
def test_before_send_rechecks_all_revocations(client,cause):
    channel,connection,contact=queued(client)
    if cause=='pause':prefs(client,paused_until=now()+3600)
    if cause=='disabled':prefs(client,enabled=False)
    if cause.startswith('source_'):
        with SessionLocal() as db:source=db.get(pro.ConversationTopic,contact['topic_id']).source_id
        memory=client.get('/api/memories').json()[0]
        if cause=='source_deleted':assert client.delete('/api/memories/'+source).status_code==200
        else:
            assert client.patch('/api/memories/'+source,json={'content':'我改了演示安排。','category':'preferences','version':memory['version']}).status_code==200
    if cause=='channel_changed':rules(client,channel,private_enabled=False)
    if cause=='closed':
        row=client.get('/api/conversation-topics').json()['items'][0]
        assert client.post('/api/conversation-topics/'+row['id']+'/close',json={'version':row['version']}).status_code==200
    if cause=='expired':
        with SessionLocal.begin() as db:db.get(pro.ConversationTopic,contact['topic_id']).expires_at=now()-1
        pc.expire_pending()
    assert prepare_outbound(channel['id'],connection) in (None,False)
    assert contacts(client)[0]['delivery']['status']=='cancelled'


def test_quiet_after_queue_waits_without_discard_and_offline_expiry(client,monkeypatch):
    channel,connection,contact=queued(client)
    monkeypatch.setattr(pro,'quiet',lambda *_:True)
    assert prepare_outbound(channel['id'],connection) is None
    assert contacts(client)[0]['delivery']['status']=='pending'
    close_connection(channel['id'],connection)
    with SessionLocal.begin() as db:db.get(pro.ConversationTopic,contact['topic_id']).expires_at=now()-1
    pc.expire_pending()
    assert contacts(client)[0]['delivery']['status']=='cancelled'


def test_reconnect_sends_unexpired_pending_once(client):
    channel,connection,contact=queued(client)
    close_connection(channel['id'],connection)
    assert contacts(client)[0]['delivery']['status']=='pending'
    connection=connect(channel['id'])
    action=prepare_outbound(channel['id'],connection)
    assert action['action']=='send_private_msg'
    receipt(channel,connection,action)
    assert prepare_outbound(channel['id'],connection) is None


def test_binding_revoke_cancels_pending(client):
    channel,connection,contact=queued(client)
    binding=client.get('/api/identity-links').json()[0]
    assert client.post('/api/identity-links/'+binding['id']+'/revoke',json={'version':binding['version']}).status_code==200
    assert prepare_outbound(channel['id'],connection) is None
    assert contacts(client)[0]['delivery']['status']=='cancelled'
    assert not client.get('/api/proactive-targets').json()


def test_rejection_of_one_topic_does_not_cancel_unrelated_queued_topic(client):
    channel,connection,first=queued(client)
    with SessionLocal.begin() as db:db.get(pro.ProactiveContact,first['id']).created_at=now()-4*3600
    another=topic(client);pro.deliver(another['id'])
    assert len(contacts(client))==2
    assert client.post('/api/proactive-contacts/'+first['id']+'/feedback',json={'version':first['version'],'feedback':'stop_topic'}).status_code==200
    assert [row['delivery']['status'] for row in contacts(client)]==['pending','cancelled']
    action=prepare_outbound(channel['id'],connection)
    assert isinstance(action,dict)


def test_accepted_conversation_delete_redacts_contact(client):
    channel,connection,_=queued(client);action=prepare_outbound(channel['id'],connection);receipt(channel,connection,action)
    row=contacts(client)[0]
    assert client.delete('/api/conversations/'+row['conversation_id']).status_code==200
    row=contacts(client)[0]
    assert row['status']=='invalidated' and not row['content']
    assert row['delivery']['status']=='accepted_by_protocol'


def test_user_text_feedback_on_exact_qq_conversation_applies_immediately(client):
    channel,connection,_=queued(client);action=prepare_outbound(channel['id'],connection);receipt(channel,connection,action)
    row=contacts(client)[0]
    with SessionLocal.begin() as db:
        from lingxi.models import Conversation
        response=pro.chat_control(db,db.get(User,'owner'),db.get(Conversation,row['conversation_id']),'这个以后别问')
        assert '不再主动提起' in response
    assert contacts(client)[0]['feedback']=='stop_topic'


def test_reviewed_reply_unknown_does_not_block_proactive_forever(client):
    channel,connection,_=queued(client)
    with SessionLocal.begin() as db:
        incoming=ChannelInbound(user_id='owner',channel_id=channel['id'],channel_version=channel['version'],
            external_message_id='99',target_key='private:87654321',sender_id='87654321',content='',status='unknown')
        db.add(incoming);db.flush()
        db.add(ChannelDelivery(user_id='owner',channel_id=channel['id'],inbound_id=incoming.id,part=0,
            content='',status='unknown',echo='old',reviewed_at=now()))
    assert isinstance(prepare_outbound(channel['id'],connection),dict)


def test_pending_content_over_budget_not_truncated(client):
    channel,connection,contact=queued(client)
    with SessionLocal.begin() as db:db.get(pro.ProactiveContact,contact['id']).content='长'*1501
    assert prepare_outbound(channel['id'],connection) is None
    assert contacts(client)[0]['delivery']['status']=='cancelled'


@pytest.mark.parametrize('cause',['timeout','disconnect','failed'])
def test_unknown_no_resend_review_is_not_retry(client,cause):
    channel,connection,contact=queued(client);action=prepare_outbound(channel['id'],connection)
    if cause=='timeout':
        with SessionLocal.begin() as db:db.scalar(select(pc.ProactiveDelivery)).sent_at=now()-31
        prepare_outbound(channel['id'],connection)
    if cause=='disconnect':
        close_connection(channel['id'],connection);connection=connect(channel['id'])
    if cause=='failed':receipt(channel,connection,action,retcode=100)
    row=contacts(client)[0];assert row['delivery']['status']==('failed' if cause=='failed' else 'unknown')
    for _ in range(3):assert prepare_outbound(channel['id'],connection) is None
    path='/api/proactive-contacts/'+row['id']+'/delivery-review'
    assert client.post(path,json={'version':row['version'],'acknowledgement':'retry'}).status_code==422
    assert client.post(path,json={'version':row['version'],'acknowledgement':'checked_no_resend'}).status_code==200
    assert prepare_outbound(channel['id'],connection) is None


def test_late_acceptance_after_source_deletion_records_receipt_only(client):
    channel,connection,contact=queued(client);action=prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:identity=db.get(pro.ConversationTopic,contact['topic_id']).source_id
    client.delete('/api/memories/'+identity)
    receipt(channel,connection,action)
    row=contacts(client)[0]
    assert row['delivery']['status']=='accepted_by_protocol'
    assert not row['content'] and not row['message_id']


def test_quota_uses_actual_send_timestamp_and_reservations(client):
    channel,connection,contact=queued(client)
    row=topic(client);pro.deliver(row['id']);assert len(contacts(client))==1
    with SessionLocal.begin() as db:
        db.get(pro.ProactiveContact,contact['id']).created_at=now()-86400
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        times=pc.quota_times(db,db.get(User,'owner'),sending=True)
        assert len(times)==1 and abs(times[0]-now())<5


def test_restore_preserves_send_barrier_without_old_content(client):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    channel,connection,contact=queued(client);prepare_outbound(channel['id'],connection)
    with SessionLocal.begin() as db:schema(db)
    data=checkpoint();assert not data['proactive_contacts'][0]['content']
    with SessionLocal.begin() as db:
        db.execute(delete(pc.ProactiveDelivery));db.execute(delete(pro.ProactiveContact))
        row=db.get(pro.ConversationTopic,contact['topic_id']);row.status='pending'
    quarantine(data)
    with SessionLocal() as db:
        assert db.scalar(select(pc.ProactiveDelivery)).status=='unknown'
        assert db.get(pro.ProactiveContact,contact['id']).content==''
        assert pc.target_for(db,db.get(User,'owner'))['kind']=='web'
        assert not pro.pref_view(db,db.get(User,'owner'))['enabled']
    pro.deliver(contact['topic_id'])
    with SessionLocal() as db:assert len(list(db.scalars(select(pc.ProactiveDelivery))))==1


@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='PostgreSQL row locks')
def test_concurrent_dispatch_sends_once(client):
    channel,connection,_=queued(client)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:prepare_outbound(channel['id'],connection),range(2)))
    assert sum(isinstance(row,dict) for row in results)==1
