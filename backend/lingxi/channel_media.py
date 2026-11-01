"""Triggered QQ attachments, staged outside the gateway transaction.

Only explicitly allowed public HTTPS origins are downloadable. Signed URLs are
short-lived encrypted transport data, never part of a message or model prompt.
"""
import hashlib
import ipaddress
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal, now, uid
from .models import Entity
from .security import decrypt, encrypt

MAX_ATTACHMENTS = 10
URL_TTL = 300
IMAGES = {'.png', '.jpg', '.jpeg', '.webp'}


class ChannelAttachment(Entity, Base):
    __tablename__ = 'channel_attachments'
    __table_args__ = (UniqueConstraint('inbound_id', 'part'),)
    inbound_id: Mapped[str] = mapped_column(String(64), index=True)
    part: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(200))
    encrypted_url: Mapped[str] = mapped_column(Text, default='')
    expires_at: Mapped[float] = mapped_column(Float)
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_state: Mapped[dict] = mapped_column(JSON, default=dict)
    document_state: Mapped[dict] = mapped_column(JSON, default=dict)


class ChannelAttachmentView(BaseModel):
    id: str
    kind: str
    status: str
    size: int
    error: str | None


def valid_host(value):
    """Exact DNS names, no wildcard, IP literals, ports, paths or credentials."""
    if not isinstance(value, str) or value != value.strip() or len(value) > 253:
        raise ValueError('附件域名须为完整域名，不含协议、路径、端口或通配符')
    host = value.lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '.' not in host:
        raise ValueError('附件域名须为完整域名，不含协议、路径、端口或通配符')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
        raise ValueError('附件域名格式无效')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError('附件下载不支持 IP 地址或内网服务')


def checked_url(value, hosts):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise HTTPException(422, '附件未提供有效的 HTTPS 下载地址，请使用 Web 上传')
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None or
                parts.fragment or parts.port not in (None, 443) or not parts.hostname or
                valid_host(parts.hostname) not in hosts):
            raise ValueError()
    except ValueError:
        raise HTTPException(422, '附件地址不在允许的 HTTPS 域名中，请核对渠道规则或使用 Web 上传') from None
    return 'https://' + parts.hostname.lower()


def declarations(segments, rules, policy=None):
    """Pure validation: called only after sender, trigger, binding and dedup checks."""
    from .media import SUFFIXES
    if not segments:
        return []
    if any(kind != 'record' for kind, _ in segments) and (not rules.attachments_enabled or not rules.attachment_hosts):
        raise HTTPException(422, 'QQ 附件接收尚未开启，请管理员配置附件域名，或使用 Web 上传')
    if any(kind == 'record' for kind, _ in segments) and not rules.voice_enabled:
        raise HTTPException(422, 'QQ 语音接收尚未开启，请管理员在渠道规则开启，或改用文字')
    if sum(kind == 'record' for kind, _ in segments) > 1:
        raise HTTPException(422, '每条消息暂只接收一段 QQ 语音，请分开发送')
    if len(segments) > MAX_ATTACHMENTS:
        raise HTTPException(422, '每条 QQ 消息最多 10 个附件，请减少附件后重发')
    if policy:
        from .media_policy import check_sizes
        check_sizes(policy, [0]*len(segments))
    result = []
    for kind, data in segments:
        if kind == 'record':
            from .channel_audio import record_id
            result.append((kind, 'QQ语音.wav', record_id(data.get('file'))))
            continue
        # OneBot image.file may be a cache key, never a trusted local pathname.
        if kind == 'image':
            checked_url(data.get('url'), rules.attachment_hosts)
            name = 'QQ图片-' + str(len(result) + 1) + '.jpg'
            transport = data['url']
        else:
            # NapCat v4.18.41 calls the basename `file`, not `name`.
            # Never treat either field as a local path or a cache key.
            name = data.get('name', data.get('file'))
            if (not isinstance(name, str) or not name or len(name) > 200 or name != Path(name).name or
                    any(c in name for c in '\\/:') or any(ord(c) < 32 or ord(c) == 127 for c in name) or
                    Path(name).suffix.lower() not in SUFFIXES - IMAGES):
                raise HTTPException(422, 'QQ 文件须有受支持的文档名；请使用 Web 上传其他文件')
            from .channel_documents import declaration
            transport = declaration(data, rules.attachment_hosts)
            if policy:
                from .media_policy import check_sizes
                check_sizes(policy, [transport['size'] or 0])
        result.append((kind, name, transport))
    from .channel_documents import check_total
    check_total(result)
    if policy:
        from .media_policy import check_sizes, check_types
        check_sizes(policy, [value.get('size') or 0 if isinstance(value, dict) else 0 for _, _, value in result])
        check_types(policy, [Path(name).suffix.lower() for _, name, _ in result])
    return result


