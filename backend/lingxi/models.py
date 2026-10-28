from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, uid, now


class Entity:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(String(64), default='personal', index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class User(Entity, Base):
    __tablename__ = 'users'
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    auto_memory: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(Entity, Base):
    __tablename__ = 'login_sessions'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(Float)


class Provider(Entity, Base):
    __tablename__ = 'providers'
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str] = mapped_column(Text)
    encrypted_key: Mapped[str] = mapped_column(Text)
    allow_private: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Model(Entity, Base):
    __tablename__ = 'models'
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(150))
    purpose: Mapped[str] = mapped_column(String(30), default='chat')
    context_window: Mapped[int] = mapped_column(Integer, default=8192)
    status: Mapped[str] = mapped_column(String(30), default='unverified')
    verified_provider_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Policy(Base):
    __tablename__ = 'policies'
    purpose: Mapped[str] = mapped_column(String(30), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)


class Conversation(Entity, Base):
    __tablename__ = 'conversations'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200), default='新的对话')
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(Entity, Base):
    __tablename__ = 'messages'
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='completed')
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)


class ChatRun(Entity, Base):
    __tablename__ = 'chat_runs'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default='queued')
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    model_id: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    memory_refs: Mapped[list] = mapped_column(JSON, default=list)
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    memory_status: Mapped[str] = mapped_column(String(30), default='pending')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_seq: Mapped[int] = mapped_column(Integer, default=0)


class RunEvent(Base):
    __tablename__ = 'run_events'
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON)


class Memory(Entity, Base):
    __tablename__ = 'memories'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(150), default='self')
    category: Mapped[str] = mapped_column(String(30), default='preferences')
    content: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[str] = mapped_column(String(30), default='explicit')
    event_at: Mapped[str | None] = mapped_column(String(100), nullable=True)
    valid_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    valid_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='active')
    version: Mapped[int] = mapped_column(Integer, default=1)
    index_status: Mapped[str] = mapped_column(String(30), default='pending')
    index_epoch: Mapped[int] = mapped_column(Integer, default=0)
    revisions: Mapped[list] = mapped_column(JSON, default=list)


class Candidate(Entity, Base):
    __tablename__ = 'memory_candidates'
    __table_args__ = (UniqueConstraint('source_id', 'fingerprint'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(150), default='self')
    evidence: Mapped[str] = mapped_column(String(30), default='inferred')
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    memory_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Operation(Entity, Base):
    __tablename__ = 'memory_operations'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='pending')
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Entity, Base):
    __tablename__ = 'jobs'
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelCall(Entity, Base):
    __tablename__ = 'model_calls'
    model_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    duration_ms: Mapped[int] = mapped_column(Integer)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Audit(Entity, Base):
    __tablename__ = 'audit'
    user_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
