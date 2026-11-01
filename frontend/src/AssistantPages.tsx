import React, {useEffect, useRef, useState} from 'react';
import {api, supportCommands} from './api';
import type {AssistantProfile, ProfileRevision, SupportPreferences, EmotionObservation, Model, Provider} from './api';
import {ErrorNotice, formatTime, Modal, useResource} from './ui';
import {PluginAgents} from './PluginAgents';

const titles={personas:'人格管理',roles:'执行角色',agents:'助手配置'};
type Page=keyof typeof titles;
const paths={personas:'/api/personas',roles:'/api/agent-roles',agents:'/api/agent-profiles'};
const categoryNames:Record<string,string>={profile:'核心档案',preferences:'稳定偏好',experiences:'共同经历',projects:'项目目标',relationships:'人物关系',procedures:'操作经验',support:'支持偏好'};
const stringValue=(spec:Record<string,unknown>,key:string)=>typeof spec[key]==='string'?spec[key] as string:'';
const listValue=(spec:Record<string,unknown>,key:string)=>Array.isArray(spec[key])?spec[key] as string[]:[];

export function AssistantPages({page}:{page:string}) {
  if(page==='support')return <SupportPage/>;
  return <div className="page-stack"><ProfilePage key={page} page={page as Page}/>{page==='agents'&&<PluginAgents/>}</div>;
}

