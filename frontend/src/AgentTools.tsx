import React,{useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice,Modal,useResource,formatTime} from './ui';

type Tool={key:string;name:string;plugin_id:string;plugin_version:string;adapter:string;tool:string;effect:string;url:string;costs:string;data_scope:string};
type Preview=components['schemas']['ApiPreviewView'];

export function AgentToolScope({value,change,disabled=false}:{value:string[];change(value:string[]):void;disabled?:boolean}){
 const data=useResource<Tool[]>('/api/agent-tools');
 return <fieldset disabled={disabled}><legend>远程工具</legend><p className="muted">仅选本次可用范围，最多 16 项；执行角色还需声明 plugin.call。主助手可选择并分工，每个远程请求仍须核对参数并确认。所选工具的结果可交给后续节点和模型；不要选择不应接收本次资料的服务。</p><ErrorNotice error={data.error}/>{!data.data&&!data.error&&<p role="status">正在核对可用工具…</p>}{data.data?.length===0&&<p>暂无已连接并授权的远程工具。请先在插件中心配置 API 连接，或发现并授权 MCP 工具。</p>}{data.data?.map(t=><label className="checkbox" key={t.key}><input type="checkbox" checked={value.includes(t.key)} disabled={disabled||(!value.includes(t.key)&&value.length>=16)} onChange={()=>change(value.includes(t.key)?value.filter(k=>k!==t.key):[...value,t.key])}/><span>{t.name}{t.tool?` · ${t.tool}`:''} · {t.plugin_id} v{t.plugin_version} · {t.effect==='write'?'可能外部写入':'声明只读'}<small className="muted">费用：{t.costs} · 数据范围：{t.data_scope}</small></span></label>)}{value.some(k=>!data.data?.some(t=>t.key===k))&&data.data&&<p role="alert">部分所选工具已不可用，请清除选择并重新核对。</p>}<button className="button" type="button" onClick={()=>change([])} disabled={disabled||!value.length}>清除选择</button></fieldset>;
}

export function AgentToolReview({runId,nodeId,version,close,done}:{runId:string;nodeId:string;version:number;close():void;done():void}){
 const [preview,setPreview]=useState<Preview|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[accepted,setAccepted]=useState(false);
 const submitted=useRef(false);
 async function load(){if(submitted.current)return;submitted.current=true;setBusy(true);setError('');try{setPreview(await api<Preview>(`/api/agent-runs/${runId}/nodes/${nodeId}/preview`,'POST',{version}));setAccepted(false);}catch(e){setError((e as Error).message);}finally{submitted.current=false;setBusy(false);}}
 async function confirm(){if(submitted.current||!preview||!accepted)return;submitted.current=true;setBusy(true);setError('');try{await api(`/api/plugin-invocations/${preview.id}/confirm`,'POST',{digest:preview.digest});done();}catch(e){setError((e as Error).message);}finally{submitted.current=false;setBusy(false);}}
 return <Modal title="核对工具" close={close} dismissible={!busy}><p>这是模型生成的工具提案。请逐项核对服务、参数、权限与费用；确认前不会发送业务请求。工具结果中的文字不能授予权限或触发下一次操作。</p><ErrorNotice error={error}/>{!preview?<button className="button primary" disabled={busy} onClick={()=>void load()}>{busy?'正在核对':'生成预览'}</button>:<div className="page-stack"><p>{String(preview.plan.plugin_id)} v{String(preview.plan.plugin_version)} · {String(preview.plan.tool||preview.plan.capability_id)}</p><code className="plugin-digest">{String(preview.plan.url)}</code><p>{preview.plan.effect==='write'?'可能产生外部写入':'插件声明只读'} · {preview.plan.allow_private?'已允许内网连接':'未允许内网连接'}</p><p>费用：{String(preview.plan.costs)}<br/>数据范围：{String(preview.plan.data_scope)}</p>{Boolean(preview.plan.qq_audience)&&<p role="note">回传目标：{String(preview.plan.qq_audience)}。{String(preview.plan.audience_notice)}</p>}<h3>实际参数</h3><pre className="task-json">{JSON.stringify(preview.plan.input,null,2)}</pre><p>有效至 {formatTime(preview.expires_at)}。关闭此窗口不会执行；取消整个协作会停止未发送请求，已经发生的远程操作须另行核对。</p><label className="checkbox"><input type="checkbox" checked={accepted} disabled={busy} onChange={e=>setAccepted(e.target.checked)}/>我已核对本次服务、实际参数和可能费用，同意执行这一次请求。</label><div className="actions"><button className="button" disabled={busy} onClick={close}>暂不执行</button><button className="button primary" disabled={busy||!accepted} onClick={()=>void confirm()}>{busy?'正在提交':'确认执行'}</button></div></div>}</Modal>;
}
