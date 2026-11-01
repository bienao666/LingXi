"""Owner-scoped operational metadata. Never serialize business payloads or secrets."""
import base64
import hashlib
import json
import shutil
from typing import Any, Literal
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, case, delete, func, literal, or_, and_, select, union_all
from sqlalchemy.orm import Mapped, mapped_column
from .config import settings
from .db import Base, SessionLocal, now
from .models import Audit, ChatRun, Entity, Job, ModelCall, User
from .web import Context, audit, owned
from .configuration_history import tracked
from .trace_graph import TraceGraph, trace_graph
from .audio_costs import AudioUsageView, summary as audio_summary

router=APIRouter()


class WorkerState(Base):
    __tablename__='worker_states'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    started_at: Mapped[float]=mapped_column(Float)
    seen_at: Mapped[float]=mapped_column(Float,index=True)
    slots: Mapped[int]=mapped_column(Integer)
    active: Mapped[int]=mapped_column(Integer)


class RuntimeEvent(Entity,Base):
    __tablename__='runtime_events'
    component: Mapped[str]=mapped_column(String(30))
    code: Mapped[str]=mapped_column(String(60))
    level: Mapped[str]=mapped_column(String(20))
    reference: Mapped[str|None]=mapped_column(String(64),nullable=True)


class OpsPreference(Entity,Base):
    __tablename__='ops_preferences'
    __table_args__=(UniqueConstraint('user_id','workspace_id'),)
    user_id: Mapped[str]=mapped_column(String(64),index=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    log_retention_days: Mapped[int]=mapped_column(Integer,default=90)
    daily_token_alert: Mapped[int]=mapped_column(Integer,default=0)


class DiagnosticBundle(Entity,Base):
    __tablename__='diagnostic_bundles'
    __table_args__=(UniqueConstraint('user_id','workspace_id','request_id'),)
    user_id: Mapped[str]=mapped_column(String(64),index=True)
    session_id: Mapped[str]=mapped_column(String(64))
    request_id: Mapped[str]=mapped_column(String(100))
    request_hash: Mapped[str]=mapped_column(String(64))
    payload: Mapped[str]=mapped_column(Text)
    digest: Mapped[str]=mapped_column(String(64))
    status: Mapped[str]=mapped_column(String(20),default='preview')
    expires_at: Mapped[float]=mapped_column(Float)


EVENTS={'job_attempt_failed':('error','后台作业失败'), 'store_sync_failed':('warning','商店目录同步不可用'),
        'proactive_tick_failed':('error','主动交流调度不可用'), 'worker_interrupted':('error','worker 执行中断')}
COMPONENTS={'worker','store','proactive'}
KINDS={'chat','task','agent','plugin'}
SECTIONS={'health','usage','logs','audit'}


def event(db,code,component='worker',workspace='personal',reference=None):
    if code not in EVENTS or component not in COMPONENTS:raise ValueError('Unknown operational event')
    db.add(RuntimeEvent(workspace_id=workspace,code=code,component=component,level=EVENTS[code][0],reference=reference))


def safe_event(code,component='worker'):
    try:
        with SessionLocal.begin() as db:event(db,code,component)
    except Exception:pass  # Observability must never mask the original failure.


def heartbeat(identity,started,active):
    with SessionLocal.begin() as db:
        row=db.get(WorkerState,identity)
        if not row:
            row=WorkerState(id=identity,started_at=started,seen_at=now(),slots=settings().worker_concurrency,active=active)
            db.add(row)
        else:row.seen_at,row.active=now(),active


def workers(db):
    rows=list(db.scalars(select(WorkerState).where(WorkerState.seen_at>now()-86400).order_by(WorkerState.seen_at.desc()).limit(50)))
    available=[r for r in rows if r.seen_at>=now()-45]
    return {'status':'ready' if available else 'unavailable','online':len(available),
        'last_seen':max((r.seen_at for r in rows),default=None),
        'slots':sum(r.slots for r in available),'busy':sum(r.active for r in available),
        'note':'依据 10 秒心跳；超过 45 秒未续约视为不可用，不将空闲当离线。'}


def count_by(db,entity,workspace,column):
    return {k:int(v) for k,v in db.execute(select(getattr(entity,column),func.count()).where(entity.workspace_id==workspace).group_by(getattr(entity,column)))}


def overview(db,user):
    try:
        usage=shutil.disk_usage(settings().runtime_dir)
        disk={'status':'warning' if usage.free<max(1024**3,usage.total//20) else 'ready',
              'total_bytes':usage.total,'free_bytes':usage.free,'used_bytes':usage.used}
    except OSError:disk={'status':'unavailable','total_bytes':None,'free_bytes':None,'used_bytes':None}
    oldest=db.scalar(
        select(func.min(case((Job.available_at>Job.created_at,Job.available_at),else_=Job.created_at))).where(
            Job.workspace_id==user.workspace_id,Job.status=='pending',Job.available_at<=now()))
    return {'worker':workers(db),'queue':count_by(db,Job,user.workspace_id,'status'),
            'oldest_ready_seconds':max(0,now()-oldest) if oldest else 0,'disk':disk,'checked_at':now()}


def prefs(db,user):
    row=db.scalar(select(OpsPreference).where(OpsPreference.user_id==user.id,OpsPreference.workspace_id==user.workspace_id))
    return {'version':row.version if row else 0,'log_retention_days':row.log_retention_days if row else 90,
        'daily_token_alert':row.daily_token_alert if row else 0}


class OpsPreferenceInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=0)
    log_retention_days:int=Field(ge=7,le=365)
    daily_token_alert:int=Field(ge=0,le=2_000_000_000)


class OpsPreferenceView(OpsPreferenceInput):pass


class OperationalHealth(BaseModel):
    worker:dict[str,Any]
    queue:dict[str,int]
    oldest_ready_seconds:float
    disk:dict[str,Any]
    checked_at:float


class MetadataPage(BaseModel):
    items:list[dict[str,Any]]
    next_cursor:str|None
    note:str=''


class UsageView(BaseModel):
    audio: AudioUsageView = Field(default_factory=AudioUsageView)
    days:int
    calls:int
    failed:int
    known_tokens:int
    unknown_usage_calls:int
    cost:float|None
    groups:list[dict[str,Any]]
    daily:dict[str,Any]
    note:str
    currencies:list[dict[str,Any]]
    unknown_cost_calls:int
    breakdowns:dict[str,Any]


class TraceView(BaseModel):
    id:str
    kind:str
    created_at:float
    status:str
    calls:list[dict[str,Any]]
    events:list[dict[str,Any]]
    truncated:bool
    versions:dict[str,Any]
    note:str
    graph:TraceGraph


class DiagnosticView(BaseModel):
    id:str
    created_at:float
    expires_at:float
    digest:str
    status:str
    bytes:int
    download_url:str|None
    preview:dict[str,Any]|None=None


@router.get('/api/admin/operations/preferences',response_model=OpsPreferenceView)
def preferences(ctx:Context):
    db,user,_=ctx
    return prefs(db,user)


@router.put('/api/admin/operations/preferences',response_model=OpsPreferenceView)
@tracked
def save_preferences(body:OpsPreferenceInput,ctx:Context):
    db,user,_=ctx
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    if body.version!=prefs(db,user)['version']:raise HTTPException(409,'运维配置已变化，请重新加载后核对')
    row=db.scalar(select(OpsPreference).where(OpsPreference.user_id==user.id,OpsPreference.workspace_id==user.workspace_id))
    if not row:
        row=OpsPreference(user_id=user.id,workspace_id=user.workspace_id)
        db.add(row)
    else:row.version+=1
    row.log_retention_days,row.daily_token_alert=body.log_retention_days,body.daily_token_alert
    db.flush()
    audit(db,user,'operations_preferences',row.id)
    return prefs(db,user)


@router.get('/api/admin/operations/health',response_model=OperationalHealth)
def health(ctx:Context):
    db,user,_=ctx
    return overview(db,user)


def encode_cursor(row):
    return base64.urlsafe_b64encode(json.dumps([row['created_at'],row['id']]).encode()).decode()


def decode_cursor(value):
    try:
        data=json.loads(base64.urlsafe_b64decode(value))
        if len(data)!=2 or type(data[0]) not in (int,float) or not 0<=data[0]<=now()+60 or not isinstance(data[1],str) or len(data[1])>128:raise ValueError()
        return data
    except Exception:raise HTTPException(422,'分页位置无效，请重新加载') from None


def page_query(db,query,entity,cursor,limit=50):
    if cursor:
        at,identity=decode_cursor(cursor)
        query=query.where(or_(entity.created_at<at,and_(entity.created_at==at,entity.id<identity)))
    rows=list(db.scalars(query.order_by(entity.created_at.desc(),entity.id.desc()).limit(limit+1)))
    more=len(rows)>limit
    rows=rows[:limit]
    return rows,encode_cursor({'created_at':rows[-1].created_at,'id':rows[-1].id}) if more else None


@router.get('/api/admin/audit',response_model=MetadataPage)
def audits(ctx:Context,cursor:str=Query(default='',max_length=512),action:str=Query(default='',max_length=100)):
    db,user,_=ctx
    query=select(Audit).where(Audit.user_id==user.id,Audit.workspace_id==user.workspace_id)
    if action:query=query.where(Audit.action==action)
    rows,next_cursor=page_query(db,query,Audit,cursor)
    return {'items':[{'id':r.id,'created_at':r.created_at,'action':r.action,'target_id':r.target_id} for r in rows],'next_cursor':next_cursor}


def log_view(row):
    return {'id':row.id,'created_at':row.created_at,'component':row.component,'code':row.code,
        'level':row.level,'message':EVENTS.get(row.code,('info','其他运行事件'))[1],'reference':row.reference}


@router.get('/api/admin/logs',response_model=MetadataPage)
def logs(ctx:Context,cursor:str=Query(default='',max_length=512),level:Literal['all','warning','error']='all'):
    db,user,_=ctx
    query=select(RuntimeEvent).where(RuntimeEvent.workspace_id==user.workspace_id)
    if level!='all':query=query.where(RuntimeEvent.level==level)
    rows,next_cursor=page_query(db,query,RuntimeEvent,cursor)
    return {'items':[log_view(r) for r in rows],'next_cursor':next_cursor,'note':'结构化运行事件，不读取容器 stdout 或模型正文；历史事件不追溯补造。'}


def tokens(value):
    if not isinstance(value,dict):return None
    def integer(key):
        v=value.get(key)
        return v if type(v) is int and 0<=v<=10**12 else None
    total=integer('total_tokens')
    if total is not None:return total
    p,c=integer('prompt_tokens'),integer('completion_tokens')
    if p is None:p=integer('input_tokens')
    if c is None:c=integer('output_tokens')
    return p+c if p is not None and c is not None else None


def usage(db,user,days=7):
    # Bounded complete scan: oversized windows fail, never silently undercount.
    calls=list(db.scalars(select(ModelCall).where(ModelCall.workspace_id==user.workspace_id,ModelCall.created_at>=now()-days*86400)
        .order_by(ModelCall.created_at,ModelCall.id).limit(100001)))
    if len(calls)>100000:raise HTTPException(413,'所选时间内调用超过统计预算，请缩小时间范围')
    groups={};group_calls={};known=0;known_calls=0;failed=0;today=0;today_unknown=0
    from datetime import datetime,timezone
    midnight=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    for c in calls:
        key=(c.model_id,c.purpose)
        group_calls.setdefault(key,[]).append(c)
        g=groups.setdefault(key,{'model_id':c.model_id,'purpose':c.purpose,'calls':0,'failed':0,'known_tokens':0,'unknown_usage_calls':0,'duration_ms':0,'cost':None})
        t=tokens(c.usage)
        g['calls']+=1;g['duration_ms']+=max(0,c.duration_ms);g['failed']+=int(c.status=='failed');failed+=int(c.status=='failed')
        if t is None:g['unknown_usage_calls']+=1
        else:g['known_tokens']+=t;known+=t;known_calls+=1
        if c.created_at>=midnight:
            if t is None:today_unknown+=1
            else:today+=t
    threshold=prefs(db,user)['daily_token_alert']
    from .usage_costs import money_summary, breakdown
    for key,group in groups.items():
        group.update(money_summary(group_calls[key]))
    return {'days':days,'calls':len(calls),'failed':failed,'known_tokens':known,'unknown_usage_calls':len(calls)-known_calls,
        'audio':audio_summary(calls),
        'cost':None,'groups':sorted(groups.values(),key=lambda x:(-x['calls'],x['model_id'],x['purpose'])),
        **money_summary(calls), 'breakdowns':{dimension:breakdown(calls,dimension) for dimension in ('channel','model','task','plugin')},
        'daily':{'timezone':'UTC','known_tokens':today,'unknown_usage_calls':today_unknown,'alert_threshold':threshold,
            'alert':bool(threshold and today>=threshold),'enforcement':'alert_only'},
        'note':'费用是调用开始时用户单价的估算，不是供应商账单。不同币种分别合计，不换汇；未知费用另列，不回填旧调用。语音只在明确配置匹配的时长／字符单价后估算；其他计费方式保持未知。日阈值按 UTC token 告警，不阻止调用。'}


@router.get('/api/admin/usage',response_model=UsageView)
def usage_summary(ctx:Context,days:int=Query(default=7,ge=1,le=30)):
    db,user,_=ctx
    return usage(db,user,days)


def trace_entities():
    from .tasks import WorkTask
    from .delegation import AgentRun
    from .plugins import PluginInvocation
    return {'chat':ChatRun,'task':WorkTask,'agent':AgentRun,'plugin':PluginInvocation}


@router.get('/api/admin/traces',response_model=MetadataPage)
def traces(ctx:Context,cursor:str=Query(default='',max_length=512),kind:Literal['all','chat','task','agent','plugin']='all'):
    db,user,_=ctx
    entities=trace_entities()
    queries=[]
    # Type is part of the stable sort key; equal timestamps cannot skip records.
    for name,entity in entities.items():
        if kind not in ('all',name):continue
        queries.append(select(entity.id.label('entity_id'),(literal(name)+literal(':')+entity.id).label('id'),
            literal(name).label('kind'),entity.created_at.label('created_at'),entity.status.label('status')).where(
            entity.user_id==user.id,entity.workspace_id==user.workspace_id))
    rows=union_all(*queries).subquery()
    query=select(rows)
    if cursor:
        at,identity=decode_cursor(cursor)
        query=query.where(or_(rows.c.created_at<at,and_(rows.c.created_at==at,rows.c.id<identity)))
    items=[dict(r) for r in db.execute(query.order_by(rows.c.created_at.desc(),rows.c.id.desc()).limit(51)).mappings()]
    return {'items':items[:50],'next_cursor':encode_cursor(items[49]) if len(items)>50 else None}


SAFE_CONFIG={'provider_version','policy_version','prompt_version','persona_version','role_version','plugin_version','skill_version','voice_config_version','audio_transport'}
def call_view(c):
    return {'id':c.id,'created_at':c.created_at,'model_id':c.model_id,'purpose':c.purpose,'status':c.status,
        'billing':c.billing or {'status':'unknown','reason':'historical'},
        'duration_ms':c.duration_ms,'known_tokens':tokens(c.usage),'versions':{k:v for k,v in c.config.items() if k in SAFE_CONFIG and type(v) in (str,int,float,type(None))}}


@router.get('/api/admin/traces/{kind}/{identity}',response_model=TraceView)
def trace_detail(kind:Literal['chat','task','agent','plugin'],identity:str,ctx:Context):
    db,user,_=ctx
    row=owned(db,trace_entities()[kind],identity,user)
    if kind=='chat':condition=ModelCall.run_id==identity
    else:condition=ModelCall.config[{'task':'task_id','agent':'agent_run_id','plugin':'invocation_id'}[kind]].as_string()==identity
    calls=list(db.scalars(select(ModelCall).where(ModelCall.workspace_id==user.workspace_id,condition).order_by(ModelCall.created_at,ModelCall.id).limit(201)))
    events=[]
    if kind=='task':
        from .tasks import TaskEvent
        events=[{'seq':e.seq,'created_at':e.created_at,'kind':e.kind} for e in db.scalars(select(TaskEvent).where(TaskEvent.task_id==identity).order_by(TaskEvent.seq).limit(201))]
    elif kind=='agent':
        from .delegation import AgentEvent
        events=[{'seq':i,'created_at':e.created_at,'kind':e.kind} for i,e in enumerate(db.scalars(select(AgentEvent).where(AgentEvent.run_id==identity).order_by(AgentEvent.created_at,AgentEvent.id).limit(201)),1)]
    elif kind=='chat':
        from .models import RunEvent
        # RunEvent has no timestamp. Do not fabricate one or expose delta text.
        events=[{'seq':e.seq,'created_at':None,'kind':e.kind} for e in db.scalars(select(RunEvent).where(RunEvent.run_id==identity,RunEvent.kind!='delta').order_by(RunEvent.seq).limit(201))]
    return {'id':identity,'kind':kind,'created_at':row.created_at,'status':row.status,
        'calls':[call_view(c) for c in calls[:200]],'events':events[:200],'truncated':len(calls)>200 or len(events)>200,
        'versions':{k:v for k,v in row.config.items() if k in SAFE_CONFIG and type(v) in (str,int,float,type(None))},
        'graph':trace_graph(db,user,kind,row),
        'note':'仅展示实际记录的阶段、配置版本与模型调用；不含提示词正文、用户内容或私有思维。历史任务缺少关联时不推测补全。'}


def ref(value):return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else None
def diagnostic_data(db,user,sections,days):
    data={'format':'lingxi-diagnostics-v1','created_at':now(),'days':days,'sections':sections,
          'redaction':'metadata_only; identifiers hashed; no prompts, content, URLs or credentials'}
    if 'health' in sections:data['health']=overview(db,user)
    if 'usage' in sections:
        u=usage(db,user,days)
        u['groups']=[dict(g,model_id=ref(g['model_id'])) for g in u['groups']]
        for dimension, group in u['breakdowns'].items():
            if dimension!='channel':
                group['items']=[dict(row,key=ref(row['key'])) for row in group['items']]
        data['usage']=u
    if 'logs' in sections:
        rows=list(db.scalars(select(RuntimeEvent).where(RuntimeEvent.workspace_id==user.workspace_id,RuntimeEvent.created_at>=now()-days*86400).order_by(RuntimeEvent.created_at.desc(),RuntimeEvent.id).limit(201)))
        data['logs']=[dict(log_view(r),id=ref(r.id),reference=ref(r.reference)) for r in rows[:200]]
        data['logs_truncated']=len(rows)>200
    if 'audit' in sections:
        rows=list(db.scalars(select(Audit).where(Audit.user_id==user.id,Audit.workspace_id==user.workspace_id,Audit.created_at>=now()-days*86400).order_by(Audit.created_at.desc(),Audit.id).limit(201)))
        # Actions are internal event codes, never user-provided notes or text.
        data['audit']=[{'id':ref(r.id),'created_at':r.created_at,'action':r.action,'target_ref':ref(r.target_id)} for r in rows[:200]]
        data['audit_truncated']=len(rows)>200
    return data


class DiagnosticInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=8,max_length=100)
    days:int=Field(default=7,ge=1,le=30)
    sections:list[Literal['health','usage','logs','audit']]=Field(default=['health','usage'],min_length=1,max_length=4)


class DiagnosticConfirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    digest:str=Field(pattern='^[a-f0-9]{64}$')


def bundle_view(row,preview=False):
    result={'id':row.id,'created_at':row.created_at,'expires_at':row.expires_at,'digest':row.digest,
        'status':row.status if row.expires_at>now() else 'expired','bytes':len(row.payload.encode()),
        'download_url':f'/api/admin/diagnostics/{row.id}/download' if row.status=='confirmed' and row.expires_at>now() else None}
    if preview:result['preview']=json.loads(row.payload) if row.payload else None
    return result


@router.post('/api/admin/diagnostics/preview',response_model=DiagnosticView)
def diagnostic_preview(body:DiagnosticInput,ctx:Context):
    db,user,login=ctx
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    fingerprint=hashlib.sha256(json.dumps(body.model_dump(),sort_keys=True).encode()).hexdigest()
    row=db.scalar(select(DiagnosticBundle).where(DiagnosticBundle.user_id==user.id,DiagnosticBundle.workspace_id==user.workspace_id,DiagnosticBundle.request_id==body.request_id))
    if row:
        if row.request_hash!=fingerprint or row.session_id!=login.id:raise HTTPException(409,'诊断请求编号已使用，请重新预览')
        if row.expires_at<=now():raise HTTPException(409,'诊断预览已过期，请重新生成')
        return bundle_view(row,True)
    recent=db.scalar(select(func.count()).select_from(DiagnosticBundle).where(DiagnosticBundle.user_id==user.id,DiagnosticBundle.created_at>now()-60))
    if recent>=5:raise HTTPException(429,'诊断预览过于频繁，请一分钟后重试')
    payload=json.dumps(diagnostic_data(db,user,sorted(set(body.sections)),body.days),ensure_ascii=False,sort_keys=True)
    if len(payload.encode())>1024**2:raise HTTPException(413,'诊断资料超过 1 MiB，请缩小时间或范围')
    row=DiagnosticBundle(user_id=user.id,workspace_id=user.workspace_id,session_id=login.id,request_id=body.request_id,request_hash=fingerprint,
        payload=payload,digest=hashlib.sha256(payload.encode()).hexdigest(),expires_at=now()+300)
    db.add(row);db.flush();audit(db,user,'diagnostic_preview',row.id)
    return bundle_view(row,True)


