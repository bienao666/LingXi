"""Local evidence and protocol fixtures only; no paid ranking probes."""
import pytest
from uuid import uuid4
from sqlalchemy import select

from lingxi.db import SessionLocal, now
from lingxi.models import ChatRun, Model, ModelCall, Provider
from lingxi.model_routing import ModelRouting
from lingxi.usage_costs import ModelPrice
from test_model_routing import candidates, body, confirm, submit as routing_submit, process, fixture, DONE


def submit(client):
    return routing_submit(client, request_id='selection-'+uuid4().hex)


def price(model_id, rate='1', **changes):
    with SessionLocal.begin() as db:
        model = db.get(Model, model_id)
        config = dict(enabled=True, currency='CNY', input_per_million=rate,
            output_per_million=rate, provider_id=model.provider_id, provider_version=1,
            model=model.name, source='owner_supplied', unit='per_million_tokens') | changes
        row = db.get(ModelPrice, model_id)
        if row:
            row.config = config; row.version += 1
        else:
            db.add(ModelPrice(model_id=model_id, user_id='owner', workspace_id='personal', version=1, config=config))


def calls(model_id, durations=(500, 600, 700), *, call_changes=None, run_changes=None, config_changes=None):
    with SessionLocal.begin() as db:
        model = db.get(Model, model_id)
        for duration in durations:
            run = ChatRun(user_id='owner', conversation_id='synthetic-only', request_id=str(now()),
                request_hash='fixture', status='completed', model_id=model_id,
                config={} if run_changes is None else run_changes)
            db.add(run); db.flush()
            data = dict(model_id=model_id, run_id=run.id, purpose='chat', status='completed',
                duration_ms=duration, config=dict(model=model.name, provider_id=model.provider_id,
                    provider_version=1, provider_protocol='openai') | (config_changes or {}))
            db.add(ModelCall(**(data | (call_changes or {}))))


def decision(run):
    return run['config']['routing']['selection']


def test_exact_decimal_cost_and_frozen_evidence(configured):
    candidates(); price('chat', '0.000002'); price('backup', '0.000001')
    confirm(configured, selection_strategy='estimated_cost', reference_input_tokens=1, reference_output_tokens=1)
    run, _ = submit(configured)
    d = decision(run)
    assert run['model_id'] == d['selected_model_id'] == 'backup'
    assert d['applied'] == 'estimated_cost'
    assert d['evidence'][1]['reference_cost'] == '0.000000000002'
    assert d['evidence'][1]['price_version'] == 1
    assert run['config']['routing']['candidates'][1]['model_id'] == 'chat'
    price('chat', '0')
    assert decision(configured.get('/api/chat-runs/'+run['id']).json()) == d
    assert submit(configured)[0]['model_id'] == 'chat'


@pytest.mark.parametrize('change', [
    {'currency':'USD'}, {'enabled':False}, {'provider_version':99}, {'provider_id':'elsewhere'},
    {'model':'renamed'}, {'input_per_million':'NaN'}, {'output_per_million':'-1'},
    {'input_per_million':1}, {'unit':'per_token'}, {'source':'external_text'},
])
def test_invalid_missing_stale_mixed_prices_retain_priority(configured, change):
    candidates(); price('chat', '10'); price('backup', '0', **change)
    confirm(configured, selection_strategy='estimated_cost')
    run, _ = submit(configured)
    assert run['model_id'] == 'chat' and decision(run)['applied'] == 'priority'
    assert decision(run)['evidence'][1]['status'] != 'ready'


@pytest.mark.parametrize('field,value', [('workspace_id','other'),('user_id','other')])
def test_other_owner_price_not_used(configured, field, value):
    candidates(); price('chat', '10'); price('backup', '0')
    with SessionLocal.begin() as db:
        setattr(db.get(ModelPrice, 'backup'), field, value)
    confirm(configured, selection_strategy='estimated_cost')
    run, _ = submit(configured)
    assert run['model_id'] == 'chat' and decision(run)['evidence'][1]['status'] == 'missing_price'


