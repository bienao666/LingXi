"""Authenticated reverse WebSocket. Persist before sending; never replay unknown sends."""
import asyncio
import hashlib
import json
import secrets
import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from .db import SessionLocal, now, uid
from .models import User, Conversation, Message, ChatRun, Memory
from .channels import Channel, ChannelRules, ChannelInbound, ChannelDelivery, ChannelSession, IdentityLink
from .security import decrypt

router = APIRouter()
TERMINAL = ('completed', 'failed', 'cancelled', 'invalidated')


def numeric(value, maximum=20, negative=False):
    text = str(value)
    digits = text[1:] if negative and text.startswith('-') else text
    return text if text.isascii() and digits.isdigit() and 1 <= len(text) <= maximum and not isinstance(value, bool) else None


def channel_scope_valid(db, scope, *, locked=False):
    query = select(Channel).where(Channel.id == scope.get('id'))
    channel = db.scalar(query.with_for_update() if locked else query.execution_options(populate_existing=True))
    if not channel or not channel.enabled or channel.version != scope.get('version'):
        return False
    if scope.get('binding_id'):
        link = db.get(IdentityLink, scope['binding_id'], populate_existing=True)
        if not link or link.channel_id != channel.id or link.status != 'active' or link.version != scope.get('binding_version'):
            return False
    return True


def authorized_channel(db, identity, connection):
    from .workspace_migrations import paused
    if paused(db):return None
    row = db.scalar(select(Channel).where(Channel.id == identity).with_for_update())
    if not row or not row.enabled or row.connection_id != connection or row.lease_until <= now():
        return None
    return row


def open_connection(identity, token, self_id):
    with SessionLocal.begin() as db:
        from .workspace_migrations import paused
        if paused(db):return None
        row = db.scalar(select(Channel).where(Channel.id == identity).with_for_update())
        if not row or not row.enabled or row.bot_id != self_id or not secrets.compare_digest(decrypt(row.encrypted_token), token):
            return None
        if row.connection_id and row.lease_until > now():
            return None
        connection = uid()
        from .channel_audio import interrupted as audio_interrupted
        if row.connection_id:
            audio_interrupted(db, identity, row.connection_id)
            from .channel_documents import interrupted as documents_interrupted
            documents_interrupted(db, identity, row.connection_id)
        from .proactive_channels import interrupted
        interrupted(db,identity)
        row.connection_id, row.lease_until = connection, now()+90
        row.login_state, row.last_seen = 'checking', now()
        # A send from a lost connection may already have reached QQ. Do not requeue it.
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
                                                                 ChannelDelivery.status == 'sending')):
            delivery.status, delivery.error = 'unknown', '旧连接中断，发送结果待人工核对；未自动重发'
            db.get(ChannelInbound, delivery.inbound_id).status = 'unknown'
        return connection


def close_connection(identity, connection):
    with SessionLocal.begin() as db:
        row = db.scalar(select(Channel).where(Channel.id == identity).with_for_update())
        if row and row.connection_id == connection:
            from .channel_audio import interrupted as audio_interrupted
            audio_interrupted(db, identity, connection)
            from .channel_documents import interrupted as documents_interrupted
            documents_interrupted(db, identity, connection)
            from .proactive_channels import interrupted
            interrupted(db,identity,connection)
            row.connection_id, row.lease_until, row.login_state = None, 0, 'unknown'
            for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
                                                                     ChannelDelivery.status == 'sending')):
                delivery.status, delivery.error = 'unknown', '连接中断，发送结果待人工核对；未自动重发'
                db.get(ChannelInbound, delivery.inbound_id).status = 'unknown'


