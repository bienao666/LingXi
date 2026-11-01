"""Explicit reply references remain scoped, source grounded and revocable."""
import hashlib
import json
import pytest
from sqlalchemy import select, func
from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Conversation, Message, Job, Model, RunEvent
from lingxi import worker, gateway
from lingxi.memory import MemoryService, invalidate_source
from lingxi.message_replies import MAX_QUOTE, digest


def seed(client, role='assistant', content='蓝桥项目：建议先核对需求。', status='completed'):
    c = client.post('/api/conversations', json={'title':'引用测试'}).json()['id']
    with SessionLocal.begin() as db:
        m = Message(conversation_id=c, role=role, content=content, status=status)
        db.add(m); db.flush(); identity=m.id
    return c, identity


def submit(client, c, source=None, **kw):
    return client.post('/api/chat', json={'conversation_id':c,'request_id':'reply-request-001',
        'content':'详细解释一下', **({'reply_to_message_id':source} if source else {}), **kw})


def process(identity):
    with SessionLocal.begin() as db:
        for j in db.scalars(select(Job).where(Job.kind=='extract')):j.status='completed'
    j=worker.claim(); assert j and j.payload['run_id']==identity
    worker.perform(j)


def capture(monkeypatch):
    result=[]
    monkeypatch.setattr(MemoryService,'search',lambda *a:[])
    monkeypatch.setattr(worker,'stream',lambda m,p,messages,*a,**k:(result.append(messages) or iter(['合成回复'])))
    return result


@pytest.mark.parametrize('role',['user','assistant'])
def test_reply_prompt_and_list_page_exports_agree(configured, monkeypatch, role):
    c, source=seed(configured,role=role,content='<script>仅作资料</script>蓝桥项目')
    captured=capture(monkeypatch)
    r=submit(configured,c,source);assert r.status_code==200,r.text
    rid=r.json()['id']
    with SessionLocal() as db:assert db.get(ChatRun,rid).source_message_ids==[source]
    assert set(r.json()['config']['reply_to'])=={'message_id','role','digest'}
    process(rid)
    reply=json.loads(next(m['content'].split('\n',1)[1] for m in captured[0] if '本次明确选择' in m['content']))
    assert reply['message_id']==source and reply['role']==role and not reply['attachments_included']
    assert reply['text']=='<script>仅作资料</script>蓝桥项目'
    listed=configured.get('/api/messages',params={'conversation_id':c}).json()
    paged=configured.get('/api/message-pages',params={'conversation_id':c}).json()['items']
    exported=configured.get(f'/api/conversations/{c}/export?format=json').json()['messages']
    refs=[next(m['reply_to'] for m in rows if m.get('run_id')==rid and m['role']=='user') for rows in (listed,paged,exported)]
    assert refs[0]==refs[1]==refs[2] and refs[0]['status']=='available'
    md=configured.get(f'/api/conversations/{c}/export').text
    assert '回复引用' in md and '未自动读取附件' in md
    if role=='assistant':assert '不等于用户事实' in md


@pytest.mark.parametrize('status',['deleted','redacted','invalidated','superseded','streaming','failed','cancelled'])
def test_unusable_sources_rejected_without_new_run(configured,status):
    c, source=seed(configured,status=status)
    assert submit(configured,c,source).status_code==409
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(ChatRun))==0


@pytest.mark.parametrize('content,role',[('','user'),('  \n','user'),('内容','system'),('内容','tool')])
def test_empty_or_non_dialogue_sources_rejected(configured,content,role):
    c, source=seed(configured,content=content,role=role)
    assert submit(configured,c,source).status_code==409


@pytest.mark.parametrize('kind',['other_conversation','other_owner','other_workspace','deleted_conversation','missing'])
def test_source_scope_is_server_enforced(configured,kind):
    c, source=seed(configured)
    if kind=='other_conversation':
        c=configured.post('/api/conversations',json={'title':'别的会话'}).json()['id']
    else:
        with SessionLocal.begin() as db:
            m=db.get(Message,source);conv=db.get(Conversation,c)
            if kind=='other_owner':conv.user_id='stranger'
            elif kind=='other_workspace':m.workspace_id='other'
            elif kind=='deleted_conversation':conv.deleted=True
            else:source='does-not-exist'
    assert submit(configured,c,source).status_code in (404,409)


