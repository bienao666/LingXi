"""Audio units, frozen rates, ownership and measured-vs-billed semantics."""
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from lingxi.audio_costs import AudioPrice, billing, summary
from lingxi.db import SessionLocal
from lingxi.models import ModelCall, Provider
from lingxi.voice import VoiceConfig, audio_call, effective_config
from test_voice import voice_config, wav


def choices(c):
    r=c.get('/api/admin/audio-prices');assert r.status_code==200,r.text
    return {row['kind']:row for row in r.json()}


def body(row, **kw):
    return dict(version=row['version'],voice_config_version=row['voice_config_version'],
        provider_version=row['provider_version'],enabled=True,currency='CNY',rate='6',
        acknowledgement='measured_input_estimate') | kw


def priced(c, kind='asr', **kw):
    row=choices(c)[kind]
    r=c.put('/api/admin/audio-prices/'+row['id'],json=body(row,**kw))
    assert r.status_code==200,r.text
    return r.json()


def test_directory_uses_voice_config_and_saves_without_network(configured):
    c=configured;assert choices(c)=={}
    voice_config(c)
    rows=choices(c);assert set(rows)=={'asr','tts'}
    assert rows['asr']['unit']=='per_minute' and rows['tts']['unit']=='per_million_characters'
    assert not rows['asr']['current'] and rows['asr']['rates']=={}
    saved=priced(c);assert saved['rates']['rate']=='6' and saved['current']
    with SessionLocal() as db:assert db.scalar(select(ModelCall)) is None
    c.headers.pop('X-CSRF-Token')
    assert c.put('/api/admin/audio-prices/'+saved['id'],json=body(saved)).status_code==403
    c.cookies.clear();assert c.get('/api/admin/audio-prices').status_code==401


@pytest.mark.parametrize('value',['-1','NaN','1e3','01','1000000','1.0000001',' 1','1.',True,1,None])
def test_invalid_unit_rate_rejected(configured,value):
    c=configured;voice_config(c);row=choices(c)['tts']
    assert c.put('/api/admin/audio-prices/'+row['id'],json=body(row,rate=value)).status_code==422


def test_versions_config_changes_scope_and_explicit_acknowledgement(configured):
    c=configured;voice_config(c);old=choices(c)['asr'];saved=priced(c)
    assert c.put('/api/admin/audio-prices/'+old['id'],json=body(old)).status_code==409
    assert c.put('/api/admin/audio-prices/'+old['id'],json=body(saved,acknowledgement='yes')).status_code==422
    assert c.put('/api/admin/audio-prices/foreign',json=body(saved)).status_code==404
    with SessionLocal.begin() as db:db.get(Provider,'provider').version=2
    assert not choices(c)['asr']['current']
    assert c.put('/api/admin/audio-prices/'+saved['id'],json=body(saved)).status_code==409
    current=choices(c)['asr'];assert priced(c,enabled=False)['enabled'] is False
    with SessionLocal.begin() as db:db.get(VoiceConfig,'owner').version+=1
    assert c.put('/api/admin/audio-prices/'+current['id'],json=body(current)).status_code==409
    with SessionLocal.begin() as db:db.get(Provider,'provider').workspace_id='foreign'
    assert choices(c)=={}


def test_foreign_price_cannot_be_read_or_overwritten(configured):
    c=configured;voice_config(c);row=choices(c)['asr']
    with SessionLocal.begin() as db:
        db.add(AudioPrice(id=row['id'],workspace_id='foreign',user_id='stranger',provider_id='provider',model='asr-model',kind='asr',version=1,config={'rate':'99'}))
    assert c.get('/api/admin/audio-prices').status_code==403
    assert c.put('/api/admin/audio-prices/'+row['id'],json=body(row)).status_code==403


def test_http_asr_freezes_rates_before_dispatch_and_keeps_only_measurements(configured,monkeypatch):
    c=configured;voice_config(c);saved=priced(c)
    def send(request):
        assert request.url.path.endswith('/audio/transcriptions')
        assert c.put('/api/admin/audio-prices/'+saved['id'],json=body(saved,rate='60')).status_code==200
        return httpx.Response(200,json={'text':'不持久化本次临时转写'})
    monkeypatch.setattr('lingxi.voice.client_for',lambda p:httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(send)))
    with SessionLocal() as db:config,provider=db.get(VoiceConfig,'owner'),db.get(Provider,'provider')
    assert audio_call(config,provider,'asr','asr-model',data=wav(.5))=='不持久化本次临时转写'
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall));assert call.billing['amount']=='0.05'
        assert call.billing['price']['version']==1 and call.billing['attribution']['channel']=='voice'
        assert call.config['audio_measurement']==dict(transport='http',input_frames=8000,input_sample_rate=16000,attempted=True)
        assert '_audio_price_snapshot' not in call.config and '不持久化' not in str(call.config)
        assert summary([call])['input_audio_ms']==500


