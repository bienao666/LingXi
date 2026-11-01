"""Child claims need an explicit grant, bounded evidence and a separate human review."""
import json
import pytest
from sqlalchemy import select,func
from lingxi.db import SessionLocal
from lingxi.models import Candidate,Memory,Message,Conversation,Job,User
from lingxi.delegation import AgentRun,AgentNode
from lingxi import gateway,worker
from lingxi.memory import MemoryService,usable
from test_delegation import create,drain,fetch,fixture_model


PROPOSAL={'content':'灵犀项目优先打通长期记忆','category':'projects','subject':'project:lingxi',
    'reason':'任务原文提出长期目标，需用户确认实际采纳','evidence_ids':['task-goal']}


def proposal_model(client,monkeypatch,proposals=None,mutate=None):
    items=proposals if proposals is not None else [dict(PROPOSAL)]
    def modify(stage,messages):
        if mutate:
            value=mutate(stage,messages)
            if value is not None:return value
        if stage=='node':
            return json.dumps({'summary':'项目目标分析','findings':[],'limitations':['事实仍需用户核实'],
                'memory_candidates':items},ensure_ascii=False)
    return fixture_model(client,monkeypatch,mutate=modify)


def propose(client,monkeypatch,**kw):
    proposal_model(client,monkeypatch)
    row=create(client,propose_memory=True,**kw)
    drain()
    candidates=client.get('/api/memory-candidates').json()
    assert len(candidates)==1,candidates
    return row,candidates[0]


def review(client,candidate):
    response=client.get('/api/memory-candidates/'+candidate['id']+'/sources')
    assert response.status_code==200,response.text
    return response.json()


def confirm(client,candidate):
    preview=review(client,candidate)
    return client.post('/api/memory-candidates/'+candidate['id']+'/actions',json={
        'action':'confirm','review_digest':preview['review_digest']})


def test_no_grant_cannot_publish_candidates(configured,monkeypatch):
    proposal_model(configured,monkeypatch)
    row=create(configured);drain()
    assert fetch(configured,row)['status']=='failed'
    assert configured.get('/api/memory-candidates').json()==[]
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Memory))==0


def test_review_only_and_idempotent_adoption(configured,monkeypatch):
    row,candidate=propose(configured,monkeypatch)
    assert candidate['status']=='pending' and candidate['evidence']=='agent_proposal'
    assert candidate['origin']['model']=='fixture-model'
    assert candidate['origin']['role']['version']>=1
    with SessionLocal() as db:
        assert db.get(User,'owner').auto_memory
        assert db.scalar(select(func.count()).select_from(Memory))==0
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='index'))==0
    path='/api/memory-candidates/'+candidate['id']+'/actions'
    assert configured.post(path,json={'action':'confirm'}).status_code==409
    assert configured.post(path,json={'action':'confirm','review_digest':'0'*64}).status_code==409
    response=confirm(configured,candidate);assert response.status_code==200,response.text
    assert confirm(configured,candidate).json()['memory_id']==response.json()['memory_id']
    with SessionLocal() as db:
        memory=db.get(Memory,response.json()['memory_id'])
        assert memory.evidence=='user_confirmed' and memory.subject=='project:lingxi'
        assert memory.provenance['run_id']==row['id'] and usable(memory)
        assert db.scalar(select(func.count()).select_from(Memory))==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='index'))==1
    source=configured.get('/api/memories/'+response.json()['memory_id']+'/sources').json()
    assert source[0]['origin']['node_id']==candidate['source_id']


@pytest.mark.parametrize('change',[
    {'evidence_ids':[]},{'evidence_ids':['foreign']},{'evidence_ids':['task-goal','task-goal']},
    {'content':' '},{'subject':' '},{'category':'passwords'},{'reason':' '},
])
def test_untrusted_proposals_rejected(configured,monkeypatch,change):
    proposal_model(configured,monkeypatch,[PROPOSAL|change])
    row=create(configured,propose_memory=True);drain()
    assert fetch(configured,row)['status']=='failed'
    assert configured.get('/api/memory-candidates').json()==[]


def test_candidate_bound_and_unknown_subject(configured,monkeypatch):
    proposal_model(configured,monkeypatch,[PROPOSAL|{'subject':'unknown'}])
    create(configured,propose_memory=True);drain()
    candidate=configured.get('/api/memory-candidates').json()[0]
    assert confirm(configured,candidate).status_code==409
    assert configured.post('/api/memory-candidates/'+candidate['id']+'/actions',json={'action':'reject'}).status_code==200
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Memory))==0


def test_only_successful_summary_publishes(configured,monkeypatch):
    def fail(stage,messages):
        if stage=='summary':raise ValueError('fixture summary failure')
    proposal_model(configured,monkeypatch,mutate=fail)
    row=create(configured,propose_memory=True);drain()
    assert fetch(configured,row)['status']=='failed'
    assert configured.get('/api/memory-candidates').json()==[]


