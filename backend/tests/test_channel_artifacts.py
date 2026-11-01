"""Explicit artifact disclosure and durable protocol intent; no real QQ/model calls."""
import base64
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func

from lingxi.api import app
from lingxi import worker
from lingxi.db import SessionLocal,now
from lingxi.models import ChatRun,Message,Conversation,User,Job,Memory
from lingxi.channels import Channel,ChannelInbound,ChannelDelivery
from lingxi.channel_gateway import prepare_outbound,receive_event,close_connection
from lingxi.channel_artifacts import ArtifactReply
from lingxi.media import Media,storage
from test_channel_files import setup as answer_setup,ack
from test_channels import connect,rules


def setup(client,monkeypatch,*,group=False,image=False):
    channel,connection,answer=answer_setup(client,monkeypatch,text='已完成的原回复',group=group)
    ack(channel,connection,answer)
    with SessionLocal.begin() as db:
        incoming=db.scalar(select(ChannelInbound))
        origin=incoming.id
        blob=b'\x89PNG\r\n\x1a\nsynthetic-only' if image else '# 受控资料\n仅发送用户所选完整文件。'.encode()
        item=Media(user_id='owner',workspace_id='personal',request_id='asset-'+uuid4().hex,
            filename='公开图片.png' if image else '项目说明.md',suffix='.png' if image else '.md',
            size=len(blob),digest=hashlib.sha256(blob).hexdigest(),status='ready',parsed={'kind':'image' if image else 'document'})
        db.add(item);db.flush();storage(item.id,'blob').write_bytes(blob)
        ref={'kind':'media','id':item.id,'version':item.version}
    return channel,connection,origin,ref,blob


def preview(client,channel,origin,refs,**changes):
    body={'origin_id':origin,'request_id':'artifact-'+uuid4().hex,'sources':refs}|changes
    result=client.post('/api/channels/'+channel['id']+'/artifact-previews',json=body)
    assert result.status_code==200,result.text
    return result.json(),body


def confirm(client,p):
    result=client.post('/api/channel-artifact-replies/'+p['id']+'/confirm',json={
        'digest':p['digest'],'acknowledgement':'share_with_original_audience'})
    assert result.status_code==200,result.text
    return result.json()


@pytest.mark.parametrize('group,image',[(False,False),(True,False),(False,True),(True,True)])
def test_real_bytes_original_audience_and_distinct_receipts(configured,monkeypatch,group,image):
    channel,connection,origin,ref,blob=setup(configured,monkeypatch,group=group,image=image)
    p,body=preview(configured,channel,origin,[ref])
    assert p['status']=='preview' and p['deliveries']==[]
    assert prepare_outbound(channel['id'],connection) is None
    assert configured.post('/api/channels/'+channel['id']+'/artifact-previews',json=body).json()['id']==p['id']
    first=confirm(configured,p);assert confirm(configured,p)['deliveries'][0]['id']==first['deliveries'][0]['id']
    action=prepare_outbound(channel['id'],connection)
    params=action['params'];assert str(params['group_id' if group else 'user_id'])==('12345' if group else '87654321')
    encoded=params['message'][0]['data']['file'] if image else params['file']
    assert base64.b64decode(encoded[9:])==blob
    assert 'url' not in params and 'path' not in params
    assert action['action']==('send_group_msg' if group else 'send_private_msg') if image else action['action']==('upload_group_file' if group else 'upload_private_file')
    ack(channel,connection,action)
    reply=configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]
    assert reply['status']=='accepted_by_protocol'
    delivery=reply['deliveries'][0]
    assert bool(delivery['protocol_message_id'])==image and bool(delivery['protocol_file_id'])!=image
    assert not any(value in str(reply) for value in ('base64','hash','password','blob'))
    assert prepare_outbound(channel['id'],connection) is None
    assert configured.get('/api/channels').json()[0]['capabilities']['image_send' if image else 'file_send']=='accepted_by_protocol'


def test_auth_csrf_session_and_confirmation_required(configured,monkeypatch):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref])
    url='/api/channel-artifact-replies/'+p['id']+'/confirm'
    assert configured.post(url,json={'digest':p['digest']}).status_code==422
    assert configured.post(url,json={'digest':'0'*64,'acknowledgement':'share_with_original_audience'}).status_code==409
    token=configured.headers.pop('X-CSRF-Token')
    assert configured.post(url,json={'digest':p['digest'],'acknowledgement':'share_with_original_audience'}).status_code==403
    configured.headers['X-CSRF-Token']=token
    with TestClient(app) as other:
        assert other.get('/api/channels/'+channel['id']+'/artifact-sources',params={'origin_id':origin}).status_code==401
        login=other.post('/api/auth/login',json={'username':'owner','password':'test-password-123'})
        other.headers['X-CSRF-Token']=login.json()['csrf']
        assert other.post(url,json={'digest':p['digest'],'acknowledgement':'share_with_original_audience'}).status_code==409
    with SessionLocal.begin() as db:db.get(ArtifactReply,p['id']).expires_at=now()-1
    assert configured.post(url,json={'digest':p['digest'],'acknowledgement':'share_with_original_audience'}).status_code==409
    assert prepare_outbound(channel['id'],c) is None


