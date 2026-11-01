"""Optional short QQ speech replies. No browser playback or proactive TTS.

Text is always delivered independently. Commit a paid-call fence before TTS;
never retry a call whose result is unknown. Store only an encrypted, bounded,
transient WAV and erase it at send intent, invalidation or expiry.
"""
import base64
import hashlib

from fastapi import HTTPException
from sqlalchemy import select

from .db import SessionLocal, now, uid
from .channels import Channel, ChannelInbound, ChannelDelivery
from .models import ChatRun, Conversation, Message, Memory, User
from .security import encrypt, decrypt
from .voice import (config_row, audio_provider, speech_snapshot, voice_checked,
                    live_persona, effective_config, reserve_audio, audio_call, speech_parts, wav_check)

MAX_TEXT = 600


def discard(output, reason, phase='cancelled'):
    output.encrypted_audio = None
    if output.status not in ('sending', 'unknown', 'accepted_by_protocol'):
        output.speech = output.speech | {'phase': phase}
    if output.status == 'pending':
        output.status, output.error = 'cancelled', reason


def finish(db, incoming):
    if not incoming:
        return
    parts = list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.inbound_id == incoming.id)))
    if parts and all(p.status == 'accepted_by_protocol' or
                     (p.kind == 'record' and p.status == 'cancelled') for p in parts):
        if incoming.status not in ('cancelled', 'failed'):
            incoming.status = 'completed' if not incoming.error else 'failed'


def snapshot(db, channel, run):
    owner = db.get(User, channel.user_id)
    config = config_row(db, owner) if owner else None
    if not config or not config.spec.get('enabled'):
        raise HTTPException(409, '语音配置未启用；本次仅保留文字')
    provider, model = audio_provider(db, config, 'tts')
    speech = speech_snapshot(config, run)
    if speech.get('source') == 'persona':
        live_persona(db, speech['persona_id'], owner)
    if not voice_checked(db, config, speech['voice']):
        raise HTTPException(409, '本轮音色或 TTS 未验证；本次仅保留文字')
    pin = speech | {'voice_version': config.version, 'provider_id': provider.id,
                    'provider_version': provider.version, 'model': model}
    return config, provider, model, pin


def source(db, output):
    """Recheck authoritative sources before synthesis, after it and at send intent."""
    from .channel_gateway import channel_scope_valid
    from .memory import usable
    from .media import assert_media_valid
    incoming = db.get(ChannelInbound, output.inbound_id, populate_existing=True)
    channel = db.get(Channel, output.channel_id, populate_existing=True)
    if channel:
        db.scalar(select(User).where(User.id == channel.user_id).with_for_update())
    run = db.get(ChatRun, incoming.run_id, populate_existing=True) if incoming else None
    conversation = db.get(Conversation, run.conversation_id, populate_existing=True) if run else None
    if (not incoming or not channel or incoming.status != 'ready' or incoming.created_at < now()-3600
            or not channel.rules.get('voice_reply_enabled') or output.status != 'pending'
            or not run or run.status != 'completed' or run.cancel_requested
            or not conversation or conversation.deleted or conversation.workspace_id != channel.workspace_id
            or conversation.user_id != run.user_id or output.user_id != channel.user_id
            or output.workspace_id != channel.workspace_id or incoming.workspace_id != channel.workspace_id
            or not channel_scope_valid(db, {'id': channel.id, 'version': incoming.channel_version,
                'binding_id': incoming.binding_id, 'binding_version': incoming.binding_version})):
        raise HTTPException(409, '语音回复来源、渠道或身份已失效')
    for ref in run.memory_refs:
        memory = db.get(Memory, ref['id'], populate_existing=True)
        if not usable(memory) or memory.version != ref['version']:
            raise HTTPException(409, '语音回复记忆依据已失效')
    for identity in run.source_message_ids:
        message = db.get(Message, identity, populate_existing=True)
        if not message or message.status in ('deleted', 'redacted', 'invalidated', 'superseded'):
            raise HTTPException(409, '语音回复对话依据已失效')
    assert_media_valid(db, run)
    message = db.scalar(select(Message).where(Message.run_id == run.id, Message.role == 'assistant', Message.status == 'completed'))
    if not message or hashlib.sha256(message.content.encode()).hexdigest() != output.speech.get('digest'):
        raise HTTPException(409, '语音回复内容已变化')
    config, provider, model, pin = snapshot(db, channel, run)
    if any(output.speech.get(k) != v for k, v in pin.items()):
        raise HTTPException(409, '语音服务、人格或音色配置已变化；本次仅保留文字')
    return incoming, config, provider, model, message


def queue(db, channel, incoming, run, message):
    if not channel.rules.get('voice_reply_enabled') or not message:
        return
    db.flush()
    part = len(list(db.scalars(select(ChannelDelivery.id).where(ChannelDelivery.inbound_id == incoming.id))))
    output = ChannelDelivery(user_id=incoming.user_id, workspace_id=incoming.workspace_id,
        channel_id=channel.id, inbound_id=incoming.id, part=part, kind='record', content='', echo=uid(),
        speech={'phase': 'queued', 'digest': hashlib.sha256(message.content.encode()).hexdigest()})
    db.add(output)
    db.flush()
    try:
        text = spoken(message.content)
        _, _, _, pin = snapshot(db, channel, run)
        output.speech = output.speech | pin | {'characters': len(text)}
        from .memory import enqueue
        enqueue(db, 'channel-speech:' + output.id, 'channel_speech', {'delivery_id': output.id})
    except HTTPException as exc:
        discard(output, str(exc.detail), 'skipped')


