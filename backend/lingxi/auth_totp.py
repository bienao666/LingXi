"""TOTP enrollment and recovery. Secrets never appear in read APIs or logs."""
import base64
import hashlib
import hmac
import re
import secrets
import struct
from urllib.parse import quote

from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint, delete, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now
from .models import Audit, Entity, LoginSession, User
from .security import decrypt, encrypt, hasher, token_hash
from .web import Context, audit

router = APIRouter()


class TwoFactor(Entity, Base):
    __tablename__ = 'two_factors'
    __table_args__ = (UniqueConstraint('user_id', 'workspace_id'),)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, default='')
    last_counter: Mapped[int] = mapped_column(Integer, default=-1)
    recovery_hashes: Mapped[list] = mapped_column(JSON, default=list)
    pending_secret: Mapped[str] = mapped_column(Text, default='')
    pending_session: Mapped[str] = mapped_column(String(64), default='')
    pending_request: Mapped[str] = mapped_column(String(100), default='')
    pending_expires: Mapped[float] = mapped_column(Float, default=0)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=300)
    code: str = Field(default='', max_length=64)


class SecurityView(BaseModel):
    version: int
    enabled: bool
    recovery_remaining: int
    pending: bool
    pending_expires_at: float | None


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=0)
    password: str = Field(min_length=1, max_length=300)


class SetupInput(PasswordInput):
    request_id: str = Field(min_length=8, max_length=100)


class FactorInput(PasswordInput):
    code: str = Field(min_length=1, max_length=64)


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    code: str = Field(pattern=r'^\d{6}$', max_length=6)


class CancelInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)


class SetupView(BaseModel):
    version: int
    secret: str
    otpauth_uri: str
    expires_at: float


class RecoveryView(BaseModel):
    status: SecurityView
    recovery_codes: list[str]


def configuration(db, user):
    return db.scalar(select(TwoFactor).where(TwoFactor.user_id == user.id, TwoFactor.workspace_id == user.workspace_id).execution_options(populate_existing=True))


def security_view(row, login):
    pending = bool(row and row.pending_secret and row.pending_session == login.id and row.pending_expires > now())
    return {'version': row.version if row else 0, 'enabled': bool(row and row.enabled),
            'recovery_remaining': len(row.recovery_hashes) if row and row.enabled else 0,
            'pending': pending, 'pending_expires_at': row.pending_expires if pending else None}


def lock_owner(db, user, login=None):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if not user:
        raise HTTPException(401, '登录已失效')
    if login:
        active = db.scalar(select(LoginSession).where(LoginSession.id == login.id,
            LoginSession.user_id == user.id, LoginSession.expires_at > now()).execution_options(populate_existing=True))
        if not active:
            raise HTTPException(401, '登录已失效')
    return user


def attempt(db, user):
    # Durable, account-scoped reservation, committed even for invalid proofs.
    lock_owner(db, user)
    count = db.scalar(select(func.count()).select_from(Audit).where(Audit.user_id == user.id,
        Audit.workspace_id == user.workspace_id, Audit.action == 'security_attempt', Audit.created_at > now()-60))
    if count >= 5:
        raise HTTPException(429, '安全验证过于频繁，请一分钟后重试')
    audit(db, user, 'security_attempt')
    db.commit()


def fail(db, user, detail):
    audit(db, user, 'security_failed')
    db.commit()
    raise HTTPException(401, detail)


def authorize(db, user, login, body, check_version=True):
    attempt(db, user)
    user = lock_owner(db, user, login)
    try:
        hasher.verify(user.password_hash, body.password)
    except (VerifyMismatchError, InvalidHashError):
        fail(db, user, '身份验证未通过，请检查当前密码')
    row = configuration(db, user)
    if check_version and body.version != (row.version if row else 0):
        raise HTTPException(409, '安全配置已变化，请刷新后重新核对')
    return user, row


def totp(secret, counter):
    digest = hmac.new(base64.b32decode(secret), struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff
    return f'{value % 1000000:06d}'


def matched_counter(secret, code, last=-1):
    if not re.fullmatch(r'[0-9]{6}', code):
        return None
    current = int(now() // 30)
    # Small clock skew allowance; a counter is accepted once, never backwards.
    matches = [c for c in (current-1, current, current+1) if c >= 0 and c > last and secrets.compare_digest(totp(secret, c), code)]
    return max(matches) if matches else None


def recovery_digest(user, code):
    normalized = code.replace('-', '').strip().lower()
    if not re.fullmatch(r'[a-f0-9]{20}', normalized):
        return None
    return token_hash('lingxi-recovery:' + user.id + ':' + normalized)


def verify_factor(db, user, row, code):
    if not row or not row.enabled:
        return False
    counter = matched_counter(decrypt(row.encrypted_secret), code.strip(), row.last_counter)
    if counter is not None:
        row.last_counter = counter
        return True
    digest = recovery_digest(user, code)
    if digest and any(secrets.compare_digest(digest, v) for v in row.recovery_hashes):
        row.recovery_hashes = [v for v in row.recovery_hashes if not secrets.compare_digest(v, digest)]
        audit(db, user, 'totp_recovery_used')
        return True
    return False


def verify_login_factor(db, user, code):
    row = configuration(db, user)
    if row and row.enabled:
        attempts = db.scalar(select(func.count()).select_from(Audit).where(Audit.user_id == user.id,
            Audit.workspace_id == user.workspace_id, Audit.action == 'login_factor_attempt', Audit.created_at > now()-60))
        if attempts >= 5:
            raise HTTPException(429, '双重验证尝试过多，请一分钟后重试')
        audit(db, user, 'login_factor_attempt')
        db.commit()
        user = lock_owner(db, user)
        row = configuration(db, user)
        if not row or not row.enabled:
            return
        if not code:
            raise HTTPException(401, '此账号已启用双重验证，请填写认证器验证码或恢复码后重新确认')
        if not verify_factor(db, user, row, code):
            audit(db, user, 'login_failed')
            db.commit()
            raise HTTPException(401, '双重验证未通过，验证码可能已使用或过期，请重试或使用恢复码')


def new_recovery(user, row):
    codes = ['-'.join(value[i:i+4] for i in range(0, 20, 4)) for value in (secrets.token_hex(10) for _ in range(10))]
    row.recovery_hashes = [recovery_digest(user, code) for code in codes]
    return codes


def clear_pending(row):
    row.pending_secret, row.pending_session, row.pending_request, row.pending_expires = '', '', '', 0


def revoke_others(db, user, login):
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id, LoginSession.id != login.id))


