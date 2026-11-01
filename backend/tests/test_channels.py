import hashlib
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import User, Conversation, ChatRun, Message, Memory, Job, Candidate
from lingxi.channels import Channel, IdentityLink, ChannelInbound, ChannelDelivery, ChannelSession
from lingxi.channel_gateway import open_connection, receive_event, prepare_outbound, close_connection
from lingxi import worker

TOKEN = 'channel-test-only-token-123456789012345678'


def create(client, **extra):
    result = client.post('/api/channels', json={'name':'测试 QQ','bot_id':'12345678','access_token':TOKEN,'enabled':True,**extra})
    assert result.status_code == 200, result.text
    return result.json()


def connect(identity):
    connection = open_connection(identity, TOKEN, '12345678')
    assert connection
    receive_event(identity, connection, {'echo':connection+':status','retcode':0,'data':{'online':True,'good':True}})
    return connection


def event(message='你好', user='87654321', message_id=1, group=None, mention=False):
    return {'self_id':12345678,'post_type':'message','message_type':'group' if group else 'private',
        'message_id':message_id,'user_id':int(user),**({'group_id':int(group)} if group else {}),
        'message':([{'type':'at','data':{'qq':'12345678'}}] if mention else [])+[{'type':'text','data':{'text':message}}]}


def rules(client, channel, **extra):
    result=client.put('/api/channels/'+channel['id']+'/rules',json={**channel['rules'],**extra,'version':channel['version']})
    assert result.status_code == 200, result.text
    return result.json()


def bind(client, channel, connection, memory=False):
    response=client.post('/api/identity-links',json={'channel_id':channel['id'],'external_user_id':'87654321','allow_memory':memory})
    assert response.status_code == 200, response.text
    request=response.json()
    receive_event(channel['id'],connection,event(request['command']))
    row=client.get('/api/identity-links').json()[0]
    assert row['status']=='proved'
    return client.post('/api/identity-links/'+row['id']+'/confirm',json={'version':row['version']}).json()


def test_channel_auth_version_and_encrypted_token(client):
    row=create(client)
    assert TOKEN not in json.dumps(row)
    assert row['connection_state']=='offline' and row['login_state']=='unknown'
    with SessionLocal() as db:
        assert db.get(Channel,row['id']).encrypted_token != TOKEN
    result=client.put('/api/channels/'+row['id'],json={'name':'新名称','bot_id':row['bot_id'],'version':99})
    assert result.status_code==409
    assert client.get('/api/channels/foreign/messages').status_code==404
    client.headers['X-CSRF-Token']='bad'
    assert client.post('/api/channels',json={'name':'x','bot_id':'12345','access_token':TOKEN}).status_code==403


def test_connection_auth_duplicate_and_expired_lease(client):
    row=create(client)
    assert not open_connection(row['id'],'wrong','12345678')
    assert not open_connection(row['id'],TOKEN,'99999999')
    connection=connect(row['id'])
    assert not open_connection(row['id'],TOKEN,'12345678')
    with SessionLocal.begin() as db:db.get(Channel,row['id']).lease_until=now()-1
    replacement=connect(row['id'])
    close_connection(row['id'],connection)
    assert client.get('/api/channels').json()[0]['connection_state']=='connected'
    close_connection(row['id'],replacement)
    assert client.get('/api/channels').json()[0]['connection_state']=='offline'


@pytest.mark.parametrize('headers', [{},{'Authorization':'Bearer wrong','X-Self-ID':'12345678'},
    {'Authorization':'Bearer '+TOKEN,'X-Self-ID':'12345678','Origin':'http://testserver'}])
