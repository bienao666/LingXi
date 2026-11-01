import {MediaPreview} from './MediaPages';
import {AgentRuns} from './AgentRuns';
import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {Task, TaskDetail, TaskPage, Reminder, ReminderPage, Skill, Conversation} from './api';
import {ErrorNotice, formatTime, Modal, useResource} from './ui';

const states:Record<string,string>={queued:'等待执行',running:'正在执行',succeeded:'执行成功',failed:'处理失败',cancelled:'已取消',scheduled:'等待到期',fired:'已存站内',acknowledged:'已确认',interrupted:'执行中断',cancel_requested:'请求取消'};
const label=(state:string)=>states[state]||state;
const skillNames:Record<string,string>={'project.plan':'项目规划','server.files':'资料清单','server.read':'文档读取','reminder.once':'待办提醒'};
const localZone=()=>Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
const localInput=(epoch:number)=>{const date=new Date(epoch*1000);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);};

function usePolling(reload:()=>void,active=true) {
  useEffect(()=>{if(!active)return;const timer=setInterval(()=>{if(document.visibilityState!=='hidden')reload();},4000);return ()=>clearInterval(timer);},[reload,active]);
}

function useRequestId() {
  const prior=useRef({body:'',id:''});
  return (body:unknown)=>{const value=JSON.stringify(body);if(prior.current.body!==value)prior.current={body:value,id:crypto.randomUUID()};return prior.current.id;};
}

function ConversationChoice({items,value,disabled=false}:{items:Conversation[];value?:string|null;disabled?:boolean}) {
  return <label>回传会话<select name="conversation_id" defaultValue={value||''} disabled={disabled}><option value="">仅保存在任务中心</option>{value&&!items.some(c=>c.id===value)&&<option value={value}>原会话（若已删除则仅站内保存）</option>}{items.map(c=><option value={c.id} key={c.id}>{c.title}</option>)}</select><small>可选写回已有 Web 会话；不发送 QQ、桌面通知或自动播音。</small></label>;
}

export function TaskPages({page,hash=''}:{page:string;hash?:string}) {
  const query=new URLSearchParams(hash.split('?')[1]||''),requested=query.get('mode')==='agents';
  const requestedRun=query.get('run')||'';
  const initialRun=/^[a-zA-Z0-9_-]{1,100}$/.test(requestedRun)?requestedRun:undefined;
  const [mode,setMode]=useState<'skills'|'agents'>(requested?'agents':'skills');
  useEffect(()=>{if(requested)setMode('agents');},[hash,requested]);
  return page==='reminders'?<ReminderList/>:<div className="page-stack"><div className="actions" aria-label="任务类型"><button className={'button'+(mode==='skills'?' primary':'')} aria-pressed={mode==='skills'} onClick={()=>setMode('skills')}>技能任务</button><button className={'button'+(mode==='agents'?' primary':'')} aria-pressed={mode==='agents'} onClick={()=>setMode('agents')}>协作任务</button></div>{mode==='agents'?<AgentRuns key={initialRun||"list"} initialRun={initialRun}/>:<TaskList/>}</div>;
}

export function SkillCatalog() {
  const result=useResource<Skill[]>('/api/skills');
  return <div className="page-stack"><div className="pane-toolbar"><h2>技能工具</h2><button className="button" onClick={result.reload}>重新加载</button></div><p className="muted">第一方能力与任务固定版本。文档读取须在插件商店确认安装，并在资源授权中单独允许正文目录。</p><ErrorNotice error={result.error}/>{!result.data&&!result.error&&<p role="status">正在加载能力…</p>}<div className="card-grid">{result.data?.map(skill=><article className="data-card profile-card" key={skill.id}><div className="pane-toolbar"><h3>{skill.name}</h3><span className="status-badge">{skill.available?'当前可用':'暂不可用'}</span></div><small>{skill.plugin_id} · v{skill.version}</small>{skill.reason&&<p className="error">{skill.reason}</p>}<p>{skill.output_description}</p><p className="muted">{skill.side_effects}</p><details><summary>输入约定</summary><pre className="task-json">{JSON.stringify(skill.input_schema,null,2)}</pre></details><div className="actions"><a className="button" href={skill.id==='reminder.once'?'#/tasks/reminders':'#/tasks/list'}>{skill.id==='reminder.once'?'设置提醒':'前往任务'}</a></div></article>)}</div></div>;
}

