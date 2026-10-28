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
from .gateway import call_config, client_for, record_call
from .memory import HindsightAdapter, MemoryService, delete_conversation, enqueue, replay_ledger, usable
from .models import Audit, Candidate, ChatRun, Conversation, Job, LoginSession, Memory, Message, Model, ModelCall, Operation, Policy, Provider, RunEvent, User
from .schemas import (CandidateAction, CandidateView, ChatInput, ChatRunView, ConversationInput,
                      ConversationView, ExportInput, LoginInput, MemoryInput, MemoryView, MessageView,
                      ModelInput, ModelView, OperationView, PolicyInput, PreferenceInput, ProviderInput,
                      ProviderView, SessionView)
from .security import authenticate, encrypt, hasher, safe_error, token_hash, validate_endpoint


@asynccontextmanager
async def lifespan(app):
    if not settings().encryption_key:
        raise RuntimeError('LINGXI_ENCRYPTION_KEY is required; run python -m lingxi.cli configure')
    with SessionLocal.begin() as db:
        replay_ledger(db)
    yield


app = FastAPI(title='灵犀 API', version='0.2.0', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


def database():
    with SessionLocal() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def context(request: Request, db=Depends(database, scope='function')):
    user, session = authenticate(request, db)
    return db, user, session


Context = Annotated[tuple, Depends(context, scope='function')]


def owned(db, entity, identity, user, lock=False):
    query = select(entity).where(entity.id == identity, entity.workspace_id == user.workspace_id)
    if hasattr(entity, 'user_id'):
        query = query.where(entity.user_id == user.id)
    item = db.scalar(query.with_for_update() if lock else query)
    if not item or (isinstance(item, Conversation) and item.deleted):
        raise HTTPException(404, '记录不存在')
    return item


def view(item, omit=()):
    return {c.name: getattr(item, c.name) for c in item.__table__.columns if c.name not in omit}


def audit(db, user, action, target=None):
    db.add(Audit(user_id=user.id, action=action, target_id=target))


def public_provider(p):
    return view(p, ('encrypted_key',)) | {'key_mask': '••••••••' if p.encrypted_key else ''}


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
        hasher.verify(user.password_hash, body.password)
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(401, '用户名或密码不正确') from None
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    session = LoginSession(user_id=user.id, token_hash=token_hash(token), csrf=csrf, expires_at=now()+86400)
    db.add(session)
    db.flush()
    response.set_cookie('lingxi_session', token, httponly=True, secure=settings().cookie_secure,
                        samesite='strict', max_age=86400, path='/')
    return {'id': user.id, 'username': user.username, 'csrf': csrf, 'auto_memory': user.auto_memory}


@app.get('/api/auth/me', response_model=SessionView)
def me(ctx: Context):
    _, user, session = ctx
    return {'id': user.id, 'username': user.username, 'csrf': session.csrf, 'auto_memory': user.auto_memory}


@app.post('/api/auth/logout')
def logout(response: Response, ctx: Context):
    db, _, session = ctx
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
    db, user, _ = ctx
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
    return [public_provider(p) for p in db.scalars(select(Provider).where(Provider.workspace_id == user.workspace_id))]


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
    audit(db, user, 'provider_create', provider.id)
    return public_provider(provider)


@app.patch('/api/providers/{provider_id}', response_model=ProviderView)
def update_provider(provider_id: str, body: ProviderInput, ctx: Context):
    db, user, _ = ctx
    provider = owned(db, Provider, provider_id, user, lock=True)
    if body.version != provider.version:
        raise HTTPException(409, '连接配置已变化，请刷新')
    embedding_policy = db.get(Policy, 'embedding')
    embedding_model = db.get(Model, embedding_policy.model_id) if embedding_policy else None
    if (body.base_url.rstrip('/') != provider.base_url and embedding_model and
            embedding_model.provider_id == provider.id and db.scalar(select(func.count()).select_from(Memory))):
        raise HTTPException(409, '该连接用于已有嵌入索引。请新建连接与模型后由部署端重建索引，不可直接修改服务地址')
    validate_endpoint(body.base_url, body.allow_private)
    provider.name, provider.base_url = body.name, body.base_url.rstrip('/')
    provider.enabled, provider.allow_private = body.enabled, body.allow_private
    if body.api_key:
        provider.encrypted_key = encrypt(body.api_key)
    provider.version += 1
    for model in db.scalars(select(Model).where(Model.provider_id == provider_id)):
        model.status = 'unverified'
    audit(db, user, 'provider_update', provider_id)
    return public_provider(provider)


@app.get('/api/models', response_model=list[ModelView])
def models(ctx: Context):
    db, user, _ = ctx
    return [view(m) for m in db.scalars(select(Model).where(Model.workspace_id == user.workspace_id))]


@app.post('/api/models', response_model=ModelView)
def create_model(body: ModelInput, ctx: Context):
    db, user, _ = ctx
    owned(db, Provider, body.provider_id, user)
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
        with client_for(provider) as client:
            result = client.post(route, json=body | {'model':model.name})
            result.raise_for_status()
            data = result.json()
        record_call(model.id, 'memory_'+purpose, started, 'completed', data.get('usage'), config=config)
        return data
    except Exception as exc:
        record_call(model.id, 'memory_'+purpose, started, 'failed', error=safe_error(exc), config=config)
        raise HTTPException(502, safe_error(exc)) from None


@app.get('/api/conversations', response_model=list[ConversationView])
def conversations(ctx: Context, q: str = Query('', max_length=200)):
    db, user, _ = ctx
    ids = select(Message.conversation_id).where(Message.content.contains(q, autoescape=True))
    query = select(Conversation).where(Conversation.user_id == user.id, Conversation.deleted.is_(False))
    if q:
        query = query.where(Conversation.title.contains(q, autoescape=True) | Conversation.id.in_(ids))
    return [view(c) for c in db.scalars(query.order_by(Conversation.created_at.desc()).limit(200))]


@app.post('/api/conversations', response_model=ConversationView)
def create_conversation(body: ConversationInput, ctx: Context):
    db, user, _ = ctx
    conversation = Conversation(user_id=user.id, title=body.title)
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


@app.get('/api/messages', response_model=list[MessageView])
def messages(conversation_id: str, ctx: Context, before: float | None = None, limit: int = Query(100, ge=1, le=1000)):
    db, user, _ = ctx
    owned(db, Conversation, conversation_id, user)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before:
        query = query.where(Message.created_at < before)
    items = list(db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)))
    return [view(m) for m in reversed(items)]