def test_ws_rejects_cookie_wrong_token_and_browser(client,headers):
    channel=create(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/gateway/onebot/v11/ws/'+channel['id'],headers=headers):pass


def test_ws_status_and_heartbeat_real_endpoint(client):
    channel=create(client)
    with client.websocket_connect('/gateway/onebot/v11/ws/'+channel['id'],headers={'Authorization':'Bearer '+TOKEN,'X-Self-ID':'12345678'}) as ws:
        status=ws.receive_json()
        assert status['action']=='get_status'
        assert ws.receive_json()['action']=='get_version_info'
        ws.send_json({'echo':status['echo'],'retcode':0,'data':{'online':False}})
        ws.send_json({'self_id':12345678,'post_type':'meta_event','meta_event_type':'heartbeat','status':{'online':True,'good':True}})
        # A status read can race the receive task; the database assertion is checked after close.
    assert client.get('/api/channels').json()[0]['connection_state']=='offline'


def test_default_discards_strangers_untriggered_groups_and_self(client):
    row=create(client);connection=connect(row['id'])
    for e in [event(), event(group='12345'), event(user='12345678'),event(user='33333333',group='12345',mention=True)]:
        receive_event(row['id'],connection,e)
    with SessionLocal() as db:assert not db.scalar(select(ChannelInbound))


def test_rules_require_explicit_group_allowlist_and_trigger(client):
    row=rules(client,create(client),group_enabled=True,group_ids=['12345'])
    connection=connect(row['id'])
    for e in [event(group='12345'),event(group='99999',mention=True),event('/灵犀伪命令',group='12345')]:receive_event(row['id'],connection,e)
    receive_event(row['id'],connection,event('/灵犀 帮我整理',group='12345',message_id=2))
    with SessionLocal() as db:
        items=list(db.scalars(select(ChannelInbound)))
        assert len(items)==1 and items[0].content=='帮我整理'


def test_dedup_account_validation_and_rate_limit(client):
    row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    receive_event(row['id'],connection,event())
    receive_event(row['id'],connection,event())
    receive_event(row['id'],connection,{**event(message_id=2),'self_id':99999999})
    for i in range(2,20):receive_event(row['id'],connection,event(message_id=i))
    with SessionLocal() as db:assert len(list(db.scalars(select(ChannelInbound))))==10


def test_identity_requires_expected_private_sender_and_web_confirmation(client):
    row=create(client);connection=connect(row['id'])
    request=client.post('/api/identity-links',json={'channel_id':row['id'],'external_user_id':'87654321','allow_memory':True}).json()
    assert request['command'] not in client.get('/api/identity-links').text
    receive_event(row['id'],connection,event(request['command'],user='33333333'))
    link=client.get('/api/identity-links').json()[0]
    assert link['status']=='pending'
    assert client.post('/api/identity-links/'+link['id']+'/confirm',json={'version':link['version']}).status_code==409
    receive_event(row['id'],connection,event(request['command']))
    link=client.get('/api/identity-links').json()[0]
    assert link['status']=='proved'
    with SessionLocal() as db:assert not db.scalar(select(ChannelInbound))
    assert client.post('/api/identity-links/'+link['id']+'/confirm',json={'version':link['version']}).json()['status']=='active'


def test_expired_identity_challenge_does_not_bind(client):
    row=create(client);connection=connect(row['id'])
    request=client.post('/api/identity-links',json={'channel_id':row['id'],'external_user_id':'87654321'}).json()
    with SessionLocal.begin() as db:db.get(IdentityLink,request['link']['id']).expires_at=now()-1
    receive_event(row['id'],connection,event(request['command']))
    assert client.get('/api/identity-links').json()[0]['status']=='pending'


def test_bound_no_memory_private_is_isolated(configured,monkeypatch):
    client=configured;row=create(client);connection=connect(row['id']);bind(client,row,connection)
    receive_event(row['id'],connection,event('请记住我喜欢蓝色'))
    assert prepare_outbound(row['id'],connection) is None
    with SessionLocal() as db:
        run=db.scalar(select(ChatRun));assert run.user_id!='owner'
        assert run.config['channel']['memory_allowed'] is False
        assert not db.scalar(select(Job).where(Job.kind=='extract'))
    monkeypatch.setattr(worker.MemoryService,'search',lambda *a:pytest.fail('isolated channel must not search private memory'))
    monkeypatch.setattr(worker,'stream',lambda *a,**k:iter(['这是隔离的回答']))
    worker.perform(worker.claim())
    output=prepare_outbound(row['id'],connection)
    assert output['action']=='send_private_msg'
    assert output['params']['message'][0]['type']=='text'
    assert output['params']['message'][0]['data']['text']=='这是隔离的回答'
    receive_event(row['id'],connection,{'echo':output['echo'],'retcode':0,'data':{'message_id':-123}})
    records=client.get('/api/channels/'+row['id']+'/messages').json()
    assert records[0]['status']=='completed'
    assert records[0]['deliveries'][0]['status']=='accepted_by_protocol'
    assert 'content' not in records[0]


def test_bound_owner_memory_does_not_merge_web_history(configured):
    client=configured;row=create(client);connection=connect(row['id']);bind(client,row,connection,True)
    original=client.post('/api/conversations',json={'title':'私人 Web'}).json()
    receive_event(row['id'],connection,event())
    prepare_outbound(row['id'],connection)
    with SessionLocal() as db:
        run=db.scalar(select(ChatRun));assert run.user_id=='owner'
        assert run.config['channel']['memory_allowed'] is True
        assert run.conversation_id!=original['id']
        assert db.scalar(select(Job).where(Job.kind=='extract'))


def test_bound_group_cannot_inherit_owner_memory_or_support(configured):
    client=configured;row=rules(client,create(client),group_enabled=True,group_ids=['12345'])
    connection=connect(row['id']);bind(client,row,connection,True)
    receive_event(row['id'],connection,event(group='12345',mention=True))
    prepare_outbound(row['id'],connection)
    with SessionLocal() as db:
        run=db.scalar(select(ChatRun));assert run.user_id!='owner'
        assert run.config['channel']['memory_allowed'] is False
        assert run.config['channel']['sender_id']=='87654321'


def test_same_target_ordered_distinct_targets_can_queue(configured):
    client=configured;row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    for e in [event(message_id=1),event('第二条',message_id=2),event(user='33333333')]:receive_event(row['id'],connection,e)
    prepare_outbound(row['id'],connection)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChatRun))))==2
        assert len(list(db.scalars(select(ChannelInbound).where(ChannelInbound.status=='queued'))))==1


