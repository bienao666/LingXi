"""One explicitly authorized private QQ destination; durable sends, never a fallback."""
from typing import Literal
from types import SimpleNamespace
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, Float, String, Text, select, case
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now, uid
from .models import Entity, User, Message
from .channels import Channel, IdentityLink, ChannelInbound, ChannelDelivery
from .web import Context, owned, view, audit

router=APIRouter()


class ContactTarget(BaseModel):
    kind: Literal['web','qq']='web'
    binding_id: str | None=Field(default=None,max_length=64)
    binding_version: int | None=Field(default=None,ge=1)
    channel_version: int | None=Field(default=None,ge=1)

    @model_validator(mode='after')
    def scope(self):
        values=(self.binding_id,self.binding_version,self.channel_version)
        if self.kind=='qq' and not all(values):raise ValueError('QQ 目标必须包含已核对的身份和平台版本')
        if self.kind=='web' and any(v is not None for v in values):raise ValueError('Web 目标不能夹带 QQ 身份')
        return self


class ContactRoute(Base):
    __tablename__='contact_routes'
    user_id: Mapped[str]=mapped_column(String(64),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(64))
    target: Mapped[dict]=mapped_column(JSON)


class ProactiveDelivery(Entity,Base):
    __tablename__='proactive_deliveries'
    user_id: Mapped[str]=mapped_column(String(64),index=True)
    contact_id: Mapped[str]=mapped_column(String(64),unique=True)
    topic_id: Mapped[str]=mapped_column(String(64),unique=True)
    kind: Mapped[str]=mapped_column(String(10))
    channel_id: Mapped[str | None]=mapped_column(String(64),nullable=True,index=True)
    target: Mapped[dict]=mapped_column(JSON,default=dict)
    status: Mapped[str]=mapped_column(String(30))
    echo: Mapped[str | None]=mapped_column(String(100),nullable=True,unique=True)
    sent_at: Mapped[float | None]=mapped_column(Float,nullable=True)
    protocol_message_id: Mapped[str | None]=mapped_column(String(60),nullable=True)
    error: Mapped[str | None]=mapped_column(Text,nullable=True)
    reviewed_at: Mapped[float | None]=mapped_column(Float,nullable=True)


class DeliveryView(BaseModel):
    kind: str
    status: str
    channel_id: str | None
    target_label: str
    sent_at: float | None
    protocol_message_id: str | None
    error: str | None
    reviewed_at: float | None


class TargetOption(BaseModel):
    target: ContactTarget
    label: str
    online: bool


def target_for(db,user):
    row=db.get(ContactRoute,user.id)
    if row and row.workspace_id!=user.workspace_id:raise HTTPException(409,'联系目标不属于当前工作区')
    return row.target if row else ContactTarget().model_dump()


def checked_target(db,user,target):
    target=ContactTarget(**target)
    if target.kind=='web':return None,None
    link=owned(db,IdentityLink,target.binding_id,user)
    channel=owned(db,Channel,link.channel_id,user)
    if (not channel.enabled or not channel.rules.get('private_enabled',True) or
            channel.version!=target.channel_version or link.status!='active' or
            link.version!=target.binding_version or not link.allow_memory):
        raise HTTPException(409,'QQ 平台或绑定已变化；仅已确认且允许个人记忆的私聊身份可接收，请重新核对授权')
    return channel,link


@router.get('/api/proactive-targets',response_model=list[TargetOption])
def targets(ctx: Context):
    db,user,_=ctx
    result=[]
    for link in db.scalars(select(IdentityLink).where(IdentityLink.user_id==user.id,
            IdentityLink.workspace_id==user.workspace_id,IdentityLink.status=='active',IdentityLink.allow_memory==True)):
        channel=db.get(Channel,link.channel_id)
        if not channel or channel.user_id!=user.id or channel.workspace_id!=user.workspace_id or not channel.enabled or not channel.rules.get('private_enabled',True):continue
        result.append(dict(target=ContactTarget(kind='qq',binding_id=link.id,binding_version=link.version,
            channel_version=channel.version).model_dump(),label=channel.name+' · QQ '+link.external_user_id,
            online=bool(channel.connection_id and channel.lease_until>now() and channel.login_state=='online')))
    return result


