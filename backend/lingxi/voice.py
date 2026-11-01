"""Explicit browser voice sessions; temporary PCM never enters persistent storage.

Speech is rendered from owned, completed core messages. Playback acknowledgments
are conservative sentence boundaries, not estimates derived from audio duration.
"""
import hashlib
import io
import re
import time
import wave
from types import SimpleNamespace
from typing import Literal
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, Float, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now, uid
from .gateway import client_for, record_call
from .models import Audit, ChatRun, Conversation, LoginSession, Message, Provider, User
from .schemas import ChatRunView
from .security import safe_error
from .web import Context, audit, owned, view

router = APIRouter()
MAX_AUDIO = 6 * 1024**2
TTL = 15 * 60
HEARTBEAT_TTL = 60


class VoiceConfig(Base):
    __tablename__ = 'voice_config'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), default='personal')
    version: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[dict] = mapped_column(JSON, default=dict)


class VoiceSession(Base):
    __tablename__ = 'voice_sessions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(String(64), default='personal')
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    login_session_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    config_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='active')
    created_at: Mapped[float] = mapped_column(Float, default=now)
    expires_at: Mapped[float] = mapped_column(Float)
    heartbeat_at: Mapped[float] = mapped_column(Float, default=now)
    mode: Mapped[str] = mapped_column(String(20), default='manual')
    allow_proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic_turns: Mapped[int] = mapped_column(Integer, default=0)


