"""Optional native Realtime audio output for already validated Core messages.

This relay never lets the upstream model decide facts, tools, memory, or the
message to play. It receives one bounded sentence from VoicePlayback, verifies
the returned audio transcript, and only then returns PCM WAV to the browser.
"""
import base64
import hashlib
import io
import json
import time
import wave

from fastapi import APIRouter, HTTPException, Response
from httpx_ws import connect_ws, WebSocketUpgradeError
from sqlalchemy import select

from .gateway import client_for, record_call
from .security import safe_error
from .voice import (audio_provider, audio_signature, playback_source, playback_speech,
                    recheck_config, reserve_audio, active_session, VoicePlayback,
                    VoiceConfig, VoiceSession, wav_check)
from .models import Provider
from .web import Context, owned

router = APIRouter()
MAX_NATIVE_AUDIO = 6 * 1024 * 1024
MAX_NATIVE_TEXT = 160


def normalized(value):
    return ''.join(value.split())


def pcm_wav(data):
    if not data or len(data) % 2 or len(data) > MAX_NATIVE_AUDIO:
        raise HTTPException(502, '实时合成音频超过处理预算')
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(data)
    value = output.getvalue()
    wav_check(value)
    return value


def native_tts(config, provider, model, text, voice, speed):
    started = time.monotonic()
    status, error = 'failed', None
    pcm = bytearray()
    transcript = []
    from .model_budgets import client as budget_client
    metadata = {'provider_id': provider.id, 'provider_version': provider.version,
                'model': model, 'voice_config_version': config.version,
                'audio_transport': 'realtime', 'validation': True}
    try:
        with budget_client(provider, client_for, metadata) as client:
            url = str(client.base_url.join('realtime'))
            with connect_ws(url, client, max_message_size_bytes=128 * 1024,
                            queue_size=16, keepalive_ping_interval_seconds=None) as ws:
                output = {'format': {'type': 'audio/pcm', 'rate': 24000},
                          'voice': voice, 'speed': speed}
                ws.send_json({'type': 'session.update', 'session': {
                    'type': 'realtime', 'model': model, 'output_modalities': ['audio'],
                    'audio': {'output': output}}})
                ready = False
                response = {'conversation': 'none', 'output_modalities': ['audio'],
                    'audio': {'output': output},
                    'instructions': 'Read the supplied text exactly once. Do not add, remove, translate, explain, or answer it.',
                    'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]}
                done = False
                deadline = time.monotonic() + 45
                while not done and time.monotonic() < deadline:
                    event = ws.receive_json(timeout=max(.1, deadline-time.monotonic()))
                    if not isinstance(event, dict):
                        raise HTTPException(502, '实时合成返回格式无效')
                    kind = event.get('type', '')
                    if kind == 'session.updated':
                        if event.get('session', {}).get('type') != 'realtime':
                            raise HTTPException(502, '服务未确认实时合成会话')
                        ready = True
                        ws.send_json({'type': 'response.create', 'response': response})
                    elif kind == 'error':
                        raise HTTPException(502, '实时合成服务拒绝请求，请检查模型与音色兼容性')
                    elif kind == 'response.output_audio.delta':
                        if not ready or not isinstance(event.get('delta'), str):
                            raise HTTPException(502, '实时合成音频回执无效')
                        try:
                            part = base64.b64decode(event['delta'], validate=True)
                        except Exception:
                            raise HTTPException(502, '实时合成音频编码无效') from None
                        pcm.extend(part)
                        if len(pcm) > MAX_NATIVE_AUDIO:
                            raise HTTPException(502, '实时合成音频超过处理预算')
                    elif kind in ('response.output_audio_transcript.delta', 'response.output_audio_transcript.done',
                                   'response.output_audio_transcript.completed'):
                        value = event.get('delta', event.get('transcript', ''))
                        if not isinstance(value, str) or len(value) > MAX_NATIVE_TEXT:
                            raise HTTPException(502, '实时合成字幕超过处理预算')
                        if kind.endswith('.delta'):
                            transcript.append(value)
                        else:
                            transcript = [value]
                    elif kind == 'response.done':
                        response_state = event.get('response', {})
                        if response_state.get('status') not in (None, 'completed'):
                            raise HTTPException(502, '实时合成未正常完成')
                        done = True
                if not done or not ready or not pcm:
                    raise HTTPException(504, '实时合成等待超时或未返回音频')
        spoken = ''.join(transcript)
        if not spoken or normalized(spoken) != normalized(text):
            raise HTTPException(409, '实时合成字幕与原回复不一致，音频已丢弃')
        result = pcm_wav(bytes(pcm))
        status = 'completed'
        return result
    except Exception as exc:
        error = safe_error(exc)
        raise HTTPException(exc.status_code if isinstance(exc, HTTPException) else 502, error) from None
    finally:
        record_call('voice:realtime-tts', 'tts', started, status, {}, error,
                    config=metadata)


@router.post('/api/voice/playbacks/{identity}/native-audio/{part}')
def synthesize_native(identity: str, part: int, ctx: Context):
    db, user, login = ctx
    playback = owned(db, VoicePlayback, identity, user)
    session = active_session(db, user, login, playback.session_id)
    _, parts = playback_source(db, playback, user)
    if playback.status != 'active' or not 0 <= part < len(parts) or part != playback.confirmed_parts:
        raise HTTPException(409, '播放进度已变化，请停止或重新开启语音')
    config = db.get(VoiceConfig, user.id)
    if not config or not config.spec.get('realtime_tts', {}).get('model') or not config.checks.get('realtime_tts'):
        raise HTTPException(409, '实时合成尚未验证，文字回复仍保留')
    provider, model = audio_provider(db, config, 'realtime_tts')
    speech = playback_speech(db, playback, user, config)
    version, provider_version = config.version, provider.version
    reserve_audio(db, user)
    result = native_tts(config, provider, model, parts[part], speech['voice'], config.spec['speed'])
    from .voice import authenticate_again
    authenticate_again(db, login.id)
    config = recheck_config(db, user, version, 'realtime_tts', provider_version)
    active_session(db, user, login, session.id, True)
    playback = owned(db, VoicePlayback, identity, user, True)
    playback_source(db, playback, user)
    playback_speech(db, playback, user, config, True)
    if playback.status != 'active' or playback.confirmed_parts != part:
        raise HTTPException(409, '播报已停止，本次音频已丢弃')
    playback.offered_parts = part + 1
    return Response(result, media_type='audio/wav')