def rows(db, inbound_id):
    return list(db.scalars(select(ChannelAttachment).where(ChannelAttachment.inbound_id == inbound_id).order_by(ChannelAttachment.part)))


def public_attachments(db, inbound_id):
    from .media import Media
    result = []
    for row in rows(db, inbound_id):
        media = db.get(Media, row.media_id) if row.media_id else None
        status = media.status if media and row.status == 'downloaded' else row.status
        result.append({'id': row.id, 'kind': row.kind, 'status': status,
                       'size': media.size if media else 0, 'error': row.error or (media.error if media else None)})
    return result


def retain(db, incoming, items):
    for part, (kind, name, url) in enumerate(items):
        state = {}
        if isinstance(url, dict):
            state = {'phase': url['phase'], 'declared_size': url['size']}
            url = url['value']
        db.add(ChannelAttachment(workspace_id=incoming.workspace_id, inbound_id=incoming.id, part=part,
            kind=kind, filename=name, encrypted_url=encrypt(url), expires_at=now()+URL_TTL, document_state=state))


def cancel(db, incoming, reason):
    from .media import Media, forget_media
    for row in rows(db, incoming.id):
        row.encrypted_url = ''
        if row.kind == 'record':
            row.audio_state = {k: v for k, v in row.audio_state.items() if k not in ('echo', 'connection')}
        if row.document_state:
            row.document_state = {k: v for k, v in row.document_state.items() if k not in ('echo', 'connection')} | {'phase': 'cancelled'}
        row.status, row.error = 'cancelled', reason
        # Only unsubmitted staging resources are discarded. Conversation assets
        # already attached to a run retain their regular user-controlled lifetime.
        media = db.get(Media, row.media_id) if row.media_id else None
        if not incoming.run_id and media and media.status != 'deleted':
            forget_media(db, media)


def context_valid(db, incoming):
    from .models import Conversation
    for row in rows(db, incoming.id):
        if row.conversation_id:
            conversation = db.get(Conversation, row.conversation_id, populate_existing=True)
            if not conversation or conversation.deleted:
                return False
    return True