def test_revoke_invalidates_queued_generation_and_stops_extract(configured,monkeypatch):
    client=configured;row=create(client);connection=connect(row['id']);link=bind(client,row,connection,True)
    receive_event(row['id'],connection,event('我喜欢蓝色'))
    prepare_outbound(row['id'],connection)
    result=client.post('/api/identity-links/'+link['id']+'/revoke',json={'version':link['version']})
    assert result.status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(ChatRun)).status=='invalidated'
        assert db.scalar(select(ChannelInbound)).status=='cancelled'
        extraction=db.scalar(select(Job).where(Job.kind=='extract'))
    monkeypatch.setattr(worker,'complete',lambda *a,**k:pytest.fail('revoked source cannot extract'))
    worker.extract(extraction)
    assert prepare_outbound(row['id'],connection) is None


def test_unknown_send_not_repeated_after_disconnect(client):
    row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    e=event();e['message'].append({'type':'image','data':{'file':'/etc/passwd'}})
    receive_event(row['id'],connection,e)
    action=prepare_outbound(row['id'],connection)
    assert action and action['params']['message'][0]['type']=='text'
    close_connection(row['id'],connection)
    replacement=connect(row['id'])
    assert prepare_outbound(row['id'],replacement) is None
    records=client.get('/api/channels/'+row['id']+'/messages').json()
    assert records[0]['deliveries'][0]['status']=='unknown'
    assert records[0]['deliveries'][0]['protocol_message_id'] is None


def test_disabled_instance_disconnects_and_stops_new_messages(client):
    row=create(client);connection=connect(row['id'])
    response=client.put('/api/channels/'+row['id'],json={'name':row['name'],'bot_id':row['bot_id'],'enabled':False,'version':row['version']})
    assert response.status_code==200
    assert prepare_outbound(row['id'],connection) is False
    assert not open_connection(row['id'],TOKEN,row['bot_id'])


def test_login_invalid_never_accepts_messages(client):
    row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    receive_event(row['id'],connection,{'self_id':12345678,'post_type':'meta_event','meta_event_type':'heartbeat','status':{'online':False}})
    receive_event(row['id'],connection,event())
    with SessionLocal() as db:assert not db.scalar(select(ChannelInbound))
    assert client.get('/api/channels').json()[0]['login_state']=='login_required'


def test_token_rotation_invalidates_old_connection(client):
    row=create(client);connection=connect(row['id'])
    result=client.put('/api/channels/'+row['id'],json={'name':row['name'],'bot_id':row['bot_id'],'enabled':True,'access_token':'another-secret-token-at-least-thirty-two-characters','version':row['version']})
    assert result.status_code==200
    assert prepare_outbound(row['id'],connection) is False
    assert not open_connection(row['id'],TOKEN,row['bot_id'])


def test_manual_unknown_review_unblocks_target_without_resend(configured):
    client=configured;row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    first=event();first['message'].append({'type':'image','data':{'file':'remote'}})
    receive_event(row['id'],connection,first)
    action=prepare_outbound(row['id'],connection);assert action
    close_connection(row['id'],connection);connection=connect(row['id'])
    receive_event(row['id'],connection,event('下一条文字',message_id=2))
    prepare_outbound(row['id'],connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        delivery=db.scalar(select(ChannelDelivery))
        identity=delivery.id
    assert client.post('/api/channels/'+row['id']+'/deliveries/'+identity+'/review',json={'acknowledgement':'checked_no_resend'}).status_code==200
    assert prepare_outbound(row['id'],connection) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChatRun))
        assert db.get(ChannelDelivery,identity).status=='unknown'
        assert db.get(ChannelDelivery,identity).reviewed_at


