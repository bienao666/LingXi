import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import time
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from .config import settings
from .db import SessionLocal, now, uid
from .gateway import resolve_model, test_model
from .gateway import call_config, client_for, record_call, compatible_json
from .providers import ProviderInput, ProviderView, ProviderProtocol, protocol_for, bind_protocol, require_capability
from .catalog import ProviderModelCatalog, discover_models
from .network import (SystemProxy, ProxyInput, ProxyView, ProxyTestInput, ProxyTestResult,
                      config_view, save_proxy, outbound_client, proxy_route)
from .memory import HindsightAdapter, MemoryService, delete_conversation, enqueue, replay_ledger, usable
from .models import Audit, Candidate, ChatRun, Conversation, Job, LoginSession, Memory, Message, Model, ModelCall, Operation, Policy, Provider, RunEvent, User
from .schemas import (CandidateAction, CandidateView, ChatInput, ChatRunView, ConversationInput,
                      ConversationView, ExportInput, MemoryInput, MemoryView, MessageView,
                      ModelInput, ModelView, OperationView, PolicyInput, PreferenceInput, SessionView)
from .security import authenticate, encrypt, hasher, safe_error, token_hash, validate_endpoint
from .media import (MediaChatInput, MediaMessageView, attach_message, media_for_message,
                    validate_attachments)
from .auth_totp import LoginInput, lock_owner, verify_login_factor
from .memory_review import CandidateReviewInput, CandidateReviewPreview, DialogueCandidateSource


@asynccontextmanager
async def lifespan(app):
    if not settings().encryption_key:
        raise RuntimeError('LINGXI_ENCRYPTION_KEY is required; run python -m lingxi.cli configure')
    with SessionLocal.begin() as db:
        replay_ledger(db)
        from .assistants import seed_defaults
        seed_defaults(db)
        from .plugins import seed_bundled
        seed_bundled(db)
        from .media_policy import seed_defaults as seed_media_policy
        seed_media_policy(db)
        from .resources import expire_all
        expire_all(db)
    yield


app = FastAPI(title='灵犀 API', version='0.2.0', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    from .workspace_migrations import paused as migration_paused
    with SessionLocal() as db:
        migration_maintenance = migration_paused(db)
    if migration_maintenance:
        auth = (request.method, request.url.path) in (
            ('POST', '/api/auth/login'), ('POST', '/api/auth/logout'), ('GET', '/api/auth/me'),
            ('GET', '/api/auth/sessions'))
        status_read = request.method in ('GET', 'HEAD') and request.url.path in (
            '/api/workspace-migrations', '/api/admin/health', '/api/admin/backups')
        health = request.method in ('GET', 'HEAD') and request.url.path in ('/health/live', '/health/ready')
        if not (auth or status_read or health):
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':'服务器迁移维护中，仅开放身份与迁移状态；请在系统迁移页核对'},
                status_code=503, headers={'Retry-After':'30', 'Cache-Control':'no-store'})
    if (request.method not in ('GET','HEAD','OPTIONS') or request.url.path == '/api/plugin-oauth/callback') and not request.url.path.startswith('/api/admin/backups'):
        from .backups import paused
        with SessionLocal() as db:
            if not migration_maintenance and paused(db):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail':'备份维护中，暂不接受新写入；请稍后重试'},status_code=503,headers={'Retry-After':'30','Cache-Control':'no-store'})
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    return response


from .web import Context, database, owned, view, audit


def public_provider(p, db=None):
    return view(p, ('encrypted_key',)) | {'key_mask': '••••••••' if p.encrypted_key else '', 'protocol': protocol_for(p, db)}


@app.get('/health/live')
def live():
    return {'status': 'alive'}


@app.get('/health/ready')
def ready(db=Depends(database)):
    db.execute(text('SELECT 1'))
    return {'status': 'ready'}


@app.post('/api/auth/login', response_model=SessionView)
def login(body: LoginInput, request: Request, response: Response, db=Depends(database)):
    if request.headers.get('origin') and request.headers['origin'] != settings().public_origin:
        raise HTTPException(403, '请求来源不被允许')
    # Shared durable throttle. Requests are audited before verifying a password.
    target = hashlib.sha256((request.client.host if request.client else 'unknown').encode()).hexdigest()
    attempts = db.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'login_attempt',
                         Audit.target_id == target, Audit.created_at > now()-60))
    if attempts >= 5:
        raise HTTPException(429, '登录尝试过多，请一分钟后重试')
    db.add(Audit(user_id='anonymous', action='login_attempt', target_id=target))
    db.commit()
    user = db.scalar(select(User).where(User.username == body.username))
    try:
        if not user:
            # Constant-cost hash verification even for an unknown account.
            hasher.verify(hasher.hash('not-the-password'), body.password)
            raise VerifyMismatchError()
        user = lock_owner(db, user)
        hasher.verify(user.password_hash, body.password)
    except (VerifyMismatchError, InvalidHashError):
        if user:
            audit(db,user,'login_failed')
            db.commit()
        raise HTTPException(401, '用户名或密码不正确') from None
    verify_login_factor(db, user, body.code)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    session = LoginSession(user_id=user.id, token_hash=token_hash(token), csrf=csrf, expires_at=now()+86400)
    db.add(session)
    db.flush()
    audit(db,user,'login_success',session.id)
    response.set_cookie('lingxi_session', token, httponly=True, secure=settings().cookie_secure,
                        samesite='strict', max_age=86400, path='/')
    return {'id': user.id, 'username': user.username, 'csrf': csrf, 'auto_memory': user.auto_memory}