def save_target(db,user,target,acknowledged):
    checked_target(db,user,target)
    if target['kind']=='qq' and target!=target_for(db,user) and not acknowledged:
        raise HTTPException(422,'请明确确认向这个 QQ 私聊账号发送主动交流，可能包含个人记忆及授权事件')
    db.merge(ContactRoute(user_id=user.id,workspace_id=user.workspace_id,target=target))


def delivery_view(db,contact):
    row=db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.contact_id==contact.id))
    if not row:return dict(kind='web',status='stored',channel_id=None,target_label='Web 站内',sent_at=contact.created_at,protocol_message_id=None,error=None,reviewed_at=None)
    return dict(kind=row.kind,status=row.status,channel_id=row.channel_id,
        target_label=row.target.get('label','Web 站内'),sent_at=row.sent_at,
        protocol_message_id=row.protocol_message_id,error=row.error,reviewed_at=row.reviewed_at)


def quota_times(db,user,sending=False):
    """Reserve unsent messages at creation; count real attempts at actual send time."""
    from .proactive import ProactiveContact
    from .proactive_voice import VoiceContact
    query=select(case(((ProactiveDelivery.kind=='qq') & ProactiveDelivery.sent_at.is_not(None),ProactiveDelivery.sent_at),else_=ProactiveContact.created_at)).select_from(ProactiveContact).outerjoin(
        ProactiveDelivery,ProactiveDelivery.contact_id==ProactiveContact.id).where(
        ProactiveContact.user_id==user.id,ProactiveContact.workspace_id==user.workspace_id,
        ~ProactiveContact.id.in_(select(VoiceContact.contact_id)))
    if sending:
        query=query.where((ProactiveDelivery.id.is_(None)) | ProactiveDelivery.sent_at.is_not(None))
    else:
        query=query.where((ProactiveDelivery.id.is_(None)) | ProactiveDelivery.sent_at.is_not(None) | (ProactiveDelivery.status=='pending'))
    orphan=select(ProactiveDelivery.sent_at).where(ProactiveDelivery.user_id==user.id,
        ProactiveDelivery.workspace_id==user.workspace_id,ProactiveDelivery.sent_at.is_not(None),
        ~ProactiveDelivery.contact_id.in_(select(ProactiveContact.id)))
    return list(db.scalars(query))+list(db.scalars(orphan))


def route_ready(db,user,target):
    channel,link=checked_target(db,user,target)
    if channel and (not channel.connection_id or channel.lease_until<=now() or channel.login_state!='online'):
        return '已授权 QQ 当前离线或待登录；等待重连，不改投其他渠道'
    return None


def record_delivery(db,user,topic,contact,target):
    channel,link=checked_target(db,user,target)
    snapshot=target.copy()
    if channel:snapshot|={'external_user_id':link.external_user_id,'label':channel.name+' · QQ '+link.external_user_id}
    db.add(ProactiveDelivery(user_id=user.id,workspace_id=user.workspace_id,contact_id=contact.id,topic_id=topic.id,
        kind=target['kind'],channel_id=channel.id if channel else None,target=snapshot,
        status='pending' if channel else 'stored',sent_at=None if channel else now()))


def stop_delivery(row,reason):
    if row.status=='pending':row.status='cancelled';row.error=reason
    elif row.status=='sending':row.status='unknown';row.error=reason+'；已经开始发送，结果待核对，不会重发'


def invalidate(db,*,contact_id=None,channel_id=None,binding_id=None,user_id=None,reason='主动交流来源或授权已失效'):
    # Preference changes, feedback and channel revocation also fence unfinished model text.
    if contact_id or user_id:
        from .proactive_voice import invalidate as invalidate_voice
        invalidate_voice(db, contact_id=contact_id, user_id=user_id)
    from .proactive_generation import invalidate as invalidate_draft
    if user_id:
        invalidate_draft(db,user_id=user_id,reason=reason)
    elif contact_id:
        from .proactive import ProactiveContact
        contact=db.get(ProactiveContact,contact_id)
        if contact:invalidate_draft(db,topic_id=contact.topic_id,reason=reason)
    elif channel_id or binding_id:
        for route in db.scalars(select(ContactRoute)):
            link=db.get(IdentityLink,route.target.get('binding_id')) if route.target.get('binding_id') else None
            if ((not channel_id or link and link.channel_id==channel_id) and
                (not binding_id or route.target.get('binding_id')==binding_id)):
                invalidate_draft(db,user_id=route.user_id,reason=reason)
    query=select(ProactiveDelivery).where(ProactiveDelivery.kind=='qq',ProactiveDelivery.status.in_(['pending','sending']))
    if contact_id:query=query.where(ProactiveDelivery.contact_id==contact_id)
    if channel_id:query=query.where(ProactiveDelivery.channel_id==channel_id)
    if user_id:query=query.where(ProactiveDelivery.user_id==user_id)
    for row in db.scalars(query.with_for_update()):
        if not binding_id or row.target.get('binding_id')==binding_id:stop_delivery(row,reason)


