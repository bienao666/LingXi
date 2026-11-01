"""Owner-scoped task and reminder API with transactional enqueue and versions."""
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from .db import now
from .models import Conversation, User
from .web import Context, owned, view, audit
from .tasks import (WorkTask, TaskAttempt, TaskEvent, Reminder, ReminderReceipt,
    TaskInput, TaskView, TaskDetail, VersionInput, ReminderInput, ReminderUpdate, ReminderView,
    PlanInput, FileListInput, SkillView, digest, event, state, scheduled_job, reminder_view,
    skill_catalog, TERMINAL)

router = APIRouter()


def task_view(db, row):
    from .chat_actions import require_origin
    if row.skill_id in ('server.files', 'server.read'):
        from .resources import expire
        expire(db, db.get(User, row.user_id))
    require_origin(db, row.id)
    return view(row)


def origin_visible(db, row):
    from .chat_actions import for_target, valid_origin
    action = for_target(db, row.id)
    return not action or valid_origin(db, action)


class TaskPage(BaseModel):
    items: list[TaskView]
    next_cursor: str | None


class ReminderPage(BaseModel):
    items: list[ReminderView]
    next_cursor: str | None


def serial_owner(db, user):
    # Serializes first-time idempotency keys without relying on a racing insert.
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def matching(existing, request_hash):
    if existing.request_hash != request_hash:
        raise HTTPException(409, '请求编号已用于其他内容，请重新提交')
    return existing


def commit_insert(db):
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '请求已并发提交，请按原请求编号查询或重试') from None


def bounded_due(due_at):
    if due_at < now() + 1 or due_at > now() + 366 * 86400:
        raise HTTPException(422, '提醒时间应在未来 1 秒至 366 天内，请检查时区和时间')


def check_conversation(db, conversation_id, user):
    if conversation_id:
        owned(db, Conversation, conversation_id, user)


def page_query(db, entity, user, cursor, query, limit):
    query = query.where(entity.user_id == user.id, entity.workspace_id == user.workspace_id)
    if cursor:
        anchor = owned(db, entity, cursor, user)
        query = query.where(or_(entity.created_at < anchor.created_at,
            and_(entity.created_at == anchor.created_at, entity.id < anchor.id)))
    rows = db.scalars(query.order_by(entity.created_at.desc(), entity.id.desc()).limit(limit+1)).all()
    return rows[:limit], rows[limit-1].id if len(rows) > limit else None


@router.get('/api/skills', response_model=list[SkillView])
def skills(ctx: Context):
    db, user, _ = ctx
    return skill_catalog(db, user)


