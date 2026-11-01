import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice, Modal, formatTime, useResource} from './ui';

type State = components['schemas']['ConfigurationState'];
type Preview = components['schemas']['ConfigurationPreview'];
type Document = components['schemas']['ConfigurationDocument'];
const names = ['workspace', 'media', 'operations', 'model_budget'] as const;
type Name = typeof names[number];
const modules: Record<Name, string> = {workspace:'空间默认',media:'媒体限制',operations:'运维偏好',model_budget:'调用限额'};
const fields: Record<string, string> = {workspace_name:'空间名称',default_timezone:'默认时区',default_persona_id:'默认人格引用',history_budget_bytes:'历史预算（字节）',
  max_files:'每条附件数',max_file_mib:'单个附件（MiB）',max_total_mib:'合计附件（MiB）',allowed_types:'允许类型',log_retention_days:'日志保留（天）',daily_token_alert:'每日 token 告警',daily_limit:'每日请求限额',execution_limit:'单次执行限额'};
const types: Record<string, string> = {text:'纯文本',markdown:'Markdown',csv:'CSV',pdf:'文本 PDF',docx:'Word',xlsx:'Excel',image:'静态图片',qq_audio:'QQ 语音'};
const limit = 65536;
function parse(text: string): Record<string, unknown> {
  if(new TextEncoder().encode(text).length>limit)throw new Error('配置包不能超过 64 KiB');
  let value:unknown;
  try {value=JSON.parse(text);}catch {throw new Error('JSON 格式不正确，请检查配置包。');}
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('请使用灵犀配置包 JSON 对象。');
  return value as Record<string,unknown>;
}
function moduleObject(doc:Record<string,unknown>) {
  if(!doc.modules||typeof doc.modules!=='object'||Array.isArray(doc.modules))throw new Error('配置包缺少有效的 modules 对象。');
  return doc.modules as Record<string,unknown>;
}
function display(field:string,value:string|number|string[]|null) {
  if(Array.isArray(value))return value.length?value.map(v=>types[v]||v).join('、'):'全部关闭';
  if(field==='default_persona_id')return value===null?'内置 · 并肩伙伴':`人格引用：${value}`;
  if((field==='daily_limit'||field==='execution_limit')&&value===0)return '不限制';
  if(field==='daily_token_alert'&&value===0)return '关闭告警';
  return String(value??'未设置');
}