@app.get('/api/auth/me', response_model=SessionView)
def me(ctx: Context):
    _, user, session = ctx
    return {'id': user.id, 'username': user.username, 'csrf': session.csrf, 'auto_memory': user.auto_memory}


@app.post('/api/auth/logout')
def logout(response: Response, ctx: Context):
    db, user, session = ctx
    lock_owner(db, user, session)
    audit(db,user,'logout',session.id)
    db.delete(session)
    response.delete_cookie('lingxi_session', path='/')
    return {'status': 'logged_out'}


@app.get('/api/auth/sessions')
def sessions(ctx: Context):
    db, user, current = ctx
    return [{'id': s.id, 'created_at': s.created_at, 'expires_at': s.expires_at, 'current': s.id == current.id}
            for s in db.scalars(select(LoginSession).where(LoginSession.user_id == user.id, LoginSession.expires_at > now()))]


@app.delete('/api/auth/sessions/{session_id}')
def revoke_session(session_id: str, ctx: Context):
    db, user, current = ctx
    lock_owner(db, user, current)
    db.delete(owned(db, LoginSession, session_id, user))
    audit(db, user, 'session_revoke', session_id)
    return {'status': 'revoked'}


@app.patch('/api/auth/preferences', response_model=SessionView)
def preferences(body: PreferenceInput, ctx: Context):
    db, user, session = ctx
    user.auto_memory = body.auto_memory
    audit(db, user, 'auto_memory_change')
    return {'id': user.id, 'username': user.username, 'csrf': session.csrf, 'auto_memory': user.auto_memory}


@app.get('/api/providers', response_model=list[ProviderView])
def providers(ctx: Context):
    db, user, _ = ctx
    return [public_provider(p, db) for p in db.scalars(select(Provider).where(Provider.workspace_id == user.workspace_id))]


@app.post('/api/providers', response_model=ProviderView)
def create_provider(body: ProviderInput, ctx: Context):
    db, user, _ = ctx
    validate_endpoint(body.base_url, body.allow_private)
    if not body.api_key:
        raise HTTPException(422, '首次创建必须填写密钥')
    provider = Provider(name=body.name, base_url=body.base_url.rstrip('/'), encrypted_key=encrypt(body.api_key),
                        allow_private=body.allow_private, enabled=body.enabled)
    db.add(provider)
    db.flush()
    db.add(ProviderProtocol(provider_id=provider.id, protocol=body.protocol or 'openai'))
    db.flush()
    audit(db, user, 'provider_create', provider.id)
    return public_provider(provider, db)


@app.patch('/api/providers/{provider_id}', response_model=ProviderView)
def update_provider(provider_id: str, body: ProviderInput, ctx: Context):
    db, user, _ = ctx
    provider = owned(db, Provider, provider_id, user, lock=True)
    if body.version != provider.version:
        raise HTTPException(409, '连接配置已变化，请刷新')
    previous_protocol = protocol_for(provider, db)
    next_protocol = body.protocol or previous_protocol
    if next_protocol != previous_protocol and not body.api_key:
        raise HTTPException(422, '切换协议必须填写对应的新密钥，不会复用旧协议密钥')
    embedding_policy = db.get(Policy, 'embedding')
    embedding_model = db.get(Model, embedding_policy.model_id) if embedding_policy else None
    if ((body.base_url.rstrip('/') != provider.base_url or next_protocol != previous_protocol) and embedding_model and
            embedding_model.provider_id == provider.id and db.scalar(select(func.count()).select_from(Memory))):
        raise HTTPException(409, '该连接用于已有嵌入索引。请新建连接与模型后由部署端重建索引，不可直接修改服务地址')
    validate_endpoint(body.base_url, body.allow_private)
    db.merge(ProviderProtocol(provider_id=provider.id, protocol=next_protocol))
    provider.name, provider.base_url = body.name, body.base_url.rstrip('/')
    provider.enabled, provider.allow_private = body.enabled, body.allow_private
    if body.api_key:
        provider.encrypted_key = encrypt(body.api_key)
    provider.version += 1
    for model in db.scalars(select(Model).where(Model.provider_id == provider_id)):
        model.status = 'unverified'
    audit(db, user, 'provider_update', provider_id)
    db.flush()
    return public_provider(provider, db)


@app.get('/api/models', response_model=list[ModelView])
def models(ctx: Context):
    db, user, _ = ctx
    return [view(m) for m in db.scalars(select(Model).where(Model.workspace_id == user.workspace_id))]


@app.get('/api/providers/{provider_id}/models', response_model=ProviderModelCatalog, response_model_exclude_none=True)
def provider_models(provider_id: str, ctx: Context):
    db, user, _ = ctx
    provider = owned(db, Provider, provider_id, user)
    bind_protocol(provider, db)
    try:
        return discover_models(provider)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from None


@app.post('/api/models', response_model=ModelView)
def create_model(body: ModelInput, ctx: Context):
    db, user, _ = ctx
    provider = owned(db, Provider, body.provider_id, user)
    require_capability(protocol_for(provider, db), body.purpose)
    model = Model(**body.model_dump())
    db.add(model)
    db.flush()
    return view(model)


@app.post('/api/models/{model_id}/test', response_model=ModelView)
def validate_model(model_id: str, ctx: Context):
    db, user, _ = ctx
    model = owned(db, Model, model_id, user)
    try:
        test_model(db, model)
    except Exception as exc:
        from .model_budgets import BudgetExceeded
        if isinstance(exc, BudgetExceeded):
            raise
        model.status = 'failed'
        db.commit()
        raise HTTPException(502, safe_error(exc)) from None
    audit(db, user, 'model_test', model_id)
    return view(model)


