"""Independent publication/review service. Cookie sessions never authenticate Core."""
from contextlib import asynccontextmanager
import json
import secrets
import uuid
from typing import Annotated, Literal
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select, text
from sqlalchemy.exc import IntegrityError
from lingxi.plugin_manifest import Manifest, resolve, digest
from lingxi.plugin_signing import canonical, key_id, load_private, public_key, sha, signed, verify
from . import artifacts
from .data import Account, Audit, Login, LoginAttempt, Reader, Release, State, now, session, settings

hasher = PasswordHasher()
DUMMY = hasher.hash(secrets.token_urlsafe(32))


def trust():
    bundle = json.loads(settings().trust_bundle.read_text())
    root = bundle['root_public_key']
    verify(root, bundle['payload'], bundle['signature'])
    private = load_private(settings().signing_key)
    identity = key_id(public_key(private))
    keys = bundle['payload']['keys']
    if not any(k['key_id'] == identity and k['status'] == 'active' and k['public_key'] == public_key(private) for k in keys):
        raise ValueError('Signing key not authorized by trust registry')
    return bundle, private


@asynccontextmanager
async def lifespan(app):
    trust()
    with session() as db:
        if not db.get(State, 'catalog'):
            raise RuntimeError('Run the independent store migration first')
    yield


app = FastAPI(title='灵犀独立插件商店', version='0.1.0', docs_url=None, redoc_url=None, lifespan=lifespan)


class SizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunks.append(message)
            size += len(message.get('body', b''))
            if size > 300*1024:
                return await JSONResponse({'detail': '提交内容超过 300 KiB 限额'}, status_code=413)(scope, receive, send)
            if not message.get('more_body'):
                break
        async def replay():
            if chunks:
                return chunks.pop(0)
            return await receive()
        await self.app(scope, replay, send)


app.add_middleware(SizeLimit)