@router.post('/api/tasks', response_model=TaskView)
def create_task(body: TaskInput, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    request_hash = digest(body.model_dump())
    existing = db.scalar(select(WorkTask).where(WorkTask.user_id == user.id,
        WorkTask.workspace_id == user.workspace_id, WorkTask.request_id == body.request_id))
    if existing:
        return task_view(db, matching(existing, request_hash))
    from .plugins import require_capability
    plugin_id = 'lingxi.documents' if body.skill_id == 'server.read' else 'lingxi.builtin'
    _, plugin_release, _ = require_capability(db, user, body.skill_id, plugin_id=plugin_id)
    check_conversation(db, body.conversation_id, user)
    try:
        from .server_documents import FileReadInput
        values = (PlanInput if body.skill_id == 'project.plan' else FileReadInput if body.skill_id == 'server.read' else FileListInput)(**body.input).model_dump()
    except ValidationError:
        raise HTTPException(422, '任务输入无效，请填写目标或服务器资料目录中的相对路径') from None
    config = {'plugin_id': plugin_id, 'plugin_version': plugin_release.version,
              'plugin_release_id': plugin_release.id}
    if body.skill_id == 'project.plan':
        from .gateway import resolve_model
        model, provider, policy_version = resolve_model(db, 'chat')
        if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
            raise HTTPException(409, '对话模型不属于当前工作区')
        config |= {'model_id': model.id, 'model': model.name, 'provider_version': provider.version,
                   'policy_version': policy_version, 'prompt_version': 'project-plan-v1'}
    else:
        from .resources import snapshot
        config['resource_policy'] = snapshot(db, user, values['path'], read=body.skill_id == 'server.read')
        if body.skill_id == 'server.read':
            from .db import uid
            config['media_id'] = uid()
    task = WorkTask(user_id=user.id, workspace_id=user.workspace_id, title=body.title,
        request_id=body.request_id, request_hash=request_hash, skill_id=body.skill_id,
        input=values, config=config, conversation_id=body.conversation_id)
    db.add(task)
    db.flush()
    event(db, task, 'queued', skill_id=task.skill_id, skill_version=task.skill_version)
    scheduled_job(db, f'task:{task.id}:1', 'task', {'task_id': task.id}, workspace_id=user.workspace_id)
    audit(db, user, 'task_create', task.id)
    commit_insert(db)
    return task_view(db, task)


@router.get('/api/tasks', response_model=TaskPage)
def tasks(ctx: Context, q: str = Query('', max_length=200),
          status: Literal['queued','running','succeeded','failed','cancelled'] | None = None,
          cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    query = select(WorkTask)
    if q:
        query = query.where(WorkTask.title.icontains(q, autoescape=True))
    if status:
        query = query.where(WorkTask.status == status)
    rows, next_cursor = page_query(db, WorkTask, user, cursor, query, limit)
    return {'items': [task_view(db, row) for row in rows if origin_visible(db, row)], 'next_cursor': next_cursor}


@router.get('/api/tasks/{identity}', response_model=TaskDetail)
def task_detail(identity: str, ctx: Context):
    db, user, _ = ctx
    task = owned(db, WorkTask, identity, user)
    return task_view(db, task) | {'attempts': [view(row, ('lease_token',)) for row in db.scalars(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id).order_by(TaskAttempt.number))],
        'events': [view(row) for row in db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)
                                               .order_by(TaskEvent.seq))]}


