"""Immutable conversation forks reference original evidence, never copy its content."""
import hashlib
import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import ChatRun, Conversation, Entity, Message, User
from .web import Context, audit, owned, view

router = APIRouter()
MAX_PREFIX = 1000
UNUSABLE = {'deleted', 'redacted', 'invalidated', 'superseded'}


class ConversationBranch(Entity, Base):
    __tablename__ = 'conversation_branches'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[str] = mapped_column(String(64), index=True)
    anchor_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(20))
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    prefix_ids: Mapped[list] = mapped_column(JSON, default=list)
    run_id: Mapped[str] = mapped_column(String(64))


class ForkInput(BaseModel):
    model_config = {'extra': 'forbid'}
    request_id: str = Field(min_length=8, max_length=100)
    message_id: str = Field(min_length=1, max_length=64)
    mode: Literal['edit', 'regenerate']
    content: str | None = Field(default=None, min_length=1, max_length=12000)


class ForkView(BaseModel):
    conversation_id: str
    run_id: str


class BranchLink(BaseModel):
    id: str
    title: str
    mode: str


class BranchRelations(BaseModel):
    is_branch: bool
    origin: BranchLink | None
    children: list[BranchLink]


def history(db, conversation, *, include_unusable=False, limit=None, before=None):
    """Flattened frozen prefix plus this conversation's own messages in order."""
    branch = db.scalar(select(ConversationBranch).where(ConversationBranch.conversation_id == conversation.id,
        ConversationBranch.user_id == conversation.user_id, ConversationBranch.workspace_id == conversation.workspace_id))
    inherited = []
    if branch and branch.prefix_ids:
        rows = db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id.in_(branch.prefix_ids), Conversation.user_id == conversation.user_id,
                Conversation.workspace_id == conversation.workspace_id, Conversation.deleted.is_(False)))
        by_id = {m.id: m for m in rows}
        inherited = [by_id[i] for i in branch.prefix_ids if i in by_id]
    query = select(Message).where(Message.conversation_id == conversation.id)
    if before is not None:
        query = query.where(Message.created_at < before)
        inherited = [m for m in inherited if m.created_at < before]
    if limit is not None:
        own = list(reversed(list(db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)))))
    else:
        own = list(db.scalars(query.order_by(Message.created_at, Message.id)))
    combined = inherited + own
    visible = combined if include_unusable else [m for m in combined if m.status not in UNUSABLE]
    return visible[-limit:] if limit is not None else visible


def action_rows(db, conversation, limit=100):
    from .chat_actions import ChatAction
    branch = db.scalar(select(ConversationBranch).where(ConversationBranch.conversation_id == conversation.id,
        ConversationBranch.user_id == conversation.user_id, ConversationBranch.workspace_id == conversation.workspace_id))
    source_ids = branch.prefix_ids if branch else []
    return list(db.scalars(select(ChatAction).where(ChatAction.user_id == conversation.user_id,
        ChatAction.workspace_id == conversation.workspace_id,
        (ChatAction.conversation_id == conversation.id) | ChatAction.source_id.in_(source_ids))
        .order_by(ChatAction.created_at.desc(), ChatAction.id.desc()).limit(limit)))


@router.get('/api/conversations/{conversation_id}/branches', response_model=BranchRelations)
def related_branches(conversation_id: str, ctx: Context):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user)
    branch = db.scalar(select(ConversationBranch).where(ConversationBranch.conversation_id == conversation_id))
    parent = db.get(Conversation, branch.parent_id) if branch else None
    origin = {'id': parent.id, 'title': parent.title, 'mode': branch.mode} if parent and not parent.deleted and parent.user_id == user.id and parent.workspace_id == user.workspace_id else None
    children = db.execute(select(Conversation, ConversationBranch).join(ConversationBranch,
        ConversationBranch.conversation_id == Conversation.id).where(ConversationBranch.parent_id == conversation_id,
        Conversation.user_id == user.id, Conversation.workspace_id == user.workspace_id,
        Conversation.deleted.is_(False)).order_by(Conversation.created_at.desc()).limit(100)).all()
    return {'is_branch': bool(branch), 'origin': origin,
        'children': [{'id': c.id, 'title': c.title, 'mode': b.mode} for c, b in children]}