def test_tts_counts_unicode_points_and_received_audio_not_playback(configured,monkeypatch):
    c=configured;voice_config(c);priced(c,'tts',rate='2')
    monkeypatch.setattr('lingxi.voice.client_for',lambda p:httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda _:httpx.Response(200,content=wav()))))
    with SessionLocal() as db:config,provider=db.get(VoiceConfig,'owner'),db.get(Provider,'provider')
    assert audio_call(effective_config(config,'alloy'),provider,'tts','tts-model',text='你🙂 。',channel='qq')
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall));assert call.billing['amount']=='0.000008'
        assert call.billing['attribution']['channel']=='qq' and call.config['audio_measurement']['input_characters']==4
        result=summary([call]);assert result['output_audio_ms']==200 and result['input_characters']==4
    data=c.get('/api/admin/usage').json();assert data['audio']['output_audio_ms']==200
    assert data['currencies'][0]['amount']=='0.000008'


def test_failure_and_budget_rejection_do_not_claim_zero_cost(configured,monkeypatch):
    from lingxi.model_budgets import BudgetExceeded
    c=configured;voice_config(c);priced(c,'tts')
    monkeypatch.setattr('lingxi.voice.client_for',lambda p:httpx.Client(base_url=p.base_url+'/',transport=httpx.MockTransport(lambda _:httpx.Response(429))))
    with SessionLocal() as db:config,provider=db.get(VoiceConfig,'owner'),db.get(Provider,'provider')
    with pytest.raises(Exception):audio_call(config,provider,'tts','tts-model',text='测试')
    with SessionLocal() as db:
        call=db.scalar(select(ModelCall));assert call.billing['amount'] is None and call.config['audio_measurement']['attempted']
    def blocked(*a,**kw):raise BudgetExceeded('daily')
    monkeypatch.setattr('lingxi.model_budgets.reserve',blocked)
    with pytest.raises(BudgetExceeded):audio_call(config,provider,'tts','tts-model',text='不发送')
    with SessionLocal() as db:
        call=list(db.scalars(select(ModelCall).order_by(ModelCall.created_at)))[-1]
        assert not call.config['audio_measurement']['attempted'] and call.billing['amount'] is None


def test_unknown_legacy_units_and_cancelled_measurements_never_infer_duration():
    price=dict(status='configured',unit='per_minute',currency='USD',rate='0')
    measured=dict(attempted=True,input_frames=2400,input_sample_rate=24000)
    assert Decimal(billing(price,measured,'completed')['amount'])==0
    assert billing(price,measured,'cancelled')['amount'] is None
    assert billing(price,measured|{'input_frames':True},'completed')['amount'] is None
    assert billing(price|{'unit':'tokens'},measured,'completed')['amount'] is None
    calls=[SimpleNamespace(purpose='asr',duration_ms=99000,config={}),
        SimpleNamespace(purpose='asr',config={'audio_measurement':measured}),
        SimpleNamespace(purpose='chat',config={})]
    total=summary(calls)
    assert total['calls']==2 and total['measured_calls']==1 and total['unknown_calls']==1 and total['input_audio_ms']==100


def test_per_call_measurements_and_rates_are_whitelisted(configured):
    with SessionLocal.begin() as db:
        db.add(ModelCall(model_id='voice:asr',purpose='asr',duration_ms=200,status='completed',
            usage={},config={'audio_measurement':{'attempted':True,'transport':'http','input_frames':8000,'input_sample_rate':16000,'recording':'private-wave','transcript':'private-text','input_characters':-1}},
            billing={'status':'estimated','amount':'0.05','currency':'CNY','price':{'rate':'6','kind':'asr','unit':'per_minute','credential':'private-key'}}))
    r=configured.get('/api/admin/model-call-pages');assert r.status_code==200,r.text
    row=r.json()['items'][0]
    assert row['config']['audio_measurement']=={'attempted':True,'transport':'http','input_frames':8000,'input_sample_rate':16000}
    assert row['billing']['price']=={'rate':'6','kind':'asr','unit':'per_minute'}
    assert 'private-' not in r.text
