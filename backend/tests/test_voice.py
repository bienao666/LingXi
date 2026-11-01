import hashlib
import io
import wave
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select
from lingxi.db import SessionLocal, now
from lingxi.models import Audit, ChatRun, Job, LoginSession, Message, Provider, User
from lingxi.voice import (VoiceConfig, VoiceSession, VoicePlayback, audio_signature, heard_history,
                          speech_parts, wav_check)


def wav(seconds=.2):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\x01\x00' * int(16000 * seconds))
    return data.getvalue()


def voice_config(client):
    result = client.put('/api/voice/config', json={'enabled': True,
        'asr': {'provider_id': 'provider', 'model': 'asr-model'},
        'tts': {'provider_id': 'provider', 'model': 'tts-model'}})
    assert result.status_code == 200, result.text
    with SessionLocal.begin() as db:
        row, provider = db.get(VoiceConfig, 'owner'), db.get(Provider, 'provider')
        row.checks = {kind: audio_signature(row, provider, kind) for kind in ('asr', 'tts')}


def opened(client):
    voice_config(client)
    conversation = client.post('/api/conversations', json={'title': '语音对话'}).json()
    result = client.post('/api/voice/sessions', json={'conversation_id': conversation['id']})
    assert result.status_code == 200, result.text
    return result.json()


def reply(client, session, text='第一句话。第二句话。'):
    result = client.post(f"/api/voice/sessions/{session['id']}/messages",
                         json={'content': '请回答', 'request_id': 'voice-message-001'})
    assert result.status_code == 200, result.text
    with SessionLocal.begin() as db:
        run = db.get(ChatRun, result.json()['id'])
        run.status = 'completed'
        message = Message(conversation_id=run.conversation_id, role='assistant', content=text, run_id=run.id)
        db.add(message)
        db.flush()
        identity = message.id
    result = client.post(f"/api/voice/sessions/{session['id']}/playbacks", json={'message_id': identity})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.fixture
