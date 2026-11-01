"""Opt-in browser presentation of an existing contact, never another delivery.

A persisted attempt is consumed before returning to the browser. Lost responses,
browser failures and restored backups must never replay a system notification.
No personal text, service-worker push or background audio crosses this interface.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Float, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import Entity, User, ChatRun, Message, Conversation
from .web import Context, owned, audit, view

router = APIRouter()
FRESHNESS = 120


class BrowserNoticePreference(Base):
    __tablename__ = 'browser_notice_preferences'
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)


class BrowserNotice(Entity, Base):
    __tablename__ = 'browser_notices'
    __table_args__ = (UniqueConstraint('contact_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    contact_id: Mapped[str] = mapped_column(String(64))
    preference_version: Mapped[int] = mapped_column(Integer)
    consent_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default='candidate')
    login_id: Mapped[str] = mapped_column(String(64), default='')
    attempted_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class NoticePreferenceView(BaseModel):
    enabled: bool
    version: int
    scope: str


class NoticePreferenceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    enabled: bool
    acknowledgement: Literal['browser_running_only']


class NoticeReceiptView(BaseModel):
    id: str
    status: str
    created_at: float
    expires_at: float
    attempted_at: float | None
    eligible: bool


class NoticeState(BaseModel):
    preference: NoticePreferenceView
    receipts: list[NoticeReceiptView]
    note: str = '请求展示不等于系统已显示、送达或已读；未知尝试不重发。仅运行中的页面可检查通知，不使用后台推送。'


class NoticeClaimInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consent_version: int = Field(ge=1)
    background: Literal[True]


class NoticeClaimView(BaseModel):
    id: str | None = None
    expires_at: float | None = None
    path: str = '#/proactive/history'


class NoticeAck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['requested', 'failed', 'clicked']


def preference(db, user):
    row = db.get(BrowserNoticePreference, user.id)
    if row and row.workspace_id != user.workspace_id:
        raise HTTPException(409,'通知设置不属于当前工作区')
    return row


def preference_view(db, user):
    import hashlib
    row = preference(db, user)
    return dict(enabled=bool(row and row.enabled), version=row.version if row else 0,
                scope=hashlib.sha256(repr((user.workspace_id,user.id)).encode()).hexdigest()[:24])


def register(db, user, topic, contact, prefs):
    consent = preference(db, user)
    if not consent or not consent.enabled or prefs['delivery']['kind'] != 'web':
        return
    db.add(BrowserNotice(user_id=user.id,workspace_id=user.workspace_id,contact_id=contact.id,
        preference_version=prefs['version'],consent_version=consent.version,
        expires_at=min(topic.expires_at,contact.created_at+FRESHNESS)))


def eligible(db, user, row):
    from .backups import paused
    from .proactive import ProactiveContact, ConversationTopic, pref_view, source_key, quiet, valid_contact
    from .proactive_channels import delivery_view
    consent = preference(db,user)
    contact = db.get(ProactiveContact,row.contact_id,populate_existing=True)
    topic = db.get(ConversationTopic,contact.topic_id,populate_existing=True) if contact else None
    prefs = pref_view(db,user)
    at = now()
    if (not consent or not consent.enabled or consent.version!=row.consent_version or paused(db)
        or row.user_id!=user.id or row.workspace_id!=user.workspace_id or row.expires_at<=at
        or row.status not in ('candidate','attempting','requested','clicked')
        or not contact or contact.user_id!=user.id or contact.workspace_id!=user.workspace_id
        or contact.status!='stored' or contact.feedback is not None or not topic or topic.status!='contacted'
        or topic.user_id!=user.id or topic.workspace_id!=user.workspace_id
        or topic.expires_at<=at or not valid_contact(db,contact)
        or prefs['version']!=row.preference_version or not prefs['enabled'] or prefs['mode']=='results_only'
        or not prefs['daily_limit'] or prefs['delivery']['kind']!='web' or delivery_view(db,contact)['kind']!='web'
        or topic.kind not in prefs['allowed_kinds'] or source_key(topic) in prefs['blocked_sources']
        or prefs['paused_until'] and prefs['paused_until']>at or quiet(prefs,at)):
        return False
    if contact.conversation_id:
        conversation = db.get(Conversation,contact.conversation_id,populate_existing=True)
        if (not conversation or conversation.deleted or conversation.user_id!=user.id
            or conversation.workspace_id!=user.workspace_id):
            return False
    # Do not supplement a voice contact or interrupt a newly active conversation.
    from .proactive_voice import VoiceContact
    if db.scalar(select(VoiceContact.id).where(VoiceContact.contact_id==contact.id).limit(1)):
        return False
    if db.scalar(select(ChatRun.id).where(ChatRun.user_id==user.id,ChatRun.workspace_id==user.workspace_id,
        ChatRun.status.in_(['queued','running'])).limit(1)):
        return False
    return not db.scalar(select(Message.id).join(Conversation,Conversation.id==Message.conversation_id).where(
        Conversation.user_id==user.id,Conversation.workspace_id==user.workspace_id,Conversation.deleted==False,
        Message.role=='user',Message.status=='completed',Message.created_at>contact.created_at).limit(1))


def receipt(db,user,row):
    valid = eligible(db,user,row)
    status = row.status
    if status == 'candidate' and not valid:status = 'expired' if row.expires_at<=now() else 'invalidated'
    return dict(id=row.id,status=status,
        created_at=row.created_at,expires_at=row.expires_at,attempted_at=row.attempted_at,eligible=bool(valid))


@router.get('/api/browser-notifications/state',response_model=NoticeState)
def state(ctx:Context):
    db,user,_=ctx
    rows=db.scalars(select(BrowserNotice).where(BrowserNotice.user_id==user.id,
        BrowserNotice.workspace_id==user.workspace_id).order_by(BrowserNotice.created_at.desc(),BrowserNotice.id).limit(20))
    return dict(preference=preference_view(db,user),receipts=[receipt(db,user,r) for r in rows])


@router.put('/api/browser-notifications/preferences',response_model=NoticePreferenceView)
def save_preferences(body:NoticePreferenceInput,ctx:Context):
    from .proactive import lock_owner
    db,user,_=ctx
    lock_owner(db,user)
    row=preference(db,user)
    if body.version!=(row.version if row else 0):raise HTTPException(409,'通知设置已变化，请重新加载')
    if not row:
        row=BrowserNoticePreference(user_id=user.id,workspace_id=user.workspace_id,enabled=body.enabled)
        db.add(row)
    elif row.enabled!=body.enabled:
        row.enabled=body.enabled;row.version+=1
    db.flush()
    audit(db,user,'browser_notice_consent',user.id)
    return preference_view(db,user)


@router.post('/api/browser-notifications/claims',response_model=NoticeClaimView)
def claim(body:NoticeClaimInput,ctx:Context):
    from .proactive import lock_owner
    db,user,login=ctx
    lock_owner(db,user)
    consent=preference(db,user)
    if not consent or not consent.enabled or consent.version!=body.consent_version:return {}
    rows=list(db.scalars(select(BrowserNotice).where(BrowserNotice.user_id==user.id,
        BrowserNotice.workspace_id==user.workspace_id,BrowserNotice.status=='candidate',BrowserNotice.expires_at>now())
        .order_by(BrowserNotice.created_at.desc(),BrowserNotice.id).limit(20)))
    for row in rows:
        if not eligible(db,user,row):
            row.status='invalidated';continue
        row.status,row.login_id,row.attempted_at='attempting',login.id,now()
        db.flush()
        # Context commits this intent before HTTP returns. Losing the reply does not retry it.
        return dict(id=row.id,expires_at=row.expires_at)
    return {}


@router.post('/api/browser-notifications/{identity}/ack',response_model=NoticeReceiptView)
def acknowledge(identity:str,body:NoticeAck,ctx:Context):
    from .proactive import lock_owner
    db,user,login=ctx
    lock_owner(db,user)
    row=owned(db,BrowserNotice,identity,user,lock=True)
    if row.login_id!=login.id:raise HTTPException(403,'通知尝试绑定原登录会话')
    if not eligible(db,user,row):return receipt(db,user,row)
    if row.status=='attempting' or row.status=='requested' and body.status in ('failed','clicked'):
        row.status=body.status
    return receipt(db,user,row)


def checkpoint(db):
    return dict(preferences=[view(r) for r in db.scalars(select(BrowserNoticePreference))],
                receipts=[view(r) for r in db.scalars(select(BrowserNotice))])


def restore(db,data):
    # Current receipts survive an older snapshot, but browser consent never does.
    for values in data.get('receipts',[]):
        db.merge(BrowserNotice(**(values|{'status':'invalidated' if values['status']=='candidate' else values['status'],'login_id':''})))
    for values in data.get('preferences',[]):db.merge(BrowserNoticePreference(**values))
    db.flush()
    for row in db.scalars(select(BrowserNoticePreference)):row.enabled=False;row.version+=1
    for row in db.scalars(select(BrowserNotice)):
        if row.status=='candidate':row.status='invalidated'
        row.login_id=''
