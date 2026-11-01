"""Complete, scoped paging across ties, forks, deletions and concurrent inserts."""
import pytest
from lingxi.db import SessionLocal
from lingxi.models import Conversation, Message, User
from lingxi.branches import ConversationBranch


def seed(count=205, messages=0):
    with SessionLocal.begin() as db:
        db.add_all([Conversation(id=f'c{i:04}', user_id='owner', title=f'会话{i}', created_at=10) for i in range(count)])
        db.add_all([Message(id=f'm{i:04}', conversation_id='c0000', role='user', content=f'内容{i}', created_at=20) for i in range(messages)])


def page(client, path='/api/conversation-pages', **params):
    result = client.get(path, params=params)
    assert result.status_code == 200, result.text
    return result.json()


def test_all_conversations_with_equal_timestamps(client):
    seed()
    seen, cursor = [], None
    while True:
        data = page(client, limit=37, **({'cursor': cursor} if cursor else {}))
        seen += [c['id'] for c in data['items']]
        cursor = data['next_cursor']
        if not cursor:
            break
    assert seen == [f'c{i:04}' for i in range(204, -1, -1)]
    assert client.get('/api/conversations/c0000').json()['title'] == '会话0'


def test_more_than_thousand_messages_without_tie_loss(client):
    seed(1, 1205)
    seen, cursor = [], None
    while True:
        data = page(client, '/api/message-pages', conversation_id='c0000', limit=73, **({'cursor': cursor} if cursor else {}))
        seen = [m['id'] for m in data['items']] + seen
        cursor = data['next_cursor']
        if not cursor:
            break
    assert seen == [f'm{i:04}' for i in range(1205)]


def test_deleted_anchor_and_new_insert_do_not_skip_remaining(client):
    seed(5, 5)
    cs = page(client, limit=2)
    ms = page(client, '/api/message-pages', conversation_id='c0000', limit=2)
    with SessionLocal.begin() as db:
        db.delete(db.get(Conversation, 'c0003'))
        db.delete(db.get(Message, 'm0003'))
        db.add(Conversation(id='new', user_id='owner', title='新会话', created_at=30))
        db.add(Message(id='new', conversation_id='c0000', role='user', created_at=30))
    assert [c['id'] for c in page(client, cursor=cs['next_cursor'])['items']] == ['c0002', 'c0001', 'c0000']
    assert [m['id'] for m in page(client, '/api/message-pages', conversation_id='c0000', cursor=ms['next_cursor'])['items']] == ['m0000', 'm0001', 'm0002']


def seed_branch():
    seed(2, 6)
    with SessionLocal.begin() as db:
        db.add(ConversationBranch(id='fork', conversation_id='c0001', user_id='owner', parent_id='c0000',
            anchor_id='m0005', mode='edit', request_id='fixture-request', request_hash='x',
            prefix_ids=['m0004', 'm0001', 'm0005', 'm0000'], run_id='none'))
        # Own message timestamps intentionally earlier than inherited messages.
        db.add_all([Message(id=f'own{i}', conversation_id='c0001', role='user', content='分支正文', created_at=1) for i in range(3)])


def test_branch_logical_order_and_prefix_boundaries(client):
    seed_branch()
    ids, cursor = [], None
    while True:
        data = page(client, '/api/message-pages', conversation_id='c0001', limit=2, **({'cursor': cursor} if cursor else {}))
        ids = [m['id'] for m in data['items']] + ids
        assert all(m['inherited'] == m['id'].startswith('m') for m in data['items'])
        cursor = data['next_cursor']
        if not cursor:
            break
    assert ids == ['m0004', 'm0001', 'm0005', 'm0000', 'own0', 'own1', 'own2']


def test_parent_deleted_between_prefix_pages(client):
    seed_branch()
    data = page(client, '/api/message-pages', conversation_id='c0001', limit=4)
    with SessionLocal.begin() as db:
        db.get(Conversation, 'c0000').deleted = True
    assert page(client, '/api/message-pages', conversation_id='c0001', cursor=data['next_cursor']) == {'items': [], 'next_cursor': None}