def transport(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith('/audio/transcriptions'):
            return httpx.Response(200, json={'text': '我喜欢喝茶。'})
        return httpx.Response(200, content=wav(), headers={'Content-Type': 'audio/wav'})
    monkeypatch.setattr('lingxi.voice.client_for', lambda p: httpx.Client(base_url=p.base_url+'/',
                        transport=httpx.MockTransport(respond)))
    return requests


def post_audio(client, path, data=None):
    return client.post(path, content=wav() if data is None else data, headers={'Content-Type':'application/octet-stream'})


def test_voice_config_version_auth_and_checks(configured):
    client = configured
    assert client.get('/api/voice/config').json()['version'] == 0
    voice_config(client)
    assert client.get('/api/voice/config').json()['checks'] == {'asr':True,'tts':True}
    assert client.put('/api/voice/config', json={'version':0}).status_code == 409
    token = client.headers.pop('X-CSRF-Token')
    assert client.put('/api/voice/config', json={'version':1}).status_code == 403
    client.headers['X-CSRF-Token'] = token
    client.cookies.clear()
    assert client.get('/api/voice/config').status_code == 401


def test_verified_signatures_follow_settings_and_provider(configured):
    voice_config(configured)
    body = configured.get('/api/voice/config').json()
    body['voice'] = 'nova'
    assert configured.put('/api/voice/config', json=body).json()['checks'] == {'asr':True,'tts':False}
    with SessionLocal.begin() as db:
        db.get(Provider, 'provider').version += 1
    assert configured.get('/api/voice/config').json()['checks'] == {'asr':False,'tts':False}


def test_real_transport_validation_no_raw_storage(configured, transport):
    voice_config(configured)
    result = post_audio(configured, '/api/voice/config/test-asr')
    assert result.json() == {'text':'我喜欢喝茶。','temporary':True}
    result = configured.post('/api/voice/config/test-tts')
    assert result.status_code == 200 and result.content[:4] == b'RIFF'
    assert str(transport[0].url) == 'https://example.com/v1/audio/transcriptions'
    assert b'name="file"' in transport[0].content and b'asr-model' in transport[0].content
    assert b'"response_format":"wav"' in transport[1].content
    from lingxi.config import settings
    assert not list(Path(settings().runtime_dir).glob('**/*.wav'))
    with SessionLocal() as db:
        assert list(db.scalars(select(Message))) == []
        assert list(db.scalars(select(Job))) == []


@pytest.mark.parametrize('audio', [b'', b'not audio', b'RIFFxxxxWAVE'+b'x'*32, wav(.05), wav(60.1)], ids=['empty','invalid','bad-header','too-short','too-long'])
def test_wav_rejects_invalid_duration_and_format(audio):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        wav_check(audio)


def test_transcript_is_draft_until_explicit_send(configured, transport):
    s = opened(configured)
    result = post_audio(configured, f"/api/voice/sessions/{s['id']}/transcriptions")
    assert result.status_code == 200
    with SessionLocal() as db:
        assert not list(db.scalars(select(Job))) and not list(db.scalars(select(Message)))
    path = f"/api/voice/sessions/{s['id']}/messages"
    body = {'content':'纠正后的转写', 'request_id':'voice-confirmed-001'}
    first, second = configured.post(path, json=body), configured.post(path, json=body)
    assert first.status_code == 200 and first.json()['id'] == second.json()['id']
    with SessionLocal() as db:
        messages = list(db.scalars(select(Message)))
        assert len(messages) == 1 and messages[0].content == body['content']
        assert len(list(db.scalars(select(Job)))) == 2


@pytest.mark.parametrize('change', ['expired','heartbeat','closed','config','conversation','login','other_tab'])
def test_session_invalidation(configured, transport, change):
    s = opened(configured)
    with SessionLocal.begin() as db:
        row = db.get(VoiceSession, s['id'])
        if change == 'expired': row.expires_at = now()-1
        if change == 'heartbeat': row.heartbeat_at = now()-61
        if change == 'closed': row.status = 'closed'
        if change == 'config': db.get(VoiceConfig, 'owner').version += 1
        if change == 'conversation':
            from lingxi.models import Conversation
            db.get(Conversation, row.conversation_id).deleted = True
        if change == 'login': db.get(LoginSession, row.login_session_id).expires_at = now()-1
        if change == 'other_tab': row.login_session_id = 'another-login'
    assert post_audio(configured, f"/api/voice/sessions/{s['id']}/transcriptions").status_code in (401,404,409)
    assert transport == []


def test_new_voice_session_closes_old_and_unconfigured_cannot_open(configured):
    c = configured.post('/api/conversations', json={'title':'未配置'}).json()
    assert configured.post('/api/voice/sessions', json={'conversation_id':c['id']}).status_code == 409
    s = opened(configured)
    new = configured.post('/api/voice/sessions', json={'conversation_id':s['conversation_id']}).json()
    assert new['id'] != s['id']
    assert configured.post(f"/api/voice/sessions/{s['id']}/heartbeat").status_code == 409
    assert configured.post(f"/api/voice/sessions/{new['id']}/heartbeat").status_code == 200
    assert configured.delete(f"/api/voice/sessions/{new['id']}").status_code == 200


def test_speech_requires_owned_complete_voice_reply(configured):
    s = opened(configured)
    assert configured.post(f"/api/voice/sessions/{s['id']}/playbacks", json={'message_id':'not-owned'}).status_code == 404
    p = reply(configured, s)
    with SessionLocal.begin() as db:
        db.get(Message,p['message_id']).status = 'invalidated'
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 409


def test_progress_sequential_stop_does_not_cancel_chat(configured, transport):
    s = opened(configured)
    p = reply(configured, s)
    path = f"/api/voice/playbacks/{p['id']}"
    assert configured.post(path+'/progress', json={'completed_parts':1}).status_code == 409
    assert configured.post(path+'/audio/1').status_code == 409
    assert configured.post(path+'/audio/0').status_code == 200
    ack = configured.post(path+'/progress', json={'completed_parts':1})
    assert ack.json()['confirmed_parts'] == 1
    assert configured.post(path+'/audio/1').status_code == 200
    assert configured.post(path+'/progress', json={'completed_parts':1,'stop':True}).json()['status'] == 'stopped'
    assert configured.post(path+'/audio/1').status_code == 409
    with SessionLocal() as db:
        message = db.get(Message, p['message_id'])
        assert message.status == 'completed' and db.get(ChatRun,message.run_id).status == 'completed'


def test_only_confirmed_sentences_enter_voice_context(configured, transport):
    s = opened(configured)
    p = reply(configured,s)
    with SessionLocal.begin() as db:
        history = list(db.scalars(select(Message).order_by(Message.created_at)))
        run = ChatRun(config={'voice_session_id':s['id']})
        assert [m.role for m in heard_history(db, run, history)] == ['user']
        db.get(VoicePlayback,p['id']).confirmed_parts = 1
        db.flush()
        filtered = heard_history(db,run,history)
        assert filtered[-1].content == '第一句话。'
        assert db.get(Message,p['message_id']).content == '第一句话。第二句话。'
        assert heard_history(db,ChatRun(config={}),history)[-1].content == '第一句话。第二句话。'


def test_new_turn_freezes_late_receipts(configured, transport):
    s = opened(configured)
    p = reply(configured,s)
    assert configured.post(f"/api/voice/playbacks/{p['id']}/audio/0").status_code == 200
    assert configured.post(f"/api/voice/sessions/{s['id']}/messages", json={'content':'打断并换个话题','request_id':'voice-interrupt-002'}).status_code == 200
    late = configured.post(f"/api/voice/playbacks/{p['id']}/progress",json={'completed_parts':1})
    assert late.json()['confirmed_parts'] == 0 and late.json()['status'] == 'stopped'


def test_source_revoked_during_synthesis_discards_audio(configured, monkeypatch):
    s = opened(configured)
    p = reply(configured,s)
    def request(*args, **kwargs):
        with SessionLocal.begin() as db:
            db.get(Message,p['message_id']).status='invalidated'
        return wav()
    monkeypatch.setattr('lingxi.voice.audio_call',request)
    response=configured.post(f"/api/voice/playbacks/{p['id']}/audio/0")
    assert response.status_code==409 and response.content[:4]!=b'RIFF'


def test_upstream_errors_never_return_private_body(configured, monkeypatch):
    s = opened(configured)
    monkeypatch.setattr('lingxi.voice.client_for',lambda p:httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda r:httpx.Response(401,text='secret-upstream-key'))))
    result=post_audio(configured,f"/api/voice/sessions/{s['id']}/transcriptions")
    assert result.status_code==502 and 'secret-upstream-key' not in result.text and '401' in result.text


