"""Authenticated backup control plane. Privileged work stays in the host runner."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, Boolean, Float, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal, now, uid
from .models import Entity, User
from .web import Context, audit, owned, view

router=APIRouter()


class BackupPolicy(Entity, Base):
    __tablename__='backup_policies'
    __table_args__=(UniqueConstraint('user_id','workspace_id'),)
    user_id:Mapped[str]=mapped_column(String(64),index=True)
    version:Mapped[int]=mapped_column(Integer,default=1)
    enabled:Mapped[bool]=mapped_column(Boolean,default=False)
    interval_hours:Mapped[int]=mapped_column(Integer,default=24)
    next_at:Mapped[float]=mapped_column(Float,default=0)


class BackupRun(Entity, Base):
    __tablename__='backup_runs'
    __table_args__=(UniqueConstraint('user_id','workspace_id','request_id'),)
    user_id:Mapped[str]=mapped_column(String(64),index=True)
    request_id:Mapped[str]=mapped_column(String(100))
    kind:Mapped[str]=mapped_column(String(20),default='backup')
    archive_id:Mapped[str]=mapped_column(String(64),default='')
    status:Mapped[str]=mapped_column(String(20),default='queued',index=True)
    finished_at:Mapped[float]=mapped_column(Float,default=0)
    digest:Mapped[str]=mapped_column(String(64),default='')
    bytes:Mapped[int]=mapped_column(BigInteger,default=0)
    error:Mapped[str]=mapped_column(String(40),default='')


class BackupAgent(Base):
    __tablename__='backup_agents'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    seen_at:Mapped[float]=mapped_column(Float)


class PolicyInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=0)
    enabled:bool
    interval_hours:int=Field(ge=1,le=168)


class PolicyView(BaseModel):
    version:int
    enabled:bool
    interval_hours:int
    next_at:float


class RunInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=8,max_length=100)
    kind:Literal['backup','verify']='backup'
    archive_id:str=Field(default='',pattern='^([a-f0-9]{32})?$')
    confirmed:bool


class RunView(BaseModel):
    id:str
    created_at:float
    kind:str
    archive_id:str
    status:str
    finished_at:float
    digest:str
    bytes:int
    error:str


class Overview(BaseModel):
    policy:PolicyView
    agent_online:bool
    maintenance:bool
    runs:list[RunView]


def policy_view(row):
    return {k:getattr(row,k) for k in ('version','enabled','interval_hours','next_at')} if row else {'version':0,'enabled':False,'interval_hours':24,'next_at':0}


def paused(db):
    from .workspace_migrations import paused as migration_paused
    return migration_paused(db) or bool(db.scalar(select(BackupRun.id).where(BackupRun.kind.in_(['backup','restore']),BackupRun.status=='running').limit(1)))


@router.get('/api/admin/backups',response_model=Overview)
def overview(ctx:Context):
    db,user,_=ctx
    agent=db.get(BackupAgent,'host')
    return {'policy':policy_view(db.scalar(select(BackupPolicy).where(BackupPolicy.user_id==user.id,BackupPolicy.workspace_id==user.workspace_id))),
        'agent_online':bool(agent and agent.seen_at>now()-60),'maintenance':paused(db),
        'runs':[view(row) for row in db.scalars(select(BackupRun).where(BackupRun.user_id==user.id,BackupRun.workspace_id==user.workspace_id).order_by(BackupRun.created_at.desc(),BackupRun.id).limit(100))]}


@router.put('/api/admin/backups/policy',response_model=PolicyView)
def save_policy(body:PolicyInput,ctx:Context):
    db,user,_=ctx
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    row=db.scalar(select(BackupPolicy).where(BackupPolicy.user_id==user.id,BackupPolicy.workspace_id==user.workspace_id))
    if body.version!=(row.version if row else 0):raise HTTPException(409,'备份配置已变化，请重新加载核对')
    if not row:row=BackupPolicy(user_id=user.id,workspace_id=user.workspace_id);db.add(row)
    else:row.version+=1
    row.enabled,row.interval_hours=body.enabled,body.interval_hours
    row.next_at=now()+body.interval_hours*3600 if body.enabled else 0
    db.flush();audit(db,user,'backup_policy',row.id)
    return policy_view(row)


@router.post('/api/admin/backups',response_model=RunView)
def create(body:RunInput,ctx:Context):
    db,user,_=ctx
    if not body.confirmed:raise HTTPException(422,'请先核对备份停写或核验范围并确认')
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    previous=db.scalar(select(BackupRun).where(BackupRun.user_id==user.id,BackupRun.workspace_id==user.workspace_id,BackupRun.request_id==body.request_id))
    if previous:
        if previous.kind!=body.kind or (body.kind=='verify' and previous.archive_id!=body.archive_id):raise HTTPException(409,'请求编号已用于其他备份操作')
        return view(previous)
    if body.kind=='verify':
        source=owned(db,BackupRun,body.archive_id,user)
        if source.kind!='backup' or source.status!='succeeded':raise HTTPException(409,'只能核验已成功完成的备份')
    elif body.archive_id:raise HTTPException(422,'新备份不接受自定义存储路径或编号')
    if db.scalar(select(BackupRun).where(BackupRun.status.in_(['queued','running']))):raise HTTPException(409,'已有备份操作等待或正在执行，请先处理')
    row=BackupRun(user_id=user.id,workspace_id=user.workspace_id,request_id=body.request_id,kind=body.kind,archive_id=body.archive_id)
    db.add(row);db.flush()
    if body.kind=='backup':row.archive_id=row.id
    audit(db,user,'backup_requested',row.id)
    return view(row)


@router.post('/api/admin/backups/{identity}/cancel',response_model=RunView)
def cancel(identity:str,ctx:Context):
    db,user,_=ctx
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    row=owned(db,BackupRun,identity,user,lock=True)
    if row.status!='queued':raise HTTPException(409,'只能取消尚未执行的操作；停写中的恢复需服务器终端处理')
    row.status,row.finished_at='cancelled',now();audit(db,user,'backup_cancelled',row.id)
    return view(row)


def agent_claim():
    with SessionLocal.begin() as db:
        from .workspace_migrations import paused as migration_paused
        if migration_paused(db):return None
        owners=restore_owners(db)
        if len(owners)!=1:raise RuntimeError('One login owner required')
        user=db.scalar(select(User).where(User.id==owners[0].id).with_for_update())
        if migration_paused(db):return None
        agent=db.get(BackupAgent,'host')
        if agent:agent.seen_at=now()
        else:db.add(BackupAgent(id='host',seen_at=now()))
        if db.scalar(select(BackupRun).where(BackupRun.status=='running')):return None
        row=db.scalar(select(BackupRun).where(BackupRun.status=='queued').order_by(BackupRun.created_at,BackupRun.id).limit(1).with_for_update())
        policy=db.scalar(select(BackupPolicy).where(BackupPolicy.user_id==user.id,BackupPolicy.enabled==True))
        if not row and policy and policy.next_at<=now():
            row=BackupRun(id=uid(),user_id=user.id,workspace_id=user.workspace_id,request_id='scheduled:'+uid())
            row.archive_id=row.id;db.add(row);db.flush()
            policy.next_at=now()+policy.interval_hours*3600
        if not row:return None
        row.status='running'
        audit(db,user,'backup_started',row.id)
        return {'id':row.id,'kind':row.kind,'archive_id':row.archive_id}


def finish(data):
    if not isinstance(data,dict) or set(data)-{'id','status','digest','bytes','error'}:raise ValueError('Invalid completion')
    if data.get('status') not in ('succeeded','failed','interrupted'):raise ValueError('Invalid status')
    with SessionLocal.begin() as db:
        row=db.get(BackupRun,data['id'])
        if not row or row.status!='running':raise ValueError('Operation is not running')
        row.status,row.finished_at=data['status'],now()
        row.digest=data.get('digest','');row.bytes=max(0,int(data.get('bytes',0)))
        if row.digest and (len(row.digest)!=64 or any(c not in '0123456789abcdef' for c in row.digest)):raise ValueError('Invalid digest')
        row.error=data.get('error','')
        if row.error not in ('','busy','runner_failed','archive_invalid','recovered'):raise ValueError('Invalid error')
        db.add(__import__('lingxi.models',fromlist=['Audit']).Audit(user_id=row.user_id,workspace_id=row.workspace_id,action='backup_'+row.status,target_id=row.id))


def checkpoint():
    from .configuration_history import checkpoint as history_checkpoint
    from .workspace_migrations import checkpoint as migration_checkpoint
    from .system_settings import checkpoint as settings_checkpoint
    from .resources import checkpoint as resources_checkpoint
    from .media_policy import MediaPolicy
    from .media_retention import checkpoint as retention_checkpoint
    from .model_budgets import checkpoint as budget_checkpoint
    from .proactive_generation import ProactiveDraft
    from .plugin_agents import PluginAgentBinding
    from .plugin_events import PluginEvent, EventSubscription
    from .auth_totp import TwoFactor
    from .memory import replay_ledger
    from .models import Memory, Message
    from .memory_settings import MemoryConfiguration
    from .proactive import ContactPreference, ProactiveContact
    from . import proactive_completion
    from . import reminder_delivery
    from .proactive_channels import ProactiveDelivery
    from .plugins import PluginInvocation, PluginRelease, PluginInstallation
    from .channel_speech import checkpoint as speech_checkpoint
    from .channel_files import checkpoint as files_checkpoint
    from .channel_artifacts import checkpoint as artifacts_checkpoint
    from .media_storage import checkpoint as storage_checkpoint
    from .browser_notifications import checkpoint as browser_notice_checkpoint
    with SessionLocal.begin() as db:
        replay_ledger(db)
        users=[view(u) for u in restore_owners(db)]
        factors=[view(r) for r in db.scalars(select(TwoFactor))]
        if len(users)!=1:raise ValueError('Only single-owner restore supported')
        ledger=settings().runtime_dir/'deletions.jsonl'
        return {'format':1,'key_fingerprint':hashlib.sha256(settings().encryption_key.encode()).hexdigest(),
            'configuration_history':history_checkpoint(db, restore_owners(db)[0]),
            'migrations':migration_checkpoint(db),
            'users':users,'factors':factors,'ledger':ledger.read_text(encoding='utf-8') if ledger.exists() else '',
            'media_policies':[view(row) for row in db.scalars(select(MediaPolicy))],
            'media_retention':retention_checkpoint(db),
            'model_budgets':budget_checkpoint(db),
            'system_settings':settings_checkpoint(db),
            'resource_policies':resources_checkpoint(db),
            'channel_speech_receipts':speech_checkpoint(db),
            'channel_file_receipts':files_checkpoint(db),
            'channel_artifact_replies':artifacts_checkpoint(db),
            'media_storage_runs':storage_checkpoint(db),
            'browser_notices':browser_notice_checkpoint(db),
            'memories':[view(row) for row in db.scalars(select(Memory))],
            'memory_configurations':[view(row) for row in db.scalars(select(MemoryConfiguration))],
            'invalid_messages':[{'id':row.id,'status':row.status} for row in db.scalars(select(Message).where(Message.status.in_(['deleted','redacted','invalidated','superseded'])))],
            'topic_completions':proactive_completion.checkpoint(db),
            'reminder_deliveries':reminder_delivery.checkpoint(db),
            'contact_preferences':[view(row) for row in db.scalars(select(ContactPreference))],
            'proactive_drafts':[view(row) | {'status':'invalidated','content':'',
                'version':row.version+1,'error':'恢复隔离：保留计费屏障，不恢复构思正文'} for row in db.scalars(select(ProactiveDraft))],
            'proactive_deliveries':[view(row) for row in db.scalars(select(ProactiveDelivery))],
            # Only receipt metadata survives an older snapshot, never old personal text.
            'proactive_contacts':[view(row) | {'content':'','reason':'恢复隔离：仅保留发送屏障与回执',
                'status':'invalidated','conversation_id':None,'message_id':None}
                for row in db.scalars(select(ProactiveContact))
                if db.scalar(select(ProactiveDelivery.id).where(ProactiveDelivery.contact_id==row.id))],
            'backup_runs':[view(row) for row in db.scalars(select(BackupRun))],
            'plugin_agent_bindings':[view(row) for row in db.scalars(select(PluginAgentBinding))],
            # Carry current event dedupe/deletion markers, never bodies or signing keys.
            'plugin_events':[view(row, ('title', 'summary')) for row in db.scalars(select(PluginEvent))],
            'plugin_event_subscriptions':[view(row, ('encrypted_secret',)) for row in db.scalars(select(EventSubscription))],
            # Preserve API and MCP send markers across restoration of an older DB.
            # Keep the api_* checkpoint keys for compatibility with existing archives.
            # No credentials travel in this checkpoint.
            'api_invocations':[view(row) for row in db.scalars(select(PluginInvocation)) if row.config.get('api') or row.config.get('mcp')],
            'api_releases':[view(row) for row in db.scalars(select(PluginRelease))
                if any(c['kind'] in ('api','mcp','event','agent') for c in row.manifest['capabilities'])],
            'api_installations':[view(row) for row in db.scalars(select(PluginInstallation))
                if any(c['kind'] in ('api','mcp','event','agent') for c in db.get(PluginRelease,row.release_id).manifest['capabilities'])],
            'schema':db.scalar(__import__('sqlalchemy').text('select version_num from alembic_version'))}


def restore_owners(db):
    # QQ creates credentialless per-scope actors, not additional login owners.
    # A real second account still blocks this single-owner restore workflow.
    import re
    return [u for u in db.scalars(select(User)) if u.password_hash or
            not re.fullmatch(r'channel-[a-f0-9]{32}', u.username)]


def quarantine(data):
    """Restore latest identity, replay latest deletions, block all old external work."""
    from .auth_totp import TwoFactor
    from .models import LoginSession, Job, Provider, Memory, Message, Candidate, ChatRun
    from .channels import Channel, IdentityLink
    from .plugins import PluginInstallation, PluginRelease, PluginInvocation
    from .plugin_api import PluginConnection
    from .proactive import ContactPreference, ProactiveContact
    from . import proactive_completion
    from . import reminder_delivery
    from .proactive_channels import ContactRoute, ContactTarget, ProactiveDelivery, stop_delivery
    from .plugin_events import PluginEvent, EventSubscription, invalidate_subscription
    from .plugin_agents import PluginAgentBinding
    from .media_policy import MediaPolicy, MediaLimits
    from .memory import append_ledger, replay_ledger, MemoryService, enqueue, invalidate_source, invalidate_runs
    if data.get('format')!=1 or data['key_fingerprint']!=hashlib.sha256(settings().encryption_key.encode()).hexdigest():raise ValueError('Encryption key mismatch')
    if len(data['users'])!=1:raise ValueError('Invalid owner checkpoint')
    from .system_settings import validate_checkpoint as validate_settings, restore as restore_settings
    validate_settings(data['system_settings'], data['users'][0])
    from .configuration_history import validate_checkpoint as validate_history, restore as restore_history
    validate_history(data['configuration_history'], data['users'][0], data)
    from .resources import ResourceScope
    for record in data.get('resource_policies', []):
        if (record['user_id'] != data['users'][0]['id'] or record['workspace_id'] != data['users'][0]['workspace_id']
                or type(record['version']) is not int or record['version'] < 1):
            raise ValueError('Invalid resource checkpoint')
        ResourceScope(roots=record['roots'], read_roots=record.get('read_roots', []))
    policies = data['media_policies']
    for values in policies:
        if values['workspace_id'] != data['users'][0]['workspace_id']:
            raise ValueError('Invalid media policy workspace')
        MediaLimits.model_validate({key: values[key] for key in MediaLimits.model_fields})
        if type(values['version']) is not int or values['version'] < 1:
            raise ValueError('Invalid media policy version')
    from .media_retention import validate_checkpoint as validate_retention, restore as restore_retention
    validate_retention(data['media_retention'], data['users'][0])
    from .model_budgets import validate_checkpoint as validate_budgets, restore as restore_budgets
    validate_budgets(data['model_budgets'], data['users'][0])
    proactive_completion.validate_checkpoint(data.get('topic_completions', []), data['users'][0])
    reminder_delivery.validate_checkpoint(data.get('reminder_deliveries', []), data['users'][0])
    # Validate all records before changing the journal or database.
    entries=[json.loads(line) for line in data['ledger'].splitlines() if line.strip()]
    with SessionLocal.begin() as db:
        existing=restore_owners(db)
        if len(existing)!=1 or existing[0].id!=data['users'][0]['id']:raise ValueError('Owner mismatch')
        for values in data['users']:
            db.merge(User(**values))
        restore_settings(db, data['system_settings'], db.get(User, data['users'][0]['id']))
        restore_retention(db, data['media_retention'])
        restore_budgets(db, data['model_budgets'])
        for values in policies:
            db.merge(MediaPolicy(**(values | {'version': values['version']+1})))
        db.execute(delete(TwoFactor))
        for values in data['factors']:db.add(TwoFactor(**values))
        db.execute(delete(LoginSession))
        for values in data.get('api_releases',[]):db.merge(PluginRelease(**values))
        for values in data.get('api_installations',[]):db.merge(PluginInstallation(**values))
        for values in data.get('api_invocations',[]):db.merge(PluginInvocation(**values))
        for values in data.get('plugin_event_subscriptions',[]):db.merge(EventSubscription(**values))
        for values in data.get('plugin_events',[]):db.merge(PluginEvent(**values))
        for values in data.get('plugin_agent_bindings',[]):db.merge(PluginAgentBinding(**values))
        from .channel_speech import restore as restore_speech
        restore_speech(db, data.get('channel_speech_receipts', {}))
        from .channel_files import restore as restore_files
        restore_files(db, data.get('channel_file_receipts', {}))
        from .channel_artifacts import restore as restore_artifacts
        restore_artifacts(db,data.get('channel_artifact_replies',[]))
        from .media_storage import restore as restore_storage
        restore_storage(db, data.get('media_storage_runs', []))
        from .browser_notifications import restore as restore_browser_notices
        restore_browser_notices(db,data.get('browser_notices',{}))
        db.flush()
        for binding in db.scalars(select(PluginAgentBinding)):
            binding.status, binding.epoch, binding.version = 'revoked', binding.epoch+1, binding.version+1
        for sub in db.scalars(select(EventSubscription)):
            invalidate_subscription(db, sub)
        for event in db.scalars(select(PluginEvent)):
            event.title, event.summary = '', ''
            if event.status != 'deleted':event.status = 'invalidated'
        for connection in db.scalars(select(PluginConnection)):
            from .plugin_oauth import revoke_local
            revoke_local(db, connection)
            connection.status,connection.encrypted_secret,connection.checked_at='revoked','',None
            connection.version+=1
        for p in db.scalars(select(Provider)):p.enabled=False;p.version+=1
        for channel in db.scalars(select(Channel)):channel.enabled=False;channel.version+=1;channel.connection_id=None;channel.lease_until=0
        for link in db.scalars(select(IdentityLink)):link.status='revoked';link.allow_memory=False;link.version+=1
        for plugin in db.scalars(select(PluginInstallation)):
            if plugin.status=='enabled':plugin.status='disabled'
            plugin.grants=[];plugin.version+=1
        from .plugin_store import StoreConnection
        for connection in db.scalars(select(StoreConnection)):connection.enabled=False;connection.version+=1
        for values in data['memories']:db.merge(Memory(**values))
        for values in data['invalid_messages']:
            message=db.get(Message,values['id'])
            if message:message.status=values['status'];message.content='';invalidate_source(db,message.id)
        for values in data['contact_preferences']:db.merge(ContactPreference(**values))
        from .proactive_generation import ProactiveDraft
        for values in data.get('proactive_drafts',[]):db.merge(ProactiveDraft(**values))
        for values in data.get('proactive_contacts',[]):db.merge(ProactiveContact(**values))
        for values in data.get('proactive_deliveries',[]):db.merge(ProactiveDelivery(**values))
        proactive_completion.restore(db, data.get('topic_completions', []))
        reminder_delivery.restore(db, data.get('reminder_deliveries', []))
        for values in data['backup_runs']:db.merge(BackupRun(**values))
        db.flush()
        for preference in db.scalars(select(ContactPreference)):preference.enabled=False;preference.version+=1
        from .proactive_generation import invalidate as invalidate_draft
        invalidate_draft(db,reason='恢复隔离：旧构思不自动重放或投递')
        for route in db.scalars(select(ContactRoute)):route.target=ContactTarget().model_dump()
        for delivery in db.scalars(select(ProactiveDelivery)):
            stop_delivery(delivery,'恢复隔离：旧主动消息不自动重放')
        for job in db.scalars(select(Job).where(Job.status.in_(['pending','running']))):
            job.status='failed';job.error='恢复隔离：旧作业不可自动重放';job.lease_token='';job.lease_until=0
        # Queued side effects and subscriptions stay quarantined until reauthorized.
        from .channels import ChannelDelivery
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.kind == 'record')):
            from .channel_speech import discard
            discard(delivery, '恢复隔离：旧语音不重放')
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.status.in_(['pending','sending']))):delivery.status='unknown'
        from .channels import ChannelInbound
        from .channel_tasks import ChannelTask, discard as discard_channel_task
        for task in db.scalars(select(ChannelTask)):
            discard_channel_task(db, task, '恢复隔离：旧 QQ 协作授权与通知不重放')
        from .channel_media import cancel as cancel_attachments
        for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.status.in_(['queued','ingesting','running','ready']))):
            cancel_attachments(db, incoming, '恢复隔离：旧附件接收不自动重放')
            incoming.status = 'cancelled'
        for candidate in db.scalars(select(Candidate).where(Candidate.status=='pending')):
            candidate.status='invalidated'
            if candidate.origin:
                candidate.content=''
                candidate.origin={k:v for k,v in candidate.origin.items() if k in ('kind','run_id','node_id','round')}
        from .emotions import EmotionObservation, discard
        from .assistants import SupportPreference
        for observation in db.scalars(select(EmotionObservation)):
            discard(db, observation)
        for pref in db.scalars(select(SupportPreference)):
            pref.spec = pref.spec | {'auto_observe': False, 'self_report': '', 'report_expires_at': None}
            pref.version += 1
        for run in db.scalars(select(ChatRun).where(ChatRun.status.in_(['queued','running']))):run.status='failed';run.cancel_requested=True
        from .tasks import WorkTask, Reminder
        from .delegation import AgentRun, AgentNode
        from .plugins import PluginInvocation
        for model in (WorkTask,AgentRun,AgentNode):
            for task in db.scalars(select(model).where(model.status.in_(['queued','running','pending','planning','waiting_children']))):task.status='failed'
        for task in db.scalars(select(PluginInvocation).where(PluginInvocation.status.in_(['preview','queued','running']))):
            sent=(task.config.get('api') or task.config.get('mcp',{})).get('dispatch')=='in_flight'
            task.status='unknown' if sent else 'invalidated'
            task.result='';task.error='恢复隔离：外部结果待核对，不会重发' if sent else '恢复隔离：未发送的调用已失效'
            task.version+=1;task.finished_at=now()
        for reminder in db.scalars(select(Reminder).where(Reminder.status=='scheduled')):reminder.status='cancelled';reminder.version+=1
        for policy in db.scalars(select(BackupPolicy)):policy.enabled=False;policy.next_at=0;policy.version+=1
        from .voice import VoiceSession
        for voice in db.scalars(select(VoiceSession).where(VoiceSession.status=='active')):voice.status='closed'
        from .voice_realtime import RealtimeTicket
        for ticket in db.scalars(select(RealtimeTicket).where(RealtimeTicket.status.in_(('issued','active')))):
            ticket.status='cancelled'
        from .proactive_voice import invalidate as invalidate_voice_contacts
        for owner in existing:invalidate_voice_contacts(db,user_id=owner.id)
        for row in db.scalars(select(BackupRun).where(BackupRun.status.in_(['queued','running']))):
            row.status=data.get('restore_outcome','succeeded') if row.kind=='restore' else 'interrupted';row.error='recovered';row.finished_at=now()
        for entry in entries:append_ledger(entry)
        replay_ledger(db)
        from .workspace_migrations import restore as restore_migration
        restore_migration(db, data.get('migrations'))
        from .resources import restore as restore_resources
        restore_resources(db, data.get('resource_policies', []), db.get(User, data['users'][0]['id']))
        # Indexes are derived data. Rebuild from current authority, and make
        # physical deletion cleanup retryable without replaying old user work.
        restored=uid()
        # A restored bank may predate a model switch. Never reuse that bank's
        # vectors; retain the latest mode but require fresh external validation.
        from .memory_settings import MemoryConfiguration, MemoryFlight, configuration, selection
        for values in data.get('memory_configurations', []):
            row = configuration(db, values['workspace_id'], create=True)
            row.mode = values['mode']
            row.epoch = max(row.epoch, values['epoch'])+1
            row.version = max(row.version, values['version'])+1
            row.phase, row.pending, row.verification, row.rebuild_id = 'ready', {}, {}, None
            row.embedding_fingerprint = selection(db, 'embedding', row.workspace_id)['fingerprint']
        for item in db.scalars(select(MemoryFlight).where(MemoryFlight.status.in_(('running','unknown')))):
            item.status = 'resolved'  # Host restore stopped Hindsight before quarantine.
        for memory in list(db.scalars(select(Memory))):
            invalidate_runs(db,memory.id)
            if memory.status=='deleted':MemoryService().forget(db,memory,journal=False)
            elif memory.status=='active':
                memory.index_status='pending'
                enqueue(db,f'restore-index:{restored}:{memory.id}','index',{'memory_id':memory.id,'version':memory.version})
        user=existing[0]
        restore_history(db, data['configuration_history'], db.get(User,user.id))
        audit(db,user,'backup_restore_quarantine')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['claim','finish','checkpoint','quarantine','idle','begin-restore','release-maintenance']);args=parser.parse_args()
    if args.action=='claim':print(json.dumps(agent_claim()))
    elif args.action=='finish':finish(json.load(sys.stdin));print('{}')
    elif args.action=='checkpoint':print(json.dumps(checkpoint()))
    elif args.action=='quarantine':quarantine(json.load(sys.stdin));print('{}')
    elif args.action=='begin-restore':
        data=json.load(sys.stdin)
        with SessionLocal.begin() as db:
            user=db.scalar(select(User).with_for_update().limit(1))
            if not user or db.scalar(select(BackupRun).where(BackupRun.status.in_(['queued','running']))):raise ValueError('Pending host operation')
            import re
            if not re.fullmatch('[a-f0-9]{32}',data['archive_id']):raise ValueError('Invalid archive identifier')
            row=BackupRun(user_id=user.id,workspace_id=user.workspace_id,request_id=uid(),kind='restore',archive_id=data['archive_id'],status='running')
            db.add(row);db.flush();audit(db,user,'backup_restore_started',row.id)
            print(json.dumps({'id':row.id,'archive_id':row.archive_id,'kind':'restore'}))
    elif args.action=='release-maintenance':
        data=json.load(sys.stdin)
        with SessionLocal.begin() as db:
            row=db.get(BackupRun,data['id'])
            if row and row.status=='running':row.status='interrupted';row.error='recovered';row.finished_at=now()
        print('{}')
    else:
        from .models import Job
        from .voice import VoiceSession
        from .voice_realtime import RealtimeTicket
        with SessionLocal() as db:
            busy=db.scalar(select(func.count()).select_from(Job).where(Job.status=='running',Job.lease_until>now()))
            voice=db.scalar(select(func.count()).select_from(VoiceSession).where(VoiceSession.status=='active',VoiceSession.expires_at>now()))
            realtime=db.scalar(select(func.count()).select_from(RealtimeTicket).where(RealtimeTicket.status=='active',RealtimeTicket.expires_at>now()))
            print(json.dumps({'idle':not busy and not voice and not realtime}))


if __name__=='__main__':main()
