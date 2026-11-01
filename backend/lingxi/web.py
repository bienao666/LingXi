"""Shared authenticated HTTP context; domain routers never trust client identities."""
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from .db import SessionLocal
from .models import Audit, Conversation
from .security import authenticate


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
    db.add(Audit(user_id=user.id, workspace_id=user.workspace_id, action=action, target_id=target))