@router.get('/api/admin/diagnostics',response_model=list[DiagnosticView])
def diagnostic_list(ctx:Context):
    db,user,_=ctx
    return [bundle_view(r) for r in db.scalars(select(DiagnosticBundle).where(DiagnosticBundle.user_id==user.id,DiagnosticBundle.workspace_id==user.workspace_id).order_by(DiagnosticBundle.created_at.desc()).limit(50))]


def valid_bundle(db,user,login,identity):
    row=owned(db,DiagnosticBundle,identity,user,lock=True)
    if row.session_id!=login.id:raise HTTPException(403,'诊断文件绑定生成预览时的登录会话，请在当前会话重新预览')
    if row.expires_at<=now() or not row.payload:raise HTTPException(410,'诊断文件已过期，请重新生成')
    if hashlib.sha256(row.payload.encode()).hexdigest()!=row.digest:raise HTTPException(409,'诊断摘要不一致，请重新生成')
    return row


@router.post('/api/admin/diagnostics/{identity}/confirm',response_model=DiagnosticView)
def diagnostic_confirm(identity:str,body:DiagnosticConfirm,ctx:Context):
    db,user,login=ctx
    row=valid_bundle(db,user,login,identity)
    if body.digest!=row.digest:raise HTTPException(409,'诊断预览已变化，请重新核对')
    if row.status!='confirmed':
        row.status='confirmed';audit(db,user,'diagnostic_confirm',row.id)
    return bundle_view(row)


