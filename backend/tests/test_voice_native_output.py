import base64
import contextlib
import json
import queue

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lingxi.db import SessionLocal
from lingxi.models import ChatRun, Message, ModelCall, Provider
from lingxi.security import encrypt
from lingxi.voice import VoiceConfig, audio_signature
from lingxi.voice_native_output import native_tts, normalized
from test_voice import opened, reply, voice_config


class NativeOutput:
    def __init__(self, mismatch=False, transcript=True):
        self.events = queue.Queue()
        self.events.put({'type': 'session.created'})
        self.sent = []
        self.mismatch = mismatch
        self.transcript = transcript

    def send_json(self, event):
        self.sent.append(event)
        if event['type'] == 'session.update':
            self.events.put({'type': 'session.updated', 'session': {'type': 'realtime'}})
        elif event['type'] == 'response.create':
            text = event['response']['input'][0]['content'][0]['text']
            if self.mismatch:
                text = '被篡改的音频内容。'
            self.events.put({'type': 'response.output_audio.delta', 'delta': base64.b64encode(b'\x01\x00' * 4800).decode()})
            if self.transcript:
                self.events.put({'type': 'response.output_audio_transcript.done', 'transcript': text})
            self.events.put({'type': 'response.done', 'response': {'status': 'completed'}})

    def receive_json(self, timeout):
        return self.events.get(timeout=timeout)


@pytest.fixture
def output(monkeypatch):
    streams = []
    @contextlib.contextmanager
    def connect(*args, **kwargs):
        stream = NativeOutput()
        streams.append(stream)
        yield stream
    monkeypatch.setattr('lingxi.voice_native_output.connect_ws', connect)
    monkeypatch.setattr('lingxi.voice_native_output.client_for', lambda provider: httpx.Client(
        base_url='https://example.com/v1/', headers={'Authorization': 'Bearer fixture'},
        transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    return streams


def native_config(client):
    voice_config(client)
    body = client.get('/api/voice/config').json()
    body['realtime_tts'] = {'provider_id': 'provider', 'model': 'live-output'}
    result = client.put('/api/voice/config', json=body)
    assert result.status_code == 200, result.text
    with SessionLocal.begin() as db:
        row, provider = db.get(VoiceConfig, 'owner'), db.get(Provider, 'provider')
        row.checks = row.checks | {'realtime_tts': audio_signature(row, provider, 'realtime_tts')}


def opened_existing(client):
    conversation = client.post('/api/conversations', json={'title': '原生输出'}).json()
    result = client.post('/api/voice/sessions', json={'conversation_id': conversation['id']})
    assert result.status_code == 200, result.text
    return result.json()


def test_native_output_uses_out_of_band_exact_text_and_returns_wav(configured, output):
    native_config(configured)
    value = native_tts(VoiceConfig(spec={'realtime_tts': {'model': 'live-output'}, 'voice': 'alloy', 'speed': 1}, version=3),
        Provider(id='provider', version=1, base_url='https://example.com/v1', encrypted_key=encrypt('fixture')), 'live-output', '你好。', 'alloy', 1)
    assert value[:4] == b'RIFF'
    stream = output[0]
    assert stream.sent[0]['session']['output_modalities'] == ['audio']
    request = stream.sent[1]['response']
    assert request['conversation'] == 'none'
    assert request['output_modalities'] == ['audio']
    assert request['input'][0]['content'][0] == {'type': 'input_text', 'text': '你好。'}
    assert 'Do not add' in request['instructions']


def test_native_output_rejects_transcript_mismatch_without_audio(configured, monkeypatch):
    native_config(configured)
    streams = []
    @contextlib.contextmanager
    def connect(*args, **kwargs):
        stream = NativeOutput(mismatch=True);streams.append(stream);yield stream
    monkeypatch.setattr('lingxi.voice_native_output.connect_ws', connect)
    with pytest.raises(HTTPException, match='不一致'):
        native_tts(VoiceConfig(spec={'realtime_tts': {'model': 'live-output'}, 'voice': 'alloy', 'speed': 1}, version=3),
            Provider(id='provider', version=1, base_url='https://example.com/v1', encrypted_key=encrypt('fixture')), 'live-output', '你好。', 'alloy', 1)


def test_native_output_requires_audio_transcript(configured, monkeypatch):
    native_config(configured)
    @contextlib.contextmanager
    def connect(*args, **kwargs):
        yield NativeOutput(transcript=False)
    monkeypatch.setattr('lingxi.voice_native_output.connect_ws', connect)
    with pytest.raises(HTTPException, match='字幕'):
        native_tts(VoiceConfig(spec={'realtime_tts': {'model': 'live-output'}, 'voice': 'alloy', 'speed': 1}, version=3),
            Provider(id='provider', version=1, base_url='https://example.com/v1', encrypted_key=encrypt('fixture')), 'live-output', '你好。', 'alloy', 1)


def test_native_playback_rechecks_source_and_marks_offer(configured, output):
    native_config(configured)
    session = opened_existing(configured)
    plan = reply(configured, session, text='第一句话。第二句话。')
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/native-audio/0")
    assert result.status_code == 200 and result.content[:4] == b'RIFF'
    with SessionLocal() as db:
        playback = db.get(__import__('lingxi.voice', fromlist=['VoicePlayback']).VoicePlayback, plan['id'])
        assert playback.offered_parts == 1
        call = db.scalar(select(ModelCall).where(ModelCall.model_id == 'voice:realtime-tts'))
        assert call and call.status == 'completed'


def test_native_playback_does_not_retry_or_use_rest_after_failure(configured, monkeypatch):
    native_config(configured)
    session = opened_existing(configured)
    plan = reply(configured, session, text='第一句话。')
    monkeypatch.setattr('lingxi.voice_native_output.native_tts', lambda *a, **k: (_ for _ in ()).throw(HTTPException(409, '字幕不一致')))
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/native-audio/0")
    assert result.status_code == 409
    with SessionLocal() as db:
        playback = db.get(__import__('lingxi.voice', fromlist=['VoicePlayback']).VoicePlayback, plan['id'])
        assert playback.offered_parts == 0


def test_native_playback_rechecks_config_and_session_before_paid_call(configured, output, monkeypatch):
    native_config(configured)
    session = opened_existing(configured)
    plan = reply(configured, session, text='配置变化。')
    with SessionLocal.begin() as db:
        db.get(VoiceConfig, 'owner').version += 1
    monkeypatch.setattr('lingxi.voice_native_output.native_tts', lambda *a, **k: (_ for _ in ()).throw(AssertionError('paid call must not start')))
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/native-audio/0")
    assert result.status_code == 409
    with SessionLocal.begin() as db:
        db.get(VoiceConfig, 'owner').version -= 1
        db.get(__import__('lingxi.voice', fromlist=['VoiceSession']).VoiceSession, session['id']).status = 'closed'
    result = configured.post(f"/api/voice/playbacks/{plan['id']}/native-audio/0")
    assert result.status_code == 409


def test_normalization_only_ignores_whitespace():
    assert normalized(' 第一\n句话。 ') == '第一句话。'
    assert normalized('第一句话。') != normalized('第一句话！')
