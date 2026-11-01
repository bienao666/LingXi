import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import {ErrorNotice,formatTime,Modal,useResource} from './ui';
import type {components} from './api.generated';
import {CostAmounts,CostBreakdowns,ModelPrices,AudioUsage,AudioPrices} from './UsageCosts';

type Readiness=components['schemas']['ReadinessView'];
type Health=components['schemas']['OperationalHealth'];
type Usage=components['schemas']['UsageView'];
type Metadata=components['schemas']['MetadataPage'];
type Trace=components['schemas']['TraceView'];
type Graph=components['schemas']['TraceGraph'];
type Bundle=components['schemas']['DiagnosticView'];
type Preferences=components['schemas']['OpsPreferenceView'];
type Budget=components['schemas']['BudgetView'];
type BudgetPreview=components['schemas']['BudgetPreviewView'];
const text=(v:unknown)=>v===null||v===undefined?'未知':String(v);
const num=(v:unknown)=>typeof v==='number'?v:0;
const labels:Record<string,string>={chat:'对话生成',task:'基础任务',agent:'助手协作',plugin:'插件调用',ready:'服务就绪',maintenance:'索引维护',unconfigured:'尚未配置',unavailable:'暂不可用',warning:'需要关注',error:'运行错误',queued:'等待处理',running:'执行中',pending:'等待执行',completed:'已完成',succeeded:'已成功',failed:'已失败',cancelled:'已取消',invalidated:'已失效',preview:'待确认',confirmed:'可下载',expired:'已过期',worker:'后台执行',store:'插件商店',proactive:'主动交流'};
Object.assign(labels,{active:'当前有效',deleted:'已删除',accepted:'协议接受',stored:'已保存',unknown:'结果未知',partial:'部分完成',blocked:'已阻断',waiting_confirmation:'等待确认',waiting_tool:'等待工具',not_sent:'尚未发送'});
const word=(v:unknown)=>labels[text(v)]||text(v);
const bytes=(v:unknown)=>typeof v==='number'?`${(v/1024**3).toFixed(1)} GiB`:'未知';
const actions:Record<string,string>={password_changed:'修改密码',model_budget_update:'调用限额',media_policy_update:'附件策略',model_price_update:'修改单价',backup_requested:'请求备份',backup_policy:'定时备份',backup_started:'开始备份',backup_succeeded:'备份完成',backup_failed:'备份失败',backup_cancelled:'取消备份',backup_interrupted:'备份中断',backup_restore_started:'开始恢复',backup_restore_quarantine:'恢复隔离',totp_setup:'绑定验证',totp_enabled:'启用验证',totp_disabled:'停用验证',totp_recovery_rotated:'更换恢复',totp_recovery_used:'恢复登录',totp_setup_cancelled:'取消绑定',totp_operator_reset:'终端重置',security_attempt:'安全校验',security_failed:'验证失败',login_factor_attempt:'双重校验',login_success:'登录成功',login_failed:'登录失败',logout:'退出登录',session_revoke:'撤销会话',operations_preferences:'修改运维配置',diagnostic_preview:'生成诊断预览',diagnostic_confirm:'确认诊断导出',diagnostic_download:'下载诊断资料',plugin_store_config:'修改商店来源',plugin_store_sync:'同步商店目录',memory_delete:'删除长期记忆',memory_correct:'纠正长期记忆',provider_update:'修改模型连接'};

export function OperationsPages({page}:{page:string}){
  if(page==='usage')return <UsagePage/>;
  if(page==='diagnostics')return <DiagnosticsPage/>;
  return <MetadataPage key={page} page={page}/>;
}