export function ConfigurationBundles({onApplied}:{onApplied():void}) {
  const current=useResource<State>('/api/admin/configuration-bundles');
  const [step,setStep]=useState<'import'|'review'>(),[text,setText]=useState(''),[selected,setSelected]=useState<Name[]>([...names]);
  const [preview,setPreview]=useState<Preview>(),[busy,setBusy]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [ack,setAck]=useState(false),[clock,setClock]=useState(Date.now()/1000);
  const alive=useRef(true),trigger=useRef<HTMLButtonElement>(null),success=useRef(false),fileRead=useRef(0);
  const heading=useRef<HTMLHeadingElement>(null),dialogError=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;fileRead.current++;};},[]);
  useEffect(()=>{if(step!=='review')return;setClock(Date.now()/1000);const timer=setInterval(()=>setClock(Date.now()/1000),1000);return()=>clearInterval(timer);},[step]);
  useEffect(()=>{if(step)heading.current?.focus();},[step]);
  useEffect(()=>{if(step&&error)dialogError.current?.focus();},[step,error]);
  let present:Name[]=[];
  try {const value=moduleObject(parse(text));present=names.filter(n=>Object.hasOwn(value,n));}catch{/* Error is shown on submit. */}
  const loading=!!busy,available=!!current.data&&!current.error;
  function close(){setStep(undefined);setError('');setPreview(undefined);setAck(false);fileRead.current++;}
  async function exportBundle(){
    if(loading)return;setBusy('export');setError('');setMessage('');
    try {const doc=await api<Document>('/api/admin/configuration-bundles/export');if(!alive.current)return;
      const url=URL.createObjectURL(new Blob([JSON.stringify(doc,null,2)],{type:'application/json'}));
      const link=document.createElement('a');link.href=url;link.download='lingxi-configuration-bundle.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      setMessage('已导出四类配置。文件不包含密钥、连接或执行授权；人格保留为引用。');
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  async function makePreview(){
    if(loading||!available)return;setError('');setMessage('');
    let doc:Record<string,unknown>,scope:Record<string,unknown>,versions:Partial<State['versions']>={};
    try {
      doc=parse(text);scope={...moduleObject(doc)};
      // Retain unknown keys for the server to reject, rather than hiding
      // unsupported or misspelled configuration supplied by another version.
      for(const name of names){if(!selected.includes(name))delete scope[name];else if(Object.hasOwn(scope,name))versions[name]=current.data!.versions[name];}
      if(!Object.keys(versions).length)throw new Error('至少选择一个配置包中存在的模块。');
    }catch(e){setError((e as Error).message);return;}
    setBusy('preview');
    try {const result=await api<Preview>('/api/admin/configuration-bundles/previews','POST',{document:{...doc,modules:scope},versions});
      if(alive.current){setPreview(result);setAck(false);setStep('review');}}
    catch(e){if(alive.current)setError((e as {status?:number}).status===422&&(e as Error).message.startsWith('请求失败（422）')?'配置包格式或字段不兼容，请使用当前导出的完整模块；不接受额外权限或密钥字段。':(e as Error).message);}
    finally{if(alive.current)setBusy('');}
  }
  async function confirm(){
    if(!preview||loading||!ack||clock>=preview.expires_at)return;setBusy('confirm');setError('');
    try {await api<State>('/api/admin/configuration-bundles','PUT',{token:preview.token,acknowledgement:'atomic_configuration_changes'});
      if(!alive.current)return;success.current=true;close();setText('');current.reload();onApplied();
      setMessage('配置包已生效。未选模块保持原值，已计模型请求次数保留。');
      window.dispatchEvent(new window.Event('lingxi:system-settings'));
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  return <section className="data-card form-stack" aria-label="配置打包">
    <div className="pane-toolbar"><h3>配置打包</h3><div className="actions"><button className="button" disabled={loading} onClick={()=>current.reload()}>刷新版本</button><button className="button" disabled={loading||!available} onClick={()=>void exportBundle()}>导出整包</button><button ref={trigger} className="button" disabled={loading||!available} onClick={()=>{success.current=false;setStep('import');setError('');setMessage('');setSelected([...names]);}}>导入整包</button></div></div>
    <p className="muted">统一管理空间默认、媒体限制、运维偏好和调用限额。可选择导入范围；先核对差异，确认后同时生效。保留导出文件可再次导入以恢复配置值，版本仍递增。</p>
    <ErrorNotice error={current.error||(!step?error:'')}/>{message&&<p role="status">{message}</p>}
    {!current.data?<p role="status">{current.error?'版本读取失败，请刷新版本。':'正在读取模块版本…'}</p>:<dl className="config-values">{names.map(n=><React.Fragment key={n}><dt>{modules[n]}</dt><dd>v{current.data!.versions[n]}{current.error?' · 上次快照':''}</dd></React.Fragment>)}</dl>}
    {step&&<Modal title={step==='review'?'核对整包':'导入整包'} dismissible={!loading} returnFocus={()=>success.current?(document.getElementById('main-content')||trigger.current):trigger.current} close={close}>
      <h3 ref={heading} tabIndex={-1}>{step==='review'?'差异总览':'配置来源'}</h3>
      {error&&<p ref={dialogError} role="alert" tabIndex={-1} className="error">{error}</p>}
      {step==='import'?<form className="form-stack" onSubmit={e=>{e.preventDefault();void makePreview();}}>
        <p className="muted">仅接受灵犀配置包，不含模型、代理、账号、插件或资源授权。人格引用必须属于当前账号且有效。未选模块不导入。</p>
        <label>选择文件<input type="file" accept=".json,application/json" disabled={loading} onChange={e=>{const file=e.target.files?.[0];const read=++fileRead.current;if(!file)return;if(file.size>limit){setError('配置包不能超过 64 KiB');return;}setBusy('file');setError('');void file.text().then(value=>{if(alive.current&&read===fileRead.current){setText(value);setSelected([...names]);}}).catch(()=>{if(alive.current&&read===fileRead.current)setError('文件读取失败，请重选文件。');}).finally(()=>{if(alive.current&&read===fileRead.current)setBusy('');});}}/></label>
        <label>配置内容<textarea rows={10} required value={text} disabled={loading} onInput={e=>setText(e.currentTarget.value)} onChange={e=>setText(e.target.value)}/></label>
        <fieldset disabled={loading}><legend>导入范围</legend>{names.map(n=><label className="checkbox" key={n}><input type="checkbox" checked={selected.includes(n)&&present.includes(n)} disabled={!present.includes(n)} onChange={e=>setSelected(old=>e.target.checked?[...old,n]:old.filter(x=>x!==n))}/>{modules[n]}{!present.includes(n)?' · 包内未提供':''}</label>)}</fieldset>
        <ErrorNotice error={current.error}/><div className="actions"><button type="button" className="button" disabled={loading} onClick={()=>current.reload()}>刷新版本</button><button className="button primary" disabled={loading||!available}>{loading?'正在处理':'预览整包'}</button></div>
      </form>:preview&&<div className="form-stack"><p>仅应用下列差异；所选模块版本变化时整批停止。</p>
        {names.filter(n=>preview.changes.some(c=>c.module===n)).map(n=><section key={n} aria-label={modules[n]}><h3>{modules[n]} · v{preview.versions[n]}</h3><dl className="config-values">{preview.changes.filter(c=>c.module===n).map(c=><React.Fragment key={c.field}><dt>{fields[c.field]||c.field}</dt><dd><span className="muted">{display(c.field,c.before)}</span><span aria-hidden="true"> → </span><span className="sr-only">更改为</span>{display(c.field,c.after)}</dd></React.Fragment>)}</dl></section>)}
        <p className="muted">{preview.note}</p><p role="status">{clock>=preview.expires_at?'预览已过期，请返回修改后重新预览。':`预览有效至 ${formatTime(preview.expires_at)}`}</p>
        <label className="checkbox"><input type="checkbox" checked={ack} disabled={loading} onChange={e=>setAck(e.target.checked)}/>已核对全部差异，了解缩短日志保留期及调整限额的影响</label>
        <div className="actions"><button className="button" disabled={loading} onClick={()=>{setStep('import');setPreview(undefined);setAck(false);setError('');current.reload();}}>返回修改</button><button className="button primary" disabled={loading||!ack||clock>=preview.expires_at} onClick={()=>void confirm()}>{busy==='confirm'?'正在应用':'确认整包'}</button></div>
      </div>}
    </Modal>}
  </section>;
}