def test_late_protocol_ack_can_resolve_unknown_with_real_evidence(client):
    row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    e=event();e['message'].append({'type':'file','data':{'file':'/secret'}})
    receive_event(row['id'],connection,e);action=prepare_outbound(row['id'],connection)
    with SessionLocal.begin() as db:db.scalar(select(ChannelDelivery)).sent_at=now()-31
    assert prepare_outbound(row['id'],connection) is None
    receive_event(row['id'],connection,{'echo':action['echo'],'retcode':0,'data':{'message_id':12345}})
    with SessionLocal() as db:
        assert db.scalar(select(ChannelInbound)).status=='failed'  # The receipt confirms the explanatory reply, not file processing.
        assert db.scalar(select(ChannelDelivery)).status=='accepted_by_protocol'


def test_deletion_redacts_unsent_channel_derivatives(configured,monkeypatch):
    client=configured;row=create(client);connection=connect(row['id']);bind(client,row,connection,True)
    memory=client.post('/api/memories',json={'content':'我喜欢蓝色','category':'preferences'}).json()
    receive_event(row['id'],connection,event('我的偏好？'))
    prepare_outbound(row['id'],connection)
    with SessionLocal.begin() as db:
        incoming=db.scalar(select(ChannelInbound));run=db.get(ChatRun,incoming.run_id)
        run.status='completed';run.memory_refs=[{'id':memory['id'],'version':1}]
        db.add(Message(conversation_id=run.conversation_id,role='assistant',run_id=run.id,content='你喜欢蓝色'))
        incoming.status='running'
        db.get(Channel,row['id']).last_sent=now()+100
    assert prepare_outbound(row['id'],connection) is None
    with SessionLocal() as db:assert db.scalar(select(ChannelDelivery)).content=='你喜欢蓝色'
    assert client.delete('/api/memories/'+memory['id']).status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(ChannelDelivery)).content==''
        assert db.scalar(select(ChannelDelivery)).status=='cancelled'


def test_inflight_extraction_rechecks_revoked_identity(configured,monkeypatch):
    client=configured;row=create(client);connection=connect(row['id']);link=bind(client,row,connection,True)
    receive_event(row['id'],connection,event('我喜欢蓝色'))
    prepare_outbound(row['id'],connection)
    with SessionLocal() as db:job=db.scalar(select(Job).where(Job.kind=='extract'))
    monkeypatch.setattr(worker,'owns',lambda *args:None)
    def output(*args,**kwargs):
        assert client.post('/api/identity-links/'+link['id']+'/revoke',json={'version':link['version']}).status_code==200
        return json.dumps({'items':[{'content':'我喜欢蓝色','quote':'我喜欢蓝色','subject':'self','category':'preferences','evidence':'explicit','sensitive':False}]})
    monkeypatch.setattr(worker,'complete',output)
    worker.extract(job)
    with SessionLocal() as db:assert not db.scalar(select(Candidate))


def test_expired_pending_message_not_replayed_after_reconnect(configured):
    client=configured;row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    receive_event(row['id'],connection,event())
    with SessionLocal.begin() as db:db.scalar(select(ChannelInbound)).created_at=now()-3601
    close_connection(row['id'],connection);connection=connect(row['id'])
    assert prepare_outbound(row['id'],connection) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChannelInbound)).status=='cancelled'
        assert not db.scalar(select(ChatRun))


def test_negative_external_message_ids_are_deduplicated(client):
    row=rules(client,create(client),allow_unbound=True);connection=connect(row['id'])
    receive_event(row['id'],connection,event(message_id=-12))
    receive_event(row['id'],connection,event(message_id=-12))
    with SessionLocal() as db:assert len(list(db.scalars(select(ChannelInbound))))==1


def test_real_foreign_owner_cannot_read_or_change_channel(client):
    from fastapi.testclient import TestClient
    from lingxi.security import hasher
    channel=create(client)
    with SessionLocal.begin() as db:
        db.add(User(id='other-owner',username='other-owner',workspace_id='another-workspace',password_hash=hasher.hash('other-test-password')))
    with TestClient(app) as other:
        auth=other.post('/api/auth/login',json={'username':'other-owner','password':'other-test-password'}).json()
        other.headers['X-CSRF-Token']=auth['csrf']
        assert other.get('/api/channels').json()==[]
        assert other.get('/api/channels/'+channel['id']+'/messages').status_code==404
        assert other.put('/api/channels/'+channel['id'],json={'name':'intrusion','bot_id':channel['bot_id'],'version':1}).status_code==404
        assert other.post('/api/identity-links',json={'channel_id':channel['id'],'external_user_id':'87654321'}).status_code==404


def test_postgres_two_cores_cannot_own_same_connection(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row-lock concurrency assertion')
    channel=create(client);start=Barrier(2)
    def attempt():
        start.wait(timeout=5)
        return open_connection(channel['id'],TOKEN,channel['bot_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:attempt(),range(2)))
    assert sum(r is not None for r in results)==1