function ProfilePage({page}:{page:Page}) {
  const list=useResource<AssistantProfile[]>(paths[page]);
  const personas=useResource<AssistantProfile[]>('/api/personas'),roles=useResource<AssistantProfile[]>('/api/agent-roles');
  const models=useResource<Model[]>('/api/models'),providers=useResource<Provider[]>('/api/providers');
  const [editing,setEditing]=useState<AssistantProfile|'new'|null>(null),[history,setHistory]=useState<AssistantProfile|null>(null);
  const [confirm,setConfirm]=useState<AssistantProfile|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  async function act(action:()=>Promise<unknown>,success:string) {
    if(busy)return;setBusy(true);setError('');setMessage('');
    try{await action();list.reload();personas.reload();roles.reload();setMessage(success);}catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  const modelOptions=(models.data||[]).filter(m=>m.purpose==='chat'&&m.status==='verified'&&providers.data?.some(p=>p.id===m.provider_id&&p.enabled));
  return <div className="page-stack"><div className="pane-toolbar"><h2>{titles[page]}</h2><div className="actions"><button className="button" disabled={busy} onClick={list.reload}>重新加载</button><button className="button primary" onClick={()=>{setError('');setEditing('new');}}>新建配置</button></div></div>
    <ErrorNotice error={error||list.error||personas.error||roles.error||models.error||providers.error}/>{message&&<p role="status">{message}</p>}
    {page==='personas'&&<p className="muted">人格调整表达方式；在对话中显式选择后生效。修改用于新回合，旧回合保留原版本。</p>}
    {page==='roles'&&<p className="muted">角色声明专长、相关记忆类别与候选工具；实际授权由执行服务检查。支持 reason 文字分析、server.files 只读清单、plugin.call 远程工具候选；工具还需在任务内选择并逐次核对确认。</p>}
    {page==='agents'&&<p className="muted">组合人格、角色和模型偏好，可由任务中心的协作任务自动选用。派发时固定版本，停用后阻止后续执行。</p>}
    {!list.data&&!list.error&&<p role="status" className="muted">正在加载配置…</p>}
    {list.data?.length===0&&<div className="data-empty">{page==='agents'?'尚无助手配置（本地组合）。也可在下方启用插件角色。':'尚无助手配置，先选择人格和角色再创建。'}</div>}
    <div className="card-grid">{list.data?.map(item=><article className="data-card profile-card" key={item.id}>
      <div className="pane-toolbar"><h3>{item.name}</h3><span className="status-badge">{item.enabled?'已启用':'已停用'} · v{item.version}</span></div>
      <p>{item.description||'尚未填写说明'}</p>
      {page==='personas'&&<><p className="muted">{stringValue(item.spec,'style')}</p><small>{stringValue(item.spec,'language')} {stringValue(item.spec,'voice')&&`· 默认音色 ${stringValue(item.spec,'voice')}（需在语音配置中验证并开启联动）`}</small></>}
      {page==='roles'&&<><p className="muted">{stringValue(item.spec,'expertise')}</p><div className="tag-row">{listValue(item.spec,'context_categories').map(c=><span className="status-badge" key={c}>{categoryNames[c]||c}</span>)}</div><small>完成条件：{stringValue(item.spec,'completion')||'尚未设置'}</small></>}
      {page==='agents'&&<p className="muted">{personas.data?.find(p=>p.id===item.spec.persona_id)?.name||'人格不可用'} / {roles.data?.find(p=>p.id===item.spec.role_id)?.name||'角色不可用'}</p>}
      {item.issues.length>0&&<p className="muted">{item.issues.join('；')}</p>}
      <div className="actions"><button className="button" disabled={busy} onClick={()=>{setError('');setEditing(item);}}>编辑配置</button><button className="button" disabled={busy} onClick={()=>void act(()=>api(`${paths[page]}/${item.id}/copy`,'POST'),'已创建停用的副本，可编辑后启用。')}>复制配置</button><button className="button" onClick={()=>setHistory(item)}>版本记录</button><button className="button" disabled={busy} onClick={()=>{setError('');setConfirm(item);}}>{item.enabled?'停用配置':'启用配置'}</button></div>
    </article>)}</div>
    {editing&&<ProfileEditor key={editing==='new'?'new':editing.id} page={page} item={editing==='new'?undefined:editing} personas={personas.data||[]} roles={roles.data||[]} models={modelOptions} close={()=>setEditing(null)} done={()=>{setEditing(null);list.reload();}}/>}
    {history&&<ProfileHistory page={page} item={history} close={()=>setHistory(null)}/>}
    {confirm&&<Modal title={confirm.enabled?'停用配置':'启用配置'} close={()=>setConfirm(null)}><p>{confirm.enabled?`停用“${confirm.name}”后不能用于新回合或新的助手配置；已经提交的回合保留原版本。`:`启用“${confirm.name}”前将重新检查关联配置和模型。`}</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={()=>setConfirm(null)}>返回编辑</button><button className="button primary" disabled={busy} onClick={()=>void act(async()=>{await api(`${paths[page]}/${confirm.id}`,'PUT',{...confirm.spec,name:confirm.name,description:confirm.description,version:confirm.version,enabled:!confirm.enabled});setConfirm(null);},'配置状态已更新。')}>{busy?'正在保存':confirm.enabled?'停用配置':'启用配置'}</button></div></Modal>}
  </div>;
}

function ProfileEditor({page,item,personas,roles,models,close,done}:{page:Page;item?:AssistantProfile;personas:AssistantProfile[];roles:AssistantProfile[];models:Model[];close():void;done():void}) {
  const spec=item?.spec||{},[busy,setBusy]=useState(false),[error,setError]=useState('');
  const active=(items:AssistantProfile[],selected:unknown)=>items.filter(p=>p.enabled||p.id===selected);
  return <Modal title={item?'编辑配置':'新建配置'} close={close}><form className="form-stack" onSubmit={e=>{
    e.preventDefault();if(busy)return;const form=new FormData(e.currentTarget),text=(key:string)=>String(form.get(key)||'');
    const body:Record<string,unknown>={name:text('name'),description:text('description'),enabled:item?.enabled??true,version:item?.version??0};
    if(page==='personas')Object.assign(body,{style:text('style'),language:text('language'),instructions:text('instructions'),examples:text('examples').split('\n').filter(Boolean),voice:text('voice'),model_id:text('model_id')||null});
    if(page==='roles')Object.assign(body,{expertise:text('expertise'),instructions:text('instructions'),tools:text('tools').split(',').map(x=>x.trim()).filter(Boolean),context_categories:form.getAll('context_categories'),completion:text('completion')});
    if(page==='agents')Object.assign(body,{persona_id:text('persona_id'),role_id:text('role_id'),model_id:text('model_id')||null});
    setBusy(true);setError('');void api(item?`${paths[page]}/${item.id}`:paths[page],item?'PUT':'POST',body).then(done).catch(e=>setError(e.message)).finally(()=>setBusy(false));
  }}><label>配置名称<input name="name" required maxLength={100} defaultValue={item?.name||''} disabled={busy}/></label>
    <label>配置说明<textarea name="description" maxLength={500} rows={2} defaultValue={item?.description||''} disabled={busy}/></label>
    {page==='personas'&&<><label>表达风格<textarea name="style" maxLength={1000} rows={2} defaultValue={stringValue(spec,'style')||'自然、坦诚、简洁'} disabled={busy}/></label><label>默认语言<input name="language" required maxLength={100} defaultValue={stringValue(spec,'language')||'简体中文'} disabled={busy}/></label><label>对话示例<textarea name="examples" rows={3} defaultValue={listValue(spec,'examples').join('\n')} placeholder="每行一条，最多 8 条，每条 1000 字" disabled={busy}/></label><label>默认音色<input name="voice" maxLength={80} pattern="[a-zA-Z0-9_-]+" defaultValue={stringValue(spec,'voice')} disabled={busy}/><small>留空沿用系统音色；填写供应商支持的标识后，前往语音配置验证并开启人格联动。</small></label></>}
    {page==='roles'&&<><label>角色专长<textarea name="expertise" maxLength={1000} defaultValue={stringValue(spec,'expertise')} disabled={busy}/></label><label>候选工具<input name="tools" defaultValue={listValue(spec,'tools').join(', ')} placeholder="工具标识，英文逗号分隔" disabled={busy}/><small>声明候选范围，不授予权限；可填 reason、server.files、plugin.call；任务授权和逐次确认仍独立检查。</small></label><fieldset disabled={busy}><legend>相关记忆类别</legend><p className="muted">仅声明所需类别，调度时仍按任务及服务端授权过滤。</p><div className="choice-grid">{Object.entries(categoryNames).map(([key,name])=><label className="checkbox" key={key}><input name="context_categories" type="checkbox" value={key} defaultChecked={listValue(spec,'context_categories').includes(key)}/>{name}</label>)}</div></fieldset><label>完成条件<textarea name="completion" maxLength={1000} defaultValue={stringValue(spec,'completion')} disabled={busy}/></label></>}
    {page!=='agents'&&<label>{page==='personas'?'表达指引':'执行指引'}<textarea name="instructions" rows={4} maxLength={4000} defaultValue={stringValue(spec,'instructions')} disabled={busy}/><small>不能扩大权限、改写记忆事实或覆盖用户明确偏好。</small></label>}
    {page==='agents'&&<><label>使用人格<select name="persona_id" required defaultValue={stringValue(spec,'persona_id')} disabled={busy}><option value="">请选择人格</option>{active(personas,spec.persona_id).map(p=><option key={p.id} value={p.id} disabled={!p.enabled}>{p.name}{p.enabled?'':'（已停用）'}</option>)}</select></label><label>执行角色<select name="role_id" required defaultValue={stringValue(spec,'role_id')} disabled={busy}><option value="">请选择角色</option>{active(roles,spec.role_id).map(p=><option key={p.id} value={p.id} disabled={!p.enabled}>{p.name}{p.enabled?'':'（已停用）'}</option>)}</select></label></>}
    {page!=='roles'&&<label>偏好模型<select name="model_id" defaultValue={stringValue(spec,'model_id')} disabled={busy}><option value="">使用对话用途默认模型</option>{Boolean(spec.model_id)&&!models.some(m=>m.id===spec.model_id)&&<option value={stringValue(spec,'model_id')} disabled>原偏好模型不可用，请重新选择</option>}{models.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select><small>仅可选择验证通过且连接有效的日常对话模型。</small></label>}
    <ErrorNotice error={error}/><div className="actions"><button type="button" className="button" disabled={busy} onClick={close}>取消编辑</button><button className="button primary" disabled={busy}>{busy?'正在保存':'保存配置'}</button></div>
  </form></Modal>;
}

function ProfileHistory({page,item,close}:{page:Page;item:AssistantProfile;close():void}) {
  const [before,setBefore]=useState<number>(),[records,setRecords]=useState<ProfileRevision[]>([]);
  const result=useResource<ProfileRevision[]>(`${paths[page]}/${item.id}/revisions${before?`?before=${before}`:''}`);
  useEffect(()=>{if(result.data)setRecords(prior=>[...prior,...result.data!.filter(r=>!prior.some(p=>p.version===r.version))]);},[result.data]);
  return <Modal title="版本记录" close={close}><p>{item.name} · 当前 v{item.version}</p><ErrorNotice error={result.error}/>{records.map(r=><details key={r.version} className="revision-record"><summary>v{r.version} · {formatTime(r.created_at)} · {String(r.snapshot.name)}</summary><pre>{JSON.stringify(r.snapshot,null,2)}</pre></details>)}{!result.data&&!result.error&&<p role="status">正在加载版本…</p>}{result.data?.length===50&&<button className="button" onClick={()=>setBefore(records[records.length-1].version)}>加载更早版本</button>}</Modal>;
}

function SupportPage() {
  const result=useResource<SupportPreferences>('/api/support-preferences');
  const [draft,setDraft]=useState<SupportPreferences>(),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState(''),[disable,setDisable]=useState(false),[disableAuto,setDisableAuto]=useState(false),[renewReport,setRenewReport]=useState(false);
  useEffect(()=>{if(result.data){setDraft(result.data);setRenewReport(false);}},[result.data]);
  const change=(values:Partial<SupportPreferences>)=>{setDraft(d=>d?{...d,...values}:d);if('self_report' in values||values.emotion_enabled===false)setRenewReport(false);setMessage('');};
  return <div className="page-stack"><div className="pane-toolbar"><h2>支持偏好</h2><button className="button" disabled={busy} onClick={result.reload}>重新加载</button></div><ErrorNotice error={error||result.error}/>{message&&<p role="status">{message}</p>}
    {!draft&&!result.error&&<p role="status">正在加载偏好…</p>}
    {draft&&<form className="data-card form-stack" onSubmit={e=>{e.preventDefault();if(busy)return;setBusy(true);setError('');setMessage('');void api<SupportPreferences>('/api/support-preferences','PUT',{...draft,renew_report:renewReport}).then(saved=>{setDraft(saved);setRenewReport(false);setMessage('支持偏好已保存，相关在途生成和已有衍生回答会失效；后续生成使用新设置。');}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>
      <label>支持方式<select value={draft.mode} onChange={e=>change({mode:e.target.value as SupportPreferences['mode']})} disabled={busy}><option value="balanced">自然平衡</option><option value="listen">先听我说</option><option value="advice">给我建议</option><option value="action">直接协助</option></select></label>
      <label>交流偏好<textarea rows={3} maxLength={1000} value={draft.notes} disabled={busy} onChange={e=>change({notes:e.target.value})} placeholder="例如：先简短回应感受；只有我需要时再给建议。"/></label>
      <label className="checkbox"><input type="checkbox" checked={draft.emotion_enabled} disabled={busy} onChange={e=>{if(e.target.checked)change({emotion_enabled:true});else setDisable(true);}}/>允许根据我的自述适配表达</label>
      <p className="muted">你填写的感受优先，不将未回应或设备状态当作情绪。关闭并保存后，清除自述和自动线索，停止使用相关表达依据。</p>
      <label className="checkbox"><input type="checkbox" checked={draft.auto_observe??false} disabled={busy||!draft.emotion_enabled} onChange={e=>{if(e.target.checked)change({auto_observe:true});else setDisableAuto(true);}}/>开启自动情绪线索</label>
      <p className="muted">默认关闭。只分析下面明确选中的入口，保存后生效；使用已验证的记忆抽取模型，所有入口合计最多每 5 分钟一次，可能产生额外费用。仅在原会话用于后续表达，不跨会话传播、不进入长期记忆或子助手。引用、假设、他人和历史感受会保守跳过；推测可能出错。</p>
      <fieldset disabled={busy||!draft.emotion_enabled||!draft.auto_observe}><legend>分析入口</legend>
        {([['web_text','网页文字'],['confirmed_voice','确认转写'],['qq_private','绑定私聊']] as const).map(([key,name])=><label className="checkbox" key={key}><input type="checkbox" checked={(draft.observation_channels??['web_text']).includes(key)} onChange={e=>{const values=draft.observation_channels??['web_text'];change({observation_channels:e.target.checked?[...values,key]:values.filter(v=>v!==key)});}}/>{name}</label>)}
        <p className="muted">确认转写仅指你核对后点击发送的浏览器语音文字，不分析音色、自动转写或 QQ 语音。绑定私聊还要求 QQ 身份已确认且允许共享本人记忆；群聊、陌生人及隔离身份不参与。选择入口不会授予绑定或记忆权限。关闭某个入口并保存，会清除旧线索及关联回答。</p>
      </fieldset>
      <label>线索有效时长<select value={draft.observation_ttl_hours??1} disabled={busy||!draft.emotion_enabled||!draft.auto_observe} onChange={e=>change({observation_ttl_hours:Number(e.target.value)})}>{[1,2,3,4].map(v=><option key={v} value={v}>{v} 小时</option>)}</select><small>从源消息开始计时，纠正不会延长。到期清除线索，依据该线索生成的回答会失效；原始聊天另行管理。</small></label>
      <label>当前感受<textarea value={draft.self_report} maxLength={500} rows={2} disabled={busy||!draft.emotion_enabled} onChange={e=>change({self_report:e.target.value})} placeholder="可留空，例如：今天有些疲惫，希望回复简短一些。"/></label>
      <label>自述有效时长<select value={draft.report_ttl_hours} disabled={busy||!draft.emotion_enabled} onChange={e=>change({report_ttl_hours:Number(e.target.value)})}>{[1,2,4,8,12,24].map(v=><option key={v} value={v}>{v} 小时</option>)}</select><small>更换感受文字或明确重新确认后才重新计时。仅修改其他偏好不会延长，过期的旧表单也不会使感受重新生效。不写入长期记忆。</small></label>
      {draft.emotion_enabled&&draft.self_report.trim()&&<><label className="checkbox"><input type="checkbox" checked={renewReport} disabled={busy} onChange={e=>setRenewReport(e.target.checked)}/>重新确认当前感受</label><small>点击“保存偏好”后，按所选时长重新计时。</small></>}
      {draft.report_expires_at&&<p className="muted">当前自述有效至 {formatTime(draft.report_expires_at)}；可清空后保存撤回。</p>}
      <div className="actions"><button className="button primary" disabled={busy}>{busy?'正在保存':'保存偏好'}</button><small>配置 v{draft.version}</small></div>
    </form>}
    <details className="data-card"><summary>聊天设置</summary><p>在独立 Web 对话中单独发送以下完整指令，即可保存支持偏好。设置指令不调用模型、不抽取记忆。引用、分支编辑、附件、QQ 与语音不改变这些设置；其他表达仍作普通对话。开启自动分析请在本页明确勾选保存。</p><ul>{supportCommands.map(command=><li key={command}><code>{command}</code></li>)}</ul><p className="muted">“以后”会影响后续对话；临时想倾诉可以直接聊天。修改会使相关在途生成和已有衍生回答失效。直接协助仍需独立授权；联系频率在“联系偏好”中管理。</p></details>
    <ObservationList revision={draft?.version??0}/>
    {disableAuto&&<Modal title="关闭线索" close={()=>setDisableAuto(false)}><p>停止自动分析，清除旧线索，相关在途生成和已有衍生回答会失效；你手动填写的感受保留。点击“保存偏好”后生效。</p><div className="actions"><button className="button" onClick={()=>setDisableAuto(false)}>保持开启</button><button className="button primary" onClick={()=>{change({auto_observe:false});setDisableAuto(false);}}>关闭线索</button></div></Modal>}
    {disable&&<Modal title="关闭适配" close={()=>setDisable(false)}><p>关闭后将清空当前感受与自动线索，相关在途生成和已有衍生回答会失效。点击“保存偏好”后生效。</p><div className="actions"><button className="button" onClick={()=>setDisable(false)}>保持开启</button><button className="button primary" onClick={()=>{change({emotion_enabled:false,auto_observe:false,self_report:'',report_expires_at:null});setDisable(false);}}>关闭适配</button></div></Modal>}
  </div>;
}

const observationLabels:Record<string,string>={fatigue:'有些疲惫',stress:'感到压力',sadness:'有些难过',anger:'感到生气',joy:'心情愉快',uncertainty:'有些犹豫',calm:'比较平静'};
const observationStates:Record<string,string>={queued:'等待分析',running:'正在分析',ready:'短期有效',none:'未作判断',failed:'分析失败',rejected:'已撤回',deleted:'已删除',expired:'已经到期',invalidated:'来源失效'};
const evidenceLabels:Record<string,string>={reported:'原文自述',inferred:'系统推测',user_correction:'本人纠正'};
const observationSources:Record<string,string>={web_text:'网页文字',confirmed_voice:'确认转写',qq_private:'绑定私聊'};

function ObservationList({revision}:{revision:number}) {
  const items=useResource<EmotionObservation[]>('/api/emotion-observations');
  useEffect(()=>{if(revision>0)items.reload();},[revision,items.reload]);
  const [selected,setSelected]=useState<EmotionObservation|null>(null),[action,setAction]=useState<'correct'|'reject'|'delete'>('correct');
  const [label,setLabel]=useState('calm'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [confirmed,setConfirmed]=useState<Record<string,EmotionObservation>>({}),[focusList,setFocusList]=useState(false);
  const reloadButton=useRef<HTMLButtonElement>(null);
  useEffect(()=>{if(focusList&&!selected&&!busy){reloadButton.current?.focus();setFocusList(false);}},[focusList,selected,busy]);
  const open=(row:EmotionObservation,operation:typeof action)=>{setSelected(row);setAction(operation);setLabel(row.label||'calm');setError('');setMessage('');};
  return <section className="page-stack" aria-label="短期线索"><div className="pane-toolbar"><h2>短期线索</h2><button ref={reloadButton} className="button" disabled={busy} onClick={items.reload}>加载线索</button></div>
    <p className="muted">显示最近 50 条分析记录。原文自述、系统推测与本人纠正分别标记，不代表诊断。修改支持偏好后，旧线索和相关回答会失效。</p>
    <ErrorNotice error={items.error||(!selected?error:'')}/>{message&&<p role="status">{message}</p>}
    {!items.data&&!items.error&&<p role="status">正在加载线索…</p>}
    {items.data?.length===0&&<div className="data-empty">暂无线索，自动分析默认关闭。</div>}
    <div className="card-grid">{!items.error&&Array.isArray(items.data)&&items.data.map(item=>{const saved=confirmed[item.id];return saved&&saved.version>=item.version?saved:item;}).map(row=><article className="data-card" key={row.id}>
      <div className="pane-toolbar"><h3>{observationLabels[row.label]||'分析记录'}</h3><span className="status-badge">{observationStates[row.status]||'状态未知'}</span></div>
      {row.evidence&&<p>{evidenceLabels[row.evidence]||'未经确认'}{row.evidence==='inferred'?' · 可能判断有误':''}</p>}
      {row.source_kind&&<p className="muted">来源入口：{observationSources[row.source_kind]||'入口未知'} · 仅用于原会话</p>}
      {row.quote&&<blockquote className="emotion-evidence">{row.quote}</blockquote>}
      <p className="muted">创建于 {formatTime(row.created_at)} · 有效至 {formatTime(row.expires_at)}</p>
      {row.error&&<p role="status">{row.error}</p>}
      <div className="actions">{row.conversation_id&&<a className="button" href={`#/chat/conversation?c=${encodeURIComponent(row.conversation_id)}`}>查看原文</a>}
        {row.status==='ready'&&<><button className="button" onClick={()=>open(row,'correct')}>纠正感受</button><button className="button" onClick={()=>open(row,'reject')}>撤回线索</button></>}
        {row.status!=='deleted'&&<button className="button" onClick={()=>open(row,'delete')}>删除线索</button>}
      </div></article>)}</div>
    {selected&&<Modal title={action==='correct'?'纠正感受':action==='reject'?'撤回线索':'删除线索'} dismissible={!busy} close={()=>setSelected(null)}><form className="form-stack" onSubmit={e=>{
      e.preventDefault();if(busy)return;setBusy(true);setError('');
      const request=action==='delete'?api<EmotionObservation>(`/api/emotion-observations/${selected.id}?version=${selected.version}`,'DELETE'):api<EmotionObservation>(`/api/emotion-observations/${selected.id}/feedback`,'POST',{version:selected.version,action,...(action==='correct'?{label}:{})});
      void request.then(saved=>{setConfirmed(old=>({...old,[saved.id]:saved}));setFocusList(action!=='correct');setSelected(null);setMessage('线索已更新，相关衍生回答已失效。');items.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));
    }}><p>{action==='correct'?'选择你当前的感受，替换这条线索的判断，保留原到期时间。':'清除这条线索的感受与摘录，阻止继续使用。'}相关在途生成和已有衍生回答会失效；原始聊天不会被删除。</p>
      {action==='correct'&&<label>当前感受<select value={label} disabled={busy} onChange={e=>setLabel(e.target.value)}>{Object.entries(observationLabels).map(([key,value])=><option value={key} key={key}>{value}</option>)}</select></label>}
      <ErrorNotice error={error}/><div className="actions"><button type="button" className="button" disabled={busy} onClick={()=>setSelected(null)}>返回列表</button><button className="button primary" disabled={busy}>{busy?'正在保存':action==='correct'?'保存纠正':action==='reject'?'确认撤回':'确认删除'}</button></div>
    </form></Modal>}
  </section>;
}
