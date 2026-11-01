"""Owner-scoped, non-secret configuration journal and reviewed value rollback."""
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import math
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import Entity
from .web import Context, owned, view

router = APIRouter()
NAMES = ('workspace', 'media', 'operations', 'model_budget')
Name = Literal['workspace', 'media', 'operations', 'model_budget']
Point = Literal['before', 'after']
Action = Literal['edit', 'import', 'rollback', 'restore']


class ConfigurationRevision(Entity, Base):
    __tablename__ = 'configuration_revisions'
    __table_args__ = (UniqueConstraint('workspace_id', 'user_id', 'number'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    modules: Mapped[list] = mapped_column(JSON)
    before_snapshot: Mapped[dict] = mapped_column(JSON)
    after_snapshot: Mapped[dict] = mapped_column(JSON)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_point: Mapped[str | None] = mapped_column(String(10), nullable=True)


class ConfigurationRevisionView(BaseModel):
    id: str
    number: int
    created_at: float
    action: Action
    modules: list[Name]
    source_id: str | None
    source_point: Point | None


class ConfigurationRevisionPage(BaseModel):
    items: list[ConfigurationRevisionView]
    next_before: int | None


class ConfigurationRevisionDetail(ConfigurationRevisionView):
    before_snapshot: dict
    after_snapshot: dict


class RollbackInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    point: Point
    modules: list[Name] = Field(min_length=1, max_length=4)
    versions: dict[Name, int] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def scope(self):
        if (len(set(self.modules)) != len(self.modules) or set(self.modules) != set(self.versions)
                or any(v < 0 for v in self.versions.values())):
            raise ValueError('请选择唯一模块，并提供每项当前版本')
        return self


def snapshot(db, user):
    from .configuration_bundles import state
    return state(db, user).model_dump()


def checked_snapshot(value):
    from .configuration_bundles import ConfigurationState
    parsed = ConfigurationState.model_validate(value)
    if (not isinstance(value, dict) or set(value) != {'document', 'versions'}
            or set(parsed.versions) != set(NAMES) or any(type(v) is not int or v < 0 for v in value['versions'].values())
            or parsed.document.modules.model_fields_set != set(NAMES)):
        raise ValueError('Incomplete configuration snapshot')
    return parsed


def append(db, user, before, after, action, source_id=None, source_point=None):
    changed = [n for n in NAMES if before['versions'][n] != after['versions'][n]
        or before['document']['modules'][n] != after['document']['modules'][n]]
    if not changed:
        return
    number = (db.scalar(select(func.max(ConfigurationRevision.number)).where(
        ConfigurationRevision.user_id == user.id, ConfigurationRevision.workspace_id == user.workspace_id)) or 0)+1
    db.add(ConfigurationRevision(user_id=user.id, workspace_id=user.workspace_id, number=number,
        action=action, modules=changed, before_snapshot=before, after_snapshot=after,
        source_id=source_id, source_point=source_point))
    db.flush()


@contextmanager
def changing(ctx, action='edit', source_id=None, source_point=None):
    db, user, login = ctx
    # Bundle imports call existing domain endpoints. Only the outer operation
    # records one atomic revision, and every path uses owner -> budget lock order.
    if db.info.get('configuration_change'):
        yield
        return
    from .auth_totp import lock_owner
    lock_owner(db, user, login)
    before = snapshot(db, user)
    db.info['configuration_change'] = True
    try:
        yield
        db.flush()
        append(db, user, before, snapshot(db, user), action, source_id, source_point)
    finally:
        db.info.pop('configuration_change', None)


def tracked(function):
    @wraps(function)
    def change(body, ctx):
        with changing(ctx):
            return function(body, ctx)
    return change


def public(row):
    return {key: getattr(row, key) for key in ConfigurationRevisionView.model_fields}


@router.get('/api/admin/configuration-revisions', response_model=ConfigurationRevisionPage)
def history(ctx: Context, before: int | None = Query(None, ge=1)):
    db, user, _ = ctx
    query = select(ConfigurationRevision).where(ConfigurationRevision.user_id == user.id,
        ConfigurationRevision.workspace_id == user.workspace_id)
    if before is not None:
        query = query.where(ConfigurationRevision.number < before)
    rows = list(db.scalars(query.order_by(ConfigurationRevision.number.desc()).limit(21)))
    return dict(items=[public(r) for r in rows[:20]], next_before=rows[19].number if len(rows)>20 else None)


@router.get('/api/admin/configuration-revisions/{identity}', response_model=ConfigurationRevisionDetail)
def detail(identity: str, ctx: Context):
    db, user, _ = ctx
    row = owned(db, ConfigurationRevision, identity, user)
    return public(row) | {'before_snapshot': checked_snapshot(row.before_snapshot).model_dump(),
        'after_snapshot': checked_snapshot(row.after_snapshot).model_dump()}


def source(ctx, identity, point):
    db, user, _ = ctx
    row = owned(db, ConfigurationRevision, identity, user)
    value = row.before_snapshot if point == 'before' else row.after_snapshot
    state = checked_snapshot(value)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return state, digest


def preview(identity: str, body: RollbackInput, ctx: Context):
    from .configuration_bundles import ConfigurationDocument, ConfigurationImport, make_preview
    previous, digest = source(ctx, identity, body.point)
    document = ConfigurationDocument.model_validate({'format': 'lingxi-configuration-bundle/v1',
        'modules': {n: getattr(previous.document.modules, n).model_dump() for n in body.modules}})
    return make_preview(ConfigurationImport(document=document, versions=body.versions), ctx,
        history={'id': identity, 'point': body.point, 'digest': digest})


def checkpoint(db, user):
    from .operations import OpsPreference
    return {'revisions': [view(r) for r in db.scalars(select(ConfigurationRevision).where(
        ConfigurationRevision.workspace_id == user.workspace_id, ConfigurationRevision.user_id == user.id)
        .order_by(ConfigurationRevision.number))], 'state': snapshot(db, user),
        'operations': [view(r) for r in db.scalars(select(OpsPreference).where(
            OpsPreference.workspace_id == user.workspace_id, OpsPreference.user_id == user.id))]}


def validate_checkpoint(data, owner, domains=None):
    from .configuration_bundles import OperationalValues
    from .operations import OpsPreference
    if not isinstance(data, dict) or set(data) != {'revisions', 'state', 'operations'}:
        raise ValueError('Invalid configuration history checkpoint')
    state = checked_snapshot(data['state'])
    if not isinstance(data['revisions'], list) or not isinstance(data['operations'], list) or len(data['operations'])>1:
        raise ValueError('Invalid configuration history records')
    ids, numbers = set(), set()
    for r in data['revisions']:
        if not isinstance(r, dict) or set(r) != {c.name for c in ConfigurationRevision.__table__.columns}:
            raise ValueError('Invalid configuration revision')
        ConfigurationRevisionView.model_validate(r)
        if (r['user_id'] != owner['id'] or r['workspace_id'] != owner['workspace_id'] or
                not isinstance(r['id'], str) or not 1<=len(r['id'])<=64 or r['id'] in ids
                or type(r['number']) is not int or r['number']<=max(numbers, default=0)
                or type(r['created_at']) not in (int,float) or not math.isfinite(r['created_at']) or not 0<=r['created_at']<10**12
                or not r['modules'] or len(set(r['modules'])) != len(r['modules'])
                or (r['source_id'] is None) != (r['source_point'] is None)
                or (r['action'] == 'rollback') != (r['source_id'] is not None)
                or (r['source_id'] is not None and r['source_id'] not in ids)):
            raise ValueError('Invalid configuration revision identity or source')
        checked_snapshot(r['before_snapshot']);checked_snapshot(r['after_snapshot'])
        ids.add(r['id']);numbers.add(r['number'])
    for r in data['operations']:
        if (not isinstance(r, dict) or set(r) != {c.name for c in OpsPreference.__table__.columns}
                or r['user_id'] != owner['id'] or r['workspace_id'] != owner['workspace_id']
                or type(r['version']) is not int or r['version']<1):
            raise ValueError('Invalid operational checkpoint')
        OperationalValues.model_validate({k:r[k] for k in OperationalValues.model_fields})
    ops = data['operations'][0] if data['operations'] else None
    if state.versions['operations'] != (ops['version'] if ops else 0) or state.document.modules.operations.model_dump() != (
            {k:ops[k] for k in OperationalValues.model_fields} if ops else {'log_retention_days':90,'daily_token_alert':0}):
        raise ValueError('Operational checkpoint disagrees with configuration snapshot')
    if domains is not None:
        from .configuration_bundles import ConfigurationModules
        from . import media_policy, model_budgets, system_settings
        workspace = domains['system_settings']['settings']
        media = domains['media_policies']
        budget = domains['model_budgets']['policies']
        expected = ConfigurationModules(
            workspace=system_settings.Defaults(**(workspace[0]['values'] if workspace else {})).model_dump(),
            media=media_policy.MediaLimits(**({k:media[0][k] for k in media_policy.MediaLimits.model_fields} if media else {})).model_dump(),
            model_budget=model_budgets.Limits(**({k:budget[0][k] for k in model_budgets.Limits.model_fields} if budget else {})).model_dump(),
            operations=state.document.modules.operations)
        versions={'workspace':workspace[0]['version'] if workspace else 0,
            'media':media[0]['version'] if media else 0, 'model_budget':budget[0]['version'] if budget else 0,
            'operations':state.versions['operations']}
        if expected != state.document.modules or versions != state.versions:
            raise ValueError('Configuration snapshot disagrees with domain checkpoints')


def restore(db, data, user):
    from .operations import OpsPreference
    db.execute(delete(ConfigurationRevision).where(ConfigurationRevision.user_id == user.id,
        ConfigurationRevision.workspace_id == user.workspace_id))
    for r in data['revisions']:
        db.add(ConfigurationRevision(**r))
    prior = db.scalar(select(OpsPreference).where(OpsPreference.user_id == user.id,
        OpsPreference.workspace_id == user.workspace_id))
    values = data['state']['document']['modules']['operations']
    latest = data['state']['versions']['operations']
    if prior is None:
        prior = OpsPreference(user_id=user.id, workspace_id=user.workspace_id)
        db.add(prior)
    prior.version = max(prior.version or 0, latest)+1
    prior.log_retention_days, prior.daily_token_alert = values['log_retention_days'], values['daily_token_alert']
    db.flush()
    append(db, user, data['state'], snapshot(db, user), 'restore')
