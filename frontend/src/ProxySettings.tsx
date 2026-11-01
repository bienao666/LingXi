import React, { useEffect, useState } from 'react';
import { api } from './api';
import type { ProxyConfig, ProxyTestResult } from './api';

const routeLabels: Record<string,string> = {http_proxy:'HTTP 代理',https_proxy:'HTTPS 代理',all_proxy:'ALL 代理',no_proxy:'直连规则',internal_direct:'内部直连',direct:'直接连接'};

export function ProxySettings({embedded=false}:{embedded?:boolean}) {
  const [config, setConfig] = useState<ProxyConfig>();
  const [error, setError] = useState(''), [message, setMessage] = useState(''), [busy, setBusy] = useState('');
  const [testUrl, setTestUrl] = useState('https://example.com'), [result,setResult] = useState<ProxyTestResult>();
  async function reload() {
    setBusy('load'); setError(''); setMessage(''); setResult(undefined);
    try {setConfig(await api<ProxyConfig>('/api/admin/proxy'));}
    catch(e) {setError((e as Error).message);} finally {setBusy('');}
  }
  useEffect(() => {void reload();}, []);
  function update(values: Partial<ProxyConfig>) {setConfig(c => c ? {...c,...values} : c); setMessage(''); setResult(undefined); setError('');}
  async function save(e: React.FormEvent) {
    e.preventDefault(); if (!config || busy) return;
    setBusy('save'); setError(''); setMessage(''); setResult(undefined);
    try {
      const saved=await api<ProxyConfig>('/api/admin/proxy','PUT',config);
      setConfig(saved);setMessage(saved.enabled ? '代理已启用，新请求立即生效。正在执行的请求沿用原配置。' : '代理已停用，新请求将直接连接。');
    } catch(e) {setError((e as Error).message);} finally {setBusy('');}
  }
  async function test() {
    if (!config || busy) return;
    setBusy('test'); setError(''); setMessage(''); setResult(undefined);
    try {setResult(await api<ProxyTestResult>('/api/admin/proxy/test','POST',{...config,test_url:testUrl}));}
    catch(e) {setError((e as Error).message);} finally {setBusy('');}
  }
  return <div className="page-stack"><div className="pane-toolbar">{embedded?<h3>代理设置</h3>:<h2>系统配置</h2>}<button className="button" disabled={!!busy} onClick={() => void reload()}>重新加载</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {message && <p role="status">{message}</p>}
    {!config ? <p role="status" className="muted">{busy ? '正在加载系统配置…' : '配置加载失败，请重新加载。'}</p> : <form className="data-card form-stack" onSubmit={e => void save(e)}>
      <h3>系统代理</h3><p className="muted">统一管理应用对外 HTTP(S) 请求。模型调用、模型目录及记忆服务的对外请求共用此配置。</p>
      <label className="checkbox"><input type="checkbox" checked={config.enabled} disabled={!!busy} onChange={e=>update({enabled:e.target.checked})}/>启用系统代理</label>
      {(['http_proxy','https_proxy','all_proxy'] as const).map(key=><label key={key}>{routeLabels[key]}<input name={key} value={config[key]} disabled={!!busy} maxLength={2000} placeholder={key==='all_proxy'?'socks5://172.20.192.1:7890':'http://172.20.192.1:7890'} onChange={e=>update({[key]:e.target.value})}/></label>)}
      <p className="muted">HTTP／HTTPS 优先使用各自代理，未填写时使用 ALL 代理。支持 http、https、socks5、socks5h；启用后须覆盖两种请求，代理失败不会自动改为直连。</p>
      <label>直连规则<textarea name="no_proxy" value={(config.no_proxy || []).join(', ')} disabled={!!busy} rows={2} placeholder=".example.local, 10.0.0.0/8" onChange={e=>update({no_proxy:e.target.value.split(',')})}/><small className="muted">使用逗号分隔主机名、以点开头的域名后缀、IP 或 CIDR；* 表示全部直连。</small></label>
      <p className="muted wrap">固定直连：{(config.protected_no_proxy || []).join('、')} 以及回环地址。数据库、容器内部通信与本机健康检查保持直连。</p>
      <p className="muted">地址从服务器或 Docker 容器视角解析；localhost 指向当前容器。配置不会更改 Windows、浏览器或 Docker 构建的系统代理。</p>
      <div className="actions"><button className="button primary" disabled={!!busy}>{busy==='save'?'正在保存':'保存配置'}</button><span className="muted">配置 v{config.version}</span></div>
      <hr/>
      <h3>连接测试</h3><label>测试地址<input type="url" value={testUrl} disabled={!!busy} onChange={e=>{setTestUrl(e.target.value);setResult(undefined);}} placeholder="https://example.com"/></label>
      <p className="muted">使用当前表单测试公网 HTTPS 地址，不保存配置；请先勾选启用，再测试代理路径。</p>
      <div className="actions"><button type="button" className="button" disabled={!!busy || !testUrl} onClick={()=>void test()}>{busy==='test'?'正在测试':'测试连接'}</button></div>
      {result && <p role="status" className={result.ok?'':'error'}>{result.ok?'连接成功':'服务返回非成功状态'} · HTTP {result.status_code} · {result.duration_ms} ms · {routeLabels[result.route] || result.route}</p>}
    </form>}
  </div>;
}
