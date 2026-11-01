"""QQ asynchronous audio. No browser session, local protocol paths or paid retries.

NapCat v4.18.41 get_record returns WAV base64 on the authenticated socket. Only
an opaque record key received in an authorized message can request conversion.
The business media lifecycle owns the waveform and unconfirmed transcript.
"""
import base64
import binascii
import hashlib
import re

from fastapi import HTTPException
from sqlalchemy import select

from .db import SessionLocal, now, uid
from .channel_media import ChannelAttachment, rows, context_valid, cancel, fail, locked_source
from .channels import Channel, ChannelInbound
from .models import User
from .media import Media, MAX_TOTAL, storage
from .voice import MAX_AUDIO, config_row, audio_provider, checked, wav_check, reserve_audio, audio_call

MAX_FRAME = (MAX_AUDIO + 2) // 3 * 4 + 65536
CONVERSION_TTL = 60


def record_id(value):
    # Pinned NapCat uses element.fileName as the contextual file key. Accept
    # cache hashes, not paths, URLs, CQ strings or arbitrary search expressions.
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{32}(?:\.(?:amr|silk|slk|wav))?', value):
        raise HTTPException(422, '语音未提供受支持的 NapCat 缓存标识，请改用文字；不读取路径或下载链接')
    return value


def snapshot(db, channel):
    owner = db.get(User, channel.user_id)
    config = config_row(db, owner) if owner else None
    if not config or not config.spec.get('enabled') or not checked(db, config, 'asr'):
        raise HTTPException(409, '请先在语音配置启用并验证语音识别模型，再接收 QQ 语音')
    provider, model = audio_provider(db, config, 'asr')
    return config, provider, model, {'voice_version': config.version, 'provider_id': provider.id,
        'provider_version': provider.version, 'model': model}


def valid_snapshot(db, channel, row):
    config, provider, model, pin = snapshot(db, channel)
    if any(row.audio_state.get(k) != v for k, v in pin.items()):
        raise HTTPException(409, '语音配置或服务连接已变化，本次识别已停止，请重新发送')
    return config, provider, model


def source_valid(db, incoming):
    from .channel_gateway import channel_scope_valid
    return (incoming.status == 'ingesting' and incoming.created_at >= now()-3600 and context_valid(db, incoming)
        and channel_scope_valid(db, {'id': incoming.channel_id, 'version': incoming.channel_version,
            'binding_id': incoming.binding_id, 'binding_version': incoming.binding_version}))


def interrupted(db, identity, connection):
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == identity,
                                                            ChannelInbound.status == 'ingesting')):
        if any(r.kind == 'record' and r.audio_state.get('phase') == 'requested' and
               r.audio_state.get('connection') == connection for r in rows(db, incoming.id)):
            fail(db, incoming, '取音频连接中断，未自动重试识别；请重新发送语音或文字')


def prepare(db, channel, connection):
    from .security import decrypt
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == channel.id,
            ChannelInbound.status == 'ingesting').order_by(ChannelInbound.created_at, ChannelInbound.id).limit(100)):
        for row in rows(db, incoming.id):
            if row.kind != 'record' or row.status in ('downloaded', 'failed', 'cancelled'):
                continue
            if not source_valid(db, incoming):
                cancel(db, incoming, '语音来源或身份已失效')
                incoming.status = 'cancelled'
                break
            media = db.get(Media, row.media_id) if row.media_id else None
            if not media or media.status in ('deleted', 'failed'):
                fail(db, incoming, '语音附件已删除或不可用，未继续识别')
                break
            try:
                from .media_policy import current, check_types
                check_types(current(db, incoming.workspace_id, lock=True), ['.wav'])
                if row.audio_state:
                    valid_snapshot(db, channel, row)
                    if row.audio_state.get('phase') == 'requested' and (
                            row.audio_state.get('connection') != connection or
                            row.audio_state.get('sent_at', 0) + CONVERSION_TTL < now()):
                        raise HTTPException(422, '取音频超时或连接变化，未自动重试；请重新发送语音或文字')
                    continue
                if row.expires_at < now():
                    raise HTTPException(422, '语音缓存标识已过期，请重新发送')
                _, _, _, pin = snapshot(db, channel)
                key = record_id(decrypt(row.encrypted_url))
                echo = connection + ':record:' + uid()
                row.audio_state = pin | {'phase': 'requested', 'connection': connection, 'echo': echo, 'sent_at': now()}
                row.status = 'converting'
                return {'action': 'get_record', 'params': {'file': key, 'out_format': 'wav'}, 'echo': echo}
            except HTTPException as exc:
                fail(db, incoming, str(exc.detail))
                break
    return None


