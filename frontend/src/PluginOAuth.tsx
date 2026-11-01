import React,{useEffect,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice,formatTime,Modal,useResource} from './ui';

export type OAuthEntry={authorization_url:string;token_url:string;revocation_url:string;issuer:string;scopes:string[];client_auth:string;resource:string};
type Connection=components['schemas']['ConnectionView'];
type Flow=components['schemas']['OAuthFlowView'];
type Started=components['schemas']['OAuthStarted'];
type Installation=components['schemas']['InstallationView'];
const labels:Record<string,string>={pending:'等待授权',exchanging:'正在核验',succeeded:'授权完成',failed:'授权失败',expired:'授权过期',cancelled:'授权取消'};

export function OAuthHistory({changed}:{changed():void}){
  const data=useResource<Flow[]>('/api/plugin-oauth/flows'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[cancel,setCancel]=useState<Flow|null>(null);
  const result=new URLSearchParams(window.location.hash.split('?')[1]||'').get('oauth');
  return <section aria-label="授权记录" className="page-stack"><div className="pane-toolbar"><h2>授权记录</h2><button className="button" onClick={()=>{data.reload();changed();}}>刷新授权</button></div>
    {result&&<p role="status">{result==='succeeded'?'授权回调已完成，请核对对应账号连接与授权范围。':'授权没有完成。请检查客户端配置、回调地址及记录，重新发起授权；原授权码不能重放。'}</p>}<ErrorNotice error={error||data.error}/>
    {!data.data&&!data.error&&<p role="status">正在加载授权记录…</p>}{data.data?.length===0&&<div className="data-empty">尚无 OAuth 授权记录。安装声明 OAuth 的插件后，可在上方配置授权。</div>}
    {data.data?.map(f=><article className="data-card" key={f.id}><div className="pane-toolbar"><h3>连接 {f.connection_id.slice(0,12)}</h3><span className="status-badge">{labels[f.status]||'状态未知'}</span></div><p>{formatTime(f.created_at)} · 有效至 {formatTime(f.expires_at)}</p>{['pending','exchanging'].includes(f.status)&&<button className="button" disabled={busy} onClick={()=>setCancel(f)}>取消授权</button>}{['failed','expired'].includes(f.status)&&<p className="muted">请从对应插件重新配置授权。系统未保存授权码或上游错误正文。</p>}</article>)}
    {cancel&&<Modal title="取消授权" close={()=>setCancel(null)} dismissible={!busy}><p>取消连接 {cancel.connection_id.slice(0,12)} 本次授权，迟到回调不会写入令牌。此前有效的连接保持原状态；第三方账号权限请在服务方核对。</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>setCancel(null)}>继续等待</button><button className="button primary" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api(`/api/plugin-oauth/flows/${cancel.id}/cancel`,'POST',{}).then(()=>{setCancel(null);data.reload();changed();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在取消':'确认取消'}</button></div></Modal>}
  </section>;
}

export function OAuthConnectionEditor({capability,row,entry,close,done}:{capability:{name:string;installation_id:string;id:string;plugin_id:string};row?:Connection;entry:OAuthEntry;close():void;done():void}){
  const installations=useResource<Installation[]>('/api/plugin-installations');
  const installation=installations.data?.find(r=>r.id===capability.installation_id);
  const [clientId,setClientId]=useState(''),[secret,setSecret]=useState(''),[allowPrivate,setAllowPrivate]=useState(row?.allow_private||false),[approved,setApproved]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[started,setStarted]=useState<Started|null>(null);
  useEffect(()=>{setApproved(false);},[clientId,secret,allowPrivate]);
  const leave=()=>{if(busy)return;if(started){setBusy(true);void api(`/api/plugin-oauth/flows/${started.id}/cancel`,'POST',{}).then(done).catch(e=>setError(e.message)).finally(()=>setBusy(false));}else close();};
  return <Modal title="账号连接" close={leave} dismissible={!busy}><h3>{capability.name} · OAuth</h3><p>{capability.plugin_id} · {capability.id}</p><p className="muted">使用你在服务方注册的专用客户端。核对以下地址与权限，授权码由服务器一次性核验。安装插件不表示已连接账号。</p>
    <div className="data-card"><p>服务标识：<code className="plugin-digest">{entry.issuer}</code></p><p>授权地址：<code className="plugin-digest">{entry.authorization_url}</code></p><p>令牌地址：<code className="plugin-digest">{entry.token_url}</code></p><p>撤销地址：<code className="plugin-digest">{entry.revocation_url||'未提供；撤销本地授权后还需到服务方核对'}</code></p>{entry.resource&&<p>目标资源：<code className="plugin-digest">{entry.resource}</code></p>}<h4>请求范围</h4><ul>{entry.scopes.map(s=><li key={s}><code>{s}</code></li>)}</ul><p>客户端认证：{entry.client_auth==='none'?'公共客户端（无需密钥）':entry.client_auth}</p></div>
    <ErrorNotice error={error||installations.error}/>{started?<><p role="status">授权入口已生成，有效至 {formatTime(started.expires_at)}。尚未连接账号。</p><p>请将下方完整回调地址登记在服务方，然后前往授权。返回后仍需完成灵犀的身份确认。</p><code className="plugin-digest">{started.redirect_uri}</code><div className="actions"><a className="button primary" href={started.authorization_url} rel="noreferrer">前往授权</a><button className="button" disabled={busy} onClick={leave}>{busy?'正在取消':'放弃授权'}</button></div></>:<form className="form-stack" onSubmit={e=>{e.preventDefault();if(busy||!approved||!installation)return;setBusy(true);setError('');void api<Started>('/api/plugin-oauth/start','POST',{installation_id:installation.id,capability_id:capability.id,installation_version:installation.version,connection_version:row?.version||0,client_id:clientId,client_secret:secret,allow_private:allowPrivate,acknowledged:true}).then(v=>{setStarted(v);setSecret('');}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>
      <label>客户端标识<input required autoComplete="off" value={clientId} maxLength={500} disabled={busy} onChange={e=>setClientId(e.target.value)}/></label>{entry.client_auth!=='none'&&<label>客户端密钥<input required type="password" autoComplete="off" value={secret} maxLength={4000} disabled={busy} onChange={e=>setSecret(e.target.value)}/></label>}
      <label className="checkbox"><input type="checkbox" checked={allowPrivate} disabled={busy} onChange={e=>setAllowPrivate(e.target.checked)}/>明确允许以上固定远程入口访问服务器内网</label><p className="muted">成功后替换此连接，新权限须重新确认；刷新仅限已授权范围。失败或中断不重放令牌请求，不代表第三方账号已退出。</p><label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已核对服务地址、客户端、权限范围和内网设置，同意发起账号授权。</label>{!installation&&!installations.error&&<p role="status">正在核对插件安装版本…</p>}<div className="actions"><button type="button" className="button" disabled={busy} onClick={close}>保留现状</button><button className="button primary" disabled={busy||!approved||!installation}>{busy?'正在生成':'生成授权'}</button></div>
    </form>}
  </Modal>;
}