def downloadable(data, filename, media_type):
    return Response(data, media_type=media_type, headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/conversations/{conversation_id}/export')
def export_conversation(conversation_id: str, ctx: Context, format: str = 'markdown'):
    db, user, _ = ctx
    c = owned(db, Conversation, conversation_id, user)
    items = [view(m, ('workspace_id',)) for m in db.scalars(select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at))]
    if format == 'json':
        return downloadable(json.dumps({'title': c.title, 'messages': items}, ensure_ascii=False, indent=2), 'conversation.json', 'application/json')
    return downloadable('# '+c.title+'\n\n'+'\n\n'.join(f"## {m['role']} · {m['status']}\n\n{m['content']}" for m in items), 'conversation.md', 'text/markdown')


@app.post('/api/chat', response_model=ChatRunView)
def chat(body: ChatInput, ctx: Context):
    db, user, _ = ctx
    digest = hashlib.sha256(json.dumps([body.conversation_id, body.content], ensure_ascii=False).encode()).hexdigest()
    existing = db.scalar(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.request_id == body.request_id))
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, '幂等请求 ID 已用于另一条消息')
        return view(existing)
    conversation = owned(db, Conversation, body.conversation_id, user, lock=True)
    if db.scalar(select(ChatRun).where(ChatRun.conversation_id == conversation.id, ChatRun.status.in_(['queued', 'running']))):
        raise HTTPException(409, '当前会话仍在生成，请先等待或停止')
    model, provider, version = resolve_model(db, 'chat')
    run = ChatRun(user_id=user.id, conversation_id=conversation.id, request_id=body.request_id,
                  request_hash=digest, model_id=model.id, config={'model': model.name, 'provider_version': provider.version,
                  'policy_version': version, 'prompt_version': 'lingxi-companion-v1'})
    db.add(run)
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
    enqueue(db, 'chat:'+run.id, 'chat', {'run_id': run.id})
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
        run.cancel_requested = True
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
    query = select(Memory).where(Memory.user_id == user.id, Memory.status != 'deleted')
    if q:
        query = query.where(Memory.content.contains(q, autoescape=True))
    if category:
        query = query.where(Memory.category == category)
    return [view(m) for m in db.scalars(query.order_by(Memory.created_at.desc()).offset(offset).limit(100))]


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
    MemoryService().correct(db, memory, body.model_dump(exclude={'version'}))
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
    return [view(c) for c in db.scalars(select(Candidate).where(Candidate.user_id == user.id, Candidate.status != 'invalidated').order_by(Candidate.created_at.desc()).limit(200))]


