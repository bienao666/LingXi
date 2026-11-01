"""QQ collaboration on persisted scopes, synthetic providers and protocol receipts."""
import json
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import User, Job, Message, ChatRun, Conversation, Provider
from lingxi.channels import ChannelInbound, ChannelDelivery, Channel
from lingxi.channel_tasks import ChannelTask, reconcile, command, require_origin
from lingxi.delegation import AgentRun, AgentNode
from lingxi import worker
from test_channels import create, rules, connect, event, bind
from test_delegation import fixture_model, drain
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection


def request(client, group=False, memory=False):
    row=create(client)
    row=rules(client,row,allow_unbound=True,group_enabled=group,group_ids=['12345'] if group else [])
    connection=connect(row['id'])
    if memory:
        bind(client,row,connection,memory=True)
    receive_event(row['id'],connection,event('/协作 整理项目建议',group='12345' if group else None,mention=group))
    ack=prepare_outbound(row['id'],connection)
    assert ack and '尚未调用模型' in json.dumps(ack,ensure_ascii=False)
    receipt(row,connection,ack)
    tasks=client.get('/api/channel-tasks',params={'channel_id':row['id']}).json()
    assert len(tasks)==1, tasks
    return row,connection,tasks[0]


def preview(client,task,**extra):
    agents=client.get('/api/agents').json()
    result=client.post('/api/channel-tasks/'+task['id']+'/preview',json={
        'version':task['version'],'agent_ids':[agents[0]['id']],**extra})
    assert result.status_code==200,result.text
    return result.json()


def confirm(client,task,p):
    response=client.post('/api/channel-tasks/'+task['id']+'/confirm',json={'version':p['version'],'digest':p['digest']})
    assert response.status_code==200,response.text
    return response.json()


def receipt(row,connection,action):
    receive_event(row['id'],connection,{'echo':action['echo'],'retcode':0,'data':{'message_id':555}})
    with SessionLocal.begin() as db:
        db.get(Channel,row['id']).last_sent=0


@pytest.mark.parametrize('text,expected', [('/协作 x','x'),('/协作\nx','x'),('/协作',''),('帮我/协作 x',None),('“/协作 x”',None),('/协作：x',None)])
def test_exact_command(text,expected):
    assert command(text)==expected


@pytest.mark.parametrize('group',[False,True])
def test_request_needs_web_confirmation_without_model(client,group):
    row,c,task=request(client,group)
    with SessionLocal() as db:
        assert not db.scalar(select(AgentRun))
        assert not db.scalar(select(Job).where(Job.kind.in_(['chat','agent_plan','extract'])))
        assert db.get(ChannelTask,task['id']).actor_id!='owner'
    assert prepare_outbound(row['id'],c) is None


def test_preview_and_confirmation_idempotency(configured):
    row,c,task=request(configured)
    p=preview(configured,task)
    with SessionLocal() as db:
        assert not db.scalar(select(AgentRun))
    result=confirm(configured,task,p)
    duplicate=confirm(configured,task,p)
    assert result['run_id']==duplicate['run_id']
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.user_id=='owner' and run.conversation_id is None
        assert run.config['share_memory'] is False
        assert len(run.config['agents'])==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='agent_plan'))==1


@pytest.mark.parametrize('group',[False,True])
def test_full_result_original_receiver_no_memory(configured,monkeypatch,group):
    _,calls=fixture_model(configured,monkeypatch)
    row,c,task=request(configured,group)
    monkeypatch.setattr(worker.MemoryService,'search',lambda *a,**k:pytest.fail('QQ isolated task must not read owner memory'))
    result=confirm(configured,task,preview(configured,task))
    drain();reconcile();reconcile()
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.status=='completed',run.error
        assert run.calls_used==3 and len(calls)==3
        t=db.get(ChannelTask,task['id'])
        assert len(t.notifications)==1
        assert not run.result_message_id
    action=prepare_outbound(row['id'],c)
    assert action['action']==('send_group_msg' if group else 'send_private_msg')
    assert action['params']['group_id' if group else 'user_id']==(12345 if group else 87654321)
    assert '整理了评审建议' in json.dumps(action,ensure_ascii=False)
    receipt(row,c,action)
    assert prepare_outbound(row['id'],c) is None


