"""Versioned provider protocol selection; missing rows preserve existing OpenAI connections."""
from typing import Literal
from fastapi import HTTPException
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal
from .schemas import ProviderInput as BaseInput, ProviderView as BaseView

Protocol = Literal['openai', 'anthropic', 'gemini']


class ProviderProtocol(Base):
    __tablename__ = 'provider_protocols'
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    protocol: Mapped[str] = mapped_column(String(20), nullable=False)


class ProviderInput(BaseInput):
    protocol: Protocol | None = None


class ProviderView(BaseView):
    protocol: Protocol = 'openai'


def protocol_for(provider, db=None):
    if db is None:
        cached = getattr(provider, '_lingxi_protocol', None)
        if cached is not None:
            return cached
        with SessionLocal() as session:
            return protocol_for(provider, session)
    row = db.get(ProviderProtocol, provider.id)
    value = row.protocol if row else 'openai'
    if value not in ('openai', 'anthropic', 'gemini'):
        raise HTTPException(409, '服务协议配置无效，请核对连接')
    return value


def bind_protocol(provider, db):
    provider._lingxi_protocol = protocol_for(provider, db)
    return provider


def require_capability(protocol, purpose):
    supported = {'openai': ('chat', 'extraction', 'vision', 'embedding', 'rerank', 'asr', 'tts'),
                 'anthropic': ('chat', 'extraction', 'vision'),
                 'gemini': ('chat', 'extraction', 'vision', 'embedding')}
    if purpose not in supported.get(protocol, ()):
        raise HTTPException(422, '所选原生协议尚未接入此用途，请为该用途选择其他支持的服务连接')