@app.get('/api/model-policies')
def policies(ctx: Context):
    db, _, _ = ctx
    return [view(p) for p in db.scalars(select(Policy))]


@app.put('/api/model-policies/{purpose}')
def set_policy(purpose: str, body: PolicyInput, ctx: Context):
    db, user, _ = ctx
    model = owned(db, Model, body.model_id, user)
    if purpose != model.purpose or purpose not in ('chat', 'extraction', 'embedding', 'rerank'):
        raise HTTPException(422, '模型用途不匹配')
    resolve_model(db, purpose, model.id)
    policy = db.scalar(select(Policy).where(Policy.purpose == purpose).with_for_update())
    if body.version != (policy.version if policy else 0):
        raise HTTPException(409, '策略已变化，请刷新')
    if policy and policy.model_id == model.id:
        return view(policy)
    if purpose == 'embedding' and policy and db.scalar(select(func.count()).select_from(Memory)):
        raise HTTPException(409, '已有记忆索引。更换嵌入模型须停写后通过部署端 reindex 命令重建，不可直接混用')
    if policy:
        policy.model_id, policy.version = model.id, policy.version+1
    else:
        policy = Policy(purpose=purpose, model_id=model.id)
        db.add(policy)
    audit(db, user, 'policy_update', model.id)
    db.flush()
    return view(policy)


@app.post('/internal/model/v1/{route:path}', include_in_schema=False)
def internal_model(route: str, body: dict, request: Request, db=Depends(database)):
    """Private Hindsight transport. It receives a service token, never the user's provider key."""
    expected = settings().memory_gateway_token
    if not expected or not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer '+expected):
        raise HTTPException(401, '无效的内部服务身份')
    purpose = {'chat/completions':'extraction', 'embeddings':'embedding', 'rerank':'rerank'}.get(route)
    if not purpose or body.get('stream'):
        raise HTTPException(422, '不支持的内部模型请求')
    if len(json.dumps(body)) > 500000:
        raise HTTPException(413, '内部模型请求超过预算')
    model, provider, _ = resolve_model(db, purpose)
    config = call_config(model, provider, purpose)
    started = time.monotonic()
    try:
        from .model_budgets import client as budget_client
        with budget_client(provider, client_for, config) as client:
            data = compatible_json(client, provider, route, body | {'model':model.name})
        record_call(model.id, 'memory_'+purpose, started, 'completed', data.get('usage'), config=config)
        return data
    except Exception as exc:
        record_call(model.id, 'memory_'+purpose, started, 'failed', error=safe_error(exc), config=config)
        from .model_budgets import BudgetExceeded
        if isinstance(exc, BudgetExceeded):
            raise
        raise HTTPException(502, safe_error(exc)) from None


@app.get('/api/conversations', response_model=list[ConversationView])
def conversations(ctx: Context, q: str = Query('', max_length=200)):
    db, user, _ = ctx
    from .conversation_pages import conversation_query
    query = conversation_query(db, user, q)
    return [view(c) for c in db.scalars(query.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(200))]


@app.post('/api/conversations', response_model=ConversationView)
def create_conversation(body: ConversationInput, ctx: Context):
    db, user, _ = ctx
    conversation = Conversation(user_id=user.id, workspace_id=user.workspace_id, title=body.title)
    db.add(conversation)
    db.flush()
    return view(conversation)


@app.patch('/api/conversations/{conversation_id}', response_model=ConversationView)
def rename_conversation(conversation_id: str, body: ConversationInput, ctx: Context):
    db, user, _ = ctx
    c = owned(db, Conversation, conversation_id, user, lock=True)
    if c.version != body.version:
        raise HTTPException(409, '对话已变化，请刷新')
    c.title, c.version = body.title, c.version+1
    return view(c)


@app.get('/api/conversations/{conversation_id}/memories', response_model=list[MemoryView])
def linked_memories(conversation_id: str, ctx: Context):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user)
    sources = set(db.scalars(select(Message.id).where(Message.conversation_id == conversation_id)))
    return [view(m) for m in db.scalars(select(Memory).where(Memory.user_id == user.id, Memory.status != 'deleted'))
            if sources.intersection(m.source_ids)]


@app.delete('/api/conversations/{conversation_id}')
def remove_conversation(conversation_id: str, ctx: Context, delete_memories: bool = False):
    db, user, _ = ctx
    conversation = owned(db, Conversation, conversation_id, user, lock=True)
    delete_conversation(db, conversation, delete_memories)
    audit(db, user, 'conversation_delete', conversation_id)
    return {'status': 'deleted'}


@app.get('/api/messages', response_model=list[MediaMessageView])
def messages(conversation_id: str, ctx: Context, before: float | None = None, limit: int = Query(100, ge=1, le=1000)):
    db, user, _ = ctx
    conversation = owned(db, Conversation, conversation_id, user)
    from .delegation import expire_delegations
    expire_delegations(db, user.id)
    from .branches import history
    items = history(db, conversation, include_unusable=True, limit=limit, before=before)
    from .message_replies import presentation
    return [view(m) | {'attachments': media_for_message(db, m), 'reply_to': presentation(db, m),
        'inherited': m.conversation_id != conversation_id} for m in items]