@pytest.mark.parametrize('group',[False,True])
def test_unbound_or_group_memory_cannot_be_granted(configured,group):
    _,_,task=request(configured,group)
    agents=configured.get('/api/agents').json()
    result=configured.post('/api/channel-tasks/'+task['id']+'/preview',json={
        'version':task['version'],'agent_ids':[agents[0]['id']],'share_memory':True})
    assert result.status_code==403


def test_bound_private_memory_is_separate_explicit_selection(configured):
    _,_,task=request(configured,memory=True)
    p=preview(configured,task,share_memory=True)
    result=confirm(configured,task,p)
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.config['share_memory'] is True
        assert require_origin(db,run).actor_id=='owner'


@pytest.mark.parametrize('mutation',['expired','provider','profile','source','channel','preview','session'])
def test_changed_preview_cannot_authorize(configured,mutation):
    _,_,task=request(configured)
    p=preview(configured,task)
    with SessionLocal.begin() as db:
        t=db.get(ChannelTask,task['id'])
        if mutation=='expired':t.preview=t.preview|{'expires_at':now()-1}
        if mutation=='provider':db.get(Provider,'provider').version+=1
        if mutation=='profile':
            from lingxi.assistants import AssistantProfile
            db.get(AssistantProfile,p['plan']['agents'][0]['persona']['id']).version+=1
        if mutation=='source':db.get(Message,t.source_id).content='changed'
        if mutation=='channel':db.get(Channel,t.channel_id).version+=1
        if mutation=='preview':t.preview=t.preview|{'digest':'0'*64}
        if mutation=='session':t.preview=t.preview|{'login_id':'other'}
    response=configured.post('/api/channel-tasks/'+task['id']+'/confirm',json={'version':p['version'],'digest':p['digest']})
    assert response.status_code in (409,410)
    with SessionLocal() as db:assert not db.scalar(select(AgentRun))


def test_duplicate_message_never_duplicates_request(client):
    row,c,t=request(client,True)
    receive_event(row['id'],c,event('/协作 整理项目建议',group='12345',mention=True))
    prepare_outbound(row['id'],c)
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(ChannelTask))==1


@pytest.mark.parametrize('mutation',['source','channel','expiry','cancel','persona'])
def test_inflight_revocation_blocks_result(configured,monkeypatch,mutation):
    fixture_model(configured,monkeypatch)
    row,c,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t))
    worker.perform(worker.claim())
    with SessionLocal.begin() as db:
        task=db.get(ChannelTask,t['id'])
        if mutation=='source':
            from lingxi.memory import delete_conversation
            delete_conversation(db,db.get(Conversation,task.conversation_id),False)
        if mutation=='channel':
            from lingxi.channels import invalidate_channel_runs
            db.get(Channel,row['id']).version+=1
            invalidate_channel_runs(db,row['id'])
        if mutation=='expiry':task.expires_at=now()-1
        if mutation=='cancel':
            from lingxi.channel_tasks import discard
            discard(db,task,'拒绝',status='cancelled')
        if mutation=='persona':
            from lingxi.assistants import AssistantProfile
            run=db.get(AgentRun,result['run_id'])
            db.get(AssistantProfile,run.config['agents'][0]['persona']['id']).version+=1
    drain();reconcile()
    with SessionLocal() as db:
        assert db.get(AgentRun,result['run_id']).status in ('invalidated','failed')
        assert not db.get(ChannelTask,t['id']).notifications


