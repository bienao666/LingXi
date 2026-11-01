import React,{useEffect,useId,useRef,useState} from 'react';
import {api} from './api';
import type {Model} from './api';
import type {components} from './api.generated';
import {ErrorNotice,formatTime,useResource} from './ui';

type Page=components['schemas']['ModelCallPage'];
type Filters={days:string;status:string;purpose:string;model_id:string};
const initial:Filters={days:'0',status:'all',purpose:'',model_id:''};
const purposes:Record<string,string>={chat:'对话生成',extraction:'记忆抽取',embedding:'向量嵌入',rerank:'检索重排',
  memory_embedding:'记忆嵌入',memory_rerank:'记忆重排',chat_intent:'任务理解',context_summary:'历史压缩',emotion:'情绪线索',asr:'语音转写',tts:'语音合成'};
const statuses:Record<string,string>={all:'全部结果',completed:'调用完成',failed:'调用失败',cancelled:'已经停止'};

export function ModelCalls(){
  const catalog=useResource<Model[]>('/api/models'),listId=useId();
  const [draft,setDraft]=useState<Filters>(initial),[filters,setFilters]=useState<Filters>(initial);
  const [cursor,setCursor]=useState(''),[stack,setStack]=useState<string[]>([]),[revision,setRevision]=useState(0);
  const [data,setData]=useState<Page>(),[busy,setBusy]=useState(true),[error,setError]=useState('');
  const pending=useRef(true);
  useEffect(()=>{
    let active=true;const controller=new AbortController();pending.current=true;setBusy(true);setError('');setData(undefined);
    const query=new URLSearchParams(filters);if(cursor)query.set('cursor',cursor);
    void api<Page>('/api/admin/model-call-pages?'+query,'GET',undefined,{signal:controller.signal})
      .then(value=>{if(active)setData(value);}).catch(e=>{if(active)setError(e.message);})
      .finally(()=>{if(active){pending.current=false;setBusy(false);}});
    return()=>{active=false;controller.abort();};
  },[filters,cursor,revision]);
  function latest(){setCursor('');setStack([]);setRevision(v=>v+1);}
  function move(next:boolean){
    if(pending.current)return;
    if(next&&data?.next_cursor){pending.current=true;setBusy(true);setStack([...stack,cursor]);setCursor(data.next_cursor);}
    else if(!next&&stack.length){pending.current=true;setBusy(true);setCursor(stack.at(-1)!);setStack(stack.slice(0,-1));}
  }
  const choices=new Map((catalog.data||[]).map(m=>[m.id,m.name]));
  choices.set('voice:asr','语音转写');choices.set('voice:tts','语音合成');
  for(const row of data?.items||[])if(!choices.has(row.model_id))choices.set(row.model_id,String(row.config.model||row.model_id));
  return <div className="page-stack">
    <div className="pane-toolbar"><h2>调用记录</h2><button className="button" disabled={busy} onClick={latest}>最新记录</button></div>
    <form className="form-stack" onSubmit={e=>{e.preventDefault();setFilters({...draft,model_id:draft.model_id.trim()});latest();}}>
      <div className="card-grid">
        <label>时间范围<select value={draft.days} onChange={e=>setDraft({...draft,days:e.target.value})}>{[['0','全部时间'],['1','最近一天'],['7','最近七天'],['30','最近三十天']].map(([value,name])=><option value={value} key={value}>{name}</option>)}</select></label>
        <label>结果状态<select value={draft.status} onChange={e=>setDraft({...draft,status:e.target.value})}>{Object.entries(statuses).map(([value,name])=><option value={value} key={value}>{name}</option>)}</select></label>
        <label>调用用途<select value={draft.purpose} onChange={e=>setDraft({...draft,purpose:e.target.value})}><option value="">全部用途</option>{Object.entries(purposes).map(([value,name])=><option value={value} key={value}>{name}</option>)}</select></label>
        <label>指定模型<input list={listId} maxLength={64} value={draft.model_id} onChange={e=>setDraft({...draft,model_id:e.target.value})} placeholder="全部模型"/><datalist id={listId}>{[...choices].map(([value,name])=><option value={value} key={value}>{name}</option>)}</datalist><small className="muted">选择或粘贴记录中的模型标识。</small></label>
      </div>
      <div className="actions"><button className="button primary" disabled={busy}>应用筛选</button><button type="button" className="button" disabled={busy} onClick={()=>{setDraft(initial);setFilters(initial);latest();}}>清除筛选</button></div>
    </form>
    <ErrorNotice error={error}/>{error&&<div className="actions"><button className="button" disabled={busy} onClick={()=>setRevision(v=>v+1)}>重试加载</button><button className="button" disabled={busy} onClick={latest}>返回最新</button></div>}
    {catalog.error&&<p className="muted">模型目录暂不可用，仍可查询调用记录并填写模型标识。</p>}
    <p className="muted">每页最多 50 条，按记录时间从新到旧排列；刷新或应用筛选回到第一页。费用未知不代表免费，仅显示版本和用量等元数据。</p>
    <p role="status" aria-live="polite">{busy?'正在读取调用记录…':data?`第 ${stack.length+1} 页 · 本页 ${data.items.length} 条 · 查询截止 ${formatTime(data.as_of)}`:'记录尚未加载'}</p>
    {data&&!busy&&<><div className="table-scroll" role="region" aria-label="模型调用记录" tabIndex={0}><table><thead><tr><th scope="col">时间</th><th scope="col">模型／用途</th><th scope="col">状态</th><th scope="col">耗时</th><th scope="col">用量／费用</th></tr></thead><tbody>{data.items.map(row=><tr key={row.id}>
      <td>{formatTime(row.created_at)}<small className="block muted">{row.id}</small></td>
      <td>{String(row.config.model||choices.get(row.model_id)||row.model_id)}<small className="block muted">{row.model_id}</small><small className="block muted">{purposes[row.purpose]||row.purpose}{row.config.validation?' · 能力验证':''}</small><details><summary>固定版本</summary><pre className="task-json">{JSON.stringify(row.config,null,2)}</pre>{row.run_id&&<small>关联执行 {row.run_id}</small>}</details></td>
      <td><span className={`status-badge status-${row.status}`}>{statuses[row.status]||row.status}</span>{row.error&&<small className="block error">{row.error}</small>}</td>
      <td>{row.duration_ms} ms</td>
      <td><code>{Object.keys(row.usage).length?JSON.stringify(row.usage):'用量未知'}</code><small className="block muted">{row.billing.status==='estimated'?`估算 ${row.billing.currency} ${row.billing.amount}`:'费用未知'}</small>{!!row.billing.price&&<details><summary>计价依据</summary><pre className="task-json">{JSON.stringify(row.billing.price,null,2)}</pre></details>}</td>
    </tr>)}</tbody></table></div>{!data.items.length&&<div className="data-empty">暂无符合条件的调用。</div>}</>}
    <nav className="actions" aria-label="调用记录分页"><button className="button" disabled={busy||!stack.length} onClick={()=>move(false)}>上一页</button><button className="button" disabled={busy||!data?.next_cursor} onClick={()=>move(true)}>下一页</button></nav>
  </div>;
}
