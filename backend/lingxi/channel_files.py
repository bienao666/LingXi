"""Bounded Markdown answer files through NapCat's upload_*_file extensions.

No user/model paths, download links, credentials or copies of private attachments.
Rebuild bytes only from the exact completed reply, validate sources, then commit
send intent in the gateway transaction before any WebSocket I/O. Never replay an
upload whose outcome is unknown. A file_id is protocol acceptance, not user receipt.
"""
import base64
import hashlib

from fastapi import HTTPException
from sqlalchemy import select

from .db import now, uid
from .channels import Channel, ChannelDelivery, ChannelInbound
from .models import ChatRun, Conversation, Memory, Message, User

TEXT_LIMIT = 6000
MAX_BYTES = 512 * 1024


def queue(db, channel, incoming, run, message):
    if not channel.rules.get('file_reply_enabled') or not message or len(message.content) <= TEXT_LIMIT:
        return False
    raw = message.content.encode('utf-8')
    if len(raw) > MAX_BYTES:
        from .channel_gateway import add_deliveries
        incoming.error = '回答超过 512 KiB 文件预算，未完整发送；请缩小范围'
        add_deliveries(db, incoming, incoming.error)
        return True
    # Generated name contains no user text, path components or personal labels.
    name = 'lingxi-answer-' + incoming.id + '.md'
    for part, kind, content in ((0, 'text', '回答较长，完整内容将另附 Markdown 文件。文件投递状态以协议回执为准。'),
                                (1, 'file', '')):
        db.add(ChannelDelivery(user_id=incoming.user_id, workspace_id=incoming.workspace_id,
            channel_id=channel.id, inbound_id=incoming.id, part=part, kind=kind, content=content, echo=uid(),
            file_info=({'name': name, 'bytes': len(raw), 'digest': hashlib.sha256(raw).hexdigest(),
                        'message_id': message.id, 'run_id': run.id} if kind == 'file' else {})))
    return True


def source(db, output):
    from .channel_gateway import channel_scope_valid
    from .memory import usable
    from .media import assert_media_valid
    incoming = db.get(ChannelInbound, output.inbound_id, populate_existing=True)
    channel = db.get(Channel, output.channel_id, populate_existing=True)
    if channel:
        db.scalar(select(User).where(User.id == channel.user_id).with_for_update())
    run = db.get(ChatRun, incoming.run_id, populate_existing=True) if incoming else None
    conversation = db.get(Conversation, run.conversation_id, populate_existing=True) if run else None
    scope = run.config.get('channel', {}) if run else {}
    if (not incoming or not channel or incoming.status != 'ready' or incoming.created_at < now()-3600
            or not channel.rules.get('file_reply_enabled') or output.status != 'pending'
            or output.kind != 'file' or not run or run.status != 'completed' or run.cancel_requested
            or not conversation or conversation.deleted or conversation.user_id != run.user_id
            or conversation.workspace_id != channel.workspace_id or incoming.channel_id != channel.id
            or output.workspace_id != channel.workspace_id or incoming.workspace_id != channel.workspace_id
            or output.user_id != channel.user_id or incoming.user_id != channel.user_id
            or output.file_info.get('run_id') != run.id
            or any(scope.get(k) != v for k, v in {'id': channel.id, 'version': incoming.channel_version,
                'binding_id': incoming.binding_id, 'binding_version': incoming.binding_version,
                'target_key': incoming.target_key, 'sender_id': incoming.sender_id, 'owner_id': channel.user_id}.items())
            or not channel_scope_valid(db, scope)):
        raise HTTPException(409, '文件回复来源、渠道或身份已失效')
    for ref in run.memory_refs:
        memory = db.get(Memory, ref['id'], populate_existing=True)
        if not usable(memory) or memory.version != ref['version']:
            raise HTTPException(409, '文件回复记忆依据已失效')
    for identity in run.source_message_ids:
        message = db.get(Message, identity, populate_existing=True)
        if (not message or message.conversation_id != run.conversation_id
                or message.status in ('deleted', 'redacted', 'invalidated', 'superseded')):
            raise HTTPException(409, '文件回复对话依据已失效')
    assert_media_valid(db, run)
    message = db.get(Message, output.file_info.get('message_id'), populate_existing=True)
    if (not message or message.run_id != run.id or message.role != 'assistant' or message.status != 'completed'
            or message.conversation_id != conversation.id
            or message.workspace_id != channel.workspace_id):
        raise HTTPException(409, '文件回复正文已失效')
    raw = message.content.encode('utf-8')
    if (not 0 < len(raw) <= MAX_BYTES or len(raw) != output.file_info.get('bytes')
            or hashlib.sha256(raw).hexdigest() != output.file_info.get('digest')
            or output.file_info.get('name') != 'lingxi-answer-' + incoming.id + '.md'):
        raise HTTPException(409, '文件回复内容或摘要已变化')
    return incoming, raw


def action(db, output):
    incoming, raw = source(db, output)
    kind, target = incoming.target_key.split(':', 1)
    return {'action': 'upload_group_file' if kind == 'group' else 'upload_private_file',
            'params': {('group_id' if kind == 'group' else 'user_id'): target,
                       'file': 'base64://' + base64.b64encode(raw).decode('ascii'),
                       'name': output.file_info['name'], 'upload_file': True}}


def receipt(delivery, event):
    """NapCat upload returns file_id (which may be null), not message_id."""
    data = event.get('data')
    identity = data.get('file_id') if isinstance(data, dict) else None
    if type(event.get('retcode')) is int and event['retcode'] == 0:
        if (event.get('status') == 'ok' and isinstance(identity, str) and 0 < len(identity) <= 200
                and all(33 <= ord(c) <= 126 for c in identity)):
            delivery.status, delivery.protocol_file_id, delivery.error = 'accepted_by_protocol', identity, None
        else:
            delivery.status, delivery.error = 'unknown', '协议未返回有效文件 ID，上传结果待核对；未自动重发'
    elif type(event.get('retcode')) is int and event['retcode'] != 0:
        delivery.status, delivery.error = 'failed', '协议端拒绝文件上传，请检查 QQ 登录与文件权限'


def checkpoint(db):
    from .channels import delivery_view
    from .web import view
    deliveries = list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.kind == 'file')))
    ids = {d.inbound_id for d in deliveries}
    return {'inbounds': [view(r, ('content', 'run_id')) | {'content': '', 'run_id': None,
            'status': 'cancelled', 'error': '恢复隔离：旧文件回复不重放'}
            for r in db.scalars(select(ChannelInbound).where(ChannelInbound.id.in_(ids)))],
        'deliveries': [delivery_view(r) | {'content': '', 'encrypted_audio': None,
            'status': 'accepted_by_protocol' if r.status == 'accepted_by_protocol' else (
                'unknown' if r.status in ('sending', 'unknown') else 'cancelled'),
            'error': None if r.status == 'accepted_by_protocol' else '恢复隔离：旧文件回复不重放'}
            for r in deliveries]}


def restore(db, data):
    for values in data.get('inbounds', []):
        db.merge(ChannelInbound(**values))
    db.flush()
    for values in data.get('deliveries', []):
        db.merge(ChannelDelivery(**values))
