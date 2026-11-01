"""Authenticated memory management pages; cursors fix ordering, never validity."""
import hashlib
import math
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from .conversation_pages import older, seal, unseal
from .db import now
from .models import Candidate, Memory, Operation
from .schemas import CandidateView, Category, MemoryView, OperationView
from .web import Context, view

router = APIRouter()


class MemoryRecordPage(BaseModel):
    items: list[MemoryView]
    next_cursor: str | None
    as_of: float


class MemoryCandidatePage(BaseModel):
    items: list[CandidateView]
    next_cursor: str | None
    as_of: float


class MemoryOperationPage(BaseModel):
    items: list[OperationView]
    next_cursor: str | None
    as_of: float


def paginate(db, user, model, query, scope, cursor, limit):
    position = unseal(cursor, user, scope)
    as_of = now()
    if cursor and position is None:
        raise HTTPException(400, '分页凭据无效，请返回最新记录')
    if position is not None:
        try:
            if (not isinstance(position, dict) or set(position) != {'time', 'id', 'as_of'}
                    or type(position['time']) not in (int, float) or type(position['as_of']) not in (int, float)
                    or not math.isfinite(position['time']) or not math.isfinite(position['as_of'])
                    or not 0 <= position['time'] <= position['as_of'] <= as_of + 60
                    or not isinstance(position['id'], str) or not 1 <= len(position['id']) <= 64):
                raise ValueError()
            as_of = position['as_of']
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, '分页凭据无效，请返回最新记录') from None
        query = query.where(older(model, position['time'], position['id']))
    query = query.where(model.user_id == user.id, model.workspace_id == user.workspace_id,
        model.created_at <= as_of)
    rows = list(db.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit+1)))
    items = rows[:limit]
    token = seal(user, scope, {'time': items[-1].created_at, 'id': items[-1].id, 'as_of': as_of}) if len(rows) > limit else None
    return {'items': [view(row) for row in items], 'next_cursor': token, 'as_of': as_of}


@router.get('/api/memory-pages', response_model=MemoryRecordPage)
def memories(ctx: Context, q: str = Query('', max_length=200), category: Category | Literal[''] = '',
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    query = select(Memory).where(Memory.status != 'deleted')
    if q:
        query = query.where(Memory.content.icontains(q, autoescape=True))
    if category:
        query = query.where(Memory.category == category)
    scope = ['memory-records', hashlib.sha256(q.encode()).hexdigest(), category]
    return paginate(db, user, Memory, query, scope, cursor, limit)


@router.get('/api/memory-candidate-pages', response_model=MemoryCandidatePage)
def candidates(ctx: Context, status: Literal['all', 'pending', 'confirmed', 'rejected'] = 'all',
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    from .agent_memory import expire
    expire(db, user)
    query = select(Candidate).where(Candidate.status != 'invalidated')
    if status != 'all':
        query = query.where(Candidate.status == status)
    return paginate(db, user, Candidate, query, ['memory-candidates', status], cursor, limit)


@router.get('/api/memory-operation-pages', response_model=MemoryOperationPage)
def operations(ctx: Context, kind: Literal['export', 'delete'],
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    query = select(Operation).where(Operation.kind == kind)
    return paginate(db, user, Operation, query, ['memory-operations', kind], cursor, limit)
