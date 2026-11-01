import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {MediaItem, MediaDetail, MediaPage, Vision} from './api';
import type {components} from './api.generated';
import {ErrorNotice, formatTime, Modal, useResource} from './ui';

const states:Record<string,string>={pending:'等待解析',fetching:'等待接收',transcribing:'正在识别',processing:'正在解析',ready:'可用附件',failed:'解析失败',deleted:'已删除',revoked:'来源失效'};
export const mediaSize=(n:number)=> n>=1024**2?`${(n/1024**2).toLocaleString('zh-CN',{maximumFractionDigits:1})} MiB`:`${Math.ceil(n/1024).toLocaleString('zh-CN')} KiB`;
const supported=/\.(txt|md|csv|pdf|docx|xlsx|png|jpe?g|webp)$/i;
type MediaPolicy=components['schemas']['MediaPolicyView'];
type PolicyPreview=components['schemas']['MediaPolicyPreview'];
type MediaType=NonNullable<MediaPolicy['allowed_types']>[number];
const typeOptions:Record<MediaType,{label:string;extensions:string[]}>={
  text:{label:'纯文本 TXT',extensions:['.txt']},markdown:{label:'Markdown',extensions:['.md']},
  csv:{label:'表格 CSV',extensions:['.csv']},pdf:{label:'文本 PDF',extensions:['.pdf']},
  docx:{label:'Word DOCX',extensions:['.docx']},xlsx:{label:'Excel XLSX',extensions:['.xlsx']},
  image:{label:'静态图片 PNG／JPEG／WebP',extensions:['.png','.jpg','.jpeg','.webp']},
  qq_audio:{label:'QQ 语音消息',extensions:['.wav']}
};
const allowedTypes=(p:MediaPolicy)=>p.allowed_types??[];
const allows=(p:MediaPolicy,suffix:string)=>allowedTypes(p).some(type=>typeOptions[type]?.extensions.includes(suffix.toLowerCase()));
const typeText=(p:MediaPolicy)=>allowedTypes(p).map(type=>typeOptions[type]?.label).join('、')||'全部关闭';
const webAllowed=(p:MediaPolicy)=>allowedTypes(p).some(type=>type!=='qq_audio'&&typeOptions[type]);
const policyText=(p:MediaPolicy)=>`每条最多 ${p.max_files} 个，单个 ${p.max_file_mib} MiB、合计 ${p.max_total_mib} MiB`;
function useMediaPolicy(){
  const resource=useResource<MediaPolicy>('/api/media-policy');
  useEffect(()=>{const update=()=>resource.reload();window.addEventListener('lingxi:media-policy',update);return()=>window.removeEventListener('lingxi:media-policy',update);},[resource.reload]);
  return resource;
}

export function MediaPolicySettings(){
  const policy=useMediaPolicy(),[edit,setEdit]=useState<MediaPolicy|null>(null),[notice,setNotice]=useState('');
  return <section className="data-card"><div className="pane-toolbar"><h3>附件策略</h3><button className="button" disabled={!policy.data||!!policy.error} onClick={()=>setEdit(policy.data!)}>调整限制</button></div><ErrorNotice error={policy.error}/>{notice&&<p role="status">{notice}</p>}{policy.error&&<button className="button" onClick={policy.reload}>重试读取</button>}{policy.data&&<><p>{policyText(policy.data)} · v{policy.data.version}</p><p className="wrap">允许类型：{typeText(policy.data)}</p></>}<p className="muted">Web、QQ 与协作提交共用限制，语音及模型自身限制可能更低。关闭类型不会删除已有文件，也不取消已提交任务。</p>{edit&&<MediaPolicyEditor initial={edit} close={()=>setEdit(null)} saved={()=>{setEdit(null);setNotice('媒体配置已保存，新附件按当前类型与大小限制接收。');window.dispatchEvent(new Event('lingxi:media-policy'));}}/>}</section>;
}

