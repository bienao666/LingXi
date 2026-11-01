import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice, formatTime, Modal, useResource} from './ui';

type Policy=components['schemas']['ResourcePolicyView'];
type Preview=components['schemas']['ResourcePolicyPreview'];

export function ResourceSettings(){
  const policy=useResource<Policy>('/api/resource-policy');
  const [readPaths,setReadPaths]=useState('');
  const [paths,setPaths]=useState(''),[busy,setBusy]=useState(''),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [preview,setPreview]=useState<Preview>(),[clock,setClock]=useState(Date.now()/1000);
  const trigger=useRef<HTMLElement|null>(null),alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  useEffect(()=>{if(policy.data){setPaths(policy.data.roots.join('\n'));setReadPaths((policy.data.read_roots||[]).join('\n'));}},[policy.data]);
  useEffect(()=>{if(!preview)return;setClock(Date.now()/1000);const timer=setInterval(()=>setClock(Date.now()/1000),1000);return()=>clearInterval(timer);},[preview]);
  async function review(roots:string[],read_roots=readPaths.split('\n').map(s=>s.trim()).filter(Boolean)){
    if(busy||!policy.data)return;trigger.current=document.activeElement as HTMLElement|null;
    setBusy('preview');setError('');setMessage('');
    try{const result=await api<Preview>('/api/admin/resource-policy/previews','POST',{version:policy.data.version,roots,read_roots});if(alive.current)setPreview(result);}
    catch(e){if(alive.current)setError((e as {status?:number}).status===422&&(e as Error).message.startsWith('请求失败（422）')?'请填写最多 20 个无重复、互不包含的相对目录；不能包含 ..、电脑盘符或控制字符。':(e as Error).message);}
    finally{if(alive.current)setBusy('');}
  }
  async function confirm(){
    if(busy||!preview)return;setBusy('confirm');setError('');
    try{const result=await api<Policy>('/api/admin/resource-policy','PUT',{token:preview.token,acknowledgement:'invalidate_previous_resource_contexts'});
      if(alive.current){setPaths(result.roots.join('\n'));setReadPaths((result.read_roots||[]).join('\n'));setPreview(undefined);policy.reload();setMessage(`资源授权 v${result.version} 已生效。旧资源任务与派生上下文已失效，新任务按当前目录范围执行。`);}}
    catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy('');}
  }
  const loading=!!busy;
  return <div className="page-stack">
    <div className="pane-toolbar"><h2>资源授权</h2><button className="button" disabled={loading} onClick={()=>{setError('');setMessage('');policy.reload();}}>重新加载</button></div>
    <ErrorNotice error={policy.error||(!preview?error:'')}/>{message&&<p role="status">{message}</p>}
    {!policy.data?<p role="status">{policy.error?'读取失败，请重新加载后管理授权。':'正在读取资源授权…'}</p>:<>
      <section className="data-card form-stack" aria-label="当前授权"><div className="pane-toolbar"><h3>当前授权</h3><span className="status-badge">{policy.data.available?(policy.data.read_roots?.length?'正文授权':'只读清单'):policy.data.roots.length?'资料离线':'尚未授权'}</span></div>
        <p className="muted">v{policy.data.version} · {policy.data.mounted?'资料卷已挂载':'资料卷不可用'}{policy.data.updated_at?` · ${formatTime(policy.data.updated_at)}`:''}</p>
        {policy.data.reason&&<p>{policy.data.reason}</p>}
        {policy.data.roots.length?<ul className="resource-roots">{policy.data.roots.map(path=><li key={path}><code>{path}</code>{path==='.'&&'（整个资料卷）'}</li>)}</ul>:<p className="muted">任务、聊天协助和子助手均不能访问服务器目录。</p>}
        <p className="muted">只列出已授权目录及后代的文件名、类型、大小和修改时间。清单授权不包含正文；不会修改文件或执行程序，不能访问用户电脑。插件权限、角色范围和每次任务授权仍须同时满足。</p>
        <h4>正文目录</h4>{policy.data.read_roots?.length?<ul className="resource-roots">{policy.data.read_roots.map(path=><li key={path}><code>{path}</code></li>)}</ul>:<p className="muted">尚未允许正文读取；旧清单授权不会自动扩权。</p>}
        <a className="button" href="#/plugins/skills">查看技能</a>
      </section>
      <form className="data-card form-stack" onSubmit={e=>{e.preventDefault();void review(paths.split('\n').map(s=>s.trim()).filter(Boolean));}}>
        <h3>允许目录</h3><label>相对目录<textarea name="resource_roots" rows={6} maxLength={8020} value={paths} disabled={loading||!policy.data.mounted} placeholder={'例如：projects\nshared'} onInput={e=>setPaths(e.currentTarget.value)} onChange={e=>setPaths(e.target.value)} aria-describedby="resource-path-help"/></label>
        <small id="resource-path-help" className="muted">每行一个目录，最多 20 个，使用 / 分隔。目录相对部署指定的资料卷；填写 . 表示整个资料卷。目录须已存在且不能是符号链接。留空可撤销全部授权；不能同时填写父目录和它的子目录。</small>
        <label>正文目录<textarea name="resource_read_roots" rows={4} maxLength={8020} value={readPaths} disabled={loading||!policy.data.mounted} placeholder={'例如：projects/docs'} onInput={e=>setReadPaths(e.currentTarget.value)} onChange={e=>setReadPaths(e.target.value)} aria-describedby="resource-read-help"/></label>
        <small id="resource-read-help" className="muted">可选，每行一个，必须位于上方允许目录内。允许文档读取插件保存 TXT、Markdown、CSV、文本 PDF、DOCX、XLSX 的受控快照；插件需在插件商店另行确认安装。不会自动向模型发送正文，需在聊天中选择附件。留空关闭正文读取。</small>
        {!policy.data.mounted&&<p className="muted">请先由部署管理员挂载受控资料目录；离线时仍可撤销现有授权。</p>}
        <p className="muted">任何范围变更都会停止使用旧资源任务、协作及派生上下文，包括仅增加目录。原始文件不变，已发往模型或 QQ 的内容无法撤回。先预览范围，再明确确认。</p>
        <div className="actions"><button className="button primary" disabled={loading||!policy.data.mounted}>{busy==='preview'?'正在校验':'预览更改'}</button><button type="button" className="button" disabled={loading||!policy.data.roots.length} onClick={()=>void review([],[])}>撤销全部</button></div>
      </form>
    </>}
    {preview&&<Modal title="确认授权" returnFocus={trigger.current} dismissible={!loading} close={()=>{setPreview(undefined);setError('');}}><div className="form-stack">
      <p>从授权 v{preview.before.version} 创建新版本</p><div className="card-grid">{[['原有范围',preview.before.roots],['新的范围',preview.after.roots]].map(([title,roots])=><section key={title as string}><h3>{title as string}</h3>{(roots as string[]).length?<ul className="resource-roots">{(roots as string[]).map(p=><li key={p}><code>{p}</code>{p==='.'&&'（整个资料卷）'}</li>)}</ul>:<p>不允许访问任何目录</p>}</section>)}</div>
      <div className="card-grid">{[['原正文范围',preview.before.read_roots||[]],['新正文范围',preview.after.read_roots||[]]].map(([title,roots])=><section key={title as string}><h3>{title as string}</h3>{(roots as string[]).length?<ul className="resource-roots">{(roots as string[]).map(p=><li key={p}><code>{p}</code></li>)}</ul>:<p>不允许读取正文</p>}</section>)}</div>
      <p>{preview.note}</p><p role="status">{clock>=preview.expires_at?'预览已过期，请关闭后重新预览。':`预览有效至 ${formatTime(preview.expires_at)}`}</p><ErrorNotice error={error}/>
      <div className="actions"><button className="button" disabled={loading} onClick={()=>{setPreview(undefined);setError('');}}>取消更改</button><button className="button primary" disabled={loading||clock>=preview.expires_at} onClick={()=>void confirm()}>{busy==='confirm'?'正在提交':preview.after.roots.length?'确认授权':'确认撤销'}</button></div>
    </div></Modal>}
  </div>;
}
