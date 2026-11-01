import time
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from lingxi.db import SessionLocal
from lingxi.models import Model, Provider, ModelCall, ChatRun, User
from lingxi.usage_costs import cost, snapshot, ModelPrice


def price(**changes):
    return dict(version=0, provider_version=1, enabled=True, currency='CNY', input_per_million='2',
        output_per_million='6', cached_input_per_million=None, cache_write_per_million=None,
        acknowledgement='estimate_only') | changes


def rate(**changes):
    return {'status':'configured', 'currency':'CNY', 'input_per_million':'2',
        'output_per_million':'6', 'cached_input_per_million':None, 'cache_write_per_million':None} | changes


def test_prices_require_owner_csrf_confirmation_and_scoped_model(configured):
    client=configured
    assert client.put('/api/admin/model-prices/chat',json=price()).status_code==200
    assert client.get('/api/admin/model-prices').json()[0]['rates']['source']=='owner_supplied'
    with SessionLocal.begin() as db:
        db.add(Provider(id='foreign',name='private',base_url='https://example.com',encrypted_key='secret',workspace_id='other'))
        db.add(Model(id='foreign-model',provider_id='foreign',name='hidden',workspace_id='other'))
    assert 'hidden' not in client.get('/api/admin/model-prices').text
    assert client.put('/api/admin/model-prices/foreign-model',json=price()).status_code==404
    assert client.put('/api/admin/model-prices/chat',json=price(acknowledgement='yes')).status_code==422
    client.headers.pop('X-CSRF-Token')
    assert client.put('/api/admin/model-prices/chat',json=price(version=1)).status_code==403
    client.cookies.clear()
    assert client.get('/api/admin/model-prices').status_code==401


@pytest.mark.parametrize('value',['-1','NaN','Infinity','1e3','01','1000000','1.0000001',' 1','1.',1,True,None])
def test_invalid_rate_rejected(configured,value):
    assert configured.put('/api/admin/model-prices/chat',json=price(input_per_million=value)).status_code==422


def test_versions_provider_changes_and_disabling(configured):
    client=configured
    assert client.put('/api/admin/model-prices/chat',json=price()).json()['version']==1
    assert client.put('/api/admin/model-prices/chat',json=price()).status_code==409
    with SessionLocal.begin() as db:db.get(Provider,'provider').version=2
    assert client.get('/api/admin/model-prices').json()[0]['current'] is False
    assert client.put('/api/admin/model-prices/chat',json=price(version=1)).status_code==409
    assert client.put('/api/admin/model-prices/chat',json=price(version=1,provider_version=2,enabled=False)).status_code==200
    with SessionLocal() as db:
        assert snapshot(db,db.get(Model,'chat'),db.get(Provider,'provider'))=={'status':'unpriced'}


