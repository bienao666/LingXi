import hashlib
import io
import json
import time
import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from lingxi import channel_media as cm
from lingxi.db import SessionLocal, now
from lingxi.channels import Channel, ChannelInbound, ChannelDelivery
from lingxi.channel_gateway import receive_event, prepare_outbound, close_connection
from lingxi.media import Media, MediaLink, VisionCapability, storage
from lingxi.models import ChatRun, Message, Job
from lingxi.security import decrypt
from lingxi.worker import claim, perform
from test_channels import create, rules, connect, bind, event

HOST = 'cdn.example.com'
URL = 'https://'+HOST+'/media?signature=private-query-only'


def setup(client, *, share=False, **extra):
    channel = rules(client, create(client), attachments_enabled=True, attachment_hosts=[HOST],
                    allow_unbound=True, **extra)
    connection = connect(channel['id'])
    if share:
        bind(client, channel, connection, True)
    return channel, connection


def inbound(channel, connection, *, kind='file', url=URL, name='资料.md', text='请根据附件回答', **extra):
    e = event(text, **extra)
    e['message'].append({'type':kind, 'data':{'url':url, 'name':name}})
    receive_event(channel['id'], connection, e)
    return e


def downloader(monkeypatch, payload=b'LingXi document evidence'):
    captured = []
    def fetch(url, hosts, path, limit, fence, deadline):
        captured.append(url)
        fence()
        path.write_bytes(payload)
        return len(payload), hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(cm, 'download', fetch)
    return captured


def process():
    job = claim()
    assert job
    perform(job)
    return job


@pytest.mark.parametrize('host', ['*.qq.com','https://cdn.qq.com','cdn.qq.com:443','cdn.qq.com/path',
    '127.0.0.1','localhost','[::1]','qq.com.','a..qq.com','a.-qq.com','a.qq-.com',' user.qq.com','用户.qq.com'])
def test_rejects_unsafe_host_rules(client, host):
    row = create(client)
    result = client.put('/api/channels/'+row['id']+'/rules', json={**row['rules'],
        'attachment_hosts':[host], 'version':row['version']})
    assert result.status_code == 422


@pytest.mark.parametrize('url', ['http://cdn.example.com/a','file:///etc/passwd','base64://abc',
    'https://user:password@cdn.example.com/a','https://cdn.example.com:444/a',
    'https://other.example.com/a','https://sub.cdn.example.com/a','https://cdn.example.com/a#fragment',
    'https://cdn.example.com\\@other.example.com/a','https://cdn.example.com/a\nheader'])
def test_signed_url_validator_does_not_weaken_service_endpoint(url):
    with pytest.raises(HTTPException):
        cm.checked_url(url,[HOST])
    assert cm.checked_url(URL,[HOST]) == 'https://'+HOST


def test_default_off_never_retains_url_or_file(client):
    channel=rules(client,create(client),allow_unbound=True);connection=connect(channel['id'])
    inbound(channel,connection)
    with SessionLocal() as db:
        assert not db.scalar(select(cm.ChannelAttachment))
        assert not db.scalar(select(Media))
        assert db.scalar(select(ChannelInbound)).error
    action=prepare_outbound(channel['id'],connection)
    assert '尚未开启' in action['params']['message'][0]['data']['text']


def test_untriggered_group_stranger_self_duplicate_and_notice_retention(client):
    channel,connection=setup(client,group_enabled=True,group_ids=['12345'])
    inbound(channel,connection,group='12345')  # untriggered
    inbound(channel,connection,user='12345678')  # own output
    e=inbound(channel,connection,group='12345',mention=True,message_id=2)
    receive_event(channel['id'],connection,e)
    receive_event(channel['id'],connection,{'self_id':12345678,'post_type':'notice','notice_type':'group_upload',
        'group_id':12345,'file':{'url':URL}})
    with SessionLocal() as db:
        assert len(list(db.scalars(select(cm.ChannelAttachment))))==1
        assert len(list(db.scalars(select(ChannelInbound))))==1


def test_private_binding_gate_precedes_url_retention(client):
    channel,connection=setup(client)
    channel=rules(client,channel,allow_unbound=False)
    inbound(channel,connection)
    with SessionLocal() as db:
        assert not db.scalar(select(cm.ChannelAttachment))


