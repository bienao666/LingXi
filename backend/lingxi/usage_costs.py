"""User supplied token rate cards and immutable, explicitly estimated call costs."""
from decimal import Decimal, localcontext
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import Model, Provider, User
from .web import Context, audit

router = APIRouter()
RATE = r'^(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?$'


class ModelPrice(Base):
    __tablename__ = 'model_prices'
    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)


class PriceInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    provider_version: int = Field(ge=1)
    enabled: bool = True
    currency: Literal['CNY', 'USD'] = 'CNY'
    input_per_million: str = Field(pattern=RATE)
    output_per_million: str = Field(pattern=RATE)
    cached_input_per_million: str | None = Field(default=None, pattern=RATE)
    cache_write_per_million: str | None = Field(default=None, pattern=RATE)
    acknowledgement: Literal['estimate_only']


class PriceView(BaseModel):
    model_id: str
    model: str
    provider: str
    provider_version: int
    purpose: str
    version: int
    enabled: bool
    current: bool
    rates: dict


def view(db, model, provider, user):
    row = db.get(ModelPrice, model.id)
    if row and (row.workspace_id != user.workspace_id or row.user_id != user.id):
        raise HTTPException(403, '当前身份不能读取此价格配置')
    rates = row.config if row else {}
    current = bool(row and rates.get('provider_id') == model.provider_id
        and rates.get('provider_version') == provider.version and rates.get('model') == model.name)
    return dict(model_id=model.id, model=model.name, provider=provider.name, provider_version=provider.version, purpose=model.purpose,
        version=row.version if row else 0, enabled=bool(rates.get('enabled')), current=current, rates=rates)


@router.get('/api/admin/model-prices', response_model=list[PriceView])
def prices(ctx: Context):
    db, user, _ = ctx
    rows = db.execute(select(Model, Provider).join(Provider, Model.provider_id == Provider.id).where(
        Model.workspace_id == user.workspace_id, Provider.workspace_id == user.workspace_id)
        .order_by(Model.created_at, Model.id).limit(1001)).all()
    if len(rows) > 1000:
        raise HTTPException(413, '价格目录超过当前展示预算，请先缩小模型目录')
    return [view(db, m, p, user) for m, p in rows]


@router.put('/api/admin/model-prices/{identity}', response_model=PriceView)
def save_price(identity: str, body: PriceInput, ctx: Context):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    model = db.get(Model, identity, populate_existing=True)
    provider = db.get(Provider, model.provider_id, populate_existing=True) if model else None
    if not model or not provider or model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(404, '模型不存在')
    row = db.get(ModelPrice, identity, populate_existing=True)
    if row and (row.workspace_id != user.workspace_id or row.user_id != user.id):
        raise HTTPException(403, '当前身份不能修改此价格配置')
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '单价已变化，请重新加载后核对')
    if body.provider_version != provider.version:
        raise HTTPException(409, '服务连接已变化，请重新加载后核对')
    config = body.model_dump(exclude={'version', 'acknowledgement', 'provider_version'}) | {
        'provider_id': provider.id, 'provider_version': provider.version, 'model': model.name,
        'updated_at': now(), 'source': 'owner_supplied', 'unit': 'per_million_tokens'}
    if not row:
        row = ModelPrice(model_id=identity, workspace_id=user.workspace_id, user_id=user.id, version=1, config=config)
        db.add(row)
    else:
        row.version, row.config = row.version + 1, config
    audit(db, user, 'model_price_update', identity)
    db.flush()
    return view(db, model, provider, user)


def snapshot(db, model, provider):
    """Taken before dispatch. Never apply a later price to an older request."""
    row = db.get(ModelPrice, model.id)
    if (not row or row.workspace_id != model.workspace_id or provider.workspace_id != model.workspace_id
        or not row.config.get('enabled') or row.config.get('provider_id') != provider.id
        or row.config.get('provider_version') != provider.version or row.config.get('model') != model.name):
        return {'status': 'unpriced'}
    return {'status': 'configured', 'version': row.version, **row.config}


