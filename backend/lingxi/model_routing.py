"""Opt-in, endpoint-pinned routing for final ordinary-chat answers only.

No generic gateway retry: task, agent, memory and voice call budgets stay pinned.
The approved recipient list is a data egress boundary, not a locality claim.
"""
import hashlib
import hmac
import json
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now
from .config import settings as runtime_settings
from .models import Model, Policy, Provider, User
from .web import Context, audit
from .model_selection import SelectionDecision, rank

router = APIRouter()


class ModelRouting(Base):
    __tablename__ = 'model_routing'
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class RoutingInput(BaseModel):
    model_config = {'extra': 'forbid', 'strict': True}
    version: int = Field(ge=0)
    mode: Literal['fixed', 'automatic'] = 'fixed'
    candidate_ids: list[str] = Field(default_factory=list, max_length=6)
    allow_fallback: bool = False
    cross_provider: bool = False
    minimum_context: int = Field(default=8192, ge=2048, le=2000000)
    selection_strategy: Literal['priority', 'estimated_cost', 'observed_latency'] = 'priority'
    reference_currency: Literal['CNY', 'USD'] = 'CNY'
    reference_input_tokens: int = Field(default=1000, ge=1, le=2000000)
    reference_output_tokens: int = Field(default=1000, ge=1, le=2000000)

    @model_validator(mode='after')
    def unique_candidates(self):
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError('候选模型不能重复')
        if self.allow_fallback and len(self.candidate_ids) < 2:
            raise ValueError('开启降级至少需要两个候选模型')
        return self


class RoutingConfirm(RoutingInput):
    preview_digest: str = Field(pattern='^[a-f0-9]{64}$')
    preview_expires_at: int


class Recipient(BaseModel):
    model_id: str
    model: str
    provider_id: str
    provider_name: str
    base_url: str
    provider_version: int
    protocol: str
    context_window: int


class RoutingView(RoutingInput):
    recipients: list[Recipient] = Field(default_factory=list)
    current: bool = True
    notice: str = ''


class RoutingPreview(BaseModel):
    preview_digest: str
    preview_expires_at: int
    recipients: list[Recipient]
    notice: str
    selection: SelectionDecision


NOTICE = ('仅用于普通聊天最终回复。候选服务可能接收当前问题、授权历史、记忆和附件；'
          '内网授权不代表本地模型或禁止云端外发。先过滤授权与能力，再按所选策略比较；证据不足保留候选顺序。'
          '最多两次最终回复调用；已有文字、取消或资料失效时不降级。'
          '人格指定模型、任务协助、子助手、记忆、语音继续固定配置。')


def row_for(db, user):
    row = db.get(ModelRouting, user.workspace_id, populate_existing=True)
    if row and row.user_id != user.id:
        raise HTTPException(403, '当前身份不能管理此工作区的路由')
    return row


def proxy_version(db):
    from .network import SystemProxy
    proxy = db.get(SystemProxy, 'system', populate_existing=True)
    return proxy.version if proxy else 0


def pin(db, model, provider):
    from .providers import protocol_for
    return {'model_id': model.id, 'model': model.name, 'provider_id': provider.id,
            'provider_name': provider.name, 'base_url': provider.base_url,
            'provider_version': provider.version, 'protocol': protocol_for(provider, db),
            'context_window': model.context_window}


def build(db, user, body):
    from .gateway import resolve_model
    row = row_for(db, user)
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '路由已变化，请刷新并重新预览')
    settings = body.model_dump(exclude={'preview_digest', 'preview_expires_at', 'version'})
    active = body.mode == 'automatic' or body.allow_fallback
    policy = db.get(Policy, 'chat', populate_existing=True)
    recipients = []
    if active:
        if not policy or not body.candidate_ids or body.candidate_ids[0] != policy.model_id:
            raise HTTPException(422, '候选列表第一项必须是当前固定对话模型')
        for identity in body.candidate_ids:
            model = db.get(Model, identity, populate_existing=True)
            provider = db.get(Provider, model.provider_id, populate_existing=True) if model else None
            if not model or not provider or model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
                raise HTTPException(404, '候选模型不存在')
            if model.purpose != 'chat':
                raise HTTPException(422, '仅支持已验证的对话模型')
            resolve_model(db, 'chat', identity)
            recipients.append(pin(db, model, provider))
        if not body.cross_provider and len({p['provider_id'] for p in recipients}) > 1:
            raise HTTPException(422, '未授权跨连接发送，请移除其他服务或明确允许')
    elif body.candidate_ids or body.cross_provider:
        raise HTTPException(422, '关闭路由时请清空候选和跨连接授权')
    return settings | {'recipients': recipients, 'policy_version': policy.version if active else 0,
                       'proxy_version': proxy_version(db)}


