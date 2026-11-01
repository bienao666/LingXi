"""Owner-scoped installations, immutable release locks and live authorization."""
from fastapi import HTTPException
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now
from .models import Entity, User
from .plugin_manifest import Manifest, bundled_manifests, digest, permissions
from packaging.specifiers import SpecifierSet
from packaging.version import Version


class PluginRelease(Base):
    __tablename__ = 'plugin_releases'
    __table_args__ = (UniqueConstraint('plugin_id', 'version'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(30))
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(30), default='bundled')
    status: Mapped[str] = mapped_column(String(30), default='published')


class PluginInstallation(Entity, Base):
    __tablename__ = 'plugin_installations'
    __table_args__ = (UniqueConstraint('user_id', 'workspace_id', 'plugin_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    plugin_id: Mapped[str] = mapped_column(String(120))
    release_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default='enabled')
    grants: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    dependency_lock: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class PluginSnapshot(Entity, Base):
    __tablename__ = 'plugin_snapshots'
    __table_args__ = (UniqueConstraint('installation_id', 'number'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[int] = mapped_column(Integer)
    release_id: Mapped[str] = mapped_column(String(64))
    grants: Mapped[list] = mapped_column(JSON)
    config: Mapped[dict] = mapped_column(JSON)
    dependency_lock: Mapped[dict] = mapped_column(JSON)


class PluginPreview(Entity, Base):
    __tablename__ = 'plugin_previews'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    login_session_id: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    result: Mapped[list] = mapped_column(JSON, default=list)


class PluginInvocation(Entity, Base):
    __tablename__ = 'plugin_invocations'
    __table_args__ = (UniqueConstraint('user_id', 'workspace_id', 'request_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    installation_id: Mapped[str] = mapped_column(String(64), index=True)
    release_id: Mapped[str] = mapped_column(String(64))
    capability_id: Mapped[str] = mapped_column(String(120))
    input: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default='')
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


def owner_lock(db, user):
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def release_checked(db, identity, published=False, user=None):
    release = db.get(PluginRelease, identity)
    if not release or release.status == 'revoked' or (published and release.status != 'published'):
        raise HTTPException(409, '插件版本不可用或已撤销，请重新查看目录')
    try:
        spec = Manifest.model_validate(release.manifest).model_dump()
        if (digest(spec) != release.digest or release.plugin_id != spec['plugin_id']
                or release.version != spec['version']):
            raise ValueError('Immutable manifest mismatch')
    except (ValueError, TypeError):
        raise HTTPException(409, '插件清单校验失败，已阻止使用该版本') from None
    if release.source == 'store':
        if user is None:
            raise HTTPException(409, '远程版本必须核对真实主体范围')
        from .plugin_store import access
        access(db, user, release, fresh=published)
    return release


def installations(db, user):
    return db.scalars(select(PluginInstallation).where(PluginInstallation.user_id == user.id,
        PluginInstallation.workspace_id == user.workspace_id,
        PluginInstallation.status != 'uninstalled')).all()


def snapshot(db, row):
    db.flush()
    db.add(PluginSnapshot(user_id=row.user_id, workspace_id=row.workspace_id,
        installation_id=row.id, number=row.version, release_id=row.release_id,
        grants=list(row.grants), config=dict(row.config), dependency_lock=dict(row.dependency_lock)))


def seed_bundled(db):
    for spec in bundled_manifests():
        manifest = spec.model_dump()
        identity = digest({'plugin_id': spec.plugin_id, 'version': spec.version})
        existing = db.get(PluginRelease, identity)
        if existing:
            if existing.digest != digest(manifest):
                raise RuntimeError('Bundled published plugin is immutable; publish a new version')
        else:
            db.add(PluginRelease(id=identity, plugin_id=spec.plugin_id, version=spec.version,
                                digest=digest(manifest), manifest=manifest))
    db.flush()
    # Existing first-party workflows have always been part of the user's core.
    # Register them once, never resurrect an explicitly disabled/uninstalled row.
    release = db.scalar(select(PluginRelease).where(PluginRelease.plugin_id == 'lingxi.builtin'))
    for user in db.scalars(select(User)):
        if not db.scalar(select(PluginInstallation).where(PluginInstallation.user_id == user.id,
            PluginInstallation.workspace_id == user.workspace_id, PluginInstallation.plugin_id == 'lingxi.builtin')):
            row = PluginInstallation(user_id=user.id, workspace_id=user.workspace_id,
                plugin_id='lingxi.builtin', release_id=release.id, grants=permissions(release.manifest))
            db.add(row)
            snapshot(db, row)


def check_installation(db, user, row, release_id=None, seen=None, dependency_lock=None):
    if not row or row.user_id != user.id or row.workspace_id != user.workspace_id or row.status != 'enabled':
        raise HTTPException(409, '插件未启用，不能开始或提交新的调用')
    seen = set() if seen is None else set(seen)
    if row.plugin_id in seen or len(seen) > 8:
        raise HTTPException(409, '插件依赖无效')
    seen.add(row.plugin_id)
    release = release_checked(db, release_id or row.release_id, user=user)
    from .plugin_manifest import CORE_VERSION
    if Version(CORE_VERSION) not in SpecifierSet(release.manifest['core']):
        raise HTTPException(409, '固定插件版本与当前 Core 不兼容')
    if release.plugin_id != row.plugin_id:
        raise HTTPException(409, '插件版本不匹配')
    current = release_checked(db, row.release_id, user=user)
    # Pinned old tasks retain code, never permissions removed by an update.
    effective = set(row.grants) & set(permissions(current.manifest))
    if not set(permissions(release.manifest)) <= effective:
        raise HTTPException(409, '插件所需权限已撤销，请核对授权')
    if release_id and release_id != row.release_id and not db.scalar(select(PluginSnapshot).where(
            PluginSnapshot.installation_id == row.id, PluginSnapshot.release_id == release_id)):
        raise HTTPException(409, '任务版本不在此安装历史中')
    # Dependency locks in the fixed snapshot are checked, not silently replaced.
    lock = row.dependency_lock if dependency_lock is None else dependency_lock
    if dependency_lock is None and release_id and release_id != row.release_id:
        old = db.scalar(select(PluginSnapshot).where(PluginSnapshot.installation_id == row.id,
             PluginSnapshot.release_id == release_id).order_by(PluginSnapshot.number.desc()))
        lock = old.dependency_lock
    for dep in release.manifest['dependencies']:
        if dep['plugin_id'] not in lock:
            raise HTTPException(409, '插件依赖锁缺失，请重新安装')
        dep_release = release_checked(db, lock[dep['plugin_id']], user=user)
        if dep_release.plugin_id != dep['plugin_id'] or Version(dep_release.version) not in SpecifierSet(dep['constraint']):
            raise HTTPException(409, '插件依赖锁不满足声明的版本范围')
        child = db.scalar(select(PluginInstallation).where(PluginInstallation.user_id == user.id,
            PluginInstallation.workspace_id == user.workspace_id, PluginInstallation.plugin_id == dep['plugin_id']))
        check_installation(db, user, child, lock[dep['plugin_id']], seen, lock)
    return release


def require_capability(db, user, capability_id, plugin_id='lingxi.builtin', release_id=None, lock=False, dependency_lock=None):
    query = select(PluginInstallation).where(PluginInstallation.user_id == user.id,
        PluginInstallation.workspace_id == user.workspace_id, PluginInstallation.plugin_id == plugin_id)
    row = db.scalar(query.with_for_update() if lock else query)
    release = check_installation(db, user, row, release_id, dependency_lock=dependency_lock)
    capability = next((c for c in release.manifest['capabilities'] if c['id'] == capability_id), None)
    if not capability:
        raise HTTPException(409, '当前固定插件版本不提供此能力')
    return row, release, capability


def invalidate_invocations(db, user, installation_id, reason):
    installation = db.get(PluginInstallation, installation_id)
    if installation and installation.plugin_id == 'lingxi.builtin':
        from .reminder_delivery import invalidate as invalidate_reminders
        invalidate_reminders(db, user_id=user.id, reason='提醒能力已停用或撤销；需重新确认投递')
    from .server_documents import invalidate_plugin
    invalidate_plugin(db, user, installation_id)
    from .plugin_oauth import invalidate_installation
    invalidate_installation(db, user, installation_id)
    from .plugin_agents import revoke_bindings
    revoke_bindings(db, user, installation_id)
    from .plugin_events import invalidate_subscriptions
    invalidate_subscriptions(db, user, installation_id)
    from .plugin_api import invalidate_api
    invalidate_api(db, user, installation_id, reason)
    for row in db.scalars(select(PluginInvocation).where(PluginInvocation.user_id == user.id,
            PluginInvocation.workspace_id == user.workspace_id, PluginInvocation.installation_id == installation_id,
            PluginInvocation.status.in_(['queued', 'running']))):
        row.status, row.error, row.finished_at = 'invalidated', reason, now()
        row.version += 1


def installation_view(db, user, row):
    release = db.get(PluginRelease, row.release_id)
    reason = None
    try:
        check_installation(db, user, row)
    except HTTPException as exc:
        reason = str(exc.detail)
    return {'id': row.id, 'plugin_id': row.plugin_id, 'name': release.manifest['name'],
        'plugin_version': release.version, 'release_id': release.id, 'version': row.version,
        'status': row.status, 'grants': row.grants, 'dependency_lock': row.dependency_lock,
        'config': row.config, 'available': reason is None, 'reason': reason,
        'digest': release.digest, 'source': release.source, 'updated_at': row.updated_at}
