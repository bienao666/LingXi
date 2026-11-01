import React, {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {ErrorNotice,Modal,useResource} from './ui';
import type {components} from './api.generated';

type Price=components['schemas']['PriceView'];
type PriceInput=components['schemas']['PriceInput'];
type Usage=components['schemas']['UsageView'];
type AudioPrice=components['schemas']['AudioPriceView'];
type AudioPriceInput=components['schemas']['AudioPriceInput'];
type AudioUsageData=components['schemas']['AudioUsageView'];
const name:Record<string,string>={channel:'来源渠道',model:'模型分组',task:'任务分组',plugin:'插件分组',web:'Web 对话',qq:'QQ 消息',voice:'浏览语音',validation:'模型验证',unknown:'未关联'};
const text=(v:unknown)=>typeof v==='string'||typeof v==='number'?String(v):'未知';

export function CostAmounts({rows}:{rows?:Record<string,unknown>[]}) {
  if(!rows?.length)return <>调用费用未知</>;
  return <>{rows.map((row,index)=><span className="wrap" key={index}>{index>0?' · ':''}{text(row.currency)} {text(row.amount)}</span>)}</>;
}

export function CostBreakdowns({usage}:{usage:Usage}) {
  const [dimension,setDimension]=useState('channel');
  const group=usage.breakdowns?.[dimension] as {items?:{key:string;calls:number;currencies:Record<string,unknown>[];unknown_cost_calls:number}[];truncated?:boolean}|undefined;
  return <section className="page-stack" aria-label="费用归属"><label>费用归属<select value={dimension} onChange={e=>setDimension(e.target.value)}>{['channel','model','task','plugin'].map(key=><option value={key} key={key}>{name[key]}</option>)}</select></label><p className="muted">各分组展示同一批调用的不同归属，不能再次相加。已知估算按币种分别汇总，未关联不等于没有调用。任务编号不包含任务正文；不统计外部插件服务费。</p><div className="card-grid">{group?.items?.map(row=><article className="data-card" key={row.key}><h3>{dimension==='channel'?name[row.key]||row.key:row.key==='unknown'?'未关联':name[dimension]}</h3>{dimension!=='channel'&&row.key!=='unknown'&&<code className="plugin-digest">{row.key}</code>}<p>调用 {row.calls} · 费用未知 {row.unknown_cost_calls} 次</p><p>已知估算：<CostAmounts rows={row.currencies}/></p></article>)}</div>{group?.items?.length===0&&<p className="data-empty">此时间范围暂无调用。</p>}{group?.truncated&&<p role="status">分组超过 1000 项，只显示前 1000 项；总计仍包含所选范围全部调用。</p>}</section>;
}

export function AudioUsage({audio}:{audio?:AudioUsageData}){
  if(!audio)return <section className="data-card"><h3>语音用量</h3><p>语音计量暂不可用，历史费用不回填。</p></section>;
  const seconds=(value:number)=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:3}).format(value/1000);
  return <section className="page-stack" aria-label="语音用量"><h3>语音用量</h3><div className="card-grid"><article className="data-card"><h4>识别输入</h4><strong>{seconds(audio.input_audio_ms??0)} 秒</strong><p>已发起请求中的本地音频量</p></article><article className="data-card"><h4>合成文本</h4><strong>{(audio.input_characters??0).toLocaleString()} 字符</strong><p>按 Unicode 码点计数，含空白与标点</p></article><article className="data-card"><h4>音频输出</h4><strong>{seconds(audio.output_audio_ms??0)} 秒</strong><p>收到的有效音频，不等于实际播完</p></article></div><p className="muted">{audio.note} 已计量 {audio.measured_calls}／{audio.calls} 次，计量未知 {audio.unknown_calls} 次；旧记录不按网络耗时推算音频时长。</p></section>;
}

const audioNames:Record<string,string>={asr:'语音识别',tts:'语音合成',realtime_asr:'实时转写',realtime_tts:'实时合成'};
const audioUnit=(kind:string)=>kind==='per_minute'?'每分钟音频':'每百万字符';

export function AudioPrices({onSaved}:{onSaved:()=>void}){
  const [open,setOpen]=useState(false);
  return <details className="data-card" onToggle={e=>setOpen(e.currentTarget.open)}><summary>语音单价</summary>{open&&<AudioPriceList onSaved={onSaved}/>}</details>;
}

