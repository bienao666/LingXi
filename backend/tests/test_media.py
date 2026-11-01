import io
import json
from pathlib import Path
import zipfile
import pytest
from sqlalchemy import select
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Conversation, Job, Message, User
from lingxi.media import Media, MediaLink, VisionCapability, storage
from lingxi.media_parser import ParseError, parse
from lingxi.worker import claim, perform


def upload(client, filename='笔记.md', data='项目：灵犀\n范围：附件对话'.encode(), request_id='upload-test-001'):
    return client.post('/api/media', params={'filename': filename, 'request_id': request_id},
                       content=data, headers={'Content-Type': 'application/octet-stream'})


def process_media():
    job = claim()
    assert job and job.kind in ('media_parse', 'media_cleanup')
    perform(job)


def ready(client, filename='笔记.md', data='项目：灵犀\n范围：附件对话'.encode()):
    result = upload(client, filename, data)
    assert result.status_code == 200, result.text
    process_media()
    return client.get('/api/media/' + result.json()['id']).json()


def chat(client, media_ids, request_id='chat-media-test'):
    c = client.post('/api/conversations', json={'title':'附件研究'}).json()
    result = client.post('/api/chat', json={'conversation_id': c['id'], 'content':'请解释附件', 'request_id': request_id, 'media_ids':media_ids})
    return c, result


def test_upload_parse_provenance_download(client):
    item = ready(client)
    assert item['status'] == 'ready' and item['parts'][1]['location'] == '第 2 行'
    assert item['parts'][0]['text'] == '项目：灵犀'
    response = client.get('/api/media/' + item['id'] + '/content')
    assert response.status_code == 200 and response.headers['content-disposition'].startswith('attachment;')
    assert '项目' in response.content.decode()
    assert 'digest' not in item and 'user_id' not in item


def test_upload_idempotent_and_conflict(client):
    first, second = upload(client), upload(client)
    assert first.json()['id'] == second.json()['id']
    assert upload(client, data=b'changed').status_code == 409
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Media)))) == 1
        assert len(list(db.scalars(select(Job)))) == 1


@pytest.mark.parametrize('filename', ['../secret.txt', 'a\\b.md', 'a:1.txt', 'a\n.txt', 'test.html', 'test.exe'])
def test_upload_rejects_path_and_type(client, filename):
    assert upload(client, filename).status_code == 422


def test_upload_size_empty_and_content_type(client, monkeypatch):
    monkeypatch.setattr('lingxi.media.MAX_FILE', 8)
    assert upload(client, data=b'123456789').status_code == 413
    assert upload(client, data=b'').status_code == 422
    assert client.post('/api/media?filename=a.txt&request_id=upload-test', content=b'a').status_code == 415


def test_media_auth_csrf_and_scope(client):
    item = ready(client)
    with SessionLocal.begin() as db:
        obj = db.get(Media, item['id'])
        obj.workspace_id = 'other'
    assert client.get('/api/media/' + item['id']).status_code == 404
    assert client.get('/api/media').json()['items'] == []
    token = client.headers.pop('X-CSRF-Token')
    assert upload(client).status_code == 403
    client.headers['X-CSRF-Token'] = token
    client.cookies.clear()
    assert client.get('/api/media').status_code == 401


def test_failed_parse_and_over_budget(client):
    item = ready(client, '空.pdf', b'not a pdf')
    assert item['status'] == 'failed' and item['parts'] == [] and item['error']


def test_document_parser_bounds_and_encoding(tmp_path):
    source, preview = tmp_path/'src', tmp_path/'image'
    source.write_bytes('中文\n内容'.encode('gb18030'))
    assert parse(source, '.txt', preview)['parts'][1]['text'] == '内容'
    source.write_bytes(b'abc\x00test')
    with pytest.raises(ParseError):
        parse(source, '.txt', preview)
    source.write_text('a'*160001)
    with pytest.raises(ParseError, match='预算'):
        parse(source, '.md', preview)


