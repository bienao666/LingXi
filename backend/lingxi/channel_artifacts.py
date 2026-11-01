"""Explicit, source-bound QQ artifact replies. No model-supplied path or target."""
import base64
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Float, String, UniqueConstraint, select, or_
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now, uid
from .models import Entity, ChatRun, Conversation, Message, User
from .channels import Channel, ChannelInbound, ChannelDelivery, ChannelSession, ChannelRules, DeliveryView, delivery_view
from .web import Context, owned, audit, view
from .tasks import digest

router = APIRouter()
KINDS = ('artifact_file', 'artifact_image')
MAX_FILE = 20 * 1024**2
MAX_TOTAL = 50 * 1024**2


class ArtifactReply(Entity, Base):
    __tablename__ = 'channel_artifact_replies'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), index=True)
    origin_id: Mapped[str] = mapped_column(String(64), index=True)
    login_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default='preview')
    expires_at: Mapped[float] = mapped_column(Float)
    inbound_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ArtifactSource(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['media', 'agent_result']
    id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)


class ArtifactSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    origin_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=8, max_length=100)
    sources: list[ArtifactSource] = Field(min_length=1, max_length=3)

    @model_validator(mode='after')
    def unique(self):
        if len({(s.kind, s.id) for s in self.sources}) != len(self.sources):
            raise ValueError('附件来源不能重复')
        return self


class ArtifactConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    digest: str = Field(pattern='^[a-f0-9]{64}$')
    acknowledgement: Literal['share_with_original_audience']


class ArtifactOption(ArtifactSource):
    name: str
    bytes: int
    format: Literal['file', 'image']


class ArtifactReplyView(BaseModel):
    id: str
    status: str
    target_key: str
    expires_at: float
    digest: str
    files: list[ArtifactOption]
    deliveries: list[DeliveryView]


def lock_channel(db, user, identity):
    channel = owned(db, Channel, identity, user, lock=True)
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    return channel


def origin(db, user, channel, identity):
    from .channel_gateway import channel_scope_valid
    incoming = owned(db, ChannelInbound, identity, user)
    run = db.get(ChatRun, incoming.run_id, populate_existing=True) if incoming.run_id else None
    conversation = db.get(Conversation, run.conversation_id, populate_existing=True) if run else None
    scope = {'id':channel.id, 'version':incoming.channel_version,
        'binding_id':incoming.binding_id, 'binding_version':incoming.binding_version}
    session_key = incoming.target_key + ':' + (f'{incoming.binding_id}:{incoming.binding_version}' if incoming.binding_id else 'isolated')
    session = db.scalar(select(ChannelSession).where(ChannelSession.channel_id == channel.id, ChannelSession.scope_key == session_key))
    rules=ChannelRules(**channel.rules)
    target_kind,target_id=incoming.target_key.split(':',1)
    enabled=(rules.private_enabled and target_id==incoming.sender_id and (bool(incoming.binding_id) or rules.allow_unbound)
        if target_kind=='private' else target_kind=='group' and rules.group_enabled and target_id in rules.group_ids)
    pinned=run.config.get('channel',{}) if run else {}
    if (incoming.channel_id != channel.id or incoming.created_at < now()-86400
            or incoming.external_message_id.startswith('artifact:') or incoming.status != 'completed'
            or not channel_scope_valid(db, scope) or not run or run.status != 'completed' or run.cancel_requested
            or not conversation or conversation.deleted or conversation.workspace_id != user.workspace_id
            or conversation.user_id != run.user_id or not session or session.conversation_id != conversation.id
            or session.actor_id != run.user_id or session.user_id != user.id or session.workspace_id != user.workspace_id
            or not enabled or pinned.get('id')!=channel.id or pinned.get('version')!=incoming.channel_version
            or ('target_key' in pinned and pinned['target_key']!=incoming.target_key)):
        raise HTTPException(409, '原 QQ 消息未完成、过期、会话已删除或渠道授权已变化')
    for identity in run.source_message_ids:
        message = db.get(Message, identity, populate_existing=True)
        if not message or message.status != 'completed' or message.conversation_id != conversation.id:
            raise HTTPException(409, '原 QQ 对话来源已失效')
    from .memory import usable
    from .models import Memory
    from .media import assert_media_valid
    from .channel_tasks import delivery_valid
    if any(not usable(m:=db.get(Memory,r['id'],populate_existing=True)) or m.version!=r['version'] for r in run.memory_refs) or not delivery_valid(db,run):
        raise HTTPException(409,'原 QQ 回答依据已失效')
    from .worker import Cancelled
    try:assert_media_valid(db,run)
    except Cancelled:raise HTTPException(409,'原 QQ 附件依据已失效') from None
    return dict(channel_id=channel.id, channel_version=channel.version, origin_id=incoming.id,
        target_key=incoming.target_key, sender_id=incoming.sender_id, binding_id=incoming.binding_id,
        binding_version=incoming.binding_version, conversation_id=conversation.id, actor_id=run.user_id, run_id=run.id)