class VoicePlayback(Base):
    __tablename__ = 'voice_playbacks'
    __table_args__ = (UniqueConstraint('session_id', 'message_id'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    workspace_id: Mapped[str] = mapped_column(String(64), default='personal')
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    confirmed_parts: Mapped[int] = mapped_column(Integer, default=0)
    offered_parts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default='active')


class AudioModel(BaseModel):
    provider_id: str = Field(default='', max_length=64)
    model: str = Field(default='', max_length=150)


class VoiceSettings(BaseModel):
    enabled: bool = False
    asr: AudioModel = Field(default_factory=AudioModel)
    tts: AudioModel = Field(default_factory=AudioModel)
    realtime_asr: AudioModel = Field(default_factory=AudioModel)
    realtime_tts: AudioModel = Field(default_factory=AudioModel)
    realtime_language_field: Literal['language', 'languages'] = 'language'
    language: str = Field(default='zh', pattern=r'^[a-z]{2,3}$')
    voice: str = Field(default='alloy', min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_-]+$')
    speed: float = Field(default=1, ge=.25, le=4)
    follow_persona: bool = False
    version: int = Field(default=0, ge=0)


class VoiceSettingsView(VoiceSettings):
    checks: dict[str, bool] = Field(default_factory=dict)


class SessionInput(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    mode: Literal['manual', 'continuous'] = 'manual'
    allow_proactive: bool = False


class VoiceSessionView(BaseModel):
    id: str
    conversation_id: str
    status: str
    expires_at: float
    config_version: int
    mode: Literal['manual', 'continuous']
    automatic_turns: int
    allow_proactive: bool = False


class TranscriptView(BaseModel):
    text: str
    temporary: bool = True


class VoiceMessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    request_id: str = Field(min_length=8, max_length=100)
    automatic: bool = False


class PlaybackInput(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)


class PlaybackAck(BaseModel):
    completed_parts: int = Field(ge=0)
    stop: bool = False


class PlaybackView(BaseModel):
    proactive: bool = False
    contact_id: str | None = None
    id: str
    message_id: str
    parts: list[str]
    confirmed_parts: int
    status: str
    voice: str = ''
    voice_source: str = 'system'
    persona_name: str = ''
    persona_version: int = 0


class PersonaVoiceView(BaseModel):
    id: str
    name: str
    version: int
    voice: str
    enabled: bool
    verified: bool
    issue: str = ''


class PersonaVoiceTest(BaseModel):
    version: int = Field(ge=1)


def valid_voice(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value))


def effective_config(config, voice):
    # A detached projection: never overwrite the persisted system default.
    return SimpleNamespace(spec=config.spec | {'voice': voice}, version=config.version, workspace_id=config.workspace_id)


def voice_key(voice):
    return 'tts_voice:' + hashlib.sha256(voice.encode()).hexdigest()


def voice_checked(db, config, voice):
    if not config or not valid_voice(voice):
        return False
    if voice == config.spec.get('voice'):
        return checked(db, config, 'tts')
    try:
        provider, _ = audio_provider(db, config, 'tts')
        return config.checks.get(voice_key(voice)) == audio_signature(effective_config(config, voice), provider, 'tts')
    except HTTPException:
        return False


def speech_snapshot(config, run):
    persona = run.config.get('persona') or {}
    voice = persona.get('spec', {}).get('voice', '') if config.spec.get('follow_persona', False) else ''
    return {'voice': voice or config.spec['voice'], 'source': 'persona' if voice else 'system',
            'persona_id': persona.get('id') if voice else None,
            'persona_name': persona.get('name', '') if voice else '',
            'persona_version': persona.get('version', 0) if voice else 0}


def playback_speech(db, playback, user, config, lock=False):
    message = db.get(Message, playback.message_id)
    run = db.get(ChatRun, message.run_id) if message and message.run_id else None
    # Existing pre-feature rounds keep their original system-default behavior.
    from .proactive_voice import entry_for
    entry = entry_for(db, playback)
    speech = entry.speech if entry else (run.config.get('speech') or {'voice': config.spec['voice'], 'source': 'system'})
    if speech.get('source') == 'persona':
        live_persona(db, speech.get('persona_id'), user, lock)
    if not voice_checked(db, config, speech.get('voice')):
        raise HTTPException(409, '本轮音色未验证或验证已失效，请在语音配置验证该音色；文字回复仍保留')
    return speech


def live_persona(db, identity, user, lock=False):
    from .assistants import AssistantProfile
    query = select(AssistantProfile).where(AssistantProfile.id == identity,
        AssistantProfile.user_id == user.id, AssistantProfile.workspace_id == user.workspace_id,
        AssistantProfile.kind == 'persona').execution_options(populate_existing=True)
    persona = db.scalar(query.with_for_update() if lock else query)
    if not persona:
        raise HTTPException(404, '人格不存在')
    if not persona.enabled:
        raise HTTPException(409, '人格已停用，停止音色验证及播报')
    return persona


def playback_view(db, playback, parts):
    message = db.get(Message, playback.message_id)
    run = db.get(ChatRun, message.run_id) if message and message.run_id else None
    from .proactive_voice import entry_for
    entry = entry_for(db, playback)
    speech = entry.speech if entry else (run.config.get('speech') or {})
    return view(playback) | {'proactive': bool(entry), 'contact_id': entry.contact_id if entry else None, 'parts': parts, 'voice': speech.get('voice', ''),
        'voice_source': speech.get('source', 'system'), 'persona_name': speech.get('persona_name', ''),
        'persona_version': speech.get('persona_version', 0)}


def config_row(db, user):
    row = db.get(VoiceConfig, user.id)
    if row and row.workspace_id != user.workspace_id:
        raise HTTPException(404, '语音配置不存在')
    return row


def audio_provider(db, row, kind):
    spec = row.spec.get(kind, {}) if row else {}
    provider = db.get(Provider, spec.get('provider_id', ''))
    if (not provider or not provider.enabled or provider.workspace_id != row.workspace_id or
            not spec.get('model', '').strip()):
        raise HTTPException(409, '请先配置并启用语音模型的服务连接')
    from .providers import protocol_for, require_capability
    require_capability(protocol_for(provider, db), 'asr' if kind == 'realtime_asr' else 'tts' if kind == 'realtime_tts' else kind)
    return provider, spec['model']


def audio_signature(row, provider, kind):
    keys = (('realtime_asr', 'language', 'realtime_language_field') if kind == 'realtime_asr' else
            ('realtime_tts', 'voice', 'speed') if kind == 'realtime_tts' else
            ('asr', 'language') if kind == 'asr' else ('tts', 'voice', 'speed'))
    return hashlib.sha256(repr(([row.spec.get(k) for k in keys], provider.version)).encode()).hexdigest()


def reserve_audio(db, user, *, commit=True):
    # Bound paid upstream requests across tabs and processes. No audio or transcript in audit.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    count = db.scalar(select(func.count()).select_from(Audit).where(Audit.user_id == user.id,
                      Audit.action == 'voice_request', Audit.created_at > now()-60))
    if count >= 30:
        raise HTTPException(429, '语音请求过多，请一分钟后重试或阅读文字')
    audit(db, user, 'voice_request')
    if commit:
        db.commit()


