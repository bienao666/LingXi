"""Confirmed voice -> persistent delegation -> one protected main-assistant result."""
import pytest
from sqlalchemy import select, func
from lingxi.db import SessionLocal
from lingxi.models import ChatRun, Message, Job, User
from lingxi.delegation import AgentRun
from lingxi.chat_actions import ChatAction
from lingxi.voice import VoicePlayback, VoiceSession, heard_history
from test_voice import opened, transport, voice_config
from test_chat_collaboration import fixture, drain, card


def submit(client, session, **kw):
    body = {'content':'请分析项目目标和风险并整理建议', 'request_id':'voice-collaboration-001',
            'collaboration':{'max_calls':6}} | kw
    result = client.post(f"/api/voice/sessions/{session['id']}/messages", json=body)
    assert result.status_code == 200, result.text
    return body, result.json()


def completed(client, monkeypatch):
    session = opened(client)
    stages = fixture(client, monkeypatch)
    body, run = submit(client, session)
    drain()
    action = card(client, run)
    assert action['status'] == 'completed', action
    assert action['voice_result_message_id']
    return session, body, run, action, stages


def play(client, session, action):
    return client.post(f"/api/voice/sessions/{session['id']}/playbacks",
                       json={'message_id':action['voice_result_message_id']})


def test_confirmed_voice_dispatch_persists_scopes_and_one_main_result(configured, monkeypatch):
    c=configured;s,body,run,action,stages=completed(c,monkeypatch)
    assert stages == ['plan','node','node','summary']
    assert c.post(f"/api/voice/sessions/{s['id']}/messages",json=body).json()['id']==run['id']
    assert c.post(f"/api/voice/sessions/{s['id']}/messages",json=body|{'collaboration':{}}).status_code==409
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun))==1
        assert db.scalar(select(func.count()).select_from(ChatAction))==1
        assert db.scalar(select(func.count()).select_from(VoicePlayback))==0
        parent=db.get(ChatRun,run['id'])
        assert parent.config['voice_input']=={'automatic':False,'confirmed':True}
        target=db.get(AgentRun,action['target_id'])
        assert not target.config['share_memory'] and not target.config['allow_files']
        message=db.get(Message,action['voice_result_message_id'])
        assert message.content.startswith('协作任务') and message.run_id is None
        assert db.scalar(select(func.count()).select_from(Message).where(Message.id==target.result_message_id))==1


@pytest.mark.parametrize('change',[{'automatic':True},{'task_assist':True},{'collaboration':{'max_calls':2}},
    {'collaboration':{'share_memory':'true'}},{'collaboration':{'unknown':True}},{'content':'字'*6001}])
def test_unconfirmed_extra_or_invalid_voice_grants_rejected(configured,change):
    c=configured;s=opened(c)
    r=c.post(f"/api/voice/sessions/{s['id']}/messages",json={'content':'委派任务','request_id':'voice-reject-001','collaboration':{}}|change)
    assert r.status_code in (409,422),r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun))==0
        assert db.scalar(select(func.count()).select_from(ChatRun))==0


def test_continuous_mode_never_delegates_even_with_forged_confirmed_flag(configured):
    c=configured;s=opened(c)
    c.delete('/api/voice/sessions/'+s['id'])
    s=c.post('/api/voice/sessions',json={'conversation_id':s['conversation_id'],'mode':'continuous'}).json()
    for automatic in (False,True):
        assert c.post(f"/api/voice/sessions/{s['id']}/messages",json={'content':'委派任务','request_id':'voice-continuous-001','automatic':automatic,'collaboration':{}}).status_code==422


def test_browser_end_preserves_background_task_but_does_not_reopen_audio(configured,monkeypatch):
    c=configured;s=opened(c);fixture(c,monkeypatch);_,run=submit(c,s)
    assert c.delete('/api/voice/sessions/'+s['id']).status_code==200
    drain();action=card(c,run);assert action['status']=='completed'
    assert play(c,s,action).status_code==409
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(VoicePlayback))==0


def test_result_playback_is_explicit_idempotent_and_only_heard_sentences_enter_history(configured,monkeypatch,transport):
    c=configured;s,_,run,action,_=completed(c,monkeypatch)
    result=play(c,s,action);assert result.status_code==200,result.text
    plan=result.json();assert plan['collaboration'] and not plan['proactive']
    assert play(c,s,action).json()['id']==plan['id']
    with SessionLocal() as db:
        current=db.get(ChatRun,run['id']);message=db.get(Message,action['voice_result_message_id'])
        assert heard_history(db,current,[message])==[]
    assert c.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code==200
    assert len(transport)==1
    assert c.post(f"/api/voice/playbacks/{plan['id']}/progress",json={'completed_parts':1,'stop':True}).status_code==200
    with SessionLocal() as db:
        current=db.get(ChatRun,run['id']);message=db.get(Message,action['voice_result_message_id'])
        heard=heard_history(db,current,[message]);assert len(heard)==1 and heard[0].content==plan['parts'][0]
        assert len(message.content)>len(heard[0].content)
    assert play(c,s,action).json()['status']=='stopped'
    assert c.post(f"/api/voice/playbacks/{plan['id']}/audio/1").status_code==409


@pytest.mark.parametrize('change',['source','persona','conversation'])
def test_result_source_revocation_blocks_status_and_synthesis(configured,monkeypatch,transport,change):
    from lingxi.assistants import AssistantProfile
    from lingxi.models import Conversation
    c=configured;s,_,run,action,_=completed(c,monkeypatch)
    plan=play(c,s,action).json()
    with SessionLocal.begin() as db:
        if change=='source':db.get(Message,db.scalar(select(ChatAction).where(ChatAction.run_id==run['id'])).source_id).status='invalidated'
        elif change=='conversation':db.get(Conversation,s['conversation_id']).deleted=True
        else:
            target=db.get(AgentRun,action['target_id']);identity=target.config['agents'][0]['persona']['id']
            db.get(AssistantProfile,identity).enabled=False
    assert c.get('/api/voice/playbacks/'+plan['id']).status_code in (404,409,410)
    assert c.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code in (404,409,410)
    assert not transport


def test_source_invalidated_during_tts_discards_audio(configured,monkeypatch):
    from test_voice import wav
    c=configured;s,_,run,action,_=completed(c,monkeypatch);plan=play(c,s,action).json()
    def audio(*a,**kw):
        with SessionLocal.begin() as db:
            source=db.scalar(select(ChatAction).where(ChatAction.run_id==run['id'])).source_id
            db.get(Message,source).status='invalidated'
        return wav()
    monkeypatch.setattr('lingxi.voice.audio_call',audio)
    assert c.post(f"/api/voice/playbacks/{plan['id']}/audio/0").status_code in (404,409,410)
    with SessionLocal() as db:assert db.get(VoicePlayback,plan['id']).offered_parts==0


def test_plain_web_result_and_new_voice_session_do_not_inherit_spoken_origin(configured,monkeypatch):
    c=configured;s,_,run,action,_=completed(c,monkeypatch)
    with SessionLocal.begin() as db:
        message=Message(conversation_id=s['conversation_id'],role='assistant',content='普通后台资料')
        db.add(message);db.flush();identity=message.id
    assert c.post(f"/api/voice/sessions/{s['id']}/playbacks",json={'message_id':identity}).status_code==404
    c.delete('/api/voice/sessions/'+s['id'])
    second=c.post('/api/voice/sessions',json={'conversation_id':s['conversation_id']}).json()
    assert play(c,second,action).status_code==404