def media_item(db, user, scope, ref, *, read=False):
    from .media import Media, MediaLink, storage, SUFFIXES
    from .server_documents import require_source
    item = db.get(Media, ref.id, populate_existing=True)
    linked = item and item.user_id == scope['actor_id'] and db.scalar(select(MediaLink.id).join(
        Message, Message.id == MediaLink.message_id).where(MediaLink.media_id == ref.id,
            Message.conversation_id == scope['conversation_id'], Message.status == 'completed'))
    if (not item or item.workspace_id != user.workspace_id or (item.user_id != user.id and not linked)
            or item.status != 'ready' or item.version != ref.version or item.suffix not in SUFFIXES
            or item.parsed.get('kind') not in ('image', 'document') or not 0 < item.size <= MAX_FILE):
        raise HTTPException(409, '附件不在可共享范围、尚未就绪、过大或版本已变化')
    require_source(db, item)
    metadata = dict(**ref.model_dump(), name='lingxi-asset-'+item.id+item.suffix, bytes=item.size,
        format='image' if item.parsed['kind']=='image' else 'file', hash=item.digest, suffix=item.suffix)
    raw = None
    if read:
        try:
            with storage(item.id, 'blob').open('rb') as stream:
                raw = stream.read(min(item.size, MAX_FILE)+1)
        except (OSError, ValueError):
            raise HTTPException(409, '附件存储不可用，未发送') from None
        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.digest:
            raise HTTPException(409, '附件完整性已变化，未发送')
    return metadata, raw


def agent_result(db, user, scope, ref):
    from .delegation import AgentRun, AnalysisResult
    from .agent_worker import validate_sources, validate_profiles
    from .channel_tasks import require_origin
    run = owned(db, AgentRun, ref.id, user)
    task = require_origin(db, run)
    if (run.status not in ('completed', 'partial') or run.cancel_requested or run.version != ref.version
            or not task or task.inbound_id != scope['origin_id'] or task.scope['target_key'] != scope['target_key']
            or (scope['target_key'].startswith('group:') and run.memory_refs)):
        raise HTTPException(409, '仅能回传原 QQ 请求的有效完成或部分完成协作结果')
    validate_sources(db, run, lock=False)
    validate_profiles(db, run)
    try:result = AnalysisResult.model_validate({k:v for k,v in run.result.items() if k in AnalysisResult.model_fields}).model_dump()
    except ValueError:raise HTTPException(409,'协作汇总结构已失效') from None
    text = '# 灵犀协作结果\n\n状态：' + ('部分完成' if run.status=='partial' else '已形成汇总')
    text += '\n\n'+result['summary']+'\n\n'+'\n'.join('- '+f['text']+'（依据节点：'+ '、'.join(f['evidence_ids'])+'）' for f in result['findings'])
    if run.result.get('attachment_coverage'):
        text+='\n\n附件覆盖记录：\n```json\n'+json.dumps(run.result['attachment_coverage'],ensure_ascii=False,indent=2)+'\n```'
    text += '\n\n限制：\n'+'\n'.join('- '+v for v in result['limitations'])
    text += '\n\n仅核验结构和引用范围；外部操作以真实回执为准。\n'
    raw = text.encode('utf-8')
    if len(raw) > 512*1024:
        raise HTTPException(413, '协作结果超过 512 KiB，未截断发送')
    return dict(**ref.model_dump(), name='lingxi-result-'+run.id+'.md', bytes=len(raw), format='file',
        hash=hashlib.sha256(raw).hexdigest(), suffix='.md', round=run.round, result_hash=digest(run.result)), raw


def build(db, user, channel, identity, sources, *, read=True):
    from .media_policy import current, check_sizes, check_types
    scope = origin(db, user, channel, identity)
    files, bodies = [], []
    for ref in sources:
        metadata, raw = (media_item(db,user,scope,ref,read=read) if ref.kind=='media' else agent_result(db,user,scope,ref))
        files.append(metadata); bodies.append(raw)
    policy = current(db, user.workspace_id)
    check_sizes(policy, [f['bytes'] for f in files]);check_types(policy, [f['suffix'] for f in files])
    if sum(f['bytes'] for f in files) > MAX_TOTAL:
        raise HTTPException(413, '合计附件超过 50 MiB')
    return {'scope':scope,'files':files}, bodies