def test_delete_after_summary_before_delivery_redacts_result(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    row,c,t=request(configured,True)
    confirm(configured,t,preview(configured,t));drain();reconcile()
    with SessionLocal.begin() as db:
        task=db.get(ChannelTask,t['id'])
        from lingxi.memory import delete_conversation
        delete_conversation(db,db.get(Conversation,task.conversation_id),False)
    assert prepare_outbound(row['id'],c) is None
    with SessionLocal() as db:
        task=db.get(ChannelTask,t['id'])
        assert not task.goal
        assert db.get(AgentRun,task.run_id).result=={}
        assert not any(d.content for d in db.scalars(select(ChannelDelivery)))


def test_unknown_result_not_resent_on_reconnect(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    row,c,t=request(configured)
    confirm(configured,t,preview(configured,t));drain();reconcile()
    action=prepare_outbound(row['id'],c);assert action
    close_connection(row['id'],c);c=connect(row['id'])
    reconcile();assert prepare_outbound(row['id'],c) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChannelDelivery).where(ChannelDelivery.echo==action['echo'])).status=='unknown'


def test_auth_csrf_and_foreign_id(configured):
    row,c,t=request(configured)
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/channel-tasks',params={'channel_id':row['id']}).status_code==401
    assert configured.get('/api/channel-tasks?channel_id=missing').status_code==404
    agents=configured.get('/api/agents').json()
    configured.headers.pop('X-CSRF-Token')
    assert configured.post('/api/channel-tasks/'+t['id']+'/preview',json={
        'version':t['version'],'agent_ids':[agents[0]['id']]}).status_code==403


def test_reject_is_versioned_and_never_executes(configured):
    _,_,t=request(configured)
    assert configured.post('/api/channel-tasks/'+t['id']+'/cancel',json={'version':99}).status_code==409
    assert configured.post('/api/channel-tasks/'+t['id']+'/cancel',json={'version':t['version']}).json()['status']=='rejected'
    with SessionLocal() as db:assert not db.scalar(select(Job).where(Job.kind=='agent_plan'))


def test_foreign_principal_cannot_list_or_approve(configured):
    row,c,t=request(configured)
    with SessionLocal.begin() as db:
        channel=db.get(Channel,row['id']);channel.workspace_id='other'
    assert configured.get('/api/channel-tasks',params={'channel_id':row['id']}).status_code==404
    assert configured.post('/api/channel-tasks/'+t['id']+'/cancel',json={'version':t['version']}).status_code==404


@pytest.mark.parametrize('adapter',['api','mcp'])
def test_remote_tool_still_needs_web_per_call_confirmation(configured,monkeypatch,adapter):
    from test_agent_tools import fixture as tool_fixture,preview as tool_preview,confirm as tool_confirm
    from lingxi.agent_tools import reconcile as tools_reconcile
    from lingxi.plugins import PluginInvocation
    tool,_,remote,_=tool_fixture(configured,monkeypatch,adapter)
    row,c,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t,tool_keys=[tool['key']]))
    drain();reconcile();reconcile()
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.status=='running'
        assert len(db.get(ChannelTask,t['id']).notifications)==1
        assert not db.scalar(select(PluginInvocation))
    p=tool_preview(configured,{'id':result['run_id']})
    assert p.status_code==200,p.text
    p=p.json()
    assert p['plan']['qq_audience']=='group:12345' and '全群可见' in p['plan']['audience_notice']
    again=tool_preview(configured,{'id':result['run_id']})
    assert again.json()['digest']==p['digest']
    assert tool_confirm(configured,p).status_code==200
    assert tool_confirm(configured,p).status_code==200
    drain();tools_reconcile();drain();reconcile()
    with SessionLocal() as db:
        assert db.get(AgentRun,result['run_id']).status=='completed'
        assert len(db.get(ChannelTask,t['id']).notifications)==2
    if adapter=='api':assert len(remote)==1
    else:assert len([r for r in remote.calls if r.get('method')=='tools/call'])==1


