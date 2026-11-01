"""Long-chat originals, Unicode search, cursor continuity and source isolation."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Conversation, Message, User
from lingxi.branches import ConversationBranch


def seed(count=25):
    with SessionLocal.begin() as db:
        db.add(Conversation(id='history', user_id='owner', title='历史原文', created_at=1))
        db.add_all([Message(id=f'm{i:04}', conversation_id='history', role='user' if i%2==0 else 'assistant',
            content=f'原文关键词{i}', created_at=10) for i in range(count)])


def search(client, cid='history', **body):
    response=client.post(f'/api/conversations/{cid}/history/search', json={'include_total':False, **body})
    assert response.status_code==200,response.text
    return response.json()


def test_cursor_full_history_with_ties_and_no_full_count(client, monkeypatch):
    seed(325)
    import lingxi.branches as branches
    monkeypatch.setattr(branches,'history',lambda *a,**kw:pytest.fail('Whole history must not load'))
    seen=[];cursor=None
    while True:
        page=search(client,limit=23,cursor=cursor)
        assert page['total'] is None
        assert all(len(row['source_hash'])==64 for row in page['items'])
        seen.extend(row['message_id'] for row in page['items'])
        cursor=page['next_cursor']
        if not cursor:break
    assert seen==[f'm{i:04}' for i in range(325)]


def test_cursor_stops_after_lookahead_and_direct_read_is_one_source(client,monkeypatch):
    seed(200)
    import lingxi.history_access as access
    scanned=[];original=access.match_start
    def scan(text,q):scanned.append(text);return original(text,q)
    monkeypatch.setattr(access,'match_start',scan)
    assert len(search(client,limit=7)['items'])==7
    assert len(scanned)==8
    import lingxi.branches as branches
    monkeypatch.setattr(branches,'history',lambda *a,**kw:pytest.fail('Whole history must not load'))
    response=client.get('/api/conversations/history/history/m0100',params={'length':2})
    assert response.status_code==200 and len(response.json()['content'])==2


def test_legacy_count_and_offset_remain_compatible(client):
    seed(125)
    page=client.post('/api/conversations/history/history/search',json={'query':'关键词','offset':110,'limit':10}).json()
    assert page['total']==125 and page['next_offset']==120 and page['next_cursor'] is None
    assert [row['message_id'] for row in page['items']]==[f'm{i:04}' for i in range(110,120)]


@pytest.mark.parametrize('query',['STRASSE','Straße','ß','%_','中英'])
def test_unicode_literal_match_preview_and_original_offsets(client,query):
    seed(1)
    content='很长的前文'*150+'Straße%_中英'+'后文'*200
    with SessionLocal.begin() as db:db.get(Message,'m0000').content=content
    row=search(client,query=query)['items'][0]
    assert row['preview_start']>0 and len(row['preview'])<=240
    assert content[row['preview_start']:row['preview_start']+len(row['preview'])]==row['preview']
    assert query.casefold() in row['preview'].casefold()
    value=client.get('/api/conversations/history/history/m0000',params={'source_hash':row['source_hash'],'start':row['match_start'],'length':20}).json()
    assert value['content']==content[row['match_start']:row['match_start']+20]
    assert value['source_hash']==row['source_hash']


def test_deleted_anchor_and_new_messages_do_not_shift_cursor(client):
    seed(6)
    page=search(client,limit=2)
    with SessionLocal.begin() as db:
        db.delete(db.get(Message,'m0001'))
        db.get(Message,'m0002').status='redacted'
        db.add(Message(id='future',conversation_id='history',role='user',content='new',created_at=now()+100))
    rest=search(client,cursor=page['next_cursor'])
    assert [r['message_id'] for r in rest['items']]==['m0003','m0004','m0005']


def branch():
    with SessionLocal.begin() as db:
        db.add(Conversation(id='fork',user_id='owner',title='分支',created_at=1))
        db.add(ConversationBranch(id='fork-link',conversation_id='fork',user_id='owner',parent_id='history',
            anchor_id='m0005',mode='edit',request_id='fork-request',request_hash='x',run_id='none',
            prefix_ids=['m0004','m0001','m0005','m0000']))
        db.add(Message(id='own',conversation_id='fork',role='user',content='own',created_at=2))


def test_frozen_prefix_order_then_own_and_deleted_prefix_anchor(client):
    seed(7);branch()
    page=search(client,'fork',limit=2)
    assert [r['message_id'] for r in page['items']]==['m0004','m0001']
    with SessionLocal.begin() as db:db.delete(db.get(Message,'m0001'))
    rest=search(client,'fork',cursor=page['next_cursor'])
    assert [r['message_id'] for r in rest['items']]==['m0005','m0000','own']
    assert client.get('/api/conversations/fork/history/m0006').status_code==404


def test_prefix_batches_keep_frozen_order_over_sixty_four_sources(client):
    seed(205);branch()
    expected=[f'm{i:04}' for i in range(200,-1,-1)]
    with SessionLocal.begin() as db:db.get(ConversationBranch,'fork-link').prefix_ids=expected
    cursor=None;seen=[]
    while True:
        value=search(client,'fork',limit=31,cursor=cursor)
        seen.extend(r['message_id'] for r in value['items']);cursor=value['next_cursor']
        if not cursor:break
    assert seen==expected+['own']


@pytest.mark.parametrize('target',['owner','workspace','deleted','message_workspace'])
def test_prefix_parent_isolation_for_both_routes_and_core_history(client,target):
    seed(7);branch()
    with SessionLocal.begin() as db:
        parent=db.get(Conversation,'history')
        if target=='owner':parent.user_id='stranger'
        elif target=='workspace':parent.workspace_id='other'
        elif target=='deleted':parent.deleted=True
        else:
            for row in db.scalars(select(Message).where(Message.conversation_id=='history')):row.workspace_id='other'
    assert [r['message_id'] for r in search(client,'fork')['items']]==['own']
    assert client.get('/api/conversations/fork/history/m0004').status_code==404
    from lingxi.branches import history
    with SessionLocal() as db:
        assert [m.id for m in history(db,db.get(Conversation,'fork'))]==['own']


def test_own_malformed_workspace_and_status_not_visible(client):
    seed(7)
    with SessionLocal.begin() as db:
        db.get(Message,'m0000').workspace_id='other'
        for identity,status in zip(['m0001','m0002','m0003','m0004','m0005'],['redacted','deleted','invalidated','superseded','streaming']):
            db.get(Message,identity).status=status
    assert [r['message_id'] for r in search(client)['items']]==['m0006']
    for i in range(6):assert client.get(f'/api/conversations/history/history/m{i:04}').status_code==404
    from lingxi.branches import history
    with SessionLocal() as db:
        assert 'm0000' not in [m.id for m in history(db,db.get(Conversation,'history'))]


def test_source_hash_rejects_changed_role_or_content_and_deleted_source(client):
    seed(1)
    row=search(client)['items'][0]
    for field,value in [('content','修正后的内容'),('role','assistant')]:
        with SessionLocal.begin() as db:setattr(db.get(Message,'m0000'),field,value)
        assert client.get('/api/conversations/history/history/m0000',params={'source_hash':row['source_hash']}).status_code==409
    with SessionLocal.begin() as db:db.get(Message,'m0000').status='redacted'
    assert client.get('/api/conversations/history/history/m0000',params={'source_hash':row['source_hash']}).status_code==404


def test_cursor_bound_to_actor_query_branch_and_mode(client):
    seed(7);branch()
    cursor=search(client,'fork',limit=1)['next_cursor']
    for cid,changes in [('history',{}),('fork',{'query':'other'}),('fork',{'include_total':True})]:
        response=client.post(f'/api/conversations/{cid}/history/search',json={'include_total':False,'cursor':cursor,**changes})
        assert response.status_code in (400,422)
    with SessionLocal.begin() as db:db.get(ConversationBranch,'fork-link').prefix_ids=['m0000']
    assert client.post('/api/conversations/fork/history/search',json={'include_total':False,'cursor':cursor}).status_code==400


def test_cursor_expiry_and_malformed_positions(client,monkeypatch):
    seed(3)
    cursor=search(client,limit=1)['next_cursor']
    import lingxi.conversation_pages as pages
    clock=pages.now()
    monkeypatch.setattr(pages,'now',lambda:clock+86401)
    assert client.post('/api/conversations/history/history/search',json={'include_total':False,'cursor':cursor}).status_code==400
    import lingxi.history_access as access
    monkeypatch.setattr(access,'unseal',lambda *a:{'as_of':now(),'position':{'segment':'own','time':float('nan'),'id':'m0000'}})
    assert client.post('/api/conversations/history/history/search',json={'include_total':False,'cursor':'not-real'}).status_code==400


def test_cursor_cannot_be_used_by_another_owner_or_workspace(client):
    seed(3)
    cursor=search(client,limit=1)['next_cursor']
    with SessionLocal.begin() as db:
        # Even a later authorized transfer of the conversation does not transfer
        # the old actor's short-lived paging permission.
        db.add(User(id='second',username='second',password_hash='not-used'))
        db.get(Conversation,'history').user_id='second'
        owner=db.get(User,'second')
        import lingxi.history_access as access
        from lingxi.conversation_context import HistorySearch
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            access.search(db,db.get(Conversation,'history'),owner,HistorySearch(cursor=cursor,include_total=False))
        assert exc.value.status_code==400


def test_unauthenticated_cross_owner_and_cross_workspace(client):
    seed(1)
    with TestClient(app) as anonymous:
        assert anonymous.post('/api/conversations/history/history/search',json={}).status_code==401
        assert anonymous.get('/api/conversations/history/history/m0000').status_code==401
    with SessionLocal.begin() as db:db.get(Conversation,'history').user_id='stranger'
    assert client.post('/api/conversations/history/history/search',json={}).status_code==404
    assert client.get('/api/conversations/history/history/m0000').status_code==404


def test_read_range_and_input_limits(client):
    seed(1)
    for params in ({'start':-1},{'start':99999},{'length':5001},{'source_hash':'bad'}):
        assert client.get('/api/conversations/history/history/m0000',params=params).status_code==422
    for body in ({'query':'x'*201},{'limit':51},{'cursor':'x'*4097}):
        assert client.post('/api/conversations/history/history/search',json=body).status_code==422


def test_malformed_branch_stops_without_exposing_sources(client):
    seed(7);branch()
    with SessionLocal.begin() as db:db.get(ConversationBranch,'fork-link').prefix_ids=['m0000','m0000']
    assert client.post('/api/conversations/fork/history/search',json={}).status_code==409
    assert client.get('/api/conversations/fork/history/m0000').status_code==409
