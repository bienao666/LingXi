import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {AssistantProfile} from './api';
import type {components} from './api.generated';
import {ErrorNotice, Modal, formatTime, useResource} from './ui';
import {ProxySettings} from './ProxySettings';

type Settings = components['schemas']['SettingsView'];
type Defaults = components['schemas']['Defaults'];
type Preview = components['schemas']['SettingsPreview'];
type PreviewInput = components['schemas']['SystemSettingsInput'];
type History = components['schemas']['SettingsHistory'];
type ConfigDocument = components['schemas']['SettingsDocument'];
const labels:Record<string,string>={workspace_name:'空间名称',default_timezone:'默认时区',default_persona_id:'默认人格',history_budget_bytes:'历史预算'};
const actions:Record<string,string>={edit:'配置修改',import:'配置导入',rollback:'版本回退',initial:'初始配置',restore:'恢复隔离'};

export function SystemSettings() {
  const settings=useResource<Settings>('/api/system-settings'),personas=useResource<AssistantProfile[]>('/api/personas');
  const [before,setBefore]=useState<number>(),[records,setRecords]=useState<History['items']>([]);
  const history=useResource<History>('/api/admin/system-settings/revisions'+(before===undefined?'':`?before=${before}`));
  const [draft,setDraft]=useState<Defaults>(),[error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState('');
  const [preview,setPreview]=useState<Preview>(),[importing,setImporting]=useState(false),[documentText,setDocumentText]=useState('');
  const [clock,setClock]=useState(Date.now()/1000);
  const alive=useRef(true);
  const previewTrigger=useRef<HTMLElement|null>(null),importTrigger=useRef<HTMLButtonElement|null>(null);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  useEffect(()=>{if(settings.data)setDraft(settings.data.config);},[settings.data]);
  useEffect(()=>{if(history.data)setRecords(old=>before===undefined?history.data!.items:[...old.filter(r=>!history.data!.items.some(x=>x.version===r.version)),...history.data!.items]);},[history.data,before]);
  useEffect(()=>{if(!preview)return;setClock(Date.now()/1000);const timer=setInterval(()=>setClock(Date.now()/1000),1000);return()=>clearInterval(timer);},[preview]);
  const loading=!!busy, ready=!!settings.data&&!!draft;
  function reload(){setError('');setMessage('');settings.reload();personas.reload();setBefore(undefined);history.reload();}
  async function makePreview(input:Omit<PreviewInput,'version'>) {
    if(!settings.data||busy)return;
    previewTrigger.current=input.action==='import'?importTrigger.current:document.activeElement as HTMLElement|null;
    setBusy('preview');setError('');setMessage('');
    try {const result=await api<Preview>('/api/admin/system-settings/previews','POST',{version:settings.data.version,...input});
      if(alive.current){setImporting(false);setPreview(result);}}
    catch(e){if(alive.current)setError((e as {status?:number}).status===422&&(e as Error).message.startsWith('请求失败（422）')?'请检查非空名称、有效 IANA 时区及 1500–48000 字节预算；导入还须使用灵犀基础配置格式。':(e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  async function confirm(){
    if(!preview||busy)return;setBusy('confirm');setError('');
    try {const result=await api<Settings>('/api/admin/system-settings','PUT',{token:preview.token,acknowledgement:'new_turns_only'});
      if(alive.current){setDraft(result.config);setPreview(undefined);setDocumentText('');reload();setMessage(`配置 v${result.version} 已生效；已提交任务保持原配置。`);}
      window.dispatchEvent(new window.Event('lingxi:system-settings'));
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  async function exportConfig(){
    if(busy)return;setBusy('export');setError('');
    try {const doc=await api<ConfigDocument>('/api/admin/system-settings/export');if(!alive.current)return;
      const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='lingxi-workspace-defaults.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      setMessage('已导出基础配置与人格引用，不含账号、密钥、代理或授权。');
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  function display(key:string,value:string|number|null|undefined){
    if(key==='default_persona_id')return value ? (personas.data?.find(p=>p.id===value)?.name||`人格引用：${value}`) : '内置 · 并肩伙伴';
    return key==='history_budget_bytes'?`${value} 字节`:String(value??'未设置');
  }
  return <div className="page-stack">
    <div className="pane-toolbar"><h2>系统配置</h2><div className="actions"><button className="button" disabled={loading} onClick={reload}>重新加载</button><button className="button" disabled={loading||!ready} onClick={()=>void exportConfig()}>导出配置</button><button ref={importTrigger} className="button" disabled={loading||!ready} onClick={()=>{setImporting(true);setError('');}}>导入配置</button></div></div>
    <ErrorNotice error={(!preview&&!importing?error:'')||settings.error}/>{message&&<p role="status">{message}</p>}
    {!ready?<p role="status" className="muted">{settings.error?'配置读取失败，请重新加载。':'正在读取系统配置…'}</p>:<form className="data-card form-stack" onSubmit={e=>{e.preventDefault();void makePreview({action:'edit',config:draft!});}}>
      <div className="pane-toolbar"><h3>工作空间</h3><span className="muted">当前版本 v{settings.data!.version}</span></div>
      <label>空间名称<input name="workspace_name" value={draft!.workspace_name} required maxLength={40} disabled={loading} onInput={e=>setDraft({...draft!,workspace_name:e.currentTarget.value})} onChange={e=>setDraft({...draft!,workspace_name:e.target.value})}/></label>
      <label>默认时区<input name="default_timezone" value={draft!.default_timezone} required maxLength={100} disabled={loading} onInput={e=>setDraft({...draft!,default_timezone:e.currentTarget.value})} onChange={e=>setDraft({...draft!,default_timezone:e.target.value})} placeholder="Asia/Shanghai"/><small className="muted">IANA 时区；仅为尚未单独设置的联系偏好提供默认值。已有联系设置与提醒时间保持原值。</small></label>
      <label>默认人格<select name="default_persona_id" value={draft!.default_persona_id||''} disabled={loading||!personas.data} onChange={e=>setDraft({...draft!,default_persona_id:e.target.value||null})}><option value="">内置 · 并肩伙伴</option>{personas.data?.filter(p=>p.enabled||p.id===draft!.default_persona_id).map(p=><option key={p.id} value={p.id} disabled={!p.enabled}>{p.name} · v{p.version}{!p.enabled?'（已停用）':''}</option>)}{draft!.default_persona_id&&personas.data&&!personas.data.some(p=>p.id===draft!.default_persona_id)&&<option value={draft!.default_persona_id} disabled>人格已不可用，请重新选择</option>}</select><small className="muted">仅用于继承工作空间的会话。会话中的显式选择优先，已提交回合固定人格版本。</small></label>
      <ErrorNotice error={personas.error}/>
      <label>历史预算<input name="history_budget_bytes" type="number" min={1500} max={48000} step={1} value={draft!.history_budget_bytes} required disabled={loading} onInput={e=>setDraft({...draft!,history_budget_bytes:e.currentTarget.valueAsNumber})} onChange={e=>setDraft({...draft!,history_budget_bytes:e.target.valueAsNumber})}/><small className="muted">1500–48000 UTF-8 字节，用于聊天历史选择与压缩，受模型更低上限约束；不是 token 数，也不是整次请求大小。</small></label>
      <p className="muted">名称用于界面展示；其余默认值在新回合或新设置中生效。保存前先核对差异，5 分钟内确认；导入和回退同样需要确认。</p>
      <div className="actions"><button className="button primary" disabled={loading||!Number.isInteger(draft!.history_budget_bytes)}>{busy==='preview'?'正在校验':'预览更改'}</button>{settings.data!.updated_at&&<small className="muted">更新于 {formatTime(settings.data!.updated_at)}</small>}</div>
    </form>}
    <section className="data-card form-stack" aria-label="配置历史"><div className="pane-toolbar"><h3>配置历史</h3><span className="muted">回退会创建新版本</span></div><ErrorNotice error={history.error}/>
      {!history.data&&!records.length?<p role="status">{history.error?'历史读取失败；重新加载可重试。':'正在读取配置历史…'}</p>:!records.length?<p className="muted">尚无更改记录；首次确认时会保留初始配置。</p>:records.map(r=><div className="revision-record" key={r.version}><div className="pane-toolbar"><strong>v{r.version} · {actions[r.action]||r.action}{r.source_version!==null?` · 来源 v${r.source_version}`:''}</strong><button className="button" disabled={loading||!ready||r.version===settings.data?.version} onClick={()=>void makePreview({action:'rollback',source_version:r.version})} aria-label={`预览回退至 v${r.version}`}>预览回退</button></div><small className="muted">{formatTime(r.created_at)}</small><details><summary>查看配置</summary><dl className="config-values">{Object.entries(r.config).map(([key,value])=><React.Fragment key={key}><dt>{labels[key]}</dt><dd>{display(key,value)}</dd></React.Fragment>)}</dl></details></div>)}
      {history.data?.next_before!==null&&history.data?.next_before!==undefined&&<button className="button" disabled={loading} onClick={()=>setBefore(history.data!.next_before!)}>更多版本</button>}
    </section>
    <ProxySettings embedded/>
    {importing&&<Modal title="导入配置" dismissible={!loading} close={()=>{setImporting(false);setError('');}}><form className="form-stack" onSubmit={e=>{e.preventDefault();setError('');try{if(new TextEncoder().encode(documentText).length>16384)throw new Error('配置文件不能超过 16 KiB');const doc=JSON.parse(documentText) as ConfigDocument;void makePreview({action:'import',document:doc});}catch(e){setError(e instanceof SyntaxError?'JSON 格式不正确，请检查配置文件。':(e as Error).message);}}}>
      <p className="muted">仅接受灵犀基础配置格式。人格引用必须属于当前账号且有效；不导入模型、密钥、代理和授权。</p>
      <label>选择文件<input type="file" accept=".json,application/json" disabled={loading} onChange={e=>{const file=e.target.files?.[0];if(!file)return;if(file.size>16384){setError('配置文件不能超过 16 KiB');return;}setError('');void file.text().then(text=>{if(alive.current)setDocumentText(text);}).catch(()=>setError('文件读取失败，请重选文件。'));}}/></label>
      <label>配置内容<textarea rows={10} required value={documentText} disabled={loading} onInput={e=>setDocumentText(e.currentTarget.value)} onChange={e=>setDocumentText(e.target.value)}/></label><ErrorNotice error={error}/><button className="button primary" disabled={loading}>{loading?'正在校验':'预览导入'}</button>
    </form></Modal>}
    {preview&&<Modal title="确认配置" returnFocus={previewTrigger.current} dismissible={!loading} close={()=>{setPreview(undefined);setError('');}}><div className="form-stack"><p>从 v{preview.before.version} 创建新版本 · {actions[preview.action]}{preview.source_version!==null?` · 来源 v${preview.source_version}`:''}</p><dl className="config-values">{preview.changes.map(change=><React.Fragment key={change.field}><dt>{labels[change.field]}</dt><dd><span className="muted">{display(change.field,change.before)}</span><span aria-hidden="true"> → </span><span className="sr-only">更改为</span>{display(change.field,change.after)}</dd></React.Fragment>)}</dl><p className="muted">{preview.note}</p><p role="status">{clock>=preview.expires_at?'预览已过期，请关闭后重新预览。':`预览有效至 ${formatTime(preview.expires_at)}`}</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={loading} onClick={()=>{setPreview(undefined);setError('');}}>取消更改</button><button className="button primary" disabled={loading||clock>=preview.expires_at} onClick={()=>void confirm()}>{busy==='confirm'?'正在提交':'确认生效'}</button></div></div></Modal>}
  </div>;
}
