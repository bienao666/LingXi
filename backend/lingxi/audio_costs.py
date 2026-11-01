"""Measured audio inputs and owner supplied unit rates; never a provider invoice."""
import hashlib
import io
import json
import wave
from decimal import Decimal, localcontext
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, SessionLocal, now
from .models import Entity, Provider, User
from .usage_costs import RATE
from .web import Context, audit

router = APIRouter()
Kind = Literal['asr', 'tts', 'realtime_asr', 'realtime_tts']
NAMES = {'asr': '语音识别', 'tts': '语音合成', 'realtime_asr': '实时转写', 'realtime_tts': '实时合成'}


class AudioPrice(Entity, Base):
    __tablename__ = 'audio_prices'
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)


class AudioPriceInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    voice_config_version: int = Field(ge=1)
    provider_version: int = Field(ge=1)
    enabled: bool = True
    currency: Literal['CNY', 'USD'] = 'CNY'
    rate: str = Field(pattern=RATE)
    acknowledgement: Literal['measured_input_estimate']


class AudioPriceView(BaseModel):
    id: str
    provider: str
    provider_version: int
    model: str
    kind: Kind
    unit: Literal['per_minute', 'per_million_characters']
    voice_config_version: int
    version: int
    enabled: bool
    current: bool
    rates: dict


class AudioUsageView(BaseModel):
    calls: int = 0
    measured_calls: int = 0
    unknown_calls: int = 0
    input_audio_ms: int = 0
    output_audio_ms: int = 0
    input_characters: int = 0
    note: str = '本地请求／接收量，不等于已播放时长或供应商计费用量；失败和取消仍可能收费。'


def identity(user_id, workspace, provider_id, model, kind):
    return hashlib.sha256(json.dumps([user_id, workspace, provider_id, model, kind], ensure_ascii=False).encode()).hexdigest()


def unit(kind):
    return 'per_minute' if kind in ('asr', 'realtime_asr') else 'per_million_characters'


def slots(db, user):
    from .voice import VoiceConfig
    config = db.get(VoiceConfig, user.id)
    if not config or config.workspace_id != user.workspace_id:
        return []
    result = []
    for kind in NAMES:
        spec = config.spec.get(kind, {})
        provider = db.get(Provider, spec.get('provider_id', ''))
        model = spec.get('model', '')
        if not provider or provider.workspace_id != user.workspace_id or not model:
            continue
        key = identity(user.id, user.workspace_id, provider.id, model, kind)
        row = db.get(AudioPrice, key)
        if row and (row.user_id != user.id or row.workspace_id != user.workspace_id):
            raise HTTPException(403, '语音单价归属不符')
        rates = row.config if row else {}
        result.append(dict(id=key, provider=provider.name, provider_version=provider.version, model=model,
            kind=kind, unit=unit(kind), voice_config_version=config.version, version=row.version if row else 0,
            enabled=bool(rates.get('enabled')), current=bool(row and rates.get('provider_version') == provider.version), rates=rates))
    return result


@router.get('/api/admin/audio-prices', response_model=list[AudioPriceView])
def prices(ctx: Context):
    db, user, _ = ctx
    return slots(db, user)


@router.put('/api/admin/audio-prices/{key}', response_model=AudioPriceView)
def save(key: str, body: AudioPriceInput, ctx: Context):
    db, user, _ = ctx
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    candidate = next((item for item in slots(db, user) if item['id'] == key), None)
    if not candidate:
        raise HTTPException(404, '当前语音配置中没有此模型，请重新加载')
    if (candidate['version'] != body.version or candidate['voice_config_version'] != body.voice_config_version
        or candidate['provider_version'] != body.provider_version):
        raise HTTPException(409, '语音配置、连接或单价已变化，请重新加载后核对')
    from .voice import VoiceConfig
    spec = db.get(VoiceConfig, user.id).spec[candidate['kind']]
    rates = dict(enabled=body.enabled, currency=body.currency, rate=body.rate, unit=candidate['unit'],
        provider_version=body.provider_version, source='owner_supplied', updated_at=now())
    row = db.get(AudioPrice, key)
    if not row:
        row = AudioPrice(id=key, workspace_id=user.workspace_id, user_id=user.id,
            provider_id=spec['provider_id'], model=spec['model'], kind=candidate['kind'], version=1, config=rates)
        db.add(row)
    else:
        row.version, row.config = row.version + 1, rates
    audit(db, user, 'audio_price_update', key)
    db.flush()
    return next(item for item in slots(db, user) if item['id'] == key)