export function OperationsOverview(){
  const live=useResource<Record<string,unknown>>('/api/admin/health');
  const runtime=useResource<Health>('/api/admin/operations/health');
  const readiness=useResource<Readiness>('/api/admin/operations/readiness');
  const [readinessError,setReadinessError]=useState('');
  useEffect(()=>{if(readiness.error)setReadinessError(readiness.error);},[readiness.error]);
  useEffect(()=>{if(readiness.data)setReadinessError('');},[readiness.data]);
  const [refresh,setRefresh]=useState(0);
  useEffect(()=>{const id=window.setInterval(()=>setRefresh(x=>x+1),15000);return()=>window.clearInterval(id);},[]);
  useEffect(()=>{if(refresh){runtime.reload();readiness.reload();}},[refresh]);
  const worker=runtime.data?.worker;
  return <div className="page-stack"><div className="pane-toolbar"><h2>系统概览</h2><button className="button" onClick={()=>{live.reload();runtime.reload();readiness.reload();}}>刷新状态</button></div><ErrorNotice error={live.error||runtime.error}/><div className="card-grid">{live.data&&[['核心服务','core'],['业务数据库','database'],['索引服务','hindsight'],['记忆能力','memory']].map(([name,key])=><article className="data-card" key={key}><h3>{name}</h3><strong>{word(live.data?.[key])}</strong></article>)}<article className="data-card"><h3>后台执行</h3><strong>{worker?word(worker.status):'正在加载'}</strong>{worker&&<><p>在线 {num(worker.online)} · 执行槽 {num(worker.slots)} · 占用 {num(worker.busy)}</p><p className="muted">{worker.last_seen?`最近心跳 ${formatTime(num(worker.last_seen))}`:'尚未收到 worker 心跳'}</p></>}</article></div>{runtime.data&&<><div className="data-card"><h3>后台队列</h3><div className="tag-row">{Object.entries(runtime.data.queue).map(([state,count])=><span className="status-badge" key={state}>{word(state)} {count}</span>)}</div><p>最早到期等待 {(runtime.data.oldest_ready_seconds/60).toFixed(1)} 分钟</p><p className="muted">{text(worker?.note)} 页面每 15 秒更新运行状态，健康不代表真实模型质量已验收。</p></div><div className="data-card"><h3>存储容量</h3><p>{word(runtime.data.disk.status)} · 可用 {bytes(runtime.data.disk.free_bytes)} / 共 {bytes(runtime.data.disk.total_bytes)}</p><p className="muted">仅检查服务器运行卷所在文件系统，不代表数据库独立存储或备份健康。</p></div></>}<ReadinessEvidence data={readiness.data} error={readiness.error||readinessError}/><p className="muted">{text(live.data?.release_status||'正在检查服务状态')}</p></div>;
}

function ReadinessEvidence({data,error}:{data:Readiness|undefined;error:string}){
  const states:Record<string,string>={configured:'配置已验',observed:'记录有效',online:'协议在线',unconfigured:'尚未配置',disabled:'已经停用',attention:'需要核对',unknown:'状态未知'};
  return <section className="page-stack" aria-label="能力就绪"><h3>能力就绪</h3><ErrorNotice error={error}/>{error&&data&&<p role="status" className="error">更新失败，以下保留上次快照，不能视为当前状态。</p>}{!data&&!error&&<p role="status">正在读取就绪依据…</p>}{data&&<><p className="muted">快照时间：{formatTime(data.checked_at)}。每 15 秒更新本地证据；远端服务没有在此实时探测。</p><p className="muted">{data.note}</p><div aria-label="异常提示" className="page-stack">{data.alerts.length?data.alerts.map(row=><article className="data-card" key={row.code}><strong>{row.title}</strong><p>{row.message}</p><a href={row.href}>前往核对</a></article>):<p>本次检查范围内暂无异常提示，不代表全部能力已验收。</p>}</div><div className="card-grid">{data.groups.map(group=><article className="data-card" key={group.id}><div className="pane-toolbar"><h4>{group.name}</h4><a href={group.href}>查看详情</a></div><p className="muted">{group.note}</p>{group.items.length?<ul className="page-stack">{group.items.map(row=><li key={row.id}><strong className="plugin-digest">{row.name}</strong> <span className="status-badge">{states[row.state]}</span><p>{row.message}</p>{row.observed_at!=null&&<small>依据时间：{formatTime(row.observed_at)}</small>}</li>)}</ul>:<p>暂无当前身份可见的配置。</p>}<p className="muted">已列 {group.items.length} / 共 {group.total} 项。{group.truncated?'仅展示最近 20 项，其余请到详情核对。':''}</p></article>)}</div></>}</section>;
}