function TaskList() {
  const [query,setQuery]=useState(''),[search,setSearch]=useState(''),[status,setStatus]=useState(''),[cursor,setCursor]=useState('');
  const params=new URLSearchParams({q:search,...(status?{status}:{}),...(cursor?{cursor}:{})});
  const result=useResource<TaskPage>('/api/tasks?'+params);
  const [create,setCreate]=useState(false),[selected,setSelected]=useState<string|null>(null);
  usePolling(result.reload,!create&&!selected);
  return <div className="page-stack"><div className="pane-toolbar"><h2>任务列表</h2><div className="actions"><button className="button" onClick={result.reload}>重新加载</button><button className="button primary" onClick={()=>setCreate(true)}>新建任务</button></div></div>
    <form className="filter-row" onSubmit={e=>{e.preventDefault();setCursor('');setSearch(query);}}><input aria-label="搜索任务" placeholder="搜索任务名称" value={query} onChange={e=>setQuery(e.target.value)}/><select aria-label="任务状态" value={status} onChange={e=>{setCursor('');setStatus(e.target.value);}}><option value="">全部状态</option>{['queued','running','succeeded','failed','cancelled'].map(s=><option value={s} key={s}>{label(s)}</option>)}</select><button className="button">搜索任务</button></form>
    <p className="muted">任务在服务器执行，关闭浏览器后继续。当前支持生成计划、只读资料清单与单独授权的文档快照，计划中的行动不会自动执行。</p><ErrorNotice error={result.error}/>
    {!result.data&&!result.error&&<p role="status">正在加载任务…</p>}{result.data?.items.length===0&&<div className="data-empty">还没有符合条件的任务。</div>}
    <div className="card-grid">{result.data?.items.map(task=><article className="data-card profile-card" key={task.id}><div className="pane-toolbar"><h3>{task.title}</h3><span className={`status-badge status-${task.status}`}>{task.cancel_requested&&task.status==='running'?'等待取消':label(task.status)}</span></div><p className="muted">{skillNames[task.skill_id]||task.skill_id} · v{task.skill_version}</p><small>{formatTime(task.created_at)}</small>{task.error&&<p className="error">{task.error}</p>}<div className="actions"><button className="button" onClick={()=>setSelected(task.id)}>查看任务</button></div></article>)}</div>
    <Pagination cursor={cursor} next={result.data?.next_cursor} change={setCursor}/>
    {create&&<NewTask close={()=>setCreate(false)} done={identity=>{setCreate(false);setSelected(identity);setCursor('');result.reload();}}/>}
    {selected&&<TaskInspector identity={selected} close={()=>{setSelected(null);result.reload();}}/>}
  </div>;
}

function Pagination({cursor,next,change}:{cursor:string;next?:string|null;change(value:string):void}) {
  return <div className="actions">{cursor&&<button className="button" onClick={()=>change('')}>返回首页</button>}{next&&<button className="button" onClick={()=>change(next)}>查看下页</button>}</div>;
}

