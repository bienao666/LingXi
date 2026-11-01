"""Human review of dialogue candidates; similarity never decides fact replacement."""
import json
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from .db import now
from .memory import MemoryService, usable
from .models import Candidate, ChatRun, Conversation, Memory, Message
from .schemas import CandidateView, MemoryInput, MemoryView
from .security import decrypt, encrypt
from .tasks import digest
from .web import owned, view


class DialogueCandidateSource(BaseModel):
    candidate: CandidateView
    source: dict
    related: list[MemoryView]
    related_count: int
    matched_count: int
    truncated: bool
    warning: str


class CandidateReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resolution: Literal['keep', 'replace']
    target_memory_id: str | None = Field(default=None, min_length=1, max_length=64)
    target_version: int | None = Field(default=None, ge=1)
    # New evidence does not inherit a prior fact's dates. The reviewer can annotate them.
    event_at: str | None = Field(default=None, max_length=100)
    valid_from: str | None = Field(default=None, max_length=100)
    valid_to: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def valid_choice(self):
        if (self.resolution == 'replace') != (self.target_memory_id is not None and self.target_version is not None):
            raise ValueError('纠正旧项须同时指定记录和版本；新增记录不指定旧项')
        if self.resolution == 'keep' and (self.target_memory_id is not None or self.target_version is not None):
            raise ValueError('新增记录不能指定纠正目标')
        MemoryInput(content='时间校验', event_at=self.event_at, valid_from=self.valid_from, valid_to=self.valid_to)
        return self


class CandidateReviewPreview(BaseModel):
    candidate_id: str
    resolution: Literal['keep', 'replace']
    before: MemoryView | None
    after: MemoryInput
    token: str
    expires_at: float
    warning: str


def dialogue_source(db, candidate, user, lock=False):
    if candidate.origin.get('kind') == 'agent':
        raise HTTPException(409, '协作候选请使用原有的来源采纳流程')
    source = db.get(Message, candidate.source_id, populate_existing=True)
    conversation = db.get(Conversation, source.conversation_id, populate_existing=True) if source else None
    if (not source or source.role != 'user' or source.status != 'completed' or not conversation or conversation.deleted
            or conversation.user_id != user.id or conversation.workspace_id != user.workspace_id
            or not candidate.content or candidate.content not in source.content):
        raise HTTPException(409, '候选原文已变化、删除或不属于当前账号，请重新核对')
    run = db.get(ChatRun, source.run_id, populate_existing=True) if source.run_id else None
    if run and run.config.get('voice_input', {}).get('automatic'):
        raise HTTPException(409, '自动语音转写不能直接保存为长期记忆')
    if run and (scope := run.config.get('channel')):
        from .channel_gateway import channel_scope_valid
        if scope.get('audio_unconfirmed') or not scope.get('memory_allowed') or not channel_scope_valid(db, scope, locked=lock):
            raise HTTPException(409, '候选渠道来源或记忆授权已失效')
    return source


def source_digest(candidate, source):
    return digest({'candidate': {key: getattr(candidate, key) for key in
        ('id', 'source_id', 'fingerprint', 'content', 'subject', 'category', 'evidence', 'reason', 'origin')},
        'source': {'id': source.id, 'content': source.content, 'run_id': source.run_id}})


def target_digest(target):
    return digest({key: getattr(target, key) for key in ('id', 'version', 'content', 'subject', 'category',
        'event_at', 'valid_from', 'valid_to', 'expires_at', 'source_ids', 'evidence', 'provenance', 'status')})


def related_query(user, candidate):
    return select(Memory).where(Memory.workspace_id == user.workspace_id, Memory.user_id == user.id,
        Memory.subject == candidate.subject, Memory.category == candidate.category, Memory.status == 'active',
        (Memory.expires_at.is_(None) | (Memory.expires_at > now())))


def sources(db, candidate, user, query=''):
    source = dialogue_source(db, candidate, user)
    base = related_query(user, candidate)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    if query:
        base = base.where(Memory.content.icontains(query, autoescape=True))
    count = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = list(db.scalars(base.order_by(Memory.created_at.desc(), Memory.id.desc()).limit(21)))
    # Derived sources are revalidated as well; never expose stale material as a choice.
    items = [view(row) for row in rows[:20] if usable(row)]
    return dict(candidate=view(candidate), source={'id': source.id, 'content': source.content,
        'created_at': source.created_at, 'conversation_id': source.conversation_id}, related=items,
        related_count=total, matched_count=count, truncated=len(rows) > 20,
        warning='同主体、同类别仅表示可能相关，不代表事实矛盾。请核对原文中的引用、假设和临时例外；'
            '新增可保留不同期间的事实，纠正仅用于你明确判断错误或过时的一项。')


