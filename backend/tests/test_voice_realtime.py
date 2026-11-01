import contextlib
import json
import queue
import threading
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Job, LoginSession, Message, ModelCall, Provider, User
from lingxi.security import hasher, token_hash
from lingxi.voice import VoiceConfig, audio_signature
from lingxi.voice_realtime import RealtimeTicket, TranscriptProtocol, claim, live, update_event
from test_voice import voice_config, opened


def setup(client):
    if not client.get('/api/voice/config').json()['version']:
        voice_config(client)
    config = client.get('/api/voice/config').json()
    config['realtime_asr'] = {'provider_id': 'provider', 'model': 'live-model'}
    result = client.put('/api/voice/config', json=config)
    assert result.status_code == 200, result.text
    return result.json()


def ticket(client, session=''):
    response = client.post('/api/voice/realtime-tickets', json={'session_id': session})
    assert response.status_code == 200, response.text
    return response.json()['token']


class Upstream:
    def __init__(self):
        self.events = queue.Queue()
        self.events.put({'type': 'session.created'})
        self.sent = []
        self.closed = False

    def send_json(self, event):
        self.sent.append(event)
        if event['type'] == 'session.update':
            self.events.put({'type': 'session.updated', 'session': {'type': 'transcription'}})
        elif event['type'] == 'input_audio_buffer.append':
            self.events.put({'type': 'conversation.item.input_audio_transcription.delta', 'item_id': 'turn1', 'delta': '临时'})
        elif event['type'] == 'input_audio_buffer.commit':
            self.events.put({'type': 'input_audio_buffer.committed', 'item_id': 'turn1'})
            self.events.put({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'turn1', 'transcript': '我喜欢喝茶。'})

    def receive_json(self, timeout):
        return self.events.get(timeout=timeout)


@pytest.fixture
def upstream(monkeypatch):
    connections, streams = [], []
    @contextlib.contextmanager
    def connection(url, client, **kwargs):
        connections.append((url, dict(client.headers), kwargs))
        stream = Upstream()
        streams.append(stream)
        try:
            yield stream
        finally:
            stream.closed = True
    monkeypatch.setattr('lingxi.voice_realtime.connect_ws', connection)
    # Keep the real provider client builder. Only the paid upstream protocol is faked.
    return connections, streams


def socket(client, token):
    context = client.websocket_connect('/api/voice/realtime', headers={'Origin': 'http://testserver'})
    ws = context.__enter__()
    ws.send_json({'token': token})
    return context, ws


def finish_worker(identity):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            row = db.get(RealtimeTicket, token_hash(identity))
            count = db.query(ModelCall).count()
            if row.status != 'active' and count:
                return row.status
        time.sleep(.02)
    pytest.fail('relay did not release its lease and record the call')


def test_ticket_auth_csrf_and_no_credential_storage(configured):
    setup(configured)
    secret = ticket(configured)
    with SessionLocal() as db:
        row = db.get(RealtimeTicket, token_hash(secret))
        assert row and row.status == 'issued' and row.validation
        assert row.expires_at <= now() + 31
        assert secret not in repr(row.__dict__)
        assert not list(db.scalars(select(Job)))
    csrf = configured.headers.pop('X-CSRF-Token')
    assert configured.post('/api/voice/realtime-tickets', json={}).status_code == 403
    configured.headers['X-CSRF-Token'] = csrf
    configured.cookies.clear()
    assert configured.post('/api/voice/realtime-tickets', json={}).status_code == 401


def test_ticket_rejects_unverified_session_and_wrong_ids(configured):
    session = opened(configured)
    setup(configured)
    assert configured.post('/api/voice/realtime-tickets', json={'session_id': 'not-owned'}).status_code == 404
    # Earlier config version also invalidates the old session.
    assert configured.post('/api/voice/realtime-tickets', json={'session_id': session['id']}).status_code == 409
    session = configured.post('/api/voice/sessions', json={'conversation_id': session['conversation_id']}).json()
    assert configured.post('/api/voice/realtime-tickets', json={'session_id': session['id']}).status_code == 409


def test_ticket_single_use_cross_login_and_one_live_stream(configured):
    setup(configured)
    first, second = ticket(configured), ticket(configured)
    identity, login = claim(first, configured.cookies, configured.headers)
    with pytest.raises(HTTPException) as exc:
        claim(first, configured.cookies, configured.headers)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        claim(second, configured.cookies, configured.headers)
    assert exc.value.status_code == 409
    with SessionLocal.begin() as db:
        db.get(RealtimeTicket, identity).status = 'cancelled'
        db.get(RealtimeTicket, token_hash(second)).expires_at = now() - 1
    with pytest.raises(HTTPException) as exc:
        claim(second, configured.cookies, configured.headers)
    assert exc.value.status_code == 401
    third = ticket(configured)
    configured.post('/api/auth/logout')
    configured.post('/api/auth/login', json={'username': 'owner', 'password': 'test-password-123'})
    with pytest.raises(HTTPException):
        claim(third, configured.cookies, configured.headers)