def interrupted(db,channel_id,connection=None):
    for row in db.scalars(select(ProactiveDelivery).where(ProactiveDelivery.channel_id==channel_id,ProactiveDelivery.status=='sending')):
        if connection is None or (row.echo or '').startswith(connection+':'):
            stop_delivery(row,'QQ 连接中断')


def receive_receipt(db,channel,echo,event):
    from .channel_gateway import numeric,actor_conversation
    from .proactive import ProactiveContact,valid_contact
    initial=db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.channel_id==channel.id,ProactiveDelivery.echo==echo))
    if not initial:return
    # Source invalidation and review lock contact -> delivery. Keep that order.
    contact=db.scalar(select(ProactiveContact).where(ProactiveContact.id==initial.contact_id).with_for_update())
    row=db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.id==initial.id).with_for_update().execution_options(populate_existing=True))
    if not row or row.status not in ('sending','unknown'):return
    data=event.get('data') if isinstance(event.get('data'),dict) else {}
    code=event.get('retcode');message_id=numeric(data.get('message_id'),30,negative=True)
    if type(code) is not int:return
    if code==0 and message_id:row.status,row.protocol_message_id,row.error='accepted_by_protocol',message_id,None
    elif code!=0:row.status,row.error='failed','QQ 协议端拒绝请求；未自动重发'
    else:return
    if not contact:return
    contact.version+=1
    try:_,link=checked_target(db,db.get(User,row.user_id),row.target)
    except HTTPException:return
    if row.status!='accepted_by_protocol' or not valid_contact(db,contact) or contact.message_id:return
    # Add only protocol-accepted text to this exact private QQ session, never an unsent proposal.
    incoming=SimpleNamespace(binding_id=link.id,target_key='private:'+link.external_user_id)
    _,conversation,_=actor_conversation(db,channel,incoming)
    message=Message(workspace_id=row.workspace_id,conversation_id=conversation.id,role='assistant',content=contact.content)
    db.add(message);db.flush()
    contact.conversation_id,contact.message_id=conversation.id,message.id


