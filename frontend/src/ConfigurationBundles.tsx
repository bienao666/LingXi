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
    <ConfigurationHistory onApplied={()=>{current.reload();onApplied();}}/>
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

type Revision = components['schemas']['ConfigurationRevisionView'];
type HistoryPage = components['schemas']['ConfigurationRevisionPage'];
type RevisionDetail = Revision & {before_snapshot:State;after_snapshot:State};
const historyActions:Record<string,string>={edit:'独立修改',import:'整包导入',rollback:'历史回退',restore:'恢复隔离'};

function ConfigurationHistory({onApplied}:{onApplied():void}) {
  const [records,setRecords]=useState<Revision[]>([]),[next,setNext]=useState<number|null>(null),[loaded,setLoaded]=useState(false);
  const [detail,setDetail]=useState<RevisionDetail>(),[current,setCurrent]=useState<State>(),[point,setPoint]=useState<'before'|'after'>('before');
  const [selected,setSelected]=useState<Name[]>([]),[step,setStep]=useState<'select'|'review'>(),[preview,setPreview]=useState<Preview>();
  const [busy,setBusy]=useState(''),[listing,setListing]=useState(false),[error,setError]=useState(''),[listError,setListError]=useState(''),[message,setMessage]=useState('');
  const [ack,setAck]=useState(false),[clock,setClock]=useState(Date.now()/1000);
  const alive=useRef(true),trigger=useRef<HTMLButtonElement|null>(null),success=useRef(false),epoch=useRef(0),listEpoch=useRef(0);
  const heading=useRef<HTMLHeadingElement>(null),dialogError=useRef<HTMLParagraphElement>(null);
  async function load(before?:number){
    const request=++listEpoch.current;setListing(true);setListError('');
    try {const result=await api<HistoryPage>(`/api/admin/configuration-revisions${before?`?before=${before}`:''}`);
      if(!Array.isArray(result.items))throw new Error('历史响应无效，请刷新历史重试。');
      if(alive.current&&request===listEpoch.current){setRecords(old=>before?[...old,...result.items.filter(r=>!old.some(o=>o.id===r.id))]:result.items);setNext(result.next_before);setLoaded(true);}}
    catch(e){if(alive.current&&request===listEpoch.current)setListError((e as Error).message);}
    finally{if(alive.current&&request===listEpoch.current)setListing(false);}
  }
  useEffect(()=>{alive.current=true;void load();const refresh=()=>void load();window.addEventListener('lingxi:system-settings',refresh);
    return()=>{alive.current=false;epoch.current++;listEpoch.current++;window.removeEventListener('lingxi:system-settings',refresh);};},[]);
  useEffect(()=>{if(step)heading.current?.focus();},[step]);
  useEffect(()=>{if(step&&error)dialogError.current?.focus();},[step,error]);
  useEffect(()=>{if(step!=='review')return;setClock(Date.now()/1000);const timer=setInterval(()=>setClock(Date.now()/1000),1000);return()=>clearInterval(timer);},[step]);
  function close(){epoch.current++;setStep(undefined);setDetail(undefined);setPreview(undefined);setError('');setAck(false);}
  async function open(row:Revision,button:HTMLButtonElement){
    if(busy)return;trigger.current=button;success.current=false;setBusy('detail');setError('');setMessage('');const request=++epoch.current;
    try {const [record,state]=await Promise.all([api<RevisionDetail>(`/api/admin/configuration-revisions/${row.id}`),api<State>('/api/admin/configuration-bundles')]);
      if(alive.current&&request===epoch.current){setDetail(record);setCurrent(state);setPoint('before');setSelected([...record.modules]);setStep('select');}}
    catch(e){if(alive.current&&request===epoch.current)setError((e as Error).message);}
    finally{if(alive.current&&request===epoch.current)setBusy('');}
  }
  async function refreshVersions(){
    if(busy)return;setBusy('versions');setError('');
    try {const result=await api<State>('/api/admin/configuration-bundles');if(alive.current)setCurrent(result);}
    catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  async function makePreview(){
    if(busy||!detail||!current)return;setError('');if(!selected.length){setError('至少选择一个回退模块。');return;}setBusy('preview');
    try {const versions=Object.fromEntries(selected.map(n=>[n,current.versions[n]]));
      const result=await api<Preview>(`/api/admin/configuration-revisions/${detail.id}/previews`,'POST',{point,modules:selected,versions});
      if(alive.current){setPreview(result);setAck(false);setStep('review');}}
    catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  async function apply(){
    if(busy||!preview||!ack||clock>=preview.expires_at)return;setBusy('confirm');setError('');
    try {await api<State>('/api/admin/configuration-bundles','PUT',{token:preview.token,acknowledgement:'atomic_configuration_changes'});
      if(!alive.current)return;success.current=true;close();setMessage('历史配置已创建新版本；未选模块与已计调用次数保留。');onApplied();
      window.dispatchEvent(new window.Event('lingxi:system-settings'));
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  const target=detail?.[point==='before'?'before_snapshot':'after_snapshot'];
  return <section className="form-stack" aria-label="统一历史">
    <div className="pane-toolbar"><h3>统一历史</h3><button className="button" disabled={!!busy||listing} onClick={()=>void load()}>刷新历史</button></div>
    <p className="muted">记录升级后的四类配置修改。可恢复每次变更前或变更后的值，需选择范围并再次确认；回退创建新版本，不能撤销已清理日志或已发请求。升级前的变更不补造记录。</p>
    <ErrorNotice error={listError||(!step?error:'')}/>{message&&<p role="status">{message}</p>}
    {busy==='detail'&&<p role="status">正在读取历史快照与当前版本…</p>}
    {!loaded&&!listError?<p role="status">正在读取统一历史…</p>:loaded&&!records.length?<p className="muted">尚无统一变更记录；首次修改将保存完整的变更前后配置。</p>:records.map(row=><article className="revision-record" key={row.id}>
      <div className="pane-toolbar"><strong>#{row.number} · {historyActions[row.action]||row.action}</strong><button className="button" disabled={!!busy} onClick={e=>void open(row,e.currentTarget)} aria-label={`核对第 ${row.number} 次配置回退`}>核对回退</button></div>
      <p>{row.modules.map(n=>modules[n]).join('、')}</p><small className="muted">{formatTime(row.created_at)}{row.source_id?` · ${row.source_point==='before'?'取自更改前':'取自更改后'}`:''}</small>
    </article>)}
    {next!==null&&<button className="button" disabled={listing||!!busy} onClick={()=>void load(next)}>更多历史</button>}
    {step&&detail&&current&&target&&<Modal title={step==='review'?'确认回退':'历史回退'} dismissible={!busy} returnFocus={()=>success.current?(document.getElementById('main-content')||trigger.current):trigger.current} close={close}>
      <h3 ref={heading} tabIndex={-1}>{step==='review'?'回退差异':'选择范围'}</h3>
      {error&&<p ref={dialogError} role="alert" tabIndex={-1} className="error">{error}</p>}
      <p>历史 #{detail.number} · {historyActions[detail.action]} · {formatTime(detail.created_at)}</p>
      {step==='select'?<form className="form-stack" onSubmit={e=>{e.preventDefault();void makePreview();}}>
        <label>恢复时点<select value={point} disabled={!!busy} onChange={e=>setPoint(e.target.value as 'before'|'after')}><option value="before">本次更改前</option><option value="after">本次更改后</option></select></label>
        <fieldset disabled={!!busy}><legend>回退范围</legend>{names.map(n=><label className="checkbox" key={n}><input type="checkbox" checked={selected.includes(n)} onChange={e=>setSelected(old=>e.target.checked?[...old,n]:old.filter(v=>v!==n))}/>{modules[n]} · 当前 v{current.versions[n]}</label>)}</fieldset>
        {selected.map(n=><details key={n}><summary>{modules[n]} · 历史 v{target.versions[n]}</summary><dl className="config-values">{Object.entries(target.document.modules[n]!).map(([field,value])=><React.Fragment key={field}><dt>{fields[field]||field}</dt><dd>{display(field,value as string|number|string[]|null)}</dd></React.Fragment>)}</dl></details>)}
        <p className="muted">人格引用须仍可用且属于当前账号；当前版本变化时整批停止。回退不包含模型、代理、账号、插件或执行授权。</p>
        <div className="actions"><button type="button" className="button" disabled={!!busy} onClick={()=>void refreshVersions()}>刷新版本</button><button className="button primary" disabled={!!busy}>{busy?'正在校验':'预览回退'}</button></div>
      </form>:preview&&<div className="form-stack">
        {names.filter(n=>preview.changes.some(c=>c.module===n)).map(n=><section key={n} aria-label={modules[n]}><h3>{modules[n]} · 当前 v{preview.versions[n]}</h3><dl className="config-values">{preview.changes.filter(c=>c.module===n).map(c=><React.Fragment key={c.field}><dt>{fields[c.field]||c.field}</dt><dd><span className="muted">{display(c.field,c.before)}</span><span aria-hidden="true"> → </span><span className="sr-only">更改为</span>{display(c.field,c.after)}</dd></React.Fragment>)}</dl></section>)}
        <p className="muted">{preview.note}</p><p role="status">{clock>=preview.expires_at?'预览已过期，请返回后重新预览。':`预览有效至 ${formatTime(preview.expires_at)}`}</p>
        <label className="checkbox"><input type="checkbox" checked={ack} disabled={!!busy} onChange={e=>setAck(e.target.checked)}/>已核对回退范围与全部差异，了解日志保留及调用限额的影响</label>
        <div className="actions"><button className="button" disabled={!!busy} onClick={()=>{setStep('select');setPreview(undefined);setAck(false);setError('');void refreshVersions();}}>返回选择</button><button className="button primary" disabled={!!busy||!ack||clock>=preview.expires_at} onClick={()=>void apply()}>{busy==='confirm'?'正在应用':'确认回退'}</button></div>
      </div>}
    </Modal>}
  </section>;
}