function MetadataPage({page}:{page:string}){
  const name=page==='traces'?'执行追踪':page==='audit'?'操作审计':'系统日志';
  const [filter,setFilter]=useState('all'),[cursor,setCursor]=useState(''),[stack,setStack]=useState<string[]>([]),[selected,setSelected]=useState<{kind:string;id:string}|null>(null);
  const endpoint=page==='traces'?'/api/admin/traces':page==='audit'?'/api/admin/audit':'/api/admin/logs';
  const suffix=`?cursor=${encodeURIComponent(cursor)}${page==='traces'?`&kind=${filter}`:page==='logs'?`&level=${filter}`:''}`;
  const rows=useResource<Metadata>(endpoint+suffix);
  return <div className="page-stack"><div className="pane-toolbar"><h2>{name}</h2><button className="button" onClick={rows.reload}>重新加载</button></div><p className="muted">{page==='traces'?'从对话、任务、子助手与插件调用回看实际执行记录。只展示阶段和证据，不展示私有思维。':page==='audit'?'查看当前所有者的关键操作、目标编号和时间。会话、账号密钥及业务正文不进入此列表。':'只显示启用本功能后的结构化运行事件，不采集容器原始输出。保留周期可在下方配置。'}</p>{page!=='audit'&&<label>筛选范围<select value={filter} onChange={e=>{setFilter(e.target.value);setCursor('');setStack([]);}}>{(page==='traces'?['all','chat','task','agent','plugin']:['all','warning','error']).map(f=><option key={f} value={f}>{f==='all'?'全部记录':word(f)}</option>)}</select></label>}<ErrorNotice error={rows.error}/>{!rows.data&&!rows.error&&<p role="status">正在读取记录…</p>}<div className="card-grid">{rows.data?.items.map(row=><article className="data-card profile-card" key={text(row.id)}><div className="pane-toolbar"><h3>{page==='traces'?word(row.kind):page==='audit'?(actions[text(row.action)]||text(row.action)):word(row.component)}</h3>{page!=='audit'&&<span className="status-badge">{word(page==='traces'?row.status:row.level)}</span>}</div><small>{formatTime(num(row.created_at))}</small>{page==='logs'?<><p>{text(row.message)}</p><small>事件 {text(row.code)} · 关联 {text(row.reference)}</small></>:<code className="plugin-digest">{text(page==='traces'?row.entity_id:row.target_id)}</code>}{page==='traces'&&<button className="button" onClick={()=>setSelected({kind:text(row.kind),id:text(row.entity_id)})}>查看追踪</button>}</article>)}</div>{rows.data?.items.length===0&&<div className="data-empty">暂无符合条件的记录。</div>}<div className="actions"><button className="button" disabled={!stack.length} onClick={()=>{setCursor(stack[stack.length-1]);setStack(stack.slice(0,-1));}}>上一页</button><button className="button" disabled={!rows.data?.next_cursor} onClick={()=>{setStack([...stack,cursor]);setCursor(rows.data!.next_cursor!);}}>下一页</button></div>{page==='logs'&&<OperationalPreferences/>}{selected&&<TraceDialog row={selected} close={()=>setSelected(null)}/>}</div>;
}

