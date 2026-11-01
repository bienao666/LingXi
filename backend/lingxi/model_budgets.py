"""Durable admission limits for upstream model requests, never a billing promise.

Every POST / WebSocket upgrade is charged before transport. No refund for an
unknown outcome, failed connection, cancellation, worker restart or new policy.
Only metadata is retained. Model directories and health GETs remain available.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, Index, Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal, now, uid
from .models import ChatRun, Provider
from .security import decrypt, encrypt
from .web import Context, audit
from .configuration_history import tracked

router = APIRouter()


class ModelBudget(Base):
    __tablename__ = 'model_budgets'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)
    execution_limit: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_hash: Mapped[str] = mapped_column(String(64), default='')


class ModelDispatch(Base):
    __tablename__ = 'model_dispatches'
    __table_args__ = (
        Index('ix_model_dispatches_daily', 'workspace_id', 'created_at'),
        Index('ix_model_dispatches_execution', 'workspace_id', 'root_kind', 'root_id'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    provider_id: Mapped[str] = mapped_column(String(64))
    root_kind: Mapped[str] = mapped_column(String(20))
    root_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='reserved')
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Limits(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    daily_limit: int = Field(default=0, ge=0, le=1000000)
    execution_limit: int = Field(default=0, ge=0, le=10000)


class BudgetPreview(Limits):
    version: int = Field(ge=0)


class BudgetConfirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=32, max_length=8192)
    acknowledgement: Literal['request_attempts_not_money']


class ExecutionUsage(BaseModel):
    kind: str
    id: str
    attempts: int
    last_at: float


class BudgetView(Limits):
    version: int
    utc_day: str
    today_attempts: int
    unsettled_attempts: int
    recent_executions: list[ExecutionUsage]
    note: str


class BudgetPreviewView(BaseModel):
    token: str
    version: int
    before: Limits
    after: Limits
    today_attempts: int
    expires_at: float
    note: str


NOTE = ('0 表示不限制。按 UTC 日及单次执行统计上游模型请求尝试；失败、取消、断线及未知结果仍计数。'
        '更改限额不清空次数，已发出的请求不撤回。次数不是 token 或费用封顶；第三方工具费用不在本限额内。'
        '升级前请求没有预扣记录，不补算；后台记忆和语音计入每日额度。')


def locked(db, workspace):
    # INSERT establishes a write fence on SQLite; PG also locks the existing row.
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(insert(ModelBudget).values(workspace_id=workspace, version=0,
        daily_limit=0, execution_limit=0, confirmed_hash='').on_conflict_do_nothing())
    return db.scalar(select(ModelBudget).where(ModelBudget.workspace_id==workspace)
        .with_for_update().execution_options(populate_existing=True))


def values(row):
    return {'daily_limit':row.daily_limit if row else 0, 'execution_limit':row.execution_limit if row else 0}


def today(db, workspace, timestamp=None):
    start = int((now() if timestamp is None else timestamp)//86400)*86400
    return db.scalar(select(func.count()).select_from(ModelDispatch).where(
        ModelDispatch.workspace_id==workspace, ModelDispatch.created_at>=start, ModelDispatch.created_at<start+86400))


def view(db, workspace):
    from datetime import datetime, timezone
    row = db.get(ModelBudget, workspace)
    grouped = db.execute(select(ModelDispatch.root_kind, ModelDispatch.root_id,func.count(),func.max(ModelDispatch.created_at))
        .where(ModelDispatch.workspace_id==workspace,ModelDispatch.root_kind!='background')
        .group_by(ModelDispatch.root_kind,ModelDispatch.root_id)
        .order_by(func.max(ModelDispatch.created_at).desc(),ModelDispatch.root_kind,ModelDispatch.root_id).limit(50)).all()
    return values(row) | {'version':row.version if row else 0,
        'utc_day':datetime.fromtimestamp(now(),timezone.utc).date().isoformat(), 'today_attempts':today(db,workspace),
        'unsettled_attempts':db.scalar(select(func.count()).select_from(ModelDispatch).where(
            ModelDispatch.workspace_id==workspace,ModelDispatch.status=='reserved')),
        'recent_executions':[dict(kind=k,id=i,attempts=n,last_at=t) for k,i,n,t in grouped], 'note':NOTE}


@router.get('/api/admin/model-budgets', response_model=BudgetView)
def get_budget(ctx: Context):
    db,user,_=ctx
    return view(db,user.workspace_id)


@router.post('/api/admin/model-budgets/previews', response_model=BudgetPreviewView)
def preview(body: BudgetPreview, ctx: Context):
    db,user,session=ctx
    row=db.get(ModelBudget,user.workspace_id)
    if body.version!=(row.version if row else 0):raise HTTPException(409,'调用限额已变化，请重新加载')
    after=Limits.model_validate(body.model_dump(exclude={'version'})).model_dump()
    expires=now()+300
    token=encrypt(json.dumps({'purpose':'model-budget','user':user.id,'workspace':user.workspace_id,
        'session':session.id,'version':body.version,'after':after,'expires_at':expires}))
    return {'token':token,'version':body.version,'before':values(row),'after':after,
        'today_attempts':today(db,user.workspace_id),'expires_at':expires,'note':NOTE}


@router.put('/api/admin/model-budgets', response_model=BudgetView)
@tracked
def confirm(body: BudgetConfirm, ctx: Context):
    db,user,session=ctx
    try:
        data=json.loads(decrypt(body.token))
        if (data['purpose']!='model-budget' or data['user']!=user.id or
            data['workspace']!=user.workspace_id or data['session']!=session.id or
            type(data['expires_at']) not in (float,int) or not now()<=data['expires_at']<=now()+301):
            raise ValueError('Invalid preview')
        after=Limits.model_validate(data['after']).model_dump()
        if type(data['version']) is not int or data['version']<0:raise ValueError('Invalid version')
    except Exception:
        raise HTTPException(409,'限额预览已失效或不属于当前登录，请重新预览') from None
    row=locked(db,user.workspace_id)
    digest=hashlib.sha256(body.token.encode()).hexdigest()
    if row.confirmed_hash==digest:return view(db,user.workspace_id)
    if row.version!=data['version']:raise HTTPException(409,'调用限额已变化，请重新预览')
    row.daily_limit,row.execution_limit=after['daily_limit'],after['execution_limit']
    row.version+=1;row.confirmed_hash=digest
    audit(db,user,'model_budget_update',user.workspace_id)
    db.flush()
    return view(db,user.workspace_id)


class BudgetExceeded(HTTPException):
    def __init__(self, dimension):
        message='UTC 当日' if dimension=='daily' else '本次执行'
        super().__init__(429,message+'模型请求次数已达到限额，未向上游发送；请在用量预算中核对')


def root(db, workspace, config, run_id):
    from .tasks import WorkTask
    from .delegation import AgentRun
    from .plugins import PluginInvocation
    supplied=[('agent',AgentRun,config.get('agent_run_id')),('task',WorkTask,config.get('task_id')),
        ('plugin',PluginInvocation,config.get('invocation_id')),('chat',ChatRun,run_id or config.get('run_id'))]
    for kind,entity,identity in supplied:
        if not identity:continue
        if not isinstance(identity,str):raise HTTPException(409,'模型请求的执行归属无效，未发送')
        record=db.get(entity,identity,populate_existing=True)
        if not record or record.workspace_id!=workspace:
            raise HTTPException(409,'模型请求的执行归属已失效，未发送')
        return kind,record.id
    return 'background',''


def reserve(provider,config,run_id=None):
    with SessionLocal.begin() as db:
        current=db.get(Provider,provider.id)
        if (not current or current.workspace_id!=provider.workspace_id or
            not current.enabled or current.version!=provider.version):
            raise HTTPException(409,'模型连接归属已失效，未发送')
        row=locked(db,current.workspace_id)
        timestamp=now()
        kind,identity=root(db,current.workspace_id,config,run_id)
        if row.daily_limit and today(db,current.workspace_id,timestamp)>=row.daily_limit:raise BudgetExceeded('daily')
        if row.execution_limit and kind!='background':
            used=db.scalar(select(func.count()).select_from(ModelDispatch).where(
                ModelDispatch.workspace_id==current.workspace_id,ModelDispatch.root_kind==kind,ModelDispatch.root_id==identity))
            if used>=row.execution_limit:raise BudgetExceeded('execution')
        attempt=ModelDispatch(workspace_id=current.workspace_id,provider_id=provider.id,created_at=timestamp,
            root_kind=kind,root_id=identity,policy_version=row.version)
        db.add(attempt);db.flush()
        return attempt.id


@contextmanager
def client(provider,factory,config=None,run_id=None):
    """Attach before the real HTTP dispatch, including HTTPX WebSocket upgrades.

    Keep the existing client factory injectable for transport/compatibility tests.
    GET catalog and non-model traffic never acquire a request slot.
    """
    metadata=config if config is not None else {}
    ids=[]
    def admit(request):
        if request.method=='POST' or request.headers.get('upgrade','').lower()=='websocket':
            identity=reserve(provider,metadata,run_id)
            ids.append(identity)
            metadata.setdefault('_dispatch_ids',[]).append(identity)
    status='interrupted'
    try:
        with factory(provider) as upstream:
            upstream.event_hooks.setdefault('request',[]).append(admit)
            yield upstream
        status='returned'
    finally:
        # Losing this final annotation never loses the pre-dispatch count.
        if ids:
            try:
                with SessionLocal.begin() as db:
                    for identity in ids:
                        record=db.get(ModelDispatch,identity)
                        if record:record.status=status
            except Exception:
                pass


def link(db,call,ids):
    for identity in ids:
        row=db.get(ModelDispatch,identity)
        if row and row.workspace_id==call.workspace_id and row.call_id is None:row.call_id=call.id


def checkpoint(db):
    from .web import view as serialize
    return {'policies':[serialize(row) for row in db.scalars(select(ModelBudget))],
        'attempts':[serialize(row) for row in db.scalars(select(ModelDispatch))]}


def validate_checkpoint(data,owner):
    if not isinstance(data,dict) or set(data)!={'policies','attempts'}:raise ValueError('Invalid budget checkpoint')
    for name,entity in [('policies',ModelBudget),('attempts',ModelDispatch)]:
        rows=data[name]
        if not isinstance(rows,list) or len(rows)>1000000:raise ValueError('Budget checkpoint too large')
        keys=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={c.name for c in entity.__table__.columns} or row['workspace_id']!=owner['workspace_id']:
                raise ValueError('Invalid budget workspace')
            identity=row['workspace_id'] if name=='policies' else row['id']
            if identity in keys:raise ValueError('Duplicate budget record')
            keys.add(identity)
            if name=='policies':
                Limits.model_validate({k:row[k] for k in Limits.model_fields})
                if type(row['version']) is not int or row['version']<0:raise ValueError('Invalid budget version')
                if (not isinstance(row['confirmed_hash'],str) or
                    (row['confirmed_hash'] and (len(row['confirmed_hash'])!=64 or any(c not in '0123456789abcdef' for c in row['confirmed_hash'])))):
                    raise ValueError('Invalid budget confirmation')
            else:
                if (row['root_kind'] not in ('background','chat','task','agent','plugin') or
                    row['status'] not in ('reserved','returned','interrupted') or
                    type(row['policy_version']) is not int or row['policy_version']<0 or
                    type(row['created_at']) not in (float,int) or not math.isfinite(row['created_at']) or not 0<=row['created_at']<10**12):
                    raise ValueError('Invalid budget attempt')
                for key in ('id','provider_id','root_id'):
                    if not isinstance(row[key],str) or len(row[key])>64:raise ValueError('Invalid budget reference')
                if (not row['id'] or not row['provider_id'] or
                    (row['root_kind']=='background') != (row['root_id']=='')):
                    raise ValueError('Invalid execution identity')
                if row['call_id'] is not None and (not isinstance(row['call_id'],str) or len(row['call_id'])>64):raise ValueError('Invalid budget call')


def restore(db,data):
    # Retain the union of latest attempts and backup attempts. Never refund a
    # dispatch because a database was restored, or revive an old preview token.
    for values_ in data['attempts']:
        existing=db.get(ModelDispatch,values_['id'])
        if existing:
            for key in ('workspace_id','provider_id','root_kind','root_id','created_at'):
                if getattr(existing,key)!=values_[key]:raise ValueError('Budget attempt identity changed')
        if not existing:
            db.add(ModelDispatch(**values_))
    for values_ in data['policies']:
        row=db.get(ModelBudget,values_['workspace_id'])
        merged=dict(values_)
        merged['version']=max(values_['version'],row.version if row else 0)+1
        merged['confirmed_hash']=''
        db.merge(ModelBudget(**merged))