def test_source_revoked_after_tool_preview_cannot_confirm(configured,monkeypatch):
    from test_agent_tools import fixture as tool_fixture,preview as tool_preview,confirm as tool_confirm
    tool,_,remote,_=tool_fixture(configured,monkeypatch)
    _,_,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t,tool_keys=[tool['key']]))
    drain();p=tool_preview(configured,{'id':result['run_id']}).json()
    with SessionLocal.begin() as db:
        task=db.get(ChannelTask,t['id'])
        from lingxi.channel_tasks import discard
        discard(db,task,'撤销')
    assert tool_confirm(configured,p).status_code==409
    drain();assert not remote


@pytest.mark.parametrize('path',['.','..','../private','/etc','share/../../private','C:\\private'])
def test_cannot_share_root_or_escape_subtree(configured,path):
    _,_,t=request(configured,True)
    agents=configured.get('/api/agents').json()
    r=configured.post('/api/channel-tasks/'+t['id']+'/preview',json={
        'version':t['version'],'agent_ids':[agents[0]['id']],'resource_path':path})
    assert r.status_code==422


@pytest.mark.skipif(__import__('os').name!='posix',reason='requires Linux openat resource guard')
def test_file_nodes_are_scoped_to_shared_subtree(configured,monkeypatch,tmp_path):
    from lingxi.config import settings
    from lingxi.assistants import AssistantProfile
    monkeypatch.setattr(settings(),'resource_dir',tmp_path)
    (tmp_path/'public').mkdir();(tmp_path/'public'/'visible.md').write_text('public')
    (tmp_path/'secret.md').write_text('private')
    from test_resources import grant
    grant(configured, ['public'])
    plan,calls=fixture_model(configured,monkeypatch)
    with SessionLocal.begin() as db:
        for role in db.scalars(select(AssistantProfile).where(AssistantProfile.kind=='role')):
            role.spec=role.spec|{'tools':['server.files']};role.version+=1
    plan['nodes'][0]['capability']='server.files'
    _,_,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t,resource_path='public'))
    drain()
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.status=='completed',run.error
        node=db.scalar(select(AgentNode).where(AgentNode.run_id==run.id))
        assert node.result['path']=='.'
        assert [e['name'] for e in node.result['entries']]==['visible.md']
    grant(configured, [])
    with SessionLocal() as db:
        from lingxi.channel_tasks import ChannelTask, delivery_valid
        t=db.get(ChannelTask,t['id'])
        assert t.status=='invalidated' and t.preview=={}
        run=db.get(AgentRun,result['run_id'])
        assert run.status=='invalidated' and run.result=={}
        node=db.scalar(select(AgentNode).where(AgentNode.run_id==run.id))
        assert node.context=={} and node.result=={}
        from lingxi.models import ChatRun
        for chat in db.scalars(select(ChatRun).where(ChatRun.conversation_id==t.conversation_id)):
            if chat.config.get('qq_task',{}).get('id')==t.id:
                assert not delivery_valid(db,chat)


def test_group_attachment_uses_source_actor_not_owner(configured,monkeypatch):
    from lingxi.channel_tasks import receive
    from lingxi.channel_gateway import actor_conversation
    from lingxi.media import Media
    from test_agent_media import attachment,protocol
    item=attachment(configured)
    row=create(configured);row=rules(configured,row,allow_unbound=True,group_enabled=True,group_ids=['12345'])
    c=connect(row['id'])
    with SessionLocal.begin() as db:
        incoming=ChannelInbound(user_id='owner',workspace_id='personal',channel_id=row['id'],channel_version=row['version'],
            external_message_id='1',target_key='group:12345',sender_id='87654321',content='/协作 分析本条资料',status='queued')
        db.add(incoming);db.flush()
        actor,conversation,share=actor_conversation(db,db.get(Channel,row['id']),incoming)
        db.get(Media,item['id']).user_id=actor.id
        receive(db,db.get(Channel,row['id']),incoming,actor,conversation,share,[item['id']],False)
    t=configured.get('/api/channel-tasks',params={'channel_id':row['id']}).json()[0]
    calls=protocol(configured,monkeypatch,[item])
    result=confirm(configured,t,preview(configured,t));drain();reconcile()
    with SessionLocal() as db:
        run=db.get(AgentRun,result['run_id'])
        assert run.status=='completed',run.error
        assert run.config['media_actor_id']!='owner'
    assert '项目预算' in json.dumps(next(call for call in calls if call[0]=='node')[3],ensure_ascii=False)
    with SessionLocal.begin() as db:
        from lingxi.media import forget_media
        forget_media(db,db.get(Media,item['id']))
    reconcile()
    with SessionLocal() as db:
        assert db.get(AgentRun,result['run_id']).status=='invalidated'
        assert not db.get(AgentRun,result['run_id']).result