def receive(db, channel, connection, event):
    echo = event.get('echo')
    if not isinstance(echo, str) or not echo.startswith(connection + ':record:'):
        return
    # The socket is authenticated; still bind each response to the exact pending
    # request, source version and connection. Late/duplicate replies do no IO.
    for incoming in db.scalars(select(ChannelInbound).where(ChannelInbound.channel_id == channel.id,
                                                           ChannelInbound.status == 'ingesting')):
        for row in rows(db, incoming.id):
            state = row.audio_state
            if row.kind != 'record' or state.get('phase') != 'requested' or state.get('echo') != echo:
                continue
            if not source_valid(db, incoming):
                cancel(db, incoming, '语音来源或身份已失效')
                incoming.status = 'cancelled'
                return
            try:
                valid_snapshot(db, channel, row)
                if state.get('connection') != connection or state['sent_at'] + CONVERSION_TTL < now():
                    raise ValueError()
                data = event.get('data')
                encoded = data.get('base64') if isinstance(data, dict) else None
                if (type(event.get('retcode')) is not int or event['retcode'] != 0 or
                        not isinstance(encoded, str) or len(encoded) > (MAX_AUDIO+2)//3*4):
                    raise ValueError()
                audio = base64.b64decode(encoded, validate=True)
                wav_check(audio)
                media = db.scalar(select(Media).where(Media.id == row.media_id).with_for_update())
                others = [db.get(Media, r.media_id) for r in rows(db, incoming.id) if r.id != row.id and r.media_id]
                if (not media or media.status != 'fetching' or
                        len(audio) + sum(m.size for m in others if m) > MAX_TOTAL):
                    raise ValueError()
                from .media_policy import current, check_sizes, check_types
                policy = current(db, incoming.workspace_id, lock=True)
                check_types(policy, ['.wav']+[m.suffix for m in others if m])
                check_sizes(policy, [m.size if m else 0 for m in others]+[len(audio)])
                temporary = storage(uid(), 'blob')
                try:
                    with temporary.open('xb') as out:
                        out.write(audio)
                    temporary.replace(storage(media.id, 'blob'))
                finally:
                    temporary.unlink(missing_ok=True)
                media.size, media.digest, media.status = len(audio), hashlib.sha256(audio).hexdigest(), 'transcribing'
                row.status, row.encrypted_url = 'transcribing', ''
                row.audio_state = {k: v for k, v in state.items() if k not in ('echo', 'connection')} | {'phase': 'received'}
                from .memory import enqueue
                enqueue(db, 'channel-audio:' + row.id, 'channel_audio', {'inbound_id': incoming.id, 'attachment_id': row.id})
                channel.capabilities = channel.capabilities | {'record_receive': 'wav_received'}
            except (ValueError, binascii.Error, HTTPException, OSError):
                fail(db, incoming, '语音转换失败或格式超限；只接收 0.1–60 秒、6 MiB 内的完整 PCM WAV，请改用文字')
            return


def transcribe(job):
    """Commit the paid-call boundary before ASR. A recovered lease never repeats it."""
    from .worker import owns, Cancelled
    try:
        with SessionLocal.begin() as db:
            pre = db.get(ChannelInbound, job.payload['inbound_id'])
            if not pre:
                return
            source = locked_source(db, job)
            if not source:
                return
            incoming, channel = source
            # Gateway takes channel then owner; use the same order, and the
            # shared browser/QQ budget. No network with either lock held.
            owner = db.scalar(select(User).where(User.id == pre.user_id).with_for_update())
            row = db.get(ChannelAttachment, job.payload['attachment_id'])
            if not row or row.inbound_id != incoming.id or row.kind != 'record':
                raise Cancelled()
            if row.status == 'downloaded':
                return
            if row.audio_state.get('phase') == 'calling':
                fail(db, incoming, '语音识别调用被中断，结果未知；未重复付费调用，请改发文字或重新发送')
                return
            if row.audio_state.get('phase') != 'received':
                raise Cancelled()
            config, provider, model = valid_snapshot(db, channel, row)
            media = db.scalar(select(Media).where(Media.id == row.media_id).with_for_update())
            if not media or media.status != 'transcribing':
                raise HTTPException(409, '语音附件已删除或失效')
            from .media_policy import check_items
            check_items(db, incoming.workspace_id, [media], lock=True)
            raw = storage(media.id, 'blob').read_bytes()
            wav_check(raw)
            reserve_audio(db, owner, commit=False)
            row.audio_state = row.audio_state | {'phase': 'calling'}
        transcript = audio_call(config, provider, 'asr', model, data=raw)
        # Keep the transcript as an unconfirmed attachment source, never an
        # automatic user-fact candidate or authority to execute a tool.
        with SessionLocal.begin() as db:
            pre = db.get(ChannelInbound, job.payload['inbound_id'])
            source = locked_source(db, job)
            if not source:
                return
            incoming, channel = source
            db.scalar(select(User).where(User.id == pre.user_id).with_for_update())
            row = db.get(ChannelAttachment, job.payload['attachment_id'])
            valid_snapshot(db, channel, row)
            media = db.scalar(select(Media).where(Media.id == row.media_id).with_for_update())
            if not media or media.status != 'transcribing':
                raise HTTPException(409, '语音附件已删除或失效')
            media.parsed = {'kind': 'audio', 'parts': [{'location': 'QQ语音转写（未经确认）', 'text': transcript}],
                'notes': ['自动识别可能有误；不作为已确认的个人事实或授权，不自动写入长期记忆。',
                          '此附件保存协议端转换后的 PCM WAV，不保存原始 SILK／AMR 文件。'],
                'asr': {k: row.audio_state[k] for k in ('provider_id', 'provider_version', 'model', 'voice_version')}}
            media.status, row.status = 'ready', 'downloaded'
            row.audio_state = row.audio_state | {'phase': 'completed'}
    except Cancelled:
        raise
    except Exception:
        # Upstream bodies, paths and raw transcripts never become error logs.
        with SessionLocal.begin() as db:
            owns(db, job)
            source = locked_source(db, job)
            if source:
                fail(db, source[0], '语音识别失败、限流或配置已失效；未自动重试，未忽略语音作答，请改用文字')


def retrieval_query(db, message):
    """Search with ASR content without copying it into durable user assertions."""
    from .media import MediaLink
    texts = []
    for item in db.scalars(select(Media).join(MediaLink, MediaLink.media_id == Media.id).where(MediaLink.message_id == message.id)):
        if item.status == 'ready' and item.parsed.get('kind') == 'audio':
            texts.extend(p['text'] for p in item.parsed.get('parts', []))
    return '\n'.join([message.content] + texts)[:18000]