def snapshot(config, provider, model, kind):
    owner = getattr(config, 'id', None)
    if not owner:
        return {'status': 'unpriced'}
    with SessionLocal() as db:
        key = identity(owner, provider.workspace_id, provider.id, model, kind)
        row = db.get(AudioPrice, key)
        if (not row or row.user_id != owner or row.workspace_id != provider.workspace_id
            or row.provider_id != provider.id or row.model != model or row.kind != kind
            or not row.config.get('enabled') or row.config.get('provider_version') != provider.version):
            return {'status': 'unpriced'}
        return {'status': 'configured', 'id': row.id, 'version': row.version, 'kind': kind, **row.config}


def wav_measurement(data, prefix):
    # Call only on the bounded WAV already accepted by the shared voice parser.
    with wave.open(io.BytesIO(data), 'rb') as audio:
        return {prefix + '_frames': audio.getnframes(), prefix + '_sample_rate': audio.getframerate()}


def frames(value, prefix):
    count, rate = value.get(prefix + '_frames'), value.get(prefix + '_sample_rate')
    if type(count) is int and 0 <= count <= 48000 * 60 and type(rate) is int and 8000 <= rate <= 48000:
        return count, rate
    return None


def public_measurement(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if type(value.get('attempted')) is bool:
        result['attempted'] = value['attempted']
    if value.get('transport') in ('http', 'realtime'):
        result['transport'] = value['transport']
    for prefix in ('input', 'output'):
        pair = frames(value, prefix)
        if pair:
            result[prefix + '_frames'], result[prefix + '_sample_rate'] = pair
    count = value.get('input_characters')
    if type(count) is int and 0 <= count <= 12000:
        result['input_characters'] = count
    return result


def billing(price, measurement, status):
    unknown = dict(status='unknown', reason='unpriced', amount=None, currency=None)
    if not price or price.get('status') != 'configured':
        return unknown
    unknown |= {'currency': price['currency'], 'price': price, 'reason': 'incomplete_audio_measurement'}
    if not isinstance(measurement, dict) or not measurement.get('attempted') or status != 'completed':
        return unknown
    with localcontext() as context:
        context.prec = 40
        if price['unit'] == 'per_minute':
            quantity = frames(measurement, 'input')
            if not quantity or not quantity[0]:
                return unknown
            amount = Decimal(quantity[0]) * Decimal(price['rate']) / (Decimal(quantity[1]) * 60)
        elif price['unit'] == 'per_million_characters':
            count = measurement.get('input_characters')
            if type(count) is not int or not 1 <= count <= 12000:
                return unknown
            amount = Decimal(count) / 1000000 * Decimal(price['rate'])
        else:
            return unknown | {'reason': 'unsupported_unit'}
    return dict(status='estimated', reason='measured_input_owner_rates', currency=price['currency'],
        amount=format(amount, 'f'), price=price)


def summary(calls):
    result = AudioUsageView().model_dump()
    for call in calls:
        if call.purpose not in ('asr', 'tts'):
            continue
        result['calls'] += 1
        value = (call.config or {}).get('audio_measurement', {})
        if not isinstance(value, dict) or not value.get('attempted'):
            result['unknown_calls'] += 1
            continue
        measured = False
        for prefix in ('input', 'output'):
            pair = frames(value, prefix)
            if pair:
                result[prefix + '_audio_ms'] += pair[0] * 1000 // pair[1]
                measured = True
        count = value.get('input_characters')
        if type(count) is int and 1 <= count <= 12000:
            result['input_characters'] += count
            measured = True
        result['measured_calls' if measured else 'unknown_calls'] += 1
    return result