def test_unpriced_not_zero_and_currency_explicit(configured):
    candidates(); price('backup', '0', currency='USD')
    confirm(configured, selection_strategy='estimated_cost', reference_currency='USD')
    assert decision(submit(configured)[0])['applied'] == 'priority'
    price('chat', '9', currency='USD')
    run, _ = submit(configured)
    assert run['model_id'] == 'backup' and decision(run)['applied'] == 'estimated_cost'


def test_input_output_weights_and_stable_ties(configured):
    candidates(); price('chat', '1', output_per_million='10'); price('backup', '5', output_per_million='0')
    confirm(configured, selection_strategy='estimated_cost', reference_input_tokens=10, reference_output_tokens=1)
    assert submit(configured)[0]['model_id'] == 'chat'
    price('backup', '1', output_per_million='10')
    assert submit(configured)[0]['model_id'] == 'chat'


def test_filters_before_ranking_and_never_adds_recipient(configured):
    candidates(context=32768); price('backup', '50')
    with SessionLocal.begin() as db:
        db.add(Model(id='unapproved',provider_id='provider',name='cheapest',purpose='chat',status='verified',verified_provider_version=1))
    price('unapproved', '0')
    confirm(configured, selection_strategy='estimated_cost', minimum_context=16000)
    run, _ = submit(configured)
    assert run['model_id'] == 'backup'
    assert [e['model_id'] for e in decision(run)['evidence']] == ['backup']


def test_preview_snapshot_is_not_authorization_or_future_decision(configured):
    candidates(); price('chat', '8'); price('backup', '1')
    value = body(selection_strategy='estimated_cost')
    p = configured.post('/api/model-routing/preview', json=value).json()
    assert p['selection']['selected_model_id'] == 'backup'
    assert configured.get('/api/model-routing').json()['version'] == 0
    price('chat', '0')
    receipt = {k:p[k] for k in ('preview_digest','preview_expires_at')}
    assert configured.put('/api/model-routing', json=value | receipt | {'reference_input_tokens':999}).status_code == 409
    assert configured.put('/api/model-routing', json=value | receipt).status_code == 200
    assert submit(configured)[0]['model_id'] == 'chat'


@pytest.mark.parametrize('values', [{'selection_strategy':'fastest'}, {'reference_currency':'EUR'},
    {'reference_input_tokens':0}, {'reference_output_tokens':2000001}, {'reference_input_tokens':'1000'}])
def test_strategy_settings_strictly_validated(configured, values):
    candidates()
    assert configured.post('/api/model-routing/preview', json=body(**values)).status_code == 422


def test_fixed_mode_ignores_ranking_and_old_config_retains_priority(configured):
    candidates(); price('chat', '10'); price('backup', '0')
    confirm(configured, mode='fixed', selection_strategy='estimated_cost')
    assert submit(configured)[0]['model_id'] == 'chat'
    with SessionLocal.begin() as db:
        row = db.get(ModelRouting, 'personal')
        row.config = {k:v for k,v in row.config.items() if not k.startswith('reference_') and k!='selection_strategy'} | {'mode':'automatic'}
    assert configured.get('/api/model-routing').json()['selection_strategy'] == 'priority'
    assert decision(submit(configured)[0])['applied'] == 'priority'


def test_latency_median_not_mean_and_ties(configured):
    candidates(); calls('chat', (100,200,900000)); calls('backup', (300,350,400))
    confirm(configured, selection_strategy='observed_latency')
    run, _ = submit(configured)
    assert run['model_id'] == 'chat' and decision(run)['applied'] == 'observed_latency'
    assert decision(run)['evidence'][0]['median_ms'] == '200'
    assert decision(run)['evidence'][0]['samples'] == 3


def test_latency_missing_samples_not_fastest(configured):
    candidates(); calls('chat'); calls('backup',(1,2))
    confirm(configured, selection_strategy='observed_latency')
    run, _ = submit(configured)
    assert run['model_id'] == 'chat' and decision(run)['applied'] == 'priority'
    assert decision(run)['evidence'][1]['status'] == 'insufficient_samples'