function NewTask({close,done}:{close():void;done(identity:string):void}) {
  const skills=useResource<Skill[]>('/api/skills'),conversations=useResource<Conversation[]>('/api/conversations');
  const [skill,setSkill]=useState('project.plan'),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const requestId=useRequestId(),selected=skills.data?.find(s=>s.id===skill);
  return <Modal title="新建任务" close={close}><form className="form-stack" onSubmit={e=>{
    e.preventDefault();if(busy||!selected?.available)return;const form=new FormData(e.currentTarget);
    const body={title:String(form.get('title')),skill_id:skill,input:skill==='project.plan'?{goal:String(form.get('goal'))}:{path:String(form.get('path')||'.')},conversation_id:form.get('conversation_id')||null};
    setBusy(true);setError('');void api<Task>('/api/tasks','POST',{...body,request_id:requestId(body)}).then(task=>done(task.id)).catch(e=>setError(e.message)).finally(()=>setBusy(false));
  }}><label>任务名称<input name="title" required maxLength={200} disabled={busy}/></label><label>使用能力<select value={skill} disabled={busy||!skills.data} onChange={e=>setSkill(e.target.value)}>{skills.data?.filter(s=>s.id!=='reminder.once').map(s=><option value={s.id} key={s.id}>{s.name}{s.available?'':'（暂不可用）'}</option>)}</select></label>
    {selected&&<p className="muted">{selected.available?selected.side_effects:selected.reason}。{selected.output_description}</p>}
    {skill==='project.plan'?<label>规划目标<textarea name="goal" rows={5} required maxLength={6000} disabled={busy} placeholder="描述目标、已知条件和希望交付的结果。"/></label>:<label>{skill==='server.read'?'文档路径':'资料目录'}<input key={skill} name="path" defaultValue={skill==='server.read'?'':'.'} placeholder={skill==='server.read'?'projects/docs/readme.md':'projects'} maxLength={400} required disabled={busy}/><small>相对服务器资料卷的路径，须在“资源授权”允许范围内，使用 / 分隔；不接受电脑路径。{skill==='server.read'?'只读单个普通文档并生成快照；不修改原件或自动发给模型。':'目录清单不读取文件正文。'}</small></label>}
    <ConversationChoice items={conversations.data||[]} disabled={busy}/><ErrorNotice error={error||skills.error||conversations.error}/><div className="actions"><button type="button" className="button" onClick={close} disabled={busy}>取消创建</button><button className="button primary" disabled={busy||!selected?.available}>{busy?'正在提交':'提交任务'}</button></div>
    {!selected?.available&&['server.files','server.read'].includes(skill)&&<a href="#/settings/resources" onClick={close}>前往资源授权</a>}
    {skill==='server.read'&&<a href="#/plugins/catalog" onClick={close}>前往插件商店</a>}
    {!selected?.available&&skill==='project.plan'&&<a href="#/models/policies" onClick={close}>前往配置对话模型</a>}
  </form></Modal>;
}