def normalize_message(event, row):
    """Only triggered messages proceed; raw CQ strings and local paths are never executed."""
    sender = numeric(event.get('user_id'))
    message_id = numeric(event.get('message_id'), 30, negative=True)
    kind = event.get('message_type')
    if not sender or not message_id or sender == row.bot_id or kind not in ('private', 'group'):
        return None
    rules = ChannelRules(**row.rules)
    if kind == 'group':
        group = numeric(event.get('group_id'))
        if not rules.group_enabled or group not in rules.group_ids:
            return None
        target = 'group:'+group
    else:
        if not rules.private_enabled:
            return None
        target = 'private:'+sender
    segments = event.get('message')
    if not isinstance(segments, list) or len(segments) > 100:
        return None
    texts, mentioned, unsupported, attachments = [], False, False, []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('data'), dict):
            return None
        data = segment['data']
        if segment.get('type') == 'text' and isinstance(data.get('text'), str):
            texts.append(data['text'])
        elif segment.get('type') == 'at':
            mentioned = mentioned or str(data.get('qq')) == row.bot_id
        elif segment.get('type') in ('image', 'file', 'record'):
            attachments.append((segment['type'], data))
        elif segment.get('type') != 'reply':
            unsupported = True
    text = ''.join(texts).strip()
    command = rules.command
    triggered = text == command or text.startswith(command+' ') or text.startswith(command+'\n')
    if kind == 'group' and not (mentioned or triggered):
        return None
    if triggered:
        text = text[len(command):].strip()
    if len(text) > 6000:
        text, unsupported = '', True
    if not text and not unsupported and not attachments:
        return None
    return sender, target, message_id, text, unsupported, attachments