def option(value):
    return ArtifactOption.model_validate({k:v for k,v in value.items() if k in ArtifactOption.model_fields})


def public(db, row):
    deliveries=[delivery_view(d) for d in db.scalars(select(ChannelDelivery).where(
        ChannelDelivery.inbound_id == row.inbound_id).order_by(ChannelDelivery.part))] if row.inbound_id else []
    status=row.status
    if deliveries and all(d['status']=='accepted_by_protocol' for d in deliveries):status='accepted_by_protocol'
    elif deliveries and all(d['status']=='cancelled' for d in deliveries):status='cancelled'
    elif any(d['status']=='unknown' for d in deliveries):status='unknown'
    elif any(d['status']=='failed' for d in deliveries):status='failed'
    elif status in ('preview','confirmed') and row.expires_at<=now():status='expired'
    return dict(id=row.id,status=status,target_key=row.plan['scope']['target_key'],expires_at=row.expires_at,
        digest=row.digest,files=[option(f).model_dump() for f in row.plan['files']],deliveries=deliveries)


@router.get('/api/channels/{identity}/artifact-sources', response_model=list[ArtifactOption])
def options(identity: str, ctx: Context, origin_id: str = Query(max_length=64), q: str = Query('', max_length=100)):
    db,user,_ = ctx
    channel = owned(db,Channel,identity,user)
    scope = origin(db,user,channel,origin_id)
    from .media import Media, MediaLink
    linked = select(MediaLink.media_id).join(Message, Message.id == MediaLink.message_id).where(
        Message.conversation_id==scope['conversation_id'],Message.status=='completed')
    query = select(Media).where(Media.workspace_id==user.workspace_id,Media.status=='ready',
        or_(Media.user_id==user.id, (Media.user_id==scope['actor_id']) & Media.id.in_(linked)))
    if q:query=query.where(Media.filename.contains(q, autoescape=True))
    result=[]
    for item in db.scalars(query.order_by(Media.created_at.desc(),Media.id).limit(50)):
        try:
            metadata,_=media_item(db,user,scope,ArtifactSource(kind='media',id=item.id,version=item.version))
            result.append(option(metadata).model_dump() | {'name':item.filename})
        except HTTPException:continue
    from .channel_tasks import ChannelTask
    from .delegation import AgentRun
    task=db.scalar(select(ChannelTask).where(ChannelTask.inbound_id==origin_id,ChannelTask.user_id==user.id))
    run=db.get(AgentRun,task.run_id) if task and task.run_id else None
    if run:
        try:
            metadata,_=agent_result(db,user,scope,ArtifactSource(kind='agent_result',id=run.id,version=run.version))
            result.append(option(metadata))
        except HTTPException:pass
    return result


@router.post('/api/channels/{identity}/artifact-previews', response_model=ArtifactReplyView)
def preview(identity: str, body: ArtifactSelection, ctx: Context):
    db,user,login=ctx
    channel=lock_channel(db,user,identity)
    old=db.scalar(select(ArtifactReply).where(ArtifactReply.user_id==user.id,ArtifactReply.request_id==body.request_id))
    if old:
        if old.workspace_id!=user.workspace_id or old.channel_id!=identity or old.login_id!=login.id or old.request_hash!=digest(body.model_dump()):
            raise HTTPException(409,'请求编号已用于另一份预览')
        if old.status=='preview' and old.expires_at<=now():raise HTTPException(409,'预览已过期，请重新选择')
        return public(db,old)
    plan,_=build(db,user,channel,body.origin_id,body.sources)
    row=ArtifactReply(user_id=user.id,workspace_id=user.workspace_id,channel_id=identity,origin_id=body.origin_id,
        login_id=login.id,request_id=body.request_id,request_hash=digest(body.model_dump()),plan=plan,
        digest=digest(plan),expires_at=now()+300)
    db.add(row);db.flush();audit(db,user,'channel_artifact_preview',row.id)
    return public(db,row)


@router.get('/api/channels/{identity}/artifact-replies', response_model=list[ArtifactReplyView])
def replies(identity: str, ctx: Context, origin_id: str = Query(max_length=64)):
    db,user,_=ctx
    owned(db,Channel,identity,user)
    return [public(db,row) for row in db.scalars(select(ArtifactReply).where(
        ArtifactReply.channel_id==identity,ArtifactReply.origin_id==origin_id,ArtifactReply.user_id==user.id,
        ArtifactReply.workspace_id==user.workspace_id).order_by(ArtifactReply.created_at.desc()).limit(20))]