function AudioPriceList({onSaved}:{onSaved:()=>void}){
  const data=useResource<AudioPrice[]>('/api/admin/audio-prices');
  const [selected,setSelected]=useState<AudioPrice|null>(null),[notice,setNotice]=useState('');
  return <div className="page-stack"><p className="muted">只适用于供应商确实按输入音频时长或合成文本字符计费的服务。按本地输入量估算，不读取供应商报价，不换汇，不限额；token、分档、取整或最低收费等其他方式保持未知。单价与语音开关独立，保存不会发起模型请求。</p><div className="actions"><button className="button" onClick={data.reload}>重新加载</button><a href="#/settings/voice">语音配置</a></div><ErrorNotice error={data.error}/>{notice&&<p role="status">{notice}</p>}{!data.data&&!data.error&&<p role="status">正在读取语音单价…</p>}{data.data?.length===0&&<p className="data-empty">请先在语音配置选择识别或合成模型。</p>}<div className="card-grid">{data.data?.map(row=><article className="data-card" key={row.id}><h3>{audioNames[row.kind]}</h3><p>{row.provider} · {row.model}</p><p>{!row.version?'尚未定价':!row.enabled?'估算停用':!row.current?'连接变化，需重新核对':'价格生效'} · v{row.version}</p>{row.version>0&&<p>{text(row.rates.currency)} {text(row.rates.rate)}／{audioUnit(row.unit).replace('每','')}</p>}<button className="button" onClick={()=>setSelected(row)}>设置单价</button></article>)}</div>{selected&&<AudioPriceEditor row={selected} close={()=>setSelected(null)} saved={()=>{setSelected(null);setNotice('语音单价已保存，只用于后续请求；历史调用与正在执行的请求不重计。');data.reload();onSaved();}}/>}</div>;
}