def receive_event(identity, connection, event):
    if not isinstance(event, dict):
        return
    with SessionLocal.begin() as db:
        channel = authorized_channel(db, identity, connection)
        if not channel:
            return
        echo = event.get('echo')
        if isinstance(echo, str) and echo.startswith(connection+':'):
            suffix = echo[len(connection)+1:]
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            if suffix.startswith('record:'):
                from .channel_audio import receive
                receive(db, channel, connection, event)
            elif suffix.startswith('document:'):
                from .channel_documents import receive
                receive(db, channel, connection, event)
            elif suffix == 'status':
                channel.login_state = 'online' if event.get('retcode') == 0 and data.get('online') is True and data.get('good') is not False else 'login_required'
                channel.last_seen, channel.lease_until = now(), now()+90
            elif suffix == 'version' and event.get('retcode') == 0:
                channel.protocol_version = (str(data.get('app_name', ''))+' '+str(data.get('app_version', '')))[:100]
            else:
                from .proactive_channels import receive_receipt
                receive_receipt(db,channel,echo,event)
                delivery = db.scalar(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
                                                                   ChannelDelivery.echo == echo).with_for_update())
                if delivery and delivery.status in ('sending', 'unknown'):
                    if delivery.kind == 'file':
                        from .channel_files import receipt
                        receipt(delivery, event)
                        if delivery.status == 'accepted_by_protocol':
                            channel.capabilities = channel.capabilities | {'file_send': 'accepted_by_protocol'}
                    elif type(event.get('retcode')) is int and event['retcode'] == 0 and (external_id := numeric(data.get('message_id'), 30, negative=True)):
                        delivery.status, delivery.protocol_message_id, delivery.error = 'accepted_by_protocol', external_id, None
                        channel.capabilities = channel.capabilities | {('record_send' if delivery.kind == 'record' else 'text_send'): 'accepted_by_protocol'}
                    elif type(event.get('retcode')) is int and event['retcode'] != 0:
                        delivery.status, delivery.error = 'failed', '协议端拒绝发送，请检查 QQ 登录与平台限制'
                    db.flush()
                    parts = list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == delivery.inbound_id)))
                    incoming = db.get(ChannelInbound, delivery.inbound_id)
                    if all(part.status == 'accepted_by_protocol' or (part.kind == 'record' and part.status == 'cancelled') for part in parts):
                        from .channel_speech import finish
                        if (delivery.kind == 'file' and incoming.status == 'unknown' and
                                incoming.error == '文件上传结果未知，需核对；未自动重发'):
                            incoming.error = None
                        finish(db, incoming)
                    elif any(part.status == 'failed' for part in parts):
                        incoming.status, incoming.error = 'failed', '至少一段投递失败，未声称完整送达'
                    elif any(part.status == 'unknown' for part in parts):
                        incoming.status = 'unknown'
                        if not incoming.error and delivery.kind == 'file':
                            incoming.error = '文件上传结果未知，需核对；未自动重发'
            return
        if str(event.get('self_id')) != channel.bot_id:
            return
        if event.get('post_type') == 'meta_event':
            if event.get('meta_event_type') == 'heartbeat':
                status = event.get('status') if isinstance(event.get('status'), dict) else {}
                channel.login_state = 'online' if status.get('online') is True and status.get('good') is not False else 'login_required'
                channel.last_seen, channel.lease_until = now(), now()+90
            return
        if channel.login_state != 'online' or event.get('post_type') != 'message':
            return
        normalized = normalize_message(event, channel)
        if not normalized:
            return
        sender, target, message_id, text, unsupported, attachments = normalized
        if target.startswith('private:') and text.startswith('/bind '):
            # Challenge material never enters history, model context or logs.
            link = db.scalar(select(IdentityLink).where(IdentityLink.channel_id == channel.id,
                                                       IdentityLink.external_user_id == sender).with_for_update())
            if link and link.status == 'pending' and link.expires_at > now():
                digest = hashlib.sha256(text[6:].encode()).hexdigest()
                if secrets.compare_digest(link.challenge_hash, digest):
                    link.status, link.version, link.challenge_hash = 'proved', link.version+1, ''
            return
        link = db.scalar(select(IdentityLink).where(IdentityLink.channel_id == channel.id,
                                                   IdentityLink.external_user_id == sender, IdentityLink.status == 'active'))
        if target.startswith('private:') and not link and not channel.rules.get('allow_unbound', False):
            return
        if db.scalar(select(ChannelInbound.id).where(ChannelInbound.channel_id == identity,
                          ChannelInbound.target_key == target, ChannelInbound.external_message_id == message_id)):
            return
        pending = db.scalar(select(func.count()).select_from(ChannelInbound).where(ChannelInbound.channel_id == identity,
                                  ChannelInbound.status.in_(['queued', 'ingesting', 'running'])))
        recent = db.scalar(select(func.count()).select_from(ChannelInbound).where(ChannelInbound.channel_id == identity,
                                 ChannelInbound.created_at > now()-60, ChannelInbound.sender_id == sender))
        if pending >= 100 or recent >= 10:
            return
        from .channel_media import declarations, retain
        from fastapi import HTTPException
        attachment_error, items = None, []
        if attachments and not unsupported:
            try:
                from .media_policy import current
                items = declarations(attachments, ChannelRules(**channel.rules), current(db, channel.workspace_id))
            except HTTPException as exc:
                attachment_error = str(exc.detail)
        incoming = ChannelInbound(user_id=channel.user_id, workspace_id=channel.workspace_id, channel_id=identity,
            channel_version=channel.version, external_message_id=message_id, target_key=target, sender_id=sender,
            content=text, binding_id=link.id if link and target.startswith('private:') else None,
            binding_version=link.version if link and target.startswith('private:') else None,
            status='ready' if unsupported or attachment_error else 'queued')
        db.add(incoming)
        db.flush()
        if unsupported:
            add_deliveries(db, incoming, '此 QQ 消息含尚未支持的视频或其他片段，未忽略它们作答；请使用文字或 Web 上传。')
        elif attachment_error:
            incoming.error = attachment_error
            add_deliveries(db, incoming, attachment_error)
        else:
            retain(db, incoming, items)


def add_deliveries(db, incoming, content):
    content = content.strip()
    # Each piece has its own durable receipt. Never discard overflow and claim full delivery.
    if len(content) > 18000:
        content = '回答超过当前 QQ 投递预算，未完整发送。请缩小问题范围。'
    parts = [content[i:i+1500] for i in range(0, len(content), 1500)] or ['模型没有返回可投递文字。']
    for part, text in enumerate(parts):
        db.add(ChannelDelivery(user_id=incoming.user_id, workspace_id=incoming.workspace_id,
            channel_id=incoming.channel_id, inbound_id=incoming.id, part=part,
            content=(f'({part+1}/{len(parts)}) ' if len(parts) > 1 else '')+text, echo=uid()))