@app.middleware('http')
async def headers(request, call_next):
    response = await call_next(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin'})
    return response


@app.exception_handler(RequestValidationError)
async def validation(request, exc):
    return JSONResponse({'detail': '输入不符合商店合约，请核对字段、版本和清单'}, status_code=422)


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return JSONResponse({'detail': '版本或请求已存在，请刷新；已提交版本不能覆盖'}, status_code=409)


def database():
    with session() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


DB = Annotated[object, Depends(database)]


def context(request: Request, db: DB):
    login = db.scalar(select(Login).where(Login.token_hash == sha(request.cookies.get('lingxi_store_session', '').encode()), Login.expires_at > now()))
    account = db.get(Account, login.account_id) if login else None
    if not account or not account.enabled:
        raise HTTPException(401, '请登录独立商店账号')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('origin') != settings().public_origin or not secrets.compare_digest(request.headers.get('X-Store-CSRF', ''), login.csrf):
            raise HTTPException(403, '商店请求校验失败')
    return db, account, login


Context = Annotated[tuple, Depends(context)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LoginInput(Input):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=300)


class Identity(BaseModel):
    username: str
    role: str
    namespace: str | None
    audiences: list[str]
    csrf: str


def identity(account, login):
    return dict(username=account.username, role=account.role, namespace=account.namespace, audiences=account.audiences, csrf=login.csrf)


@app.get('/store/v1/health')
def health(db: DB):
    db.execute(text('SELECT 1'))
    trust()
    return {'status': 'ready'}


@app.post('/store/v1/auth/login', response_model=Identity)
def login(body: LoginInput, request: Request, response: Response, db: DB):
    if request.headers.get('origin') != settings().public_origin:
        raise HTTPException(403, '商店请求来源不被允许')
    # Global lock makes rate counters accurate across multiple workers. Bounded service.
    db.scalar(select(State).where(State.id == 'catalog').with_for_update())
    db.execute(delete(LoginAttempt).where(LoginAttempt.window < now()-3600))
    db.execute(delete(Login).where(Login.expires_at < now()-3600))
    name = body.username.casefold()
    keys = [sha(('user:'+name).encode()), sha(('ip:'+(request.client.host if request.client else 'unknown')).encode())]
    rows = []
    for key in keys:
        row = db.get(LoginAttempt, key)
        if not row:
            row = LoginAttempt(id=key, failures=0, window=now())
            db.add(row)
        if row.window < now()-300:
            row.failures, row.window = 0, now()
        if row.failures >= (8 if key == keys[0] else 40):
            raise HTTPException(429, '尝试过多，请五分钟后重试')
        rows.append(row)
    account = db.scalar(select(Account).where(Account.username == name))
    try:
        valid = hasher.verify(account.password_hash if account else DUMMY, body.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not account or not account.enabled or not valid:
        for row in rows:
            row.failures += 1
        db.commit()
        raise HTTPException(401, '商店账号或密码错误')
    rows[0].failures = 0
    token = secrets.token_urlsafe(48)
    row = Login(id=uuid.uuid4().hex, account_id=account.id, token_hash=sha(token.encode()), csrf=secrets.token_urlsafe(32), expires_at=now()+28800)
    db.add(row)
    response.set_cookie('lingxi_store_session', token, httponly=True, secure=settings().cookie_secure, samesite='strict', path='/store/v1', max_age=28800)
    return identity(account, row)


@app.get('/store/v1/auth/me', response_model=Identity)
def me(ctx: Context):
    _, account, login = ctx
    return identity(account, login)


@app.post('/store/v1/auth/logout')
def logout(response: Response, ctx: Context):
    db, _, login = ctx
    db.delete(login)
    response.delete_cookie('lingxi_store_session', path='/store/v1')
    return {'status': 'logged_out'}


class Submission(Input):
    manifest: Manifest
    visibility: Literal['public', 'private'] = 'public'
    audience: str = Field(default='', max_length=100)


class Decision(Input):
    version: int = Field(ge=1)
    action: Literal['approve', 'reject', 'withdraw', 'republish', 'revoke']
    note: str = Field(min_length=1, max_length=1000)


class ReleaseView(BaseModel):
    id: str
    plugin_id: str
    version: str
    manifest: dict
    digest: str
    visibility: str
    audience: str
    status: str
    version_number: int
    checks: dict
    review_note: str
    submitted_at: float
    published_at: float | None
    artifact: dict


def view(row):
    return {k: getattr(row, k) for k in ReleaseView.model_fields}


@app.post('/store/v1/submissions', response_model=ReleaseView)
def submit(body: Submission, ctx: Context):
    db, account, _ = ctx
    if account.role != 'publisher' or account.namespace != body.manifest.publisher or body.manifest.publisher == 'lingxi':
        raise HTTPException(403, '只能提交已分配发布者命名空间中的插件')
    if body.visibility == 'private' and (not body.audience or body.audience not in account.audiences):
        raise HTTPException(403, '没有该私有工作区的发布权限')
    if body.visibility == 'public' and body.audience:
        raise HTTPException(422, '公开发布不应包含私有工作区标识')
    data = body.manifest.model_dump()
    if any(c['kind'] == 'native' for c in data['capabilities']):
        raise HTTPException(422, '商店不接受原生执行入口')
    blob = canonical(data)
    if len(blob) > 256*1024:
        raise HTTPException(422, '插件清单超过 256 KiB')
    # Store identity is immutable, including rejected versions: publish a new version.
    checks = {'schema': 'passed', 'namespace': 'passed', 'artifact': 'json_only',
              'supported_by_current_core': all(c['kind'] in ('skill', 'api', 'mcp', 'event', 'agent') for c in data['capabilities'])}
    row = Release(id=digest({'plugin_id':data['plugin_id'], 'version':data['version']}),
        plugin_id=data['plugin_id'], version=data['version'], publisher_id=account.id,
        visibility=body.visibility, audience=body.audience, manifest=data, digest=sha(blob), checks=checks)
    db.add(row)
    db.flush()
    db.add(Audit(account_id=account.id, release_id=row.id, action='submit'))
    return view(row)


@app.get('/store/v1/submissions', response_model=list[ReleaseView])
def submissions(ctx: Context, status: Literal['all', 'pending', 'published', 'rejected', 'withdrawn', 'revoked'] = 'all'):
    db, account, _ = ctx
    query = select(Release)
    if account.role == 'publisher':
        query = query.where(Release.publisher_id == account.id)
    elif account.role != 'reviewer':
        raise HTTPException(403, '没有审核或发布权限')
    if status != 'all':
        query = query.where(Release.status == status)
    return [view(r) for r in db.scalars(query.order_by(Release.submitted_at.desc(), Release.id).limit(200))]


def dependency_catalog(db, row):
    # Public releases may depend only on public versions. Private ones may add
    # versions in their own audience, never a different private workspace.
    q = select(Release).where(Release.status == 'published')
    q = q.where(Release.visibility == 'public') if row.visibility == 'public' else q.where(or_(Release.visibility == 'public', Release.audience == row.audience))
    return list(db.scalars(q)) + [row]


@app.post('/store/v1/releases/{release_id}/decision', response_model=ReleaseView)
def decide(release_id: str, body: Decision, ctx: Context):
    db, account, _ = ctx
    if account.role != 'reviewer':
        raise HTTPException(403, '仅审核员可决定发布与撤销')
    state = db.scalar(select(State).where(State.id == 'catalog').with_for_update())
    row = db.scalar(select(Release).where(Release.id == release_id).with_for_update())
    if not row:
        raise HTTPException(404, '未找到版本')
    if row.publisher_id == account.id:
        raise HTTPException(403, '不能审核本人提交')
    if row.version_number != body.version:
        raise HTTPException(409, '审核状态已变化，请重新查看')
    transitions = {'approve':('pending','published'), 'reject':('pending','rejected'),
                  'withdraw':('published','withdrawn'), 'republish':('withdrawn','published')}
    if body.action == 'revoke':
        if row.status not in ('published','withdrawn'):
            raise HTTPException(409, '只能撤销已经发布过的版本')
        target = 'revoked'
    else:
        source, target = transitions[body.action]
        if row.status != source:
            raise HTTPException(409, '当前状态不允许此操作')
    if body.action in ('approve','republish'):
        old = row.status
        row.status = 'published'
        try:
            resolve(dependency_catalog(db, row), row.plugin_id, row.version, {})
        finally:
            row.status = old
        bundle, private = trust()
        if not row.artifact:
            try:
                artifacts.put(row.digest, canonical(row.manifest))
            except Exception:
                raise HTTPException(503, '制品存储校验失败，版本尚未发布') from None
            row.artifact = signed(private, {'release_id': row.id, 'digest':row.digest,
                'size':len(canonical(row.manifest)), 'plugin_id':row.plugin_id, 'version':row.version})
        else:
            active = {k['key_id'] for k in bundle['payload']['keys'] if k['status'] == 'active'}
            if row.artifact['key_id'] not in active:
                raise HTTPException(409, '制品签名密钥已撤销，必须提交新版本')
        if row.published_at is None:
            row.published_at = now()
    row.status, row.review_note, row.reviewer_id = target, body.note, account.id
    row.version_number += 1
    state.revision += 1
    db.add(Audit(account_id=account.id, release_id=row.id, action=body.action, note=body.note))
    return view(row)


def audience(request, db):
    auth = request.headers.get('authorization', '')
    if not auth:
        return ''
    if not auth.startswith('Bearer '):
        raise HTTPException(401, '商店读取令牌无效')
    row = db.scalar(select(Reader).where(Reader.token_hash == sha(auth[7:].encode()), Reader.enabled == True, Reader.expires_at > now()))
    if not row:
        raise HTTPException(401, '商店读取令牌无效或过期')
    return row.audience


def visible(db, scope):
    return select(Release).where(Release.published_at.is_not(None),
        or_(Release.visibility == 'public', (Release.audience == scope) & (Release.visibility == 'private')) if scope else Release.visibility == 'public')


@app.get('/store/v1/catalog', response_model=list[ReleaseView])
def catalog(request: Request, db: DB):
    scope = audience(request, db)
    return [view(r) for r in db.scalars(visible(db, scope).where(Release.status == 'published').order_by(Release.plugin_id, Release.version).limit(200))]


@app.get('/store/v1/feed')
def feed(request: Request, db: DB):
    scope = audience(request, db)
    # Serialize with publication so revision and all rows describe one snapshot.
    state = db.scalar(select(State).where(State.id == 'catalog').with_for_update())
    rows = list(db.scalars(visible(db, scope).order_by(Release.id).limit(501)))
    if len(rows) > 500:
        raise HTTPException(503, '目录超过单快照上限，需升级分页同步协议；不返回截断目录')
    bundle, private = trust()
    payload = {'format': 'lingxi-catalog-v1', 'revision': state.revision, 'issued_at': now(),
               'expires_at': now()+600, 'audience':scope, 'trust_epoch':bundle['payload']['epoch'],
               'releases':[{'id':r.id,'status':r.status,'visibility':r.visibility,'artifact':r.artifact} for r in rows]}
    return {'trust':bundle, 'snapshot':signed(private,payload)}


@app.get('/store/v1/artifacts/{digest_}')
def artifact(digest_: str, request: Request, db: DB):
    scope = audience(request, db)
    if not db.scalar(visible(db, scope).where(Release.digest == digest_, Release.status.in_(['published','withdrawn']))):
        raise HTTPException(404, '未找到可访问的制品')
    try:
        data = artifacts.get(digest_)
    except Exception:
        raise HTTPException(503, '制品不可用或完整性校验失败') from None
    return Response(data, media_type='application/json')


@app.get('/store/v1/trust')
def trust_view(ctx: Context):
    _, account, _ = ctx
    if account.role != 'reviewer':
        raise HTTPException(403, '仅审核员可查看治理信息')
    bundle, _ = trust()
    return bundle


@app.get('/store/v1/audit')
def audit_view(ctx: Context):
    db, account, _ = ctx
    if account.role != 'reviewer':
        raise HTTPException(403, '仅审核员可查看治理审计')
    return [dict(id=r.id, account_id=r.account_id, release_id=r.release_id, action=r.action,
        note=r.note, created_at=r.created_at) for r in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(200))]
