import time
import uuid
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='LINGXI_STORE_', extra='ignore')
    database_url: str = 'sqlite:///runtime/store.db'
    public_origin: str = 'https://localhost'
    cookie_secure: bool = True
    artifact_dir: Path = Path('runtime/store-artifacts')
    signing_key: Path = Path('runtime/store-keys/signing.key')
    trust_bundle: Path = Path('runtime/store-keys/trust.json')
    s3_endpoint: str = ''
    s3_bucket: str = ''
    s3_region: str = 'us-east-1'
    s3_access_key: str = ''
    s3_secret_key: str = ''


@lru_cache
def settings():
    return Settings()


class Base(DeclarativeBase):
    pass


def now():
    return time.time()


class Account(Base):
    __tablename__ = 'store_accounts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    namespace: Mapped[str | None] = mapped_column(String(60), unique=True, nullable=True)
    audiences: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)


class Login(Base):
    __tablename__ = 'store_logins'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(Float)


class Reader(Base):
    __tablename__ = 'store_readers'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    audience: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(default=True)


class Release(Base):
    __tablename__ = 'store_releases'
    __table_args__ = (UniqueConstraint('plugin_id', 'version'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(30))
    publisher_id: Mapped[str] = mapped_column(String(64), index=True)
    visibility: Mapped[str] = mapped_column(String(20))
    audience: Mapped[str] = mapped_column(String(100), default='')
    manifest: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    checks: Mapped[dict] = mapped_column(JSON)
    review_note: Mapped[str] = mapped_column(String(1000), default='')
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact: Mapped[dict] = mapped_column(JSON, default=dict)
    submitted_at: Mapped[float] = mapped_column(Float, default=now)
    published_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class State(Base):
    __tablename__ = 'store_state'
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class Audit(Base):
    __tablename__ = 'store_audits'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    account_id: Mapped[str] = mapped_column(String(64))
    release_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(40))
    note: Mapped[str] = mapped_column(String(1000), default='')
    created_at: Mapped[float] = mapped_column(Float, default=now)


class LoginAttempt(Base):
    __tablename__ = 'store_login_attempts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window: Mapped[float] = mapped_column(Float, default=now)


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True,
        connect_args={'check_same_thread': False, 'timeout': 30} if settings().database_url.startswith('sqlite') else {})


def session():
    return sessionmaker(engine(), expire_on_commit=False)()