@pytest.mark.parametrize('change',['deleted','version','bytes','hash','other_owner','workspace','raw_tamper','missing_blob','source_grant','origin_deleted','origin_target','origin_age','binding','policy'])
def test_invalidated_source_before_confirmation_or_sending(configured,monkeypatch,change):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    with SessionLocal.begin() as db:
        item=db.get(Media,ref['id']);incoming=db.get(ChannelInbound,origin)
        if change=='deleted':item.status='deleted'
        elif change=='version':item.version+=1
        elif change=='bytes':item.size+=1
        elif change=='hash':item.digest='0'*64
        elif change=='other_owner':item.user_id='other'
        elif change=='workspace':item.workspace_id='other'
        elif change=='raw_tamper':storage(item.id,'blob').write_bytes(b'tampered')
        elif change=='missing_blob':storage(item.id,'blob').unlink()
        elif change=='source_grant':item.source={'kind':'server_document','task_id':'revoked'}
        elif change=='origin_deleted':db.get(Conversation,db.get(ChatRun,incoming.run_id).conversation_id).deleted=True
        elif change=='origin_target':incoming.target_key='private:99999999'
        elif change=='origin_age':incoming.created_at=now()-86401
        elif change=='binding':incoming.binding_id='no-longer-present'
        else:
            from lingxi.media_policy import MediaPolicy
            db.get(MediaPolicy,'personal').allowed_types=[]
    assert prepare_outbound(channel['id'],c) is None
    with SessionLocal() as db:
        output=db.scalar(select(ChannelDelivery).where(ChannelDelivery.kind=='artifact_file'))
        assert output.status=='cancelled'


def test_preview_source_version_hash_and_target_changes_reject_confirmation(configured,monkeypatch):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref])
    with SessionLocal.begin() as db:db.get(Media,ref['id']).version+=1
    result=configured.post('/api/channel-artifact-replies/'+p['id']+'/confirm',json={'digest':p['digest'],'acknowledgement':'share_with_original_audience'})
    assert result.status_code==409
    assert prepare_outbound(channel['id'],c) is None


def test_cancel_and_channel_disable_stop_pending_without_retracting_sent(configured,monkeypatch):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    r=configured.post('/api/channel-artifact-replies/'+p['id']+'/cancel')
    assert r.status_code==200 and r.json()['deliveries'][0]['status']=='cancelled'
    assert prepare_outbound(channel['id'],c) is None
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    rules(configured,channel,private_enabled=False)
    assert prepare_outbound(channel['id'],c) is None


@pytest.mark.parametrize('image',[False,True])
def test_unknown_disconnect_no_replay_and_late_receipt(configured,monkeypatch,image):
    channel,c,origin,ref,_=setup(configured,monkeypatch,image=image)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    action=prepare_outbound(channel['id'],c)
    receive_event(channel['id'],c,{'echo':action['echo'],'status':'ok','retcode':0,'data':{}})
    assert configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]['status']=='unknown'
    assert prepare_outbound(channel['id'],c) is None
    ack(channel,c,action)
    assert prepare_outbound(channel['id'],c) is None
    assert configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]['status']=='accepted_by_protocol'
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    prepare_outbound(channel['id'],c);close_connection(channel['id'],c);c=connect(channel['id'])
    assert prepare_outbound(channel['id'],c) is None
    with SessionLocal() as db:assert db.scalar(select(ChannelDelivery).where(ChannelDelivery.inbound_id==db.get(ArtifactReply,p['id']).inbound_id)).status=='unknown'


def test_offline_expiry_and_restore_never_replay(configured,monkeypatch):
    from lingxi.channel_artifacts import checkpoint,restore
    from lingxi.channel_files import checkpoint as receipts,restore as restore_receipts
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    with SessionLocal.begin() as db:
        saved=checkpoint(db);barriers=receipts(db)
        assert 'blob' not in str(saved) and 'base64' not in str(saved)
        restore(db,saved);restore_receipts(db,barriers)
    assert prepare_outbound(channel['id'],c) is None
    assert configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]['status']=='cancelled'


def test_catalog_ownership_search_and_no_path_target_inputs(configured,monkeypatch):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    url='/api/channels/'+channel['id']+'/artifact-sources'
    rows=configured.get(url,params={'origin_id':origin,'q':'项目'}).json()
    assert rows[0]['id']==ref['id'] and rows[0]['name']=='项目说明.md'
    assert configured.get(url,params={'origin_id':origin,'q':'does-not-exist'}).json()==[]
    body={'origin_id':origin,'request_id':'bad-extra-request','sources':[ref]}
    for extra in ({'target':'private:88888888'},{'path':'/etc/passwd'}):
        assert configured.post('/api/channels/'+channel['id']+'/artifact-previews',json=body|extra).status_code==422
    assert configured.post('/api/channels/'+channel['id']+'/artifact-previews',json=body|{'sources':[ref]*2}).status_code==422
    with SessionLocal.begin() as db:db.get(Media,ref['id']).workspace_id='other'
    assert configured.get(url,params={'origin_id':origin}).json()==[]