@router.get('/api/admin/diagnostics/{identity}/download')
def diagnostic_download(identity:str,ctx:Context):
    db,user,login=ctx
    row=valid_bundle(db,user,login,identity)
    if row.status!='confirmed':raise HTTPException(409,'请先核对诊断预览并确认导出')
    audit(db,user,'diagnostic_download',row.id)
    return Response(row.payload,media_type='application/json',headers={'Content-Disposition':'attachment; filename="lingxi-diagnostics.json"'})


def maintenance():
    with SessionLocal.begin() as db:
        from .auth_totp import prune_pending
        prune_pending(db)
        # Only structured operational logs expire. Never prune evidence, audit,
        # deletion ledgers, task checkpoints or model accounting with this switch.
        workspaces=set(db.scalars(select(RuntimeEvent.workspace_id).distinct()))
        for workspace in workspaces:
            days=db.scalar(select(func.max(OpsPreference.log_retention_days)).where(OpsPreference.workspace_id==workspace)) or 90
            db.execute(delete(RuntimeEvent).where(RuntimeEvent.workspace_id==workspace,RuntimeEvent.created_at<now()-days*86400))
        for row in db.scalars(select(DiagnosticBundle).where(DiagnosticBundle.expires_at<=now(),DiagnosticBundle.payload!='')):
            row.payload='';row.status='expired'
        db.execute(delete(DiagnosticBundle).where(DiagnosticBundle.created_at<now()-90*86400))
        db.execute(delete(WorkerState).where(WorkerState.seen_at<now()-7*86400))