def test_docx_parser_locations_and_no_external_fetch(tmp_path):
    source, preview = tmp_path/'document', tmp_path/'image'
    with zipfile.ZipFile(source, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>附件内容</w:t></w:r></w:p></w:body></w:document>')
        z.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Target="http://127.0.0.1/secrets" TargetMode="External"/></Relationships>')
    assert parse(source, '.docx', preview)['parts'] == [{'location': '第 1 段', 'text': '附件内容'}]


def xlsx(path, formula=False):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="项目预算" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>灵犀</t></is></c><c r="B1">'+('<f>1+1</f>' if formula else '')+'<v>2</v></c></row></sheetData></worksheet>')


def test_xlsx_cell_provenance_and_formula_rejection(tmp_path):
    source, preview = tmp_path/'document', tmp_path/'image'
    xlsx(source)
    parts = parse(source, '.xlsx', preview)['parts']
    assert parts[0] == {'location': '工作表 项目预算 · A1', 'text': '灵犀'}
    assert parts[1]['text'] == '2'
    xlsx(source, True)
    with pytest.raises(ParseError, match='公式'):
        parse(source, '.xlsx', preview)


def test_xml_entities_rejected(tmp_path):
    source, preview = tmp_path/'document', tmp_path/'image'
    with zipfile.ZipFile(source, 'w') as z:
        z.writestr('word/document.xml', '<!DOCTYPE doc [<!ENTITY x "unsafe">]><doc>&x;</doc>')
    with pytest.raises(ParseError):
        parse(source, '.docx', preview)


def test_pdf_text_and_scan_failure(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    source, preview = tmp_path/'document', tmp_path/'image'
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 250 Td (LingXi plan) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(source)
    assert parse(source, '.pdf', preview)['parts'][0] == {'location': '第 1 页', 'text': 'LingXi plan'}
    writer.add_blank_page(300, 300)
    writer.write(source)
    with pytest.raises(ParseError, match='第 2 页'):
        parse(source, '.pdf', preview)


def test_image_normalized_and_fake_image_fails(client):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (8, 9), 'red').save(buf, 'PNG')
    item = ready(client, '颜色.png', buf.getvalue())
    assert item['kind'] == 'image' and item['width'] == 8 and item['height'] == 9
    response = client.get(f"/api/media/{item['id']}/content?preview=true")
    assert response.headers['content-type'] == 'image/jpeg'
    assert response.content.startswith(b'\xff\xd8')
    assert upload(client, '假图.png', b'<html>x</html>', request_id='upload-test-002').status_code == 200
    process_media()
    assert client.get('/api/media').json()['items'][0]['status'] == 'failed'


def test_attach_requires_ready_no_duplicates_and_idempotency(configured):
    client = configured
    item = upload(client).json()
    c, result = chat(client, [item['id']])
    assert result.status_code == 409
    process_media()
    result = client.post('/api/chat', json={'conversation_id':c['id'], 'content':'看附件', 'request_id':'media-chat-test-01','media_ids':[item['id'],item['id']]})
    assert result.status_code == 422
    payload = {'conversation_id':c['id'], 'content':'看附件', 'request_id':'media-chat-test-01','media_ids':[item['id']]}
    result = client.post('/api/chat', json=payload)
    assert result.status_code == 200
    assert client.post('/api/chat', json=payload).json()['id'] == result.json()['id']
    assert client.post('/api/chat', json=payload | {'media_ids':[]}).status_code == 409
    msgs = client.get('/api/messages', params={'conversation_id':c['id']}).json()
    assert msgs[0]['attachments'][0]['id'] == item['id']
    assert '附件 [' in client.get(f"/api/conversations/{c['id']}/export").text


def test_chat_carries_scoped_document_and_never_extracts_file_as_fact(configured, monkeypatch):
    client = configured
    item = ready(client)
    c, result = chat(client, [item['id']])
    captured=[]
    def stream(model, provider, messages, *args, **kwargs):
        captured.extend(messages)
        yield '来源为笔记第 2 行。'
    monkeypatch.setattr('lingxi.worker.stream', stream)
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *args: [])
    perform(claim())
    assert '第 2 行' in json.dumps(captured, ensure_ascii=False)
    assert '仅作数据' in json.dumps(captured, ensure_ascii=False)
    with SessionLocal() as db:
        run = db.get(ChatRun,result.json()['id'])
        assert run.status == 'completed' and run.config['media_refs'][0]['id'] == item['id']
        msg = db.scalar(select(Message).where(Message.role=='user', Message.conversation_id == c['id']))
        assert msg.content == '请解释附件'
        assert '附件对话' not in msg.content