def downloadable(data, filename, media_type):
    return Response(data, media_type=media_type, headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/conversations/{conversation_id}/export')
def export_conversation(conversation_id: str, ctx: Context, format: str = 'markdown'):
    db, user, _ = ctx
    c = owned(db, Conversation, conversation_id, user)
    from .delegation import expire_delegations
    expire_delegations(db, user.id)
    from .branches import history
    from .message_replies import presentation, markdown as reply_markdown
    items = [view(m, ('workspace_id',)) | {'attachments': media_for_message(db, m), 'reply_to': presentation(db, m)} for m in history(db, c, include_unusable=True)]
    if format == 'json':
        return downloadable(json.dumps({'title': c.title, 'messages': items}, ensure_ascii=False, indent=2), 'conversation.json', 'application/json')
    return downloadable('# '+c.title+'\n\n'+'\n\n'.join(f"## {m['role']} · {m['status']}\n\n{m['content']}" + reply_markdown(m['reply_to']) + ''.join(f"\n\n附件 [{a['id']}]：{a['filename']}（{a['status']}）" for a in m['attachments']) for m in items), 'conversation.md', 'text/markdown')


@app.post('/api/chat', response_model=ChatRunView)
def chat(body: MediaChatInput, ctx: Context):
    return submit_chat(body, ctx)


def submit_chat(body: ChatInput, ctx, channel_context=None, fork_context=False, voice_context=None):
    db, user, _ = ctx
    # Same owner lock as contact delivery; contact controls cannot race a new delivery.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    media_ids = getattr(body, 'media_ids', [])
    task_assist = getattr(body, 'task_assist', False)
    collaboration = getattr(body, 'collaboration', None)
    reply_id = getattr(body, 'reply_to_message_id', None)
    if reply_id and (task_assist or collaboration or channel_context or voice_context):
        raise HTTPException(422, '回复引用用于 Web 普通聊天，请先关闭任务协助、助手协作或语音模式')
    automatic_voice = bool(voice_context and voice_context.get('automatic'))
    if collaboration and task_assist:
        raise HTTPException(422, '任务协助与助手协作不能同时开启')
    if collaboration and len(body.content) > 6000:
        raise HTTPException(422, '协作目标最多 6000 字符，请缩小目标')
    if (task_assist and media_ids) or ((task_assist or collaboration) and channel_context):
        raise HTTPException(422, '任务协助目前仅支持 Web 文字消息，请移除附件或关闭任务协助')
    # Preserve old request hashes for text-only requests.
    hash_content = [body.conversation_id, body.content] + ([media_ids] if media_ids else [])
    if reply_id:
        hash_content += [{'reply_to_message_id':reply_id}]
    if task_assist:
        hash_content += [{'task_assist': True}]
    if automatic_voice:
        hash_content += [{'voice_input': voice_context}]
    if collaboration:
        hash_content += [{'collaboration': collaboration.model_dump(exclude=({'propose_memory'} if not collaboration.propose_memory else set()) | ({'tool_keys'} if not collaboration.tool_keys else set()))}]
    digest = hashlib.sha256(json.dumps(hash_content, ensure_ascii=False).encode()).hexdigest()
    existing = db.scalar(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.request_id == body.request_id))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, '幂等请求 ID 已用于另一条消息')
        return view(existing)
    conversation = owned(db, Conversation, body.conversation_id, user, lock=True)
    from .message_replies import snapshot as reply_snapshot
    if reply_id:
        from .channels import ChannelSession
        from .voice import VoiceSession
        if (db.scalar(select(ChannelSession).where(ChannelSession.conversation_id == conversation.id)) or
                db.scalar(select(VoiceSession).where(VoiceSession.conversation_id == conversation.id,
                    VoiceSession.status == 'active', VoiceSession.expires_at > now()))):
            raise HTTPException(409, '请在独立 Web 文字会话中引用，或先结束实时语音')
    reply = reply_snapshot(db, conversation, reply_id)
    if task_assist or collaboration:
        from .channels import ChannelSession
        if db.scalar(select(ChannelSession).where(ChannelSession.conversation_id == conversation.id)):
            raise HTTPException(409, 'QQ 会话尚未开放聊天任务协助，请使用独立 Web 对话')
    from .proactive import chat_control
    control_reply = chat_control(db, user, conversation, body.content) if not channel_context and not media_ids and not fork_context and not automatic_voice and not reply else None
    from .support import chat_control as support_control
    support_result = support_control(db, user, conversation, body.content) if not channel_context and not media_ids and not fork_context and not voice_context and not reply else None
    if support_result:
        control_reply = support_result['reply']
    if control_reply:
        run = ChatRun(user_id=user.id, workspace_id=user.workspace_id, conversation_id=conversation.id,
            request_id=body.request_id, request_hash=digest, model_id='support-control' if support_result else 'contact-control',
            status='completed', memory_status='not_used', config={'model': '支持偏好控制，不调用模型' if support_result else '联系偏好控制，不调用模型',
                **({'support_control': {k: v for k, v in support_result.items() if k != 'reply'}} if support_result else {})}, next_seq=1)
        db.add(run)
        db.flush()
        db.add_all([Message(workspace_id=user.workspace_id, conversation_id=conversation.id, role='user', content=body.content, run_id=run.id),
            Message(workspace_id=user.workspace_id, conversation_id=conversation.id, role='assistant', content=control_reply, run_id=run.id),
            RunEvent(run_id=run.id, seq=1, kind='done', data={'status':'completed'})])
        return view(run)
    if db.scalar(select(ChatRun).where(ChatRun.conversation_id == conversation.id, ChatRun.status.in_(['queued', 'running']))):
        raise HTTPException(409, '当前会话仍在生成，请先等待或停止')
    from .assistants import chat_profile
    persona = chat_profile(db, conversation, user)
    from .model_routing import select_chat
    routing_user = db.get(User, channel_context.get('owner_id', user.id)) if channel_context else user
    model, provider, version, routing = select_chat(db, routing_user, conversation, body.content, media_ids,
        persona['spec'].get('model_id'), assisted=bool(task_assist or collaboration or voice_context))
    if routing:
        routing = routing | {'subject_user_id': user.id}
    if collaboration:
        from .agent_media import select_media
        attachments = select_media(db, media_ids, user)
    else:
        attachments = validate_attachments(db, media_ids, user, model, provider)
    from .chat_actions import snapshot as action_snapshot
    from .system_settings import run_snapshot
    run = ChatRun(user_id=user.id, workspace_id=user.workspace_id, conversation_id=conversation.id, request_id=body.request_id,
                  request_hash=digest, model_id=model.id, config={'model': model.name, 'provider_version': provider.version,
                  'policy_version': version, 'prompt_version': 'lingxi-expression-v2', 'persona': persona,
                  **run_snapshot(db, user),
                  **({'reply_to':reply} if reply else {}),
                  **({'routing': routing} if routing else {}),
                  **({'channel': channel_context} if channel_context else {}),
                  **({'voice_input': voice_context} if voice_context else {}),
                  **({'task_assist': action_snapshot(db, user)} if task_assist else {}),
                  **({'task_assist': {'mode': 'collaboration', 'options': collaboration.model_dump(),
                                     'status': 'pending'}} if collaboration else {}),
                  **({'media_refs': [{'id': a.id, 'version': a.version} for a in attachments]} if attachments else {})})
    db.add(run)
    if reply:
        run.source_message_ids = [reply['message_id']]
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.request_id == body.request_id))
        if existing and existing.request_hash == digest:
            return view(existing)
        raise HTTPException(409, '重复或冲突的请求') from None
    message = Message(conversation_id=conversation.id, role='user', content=body.content, run_id=run.id)
    db.add(message)
    db.flush()
    attach_message(db, attachments, message)
    from .emotions import enqueue_observation
    enqueue_observation(db, run, message)
    from .branches import ConversationBranch
    branch = db.scalar(select(ConversationBranch).where(ConversationBranch.conversation_id == conversation.id))
    if branch:
        from .branches import history
        run.source_message_ids = [m.id for m in history(db, conversation) if m.status == 'completed']
    enqueue(db, 'chat:'+run.id, 'chat', {'run_id': run.id})
    if not automatic_voice and (not channel_context or (channel_context.get('memory_allowed') and not channel_context.get('attachment_only') and not channel_context.get('audio_unconfirmed'))):
        enqueue(db, 'extract:'+message.id, 'extract', {'message_id': message.id})
    return view(run)


