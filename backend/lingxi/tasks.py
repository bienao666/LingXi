"""Durable first-party skills. Definitions describe capabilities, never grants."""
import hashlib
import json
import os
from pathlib import PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .config import settings
from .db import Base, now
from .models import Entity, Job, Message, Conversation

TERMINAL = {'succeeded', 'failed', 'cancelled'}
SKILL_VERSION = '1.0.0'


class WorkTask(Entity, Base):
    __tablename__ = 'work_tasks'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    skill_id: Mapped[str] = mapped_column(String(100))
    skill_version: Mapped[str] = mapped_column(String(30), default=SKILL_VERSION)
    input: Mapped[dict] = mapped_column(JSON)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    next_seq: Mapped[int] = mapped_column(Integer, default=0)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class TaskAttempt(Entity, Base):
    __tablename__ = 'task_attempts'
    __table_args__ = (UniqueConstraint('task_id', 'number'),)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default='running')
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)


class TaskEvent(Base):
    __tablename__ = 'task_events'
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Reminder(Entity, Base):
    __tablename__ = 'reminders'
    __table_args__ = (UniqueConstraint('user_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    due_at: Mapped[float] = mapped_column(Float, index=True)
    timezone: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default='scheduled', index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReminderReceipt(Entity, Base):
    __tablename__ = 'reminder_receipts'
    __table_args__ = (UniqueConstraint('reminder_id', 'reminder_version'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    reminder_id: Mapped[str] = mapped_column(String(64), index=True)
    reminder_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='stored')
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class PlanInput(BaseModel):
    goal: str = Field(min_length=1, max_length=6000)


class FileListInput(BaseModel):
    path: str = Field(default='.', max_length=400)

    @field_validator('path')
    @classmethod
    def scoped_path(cls, value):
        if not value or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value) or ':' in value:
            raise ValueError('使用服务器资料目录中的相对路径')
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('路径不能离开服务器资料目录')
        return str(path)


class TaskInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    skill_id: Literal['project.plan', 'server.files', 'server.read']
    input: dict
    conversation_id: str | None = None


class VersionInput(BaseModel):
    version: int = Field(ge=1)


class ReminderValues(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default='', max_length=4000)
    due_at: float = Field(gt=0, allow_inf_nan=False)
    timezone: str = Field(default='Asia/Shanghai', max_length=100)
    conversation_id: str | None = None

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('请选择有效的 IANA 时区，例如 Asia/Shanghai') from None
        return value


class ReminderInput(ReminderValues):
    request_id: str = Field(min_length=8, max_length=100)


class ReminderUpdate(ReminderValues, VersionInput):
    pass


class TaskView(BaseModel):
    id: str
    title: str
    skill_id: str
    skill_version: str
    input: dict
    config: dict
    status: str
    cancel_requested: bool
    version: int
    conversation_id: str | None
    result_message_id: str | None
    result: dict
    error: str | None
    created_at: float
    updated_at: float


class AttemptView(BaseModel):
    number: int
    status: str
    created_at: float
    finished_at: float | None
    error: str | None
    receipt: dict


class TaskEventView(BaseModel):
    seq: int
    kind: str
    created_at: float
    data: dict


class TaskDetail(TaskView):
    attempts: list[AttemptView]
    events: list[TaskEventView]


class ReminderView(BaseModel):
    id: str
    title: str
    content: str
    due_at: float
    timezone: str
    conversation_id: str | None
    version: int
    status: str
    created_at: float
    updated_at: float
    error: str | None
    receipt: dict | None


class SkillView(BaseModel):
    id: str
    name: str
    version: str
    plugin_id: str
    available: bool
    reason: str | None
    side_effects: str
    input_schema: dict
    output_description: str


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def event(db, task, kind, **data):
    task.next_seq += 1
    task.updated_at = now()
    db.add(TaskEvent(task_id=task.id, seq=task.next_seq, kind=kind, data=data))


def state(db, task, status, **data):
    task.status, task.version = status, task.version + 1
    event(db, task, status, **data)


def scheduled_job(db, key, kind, payload, due_at=None, workspace_id='personal'):
    job = Job(key=key, kind=kind, payload=payload, workspace_id=workspace_id)
    if due_at is not None:
        job.available_at = due_at
    db.add(job)


def reminder_view(db, row):
    from .chat_actions import require_origin
    require_origin(db, row.id)
    receipt = db.scalar(select(ReminderReceipt).where(ReminderReceipt.reminder_id == row.id,
        ReminderReceipt.reminder_version == row.version))
    values = {key: getattr(row, key) for key in ReminderView.model_fields if key != 'receipt'}
    values['receipt'] = ({'status': receipt.status, 'stored_at': receipt.created_at,
        'message_id': receipt.message_id, 'acknowledged_at': receipt.acknowledged_at} if receipt else None)
    return values


def append_result(db, owner, conversation_id, content):
    """An atomic in-app record, not a claim of browser/QQ delivery or reading."""
    if not conversation_id:
        return None
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id,
        Conversation.user_id == owner.id, Conversation.workspace_id == owner.workspace_id).with_for_update())
    if not conversation or conversation.deleted:
        return None
    message = Message(workspace_id=owner.workspace_id, conversation_id=conversation.id,
        role='assistant', content=content, status='completed')
    db.add(message)
    db.flush()
    return message.id