def test_task_result_is_original_only_and_preserves_partial_status(configured,monkeypatch):
    from test_channel_tasks import request,preview as task_preview,confirm as task_confirm,receipt
    from test_delegation import fixture_model,drain
    from lingxi.channel_tasks import ChannelTask
    from lingxi.delegation import AgentRun
    fixture_model(configured,monkeypatch)
    channel,c,task=request(configured)
    task_result=task_confirm(configured,task,task_preview(configured,task));drain()
    with SessionLocal.begin() as db:
        taskrow=db.get(ChannelTask,task['id']);origin=taskrow.inbound_id
        run=db.get(AgentRun,task_result['run_id']);assert run.status=='completed',run.error
        run.status='partial'
        ref={'kind':'agent_result','id':run.id,'version':run.version}
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    action=prepare_outbound(channel['id'],c)
    raw=base64.b64decode(action['params']['file'][9:]).decode()
    assert '部分完成' in raw and '依据节点' in raw and '外部操作以真实回执' in raw
    assert 'config' not in raw and 'password' not in raw

@pytest.mark.parametrize('outcome',['failed','unknown'])
def test_multiple_files_block_after_uncertain_or_failed_first(configured,monkeypatch,outcome):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    with SessionLocal.begin() as db:
        item=db.get(Media,ref['id']);blob=storage(item.id,'blob').read_bytes()
        other=Media(user_id=item.user_id,workspace_id=item.workspace_id,request_id=uuid4().hex,filename='第二份.md',suffix='.md',size=len(blob),digest=item.digest,status='ready',parsed=item.parsed)
        db.add(other);db.flush();storage(other.id,'blob').write_bytes(blob)
        ref2={'kind':'media','id':other.id,'version':other.version}
    p,_=preview(configured,channel,origin,[ref,ref2]);confirm(configured,p)
    first=prepare_outbound(channel['id'],c)
    receive_event(channel['id'],c,{'echo':first['echo'],'retcode':1 if outcome=='failed' else 0,'status':'failed' if outcome=='failed' else 'ok','data':{}})
    with SessionLocal.begin() as db:db.get(Channel,channel['id']).last_sent=0
    assert prepare_outbound(channel['id'],c) is None
    rows=configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]
    assert rows['status']==outcome and rows['deliveries'][1]['status']=='pending'
    configured.post('/api/channel-artifact-replies/'+p['id']+'/cancel')
    rows=configured.get('/api/channels/'+channel['id']+'/artifact-replies',params={'origin_id':origin}).json()[0]
    assert rows['deliveries'][0]['status']==outcome and rows['deliveries'][1]['status']=='cancelled'


def test_confirmed_expiry_never_sends(configured,monkeypatch):
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    with SessionLocal.begin() as db:db.get(ArtifactReply,p['id']).expires_at=now()-1
    assert prepare_outbound(channel['id'],c) is None
    with SessionLocal() as db:assert db.scalar(select(ChannelDelivery).where(ChannelDelivery.kind=='artifact_file')).status=='cancelled'


def test_backup_quarantine_preserves_latest_artifact_barriers(configured,monkeypatch):
    from lingxi.backups import checkpoint,quarantine
    from test_backups import schema
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref]);confirm(configured,p)
    action=prepare_outbound(channel['id'],c)
    with SessionLocal.begin() as db:schema(db)
    saved=checkpoint()
    assert len(saved['channel_artifact_replies'])==1
    with SessionLocal.begin() as db:
        db.query(ChannelDelivery).filter(ChannelDelivery.kind=='artifact_file').delete()
        db.query(ArtifactReply).delete()
    quarantine(saved)
    with SessionLocal() as db:
        row=db.get(ArtifactReply,p['id']);assert row.status=='cancelled' and row.login_id=='revoked'
        delivery=db.scalar(select(ChannelDelivery).where(ChannelDelivery.kind=='artifact_file'))
        assert delivery.echo==action['echo'] and delivery.status=='unknown'
    assert prepare_outbound(channel['id'],c) is False

@pytest.mark.skipif(__import__('lingxi.db',fromlist=['engine']).engine.dialect.name!='postgresql',reason='PostgreSQL serialization')
def test_concurrent_confirmation_creates_one_batch(configured,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    channel,c,origin,ref,_=setup(configured,monkeypatch)
    p,_=preview(configured,channel,origin,[ref])
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:confirm(configured,p),range(2)))
    assert rows[0]['deliveries'][0]['id']==rows[1]['deliveries'][0]['id']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ChannelInbound).where(ChannelInbound.external_message_id=='artifact:'+p['id']))==1
        assert db.scalar(select(func.count()).select_from(ChannelDelivery).where(ChannelDelivery.kind=='artifact_file'))==1