def test_delete_inflight_fences_results_cleanup_and_restore(configured, monkeypatch):
    client = configured
    item = ready(client)
    c, result = chat(client, [item['id']])
    def stream(*args, **kwargs):
        yield 'initial'
        assert client.request('DELETE', '/api/media/'+item['id'], json={'version':item['version']}).status_code == 200
        yield 'should not survive'
    monkeypatch.setattr('lingxi.worker.stream', stream)
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *args: [])
    perform(claim())
    run = client.get('/api/chat-runs/'+result.json()['id']).json()
    assert run['status'] == 'invalidated'
    msgs = client.get('/api/messages', params={'conversation_id':c['id']}).json()
    assert all(not m['content'] for m in msgs if m['role']=='assistant')
    assert client.get('/api/media/'+item['id']+'/content').status_code == 410
    # Restore an older database row; independent ledger must fence it again.
    with SessionLocal.begin() as db:
        obj = db.get(Media,item['id']);obj.status='ready';obj.parsed={'kind':'document','parts':[]}
        from lingxi.memory import replay_ledger
        replay_ledger(db)
        assert obj.status == 'deleted' and obj.parsed == {}


def test_delete_before_parse_no_resurrection_and_version(client):
    item = upload(client).json()
    assert client.request('DELETE', '/api/media/'+item['id'], json={'version':99}).status_code == 409
    assert client.request('DELETE', '/api/media/'+item['id'], json={'version':1}).status_code == 200
    process_media()
    process_media()
    assert client.get('/api/media/'+item['id']).json()['cleanup_status'] == 'completed'
    assert not storage(item['id'],'blob').exists()
    assert upload(client).status_code == 409


