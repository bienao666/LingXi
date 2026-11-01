"""Authenticated memory management pages; cursors fix ordering, never validity."""
import hashlib
import math
from datetime import date, datetime, time, timezone
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
    scanned: int = 0
    scan_limited: bool = False


class MemoryCandidatePage(BaseModel):
    items: list[CandidateView]
    next_cursor: str | None
    as_of: float


class MemoryOperationPage(BaseModel):
    items: list[OperationView]
    next_cursor: str | None
    as_of: float


def paginate(db, user, model, query, scope, cursor, limit, predicate=None):
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
    if predicate is not None:
        # Legacy temporal evidence is free text. Parse it conservatively in bounded
        # batches, rather than lexical SQL comparisons or loading the entire bank.
        items, scanned, last, exhausted = [], 0, None, False
        while len(items) < limit and scanned < 1024:
            count = min(64, 1024-scanned)
            batch_query = query.where(older(model, last.created_at, last.id)) if last else query
            rows = list(db.scalars(batch_query.order_by(model.created_at.desc(), model.id.desc()).limit(count)))
            for row in rows:
                last = row
                scanned += 1
                if predicate(row):
                    items.append(row)
                if len(items) == limit:
                    break
            if len(rows) < count and (not rows or last is rows[-1]):
                exhausted = True
                break
        more = bool(not exhausted and last and db.scalar(query.with_only_columns(model.id)
            .where(older(model, last.created_at, last.id)).limit(1)))
        token = seal(user, scope, {'time': last.created_at, 'id': last.id, 'as_of': as_of}) if more else None
        return {'items': [view(row) for row in items], 'next_cursor': token, 'as_of': as_of,
            'scanned': scanned, 'scan_limited': more and scanned >= 1024 and len(items) < limit}
    rows = list(db.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit+1)))
    items = rows[:limit]
    token = seal(user, scope, {'time': items[-1].created_at, 'id': items[-1].id, 'as_of': as_of}) if len(rows) > limit else None
    return {'items': [view(row) for row in items], 'next_cursor': token, 'as_of': as_of}


def time_bounds(basis, since, until):
    if basis == 'all' and (since or until):
        raise HTTPException(422, '请先选择时间依据，再填写日期')
    try:
        start = datetime.combine(date.fromisoformat(since), time.min, timezone.utc) if since else None
        end = datetime.combine(date.fromisoformat(until), time.max, timezone.utc) if until else None
        if start and end and start > end:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, '日期必须有效，且开始日期不能晚于结束日期') from None
    return start, end


def temporal_match(memory, basis, start, end):
    from .memory_time import instant
    try:
        first = instant(memory.event_at if basis == 'event' else memory.valid_from)
        last = first if basis == 'event' else instant(memory.valid_to)
    except (ValueError, OverflowError):
        return False
    # An unspecified endpoint is unknown, not an infinite/permanent period.
    return bool(first is not None and last is not None and first <= last
        and (start is None or last >= start) and (end is None or first <= end))


@router.get('/api/memory-pages', response_model=MemoryRecordPage)
def memories(ctx: Context, q: str = Query('', max_length=200), category: Category | Literal[''] = '',
        subject: str = Query('', max_length=150),
        time_basis: Literal['all', 'created', 'event', 'period'] = 'all',
        since: str = Query('', max_length=10, pattern=r'^(|\d{4}-\d{2}-\d{2})$'),
        until: str = Query('', max_length=10, pattern=r'^(|\d{4}-\d{2}-\d{2})$'),
        cursor: str | None = Query(None, max_length=4096), limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    start, end = time_bounds(time_basis, since, until)
    query = select(Memory).where(Memory.status != 'deleted')
    if q:
        query = query.where(Memory.content.icontains(q, autoescape=True))
    if category:
        query = query.where(Memory.category == category)
    if subject:
        query = query.where(Memory.subject == subject)
    if time_basis == 'created':
        if start:
            query = query.where(Memory.created_at >= start.timestamp())
        if end:
            query = query.where(Memory.created_at <= end.timestamp())
    scope = ['memory-records', hashlib.sha256(q.encode()).hexdigest(), category]
    if subject or time_basis != 'all':
        scope += [subject, time_basis, since, until]
    predicate = (lambda row: temporal_match(row, time_basis, start, end)) if time_basis in ('event', 'period') else None
    return paginate(db, user, Memory, query, scope, cursor, limit, predicate)


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