def actor_conversation(db, channel, incoming):
    link = db.get(IdentityLink, incoming.binding_id) if incoming.binding_id else None
    share = bool(link and link.status == 'active' and link.allow_memory and incoming.target_key.startswith('private:'))
    scope_key = incoming.target_key+':'+(f'{link.id}:{link.version}' if link else 'isolated')
    scope = db.scalar(select(ChannelSession).where(ChannelSession.channel_id == channel.id, ChannelSession.scope_key == scope_key))
    old_conversation = db.get(Conversation, scope.conversation_id) if scope else None
    if not scope or not old_conversation or old_conversation.deleted:
        if share:
            actor_id = channel.user_id
        else:
            actor = User(workspace_id=channel.workspace_id, username='channel-'+uid(), password_hash='', auto_memory=False)
            db.add(actor)
            db.flush()
            actor_id = actor.id
        conversation = Conversation(user_id=actor_id, workspace_id=channel.workspace_id, title='QQ '+incoming.target_key)
        db.add(conversation)
        db.flush()
        if scope:
            scope.actor_id, scope.conversation_id = actor_id, conversation.id
        else:
            scope = ChannelSession(user_id=channel.user_id, workspace_id=channel.workspace_id, channel_id=channel.id,
                scope_key=scope_key, actor_id=actor_id, conversation_id=conversation.id)
            db.add(scope)
    return db.get(User, scope.actor_id), db.get(Conversation, scope.conversation_id), share


def prepare_outbound(identity, connection):
    result=prepare_reply(identity,connection)
    if result is not None:return result
    with SessionLocal.begin() as db:
        channel = authorized_channel(db, identity, connection)
        if not channel:
            return False
        if channel.login_state == 'online':
            from .channel_documents import prepare as prepare_documents
            action = prepare_documents(db, channel, connection)
            if action:
                return action
            from .channel_audio import prepare
            action = prepare(db, channel, connection)
            if action:
                return action
    from .proactive_channels import prepare
    return prepare(identity,connection)