def test_idempotency_includes_reference_but_old_hash_unchanged(configured):
    c, source=seed(configured)
    a=submit(configured,c,source); b=submit(configured,c,source)
    assert a.status_code==b.status_code==200 and a.json()['id']==b.json()['id']
    assert submit(configured,c,None).status_code==409
    c2,_=seed(configured)
    plain=submit(configured,c2,None,request_id='plain-request-001')
    with SessionLocal() as db:
        r=db.get(ChatRun,plain.json()['id'])
        assert r.request_hash==hashlib.sha256(json.dumps([c2,'详细解释一下'],ensure_ascii=False).encode()).hexdigest()


@pytest.mark.parametrize('options',[{'task_assist':True},{'collaboration':{'share_memory':False,'allow_files':False,'max_calls':12}}])
def test_reference_is_not_tool_authorization(configured,options):
    c, source=seed(configured)
    assert submit(configured,c,source,**options).status_code==422


def test_reference_prevents_control_command_side_effects(configured,monkeypatch):
    c, source=seed(configured)
    import lingxi.proactive as proactive
    import lingxi.support as support
    def forbidden(*a,**kw):raise AssertionError('引用不应修改偏好')
    monkeypatch.setattr(proactive,'chat_control',forbidden)
    monkeypatch.setattr(support,'chat_control',forbidden)
    r=submit(configured,c,source,content='先别聊')
    assert r.status_code==200 and r.json()['status']=='queued'


@pytest.mark.parametrize('moment',['before','search','stream'])
@pytest.mark.parametrize('mutation',['delete','hash'])
def test_late_invalidation_cannot_publish_old_reference(configured,monkeypatch,moment,mutation):
    c, source=seed(configured,content='旧来源独有内容')
    def change():
        with SessionLocal.begin() as db:
            m=db.get(Message,source)
            if mutation=='delete':
                m.status='redacted';m.content='';invalidate_source(db,source)
            else:m.content='已变更的内容'
    def search(*a):
        if moment=='search':change()
        return []
    def stream(*a,**kw):
        yield '临时片段'
        if moment=='stream':change()
        yield '不得送出的旧依据'
    monkeypatch.setattr(MemoryService,'search',search);monkeypatch.setattr(worker,'stream',stream)
    r=submit(configured,c,source);rid=r.json()['id']
    if moment=='before':change()
    process(rid)
    current=configured.get('/api/chat-runs/'+rid).json();assert current['status']=='invalidated'
    with SessionLocal() as db:
        assert all(not m.content for m in db.scalars(select(Message).where(Message.run_id==rid,Message.role=='assistant')))
        assert not list(db.scalars(select(RunEvent).where(RunEvent.run_id==rid,RunEvent.kind=='delta')))
    output=configured.get(f'/api/conversations/{c}/export?format=json').json()['messages']
    ref=next(m['reply_to'] for m in output if m.get('run_id')==rid and m['role']=='user')
    assert ref['status']=='unavailable' and not ref['content']
    assert '旧来源独有内容' not in json.dumps(ref,ensure_ascii=False)


def test_old_quote_survives_history_budget_without_copying_attachments(configured,monkeypatch):
    c, source=seed(configured,content='早期决定。'*600)
    with SessionLocal.begin() as db:
        db.get(Model,'chat').context_window=20000
        db.get(Message,source).created_at=1
        for i in range(20):db.add(Message(conversation_id=c,role='user',content='后续背景。'*500,created_at=i+2))
    captured=capture(monkeypatch)
    monkeypatch.setattr(gateway,'complete',lambda *a,**kw:'{"quotes":[],"reads":[]}')
    r=submit(configured,c,source);process(r.json()['id'])
    run=configured.get('/api/chat-runs/'+r.json()['id']).json()
    assert run['status']=='completed' and run['config']['history_truncated']
    with SessionLocal() as db:assert source in db.get(ChatRun,r.json()['id']).source_message_ids
    quote=json.loads(next(m['content'].split('\n',1)[1] for m in captured[0] if '本次明确选择' in m['content']))
    assert len(quote['text'])==MAX_QUOTE and quote['truncated'] and not quote['attachments_included']
    assert captured[0][-1]['content']=='详细解释一下'
    assert '早期决定' not in json.dumps(run['config'],ensure_ascii=False)


def test_extraction_receives_only_new_user_text(configured,monkeypatch):
    c, source=seed(configured,content='我住在蓝桥市。',role='assistant')
    r=submit(configured,c,source,content='我喜欢茶。')
    captured=[]
    monkeypatch.setattr(worker,'complete',lambda m,p,messages,**kw:captured.append(messages) or '{"items":[]}')
    with SessionLocal() as db:
        j=db.scalar(select(Job).where(Job.kind=='extract'))
    # Claim extraction explicitly; do not run the chat protocol here.
    with SessionLocal.begin() as db:
        job=db.get(Job,j.id);job.status='running';job.lease_token='test';job.lease_until=now()+60
    with SessionLocal() as db:worker.extract(db.get(Job,j.id))
    assert captured[0][-1]['content']=='我喜欢茶。'
    assert '蓝桥市' not in json.dumps(captured,ensure_ascii=False)