def prepare(identity,connection):
    from .proactive import (ProactiveContact,ConversationTopic,source_record,lock_owner,pref_view,timing_reason)
    with SessionLocal() as db:
        ids=list(db.scalars(select(ProactiveDelivery.id).where(ProactiveDelivery.channel_id==identity,
            ProactiveDelivery.status.in_(['pending','sending'])).order_by(ProactiveDelivery.created_at)))
    for key in ids:
        with SessionLocal.begin() as db:
            initial=db.get(ProactiveDelivery,key)
            topic=db.get(ConversationTopic,initial.topic_id)
            # Match source -> owner -> channel order, never hold a channel while locking memory.
            source=source_record(db,topic,lock=True) if topic else None
            user=db.get(User,initial.user_id)
            if not user:continue
            lock_owner(db,user)
            topic=db.get(ConversationTopic,initial.topic_id,populate_existing=True)
            source=source_record(db,topic) if topic else None
            from .channel_gateway import authorized_channel
            channel=authorized_channel(db,identity,connection)
            if not channel:return False
            row=db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.id==key).with_for_update().execution_options(populate_existing=True))
            contact=db.get(ProactiveContact,row.contact_id,populate_existing=True)
            if row.status=='sending':
                if row.sent_at<now()-30:stop_delivery(row,'QQ 回执超时')
                continue
            if row.status!='pending':continue
            prefs=pref_view(db,user)
            try:
                checked_target(db,user,row.target)
                if ContactTarget(**row.target).model_dump()!=prefs['delivery']:raise HTTPException(409,'联系目标已修改')
            except HTTPException:
                stop_delivery(row,'QQ 联系目标、平台或身份授权已变化');continue
            if not source or not contact or contact.status=='invalidated' or topic.status in ('invalidated','closed'):
                stop_delivery(row,'主动消息来源已失效或关闭');continue
            if topic.expires_at<=now():stop_delivery(row,'主动话题已过期，未补发');continue
            if channel.login_state!='online':continue
            if timing_reason(db,user,topic,prefs,now(),sending=True):continue
            # Reply traffic and unresolved sends take precedence over unsolicited contact.
            if now()-channel.last_sent<2:continue
            target='private:'+row.target['external_user_id']
            if db.scalar(select(ChannelInbound.id).where(ChannelInbound.channel_id==identity,ChannelInbound.target_key==target,
                    ChannelInbound.status.in_(['queued','ingesting','running','ready']))):continue
            if db.scalar(select(ChannelDelivery.id).join(ChannelInbound,ChannelInbound.id==ChannelDelivery.inbound_id).where(
                    ChannelDelivery.channel_id==identity,ChannelInbound.target_key==target,
                    ChannelDelivery.status.in_(['sending','unknown','failed']),ChannelDelivery.reviewed_at.is_(None))):continue
            if db.scalar(select(ProactiveDelivery.id).where(ProactiveDelivery.channel_id==identity,ProactiveDelivery.id!=row.id,
                    ProactiveDelivery.status.in_(['sending','unknown','failed']),ProactiveDelivery.reviewed_at.is_(None))):continue
            if len(contact.content)>1500:
                stop_delivery(row,'主动内容超过 1500 字投递预算，未截断发送');continue
            row.status,row.sent_at,row.echo='sending',now(),connection+':'+uid()
            channel.last_sent=now();contact.version+=1
            return {'action':'send_private_msg','params':{'user_id':int(row.target['external_user_id']),
                'message':[{'type':'text','data':{'text':contact.content}}]},'echo':row.echo}
    return None


def expire_pending():
    from .proactive import ProactiveContact,ConversationTopic,source_record,lock_owner
    with SessionLocal() as db:
        ids=list(db.scalars(select(ProactiveDelivery.id).where(ProactiveDelivery.kind=='qq',ProactiveDelivery.status=='pending')))
    for key in ids:
        with SessionLocal.begin() as db:
            initial=db.get(ProactiveDelivery,key);user=db.get(User,initial.user_id)
            if not user:continue
            lock_owner(db,user)
            row=db.get(ProactiveDelivery,key,populate_existing=True)
            if row.status!='pending':continue
            topic=db.get(ConversationTopic,row.topic_id,populate_existing=True)
            contact=db.get(ProactiveContact,row.contact_id,populate_existing=True)
            if not topic or topic.expires_at<=now() or topic.status in ('closed','invalidated') or not contact or contact.status=='invalidated' or not source_record(db,topic):
                stop_delivery(row,'话题过期、关闭或来源失效；离线时也不保留待补发内容')


class ReviewInput(BaseModel):
    version: int=Field(ge=1)
    acknowledgement: Literal['checked_no_resend']


@router.post('/api/proactive-contacts/{identity}/delivery-review',response_model=DeliveryView)
def review(identity:str,body:ReviewInput,ctx:Context):
    from .proactive import ProactiveContact,lock_owner
    db,user,_=ctx;lock_owner(db,user)
    contact=owned(db,ProactiveContact,identity,user,True)
    if contact.version!=body.version:raise HTTPException(409,'联系记录已变化，请重新加载')
    row=db.scalar(select(ProactiveDelivery).where(ProactiveDelivery.contact_id==contact.id).with_for_update())
    if not row or row.kind!='qq' or row.status not in ('unknown','failed') or row.reviewed_at:
        raise HTTPException(409,'没有可结束等待的未知或失败 QQ 请求')
    row.reviewed_at=now();contact.version+=1
    audit(db,user,'proactive_delivery_review',row.id)
    return delivery_view(db,contact)