@router.post('/api/channel-artifact-replies/{identity}/confirm', response_model=ArtifactReplyView)
def confirm(identity: str, body: ArtifactConfirm, ctx: Context):
    db,user,login=ctx
    row=owned(db,ArtifactReply,identity,user)
    channel=lock_channel(db,user,row.channel_id)
    db.refresh(row)
    if row.login_id!=login.id or row.digest!=body.digest:raise HTTPException(409,'预览身份或内容不匹配')
    if row.inbound_id:return public(db,row)  # lost HTTP response never duplicates a send
    if row.status!='preview' or row.expires_at<=now():raise HTTPException(409,'预览已失效，请重新选择')
    sources=[ArtifactSource(**{k:f[k] for k in ('kind','id','version')}) for f in row.plan['files']]
    plan,_=build(db,user,channel,row.origin_id,sources)
    if digest(plan)!=row.digest:raise HTTPException(409,'来源内容或接收范围已变化，请重新预览')
    scope=plan['scope']
    incoming=ChannelInbound(user_id=user.id,workspace_id=user.workspace_id,channel_id=channel.id,
        channel_version=channel.version,external_message_id='artifact:'+row.id,target_key=scope['target_key'],
        sender_id=scope['sender_id'],binding_id=scope['binding_id'],binding_version=scope['binding_version'],content='',status='ready')
    db.add(incoming);db.flush()
    row.inbound_id,row.status,row.expires_at=incoming.id,'confirmed',now()+3600
    for index,file in enumerate(plan['files']):
        db.add(ChannelDelivery(user_id=user.id,workspace_id=user.workspace_id,channel_id=channel.id,inbound_id=incoming.id,
            part=index,kind='artifact_'+file['format'],content='',echo=uid(),file_info={'batch_id':row.id,'index':index,
                'name':file['name'],'bytes':file['bytes']}))
    db.flush();audit(db,user,'channel_artifact_confirm',row.id)
    return public(db,row)


@router.post('/api/channel-artifact-replies/{identity}/cancel', response_model=ArtifactReplyView)
def cancel(identity: str, ctx: Context):
    db,user,_=ctx
    row=owned(db,ArtifactReply,identity,user)
    lock_channel(db,user,row.channel_id);db.refresh(row)
    row.status='cancelled'
    if row.inbound_id:
        incoming=db.get(ChannelInbound,row.inbound_id);incoming.status='cancelled'
        for output in db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id==row.inbound_id,ChannelDelivery.status=='pending')):
            output.status,output.error='cancelled','用户取消未发送附件；已发出的操作无法撤回'
    audit(db,user,'channel_artifact_cancel',row.id)
    return public(db,row)


def action(db, output):
    user=db.get(User,output.user_id,populate_existing=True)
    row=db.get(ArtifactReply,output.file_info.get('batch_id'),populate_existing=True)
    if (not user or not row or row.user_id!=user.id or row.workspace_id!=output.workspace_id
            or row.channel_id!=output.channel_id or row.inbound_id!=output.inbound_id or row.status!='confirmed'
            or row.expires_at<=now() or output.status!='pending'):
        raise HTTPException(409,'附件投递已取消或失效')
    channel=lock_channel(db,user,row.channel_id)
    sources=[ArtifactSource(**{k:f[k] for k in ('kind','id','version')}) for f in row.plan['files']]
    plan,bodies=build(db,user,channel,row.origin_id,sources)
    if digest(plan)!=row.digest:raise HTTPException(409,'附件来源或范围已失效')
    index=output.file_info.get('index')
    if type(index) is not int or not 0<=index<len(bodies):raise HTTPException(409,'附件编号无效')
    file=plan['files'][index]
    if (output.kind!='artifact_'+file['format'] or output.file_info!={'batch_id':row.id,'index':index,'name':file['name'],'bytes':file['bytes']}):
        raise HTTPException(409,'附件发送元数据已变化')
    kind,target=plan['scope']['target_key'].split(':',1)
    encoded='base64://'+base64.b64encode(bodies[index]).decode('ascii')
    if file['format']=='image':
        return {'action':'send_group_msg' if kind=='group' else 'send_private_msg','params':{
            'group_id' if kind=='group' else 'user_id':target,'message':[{'type':'image','data':{'file':encoded}}]}}
    return {'action':'upload_group_file' if kind=='group' else 'upload_private_file','params':{
        'group_id' if kind=='group' else 'user_id':target,'file':encoded,'name':file['name'],'upload_file':True}}


def checkpoint(db):
    return [view(row) | {'status':'cancelled','login_id':'revoked','expires_at':0} for row in db.scalars(select(ArtifactReply))]


def restore(db, records):
    for data in records:db.merge(ArtifactReply(**(data|{'status':'cancelled','login_id':'revoked','expires_at':0})))