@pytest.mark.parametrize('mode',['edit','regenerate'])
def test_branches_inherit_reference_and_remain_revocable(configured,monkeypatch,mode):
    c, source=seed(configured)
    capture(monkeypatch)
    r=submit(configured,c,source);process(r.json()['id'])
    rows=configured.get('/api/messages',params={'conversation_id':c}).json()
    anchor=next(m for m in rows if m.get('run_id')==r.json()['id'] and m['role']==('user' if mode=='edit' else 'assistant'))
    body={'request_id':'reply-branch-001','message_id':anchor['id'],'mode':mode}
    if mode=='edit':body['content']='修改后的提问'
    result=configured.post(f'/api/conversations/{c}/branches',json=body)
    assert result.status_code==200,result.text
    child=result.json();process(child['run_id'])
    run=configured.get('/api/chat-runs/'+child['run_id']).json()
    assert run['status']=='completed' and run['config']['reply_to']['message_id']==source
    again=submit(configured,child['conversation_id'],source,request_id='child-reply-001')
    assert again.status_code==200
    with SessionLocal.begin() as db:
        db.get(Message,source).status='redacted';db.get(Message,source).content='';invalidate_source(db,source)
    assert configured.get('/api/chat-runs/'+child['run_id']).json()['status']=='invalidated'


def test_branch_rejects_reference_outside_frozen_prefix(configured,monkeypatch):
    c, source=seed(configured)
    capture(monkeypatch)
    r=submit(configured,c,source);process(r.json()['id'])
    user=next(m for m in configured.get('/api/messages',params={'conversation_id':c}).json() if m.get('run_id')==r.json()['id'] and m['role']=='user')
    result=configured.post(f'/api/conversations/{c}/branches',json={'request_id':'branch-outside-001','mode':'edit','message_id':user['id'],'content':'分支提问'})
    process(result.json()['run_id'])
    with SessionLocal.begin() as db:
        late=Message(conversation_id=c,role='assistant',content='分支创建后的原对话内容');db.add(late);db.flush();late_id=late.id
    assert submit(configured,result.json()['conversation_id'],late_id,request_id='outside-reply-001').status_code==409


def test_small_model_reports_real_quote_coverage(configured,monkeypatch):
    c,source=seed(configured,content='中文历史。'*800)
    capture(monkeypatch)
    r=submit(configured,c,source);process(r.json()['id'])
    run=configured.get('/api/chat-runs/'+r.json()['id']).json()
    assert run['status']=='completed',run
    usage=run['config']['reply_to_coverage']
    assert usage['truncated'] and 0<usage['included']<MAX_QUOTE and usage['characters']==4000


def test_reference_skips_old_attachment_but_accepts_explicit_new_one(configured,monkeypatch):
    from test_media import ready, upload, process_media
    from lingxi.media import MediaLink
    old=ready(configured,data='旧附件不应读取'.encode())
    uploaded=upload(configured,data='本次明确选择的附件'.encode(),request_id='reply-new-attachment')
    assert uploaded.status_code==200,uploaded.text
    process_media()
    new=configured.get('/api/media/'+uploaded.json()['id']).json()
    c,source=seed(configured,role='user')
    with SessionLocal.begin() as db:db.add(MediaLink(media_id=old['id'],message_id=source))
    captured=capture(monkeypatch)
    r=submit(configured,c,source,media_ids=[new['id']]);assert r.status_code==200,r.text
    process(r.json()['id'])
    prompt=json.dumps(captured,ensure_ascii=False)
    assert '旧附件不应读取' not in prompt and '本次明确选择的附件' in prompt
    with SessionLocal() as db:
        refs=db.get(ChatRun,r.json()['id']).config['media_refs']
        assert [ref['id'] for ref in refs]==[new['id']]


@pytest.mark.parametrize('mode',['qq','voice'])
def test_channel_or_active_voice_conversations_reject_web_reference(configured,mode):
    c,source=seed(configured)
    with SessionLocal.begin() as db:
        if mode=='qq':
            from lingxi.channels import ChannelSession
            db.add(ChannelSession(user_id='owner',conversation_id=c,channel_id='channel',scope_key='private:1',actor_id='1'))
        else:
            from lingxi.voice import VoiceSession
            db.add(VoiceSession(user_id='owner',conversation_id=c,login_session_id='session',config_version=1,expires_at=now()+300))
    assert submit(configured,c,source).status_code==409