export function MediaSettings(){
  return <div className="page-stack"><div className="pane-toolbar"><h2>媒体配置</h2><a className="button" href="#/data/files">管理文件</a></div><MediaPolicySettings/>
    <section className="data-card form-stack"><h3>解析能力</h3><p>允许的附件仍须通过文件内容、权限和解析预算检查。改名或选择类型不会增加解析能力。</p>
      <div className="table-scroll"><table><caption>当前解析方式与限制</caption><thead><tr><th>类型</th><th>处理方式</th><th>边界</th></tr></thead><tbody>
        <tr><td>文字与文档</td><td>提取正文并保留行、页、段落或工作表位置</td><td>扫描 PDF 的专门 OCR 尚未接入；超出预算明确报错</td></tr>
        <tr><td>静态图片</td><td>解码校验、生成预览；经视觉验证的模型理解</td><td>不支持动画、视频或图像生成</td></tr>
        <tr><td>QQ 语音</td><td>协议端转换 PCM WAV，经已验证 ASR 转写</td><td>单条 0.1–60 秒、最多 6 MiB，仍取附件限制中的更低值</td></tr>
      </tbody></table></div><p className="muted">类型开关控制新上传、新选择、协作及 QQ 接收。浏览器实时麦克风、TTS 与 QQ 回复语音在语音和渠道设置中管理，不由此开启或关闭。</p><a className="button" href="#/settings/voice">配置语音</a>
    </section></div>;
}

function MediaPolicyEditor({initial,close,saved}:{initial:MediaPolicy;close():void;saved():void}){
  const [draft,setDraft]=useState(initial),[preview,setPreview]=useState<PolicyPreview|null>(null),[ack,setAck]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[clock,setClock]=useState(Date.now()/1000);
  const lock=useRef(false),focus=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{focus.current?.focus();if(!preview)return;setClock(Date.now()/1000);const timer=setInterval(()=>setClock(Date.now()/1000),1000);return()=>clearInterval(timer);},[preview]);
  async function submit(){
    if(lock.current)return;lock.current=true;setBusy(true);setError('');
    try{if(preview){if(!ack||Date.now()/1000>=preview.expires_at)return;await api('/api/admin/media-policy','PUT',{token:preview.token,acknowledgement:'new_submissions_only'});saved();}
      else{setPreview(await api<PolicyPreview>('/api/admin/media-policy/previews','POST',draft));setAck(false);}}
    catch(e){setError((e as Error).message);}
    finally{lock.current=false;setBusy(false);}
  }
  return <Modal title="附件策略" close={close} dismissible={!busy}><p ref={focus} tabIndex={-1}>调整当前工作区的新附件类型与接收上限，最高为 10 个、20 MiB／个、50 MiB／条。全部取消可关闭新附件；历史文件仍保留。</p><ErrorNotice error={error}/>{preview?<div className="page-stack"><h3>核对变更</h3><p>当前：{policyText(preview.before)}</p><p className="wrap">原有类型：{typeText(preview.before)}</p><p>调整后：{policyText({...preview.after,version:initial.version})}</p><p className="wrap">新的类型：{typeText({...preview.after,version:initial.version})}</p><p>{preview.note}</p><p role="status" className="muted">{clock>=preview.expires_at?'预览已过期，请返回修改后重新预览。':`预览有效至 ${formatTime(preview.expires_at)}；`}配置或登录改变后请重新预览。</p><label className="checkbox"><input type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/>我已核对类型、大小及下载中附件可能接收失败的影响</label><div className="actions"><button className="button" disabled={busy} onClick={()=>{setPreview(null);setAck(false);setError('');}}>返回修改</button><button className="button primary" disabled={busy||!ack||clock>=preview.expires_at} onClick={()=>void submit()}>{busy?'正在保存':'确认保存'}</button></div></div>:<form className="page-stack" onSubmit={e=>{e.preventDefault();void submit();}}>{([['max_files','附件数量',10],['max_file_mib','单个上限（MiB）',20],['max_total_mib','合计上限（MiB）',50]] as const).map(([key,label,max])=><label key={key}>{label}<input required type="number" min={1} max={max} step={1} value={draft[key]} disabled={busy} onChange={e=>setDraft({...draft,[key]:Number(e.target.value)})}/></label>)}<fieldset className="form-stack"><legend>允许类型</legend>{(Object.keys(typeOptions) as MediaType[]).map(type=><label className="checkbox" key={type}><input type="checkbox" checked={allowedTypes(draft).includes(type)} disabled={busy} onChange={e=>setDraft({...draft,allowed_types:e.target.checked?[...allowedTypes(draft),type]:allowedTypes(draft).filter(v=>v!==type)})}/>{typeOptions[type].label}</label>)}<p className="muted">QQ 语音不等于浏览器上传音频。只开启附件类型不会开启渠道接收或模型能力。</p></fieldset><button className="button primary" type="submit" disabled={busy}>{busy?'正在核对':'预览变更'}</button></form>}</Modal>;
}