def prepare_reply(identity, connection):
    """One instance lock serializes inbox order and protocol writes across Core processes."""
    with SessionLocal.begin() as db:
        channel = authorized_channel(db, identity, connection)
        if not channel:
            return False
        if channel.login_state != 'online':
            return None
        for delivery in db.scalars(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
                                                                 ChannelDelivery.status == 'sending', ChannelDelivery.sent_at < now()-30)):
            delivery.status, delivery.error = 'unknown', '协议未返回可确认回执，未自动重发'
            db.get(ChannelInbound, delivery.inbound_id).status = 'unknown'
        unresolved = {}
        for output, incoming in db.execute(select(ChannelDelivery, ChannelInbound).join(ChannelInbound, ChannelDelivery.inbound_id == ChannelInbound.id)
            .where(ChannelDelivery.channel_id == identity, ChannelDelivery.status.in_(['pending','sending','unknown','failed']),
                   ChannelDelivery.reviewed_at.is_(None), ChannelInbound.created_at > now()-3600,
                   ChannelInbound.channel_version == channel.version, ChannelInbound.status != 'cancelled')):
            unresolved.setdefault(incoming.target_key, set()).add(incoming.id)
        inbounds = list(db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == identity,
                             ChannelInbound.status.in_(['queued','ingesting','running','ready'])).order_by(ChannelInbound.created_at, ChannelInbound.id).limit(100)))
        busy_targets = set()
        for incoming in inbounds:
            if incoming.created_at < now()-3600:
                incoming.status, incoming.error = 'cancelled', '消息已超过一小时处理期限，未集中补发'
                from .channel_media import cancel
                cancel(db, incoming, incoming.error)
                continue
            if incoming.target_key in busy_targets:
                continue
            busy_targets.add(incoming.target_key)
            scope = {'id':identity, 'version':incoming.channel_version, 'binding_id':incoming.binding_id,
                     'binding_version':incoming.binding_version}
            if not channel_scope_valid(db, scope):
                incoming.status, incoming.error = 'cancelled', '渠道或身份授权已变化'
                from .channel_media import cancel
                cancel(db, incoming, incoming.error)
                continue
            if incoming.status in ('queued', 'ingesting'):
                if unresolved.get(incoming.target_key, set()) - {incoming.id}:
                    continue
                from .channel_media import context_valid, cancel
                if not context_valid(db, incoming):
                    cancel(db, incoming, '原会话已删除，旧附件接收不转移到新会话')
                    incoming.status = 'cancelled'
                    continue
                actor, conversation, share = actor_conversation(db, channel, incoming)
                if db.scalar(select(ChatRun.id).where(ChatRun.conversation_id == conversation.id, ChatRun.status.in_(['queued','running']))):
                    continue
                from .api import submit_chat
                from .media import MediaChatInput
                from .channel_media import stage, fail, rows as attachment_rows
                from fastapi import HTTPException
                try:
                    media_ids = stage(db, incoming, actor, conversation)
                    if media_ids is None:
                        continue
                    audio = any(r.kind == 'record' for r in attachment_rows(db, incoming.id))
                    from .channel_tasks import receive as receive_task
                    if receive_task(db, channel, incoming, actor, conversation, share, media_ids, audio):
                        continue
                    run_view = submit_chat(MediaChatInput(conversation_id=conversation.id, request_id='onebot:'+incoming.id,
                        content=(f'群成员 {incoming.sender_id}：' if incoming.target_key.startswith('group:') else '')+
                            (incoming.content or ('请回应所附语音中的问题或内容。转写可能有误，不能据此确认个人事实或授权。' if audio else '请说明所附图片或文档的内容。')), media_ids=media_ids),
                        (db, actor, None), channel_context=scope | {'memory_allowed':share, 'sender_id':incoming.sender_id,
                            'target_key':incoming.target_key, 'owner_id':channel.user_id, 'attachment_only': not bool(incoming.content),
                            'audio_unconfirmed': audio})
                    incoming.run_id, incoming.status = run_view['id'], 'running'
                except HTTPException:
                    if attachment_rows(db, incoming.id):
                        fail(db, incoming, '附件或模型能力暂不可用，未忽略附件作答；请检查解析状态与视觉验证，或使用 Web 上传')
                    else:
                        incoming.status, incoming.error = 'failed', '对话能力暂不可用，请管理员检查模型配置'
                        add_deliveries(db, incoming, incoming.error)
                continue
            if incoming.status == 'running':
                run = db.get(ChatRun, incoming.run_id, populate_existing=True)
                if not run or run.status not in TERMINAL:
                    continue
                incoming.status = 'ready'
                if run.status == 'completed':
                    reply = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == 'assistant', Message.status == 'completed'))
                    from .channel_files import queue as queue_file
                    if not queue_file(db, channel, incoming, run, reply):
                        add_deliveries(db, incoming, reply.content if reply else '没有可投递的完整结果。')
                        from .channel_speech import queue
                        queue(db, channel, incoming, run, reply)
                else:
                    incoming.status, incoming.error = 'failed', '生成已停止或依据已失效，未发送部分回答'
                continue
        db.flush()
        # A failed/unknown part blocks later parts and later messages for that target until expiry.
        outputs = list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
            ChannelDelivery.status.in_(['pending','sending','unknown','failed']), ChannelDelivery.reviewed_at.is_(None),
            ChannelDelivery.created_at > now()-3600).order_by(ChannelDelivery.created_at, ChannelDelivery.inbound_id, ChannelDelivery.part).limit(200)))
        blocked = set()
        for output in outputs:
            incoming = db.get(ChannelInbound, output.inbound_id)
            if incoming.target_key in blocked:
                continue
            if incoming.status == 'cancelled' or incoming.channel_version != channel.version or incoming.created_at < now()-3600:
                if output.status == 'pending':
                    output.status, output.content, output.error = 'cancelled', '', '来源、配置或消息有效期已失效'
                    output.encrypted_audio = None
                continue
            blocked.add(incoming.target_key)
            if output.status != 'pending' or now()-channel.last_sent < 2:
                continue
            if not channel_scope_valid(db, {'id':identity, 'version':incoming.channel_version,
                    'binding_id':incoming.binding_id, 'binding_version':incoming.binding_version}):
                output.status, output.content = 'cancelled', ''
                output.encrypted_audio = None
                continue
            if incoming.run_id:
                from .memory import usable
                run = db.get(ChatRun, incoming.run_id, populate_existing=True)
                from .channel_tasks import delivery_valid
                if run and not delivery_valid(db, run):
                    output.status, output.content, output.error = 'cancelled', '', 'QQ 协作来源或授权已失效，未发送旧结果'
                    output.encrypted_audio = None
                    continue
                if not run or run.status != 'completed' or any(not usable(m := db.get(Memory, r['id'])) or m.version != r['version'] for r in run.memory_refs):
                    output.status, output.content, output.error = 'cancelled', '', '回答依据已失效'
                    output.encrypted_audio = None
                    continue
                from .media import assert_media_valid
                from .worker import Cancelled
                try:
                    assert_media_valid(db, run)
                except Cancelled:
                    output.status, output.content, output.error = 'cancelled', '', '附件来源已失效'
                    output.encrypted_audio = None
                    continue
            file_action = None
            if output.kind == 'file':
                from .channel_files import action
                from fastapi import HTTPException
                from .worker import Cancelled
                try:
                    file_action = action(db, output)
                except (HTTPException, Cancelled):
                    output.status, output.error = 'cancelled', '文件来源、正文或授权已失效；未发送文件'
                    incoming.status, incoming.error = 'failed', output.error
                    continue
            if output.kind == 'record':
                from .channel_speech import segment, discard, finish
                try:
                    audio = segment(db, output)
                except Exception:
                    discard(output, '语音依据、配置或音频已失效；本次仅保留文字')
                    finish(db, incoming)
                    continue
                if audio is None:
                    continue
            output.status, output.sent_at = 'sending', now()
            output.echo, channel.last_sent = connection+':'+uid(), now()
            if file_action:
                return file_action | {'echo': output.echo}
            kind, target = incoming.target_key.split(':',1)
            # NapCat sends record as its own message. Group text immediately
            # preceding it already names the sender; do not mix at+record.
            segments = [audio] if output.kind == 'record' else ([{'type':'at','data':{'qq':incoming.sender_id}}] if kind == 'group' else []) + [{'type':'text','data':{'text':output.content}}]
            return {'action':'send_group_msg' if kind == 'group' else 'send_private_msg',
                    'params': {('group_id' if kind == 'group' else 'user_id'):int(target),'message':segments}, 'echo':output.echo}
        return None