@app.post('/api/memory-candidates/{candidate_id}/actions', response_model=CandidateView)
def candidate_action(candidate_id: str, body: CandidateAction, ctx: Context):
    db, user, _ = ctx
    candidate = owned(db, Candidate, candidate_id, user, lock=True)
    source = db.get(Message, candidate.source_id)
    conversation = db.get(Conversation, source.conversation_id) if source else None
    if not conversation or conversation.deleted or not source or source.status != 'completed':
        raise HTTPException(409, '候选来源已失效')
    if candidate.status != 'pending':
        return view(candidate)
    if body.action == 'confirm':
        memory = MemoryService().remember(db, user.id, candidate.content, candidate.category,
                                          [candidate.source_id], subject=candidate.subject, evidence='explicit')
        candidate.memory_id, candidate.status = memory.id, 'confirmed'
    else:
        candidate.status = 'rejected'
    audit(db, user, 'candidate_'+body.action, candidate_id)
    return view(candidate)


@app.get('/api/memory-operations', response_model=list[OperationView])
def operations(ctx: Context, kind: str = ''):
    db, user, _ = ctx
    query = select(Operation).where(Operation.user_id == user.id)
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
    rows = [view(m, ('workspace_id', 'user_id')) for m in db.scalars(select(Memory).where(Memory.user_id == user.id, Memory.status != 'deleted'))]
    if operation.result['format'] == 'json':
        return downloadable(json.dumps(rows, ensure_ascii=False, indent=2), 'lingxi-memories.json', 'application/json')
    result = '# 灵犀长期记忆\n\n'+'\n\n'.join(f"## {m['category']} · v{m['version']}\n\n{m['content']}\n\n来源：{', '.join(m['source_ids'])}\n状态：{m['status']} / {m['index_status']}" for m in rows)
    return downloadable(result, 'lingxi-memories.md', 'text/markdown')


@app.get('/api/admin/model-calls')
def calls(ctx: Context):
    db, user, _ = ctx
    return [view(c) | {'cost': None} for c in db.scalars(select(ModelCall).where(ModelCall.workspace_id == user.workspace_id).order_by(ModelCall.created_at.desc()).limit(200))]


@app.get('/api/admin/health')
def health(ctx: Context):
    db, _, _ = ctx
    workers = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'running', Job.lease_until > now()))
    pending = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'pending'))
    failed = db.scalar(select(func.count()).select_from(Job).where(Job.status == 'failed'))
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
            'worker': {'active_jobs': workers, 'pending_jobs': pending, 'failed_jobs': failed,
                       'note': '无运行作业不等于 worker 离线'},
            'release_status': '首批实现，真实模型和中文质量待验证'}