@router.post('/api/tasks/{identity}/cancel', response_model=TaskView)
def cancel_task(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    task = owned(db, WorkTask, identity, user, lock=True)
    task_view(db, task)
    if task.version != body.version:
        raise HTTPException(409, '任务状态已变化，请重新加载后操作')
    if task.status not in TERMINAL and not task.cancel_requested:
        task.cancel_requested = True
        if task.status == 'queued':
            state(db, task, 'cancelled', reason='用户在开始前取消')
        else:
            task.version += 1
            event(db, task, 'cancel_requested', reason='已请求停止，等待执行器确认')
        audit(db, user, 'task_cancel', task.id)
    return view(task)


@router.post('/api/tasks/{identity}/retry', response_model=TaskView)
def retry_task(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    task = owned(db, WorkTask, identity, user, lock=True)
    task_view(db, task)
    if task.version != body.version:
        raise HTTPException(409, '任务状态已变化，请重新加载后操作')
    if task.status != 'failed':
        raise HTTPException(409, '仅明确失败且无外部写入副作用的任务可以重试')
    from .plugins import require_capability
    require_capability(db, user, task.skill_id, plugin_id=task.config.get('plugin_id', 'lingxi.builtin'), release_id=task.config.get('plugin_release_id'))
    if task.skill_id in ('server.files', 'server.read'):
        from .resources import require_snapshot
        require_snapshot(db, user, task.config, task.input['path'])
    task.cancel_requested, task.error = False, None
    state(db, task, 'queued', reason='用户明确重试；保留原任务与配置版本')
    scheduled_job(db, f'task:{task.id}:{task.version}', 'task', {'task_id': task.id}, workspace_id=user.workspace_id)
    audit(db, user, 'task_retry', task.id)
    return view(task)


@router.post('/api/reminders', response_model=ReminderView)
def create_reminder(body: ReminderInput, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    request_hash = digest(body.model_dump())
    existing = db.scalar(select(Reminder).where(Reminder.user_id == user.id,
        Reminder.workspace_id == user.workspace_id, Reminder.request_id == body.request_id))
    if existing:
        return reminder_view(db, matching(existing, request_hash))
    from .plugins import require_capability
    require_capability(db, user, 'reminder.once')
    bounded_due(body.due_at)
    check_conversation(db, body.conversation_id, user)
    row = Reminder(user_id=user.id, workspace_id=user.workspace_id,
        request_hash=request_hash, **body.model_dump())
    db.add(row)
    db.flush()
    scheduled_job(db, f'reminder:{row.id}:1', 'reminder', {'reminder_id': row.id, 'version': 1}, row.due_at, user.workspace_id)
    audit(db, user, 'reminder_create', row.id)
    commit_insert(db)
    return reminder_view(db, row)


@router.get('/api/reminders', response_model=ReminderPage)
def reminders(ctx: Context, status: Literal['scheduled','fired','acknowledged','cancelled','failed'] | None = None,
              q: str = Query('', max_length=200), cursor: str | None = None, limit: int = Query(50, ge=1, le=100)):
    db, user, _ = ctx
    query = select(Reminder)
    if q:
        query = query.where(Reminder.title.icontains(q, autoescape=True))
    if status:
        query = query.where(Reminder.status == status)
    rows, next_cursor = page_query(db, Reminder, user, cursor, query, limit)
    return {'items': [reminder_view(db, row) for row in rows if origin_visible(db, row)], 'next_cursor': next_cursor}


@router.put('/api/reminders/{identity}', response_model=ReminderView)
def edit_reminder(identity: str, body: ReminderUpdate, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    from .plugins import require_capability
    require_capability(db, user, 'reminder.once')
    row = owned(db, Reminder, identity, user, lock=True)
    reminder_view(db, row)
    if row.version != body.version or row.status not in ('scheduled', 'failed'):
        raise HTTPException(409, '提醒已变化或已到期，请重新加载；已触发提醒不能改写')
    bounded_due(body.due_at)
    check_conversation(db, body.conversation_id, user)
    from .reminder_delivery import invalidate
    invalidate(db, reminder_id=row.id, reason='提醒内容或时间已修改，请重新确认 QQ 投递')
    for key, value in body.model_dump(exclude={'version'}).items():
        setattr(row, key, value)
    row.version, row.updated_at, row.status, row.error = row.version + 1, now(), 'scheduled', None
    scheduled_job(db, f'reminder:{row.id}:{row.version}', 'reminder',
        {'reminder_id': row.id, 'version': row.version}, row.due_at, user.workspace_id)
    audit(db, user, 'reminder_update', row.id)
    return reminder_view(db, row)


@router.post('/api/reminders/{identity}/cancel', response_model=ReminderView)
def cancel_reminder(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    serial_owner(db, user)
    row = owned(db, Reminder, identity, user, lock=True)
    reminder_view(db, row)
    if row.version != body.version:
        raise HTTPException(409, '提醒已变化，请重新加载后操作')
    if row.status != 'scheduled':
        raise HTTPException(409, '提醒已触发或已取消，不能撤回已发生的记录')
    row.status, row.version, row.updated_at = 'cancelled', row.version + 1, now()
    from .reminder_delivery import invalidate
    invalidate(db, reminder_id=row.id, reason='用户取消提醒')
    audit(db, user, 'reminder_cancel', row.id)
    return reminder_view(db, row)


@router.post('/api/reminders/{identity}/acknowledge', response_model=ReminderView)
def acknowledge_reminder(identity: str, body: VersionInput, ctx: Context):
    db, user, _ = ctx
    row = owned(db, Reminder, identity, user, lock=True)
    reminder_view(db, row)
    if row.version != body.version or row.status not in ('fired', 'acknowledged'):
        raise HTTPException(409, '提醒尚未触发或版本已变化，请重新加载')
    receipt = db.scalar(select(ReminderReceipt).where(ReminderReceipt.reminder_id == row.id,
        ReminderReceipt.reminder_version == row.version))
    if not receipt:
        raise HTTPException(409, '提醒回执不存在，请检查服务状态')
    if not receipt.acknowledged_at:
        receipt.status, receipt.acknowledged_at = 'acknowledged', now()
        row.status, row.updated_at = 'acknowledged', now()
        audit(db, user, 'reminder_acknowledge', row.id)
    return reminder_view(db, row)