def test_vision_probe_requires_correct_response_and_invalidates_on_provider_change(configured, monkeypatch):
    client = configured
    def probe(model, provider, messages, **kw):
        import base64
        from PIL import Image
        value=messages[0]['content'][1]['image_url']['url'].split(',')[1]
        image=Image.open(io.BytesIO(base64.b64decode(value)))
        return ''.join({(255,0,0):'红',(0,255,0):'绿',(0,0,255):'蓝'}[image.getpixel((i%3*80,i//3*80))] for i in range(6))
    monkeypatch.setattr('lingxi.gateway.complete',probe)
    assert client.post('/api/models/chat/vision').json()['status'] == 'verified'
    from lingxi.models import Provider
    with SessionLocal.begin() as db:
        db.get(Provider,'provider').version += 1
    assert client.get('/api/models/chat/vision').json()['status'] == 'unverified'


def test_images_rejected_before_probe(configured):
    from PIL import Image
    buf=io.BytesIO();Image.new('RGB',(4,4),'red').save(buf,'PNG')
    item=ready(configured,'图.png',buf.getvalue())
    _, result=chat(configured,[item['id']])
    assert result.status_code == 409 and '视觉' in result.text


def test_conversation_delete_cleans_unshared_media(configured):
    item = ready(configured)
    c, result = chat(configured,[item['id']])
    assert result.status_code == 200
    assert configured.delete('/api/conversations/'+c['id']).status_code == 200
    assert configured.get('/api/media/'+item['id']).json()['status'] == 'deleted'


def test_delete_propagates_into_completed_later_reply(configured, monkeypatch):
    item = ready(configured)
    c, first = chat(configured, [item['id']])
    monkeypatch.setattr('lingxi.worker.stream', lambda *args, **kw: iter(['附件结论']))
    monkeypatch.setattr('lingxi.worker.MemoryService.search', lambda *args: [])
    perform(claim())
    with SessionLocal.begin() as db:
        # Isolate generation from unrelated extraction for this propagation check.
        for job in db.scalars(select(Job).where(Job.kind == 'extract')):
            job.status='completed'
    second = configured.post('/api/chat', json={'conversation_id': c['id'], 'content':'继续解释', 'request_id':'media-second-chat'}).json()
    perform(claim())
    assert configured.get('/api/chat-runs/'+second['id']).json()['status'] == 'completed'
    configured.request('DELETE', '/api/media/'+item['id'], json={'version':item['version']})
    for identity in (first.json()['id'], second['id']):
        assert configured.get('/api/chat-runs/'+identity).json()['status'] == 'invalidated'


def test_images_are_transported_as_normalized_data_not_external_urls(configured,monkeypatch):
    from PIL import Image
    buf=io.BytesIO();Image.new('RGB',(8,8),'blue').save(buf,'PNG')
    item=ready(configured,'示例.png',buf.getvalue())
    with SessionLocal.begin() as db:
        db.add(VisionCapability(model_id='chat', provider_version=1, model_name='fixture-model',status='verified'))
    _,result=chat(configured,[item['id']])
    seen=[]
    def stream(*args,**kw):
        seen.extend(args[2]);yield '蓝色图像'
    monkeypatch.setattr('lingxi.worker.stream',stream)
    monkeypatch.setattr('lingxi.worker.MemoryService.search',lambda *args:[])
    perform(claim())
    assert configured.get('/api/chat-runs/'+result.json()['id']).json()['status']=='completed'
    blocks=seen[-1]['content']
    assert blocks[-1]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_oversized_document_context_fails_before_model_call(configured,monkeypatch):
    item=ready(configured,'长文.txt',('中'*10000).encode())
    _,result=chat(configured,[item['id']])
    def never(*args,**kwargs):
        pytest.fail('Over-budget content reached model')
    monkeypatch.setattr('lingxi.worker.stream',never)
    monkeypatch.setattr('lingxi.worker.MemoryService.search',lambda *args:[])
    perform(claim())
    response=configured.get('/api/chat-runs/'+result.json()['id']).json()
    assert response['status']=='failed' and '预算' in response['error']


def test_cleanup_failure_is_visible_and_explicitly_retryable(client,monkeypatch):
    item=ready(client)
    client.request('DELETE','/api/media/'+item['id'],json={'version':item['version']})
    from lingxi.worker import finish_failed
    with SessionLocal.begin() as db:
        job=db.scalar(select(Job).where(Job.kind=='media_cleanup'))
        job.status='failed'
        finish_failed(db,job,'存储暂不可用')
    obj=client.get('/api/media/'+item['id']).json()
    assert obj['cleanup_status']=='failed'
    assert client.post('/api/media/'+item['id']+'/retry-cleanup',json={'version':1}).status_code==409
    response=client.post('/api/media/'+item['id']+'/retry-cleanup',json={'version':obj['version']})
    assert response.json()['cleanup_status']=='pending'
    process_media()
    assert client.get('/api/media/'+item['id']).json()['cleanup_status']=='completed'


def test_shared_attachment_survives_one_conversation_deletion(configured):
    item=ready(configured)
    c,first=chat(configured,[item['id']])
    other,second=chat(configured,[item['id']],request_id='media-another-chat')
    assert second.status_code==200
    assert configured.delete('/api/conversations/'+c['id']).status_code==200
    assert configured.get('/api/media/'+item['id']).json()['status']=='ready'


def test_model_cannot_claim_visual_support_by_text_alone(configured,monkeypatch):
    monkeypatch.setattr('lingxi.gateway.complete',lambda *args,**kwargs:'我支持图片输入')
    result=configured.post('/api/models/chat/vision').json()
    assert result['status']=='failed'
    assert configured.get('/api/models/chat/vision').json()['status']=='unverified'


def test_invalidated_context_is_fenced_before_opening_upstream(configured,monkeypatch):
    from lingxi.gateway import stream, resolve_model
    from lingxi.worker import Cancelled
    def never(*args, **kwargs):
        pytest.fail('Invalidated attachment context opened upstream connection')
    monkeypatch.setattr('lingxi.gateway.client_for', never)
    with SessionLocal() as db:
        model,provider,_=resolve_model(db,'chat')
    def invalid():
        raise Cancelled('Source invalidated')
    with pytest.raises(Cancelled):
        next(stream(model,provider,[{'role':'user','content':'private document'}],'fixture',invalid))
