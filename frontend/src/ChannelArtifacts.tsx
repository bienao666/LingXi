import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice,formatTime,Modal,useResource} from './ui';

type Option=components['schemas']['ArtifactOption'];
type Reply=components['schemas']['ArtifactReplyView'];
const state:Record<string,string>={preview:'待确认',confirmed:'等待投递',accepted_by_protocol:'协议接受',unknown:'结果待核',failed:'投递失败',cancelled:'已取消',expired:'已经过期',pending:'等待发送',sending:'正在发送'};
const key=(s:Option)=>s.kind+':'+s.id;

export function ChannelArtifacts({channelId,originId,target,close}:{channelId:string;originId:string;target:string;close():void}) {
  const [draft,setDraft]=useState(''),[query,setQuery]=useState(''),[selected,setSelected]=useState<Option[]>([]);
  const [preview,setPreview]=useState<Reply>(),[ack,setAck]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[showHistory,setShowHistory]=useState(false);
  const pending=useRef(false),requestId=useRef<string>(''),acknowledgement=useRef<HTMLInputElement>(null);
  const source=useResource<Option[]>(`/api/channels/${channelId}/artifact-sources?origin_id=${encodeURIComponent(originId)}&q=${encodeURIComponent(query)}`);
  const history=useResource<Reply[]>(`/api/channels/${channelId}/artifact-replies?origin_id=${encodeURIComponent(originId)}`);
  useEffect(()=>{if(preview)acknowledgement.current?.focus();},[preview]);
  useEffect(()=>{if(preview||busy)return;const id=window.setInterval(history.reload,5000);return()=>window.clearInterval(id);},[preview,busy,history.reload]);
  async function perform(action:()=>Promise<void>){
    if(pending.current)return;pending.current=true;setBusy(true);setError('');
    try{await action();}catch(e){setError((e as Error).message);}finally{pending.current=false;setBusy(false);}
  }
  const edit=()=>{setPreview(undefined);setAck(false);requestId.current='';};
  return <Modal title="发送附件" close={close} dismissible={!busy}>
    <div className="page-stack">
      <p>原会话：<strong>{target}</strong>。仅回复这条已完成且 24 小时内的 QQ 消息，不选择其他接收者。</p>
      <p className="muted">每次最多 3 个，单个 20 MiB、合计 50 MiB，并受当前附件策略更低限制。确认后排队一小时；原始文件将交给 QQ，群内成员可见。协议接受不等于送达、已读或下载，未知结果不自动重发。</p>
      <ErrorNotice error={error||source.error||history.error}/>
      {preview?<section className="form-stack" aria-label="附件预览">
        <h3>附件预览</h3><p>确认接收者：{preview.target_key} · 预览截止 {formatTime(preview.expires_at)}</p>
        <ul>{preview.files.map(f=><li key={key(f)} className="wrap">{f.name} · {f.format==='image'?'图片消息':'文件上传'} · {(f.bytes/1024).toFixed(1)} KiB · 来源 v{f.version}</li>)}</ul>
        <p>只发送上述来源的完整内容；不携带其他聊天历史、模型密钥或额外记忆。协作结果保留真实完成状态和限制，不表示计划步骤已经执行。</p>
        <label className="checkbox"><input ref={acknowledgement} type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/>我已核对文件来源，允许向上述原会话分享完整内容{preview.target_key.startsWith('group:')?'，并知晓全群可见':''}</label>
        <div className="actions"><button className="button" disabled={busy} onClick={edit}>继续选择</button><button className="button primary" disabled={busy||!ack} onClick={()=>void perform(async()=>{
          await api(`/api/channel-artifact-replies/${preview.id}/confirm`,'POST',{digest:preview.digest,acknowledgement:'share_with_original_audience'});
          edit();setSelected([]);history.reload();
        })}>{busy?'正在提交':'确认发送'}</button></div>
      </section>:<>
        <form className="inline-form" onSubmit={e=>{e.preventDefault();setQuery(draft.trim());}}>
          <label>查找文件<input aria-label="查找文件" value={draft} disabled={busy} maxLength={100} onChange={e=>setDraft(e.target.value)}/></label>
          <button className="button" disabled={busy}>查找文件</button>
        </form>
        <fieldset disabled={busy} className="form-stack"><legend>可用来源</legend>
          {!source.data&&!source.error&&<p role="status">正在加载来源…</p>}
          {source.data?.length===0&&<p>没有匹配的已就绪文件或原请求协作结果。可先在文件管理上传并等待解析。</p>}
          {source.data?.map(s=><label key={key(s)} className="checkbox wrap"><input type="checkbox" checked={selected.some(v=>key(v)===key(s))} disabled={selected.length>=3&&!selected.some(v=>key(v)===key(s))} onChange={()=>{requestId.current='';setSelected(values=>values.some(v=>key(v)===key(s))?values.filter(v=>key(v)!==key(s)):[...values,s]);}}/>{s.name} · {s.kind==='agent_result'?'协作结果':s.format==='image'?'图片':'受控文档'} · {(s.bytes/1024).toFixed(1)} KiB</label>)}
        </fieldset>
        {!!selected.length&&<section aria-label="已选来源"><h3>已选来源</h3><ul>{selected.map(s=><li key={key(s)} className="wrap">{s.name}<button className="button" disabled={busy} onClick={()=>{requestId.current='';setSelected(values=>values.filter(v=>key(v)!==key(s)));}} aria-label={`移除${s.name}`}>移除</button></li>)}</ul></section>}
        <div className="actions"><button className="button" disabled={busy} onClick={()=>source.reload()}>重载来源</button><button className="button primary" disabled={busy||!selected.length} onClick={()=>void perform(async()=>{
          requestId.current ||= crypto.randomUUID();
          const result=await api<Reply>(`/api/channels/${channelId}/artifact-previews`,'POST',{
            origin_id:originId,request_id:requestId.current,sources:selected.map(({kind,id,version})=>({kind,id,version}))});
          if(result.status==='preview'){setPreview(result);setAck(false);}else{edit();history.reload();}
        })}>{busy?'正在核对':'预览附件'}</button></div>
      </>}
      <button className="button" aria-expanded={showHistory} onClick={()=>setShowHistory(value=>!value)}>附件记录（{history.data?.length??0}）</button>
      {showHistory&&<section className="form-stack" aria-label="附件记录"><button className="button" disabled={busy} onClick={history.reload}>重载记录</button>
        {history.data?.length===0&&<p>尚无附件发送记录。</p>}
        {history.data?.map(r=><article className="data-card" key={r.id}><strong>{state[r.status]||r.status}</strong><p className="wrap">{r.files.map(f=>f.name).join('、')}</p>
          {r.deliveries.map(d=><p key={d.id}>{d.kind==='artifact_image'?'图片':'文件'} · {state[d.status]||d.status}{d.protocol_file_id?' · 已返回协议文件 ID':''}{d.protocol_message_id?' · 已返回协议消息 ID':''}{d.error?` · ${d.error}`:''}</p>)}
          {r.status==='confirmed'&&<button className="button" disabled={busy} onClick={()=>void perform(async()=>{await api(`/api/channel-artifact-replies/${r.id}/cancel`,'POST');history.reload();})}>取消待发</button>}
        </article>)}
      </section>}
    </div>
  </Modal>;
}
