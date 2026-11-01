import React,{useEffect,useRef,useState} from 'react';
import {api,ApiError} from './api';
import type {Candidate} from './api';
import type {components} from './api.generated';
import {Modal,formatTime} from './ui';
import {MemoryTimeDetails,memoryTimeFields} from './MemoryTimes';

type Sources=components['schemas']['DialogueCandidateSource'];
type Preview=components['schemas']['CandidateReviewPreview'];

export function DialogueCandidateReview({candidate,close,done}:{candidate:Candidate;close():void;done():void}){
  const [data,setData]=useState<Sources|null>(null),[query,setQuery]=useState(''),[search,setSearch]=useState(''),[revision,setRevision]=useState(0);
  const [target,setTarget]=useState(''),[times,setTimes]=useState({event_at:'',valid_from:'',valid_to:''});
  const [preview,setPreview]=useState<Preview|null>(null),[accepted,setAccepted]=useState(false),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState('');
  const pending=useRef(false),alert=useRef<HTMLParagraphElement>(null),heading=useRef<HTMLHeadingElement>(null);
  const trigger=useRef(document.activeElement as HTMLElement|null),saved=useRef(false);
  useEffect(()=>{if(error)alert.current?.focus();},[error]);
  useEffect(()=>{if(data)heading.current?.focus();},[data,preview]);
  useEffect(()=>{
    let active=true;const controller=new AbortController();setLoading(true);setData(null);setError('');setTarget('');setPreview(null);setAccepted(false);
    void api<Sources>(`/api/memory-candidates/${candidate.id}/sources?q=${encodeURIComponent(search)}`,'GET',undefined,{signal:controller.signal})
      .then(value=>{if(active)setData(value);}).catch(e=>{if(active)setError((e as Error).message);}).finally(()=>{if(active)setLoading(false);});
    return()=>{active=false;controller.abort();};
  },[candidate.id,search,revision]);
  async function makePreview(){
    if(pending.current||!data)return;
    const selected=data.related.find(m=>m.id===target);if(target&&!selected)return;
    pending.current=true;setBusy(true);setError('');setAccepted(false);
    try{setPreview(await api<Preview>(`/api/memory-candidates/${candidate.id}/review`,'POST',{
      resolution:target?'replace':'keep',...(selected?{target_memory_id:selected.id,target_version:selected.version}:{}),
      ...Object.fromEntries(Object.entries(times).map(([key,value])=>[key,value.trim()||null]))}));}
    catch(e){setError((e as Error).message+'；请核对来源、日期格式和期间顺序。');}
    finally{pending.current=false;setBusy(false);}
  }
  async function confirm(){
    if(pending.current||!preview||!accepted)return;
    pending.current=true;setBusy(true);setError('');
    try{await api(`/api/memory-candidates/${candidate.id}/actions`,'POST',{action:'confirm',review_token:preview.token});saved.current=true;done();}
    catch(e){setError((e as Error).message);if(e instanceof ApiError&&[409,401,404,410].includes(e.status)){setPreview(null);setAccepted(false);setData(null);}}
    finally{pending.current=false;setBusy(false);}
  }
  const row=data?.candidate;
  return <Modal title="核对记忆" close={close} dismissible={!busy} returnFocus={()=>saved.current?document.getElementById('main-content'):trigger.current}><div className="page-stack">
    {error&&<p ref={alert} role="alert" tabIndex={-1} className="error">{error}</p>}
    {!data&&!loading&&<button className="button" disabled={busy} onClick={()=>setRevision(v=>v+1)}>重读来源</button>}
    {loading&&<p role="status">正在读取原文与相关记忆…</p>}
    {data&&row&&<><p>{data.warning}</p><section><h3 ref={preview?undefined:heading} tabIndex={-1}>待存内容</h3><p className="memory-content">{row.content}</p><p>事实主体：{row.subject==='self'?'本人':row.subject==='unknown'?'主体不明，请拒绝后手动记录':row.subject}</p><p className="muted">核对原因：{row.reason}</p></section>
      <section><h3>原始依据</h3><p className="muted">{formatTime(Number(data.source.created_at))} · 用户原文</p><blockquote className="history-original memory-content">{String(data.source.content)}</blockquote></section>
      {!preview?<><section><h3>相关记忆</h3><p className="muted">同主体、同类别共 {data.related_count} 条，当前匹配 {data.matched_count} 条；只供人工对照，不自动判断矛盾。</p>
        <form className="filter-row" onSubmit={e=>{e.preventDefault();setSearch(query);setRevision(v=>v+1);}}><input aria-label="检索相关记忆" maxLength={200} value={query} disabled={busy} onChange={e=>setQuery(e.target.value)}/><button className="button" disabled={busy}>检索记录</button></form>
        {data.truncated&&<p className="muted">仅展示最新 20 条匹配记录，请缩小检索范围；未显示的记录不代表已核对。</p>}
        {!data.related.length&&<p>当前没有可选的相关记录，可新增保存或先到记忆列表管理。</p>}
        <fieldset disabled={busy}><legend>保存方式</legend><label className="checkbox"><input type="radio" name="memory-resolution" checked={!target} onChange={()=>setTarget('')}/>新增记录，保留已有事实</label>
        {data.related.map(m=><article className="data-card" key={m.id}><label className="checkbox"><input type="radio" name="memory-resolution" checked={target===m.id} onChange={()=>setTarget(m.id)} disabled={m.source_ids.includes(row.source_id)}/>纠正此项 · v{m.version}</label><p className="memory-content">{m.content}</p><MemoryTimeDetails value={m}/>{m.source_ids.includes(row.source_id)&&<small className="muted">共用本条原文，不能互相纠正；可新增记录。</small>}</article>)}</fieldset></section>
        <fieldset disabled={busy}><legend>新事实时间</legend><p className="muted">选填 YYYY-MM-DD 或带时区的 ISO 8601 时间；留空表示未说明，不继承旧项日期。期间不是自动删除时间。</p><div className="form-stack">{memoryTimeFields.map(([key,title])=><label key={key}>{title}<input name={key} value={times[key]} maxLength={100} autoComplete="off" onChange={e=>setTimes(old=>({...old,[key]:e.target.value}))}/></label>)}</div></fieldset>
        <div className="actions"><button className="button" disabled={busy} onClick={close}>暂不保存</button><button className="button primary" disabled={busy||!row.subject.trim()||row.subject.trim()==='unknown'||row.status!=='pending'} onClick={()=>void makePreview()}>{busy?'正在核对':'预览保存'}</button></div>
      </>:<section className="page-stack" aria-label="保存预览"><h3 ref={heading} tabIndex={-1}>{preview.resolution==='replace'?'纠正旧项':'新增记录'}</h3>{preview.before&&<article className="data-card"><h4>原有内容 · v{preview.before.version}</h4><p className="memory-content">{preview.before.content}</p><MemoryTimeDetails value={preview.before}/></article>}<article className="data-card"><h4>确认后内容</h4><p className="memory-content">{preview.after.content}</p><p>主体：{preview.after.subject==='self'?'本人':preview.after.subject}</p><MemoryTimeDetails value={preview.after}/>{!memoryTimeFields.some(([key])=>preview.after[key])&&<p className="muted">时间未说明</p>}</article><p>{preview.warning}</p><small className="muted">预览有效至 {formatTime(preview.expires_at)}；来源或旧项变化后须重新核对。</small><label className="checkbox"><input type="checkbox" checked={accepted} disabled={busy} onChange={e=>setAccepted(e.target.checked)}/>我已核对原文、主体、时间和保存方式</label><div className="actions"><button className="button" disabled={busy} onClick={()=>{setPreview(null);setAccepted(false);}}>返回修改</button><button className="button primary" disabled={busy||!accepted} onClick={()=>void confirm()}>{busy?'正在保存':'确认保存'}</button></div></section>}
    </>}
  </div></Modal>;
}