function TraceDialog({row,close}:{row:{kind:string;id:string};close():void}){
  const detail=useResource<Trace>(`/api/admin/traces/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.id)}`);
  return <Modal title="执行追踪" close={close}><ErrorNotice error={detail.error}/>{detail.error&&<button className="button" onClick={detail.reload}>重试加载</button>}{!detail.data&&!detail.error&&<p role="status">正在读取执行记录…</p>}{detail.data&&<><p>{word(detail.data.kind)} · {word(detail.data.status)}</p><code className="plugin-digest">{detail.data.id}</code><p className="muted">{detail.data.note}</p>{detail.data.graph&&<TraceRelations key={detail.data.graph.root} graph={detail.data.graph}/>}<h3>固定版本</h3><pre className="task-json">{JSON.stringify(detail.data.versions,null,2)}</pre><h3>阶段记录</h3>{detail.data.events.map(e=><div className="revision-record" key={text(e.seq)}>#{text(e.seq)} · {text(e.kind)} · {e.created_at?formatTime(num(e.created_at)):'未记录独立时刻'}</div>)}{!detail.data.events.length&&<p>暂无独立阶段记录。</p>}<h3>模型调用</h3>{detail.data.calls.map(c=><div className="data-card" key={text(c.id)}><strong>{word(c.status)} · {text(c.purpose)}</strong><p>{formatTime(num(c.created_at))} · {num(c.duration_ms)} 毫秒</p><code className="plugin-digest">{text(c.model_id)}</code><p>{c.known_tokens===null?'用量未知':`已知 token ${text(c.known_tokens)}`}</p><pre className="task-json">{JSON.stringify(c.versions,null,2)}</pre></div>)}{!detail.data.calls.length&&<p>暂无已关联的模型调用；不代表没有费用。</p>}{detail.data.truncated&&<p role="status">阶段或调用已达到 200 条展示上限，当前记录未完整列出。</p>}</>}</Modal>;
}

const nodeLabels:Record<string,string>={conversation:'所属会话',message:'会话消息',memory:'长期记忆',media:'引用附件',model_call:'模型调用',attempt:'执行尝试',agent_node:'协作节点',inbound:'渠道收信',delivery:'协议投递',channel_task:'渠道任务',reminder:'待办提醒',reminder_receipt:'提醒回执'};
const relationLabels:Record<string,string>={contains:'包含执行',result_message:'输出消息',context_message:'提供上下文',run_message:'记录消息',used_memory:'引用记忆',used_media:'引用附件',model_call:'调用模型',received_message:'触发对话',protocol_delivery:'协议投递',dispatched:'派发任务',reminder_receipt:'记录回执',attempted:'执行尝试',delegated:'分配节点',depends_on:'先于执行',tool_invocation:'调用插件',authorized_execution:'授权执行',requested:'请求协作'};

function TraceRelations({graph}:{graph:Graph}){
  const [selected,setSelected]=useState(graph.root);
  const nodes=new Map(graph.nodes.map((node,index)=>[node.key,{...node,number:index+1}]));
  const node=nodes.get(selected);
  const edges=graph.edges.filter(edge=>selected==='all'||edge.source===selected||edge.target===selected);
  const name=(key:string)=>{const value=nodes.get(key);return value?`#${value.number} ${nodeLabels[value.kind]||word(value.kind)}`:'记录缺失';};
  return <section className="page-stack" aria-label="执行关系"><h3>执行关系</h3><p className="muted">{graph.note}</p><label>查看节点<select value={selected} onChange={e=>setSelected(e.target.value)}><option value="all">全部关系</option>{graph.nodes.map(n=><option key={n.key} value={n.key}>{name(n.key)} · {n.id}</option>)}</select></label><p role="status">已列 {graph.nodes.length} 个节点、{graph.edges.length} 条关系；当前显示 {edges.length} 条。</p>{node&&<article className="data-card"><h4>{name(node.key)}</h4><code className="plugin-digest">{node.id}</code><p>当前状态：{word(node.status)} · {formatTime(node.created_at)}</p>{node.version!==null&&<p>当前版本：{node.version}</p>}{node.sequence!==null&&<p>尝试序号：{node.sequence}</p>}</article>}{edges.length?<ol className="page-stack">{edges.map(edge=><li key={`${edge.source}:${edge.target}:${edge.relation}:${edge.recorded_version??''}`}><div className="actions"><button type="button" className="button" onClick={()=>setSelected(edge.source)}>{name(edge.source)}</button><span>{relationLabels[edge.relation]||edge.relation} →</span><button type="button" className="button" onClick={()=>setSelected(edge.target)}>{name(edge.target)}</button></div>{edge.recorded_version!==null&&<p className="muted">当时引用版本：{edge.recorded_version}，不代表当前仍然有效。</p>}</li>)}</ol>:<p>暂无已记录的直接关联，未推测补全。</p>}{graph.truncated&&<p role="status" className="error">关联记录达到查询或展示上限，当前关系不完整。</p>}</section>;
}

function UsagePage(){
  const [days,setDays]=useState(7);
  const data=useResource<Usage>(`/api/admin/usage?days=${days}`);
  return <div className="page-stack"><div className="pane-toolbar"><h2>用量预算</h2><button className="button" onClick={data.reload}>刷新用量</button></div><label>统计范围<select value={days} onChange={e=>setDays(Number(e.target.value))}>{[1,7,30].map(d=><option key={d} value={d}>最近 {d} 天</option>)}</select></label><ErrorNotice error={data.error}/>{data.data&&<><div className="card-grid"><article className="data-card"><h3>模型调用</h3><strong>{data.data.calls}</strong><p>失败 {data.data.failed}</p></article><article className="data-card"><h3>已知用量</h3><strong>{data.data.known_tokens.toLocaleString()} token</strong><p>用量缺失 {data.data.unknown_usage_calls} 次</p></article><article className="data-card"><h3>调用费用</h3><strong><CostAmounts rows={data.data.currencies}/></strong><p>已知估算 · 费用未知 {data.data.unknown_cost_calls??data.data.calls} 次</p></article></div><p className="muted">{data.data.note}</p><div className="data-card"><h3>当日告警</h3><p>UTC 今日已知 {text(data.data.daily.known_tokens)} token · 阈值 {num(data.data.daily.alert_threshold)||'未设置'}</p>{!!data.data.daily.alert&&<p role="alert" className="error">已达到日用量告警阈值，请检查模型调用和任务。</p>}<p>当日有 {text(data.data.daily.unknown_usage_calls)} 次用量未知；低于阈值不能证明总量未超出。</p></div><div className="card-grid">{data.data.groups.map(g=><article className="data-card" key={text(g.model_id)+':'+text(g.purpose)}><h3>{text(g.purpose)}</h3><code className="plugin-digest">{text(g.model_id)}</code><p>调用 {text(g.calls)} · 失败 {text(g.failed)}</p><p>已知 {text(g.known_tokens)} token · 未知 {text(g.unknown_usage_calls)} 次</p><p>总耗时 {num(g.duration_ms)/1000} 秒 · 已知估算：<CostAmounts rows={g.currencies as Record<string,unknown>[]|undefined}/></p></article>)}</div><AudioUsage audio={data.data.audio}/><CostBreakdowns usage={data.data}/></>}<ModelBudgets/><ModelPrices onSaved={data.reload}/><AudioPrices onSaved={data.reload}/><OperationalPreferences/></div>;
}

function ModelBudgets(){
  const data=useResource<Budget>('/api/admin/model-budgets');
  const [daily,setDaily]=useState(0),[execution,setExecution]=useState(0),[version,setVersion]=useState(0);
  const [preview,setPreview]=useState<BudgetPreview|null>(null),[approved,setApproved]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  useEffect(()=>{if(data.data){setDaily(data.data.daily_limit);setExecution(data.data.execution_limit);setVersion(data.data.version);}},[data.data]);
  const valid=Number.isInteger(daily)&&daily>=0&&daily<=1000000&&Number.isInteger(execution)&&execution>=0&&execution<=10000;
  async function prepare(){
    if(busy||!valid)return;
    setBusy(true);setError('');setNotice('');
    try{setPreview(await api<BudgetPreview>('/api/admin/model-budgets/previews','POST',{version,daily_limit:daily,execution_limit:execution}));setApproved(false);}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function save(){
    if(busy||!preview||!approved)return;
    setBusy(true);setError('');
    try{await api<Budget>('/api/admin/model-budgets','PUT',{token:preview.token,acknowledgement:'request_attempts_not_money'});setPreview(null);setNotice('调用限额已保存，下次模型请求开始生效，已用次数不清空。');data.reload();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  const context=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{if(preview)context.current?.focus();},[preview]);
  const limit=(value:number)=>value?`${value.toLocaleString()} 次`:'不限制';
  return <section className="data-card page-stack" aria-label="调用限额"><div className="pane-toolbar"><h3>调用限额</h3><button className="button" disabled={busy} onClick={data.reload}>重新加载</button></div><ErrorNotice error={error||data.error}/>{notice&&<p role="status">{notice}</p>}{!data.data&&!data.error&&<p role="status">正在读取限额…</p>}{data.data&&<><p className="muted">{data.data.note}</p><p>UTC {data.data.utc_day}：已尝试 {data.data.today_attempts} 次，上限 {limit(data.data.daily_limit)}。尚未结算 {data.data.unsettled_attempts} 次；可能仍在执行或已中断，仍占用次数。</p><form className="form-stack" onSubmit={e=>{e.preventDefault();void prepare();}}><label>每日请求上限<input type="number" min={0} max={1000000} step={1} required disabled={busy} value={daily} onChange={e=>setDaily(Number(e.target.value))}/></label><label>单次执行上限<input type="number" min={0} max={10000} step={1} required disabled={busy} value={execution} onChange={e=>setExecution(Number(e.target.value))}/></label><p className="muted">0 表示不限制。单次执行覆盖同一对话回合、任务、协作或文字插件的模型请求，按执行编号累计，不按天重置；后台记忆与语音仅使用每日上限。目录查询不计数，模型验证计数。</p><button className="button" disabled={busy||!valid}>{busy?'正在处理':'预览更改'}</button></form><details><summary>近期执行</summary>{data.data.recent_executions.length?<div className="card-grid">{data.data.recent_executions.map(row=><article className="data-card" key={row.kind+row.id}><strong>{word(row.kind)} · {row.attempts} 次</strong><p><code className="plugin-digest">{row.id}</code></p><small>{formatTime(row.last_at)}</small></article>)}</div>:<p>暂无启用本版本后的执行记录；旧调用不补算。</p>}</details></>}{preview&&<Modal title="限额确认" close={()=>{setPreview(null);setError('');}} dismissible={!busy}><p ref={context} tabIndex={-1}>每日请求：{limit(preview.before.daily_limit)} → {limit(preview.after.daily_limit)}</p><p>单次执行：{limit(preview.before.execution_limit)} → {limit(preview.after.execution_limit)}</p><p>预览时 UTC 当日已用 {preview.today_attempts} 次；确认期间其他请求仍可能增加次数。到期：{formatTime(preview.expires_at)}。</p><p>{preview.note}</p><label className="checkbox"><input type="checkbox" disabled={busy} checked={approved} onChange={e=>setApproved(e.target.checked)}/>已了解这是请求尝试次数限制，不是 token、金额或实际账单封顶。</label><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>{setPreview(null);setError('');}}>保留配置</button><button className="button primary" disabled={busy||!approved} onClick={()=>void save()}>{busy?'正在保存':'保存限额'}</button></div></Modal>}</section>;
}

function OperationalPreferences(){
  const data=useResource<Preferences>('/api/admin/operations/preferences');
  const [draft,setDraft]=useState<Preferences>(),[confirm,setConfirm]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  useEffect(()=>{if(data.data)setDraft(data.data);},[data.data]);
  return <details className="data-card"><summary>运维配置</summary><ErrorNotice error={error||data.error}/>{notice&&<p role="status">{notice}</p>}{draft&&<form className="form-stack" onSubmit={e=>{e.preventDefault();setConfirm(true);}}><label>运行日志保留天数<input type="number" min={7} max={365} required value={draft.log_retention_days} onChange={e=>setDraft({...draft,log_retention_days:Number(e.target.value)})}/></label><label>UTC 日 token 告警阈值<input type="number" min={0} max={2000000000} required value={draft.daily_token_alert} onChange={e=>setDraft({...draft,daily_token_alert:Number(e.target.value)})}/></label><p className="muted">0 表示关闭用量告警。阈值只提示，不自动停用模型。日志清理仅影响结构化运行事件，审计、模型账目、任务与删除记录继续保留。</p><button className="button">预览更改</button></form>}{confirm&&draft&&<Modal title="运维配置" close={()=>setConfirm(false)} dismissible={!busy}><p>运行日志从 {data.data?.log_retention_days} 天调整为 {draft.log_retention_days} 天；后台每小时清理，超期运行事件清理后无法从本系统恢复。</p><p>UTC 日 token 告警：{draft.daily_token_alert||'关闭'}。未知用量不会伪装为零。</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>setConfirm(false)}>保留配置</button><button className="button primary" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api<Preferences>('/api/admin/operations/preferences','PUT',draft).then(v=>{setDraft(v);setConfirm(false);setNotice('运维配置已保存，从下一轮清理与统计生效。');data.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>保存更改</button></div></Modal>}</details>;
}

function DiagnosticsPage(){
  const history=useResource<Bundle[]>('/api/admin/diagnostics');
  const [days,setDays]=useState(7),[sections,setSections]=useState(['health','usage']),[preview,setPreview]=useState<Bundle|null>(null),[busy,setBusy]=useState(false),[approved,setApproved]=useState(false),[error,setError]=useState('');
  const request=useRef(crypto.randomUUID());
  function change(){request.current=crypto.randomUUID();setApproved(false);}
  async function downloadBundle(url:string){
    if(busy)return;
    setBusy(true);setError('');
    try{
      const blob=await api<Blob>(url,'GET',undefined,{response:'blob'});
      const object=URL.createObjectURL(blob),anchor=document.createElement('a');
      anchor.href=object;anchor.download='lingxi-diagnostics.json';document.body.append(anchor);anchor.click();anchor.remove();
      window.setTimeout(()=>URL.revokeObjectURL(object),1000);
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <div className="page-stack"><div className="pane-toolbar"><h2>诊断导出</h2><button className="button" onClick={history.reload}>重新加载</button></div><p className="muted">生成可核对的 JSON 元数据快照，编号使用摘要替换。默认不包含对话、记忆、附件、录音、服务地址或账号凭据；不会自动上传或发送给他人。预览有效 5 分钟，并绑定当前登录会话。</p><form className="form-stack" onSubmit={e=>{e.preventDefault();if(busy)return;setBusy(true);setError('');void api<Bundle>('/api/admin/diagnostics/preview','POST',{request_id:request.current,days,sections}).then(v=>{setPreview(v);setApproved(false);history.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}><label>时间范围<select value={days} disabled={busy} onChange={e=>{setDays(Number(e.target.value));change();}}>{[1,7,30].map(d=><option key={d} value={d}>最近 {d} 天</option>)}</select></label><fieldset disabled={busy}><legend>导出范围</legend><div className="choice-grid">{[['health','运行健康'],['usage','模型用量'],['logs','运行日志'],['audit','操作审计']].map(([key,name])=><label className="checkbox" key={key}><input type="checkbox" checked={sections.includes(key)} onChange={e=>{setSections(e.target.checked?[...sections,key]:sections.filter(v=>v!==key));change();}}/>{name}</label>)}</div></fieldset><ErrorNotice error={error}/><button className="button primary" disabled={busy||!sections.length}>{busy?'正在准备':'生成预览'}</button></form><h3>导出记录</h3><ErrorNotice error={history.error}/>{history.data?.map(row=><article className="data-card" key={row.id}><div className="pane-toolbar"><strong>{word(row.status)}</strong>{row.download_url&&<button className="button" disabled={busy} onClick={()=>void downloadBundle(row.download_url!)}>下载诊断</button>}</div><p>{formatTime(row.created_at)} · 到期 {formatTime(row.expires_at)}</p><small>{row.bytes} 字节 · 只保留最近 50 条展示</small></article>)}{history.data?.length===0&&<p>暂无诊断导出。</p>}{preview&&<Modal title="诊断预览" close={()=>{setPreview(null);change();setError('');}} dismissible={!busy}><p>请核对将导出的全部字段。快照 {preview.bytes} 字节 · 到期 {formatTime(preview.expires_at)}。</p><code className="plugin-digest">{preview.digest}</code><pre className="task-json ops-preview">{JSON.stringify(preview.preview,null,2)}</pre><label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已核对范围与脱敏预览，确认生成可下载文件。</label><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>{setPreview(null);change();}}>暂不导出</button><button className="button primary" disabled={busy||!approved} onClick={()=>{if(busy)return;setBusy(true);setError('');void api<Bundle>(`/api/admin/diagnostics/${preview.id}/confirm`,'POST',{digest:preview.digest}).then(()=>{setPreview(null);change();history.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>确认导出</button></div></Modal>}</div>;
}
