import React from 'react';
import type { Model, Provider } from './api';
import type {components} from './api.generated';

export const modelPurposes: Record<string, string> = {chat:'日常对话', extraction:'记忆抽取', embedding:'向量嵌入', rerank:'检索重排'};
export type ModelPolicy = {purpose: string; model_id: string; version: number};

type RoutingInput = Required<components['schemas']['RoutingInput']>;
type RoutingView = Required<components['schemas']['RoutingView']>;
type RoutingPreview = components['schemas']['RoutingPreview'];

export function RoutingControls({models, providers, primary, request}: {
  models: Model[]; providers: Provider[]; primary?: ModelPolicy;
  request<T>(url: string, method?: string, body?: unknown): Promise<T>;
}) {
  const [saved,setSaved]=React.useState<RoutingView>(),[form,setForm]=React.useState<RoutingInput>();
  const [preview,setPreview]=React.useState<RoutingPreview>(),[pending,setPending]=React.useState<RoutingInput>();
  const [busy,setBusy]=React.useState(false),[error,setError]=React.useState(''),[notice,setNotice]=React.useState('');
  const [revision,reload]=React.useState(0);
  React.useEffect(()=>{
    let current=true;setError('');setPreview(undefined);setPending(undefined);setForm(undefined);
    void request<RoutingView>('/api/model-routing').then(value=>{if(current){setSaved(value);setForm({
      version:value.version,mode:value.mode,candidate_ids:value.candidate_ids,allow_fallback:value.allow_fallback,
      cross_provider:value.cross_provider,minimum_context:value.minimum_context});}}).catch(e=>{if(current)setError((e as Error).message);});
    return ()=>{current=false;};
  },[request,primary?.version,revision]);
  const update=(values:Partial<RoutingInput>)=>{setForm(f=>f?{...f,...values}:f);setPreview(undefined);setPending(undefined);setNotice('');};
  const available=models.filter(m=>m.purpose==='chat'&&m.status==='verified'&&providers.some(p=>p.id===m.provider_id&&p.enabled));
  const candidates=form?.candidate_ids||[];
  const routed=form?.mode==='automatic'||form?.allow_fallback;
  return <section className="data-card form-stack" aria-labelledby="routing-controls-title">
    <h3 id="routing-controls-title">自动选模</h3>
    <p className="muted">默认固定模型。开启后按候选顺序筛选已验证的能力与上下文；不推测费用或速度。仅适用于 Web／QQ 普通聊天最终回复，人格指定模型、任务、子助手、记忆和语音保持各自配置。</p>
    {error&&<p role="alert" className="error">{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    {!form?<><p role="status" className="muted">{error?'路由配置未加载':'正在加载路由配置…'}</p>{error&&<button className="button" onClick={()=>reload(x=>x+1)}>重新加载</button>}</>:
    <form className="form-stack" onSubmit={async e=>{
      e.preventDefault();if(busy)return;setError('');setNotice('');setPreview(undefined);setBusy(true);
      const body={...form,candidate_ids:routed?[primary?.model_id||'',...candidates.filter(id=>id!==primary?.model_id)]:[],cross_provider:!!routed&&form.cross_provider};
      try{const result=await request<RoutingPreview>('/api/model-routing/preview','POST',body);setPending(body);setPreview(result);}
      catch(e){setError((e as Error).message);}finally{setBusy(false);}
    }}>
      {saved&&!saved.current&&<p role="status" className="error">{saved.notice}。重新核对候选并预览；也可关闭路由恢复固定配置。</p>}
      <fieldset disabled={busy} className="form-stack"><legend>调用策略</legend>
        <label>选模方式<select aria-label="选模方式" value={form.mode} onChange={e=>update({mode:e.target.value as RoutingInput['mode']})}><option value="fixed">固定模型</option><option value="automatic">自动选模</option></select></label>
        <label className="checkbox"><input type="checkbox" checked={form.allow_fallback} onChange={e=>update({allow_fallback:e.target.checked})}/>允许受控降级</label>
        <p className="muted">未输出任何文字时，遇限流、部分服务故障或网络超时，最多再调用一个已授权备用模型。已有部分回答则停止并显示错误；不会重新执行工具。</p>
        {routed&&<>
          <label>最低上下文<input aria-label="最低上下文" type="number" min={2048} max={2000000} value={form.minimum_context} onChange={e=>update({minimum_context:Number(e.target.value)})} required/></label>
          <p className="muted">第一项为上方保存的固定对话模型。自动模式会跳过能力或上下文不足的模型；备用模型不能比实际首选的上下文更小。上下文容量为配置值，仍需真实服务验证。</p>
          <ol className="form-stack">
            {[primary?.model_id||'',...candidates.filter(id=>id!==primary?.model_id)].map((id,index)=><li key={id||'missing'}>
              <strong>{models.find(m=>m.id===id)?.name||'请先保存固定对话模型'}</strong><small className="block muted">{providers.find(p=>p.id===models.find(m=>m.id===id)?.provider_id)?.name||'尚未配置'}</small>
              {index>0&&<div className="actions"><button className="button" type="button" disabled={index===1} aria-label={`上移候选${index+1}`} onClick={()=>{const ids=[primary?.model_id||'',...candidates.filter(x=>x!==primary?.model_id)];[ids[index-1],ids[index]]=[ids[index],ids[index-1]];update({candidate_ids:ids});}}>上移</button><button type="button" className="button" aria-label={`移除候选${index+1}`} onClick={()=>update({candidate_ids:candidates.filter(x=>x!==id)})}>移除</button></div>}
            </li>)}
          </ol>
          <label>添加候选<select aria-label="添加候选" value="" disabled={candidates.filter(id=>id!==primary?.model_id).length>=5||!primary} onChange={e=>{if(e.target.value)update({candidate_ids:[primary?.model_id||'',...candidates.filter(id=>id!==primary?.model_id),e.target.value]});}}><option value="">请选择已验证对话模型</option>{available.filter(m=>m.id!==primary?.model_id&&!candidates.includes(m.id)).map(m=><option key={m.id} value={m.id}>{m.name} · {providers.find(p=>p.id===m.provider_id)?.name}</option>)}</select></label>
          <label className="checkbox"><input type="checkbox" checked={form.cross_provider} onChange={e=>update({cross_provider:e.target.checked})}/>允许向候选中的其他服务连接发送相同资料</label>
          <p className="muted">候选服务可能收到问题、授权历史、长期记忆和附件。内网连接不是数据本地化保证；有外发限制时，只选择允许接收这些资料的服务。服务地址或代理变化后必须重新授权。</p>
        </>}
      </fieldset>
      <button className="button" type="submit" disabled={busy||(!primary&&!!routed)}>{busy?'正在处理…':'预览策略'}</button>
    </form>}
    {preview&&pending&&<section className="data-card form-stack" aria-label="策略预览">
      <h4>策略预览</h4><p>{preview.notice}</p>
      <p>选模：{pending.mode==='automatic'?'自动选模':'固定模型'} · 降级：{pending.allow_fallback?'最多一个备用':'关闭'} · 跨连接：{pending.cross_provider?'允许所列接收者':'关闭'}</p>
      {preview.recipients.map(r=><div key={r.model_id}><strong>{r.model} · {r.provider_name}</strong><p className="wrap">{r.base_url}</p><small>{r.protocol} · 配置 v{r.provider_version} · 上下文 {r.context_window}</small></div>)}
      {!preview.recipients.length&&<p>关闭自动选模与降级，恢复固定配置。</p>}
      <p className="muted">预览五分钟内有效。确认或关闭路由将使尚在进行的旧路由回合停止。</p>
      <div className="actions"><button type="button" className="button primary" disabled={busy} onClick={async()=>{
        if(busy)return;setBusy(true);setError('');
        try{const value=await request<RoutingView>('/api/model-routing','PUT',{...pending,preview_digest:preview.preview_digest,preview_expires_at:preview.preview_expires_at});setSaved(value);setForm({...pending,version:value.version});setPreview(undefined);setPending(undefined);setNotice('策略已保存，新普通聊天按此配置执行。');}
        catch(e){setError((e as Error).message);setPreview(undefined);setPending(undefined);}finally{setBusy(false);}
      }}>{busy?'正在保存…':'确认策略'}</button><button className="button" disabled={busy} onClick={()=>{setPreview(undefined);setPending(undefined);}}>取消预览</button></div>
    </section>}
  </section>;
}

export function ModelSetup({providers, models, policies}: {providers: Provider[]; models: Model[]; policies: ModelPolicy[]}) {
  const enabled = (m: Model) => providers.some(p => p.id === m.provider_id && p.enabled);
  const rows = Object.entries(modelPurposes).map(([purpose, title]) => {
    const policy = policies.find(p => p.purpose === purpose);
    const selected = models.find(m => m.id === policy?.model_id);
    const candidates = models.filter(m => m.purpose === purpose && enabled(m));
    const ready = !!selected && selected.purpose === purpose && enabled(selected) && selected.status === 'verified';
    let status = '等待配置', href = '#/models/catalog', next = '添加模型';
    if (!providers.some(p => p.enabled)) {status = '缺少连接'; href = '#/models/providers'; next = '配置连接';}
    else if (selected && !enabled(selected)) {status = '连接停用'; href = '#/models/providers'; next = '启用连接';}
    else if (ready) {status = '配置就绪'; href = '#/models/policies'; next = '查看配置';}
    else if (selected?.status === 'failed') {status = '验证失败'; next = '重新验证';}
    else if (selected) {status = '需要验证'; next = '验证模型';}
    else if (candidates.some(m => m.status === 'verified')) {status = '等待选用'; href = '#/models/policies'; next = '选择模型';}
    else if (candidates.length) {status = '需要验证'; next = '验证模型';}
    return {purpose, title, selected, ready, status, href, next};
  });
  const chatReady = rows[0].ready, memoryReady = rows.slice(1).every(r => r.ready);
  return <section className="data-card" aria-labelledby="model-setup-title">
    <h3 id="model-setup-title">配置检查</h3>
    <p className="muted">{chatReady ? '对话模型已选用。' : '先配置日常对话模型，即可开始文字聊天。'}{memoryReady ? '记忆模型已配齐，实际索引与召回仍需联调验证。' : '长期记忆还需抽取、嵌入和重排三个用途。'}</p>
    <ul className="setup-list">{rows.map(row => <li key={row.purpose}>
      <div><strong>{row.title}</strong><small className="block wrap">{row.selected?.name || '尚未选用模型'}</small></div>
      <span className={`status-badge ${row.ready ? 'status-verified' : ''}`}>{row.status}</span>
      <a className="button" href={row.href} aria-label={`${row.title}：${row.next}`}>{row.next}</a>
    </li>)}</ul>
    <p className="muted">这里仅检查模型配置。连接验证不代表中文质量达标，也不代表记忆服务、后台作业及整个闭环已验收。</p>
  </section>;
}
