"""Explicit owner support controls; neither model text nor other channels grant consent."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import now
from .assistants import SupportPreference, SupportUpdate, support_view
from .web import audit


COMMANDS = {
    '以后先听我说': ('listen', '已将支持方式设为“先听我说”，后续对话优先倾听。'),
    '以后多给建议': ('advice', '已将支持方式设为“给我建议”，后续对话优先提供建议。'),
    '以后优先帮我行动': ('action', '已将支持方式设为“直接协助”。实际执行仍需要独立的任务授权，不会自动操作。'),
    '恢复平衡回应': ('balanced', '已将支持方式恢复为“自然平衡”。'),
    '关闭自动情绪线索': ('stop_observing', '已关闭自动情绪线索，清除旧线索；手动填写的有效感受保留。'),
    '关闭情绪适配': ('disable', '已关闭情绪适配，清除当前感受和自动线索。'),
    '清空当前感受': ('clear_report', '已清空当前感受，清除旧线索；自动分析开关保持原设置。'),
}


def save_support(db, user, body: SupportUpdate):
    from .emotions import owner_lock, invalidate_preferences
    owner_lock(db, user.id)
    row = db.scalar(select(SupportPreference).where(SupportPreference.user_id == user.id,
        SupportPreference.workspace_id == user.workspace_id).with_for_update().execution_options(populate_existing=True))
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '支持偏好已变化，请重新加载后编辑')
    if body.auto_observe and not body.emotion_enabled:
        raise HTTPException(422, '请先允许情绪适配，再开启自动线索')
    if body.auto_observe and not body.observation_channels:
        raise HTTPException(422, '请至少选择一个自动线索入口，或关闭自动线索')
    if body.renew_report and (not body.emotion_enabled or not body.self_report.strip()):
        raise HTTPException(422, '重新确认需要填写当前感受并允许情绪适配')
    if body.auto_observe and (not (row and row.spec.get('auto_observe'))
            or set(body.observation_channels) - set(support_view(row).observation_channels)):
        from .gateway import resolve_model
        model, provider, _ = resolve_model(db, 'extraction')
        if model.workspace_id != user.workspace_id or provider.workspace_id != user.workspace_id:
            raise HTTPException(409, '记忆抽取模型范围无效')
    values = body.model_dump(exclude={'version', 'renew_report'})
    report = body.self_report.strip() if body.emotion_enabled else ''
    old_report = (row.spec.get('self_report') or '').strip() if row else ''
    old_expiry = row.spec.get('report_expires_at') if row else None
    if report and (report != old_report or body.renew_report):
        expiry = now() + body.report_ttl_hours * 3600
    else:
        expiry = old_expiry if report and old_expiry and old_expiry > now() else None
        if expiry is None:
            report = ''
    values |= {'self_report': report, 'report_expires_at': expiry}
    if row:
        row.version, row.spec, row.updated_at = row.version + 1, values, now()
    else:
        row = SupportPreference(user_id=user.id, workspace_id=user.workspace_id, spec=values)
        db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '支持偏好已变化，请重新加载后编辑') from None
    from .memory import append_ledger
    append_ledger({'kind': 'support', 'user_id': user.id, 'max_version': body.version})
    invalidate_preferences(db, user.id, body.version)
    audit(db, user, 'support_preferences_update')
    return support_view(row)


def chat_control(db, user, conversation, text):
    command = text.strip().rstrip('。！!，,').strip()
    if command not in COMMANDS or not user.password_hash:
        return None
    from .channels import ChannelSession
    if db.scalar(select(ChannelSession.id).where(ChannelSession.conversation_id == conversation.id)):
        return None
    # Caller holds the owner lock; serializes page edits and chat commands.
    current = support_view(db.get(SupportPreference, user.id))
    action, reply = COMMANDS[command]
    values = current.model_dump(exclude={'evidence', 'report_expires_at'})
    if action in ('listen', 'advice', 'action', 'balanced'):
        values['mode'] = action
    elif action == 'stop_observing':
        values['auto_observe'] = False
    elif action == 'disable':
        values |= {'emotion_enabled': False, 'auto_observe': False, 'self_report': ''}
    else:
        values['self_report'] = ''
    # Turning off a setting must work even when its old extraction model is down.
    if action != 'clear_report' and values == current.model_dump(exclude={'evidence', 'report_expires_at'}):
        return {'action': action, 'version': current.version,
                'reply': '当前已经是这项设置，无需重复修改。' + reply}
    saved = save_support(db, user, SupportUpdate(**values))
    return {'action': action, 'version': saved.version, 'reply': reply +
        ' 设置已保存；相关在途生成和已有衍生回答会失效。可在“支持偏好”中查看和修改。'}