def test_candidate_limit(configured,monkeypatch):
    proposal_model(configured,monkeypatch,[PROPOSAL]*4)
    row=create(configured,propose_memory=True);drain()
    assert fetch(configured,row)['status']=='failed'
    assert configured.get('/api/memory-candidates').json()==[]


def test_no_summary_memory_injection(configured,monkeypatch):
    def inject(stage,messages):
        if stage=='summary':return json.dumps({'summary':'s','memory_candidates':[PROPOSAL]})
    proposal_model(configured,monkeypatch,mutate=inject)
    row=create(configured,propose_memory=True);drain()
    assert fetch(configured,row)['status']=='failed'
    assert configured.get('/api/memory-candidates').json()==[]


def test_rejected_proposal_does_not_reappear(configured,monkeypatch):
    row,candidate=propose(configured,monkeypatch)
    configured.post('/api/memory-candidates/'+candidate['id']+'/actions',json={'action':'reject'})
    from lingxi.agent_memory import publish
    with SessionLocal.begin() as db:
        publish(db,db.get(AgentRun,row['id']),list(db.scalars(select(AgentNode))))
    assert len(configured.get('/api/memory-candidates').json())==1
    assert configured.get('/api/memory-candidates').json()[0]['status']=='rejected'


@pytest.mark.parametrize('kind',['cancel','profile','result','grant','round'])
def test_pending_revocation_blocks_save(configured,monkeypatch,kind):
    row,candidate=propose(configured,monkeypatch);preview=review(configured,candidate)
    with SessionLocal.begin() as db:
        run=db.get(AgentRun,row['id'])
        if kind=='cancel':
            from lingxi.delegation import revoke_run
            revoke_run(db,run,'fixture cancellation')
        elif kind=='profile':
            from lingxi.assistants import AssistantProfile
            db.get(AssistantProfile,candidate['origin']['role']['id']).enabled=False
        elif kind=='result':db.get(AgentNode,candidate['source_id']).result={'summary':'replaced'}
        elif kind=='grant':run.config=run.config|{'propose_memory':False}
        elif kind=='round':run.round+=1
    response=configured.post('/api/memory-candidates/'+candidate['id']+'/actions',json={'action':'confirm','review_digest':preview['review_digest']})
    assert response.status_code in (200,409,410)
    assert not response.json().get('memory_id')
    with SessionLocal() as db:assert db.scalar(select(func.count()).select_from(Memory))==0
    assert configured.get('/api/memory-candidates').json()==[]


def test_owner_and_workspace_isolation(configured,monkeypatch):
    row,candidate=propose(configured,monkeypatch)
    with SessionLocal.begin() as db:db.get(Candidate,candidate['id']).workspace_id='other'
    assert configured.get('/api/memory-candidates/'+candidate['id']+'/sources').status_code==404
    assert configured.post('/api/memory-candidates/'+candidate['id']+'/actions',json={'action':'confirm'}).status_code==404
    assert configured.get('/api/memory-candidates').json()==[]


def test_adopted_fact_survives_role_disable(configured,monkeypatch):
    row,candidate=propose(configured,monkeypatch)
    memory_id=confirm(configured,candidate).json()['memory_id']
    with SessionLocal.begin() as db:
        from lingxi.assistants import AssistantProfile
        db.get(AssistantProfile,candidate['origin']['role']['id']).enabled=False
        assert usable(db.get(Memory,memory_id))


def test_source_memory_correction_deletes_derived_fact(configured,monkeypatch):
    root=configured.post('/api/memories',json={'content':'项目预算42万元','category':'projects'}).json()
    monkeypatch.setattr(MemoryService,'search',lambda self,db,user_id,query:[db.get(Memory,root['id'])])
    proposal_model(configured,monkeypatch,[PROPOSAL|{'evidence_ids':[root['id']]}])
    row=create(configured,propose_memory=True,share_memory=True);drain()
    candidate=configured.get('/api/memory-candidates').json()[0]
    memory_id=confirm(configured,candidate).json()['memory_id']
    corrected=configured.patch('/api/memories/'+root['id'],json={'content':'预算更新为40万元','category':'projects','version':1})
    assert corrected.status_code==200,corrected.text
    with SessionLocal() as db:
        derived=db.get(Memory,memory_id)
        assert derived.status=='deleted' and not derived.content and not usable(derived)
        assert db.get(Candidate,candidate['id']).status=='invalidated'


def test_web_collaboration_grant_is_forwarded(configured,monkeypatch):
    proposal_model(configured,monkeypatch)
    c=configured.post('/api/conversations',json={'title':'项目'}).json()
    r=configured.post('/api/chat',json={'conversation_id':c['id'],'content':'规划灵犀项目','request_id':'chat-propose-memory-001',
        'collaboration':{'propose_memory':True}})
    assert r.status_code==200,r.text
    drain()
    candidate=configured.get('/api/memory-candidates').json()[0]
    assert candidate['origin']['source_message_ids']
    assert confirm(configured,candidate).status_code==200