def target_for(db, candidate, user, body, lock=False):
    if body.resolution == 'keep':
        return None
    target = owned(db, Memory, body.target_memory_id, user, lock=lock)
    db.refresh(target)
    if (not usable(target) or target.version != body.target_version or target.category != candidate.category
            or target.subject != candidate.subject or candidate.source_id in target.source_ids):
        raise HTTPException(409, '旧项已变化、失效、主体类别不同或共用本条原文，请重新选择；共用原文可新增保存')
    return target


def preview(db, candidate, user, session, body):
    source = dialogue_source(db, candidate, user)
    if candidate.status != 'pending' or candidate.subject.strip() in ('unknown', ''):
        raise HTTPException(409, '候选已处理或主体不明；主体不明时请拒绝后手动记录')
    target = target_for(db, candidate, user, body)
    after = MemoryInput(content=candidate.content, subject=candidate.subject, category=candidate.category,
        event_at=body.event_at, valid_from=body.valid_from, valid_to=body.valid_to, version=target.version+1 if target else 1)
    expiry = now()+300
    token = encrypt(json.dumps({'purpose': 'dialogue-memory-review', 'user': user.id,
        'workspace': user.workspace_id, 'session': session.id, 'candidate': candidate.id,
        'source_digest': source_digest(candidate, source), 'choice': body.model_dump(),
        'target_digest': target_digest(target) if target else None, 'expires_at': expiry}, sort_keys=True))
    return dict(candidate_id=candidate.id, resolution=body.resolution, before=view(target) if target else None,
        after=after, token=token, expires_at=expiry,
        warning='确认只处理当前候选，不改变自动保存设置。新事实的时间默认未说明，不继承旧项日期。'
            + ('纠正会保留旧版本，并使使用旧项的回答、衍生记录和待发送内容失效；其他记忆不变。' if target else
                '新增不会覆盖或结束已有记录；临时例外请明确标注期间。'))


def confirm(db, candidate, user, session, token):
    try:
        data = json.loads(decrypt(token or ''))
        assert data['purpose'] == 'dialogue-memory-review' and data['user'] == user.id
        assert data['workspace'] == user.workspace_id and data['session'] == session.id and data['candidate'] == candidate.id
        assert now() <= data['expires_at'] <= now()+301
        body = CandidateReviewInput.model_validate(data['choice'])
    except (InvalidToken, ValueError, KeyError, TypeError, AssertionError):
        raise HTTPException(409, '核对预览已过期或不属于当前登录，请重新核对') from None
    # Serialize against conversation deletion before locking the target, source and candidate.
    # Otherwise deletion could scan linked memories just before this confirmation adds one.
    source = dialogue_source(db, candidate, user, lock=True)
    owned(db, Conversation, source.conversation_id, user, lock=True)
    dialogue_source(db, candidate, user)
    target = owned(db, Memory, body.target_memory_id, user, lock=True) if body.resolution == 'replace' else None
    source = db.scalar(select(Message).where(Message.id == candidate.source_id).with_for_update().execution_options(populate_existing=True))
    candidate = owned(db, Candidate, candidate.id, user, lock=True)
    db.refresh(candidate)
    # A retry reuses the original receipt; it never creates another memory/version.
    if candidate.status != 'pending':
        return candidate
    dialogue_source(db, candidate, user)
    target_for(db, candidate, user, body)
    if candidate.subject.strip() in ('unknown', '') or data['source_digest'] != source_digest(candidate, source):
        raise HTTPException(409, '候选内容或原文已变化，请重新核对')
    if target and data['target_digest'] != target_digest(target):
        raise HTTPException(409, '旧项在预览后发生变化，请重新核对')
    fields = {'content': candidate.content, 'subject': candidate.subject, 'category': candidate.category,
        'event_at': body.event_at, 'valid_from': body.valid_from, 'valid_to': body.valid_to}
    if target:
        MemoryService().correct(db, target, fields)
        target.source_ids = target.source_ids + [candidate.source_id, 'user-confirmed:'+candidate.id]
        target.evidence = 'user_confirmed'
        memory = target
    else:
        memory = MemoryService().remember(db, user.id, fields.pop('content'), fields.pop('category'),
            [candidate.source_id, 'user-confirmed:'+candidate.id], workspace_id=user.workspace_id,
            evidence='user_confirmed', **fields)
    candidate.memory_id, candidate.status = memory.id, 'confirmed'
    return candidate