def checked(db, row, kind):
    try:
        provider, _ = audio_provider(db, row, kind)
        return row.checks.get(kind) == audio_signature(row, provider, kind)
    except HTTPException:
        return False


@router.get('/api/voice/config', response_model=VoiceSettingsView)
def get_config(ctx: Context):
    db, user, _ = ctx
    row = config_row(db, user)
    kinds = ['asr', 'tts']
    if row:
        for optional in ('realtime_asr', 'realtime_tts'):
            if row.spec.get(optional, {}).get('model'):
                kinds.append(optional)
    return VoiceSettingsView(**(row.spec if row else {}), version=row.version if row else 0,
                             checks={k: checked(db, row, k) for k in kinds})


@router.put('/api/voice/config', response_model=VoiceSettingsView)
def put_config(body: VoiceSettings, ctx: Context):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = config_row(db, user)
    if body.version != (row.version if row else 0):
        raise HTTPException(409, '语音配置已变化，请重新加载')
    spec = body.model_dump(exclude={'version'})
    # Preserve existing validation hashes on a no-op roundtrip (1 vs 1.0).
    if row and spec['speed'] == row.spec.get('speed'):
        spec['speed'] = row.spec['speed']
    for kind in ('asr', 'tts', 'realtime_asr', 'realtime_tts'):
        if spec[kind]['provider_id']:
            candidate = db.get(Provider, spec[kind]['provider_id'])
            if not candidate or candidate.workspace_id != user.workspace_id:
                raise HTTPException(422, '语音服务连接不存在')
            from .providers import protocol_for, require_capability
            require_capability(protocol_for(db.get(Provider, spec[kind]['provider_id']), db), 'asr' if kind == 'realtime_asr' else 'tts' if kind == 'realtime_tts' else kind)
        spec[kind]['model'] = spec[kind]['model'].strip()
    if not row:
        row = VoiceConfig(id=user.id, workspace_id=user.workspace_id, spec=spec)
        db.add(row)
    else:
        row.spec, row.version = spec, row.version + 1
    audit(db, user, 'voice_config', user.id)
    db.flush()
    return get_config(ctx)


def wav_check(data):
    if not data or len(data) > MAX_AUDIO or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise HTTPException(422, '音频须为不超过 6 MiB 的 PCM WAV')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels() not in (1, 2) or audio.getsampwidth() != 2 or
                    not 8000 <= audio.getframerate() <= 48000 or
                    not .1 <= frames / audio.getframerate() <= 60 or
                    len(audio.readframes(frames)) != frames * audio.getnchannels() * 2):
                raise ValueError()
    except (ValueError, EOFError, RuntimeError, wave.Error):
        raise HTTPException(422, '音频须为完整的 16 位 PCM WAV，时长 0.1–60 秒') from None