@pytest.mark.parametrize('adopt',[False,True])
def test_attachment_delete_clears_pending_and_adopted_derivatives(configured,monkeypatch,adopt):
    from test_agent_media import attachment
    item=attachment(configured)
    agents=configured.get('/api/agents').json()
    def reply(model,provider,messages,purpose='chat',config=None):
        if config['stage']=='plan':
            return json.dumps({'mode':'direct','reason':'读取材料','nodes':[{'key':'n0','title':'分析材料',
                'instruction':'提取项目目标','agent_id':agents[0]['id'],'media_ids':[item['id']]}]})
        if config['stage']=='node':
            return json.dumps({'summary':'分析附件','findings':[],'memory_candidates':[PROPOSAL|{'evidence_ids':[item['id']]}]})
        return json.dumps({'summary':'汇总附件','findings':[]})
    monkeypatch.setattr(gateway,'complete',reply)
    create(configured,propose_memory=True,media_ids=[item['id']]);drain()
    candidate=configured.get('/api/memory-candidates').json()[0]
    memory_id=confirm(configured,candidate).json()['memory_id'] if adopt else None
    response=configured.request('DELETE','/api/media/'+item['id'],json={'version':item['version']})
    assert response.status_code==200,response.text
    assert configured.get('/api/memory-candidates').json()==[]
    with SessionLocal() as db:
        candidate=db.get(Candidate,candidate['id'])
        assert not candidate.content and not candidate.origin.get('citations')
        if memory_id:
            memory=db.get(Memory,memory_id)
            assert memory.status=='deleted' and not memory.content and not memory.provenance


def test_temporal_dependency_cannot_be_used_after_expiry(configured,monkeypatch):
    from lingxi.db import now
    root=configured.post('/api/memories',json={'content':'项目范围一期','category':'projects'}).json()
    monkeypatch.setattr(MemoryService,'search',lambda self,db,user_id,query:[db.get(Memory,root['id'])])
    row,candidate=propose(configured,monkeypatch,share_memory=True)
    memory_id=confirm(configured,candidate).json()['memory_id']
    with SessionLocal.begin() as db:db.get(Memory,root['id']).expires_at=now()-1
    with SessionLocal() as db:assert not usable(db.get(Memory,memory_id))


def test_user_correction_becomes_independent_but_old_derivative_revision_is_redacted(configured,monkeypatch):
    root=configured.post('/api/memories',json={'content':'原项目目标','category':'projects'}).json()
    monkeypatch.setattr(MemoryService,'search',lambda self,db,user_id,query:[db.get(Memory,root['id'])])
    row,candidate=propose(configured,monkeypatch,share_memory=True)
    memory_id=confirm(configured,candidate).json()['memory_id']
    response=configured.patch('/api/memories/'+memory_id,json={'content':'用户重新指定的新目标','category':'projects','version':1})
    assert response.status_code==200,response.text
    assert configured.delete('/api/memories/'+root['id']).status_code==200
    with SessionLocal() as db:
        memory=db.get(Memory,memory_id)
        assert usable(memory) and memory.content=='用户重新指定的新目标'
        assert not memory.provenance and not memory.revisions[0]['content']


def test_late_node_answer_after_cancel_creates_no_candidate(configured,monkeypatch):
    def cancel(stage,messages):
        if stage=='node':
            with SessionLocal.begin() as db:
                from lingxi.delegation import revoke_run
                revoke_run(db,db.scalar(select(AgentRun)),'cancel during model')
    proposal_model(configured,monkeypatch,mutate=cancel)
    row=create(configured,propose_memory=True);drain()
    assert fetch(configured,row)['status']=='cancelled'
    assert configured.get('/api/memory-candidates').json()==[]


def test_restore_quarantines_proposals_but_preserves_adopted_facts(configured,monkeypatch):
    from test_backups import schema
    from lingxi.backups import checkpoint,quarantine
    row,candidate=propose(configured,monkeypatch)
    memory_id=confirm(configured,candidate).json()['memory_id']
    create(configured,propose_memory=True,request_id='another-proposal-run');drain()
    with SessionLocal.begin() as db:
        schema(db)
    state=checkpoint()
    quarantine(state)
    with SessionLocal() as db:
        memory=db.get(Memory,memory_id)
        assert memory.evidence=='user_confirmed' and memory.provenance['kind']=='agent' and usable(memory)
        assert not list(db.scalars(select(Candidate).where(Candidate.status=='pending')))


def test_parallel_confirmation_creates_one_memory(configured,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('PostgreSQL row lock regression')
    row,candidate=propose(configured,monkeypatch);payload={'action':'confirm','review_digest':review(configured,candidate)['review_digest']}
    from lingxi import agent_memory
    original=agent_memory.validate;barrier=Barrier(2)
    def synchronized(db,candidate,lock=False):
        if lock:barrier.wait(timeout=10)
        return original(db,candidate,lock)
    monkeypatch.setattr(agent_memory,'validate',synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:configured.post('/api/memory-candidates/'+candidate['id']+'/actions',json=payload),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert len({r.json()['memory_id'] for r in responses})==1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Memory))==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='index'))==1
