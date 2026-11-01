"""Bounded, source-checked original-text access; never materialize a whole chat."""
import hashlib
import json
import math

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from .branches import ConversationBranch, MAX_PREFIX
from .conversation_pages import seal, unseal
from .db import now
from .models import Conversation, Message

BATCH = 64
COLUMNS = (Message.id, Message.role, Message.status, Message.content, Message.created_at)


def prefix_ids(db, conversation, user):
    row = db.scalar(select(ConversationBranch).where(
        ConversationBranch.conversation_id == conversation.id,
        ConversationBranch.user_id == user.id, ConversationBranch.workspace_id == user.workspace_id))
    ids = row.prefix_ids if row else []
    if (not isinstance(ids, list) or len(ids) > MAX_PREFIX or
            any(not isinstance(v, str) or not 1 <= len(v) <= 64 for v in ids) or len(set(ids)) != len(ids)):
        raise HTTPException(409, '对话分支记录异常，请联系部署管理员核对')
    return ids


def scoped_query(user):
    return select(*COLUMNS).join(Conversation, Conversation.id == Message.conversation_id).where(
        Message.workspace_id == user.workspace_id, Conversation.workspace_id == user.workspace_id,
        Conversation.user_id == user.id, Conversation.deleted.is_(False), Message.status == 'completed')


def source_hash(row):
    return hashlib.sha256(json.dumps([row.id, row.role, row.status, row.content],
        ensure_ascii=False).encode()).hexdigest()


def rows(db, conversation, user, ids, as_of, position=None):
    query = scoped_query(user).where(Message.created_at <= as_of)
    if not position or position['segment'] == 'prefix':
        start = position['index'] + 1 if position else 0
        for offset in range(start, len(ids), BATCH):
            part = ids[offset:offset+BATCH]
            found = {r.id: r for r in db.execute(query.where(Message.id.in_(part)))}
            for index, identity in enumerate(part, offset):
                if identity in found:
                    yield found[identity], {'segment': 'prefix', 'index': index}
    own = query.where(Message.conversation_id == conversation.id)
    if position and position['segment'] == 'own':
        own = own.where(or_(Message.created_at > position['time'],
            and_(Message.created_at == position['time'], Message.id > position['id'])))
    # Server-side cursors on PostgreSQL, incremental fetches on SQLite. No ORM
    # instances or full-history list survive a batch; Unicode casefold is shared.
    result = db.execute(own.order_by(Message.created_at, Message.id).execution_options(yield_per=BATCH))
    try:
        for row in result:
            yield row, {'segment': 'own', 'time': row.created_at, 'id': row.id}
    finally:
        result.close()


def match_start(text, query):
    if not query:
        return 0
    # Find a source character offset even when casefold expands a character,
    # e.g. Straße/STRASSE. Never slice original text at a folded-text offset.
    folded = text.casefold()
    offset = folded.find(query)
    if offset < 0:
        return None
    count = 0
    for i, char in enumerate(text):
        count += len(char.casefold())
        if count > offset:
            return i
    return 0


def item(row, start):
    begin = max(0, start-40)
    return {'message_id': row.id, 'role': row.role, 'created_at': row.created_at,
        'preview': row.content[begin:begin+240], 'characters': len(row.content),
        'preview_start': begin, 'match_start': start, 'source_hash': source_hash(row)}


def search(db, conversation, user, body):
    if body.cursor and body.include_total:
        raise HTTPException(422, '游标分页请关闭完整计数；完整计数继续使用偏移分页')
    ids = prefix_ids(db, conversation, user)
    query = body.query.casefold()
    scope = ['history-search-v1', conversation.id, hashlib.sha256(body.query.encode()).hexdigest(),
        hashlib.sha256(json.dumps(ids).encode()).hexdigest(), body.include_total]
    cursor = unseal(body.cursor, user, scope)
    at = now()
    if body.cursor:
        try:
            if not isinstance(cursor, dict) or set(cursor) != {'as_of', 'position'}:
                raise ValueError()
            if (type(cursor['as_of']) not in (int, float) or not math.isfinite(cursor['as_of']) or
                    not 0 <= cursor['as_of'] <= at+60):
                raise ValueError()
            pos = cursor['position']
            if not isinstance(pos, dict): raise ValueError()
            if pos.get('segment') == 'prefix':
                if set(pos) != {'segment', 'index'} or type(pos['index']) is not int or not 0 <= pos['index'] < len(ids):
                    raise ValueError()
            elif pos.get('segment') == 'own':
                if (set(pos) != {'segment', 'time', 'id'} or type(pos['time']) not in (int, float) or
                        not math.isfinite(pos['time']) or not 0 <= pos['time'] <= cursor['as_of'] or
                        not isinstance(pos['id'], str) or not 1 <= len(pos['id']) <= 64):
                    raise ValueError()
            else: raise ValueError()
            at = cursor['as_of']
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, '原文分页凭据无效，请重新检索') from None
    if body.cursor and body.offset:
        raise HTTPException(422, '不能同时使用分页凭据与旧式偏移')
    position = cursor['position'] if cursor else None
    items, last, count, more = [], None, 0, False
    # Legacy callers can request an exact count without loading all matches.
    # The Web UI uses cursor pages and stops after one extra match.
    stream = rows(db, conversation, user, ids, at, position)
    try:
        for row, pointer in stream:
            start = match_start(row.content, query)
            if start is None: continue
            count += 1
            if not body.cursor and count <= body.offset: continue
            if len(items) < body.limit:
                items.append(item(row, start));last = pointer
            else:
                more = True
                if not body.include_total: break
    finally:
        stream.close()
    token = seal(user, scope, {'as_of': at, 'position': last}) if more and not body.include_total else None
    return {'items': items, 'next_offset': body.offset+len(items) if more and not body.cursor else None,
        'total': count if body.include_total else None, 'next_cursor': token, 'as_of': at}


def read(db, conversation, user, identity, start, length, expected=None):
    ids = prefix_ids(db, conversation, user)
    row = db.execute(scoped_query(user).where(Message.id == identity,
        or_(Message.conversation_id == conversation.id, Message.id.in_(ids)))).first()
    if not row:
        raise HTTPException(404, '原文不可用或不属于当前对话分支')
    digest = source_hash(row)
    if expected and expected != digest:
        raise HTTPException(409, '原文已变化，请重新检索后读取，旧片段已停止展示')
    if start > len(row.content):
        raise HTTPException(422, '原文位置超出范围，请从头读取')
    from .media import MediaLink, Media
    # Return only references that are still within this actor's workspace.
    attachments = list(db.scalars(select(Media.id).join(MediaLink, MediaLink.media_id == Media.id).where(
        MediaLink.message_id == identity, Media.workspace_id == user.workspace_id,
        Media.user_id == user.id, Media.status != 'deleted')))
    return {'message_id': row.id, 'role': row.role, 'content': row.content[start:start+length],
        'start': start, 'end': min(start+length, len(row.content)), 'total': len(row.content),
        'attachment_ids': attachments, 'source_hash': digest}