@pytest.mark.parametrize('changes', [
    {'call_changes':{'status':'failed'}}, {'call_changes':{'status':'cancelled'}},
    {'call_changes':{'purpose':'extraction'}}, {'call_changes':{'created_at':1}},
    {'call_changes':{'created_at':9999999999}}, {'call_changes':{'workspace_id':'other'}},
    {'config_changes':{'model':'old-name'}}, {'config_changes':{'provider_version':99}},
    {'config_changes':{'provider_id':'different'}}, {'config_changes':{'provider_id':None}},
    {'config_changes':{'provider_protocol':'gemini'}}, {'config_changes':{'validation':True}},
    {'config_changes':{'task_id':'task'}}, {'config_changes':{'agent_run_id':'agent'}},
    {'config_changes':{'invocation_id':'plugin'}}, {'run_changes':{'task_assist':{'mode':'collaboration'}}},
    {'run_changes':{'voice_session_id':'voice'}}, {'run_changes':{'channel':{'owner_id':'other'}}},
    {'run_changes':{'voice_input':{'session_id':'voice'}}},
])
def test_invalid_latency_records_excluded(configured, changes):
    candidates(); calls('chat'); calls('backup', **changes)
    confirm(configured, selection_strategy='observed_latency')
    d = decision(submit(configured)[0])
    assert d['applied'] == 'priority' and d['evidence'][1]['samples'] == 0


def test_failed_run_and_invalid_duration_not_latency_evidence(configured):
    candidates(); calls('chat'); calls('backup', (0,-1,3600001))
    calls('backup')
    with SessionLocal.begin() as db:
        for row in db.scalars(select(ChatRun).where(ChatRun.model_id=='backup')):
            row.status = 'cancelled'
    confirm(configured, selection_strategy='observed_latency')
    assert decision(submit(configured)[0])['evidence'][1]['samples'] == 0


def test_latency_reads_bounded_samples_and_latest_twenty(configured):
    candidates(); calls('chat', (1000,)*20); calls('backup',(900,)*20); calls('backup',(10,)*20)
    confirm(configured, selection_strategy='observed_latency')
    run, _ = submit(configured)
    assert run['model_id'] == 'backup'
    assert decision(run)['evidence'][1]['median_ms'] == '10'
    assert decision(run)['evidence'][1]['samples'] == 20
    calls('backup',(1,)*200,config_changes={'validation':True})
    assert decision(submit(configured)[0])['applied'] == 'priority'


def test_ranked_run_executes_frozen_recipient_without_probe(configured, monkeypatch):
    candidates(); price('chat', '10'); price('backup','1')
    confirm(configured, selection_strategy='estimated_cost')
    run, _ = submit(configured)
    price('chat', '0')
    seen = fixture(monkeypatch, [DONE])
    process()
    result = configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status'] == 'completed' and result['model_id'] == 'backup'
    assert len(seen) == 1 and seen[0][1]['model'] == 'backup-model'
    assert decision(result)['evidence'][0]['price_version'] == 1
    entries = configured.get('/api/admin/model-call-pages?purpose=chat').json()['items']
    assert entries[0]['config']['routing']['selection_applied'] == 'estimated_cost'
    assert 'evidence' not in entries[0]['config']['routing']


def test_fallback_ranked_after_context_filter(configured):
    candidates()
    with SessionLocal.begin() as db:
        db.add(Model(id='third',provider_id='provider',name='third',purpose='chat',status='verified',verified_provider_version=1))
    for model_id, rate in [('chat','2'),('backup','3'),('third','1')]:price(model_id,rate)
    confirm(configured, selection_strategy='estimated_cost',candidate_ids=['chat','backup','third'])
    run, _ = submit(configured)
    assert [p['model_id'] for p in run['config']['routing']['candidates']] == ['third','chat']
    assert run['config']['routing']['fallback_selection']['applied'] == 'estimated_cost'


def test_ranked_route_cannot_bypass_execution_quota(configured, monkeypatch):
    from test_model_budgets import save as save_budget
    candidates(); price('chat','10'); price('backup','1')
    confirm(configured,selection_strategy='estimated_cost')
    save_budget(configured,execution=1)
    run,_=submit(configured)
    seen=fixture(monkeypatch,[503,DONE])
    process()
    result=configured.get('/api/chat-runs/'+run['id']).json()
    assert result['status']=='failed' and len(seen)==1
    assert seen[0][1]['model']=='backup-model'
