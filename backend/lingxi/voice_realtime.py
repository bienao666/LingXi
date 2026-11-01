"""One-turn native transcription relay. Audio and drafts exist only in bounded RAM.

The model cannot generate chat or invoke tools through this protocol. The browser
must submit its reviewed final draft to the ordinary voice messages endpoint.
"""
import asyncio
import base64
import json
import queue
import secrets
import threading
import time
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from httpx_ws import connect_ws, WebSocketUpgradeError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, Float, Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .config import settings
from .db import Base, SessionLocal, now
from .gateway import client_for, record_call
from .models import LoginSession, Provider, User
from .network import load_proxy
from .security import authenticate, safe_error, token_hash
from .voice import (active_session, audio_provider, audio_signature, checked, config_row, reserve_audio)
from .web import Context

router = APIRouter()
MAX_FRAME = 9600  # 200 ms mono PCM16, 24 kHz
MAX_PCM = 60 * 48000
MAX_TEXT = 12000


class RealtimeTicket(Base):
    __tablename__ = 'voice_realtime_tickets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # hash only
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64))
    login_session_id: Mapped[str] = mapped_column(String(64))
    voice_session_id: Mapped[str] = mapped_column(String(64), default='')
    config_version: Mapped[int] = mapped_column(Integer)
    provider_version: Mapped[int] = mapped_column(Integer)
    validation: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(20), default='issued')
    created_at: Mapped[float] = mapped_column(Float, default=now)
    expires_at: Mapped[float] = mapped_column(Float)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(default='', max_length=64)


class TicketView(BaseModel):
    token: str
    expires_at: float
    path: str = '/api/voice/realtime'


@router.post('/api/voice/realtime-tickets', response_model=TicketView)
def issue_ticket(body: TicketInput, ctx: Context):
    db, user, login = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    config = config_row(db, user)
    provider, _ = audio_provider(db, config, 'realtime_asr')
    if not config.spec.get('enabled'):
        raise HTTPException(409, '请先启用并保存语音配置')
    if body.session_id:
        active_session(db, user, login, body.session_id)
        if not checked(db, config, 'realtime_asr'):
            raise HTTPException(409, '实时转写尚未验证或配置已变化，请先在语音配置验证')
    recent = db.scalar(select(func.count()).select_from(RealtimeTicket).where(
        RealtimeTicket.user_id == user.id, RealtimeTicket.created_at > now() - 60))
    if recent >= 30:
        raise HTTPException(429, '实时转写请求过多，请稍后重试')
    token = secrets.token_urlsafe(32)
    expiry = min(now() + 30, login.expires_at)
    db.add(RealtimeTicket(id=token_hash(token), user_id=user.id, workspace_id=user.workspace_id,
        login_session_id=login.id, voice_session_id=body.session_id, config_version=config.version,
        provider_version=provider.version, validation=not body.session_id, expires_at=expiry))
    # Remove only short-lived expired authorization records, never business data.
    for old in db.scalars(select(RealtimeTicket).where(RealtimeTicket.user_id == user.id,
            RealtimeTicket.expires_at < now() - 86400)):
        db.delete(old)
    return {'token': token, 'expires_at': expiry}


def live(db, identity, login_id):
    from .workspace_migrations import paused
    if paused(db):
        raise HTTPException(409, '服务器切换中，实时转写已停止')
    ticket = db.get(RealtimeTicket, identity, populate_existing=True)
    login = db.get(LoginSession, login_id, populate_existing=True)
    if (not ticket or ticket.status not in ('issued', 'active') or not login or ticket.login_session_id != login.id or
            ticket.user_id != login.user_id or ticket.expires_at <= now()):
        raise HTTPException(401, '实时转写授权已失效，请重新开启')
    user = db.get(User, login.user_id)
    if not user or login.expires_at <= now() or user.workspace_id != ticket.workspace_id:
        raise HTTPException(401, '登录已失效，请重新确认身份')
    config = config_row(db, user)
    provider, model = audio_provider(db, config, 'realtime_asr')
    if (not config.spec.get('enabled') or config.version != ticket.config_version or
            provider.version != ticket.provider_version):
        raise HTTPException(409, '语音配置或服务连接已变化，本段已丢弃')
    if ticket.voice_session_id:
        active_session(db, user, login, ticket.voice_session_id)
        if not checked(db, config, 'realtime_asr'):
            raise HTTPException(409, '实时转写验证已失效')
    return ticket, config, provider, model


