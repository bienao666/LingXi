"""Server handover control plane. Transfer and database replacement stay on the host.

One prepared target, one frozen source, one encrypted handover bound to the exact
target instance and public origin. A source never automatically resumes writes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal
from urllib.parse import urlsplit

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal, now, uid
from .models import Entity, User
from .security import decrypt, encrypt
from .web import Context, audit, owned

router = APIRouter()


def migration_schema(directory: Path | None = None):
    """Use the packaged migration graph, independently of the working directory.

    Handover requires a fully upgraded, single-head database on both servers.
    Missing or branched migration packages must never relax that requirement.
    """
    directory = directory if directory is not None else Path(__file__).resolve().parents[1] / 'migrations'
    heads = ScriptDirectory(str(directory)).get_heads()
    if len(heads) != 1:
        raise RuntimeError('Server migration package requires exactly one Alembic head')
    return heads[0]


SCHEMA = migration_schema()
ACTIVE = ('planned', 'freezing', 'exported', 'awaiting_activation')


class MigrationSite(Base):
    __tablename__ = 'migration_sites'
    id: Mapped[str] = mapped_column(String(20), primary_key=True, default='local')
    instance_id: Mapped[str] = mapped_column(String(32), default=uid)
    mode: Mapped[str] = mapped_column(String(30), default='normal')
    origin: Mapped[str] = mapped_column(String(500))
    migration_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ticket: Mapped[str] = mapped_column(Text, default='')
    version: Mapped[int] = mapped_column(Integer, default=1)


class WorkspaceMigration(Entity, Base):
    __tablename__ = 'workspace_migrations'
    __table_args__ = (UniqueConstraint('user_id', 'workspace_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_digest: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(10), default='outbound')
    status: Mapped[str] = mapped_column(String(30), default='planned')
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_id: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(32))
    target_label: Mapped[str] = mapped_column(String(80))
    target_origin: Mapped[str] = mapped_column(String(500))
    target_ticket: Mapped[str] = mapped_column(Text)
    manifest_digest: Mapped[str] = mapped_column(String(64), default='')
    finished_at: Mapped[float] = mapped_column(Float, default=0)


class MigrationView(BaseModel):
    id: str
    direction: str
    status: str
    version: int
    source_id: str
    target_id: str
    target_label: str
    target_origin: str
    manifest_digest: str
    created_at: float
    finished_at: float


class MigrationOverview(BaseModel):
    mode: str
    instance_id: str | None
    runs: list[MigrationView]


class TargetInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    target_ticket: str = Field(min_length=40, max_length=8192)


class MigrationPreview(BaseModel):
    target_label: str
    target_origin: str
    target_id: str
    expires_at: float
    token: str
    note: str


class MigrationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=8, max_length=100)
    token: str = Field(min_length=40, max_length=16384)
    acknowledgement: Literal['freeze_source_before_transfer']


class MigrationVersion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=1)


def public(row):
    return {key: getattr(row, key) for key in MigrationView.model_fields}


def site(db, create=False):
    row = db.get(MigrationSite, 'local', populate_existing=True)
    if row is None and create:
        row = MigrationSite(id='local', origin=origin(settings().public_origin))
        db.add(row)
        db.flush()
    return row


def paused(db):
    row = site(db)
    return row is not None and row.mode != 'normal'


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment or len(value) > 500):
        raise ValueError('Use an HTTP(S) public origin without a path or credentials')
    # Parsing the port also rejects malformed registrations. No outbound request.
    port = parsed.port
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    default = 443 if parsed.scheme == 'https' else 80
    return parsed.scheme + '://' + host + (':' + str(port) if port and port != default else '')


def unpack(token, purpose):
    try:
        data = json.loads(decrypt(token))
        if not isinstance(data, dict) or data.get('purpose') != purpose:
            raise ValueError()
        return data
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('Invalid handover token or server master key') from None


def target_info(token, expired=False):
    data = unpack(token, 'migration-target')
    if (data.get('schema') != SCHEMA or type(data.get('expires_at')) not in (int, float)
            or (not expired and data['expires_at'] <= now())
            or not isinstance(data.get('instance_id'), str) or len(data['instance_id']) != 32
            or any(c not in '0123456789abcdef' for c in data['instance_id'])
            or not isinstance(data.get('label'), str) or not 1 <= len(data['label']) <= 80
            or any(ord(c) < 32 for c in data['label'])
            or origin(data.get('origin', '')) != data['origin']):
        raise ValueError('Target registration expired or is incompatible')
    return data


def schema(db):
    if MigrationContext.configure(db.connection()).get_current_heads() != (SCHEMA,):
        raise ValueError('Source and target require schema ' + SCHEMA)


def owner(db):
    from .backups import restore_owners
    users = restore_owners(db)
    if len(users) != 1:
        raise ValueError('Migration requires one login owner')
    return db.scalar(select(User).where(User.id == users[0].id).with_for_update())


def ensure_normal(db):
    if paused(db):
        raise HTTPException(409, '当前实例已处于迁移维护状态，请在服务器终端核对')


@router.get('/api/workspace-migrations', response_model=MigrationOverview)
def overview(ctx: Context):
    db, user, _ = ctx
    row = site(db)
    return dict(mode=row.mode if row else 'normal', instance_id=row.instance_id if row else None,
        runs=[public(item) for item in db.scalars(select(WorkspaceMigration).where(
            WorkspaceMigration.user_id == user.id, WorkspaceMigration.workspace_id == user.workspace_id)
            .order_by(WorkspaceMigration.created_at.desc(), WorkspaceMigration.id).limit(50))])


@router.post('/api/workspace-migrations/previews', response_model=MigrationPreview)
def preview(body: TargetInput, ctx: Context):
    db, user, session = ctx
    ensure_normal(db)
    try:
        schema(db)
        target = target_info(body.target_ticket)
        if target['origin'] == origin(settings().public_origin):
            raise ValueError('Target must use a different public origin')
    except (ValueError, TypeError):
        raise HTTPException(409, '目标凭证无效、已过期或版本／主密钥不匹配，请在目标服务器重新核对') from None
    expiry = min(now() + 300, target['expires_at'])
    token = encrypt(json.dumps(dict(purpose='migration-preview', user=user.id, workspace=user.workspace_id,
        session=session.id, target=body.target_ticket, expires_at=expiry)))
    return dict(target_label=target['label'], target_origin=target['origin'], target_id=target['instance_id'],
        expires_at=expiry, token=token,
        note='此步只建立迁移记录，不搬运数据。终端导出会冻结源端写入并停止旧 QQ 和订阅；'
            '目标只接受空工作区，导入后需核验及重新授权。主密钥单独传递，独立商店不在迁移包内。')


@router.post('/api/workspace-migrations', response_model=MigrationView)
def create(body: MigrationInput, ctx: Context):
    db, user, session = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    previous = db.scalar(select(WorkspaceMigration).where(WorkspaceMigration.user_id == user.id,
        WorkspaceMigration.workspace_id == user.workspace_id, WorkspaceMigration.request_id == body.request_id))
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    if previous:
        if previous.request_digest != digest:
            raise HTTPException(409, '请求编号已用于其他迁移预览')
        return public(previous)
    ensure_normal(db)
    try:
        data = unpack(body.token, 'migration-preview')
        if (data['user'] != user.id or data['workspace'] != user.workspace_id or data['session'] != session.id
                or not now() <= data['expires_at'] <= now() + 301):
            raise ValueError()
        schema(db)
        target = target_info(data['target'])
        if target['origin'] == origin(settings().public_origin):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, '迁移预览已失效或不属于当前登录，请重新核对') from None
    if db.scalar(select(WorkspaceMigration.id).where(WorkspaceMigration.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, '已有迁移等待处理，请先查看或取消尚未导出的记录')
    local = site(db, create=True)
    row = WorkspaceMigration(user_id=user.id, workspace_id=user.workspace_id, request_id=body.request_id,
        request_digest=digest, source_id=local.instance_id, target_id=target['instance_id'],
        target_label=target['label'], target_origin=target['origin'], target_ticket=data['target'])
    db.add(row)
    db.flush()
    audit(db, user, 'migration_planned', row.id)
    return public(row)


@router.post('/api/workspace-migrations/{identity}/cancel', response_model=MigrationView)
def cancel(identity: str, body: MigrationVersion, ctx: Context):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = owned(db, WorkspaceMigration, identity, user, lock=True)
    if row.version != body.version or row.status != 'planned':
        raise HTTPException(409, '仅可取消版本未变化且尚未开始导出的迁移')
    row.status, row.version, row.finished_at = 'cancelled', row.version + 1, now()
    audit(db, user, 'migration_cancelled', row.id)
    return public(row)


def counts(db):
    from .models import Conversation, Message, Memory, Candidate, Provider, Model
    from .media import Media
    from .tasks import WorkTask, Reminder
    from .channels import Channel
    return {model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (User, Conversation, Message, Memory, Candidate, Provider, Model, Media, WorkTask, Reminder, Channel)}


def pristine(db):
    from pathlib import Path
    from .assistants import AssistantProfile, ProfileRevision, PRESETS, PersonaInput, RoleInput, snapshot
    from .plugins import PluginInstallation, PluginRelease, PluginSnapshot
    from .plugin_manifest import bundled_manifests, digest, permissions
    from .media_policy import MediaPolicy, MediaLimits
    # Empty means no user business data, configured providers, reminders or IM accounts.
    # First-party defaults and initial administrator authentication may be present.
    totals = counts(db)
    if any(value for key, value in totals.items() if key != 'users') or totals['users'] != 1:
        raise ValueError('Target workspace must be empty; existing business data will not be overwritten')
    resources = settings().resource_dir or settings().runtime_dir / 'resources'
    if Path(resources).is_symlink() or (Path(resources).is_dir() and next(Path(resources).iterdir(), None) is not None):
        raise ValueError('Target resource volume must be empty')
    # Fail closed for every non-default table, including future features. Even a
    # disabled connection or saved preference is a target asset, not empty data.
    allowed = {'users', 'login_sessions', 'audit', 'two_factors', 'worker_states', 'runtime_events',
        'backup_agents', 'migration_sites', 'assistant_profiles', 'assistant_profile_revisions',
        'plugin_releases', 'plugin_installations', 'plugin_snapshots', 'media_policies'}
    for table in Base.metadata.sorted_tables:
        if table.name not in allowed and db.scalar(select(func.count()).select_from(table)):
            raise ValueError('Target has saved business data or configuration: ' + table.name)
    user = db.scalar(select(User))
    defaults = {}
    for key, name, description, style, expertise, completion in PRESETS:
        for kind in ('persona', 'role'):
            identity = hashlib.sha256(f'{user.workspace_id}:{user.id}:{kind}:{key}'.encode()).hexdigest()[:32]
            spec = (PersonaInput(name=name, style=style) if kind == 'persona' else RoleInput(
                name=name, expertise=expertise, context_categories=['projects', 'procedures'], completion=completion))
            defaults[identity] = dict(id=identity, kind=kind, name=name, description=description, enabled=True,
                version=1, spec=spec.model_dump(exclude={'name', 'description', 'enabled', 'version'}))
    for profile in db.scalars(select(AssistantProfile)):
        if profile.user_id != user.id or profile.workspace_id != user.workspace_id or snapshot(profile) != defaults.get(profile.id):
            raise ValueError('Target has customized assistant profiles')
    for revision in db.scalars(select(ProfileRevision)):
        if revision.version != 1 or revision.snapshot != defaults.get(revision.profile_id):
            raise ValueError('Target has assistant revision history')
    releases = {digest({'plugin_id': item.plugin_id, 'version': item.version}): item.model_dump()
        for item in bundled_manifests()}
    for release in db.scalars(select(PluginRelease)):
        if (release.manifest != releases.get(release.id) or release.digest != digest(release.manifest)
                or release.status != 'published' or release.source != 'bundled'):
            raise ValueError('Target has non-default plugin releases')
    for installation in db.scalars(select(PluginInstallation)):
        release = releases.get(installation.release_id)
        if (not release or installation.plugin_id != 'lingxi.builtin' or installation.version != 1
                or installation.user_id != user.id or installation.workspace_id != user.workspace_id
                or installation.status != 'enabled' or installation.config or installation.dependency_lock
                or installation.grants != permissions(release)):
            raise ValueError('Target has customized plugin installations')
    for revision in db.scalars(select(PluginSnapshot)):
        install = db.get(PluginInstallation, revision.installation_id)
        if (not install or revision.number != 1 or revision.config or revision.dependency_lock
                or revision.release_id != install.release_id or revision.grants != install.grants):
            raise ValueError('Target has plugin history')
    for policy in db.scalars(select(MediaPolicy)):
        if (policy.workspace_id != user.workspace_id or policy.version != 1
                or {key: getattr(policy, key) for key in MediaLimits.model_fields} != MediaLimits().model_dump()):
            raise ValueError('Target has customized media configuration')


def prepare_target(label):
    with SessionLocal.begin() as db:
        user = owner(db)
        schema(db)
        pristine(db)
        local = site(db, create=True)
        if local.mode == 'prepared' and local.ticket:
            # Reuse the exact registration, including after expiry. A frozen
            # source may still reference it; silently rotating breaks handover.
            return {'ticket': local.ticket, 'instance_id': local.instance_id, 'origin': local.origin}
        if local.mode != 'normal' or db.scalar(select(WorkspaceMigration.id).limit(1)):
            raise ValueError('Target already participates in a migration')
        if not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 for c in label):
            raise ValueError('Provide a short target label')
        token = encrypt(json.dumps(dict(purpose='migration-target', instance_id=local.instance_id,
            origin=local.origin, label=label.strip(), schema=SCHEMA, expires_at=now() + 7 * 86400)))
        local.mode, local.ticket, local.version = 'prepared', token, local.version + 1
        audit(db, user, 'migration_target_prepared')
        return {'ticket': token, 'instance_id': local.instance_id, 'origin': local.origin}


def freeze(identity):
    from .backups import BackupRun
    from .voice import VoiceSession
    from .voice_realtime import RealtimeTicket
    from .channels import Channel
    with SessionLocal.begin() as db:
        user = owner(db)
        schema(db)
        row = owned(db, WorkspaceMigration, identity, user, lock=True)
        local = site(db)
        if local and local.mode == 'source_frozen' and local.migration_id == row.id and row.status in ('freezing', 'exported'):
            return public(row)
        if not local or local.mode != 'normal' or row.status != 'planned':
            raise ValueError('Source cannot start this migration')
        target_info(row.target_ticket)
        if db.scalar(select(BackupRun.id).where(BackupRun.status.in_(['queued', 'running'])).limit(1)):
            raise ValueError('Finish or cancel pending backup operations first')
        if db.scalar(select(VoiceSession.id).where(VoiceSession.status == 'active', VoiceSession.expires_at > now()).limit(1)):
            raise ValueError('End active browser voice sessions before freezing')
        if db.scalar(select(RealtimeTicket.id).where(RealtimeTicket.status == 'active', RealtimeTicket.expires_at > now()).limit(1)):
            raise ValueError('End active realtime transcription before freezing')
        local.mode, local.migration_id, local.version = 'source_frozen', row.id, local.version + 1
        row.status, row.version = 'freezing', row.version + 1
        # Disconnect existing sockets as well as rejecting newly authenticated ones.
        for channel in db.scalars(select(Channel)):
            channel.enabled, channel.connection_id, channel.lease_until = False, None, 0
            channel.version += 1
        audit(db, user, 'migration_source_frozen', row.id)
        return public(row)


def seal(data):
    with SessionLocal.begin() as db:
        user = owner(db)
        row = owned(db, WorkspaceMigration, data['id'], user, lock=True)
        local = site(db)
        digest = data['manifest_digest']
        if (not local or local.mode != 'source_frozen' or local.migration_id != row.id
                or row.status not in ('freezing', 'exported') or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)
                or (row.manifest_digest and row.manifest_digest != digest)):
            raise ValueError('Source is not frozen for this archive')
        summary = counts(db)
        # Host passes the cold snapshot's safety checkpoint, not a later live one.
        checkpoint = data['checkpoint']
        if checkpoint['schema'] != SCHEMA or checkpoint['users'][0]['id'] != user.id:
            raise ValueError('Snapshot owner or schema changed')
        state = checkpoint.get('migrations', {})
        if state.get('site', {}).get('mode') != 'source_frozen' or state['site']['migration_id'] != row.id:
            raise ValueError('Snapshot was not taken after source freeze')
        bundle = dict(purpose='migration-handover', format=1, migration_id=row.id,
            source_id=row.source_id, target_ticket=row.target_ticket, manifest_digest=digest,
            counts=summary, checkpoint=checkpoint)
        row.status, row.manifest_digest, row.version = 'exported', digest, row.version + 1
        audit(db, user, 'migration_exported', row.id)
        return {'handover': encrypt(json.dumps(bundle)), 'migration': public(row)}


def inspect_bundle(data, imported=False):
    bundle = unpack(data['handover'], 'migration-handover')
    target = target_info(bundle['target_ticket'], expired=True)
    if (bundle.get('format') != 1 or bundle['manifest_digest'] != data['manifest_digest']
            or target['origin'] != origin(settings().public_origin)
            or bundle['checkpoint']['schema'] != SCHEMA):
        raise ValueError('Handover digest, schema or target origin mismatch')
    local = data.get('target') if imported else None
    with SessionLocal() as db:
        schema(db)
        if not imported:
            pristine(db)
            current = site(db)
            local = {c.name: getattr(current, c.name) for c in MigrationSite.__table__.columns} if current else {}
        if (not local or local.get('mode') != 'prepared' or local.get('ticket') != bundle['target_ticket']
                or local.get('instance_id') != target['instance_id'] or local.get('origin') != target['origin']):
            raise ValueError('Bundle belongs to a different prepared target')
    return bundle, local


def imported(data):
    from .backups import quarantine
    bundle, target = inspect_bundle(data, imported=True)
    # The imported database must itself contain the frozen source marker.
    with SessionLocal() as db:
        local = site(db)
        row = db.get(WorkspaceMigration, bundle['migration_id'])
        if (not local or local.mode != 'source_frozen' or local.instance_id != bundle['source_id']
                or local.migration_id != bundle['migration_id'] or not row
                or row.source_id != bundle['source_id'] or row.target_ticket != bundle['target_ticket']):
            raise ValueError('Archive does not contain this frozen source')
    quarantine(bundle['checkpoint'])
    with SessionLocal.begin() as db:
        user = owner(db)
        if counts(db) != bundle['counts']:
            raise ValueError('Imported authority counts differ from source snapshot')
        verify_media(db)
        local = site(db)
        local.instance_id, local.mode, local.origin = target['instance_id'], 'target_pending', target['origin']
        local.migration_id = bundle['migration_id']
        local.ticket, local.version = target['ticket'], target['version'] + 1
        row = db.get(WorkspaceMigration, bundle['migration_id'])
        row.direction, row.status, row.version = 'inbound', 'awaiting_activation', row.version + 1
        row.manifest_digest = bundle['manifest_digest']
        audit(db, user, 'migration_import_verified', row.id)
        return public(row)


def verify_media(db):
    from .media import Media, storage
    for item in db.scalars(select(Media).where(Media.status.not_in(['deleted', 'fetching']))):
        if item.status == 'failed' and not item.digest and item.size == 0:
            continue  # QQ staged download failed before any file was committed.
        path = storage(item.id, 'blob')
        if not path.is_file() or path.stat().st_size != item.size:
            raise ValueError('Imported media file is missing or has changed size')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024**2), b''):
                digest.update(chunk)
        if digest.hexdigest() != item.digest:
            raise ValueError('Imported media digest mismatch')


def activate(identity):
    with SessionLocal.begin() as db:
        user = owner(db)
        schema(db)
        local = site(db)
        row = owned(db, WorkspaceMigration, identity, user, lock=True)
        if (not local or local.mode != 'target_pending' or local.migration_id != row.id
                or local.instance_id != row.target_id or local.origin != origin(settings().public_origin)
                or row.direction != 'inbound' or row.status != 'awaiting_activation'):
            raise ValueError('Target is not awaiting activation for this migration')
        verify_media(db)
        local.mode, local.version = 'normal', local.version + 1
        row.status, row.version, row.finished_at = 'completed', row.version + 1, now()
        audit(db, user, 'migration_target_activated', row.id)
        return public(row)


def recover_target(data):
    from .models import LoginSession
    from sqlalchemy import delete
    with SessionLocal.begin() as db:
        owner(db)
        schema(db)
        pristine(db)
        local = site(db)
        expected = data.get('migrations', {}).get('site')
        if (not local or not expected or local.mode != 'prepared' or expected.get('mode') != 'prepared'
                or local.instance_id != expected.get('instance_id') or local.ticket != expected.get('ticket')
                or local.origin != origin(settings().public_origin)):
            raise ValueError('Rollback must restore the exact empty prepared target')
        db.execute(delete(LoginSession))
        return {'mode': 'prepared'}


def checkpoint(db):
    from .web import view
    local = site(db)
    return {'site': view(local) if local else None, 'runs': [view(row) for row in db.scalars(select(WorkspaceMigration))]}


def restore(db, data):
    if data is None:
        return  # Older snapshots may not release an existing instance fence.
    local = data.get('site')
    if local:
        db.merge(MigrationSite(**local))
    for values in data.get('runs', []):
        db.merge(WorkspaceMigration(**values))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare-target', 'freeze', 'seal', 'inspect', 'imported', 'activate', 'recover-target'])
    args = parser.parse_args()
    data = json.load(sys.stdin)
    if args.action == 'prepare-target':
        result = prepare_target(data['label'])
    elif args.action == 'freeze':
        result = freeze(data['id'])
    elif args.action == 'seal':
        result = seal(data)
    elif args.action == 'inspect':
        bundle, target = inspect_bundle(data)
        result = {'id': bundle['migration_id'], 'target': target, 'counts': bundle['counts']}
    elif args.action == 'imported':
        result = imported(data)
    elif args.action == 'recover-target':
        result = recover_target(data)
    else:
        result = activate(data['id'])
    print(json.dumps(result))


if __name__ == '__main__':
    main()
