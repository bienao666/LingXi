"""Explicit topic completion, independent of memory facts and contact preferences."""
import hashlib
import json
from math import isfinite

from fastapi import HTTPException
from sqlalchemy import Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import Entity


class TopicCompletion(Entity, Base):
    __tablename__ = 'topic_completions'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer)
    topic_id: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[float] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)


def key(user, source_type, source_id, source_version=0):
    # Plugin source IDs identify a subscription, not an individual event.
    scope = source_version if source_type == 'plugin_event' else 0
    return hashlib.sha256(json.dumps([user.id, user.workspace_id, source_type, source_id, scope]).encode()).hexdigest()


def completed(db, user, source_type, source_id, source_version=0):
    row = db.get(TopicCompletion, key(user, source_type, source_id, source_version))
    return bool(row and row.user_id == user.id and row.workspace_id == user.workspace_id
                and row.source_type == source_type and row.source_id == source_id)


def finish(db, topic):
    from .proactive import ProactiveContact
    from .proactive_generation import invalidate as invalidate_draft
    from .proactive_channels import invalidate
    topic.status, topic.reason, topic.version = 'resolved', '用户已结束这件事，不再自动跟进', topic.version + 1
    invalidate_draft(db, topic_id=topic.id, reason='用户已结束话题')
    contact = db.scalar(select(ProactiveContact).where(ProactiveContact.topic_id == topic.id).with_for_update())
    if contact:
        # Keep the conversation and factual delivery receipts. A send already
        # dispatched may still be accepted; it is never claimed to be recalled.
        contact.feedback, contact.feedback_at, contact.version = 'resolved', now(), contact.version + 1
        invalidate(db, contact_id=contact.id, reason='用户已结束话题')


def resolve(db, user, topic):
    """Caller holds the owner lock. No model, task completion or memory write."""
    from .proactive import ConversationTopic, source_record
    if (topic.user_id != user.id or topic.workspace_id != user.workspace_id
            or topic.status not in ('pending', 'contacted') or not source_record(db, topic)):
        raise HTTPException(409, '话题已结束或来源失效，请重新加载后核对')
    identity = key(user, topic.source_type, topic.source_id, topic.source_version)
    row = db.get(TopicCompletion, identity)
    if row and (row.user_id != user.id or row.workspace_id != user.workspace_id):
        raise HTTPException(409, '话题收尾记录归属不符')
    if not row:
        at = now()
        row = TopicCompletion(id=identity, user_id=user.id, workspace_id=user.workspace_id,
            source_type=topic.source_type, source_id=topic.source_id, source_version=topic.source_version,
            topic_id=topic.id, created_at=at, completed_at=at)
        db.add(row)
    else:
        row.topic_id, row.source_version, row.completed_at, row.version = topic.id, topic.source_version, now(), row.version + 1
    db.flush()
    candidates = select(ConversationTopic).where(ConversationTopic.user_id == user.id,
            ConversationTopic.workspace_id == user.workspace_id, ConversationTopic.source_type == topic.source_type,
            ConversationTopic.source_id == topic.source_id, ConversationTopic.status.in_(['pending', 'contacted']),
            ConversationTopic.created_at <= row.completed_at)
    if topic.source_type == 'plugin_event':
        candidates = candidates.where(ConversationTopic.source_version == topic.source_version)
    for item in db.scalars(candidates.with_for_update()):
        finish(db, item)


def checkpoint(db):
    from .web import view
    return [view(row) for row in db.scalars(select(TopicCompletion))]


def validate_checkpoint(rows, user):
    from types import SimpleNamespace
    owner = SimpleNamespace(**user)
    if not isinstance(rows, list):
        raise ValueError('Invalid topic completion checkpoint')
    seen = set()
    expected = {column.name for column in TopicCompletion.__table__.columns}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != expected or not isinstance(row['id'], str) or row['id'] in seen
            or row['user_id'] != owner.id or row['workspace_id'] != owner.workspace_id
            or row['source_type'] not in ('memory', 'task', 'plugin_event')
            or not all(isinstance(row[k], str) and 1 <= len(row[k]) <= 64 for k in ('source_id', 'topic_id'))
            or type(row['source_version']) is not int or row['source_version'] < 1
            or row['id'] != key(owner, row['source_type'], row['source_id'], row['source_version'])
            or type(row['version']) is not int or row['version'] < 1
            or any(type(row[k]) not in (int, float) or not isfinite(row[k]) or row[k] <= 0
                   for k in ('created_at', 'completed_at')) or row['completed_at'] < row['created_at']):
            raise ValueError('Invalid topic completion checkpoint')
        seen.add(row['id'])


def restore(db, rows):
    from .proactive import ConversationTopic
    for values in rows:
        existing = db.get(TopicCompletion, values['id'])
        if not existing or existing.completed_at < values['completed_at']:
            db.merge(TopicCompletion(**values))
    db.flush()
    for record in db.scalars(select(TopicCompletion)):
        candidates = select(ConversationTopic).where(ConversationTopic.user_id == record.user_id,
            ConversationTopic.workspace_id == record.workspace_id, ConversationTopic.source_type == record.source_type,
            ConversationTopic.source_id == record.source_id, ConversationTopic.status.in_(['pending', 'contacted']),
            ConversationTopic.created_at <= record.completed_at)
        if record.source_type == 'plugin_event':
            candidates = candidates.where(ConversationTopic.source_version == record.source_version)
        for topic in db.scalars(candidates):
            finish(db, topic)
