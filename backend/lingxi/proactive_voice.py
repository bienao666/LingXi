"""In-session contact delivery. No autonomous audio outside explicit browser consent."""
import hashlib
from types import SimpleNamespace
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import ChatRun, Conversation, Entity, LoginSession, Message, User
from .voice import (HEARTBEAT_TTL, PlaybackView, VoicePlayback, VoiceSession, active_session,
    checked, config_row, playback_view, speech_parts, speech_snapshot, voice_checked)
from .web import Context, audit, owned, view

router = APIRouter()
FRESHNESS = 120


class VoiceContact(Entity, Base):
    __tablename__ = 'voice_contacts'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    contact_id: Mapped[str] = mapped_column(String(64), unique=True)
    message_id: Mapped[str] = mapped_column(String(64), unique=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    playback_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(30), default='candidate')
    expires_at: Mapped[float] = mapped_column(Float)
    preference_version: Mapped[int] = mapped_column(Integer, default=0)
    speech: Mapped[dict] = mapped_column(JSON, default=dict)


class VoiceContactView(BaseModel):
    status: str
    expires_at: float
    confirmed_parts: int = 0


class ConsentInput(BaseModel):
    enabled: bool


class ClaimInput(BaseModel):
    idle_ms: int = Field(ge=0, le=3600000)


class ClaimView(BaseModel):
    playback: PlaybackView | None = None
    notice: str = ''


def selected_session(db, user, topic, prefs):
    """May prefer the currently open Web conversation, never reroute a QQ target."""
    if prefs['delivery']['kind'] != 'web':
        return None
    target = prefs['conversation_id'] or topic.conversation_id
    query = select(VoiceSession).join(LoginSession, LoginSession.id == VoiceSession.login_session_id).where(
        VoiceSession.user_id == user.id, VoiceSession.workspace_id == user.workspace_id,
        VoiceSession.allow_proactive.is_(True), VoiceSession.status == 'active',
        VoiceSession.expires_at > now(), VoiceSession.heartbeat_at > now()-HEARTBEAT_TTL,
        LoginSession.expires_at > now())
    if target:
        query = query.where(VoiceSession.conversation_id == target)
    row = db.scalar(query.order_by(VoiceSession.created_at.desc()).limit(1))
    config = config_row(db, user)
    if not row or not config or config.version != row.config_version or not config.spec.get('enabled'):
        return None
    conversation = db.get(Conversation, row.conversation_id)
    return row if (conversation and not conversation.deleted and conversation.user_id == user.id and
        conversation.workspace_id == user.workspace_id and checked(db, config, 'tts')) else None


def register(db, user, topic, contact, session, prefs):
    if not contact.message_id:
        return
    db.add(VoiceContact(user_id=user.id, workspace_id=user.workspace_id, contact_id=contact.id,
        message_id=contact.message_id, session_id=session.id, preference_version=prefs['version'],
        expires_at=min(now()+FRESHNESS, topic.expires_at, session.expires_at)))


def invalidate(db, *, contact_id=None, user_id=None, session_id=None):
    query = select(VoiceContact).where(VoiceContact.status.in_(['candidate', 'claimed']))
    if contact_id:
        query = query.where(VoiceContact.contact_id == contact_id)
    if user_id:
        query = query.where(VoiceContact.user_id == user_id)
    if session_id:
        query = query.where(VoiceContact.session_id == session_id)
    for row in db.scalars(query):
        row.status = 'invalidated'
        playback = db.get(VoicePlayback, row.playback_id) if row.playback_id else None
        if playback and playback.status == 'active':
            playback.status = 'stopped'


def entry_for(db, playback):
    return db.scalar(select(VoiceContact).where(VoiceContact.playback_id == playback.id))


def validate(db, entry, user, *, claiming=False):
    from .proactive import ConversationTopic, ProactiveContact, pref_view, quiet, source_key, valid_contact
    contact = db.get(ProactiveContact, entry.contact_id, populate_existing=True)
    topic = db.get(ConversationTopic, contact.topic_id, populate_existing=True) if contact else None
    prefs = pref_view(db, user)
    session = db.get(VoiceSession, entry.session_id, populate_existing=True)
    if (entry.user_id != user.id or entry.workspace_id != user.workspace_id or not session or
        not session.allow_proactive or entry.status not in ('candidate', 'claimed', 'completed', 'stopped') or
        not contact or contact.user_id != user.id or contact.workspace_id != user.workspace_id or
        not topic or topic.status != 'contacted' or not valid_contact(db, contact) or
        contact.message_id != entry.message_id or contact.conversation_id != session.conversation_id or
        not prefs['enabled'] or prefs['mode'] == 'results_only' or prefs['version'] != entry.preference_version or
        prefs['delivery']['kind'] != 'web' or topic.kind not in prefs['allowed_kinds'] or
        source_key(topic) in prefs['blocked_sources'] or prefs['paused_until'] and prefs['paused_until'] > now() or
        quiet(prefs, now()) or topic.expires_at <= now() or claiming and entry.expires_at <= now()):
        raise HTTPException(409, '主动话题已过期、被拒绝或依据变化，不再播报')
    if db.scalar(select(Message.id).where(Message.conversation_id == session.conversation_id,
        Message.workspace_id == user.workspace_id, Message.role == 'user', Message.created_at > entry.created_at).limit(1)):
        raise HTTPException(409, '用户已有新发言，不补播旧话题')
    message = owned(db, Message, entry.message_id, user)
    if message.status != 'completed' or message.role != 'assistant' or message.content != contact.content:
        raise HTTPException(409, '主动消息已变化，不再播报')
    return message