def test_ingestion_durable_encrypted_url_metadata_and_redaction(client,monkeypatch):
    channel,connection=setup(client)
    calls=downloader(monkeypatch)
    e=inbound(channel,connection)
    with SessionLocal() as db:
        attachment=db.scalar(select(cm.ChannelAttachment))
        assert URL not in attachment.encrypted_url and decrypt(attachment.encrypted_url)==URL
        assert attachment.media_id is None  # no network on gateway receipt
    assert prepare_outbound(channel['id'],connection) is None
    assert process().kind=='channel_media'
    assert calls==[URL]
    assert process().kind=='media_parse'
    record=client.get('/api/channels/'+channel['id']+'/messages').json()[0]
    assert record['attachments'][0]['status']=='ready'
    assert 'signature' not in json.dumps(record) and '资料.md' not in json.dumps(record,ensure_ascii=False)
    with SessionLocal() as db:
        attachment=db.scalar(select(cm.ChannelAttachment))
        assert attachment.encrypted_url==''
        assert db.get(Media,attachment.media_id).status=='ready'
    receive_event(channel['id'],connection,e)
    assert calls==[URL]


def test_document_reuses_scoped_actor_and_parser_then_actual_chat(configured,monkeypatch):
    channel,connection=setup(configured)
    downloader(monkeypatch,'项目：灵犀\n附件证据'.encode())
    inbound(channel,connection)
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    captured=[]
    def stream(*args,**kwargs):
        captured.extend(args[2]);yield '附件第 2 行写了附件证据。'
    monkeypatch.setattr('lingxi.worker.stream',stream)
    process()
    with SessionLocal() as db:
        run=db.scalar(select(ChatRun));assert run.status=='completed' and run.user_id!='owner'
        assert run.config['channel']['memory_allowed'] is False
        assert run.config['media_refs']
        assert not db.scalar(select(Job).where(Job.kind=='extract'))
        assert db.scalar(select(Media)).user_id==run.user_id
        assert db.scalar(select(MediaLink))
    assert '第 2 行' in json.dumps(captured,ensure_ascii=False)
    assert '仅作数据' in json.dumps(captured,ensure_ascii=False)
    action=prepare_outbound(channel['id'],connection)
    assert action['action']=='send_private_msg'
    assert '附件证据' in action['params']['message'][0]['data']['text']
    receive_event(channel['id'],connection,{'echo':action['echo'],'retcode':0,'data':{'message_id':11}})
    record=configured.get('/api/channels/'+channel['id']+'/messages').json()[0]
    assert record['status']=='completed' and record['deliveries'][0]['status']=='accepted_by_protocol'


def test_bound_owner_attachment_only_does_not_extract_synthetic_fact(configured,monkeypatch):
    channel,connection=setup(configured,share=True)
    downloader(monkeypatch)
    inbound(channel,connection,text='',message_id=2)
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        assert db.scalar(select(ChatRun)).user_id=='owner'
        assert not db.scalar(select(Job).where(Job.kind=='extract'))
        assert db.scalar(select(Message)).content=='请说明所附图片或文档的内容。'


def test_image_normalization_and_verified_vision_required(configured,monkeypatch):
    channel,connection=setup(configured)
    buf=io.BytesIO();Image.new('RGB',(8,9),'red').save(buf,'PNG')
    downloader(monkeypatch,buf.getvalue())
    inbound(channel,connection,kind='image')
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert db.scalar(select(Media)).status=='deleted'
        assert db.scalar(select(cm.ChannelAttachment)).encrypted_url==''


def test_verified_image_is_passed_to_vision_model(configured,monkeypatch):
    channel,connection=setup(configured)
    with SessionLocal.begin() as db:
        db.add(VisionCapability(model_id='chat',provider_version=1,model_name='fixture-model',status='verified'))
    buf=io.BytesIO();Image.new('RGB',(7,9),'blue').save(buf,'PNG')
    downloader(monkeypatch,buf.getvalue())
    inbound(channel,connection,kind='image')
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    captured=[]
    def stream(*args,**kw):captured.extend(args[2]);yield '蓝色'
    monkeypatch.setattr('lingxi.worker.stream',stream)
    process()
    assert 'data:image/jpeg;base64,' in json.dumps(captured)
    assert URL not in json.dumps(captured)


@pytest.mark.parametrize('name',['../evil.md','bad.exe','a\\b.txt','a\n.txt','', 'a.png'])
def test_file_name_type_and_path_are_not_downloaded(client,name):
    channel,connection=setup(client)
    inbound(channel,connection,name=name)
    with SessionLocal() as db:
        assert not db.scalar(select(cm.ChannelAttachment))
        assert db.scalar(select(ChannelInbound)).error


def test_mixed_unsupported_or_missing_url_rejects_whole_message(client):
    channel,connection=setup(client)
    e=event('看看');e['message'] += [{'type':'image','data':{'url':URL}}, {'type':'record','data':{'url':URL}}]
    receive_event(channel['id'],connection,e)
    inbound(channel,connection,url=None,message_id=2)
    with SessionLocal() as db:
        assert not db.scalar(select(cm.ChannelAttachment))
        assert not db.scalar(select(ChatRun))