@app.get('/api/chat-runs', response_model=list[ChatRunView])
def chat_runs(conversation_id: str, ctx: Context):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user)
    return [view(r) for r in db.scalars(select(ChatRun).where(ChatRun.conversation_id == conversation_id).order_by(ChatRun.created_at.desc()).limit(100))]


@app.get('/api/chat-runs/{run_id}', response_model=ChatRunView)
def chat_run(run_id: str, ctx: Context):
    db, user, _ = ctx
    return view(owned(db, ChatRun, run_id, user))


@app.post('/api/chat-runs/{run_id}/cancel', response_model=ChatRunView)
def cancel_run(run_id: str, ctx: Context):
    db, user, _ = ctx
    run = owned(db, ChatRun, run_id, user, lock=True)
    if run.status in ('queued', 'running'):
        from .conversation_context import invalidate as invalidate_context
        invalidate_context(db, run.id, 'cancelled')
        run.cancel_requested = True
        if run.config.get('task_assist'):
            run.config = run.config | {'task_assist': run.config['task_assist'] | {'status': 'cancelled'}}
        # Immediate cancellation is visible even while upstream is blocked waiting for its next byte.
        run.status = 'cancelled'
        for message in db.scalars(select(Message).where(Message.run_id == run.id, Message.role == 'assistant')):
            message.status = 'cancelled'
    return view(run)


@app.get('/api/chat-runs/{run_id}/events')
def run_events(run_id: str, request: Request, ctx: Context, after: int = Query(0, ge=0)):
    db, user, session = ctx
    owned(db, ChatRun, run_id, user)
    session_id, user_id = session.id, user.id
    try:
        after = max(after, int(request.headers.get('last-event-id', '0')))
    except ValueError:
        raise HTTPException(422, '无效的事件游标') from None

    async def events():
        cursor = after
        while not await request.is_disconnected():
            with SessionLocal() as conn:
                login_session = conn.get(LoginSession, session_id)
                if not login_session or login_session.expires_at <= now():
                    yield 'event: revoked\ndata: {}\n\n'
                    return
                run = conn.get(ChatRun, run_id)
                if not run or run.user_id != user_id:
                    return
                invalid = run.status == 'invalidated' or any(not usable(m := conn.get(Memory, ref['id'])) or m.version != ref['version'] for ref in run.memory_refs)
                if invalid:
                    yield 'event: invalidated\ndata: {"status":"invalidated"}\n\n'
                    return
                rows = conn.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > cursor).order_by(RunEvent.seq).limit(200)).all()
                for item in rows:
                    cursor = item.seq
                    yield f'id: {item.seq}\nevent: {item.kind}\ndata: {json.dumps(item.data, ensure_ascii=False)}\n\n'
                if run.status not in ('queued', 'running') and (not rows or cursor >= run.next_seq):
                    yield f'event: settled\ndata: {json.dumps({"status": run.status})}\n\n'
                    return
            yield ': heartbeat\n\n'
            await asyncio.sleep(.3)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})


