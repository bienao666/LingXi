import React from 'react';
import type { Model, Provider } from './api';
import type {components} from './api.generated';

export const modelPurposes: Record<string, string> = {chat:'日常对话', extraction:'记忆抽取', embedding:'向量嵌入', rerank:'检索重排'};
export type ModelPolicy = {purpose: string; model_id: string; version: number};

type MemorySettings = components['schemas']['MemorySettingsView'];
type MemoryPreview = components['schemas']['MemorySettingsPreview'];
type MemoryRebuild = components['schemas']['MemoryRebuildView'];
const memoryStates:Record<string,string>={configured:'已配置',unconfigured:'未配置',unverified:'等待验证',
  not_downloaded:'等待下载',downloading:'正在下载',loading:'正在加载',ready:'模型就绪',failed:'处理失败',unavailable:'暂不可用'};

export function MemoryControls({models,providers,policies,request}:{models:Model[];providers:Provider[];policies:ModelPolicy[];
  request<T>(url:string,method?:string,body?:unknown):Promise<T>}) {
  const [saved,setSaved]=React.useState<MemorySettings>(),[mode,setMode]=React.useState<'simple'|'advanced'>('simple');
  const [selected,setSelected]=React.useState<Record<string,string>>({}),[preview,setPreview]=React.useState<MemoryPreview>();
  const [rebuilds,setRebuilds]=React.useState<MemoryRebuild[]>([]),[busy,setBusy]=React.useState(false);
  const [error,setError]=React.useState(''),[notice,setNotice]=React.useState(''),[revision,reload]=React.useState(0);
  const [restarted,setRestarted]=React.useState(false),[dirty,setDirty]=React.useState(false);
  const pending=React.useRef(false);
  const errorRef=React.useRef<HTMLParagraphElement>(null);
  const receive=(value:MemorySettings)=>{setSaved(value);setMode(value.mode);setDirty(false);setPreview(undefined);};
  React.useEffect(()=>{if(error)errorRef.current?.focus();},[error]);
  React.useEffect(()=>{
    let current=true;
    void request<MemorySettings>('/api/memory-settings').then(value=>{
      if(!current)return;if(!value||!['simple','advanced'].includes(value.mode))throw new Error('记忆配置未能加载，请重试');
      receive(value);setSelected(Object.fromEntries(policies.filter(p=>p.purpose!=='chat').map(p=>[p.purpose,p.model_id])));setError('');
    }).catch(e=>{if(current)setError((e as Error).message);});
    return ()=>{current=false;};
  },[request,revision,policies]);
  React.useEffect(()=>{
    if(!saved)return;
    let current=true;
    const poll=async()=>{
      try{
        const value=await request<MemorySettings>('/api/memory-settings');
        if(current&&value.version>=(saved?.version||0)){setSaved(value);if(!dirty)setMode(value.mode);}
        const records=await request<MemoryRebuild[]>('/api/memory-rebuilds');
        if(current&&Array.isArray(records))setRebuilds(records);
      }catch(e){if(current)setError((e as Error).message);}
    };
    void poll();const timer=window.setInterval(()=>void poll(),10000);
    return ()=>{current=false;window.clearInterval(timer);};
  },[request,saved?.version,dirty]);
  const run=async(action:()=>Promise<void>)=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');setNotice('');try{await action();}catch(e){setError((e as Error).message);}finally{pending.current=false;setBusy(false);}};
  const body=()=>({version:saved?.version||0,mode,models:mode==='advanced'?Object.fromEntries(Object.entries(selected).filter(([,id])=>id)): {}});
  const update=()=>{setDirty(true);setPreview(undefined);setNotice('');};
  const currentRebuild=rebuilds.find(r=>r.id===saved?.rebuild_id);
  const targetChanged=!!currentRebuild?.error?.includes('目标配置已变化');
  return <section className="data-card form-stack" aria-labelledby="memory-controls-title">
    <h3 id="memory-controls-title">记忆配置</h3>
    <p className="muted">简单模式只需对话模型：抽取沿用固定对话服务，嵌入和重排在服务器运行。抽取可能产生服务费用；内置模型无需 GPU，首次下载和 CPU 推理会占用服务器资源。抽取或内置服务失败不影响正常文字聊天。</p>
    {error&&<p role="alert" className="error" ref={errorRef} tabIndex={-1}>{error}</p>}
    {notice&&<p role="status">{notice}</p>}
    {!saved?<><p role="status">{error?'记忆配置不可用':'正在检查记忆配置…'}</p><button className="button" onClick={()=>reload(x=>x+1)}>重新加载</button></>:
    <>
      <p className="muted">当前：{saved.mode==='simple'?'简单模式':'高级模式'} · 配置 v{saved.version} · 索引代际 {saved.epoch}</p>
      {saved.rebuild_required&&<p role="alert" className="error">嵌入模型指纹已变化，已阻止旧索引检索。请按当前配置预览并确认重建。</p>}
      {saved.phase!=='ready'&&<p role="status">{saved.phase==='draining'?'正在等待旧记忆请求结束':saved.phase==='blocked'?'旧请求状态待核对':'索引正在后台重建'}。暂不检索长期记忆，文字聊天、查看与纠正删除继续可用。</p>}
      <ul className="setup-list">{saved.purposes.map(p=><li key={p.purpose}><div><strong>{modelPurposes[p.purpose]}</strong><small className="block wrap">{p.model||'尚未配置'} · {{inherited:'沿用对话',builtin:'服务器内置',independent:'独立配置'}[p.source]}</small><small className="block muted wrap">{p.notice}</small></div><span className={`status-badge ${['ready','configured'].includes(p.state)?'status-verified':''}`}>{memoryStates[p.state]||p.state}</span></li>)}</ul>
      {saved.verification_error&&<p role="status" className="error">抽取验证失败：{saved.verification_error}</p>}
      {saved.mode==='simple'&&<div className="actions">
        <button className="button" disabled={busy||['draining','blocked'].includes(saved.phase)} onClick={()=>void run(async()=>{receive(await request<MemorySettings>('/api/memory-settings/verify-extraction','POST'));setNotice('已完成抽取验证，请查看实际状态；验证不代表中文质量达标。');})}>验证抽取</button>
        <button className="button" disabled={busy} onClick={()=>void run(async()=>{await request('/api/memory-settings/prepare','POST');setNotice('已请求准备内置模型，可刷新查看下载与加载状态。');})}>准备模型</button>
        <button className="button" disabled={busy} onClick={()=>reload(x=>x+1)}>刷新状态</button>
      </div>}
      <form className="form-stack" onSubmit={e=>{e.preventDefault();void run(async()=>setPreview(await request<MemoryPreview>('/api/memory-settings/preview','POST',body())));}}>
        <fieldset disabled={busy||saved.phase!=='ready'} className="form-stack"><legend>配置模式</legend>
          <label>记忆模式<select aria-label="记忆模式" value={mode} onChange={e=>{setMode(e.target.value as 'simple'|'advanced');update();}}><option value="simple">简单模式</option><option value="advanced">高级模式</option></select></label>
          {mode==='advanced'&&<div className="card-grid">{Object.entries(modelPurposes).filter(([p])=>p!=='chat').map(([p,title])=><label key={p}>{title}<select aria-label={title+'模型'} value={selected[p]||''} onChange={e=>{setSelected(s=>({...s,[p]:e.target.value}));update();}}><option value="">保留现有配置／尚未选用</option>{models.filter(m=>m.purpose===p&&m.status==='verified'&&providers.some(v=>v.id===m.provider_id&&v.enabled)).map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>)}</div>}
          <p className="muted">高级配置会保留，切回简单模式不会删除。嵌入固定为 1024 维；更换嵌入模型可能需要重建。切换或更换对话模型后，抽取可能需要重新验证。</p>
          <button className="button" type="submit">预览配置</button>
        </fieldset>
      </form>
      {preview&&<section className="data-card form-stack" aria-label="记忆配置预览"><h4>变更预览</h4><p>{preview.notice}</p><p>有效记忆 {preview.active_memories} 条 · {preview.rebuild_required?'需要重建':'无需重建'}</p><ul>{preview.purposes.map(p=><li key={p.purpose}>{modelPurposes[p.purpose]}：{p.model||'尚未配置'} · {{builtin:'服务器内置',inherited:'沿用对话',independent:'独立配置'}[p.source]}</li>)}</ul><div className="actions"><button className="button primary" disabled={busy} onClick={()=>void run(async()=>{receive(await request<MemorySettings>('/api/memory-settings','PUT',{...body(),preview_digest:preview.preview_digest,preview_expires_at:preview.preview_expires_at}));setNotice('配置已确认，请查看索引和模型实际状态。');})}>确认配置</button><button className="button" disabled={busy} onClick={()=>setPreview(undefined)}>取消预览</button></div></section>}
      {currentRebuild&&<section className="form-stack" aria-label="索引重建进度"><h4>重建进度</h4><p>当前有效记忆 {currentRebuild.total} · 已就绪 {currentRebuild.ready} · 待处理 {currentRebuild.pending} · 失败 {currentRebuild.failed}</p>{currentRebuild.error&&<p role="status" className="error">{currentRebuild.error}</p>}{saved.phase==='blocked'&&!targetChanged&&<label className="checkbox"><input type="checkbox" checked={restarted} onChange={e=>setRestarted(e.target.checked)}/>我已由部署端重启 Hindsight，确认旧请求不再运行</label>}{!targetChanged&&(saved.phase==='blocked'||currentRebuild.failed>0)&&<button className="button" disabled={busy||(saved.phase==='blocked'&&!restarted)} onClick={()=>void run(async()=>{receive(await request<MemorySettings>(`/api/memory-rebuilds/${currentRebuild.id}/retry`,'POST',{version:saved.version,acknowledge_service_restarted:restarted}));setRestarted(false);setNotice('已提交恢复请求。');})}>重试重建</button>}{['draining','blocked'].includes(saved.phase)&&<button className="button" disabled={busy} onClick={()=>void run(async()=>{receive(await request<MemorySettings>(`/api/memory-rebuilds/${currentRebuild.id}/cancel`,'POST',{version:saved.version}));setRestarted(false);setNotice('已取消切换，保留原配置与索引代际。');})}>取消重建</button>}</section>}
      <p className="muted">以上状态不代表跨会话召回和 150 题中文质量评测已经通过。模型或索引不可用时会明确提示，不会伪装成成功召回。</p>
    </>}
  </section>;
}

type RoutingInput = Required<components['schemas']['RoutingInput']>;
type RoutingView = Required<components['schemas']['RoutingView']>;
type RoutingPreview = components['schemas']['RoutingPreview'];
const selectionLabels = {priority:'候选顺序',estimated_cost:'参考费用',observed_latency:'历史耗时'};

function SelectionSummary({selection,models}: {selection: RoutingPreview['selection']; models: Model[]}) {
  if(!selection)return null;
  const status=(value:string)=>({ready:'可比较',missing_price:'价格缺失或失效',currency_mismatch:'币种不一致',insufficient_samples:'样本不足'}[value]||'不可比较');
  return <section className="form-stack" aria-label="选择依据">
    <h4>选择依据</h4><p>{selection.reason}</p>
    <p>实际比较：{selectionLabels[selection.applied as keyof typeof selectionLabels]||selection.applied} · 首选：{models.find(m=>m.id===selection.selected_model_id)?.name||'无适合候选'}</p>
    {!!selection.evidence?.length&&<ul className="form-stack">{selection.evidence.map(item=><li key={item.model_id}>
      <strong>{models.find(m=>m.id===item.model_id)?.name||item.model_id}</strong> · {status(item.status)}
      {item.reference_cost!=null&&<small className="block muted">参考估算 {item.reference_cost} {item.currency} · 价格 v{item.price_version}</small>}
      {selection.strategy==='observed_latency'&&<small className="block muted">有效样本 {item.samples} 个 · 总耗时中位数 {item.median_ms==null?'未知':`${item.median_ms} ms`}</small>}
    </li>)}</ul>}
    <p className="muted">这是预览时按最低上下文筛选的快照。新回合按实际图片、上下文和最新证据重新选择；运行中的回合不重新排序。</p>
  </section>;
}

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
      cross_provider:value.cross_provider,minimum_context:value.minimum_context,
      selection_strategy:value.selection_strategy||'priority',reference_currency:value.reference_currency||'CNY',
      reference_input_tokens:value.reference_input_tokens??1000,reference_output_tokens:value.reference_output_tokens??1000});}}).catch(e=>{if(current)setError((e as Error).message);});
    return ()=>{current=false;};
  },[request,primary?.version,revision]);
  const update=(values:Partial<RoutingInput>)=>{setForm(f=>f?{...f,...values}:f);setPreview(undefined);setPending(undefined);setNotice('');};
  const available=models.filter(m=>m.purpose==='chat'&&m.status==='verified'&&providers.some(p=>p.id===m.provider_id&&p.enabled));
  const candidates=form?.candidate_ids||[];
  const routed=form?.mode==='automatic'||form?.allow_fallback;
  return <section className="data-card form-stack" aria-labelledby="routing-controls-title">
    <h3 id="routing-controls-title">自动选模</h3>
    <p className="muted">默认固定模型。自动模式先筛选授权、能力与上下文，再按候选顺序、参考费用或历史耗时选择。仅适用于 Web／QQ 普通聊天最终回复，人格指定模型、任务、子助手、记忆和语音保持各自配置。</p>
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
        {form.mode==='automatic'&&<>
          <label>比较策略<select aria-label="比较策略" value={form.selection_strategy} onChange={e=>update({selection_strategy:e.target.value as RoutingInput['selection_strategy']})}>{Object.entries(selectionLabels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
          {form.selection_strategy==='estimated_cost'&&<>
            <label>参考币种<select aria-label="参考币种" value={form.reference_currency} onChange={e=>update({reference_currency:e.target.value as RoutingInput['reference_currency']})}><option value="CNY">CNY</option><option value="USD">USD</option></select></label>
            <label>参考输入<input aria-label="参考输入" type="number" min={1} max={2000000} value={form.reference_input_tokens} onChange={e=>update({reference_input_tokens:Number(e.target.value)})} required/></label>
            <label>参考输出<input aria-label="参考输出" type="number" min={1} max={2000000} value={form.reference_output_tokens} onChange={e=>update({reference_output_tokens:Number(e.target.value)})} required/></label>
            <p className="muted">单位为 token。使用「用量预算」中当前连接版本的人工价格，不含缓存优惠；只是固定参考用量的估算，不是本轮账单或金额限额。任一适合候选缺价、失效或币种不一致时保留候选顺序，不自动换汇。</p>
          </>}
          {form.selection_strategy==='observed_latency'&&<p className="muted">比较近七天、同模型与连接协议版本的成功普通聊天总耗时中位数；每个模型最多扫描最近 200 条、取 20 个有效样本，至少需 3 个。不是首字延迟或模型质量评测，不额外调用模型测速；样本不足时保留候选顺序。</p>}
        </>}
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
      <SelectionSummary selection={preview.selection} models={models}/>
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