def claim(token, cookies, headers):
    with SessionLocal.begin() as db:
        user, login = authenticate(SimpleNamespace(method='GET', cookies=cookies, headers=headers), db)
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        ticket, config, provider, model = live(db, token_hash(token), login.id)
        if ticket.status != 'issued':
            raise HTTPException(409, '实时转写授权已使用，请重新开启')
        other = db.scalar(select(RealtimeTicket.id).where(RealtimeTicket.user_id == user.id,
            RealtimeTicket.status == 'active', RealtimeTicket.expires_at > now()))
        if other:
            raise HTTPException(409, '其他窗口正在实时转写，请先结束该段')
        reserve_audio(db, user, commit=False)
        ticket.status, ticket.expires_at = 'active', min(now() + 75, login.expires_at)
        return ticket.id, login.id


def update_event(config, model):
    field = config.spec.get('realtime_language_field', 'language')
    language = config.spec['language']
    return {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
        'format': {'type': 'audio/pcm', 'rate': 24000},
        'transcription': {'model': model, field: [language] if field == 'languages' else language},
        'turn_detection': None}}}}


class TranscriptProtocol:
    """Whitelisted upstream protocol; one committed item, bounded provisional text."""
    def __init__(self):
        self.ready = False
        self.item = None
        self.text = ''
        self.pending = []

    def process(self, event, committed):
        if not isinstance(event, dict):
            raise HTTPException(502, '实时转写协议格式无效')
        kind = event.get('type')
        if kind == 'error' or kind == 'conversation.item.input_audio_transcription.failed':
            raise HTTPException(502, '实时转写服务拒绝或中断请求，请检查模型、语言格式与服务兼容性')
        if kind == 'session.updated':
            session = event.get('session', {})
            if session.get('type') != 'transcription':
                raise HTTPException(502, '服务未确认转写会话，已停止')
            self.ready = True
            return {'type': 'ready', 'sample_rate': 24000}
        if kind == 'input_audio_buffer.committed':
            item = event.get('item_id')
            if (not committed or self.item or not isinstance(item, str) or not item or len(item) > 200 or
                    (self.pending and self.pending[0] != item)):
                raise HTTPException(502, '转写音频回执不匹配')
            self.item = item
            return None
        if kind not in ('conversation.item.input_audio_transcription.delta', 'conversation.item.input_audio_transcription.completed'):
            return None
        item = event.get('item_id')
        if not isinstance(item, str) or not item or len(item) > 200:
            raise HTTPException(502, '转写来源标识无效')
        if self.item and item != self.item:
            raise HTTPException(502, '转写来源与音频回执不匹配')
        value = event.get('transcript' if kind.endswith('.completed') else 'delta')
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise HTTPException(502, '转写内容超过预算或格式无效')
        if kind.endswith('.completed'):
            if not committed or not self.item or item != self.item or not value.strip():
                raise HTTPException(502, '最终转写缺少匹配的音频回执或有效内容')
            return {'type': 'completed', 'text': value.strip(), 'temporary': True}
        # Some live models produce deltas before commit. These are display-only;
        # final completion must still match the acknowledged committed item.
        if self.pending and self.pending[0] != item:
            raise HTTPException(502, '转写包含多个不匹配的音频片段')
        self.pending = [item]
        self.text += value
        if len(self.text) > MAX_TEXT:
            raise HTTPException(502, '实时字幕超过预算')
        return {'type': 'partial', 'text': self.text, 'temporary': True}