export function FileUploader({selected=[],disabled=false,onUploaded,onBusy}:{selected?:MediaItem[];disabled?:boolean;onUploaded(item:MediaItem):void;onBusy?(value:boolean):void}) {
  const policy=useMediaPolicy();
  const input=useRef<HTMLInputElement>(null), locked=useRef(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[failed,setFailed]=useState<{file:File;id:string}|null>(null);
  const [progress,setProgress]=useState('');
  async function send(jobs:{file:File;id:string}[]) {
    if(locked.current||disabled)return;
    const limits=policy.data;
    if(!limits||policy.error){setError('附件策略暂不可用，请重试读取后再上传。');return;}
    if(jobs.length+selected.length>limits.max_files || [...jobs.map(v=>v.file.size),...selected.map(v=>v.size)].some(size=>size>limits.max_file_mib*1024**2) || jobs.reduce((s,v)=>s+v.file.size,0)+selected.reduce((s,v)=>s+v.size,0)>limits.max_total_mib*1024**2) {setError(policyText(limits)+'。请减少或缩小文件。');return;}
    if(jobs.some(v=>!supported.test(v.file.name)||!v.file.size)){setError('请选择非空的文字、文档或静态图片；音频与其他格式尚未支持。');return;}
    if([...jobs.map(v=>v.file.name.slice(v.file.name.lastIndexOf('.'))),...selected.map(v=>v.suffix)].some(suffix=>!allows(limits,suffix))){setError('当前媒体配置未允许此附件类型，请调整配置或选择允许的格式。');return;}
    locked.current=true;setBusy(true);onBusy?.(true);setError('');setFailed(null);
    try {
      for(let i=0;i<jobs.length;i++) {
        const job=jobs[i];setProgress(`正在上传 ${i+1}/${jobs.length}：${job.file.name}`);
        try {
          const result=await api<MediaItem>(`/api/media?filename=${encodeURIComponent(job.file.name)}&request_id=${job.id}`,'POST',undefined,{binary:job.file});
          onUploaded(result);
        } catch(e) {setFailed(job);setError(`${(e as Error).message}。已成功上传的文件保留；此文件可重试，其余未上传文件请重新选择。`);break;}
      }
    } finally {locked.current=false;setBusy(false);onBusy?.(false);setProgress('');if(input.current)input.current.value='';}
  }
  return <div className="media-uploader"><input className="sr-only" type="file" ref={input} multiple accept={policy.data?allowedTypes(policy.data).filter(t=>t!=='qq_audio').flatMap(t=>typeOptions[t].extensions).join(','):''} aria-label="选择附件" disabled={disabled||busy||!policy.data||!!policy.error||!webAllowed(policy.data)} onChange={e=>void send(Array.from(e.target.files||[]).map(file=>({file,id:crypto.randomUUID()})))}/><button type="button" className="button" disabled={disabled||busy||!policy.data||!!policy.error||!webAllowed(policy.data)} onClick={()=>input.current?.click()}>{busy?'正在上传':'上传附件'}</button><small className="muted">{policy.data?policyText(policy.data):'正在读取附件策略'}</small>{policy.data&&!policy.error&&<small className="muted">{webAllowed(policy.data)?`当前允许：${allowedTypes(policy.data).filter(type=>type!=='qq_audio').map(type=>typeOptions[type].label).join('、')}`:<>当前未允许 Web 文件上传，请在<a href="#/settings/media">媒体配置</a>调整类型。</>}</small>}{progress&&<p role="status">{progress}</p>}<ErrorNotice error={error||policy.error}/>{policy.error&&<button type="button" className="button" onClick={policy.reload}>重试读取</button>}{failed&&!busy&&<button type="button" className="button" disabled={disabled||!!policy.error} onClick={()=>void send([failed])}>重试上传</button>}</div>;
}

export function AttachmentList({items,remove}:{items:MediaItem[];remove?(item:MediaItem):void}) {
  const [detail,setDetail]=useState<string|null>(null);
  return <><ul className="attachment-list" aria-label="消息附件">{items.map(item=><li key={item.id}><button type="button" className="attachment-open" onClick={()=>setDetail(item.id)}><span aria-hidden="true">{item.kind==='image'?'▧':'▤'}</span><span><strong>{item.filename}</strong><small>{mediaSize(item.size)} · {states[item.status]||item.status}</small></span></button>{remove&&<button type="button" className="button" onClick={()=>remove(item)} aria-label={`移除${item.filename}`}>移除</button>}</li>)}</ul>{detail&&<MediaPreview identity={detail} close={()=>setDetail(null)}/>}</>;
}

export function MediaPreview({identity,close}:{identity:string;close():void}) {
  const item=useResource<MediaDetail>('/api/media/'+identity),[count,setCount]=useState(50);
  useEffect(()=>{if(!item.data||!['pending','processing'].includes(item.data.status))return;const timer=setInterval(item.reload,2000);return()=>clearInterval(timer);},[item.data?.status,item.reload]);
  return <Modal title="附件来源" close={close}><ErrorNotice error={item.error}/>{!item.data&&!item.error&&<p role="status">正在读取附件…</p>}{item.data&&<><h3 className="wrap">{item.data.filename}</h3><p className="muted">{states[item.data.status]} · {mediaSize(item.data.size)} · 版本 {item.data.version}</p><ErrorNotice error={item.data.error}/>{item.data.source?.kind==='server_document'&&<div className="data-card"><h4>服务器来源</h4><p className="wrap">{String(item.data.source.path)}</p><p className="muted">快照时间：{formatTime(Number(item.data.source.observed_at))}。原文件未修改，后续变动不自动同步；删除此附件只清理快照。</p></div>}{item.data.status==='ready'&&<>{item.data.kind==='image'?<img className="media-preview-image" src={`/api/media/${identity}/content?preview=true`} alt={`已上传：${item.data.filename}`}/>:<><p>{item.data.parts_count} 个位置，当前展示 {Math.min(count,item.data.parts.length)} 个；聊天实际使用范围受上下文预算限制。</p><div className="media-parts">{item.data.parts.slice(0,count).map((part,i)=><section key={i}><h4>{part.location}</h4><pre>{part.text}</pre></section>)}</div>{item.data.parts.length>count&&<button type="button" className="button" onClick={()=>setCount(n=>n+50)}>加载更多</button>}</>}{item.data.notes.map((n,i)=><p key={i} className="muted">{n}</p>)}</>}{!['deleted','revoked'].includes(item.data.status)&&<a className="button" href={`/api/media/${identity}/content`}>{item.data.kind==='audio'?'下载音频':'下载原件'}</a>}</>}</Modal>;
}

export function MediaFiles() {
  const [q,setQ]=useState(''),[cursor,setCursor]=useState<string|null>(null),[remove,setRemove]=useState<MediaItem|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const data=useResource<MediaPage>('/api/media?q='+encodeURIComponent(q)+(cursor?'&cursor='+cursor:''));
  const [detail,setDetail]=useState<string|null>(null);
  const refresh=data.reload;
  useEffect(()=>{const t=setInterval(refresh,3000);return()=>clearInterval(t);},[refresh]);
  return <div className="data-panel"><div className="pane-toolbar"><h2>文件管理</h2><button className="button" onClick={refresh}>刷新文件</button></div><MediaPolicySettings/><p className="muted">附件只保存在服务器授权存储中。不会自动保存为长期记忆；新上传文件可在聊天中选择。</p><FileUploader onUploaded={refresh}/><label>搜索文件<input value={q} placeholder="按文件名称搜索" onChange={e=>{setQ(e.target.value);setCursor(null);}}/></label><ErrorNotice error={data.error||error}/>{!data.data&&!data.error&&<p role="status">正在读取文件…</p>}{data.data?.items.length===0&&<div className="data-empty">没有找到附件。可先上传一份文档。</div>}<div className="media-file-grid">{data.data?.items.map(item=><article className="data-card" key={item.id}><div className="pane-toolbar"><h3 className="wrap">{item.filename}</h3><span className={`status-badge status-${item.status}`}>{states[item.status]}</span></div><p className="muted">{mediaSize(item.size)} · {formatTime(item.created_at)}</p><p>{item.kind==='audio'?'语音转写（未经确认）':item.kind==='image'?'静态图片':`${item.parts_count} 个来源位置`}</p><ErrorNotice error={item.error}/>{item.status==='deleted'?<div><p className="muted">清理状态：{item.cleanup_status==='completed'?'文件已清理':item.cleanup_status==='failed'?'清理失败':'后台清理中'}</p>{item.cleanup_status==='failed'&&<button type="button" className="button" disabled={busy} onClick={()=>{setBusy(true);setError('');void api('/api/media/'+item.id+'/retry-cleanup','POST',{version:item.version}).then(refresh).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>重试清理</button>}</div>:<div className="actions"><button className="button" onClick={()=>setDetail(item.id)}>查看来源</button><button className="button danger" onClick={()=>{setError('');setRemove(item);}}>删除附件</button></div>}</article>)}</div>{(cursor||data.data?.next_cursor)&&<div className="actions">{cursor&&<button className="button" onClick={()=>setCursor(null)}>返回首页</button>}{data.data?.next_cursor&&<button className="button" onClick={()=>setCursor(data.data!.next_cursor!)}>下一页</button>}</div>}{detail&&<MediaPreview identity={detail} close={()=>setDetail(null)}/ >}{remove&&<Modal title="删除附件" close={()=>setRemove(null)}><p>将删除“{remove.filename}”的附件副本与解析结果，并作废引用它的回答及后续衍生回复。此操作无法撤销。</p><p className="muted">立即停止使用，后台完成文件清理。服务器原始资料不受影响；不会删除你的其他文字消息或已确认的独立长期记忆。</p><ErrorNotice error={error}/><button className="button danger" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api('/api/media/'+remove.id,'DELETE',{version:remove.version}).then(()=>{setRemove(null);refresh();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在删除':'确认删除'}</button></Modal>}</div>;
}

function FilePicker({items,setItems,close}:{items:MediaItem[];setItems(items:MediaItem[]):void;close():void}) {
  const [q,setQ]=useState(''),[cursor,setCursor]=useState<string|null>(null);
  const policy=useMediaPolicy();
  const files=useResource<MediaPage>('/api/media?limit=40&q='+encodeURIComponent(q)+(cursor?'&cursor='+cursor:''));
  return <Modal title="选择文件" close={close}><p className="muted">{policy.data?policyText(policy.data):'正在读取附件策略'}；只可选择解析完成的附件。</p><label>搜索附件<input value={q} onChange={e=>{setQ(e.target.value);setCursor(null);}} placeholder="按文件名称搜索"/></label><ErrorNotice error={files.error||policy.error}/>{policy.error&&<button type="button" className="button" onClick={policy.reload}>重试读取</button>}{!files.data&&!files.error&&<p role="status">正在读取附件…</p>}{files.data?.items.filter(v=>v.status==='ready').map(item=><label className="checkbox media-choice" key={item.id}><input type="checkbox" checked={items.some(v=>v.id===item.id)} disabled={!items.some(v=>v.id===item.id)&&(!policy.data||!!policy.error||!allows(policy.data,item.suffix)||items.length>=policy.data.max_files||item.size>policy.data.max_file_mib*1024**2||items.reduce((s,v)=>s+v.size,0)+item.size>policy.data.max_total_mib*1024**2)} onChange={e=>setItems(e.target.checked?[...items,item]:items.filter(v=>v.id!==item.id))}/><span>{item.filename} · {mediaSize(item.size)}{policy.data&&!allows(policy.data,item.suffix)?' · 当前类型未允许':''}</span></label>)}{files.data&&!files.data.items.some(v=>v.status==='ready')&&<p>本页暂无可用附件。请上传并等待解析，或查看下一页。</p>}<div className="actions">{cursor&&<button type="button" className="button" onClick={()=>setCursor(null)}>返回首页</button>}{files.data?.next_cursor&&<button type="button" className="button" onClick={()=>setCursor(files.data!.next_cursor!)}>下一页</button>}<button type="button" className="button primary" onClick={close}>完成选择</button></div></Modal>;
}

export function ChatAttachments({items,setItems,disabled,onBusy}:{items:MediaItem[];setItems(items:MediaItem[]):void;disabled:boolean;onBusy(value:boolean):void}) {
  const current=useRef(items);current.current=items;
  const [choose,setChoose]=useState(false),[error,setError]=useState('');
  useEffect(()=>{
    if(!items.some(v=>['pending','processing'].includes(v.status)))return;
    let live=true;const controller=new AbortController();
    const t=setInterval(()=>{void Promise.all(items.map(v=>api<MediaItem>('/api/media/'+v.id,'GET',undefined,{signal:controller.signal}))).then(next=>{if(live){setItems(next);setError('');}}).catch(()=>{if(live)setError('附件状态暂时无法读取，正在重试。也可稍后在文件管理中查看。');});},1500);
    return()=>{live=false;controller.abort();clearInterval(t);};
  },[items,setItems]);
  return <div className="chat-attachments"><FileUploader selected={items} disabled={disabled} onBusy={onBusy} onUploaded={v=>setItems([...current.current,v])}/><button type="button" className="button" disabled={disabled} onClick={()=>setChoose(true)}>选择文件</button><AttachmentList items={items} remove={disabled?undefined:item=>setItems(items.filter(v=>v.id!==item.id))}/><ErrorNotice error={error}/>{items.some(v=>v.status!=='ready')&&<p className="muted">请等待附件解析完成；解析失败或已删除的附件需移除后再发送。</p>}{choose&&<FilePicker items={items} setItems={setItems} close={()=>setChoose(false)}/>}</div>;
}

export function VisionControl({modelId,enabled}:{modelId:string;enabled:boolean}) {
  const info=useResource<Vision>('/api/models/'+modelId+'/vision'),[busy,setBusy]=useState(false),[error,setError]=useState('');
  return <div className="vision-control"><small className="block muted">视觉：{info.data?.status==='verified'?'图片探针通过':'未验证'}</small><button type="button" className="button" disabled={!enabled||busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api<Vision>('/api/models/'+modelId+'/vision','POST').then(v=>{setError(v.error||'');info.reload();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>{busy?'正在验证':'验证视觉'}</button>{!enabled&&<small className="block muted">先通过对话验证</small>}<ErrorNotice error={error||info.error}/></div>;
}