def test_unconfirmed_voice_cannot_submit_task(configured):
    from lingxi.channel_tasks import receive
    from lingxi.channel_gateway import actor_conversation
    row=create(configured)
    with SessionLocal.begin() as db:
        incoming=ChannelInbound(user_id='owner',workspace_id='personal',channel_id=row['id'],channel_version=row['version'],
            external_message_id='1',target_key='group:12345',sender_id='87654321',content='/协作 转写混合授权',status='queued')
        db.add(incoming);db.flush()
        actor,conversation,share=actor_conversation(db,db.get(Channel,row['id']),incoming)
        assert receive(db,db.get(Channel,row['id']),incoming,actor,conversation,share,[],True)
        assert not db.scalar(select(ChannelTask))


def test_restore_quarantines_qq_tasks(configured,monkeypatch):
    from lingxi import backups
    fixture_model(configured,monkeypatch)
    _,_,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t))
    from test_backups import schema
    with SessionLocal.begin() as db:
        schema(db)
    snapshot=backups.checkpoint()
    backups.quarantine(snapshot)
    with SessionLocal() as db:
        task=db.get(ChannelTask,t['id'])
        assert task.status=='invalidated' and not task.preview
        assert db.get(AgentRun,result['run_id']).status=='invalidated'


def test_offline_result_expires_without_resend_or_false_waiting(configured,monkeypatch):
    fixture_model(configured,monkeypatch)
    row,c,t=request(configured,True)
    result=confirm(configured,t,preview(configured,t));drain();reconcile()
    with SessionLocal.begin() as db:
        task=db.get(ChannelTask,t['id'])
        incoming=db.get(ChannelInbound,next(iter(task.notifications.values())))
        incoming.created_at=now()-3601
    reconcile()
    with SessionLocal() as db:
        task=db.get(ChannelTask,t['id'])
        assert db.get(AgentRun,result['run_id']).status=='completed'
        deliveries=list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id.in_(task.notifications.values()))))
        assert len(deliveries)==1 and deliveries[0].status=='cancelled' and not deliveries[0].content
    assert prepare_outbound(row['id'],c) is None


@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',
                    reason='requires PostgreSQL row locks')
@pytest.mark.parametrize('action',['preview','confirm'])
def test_concurrent_authorization_refreshes_state_after_channel_lock(configured,monkeypatch,action):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from lingxi import channel_tasks
    _,_,t=request(configured,True)
    agents=configured.get('/api/agents').json()
    if action=='preview':body={'version':t['version'],'agent_ids':[agents[0]['id']]}
    else:
        p=preview(configured,t)
        body={'version':p['version'],'digest':p['digest']}
    original=channel_tasks.owned
    barrier=threading.Barrier(2)
    def owned(db,entity,identity,user,lock=False):
        row=original(db,entity,identity,user,lock)
        if entity is ChannelTask and not lock:
            barrier.wait(timeout=10)
        return row
    monkeypatch.setattr(channel_tasks,'owned',owned)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:configured.post('/api/channel-tasks/'+t['id']+'/'+action,json=body),range(2)))
    if action=='preview':
        assert sorted(r.status_code for r in responses)==[200,409]
    else:
        assert [r.status_code for r in responses]==[200,200]
        assert responses[0].json()['run_id']==responses[1].json()['run_id']
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(AgentRun))==1
            assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='agent_plan'))==1