def test_attachment_count_rejected_atomically(client):
    channel,connection=setup(client)
    e=event();e['message'] += [{'type':'image','data':{'url':URL}}]*11
    receive_event(channel['id'],connection,e)
    with SessionLocal() as db:
        assert not db.scalar(select(cm.ChannelAttachment))
        assert '10' in db.scalar(select(ChannelInbound)).error


def test_channel_revoke_during_download_fences_and_deletes_staging(client,monkeypatch):
    channel,connection=setup(client)
    inbound(channel,connection);prepare_outbound(channel['id'],connection)
    def fetch(url,hosts,path,limit,fence,deadline):
        path.write_bytes(b'partial')
        rules(client,channel,attachments_enabled=False)
        fence()
        raise AssertionError('revocation must fence')
    monkeypatch.setattr(cm,'download',fetch)
    process()
    with SessionLocal() as db:
        assert db.scalar(select(ChannelInbound)).status=='cancelled'
        assert db.scalar(select(cm.ChannelAttachment)).encrypted_url==''
        assert db.scalar(select(Media)).status=='deleted'
        assert not db.scalar(select(ChatRun))


def test_expired_url_and_network_error_do_not_retry_or_leak(client,monkeypatch):
    channel,connection=setup(client)
    inbound(channel,connection);prepare_outbound(channel['id'],connection)
    with SessionLocal.begin() as db:db.scalar(select(cm.ChannelAttachment)).expires_at=now()-1
    calls=downloader(monkeypatch)
    process()
    assert calls==[]
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.kind=='channel_media')).status=='completed'
        assert db.scalar(select(cm.ChannelAttachment)).encrypted_url==''
        assert db.scalar(select(Media)).status=='deleted'
    assert URL not in client.get('/api/channels/'+channel['id']+'/messages').text


def test_download_failure_and_parse_failure_never_answer_partial(configured,monkeypatch):
    channel,connection=setup(configured)
    downloader(monkeypatch,b'not a PDF')
    inbound(channel,connection,name='坏.pdf')
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert db.scalar(select(Media)).status=='deleted'


def test_delete_before_final_send_blocks_attachment_answer(configured,monkeypatch):
    channel,connection=setup(configured,share=True)
    downloader(monkeypatch)
    inbound(channel,connection,message_id=2)
    prepare_outbound(channel['id'],connection);process();process()
    prepare_outbound(channel['id'],connection)
    monkeypatch.setattr('lingxi.worker.stream',lambda *a,**k:iter(['private attachment fact']))
    monkeypatch.setattr('lingxi.worker.MemoryService.search',lambda *a:[])
    process()
    with SessionLocal.begin() as db:db.get(Channel,channel['id']).last_sent=now()
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:item=db.scalar(select(Media));identity,version=item.id,item.version
    assert configured.request('DELETE','/api/media/'+identity,json={'version':version}).status_code==200
    with SessionLocal.begin() as db:db.get(Channel,channel['id']).last_sent=0
    assert prepare_outbound(channel['id'],connection) is None
    with SessionLocal() as db:
        assert db.scalar(select(ChannelDelivery)).status=='cancelled'


def test_restart_reuses_download_and_same_message(configured,monkeypatch):
    channel,connection=setup(configured)
    calls=downloader(monkeypatch)
    inbound(channel,connection);prepare_outbound(channel['id'],connection);process()
    close_connection(channel['id'],connection);connection=connect(channel['id'])
    process();prepare_outbound(channel['id'],connection);prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ChatRun))))==1
        assert len(list(db.scalars(select(Media))))==1
    assert len(calls)==1


def test_bound_owner_group_attachment_never_inherits_owner_memory(configured,monkeypatch):
    channel,connection=setup(configured,share=True,group_enabled=True,group_ids=['12345'])
    downloader(monkeypatch)
    inbound(channel,connection,group='12345',mention=True,message_id=2)
    prepare_outbound(channel['id'],connection);process();process();prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        run=db.scalar(select(ChatRun));media=db.scalar(select(Media))
        assert run.user_id!='owner' and media.user_id==run.user_id
        assert not run.config['channel']['memory_allowed']
        assert run.config['channel']['binding_id'] is None
    assert configured.get('/api/media/'+media.id).status_code==404


def test_network_error_is_sanitized_clears_urls_and_has_no_partial_chat(configured,monkeypatch):
    channel,connection=setup(configured)
    inbound(channel,connection);prepare_outbound(channel['id'],connection)
    def fetch(*a):raise RuntimeError('private upstream body '+URL)
    monkeypatch.setattr(cm,'download',fetch)
    process()
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert not db.scalar(select(cm.ChannelAttachment)).encrypted_url
        assert db.scalar(select(Job).where(Job.kind=='channel_media')).status=='completed'
    record=configured.get('/api/channels/'+channel['id']+'/messages').text
    assert 'signature' not in record and 'private upstream' not in record


