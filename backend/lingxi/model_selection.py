"""Bounded, local ranking evidence; never probes providers or broadens recipients."""
import re
from decimal import Decimal, localcontext
from statistics import median

from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import now
from .models import ChatRun, ModelCall
from .usage_costs import ModelPrice, RATE


class SelectionEvidence(BaseModel):
    model_id: str
    status: str
    reference_cost: str | None = None
    currency: str | None = None
    price_version: int | None = None
    samples: int = 0
    median_ms: str | None = None


class SelectionDecision(BaseModel):
    strategy: str
    applied: str
    reason: str
    selected_model_id: str | None = None
    evaluated_at: float
    evidence: list[SelectionEvidence] = Field(default_factory=list)


def price_evidence(db, user, model, provider, config):
    row = db.get(ModelPrice, model.id, populate_existing=True)
    rates = row.config if row else {}
    valid = (row and row.workspace_id == user.workspace_id and row.user_id == user.id
        and rates.get('enabled') is True and rates.get('provider_id') == provider.id
        and rates.get('provider_version') == provider.version and rates.get('model') == model.name
        and rates.get('unit') == 'per_million_tokens' and rates.get('source') == 'owner_supplied'
        and all(isinstance(rates.get(k), str) and re.fullmatch(RATE, rates[k])
                for k in ('input_per_million', 'output_per_million')))
    if not valid:
        return {'model_id': model.id, 'status': 'missing_price'}
    currency = rates.get('currency')
    if currency != config.get('reference_currency', 'CNY'):
        return {'model_id': model.id, 'status': 'currency_mismatch'}
    with localcontext() as ctx:
        ctx.prec = 50
        amount = (Decimal(rates['input_per_million']) * config.get('reference_input_tokens', 1000)
            + Decimal(rates['output_per_million']) * config.get('reference_output_tokens', 1000)) / 1000000
    return {'model_id': model.id, 'status': 'ready', 'reference_cost': format(amount, 'f'),
            'currency': currency, 'price_version': row.version}


def latency_evidence(db, user, model, provider, approved, timestamp):
    # Bound database reads as well as samples. Sparse/invalid records fail closed
    # to priority; do not read unbounded history to manufacture enough evidence.
    rows = db.execute(select(ModelCall, ChatRun).join(ChatRun, ChatRun.id == ModelCall.run_id).where(
        ModelCall.workspace_id == user.workspace_id, ChatRun.workspace_id == user.workspace_id,
        ChatRun.status == 'completed',
        ModelCall.model_id == model.id, ModelCall.purpose == 'chat', ModelCall.status == 'completed',
        ModelCall.created_at >= timestamp - 7 * 86400, ModelCall.created_at <= timestamp,
        ModelCall.duration_ms > 0, ModelCall.duration_ms <= 3600000)
        .order_by(ModelCall.created_at.desc(), ModelCall.id.desc()).limit(200)).all()
    samples = []
    for call, run in rows:
        config = call.config or {}
        run_config = run.config or {}
        owner = run_config.get('channel', {}).get('owner_id', run.user_id)
        if (owner != user.id or run_config.get('task_assist') or run_config.get('voice_session_id') or run_config.get('voice_input')
                or any(config.get(k) for k in ('validation', 'task_id', 'agent_run_id', 'invocation_id'))
                or config.get('model') != model.name or config.get('provider_id') != provider.id
                or config.get('provider_version') != provider.version
                or config.get('provider_protocol') != approved['protocol']):
            continue
        samples.append(Decimal(call.duration_ms))
        if len(samples) == 20:
            break
    return {'model_id': model.id, 'status': 'ready' if len(samples) >= 3 else 'insufficient_samples',
            'samples': len(samples), 'median_ms': format(median(samples), 'f') if samples else None}


def rank(db, user, config, allowed):
    """Input has already passed endpoint, capability, scope and availability checks.

    Return a stable order and a per-run snapshot. Dynamic evidence is deliberately
    absent from authorization digests and never reranked during a running request.
    """
    strategy = config.get('selection_strategy', 'priority')
    timestamp = now()
    applied, evidence = 'priority', []
    reason = '按已授权候选顺序选择。'
    if config.get('mode') != 'automatic':
        reason = '固定模式保持首选模型，备用按候选顺序。'
    elif strategy != 'priority' and allowed:
        for approved, (model, provider) in allowed:
            evidence.append(price_evidence(db, user, model, provider, config) if strategy == 'estimated_cost'
                else latency_evidence(db, user, model, provider, approved, timestamp))
        if all(e['status'] == 'ready' for e in evidence):
            key = 'reference_cost' if strategy == 'estimated_cost' else 'median_ms'
            values = {e['model_id']: Decimal(e[key]) for e in evidence}
            allowed = sorted(allowed, key=lambda item: values[item[0]['model_id']])
            applied = strategy
            reason = ('按同币种参考用量估算费用排序；不是本轮账单，不含缓存优惠。' if strategy == 'estimated_cost'
                else '按近七天成功普通聊天的总耗时中位数排序；不是首字延迟或质量评测。')
        else:
            reason = ('价格缺失、失效或币种不一致，保留候选顺序；不换算币种。' if strategy == 'estimated_cost'
                else '部分候选不足三个有效样本，保留候选顺序；未额外调用模型测速。')
    return allowed, dict(strategy=strategy, applied=applied, reason=reason,
        selected_model_id=allowed[0][0]['model_id'] if allowed else None,
        evaluated_at=timestamp, evidence=evidence)
