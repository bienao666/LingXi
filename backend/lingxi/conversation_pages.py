"""Scoped keyset pagination; branch history retains its frozen logical order."""
import hashlib
import json
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.orm import aliased
from .branches import ConversationBranch, UNUSABLE
from .db import now
from .media import MediaMessageView, media_for_message
from .models import Conversation, Message
from .schemas import ConversationView
from .security import decrypt, encrypt
from .web import Context, owned, view

router = APIRouter()


class ConversationPage(BaseModel):
    items: list[ConversationView]
    next_cursor: str | None


class MessagePage(BaseModel):
    items: list[MediaMessageView]
    next_cursor: str | None


def seal(user, scope, position):
    return encrypt(json.dumps({'user': user.id, 'workspace': user.workspace_id,
        'scope': scope, 'position': position, 'expires': now()+86400}, separators=(',', ':')))


def unseal(token, user, scope):
    if not token:
        return None
    try:
        data = json.loads(decrypt(token))
        if (data['user'] != user.id or data['workspace'] != user.workspace_id or
                data['scope'] != scope or data['expires'] <= now()):
            raise ValueError()
        return data['position']
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(400, '分页凭据无效或已过期，请返回最新记录') from None


def conversation_query(db, user, q):
    query = select(Conversation).where(Conversation.user_id == user.id,
        Conversation.workspace_id == user.workspace_id, Conversation.deleted.is_(False))
    if not q:
        return query
    own = exists(select(Message.id).where(Message.conversation_id == Conversation.id,
        Message.workspace_id == user.workspace_id, Message.status.not_in(UNUSABLE),
        Message.content.icontains(q, autoescape=True)))
    # JSON expansion stays in the database instead of loading every matching
    # message and branch into application memory. Both supported dialects tested.
    source = aliased(Conversation)
    if db.bind.dialect.name == 'postgresql':
        prefix = func.json_array_elements_text(ConversationBranch.prefix_ids).table_valued('value').render_derived()
    else:
        prefix = func.json_each(ConversationBranch.prefix_ids).table_valued('value')
    inherited = exists(select(ConversationBranch.id).select_from(ConversationBranch)
        .join(prefix, true()).join(Message, Message.id == prefix.c.value)
        .join(source, source.id == Message.conversation_id)
        .where(ConversationBranch.conversation_id == Conversation.id,
            ConversationBranch.user_id == user.id, ConversationBranch.workspace_id == user.workspace_id,
            source.user_id == user.id, source.workspace_id == user.workspace_id, source.deleted.is_(False),
            Message.workspace_id == user.workspace_id, Message.status.not_in(UNUSABLE),
            Message.content.icontains(q, autoescape=True)))
    return query.where(or_(Conversation.title.icontains(q, autoescape=True), own, inherited))


def older(model, timestamp, identity):
    return or_(model.created_at < timestamp, and_(model.created_at == timestamp, model.id < identity))


@router.get('/api/conversation-pages', response_model=ConversationPage)
def conversations(ctx: Context, q: str = Query('', max_length=200),
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(50, ge=1, le=200)):
    db, user, _ = ctx
    scope = ['conversations', hashlib.sha256(q.encode()).hexdigest()]
    position = unseal(cursor, user, scope)
    query = conversation_query(db, user, q)
    if position:
        query = query.where(older(Conversation, position['time'], position['id']))
    rows = list(db.scalars(query.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(limit+1)))
    items = rows[:limit]
    last = items[-1] if items else None
    next_cursor = seal(user, scope, {'time': last.created_at, 'id': last.id}) if len(rows) > limit else None
    return {'items': [view(c) for c in items], 'next_cursor': next_cursor}


@router.get('/api/conversations/{conversation_id}', response_model=ConversationView)
def conversation(conversation_id: str, ctx: Context):
    db, user, _ = ctx
    return view(owned(db, Conversation, conversation_id, user))


@router.get('/api/message-pages', response_model=MessagePage)
def messages(conversation_id: str, ctx: Context,
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(100, ge=1, le=200)):
    db, user, _ = ctx
    conversation = owned(db, Conversation, conversation_id, user)
    from .delegation import expire_delegations
    expire_delegations(db, user.id)
    branch = db.scalar(select(ConversationBranch).where(ConversationBranch.conversation_id == conversation.id,
        ConversationBranch.user_id == user.id, ConversationBranch.workspace_id == user.workspace_id))
    ids = branch.prefix_ids if branch else []
    scope = ['messages', conversation_id, hashlib.sha256(json.dumps(ids).encode()).hexdigest()]
    position = unseal(cursor, user, scope)
    # Descending logical traversal: own messages first, then frozen prefix.
    rows = []
    if not position or position['segment'] == 'own':
        query = select(Message).where(Message.conversation_id == conversation_id, Message.workspace_id == user.workspace_id)
        if position:
            query = query.where(older(Message, position['time'], position['id']))
        rows = [(m, {'segment': 'own', 'time': m.created_at, 'id': m.id}) for m in db.scalars(
            query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit+1))]
    if len(rows) <= limit and ids:
        end = position['index'] if position and position['segment'] == 'prefix' else len(ids)
        prefix_ids = ids[:end]
        found = {m.id: m for m in db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id.in_(prefix_ids), Message.workspace_id == user.workspace_id,
                Conversation.user_id == user.id, Conversation.workspace_id == user.workspace_id,
                Conversation.deleted.is_(False)))}
        for index in range(end-1, -1, -1):
            m = found.get(ids[index])
            if m:
                rows.append((m, {'segment': 'prefix', 'index': index}))
                if len(rows) > limit:
                    break
    selected = rows[:limit]
    token = seal(user, scope, selected[-1][1]) if len(rows) > limit else None
    return {'items': [view(m) | {'attachments': media_for_message(db, m),
        'inherited': m.conversation_id != conversation_id} for m, _ in reversed(selected)], 'next_cursor': token}