@pytest.mark.parametrize('change', ['provider', 'config', 'login', 'workspace', 'disabled', 'frozen'])
def test_live_authorization_changes_reject_result(configured, change):
    setup(configured)
    token = ticket(configured)
    identity, login = claim(token, configured.cookies, configured.headers)
    with SessionLocal.begin() as db:
        if change == 'provider': db.get(Provider, 'provider').version += 1
        elif change == 'config': db.get(VoiceConfig, 'owner').version += 1
        elif change == 'disabled': db.get(VoiceConfig, 'owner').spec = db.get(VoiceConfig, 'owner').spec | {'enabled': False}
        elif change == 'login': db.delete(db.get(LoginSession, login))
        elif change == 'workspace': db.get(User, 'owner').workspace_id = 'other'
        else:
            from lingxi.workspace_migrations import MigrationSite
            db.add(MigrationSite(id='local', origin='http://testserver', mode='frozen'))
    with SessionLocal() as db, pytest.raises(HTTPException):
        live(db, identity, login)


def test_ticket_issue_rate_limit(configured):
    setup(configured)
    for _ in range(30): ticket(configured)
    assert configured.post('/api/voice/realtime-tickets', json={}).status_code == 429


@pytest.mark.parametrize('origin,path', [('http://evil.test', '/api/voice/realtime'), ('', '/api/voice/realtime'),
    ('http://testserver', '/api/voice/realtime?token=should-not-be-in-url')])
def test_socket_rejects_origin_and_query(configured, origin, path):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with configured.websocket_connect(path, headers={'Origin': origin}): pass


def test_real_socket_deltas_final_verification_and_no_memory_write(configured, upstream):
    setup(configured)
    token = ticket(configured)
    with configured.websocket_connect('/api/voice/realtime', headers={'Origin': 'http://testserver'}) as ws:
        ws.send_json({'token': token})
        assert ws.receive_json() == {'type': 'ready', 'sample_rate': 24000}
        ws.send_bytes(b'\x01\x00' * 2400)
        assert ws.receive_json()['type'] == 'partial'
        ws.send_json({'type': 'commit'})
        assert ws.receive_json() == {'type': 'completed', 'text': '我喜欢喝茶。', 'temporary': True}
    assert finish_worker(token) == 'completed'
    connections, streams = upstream
    assert connections[0][0] == 'https://example.com/v1/realtime?intent=transcription'
    assert connections[0][1]['authorization'] == 'Bearer secret-fixture-key'
    assert streams[0].sent[0] == {'type': 'session.update', 'session': {'type': 'transcription', 'audio': {'input': {
        'format': {'type': 'audio/pcm', 'rate': 24000}, 'transcription': {'model': 'live-model', 'language': 'zh'}, 'turn_detection': None}}}}
    with SessionLocal() as db:
        config, provider = db.get(VoiceConfig, 'owner'), db.get(Provider, 'provider')
        assert config.checks['realtime_asr'] == audio_signature(config, provider, 'realtime_asr')
        assert not list(db.scalars(select(Message))) and not list(db.scalars(select(Job))) and not list(db.scalars(select(ChatRun)))
        call = db.scalar(select(ModelCall))
        assert call.status == 'completed' and call.config['model'] == 'live-model'
        assert call.config['audio_measurement']['input_frames'] == 2400
        assert call.config['audio_measurement']['input_sample_rate'] == 24000
        assert '我喜欢' not in repr(call.__dict__) and 'secret-fixture-key' not in repr(call.__dict__)


@pytest.mark.parametrize('frame', [b'x', b'x'*9602, '{"type":"response.create"}', '{"type":"commit"}'])
def test_invalid_input_fails_no_paid_retry_or_memory(configured, upstream, frame):
    setup(configured)
    token = ticket(configured)
    with configured.websocket_connect('/api/voice/realtime', headers={'Origin': 'http://testserver'}) as ws:
        ws.send_json({'token': token})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(frame) if isinstance(frame, bytes) else ws.send_text(frame)
        assert ws.receive_json()['type'] == 'error'
    finish_worker(token)
    assert len(upstream[0]) == 1
    with SessionLocal() as db:
        assert not list(db.scalars(select(Message))) and not list(db.scalars(select(Job)))


def test_disconnection_releases_upstream_and_does_not_verify(configured, upstream):
    setup(configured)
    token = ticket(configured)
    with configured.websocket_connect('/api/voice/realtime', headers={'Origin': 'http://testserver'}) as ws:
        ws.send_json({'token': token})
        assert ws.receive_json()['type'] == 'ready'
    assert finish_worker(token) == 'cancelled'
    assert upstream[1][0].closed
    assert not configured.get('/api/voice/config').json()['checks']['realtime_asr']