async def read_audio(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/octet-stream':
        raise HTTPException(415, '请上传 WAV 二进制内容')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_AUDIO:
            raise HTTPException(413, '语音片段超过 6 MiB，请缩短录音')
    wav_check(data)
    return bytes(data)


def audio_call(row, provider, kind, model, data=None, text=None, validation=False):
    started, status, error, usage = time.monotonic(), 'failed', None, {}
    try:
        with client_for(provider) as client:
            if kind == 'asr':
                import json
                with client.stream('POST', 'audio/transcriptions', data={'model': model, 'language': row.spec['language'],
                                   'response_format': 'json'}, files={'file': ('speech.wav', data, 'audio/wav')}) as result:
                    result.raise_for_status()
                    raw = bytearray()
                    for chunk in result.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 128 * 1024:
                            raise HTTPException(502, '转写响应超过预算，请缩短录音')
                    obj = json.loads(raw)
                value = obj.get('text')
                usage = obj.get('usage') or {}
                if not isinstance(value, str) or not value.strip() or len(value) > 12000:
                    raise HTTPException(502, '转写未返回有效文字，请重录或改用文字输入')
                value = value.strip()
            else:
                with client.stream('POST', 'audio/speech', json={'model': model, 'input': text,
                        'voice': row.spec['voice'], 'speed': row.spec['speed'], 'response_format': 'wav'}) as result:
                    result.raise_for_status()
                    chunks, size = [], 0
                    for chunk in result.iter_bytes():
                        size += len(chunk)
                        if size > MAX_AUDIO:
                            raise HTTPException(502, '合成音频超过预算，请减少单句长度')
                        chunks.append(chunk)
                    value = b''.join(chunks)
                wav_check(value)
        status = 'completed'
        return value
    except Exception as exc:
        error = safe_error(exc)
        raise HTTPException(502, error) from None
    finally:
        record_call('voice:' + kind, kind, started, status, usage, error,
                    config={'provider_id': provider.id, 'provider_version': provider.version,
                            'model': model, 'voice_config_version': row.version, 'validation': validation,
                            **({'voice': row.spec['voice'], 'speed': row.spec['speed']} if kind == 'tts' else {})})


def recheck_config(db, user, version, kind, provider_version):
    db.expire_all()
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    row = config_row(db, user)
    provider, _ = audio_provider(db, row, kind)
    db.scalar(select(Provider).where(Provider.id == provider.id).with_for_update())
    db.refresh(provider)
    if row.version != version or provider.version != provider_version:
        raise HTTPException(409, '语音配置或连接已变化，本次结果已丢弃')
    return row


@router.post('/api/voice/config/test-asr', response_model=TranscriptView)
async def test_asr(request: Request, ctx: Context):
    import starlette.concurrency
    db, user, login = ctx
    data = await read_audio(request)
    row = config_row(db, user)
    provider, model = audio_provider(db, row, 'asr')
    version, pv = row.version, provider.version
    reserve_audio(db, user)
    result = await starlette.concurrency.run_in_threadpool(audio_call, row, provider, 'asr', model, data, None, True)
    authenticate_again(db, login.id)
    row = recheck_config(db, user, version, 'asr', pv)
    row.checks = row.checks | {'asr': audio_signature(row, provider, 'asr')}
    return {'text': result}


@router.post('/api/voice/config/test-tts')
def test_tts(ctx: Context):
    db, user, login = ctx
    row = config_row(db, user)
    provider, model = audio_provider(db, row, 'tts')
    version, pv = row.version, provider.version
    reserve_audio(db, user)
    result = audio_call(row, provider, 'tts', model, text='你好，我是灵犀。这是人工智能合成语音。', validation=True)
    authenticate_again(db, login.id)
    row = recheck_config(db, user, version, 'tts', pv)
    row.checks = row.checks | {'tts': audio_signature(row, provider, 'tts')}
    return Response(result, media_type='audio/wav')


@router.post('/api/voice/config/test-realtime-tts')
def test_realtime_tts(ctx: Context):
    db, user, login = ctx
    row = config_row(db, user)
    provider, model = audio_provider(db, row, 'realtime_tts')
    version, pv = row.version, provider.version
    reserve_audio(db, user)
    from .voice_native_output import native_tts
    result = native_tts(row, provider, model, '你好，我是灵犀。这是实时合成验证。', row.spec['voice'], row.spec['speed'])
    authenticate_again(db, login.id)
    row = recheck_config(db, user, version, 'realtime_tts', pv)
    row.checks = row.checks | {'realtime_tts': audio_signature(row, provider, 'realtime_tts')}
    return Response(result, media_type='audio/wav')


@router.get('/api/voice/personas', response_model=list[PersonaVoiceView])
def persona_voices(ctx: Context):
    from .assistants import AssistantProfile
    db, user, _ = ctx
    config = config_row(db, user)
    result = []
    for persona in db.scalars(select(AssistantProfile).where(AssistantProfile.user_id == user.id,
            AssistantProfile.workspace_id == user.workspace_id, AssistantProfile.kind == 'persona')
            .order_by(AssistantProfile.created_at, AssistantProfile.id)):
        voice = persona.spec.get('voice', '')
        if not voice:
            continue
        issue = ('人格已停用' if not persona.enabled else
                 '音色标识须为 1–80 位英文字母、数字、下划线或连字符' if not valid_voice(voice) else '')
        result.append(PersonaVoiceView(id=persona.id, name=persona.name, version=persona.version,
            voice=voice, enabled=persona.enabled, verified=not issue and voice_checked(db, config, voice), issue=issue))
    return result


@router.post('/api/voice/personas/{identity}/test')
def test_persona_voice(identity: str, body: PersonaVoiceTest, ctx: Context):
    db, user, login = ctx
    persona = live_persona(db, identity, user)
    if persona.version != body.version:
        raise HTTPException(409, '人格已变化，请重新加载后验证')
    voice = persona.spec.get('voice', '')
    if not valid_voice(voice):
        raise HTTPException(422, '请先填写有效的人格音色标识（1–80 位英文字母、数字、下划线或连字符）')
    config = config_row(db, user)
    provider, model = audio_provider(db, config, 'tts')
    version, pv = config.version, provider.version
    reserve_audio(db, user)
    result = audio_call(effective_config(config, voice), provider, 'tts', model,
        text='你好，我是灵犀。这是人工智能合成语音。', validation=True)
    authenticate_again(db, login.id)
    config = recheck_config(db, user, version, 'tts', pv)
    persona = live_persona(db, identity, user, True)
    if not persona.enabled or persona.version != body.version or persona.spec.get('voice') != voice:
        raise HTTPException(409, '人格已变化，本次验证及音频已丢弃，请重新加载')
    key = 'tts' if voice == config.spec['voice'] else voice_key(voice)
    # Bound historical checks without persisting audio, text or persona instructions.
    checks = {k: v for k, v in config.checks.items() if not k.startswith('tts_voice:')}
    custom = {k: v for k, v in config.checks.items() if k.startswith('tts_voice:') and k != key}
    checks.update(dict(list(custom.items())[-99:]))
    config.checks = checks | {key: audio_signature(effective_config(config, voice), provider, 'tts')}
    audit(db, user, 'persona_voice_test', persona.id)
    return Response(result, media_type='audio/wav')


def authenticate_again(db, identity):
    db.expire_all()
    login = db.get(LoginSession, identity)
    if not login or login.expires_at <= now():
        raise HTTPException(401, '登录已失效，请重新确认身份')


def active_session(db, user, login, identity, lock=False):
    row = owned(db, VoiceSession, identity, user, lock)
    config = config_row(db, user)
    if (row.login_session_id != login.id or row.status != 'active' or row.expires_at <= now() or
            row.heartbeat_at + HEARTBEAT_TTL <= now() or not config or not config.spec.get('enabled') or
            config.version != row.config_version):
        raise HTTPException(409, '语音会话已结束或配置变化，请重新开启')
    owned(db, Conversation, row.conversation_id, user)
    return row


@router.post('/api/voice/sessions', response_model=VoiceSessionView)
def open_session(body: SessionInput, ctx: Context):
    db, user, login = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    owned(db, Conversation, body.conversation_id, user)
    config = config_row(db, user)
    if not config or not config.spec.get('enabled') or not all(checked(db, config, k) for k in ('asr', 'tts')):
        raise HTTPException(409, '请先启用语音，并在语音配置中验证转写及合成服务')
    for old in db.scalars(select(VoiceSession).where(VoiceSession.user_id == user.id, VoiceSession.status == 'active')):
        old.status = 'closed'
        from .proactive_voice import invalidate
        invalidate(db, session_id=old.id)
    row = VoiceSession(user_id=user.id, workspace_id=user.workspace_id, login_session_id=login.id,
                       conversation_id=body.conversation_id, config_version=config.version,
                       mode=body.mode, allow_proactive=body.allow_proactive, automatic_turns=0,
                       expires_at=min(now() + TTL, login.expires_at))
    db.add(row)
    db.flush()
    audit(db, user, 'voice_open', row.id)
    return view(row)


@router.post('/api/voice/sessions/{identity}/heartbeat', response_model=VoiceSessionView)
def heartbeat(identity: str, ctx: Context):
    db, user, login = ctx
    row = active_session(db, user, login, identity, True)
    row.heartbeat_at = now()
    return view(row)


@router.delete('/api/voice/sessions/{identity}', response_model=VoiceSessionView)
def close_session(identity: str, ctx: Context):
    db, user, login = ctx
    row = owned(db, VoiceSession, identity, user, True)
    if row.login_session_id != login.id:
        raise HTTPException(404, '语音会话不存在')
    row.status = 'closed'
    from .proactive_voice import invalidate
    invalidate(db, session_id=row.id)
    return view(row)


@router.post('/api/voice/sessions/{identity}/transcriptions', response_model=TranscriptView)
async def transcribe(identity: str, request: Request, ctx: Context):
    import starlette.concurrency
    db, user, login = ctx
    active_session(db, user, login, identity)
    data = await read_audio(request)
    config = config_row(db, user)
    if not checked(db, config, 'asr'):
        raise HTTPException(409, '转写服务已变化，请重新验证')
    provider, model = audio_provider(db, config, 'asr')
    version, pv = config.version, provider.version
    reserve_audio(db, user)
    result = await starlette.concurrency.run_in_threadpool(audio_call, config, provider, 'asr', model, data)
    authenticate_again(db, login.id)
    recheck_config(db, user, version, 'asr', pv)
    active_session(db, user, login, identity)
    # This is a temporary draft. Only explicit /messages submission can create memory outbox.
    return {'text': result}


@router.post('/api/voice/sessions/{identity}/messages', response_model=ChatRunView)
def voice_message(identity: str, body: VoiceMessageInput, ctx: Context):
    from .api import submit_chat
    from .schemas import ChatInput
    db, user, login = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    session = active_session(db, user, login, identity, True)
    previous = db.scalar(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.request_id == body.request_id))
    if previous and previous.config.get('voice_session_id') != identity:
        raise HTTPException(409, '此请求 ID 已用于其他会话，请重新提交')
    replay = previous is not None
    if body.automatic and session.mode != 'continuous':
        raise HTTPException(409, '请先明确开启连续对话')
    result = submit_chat(ChatInput(conversation_id=session.conversation_id,
                                  **body.model_dump(exclude={'automatic'})), ctx,
                         voice_context={'automatic': body.automatic, 'confirmed': not body.automatic})
    run = db.get(ChatRun, result['id'])
    if replay:
        return view(run)
    if body.automatic:
        session.automatic_turns += 1
    previous = run.config.get('voice_session_id')
    if previous and previous != identity:
        raise HTTPException(409, '此请求已在另一语音会话提交')
    if not previous:
        run.config = run.config | {'voice_session_id': identity, 'speech': speech_snapshot(config_row(db, user), run)}
    # Freeze acknowledgments at the new turn boundary. Late browser events cannot
    # make the model assume more of an earlier reply was heard.
    for playback in db.scalars(select(VoicePlayback).where(VoicePlayback.user_id == user.id,
                                                          VoicePlayback.status == 'active')):
        message = db.get(Message, playback.message_id)
        if message and message.conversation_id == session.conversation_id:
            playback.status = 'stopped'
    return view(run)


