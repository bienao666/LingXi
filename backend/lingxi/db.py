import time
import uuid
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


def now() -> float:
    return time.time()


def uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


engine = create_engine(settings().database_url, pool_pre_ping=True,
                       connect_args={'check_same_thread': False, 'timeout': 30}
                       if settings().database_url.startswith('sqlite') else {})
if engine.dialect.name == 'sqlite':
    @event.listens_for(engine, 'connect')
    def sqlite_settings(dbapi, _):
        dbapi.execute('PRAGMA journal_mode=WAL')
        dbapi.execute('PRAGMA foreign_keys=ON')

SessionLocal = sessionmaker(engine, expire_on_commit=False)