@router.post('/api/conversations/{conversation_id}/branches', response_model=ForkView)
def fork(conversation_id: str, body: ForkInput, ctx: Context):
    db, user, _ = ctx
    # Same serialization fence as normal submission, deletion and contact controls.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    parent = owned(db, Conversation, conversation_id, user, lock=True)
    digest = hashlib.sha256(json.dumps([conversation_id, body.model_dump(exclude={'request_id'})],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(ConversationBranch).where(ConversationBranch.user_id == user.id,
        ConversationBranch.request_id == body.request_id))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, '分支请求标识已被使用，请重新提交')
        owned(db, Conversation, existing.conversation_id, user)
        return {'conversation_id': existing.conversation_id, 'run_id': existing.run_id}
    from .channels import ChannelSession
    from .voice import VoiceSession
    from .db import now
    if db.scalar(select(ChannelSession).where(ChannelSession.conversation_id == parent.id)):
        raise HTTPException(409, 'QQ 会话不支持编辑或重新生成，请使用独立 Web 对话')
    if db.scalar(select(VoiceSession).where(VoiceSession.conversation_id == parent.id,
            VoiceSession.status == 'active', VoiceSession.expires_at > now())):
        raise HTTPException(409, '请先结束实时语音，再创建消息分支')
    if db.scalar(select(ChatRun).where(ChatRun.conversation_id == parent.id, ChatRun.status.in_(['queued', 'running']))):
        raise HTTPException(409, '请先等待或停止当前回复，再创建分支')
    messages = history(db, parent)
    anchor = next((m for m in messages if m.id == body.message_id), None)
    if not anchor or anchor.status not in ('completed', 'failed', 'cancelled'):
        raise HTTPException(409, '消息已失效或仍在生成，请刷新后重试')
    position = messages.index(anchor)
    if body.mode == 'edit':
        if anchor.role != 'user' or not body.content or not body.content.strip():
            raise HTTPException(422, '编辑分支需要用户消息及修改后的文字')
        prefix, input_message = messages[:position], None
    else:
        if anchor.role != 'assistant' or not anchor.run_id or body.content is not None:
            raise HTTPException(422, '请选择可重新生成的助手回复')
        source_run = db.get(ChatRun, anchor.run_id)
        input_id = source_run.config.get('input_message_id') if source_run else None
        input_message = next((m for m in reversed(messages[:position]) if m.role == 'user' and
            (m.id == input_id if input_id else m.run_id == anchor.run_id)), None)
        if not input_message or input_message.status != 'completed':
            raise HTTPException(409, '原始提问已失效，无法重新生成')
        if source_run.config.get('channel') or source_run.config.get('voice_session_id'):
            raise HTTPException(409, '渠道或语音回复不支持重新生成，请发送新的文字提问')
        prefix = messages[:messages.index(input_message) + 1]
    if len(prefix) > MAX_PREFIX:
        raise HTTPException(409, '分支原文超过 1000 条，请另建会话并明确所需资料')
    # Web forks inherit finished text as shown in the Web history.
    prefix = [m for m in prefix if m.status == 'completed']
    child = Conversation(user_id=user.id, workspace_id=user.workspace_id,
        title=parent.title[:190] + (' · 编辑分支' if body.mode == 'edit' else ' · 回复分支'))
    db.add(child)
    db.flush()
    branch = ConversationBranch(user_id=user.id, workspace_id=user.workspace_id, conversation_id=child.id,
        parent_id=parent.id, anchor_id=anchor.id, mode=body.mode, request_id=body.request_id,
        request_hash=digest, prefix_ids=[m.id for m in prefix], run_id='')
    db.add(branch)
    from .assistants import ConversationPersona
    persona = db.get(ConversationPersona, parent.id)
    if persona:
        db.add(ConversationPersona(conversation_id=child.id, persona_id=persona.persona_id, inherit_default=persona.inherit_default))
    db.flush()
    from .message_replies import resolve as resolve_reply, snapshot as reply_snapshot
    original_run = (db.get(ChatRun, anchor.run_id) if anchor.run_id else None) if body.mode == 'edit' else source_run
    original_reply = resolve_reply(db, original_run) if original_run else None
    reply = reply_snapshot(db, child, original_reply.id) if original_reply else None
    if body.mode == 'edit':
        from .api import submit_chat
        from .media import MediaChatInput, MediaLink
        attachments = list(db.scalars(select(MediaLink.media_id).where(MediaLink.message_id == anchor.id)))
        run = submit_chat(MediaChatInput(conversation_id=child.id, request_id='branch:' + branch.id,
            content=body.content, media_ids=attachments,
            reply_to_message_id=reply['message_id'] if reply else None), ctx, fork_context=True)
        generated = db.get(ChatRun, run['id'])
    else:
        from .assistants import chat_profile
        from .gateway import resolve_model
        from .memory import enqueue
        persona = chat_profile(db, child, user)
        model, provider, version = resolve_model(db, 'chat', persona['spec'].get('model_id'))
        from .system_settings import run_snapshot
        generated = ChatRun(user_id=user.id, workspace_id=user.workspace_id, conversation_id=child.id,
            request_id='branch:' + branch.id, request_hash=digest, model_id=model.id,
            config={'model': model.name, 'provider_version': provider.version, 'policy_version': version,
                'prompt_version': 'lingxi-expression-v2', 'persona': persona, **run_snapshot(db, user),
                **({'reply_to':reply} if reply else {}),
                'input_message_id': input_message.id, 'regeneration_of': anchor.run_id})
        db.add(generated)
        db.flush()
        enqueue(db, 'chat:' + generated.id, 'chat', {'run_id': generated.id})
    # Reference fence exists from submission, before a worker starts searching.
    generated.source_message_ids = list(dict.fromkeys(generated.source_message_ids + branch.prefix_ids))
    branch.run_id = generated.id
    audit(db, user, 'conversation_' + body.mode, child.id)
    return {'conversation_id': child.id, 'run_id': generated.id}