@app.get('/api/memories', response_model=list[MemoryView])
def memories(ctx: Context, q: str = Query('', max_length=200), category: str = '', offset: int = Query(0, ge=0)):
    db, user, _ = ctx
    query = select(Memory).where(Memory.user_id == user.id, Memory.workspace_id == user.workspace_id, Memory.status != 'deleted')
    if q:
        query = query.where(Memory.content.contains(q, autoescape=True))
    if category:
        query = query.where(Memory.category == category)
    return [view(m) for m in db.scalars(query.order_by(Memory.created_at.desc(), Memory.id.desc()).offset(offset).limit(100))]


@app.post('/api/memories', response_model=MemoryView)
def create_memory(body: MemoryInput, ctx: Context):
    db, user, _ = ctx
    memory = MemoryService().remember(db, user.id, body.content, body.category, ['user-entry:'+uid()],
                                     **body.model_dump(exclude={'content', 'category', 'version'}))
    audit(db, user, 'memory_create', memory.id)
    return view(memory)


@app.get('/api/memories/{memory_id}', response_model=MemoryView)
def get_memory(memory_id: str, ctx: Context):
    db, user, _ = ctx
    memory = owned(db, Memory, memory_id, user)
    if memory.status == 'deleted':
        raise HTTPException(404, '记忆已删除')
    return view(memory)


@app.get('/api/memories/{memory_id}/sources')
def memory_sources(memory_id: str, ctx: Context):
    db, user, _ = ctx
    memory = owned(db, Memory, memory_id, user)
    if memory.status == 'deleted':
        raise HTTPException(404, '记忆已删除')
    sources = []
    if memory.provenance.get('kind')=='agent':
        sources.append({'id':'agent-node:'+memory.provenance['node_id'],'role':'user_confirmed',
            'content':'用户核对并采纳的子助手提议；原始分析仅校验结构与来源，不表示独立事实核验。',
            'origin':memory.provenance})
    for source_id in memory.source_ids:
        message = db.get(Message, source_id)
        c = db.get(Conversation, message.conversation_id) if message else None
        if message and c and c.user_id == user.id and not c.deleted:
            sources.append({'id': source_id, 'content': message.content, 'role': message.role, 'created_at': message.created_at})
        else:
            sources.append({'id': source_id, 'content': '用户手动记录／纠正，或原会话已删除', 'role': 'source'})
    return sources


@app.patch('/api/memories/{memory_id}', response_model=MemoryView)
def correct_memory(memory_id: str, body: MemoryInput, ctx: Context):
    db, user, _ = ctx
    memory = owned(db, Memory, memory_id, user, lock=True)
    if memory.status == 'deleted' or body.version != memory.version:
        raise HTTPException(409, '记忆已变化或删除，请刷新')
    # PATCH preserves omitted metadata, including legacy time strings. New or
    # explicitly changed values are validated, together with the retained bound.
    values = body.model_dump(exclude={'version'}, exclude_unset=True)
    if any(key in values for key in ('valid_from', 'valid_to')):
        from .memory_time import check_period
        try:
            check_period({key: values.get(key, getattr(memory, key)) for key in ('valid_from', 'valid_to')})
        except ValueError:
            raise HTTPException(422, '生效／结束时间无效或顺序冲突，请同时核对两个时间；留空可清除') from None
    MemoryService().correct(db, memory, values)
    audit(db, user, 'memory_correct', memory_id)
    return view(memory)


@app.delete('/api/memories/{memory_id}', response_model=OperationView)
def forget_memory(memory_id: str, ctx: Context):
    db, user, _ = ctx
    memory = owned(db, Memory, memory_id, user, lock=True)
    operation = MemoryService().forget(db, memory)
    audit(db, user, 'memory_delete', memory_id)
    return view(operation)


@app.post('/api/memories/{memory_id}/retry')
def retry_index(memory_id: str, ctx: Context):
    db, user, _ = ctx
    memory = owned(db, Memory, memory_id, user, lock=True)
    if not usable(memory):
        raise HTTPException(409, '记忆已不可用')
    job = db.scalar(select(Job).where(Job.key == f'index:{memory.id}:{memory.version}').with_for_update())
    if job and job.status == 'failed':
        job.status, job.attempts, job.available_at = 'pending', 0, now()
        memory.index_status = 'pending'
    return {'status': memory.index_status}


@app.get('/api/memory-candidates', response_model=list[CandidateView])
def candidates(ctx: Context):
    db, user, _ = ctx
    from .agent_memory import expire
    expire(db,user)
    return [view(c) for c in db.scalars(select(Candidate).where(Candidate.user_id == user.id,
        Candidate.workspace_id==user.workspace_id,Candidate.status != 'invalidated').order_by(Candidate.created_at.desc()).limit(200))]


@app.get('/api/memory-candidates/{candidate_id}/sources', response_model=DialogueCandidateSource | dict)
def candidate_sources(candidate_id: str, ctx: Context, q: str = Query('', max_length=200)):
    db,user,_=ctx
    candidate = owned(db,Candidate,candidate_id,user)
    if candidate.origin.get('kind') != 'agent':
        from .memory_review import sources
        return sources(db,candidate,user,q)
    from .agent_memory import sources_for
    return sources_for(db,candidate,user)


@app.post('/api/memory-candidates/{candidate_id}/review', response_model=CandidateReviewPreview)
def review_dialogue_candidate(candidate_id: str, body: CandidateReviewInput, ctx: Context):
    db,user,session=ctx
    from .memory_review import preview
    return preview(db,owned(db,Candidate,candidate_id,user),user,session,body)