def resource_available():
    return bool(settings().resource_dir and os.name == 'posix' and hasattr(os, 'O_NOFOLLOW')
                and os.open in os.supports_dir_fd and os.scandir in os.supports_fd
                and settings().resource_dir.is_dir())


def open_resource_directory(relative_path, check):
    """Linux openat traversal prevents symlink escape, including rename races."""
    if not resource_available():
        raise HTTPException(409, '服务器资料目录尚未配置或当前系统不支持安全目录访问')
    path = FileListInput(path=relative_path).path
    check()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(settings().resource_dir, flags)
    except OSError:
        raise HTTPException(409, '服务器资料目录不可访问，请检查服务端挂载与权限') from None
    try:
        for component in PurePosixPath(path).parts:
            check()
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        os.close(descriptor)
        raise HTTPException(409, '目录不存在、无权读取或包含符号链接，请使用授权目录内的普通目录') from None
    except BaseException:
        os.close(descriptor)
        raise


def list_resource(relative_path, check, *, grant):
    from .resources import check_path
    path = check_path(grant, relative_path)
    descriptor = open_resource_directory(path, check)
    try:
        entries = []
        truncated = False
        scanned = 0
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                check()
                scanned += 1
                if len(entries) >= 200 or scanned > 1000:
                    truncated = True
                    break
                if entry.is_symlink():
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                    directory = entry.is_dir(follow_symlinks=False)
                    if not directory and not entry.is_file(follow_symlinks=False):
                        continue
                    entries.append({'name': entry.name, 'kind': 'directory' if directory else 'file',
                                    'size': stat.st_size if not directory else None, 'modified_at': stat.st_mtime})
                except FileNotFoundError:
                    continue
        return {'path': path, 'entries': sorted(entries, key=lambda x: (x['kind'] != 'directory', x['name'])),
                'truncated': truncated, 'limit': 200, 'observed_at': now(),
                'verification': 'filesystem_metadata', 'note': '仅列出当前目录，不读取文件正文或执行程序；符号链接已跳过。'}
    except OSError:
        raise HTTPException(409, '目录不存在、无权读取或包含符号链接，请使用授权目录内的普通目录') from None
    finally:
        os.close(descriptor)


class PlanStep(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=1000)
    completion: str = Field(min_length=1, max_length=500)


class ProjectPlan(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    steps: list[PlanStep] = Field(min_length=1, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=8)


def skill_catalog(db, user):
    from .gateway import resolve_model
    from .plugins import require_capability
    plugin_reason = None
    try:
        require_capability(db, user, 'project.plan')
    except HTTPException as exc:
        plugin_reason = str(exc.detail)
    try:
        model, provider, _ = resolve_model(db, 'chat')
        if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
            raise HTTPException(409, '对话模型不属于当前工作区')
        model_reason = None
    except HTTPException as exc:
        model_reason = exc.detail
    from .resources import current as resource_policy
    resources = resource_policy(db, user)
    result = [SkillView(id='project.plan', name='项目规划', version=SKILL_VERSION, plugin_id='lingxi.builtin',
        available=model_reason is None, reason=model_reason, side_effects='模型调用费用；仅生成计划，不执行计划中的操作',
        input_schema=PlanInput.model_json_schema(), output_description='目标摘要、行动步骤、完成条件和假设；校验结构，不声称已完成计划'),
        SkillView(id='server.files', name='资料清单', version=SKILL_VERSION, plugin_id='lingxi.builtin',
        available=resources.available, reason=resources.reason,
        side_effects='只读服务器授权资料目录，不读取用户电脑', input_schema=FileListInput.model_json_schema(),
        output_description='当前目录最多 200 项元数据，明确提示截断并跳过符号链接'),
        SkillView(id='reminder.once', name='待办提醒', version=SKILL_VERSION, plugin_id='lingxi.builtin',
        available=True, reason=None, side_effects='到时写入站内提醒，可选写回已授权 Web 会话；不发 QQ 或浏览器推送',
        input_schema=ReminderInput.model_json_schema(), output_description='持久化到期记录及实际入站时间；存入站内不等于已读')]
    if plugin_reason:
        for item in result:
            item.available, item.reason = False, plugin_reason
    from .server_documents import FileReadInput, PLUGIN
    reason = None
    try:
        require_capability(db, user, 'server.read', plugin_id=PLUGIN)
        if not resources.available or not resources.read_roots:
            raise HTTPException(403, resources.reason or '请在资源授权中单独允许正文目录')
    except HTTPException as exc:
        reason = str(exc.detail)
    result.append(SkillView(id='server.read', name='文档读取', version=SKILL_VERSION, plugin_id=PLUGIN,
        available=reason is None, reason=reason, side_effects='只读服务器正文授权目录；保存文件快照，不修改原件或自动发给模型',
        input_schema=FileReadInput.model_json_schema(), output_description='生成受控附件快照，异步解析后可预览、选择用于聊天；快照不自动同步原文件'))
    return result