function TaskInspector({identity,close}:{identity:string;close():void}) {
  const detail=useResource<TaskDetail>('/api/tasks/'+identity),task=detail.data;
  const [confirm,setConfirm]=useState<'cancel'|'retry'|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  usePolling(detail.reload,!confirm&&!busy&&(!task||['queued','running'].includes(task.status)));
  const result=task?.result||{};
  const [mediaOpen,setMediaOpen]=useState(false);
  return <Modal title="任务详情" close={close}><ErrorNotice error={detail.error||error}/>{!task&&!detail.error&&<p role="status">正在加载任务详情…</p>}{task&&<div className="page-stack"><div className="pane-toolbar"><h3>{task.title}</h3><span className="status-badge">{task.cancel_requested&&task.status==='running'?'等待取消':label(task.status)}</span></div><small>{skillNames[task.skill_id]} · v{task.skill_version} · 创建于 {formatTime(task.created_at)}</small><ErrorNotice error={task.error}/>
    {typeof result.note==='string'&&<p className="muted">{result.note}</p>}{typeof result.summary==='string'&&<p>{result.summary}</p>}
    {Array.isArray(result.steps)&&<ol className="task-plan">{result.steps.map((item:Record<string,unknown>,i:number)=><li key={i}><strong>{String(item.title||'')}</strong><p>{String(item.action||'')}</p><small>完成条件：{String(item.completion||'')}</small></li>)}</ol>}
    {Array.isArray(result.assumptions)&&result.assumptions.length>0&&<div><h3>待确认条件</h3><ul>{result.assumptions.map((item:unknown,i:number)=><li key={i}>{String(item)}</li>)}</ul></div>}
    {Array.isArray(result.entries)&&<div className="table-scroll"><table><caption>目录 {String(result.path)}{result.truncated?' · 尚未完整列出':''}</caption><thead><tr><th>名称</th><th>类型</th><th>大小</th></tr></thead><tbody>{result.entries.map((entry:Record<string,unknown>,i:number)=><tr key={i}><td>{String(entry.name)}</td><td>{entry.kind==='directory'?'目录':'文件'}</td><td>{entry.size===null?'—':`${Number(entry.size).toLocaleString()} B`}</td></tr>)}</tbody></table>{result.entries.length===0&&<p className="muted">当前目录为空。</p>}</div>}
    {typeof result.media_id==='string'&&<section className="data-card form-stack"><h3>文档快照</h3><p className="wrap">服务器来源：{String(result.path)}</p><p className="muted">读取于 {formatTime(Number(result.observed_at))}。任务成功表示快照已保存，正文解析可能仍在处理；请查看文件状态。</p><div className="actions"><button className="button" onClick={()=>setMediaOpen(true)}>预览文件</button><a className="button" href="#/data/files" onClick={close}>文件管理</a></div>{mediaOpen&&<MediaPreview identity={result.media_id} close={()=>setMediaOpen(false)}/>}</section>}
    <details><summary>执行记录 · {task.attempts.length} 次</summary>{task.attempts.map(a=><div className="revision-record" key={a.number}><strong>第 {a.number} 次 · {label(a.status)}</strong><p>{formatTime(a.created_at)}{a.finished_at?` → ${formatTime(a.finished_at)}`:''}</p>{a.error&&<p className="error">{a.error}</p>}<small>{a.receipt.external_writes===false?'无外部写入副作用':''}</small></div>)}{task.attempts.length===0&&<p>尚未开始执行。</p>}</details>
    <details><summary>状态记录 · {task.events.length} 项</summary>{task.events.map(event=><p key={event.seq}>#{event.seq} · {label(event.kind)} · {formatTime(event.created_at)}</p>)}</details>
    <details><summary>配置与输入</summary><pre className="task-json">{JSON.stringify({input:task.input,config:task.config},null,2)}</pre></details>
    {task.result_message_id&&task.conversation_id&&<a href={`#/chat/conversation?c=${task.conversation_id}`} onClick={close}>查看回传会话</a>}
    <div className="actions"><button className="button" onClick={detail.reload}>重新加载</button>{['queued','running'].includes(task.status)&&<button className="button" disabled={busy||task.cancel_requested} onClick={()=>setConfirm('cancel')}>取消任务</button>}{task.status==='failed'&&<button className="button" disabled={busy} onClick={()=>setConfirm('retry')}>重试任务</button>}</div>
    {confirm&&<Modal title={confirm==='cancel'?'取消任务':'重试任务'} close={()=>setConfirm(null)}><p>{confirm==='cancel'?`取消“${task.title}”后停止后续执行；已产生的模型费用不会撤销，等待执行器确认。`:'仅重试无外部写入副作用的本任务，保留固定配置；模型调用可能产生费用。'}</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>setConfirm(null)}>返回详情</button><button className="button primary" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api('/api/tasks/'+identity+'/'+confirm,'POST',{version:task.version}).then(()=>{setConfirm(null);detail.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在提交':confirm==='cancel'?'确认取消':'确认重试'}</button></div></Modal>}
  </div>}</Modal>;
}

function ReminderList() {
  const [query,setQuery]=useState(''),[search,setSearch]=useState(''),[status,setStatus]=useState(''),[cursor,setCursor]=useState('');
  const params=new URLSearchParams({q:search,...(status?{status}:{}),...(cursor?{cursor}:{})});
  const result=useResource<ReminderPage>('/api/reminders?'+params),conversations=useResource<Conversation[]>('/api/conversations');
  const [editing,setEditing]=useState<Reminder|'new'|null>(null),[cancel,setCancel]=useState<Reminder|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  usePolling(result.reload,!editing&&!cancel&&!busy);
  return <div className="page-stack"><div className="pane-toolbar"><h2>待办提醒</h2><div className="actions"><button className="button" onClick={result.reload}>重新加载</button><button className="button primary" onClick={()=>setEditing('new')}>新建提醒</button></div></div>
    <form className="filter-row" onSubmit={e=>{e.preventDefault();setCursor('');setSearch(query);}}><input aria-label="搜索提醒" placeholder="搜索提醒名称" value={query} onChange={e=>setQuery(e.target.value)}/><select aria-label="提醒状态" value={status} onChange={e=>{setCursor('');setStatus(e.target.value);}}><option value="">全部状态</option>{['scheduled','fired','acknowledged','cancelled','failed'].map(s=><option value={s} key={s}>{label(s)}</option>)}</select><button className="button">搜索提醒</button></form>
    <p className="muted">一次性提醒由服务器保存并触发，关闭浏览器后继续。到期存入站内，可回传已选择的 Web 会话；不代表推送送达或已读。</p><ErrorNotice error={error||result.error||conversations.error}/>{message&&<p role="status">{message}</p>}
    {!result.data&&!result.error&&<p role="status">正在加载提醒…</p>}{result.data?.items.length===0&&<div className="data-empty">还没有符合条件的提醒。</div>}
    <div className="card-grid">{result.data?.items.map(row=><article className="data-card profile-card" key={row.id}><div className="pane-toolbar"><h3>{row.title}</h3><span className={`status-badge status-${row.status}`}>{label(row.status)}</span></div><p className="memory-content">{row.content||'未填写补充内容'}</p><p>{new Date(row.due_at*1000).toLocaleString('zh-CN',{timeZone:row.timezone})} · {row.timezone}</p>{row.receipt&&<small>实际存入：{formatTime(Number(row.receipt.stored_at))}{row.receipt.acknowledged_at?` · 确认于 ${formatTime(Number(row.receipt.acknowledged_at))}`:' · 尚未确认'}</small>}<ErrorNotice error={row.error}/><div className="actions">{['scheduled','failed'].includes(row.status)&&<button className="button" disabled={busy} onClick={()=>setEditing(row)}>{row.status==='failed'?'重新安排':'编辑提醒'}</button>}{row.status==='scheduled'&&<button className="button" disabled={busy} onClick={()=>{setError('');setCancel(row);}}>取消提醒</button>}{row.status==='fired'&&<button className="button" disabled={busy} onClick={()=>{setBusy(true);setError('');setMessage('');void api('/api/reminders/'+row.id+'/acknowledge','POST',{version:row.version}).then(()=>{result.reload();setMessage('提醒已标记为确认。');}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>标记确认</button>}{Boolean(row.receipt?.message_id)&&row.conversation_id&&<a className="button" href={`#/chat/conversation?c=${row.conversation_id}`}>查看会话</a>}</div></article>)}</div>
    <Pagination cursor={cursor} next={result.data?.next_cursor} change={setCursor}/>
    {editing&&<ReminderEditor row={editing==='new'?undefined:editing} conversations={conversations.data||[]} close={()=>setEditing(null)} done={()=>{setEditing(null);setCursor('');result.reload();setMessage('提醒已保存。');}}/>}
    {cancel&&<Modal title="取消提醒" close={()=>setCancel(null)}><p>取消“{cancel.title}”后不再触发这次提醒。已经写入站内的提醒不能撤回。</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>setCancel(null)}>保留提醒</button><button className="button primary" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api('/api/reminders/'+cancel.id+'/cancel','POST',{version:cancel.version}).then(()=>{setCancel(null);result.reload();setMessage('提醒已取消。');}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在取消':'取消提醒'}</button></div></Modal>}
  </div>;
}

function ReminderEditor({row,conversations,close,done}:{row?:Reminder;conversations:Conversation[];close():void;done():void}) {
  const [time,setTime]=useState(()=>localInput(row?.due_at||Date.now()/1000+600)),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const requestId=useRequestId();
  return <Modal title={row?'编辑提醒':'新建提醒'} close={close}><form className="form-stack" onSubmit={e=>{
    e.preventDefault();if(busy)return;const form=new FormData(e.currentTarget),chosen=String(form.get('due_local')||''),due=new Date(chosen).getTime()/1000;
    if(!Number.isFinite(due)||localInput(due)!==chosen){setError('提醒时间无效或处于夏令时跳转，请重新选择时间。');return;}
    const body={title:String(form.get('title')),content:String(form.get('content')||''),due_at:due,timezone:localZone(),conversation_id:form.get('conversation_id')||null};
    setBusy(true);setError('');void api(row?'/api/reminders/'+row.id:'/api/reminders',row?'PUT':'POST',row?{...body,version:row.version}:{...body,request_id:requestId(body)}).then(done).catch(e=>setError(e.message)).finally(()=>setBusy(false));
  }}><label>提醒名称<input name="title" required maxLength={200} defaultValue={row?.title} disabled={busy}/></label><label>补充内容<textarea name="content" rows={3} maxLength={4000} defaultValue={row?.content} disabled={busy}/></label><label>提醒时间<input name="due_local" type="datetime-local" required value={time} onInput={e=>setTime(e.currentTarget.value)} onChange={e=>setTime(e.target.value)} disabled={busy}/><small>按浏览器时区 {localZone()} 输入，保存绝对时间；支持未来 366 天内的一次性提醒。</small></label><ConversationChoice items={conversations} value={row?.conversation_id} disabled={busy}/><ErrorNotice error={error}/><div className="actions"><button className="button" type="button" disabled={busy} onClick={close}>取消编辑</button><button className="button primary" disabled={busy}>{busy?'正在保存':'保存提醒'}</button></div></form></Modal>;
}
