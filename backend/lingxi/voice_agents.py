"""Confirmed voice delegation uses the existing task and playback authorities."""
from fastapi import HTTPException
from sqlalchemy import select
from .models import ChatRun


def confirmed(run):
    return bool(run and run.config.get('voice_session_id')
        and run.config.get('voice_input') == {'automatic': False, 'confirmed': True}
        and run.config.get('task_assist', {}).get('mode') == 'collaboration')


def result_origin(db, message, user):
    """Resolve a persisted main-assistant result; never accept browser supplied text."""
    from .delegation import AgentRun
    from .chat_actions import for_target, valid_origin
    from .agent_worker import validate_sources, validate_profiles
    if not message or message.run_id:
        return None
    task = db.scalar(select(AgentRun).where(AgentRun.result_message_id == message.id,
        AgentRun.user_id == user.id, AgentRun.workspace_id == user.workspace_id))
    if not task:
        return None
    action = for_target(db, task.id)
    parent = db.get(ChatRun, action.run_id, populate_existing=True) if action else None
    if (task.status not in ('completed', 'partial') or task.cancel_requested
        or not action or not valid_origin(db, action) or not confirmed(parent)
        or message.workspace_id != user.workspace_id or message.status != 'completed'
        or message.role != 'assistant' or message.conversation_id != task.conversation_id
        or parent.conversation_id != message.conversation_id):
        raise HTTPException(409, '协作汇总来源已失效，停止播报')
    validate_sources(db, task, lock=False)
    validate_profiles(db, task)
    return parent