def test_continuous_native_final_uses_ordinary_unconfirmed_idempotent_submission(configured, upstream):
    from lingxi.models import Candidate, Memory
    setup(configured)
    with SessionLocal.begin() as db:
        config, provider = db.get(VoiceConfig, 'owner'), db.get(Provider, 'provider')
        config.checks = config.checks | {'realtime_asr': audio_signature(config, provider, 'realtime_asr')}
    conversation = configured.post('/api/conversations', json={'title': '连续实时转写'}).json()
    response = configured.post('/api/voice/sessions', json={'conversation_id': conversation['id'], 'mode': 'continuous'})
    assert response.status_code == 200, response.text
    session = response.json()
    token = ticket(configured, session['id'])
    with configured.websocket_connect('/api/voice/realtime', headers={'Origin': 'http://testserver'}) as ws:
        ws.send_json({'token': token})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_bytes(b'\x01\x00' * 2400)
        assert ws.receive_json()['type'] == 'partial'
        with SessionLocal() as db:
            assert not db.scalar(select(Message)) and not db.scalar(select(Job))
        ws.send_json({'type': 'commit'})
        final = ws.receive_json()
    assert finish_worker(token) == 'completed'
    body = {'content': final['text'], 'request_id': 'native-continuous-turn', 'automatic': True}
    result = configured.post(f"/api/voice/sessions/{session['id']}/messages", json=body)
    assert result.status_code == 200, result.text
    assert configured.post(f"/api/voice/sessions/{session['id']}/messages", json=body).json()['id'] == result.json()['id']
    with SessionLocal() as db:
        assert db.get(ChatRun, result.json()['id']).config['voice_input'] == {'automatic': True, 'confirmed': False}
        assert len(list(db.scalars(select(Message)))) == 1
        assert [job.kind for job in db.scalars(select(Job))] == ['chat']
        assert not db.scalar(select(Candidate)) and not db.scalar(select(Memory))


def test_backup_restore_cancels_unused_and_active_authorization(configured):
    from test_backups import schema
    from lingxi.backups import checkpoint, quarantine
    setup(configured)
    first, second = ticket(configured), ticket(configured)
    identity, login = claim(first, configured.cookies, configured.headers)
    with SessionLocal.begin() as db:
        schema(db)
    quarantine(checkpoint())
    with SessionLocal() as db:
        assert db.get(RealtimeTicket, identity).status == 'cancelled'
        assert db.get(RealtimeTicket, token_hash(second)).status == 'cancelled'
        with pytest.raises(HTTPException):
            live(db, identity, login)


def test_migration_requires_active_transcription_to_end(configured):
    from test_workspace_migrations import plan
    from lingxi.workspace_migrations import freeze
    setup(configured)
    value = ticket(configured)
    identity, _ = claim(value, configured.cookies, configured.headers)
    migration, _ = plan(configured)
    with pytest.raises(ValueError, match='realtime transcription'):
        freeze(migration['id'])
    with SessionLocal.begin() as db:
        db.get(RealtimeTicket, identity).status = 'cancelled'
    assert freeze(migration['id'])['status'] == 'freezing'


def test_protocol_language_arrays_and_revision_replaces_partial(configured):
    setup(configured)
    with SessionLocal() as db:
        row = db.get(VoiceConfig, 'owner')
        row.spec = row.spec | {'realtime_language_field': 'languages'}
        transcription = update_event(row, 'gpt-live-transcribe')['session']['audio']['input']['transcription']
        assert transcription == {'model': 'gpt-live-transcribe', 'languages': ['zh']}
    protocol = TranscriptProtocol()
    assert protocol.process({'type': 'conversation.item.input_audio_transcription.delta', 'item_id': 'a', 'delta': '错字'}, False)['text'] == '错字'
    protocol.process({'type': 'input_audio_buffer.committed', 'item_id': 'a'}, True)
    assert protocol.process({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'a', 'transcript': '定稿'}, True)['text'] == '定稿'


@pytest.mark.parametrize('events', [
    [({'type':'session.updated','session':{'type':'realtime'}},False)],
    [({'type':'conversation.item.input_audio_transcription.completed','item_id':'a','transcript':'无回执'},True)],
    [({'type':'input_audio_buffer.committed','item_id':'a'},False)],
    [({'type':'conversation.item.input_audio_transcription.delta','item_id':'a','delta':'x'*12001},False)],
    [({'type':'conversation.item.input_audio_transcription.delta','item_id':'a','delta':'x'},False),({'type':'input_audio_buffer.committed','item_id':'b'},True)],
    [({'type':'input_audio_buffer.committed','item_id':'a'},True),({'type':'conversation.item.input_audio_transcription.completed','item_id':'b','transcript':'其他轮'},True)],
    [({'type':'error','error':{'message':'secret-key'}},False)],
])
def test_protocol_rejects_mismatch_and_unbounded_content(events):
    protocol = TranscriptProtocol()
    with pytest.raises(HTTPException) as error:
        for event, committed in events: protocol.process(event, committed)
    assert 'secret-key' not in str(error.value.detail)
