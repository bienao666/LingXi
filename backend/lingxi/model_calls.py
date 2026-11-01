"""Workspace-scoped model call history, with bounded metadata and keyset paging."""
import math
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from .conversation_pages import seal, unseal
from .db import now
from .models import ModelCall
from .operations import SAFE_CONFIG
from .web import Context

router = APIRouter()

USAGE_KEYS = {'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens',
    'total_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'audio_tokens',
    'cached_tokens', 'reasoning_tokens', 'accepted_prediction_tokens', 'rejected_prediction_tokens',
    'ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'}
USAGE_DETAILS = {'prompt_tokens_details', 'completion_tokens_details', 'input_tokens_details',
    'output_tokens_details', 'cache_creation'}
PRICE_KEYS = {'version', 'provider_version', 'model', 'currency', 'unit', 'source',
    'rate', 'kind',
    'input_per_million', 'output_per_million', 'cached_input_per_million', 'cache_write_per_million'}


class ModelCallEntry(BaseModel):
    id: str
    created_at: float
    model_id: str
    run_id: str | None
    purpose: str
    status: str
    duration_ms: int
    usage: dict[str, Any]
    billing: dict[str, Any]
    config: dict[str, Any]
    error: str | None
    cost: None = None  # Legacy field: an unknown price is never zero.


class ModelCallPage(BaseModel):
    items: list[ModelCallEntry]
    next_cursor: str | None
    as_of: float


def numeric_usage(value, nested=False):
    if not isinstance(value, dict):
        return {}
    result = {k: v for k, v in value.items() if k in USAGE_KEYS and type(v) is int and 0 <= v <= 10**12}
    if not nested:
        for key in USAGE_DETAILS:
            if key in value and isinstance(value[key], dict):
                result[key] = numeric_usage(value[key], True)
    return result


def entry(row):
    source = row.config or {}
    config = {k: v for k, v in source.items() if k in SAFE_CONFIG | {'model', 'provider_protocol'}
        and type(v) in (str, int, float, type(None))}
    if type(source.get('validation')) is bool:
        config['validation'] = source['validation']
    if 'audio_measurement' in source:
        from .audio_costs import public_measurement
        config['audio_measurement'] = public_measurement(source['audio_measurement'])
    routing = source.get('routing')
    if isinstance(routing, dict) and type(routing.get('version')) is int:
        config['routing'] = {'version': routing['version'],
            'attempt_count': len(routing['attempts']) if isinstance(routing.get('attempts'), list) else 1}
        selection = routing.get('selection')
        if isinstance(selection, dict):
            # Whitelist ranking labels, never serialize arbitrary plan/history.
            for key in ('strategy', 'applied'):
                if selection.get(key) in ('priority', 'estimated_cost', 'observed_latency'):
                    config['routing']['selection_' + key] = selection[key]
    stored = row.billing or {}
    billing = {k: v for k, v in stored.items() if k in {'status', 'reason', 'amount', 'currency'}
        and type(v) in (str, type(None))}
    if not billing:
        billing = {'status': 'unknown', 'reason': 'historical', 'amount': None}
    price = stored.get('price')
    if isinstance(price, dict):
        billing['price'] = {k: v for k, v in price.items() if k in PRICE_KEYS and type(v) in (str, int, type(None))}
    return {'id': row.id, 'created_at': row.created_at, 'model_id': row.model_id,
        'run_id': row.run_id, 'purpose': row.purpose, 'status': row.status, 'duration_ms': row.duration_ms,
        'usage': numeric_usage(row.usage), 'billing': billing, 'config': config,
        'error': row.error, 'cost': None}


@router.get('/api/admin/model-call-pages', response_model=ModelCallPage)
def pages(ctx: Context, cursor: str | None = Query(None, max_length=4096),
        limit: int = Query(50, ge=1, le=200), days: Literal['0', '1', '7', '30'] = '0',
        status: Literal['all', 'completed', 'failed', 'cancelled'] = 'all',
        model_id: str = Query('', max_length=64), purpose: str = Query('', max_length=30)):
    db, user, _ = ctx
    scope = ['model-calls', days, status, model_id, purpose]
    position = unseal(cursor, user, scope)
    as_of = now()
    if position is not None:
        try:
            if (set(position) != {'time', 'id', 'as_of'} or type(position['time']) not in (int, float)
                    or type(position['as_of']) not in (int, float)
                    or not math.isfinite(position['time']) or not math.isfinite(position['as_of'])
                    or not 0 <= position['time'] <= position['as_of'] <= as_of + 60
                    or not isinstance(position['id'], str) or not 1 <= len(position['id']) <= 64):
                raise ValueError()
            as_of = position['as_of']
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, '分页凭据无效，请返回最新记录') from None
    query = select(ModelCall).where(ModelCall.workspace_id == user.workspace_id, ModelCall.created_at <= as_of)
    if days != '0':
        query = query.where(ModelCall.created_at >= as_of - int(days) * 86400)
    if status != 'all':
        query = query.where(ModelCall.status == status)
    if model_id:
        query = query.where(ModelCall.model_id == model_id)
    if purpose:
        query = query.where(ModelCall.purpose == purpose)
    if position is not None:
        query = query.where(or_(ModelCall.created_at < position['time'],
            and_(ModelCall.created_at == position['time'], ModelCall.id < position['id'])))
    rows = list(db.scalars(query.order_by(ModelCall.created_at.desc(), ModelCall.id.desc()).limit(limit + 1)))
    items = rows[:limit]
    token = seal(user, scope, {'time': items[-1].created_at, 'id': items[-1].id, 'as_of': as_of}) if len(rows) > limit else None
    return {'items': [entry(row) for row in items], 'next_cursor': token, 'as_of': as_of}
