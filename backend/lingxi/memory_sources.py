"""A source ID is evidence only inside its owner's message/conversation scope."""
from sqlalchemy import select
from .models import Conversation, Message, User


def resolve(db, identity, workspace_id, user_id=None, *, allow_deleted=False, lock=False):
    query = select(Message).join(Conversation, Conversation.id == Message.conversation_id).join(
        User, User.id == Conversation.user_id).where(Message.id == identity,
        Message.workspace_id == workspace_id, Conversation.workspace_id == workspace_id,
        User.workspace_id == workspace_id)
    if user_id is not None:
        query = query.where(User.id == user_id)
    if not allow_deleted:
        query = query.where(Conversation.deleted == False)
    if lock:
        query = query.with_for_update(of=Message)
    return db.scalar(query.execution_options(populate_existing=True))


def run_matches(run, source, user_id, workspace_id):
    return not source.run_id or bool(run and run.id == source.run_id and run.user_id == user_id
        and run.workspace_id == workspace_id and run.conversation_id == source.conversation_id)
