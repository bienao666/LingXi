"""Generation-bound index recovery; never replay a running retain request."""
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import now
from .memory import enqueue, index_epoch, usable
from .models import Job


class IndexRetry(BaseModel):
    version: int = Field(ge=1)
    epoch: int = Field(ge=0)


class IndexState(BaseModel):
    version: int
    epoch: int
    status: Literal['ready', 'pending', 'running', 'failed', 'missing', 'unavailable']
    attempts: int
    retry_at: float | None
    can_retry: bool


def job_epoch(job, current):
    if 'epoch' in job.payload:
        return job.payload['epoch']
    # Upgrade old reindex jobs without reinterpreting an old bank as current.
    if job.key.startswith('reindex:'):
        try:
            return int(job.key.split(':')[1])
        except (ValueError, IndexError):
            return -1
    # Legacy creation/restore jobs did not record a bank. Pin it on first claim.
    return current


def jobs(db, memory, epoch):
    query = select(Job).where(Job.workspace_id == memory.workspace_id, Job.kind == 'index',
        Job.payload['memory_id'].as_string() == memory.id,
        Job.payload['version'].as_integer() == memory.version).order_by(Job.created_at.desc(), Job.id.desc())
    return (job for job in db.scalars(query).yield_per(64) if job_epoch(job, epoch) == epoch)


def state(db, memory):
    epoch = index_epoch(db)
    result = dict(version=memory.version, epoch=epoch, attempts=0, retry_at=None, can_retry=False)
    if not usable(memory):
        return IndexState(**result, status='unavailable')
    if memory.index_status == 'ready' and memory.index_epoch == epoch:
        return IndexState(**result, status='ready')
    failed = None
    for job in jobs(db, memory, epoch):
        if job.status in ('pending', 'running'):
            return IndexState(**(result | {'attempts': job.attempts,
                'retry_at': job.available_at if job.status == 'pending' and job.available_at > now() else None}), status=job.status)
        if failed is None and job.status == 'failed':
            failed = job
    return IndexState(**(result | {'attempts': failed.attempts if failed else 0, 'can_retry': True}),
        status='failed' if failed else 'missing')


def retry(db, memory, body):
    # Caller holds the authoritative memory row. Concurrent clicks serialize here;
    # worker failure locks Job -> Memory, so never acquire a Job row lock here.
    if body.version != memory.version or body.epoch != index_epoch(db) or not usable(memory):
        raise HTTPException(409, '记忆或索引配置已变化，请刷新状态后重试')
    current = state(db, memory)
    if not current.can_retry:
        return current
    failed = next((job for job in jobs(db, memory, body.epoch) if job.status == 'failed'), None)
    if failed is None:
        key = f'index-recovery:{body.epoch}:{memory.id}:{memory.version}'
        enqueue(db, key, 'index', {'memory_id': memory.id, 'version': memory.version})
        db.flush()
        failed = db.scalar(select(Job).where(Job.key == key, Job.workspace_id == memory.workspace_id))
        if failed is None:
            raise HTTPException(409, '索引恢复记录不一致，请联系管理员检查')
    failed.payload = failed.payload | {'epoch': body.epoch}
    failed.status, failed.attempts, failed.available_at = 'pending', 0, now()
    failed.error, failed.lease_token, failed.lease_until = None, None, 0
    memory.index_status = 'pending'
    db.flush()
    return state(db, memory)