def digest(config, version, user, expires):
    data = json.dumps([user.id, user.workspace_id, version, config, expires],
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hmac.new(runtime_settings().encryption_key.encode(), data, hashlib.sha256).hexdigest()


def active(config):
    return config.get('mode') == 'automatic' or config.get('allow_fallback', False)


def assert_current(db, user, row):
    config = row.config
    if not active(config):
        return
    policy = db.get(Policy, 'chat', populate_existing=True)
    if not policy or policy.version != config['policy_version'] or policy.model_id != config['candidate_ids'][0] or proxy_version(db) != config['proxy_version']:
        raise HTTPException(409, '对话或代理配置已变化，请重新预览路由授权')
    for approved in config['recipients']:
        model = db.get(Model, approved['model_id'], populate_existing=True)
        provider = db.get(Provider, approved['provider_id'], populate_existing=True)
        # A temporarily disabled/unverified candidate is filtered, not substituted.
        # Its endpoint/name/version must still be the one explicitly approved.
        if (not model or not provider or model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id
                or model.provider_id != provider.id or pin(db, model, provider) != approved or model.purpose != 'chat'):
            raise HTTPException(409, '候选连接或模型已变化，请重新预览路由授权')


@router.get('/api/model-routing', response_model=RoutingView)
def get_routing(ctx: Context):
    db, user, _ = ctx
    row = row_for(db, user)
    if not row:
        return {'version': 0, 'notice': NOTICE}
    current, notice = True, NOTICE
    try:
        assert_current(db, user, row)
    except HTTPException as exc:
        current, notice = False, str(exc.detail)
    return {k: v for k, v in row.config.items() if k not in ('policy_version', 'proxy_version')} | {
        'version': row.version, 'current': current, 'notice': notice}


@router.post('/api/model-routing/preview', response_model=RoutingPreview)
def preview(ctx: Context, body: RoutingInput):
    db, user, _ = ctx
    config = build(db, user, body)
    expires = int(now()) + 300
    allowed = [(p, eligible(db, user, p, {'context': body.minimum_context, 'vision': False}))
               for p in config['recipients']]
    if body.mode == 'fixed':
        allowed = [(p, pair) for p, pair in allowed if p['model_id'] == body.candidate_ids[0]]
    _, selection = rank(db, user, config, [(p, pair) for p, pair in allowed if pair])
    return {'preview_digest': digest(config, body.version, user, expires), 'preview_expires_at': expires,
            'recipients': config['recipients'], 'notice': NOTICE, 'selection': selection}


@router.put('/api/model-routing', response_model=RoutingView)
def save(ctx: Context, body: RoutingConfirm):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    config = build(db, user, body)
    if (not now() < body.preview_expires_at <= now() + 301 or
            not hmac.compare_digest(digest(config, body.version, user, body.preview_expires_at), body.preview_digest)):
        raise HTTPException(409, '预览已变化，请重新核对接收地址后确认')
    row = row_for(db, user)
    if not row:
        row = ModelRouting(workspace_id=user.workspace_id, user_id=user.id, version=1, config=config)
        db.add(row)
    else:
        row.config, row.version = config, row.version + 1
    db.flush()
    audit(db, user, 'model_routing_update', user.workspace_id)
    return get_routing(ctx)


def requirements(db, conversation, content, media_ids):
    """Check potential history images without reading attachment bytes or model calls."""
    from .branches import history
    from .media import Media, MediaLink
    messages = history(db, conversation, limit=1000)
    linked = select(MediaLink.media_id).where(MediaLink.message_id.in_([m.id for m in messages]))
    images = db.scalar(select(Media.id).where(Media.status == 'ready',
        Media.workspace_id == conversation.workspace_id, (Media.id.in_(media_ids)) | Media.id.in_(linked),
        Media.suffix.in_(['.png', '.jpg', '.jpeg', '.webp', '.gif'])).limit(1))
    # This is a conservative UTF-8 heuristic, not a provider tokenizer guarantee.
    return {'vision': bool(images), 'context': max(2048, len(content.encode('utf-8')) * 2 + 3000)}


def eligible(db, user, approved, requirement):
    from .gateway import resolve_model
    from .media import verify_vision
    try:
        model, provider, _ = resolve_model(db, 'chat', approved['model_id'])
    except HTTPException:
        return None
    if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id or pin(db, model, provider) != approved:
        return None
    if model.context_window < requirement['context'] or (requirement['vision'] and not verify_vision(db, model, provider)):
        return None
    return model, provider


def select_chat(db, user, conversation, content, media_ids, explicit_id=None, *, assisted=False):
    from .gateway import resolve_model
    # Explicit roles and bounded task/agent flows retain their exact call budget.
    if explicit_id or assisted:
        return (*resolve_model(db, 'chat', explicit_id), None)
    row = row_for(db, user)
    if not row or not active(row.config):
        return (*resolve_model(db, 'chat'), None)
    assert_current(db, user, row)
    requirement = requirements(db, conversation, content, media_ids)
    requirement['context'] = max(requirement['context'], row.config['minimum_context'])
    matches = [(p, eligible(db, user, p, requirement)) for p in row.config['recipients']]
    allowed = [(p, pair) for p, pair in matches if pair]
    if row.config['mode'] == 'fixed':
        allowed = [(p, pair) for p, pair in allowed if p['model_id'] == row.config['candidate_ids'][0]]
    if not allowed:
        raise HTTPException(409, '授权候选中没有满足上下文、视觉和可用状态的对话模型')
    allowed, selection = rank(db, user, row.config, allowed)
    chosen, (model, provider) = allowed[0]
    # Same context payload on fallback; a smaller model cannot silently lose data.
    fallback_allowed = [(p, pair) for p, pair in matches if pair and p['model_id'] != model.id
                        and p['context_window'] >= model.context_window]
    fallback_allowed, fallback_selection = rank(db, user, row.config, fallback_allowed) if row.config['allow_fallback'] else ([], None)
    fallback = [p for p, _ in fallback_allowed[:1]]
    plan = {'version': row.version, 'user_id': user.id, 'mode': row.config['mode'],
            'requirement': requirement, 'candidates': [chosen] + fallback, 'attempts': [],
            'selection': selection, 'fallback_selection': fallback_selection}
    return model, provider, row.config['policy_version'], plan


def validate_plan(db, run):
    plan = run.config.get('routing')
    if not plan:
        return
    user = db.get(User, plan['user_id'])
    owner = run.config.get('channel', {}).get('owner_id', run.user_id)
    if not user or user.workspace_id != run.workspace_id or user.id != owner or plan.get('subject_user_id') != run.user_id:
        raise HTTPException(409, '路由身份已失效')
    row = row_for(db, user)
    if not row or row.version != plan['version']:
        raise HTTPException(409, '路由授权已变化，本轮已停止')
    assert_current(db, user, row)
    approved = next((p for p in plan['candidates'] if p['model_id'] == run.model_id), None)
    if not approved or not eligible(db, user, approved, plan['requirement']):
        raise HTTPException(409, '本轮模型能力或授权已失效')


def retryable(exc):
    # No retries on invalid output, auth/input errors or application cancellation.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 502, 503, 504)
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError))