def spoken(content):
    # The full reply stays in text. Never silently speak a truncated excerpt.
    if len(content) > MAX_TEXT:
        raise HTTPException(409, '回复超过 600 字语音预算；完整内容保留为文字，未截断合成')
    text = ''.join(speech_parts(content)).strip()
    if not text:
        raise HTTPException(409, '回复没有可合成的文字；本次仅保留文字')
    return text


def locked(db, job):
    from .worker import owns
    pre = db.get(ChannelDelivery, job.payload['delivery_id'])
    if not pre or pre.kind != 'record':
        return None
    db.scalar(select(Channel).where(Channel.id == pre.channel_id).with_for_update())
    owns(db, job)
    return db.scalar(select(ChannelDelivery).where(ChannelDelivery.id == pre.id)
                     .with_for_update().execution_options(populate_existing=True))


def synthesize(job):
    from .worker import owns, Cancelled
    try:
        with SessionLocal.begin() as db:
            output = locked(db, job)
            if not output or output.status != 'pending' or output.speech.get('phase') == 'ready':
                return
            incoming, config, provider, model, message = source(db, output)
            if output.speech.get('phase') != 'queued':
                discard(output, '合成调用曾被中断，结果未知；未重复付费调用，文字仍保留', 'unknown')
                finish(db, incoming)
                return
            owner = db.scalar(select(User).where(User.id == output.user_id).with_for_update())
            reserve_audio(db, owner, commit=False)
            text, voice = spoken(message.content), output.speech['voice']
            output.speech = output.speech | {'phase': 'calling'}
        raw = audio_call(effective_config(config, voice), provider, 'tts', model, text=text, channel='qq')
        wav_check(raw)
        with SessionLocal.begin() as db:
            output = locked(db, job)
            if not output or output.status != 'pending' or output.speech.get('phase') != 'calling':
                return
            source(db, output)
            output.encrypted_audio = encrypt(base64.b64encode(raw).decode('ascii'))
            output.speech = output.speech | {'phase': 'ready', 'bytes': len(raw)}
    except Cancelled:
        raise
    except Exception:
        with SessionLocal.begin() as db:
            owns(db, job)
            output = locked(db, job)
            if output and output.status == 'pending':
                discard(output, '语音合成失败、限流或依据已失效；未自动重试，文字仍保留', 'failed')
                finish(db, db.get(ChannelInbound, output.inbound_id))


def segment(db, output):
    if output.speech.get('phase') != 'ready' or not output.encrypted_audio:
        return None
    source(db, output)
    value = decrypt(output.encrypted_audio)
    raw = base64.b64decode(value, validate=True)
    wav_check(raw)
    # Send intent is committed by the gateway before network I/O; a lost socket
    # never retries this waveform. Protocol acceptance never means heard/read.
    output.encrypted_audio = None
    output.speech = output.speech | {'phase': 'submitted'}
    return {'type': 'record', 'data': {'file': 'base64://' + value}}


def failed(db, job):
    output = locked(db, job)
    if output and output.status == 'pending':
        discard(output, '合成作业已停止；未自动重试，文字仍保留', 'failed')
        finish(db, db.get(ChannelInbound, output.inbound_id))


def sweep():
    # Runs independently of a protocol connection. Offline pending audio must
    # expire as well; completed/unknown receipts are preserved without payload.
    with SessionLocal.begin() as db:
        ids = list(db.scalars(select(ChannelDelivery.channel_id).where(
            ChannelDelivery.kind == 'record', ChannelDelivery.created_at < now()-3600,
            ChannelDelivery.status == 'pending').distinct().limit(100)))
        for identity in ids:
            db.scalar(select(Channel).where(Channel.id == identity).with_for_update())
            for output in db.scalars(select(ChannelDelivery).where(ChannelDelivery.channel_id == identity,
                    ChannelDelivery.kind == 'record', ChannelDelivery.status == 'pending',
                    ChannelDelivery.created_at < now()-3600)):
                discard(output, '语音回复已过期；重连不补发', 'expired')
                incoming = db.get(ChannelInbound, output.inbound_id)
                if incoming:
                    finish(db, incoming)


def checkpoint(db):
    """Carry dedupe and call/send barriers past an older backup, never audio/text."""
    from .channels import delivery_view
    from .web import view
    deliveries = list(db.scalars(select(ChannelDelivery).where(ChannelDelivery.kind == 'record')))
    inbounds = {d.inbound_id for d in deliveries}
    return {'inbounds': [view(row, ('content', 'run_id')) | {'content': '', 'run_id': None,
            'status': 'cancelled', 'error': '恢复隔离：旧语音回复不重放'}
            for row in db.scalars(select(ChannelInbound).where(ChannelInbound.id.in_(inbounds)))],
        'deliveries': [delivery_view(row) | {'content': '', 'encrypted_audio': None,
            'status': 'accepted_by_protocol' if row.status == 'accepted_by_protocol' else (
                'unknown' if row.status in ('sending', 'unknown') else 'cancelled'),
            'error': None if row.status == 'accepted_by_protocol' else '恢复隔离：旧语音回复不重放'}
            for row in deliveries]}


def restore(db, data):
    for values in data.get('inbounds', []):
        db.merge(ChannelInbound(**values))
    db.flush()
    for values in data.get('deliveries', []):
        db.merge(ChannelDelivery(**values))