function AudioPriceEditor({row,close,saved}:{row:AudioPrice;close:()=>void;saved:()=>void}){
  const [draft,setDraft]=useState<AudioPriceInput>({version:row.version,voice_config_version:row.voice_config_version,provider_version:row.provider_version,enabled:row.version?row.enabled:true,currency:row.rates.currency==='USD'?'USD':'CNY',rate:typeof row.rates.rate==='string'?row.rates.rate:'',acknowledgement:'measured_input_estimate'});
  const [review,setReview]=useState(false),[ack,setAck]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const context=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{context.current?.focus();},[review]);
  return <Modal title="语音单价" close={close} dismissible={!busy}><p ref={context} tabIndex={-1}>{audioNames[row.kind]} · {row.provider} · {row.model}</p><ErrorNotice error={error}/>{!review?<form className="form-stack" onSubmit={e=>{e.preventDefault();setError('');setAck(false);setReview(true);}}><label className="checkbox"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用估算</label><label>计价币种<select value={draft.currency} onChange={e=>setDraft({...draft,currency:e.target.value as 'CNY'|'USD'})}><option value="CNY">CNY 人民币</option><option value="USD">USD 美元</option></select></label><label>{audioUnit(row.unit)}单价<input name="audio_rate" inputMode="decimal" required pattern="(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?" value={draft.rate} onChange={e=>setDraft({...draft,rate:e.target.value})}/></label><p className="muted">单价非负，最多六位整数、六位小数；缺失不是免费，只有服务确实免费时填写 0。供应商使用其他计费单位时请勿套用。</p><div className="actions"><button type="button" className="button" onClick={close}>暂不修改</button><button className="button primary">预览单价</button></div></form>:<><p>{draft.enabled?'启用':'停用'} {draft.currency} {draft.rate}／{audioUnit(row.unit).replace('每','')}，只用于当前连接版本的后续请求，不回填历史。失败或取消不能证明供应商不收费，费用保留未知。</p><label className="checkbox"><input type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/>已核对供应商计费单位，理解这是本地输入量估算</label><div className="actions"><button className="button" disabled={busy} onClick={()=>{setReview(false);setAck(false);}}>继续修改</button><button className="button primary" disabled={busy||!ack} onClick={()=>{if(busy||!ack)return;setBusy(true);setError('');void api('/api/admin/audio-prices/'+encodeURIComponent(row.id),'PUT',draft).then(saved).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在保存':'保存单价'}</button></div></>}</Modal>;
}

export function ModelPrices({onSaved}:{onSaved:()=>void}) {
  const [open,setOpen]=useState(false);
  return <details className="data-card" onToggle={e=>setOpen(e.currentTarget.open)}><summary>模型单价</summary>{open&&<ModelPriceList onSaved={onSaved}/>}</details>;
}

function ModelPriceList({onSaved}:{onSaved:()=>void}) {
  const data=useResource<Price[]>('/api/admin/model-prices');
  const [selected,setSelected]=useState<Price|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
  return <><p className="muted">填写此服务实际适用的每百万 token 单价，币种支持 CNY／USD。系统不自动获取供应商报价，不换汇，不含税费、折扣、固定请求费和插件服务费。语音的时长／字符单价请在“语音单价”独立配置。</p><button className="button" onClick={data.reload}>重新加载</button><ErrorNotice error={error||data.error}/>{notice&&<p role="status">{notice}</p>}{!data.data&&!data.error&&<p role="status">正在读取模型单价…</p>}{data.data?.length===0&&<p className="data-empty">请先在模型目录添加模型。</p>}<div className="card-grid">{data.data?.map(row=><article className="data-card" key={row.model_id}><h3>{row.model}</h3><p>{row.provider} · {row.purpose} · 价格 v{row.version}</p><p>{!row.version?'尚未定价':!row.enabled?'价格停用':!row.current?'连接变化，需重新核对':'价格生效'}</p>{row.version>0&&<p>{text(row.rates.currency)}／百万 token · 输入 {text(row.rates.input_per_million)} · 输出 {text(row.rates.output_per_million)}</p>}<button className="button" onClick={()=>{setError('');setSelected(row);}}>设置单价</button></article>)}</div>{selected&&<PriceEditor row={selected} close={()=>setSelected(null)} saved={()=>{setSelected(null);setNotice('单价已保存，仅影响后续开始的调用；历史账目和正在执行的调用保持原计价快照。');data.reload();onSaved();}}/>}</>;
}

function PriceEditor({row,close,saved}:{row:Price;close:()=>void;saved:()=>void}) {
  const [draft,setDraft]=useState<PriceInput>(()=>({version:row.version,provider_version:row.provider_version,enabled:row.version?row.enabled:true,currency:row.rates.currency==='USD'?'USD':'CNY',input_per_million:String(row.rates.input_per_million??''),output_per_million:String(row.rates.output_per_million??''),cached_input_per_million:row.rates.cached_input_per_million?String(row.rates.cached_input_per_million):null,cache_write_per_million:row.rates.cache_write_per_million?String(row.rates.cache_write_per_million):null,acknowledgement:'estimate_only'}));
  const [preview,setPreview]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[ack,setAck]=useState(false);
  const context=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{context.current?.focus();},[preview]);
  const fields=[['input_per_million','输入单价'],['output_per_million','输出单价'],['cached_input_per_million','缓存读取'],['cache_write_per_million','缓存写入']] as const;
  return <Modal title="模型单价" close={close} dismissible={!busy}><p ref={context} tabIndex={-1}>{row.model} · {row.provider} · 服务 v{row.provider_version}</p><ErrorNotice error={error}/>{!preview?<form className="form-stack" onSubmit={e=>{e.preventDefault();setAck(false);setError('');setPreview(true);}}><label className="checkbox"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用估算</label><label>计价币种<select value={draft.currency} onChange={e=>setDraft({...draft,currency:e.target.value as 'CNY'|'USD'})}><option value="CNY">CNY 人民币</option><option value="USD">USD 美元</option></select></label>{fields.map(([key,label],index)=><label key={key}>{label}（每百万 token）<input name={key} inputMode="decimal" required={index<2} pattern="(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?" value={draft[key]??''} onChange={e=>setDraft({...draft,[key]:index<2?e.target.value:e.target.value||null})}/>{index>=2&&<small>可留空；有对应缓存用量但未填单价时，整次费用标为未知。</small>}</label>)}<p className="muted">单价非负，最多六位整数和六位小数；0 仅在服务确实免费时填写。不同用途的模型条目分别配置。</p><div className="actions"><button type="button" className="button" onClick={close}>暂不修改</button><button className="button primary">预览单价</button></div></form>:<><p>{draft.enabled?'启用':'停用'} {draft.currency} 估算。输入 {draft.input_per_million}、输出 {draft.output_per_million}、缓存读取 {draft.cached_input_per_million??'未知'}、缓存写入 {draft.cache_write_per_million??'未知'}，单位均为每百万 token。</p><p>只影响此模型后续开始的请求，不回填历史调用，不改变供应商实际收费。此估算不等于账单，也不会限制模型调用。</p><label className="checkbox"><input type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/>我已核对服务单价，理解这里只进行费用估算</label><div className="actions"><button className="button" disabled={busy} onClick={()=>setPreview(false)}>继续修改</button><button className="button primary" disabled={busy||!ack} onClick={()=>{if(busy||!ack)return;setBusy(true);setError('');void api('/api/admin/model-prices/'+encodeURIComponent(row.model_id),'PUT',draft).then(saved).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在保存':'保存单价'}</button></div></>}</Modal>;
}
