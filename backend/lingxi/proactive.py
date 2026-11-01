"""Grounded proactive Web contact; source checks and limits stay inside the Core."""
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now
from .models import Entity, User, Memory, Conversation, Message, ChatRun
from .tasks import WorkTask
from .web import Context, owned, view, audit
from .proactive_channels import ContactTarget, DeliveryView
from .proactive_generation import DraftView
from .proactive_voice import VoiceContactView

KINDS = ('follow_up', 'suggestion', 'interest')
router = APIRouter()


class ContactPreference(Base):
    __tablename__ = 'contact_preferences'
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(30), default='companion')
    model_generation: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(100), default='Asia/Shanghai')
    quiet_start: Mapped[str] = mapped_column(String(5), default='22:00')
    quiet_end: Mapped[str] = mapped_column(String(5), default='08:00')
    daily_limit: Mapped[int] = mapped_column(Integer, default=2)
    min_gap_hours: Mapped[int] = mapped_column(Integer, default=3)
    allowed_kinds: Mapped[list] = mapped_column(JSON, default=lambda: list(KINDS))
    paused_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    blocked_sources: Mapped[list] = mapped_column(JSON, default=list)


class ConversationTopic(Entity, Base):
    __tablename__ = 'conversation_topics'
    __table_args__ = (UniqueConstraint('user_id', 'dedupe_key'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    source_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    not_before: Mapped[float] = mapped_column(Float, index=True)
    next_check_at: Mapped[float] = mapped_column(Float, default=0, index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(Text, default='等待合适时机')
    origin: Mapped[str] = mapped_column(String(30), default='user')


class ProactiveContact(Entity, Base):
    __tablename__ = 'proactive_contacts'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    topic_id: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='stored')
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feedback: Mapped[str | None] = mapped_column(String(30), nullable=True)
    feedback_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class PreferenceValues(BaseModel):
    enabled: bool = True
    mode: Literal['companion', 'results_only'] = 'companion'
    model_generation: bool = False
    timezone: str = Field(default='Asia/Shanghai', max_length=100)
    quiet_start: str = Field(default='22:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    quiet_end: str = Field(default='08:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    daily_limit: int = Field(default=2, ge=0, le=10)
    min_gap_hours: int = Field(default=3, ge=1, le=24)
    allowed_kinds: list[Literal['follow_up','suggestion','interest']] = Field(default_factory=lambda: list(KINDS), max_length=3)
    paused_until: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    conversation_id: str | None = None
    blocked_sources: list[str] = Field(default_factory=list, max_length=1000)
    delivery: ContactTarget = Field(default_factory=ContactTarget)

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('使用有效的 IANA 时区，例如 Asia/Shanghai') from None
        return value

    @field_validator('blocked_sources')
    @classmethod
    def blocks(cls, values):
        if any(len(v) > 100 or not v.startswith(('memory:', 'task:', 'plugin_event:')) for v in values):
            raise ValueError('话题屏蔽标识无效')
        return list(dict.fromkeys(values))


class PreferenceUpdate(PreferenceValues):
    version: int = Field(ge=0)
    qq_acknowledged: bool = False


class PreferenceView(PreferenceValues):
    version: int


class TopicInput(BaseModel):
    source_type: Literal['memory','task']
    source_id: str = Field(min_length=1, max_length=64)
    kind: Literal['follow_up','suggestion','interest'] = 'follow_up'
    title: str = Field(min_length=1, max_length=200)
    not_before: float = Field(gt=0, allow_inf_nan=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    conversation_id: str | None = None

    @model_validator(mode='after')
    def times(self):
        if not self.not_before < self.expires_at <= self.not_before + 30*86400:
            raise ValueError('有效期必须晚于适宜时间，且不超过 30 天')
        return self


class TopicView(BaseModel):
    id: str
    title: str
    source_type: str
    source_id: str
    source_version: int
    kind: str
    not_before: float
    expires_at: float
    conversation_id: str | None
    status: str
    version: int
    reason: str
    origin: str
    created_at: float
    generation: DraftView | None = None


class CloseInput(BaseModel):
    version: int = Field(ge=1)
    block_source: bool = True


class GenerationRetry(BaseModel):
    version: int = Field(ge=1)
    draft_version: int = Field(ge=1)
    acknowledgement: Literal['retry_one_model_call']


class TopicUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    not_before: float = Field(gt=0, allow_inf_nan=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    conversation_id: str | None = None


class TopicSourceView(BaseModel):
    source_type: str
    source_id: str
    version: int
    excerpt: str
    truncated: bool


class ContactView(BaseModel):
    voice: VoiceContactView | None = None
    id: str
    topic_id: str
    content: str
    reason: str
    status: str
    conversation_id: str | None
    message_id: str | None
    feedback: str | None
    feedback_at: float | None
    version: int
    created_at: float
    delivery: DeliveryView


class FeedbackInput(BaseModel):
    version: int = Field(ge=1)
    feedback: Literal['helpful','stop_topic','pause','results_only']


class ContinueView(BaseModel):
    conversation_id: str


class TopicPage(BaseModel):
    items: list[TopicView]
    next_cursor: str | None


class ContactPage(BaseModel):
    items: list[ContactView]
    next_cursor: str | None


def lock_owner(db, user):
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def preference(db, user, create=False):
    row = db.get(ContactPreference, user.id, populate_existing=True)
    if row and row.workspace_id != user.workspace_id:
        raise HTTPException(409, '联系偏好不属于当前工作区')
    if not row and create:
        from .system_settings import current
        defaults = PreferenceValues(timezone=current(db, user).config.default_timezone)
        row = ContactPreference(user_id=user.id, workspace_id=user.workspace_id, **defaults.model_dump(exclude={'delivery'}))
        db.add(row)
        db.flush()
    return row


def pref_view(db, user):
    row = preference(db, user)
    from .proactive_channels import target_for
    from .system_settings import current
    return (view(row) if row else PreferenceValues(timezone=current(db, user).config.default_timezone).model_dump() | {'version': 0}) | {'delivery':target_for(db,user)}


def source_key(topic):
    return topic.source_type + ':' + topic.source_id


def source_record(db, topic, lock=False):
    if topic.source_type == 'plugin_event':
        from .plugin_events import PluginEvent, valid_event
        source = db.scalar(select(PluginEvent).where(PluginEvent.subscription_id == topic.source_id,
            PluginEvent.sequence == topic.source_version, PluginEvent.user_id == topic.user_id,
            PluginEvent.workspace_id == topic.workspace_id).execution_options(populate_existing=True))
        owner = db.get(User, topic.user_id)
        return source if owner and valid_event(db, owner, source) else None
    entity = Memory if topic.source_type == 'memory' else WorkTask
    query = select(entity).where(entity.id == topic.source_id, entity.user_id == topic.user_id,
                                 entity.workspace_id == topic.workspace_id)
    source = db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if not source or source.version != topic.source_version:
        return None
    if isinstance(source, Memory):
        from .memory import usable
        if not usable(source) or source.subject != 'self' or source.evidence != 'explicit':
            return None
    elif source.status != 'succeeded' or source.skill_id != 'project.plan' or not source.result.get('steps'):
        return None
    return source


def automatic_topic(db, source):
    """One grounded follow-up per fact/plan version, no inferred outcomes or emotions."""
    if isinstance(source, Memory):
        if source.category not in ('projects', 'procedures') or source.subject != 'self' or source.evidence != 'explicit' or source.status != 'active':
            return
        source_type, kind, title = 'memory', 'follow_up', '接续项目'
    else:
        if source.skill_id != 'project.plan' or source.status != 'succeeded':
            return
        source_type, kind, title = 'task', 'suggestion', '计划建议'
    owner = db.get(User, source.user_id)
    if not owner or owner.workspace_id != source.workspace_id:
        return
    lock_owner(db, owner)
    prefs = pref_view(db, owner)
    if not prefs['enabled'] or prefs['mode'] == 'results_only' or f'{source_type}:{source.id}' in prefs['blocked_sources']:
        return
    key = f'{source_type}:{source.id}:{source.version}'
    if db.scalar(select(ConversationTopic.id).where(ConversationTopic.user_id == owner.id,
                                                   ConversationTopic.dedupe_key == key)):
        return
    db.add(ConversationTopic(user_id=owner.id, workspace_id=owner.workspace_id, dedupe_key=key,
        source_type=source_type, source_id=source.id, source_version=source.version, title=title,
        kind=kind, not_before=now()+86400, expires_at=now()+7*86400, origin='core'))


def invalidate_topics(db, source_type=None, source_id=None, conversation_id=None, source_version=None):
    assert source_type or conversation_id
    query = select(ConversationTopic)
    if source_type:
        query = query.where(ConversationTopic.source_type == source_type, ConversationTopic.source_id == source_id)
    if conversation_id:
        query = query.where((ConversationTopic.conversation_id == conversation_id) | ConversationTopic.id.in_(
            select(ProactiveContact.topic_id).where(ProactiveContact.conversation_id == conversation_id)))
    if source_version is not None:
        query = query.where(ConversationTopic.source_version == source_version)
    for topic in db.scalars(query.with_for_update().execution_options(populate_existing=True)):
        from .proactive_generation import invalidate as invalidate_draft
        invalidate_draft(db, topic_id=topic.id, reason='来源已纠正、删除或失效')
        topic.status, topic.reason, topic.title = 'invalidated', '来源已纠正、删除或失效', '来源失效'
        topic.version += 1
        contact = db.scalar(select(ProactiveContact).where(ProactiveContact.topic_id == topic.id).with_for_update())
        if contact:
            contact.status, contact.content, contact.reason = 'invalidated', '', topic.reason
            contact.version += 1
            from .proactive_channels import invalidate
            invalidate(db,contact_id=contact.id)
            if contact.message_id:
                message = db.get(Message, contact.message_id)
                if message:
                    message.content, message.status = '', 'invalidated'
                    from .memory import invalidate_source
                    invalidate_source(db, message.id)


def quiet(prefs, instant):
    local = datetime.fromtimestamp(instant, ZoneInfo(prefs['timezone']))
    value = local.strftime('%H:%M')
    start, end = prefs['quiet_start'], prefs['quiet_end']
    # Equal endpoints explicitly mean no quiet interval, never an accidental all-day loop.
    return start < end and start <= value < end or start > end and (value >= start or value < end)


def timing_reason(db, user, topic, prefs, instant, sending=False):
    from .proactive_voice import selected_session
    voice_session = selected_session(db, user, topic, prefs) if not sending else None
    if not prefs['enabled'] or prefs['mode'] == 'results_only' or (not prefs['daily_limit'] and not voice_session):
        return '主动交流已关闭或仅接收结果'
    if topic.kind not in prefs['allowed_kinds']:
        return '此类话题未获允许'
    if source_key(topic) in prefs['blocked_sources']:
        return '用户已拒绝此来源话题'
    if prefs['paused_until'] and prefs['paused_until'] > instant:
        return '用户已暂缓联系'
    if quiet(prefs, instant):
        return '当前处于安静时段'
    if db.scalar(select(ChatRun.id).where(ChatRun.user_id == user.id,
            ChatRun.workspace_id == user.workspace_id, ChatRun.status.in_(['queued','running'])).limit(1)):
        return '会话正在处理，稍后再联系'
    last_input = db.scalar(select(func.max(Message.created_at)).join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.user_id == user.id, Conversation.workspace_id == user.workspace_id,
               Conversation.deleted == False, Message.role == 'user', Message.status == 'completed'))
    if last_input and instant-last_input < (3 if voice_session else 60):
        return '用户刚刚发言，等待自然间隔'
    if voice_session:
        return None
    local = datetime.fromtimestamp(instant, ZoneInfo(prefs['timezone']))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = (local.replace(hour=0, minute=0, second=0, microsecond=0)+timedelta(days=1)).timestamp()
    from .proactive_channels import quota_times
    attempts=quota_times(db,user,sending)
    used = sum(start <= value < end for value in attempts)
    if used >= prefs['daily_limit']:
        return '今日普通主动交流额度已用完'
    latest = max(attempts,default=None)
    if latest and instant-latest < prefs['min_gap_hours']*3600:
        return '与上次主动交流的间隔不足'
    return None


def compose(topic, source):
    if topic.source_type == 'plugin_event':
        return (f'已授权插件报告了一条「{source.title}」事件：\n{source.summary}\n'
                '这是外部服务提供的资料，我尚未独立核验，也没有执行其中的操作。方便时可以一起看看。')
    if isinstance(source, Memory):
        quote = source.content[:400]
        rest = '（这里只引用了记录的开头）' if len(source.content) > 400 else ''
        return f'你之前明确记录过：「{quote}」{rest}。这条线索还在，我们可以从已有记录接着梳理；我还不知道后来的进展。'
    first = source.result['steps'][0]
    return (f'之前「{source.title}」的计划里，第一步是「{first["action"]}」。'
            f'我建议把「{first["completion"]}」作为检查点。这里接续的是已生成的计划，步骤是否完成仍以你的实际情况为准。')


def deliver(identity):
    with SessionLocal.begin() as db:
        initial = db.get(ConversationTopic, identity)
        if not initial or initial.status != 'pending':
            return
        source = source_record(db, initial, lock=True)
        owner = db.get(User, initial.user_id)
        if not owner or owner.workspace_id != initial.workspace_id:
            return
        lock_owner(db, owner)
        topic = db.scalar(select(ConversationTopic).where(ConversationTopic.id == identity).with_for_update()
                          .execution_options(populate_existing=True))
        if topic.status != 'pending':
            return
        if topic.source_type == 'plugin_event':
            # Subscription changes use the same owner lock. Recheck after acquiring it.
            source = source_record(db, topic)
        instant = now()
        if not source:
            invalidate_topics(db, topic.source_type, topic.source_id, source_version=topic.source_version)
            return
        if topic.expires_at <= instant:
            topic.status, topic.reason, topic.version = 'expired', '话题已过期，不再补发', topic.version+1
            return
        if topic.not_before > instant:
            return
        prefs = pref_view(db, owner)
        reason = timing_reason(db, owner, topic, prefs, instant)
        if not reason:
            from .proactive_channels import route_ready
            try:reason=route_ready(db,owner,prefs['delivery'])
            except HTTPException:reason='联系目标授权已变化，请重新核对；不会自动改投 Web 或其他账号'
        if reason:
            topic.next_check_at = instant+60
            if topic.reason != reason:
                topic.reason, topic.version = reason, topic.version+1
            return
        if db.scalar(select(ProactiveContact.id).where(ProactiveContact.topic_id == topic.id)):
            return
        from .proactive_channels import ProactiveDelivery
        if db.scalar(select(ProactiveDelivery.id).where(ProactiveDelivery.topic_id==topic.id)):
            topic.status,topic.reason,topic.version='contacted','已有投递屏障，不重放旧备份中的主动联系',topic.version+1
            return
        from .proactive_voice import selected_session, register
        voice_session = selected_session(db, owner, topic, prefs)
        conversation_id = prefs['conversation_id'] or topic.conversation_id or (voice_session.conversation_id if voice_session else None)
        conversation = None
        if conversation_id and prefs['delivery']['kind']=='web':
            conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id,
                Conversation.user_id == owner.id, Conversation.workspace_id == owner.workspace_id,
                Conversation.deleted == False).with_for_update())
            if not conversation:
                topic.status, topic.reason, topic.version = 'closed', '授权目标会话已删除；不擅自切换接收目标', topic.version+1
                return
        if prefs['model_generation']:
            from .proactive_generation import prepare
            draft = prepare(db, owner, topic, prefs, source)
            if not draft or draft.status != 'ready':
                topic.next_check_at = instant+60
                reason = {'queued':'已排队构思，尚未生成或投递', 'running':'正在构思，尚未投递',
                    'deferred':'开始构思前时机变化，尚未调用模型；等待合适时机',
                    'failed':'构思失败，未自动重试；可核对后重试',
                    'invalidated':'构思来源或授权已变化，请核对后重试'}.get(draft.status if draft else '', '自动构思预算或并发已占用，等待后续时机')
                if topic.reason != reason:
                    topic.reason, topic.version = reason, topic.version+1
                return
            content = draft.content
        else:
            content = compose(topic, source)
        source_name = {'memory': '项目记忆', 'task': '已生成计划', 'plugin_event': '已授权插件事件'}[topic.source_type]
        reason = f'{source_name}仍有效，话题到时且通过安静时段、联系偏好与额度检查。来源版本：{topic.source_version}。'
        if voice_session:
            reason = (f'{source_name}仍有效，话题到时且通过联系偏好、安静时段和当前语音会话授权检查；'
                      f'不占会话外普通联系额度，播放仍需自然停顿与有效回执。来源版本：{topic.source_version}。')
        if prefs['model_generation']:
            reason += f'模型构思：{draft.config["model"]}，原文引用匹配；建议未被独立事实核验。'
        message = None
        if conversation:
            topic.conversation_id = conversation.id
            message = Message(workspace_id=owner.workspace_id, conversation_id=conversation.id,
                              role='assistant', content=content)
            db.add(message)
            db.flush()
        contact=ProactiveContact(workspace_id=owner.workspace_id, user_id=owner.id, topic_id=topic.id,
            content=content, reason=reason, conversation_id=conversation.id if conversation else None,
            message_id=message.id if message else None)
        db.add(contact);db.flush()
        if voice_session:
            register(db, owner, topic, contact, voice_session, prefs)
        from .proactive_channels import record_delivery
        record_delivery(db,owner,topic,contact,prefs['delivery'])
        topic.status, topic.reason, topic.version = 'contacted', ('已排队等待授权 QQ 私聊投递；尚无发送回执' if prefs['delivery']['kind']=='qq' else '已存站内；不等于已读或推送送达'), topic.version+1


def tick():
    from .proactive_channels import expire_pending
    expire_pending()
    from .plugin_events import expire_events
    expire_events()
    with SessionLocal() as db:
        identities = list(db.scalars(select(ConversationTopic.id).where(ConversationTopic.status == 'pending',
            ConversationTopic.not_before <= now(), ConversationTopic.next_check_at <= now())
            .order_by(ConversationTopic.next_check_at, ConversationTopic.not_before).limit(100)))
    for identity in identities:
        try:
            deliver(identity)
        except Exception as exc:
            import logging
            from .security import safe_error
            logging.error('Proactive check failed: %s', safe_error(exc))


def valid_contact(db, contact):
    topic = db.get(ConversationTopic, contact.topic_id)
    return contact.status != 'invalidated' and topic and topic.status != 'invalidated' and source_record(db, topic)


def contact_view(db, row):
    from .proactive_channels import delivery_view
    from .proactive_voice import public_entry
    values = view(row) | {'delivery':delivery_view(db,row), 'voice':public_entry(db,row)}
    if not valid_contact(db, row):
        values |= {'status': 'invalidated', 'content': '', 'reason': '来源已失效，不再展示原内容'}
    return values


def apply_feedback(db, user, contact, action):
    prefs = preference(db, user, create=True)
    if action == 'stop_topic':
        topic = db.get(ConversationTopic, contact.topic_id)
        prefs.blocked_sources = list(dict.fromkeys(prefs.blocked_sources+[source_key(topic)]))
        topic.status, topic.reason, topic.version = 'closed', '用户明确拒绝后续交流', topic.version+1
    elif action == 'pause':
        prefs.paused_until = now()+86400
    elif action == 'results_only':
        prefs.mode = 'results_only'
    if action!='helpful':
        from .proactive_channels import invalidate
        invalidate(db,**({'contact_id':contact.id} if action=='stop_topic' else {'user_id':user.id}),reason='用户明确拒绝或暂缓主动交流')
    prefs.version += 1
    contact.feedback, contact.feedback_at, contact.version = action, now(), contact.version+1


def chat_control(db, user, conversation, text):
    command = text.strip().rstrip('。！!，,').strip()
    actions = {'先别聊':'pause', '暂停主动交流':'disable', '以后别主动联系':'disable',
               '直接告诉我结果':'results_only', '这个以后别问':'stop_topic'}
    action = actions.get(command)
    if not action:
        return None
    prefs = preference(db, user, create=True)
    if action == 'stop_topic':
        contact = db.scalar(select(ProactiveContact).where(ProactiveContact.user_id == user.id,
            ProactiveContact.conversation_id == conversation.id, ProactiveContact.status == 'stored')
            .order_by(ProactiveContact.created_at.desc()).with_for_update().limit(1))
        if not contact:
            return '我没有找到这个会话对应的主动话题。可以在“联系记录”中关闭具体话题；我不会猜测你指的是哪件事。'
        apply_feedback(db, user, contact, action)
        return '好，这个来源的话题以后不再主动提起。你仍然可以自己接着聊。'
    if action == 'pause':
        prefs.paused_until = now()+86400
        reply = '好，普通主动交流暂停 24 小时。你设置的提醒和等待中的任务结果仍正常保存。'
    elif action == 'disable':
        prefs.enabled = False
        reply = '已关闭普通主动交流。你可以在“联系偏好”中重新开启；提醒和任务结果继续保留。'
    else:
        prefs.mode = 'results_only'
        reply = '好，接下来只接收你安排的任务结果与提醒，暂停额外的话题交流。'
    prefs.version += 1
    audit(db, user, 'contact_feedback', action)
    from .proactive_channels import invalidate
    invalidate(db,user_id=user.id,reason='用户明确停止或暂缓主动交流')
    return reply


@router.get('/api/contact-preferences', response_model=PreferenceView)
def get_preferences(ctx: Context):
    db, user, _ = ctx
    return pref_view(db, user)


@router.put('/api/contact-preferences', response_model=PreferenceView)
def put_preferences(body: PreferenceUpdate, ctx: Context):
    db, user, _ = ctx
    lock_owner(db, user)
    prior = pref_view(db, user)
    if body.version != prior['version']:
        raise HTTPException(409, '联系偏好已变化，请重新加载后保存')
    if body.conversation_id:
        owned(db, Conversation, body.conversation_id, user)
    if body.delivery.kind=='qq' and body.conversation_id:
        raise HTTPException(422,'QQ 私聊与固定 Web 会话不能同时选为主动目标')
    from .proactive_channels import save_target,invalidate
    save_target(db,user,body.delivery.model_dump(),body.qq_acknowledged)
    invalidate(db,user_id=user.id,reason='联系偏好已修改；旧排队消息不补发')
    row = preference(db, user, create=True)
    for key, value in body.model_dump(exclude={'version','delivery','qq_acknowledged'}).items():
        setattr(row, key, value)
    row.version = prior['version']+1
    audit(db, user, 'contact_preferences', user.id)
    db.flush()
    return pref_view(db,user)


@router.post('/api/conversation-topics', response_model=TopicView)
def create_topic(body: TopicInput, ctx: Context):
    db, user, _ = ctx
    if body.not_before > now()+366*86400 or body.expires_at <= now():
        raise HTTPException(422, '话题必须尚未过期，适宜时间不能超过未来一年')
    entity = Memory if body.source_type == 'memory' else WorkTask
    source = owned(db, entity, body.source_id, user, lock=True)
    topic = ConversationTopic(user_id=user.id, workspace_id=user.workspace_id,
        source_version=source.version, dedupe_key=f'{body.source_type}:{source.id}:{source.version}', **body.model_dump())
    if not source_record(db, topic):
        raise HTTPException(409, '只允许有效的本人明确记忆或已成功的项目计划作为来源')
    lock_owner(db, user)
    if source_key(topic) in pref_view(db, user)['blocked_sources']:
        raise HTTPException(409, '此来源已被屏蔽，请先在联系偏好中明确恢复')
    if body.conversation_id:
        owned(db, Conversation, body.conversation_id, user)
    existing = db.scalar(select(ConversationTopic).where(ConversationTopic.user_id == user.id,
                                                       ConversationTopic.dedupe_key == topic.dedupe_key))
    if existing:
        raise HTTPException(409, '此来源版本已有话题；不会为同一件事重复联系')
    db.add(topic)
    db.flush()
    audit(db, user, 'topic_create', topic.id)
    return view(topic)


@router.get('/api/conversation-topics', response_model=TopicPage)
def list_topics(ctx: Context, q: str = Query('', max_length=200),
                status: Literal['pending','contacted','closed','invalidated','expired'] | None = None,
                cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
    from .task_routes import page_query
    db, user, _ = ctx
    query = select(ConversationTopic)
    if q:
        query = query.where(ConversationTopic.title.icontains(q, autoescape=True))
    if status:
        query = query.where(ConversationTopic.status == status)
    rows, next_cursor = page_query(db, ConversationTopic, user, cursor, query, limit)
    from .proactive_generation import draft_view
    return {'items': [view(row) | {'generation': draft_view(db,row)} for row in rows], 'next_cursor': next_cursor}


@router.get('/api/conversation-topics/{identity}/source', response_model=TopicSourceView)
def topic_source(identity: str, ctx: Context):
    db, user, _ = ctx
    topic = owned(db, ConversationTopic, identity, user)
    source = source_record(db, topic)
    if not source:
        raise HTTPException(409, '来源已失效，不再展示旧内容')
    text = (source.title + '\n' + source.summary if topic.source_type == 'plugin_event' else
            source.content if isinstance(source, Memory) else str(source.result.get('summary', source.title)))
    return {'source_type':topic.source_type, 'source_id':topic.source_id, 'version':topic.source_version,
            'excerpt':text[:400], 'truncated':len(text)>400}


@router.put('/api/conversation-topics/{identity}', response_model=TopicView)
def update_topic(identity: str, body: TopicUpdate, ctx: Context):
    db, user, _ = ctx
    initial = owned(db, ConversationTopic, identity, user)
    if not source_record(db, initial, lock=True):
        raise HTTPException(409, '来源已失效，请重新整理话题')
    lock_owner(db, user)
    topic = owned(db, ConversationTopic, identity, user, lock=True)
    if body.version != topic.version or topic.status != 'pending':
        raise HTTPException(409, '话题状态已变化，只能修改等待中的话题')
    if not now() < body.expires_at <= body.not_before+30*86400 or body.not_before >= body.expires_at or body.not_before > now()+366*86400:
        raise HTTPException(422, '检查适宜时间和有效期：到期晚于适宜时间且间隔不超过 30 天')
    if body.conversation_id:
        owned(db, Conversation, body.conversation_id, user)
    from .proactive_generation import invalidate as invalidate_draft
    invalidate_draft(db, topic_id=topic.id, reason='用户修改了话题安排')
    for key, value in body.model_dump(exclude={'version'}).items():
        setattr(topic, key, value)
    topic.next_check_at, topic.reason, topic.version = 0, '用户修改安排，等待重新判断时机', topic.version+1
    audit(db, user, 'topic_update', topic.id)
    return view(topic)


@router.post('/api/conversation-topics/{identity}/generation-retry', response_model=DraftView)
def retry_generation(identity: str, body: GenerationRetry, ctx: Context):
    from .proactive_generation import ProactiveDraft, prepare, draft_view
    db, user, _ = ctx
    initial = owned(db, ConversationTopic, identity, user)
    source = source_record(db, initial, lock=True)
    if not source:
        raise HTTPException(409, '来源已失效，不能重试旧话题')
    lock_owner(db, user)
    topic = owned(db, ConversationTopic, identity, user, lock=True)
    draft = db.scalar(select(ProactiveDraft).where(ProactiveDraft.topic_id==topic.id).with_for_update())
    prefs = pref_view(db, user)
    if (topic.version != body.version or topic.status != 'pending' or topic.expires_at <= now()
            or not draft or draft.version != body.draft_version or draft.status not in ('failed','invalidated')
            or not prefs['model_generation'] or not prefs['enabled'] or prefs['mode'] != 'companion'
            or source_key(topic) in prefs['blocked_sources']):
        raise HTTPException(409, '构思状态或联系授权已变化，请重新加载后核对')
    prepare(db,user,topic,prefs,source,restart=True)
    topic.next_check_at, topic.version = 0, topic.version+1
    audit(db,user,'proactive_generation_retry',topic.id)
    db.flush()
    return draft_view(db,topic)


@router.post('/api/conversation-topics/{identity}/close', response_model=TopicView)
def close_topic(identity: str, body: CloseInput, ctx: Context):
    db, user, _ = ctx
    lock_owner(db, user)
    topic = owned(db, ConversationTopic, identity, user, lock=True)
    if topic.version != body.version:
        raise HTTPException(409, '话题已变化，请重新加载后操作')
    if body.block_source:
        prefs = preference(db, user, create=True)
        prefs.blocked_sources = list(dict.fromkeys(prefs.blocked_sources+[source_key(topic)]))
        prefs.version += 1
    topic.status, topic.reason, topic.version = 'closed', '用户关闭话题，不再补发', topic.version+1
    from .proactive_generation import invalidate as invalidate_draft
    invalidate_draft(db, topic_id=topic.id, reason='用户关闭了话题')
    from .proactive_channels import invalidate
    contact=db.scalar(select(ProactiveContact).where(ProactiveContact.topic_id==topic.id))
    if contact:invalidate(db,contact_id=contact.id,reason='用户已关闭话题')
    audit(db, user, 'topic_close', topic.id)
    return view(topic)


@router.get('/api/proactive-contacts', response_model=ContactPage)
def list_contacts(ctx: Context, cursor: str | None = None, limit: int = Query(50, ge=1, le=100), unreviewed: bool = False):
    from .task_routes import page_query
    db, user, _ = ctx
    query = select(ProactiveContact)
    if unreviewed:
        from .proactive_channels import ProactiveDelivery
        query=query.where(~ProactiveContact.id.in_(select(ProactiveDelivery.contact_id).where(ProactiveDelivery.kind=='qq')))
        prefs = pref_view(db, user)
        if not prefs['enabled'] or not prefs['daily_limit'] or prefs['mode'] == 'results_only' or (prefs['paused_until'] and prefs['paused_until'] > now()) or quiet(prefs, now()):
            return {'items': [], 'next_cursor': None}
        query = query.join(ConversationTopic, ConversationTopic.id == ProactiveContact.topic_id).where(
            ProactiveContact.feedback == None, ProactiveContact.status == 'stored',
            ConversationTopic.expires_at > now(), ConversationTopic.kind.in_(prefs['allowed_kinds']),
            ~(ConversationTopic.source_type+':'+ConversationTopic.source_id).in_(prefs['blocked_sources']))
    rows, next_cursor = page_query(db, ProactiveContact, user, cursor, query, limit)
    return {'items': [contact_view(db, row) for row in rows], 'next_cursor': next_cursor}


@router.post('/api/proactive-contacts/{identity}/feedback', response_model=ContactView)
def feedback(identity: str, body: FeedbackInput, ctx: Context):
    db, user, _ = ctx
    lock_owner(db, user)
    contact = owned(db, ProactiveContact, identity, user, lock=True)
    if contact.version != body.version:
        raise HTTPException(409, '联系记录已变化，请重新加载后操作')
    apply_feedback(db, user, contact, body.feedback)
    audit(db, user, 'proactive_feedback', contact.id)
    return contact_view(db, contact)


@router.post('/api/proactive-contacts/{identity}/continue', response_model=ContinueView)
def continue_contact(identity: str, ctx: Context):
    db, user, _ = ctx
    initial = owned(db, ProactiveContact, identity, user)
    from .proactive_channels import delivery_view
    delivery=delivery_view(db,initial)
    if delivery['kind']=='qq' and (delivery['status']!='accepted_by_protocol' or not initial.conversation_id):
        raise HTTPException(409,'QQ 尚无有效的协议接受回执；不会自动改为 Web 接续或重发')
    topic = db.get(ConversationTopic, initial.topic_id)
    if not topic or not source_record(db, topic, lock=True):
        raise HTTPException(409, '来源已失效，请重新整理后继续')
    lock_owner(db, user)
    contact = owned(db, ProactiveContact, identity, user, lock=True)
    if not valid_contact(db, contact):
        raise HTTPException(409, '来源已失效，请重新整理后继续')
    if contact.conversation_id:
        owned(db, Conversation, contact.conversation_id, user)
        return {'conversation_id': contact.conversation_id}
    conversation = Conversation(user_id=user.id, workspace_id=user.workspace_id, title='接续话题：'+topic.title[:180])
    db.add(conversation)
    db.flush()
    message = Message(workspace_id=user.workspace_id, conversation_id=conversation.id,
                      role='assistant', content=contact.content)
    db.add(message)
    db.flush()
    contact.conversation_id, contact.message_id, contact.version = conversation.id, message.id, contact.version+1
    # Also bind this created conversation so its deletion invalidates the source-bearing message.
    topic.conversation_id = conversation.id
    audit(db, user, 'proactive_continue', contact.id)
    return {'conversation_id': conversation.id}