@app.post('/api/memory-candidates/{candidate_id}/actions', response_model=CandidateView)
def candidate_action(candidate_id: str, body: CandidateAction, ctx: Context):
    db, user, session = ctx
    candidate = owned(db, Candidate, candidate_id, user)
    if candidate.origin.get('kind')=='agent':
        if candidate.status!='pending':
            return view(candidate)
        from .agent_memory import validate,preview
        validate(db,candidate,lock=True)
        candidate=owned(db,Candidate,candidate_id,user,lock=True)
        db.refresh(candidate)
        if candidate.status!='pending':
            return view(candidate)
        if body.action=='confirm':
            if candidate.subject=='unknown':
                raise HTTPException(409,'候选主体尚未确定，请拒绝此项并按正确主体手动记录')
            if body.review_digest!=preview(db,candidate)['review_digest']:
                raise HTTPException(409,'请先核对本项候选的内容、主体和来源，再确认保存')
            memory=MemoryService().remember(db,user.id,candidate.content,candidate.category,
                ['agent-node:'+candidate.source_id,*candidate.origin.get('source_message_ids',[]),'user-confirmed:'+candidate.id],
                workspace_id=candidate.workspace_id,subject=candidate.subject,evidence='user_confirmed',provenance=candidate.origin)
            candidate.memory_id,candidate.status=memory.id,'confirmed'
        else:
            candidate.status='rejected'
        audit(db,user,'candidate_'+body.action,candidate_id)
        return view(candidate)
    if body.action == 'confirm':
        from .memory_review import confirm
        result=confirm(db,candidate,user,session,body.review_token)
        if result.status == 'confirmed':
            audit(db,user,'candidate_confirm',candidate_id)
        return view(result)
    candidate = owned(db, Candidate, candidate_id, user, lock=True)
    source = db.get(Message, candidate.source_id)
    conversation = db.get(Conversation, source.conversation_id) if source else None
    if not conversation or conversation.deleted or not source or source.status != 'completed':
        raise HTTPException(409, '候选来源已失效')
    if candidate.status != 'pending':
        return view(candidate)
    candidate.status = 'rejected'
    audit(db, user, 'candidate_'+body.action, candidate_id)
    return view(candidate)


@app.get('/api/memory-operations', response_model=list[OperationView])
def operations(ctx: Context, kind: str = ''):
    db, user, _ = ctx
    query = select(Operation).where(Operation.user_id == user.id, Operation.workspace_id == user.workspace_id)
    if kind:
        query = query.where(Operation.kind == kind)
    return [view(o) for o in db.scalars(query.order_by(Operation.created_at.desc()).limit(200))]


@app.get('/api/memory-operations/{operation_id}', response_model=OperationView)
def operation(operation_id: str, ctx: Context):
    db, user, _ = ctx
    return view(owned(db, Operation, operation_id, user))


@app.post('/api/memory-operations/{operation_id}/retry')
def retry_operation(operation_id: str, ctx: Context):
    db, user, _ = ctx
    operation = owned(db, Operation, operation_id, user, lock=True)
    job = db.scalar(select(Job).where(Job.key == f'delete:{operation.id}').with_for_update())
    if job and job.status == 'failed':
        job.status, job.attempts, job.available_at = 'pending', 0, now()
        operation.status, operation.error = 'pending', None
    return {'status': operation.status}


@app.post('/api/memory-exports', response_model=OperationView)
def memory_export(body: ExportInput, ctx: Context):
    db, user, _ = ctx
    operation = Operation(user_id=user.id, kind='export')
    db.add(operation)
    db.flush()
    enqueue(db, 'export:'+operation.id, 'export', {'operation_id': operation.id, 'format': body.format})
    return view(operation)


@app.get('/api/memory-exports/{operation_id}/download')
def download_export(operation_id: str, ctx: Context):
    db, user, _ = ctx
    operation = owned(db, Operation, operation_id, user)
    if operation.kind != 'export' or operation.status != 'completed':
        raise HTTPException(409, '导出尚未就绪')
    rows = [view(m, ('workspace_id', 'user_id')) for m in db.scalars(select(Memory).where(Memory.user_id == user.id, Memory.workspace_id == user.workspace_id, Memory.status != 'deleted'))]
    if operation.result['format'] == 'json':
        return downloadable(json.dumps(rows, ensure_ascii=False, indent=2), 'lingxi-memories.json', 'application/json')
    result = '# 灵犀长期记忆\n\n'+'\n\n'.join(f"## {m['category']} · v{m['version']}\n\n{m['content']}\n\n主体：{m['subject']}\n事件时间：{m['event_at'] or '未说明'}\n生效时间：{m['valid_from'] or '未说明'}\n结束时间：{m['valid_to'] or '未说明'}\n来源：{', '.join(m['source_ids'])}\n状态：{m['status']} / {m['index_status']}" for m in rows)
    return downloadable(result, 'lingxi-memories.md', 'text/markdown')


from .model_calls import ModelCallEntry, entry as model_call_entry


@app.get('/api/admin/model-calls', response_model=list[ModelCallEntry])
def calls(ctx: Context):
    db, user, _ = ctx
    return [model_call_entry(c) for c in db.scalars(select(ModelCall).where(ModelCall.workspace_id == user.workspace_id).order_by(ModelCall.created_at.desc(), ModelCall.id.desc()).limit(200))]


@app.get('/api/admin/proxy', response_model=ProxyView)
def get_proxy(ctx: Context):
    db, _, _ = ctx
    return config_view(db.get(SystemProxy, 'system'))