def relay(identity, login_id, incoming, outgoing, stop):
    started, status, error, config_snapshot = time.monotonic(), 'failed', None, None
    def emit(event):
        try:
            outgoing.put(event, timeout=.2)
        except queue.Full:
            raise HTTPException(429, '浏览器接收过慢，实时转写已停止') from None
    try:
        with SessionLocal() as db:
            ticket, config, provider, model = live(db, identity, login_id)
            config_snapshot = {'provider_id': provider.id, 'provider_version': provider.version,
                'model': model, 'voice_config_version': config.version, 'validation': ticket.validation,
                'audio_transport': 'realtime'}
            from .audio_costs import snapshot
            config_snapshot['_audio_price_snapshot'] = snapshot(config, provider, model, 'realtime_asr')
            config_snapshot['audio_channel'] = 'voice'
            config_snapshot['audio_measurement'] = {'transport': 'realtime', 'input_frames': 0, 'input_sample_rate': 24000}
            initial = update_event(config, model)
            proxy_version = load_proxy().version
            # Use the same pinned DNS/TLS transport and HTTP(S)/SOCKS proxy as
            # other model calls. HTTPX-WS upgrades this checked HTTP(S) request.
            from .model_budgets import client as budget_client
            with budget_client(provider, client_for, config_snapshot) as client:
                url = str(client.base_url.join('realtime')) + '?intent=transcription'
                with connect_ws(url, client, max_message_size_bytes=128 * 1024, queue_size=16) as upstream:
                    upstream.send_json(initial)
                    protocol, size, committed = TranscriptProtocol(), 0, False
                    next_check, ready_at, deadline = 0, None, started + 15
                    while not stop.is_set():
                        clock = time.monotonic()
                        if clock > deadline:
                            raise HTTPException(504, '实时转写等待超时，本段已停止；不会自动重试')
                        if clock >= next_check:
                            db.rollback()
                            live(db, identity, login_id)
                            if load_proxy().version != proxy_version:
                                raise HTTPException(409, '系统代理已变化，请重新开启实时转写')
                            db.rollback()
                            next_check = clock + 1
                        try:
                            frame = incoming.get_nowait()
                        except queue.Empty:
                            frame = None
                        if frame is not None:
                            if not protocol.ready or committed:
                                raise HTTPException(422, '实时转写尚未就绪或已结束输入')
                            if isinstance(frame, bytes):
                                size += len(frame)
                                if (not frame or len(frame) > MAX_FRAME or len(frame) % 2 or size > MAX_PCM or
                                        size > (clock - ready_at + 2) * 48000):
                                    raise HTTPException(422, '音频须为有界的 24 kHz 单声道 PCM16 流')
                                upstream.send_json({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(frame).decode()})
                                config_snapshot['audio_measurement']['input_frames'] += len(frame) // 2
                            elif frame == 'commit':
                                if size < 4800:
                                    raise HTTPException(422, '录音过短，请至少说一句话')
                                upstream.send_json({'type': 'input_audio_buffer.commit'})
                                committed, deadline = True, min(started + 73, clock + 15)
                            else:
                                raise HTTPException(422, '实时转写不支持此操作')
                        try:
                            event = upstream.receive_json(timeout=.02)
                        except queue.Empty:
                            continue
                        output = protocol.process(event, committed)
                        if not output:
                            continue
                        if output['type'] == 'ready':
                            if ready_at is not None:
                                raise HTTPException(502, '服务重复确认会话')
                            ready_at, deadline = clock, started + 70
                        if output['type'] == 'completed':
                            db.rollback()
                            owner = db.get(RealtimeTicket, identity)
                            db.scalar(select(User).where(User.id == owner.user_id).with_for_update())
                            ticket, current, current_provider, _ = live(db, identity, login_id)
                            db.scalar(select(Provider).where(Provider.id == current_provider.id).with_for_update())
                            db.refresh(current_provider)
                            if current_provider.version != ticket.provider_version or not current_provider.enabled:
                                raise HTTPException(409, '转写连接已变化，定稿已丢弃')
                            if ticket.validation:
                                current.checks = current.checks | {'realtime_asr': audio_signature(current, current_provider, 'realtime_asr')}
                            ticket.status = 'completed'
                            db.commit()
                            status = 'completed'
                            emit(output)
                            return
                        emit(output)
                    status = 'cancelled'
    except Exception as exc:
        # Upgrade exceptions may carry request credentials and upstream bodies.
        error = safe_error(exc)
        if isinstance(exc, WebSocketUpgradeError):
            error = f'实时转写接口返回 HTTP {exc.response.status_code}，请检查服务兼容性及授权'
        if not stop.is_set():
            try:
                emit({'type': 'error', 'message': error, 'status': exc.status_code if isinstance(exc, HTTPException) else 502})
            except Exception:
                pass
    finally:
        with SessionLocal.begin() as db:
            ticket = db.get(RealtimeTicket, identity)
            if ticket and ticket.status == 'active':
                ticket.status = status
        if config_snapshot:
            record_call('voice:asr', 'asr', started, status, {}, error, config=config_snapshot)
        stop.set()