@router.websocket('/gateway/onebot/v11/ws/{identity}')
async def onebot_socket(socket: WebSocket, identity: str):
    # No cookie, URL-token or browser authentication. NapCat supplies service headers.
    authorization = socket.headers.get('authorization', '')
    if socket.headers.get('origin') or socket.query_params or not authorization.startswith('Bearer '):
        await socket.close(code=1008)
        return
    token, self_id = authorization[7:], socket.headers.get('x-self-id', '')
    connection = await asyncio.to_thread(open_connection, identity, token, self_id)
    if not connection:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        for action, suffix in [('get_status','status'),('get_version_info','version')]:
            await socket.send_json({'action':action,'params':{},'echo':connection+':'+suffix})
        while True:
            try:
                text = await asyncio.wait_for(socket.receive_text(), timeout=1)
                from .channel_audio import MAX_FRAME
                size = len(text.encode('utf-8'))
                if size > MAX_FRAME:
                    await socket.close(code=1009)
                    return
                try:
                    event = json.loads(text)
                except (ValueError, TypeError):
                    await socket.close(code=1007)
                    return
                if size > 65536 and (not isinstance(event, dict) or 'post_type' in event or
                        not isinstance(event.get('echo'), str) or not event['echo'].startswith(connection+':record:')):
                    await socket.close(code=1009)
                    return
                await asyncio.to_thread(receive_event, identity, connection, event)
            except asyncio.TimeoutError:
                pass
            action = await asyncio.to_thread(prepare_outbound, identity, connection)
            if action is False:
                await socket.close(code=1008)
                return
            if action:
                await socket.send_json(action)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(close_connection, identity, connection)