@app.put('/api/admin/proxy', response_model=ProxyView)
def update_proxy(body: ProxyInput, ctx: Context):
    db, user, _ = ctx
    try:
        result = save_proxy(db, body)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '代理配置已变化，请重新加载后再保存') from None
    audit(db, user, 'system_proxy_update', 'system')
    return result


@app.post('/api/admin/proxy/test', response_model=ProxyTestResult)
def test_proxy(body: ProxyTestInput, ctx: Context):
    started = time.monotonic()
    # Testing a draft does not save or enable it. Test destinations remain public HTTPS.
    address = validate_endpoint(body.test_url, False)
    _, route = proxy_route(body.test_url, address, body)
    try:
        with outbound_client(body.test_url, timeout=15, config=body) as client:
            with client.stream('GET', body.test_url) as response:
                return ProxyTestResult(ok=response.is_success, status_code=response.status_code,
                                       duration_ms=int((time.monotonic()-started)*1000), route=route)
    except Exception as exc:
        raise HTTPException(502, safe_error(exc)) from None


@app.get('/api/admin/health')
def health(ctx: Context):
    db, user, _ = ctx
    workers = db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == user.workspace_id, Job.status == 'running', Job.lease_until > now()))
    pending = db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == user.workspace_id, Job.status == 'pending'))
    failed = db.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == user.workspace_id, Job.status == 'failed'))
    from .operations import workers as worker_status
    hindsight = 'ready' if HindsightAdapter().healthy() else 'unavailable'
    missing = []
    for purpose in ('chat', 'extraction', 'embedding', 'rerank'):
        try:
            resolve_model(db, purpose)
        except HTTPException:
            missing.append(purpose)
    return {'core': 'ready', 'database': 'ready', 'hindsight': hindsight,
            'memory': 'unconfigured' if any(p in missing for p in ('extraction','embedding','rerank')) else hindsight,
            'unconfigured_purposes': missing,
            'worker': worker_status(db) | {'active_jobs': workers, 'pending_jobs': pending, 'failed_jobs': failed},
            'release_status': '首批实现，真实模型和中文质量待验证'}


from .assistant_routes import router as assistant_router
app.include_router(assistant_router)
from .emotions import router as emotion_router
app.include_router(emotion_router)
from .task_routes import router as task_router
app.include_router(task_router)
from .proactive import router as proactive_router
app.include_router(proactive_router)
from .channels import router as channels_router
from .channel_gateway import router as channel_gateway_router
app.include_router(channels_router)
app.include_router(channel_gateway_router)
from .channel_tasks import router as channel_tasks_router
app.include_router(channel_tasks_router)
from .channel_artifacts import router as channel_artifacts_router
app.include_router(channel_artifacts_router)
from .media_storage import router as media_storage_router
app.include_router(media_storage_router)
from .readiness import router as readiness_router
app.include_router(readiness_router)
from .browser_notifications import router as browser_notifications_router
app.include_router(browser_notifications_router)

from .delegation import router as delegation_router
app.include_router(delegation_router)
from .media import router as media_router
app.include_router(media_router)
from .media_policy import router as media_policy_router
app.include_router(media_policy_router)
from .media_retention import router as media_retention_router
app.include_router(media_retention_router)
from .voice import router as voice_router
app.include_router(voice_router)
from .voice_native_output import router as voice_native_output_router
app.include_router(voice_native_output_router)
from .voice_realtime import router as voice_realtime_router
app.include_router(voice_realtime_router)
from .plugin_routes import router as plugin_router
app.include_router(plugin_router)
from .plugin_api import router as plugin_api_router
app.include_router(plugin_api_router)
from .plugin_store import router as plugin_store_router
app.include_router(plugin_store_router)
from .operations import router as operations_router
app.include_router(operations_router)
from .usage_costs import router as usage_costs_router
app.include_router(usage_costs_router)
from .auth_totp import router as security_router
app.include_router(security_router)
from .backups import router as backup_router
app.include_router(backup_router)
from .chat_actions import router as chat_actions_router
app.include_router(chat_actions_router)
from .branches import router as branches_router
app.include_router(branches_router)
from .conversation_context import router as context_router
app.include_router(context_router)

from .plugin_mcp import router as plugin_mcp_router
app.include_router(plugin_mcp_router)
from .plugin_events import router as plugin_events_router
app.include_router(plugin_events_router)
from .plugin_agents import router as plugin_agents_router
app.include_router(plugin_agents_router)
from .plugin_oauth import router as plugin_oauth_router
app.include_router(plugin_oauth_router)
from .agent_tools import router as agent_tools_router
app.include_router(agent_tools_router)
from .proactive_channels import router as proactive_channels_router
app.include_router(proactive_channels_router)
from .model_routing import router as model_routing_router
app.include_router(model_routing_router)

from .conversation_pages import router as conversation_pages_router
app.include_router(conversation_pages_router)

from .proactive_voice import router as proactive_voice_router
app.include_router(proactive_voice_router)

from .system_settings import router as system_settings_router
app.include_router(system_settings_router)

from .resources import router as resources_router
app.include_router(resources_router)

from .workspace_migrations import router as workspace_migration_router
app.include_router(workspace_migration_router)

from .model_calls import router as model_call_router
app.include_router(model_call_router)

from .memory_pages import router as memory_pages_router
app.include_router(memory_pages_router)
from .model_budgets import router as model_budgets_router
app.include_router(model_budgets_router)
from .audio_costs import router as audio_costs_router
app.include_router(audio_costs_router)
from .reminder_delivery import router as reminder_delivery_router
app.include_router(reminder_delivery_router)
from .configuration_bundles import router as configuration_bundle_router
app.include_router(configuration_bundle_router)
