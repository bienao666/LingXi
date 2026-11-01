"""Effective memory models and generation-bound, reviewed index transitions.

The SQL records remain authoritative. Model selection is never delegated to a
chat persona or automatic conversation routing.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, Integer, JSON, String, func, select, text
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal, now, uid
from .models import Job, Memory, Model, Operation, Policy, Provider
from .web import Context, audit

router = APIRouter()
PURPOSES = ('extraction', 'embedding', 'rerank')
# Artifact revisions are shared with the inference image.
from .builtin_models import BUILTIN_MODELS


class MemoryConfiguration(Base):
    __tablename__ = 'memory_configuration'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default='simple')
    version: Mapped[int] = mapped_column(Integer, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    embedding_fingerprint: Mapped[str] = mapped_column(String(64), default='')
    phase: Mapped[str] = mapped_column(String(20), default='ready')
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    pending: Mapped[dict] = mapped_column(JSON, default=dict)
    rebuild_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MemoryFlight(Base):
    __tablename__ = 'memory_flights'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=now)
    status: Mapped[str] = mapped_column(String(20), default='running')


class MemorySettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    mode: Literal['simple', 'advanced']
    models: dict[Literal['extraction', 'embedding', 'rerank'], str] = Field(default_factory=dict)


class MemorySettingsConfirm(MemorySettingsInput):
    preview_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    preview_expires_at: int


class MemoryPurposeView(BaseModel):
    purpose: str
    source: Literal['inherited', 'builtin', 'independent']
    model_id: str | None = None
    model: str | None = None
    fingerprint: str
    state: str
    notice: str


class MemorySettingsView(BaseModel):
    version: int
    mode: Literal['simple', 'advanced']
    epoch: int
    phase: str
    purposes: list[MemoryPurposeView]
    rebuild_id: str | None
    verification_error: str | None
    rebuild_required: bool = False


class MemorySettingsPreview(BaseModel):
    preview_digest: str
    preview_expires_at: int
    rebuild_required: bool
    active_memories: int
    purposes: list[MemoryPurposeView]
    notice: str


class MemoryRebuildView(BaseModel):
    id: str
    status: str
    phase: str
    epoch: int
    total: int
    ready: int
    failed: int
    pending: int
    error: str | None


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def configuration(db, workspace='personal', create=False, lock=False):
    query = select(MemoryConfiguration).where(MemoryConfiguration.workspace_id == workspace).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row or not create:
        return row
    legacy = any(db.get(Policy, p) for p in PURPOSES)
    embedding = db.get(Policy, 'embedding')
    row = MemoryConfiguration(workspace_id=workspace, mode='advanced' if legacy else 'simple',
        version=1, epoch=embedding.version if embedding else 0, phase='ready', verification={}, pending={})
    db.add(row); db.flush()
    row.embedding_fingerprint = selection(db, 'embedding', workspace)['fingerprint']
    db.flush()
    return row


def mode(db, workspace='personal'):
    row = configuration(db, workspace)
    return row.mode if row else ('advanced' if any(db.get(Policy, p) for p in PURPOSES) else 'simple')


def selection(db, purpose, workspace='personal', selected_mode=None, overrides=None):
    from .providers import protocol_for
    selected_mode = selected_mode or mode(db, workspace)
    if selected_mode == 'simple' and purpose in ('embedding', 'rerank'):
        spec = BUILTIN_MODELS[purpose]
        return dict(purpose=purpose, source='builtin', model_id='builtin-'+purpose, model=spec['name'],
            fingerprint=hash_value(spec | {'dimensions': 1024 if purpose == 'embedding' else None}),
            state='configured', notice='服务器内置；需检查实际下载与推理状态')
    target = 'chat' if selected_mode == 'simple' and purpose == 'extraction' else purpose
    policy = db.get(Policy, target, populate_existing=True)
    identity = (overrides or {}).get(purpose) if target == purpose else None
    model = db.get(Model, identity or (policy.model_id if policy else ''), populate_existing=True)
    provider = db.get(Provider, model.provider_id, populate_existing=True) if model else None
    valid_scope = model and provider and model.workspace_id == workspace and provider.workspace_id == workspace and model.purpose == target
    fingerprint = hash_value([purpose, target, model.id, model.name, provider.id, provider.base_url,
        protocol_for(provider, db), settings().memory_embedding_dimensions if purpose == 'embedding' else None]) if valid_scope else ''
    state = 'configured' if valid_scope and provider.enabled and model.status == 'verified' and model.verified_provider_version == provider.version else 'unconfigured'
    return dict(purpose=purpose, source='inherited' if target != purpose else 'independent',
        model_id=model.id if valid_scope else None, model=model.name if valid_scope else None,
        fingerprint=fingerprint, state=state, notice='沿用固定对话模型；抽取能力单独验证' if target != purpose else '独立记忆模型')


def extraction_signature(db, workspace='personal'):
    choice = selection(db, 'extraction', workspace)
    model = db.get(Model, choice['model_id'] or '')
    provider = db.get(Provider, model.provider_id) if model else None
    chat = db.get(Policy, 'chat', populate_existing=True)
    return hash_value([choice['fingerprint'], provider.version if provider else None, chat.version if chat else None])


def inherited_verified(db, workspace='personal'):
    row = configuration(db, workspace)
    return bool(row and row.verification.get('status') == 'verified'
        and row.verification.get('signature') == extraction_signature(db, workspace))


def resolve_effective(db, purpose, model_id=None, verified=True):
    """Return None for ordinary/legacy resolution, otherwise an effective triple."""
    if purpose not in PURPOSES or model_id is not None or mode(db) != 'simple':
        return None
    row = configuration(db)
    if purpose == 'extraction':
        from .gateway import resolve_model
        model, provider, version = resolve_model(db, 'chat', verified=verified)
        if verified and not inherited_verified(db, model.workspace_id):
            raise HTTPException(409, '记忆抽取沿用对话模型，请先在路由策略中验证抽取能力')
        return model, provider, version
    choice = selection(db, purpose)
    model = SimpleNamespace(id=choice['model_id'], name=choice['model'], purpose=purpose,
        provider_id='builtin-memory', workspace_id='personal', status='verified', verified_provider_version=1)
    provider = SimpleNamespace(id='builtin-memory', base_url=settings().memory_inference_url,
        version=1, workspace_id='personal', enabled=True, builtin=True)
    return model, provider, row.version if row else 0


def effective_snapshot(db, purpose, workspace='personal'):
    if purpose not in PURPOSES:
        return {}
    choice = selection(db, purpose, workspace)
    row = configuration(db, workspace)
    policy_purpose = 'chat' if choice['source'] == 'inherited' else purpose
    policy = db.get(Policy, policy_purpose, populate_existing=True)
    return {'memory_configuration_version': row.version if row else 0,
        'configuration_source': choice['source'], 'model_fingerprint': choice['fingerprint'],
        'source_policy_purpose': policy_purpose, 'source_policy_version': policy.version if policy else None,
        'inference_location': 'server' if choice['source'] == 'builtin' else 'provider'}


def builtin_request(route, body=None):
    from .network import outbound_client
    with outbound_client(settings().memory_inference_url, allow_private=True, timeout=3 if body is None else 180,
            headers={'Authorization': 'Bearer '+settings().memory_gateway_token}) as client:
        response = client.get(route) if body is None else client.post(route, json=body)
        if not response.is_success:
            raise HTTPException(503, '内置记忆推理服务尚未就绪，请查看模型下载、资源与服务状态')
        return response.json()


def view(db, workspace='personal', probe=False):
    row = configuration(db, workspace)
    choices = [selection(db, p, workspace) for p in PURPOSES]
    for choice in choices:
        if choice['source'] == 'inherited' and choice['state'] == 'configured' and not inherited_verified(db, workspace):
            choice['state'] = 'unverified'
        if probe and choice['source'] == 'builtin':
            try:
                status = builtin_request('status')['models'][choice['purpose']]
                choice['state'], choice['notice'] = status['state'], status['notice']
            except Exception:
                choice['state'], choice['notice'] = 'unavailable', '内置推理服务不可达，文字聊天仍可使用'
    return dict(version=row.version if row else 0, mode=mode(db, workspace), epoch=row.epoch if row else legacy_epoch(db),
        phase=row.phase if row else 'ready', purposes=choices, rebuild_id=row.rebuild_id if row else None,
        rebuild_required=bool(row and row.embedding_fingerprint and row.embedding_fingerprint != choices[1]['fingerprint']),
        verification_error=row.verification.get('error') if row and row.verification.get('signature') == extraction_signature(db, workspace) else None)


def legacy_epoch(db):
    policy = db.get(Policy, 'embedding')
    return policy.version if policy else 0


def proposed(db, user, body):
    if body.mode == 'simple' and body.models:
        raise HTTPException(422, '简单模式不接受独立记忆模型配置')
    choices = [selection(db, p, user.workspace_id, body.mode, body.models) for p in PURPOSES]
    if body.mode == 'simple' and settings().memory_embedding_dimensions != 1024:
        raise HTTPException(409, '简单模式固定为 1024 维，请先完成部署级维度迁移')
    if choices[1]['fingerprint'] != selection(db, 'embedding', user.workspace_id)['fingerprint'] and body.mode == 'advanced' and any(c['state'] != 'configured' for c in choices):
        raise HTTPException(409, '请先添加并验证三个独立记忆模型，再切换高级模式以重建索引')
    if body.mode == 'advanced':
        for purpose, identity in body.models.items():
            model = db.get(Model, identity)
            if not model or model.workspace_id != user.workspace_id or model.purpose != purpose:
                raise HTTPException(422, '所选记忆模型用途或范围不匹配')
            from .gateway import resolve_model
            resolve_model(db, purpose, identity)
    from .network import SystemProxy
    proxy = db.get(SystemProxy, 'system')
    return {'mode': body.mode, 'models': body.models, 'purposes': choices,
        'provider_versions': [(c['model_id'], db.get(Provider, db.get(Model, c['model_id']).provider_id).version)
            for c in choices if c['model_id'] and c['source'] != 'builtin'], 'proxy_version': proxy.version if proxy else 0}


def digest(payload, version, user, expires):
    value = json.dumps([user.id, user.workspace_id, version, payload, expires], sort_keys=True, ensure_ascii=False).encode()
    return hmac.new(settings().encryption_key.encode(), value, hashlib.sha256).hexdigest()


@router.get('/api/memory-settings', response_model=MemorySettingsView)
def settings_get(ctx: Context):
    db, user, _ = ctx
    return view(db, user.workspace_id, probe=True)


@router.post('/api/memory-settings/preview', response_model=MemorySettingsPreview)
def settings_preview(body: MemorySettingsInput, ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id)
    if body.version != (row.version if row else 0) or (row and row.phase != 'ready'):
        raise HTTPException(409, '记忆配置变化或重建中，请刷新状态')
    payload = proposed(db, user, body)
    previous = row.embedding_fingerprint if row and row.embedding_fingerprint else selection(db, 'embedding', user.workspace_id)['fingerprint']
    changed = previous != payload['purposes'][1]['fingerprint']
    count = db.scalar(select(func.count()).select_from(Memory).where(Memory.workspace_id == user.workspace_id, Memory.status == 'active'))
    expires = int(now())+300
    return dict(preview_digest=digest(payload, body.version, user, expires), preview_expires_at=expires,
        rebuild_required=bool(changed and count), active_memories=count, purposes=payload['purposes'],
        notice='更换嵌入模型会暂停记忆检索并后台重建；文字聊天继续。' if changed and count else '无需重建索引。抽取沿用对话服务时仍可能产生费用。')


@router.put('/api/memory-settings', response_model=MemorySettingsView)
def settings_save(body: MemorySettingsConfirm, ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id, lock=True)
    if body.version != (row.version if row else 0) or (row and row.phase != 'ready'):
        raise HTTPException(409, '记忆配置变化或重建中，请刷新后预览')
    payload = proposed(db, user, body)
    if not now() < body.preview_expires_at <= now()+301 or not hmac.compare_digest(
            digest(payload, body.version, user, body.preview_expires_at), body.preview_digest):
        raise HTTPException(409, '预览已过期或配置发生变化，请重新预览')
    previous = row.embedding_fingerprint if row and row.embedding_fingerprint else selection(db, 'embedding', user.workspace_id)['fingerprint']
    row = row or configuration(db, user.workspace_id, create=True)
    if previous == payload['purposes'][1]['fingerprint'] and payload['mode'] == row.mode and all(db.get(Policy, p) and db.get(Policy, p).model_id == i for p, i in body.models.items()):
        return view(db, user.workspace_id)
    count = db.scalar(select(func.count()).select_from(Memory).where(Memory.workspace_id == user.workspace_id, Memory.status == 'active'))
    needs_rebuild = previous != payload['purposes'][1]['fingerprint']
    # Always drain for an embedding change, even with zero memories: an old
    # in-flight retain must not race a new bank/configuration.
    if needs_rebuild:
        operation = Operation(user_id=user.id, workspace_id=user.workspace_id, kind='memory_rebuild', status='pending',
            result={'previous_fingerprint': previous, 'target_fingerprint': payload['purposes'][1]['fingerprint'], 'initial_total': count})
        db.add(operation); db.flush()
        row.phase, row.pending, row.rebuild_id = 'draining', payload, operation.id
    else:
        apply_configuration(db, row, payload)
    row.version += 1
    audit(db, user, 'memory_configuration_update', row.rebuild_id)
    db.flush()
    return view(db, user.workspace_id)


def apply_configuration(db, row, payload):
    for purpose, identity in payload['models'].items():
        policy = db.get(Policy, purpose)
        if policy and policy.model_id != identity:
            policy.model_id, policy.version = identity, policy.version+1
        elif not policy:
            db.add(Policy(purpose=purpose, model_id=identity))
    if row.mode != payload['mode']:
        row.verification = {}
    row.mode = payload['mode']


@router.post('/api/memory-settings/verify-extraction', response_model=MemorySettingsView)
def verify_extraction(ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id, create=True, lock=True)
    if row.mode != 'simple' or row.phase in ('draining', 'blocked'):
        raise HTTPException(409, '请在简单模式且非重建期间验证继承的抽取能力')
    signature = extraction_signature(db, user.workspace_id)
    if row.verification.get('status') == 'running' and row.verification.get('started_at', 0) > now()-180:
        raise HTTPException(409, '抽取验证正在执行，请稍后刷新')
    from .gateway import resolve_model, complete, validation_request, validate_capability
    from .security import safe_error
    model, provider, _ = resolve_model(db, 'chat')
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
        raise HTTPException(409, '缺少当前工作区的对话模型')
    row.verification = {'signature': signature, 'status': 'running', 'started_at': now()}
    db.commit()
    error = None
    try:
        _, payload = validation_request(SimpleNamespace(purpose='extraction', name=model.name))
        output = complete(model, provider, payload['messages'], purpose='extraction', max_tokens=512,
            config={'validation': True, 'configuration_source': 'inherited'})
        validate_capability('extraction', {'choices': [{'message': {'content': output}}]})
    except Exception as exc:
        error = safe_error(exc)
    row = configuration(db, user.workspace_id, lock=True)
    if row.mode != 'simple' or signature != extraction_signature(db, user.workspace_id):
        raise HTTPException(409, '验证期间对话配置变化，请按新配置重新验证')
    row.verification = {'signature': signature, 'status': 'failed' if error else 'verified', 'at': now(), 'error': error}
    audit(db, user, 'inherited_extraction_test')
    return view(db, user.workspace_id)


@contextmanager
def flight(epoch, *, recall=False, workspace='personal'):
    """Nonblocking shared gate: never wait behind an exclusive switch while a
    Hindsight callback is waiting for the Core. Unknown requests block cutover.
    """
    with SessionLocal() as guard:
        if guard.bind.dialect.name == 'postgresql':
            if not guard.scalar(text('SELECT pg_try_advisory_xact_lock_shared(1861024)')):
                raise HTTPException(503, '记忆配置切换中，请稍后重试')
        row = configuration(guard, workspace)
        current_epoch = row.epoch if row else legacy_epoch(guard)
        if epoch != current_epoch or row and (row.phase in ('draining', 'blocked') or recall and row.phase != 'ready'):
            raise HTTPException(503, '记忆索引维护中，暂不使用长期记忆')
        if row and row.embedding_fingerprint and row.embedding_fingerprint != selection(guard, 'embedding', workspace)['fingerprint']:
            raise HTTPException(503, '嵌入模型指纹变化，请在记忆配置中预览并确认重建索引')
        identity = uid()
        with SessionLocal.begin() as db:
            db.add(MemoryFlight(id=identity, workspace_id=workspace, epoch=epoch, status='running'))
        try:
            yield
        except Exception as exc:
            import httpx
            with SessionLocal.begin() as db:
                db.get(MemoryFlight, identity).status = 'unknown' if isinstance(exc, httpx.TransportError) else 'failed'
            raise
        else:
            with SessionLocal.begin() as db:
                db.get(MemoryFlight, identity).status = 'completed'


def rebuild_view(db, operation, row):
    from .memory import usable
    total = ready = failed = 0
    for memory in db.scalars(select(Memory).where(Memory.workspace_id == operation.workspace_id,
            Memory.status == 'active')).yield_per(64):
        if not usable(memory):
            continue
        total += 1
        ready += memory.index_status == 'ready' and memory.index_epoch == row.epoch
        failed += memory.index_status == 'failed'
    return dict(id=operation.id, status=operation.status, phase=row.phase, epoch=row.epoch, total=total,
        ready=ready, failed=failed, pending=max(0, total-ready-failed), error=operation.error)


@router.get('/api/memory-rebuilds', response_model=list[MemoryRebuildView])
def rebuilds(ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id)
    if not row:
        return []
    operations = db.scalars(select(Operation).where(Operation.workspace_id == user.workspace_id,
        Operation.user_id == user.id, Operation.kind == 'memory_rebuild').order_by(Operation.created_at.desc()).limit(20))
    return [rebuild_view(db, op, row) if op.id == row.rebuild_id else dict(id=op.id, status=op.status, phase='historical',
        epoch=op.result.get('epoch', 0), total=op.result.get('total', 0), ready=op.result.get('ready', 0),
        failed=op.result.get('failed', 0), pending=0, error=op.error) for op in operations]


class RebuildRetry(BaseModel):
    version: int = Field(ge=1)
    acknowledge_service_restarted: bool = False


class RebuildStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    confirmation: Literal['rebuild_current_index']


@router.post('/api/memory-rebuilds', response_model=MemoryRebuildView, status_code=202)
def rebuild_start(body: RebuildStart, ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id, lock=True)
    if body.version != (row.version if row else 0) or row and row.phase != 'ready':
        raise HTTPException(409, '记忆配置变化或重建中，请刷新')
    for purpose in PURPOSES:
        from .gateway import resolve_model
        model, provider, _ = resolve_model(db, purpose)
        if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
            raise HTTPException(409, '记忆模型范围无效')
    row = row or configuration(db, user.workspace_id, create=True)
    operation = Operation(user_id=user.id, workspace_id=user.workspace_id, kind='memory_rebuild', status='pending', result={})
    db.add(operation); db.flush()
    row.phase, row.rebuild_id = 'draining', operation.id
    row.pending = proposed(db, user, MemorySettingsInput(version=row.version, mode=row.mode))
    row.version += 1
    audit(db, user, 'memory_rebuild_start', operation.id)
    db.flush()
    return rebuild_view(db, operation, row)


@router.post('/api/memory-rebuilds/{identity}/cancel', response_model=MemorySettingsView)
def rebuild_cancel(identity: str, body: RebuildRetry, ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id, lock=True)
    if not row or row.version != body.version or row.rebuild_id != identity or row.phase not in ('draining', 'blocked'):
        raise HTTPException(409, '只可取消尚未切换索引代际的操作，请刷新状态')
    op = db.get(Operation, identity)
    if op.user_id != user.id or op.workspace_id != user.workspace_id:
        raise HTTPException(404, '重建记录不存在')
    row.phase, row.pending = 'ready', {}
    row.version += 1
    op.status, op.error = 'cancelled', '用户取消切换；保留原模型与索引代际'
    audit(db, user, 'memory_rebuild_cancel', identity)
    return view(db, user.workspace_id)


@router.post('/api/memory-rebuilds/{identity}/retry', response_model=MemorySettingsView)
def rebuild_retry(identity: str, body: RebuildRetry, ctx: Context):
    db, user, _ = ctx
    row = configuration(db, user.workspace_id, lock=True)
    if not row or row.rebuild_id != identity or row.version != body.version:
        raise HTTPException(409, '重建状态变化，请刷新')
    op = db.get(Operation, identity)
    if op.user_id != user.id or op.workspace_id != user.workspace_id:
        raise HTTPException(404, '重建记录不存在')
    if row.phase == 'blocked':
        if not body.acknowledge_service_restarted:
            raise HTTPException(409, '旧记忆调用结果未知，须先由管理员重启 Hindsight，再明确确认后重试')
        for item in db.scalars(select(MemoryFlight).where(MemoryFlight.workspace_id == user.workspace_id,
                MemoryFlight.status.in_(('running', 'unknown')))):
            item.status = 'resolved'
        row.phase = 'draining'
    elif row.phase == 'rebuilding':
        from .memory_index import retry, IndexRetry
        for memory in db.scalars(select(Memory).where(Memory.workspace_id == user.workspace_id,
                Memory.status == 'active', Memory.index_status == 'failed').with_for_update()):
            retry(db, memory, IndexRetry(version=memory.version, epoch=row.epoch))
    else:
        raise HTTPException(409, '当前重建无需重试')
    op.status, op.error = 'pending', None
    audit(db, user, 'memory_rebuild_retry', identity)
    return view(db, user.workspace_id)


def tick():
    """Small durable scheduler step; can be retried after a worker restart."""
    with SessionLocal.begin() as db:
        for row in db.scalars(select(MemoryConfiguration).where(MemoryConfiguration.phase.in_(('draining', 'rebuilding'))).with_for_update(skip_locked=True)):
            op = db.get(Operation, row.rebuild_id)
            if not op:
                continue
            if row.phase == 'draining':
                if db.bind.dialect.name == 'postgresql' and not db.scalar(text('SELECT pg_try_advisory_xact_lock(1861024)')):
                    continue
                uncertain = db.scalar(select(func.count()).select_from(MemoryFlight).where(
                    MemoryFlight.workspace_id == row.workspace_id, MemoryFlight.status.in_(('running', 'unknown'))))
                if uncertain:
                    row.phase, op.status, op.error = 'blocked', 'failed', '旧记忆调用结果未知；请先重启 Hindsight，再确认恢复'
                    continue
                # The reviewed recipient/connection can change while an old
                # request drains. Never apply a now different target silently.
                try:
                    target = proposed(db, SimpleNamespace(workspace_id=row.workspace_id),
                        MemorySettingsInput(version=row.version, mode=row.pending['mode'], models=row.pending['models']))
                    if hash_value(target) != hash_value(row.pending):
                        raise ValueError('Target changed')
                except (HTTPException, ValueError):
                    row.phase, op.status, op.error = 'blocked', 'failed', '目标配置已变化；请取消本次重建并重新预览确认'
                    continue
                apply_configuration(db, row, row.pending)
                db.flush()
                row.embedding_fingerprint = selection(db, 'embedding', row.workspace_id)['fingerprint']
                row.epoch += 1
                row.pending, row.phase, op.status = {}, 'rebuilding', 'running'
                from .memory import enqueue, usable
                for memory in db.scalars(select(Memory).where(Memory.workspace_id == row.workspace_id, Memory.status == 'active').with_for_update()):
                    if usable(memory):
                        memory.index_status = 'pending'
                        enqueue(db, f'reindex:{row.epoch}:{memory.id}:{memory.version}', 'index',
                            {'memory_id': memory.id, 'version': memory.version, 'epoch': row.epoch})
                op.result = op.result | {'epoch': row.epoch}
            db.flush()
            progress = rebuild_view(db, op, row)
            if progress['failed']:
                op.status, op.error = 'failed', '部分索引处理失败，可核对服务后重试失败项'
            elif not progress['pending']:
                row.phase, op.status, op.error = 'ready', 'completed', None
                op.result = op.result | {k: progress[k] for k in ('total', 'ready', 'failed')}


@router.post('/api/memory-settings/prepare', response_model=dict[str, bool])
def prepare_builtin(ctx: Context):
    db, user, _ = ctx
    if mode(db, user.workspace_id) != 'simple':
        raise HTTPException(409, '高级模式无需准备内置模型')
    builtin_request('prepare', {})
    audit(db, user, 'memory_builtin_prepare')
    return {'accepted': True}