def integer(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def cost(snapshot, usage):
    """Exact decimal estimate; unknown is never zero or inferred from totals."""
    unknown = {'status': 'unknown', 'reason': 'unpriced', 'amount': None, 'currency': None}
    if not snapshot or snapshot.get('status') != 'configured':
        return unknown
    unknown |= {'reason': 'incomplete_usage', 'currency': snapshot['currency'], 'price': snapshot}
    if not isinstance(usage, dict):
        return unknown
    inp = integer(usage.get('prompt_tokens', usage.get('input_tokens')))
    out = integer(usage.get('completion_tokens', usage.get('output_tokens')))
    if inp is None or out is None:
        return unknown
    total = integer(usage.get('total_tokens'))
    if 'total_tokens' in usage and (total is None or total != inp + out):
        return unknown | {'reason': 'inconsistent_usage'}
    details = usage.get('prompt_tokens_details', usage.get('input_tokens_details', {}))
    output_details = usage.get('completion_tokens_details', {})
    if not isinstance(details, dict) or not isinstance(output_details, dict):
        return unknown
    # Audio pricing and per-character/per-second billing need their own adapters.
    if details.get('audio_tokens', 0) != 0 or output_details.get('audio_tokens', 0) != 0:
        return unknown | {'reason': 'unsupported_unit'}
    cached = integer(usage.get('cache_read_input_tokens', details.get('cached_tokens', 0)))
    write = integer(usage.get('cache_creation_input_tokens', 0))
    if cached is None or write is None or cached + write > inp:
        return unknown | {'reason': 'inconsistent_usage'}
    if (cached and snapshot.get('cached_input_per_million') is None
        or write and snapshot.get('cache_write_per_million') is None):
        return unknown | {'reason': 'missing_cache_rate'}
    with localcontext() as context:
        context.prec = 40
        amount = (Decimal(inp - cached - write) * Decimal(snapshot['input_per_million'])
            + Decimal(out) * Decimal(snapshot['output_per_million'])
            + Decimal(cached) * Decimal(snapshot.get('cached_input_per_million') or '0')
            + Decimal(write) * Decimal(snapshot.get('cache_write_per_million') or '0')) / Decimal(1000000)
    return {'status': 'estimated', 'reason': 'owner_supplied_rates', 'currency': snapshot['currency'],
        'amount': format(amount, 'f'), 'price': snapshot}


def attribution(db, model_id, run_id, config, workspace):
    """Use only scoped durable references, no message text or arbitrary labels."""
    from .models import ChatRun
    from .tasks import WorkTask
    from .delegation import AgentRun
    from .plugins import PluginInvocation, PluginRelease
    result = {'channel': 'unknown', 'model': model_id, 'task': '', 'plugin': ''}
    def scoped(entity, identity):
        row = db.get(entity, identity) if isinstance(identity, str) and identity else None
        return row if row and row.workspace_id == workspace else None
    chat = scoped(ChatRun, run_id or config.get('run_id'))
    task = scoped(WorkTask, config.get('task_id'))
    agent = scoped(AgentRun, config.get('agent_run_id'))
    invocation = scoped(PluginInvocation, config.get('invocation_id'))
    if invocation:
        release = db.get(PluginRelease, invocation.release_id)
        if release:
            result['plugin'] = release.plugin_id
    if task:
        result['task'] = task.id
        release = db.get(PluginRelease, task.config.get('plugin_release_id', ''))
        if release and release.plugin_id == task.config.get('plugin_id'):
            result['plugin'] = release.plugin_id
    if agent:
        result['task'] = agent.id
    context = (agent or task or chat)
    if context:
        data = context.config
        result['channel'] = 'qq' if data.get('channel') else 'voice' if data.get('voice_session_id') else 'web'
    if config.get('validation'):
        result['channel'] = 'validation'
    if not context and invocation:
        result['channel'] = 'web'
    return result


def money_summary(calls):
    values = {}
    unknown = 0
    for call in calls:
        billing = call.billing or {}
        if billing.get('status') != 'estimated':
            unknown += 1
            continue
        key = billing['currency']
        value = values.setdefault(key, {'currency': key, 'amount': Decimal(0), 'calls': 0})
        with localcontext() as context:
            context.prec = 50
            value['amount'] += Decimal(billing['amount'])
        value['calls'] += 1
    return {'currencies': [dict(v, amount=format(v['amount'], 'f')) for _, v in sorted(values.items())],
        'unknown_cost_calls': unknown}


def breakdown(calls, dimension):
    groups = {}
    for call in calls:
        key = (call.billing or {}).get('attribution', {}).get(dimension, '')
        if dimension == 'model':
            key = call.model_id
        groups.setdefault(key or 'unknown', []).append(call)
    rows = [dict(key=k, calls=len(v), **money_summary(v)) for k, v in sorted(groups.items(), key=lambda p: (-len(p[1]), p[0]))]
    return {'items': rows[:1000], 'truncated': len(rows) > 1000}
