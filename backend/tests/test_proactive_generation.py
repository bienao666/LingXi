"""Paid opt-in, grounded drafts, durable call fences and last-moment delivery checks."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from lingxi import proactive as pro, proactive_generation as gen, worker
from lingxi.api import app
from lingxi.db import SessionLocal, now
from lingxi.models import Memory, Message, Job, Provider, Model, User

VALID = {'decision':'share','reason':'useful_next_step','source_id':'memory',
         'quote':'我在准备一个中文项目演示。','suggestion':'不妨先用三句话整理你想展示的重点，方便之后接着完善。'}


def preferences(client, **values):
    current=client.get('/api/contact-preferences').json()
    response=client.put('/api/contact-preferences',json=current|values)
    assert response.status_code==200,response.text
    return response.json()


def setup(client, **settings):
    preferences(client,quiet_start='00:00',quiet_end='00:00',model_generation=True,**settings)
    source=client.post('/api/memories',json={'content':VALID['quote'],'category':'preferences'}).json()
    response=client.post('/api/conversation-topics',json={'source_type':'memory','source_id':source['id'],
        'kind':'follow_up','title':'继续演示','not_before':now()-1,'expires_at':now()+86400})
    assert response.status_code==200,response.text
    return source,response.json()


def rows(client):
    return client.get('/api/proactive-contacts').json()['items']


def draft(topic):
    with SessionLocal() as db:
        return db.scalar(select(gen.ProactiveDraft).where(gen.ProactiveDraft.topic_id==topic['id']))


def run(monkeypatch, value=VALID, hook=None):
    calls=[]
    def complete(model,provider,messages,**kwargs):
        calls.append((model.id,provider.id,messages,kwargs))
        if hook:hook()
        if isinstance(value,Exception):raise value
        return value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
    monkeypatch.setattr('lingxi.gateway.complete',complete)
    with SessionLocal.begin() as db:
        job=db.scalar(select(Job).where(Job.kind=='proactive_draft',Job.status=='pending').order_by(Job.created_at))
        assert job
        job.status,job.lease_token,job.lease_until,job.attempts='running','draft-fixture-lease',now()+600,1
    worker.perform(job)
    return calls


def test_default_templates_have_no_model_cost(configured,monkeypatch):
    _,topic=setup(configured)
    preferences(configured,model_generation=False)
    monkeypatch.setattr('lingxi.gateway.complete',lambda *a,**k:pytest.fail('Template made a paid call'))
    pro.deliver(topic['id'])
    assert len(rows(configured))==1 and draft(topic) is None


def test_durable_job_and_source_only_input_then_one_delivery(configured,monkeypatch):
    source,topic=setup(configured)
    pro.deliver(topic['id'])
    assert rows(configured)==[] and draft(topic).status=='queued'
    for _ in range(3):pro.deliver(topic['id'])
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='proactive_draft'))==1
    calls=run(monkeypatch)
    assert len(calls)==1 and draft(topic).status=='ready'
    payload=json.loads(calls[0][2][-1]['content'])
    assert list(payload)==['topic_kind','sources']
    assert payload['sources']==[{'id':'memory','text':VALID['quote'],'kind':'user_record'}]
    assert calls[0][3]['config']['prompt_version']=='proactive-suggestion-v1'
    assert rows(configured)==[]
    pro.deliver(topic['id']);pro.deliver(topic['id'])
    contact=rows(configured)
    assert len(contact)==1 and VALID['quote'] in contact[0]['content'] and VALID['suggestion'] in contact[0]['content']
    assert '未被独立事实核验' in contact[0]['reason']
    assert configured.get('/api/conversation-topics').json()['items'][0]['generation']['status']=='ready'


def test_model_can_decline_without_sending_or_repeated_calls(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id'])
    run(monkeypatch,{'decision':'skip','reason':'no_new_value'})
    assert draft(topic).status=='skipped'
    for _ in range(3):pro.deliver(topic['id'])
    assert rows(configured)==[]
    assert configured.get('/api/conversation-topics').json()['items'][0]['status']=='closed'


@pytest.mark.parametrize('change',[
    {'quote':'用户已经完成演示'}, {'source_id':'other-user'}, {'quote':' '},
    {'suggestion':'我刚才替你调查了市场'}, {'suggestion':'建议你已经完成项目，可以庆祝'},
    {'suggestion':'建议打开 https://evil.example'}, {'suggestion':'建议执行 <script>注入</script>'},
    {'suggestion':'用户很开心'}, {'suggestion':'不妨'+('长'*300)}, {'extra_tool':'send_message'},
    {'decision':'skip','reason':'no_new_value'}])
def test_bad_structure_claims_and_unmatched_sources_never_deliver(configured,monkeypatch,change):
    _,topic=setup(configured);pro.deliver(topic['id'])
    assert len(run(monkeypatch,VALID|change))==1
    assert draft(topic).status=='failed' and not draft(topic).content
    for _ in range(3):pro.deliver(topic['id'])
    assert rows(configured)==[]
    with SessionLocal() as db:
        job=db.scalar(select(Job).where(Job.kind=='proactive_draft'))
        assert job.status=='failed' and job.attempts==1


@pytest.mark.parametrize('action',['delete','correct','preferences','close','model','expire'])
def test_changes_during_model_call_fence_late_results(configured,monkeypatch,action):
    source,topic=setup(configured);pro.deliver(topic['id'])
    def change():
        if action=='delete':assert configured.delete('/api/memories/'+source['id']).status_code==200
        elif action=='correct':assert configured.patch('/api/memories/'+source['id'],json={'version':1,'content':'现在准备另一件事'}).status_code==200
        elif action=='preferences':preferences(configured,enabled=False)
        elif action=='close':
            current=configured.get('/api/conversation-topics').json()['items'][0]
            assert configured.post('/api/conversation-topics/'+topic['id']+'/close',json={'version':current['version']}).status_code==200
        elif action=='model':
            with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
        elif action=='expire':
            with SessionLocal.begin() as db:db.get(Memory,source['id']).expires_at=now()-1
    run(monkeypatch,hook=change)
    pro.deliver(topic['id'])
    assert rows(configured)==[] and draft(topic).status in ('invalidated','failed') and not draft(topic).content


def test_ready_text_waits_for_new_quiet_period_and_is_not_sent_after_source_delete(configured,monkeypatch):
    source,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch)
    # Simulate crossing the clock boundary without changing the preference version.
    monkeypatch.setattr(pro,'quiet',lambda p,t:True)
    pro.deliver(topic['id']);assert not rows(configured)
    assert draft(topic).status=='ready'
    configured.delete('/api/memories/'+source['id'])
    assert draft(topic).status=='invalidated' and not draft(topic).content


def test_automatic_generation_has_per_owner_concurrency_and_daily_budget(configured,monkeypatch):
    _,one=setup(configured,daily_limit=1)
    pro.deliver(one['id'])
    source=configured.post('/api/memories',json={'content':'另一个项目的明确记录','category':'preferences'}).json()
    two=configured.post('/api/conversation-topics',json={'source_type':'memory','source_id':source['id'],
        'title':'第二个话题','not_before':now()-1,'expires_at':now()+86400}).json()
    pro.deliver(two['id']);assert draft(two) is None
    run(monkeypatch,{'decision':'skip','reason':'insufficient_context'})
    pro.deliver(two['id']);assert draft(two) is None


@pytest.mark.parametrize('values',[{'enabled':False},{'mode':'results_only'},{'paused_until':now()+3600},
    {'quiet_start':'00:00','quiet_end':'23:59'}])
def test_no_generation_when_contact_is_not_allowed(configured,values):
    _,topic=setup(configured);preferences(configured,**values)
    pro.deliver(topic['id'])
    assert draft(topic) is None and not rows(configured)


def test_busy_after_queue_makes_no_call(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id'])
    original=pro.timing_reason
    monkeypatch.setattr(pro,'timing_reason',lambda *a,**k:'用户正在处理')
    assert run(monkeypatch)==[] and draft(topic).status=='deferred' and not rows(configured)
    monkeypatch.setattr(pro,'timing_reason',original)
    pro.deliver(topic['id'])
    assert draft(topic).status=='queued'
    assert len(run(monkeypatch))==1
    pro.deliver(topic['id'])
    assert len(rows(configured))==1


@pytest.mark.parametrize('change',['workspace','unverified','provider_version'])
def test_unverified_or_cross_workspace_models_fail_without_sending(configured,change):
    _,topic=setup(configured)
    with SessionLocal.begin() as db:
        model=db.get(Model,'chat')
        if change=='workspace':model.workspace_id='another-workspace'
        elif change=='unverified':model.status='unverified'
        else:db.get(Provider,'provider').version+=1
    pro.deliver(topic['id'])
    assert draft(topic).status=='failed' and not rows(configured)
    with SessionLocal() as db:assert not db.scalar(select(Job.id).where(Job.kind=='proactive_draft'))


def test_explicit_retry_requires_current_versions_ack_and_does_not_repeat_automatically(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch,RuntimeError('secret-upstream-body'))
    current=configured.get('/api/conversation-topics').json()['items'][0]
    path='/api/conversation-topics/'+topic['id']+'/generation-retry'
    body={'version':current['version'],'draft_version':current['generation']['version'],'acknowledgement':'retry_one_model_call'}
    assert configured.post(path,json=body|{'draft_version':1}).status_code==409
    assert configured.post(path,json={k:v for k,v in body.items() if k!='acknowledgement'}).status_code==422
    response=configured.post(path,json=body);assert response.status_code==200,response.text
    assert response.json()['attempt']==2
    assert configured.post(path,json=body).status_code==409
    run(monkeypatch);pro.deliver(topic['id'])
    assert len(rows(configured))==1


def test_retry_authentication_csrf_and_owner_scope(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch,'not json')
    path='/api/conversation-topics/'+topic['id']+'/generation-retry'
    body={'version':1,'draft_version':1,'acknowledgement':'retry_one_model_call'}
    with TestClient(app) as anonymous:assert anonymous.post(path,json=body).status_code==401
    configured.headers.pop('X-CSRF-Token')
    assert configured.post(path,json=body).status_code==403


def test_lease_reclaim_does_not_replay_an_already_started_call(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id'])
    with SessionLocal.begin() as db:
        db.get(gen.ProactiveDraft,draft(topic).id).status='running'
    assert run(monkeypatch)==[]
    assert draft(topic).status=='failed' and not rows(configured)


def test_ready_generation_invalidates_when_fixed_provider_changes(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch)
    with SessionLocal.begin() as db:db.get(Provider,'provider').version+=1
    pro.deliver(topic['id'])
    assert not rows(configured) and draft(topic).status=='invalidated' and not draft(topic).content


def test_restore_quarantines_pending_generation(configured,monkeypatch):
    from lingxi.backups import checkpoint, quarantine
    from sqlalchemy import text
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch)
    # Reuse the public checkpoint contract; restoration must not replay model work.
    with SessionLocal.begin() as db:
        db.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)'))
        db.execute(text('DELETE FROM alembic_version'));db.execute(text("INSERT INTO alembic_version VALUES ('0039')"))
    payload=checkpoint()
    assert payload['proactive_drafts'][0]['content']==''
    with SessionLocal.begin() as db:db.delete(db.get(gen.ProactiveDraft,draft(topic).id))
    quarantine(payload)
    assert draft(topic).status=='invalidated' and not draft(topic).content


def test_concurrent_schedulers_reserve_only_one_automatic_generation(configured):
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('Requires PostgreSQL row locks')
    from concurrent.futures import ThreadPoolExecutor
    _,topic=setup(configured)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(pro.deliver,[topic['id'],topic['id']]))
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(gen.ProactiveDraft))==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind=='proactive_draft'))==1


def test_generated_plan_citation_never_claims_the_step_was_executed(configured,monkeypatch):
    from lingxi.tasks import WorkTask
    preferences(configured,quiet_start='00:00',quiet_end='00:00',model_generation=True)
    with SessionLocal.begin() as db:
        task=WorkTask(user_id='owner',request_id='progen-plan',request_hash='fixture',title='演示计划',
            skill_id='project.plan',input={'goal':'准备演示'},status='succeeded',
            result={'steps':[{'title':'提纲','action':'整理提纲','completion':'列出三个重点'}]})
        db.add(task);db.flush();pro.automatic_topic(db,task)
        topic=db.scalar(select(pro.ConversationTopic));topic.not_before=now()-1;identity=topic.id
    pro.deliver(identity)
    run(monkeypatch,VALID|{'source_id':'plan_step_1','quote':'整理提纲'})
    pro.deliver(identity)
    assert '之前生成的计划' in rows(configured)[0]['content']
    assert '已完成' not in rows(configured)[0]['content']


def test_plugin_report_is_labeled_external_and_revocation_removes_draft(configured,monkeypatch):
    from test_plugin_events import setup as event_setup, signed, topic_for
    from lingxi.plugin_events import EventSubscription
    preferences(configured,quiet_start='00:00',quiet_end='00:00',model_generation=True)
    _,subscription=event_setup(configured)
    assert signed(configured,subscription).status_code==200
    topic=topic_for(subscription);pro.deliver(topic.id)
    run(monkeypatch,VALID|{'source_id':'plugin_report','quote':'远程服务报告新增了三页资料，尚未核验。'})
    with SessionLocal.begin() as db:
        sub=db.get(EventSubscription,subscription['id']);sub.enabled=False;sub.version+=1
    pro.deliver(topic.id)
    with SessionLocal() as db:
        current=db.scalar(select(gen.ProactiveDraft).where(gen.ProactiveDraft.topic_id==topic.id))
        assert current.status=='invalidated' and not current.content
    assert not rows(configured)


def test_qq_disconnect_after_generation_waits_and_does_not_fallback(configured,monkeypatch):
    from test_proactive_channels import setup as qq_setup
    from lingxi.channel_gateway import close_connection
    channel,connection,_=qq_setup(configured)
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch)
    close_connection(channel['id'],connection)
    pro.deliver(topic['id'])
    assert not rows(configured) and draft(topic).status=='ready'


def test_plain_failure_never_exposes_upstream_secret_or_retries(configured,monkeypatch):
    _,topic=setup(configured);pro.deliver(topic['id']);run(monkeypatch,RuntimeError('sensitive-provider-secret'))
    listing=configured.get('/api/conversation-topics').json()['items'][0]
    assert 'sensitive-provider-secret' not in json.dumps(listing)
    assert listing['generation']['status']=='failed'
    assert '未自动重试' in listing['generation']['error']


def test_source_larger_than_context_budget_is_not_silently_truncated(configured):
    source,topic=setup(configured)
    with SessionLocal.begin() as db:db.get(Memory,source['id']).content='很长的原始来源'*6000
    pro.deliver(topic['id'])
    assert draft(topic).status=='failed' and '预算' in draft(topic).error and not rows(configured)


def test_manual_retry_cannot_overlap_another_topics_generation(configured,monkeypatch):
    _,one=setup(configured);pro.deliver(one['id']);run(monkeypatch,'invalid json')
    source=configured.post('/api/memories',json={'content':'第二个项目记录','category':'preferences'}).json()
    two=configured.post('/api/conversation-topics',json={'source_type':'memory','source_id':source['id'],
        'title':'另一个话题','not_before':now()-1,'expires_at':now()+86400}).json()
    pro.deliver(two['id']);assert draft(two).status=='queued'
    current=next(row for row in configured.get('/api/conversation-topics').json()['items'] if row['id']==one['id'])
    response=configured.post('/api/conversation-topics/'+one['id']+'/generation-retry',json={
        'version':current['version'],'draft_version':current['generation']['version'],'acknowledgement':'retry_one_model_call'})
    assert response.status_code==409 and '其他话题' in response.json()['detail']
    assert draft(one).attempt==1
