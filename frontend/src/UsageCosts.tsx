import React, {useEffect,useRef,useState} from 'react';
import {api} from './api';
import {ErrorNotice,Modal,useResource} from './ui';
import type {components} from './api.generated';

type Price=components['schemas']['PriceView'];
type PriceInput=components['schemas']['PriceInput'];
type Usage=components['schemas']['UsageView'];
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

export function ModelPrices({onSaved}:{onSaved:()=>void}) {
  const [open,setOpen]=useState(false);
  return <details className="data-card" onToggle={e=>setOpen(e.currentTarget.open)}><summary>模型单价</summary>{open&&<ModelPriceList onSaved={onSaved}/>}</details>;
}

function ModelPriceList({onSaved}:{onSaved:()=>void}) {
  const data=useResource<Price[]>('/api/admin/model-prices');
  const [selected,setSelected]=useState<Price|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
  return <><p className="muted">填写此服务实际适用的每百万 token 单价，币种支持 CNY／USD。系统不自动获取供应商报价，不换汇，不含税费、折扣、固定请求费和插件服务费。ASR／TTS 的时长或字符计费暂不适用。</p><button className="button" onClick={data.reload}>重新加载</button><ErrorNotice error={error||data.error}/>{notice&&<p role="status">{notice}</p>}{!data.data&&!data.error&&<p role="status">正在读取模型单价…</p>}{data.data?.length===0&&<p className="data-empty">请先在模型目录添加模型。</p>}<div className="card-grid">{data.data?.map(row=><article className="data-card" key={row.model_id}><h3>{row.model}</h3><p>{row.provider} · {row.purpose} · 价格 v{row.version}</p><p>{!row.version?'尚未定价':!row.enabled?'价格停用':!row.current?'连接变化，需重新核对':'价格生效'}</p>{row.version>0&&<p>{text(row.rates.currency)}／百万 token · 输入 {text(row.rates.input_per_million)} · 输出 {text(row.rates.output_per_million)}</p>}<button className="button" onClick={()=>{setError('');setSelected(row);}}>设置单价</button></article>)}</div>{selected&&<PriceEditor row={selected} close={()=>setSelected(null)} saved={()=>{setSelected(null);setNotice('单价已保存，仅影响后续开始的调用；历史账目和正在执行的调用保持原计价快照。');data.reload();onSaved();}}/>}</>;
}

function PriceEditor({row,close,saved}:{row:Price;close:()=>void;saved:()=>void}) {
  const [draft,setDraft]=useState<PriceInput>(()=>({version:row.version,provider_version:row.provider_version,enabled:row.version?row.enabled:true,currency:row.rates.currency==='USD'?'USD':'CNY',input_per_million:String(row.rates.input_per_million??''),output_per_million:String(row.rates.output_per_million??''),cached_input_per_million:row.rates.cached_input_per_million?String(row.rates.cached_input_per_million):null,cache_write_per_million:row.rates.cache_write_per_million?String(row.rates.cache_write_per_million):null,acknowledgement:'estimate_only'}));
  const [preview,setPreview]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[ack,setAck]=useState(false);
  const context=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{context.current?.focus();},[preview]);
  const fields=[['input_per_million','输入单价'],['output_per_million','输出单价'],['cached_input_per_million','缓存读取'],['cache_write_per_million','缓存写入']] as const;
  return <Modal title="模型单价" close={close} dismissible={!busy}><p ref={context} tabIndex={-1}>{row.model} · {row.provider} · 服务 v{row.provider_version}</p><ErrorNotice error={error}/>{!preview?<form className="form-stack" onSubmit={e=>{e.preventDefault();setAck(false);setError('');setPreview(true);}}><label className="checkbox"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用估算</label><label>计价币种<select value={draft.currency} onChange={e=>setDraft({...draft,currency:e.target.value as 'CNY'|'USD'})}><option value="CNY">CNY 人民币</option><option value="USD">USD 美元</option></select></label>{fields.map(([key,label],index)=><label key={key}>{label}（每百万 token）<input name={key} inputMode="decimal" required={index<2} pattern="(0|[1-9][0-9]{0,5})(\.[0-9]{1,6})?" value={draft[key]??''} onChange={e=>setDraft({...draft,[key]:index<2?e.target.value:e.target.value||null})}/>{index>=2&&<small>可留空；有对应缓存用量但未填单价时，整次费用标为未知。</small>}</label>)}<p className="muted">单价非负，最多六位整数和六位小数；0 仅在服务确实免费时填写。不同用途的模型条目分别配置。</p><div className="actions"><button type="button" className="button" onClick={close}>暂不修改</button><button className="button primary">预览单价</button></div></form>:<><p>{draft.enabled?'启用':'停用'} {draft.currency} 估算。输入 {draft.input_per_million}、输出 {draft.output_per_million}、缓存读取 {draft.cached_input_per_million??'未知'}、缓存写入 {draft.cache_write_per_million??'未知'}，单位均为每百万 token。</p><p>只影响此模型后续开始的请求，不回填历史调用，不改变供应商实际收费。此估算不等于账单，也不会限制模型调用。</p><label className="checkbox"><input type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/>我已核对服务单价，理解这里只进行费用估算</label><div className="actions"><button className="button" disabled={busy} onClick={()=>setPreview(false)}>继续修改</button><button className="button primary" disabled={busy||!ack} onClick={()=>{if(busy||!ack)return;setBusy(true);setError('');void api('/api/admin/model-prices/'+encodeURIComponent(row.model_id),'PUT',draft).then(saved).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在保存':'保存单价'}</button></div></>}</Modal>;
}
