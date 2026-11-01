import React, { useEffect, useState } from 'react';
import { api } from './api';
import type { Provider, ModelInput, ProviderModelCatalog } from './api';
import { modelPurposes } from './ModelSetup';

type CatalogState = {provider: string; status: 'loading' | 'ready' | 'failed'; names: string[]; error: string; purposes?:Record<string,string[]|null|undefined>};

export function AddModelForm({providers, busy, error, save}: {
  providers: Provider[]; busy: boolean; error: string; save(body: ModelInput): Promise<unknown>;
}) {
  const [providerId, setProviderId] = useState(providers.find(p => p.enabled)?.id || '');
  const [name, setName] = useState(''), [query, setQuery] = useState('');
  const [purpose,setPurpose]=useState<ModelInput['purpose']>('chat');
  const [catalog, setCatalog] = useState<CatalogState>({provider: '', status: 'loading', names: [], error: ''});
  const provider = providers.find(p => p.id === providerId);
  useEffect(() => {
    if (!providerId || !provider?.enabled) return;
    const controller = new AbortController();
    let ignore = false;
    setCatalog({provider: providerId, status: 'loading', names: [], error: ''});
    void api<ProviderModelCatalog>(`/api/providers/${encodeURIComponent(providerId)}/models`, 'GET', undefined,
      {signal: controller.signal}).then(data => {
      if (!ignore && data.provider_id === providerId && data.provider_version === provider.version) {
        setCatalog({provider: providerId, status: 'ready', names: data.models.map(m => m.id), purposes:Object.fromEntries(data.models.map(m=>[m.id,m.purposes])), error: ''});
      } else if (!ignore) {
        setCatalog({provider: providerId, status: 'failed', names: [], error: '服务连接已变化，请重新打开添加模型窗口'});
      }
    }).catch(e => {if (!ignore) setCatalog({provider: providerId, status: 'failed', names: [], error: (e as Error).message});});
    return () => {ignore = true; controller.abort();};
  }, [providerId, provider?.version, provider?.enabled]);
  const current = catalog.provider === providerId && provider?.enabled;
  const loaded = current && catalog.status === 'ready';
  const allowed=(value:string)=>provider?.protocol==='anthropic'?['chat','extraction'].includes(value):provider?.protocol==='gemini'?value!=='rerank':true;
  const supports=(id:string)=>!catalog.purposes?.[id]||catalog.purposes[id]!.includes(purpose);
  const filtered = loaded ? catalog.names.filter(n => supports(n)&&n.toLowerCase().includes(query.toLowerCase())) : [];
  // Always keep the selected option visible while filtering, so the displayed value matches submission.
  const options = name && loaded && catalog.names.includes(name) && !filtered.includes(name) ? [name, ...filtered] : filtered;
  const canSave = !!provider?.enabled && !!name.trim() && loaded && catalog.names.includes(name)&&allowed(purpose)&&supports(name);
  return <form className="form-stack" onSubmit={e => {
    e.preventDefault();
    if (!canSave || busy) return;
    const form = new FormData(e.currentTarget);
    void save({provider_id: providerId, name: name.trim(), purpose: form.get('purpose') as ModelInput['purpose'],
      context_window: Number(form.get('context_window'))});
  }}>
    <label>服务连接<select name="provider_id" required value={providerId} disabled={busy} onChange={e => {
      setProviderId(e.target.value); setName(''); setQuery('');setPurpose('chat');
    }}><option value="" disabled>请选择服务连接</option>{providers.map(p => <option key={p.id} value={p.id} disabled={!p.enabled}>{p.name}{p.enabled ? '' : '（已停用）'}</option>)}</select></label>
    {!provider?.enabled ? <p role="status" className="muted">请先添加并启用服务连接。</p> : <>
      <div aria-live="polite">
        {(!current || catalog.status === 'loading') && <p role="status" className="muted">正在加载供应商模型…</p>}
        {current && catalog.status === 'failed' && <p role="alert" className="error">{catalog.error}</p>}
        {loaded && <p className="muted">{catalog.names.length ? `已获取 ${catalog.names.length} 个模型。` : '供应商返回的模型列表为空。'}</p>}
      </div>
        <label>筛选模型<input value={query} onChange={e => setQuery(e.target.value)} disabled={!loaded || busy} placeholder="输入模型名称筛选"/></label>
        <label>模型标识<select name="name" value={name} onChange={e => setName(e.target.value)} disabled={!loaded || busy} required><option value="">请选择供应商模型</option>{options.map(id => <option key={id} value={id}>{id}</option>)}</select></label>
        {loaded && catalog.names.length > 0 && filtered.length === 0 && <p className="muted">没有匹配模型，请修改筛选词。</p>}
    </>}
    <small className="muted">列表来自所选连接，请从列表选择；添加后仍需验证模型用途能力。</small>
    <label>模型用途<select name="purpose" disabled={busy} value={purpose} onChange={e=>{setPurpose(e.target.value as ModelInput['purpose']);setName('');}}>{Object.entries(modelPurposes).map(([key, title]) => <option key={key} value={key} disabled={!allowed(key)}>{title}{allowed(key)?'':'（此协议未接入）'}</option>)}</select></label>
    <label>上下文窗口<input name="context_window" type="number" min={2048} max={2000000} defaultValue={8192} required disabled={busy}/><small className="muted">按提供商文档填写，模型列表通常不含此参数。</small></label>
    {error && <p role="alert" className="error">{error}</p>}
    <button className="button primary" disabled={busy || !canSave}>{busy ? '正在添加' : '添加模型'}</button>
  </form>;
}