def test_deleted_prefix_anchor_continues_by_index(client):
    seed_branch()
    data = page(client, '/api/message-pages', conversation_id='c0001', limit=5)
    with SessionLocal.begin() as db:
        db.delete(db.get(Message, 'm0005'))
    assert [m['id'] for m in page(client, '/api/message-pages', conversation_id='c0001', cursor=data['next_cursor'])['items']] == ['m0004', 'm0001']


@pytest.mark.parametrize('status', ['deleted', 'redacted', 'invalidated', 'superseded'])
def test_unusable_text_not_searchable_in_own_or_fork(client, status):
    seed_branch()
    with SessionLocal.begin() as db:
        m = db.get(Message, 'm0004')
        m.content, m.status = '绝密匹配', status
    assert page(client, q='绝密匹配')['items'] == []
    assert client.get('/api/conversations', params={'q': '绝密匹配'}).json() == []


def test_scoped_search_including_prefix_and_literal_wildcards(client):
    seed_branch()
    with SessionLocal.begin() as db:
        db.get(Message, 'm0004').content = 'Evidence%_中文'
    assert {c['id'] for c in page(client, q='evidence%_')['items']} == {'c0000', 'c0001'}
    with SessionLocal.begin() as db:
        db.get(Conversation, 'c0000').deleted = True
    assert page(client, q='Evidence')['items'] == []


def test_cursor_bound_to_query_conversation_owner_workspace_and_kind(client):
    seed(3, 3)
    cs = page(client, limit=1)['next_cursor']
    ms = page(client, '/api/message-pages', conversation_id='c0000', limit=1)['next_cursor']
    assert client.get('/api/conversation-pages', params={'cursor': cs, 'q': 'other'}).status_code == 400
    for cid, cursor in [('c0001', ms), ('c0000', cs)]:
        assert client.get('/api/message-pages', params={'conversation_id': cid, 'cursor': cursor}).status_code == 400
    with SessionLocal.begin() as db:
        db.get(User, 'owner').workspace_id = 'other'
    assert client.get('/api/conversation-pages', params={'cursor': cs}).status_code == 400
    assert client.get('/api/conversations/c0000').status_code == 404


def test_scope_filters_malformed_cross_workspace_messages(client):
    seed_branch()
    with SessionLocal.begin() as db:
        db.get(Message, 'm0004').workspace_id = 'other'
        db.get(Message, 'm0004').content = '隔离关键词'
        db.get(Conversation, 'c0000').user_id = 'stranger'
    assert page(client, q='隔离关键词')['items'] == []
    data = page(client, '/api/message-pages', conversation_id='c0001')
    assert all(not m['inherited'] for m in data['items'])
    assert client.get('/api/conversations/c0000').status_code == 404


@pytest.mark.parametrize('cursor', ['broken', 'x'*4000])
def test_bad_cursor_is_explicit_error(client, cursor):
    seed(1)
    assert client.get('/api/conversation-pages', params={'cursor': cursor}).status_code == 400


def test_empty_last_page_deleted_conversation_and_auth(client):
    seed(2)
    cursor = page(client, limit=1)['next_cursor']
    with SessionLocal.begin() as db:
        db.get(Conversation, 'c0000').deleted = True
    assert page(client, cursor=cursor) == {'items': [], 'next_cursor': None}
    assert client.get('/api/message-pages', params={'conversation_id': 'c0000'}).status_code == 404
    client.cookies.clear()
    assert client.get('/api/conversation-pages').status_code == 401


def test_expired_cursor_and_changed_prefix_require_reload(client, monkeypatch):
    import lingxi.conversation_pages as paging
    seed_branch()
    data = page(client, '/api/message-pages', conversation_id='c0001', limit=1)
    with SessionLocal.begin() as db:
        db.get(ConversationBranch, 'fork').prefix_ids = ['m0001']
    assert client.get('/api/message-pages', params={'conversation_id': 'c0001', 'cursor': data['next_cursor']}).status_code == 400
    cursor = page(client, limit=1)['next_cursor']
    real_now = paging.now
    monkeypatch.setattr(paging, 'now', lambda: real_now()+90000)
    assert client.get('/api/conversation-pages', params={'cursor': cursor}).status_code == 400
