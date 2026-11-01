import React,{useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import type {AssistantProfile,Model,Provider} from './api';
import {ErrorNotice,Modal,useResource} from './ui';

type Binding=components['schemas']['BindingView'];
type Installation=components['schemas']['InstallationView'];
type Template={id:string;name:string;kind:string;installation_id:string;plugin_id:string;plugin_version:string;agent?:{expertise:string;instructions:string;completion:string;tools:string[];context_categories:string[]}};
const categories:Record<string,string>={profile:'核心档案',preferences:'稳定偏好',experiences:'共同经历',projects:'项目目标',relationships:'人物关系',procedures:'操作经验',support:'支持偏好'};
const toolNames:Record<string,string>={reason:'文字分析','server.files':'只读清单','plugin.call':'远程工具候选'};

export function PluginAgents(){
 const bindings=useResource<Binding[]>('/api/plugin-agent-bindings'),caps=useResource<Template[]>('/api/plugin-capabilities');
 const installs=useResource<Installation[]>('/api/plugin-installations'),personas=useResource<AssistantProfile[]>('/api/personas');
 const models=useResource<Model[]>('/api/models'),providers=useResource<Provider[]>('/api/providers');
 const [editing,setEditing]=useState<Template|null>(null),[revoking,setRevoking]=useState<Binding|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const templates=(caps.data||[]).filter(c=>c.kind==='agent'&&c.agent);
 const refresh=()=>{bindings.reload();caps.reload();installs.reload();};
 const choices=(models.data||[]).filter(m=>m.purpose==='chat'&&m.status==='verified'&&providers.data?.some(p=>p.id===m.provider_id&&p.enabled));
 async function revoke(){if(!revoking||busy)return;setBusy(true);setError('');try{await api(`/api/plugin-agent-bindings/${revoking.id}/revoke`,'POST',{version:revoking.version});setRevoking(null);refresh();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 return <section className="page-stack" aria-label="插件角色"><div className="pane-toolbar"><h2>插件角色</h2><button className="button" onClick={refresh}>刷新角色</button></div>
  <p className="muted">安装插件后，单独选择本地人格、模型与相关记忆范围。启用后可由任务中心或聊天的助手协作自动选用；不会自动执行插件指令或外部操作。</p>
  <ErrorNotice error={error||bindings.error||caps.error||installs.error||personas.error||models.error||providers.error}/>
  {(!bindings.data||!caps.data)&&!bindings.error&&!caps.error&&<p role="status">正在加载角色…</p>}
  {caps.data&&templates.length===0&&<div className="data-empty">暂无已安装的角色插件。<a href="#/plugins/catalog">前往插件商店</a></div>}
  <div className="card-grid">{templates.map(cap=>{const row=bindings.data?.find(b=>b.installation_id===cap.installation_id&&b.capability_id===cap.id),install=installs.data?.find(i=>i.id===cap.installation_id);return <article className="data-card profile-card" key={cap.installation_id+cap.id}>
   <div className="pane-toolbar"><h3>{cap.name}</h3><span className="status-badge">{row?.available?'可以调度':row?.status==='revoked'?'已撤销':row?'需要核对':'尚未配置'}</span></div>
   <small>{cap.plugin_id} · v{cap.plugin_version} · {cap.id}</small><p>{cap.agent!.expertise}</p><p className="muted">完成条件：{cap.agent!.completion}</p>
   {row&&<><p className="muted">人格：{personas.data?.find(p=>p.id===row.persona_id)?.name||'不可用'} · 配置 v{row.version} · 已确认插件 v{row.plugin_version}</p><p className="muted">共享类别：{row.context_categories.map(c=>categories[c]||c).join('、')||'不共享记忆'} · 候选工具：{row.tools.map(t=>toolNames[t]||t).join('、')}</p>{row.reason&&<p className="muted">{row.reason}</p>}</>}
   <details><summary>角色声明</summary><p className="memory-content">{cap.agent!.instructions||'没有额外指令。'}</p><p className="muted">声明是外部配置，不能授予权限、写入记忆或自行联系用户。</p></details>
   <div className="actions"><button className="button primary" disabled={!install?.available||!bindings.data||!personas.data||!models.data||!providers.data} onClick={()=>{setError('');setEditing(cap);}}>{row?'编辑角色':'配置角色'}</button>{row?.status==='enabled'&&<button className="button" onClick={()=>{setError('');setRevoking(row);}}>撤销角色</button>}<a className="button" href="#/tasks/list?mode=agents">助手协作</a></div>
  </article>;})}</div>
  {editing&&<Editor key={editing.installation_id+editing.id} template={editing} binding={bindings.data?.find(b=>b.installation_id===editing.installation_id&&b.capability_id===editing.id)} install={installs.data!.find(i=>i.id===editing.installation_id)!} personas={(personas.data||[]).filter(p=>p.enabled)} models={choices} close={()=>setEditing(null)} done={()=>{setEditing(null);refresh();}}/>}
  {revoking&&<Modal title="撤销角色" close={()=>setRevoking(null)} dismissible={!busy}><p>撤销“{revoking.role_name}”后阻止新派发，在途任务提交前也会检查。再次启用不能恢复旧任务；已完成的分析和真实模型费用保留。</p><ErrorNotice error={error}/><button className="button primary" disabled={busy} onClick={()=>void revoke()}>确认撤销</button></Modal>}
 </section>;
}

function Editor({template,binding,install,personas,models,close,done}:{template:Template;binding?:Binding;install:Installation;personas:AssistantProfile[];models:Model[];close():void;done():void}){
 const spec=template.agent!;
 const [persona,setPersona]=useState(binding?.persona_id||''),[model,setModel]=useState(binding?.model_id||'');
 const [tools,setTools]=useState((binding?.tools||['reason']).filter(t=>spec.tools.includes(t))),[shared,setShared]=useState((binding?.context_categories||[]).filter(c=>spec.context_categories.includes(c)));
 const [accepted,setAccepted]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const toggle=(list:string[],key:string)=>list.includes(key)?list.filter(c=>c!==key):[...list,key];
 async function save(e:React.FormEvent){e.preventDefault();if(busy||!accepted)return;setBusy(true);setError('');try{await api('/api/plugin-agent-bindings','POST',{installation_id:install.id,installation_version:install.version,capability_id:template.id,version:binding?.version||0,persona_id:persona,model_id:model||null,tools,context_categories:shared,acknowledged:true});done();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 return <Modal title="角色配置" close={close} dismissible={!busy}><form onSubmit={e=>void save(e)} className="form-stack"><h3>{template.name} · v{template.plugin_version}</h3><p className="muted">{template.plugin_id} · {template.id}。旧任务保留版本；缩小范围或撤销仍会阻止旧上下文继续使用。</p>
  <label>选择人格<select required value={persona} onChange={e=>{setPersona(e.target.value);setAccepted(false);}}><option value="">请选择本地人格</option>{personas.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
  <label>对话模型<select value={model} onChange={e=>{setModel(e.target.value);setAccepted(false);}}><option value="">跟随人格／固定对话策略</option>{models.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
  <fieldset><legend>候选工具</legend>{spec.tools.map(t=><label className="checkbox" key={t}><input type="checkbox" checked={tools.includes(t)} disabled={t==='reason'} onChange={()=>{setTools(toggle(tools,t));setAccepted(false);}}/>{toolNames[t]||t}</label>)}</fieldset>
  <fieldset><legend>相关记忆</legend><p className="muted">默认不共享；任务还需开启共享记忆。仅检索所选类别的任务相关内容，不提供完整记忆库。</p>{spec.context_categories.map(c=><label className="checkbox" key={c}><input type="checkbox" checked={shared.includes(c)} onChange={()=>{setShared(toggle(shared,c));setAccepted(false);}}/>{categories[c]||c}</label>)}</fieldset>
  {tools.includes('server.files')&&<p className="muted">只读清单仍需服务器资源可用、基础插件授权及任务明确允许；不授予写文件、联网或第三方工具权限。</p>}
  <p className="muted">角色不能增加权限、修改事实、写记忆或独立通知用户。使用已配置模型可能计费，未知价格不作估算。</p><label className="checkbox"><input type="checkbox" checked={accepted} onChange={e=>setAccepted(e.target.checked)}/>我已核对插件版本、人格、模型、工具及相关记忆范围，同意启用。</label><ErrorNotice error={error}/><button className="button primary" disabled={busy||!accepted||!personas.some(p=>p.id===persona)||(!!model&&!models.some(m=>m.id===model))} type="submit">{busy?'正在保存':'确认启用'}</button>
 </form></Modal>;
}