@pytest.mark.parametrize('change', ['login','config','close','provider'])
def test_asr_rechecks_authorization_after_upstream(configured, monkeypatch, change):
    s=opened(configured)
    def upstream(*args,**kwargs):
        with SessionLocal.begin() as db:
            session=db.get(VoiceSession,s['id'])
            if change=='login':db.get(LoginSession,session.login_session_id).expires_at=now()-1
            if change=='config':db.get(VoiceConfig,'owner').version+=1
            if change=='close':session.status='closed'
            if change=='provider':db.get(Provider,'provider').version+=1
        return 'should be discarded'
    monkeypatch.setattr('lingxi.voice.audio_call',upstream)
    result=post_audio(configured,f"/api/voice/sessions/{s['id']}/transcriptions")
    assert result.status_code in (401,409) and 'should be discarded' not in result.text


def test_paid_request_throttle(configured, transport):
    s = opened(configured)
    with SessionLocal.begin() as db:
        db.add_all([Audit(user_id='owner',action='voice_request') for _ in range(30)])
    assert post_audio(configured,f"/api/voice/sessions/{s['id']}/transcriptions").status_code==429
    assert transport==[]


def test_voice_scope_does_not_trust_ids(configured, transport):
    s=opened(configured)
    with SessionLocal.begin() as db:
        db.get(VoiceSession,s['id']).workspace_id='other'
    assert configured.post(f"/api/voice/sessions/{s['id']}/heartbeat").status_code==404
    assert configured.delete(f"/api/voice/sessions/{s['id']}").status_code==404


def test_speech_readable_markdown_and_no_fetch():
    assert speech_parts('你好。\n[来源](https://private.example)！```shell\nrm -rf\n```')==['你好。','来源！','（代码请查看文字）']
    assert all(len(v)<=160 for v in speech_parts('中'*400))


def test_stop_can_race_an_ack_without_rewinding_progress(configured, transport):
    s=opened(configured)
    p=reply(configured,s)
    path=f"/api/voice/playbacks/{p['id']}"
    assert configured.post(path+'/audio/0').status_code==200
    assert configured.post(path+'/progress',json={'completed_parts':1}).status_code==200
    result=configured.post(path+'/progress',json={'completed_parts':0,'stop':True})
    assert result.status_code==200 and result.json()['confirmed_parts']==1
    assert result.json()['status']=='stopped'


def test_voice_request_cannot_relabel_text_request(configured):
    s=opened(configured)
    body={'conversation_id':s['conversation_id'],'content':'同一个请求','request_id':'shared-request-001'}
    assert configured.post('/api/chat',json=body).status_code==200
    assert configured.post(f"/api/voice/sessions/{s['id']}/messages",json={k:v for k,v in body.items() if k!='conversation_id'}).status_code==409


def test_worker_uses_heard_projection_without_overwriting_full_history(configured, monkeypatch):
    from lingxi.worker import claim, perform
    s=opened(configured)
    p=reply(configured,s)
    with SessionLocal.begin() as db:
        db.get(VoicePlayback,p['id']).confirmed_parts=1
        for job in db.scalars(select(Job)):
            job.status='completed'
    result=configured.post(f"/api/voice/sessions/{s['id']}/messages",
        json={'content':'继续说','request_id':'voice-second-turn'})
    assert result.status_code==200
    observed=[]
    monkeypatch.setattr('lingxi.worker.MemoryService.search',lambda *a:[])
    def stream(model,provider,messages,*args,**kwargs):
        observed.extend(messages)
        yield '接着说。'
    monkeypatch.setattr('lingxi.worker.stream',stream)
    job=claim()
    assert job.kind=='chat' and job.payload['run_id']==result.json()['id']
    perform(job)
    assert [m['content'] for m in observed if m['role']=='assistant']==['第一句话。']
    with SessionLocal() as db:
        assert db.get(Message,p['message_id']).content=='第一句话。第二句话。'
        assert db.get(ChatRun,result.json()['id']).status=='completed'