def stage(db, incoming, actor, conversation):
    from .media import Media
    from .memory import enqueue
    attachments = rows(db, incoming.id)
    if not attachments:
        return []
    if any(row.status in ('failed', 'cancelled') for row in attachments):
        raise HTTPException(409, '附件接收已停止，未发送部分资料给模型；请重新上传')
    from .media_policy import current, check_sizes, check_types
    policy = current(db, incoming.workspace_id, lock=True)
    check_types(policy, [Path(row.filename).suffix.lower() for row in attachments])
    check_sizes(policy,
        [(db.get(Media, row.media_id).size if row.media_id and db.get(Media, row.media_id) else 0) for row in attachments])
    pending = False
    for row in attachments:
        if row.conversation_id and row.conversation_id != conversation.id:
            raise HTTPException(409, '附件会话已变化，未继续处理')
        if not row.media_id:
            media = Media(user_id=actor.id, workspace_id=actor.workspace_id, request_id='onebot-media:'+row.id,
                filename=row.filename, suffix=Path(row.filename).suffix.lower(), size=0, digest='', status='fetching')
            db.add(media)
            db.flush()
            row.media_id = media.id
            row.conversation_id = conversation.id
            pending = True
        else:
            media = db.get(Media, row.media_id)
        if not media or media.user_id != actor.id or media.workspace_id != actor.workspace_id:
            raise HTTPException(409, '附件身份范围已变化，未继续处理')
        if media.status in ('failed', 'deleted'):
            raise HTTPException(409, '附件解析失败或已删除，未忽略附件作答；请使用 Web 查看或重新上传')
        if media.status != 'ready':
            pending = True
    if pending:
        incoming.status = 'ingesting'
        # A single durable download job starts only after all document IDs have
        # resolved. Otherwise a completed mixed-message job could miss late URLs.
        if (any(row.kind != 'record' for row in attachments) and
                not any(row.document_state.get('phase') in ('pending', 'requested') for row in attachments)):
            enqueue(db, 'channel-media:'+incoming.id, 'channel_media', {'inbound_id': incoming.id})
        return None
    return [row.media_id for row in attachments]


def locked_source(db, job):
    from .channels import Channel, ChannelInbound
    from .channel_gateway import channel_scope_valid
    from .worker import owns, Cancelled
    owns(db, job)
    identity = job.payload['inbound_id']
    pre = db.get(ChannelInbound, identity)
    if not pre:
        raise Cancelled()
    channel = db.scalar(select(Channel).where(Channel.id == pre.channel_id).with_for_update())
    incoming = db.get(ChannelInbound, identity, populate_existing=True)
    scope = {'id': incoming.channel_id, 'version': incoming.channel_version,
             'binding_id': incoming.binding_id, 'binding_version': incoming.binding_version}
    if (not channel or incoming.status != 'ingesting' or incoming.created_at < now()-3600 or
            not channel_scope_valid(db, scope) or not context_valid(db, incoming)):
        cancel(db, incoming, '渠道、身份或接收期限已失效')
        incoming.status = 'cancelled'
        return None
    return incoming, channel


def download(url, hosts, path, limit, fence, deadline):
    """The origin gets DNS-checked and pinned by the shared proxy-aware client.

    Queries stay only on the exact request, not on the service endpoint. No
    redirects, cookies, inherited environment proxies, decompression or local IO.
    """
    from .network import outbound_client
    origin = checked_url(url, hosts)
    size, digest = 0, hashlib.sha256()
    with outbound_client(origin, timeout=15, headers={'Accept-Encoding':'identity'}) as client:
        with client.stream('GET', url) as response:
            if response.status_code != 200 or response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise HTTPException(422, '附件下载失败或返回重定向／压缩内容，请使用 Web 上传')
            length = response.headers.get('content-length')
            if length is not None and (not length.isascii() or not length.isdigit() or int(length) > limit):
                raise HTTPException(413, '附件大小超出接收预算，未下载完整文件')
            with path.open('xb') as output:
                for chunk in response.iter_raw(chunk_size=65536):
                    if time.monotonic() > deadline:
                        raise HTTPException(422, '附件下载超时，请使用 Web 上传')
                    fence()
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, '附件大小超出接收预算，未下载完整文件')
                    output.write(chunk)
                    digest.update(chunk)
            if not size or (length is not None and size != int(length)):
                raise HTTPException(422, '附件为空或下载不完整，请重新上传')
    return size, digest.hexdigest()