def speech_parts(text):
    # Render readable text; never follow or fetch markdown URLs while synthesizing.
    if len(text) > 6000:
        raise HTTPException(409, '回复超过语音预算，请阅读文字或请求简短回答')
    text = re.sub(r'```[\s\S]*?```', '（代码请查看文字）', text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[#*`_>]+', '', text)
    sentences = re.findall(r'[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+', text)
    return [sentence[i:i+160].strip() for sentence in sentences for i in range(0, len(sentence), 160)
            if sentence[i:i+160].strip()]


def playback_source(db, playback, user):
    from .proactive_voice import entry_for, validate
    entry = entry_for(db, playback)
    if entry:
        message = validate(db, entry, user)
        if hashlib.sha256(message.content.encode()).hexdigest() != playback.digest:
            raise HTTPException(409, '主动消息已变化，停止播报')
        return message, speech_parts(message.content)
    message = db.get(Message, playback.message_id)
    conversation = db.get(Conversation, message.conversation_id) if message else None
    run = db.get(ChatRun, message.run_id) if message and message.run_id else None if message else None
    if (not conversation or conversation.deleted or conversation.user_id != user.id or
            conversation.workspace_id != user.workspace_id or message.role != 'assistant' or
            message.status != 'completed' or not run or run.status != 'completed' or
            hashlib.sha256(message.content.encode()).hexdigest() != playback.digest):
        raise HTTPException(409, '回复已失效或未完成，停止播报')
    return message, speech_parts(message.content)


@router.post('/api/voice/sessions/{identity}/playbacks', response_model=PlaybackView)
def start_playback(identity: str, body: PlaybackInput, ctx: Context):
    db, user, login = ctx
    session = active_session(db, user, login, identity, True)
    message = db.get(Message, body.message_id)
    run = db.get(ChatRun, message.run_id) if message and message.run_id else None if message else None
    if (not message or message.conversation_id != session.conversation_id or not run or
            run.config.get('voice_session_id') != identity):
        raise HTTPException(404, '当前语音会话没有此回复')
    playback = db.scalar(select(VoicePlayback).where(VoicePlayback.session_id == identity,
                                                    VoicePlayback.message_id == body.message_id))
    if not playback:
        playback = VoicePlayback(user_id=user.id, workspace_id=user.workspace_id, session_id=identity,
                                 message_id=message.id, digest=hashlib.sha256(message.content.encode()).hexdigest())
        db.add(playback)
        db.flush()
    _, parts = playback_source(db, playback, user)
    playback_speech(db, playback, user, config_row(db, user))
    return playback_view(db, playback, parts)


@router.post('/api/voice/playbacks/{identity}/audio/{part}')
def synthesize(identity: str, part: int, ctx: Context):
    db, user, login = ctx
    playback = owned(db, VoicePlayback, identity, user)
    session = active_session(db, user, login, playback.session_id)
    _, parts = playback_source(db, playback, user)
    if playback.status != 'active' or not 0 <= part < len(parts) or part != playback.confirmed_parts:
        raise HTTPException(409, '播放进度已变化，请停止或重新开启语音')
    config = config_row(db, user)
    if not checked(db, config, 'tts'):
        raise HTTPException(409, '合成服务已变化，请重新验证')
    speech = playback_speech(db, playback, user, config)
    provider, model = audio_provider(db, config, 'tts')
    version, pv = config.version, provider.version
    reserve_audio(db, user)
    result = audio_call(effective_config(config, speech['voice']), provider, 'tts', model, text=parts[part])
    authenticate_again(db, login.id)
    config = recheck_config(db, user, version, 'tts', pv)
    active_session(db, user, login, session.id, True)
    playback = owned(db, VoicePlayback, identity, user, True)
    playback_source(db, playback, user)
    playback_speech(db, playback, user, config, True)
    if playback.status != 'active' or playback.confirmed_parts != part:
        raise HTTPException(409, '播报已停止，本次音频已丢弃')
    playback.offered_parts = part + 1
    return Response(result, media_type='audio/wav')


@router.post('/api/voice/playbacks/{identity}/progress', response_model=PlaybackView)
def acknowledge(identity: str, body: PlaybackAck, ctx: Context):
    db, user, login = ctx
    playback = owned(db, VoicePlayback, identity, user)
    active_session(db, user, login, playback.session_id, True)
    playback = owned(db, VoicePlayback, identity, user, True)
    _, parts = playback_source(db, playback, user)
    if (not 0 <= body.completed_parts <= min(len(parts), playback.offered_parts) or
            (not body.stop and body.completed_parts < playback.confirmed_parts)):
        raise HTTPException(409, '播放回执必须按已生成的短句顺序提交')
    if playback.status == 'active':
        playback.confirmed_parts = max(playback.confirmed_parts, body.completed_parts)
        if body.stop or body.completed_parts == len(parts):
            playback.status = 'stopped' if body.stop else 'completed'
    from .proactive_voice import entry_for
    entry = entry_for(db, playback)
    if entry and playback.status in ('stopped', 'completed'):
        entry.status = playback.status
    return playback_view(db, playback, parts)


def heard_history(db, run, history):
    """Voice turns only receive confirmed speech from previous voice replies.

    Full text remains visible in the Web history. Unacknowledged partial sentences
    are omitted, including on browser loss; no proportional duration estimate.
    """
    if not run.config.get('voice_session_id'):
        return history
    visible = []
    for message in history:
        previous = db.get(ChatRun, message.run_id) if message.run_id else None
        from .proactive_voice import VoiceContact
        entry = db.scalar(select(VoiceContact).where(VoiceContact.message_id == message.id,
            VoiceContact.user_id == run.user_id, VoiceContact.workspace_id == run.workspace_id)) if message.role == 'assistant' else None
        playback = None
        if entry:
            playback = db.get(VoicePlayback, entry.playback_id) if entry.playback_id else None
        elif message.role == 'assistant' and previous and previous.config.get('voice_session_id'):
            playback = db.scalar(select(VoicePlayback).where(VoicePlayback.message_id == message.id,
                VoicePlayback.session_id == previous.config['voice_session_id']))
        if entry or message.role == 'assistant' and previous and previous.config.get('voice_session_id'):
            if not playback or not playback.confirmed_parts:
                continue
            text = ''.join(speech_parts(message.content)[:playback.confirmed_parts])
            # Keep the stored Web text intact; only confirmed sentences enter voice context.
            message = Message(id=message.id, role=message.role, content=text, run_id=message.run_id,
                              conversation_id=message.conversation_id, status=message.status,
                              created_at=message.created_at, workspace_id=message.workspace_id)
        visible.append(message)
    return visible