def public_entry(db, contact):
    entry = db.scalar(select(VoiceContact).where(VoiceContact.contact_id == contact.id))
    if not entry:
        return None
    playback = db.get(VoicePlayback, entry.playback_id) if entry.playback_id else None
    status = playback.status if playback and entry.status != 'invalidated' else entry.status
    if status == 'candidate' and entry.expires_at <= now():
        status = 'expired'
    return {'status': status, 'expires_at': entry.expires_at,
        'confirmed_parts': playback.confirmed_parts if playback else 0}


@router.put('/api/voice/sessions/{identity}/proactive', response_model=ConsentInput)
def consent(identity: str, body: ConsentInput, ctx: Context):
    db, user, login = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    session = active_session(db, user, login, identity, True)
    # Old candidates never become eligible again when the switch is re-enabled.
    invalidate(db, session_id=identity)
    session.allow_proactive = body.enabled
    audit(db, user, 'voice_contact_consent', identity)
    return {'enabled': session.allow_proactive}


@router.post('/api/voice/sessions/{identity}/proactive-playback', response_model=ClaimView)
def claim(identity: str, body: ClaimInput, ctx: Context):
    db, user, login = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    session = active_session(db, user, login, identity, True)
    if not session.allow_proactive or body.idle_ms < 3000:
        return {}
    if db.scalar(select(ChatRun.id).where(ChatRun.user_id == user.id,
        ChatRun.workspace_id == user.workspace_id, ChatRun.status.in_(['queued', 'running'])).limit(1)):
        return {}
    if db.scalar(select(VoicePlayback.id).where(VoicePlayback.user_id == user.id,
        VoicePlayback.session_id == identity, VoicePlayback.status == 'active').limit(1)):
        return {}
    candidates = list(db.scalars(select(VoiceContact).where(VoiceContact.session_id == identity,
        VoiceContact.user_id == user.id, VoiceContact.workspace_id == user.workspace_id,
        VoiceContact.status == 'candidate').order_by(VoiceContact.created_at, VoiceContact.id).with_for_update()))
    for entry in candidates:
        if entry.expires_at <= now():
            entry.status = 'expired'
            continue
        try:
            message = validate(db, entry, user, claiming=True)
        except HTTPException:
            entry.status = 'invalidated'
            continue
        # A new utterance has priority; browser availability is an additional
        # timing signal, never an authorization grant.
        latest = db.scalar(select(Message.created_at).where(Message.conversation_id == session.conversation_id,
            Message.workspace_id == user.workspace_id, Message.role == 'user').order_by(Message.created_at.desc()).limit(1))
        if latest and now()-latest < 3:
            return {}
        from .assistants import chat_profile
        config = config_row(db, user)
        try:
            speech = speech_snapshot(config, SimpleNamespace(config={'persona': chat_profile(db,
                owned(db, Conversation, session.conversation_id, user), user)}))
            if not voice_checked(db, config, speech['voice']):
                raise HTTPException(409, '主动语音音色未验证，请阅读文字或验证音色后开启新会话')
            parts = speech_parts(message.content)
            if not parts:
                raise HTTPException(409, '主动消息没有可播报文字')
        except HTTPException as exc:
            entry.status = 'failed'
            return {'notice': str(exc.detail)}
        playback = VoicePlayback(user_id=user.id, workspace_id=user.workspace_id, session_id=identity,
            message_id=message.id, digest=hashlib.sha256(message.content.encode()).hexdigest())
        db.add(playback)
        db.flush()
        entry.playback_id, entry.status, entry.speech = playback.id, 'claimed', speech
        audit(db, user, 'voice_contact_claim', entry.id)
        db.flush()
        return {'playback': playback_view(db, playback, parts)}
    return {}


@router.get('/api/voice/playbacks/{identity}', response_model=PlaybackView)
def playback_status(identity: str, ctx: Context):
    from .voice import playback_source
    db, user, login = ctx
    playback = owned(db, VoicePlayback, identity, user)
    active_session(db, user, login, playback.session_id)
    _, parts = playback_source(db, playback, user)
    return playback_view(db, playback, parts)