def answer_stream(job, run_id, model, provider, messages, check, factory):
    """At most one pre-text fallback. Never reenter dispatch or compact history."""
    from .models import ChatRun
    from .worker import add_event, valid_run
    from .security import safe_error
    emitted = False
    for index in range(2):
        with SessionLocal.begin() as db:
            run = valid_run(db, job, run_id)
            plan = run.config.get('routing')
            if not plan:
                config = run.config
            else:
                if len(plan['attempts']) != index:
                    raise HTTPException(409, '模型调用已经登记，不能重复执行')
                if index >= len(plan['candidates']):
                    raise HTTPException(409, '本轮没有获准的备用模型')
                target = plan['candidates'][index]
                user = db.get(User, plan['user_id'])
                pair = eligible(db, user, target, plan['requirement'])
                if not pair:
                    raise HTTPException(409, '备用模型已不可用，未重新发送资料')
                model, provider = pair
                attempts = plan['attempts'] + [{'model_id': model.id, 'provider_version': provider.version,
                    'state': 'calling', 'index': index + 1}]
                run.model_id = model.id
                run.config = run.config | {'model': model.name, 'provider_version': provider.version,
                    'routing': plan | {'attempts': attempts}}
                add_event(db, run, 'model_route', {'model_id': model.id, 'attempt': index + 1,
                    'routing_version': plan['version'], 'reason': 'fallback' if index else plan['mode']})
                config = run.config
        iterator = factory(model, provider, messages, run_id, check, config=config)
        try:
            for text in iterator:
                emitted = True
                yield text
            if plan:
                mark_attempt(job, run_id, 'completed')
            return
        except Exception as exc:
            if plan:
                mark_attempt(job, run_id, 'failed', safe_error(exc))
            if emitted or not plan or index or len(plan['candidates']) < 2 or not retryable(exc):
                raise
            # Fresh source/lease/authorization validation before changing recipients.
            check()
        finally:
            close = getattr(iterator, 'close', None)
            if close:
                close()


def mark_attempt(job, run_id, state, error=None):
    from .worker import valid_run
    with SessionLocal.begin() as db:
        run = valid_run(db, job, run_id)
        plan = run.config['routing']
        attempts = list(plan['attempts'])
        attempts[-1] = attempts[-1] | {'state': state, **({'error': error} if error else {})}
        run.config = run.config | {'routing': plan | {'attempts': attempts}}
