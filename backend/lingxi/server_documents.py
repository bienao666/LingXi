"""Explicit, read-only server document snapshots using the existing media lifecycle."""
import hashlib
import os
from pathlib import PurePosixPath
import stat

from fastapi import HTTPException
from pydantic import field_validator
from sqlalchemy import select

from .db import now, uid
from .tasks import FileListInput, open_resource_directory

PLUGIN = 'lingxi.documents'
DOCUMENT_TYPES = {'.txt', '.md', '.csv', '.pdf', '.docx', '.xlsx'}


class FileReadInput(FileListInput):
    @field_validator('path')
    @classmethod
    def document(cls, value):
        path = PurePosixPath(value)
        if path.suffix.lower() not in DOCUMENT_TYPES or len(path.name) > 200:
            raise ValueError('请选择 TXT、Markdown、CSV、文本 PDF、DOCX 或 XLSX 文件')
        return value


def read_snapshot(path, check, *, grant, limit):
    """Never follow links or open blocking special files; fence mutations on the open fd."""
    from .resources import check_path
    path = check_path(grant, FileReadInput(path=path).path)
    relative = PurePosixPath(path)
    directory = open_resource_directory(str(relative.parent), check)
    descriptor = None
    try:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HTTPException(422, '只能读取普通文件，不接受符号链接、硬链接或特殊文件')
        if not 0 < before.st_size <= limit:
            raise HTTPException(413, '文件为空或超过当前附件大小上限，请先拆分文件')
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(65536, limit + 1 - size)):
            check()
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, '文件读取期间超过附件上限，未保存快照')
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
        if signature(before) != signature(after) or signature(after) != signature(named) or size != after.st_size:
            raise HTTPException(409, '文件在读取期间发生变化，未保存；请稳定文件后重新创建任务')
        check()
        data = b''.join(chunks)
        return data, {'path': path, 'digest': hashlib.sha256(data).hexdigest(), 'size': size,
                      'observed_at': now(), 'modified_at': after.st_mtime}
    except OSError:
        raise HTTPException(409, '文件不可访问、已移动或包含链接，请检查授权资料目录') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def source_valid(db, item):
    source = item.source or {}
    if not source:
        return True  # Uploaded/QQ media retain their existing source policies.
    from .models import User
    from .resources import valid
    from .plugins import require_capability
    from .tasks import WorkTask
    owner = db.get(User, item.user_id)
    if (source.get('kind') != 'server_document' or not owner or owner.workspace_id != item.workspace_id
            or not valid(db, source.get('resource_policy'), owner)):
        return False
    task = db.get(WorkTask, source.get('task_id'))
    if (not task or task.user_id != item.user_id or task.workspace_id != item.workspace_id
            or task.skill_id != 'server.read' or task.status != 'succeeded'
            or task.config.get('resource_revoked') or task.result.get('media_id') != item.id):
        return False
    try:
        require_capability(db, owner, 'server.read', plugin_id=PLUGIN, release_id=source.get('plugin_release_id'))
    except HTTPException:
        return False
    return True


def require_source(db, item):
    if not source_valid(db, item):
        raise HTTPException(410, '服务器文档快照的来源授权已失效，请核对授权后重新读取原文件')


def invalidate_plugin(db, user, installation_id):
    from .plugins import PluginInstallation
    from .tasks import WorkTask
    from .resources import invalidate_task
    installation = db.get(PluginInstallation, installation_id)
    if installation and installation.plugin_id == PLUGIN:
        for task in db.scalars(select(WorkTask).where(WorkTask.user_id == user.id,
                WorkTask.workspace_id == user.workspace_id, WorkTask.skill_id == 'server.read')):
            invalidate_task(db, task, reason='文档读取插件已停用、卸载或撤销；旧快照与派生上下文停止使用，请核对后新建任务')


def import_media(db, task, owner, data, observed):
    from .media import Media, storage
    from .media_policy import current, check_sizes, check_types
    from .memory import enqueue
    suffix = PurePosixPath(task.input['path']).suffix.lower()
    policy = current(db, owner.workspace_id, lock=True)
    check_sizes(policy, [len(data)])
    check_types(policy, [suffix])
    identity = task.config['media_id']
    if db.get(Media, identity):
        raise HTTPException(409, '本任务的快照已存在；不重复导入或复活已删除快照')
    source = observed | {'kind': 'server_document', 'task_id': task.id,
        'resource_policy': task.config['resource_policy'], 'plugin_release_id': task.config['plugin_release_id'], 'plugin_version': task.config['plugin_version']}
    item = Media(id=identity, workspace_id=owner.workspace_id, user_id=owner.id,
        request_id='server-document:' + task.id, filename=PurePosixPath(task.input['path']).name,
        suffix=suffix, size=len(data), digest=observed['digest'], source=source)
    # A retry may replace an orphan blob from a transaction that never committed.
    # No API can read it before its authoritative Media row exists.
    path = storage(identity, 'blob')
    temporary = storage(uid(), 'blob')
    try:
        with temporary.open('xb') as output:
            output.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    db.add(item)
    enqueue(db, 'media-parse:' + identity, 'media_parse', {'media_id': identity})
    return {'media_id': identity, **observed, 'verification': 'filesystem_snapshot',
        'note': '只读快照已保存，正文解析状态见文件预览；不代表完整解析成功。原文件未修改，后续变化不会自动同步。'
                '在聊天中选择此附件才向所选模型发送正文；删除快照不删除服务器原文件。'}
