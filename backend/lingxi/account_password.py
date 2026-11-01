"""Authenticated password rotation; never retain plaintext or keep old sessions."""
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete

from .auth_totp import authorize, clear_pending, fail, verify_factor
from .models import LoginSession
from .security import hasher
from .web import Context, audit

router = APIRouter()


class PasswordChangeInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    password: SecretStr = Field(min_length=1, max_length=300)
    new_password: SecretStr = Field(min_length=12, max_length=300)
    confirmation: SecretStr = Field(min_length=12, max_length=300)
    code: SecretStr = Field(default=SecretStr(''), max_length=64)
    acknowledgement: Literal['revoke_all_logins']


class PasswordChangeView(BaseModel):
    status: Literal['reauthentication_required'] = 'reauthentication_required'


@router.put('/api/auth/password', response_model=PasswordChangeView)
def change_password(body: PasswordChangeInput, response: Response, ctx: Context):
    db, user, login = ctx
    new = body.new_password.get_secret_value()
    if new != body.confirmation.get_secret_value() or not new.strip():
        raise HTTPException(422, '两次新密码须一致，且不能只含空白；密码不会自动去除首尾空格')
    user, factor = authorize(db, user, login,
        SimpleNamespace(password=body.password.get_secret_value(), version=body.version))
    if new == body.password.get_secret_value():
        raise HTTPException(422, '新密码不能与当前密码相同')
    if factor and factor.enabled and not verify_factor(db, user, factor, body.code.get_secret_value()):
        fail(db, user, '双重验证未通过，请使用新的验证码或未使用的恢复码')
    user.password_hash = hasher.hash(new)
    if factor:
        clear_pending(factor)
        factor.version += 1
    # One owner lock serializes password, TOTP, login and session mutations.
    # No replacement session is issued, including to this browser.
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    audit(db, user, 'password_changed')
    response.delete_cookie('lingxi_session', path='/')
    return PasswordChangeView()