@pytest.mark.parametrize('usage,reason',[
    ({},'incomplete_usage'),({'total_tokens':100},'incomplete_usage'),
    ({'prompt_tokens':True,'completion_tokens':3},'incomplete_usage'),
    ({'prompt_tokens':-1,'completion_tokens':3},'incomplete_usage'),
    ({'prompt_tokens':10,'completion_tokens':3,'total_tokens':14},'inconsistent_usage'),
    ({'prompt_tokens':10,'completion_tokens':3,'total_tokens':True},'inconsistent_usage'),
    ({'prompt_tokens':10,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':11}},'inconsistent_usage'),
    ({'prompt_tokens':10,'completion_tokens':3,'cache_creation_input_tokens':2},'missing_cache_rate'),
    ({'prompt_tokens':10,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':2}},'missing_cache_rate'),
    ({'prompt_tokens':10,'completion_tokens':3,'prompt_tokens_details':{'audio_tokens':1}},'unsupported_unit'),
])
def test_unknown_is_not_zero(usage,reason):
    result=cost(rate(),usage)
    assert result['amount'] is None and result['reason']==reason


def test_decimal_zero_cached_and_native_accounting():
    assert cost(None,{'prompt_tokens':10,'completion_tokens':3})['amount'] is None
    assert cost(rate(),{'prompt_tokens':1000000,'completion_tokens':500000})['amount']=='5'
    assert cost(rate(input_per_million='0',output_per_million='0'),{'input_tokens':10,'output_tokens':20})['amount']=='0'
    assert cost(rate(input_per_million='0.000001',output_per_million='0'),{'input_tokens':1,'output_tokens':0})['amount']=='0.000000000001'
    result=cost(rate(cached_input_per_million='0.5',cache_write_per_million='3'),
        {'prompt_tokens':100,'completion_tokens':20,'cache_read_input_tokens':30,'cache_creation_input_tokens':10})
    assert result['amount']=='0.000285'
    from lingxi.protocols import usage
    gemini=usage('gemini',{'promptTokenCount':100,'candidatesTokenCount':20,'cachedContentTokenCount':30,'totalTokenCount':120})
    assert cost(rate(cached_input_per_million='0.5'),gemini)['amount']=='0.000275'


def test_call_freezes_rate_before_network_and_preserves_prompt_version(configured,monkeypatch):
    client=configured
    client.put('/api/admin/model-prices/chat',json=price())
    from lingxi import gateway
    class Result:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'正常回复'}}],'usage':{'prompt_tokens':1000000,'completion_tokens':500000}}
    class Client:
        def post(self,*args,**kwargs):
            client.put('/api/admin/model-prices/chat',json=price(version=1,input_per_million='20'))
            return Result()
    @contextmanager
    def fake(_):yield Client()
    monkeypatch.setattr(gateway,'client_for',fake)
    with SessionLocal() as db:model,provider=db.get(Model,'chat'),db.get(Provider,'provider')
    assert gateway.complete(model,provider,[{'role':'user','content':'hello'}],purpose='chat',config={'prompt_version':'fixed-v9'})=='正常回复'
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall))
        assert call.billing['amount']=='5' and call.billing['price']['version']==1
        assert call.config['prompt_version']=='fixed-v9' and '_price_snapshot' not in call.config
    assert client.get('/api/admin/model-prices').json()[0]['version']==2


def test_stream_cancel_records_usage_with_start_snapshot(configured,monkeypatch):
    from lingxi import gateway
    configured.put('/api/admin/model-prices/chat',json=price())
    class Response:
        def raise_for_status(self):pass
        def iter_lines(self):
            yield 'data: {"usage":{"prompt_tokens":100,"completion_tokens":10},"choices":[]}'
            yield 'data: {"choices":[{"delta":{"content":"开始"}}]}'
            yield 'data: [DONE]'
    class Client:
        @contextmanager
        def stream(self,*a,**k):yield Response()
    @contextmanager
    def fake(_):yield Client()
    monkeypatch.setattr(gateway,'client_for',fake)
    with SessionLocal() as db:model,provider=db.get(Model,'chat'),db.get(Provider,'provider')
    stream=gateway.stream(model,provider,[{'role':'user','content':'hello'}],'missing-run',lambda:None,config={'prompt_version':'fixed-v10'})
    assert next(stream)=='开始'
    stream.close()
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall))
        assert call.status=='cancelled' and call.billing['amount']=='0.00026'
        assert call.config['prompt_version']=='fixed-v10'


def test_failed_content_still_records_reported_billable_usage(configured,monkeypatch):
    from lingxi import gateway
    configured.put('/api/admin/model-prices/chat',json=price())
    class Result:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':''}}],'usage':{'prompt_tokens':100,'completion_tokens':10}}
    class Client:
        def post(self,*a,**k):return Result()
    @contextmanager
    def fake(_):yield Client()
    monkeypatch.setattr(gateway,'client_for',fake)
    with SessionLocal() as db:model,provider=db.get(Model,'chat'),db.get(Provider,'provider')
    with pytest.raises(Exception):gateway.complete(model,provider,[{'role':'user','content':'test'}],purpose='chat')
    with SessionLocal() as db:
        row=db.scalar(select(ModelCall))
        assert row.status=='failed' and row.billing['amount']=='0.00026'


def test_aggregate_multiple_currencies_historical_unknown_and_redacted_diagnostics(configured):
    from lingxi.gateway import record_call,call_config
    from lingxi.operations import diagnostic_data
    client=configured
    for currency in ['USD','CNY']:
        version=0 if currency=='USD' else 1
        client.put('/api/admin/model-prices/chat',json=price(version=version,currency=currency))
        with SessionLocal() as db:config=call_config(db.get(Model,'chat'),db.get(Provider,'provider'),'chat')
        record_call('chat','chat',time.monotonic(),'completed',{'prompt_tokens':1000000,'completion_tokens':500000},config=config)
    with SessionLocal.begin() as db:db.add(ModelCall(model_id='historical',purpose='chat',duration_ms=2,status='failed'))
    data=client.get('/api/admin/usage?days=1').json()
    assert data['currencies']==[{'currency':'CNY','amount':'5','calls':1},{'currency':'USD','amount':'5','calls':1}]
    assert data['cost'] is None and data['unknown_cost_calls']==1 and data['calls']==3
    assert sum(r['calls'] for r in data['breakdowns']['channel']['items'])==3
    with SessionLocal() as db:
        diagnostic=diagnostic_data(db,db.get(User,'owner'),['usage'],1)['usage']
        assert all(g['key']!='chat' for g in diagnostic['breakdowns']['model']['items'])