@router.get('/api/auth/security', response_model=SecurityView)
def status(ctx: Context):
    db, user, login = ctx
    return security_view(configuration(db, user), login)


@router.post('/api/auth/totp/setup', response_model=SetupView)
def setup(body: SetupInput, ctx: Context):
    db, user, login = ctx
    user, row = authorize(db, user, login, body, check_version=False)
    if row and row.enabled:
        raise HTTPException(409, '双重验证已经启用，请先验证并停用后再绑定其他认证器')
    resumed = bool(row and row.pending_request == body.request_id and row.pending_session == login.id and row.pending_expires > now())
    expected = row.version - 1 if resumed else row.version if row else 0
    if body.version != expected:
        raise HTTPException(409, '安全配置已变化，请刷新后重新核对')
    if not row:
        row = TwoFactor(user_id=user.id, workspace_id=user.workspace_id, version=0)
        db.add(row)
        db.flush()
    if not resumed:
        row.pending_secret = encrypt(base64.b32encode(secrets.token_bytes(20)).decode())
        row.pending_session, row.pending_request, row.pending_expires = login.id, body.request_id, now()+300
        row.version += 1
        audit(db, user, 'totp_setup')
    secret = decrypt(row.pending_secret)
    uri = 'otpauth://totp/' + quote('灵犀:' + user.username, safe='') + '?secret=' + secret + '&issuer=' + quote('灵犀') + '&algorithm=SHA1&digits=6&period=30'
    return {'version': row.version, 'secret': secret, 'otpauth_uri': uri, 'expires_at': row.pending_expires}


@router.post('/api/auth/totp/confirm', response_model=RecoveryView)
def confirm(body: ConfirmInput, ctx: Context):
    db, user, login = ctx
    attempt(db, user)
    user = lock_owner(db, user, login)
    row = configuration(db, user)
    if not row or row.enabled or row.version != body.version:
        raise HTTPException(409, '绑定配置已变化，请重新生成并核对')
    if not row.pending_secret or row.pending_expires <= now() or row.pending_session != login.id:
        raise HTTPException(410, '绑定已过期或不属于当前会话，请重新开始')
    counter = matched_counter(decrypt(row.pending_secret), body.code)
    if counter is None:
        fail(db, user, '认证器验证码无效，请检查时间同步并重试')
    row.encrypted_secret, row.last_counter, row.enabled = row.pending_secret, counter, True
    clear_pending(row)
    row.version += 1
    codes = new_recovery(user, row)
    revoke_others(db, user, login)
    audit(db, user, 'totp_enabled')
    return {'status': security_view(row, login), 'recovery_codes': codes}


@router.post('/api/auth/totp/cancel', response_model=SecurityView)
def cancel(body: CancelInput, ctx: Context):
    db, user, login = ctx
    user = lock_owner(db, user, login)
    row = configuration(db, user)
    if not row or row.version != body.version or row.enabled:
        raise HTTPException(409, '绑定配置已变化，请刷新后核对')
    if row.pending_session and row.pending_session != login.id:
        raise HTTPException(403, '只能取消当前登录会话发起的绑定')
    clear_pending(row)
    row.version += 1
    audit(db, user, 'totp_setup_cancelled')
    return security_view(row, login)


@router.post('/api/auth/totp/recovery', response_model=RecoveryView)
def rotate_recovery(body: FactorInput, ctx: Context):
    db, user, login = ctx
    user, row = authorize(db, user, login, body)
    if not verify_factor(db, user, row, body.code):
        fail(db, user, '双重验证未通过，请使用新的验证码或未使用的恢复码')
    row.version += 1
    codes = new_recovery(user, row)
    revoke_others(db, user, login)
    audit(db, user, 'totp_recovery_rotated')
    return {'status': security_view(row, login), 'recovery_codes': codes}


@router.post('/api/auth/totp/disable', response_model=SecurityView)
def disable(body: FactorInput, ctx: Context):
    db, user, login = ctx
    user, row = authorize(db, user, login, body)
    if not verify_factor(db, user, row, body.code):
        fail(db, user, '双重验证未通过，请使用新的验证码或未使用的恢复码')
    clear_factor(row)
    revoke_others(db, user, login)
    audit(db, user, 'totp_disabled')
    return security_view(row, login)


def clear_factor(row):
    clear_pending(row)
    row.enabled, row.encrypted_secret, row.recovery_hashes, row.last_counter = False, '', [], -1
    row.version += 1


def prune_pending(db):
    db.execute(update(TwoFactor).where(TwoFactor.pending_expires <= now(), TwoFactor.pending_secret != '').values(
        pending_secret='', pending_session='', pending_request='', pending_expires=0))
