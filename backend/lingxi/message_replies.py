"""Explicit Web reply references. No copied evidence, implicit attachments or grants."""
import hashlib

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from .models import ChatRun, Conversation, Message

MAX_QUOTE = 1200


class ReplyView(BaseModel):
    message_id: str
    status: str
    role: str | None = None
    content: str = ''
    truncated: bool = False
    characters: int = 0


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def visible(db, conversation, identity):
    """Single exact source lookup, including a frozen inherited branch prefix."""
    from .branches import ConversationBranch
    source = db.get(Message, identity, populate_existing=True)
    if not source or source.workspace_id != conversation.workspace_id:
        return None
    owner = db.get(Conversation, source.conversation_id, populate_existing=True)
    if (not owner or owner.deleted or owner.workspace_id != conversation.workspace_id
            or owner.user_id != conversation.user_id):
        return None
    if source.conversation_id != conversation.id:
        branch = db.scalar(select(ConversationBranch).where(
            ConversationBranch.conversation_id == conversation.id,
            ConversationBranch.user_id == conversation.user_id,
            ConversationBranch.workspace_id == conversation.workspace_id))
        if not branch or identity not in branch.prefix_ids:
            return None
    return source


def snapshot(db, conversation, identity):
    if not identity:
        return None
    source = visible(db, conversation, identity)
    if not source or source.status != 'completed' or source.role not in ('user','assistant') or not source.content.strip():
        raise HTTPException(409, '引用消息不可用或不在当前历史中，请取消引用后重试')
    return {'message_id':source.id, 'role':source.role, 'digest':digest(source.content)}


def resolve(db, run):
    ref = run.config.get('reply_to')
    if not ref:
        return None
    conversation = db.get(Conversation, run.conversation_id, populate_existing=True)
    if (not conversation or conversation.deleted or conversation.user_id != run.user_id
            or conversation.workspace_id != run.workspace_id):
        raise HTTPException(409, '引用消息的会话已失效')
    source = visible(db, conversation, ref['message_id'])
    if (not source or source.status != 'completed' or source.role != ref['role']
            or digest(source.content) != ref['digest']):
        raise HTTPException(409, '引用消息已变化或失效，请取消引用后重新提问')
    return source


def presentation(db, message):
    if message.role != 'user' or not message.run_id or message.status != 'completed':
        return None
    run = db.get(ChatRun, message.run_id)
    if not run or run.workspace_id != message.workspace_id or run.conversation_id != message.conversation_id:
        return None
    ref = run.config.get('reply_to')
    if not ref:
        return None
    try:
        source = resolve(db, run)
    except HTTPException:
        return ReplyView(message_id=ref['message_id'],status='unavailable').model_dump()
    return ReplyView(message_id=source.id,status='available',role=source.role,
        content=source.content[:MAX_QUOTE],truncated=len(source.content)>MAX_QUOTE,
        characters=len(source.content)).model_dump()


def context(db, run, *, byte_limit=None):
    source = resolve(db, run)
    if not source:
        return None
    text = source.content[:MAX_QUOTE]
    if byte_limit is not None:
        text = text.encode('utf-8')[:max(0,byte_limit)].decode('utf-8',errors='ignore')
    return {'message_id':source.id,'role':source.role,'text':text,
        'truncated':len(source.content)>len(text),'characters':len(source.content),
        'attachments_included':False}


def markdown(ref):
    if not ref:
        return ''
    if ref['status'] != 'available':
        return '\n\n> 引用消息已不可用。'
    header = '用户原文' if ref['role']=='user' else '助手历史回复（不等于用户事实）'
    return '\n\n> 回复引用 · '+header+'\n'+ '\n'.join('> '+line for line in ref['content'].splitlines()) + (
        '\n> 引用已截取；未自动读取附件。' if ref['truncated'] else '\n> 未自动读取附件。')