def ingest(job):
    from .media import Media, MAX_FILE, MAX_TOTAL, storage
    from .memory import enqueue
    from .worker import owns, Cancelled
    deadline = time.monotonic()+120
    try:
        with SessionLocal.begin() as db:
            source = locked_source(db, job)
            if not source:
                return
            incoming, channel = source
            ids = [row.id for row in rows(db, incoming.id) if row.kind != 'record']
        for identity in ids:
            with SessionLocal.begin() as db:
                source = locked_source(db, job)
                if not source:
                    return
                incoming, channel = source
                row = db.get(ChannelAttachment, identity)
                if row.status == 'downloaded':
                    continue
                if row.document_state and row.document_state.get('phase') != 'url_ready':
                    raise HTTPException(409, '文档下载地址尚未核验，未读取缓存标识')
                if not row.encrypted_url or row.expires_at < now():
                    raise HTTPException(422, '附件下载地址已过期，请重新上传')
                media = db.get(Media, row.media_id)
                if not media or media.status != 'fetching':
                    raise Cancelled('Attachment no longer available')
                url, hosts, media_id = decrypt(row.encrypted_url), channel.rules.get('attachment_hosts', []), media.id
                others = [db.get(Media, r.media_id) for r in rows(db, incoming.id) if r.id != identity and r.media_id]
                from .media_policy import current, check_sizes, check_types, MIB
                policy = current(db, incoming.workspace_id, lock=True)
                check_types(policy, [media.suffix]+[m.suffix for m in others if m])
                check_sizes(policy, [m.size if m else 0 for m in others]+[0])
                used = sum(m.size for m in others if m)
                limit = min(MAX_FILE, policy.max_file_mib*MIB, MAX_TOTAL-used, policy.max_total_mib*MIB-used)
                if limit <= 0:
                    raise HTTPException(413, '当前附件合计上限已用完，请减少附件')
                row.status = 'downloading'
            temporary = storage(uid(), 'blob')
            def fence():
                with SessionLocal.begin() as db:
                    if not locked_source(db, job):
                        raise Cancelled('Channel attachment invalidated')
                    item = db.get(Media, media_id, populate_existing=True)
                    if not item or item.status != 'fetching':
                        raise Cancelled('Attachment deleted')
            try:
                size, digest = download(url, hosts, temporary, limit, fence, deadline)
                with SessionLocal.begin() as db:
                    source = locked_source(db, job)
                    if not source:
                        return
                    row = db.get(ChannelAttachment, identity)
                    declared = row.document_state.get('declared_size')
                    if declared is not None and size != declared:
                        raise HTTPException(422, '文档大小与协议声明不符，未读取不完整内容')
                    media = db.scalar(select(Media).where(Media.id == media_id).with_for_update())
                    if not media or media.status != 'fetching':
                        raise Cancelled('Attachment deleted')
                    from .media_policy import check_sizes, check_types, current
                    incoming = source[0]
                    others = [db.get(Media, r.media_id) for r in rows(db, incoming.id) if r.id != identity and r.media_id]
                    policy = current(db, incoming.workspace_id, lock=True)
                    check_types(policy, [media.suffix]+[m.suffix for m in others if m])
                    check_sizes(policy, [m.size if m else 0 for m in others]+[size])
                    temporary.replace(storage(media.id, 'blob'))
                    media.size, media.digest, media.status = size, digest, 'pending'
                    row.status, row.encrypted_url = 'downloaded', ''
                    if row.document_state:
                        row.document_state = {'phase': 'downloaded', 'declared_size': declared}
                    enqueue(db, 'media-parse:'+media.id, 'media_parse', {'media_id':media.id})
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        # Download errors never serialize upstream URLs or bodies into logs.
        with SessionLocal.begin() as db:
            owns(db, job)
            source = locked_source(db, job)
            if source:
                incoming, _ = source
                fail(db, incoming, '附件接收失败，未读取全部资料；请检查域名、格式、网络或使用 Web 上传')


def fail(db, incoming, error):
    from .channel_gateway import add_deliveries
    cancel(db, incoming, error)
    for row in rows(db, incoming.id):
        row.status = 'failed'
    incoming.status, incoming.error = 'ready', error
    add_deliveries(db, incoming, error)