def test_attribution_preserves_real_qq_channel_and_workspace(configured):
    from lingxi.gateway import record_call
    with SessionLocal.begin() as db:
        db.add(ChatRun(id='qq-run',user_id='owner',conversation_id='conversation',request_id='qq-request',request_hash='hash',model_id='chat',config={'channel':{'id':'qq'}}))
    record_call('chat','chat',time.monotonic(),'failed',run_id='qq-run')
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall))
        assert call.billing['attribution']['channel']=='qq' and call.billing['amount'] is None
    assert configured.get('/api/admin/usage?days=1').json()['breakdowns']['channel']['items'][0]['key']=='qq'


def test_task_and_plugin_attribution_and_foreign_reference_isolation(configured):
    from lingxi.tasks import WorkTask
    from lingxi.usage_costs import attribution
    from lingxi.plugins import PluginRelease
    with SessionLocal.begin() as db:
        release=db.scalar(select(PluginRelease).where(PluginRelease.plugin_id=='lingxi.builtin'))
        db.add(WorkTask(id='plan-task',user_id='owner',request_id='task-request',request_hash='hash',title='SECRET-goal',skill_id='project.plan',input={},
            config={'plugin_id':release.plugin_id,'plugin_release_id':release.id}))
    with SessionLocal() as db:
        value=attribution(db,'chat',None,{'task_id':'plan-task'},'personal')
        assert value=={'channel':'web','model':'chat','task':'plan-task','plugin':'lingxi.builtin'}
        assert attribution(db,'chat',None,{'task_id':'plan-task'},'other')['task']==''
    assert 'SECRET-goal' not in configured.get('/api/admin/usage').text


def test_direct_plugin_accounting_uses_scoped_invocation_release(configured):
    from lingxi.plugins import PluginInvocation,PluginRelease
    from lingxi.usage_costs import attribution
    with SessionLocal.begin() as db:
        release=db.scalar(select(PluginRelease).where(PluginRelease.plugin_id=='lingxi.builtin'))
        db.add(PluginInvocation(id='invocation',user_id='owner',request_id='invocation-request',request_hash='hash',
            installation_id='unused',release_id=release.id,capability_id='project.plan',input='SECRET-input'))
    with SessionLocal() as db:
        data=attribution(db,'chat',None,{'invocation_id':'invocation'},'personal')
        assert data['plugin']=='lingxi.builtin' and data['channel']=='web'
        assert attribution(db,'chat',None,{'invocation_id':'invocation'},'other')['plugin']==''


def test_aggregation_has_no_decimal_context_rounding():
    from types import SimpleNamespace
    from lingxi.usage_costs import money_summary
    calls=[SimpleNamespace(billing={'status':'estimated','currency':'CNY','amount':'99999999999999999.999999999999'}),
           SimpleNamespace(billing={'status':'estimated','currency':'CNY','amount':'0.000000000001'})]
    assert money_summary(calls)['currencies'][0]['amount']=='100000000000000000.000000000000'


def test_disabled_or_foreign_snapshot_cannot_apply_price(configured):
    configured.put('/api/admin/model-prices/chat',json=price())
    with SessionLocal.begin() as db:
        db.get(ModelPrice,'chat').workspace_id='other'
    with SessionLocal() as db:
        assert snapshot(db,db.get(Model,'chat'),db.get(Provider,'provider'))['status']=='unpriced'


def test_price_save_serializes_two_same_version_requests(configured):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from lingxi.db import engine
    if engine.dialect.name!='postgresql':pytest.skip('Owner row locking is verified against PostgreSQL')
    barrier=Barrier(2)
    def save():
        barrier.wait(timeout=5)
        return configured.put('/api/admin/model-prices/chat',json=price()).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(lambda _:save(),range(2)))
    assert sorted(statuses)==[200,409]
