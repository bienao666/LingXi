"""Resolve received NapCat file IDs into controlled, short-lived HTTPS URLs.

Pinned to v4.18.41: get_private_file_url(file_id) and
get_group_file_url(group_id, file_id). No get_file, local paths, Base64 or
untriggered file notices. IDs and URLs remain encrypted transient transport data.
"""
import re

from fastapi import HTTPException
from sqlalchemy import select

from .db import now, uid
from .channels import ChannelInbound
from .channel_media import rows, checked_url, context_valid, cancel, fail, URL_TTL
from .media import Media, MAX_FILE, MAX_TOTAL
from .security import encrypt, decrypt

RESOLUTION_TTL = 60


def file_id(value):
    # QQ file UUIDs are opaque and may contain a leading slash. They are only
    # passed to the exact protocol lookup, never interpreted as paths/URLs.
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9/_+=.\-]{1,512}', value) or
            '..' in value or value.startswith('//')):
        raise HTTPException(422, '文档未提供有效的 NapCat 文件标识，请使用 Web 上传')
    return value


def declaration(data, hosts):
    size = data.get('file_size', data.get('size'))
    if size is not None:
        if isinstance(size, str) and size.isascii() and size.isdigit() and len(size) <= 12:
            size = int(size)
        if type(size) is not int or not 0 < size <= MAX_FILE:
            raise HTTPException(413, '文档声明大小无效或超过单个 20 MiB，请使用 Web 上传')
    url = data.get('url')
    # An explicitly supplied unsafe URL is an error, not permission to bypass
    # origin checks by trying another transport. Empty NapCat URLs can resolve.
    if url not in (None, ''):
        checked_url(url, hosts)
        return {'phase': 'url_ready', 'size': size, 'value': url}
    return {'phase': 'pending', 'size': size, 'value': file_id(data.get('file_id'))}


def check_total(items):
    if sum(value.get('size') or 0 for _, _, value in items if isinstance(value, dict)) > MAX_TOTAL:
        raise HTTPException(413, '文档声明总大小超过单条 50 MiB，请分开发送')


def source_valid(db, incoming):
    from .channel_gateway import channel_scope_valid
    return (incoming.status == 'ingesting' and incoming.created_at >= now()-3600 and context_valid(db, incoming)
        and channel_scope_valid(db, {'id': incoming.channel_id, 'version': incoming.channel_version,
            'binding_id': incoming.binding_id, 'binding_version': incoming.binding_version}))


def interrupted(db, identity, connection):
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == identity,
                                                           ChannelInbound.status == 'ingesting')):
        if any(r.document_state.get('phase') == 'requested' and
               r.document_state.get('connection') == connection for r in rows(db, incoming.id)):
            fail(db, incoming, '文档取址连接中断，未自动重试；请重新发送文档或使用 Web 上传')


def prepare(db, channel, connection):
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == channel.id,
            ChannelInbound.status == 'ingesting').order_by(ChannelInbound.created_at, ChannelInbound.id).limit(100)):
        for row in rows(db, incoming.id):
            state = row.document_state
            if row.kind != 'file' or state.get('phase') not in ('pending', 'requested'):
                continue
            if not source_valid(db, incoming):
                cancel(db, incoming, '文档来源或身份已失效')
                incoming.status = 'cancelled'
                break
            media = db.get(Media, row.media_id) if row.media_id else None
            if not media or media.status != 'fetching':
                fail(db, incoming, '文档附件已删除或不可用，未继续取址')
                break
            try:
                if row.expires_at < now():
                    raise HTTPException(422, '文档缓存标识已过期，请重新发送')
                if state['phase'] == 'requested':
                    if state.get('connection') != connection or state.get('sent_at', 0)+RESOLUTION_TTL < now():
                        raise HTTPException(422, '文档取址超时或连接变化，未自动重试；请重新发送')
                    continue
                key = file_id(decrypt(row.encrypted_url))
                echo = connection+':document:'+uid()
                row.document_state = state | {'phase': 'requested', 'echo': echo, 'connection': connection, 'sent_at': now()}
                row.status = 'resolving'
                params = {'file_id': key}
                group = incoming.target_key.startswith('group:')
                if group:
                    params['group_id'] = incoming.target_key.split(':', 1)[1]
                return {'action': 'get_group_file_url' if group else 'get_private_file_url', 'params': params, 'echo': echo}
            except HTTPException as exc:
                fail(db, incoming, str(exc.detail))
                break
    return None


def receive(db, channel, connection, event):
    echo = event.get('echo')
    if not isinstance(echo, str) or not echo.startswith(connection+':document:'):
        return
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == channel.id,
                                                           ChannelInbound.status == 'ingesting')):
        for row in rows(db, incoming.id):
            state = row.document_state
            if row.kind != 'file' or state.get('phase') != 'requested' or state.get('echo') != echo:
                continue
            if not source_valid(db, incoming):
                cancel(db, incoming, '文档来源或身份已失效')
                incoming.status = 'cancelled'
                return
            try:
                if (state.get('connection') != connection or state.get('sent_at', 0)+RESOLUTION_TTL < now() or
                        row.expires_at < now() or type(event.get('retcode')) is not int or
                        event['retcode'] != 0 or event.get('status') != 'ok'):
                    raise ValueError()
                data = event.get('data')
                url = data.get('url') if isinstance(data, dict) else None
                checked_url(url, channel.rules.get('attachment_hosts', []))
                media = db.get(Media, row.media_id, populate_existing=True)
                if not media or media.status != 'fetching':
                    raise ValueError()
                row.encrypted_url, row.expires_at, row.status = encrypt(url), now()+URL_TTL, 'pending'
                row.document_state = {'phase': 'url_ready', 'declared_size': state.get('declared_size')}
                channel.capabilities = channel.capabilities | {'file_resolve': 'url_received'}
                # stage() enqueues once all mixed-message declarations resolve.
            except (ValueError, HTTPException):
                fail(db, incoming, '文档取址失败、地址不受信任或已过期；请检查 NapCat 文件接口与允许域名，或使用 Web 上传')
            return