def test_delete_while_downloading_prevents_parse_and_resurrection(configured,monkeypatch):
    channel,connection=setup(configured,share=True)
    inbound(channel,connection,message_id=2);prepare_outbound(channel['id'],connection)
    def fetch(url,hosts,path,limit,fence,deadline):
        with SessionLocal() as db:media=db.scalar(select(Media))
        configured.request('DELETE','/api/media/'+media.id,json={'version':media.version}).raise_for_status()
        fence()
        raise AssertionError('deletion must fence')
    monkeypatch.setattr(cm,'download',fetch)
    process()
    with SessionLocal() as db:
        assert db.scalar(select(Media)).status=='deleted'
        assert not db.scalar(select(Job).where(Job.kind=='media_parse'))
        assert not db.scalar(select(cm.ChannelAttachment)).encrypted_url


def test_worker_lease_reclaim_downloads_once_and_parser_is_idempotent(client,monkeypatch):
    channel,connection=setup(client)
    inbound(channel,connection);prepare_outbound(channel['id'],connection)
    old=claim();assert old.kind=='channel_media'
    with SessionLocal.begin() as db:db.get(Job,old.id).lease_until=now()-1
    calls=downloader(monkeypatch)
    new=claim();assert new.id==old.id and new.lease_token!=old.lease_token
    perform(new);perform(old)
    process()
    with SessionLocal() as db:
        assert db.scalar(select(Media)).status=='ready'
        assert len(list(db.scalars(select(Job).where(Job.kind=='media_parse'))))==1
    assert len(calls)==1


@pytest.mark.parametrize('after_download',[False,True])
def test_deleted_conversation_cannot_resurrect_staged_attachment(configured,monkeypatch,after_download):
    channel,connection=setup(configured,share=True)
    calls=downloader(monkeypatch)
    inbound(channel,connection,message_id=2);prepare_outbound(channel['id'],connection)
    if after_download:process();process()
    with SessionLocal() as db:conversation_id=db.scalar(select(cm.ChannelAttachment)).conversation_id
    response=configured.request('DELETE','/api/conversations/'+conversation_id,json={'delete_memories':False})
    assert response.status_code==200,response.text
    if not after_download:process()
    prepare_outbound(channel['id'],connection)
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun))
        assert db.scalar(select(ChannelInbound)).status=='cancelled'
        assert db.scalar(select(Media)).status=='deleted'
    assert len(calls)==(1 if after_download else 0)


class BytesStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b'abc'
        yield b'def'


@pytest.mark.parametrize('status,headers,limit',[(302,{},99),(200,{'Content-Encoding':'gzip'},99),
    (200,{'Content-Length':'999'},8),(200,{},5),(200,{'Content-Length':'7'},9)])
def test_download_enforces_redirect_compression_size_and_complete_length(tmp_path,monkeypatch,status,headers,limit):
    monkeypatch.setattr('lingxi.network.outbound_client',lambda *a,**k:httpx.Client(
        transport=httpx.MockTransport(lambda r:httpx.Response(status,headers=headers,stream=BytesStream()))))
    with pytest.raises(HTTPException):
        cm.download(URL,[HOST],tmp_path/'part',limit,lambda:None,time.monotonic()+30)


def test_download_keeps_query_and_uses_saved_system_proxy(client,monkeypatch,tmp_path):
    seen=[]
    def transport(address,host,proxy=None,**kw):
        seen.append((address,host,proxy))
        def response(request):
            seen.append((str(request.url),request.headers.get('authorization'),request.headers.get('accept-encoding')))
            return httpx.Response(200,stream=BytesStream())
        return httpx.MockTransport(response)
    monkeypatch.setattr('lingxi.network.PinnedTransport',transport)
    monkeypatch.setattr('lingxi.network.validate_endpoint',lambda url,allow: '8.8.8.8')
    client.put('/api/admin/proxy',json={'enabled':True,'http_proxy':'http://proxy.test:7890','https_proxy':'http://proxy.test:7890','version':0})
    size,digest=cm.download(URL,[HOST],tmp_path/'part',20,lambda:None,time.monotonic()+30)
    assert size==6 and digest==hashlib.sha256(b'abcdef').hexdigest()
    assert seen==[('8.8.8.8',HOST,'http://proxy.test:7890'),(URL,None,'identity')]


def test_dns_private_rebinding_rejected_before_connection(client,monkeypatch,tmp_path):
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(HTTPException,match='内网'):
        cm.download(URL,[HOST],tmp_path/'part',20,lambda:None,time.monotonic()+30)
    assert not (tmp_path/'part').exists()