@router.websocket('/api/voice/realtime')
async def realtime_socket(socket: WebSocket):
    # No cross-origin sockets, query tokens, provider credentials or model events.
    if socket.headers.get('origin') != settings().public_origin or socket.query_params:
        await socket.close(code=1008)
        return
    await socket.accept()
    incoming, outgoing, stop = queue.Queue(16), queue.Queue(16), threading.Event()
    thread = None
    receiver = None
    try:
        first = await asyncio.wait_for(socket.receive_text(), 5)
        if len(first) > 256:
            raise HTTPException(403, '实时转写授权无效')
        data = json.loads(first)
        if not isinstance(data, dict) or set(data) != {'token'} or not isinstance(data['token'], str):
            raise HTTPException(403, '实时转写授权无效')
        identity, login = await asyncio.to_thread(claim, data['token'], socket.cookies, socket.headers)
        thread = threading.Thread(target=relay, args=(identity, login, incoming, outgoing, stop), daemon=True)
        thread.start()
        receiver = asyncio.create_task(socket.receive())
        while True:
            while not outgoing.empty():
                event = outgoing.get_nowait()
                await asyncio.wait_for(socket.send_json(event), 2)
                if event['type'] in ('completed', 'error'):
                    return
            if stop.is_set():
                return
            done, _ = await asyncio.wait({receiver}, timeout=.02)
            if not done:
                continue
            event = receiver.result()
            receiver = asyncio.create_task(socket.receive())
            if event['type'] == 'websocket.disconnect':
                return
            if event.get('bytes') is not None:
                frame = event['bytes']
                if len(frame) > MAX_FRAME:
                    raise HTTPException(422, '音频分片超过预算')
            elif event.get('text') is not None and len(event['text']) <= 128:
                if json.loads(event['text']) != {'type': 'commit'}:
                    raise HTTPException(422, '仅允许结束录音指令')
                frame = 'commit'
            else:
                raise HTTPException(422, '仅允许音频输入与结束录音')
            try:
                incoming.put_nowait(frame)
            except queue.Full:
                raise HTTPException(429, '音频上传过快，已停止本段') from None
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await socket.send_json({'type': 'error', 'message': safe_error(exc), 'status': exc.status_code if isinstance(exc, HTTPException) else 502})
        except Exception:
            pass
    finally:
        stop.set()
        if receiver:
            receiver.cancel()
            try:
                await receiver
            except (asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
                pass
        try:
            await socket.close()
        except RuntimeError:
            pass
