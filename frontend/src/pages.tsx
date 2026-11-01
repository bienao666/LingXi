import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import {AttachmentList, ChatAttachments, VisionControl, MediaPreview} from './MediaPages';
import {VoicePanel} from './VoicePages';
import {AgentToolScope} from './AgentTools';
import type {MediaItem,ChatAction} from './api';
import { api, download, isChatControl } from './api';
import { ModelSetup, RoutingControls, modelPurposes } from './ModelSetup';
import { AddModelForm } from './AddModelForm';
import { ModelCalls } from './ModelCalls';
import {MemoryPager,useMemoryRecords} from './MemoryPaging';
import {DialogueCandidateReview} from './MemoryReview';
import {MemoryTimeDetails,memoryTimeFields} from './MemoryTimes';
import { ConversationPersona } from './ConversationPersona';
import type { ModelPolicy } from './ModelSetup';
import type { Candidate, Conversation, ConversationPage, MessagePage, Memory, Message, Model, Operation, Provider, ProviderInput, Run, Session } from './api';

const categories: Record<string, string> = {profile:'核心档案', preferences:'稳定偏好', experiences:'共同经历', projects:'项目目标', relationships:'人物关系', procedures:'操作经验', support:'支持偏好'};
const purposes = modelPurposes;
const states: Record<string, string> = {not_used:'未使用', healthy:'服务正常', unconfigured:'尚未配置', pending:'等待处理', unverified:'未验证', verified:'已验证', ready:'可检索', active:'有效记忆', queued:'等待生成', running:'正在生成', completed:'已完成', confirmed:'已确认', rejected:'已拒绝', failed:'处理失败', cancelled:'已停止', invalidated:'依据失效', streaming:'正在回复', unavailable:'暂不可用', deleted:'已删除', deleting:'正在清理'};
const label = (v: string) => states[v] || v;
const date = (v: number) => new Date(v*1000).toLocaleString();

export function AgentCandidateReview({candidate,close,done}:{candidate:Candidate;close():void;done():void}){
  const source=useResource<{candidate:Candidate;review_digest:string;warning:string}>('/api/memory-candidates/'+candidate.id+'/sources');
  const [accepted,setAccepted]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(false),row=source.data?.candidate,origin=row?.origin;
  const [mediaSource,setMediaSource]=useState<string|null>(null);
  async function confirm(){
    if(!source.data||!accepted||pending.current||source.error)return;
    pending.current=true;setBusy(true);setError('');
    try{await api('/api/memory-candidates/'+candidate.id+'/actions','POST',{action:'confirm',review_digest:source.data.review_digest});done();}
    catch(e){setError((e as Error).message);}finally{pending.current=false;setBusy(false);}
  }
  return <Modal title="核对记忆" close={close} dismissible={!busy}><Notice error={source.error||error}/>{source.error&&<button className="button" disabled={busy} onClick={()=>{setAccepted(false);source.refresh();}}>重试来源</button>}{!source.data&&!source.error&&<p role="status">正在核对候选来源…</p>}{row&&origin&&<div className="page-stack"><p>{source.data!.warning}</p><section><h3>待存内容</h3><p className="memory-content">{row.content}</p><p>{categories[row.category]||row.category} · 主体：{row.subject==='self'?'本人':row.subject==='unknown'?'尚未确定，请拒绝后手动记录':row.subject}</p><p className="muted">提出理由：{row.reason}</p></section><section><h3>原始依据</h3>{Array.isArray(origin.citations)&&origin.citations.map((c:any,i:number)=><article className="data-card" key={i}><h4>{c.kind==='task_goal'?'用户任务原文':c.kind==='memory'?'已检索记忆':c.kind==='media'?'分配附件':'依赖分析（仍需核实）'}</h4>{typeof c.content==='string'&&<p className="memory-content">{c.content}</p>}{c.kind==='memory'&&<MemoryTimeDetails value={c}/>} {c.kind==='media'&&<div><p>{String(c.filename)} · v{String(c.version)} · {String(c.parts_count)} 段。请结合原附件核对。</p><button className="button" onClick={()=>setMediaSource(String(c.id))}>查看附件</button></div>}{c.kind==='dependency'&&<pre className="task-json">{JSON.stringify(c.result,null,2)}</pre>}<small>来源：{String(c.id)}</small></article>)}</section><p>子助手：{String(origin.agent_name)} · 模型：{String(origin.model)}</p><details><summary>版本来源</summary><pre className="task-json">{JSON.stringify({task:origin.run_id,node:origin.node_key,round:origin.round,model:origin.model_id,provider_version:origin.provider_version,persona:origin.persona,role:origin.role,plugin:origin.plugin_id,plugin_version:origin.plugin_version,release:origin.plugin_release_id},null,2)}</pre></details><p>请核实主体、引用、假设、时间及冲突；这次确认仅保存当前一项，不授予后续自动保存权限。</p><label className="checkbox"><input type="checkbox" checked={accepted} disabled={busy||!!source.error} onChange={e=>setAccepted(e.target.checked)}/>我已核对内容、主体和来源，采纳为长期记忆。</label><div className="actions"><button className="button" disabled={busy} onClick={close}>暂不保存</button><button className="button primary" disabled={busy||!accepted||!!source.error||row.subject==='unknown'} onClick={()=>void confirm()}>{busy?'正在保存':'确认保存'}</button></div></div>}{mediaSource&&<MediaPreview identity={mediaSource} close={()=>setMediaSource(null)}/>}</Modal>;
}
function useResource<T>(url: string) {
  const [data, setData] = useState<T>(), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(x => x+1), []);
  useEffect(() => { let ignore = false; setError(''); void api<T>(url).then(d => {if (!ignore) setData(d);}).catch(e => {if (!ignore) setError(e.message);}); return () => {ignore = true;}; }, [url, revision]);
  return {data, error, refresh};
}
function useAction(refresh?: () => void) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const act = async (fn: () => Promise<unknown>) => {setBusy(true); setError(''); try {await fn(); refresh?.();} catch(e) {setError((e as Error).message);} finally {setBusy(false);}};
  return {act, busy, error};
}
function Notice({error}: {error?: string}) {return error ? <p role="alert" className="error">{error}</p> : null;}
function Empty({text = '暂时没有记录'}: {text?: string}) {return <div className="data-empty">{text}</div>;}
function Badge({value}: {value: string}) {return <span className={`status-badge status-${value}`}>{label(value)}</span>;}
function Modal({title, close, children,dismissible=true}: {title: string; close(): void; children: React.ReactNode;dismissible?:boolean}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId=useId();
  useEffect(() => {const prior=document.activeElement as HTMLElement|null;ref.current?.showModal();return()=>prior?.focus();}, []);
  return <dialog ref={ref} aria-labelledby={titleId} className="business-dialog" onClose={e=>{if(e.target===e.currentTarget)close();}} onCancel={e=>{if(!dismissible)e.preventDefault();}} onKeyDown={e=>{
    if(e.key!=='Tab'||(e.target as Element).closest('dialog')!==e.currentTarget)return;
    const controls=Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary')||[]).filter(el=>el.closest('dialog')===e.currentTarget&&el.getClientRects().length);
    const first=controls[0],last=controls.at(-1);
    if(e.shiftKey&&(document.activeElement===first||document.activeElement===ref.current)){e.preventDefault();last?.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
  }}><div className="dialog-heading"><h2 id={titleId}>{title}</h2><button className="button" disabled={!dismissible} onClick={() => ref.current?.close()} aria-label={`关闭${title}`}>关闭</button></div>{children}</dialog>;
}

export function ChatPage({hash}: {hash: string}) {
  const id = new URLSearchParams(hash.split('?')[1]).get('c');
  const [q, setQ] = useState(''), [input, setInput] = useState('');
  const [directory,setDirectory]=useState<Conversation[]>([]),[directoryNext,setDirectoryNext]=useState<string|null>(null),[directoryError,setDirectoryError]=useState(''),[directoryBusy,setDirectoryBusy]=useState(false),[directoryRevision,setDirectoryRevision]=useState(0);
  const directoryGeneration=useRef(0),directoryPending=useRef(false);
  const refreshDirectory=useCallback(()=>setDirectoryRevision(v=>v+1),[]);
  useEffect(()=>{
    const token=++directoryGeneration.current;directoryPending.current=true;setDirectoryBusy(true);setDirectory([]);setDirectoryNext(null);setDirectoryError('');
    void api<ConversationPage>('/api/conversation-pages?q='+encodeURIComponent(q)).then(page=>{if(token===directoryGeneration.current){setDirectory(page.items);setDirectoryNext(page.next_cursor);}}).catch(e=>{if(token===directoryGeneration.current)setDirectoryError(e.message);}).finally(()=>{if(token===directoryGeneration.current){directoryPending.current=false;setDirectoryBusy(false);}});
    return()=>{directoryGeneration.current++;};
  },[q,directoryRevision]);
  async function moreConversations(){
    if(!directoryNext||directoryPending.current)return;
    const token=directoryGeneration.current;directoryPending.current=true;setDirectoryBusy(true);setDirectoryError('');
    try{const page=await api<ConversationPage>('/api/conversation-pages?q='+encodeURIComponent(q)+'&cursor='+encodeURIComponent(directoryNext));if(token===directoryGeneration.current){setDirectory(old=>[...old,...page.items.filter(c=>!old.some(v=>v.id===c.id))]);setDirectoryNext(page.next_cursor);}}
    catch(e){if(token===directoryGeneration.current)setDirectoryError((e as Error).message);}
    finally{if(token===directoryGeneration.current){directoryPending.current=false;setDirectoryBusy(false);}}
  }
  const list={data:directory,error:directoryError,refresh:refreshDirectory};
  const [current,setCurrent]=useState<Conversation>();
  const [branchInfo,setBranchInfo]=useState<{is_branch?:boolean;origin?:{id:string;title:string}|null;children?:{id:string;title:string}[]}>({});
  const [forkMessage,setForkMessage]=useState<Message|null>(null);
  const [reply,setReply]=useState<Message|null>(null),[voiceActive,setVoiceActive]=useState(false);
  useEffect(()=>{if(voiceActive)setReply(null);},[voiceActive]);
  const [historyOpen,setHistoryOpen]=useState(false),[contextRun,setContextRun]=useState<string|null>(null);
  const [messages, setMessages] = useState<Message[]>([]), [runs, setRuns] = useState<Run[]>([]), [error, setError] = useState('');
  const action = useAction(list.refresh), streamRef = useRef<EventSource | null>(null);
  const [media,setMedia]=useState<MediaItem[]>([]),[uploading,setUploading]=useState(false);
  const [taskAssist,setTaskAssist]=useState(false),[chatActions,setChatActions]=useState<ChatAction[]>([]),[taskError,setTaskError]=useState('');
  const [toolKeys,setToolKeys]=useState<string[]>([]);
  const [collaborate,setCollaborate]=useState(false),[shareMemory,setShareMemory]=useState(false),[allowFiles,setAllowFiles]=useState(false),[proposeMemory,setProposeMemory]=useState(false),[maxCalls,setMaxCalls]=useState(12);
  const pending = useRef<{id: string; content: string; mediaKey: string; taskAssist:boolean; collaborationKey:string; replyId:string|null; request_id: string} | null>(null);
  const generation = useRef(0),pageGeneration=useRef(0),requestSeq=useRef(0),appliedSeq=useRef(0);
  const cursors=useRef<(string|null)[]>([null]),navigationBusy=useRef(false),focusPage=useRef(false),messageArea=useRef<HTMLDivElement>(null);
  const [pageDepth,setPageDepth]=useState(0),[nextMessageCursor,setNextMessageCursor]=useState<string|null>(null),[messageReady,setMessageReady]=useState(false),[pageBusy,setPageBusy]=useState(false);
  const [latestMessages,setLatestMessages]=useState<Message[]>([]);
  const inFlightLoads=useRef(new Map<string,Promise<void>>());
  const load = useCallback(():Promise<void> => {
    if (!id) return Promise.resolve();
    const currentGeneration=generation.current,pageToken=pageGeneration.current,seq=++requestSeq.current,cursor=cursors.current.at(-1);
    const key=id+':'+currentGeneration+':'+pageToken,prior=inFlightLoads.current.get(key);
    if(prior)return prior;
    const latestUrl='/api/message-pages?conversation_id='+encodeURIComponent(id);
    const work=(async()=>{try {
      const [page,latest,rs,acts,branches,detail] = await Promise.all([
        api<MessagePage>(latestUrl+(cursor?'&cursor='+encodeURIComponent(cursor):'')),
        cursor?api<MessagePage>(latestUrl):Promise.resolve(null),
        api<Run[]>('/api/chat-runs?conversation_id='+id),
        api<ChatAction[]>('/api/chat-actions?conversation_id='+id).then(data=>({data,error:''})).catch(e=>({data:[],error:(e as Error).message})),
        api<{is_branch?:boolean;origin?:{id:string;title:string}|null;children?:{id:string;title:string}[]}>(`/api/conversations/${id}/branches`),
        api<Conversation>('/api/conversations/'+encodeURIComponent(id))]);
      if (currentGeneration===generation.current&&pageToken===pageGeneration.current&&seq>=appliedSeq.current) {
        appliedSeq.current=seq;setMessages(page.items);setLatestMessages((latest||page).items);setNextMessageCursor(page.next_cursor);setMessageReady(true);setPageBusy(false);navigationBusy.current=false;if(focusPage.current){focusPage.current=false;messageArea.current?.focus();}
        setRuns(rs);setChatActions(acts.data);setTaskError(acts.error);setBranchInfo(branches);setCurrent(detail);setError('');
        setReply(previous=>{
          const source=[...page.items,...(latest?.items||[])].find(m=>m.id===previous?.id);
          return source&&source.status!=='completed'?null:previous;
        });
      }
    } catch(e) {if(currentGeneration===generation.current&&pageToken===pageGeneration.current&&seq>=appliedSeq.current){appliedSeq.current=seq;setError((e as Error).message);setPageBusy(false);navigationBusy.current=false;}}})();
    const tracked=work.finally(()=>{if(inFlightLoads.current.get(key)===tracked)inFlightLoads.current.delete(key);});
    inFlightLoads.current.set(key,tracked);return tracked;
  }, [id]);
  useEffect(()=>{setReply(null);setVoiceActive(false);},[id]);
  function navigateMessages(direction:'older'|'newer'|'latest'){
    if(direction==='older'){if(!nextMessageCursor||navigationBusy.current)return;cursors.current.push(nextMessageCursor);}
    else if(direction==='newer'){if(cursors.current.length<=1)return;cursors.current.pop();}
    else cursors.current=[null];
    pageGeneration.current++;navigationBusy.current=true;focusPage.current=true;setPageDepth(cursors.current.length-1);setNextMessageCursor(null);setMessageReady(false);setMessages([]);setPageBusy(true);setError('');void load();
  }
  useEffect(() => {generation.current++;pageGeneration.current++;navigationBusy.current=false;focusPage.current=false;cursors.current=[null];setPageDepth(0);setNextMessageCursor(null);setMessageReady(false);setPageBusy(!!id);setCurrent(undefined);setMessages([]);setLatestMessages([]);setRuns([]);setChatActions([]);setBranchInfo({});setForkMessage(null);setHistoryOpen(false);setContextRun(null);setTaskError('');setError('');void load();const timer=setInterval(()=>void load(),1500);return()=>{generation.current++;clearInterval(timer);};}, [load]);
  const active = runs.find(r => ['queued','running'].includes(r.status));
  useEffect(() => {
    if (!active) return;
    const source = new EventSource(`/api/chat-runs/${active.id}/events?after=${active.next_seq}`); streamRef.current = source;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = () => {if (!timer) timer = setTimeout(() => {timer = undefined; void load();}, 80);};
    ['delta','context','action'].forEach(kind => source.addEventListener(kind, update));
    ['done','settled','invalidated','revoked'].forEach(kind => source.addEventListener(kind, () => {source.close(); void load();}));
    return () => {source.close(); clearTimeout(timer);};
  }, [active?.id, load]);
  async function create() {
    const c = await api<Conversation>('/api/conversations', 'POST', {title:'新的对话'}); location.hash = `#/chat/conversation?c=${c.id}`; list.refresh(); return c.id;
  }
  const [rename, setRename] = useState<Conversation | null>(null), [remove, setRemove] = useState<Conversation | null>(null);
  return <div className="chat-layout"><aside className="conversation-list"><div className="pane-toolbar"><h2>我的对话</h2><button className="button" disabled={action.busy} onClick={() => void action.act(create)}>新建对话</button></div><input aria-label="搜索对话" placeholder="搜索对话与消息" value={q} onChange={e => setQ(e.target.value)}/><Notice error={list.error}/><div className="conversation-items" aria-busy={directoryBusy}>{list.data?.map(c => <a className={`conversation-link ${c.id === id ? 'selected' : ''}`} key={c.id} href={`#/chat/conversation?c=${c.id}`}><strong>{c.title}</strong><small>{date(c.created_at)}</small></a>)}{directoryBusy&&<p role="status">正在加载对话…</p>}{!directoryBusy&&!directoryError&&list.data.length===0&&<Empty text={q?'没有匹配的对话':'开启第一段对话'}/>}<div className="actions">{directoryNext&&<button className="button" disabled={directoryBusy} onClick={()=>void moreConversations()}>更多对话</button>}{directoryError&&<button className="button" disabled={directoryBusy} onClick={()=>directoryNext?void moreConversations():list.refresh()}>重试列表</button>}</div></div></aside>
    <div className="chat-main"><div className="pane-toolbar"><h2>{current?.title || '与灵犀聊聊'}</h2>{current && <div className="actions"><button className="button" onClick={()=>setHistoryOpen(true)}>历史原文</button><button className="button" onClick={() => setRename(current)}>重命名</button><button className="button" onClick={() => download(`/api/conversations/${id}/export`)}>导出 MD</button><button className="button" onClick={() => download(`/api/conversations/${id}/export?format=json`)}>导出 JSON</button><button className="button danger" onClick={() => setRemove(current)}>删除</button></div>}</div>
    {(branchInfo.is_branch||!!branchInfo.children?.length)&&<nav className="branch-links" aria-label="对话分支">{branchInfo.is_branch&&<span className="muted">独立分支 · 保留原始历史</span>}{branchInfo.origin?<a href={`#/chat/conversation?c=${branchInfo.origin.id}`}>返回原话：{branchInfo.origin.title}</a>:branchInfo.is_branch&&<span className="muted">原对话已不可用</span>}{branchInfo.children?.map(c=><a key={c.id} href={`#/chat/conversation?c=${c.id}`}>查看分支：{c.title}</a>)}</nav>}<Notice error={error || action.error || taskError}/>{id && <ConversationPersona key={id} conversationId={id} disabled={!!active}/>}<div className="messages" aria-label="对话消息" ref={messageArea} tabIndex={-1} aria-busy={pageBusy}>{id&&<nav className="actions" aria-label="消息分页">{nextMessageCursor&&<button className="button" disabled={pageBusy} onClick={()=>navigateMessages('older')}>加载更早消息</button>}{pageDepth>0&&<><button className="button" disabled={pageBusy} onClick={()=>navigateMessages('newer')}>较新消息</button><button className="button" onClick={()=>navigateMessages('latest')}>返回最新</button><span role="status">正在浏览历史，第 {pageDepth+1} 页；最新回复可返回查看。</span></>}{pageBusy&&<span role="status">正在加载消息…</span>}{error&&<button className="button" disabled={pageBusy} onClick={()=>{setPageBusy(true);void load();}}>重试加载</button>}</nav>}{(!id||messageReady&&!error&&messages.length===0) && <div className="chat-welcome"><img src="/assets/lingxi.svg" width="64" height="64" alt=""/><h2>想从哪里开始？</h2><p className="muted">聊聊今天的想法，或一起梳理一个问题。</p><a href="#/models/providers">首次使用，先配置模型</a></div>}
    {messages.map(m => <article className={`message message-${m.role}`} key={m.id}><div className="message-heading"><strong>{m.role === 'user' ? '你' : '灵犀'}</strong><small>{date(m.created_at)}</small>{m.status !== 'completed' && <Badge value={m.status}/>} {m.inherited&&<span className="status-badge">引用原文</span>}{m.role==='user'&&!!(runs.find(r=>r.id===m.run_id)?.config.voice_input as {automatic?:boolean}|undefined)?.automatic&&<span className="status-badge">转写未核</span>}</div>{m.reply_to&&<details><summary>{m.reply_to.status==='available'?'查看引用':'引用失效'}</summary>{m.reply_to.status==='available'?<><p className="muted">{m.reply_to.role==='user'?'用户原文':'助手历史输出，不等于用户事实'} · {m.reply_to.truncated?'已截取前 1200 字符':'文字引用'} · 未自动读取附件</p><blockquote className="emotion-evidence">{m.reply_to.content}</blockquote></>:<p className="muted">原消息已变化或不可用，旧引用内容不再展示。</p>}</details>}<div className="markdown"><Markdown skipHtml components={{img: () => <span>外部图片未加载</span>, a: props => <a href={props.href} target="_blank" rel="noopener noreferrer">{props.children}</a>}}>{m.status === 'invalidated' ? '这条回复的依据已变化，请重新提问。' : ['deleted','redacted','superseded'].includes(m.status) ? '这条消息已不可用。' : m.content || (m.status==='streaming'?'正在组织回复…':'暂无文字内容。')}</Markdown></div>{m.attachments && m.attachments.length>0&&<AttachmentList items={m.attachments}/>}{m.role==='assistant'&&chatActions.filter(a=>a.run_id===m.run_id).map(a=><ChatActionCard key={a.id} action={a} reload={load}/>) }{(m.role==='user'||m.role==='assistant')&&['completed','cancelled','failed'].includes(m.status)&&<div className="actions message-actions">{m.status==='completed'&&!!m.content&&<button className="button" disabled={!!active||action.busy||voiceActive} onClick={()=>{setReply(m);setTaskAssist(false);setCollaborate(false);document.getElementById('chat-input')?.focus();}}>回复引用</button>}{m.role==='assistant'&&m.run_id&&<button className="button" onClick={()=>setContextRun(m.run_id!)}>历史依据</button>}{(m.role==='user'||!!m.run_id)&&<button className="button" disabled={!!active||action.busy} onClick={()=>setForkMessage(m)}>{m.role==='user'?'编辑消息':'重新生成'}</button>}</div>}</article>)}
    </div>{runs[0] && <div className="run-summary"><Badge value={runs[0].status}/><button className="button" onClick={()=>setContextRun(runs[0].id)}>查看依据</button><span>{String(runs[0].config.model || '')}</span>{Boolean(runs[0].config.persona) && typeof runs[0].config.persona === 'object' && <span>{String((runs[0].config.persona as {name?:string;version?:number}).name||'并肩伙伴')} · v{String((runs[0].config.persona as {version?:number}).version||0)}</span>}{runs[0].config.history_truncated === true && <span>近期原文保留；早期历史按需摘录，覆盖情况见依据</span>}{Boolean(runs[0].config.task_assist)&&typeof runs[0].config.task_assist==='object'&&<span>任务协助：{({pending:'正在理解请求',accepted:'已安排任务',needs_input:'待补充信息',not_requested:'本条正常对话',unavailable:'理解暂不可用，本条未安排任务',cancelled:'已停止安排',failed:'安排失败',invalidated:'依据已失效'} as Record<string,string>)[String((runs[0].config.task_assist as {status?:string}).status)]||'状态待确认'}</span>}{Boolean(runs[0].config.reply_to_coverage)&&<span>引用覆盖：{String((runs[0].config.reply_to_coverage as {included:number}).included)} / {String((runs[0].config.reply_to_coverage as {characters:number}).characters)} 字符{(runs[0].config.reply_to_coverage as {truncated:boolean}).truncated?' · 仅片段':''}</span>}<span>记忆：{runs[0].memory_status === 'pending' ? '待检索' : label(runs[0].memory_status)}</span>{runs[0].error && <span className="error">{runs[0].error}</span>}{runs[0].memory_refs.length > 0 && <details><summary>使用的记忆（{runs[0].memory_refs.length}）</summary>{runs[0].memory_refs.map((r, i) => <MemoryReference key={i} id={String(r.id)} />)}</details>}</div>}
    <>{id&&<VoicePanel key={id} conversationId={id} messages={latestMessages} runs={runs} actions={chatActions} reload={load} userBusy={!!input.trim()||uploading||!!reply} referenceSelected={!!reply} onSessionChange={setVoiceActive}/>}</><form className="composer" onSubmit={e => {e.preventDefault(); if (!input.trim() || (active && (reply || !isChatControl(input))) || action.busy || uploading || media.some(v=>v.status!=='ready')) return; void action.act(async () => {const conversation = id || await create(); const content = input.trim(),mediaKey=JSON.stringify(media.map(v=>v.id)),collaboration=collaborate?{share_memory:shareMemory,propose_memory:proposeMemory,allow_files:allowFiles,max_calls:maxCalls,tool_keys:toolKeys}:null,collaborationKey=JSON.stringify(collaboration),replyId=reply?.id||null; if (!pending.current || pending.current.content !== content || pending.current.id !== conversation || pending.current.mediaKey!==mediaKey || pending.current.taskAssist!==taskAssist || pending.current.collaborationKey!==collaborationKey || pending.current.replyId!==replyId) pending.current = {id:conversation, content, mediaKey, taskAssist, collaborationKey, replyId, request_id:crypto.randomUUID()}; await api<Run>('/api/chat', 'POST', {conversation_id:conversation, content, media_ids:media.map(v=>v.id), task_assist:taskAssist, collaboration, ...(replyId?{reply_to_message_id:replyId}:{}), request_id:pending.current.request_id}); pending.current = null; setReply(null); setInput(''); setMedia([]); navigateMessages('latest');});}}>{reply&&<section aria-label="当前引用"><div className="pane-toolbar"><strong>回复引用 · {reply.role==='user'?'用户原文':'助手历史'}</strong><button className="button" type="button" disabled={action.busy} onClick={()=>{setReply(null);document.getElementById('chat-input')?.focus();}}>取消引用</button></div><blockquote className="emotion-evidence">{reply.content.slice(0,1200)}</blockquote><small className="muted">{reply.content.length>1200?'仅引用前 1200 字符；':''}模型会按预算缩短引用；未自动读取历史附件；本条仅作对话，不修改设置或执行任务。</small></section>}<ChatAttachments items={media} setItems={setMedia} disabled={action.busy||!!active||taskAssist} onBusy={setUploading}/><label className="checkbox chat-assist"><input type="checkbox" checked={taskAssist} disabled={action.busy||!!active||uploading||media.length>0||!!reply} onChange={e=>{setTaskAssist(e.target.checked);setCollaborate(false);}}/>任务协助</label><label className="checkbox chat-assist"><input type="checkbox" checked={collaborate} disabled={action.busy||!!active||uploading||!!reply} onChange={e=>{setCollaborate(e.target.checked);setTaskAssist(false);}}/>助手协作</label>{collaborate&&<details className="chat-collaboration-options"><summary>协作授权</summary><fieldset disabled={action.busy||!!active}><legend>仅对本条协作生效</legend><label className="checkbox"><input type="checkbox" checked={shareMemory} onChange={e=>setShareMemory(e.target.checked)}/>共享相关记忆</label><label className="checkbox"><input type="checkbox" checked={proposeMemory} onChange={e=>setProposeMemory(e.target.checked)}/>允许候选记忆（逐项确认）</label><label className="checkbox"><input type="checkbox" checked={allowFiles} onChange={e=>setAllowFiles(e.target.checked)}/>允许资料清单</label><p className="muted">仅检索当前身份、工作区和角色允许的相关记忆；资料清单只列服务器授权目录，不读正文。</p><AgentToolScope value={toolKeys} change={setToolKeys} disabled={action.busy||!!active}/><label>调用上限<select value={maxCalls} onChange={e=>setMaxCalls(Number(e.target.value))}>{[6,12,20,30].map(n=><option value={n} key={n}>{n} 次</option>)}</select></label></fieldset></details>}<small className="muted">{media.length>0?(collaborate?'附件将按节点分配，图片需要子助手模型通过视觉验证；删除附件会使相关协作结果失效。':'附件可用于对话或助手协作；任务协助仍只接受文字。'):collaborate?'主助手自动判断直接处理或分工，最多 8 个节点、3 个并发；可能多次调用模型，建议不等于操作已执行。':taskAssist?'仅限文字，关闭后可添加附件；自动选择一项项目规划、资料清单或站内提醒，可能增加模型调用费用。':'开启后可直接用文字安排任务；当前只对话，不执行操作。'}</small><label className="sr-only" htmlFor="chat-input">发送消息</label><textarea id="chat-input" value={input} onChange={e => setInput(e.target.value)} placeholder="写下你的想法…" rows={3} maxLength={collaborate?6000:12000} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); e.currentTarget.form?.requestSubmit();}}}/><div className="composer-bottom"><small>Enter 发送 · Shift+Enter 换行</small>{active && (reply || !isChatControl(input)) ? <button className="button" type="button" disabled={action.busy} onClick={() => void action.act(async () => {await api(`/api/chat-runs/${active.id}/cancel`, 'POST'); await load();})}>停止生成</button> : <button className="button primary" disabled={action.busy || uploading || !input.trim() || media.some(v=>v.status!=='ready')}>发送消息</button>}</div></form></div>
    {forkMessage&&id&&<MessageFork message={forkMessage} conversationId={id} close={()=>setForkMessage(null)} done={conversationId=>{setForkMessage(null);location.hash=`#/chat/conversation?c=${conversationId}`;list.refresh();}}/>}
    {historyOpen&&id&&<HistoryBrowser key={id} conversationId={id} close={()=>setHistoryOpen(false)}/>}
    {contextRun&&id&&<ContextEvidence key={contextRun} runId={contextRun} conversationId={id} close={()=>setContextRun(null)}/>}
    {rename && <Modal title="重命名" close={() => setRename(null)}><form className="form-stack" onSubmit={e => {e.preventDefault(); const title = new FormData(e.currentTarget).get('title'); void action.act(async () => {await api(`/api/conversations/${rename.id}`, 'PATCH', {title, version:rename.version}); setRename(null);});}}><label>对话名称<input name="title" defaultValue={rename.title} required maxLength={200}/></label><Notice error={action.error}/><button className="button primary" disabled={action.busy}>保存名称</button></form></Modal>}
    {remove && <DeleteConversation conversation={remove} close={() => setRemove(null)} done={() => {setRemove(null); if (id === remove.id) location.hash='#/chat/conversation'; list.refresh();}}/>}
  </div>;
}
export function MessageFork({message,conversationId,close,done}:{message:Message;conversationId:string;close():void;done(id:string):void}) {
  const editing=message.role==='user';
  const [content,setContent]=useState(message.content),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef<{content:string;id:string}|null>(null),sending=useRef(false);
  async function submit(){
    if(sending.current)return;
    if(editing&&!content.trim()){setError('请填写修改后的消息');return;}
    sending.current=true;setBusy(true);setError('');
    if(!pending.current||pending.current.content!==content)pending.current={content,id:crypto.randomUUID()};
    try{
      const result=await api<{conversation_id:string;run_id:string}>(`/api/conversations/${conversationId}/branches`,'POST',{
        request_id:pending.current.id,message_id:message.id,mode:editing?'edit':'regenerate',...(editing?{content}:{})});
      done(result.conversation_id);
    }catch(e){setError((e as Error).message);}finally{sending.current=false;setBusy(false);}
  }
  return <Modal title={editing?'编辑消息':'重新生成'} close={close} dismissible={!busy}><form className="form-stack" onSubmit={e=>{e.preventDefault();void submit();}}>
    <p>将在新的对话分支中继续，原消息和后续历史保持不变。</p>
    {editing?<><label>修改内容<textarea value={content} onChange={e=>setContent(e.target.value)} required maxLength={12000} rows={6} disabled={busy}/></label><p className="muted">保留这条消息的附件；编辑不修改已有长期记忆。如需纠正事实，请到记忆列表操作。</p></>:<p className="muted">重新回答原始提问，可能产生模型费用。已提交的任务只读取真实记录，不重新安排或执行。</p>}
    <Notice error={error}/><button className="button primary" disabled={busy}>{busy?'正在创建':'创建分支'}</button>
  </form></Modal>;
}

function MemoryReference({id}: {id: string}) {const r = useResource<Memory>('/api/memories/'+id); return <p>{r.data?.content || (r.error ? '记忆已失效' : '正在读取…')}</p>;}

export function ChatActionCard({action:task,reload}:{action:ChatAction;reload():Promise<void>}) {
  const [confirm,setConfirm]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const submitting=useRef(false);
  const reminder=task.capability==='reminder.once',collaboration=task.capability==='agent.delegate',result=task.result;
  const collaborationInfo=task.collaboration as {decision?:{mode?:string;reason?:string};calls_used:number;max_calls:number;share_memory:boolean;allow_files:boolean;nodes:{key:string;title:string;status:string;assistant:string;model:string;capability:string;depends_on:string[];memory_status:string}[]}|null|undefined;
  const canCancel=reminder?task.status==='scheduled':collaboration?['queued','planning','running','summarizing'].includes(task.status):['queued','running'].includes(task.status);
  const statuses:Record<string,string>={planning:'正在分工',summarizing:'正在汇总',completed:'已形成汇总',partial:'部分完成',pending:'等待依赖',blocked:'依赖阻塞',waiting_confirmation:'等待确认',waiting_tool:'远程处理中',queued:'等待执行',running:'正在执行',succeeded:'执行完成',failed:'执行失败',cancelled:'已取消',scheduled:'等待到期',fired:'已记入站内',acknowledged:'已确认提醒',invalidated:'来源已失效'};
  async function cancel(){
    if(submitting.current)return;
    submitting.current=true;setBusy(true);setError('');
    try{await api(`/api/${reminder?'reminders':collaboration?'agent-runs':'tasks'}/${task.target_id}/cancel`,'POST',{version:task.version});setConfirm(false);await reload();}
    catch(e){setError((e as Error).message);}
    finally{submitting.current=false;setBusy(false);}
  }
  return <section className="chat-task-card" aria-label={`聊天任务：${task.title}`}><div className="pane-toolbar"><strong>{task.title}</strong><span className={`status-badge status-${task.status}`}>{statuses[task.status]||task.status}</span></div>
    {reminder&&task.due_at&&<p>{new Date(task.due_at*1000).toLocaleString('zh-CN',{timeZone:task.timezone||undefined})} · {task.timezone} · 站内提醒</p>}
    {!reminder&&['queued','running'].includes(task.status)&&<p className="muted">任务在服务器运行，关闭浏览器后仍会继续。请以执行结果为准。</p>}
    {['succeeded','completed','partial'].includes(task.status)&&<><p>{String(result.summary||result.note||'结果已核验')}</p>{task.capability==='project.plan'&&<p className="muted">仅生成计划，步骤尚未执行。</p>}
      {Array.isArray(result.steps)&&<ol>{result.steps.map((value,i)=>{const step=value as Record<string,unknown>;return <li key={i}><strong>{String(step.title||'')}</strong><p>{String(step.action||'')}</p><small className="muted">完成条件：{String(step.completion||'')}</small></li>;})}</ol>}
      {Array.isArray(result.entries)&&<details><summary>查看资料（{result.entries.length} 项）</summary><ul>{result.entries.map((value,i)=>{const entry=value as Record<string,unknown>;return <li key={i}>{String(entry.name)} · {entry.kind==='directory'?'目录':'文件'}</li>;})}</ul></details>}
      {result.truncated===true&&<p className="muted">达到目录扫描限制，结果并非完整清单。</p>}
      {Array.isArray(result.assumptions)&&result.assumptions.length>0&&<details><summary>假设与待核对项</summary><ul>{result.assumptions.map((v,i)=><li key={i}>{String(v)}</li>)}</ul></details>}
    </>}
    {collaboration&&collaborationInfo&&<div className="page-stack"><p>模型调用 {collaborationInfo.calls_used}/{collaborationInfo.max_calls} 次 · {collaborationInfo.share_memory?'已允许相关记忆':'未共享记忆'} · {collaborationInfo.allow_files?'已允许只读清单':'未允许资料读取'}</p>{collaborationInfo.decision?.mode&&<p>{collaborationInfo.decision.mode==='direct'?'直接处理':'分工协作'}：{collaborationInfo.decision.reason}</p>}{collaborationInfo.nodes.length>0&&<details><summary>分工节点（{collaborationInfo.nodes.length} 项）</summary><ol>{collaborationInfo.nodes.map(node=><li key={node.key}><div className="pane-toolbar"><strong>{node.key} · {node.title}</strong><span className="status-badge">{statuses[node.status]||node.status}</span></div><p>{node.assistant} · {node.model} · {node.capability==='reason'?'文字分析':node.capability==='plugin.call'?'远程工具':'只读清单'}</p><small>依赖：{node.depends_on.join('、')||'无，可独立处理'} · 记忆：{({ready:'检索就绪',unavailable:'暂不可用',not_allowed:'未获授权',pending:'尚未检索'} as Record<string,string>)[node.memory_status]||'状态待确认'}</small></li>)}</ol></details>}{Array.isArray(result.findings)&&result.findings.length>0&&<ul>{result.findings.map((value,i)=>{const finding=value as {text:string;evidence_ids:string[]};return <li key={i}>{finding.text}<small> · 节点：{finding.evidence_ids.join('、')}</small></li>;})}</ul>}{Array.isArray(result.limitations)&&result.limitations.length>0&&<details><summary>待核条件</summary><ul>{result.limitations.map((v,i)=><li key={i}>{String(v)}</li>)}</ul></details>}<p className="muted">仅校验结构与引用范围；远程节点请在“查看协作”核对工具和真实回执，文字建议不代表操作已执行。</p></div>}
    {task.status==='invalidated'&&<p className="muted">原始依据已删除或失效，相关结果不再展示。</p>}
    <Notice error={task.error||error}/><div className="actions">{task.status!=='invalidated'&&<a className="button" href={reminder?'#/tasks/reminders':collaboration?'#/tasks/list?mode=agents&run='+encodeURIComponent(task.target_id):'#/tasks/list'}>{reminder?'查看提醒':collaboration?'查看协作':'前往任务'}</a>}{canCancel&&<button className="button" type="button" disabled={busy} onClick={()=>setConfirm(true)}>取消任务</button>}</div>
    {confirm&&<Modal title="取消任务" dismissible={!busy} close={()=>setConfirm(false)}><p>将取消“{task.title}”。{reminder?'尚未到期的提醒将停止。':'执行器会停止后续步骤；已产生的模型费用无法撤销。'}</p><Notice error={error}/><button type="button" className="button danger" disabled={busy} onClick={()=>void cancel()}>{busy?'正在取消':'确认取消'}</button></Modal>}
  </section>;
}
type ContextData=import('./api.generated').components['schemas']['ContextView'];
type HistoryData=import('./api.generated').components['schemas']['HistoryPage'];
type OriginalData=import('./api.generated').components['schemas']['HistoryRead'];
const contextLabels:Record<string,string>={preparing:'正在压缩',ready:'摘录就绪',partial:'覆盖有限',unavailable:'压缩暂不可用',not_needed:'无需压缩',invalidated:'依据失效',cancelled:'已经停止',failed:'处理失败'};

export function HistoryReader({conversationId,messageId,start=0}:{conversationId:string;messageId:string;start?:number}) {
  const [position,setPosition]=useState(start),[data,setData]=useState<OriginalData|null>(null),[error,setError]=useState('');
  useEffect(()=>{let active=true;setData(null);setError('');const read=()=>void api<OriginalData>(`/api/conversations/${conversationId}/history/${messageId}?start=${position}&length=2000`).then(v=>{if(active){setData(v);setError('');}}).catch(e=>{if(active){setData(null);setError((e as Error).message);}});read();const timer=setInterval(read,3000);return()=>{active=false;clearInterval(timer);};},[conversationId,messageId,position]);
  return <section className="history-reader" aria-label="原文补读"><Notice error={error}/>{!data&&!error&&<p role="status">正在读取原文…</p>}{data&&<><p className="muted">{data.role==='user'?'用户原文':'助手原文'} · 字符 {data.total?data.start+1:0}–{data.end} / {data.total}</p><pre className="history-original">{data.content||'原文为空'}</pre>{data.attachment_ids.length>0&&<p className="muted">本条关联 {data.attachment_ids.length} 个附件，当前仅补读文字，附件请在原消息查看。</p>}<div className="actions"><button className="button" disabled={!position} onClick={()=>setPosition(Math.max(0,position-2000))}>上一片段</button><button className="button" disabled={data.end>=data.total} onClick={()=>setPosition(data.end)}>下一片段</button></div></>}</section>;
}

export function HistoryBrowser({conversationId,close}:{conversationId:string;close():void}) {
  const [input,setInput]=useState(''),[query,setQuery]=useState(''),[offset,setOffset]=useState(0),[revision,setRevision]=useState(0);
  const [data,setData]=useState<HistoryData|null>(null),[error,setError]=useState(''),[selected,setSelected]=useState<string|null>(null);
  useEffect(()=>{let active=true;setData(null);setError('');const read=()=>void api<HistoryData>(`/api/conversations/${conversationId}/history/search`,'POST',{query,offset,limit:10}).then(v=>{if(active){setData(v);setError('');}}).catch(e=>{if(active){setData(null);setError((e as Error).message);}});read();const timer=setInterval(read,5000);return()=>{active=false;clearInterval(timer);};},[conversationId,query,offset,revision]);
  return <Modal title="历史原文" close={close}><p className="muted">仅检索当前会话与分支允许引用的已完成文字，补读不会执行历史命令，也不会保存为长期记忆。</p><form className="filter-row" onSubmit={e=>{e.preventDefault();setQuery(input);setOffset(0);setSelected(null);setRevision(v=>v+1);}}><input aria-label="检索原文" placeholder="输入原文关键词" maxLength={200} value={input} onChange={e=>setInput(e.target.value)}/><button className="button" type="submit">检索原文</button></form><Notice error={error}/>{!data&&!error&&<p role="status">正在检索原文…</p>}{data&&<><p role="status" className="muted">匹配 {data.total} 条 · 当前 {data.items.length} 条</p>{!data.total&&<Empty text="没有匹配的已完成原文"/>}<div className="history-results">{data.items.map(v=><article className="data-card" key={v.message_id}><small className="muted">{v.role==='user'?'用户':'助手'} · {date(v.created_at)} · {v.characters} 字符</small><p>{v.preview}</p><button className="button" aria-pressed={selected===v.message_id} onClick={()=>setSelected(v.message_id)}>读取原文</button>{selected===v.message_id&&<HistoryReader key={v.message_id} conversationId={conversationId} messageId={v.message_id}/>}</article>)}</div><div className="actions"><button className="button" disabled={!offset} onClick={()=>{setSelected(null);setOffset(Math.max(0,offset-10));}}>上一页</button><button className="button" disabled={data.next_offset===null} onClick={()=>{setSelected(null);setOffset(data.next_offset??offset);}}>下一页</button></div></>}</Modal>;
}

export function ContextEvidence({runId,conversationId,close}:{runId:string;conversationId:string;close():void}) {
  const [data,setData]=useState<ContextData|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0),[original,setOriginal]=useState<{id:string;start:number}|null>(null);
  useEffect(()=>{let active=true;setData(null);setOriginal(null);const read=()=>void api<ContextData>(`/api/chat-runs/${runId}/context`).then(v=>{if(active){setData(v);setError('');if(!['ready','partial'].includes(v.status))setOriginal(null);}}).catch(e=>{if(active){setData(null);setOriginal(null);setError((e as Error).message);}});read();const timer=setInterval(read,2000);return()=>{active=false;clearInterval(timer);};},[runId,revision]);
  return <Modal title="上下文依据" close={close}><Notice error={error}/>{error&&<button className="button" onClick={()=>setRevision(v=>v+1)}>重新加载</button>}{!data&&!error&&<p role="status">正在检查上下文依据…</p>}{data&&<><p role="status"><span className="status-badge">{contextLabels[data.status]||data.status}</span></p><p className="muted">{data.notice}</p><p className="muted">长对话按需额外调用一次当前对话模型，会增加延迟和用量；实际记录见“调用记录”。</p>{data.counts&&'older' in data.counts&&<p className="muted">近期原文 {data.counts.recent} 条 · 早期 {data.counts.older} 条 · 提供候选 {data.counts.examined} 条（片段 {data.counts.partial_sources} 条）· 摘录 {data.counts.quoted} 段 · 自动补读 {data.counts.reread} 段</p>}{data.status==='unavailable'&&<p>本轮未能补充早期上下文，仍可查看近期回复与检索原文。</p>}{[...(data.quotes||[]),...(data.reads||[])].map((v,i)=><article className="data-card" key={`${v.message_id}-${i}`}><small className="muted">{v.role==='user'?'用户原文':'助手原文'}{v.start!==null&&v.start!==undefined?` · 字符 ${v.start+1}–${v.end} / ${v.total}`:' · 精确摘录'}</small><pre className="history-original">{v.content}</pre><button className="button" onClick={()=>setOriginal({id:v.message_id,start:v.start??0})}>读取原文</button></article>)}{original&&<HistoryReader key={`${original.id}-${original.start}`} conversationId={conversationId} messageId={original.id} start={original.start}/>}</>}</Modal>;
}


function DeleteConversation({conversation, close, done}: {conversation: Conversation; close(): void; done(): void}) {
  const linked = useResource<Memory[]>(`/api/conversations/${conversation.id}/memories`), [all, setAll] = useState(false), action = useAction();
  return <Modal title="删除对话" close={close}><p>将删除“{conversation.title}”及尚未提交的记忆候选。</p><p>关联长期记忆：{linked.data?.length ?? '正在查询'} 条</p>{linked.data?.map(m => <p key={m.id} className="muted">{m.content}</p>)}<label className="checkbox"><input type="checkbox" checked={all} onChange={e => setAll(e.target.checked)}/>同时删除关联长期记忆</label><Notice error={linked.error || action.error}/><button className="button danger" disabled={action.busy || !linked.data} onClick={() => void action.act(async () => {await api(`/api/conversations/${conversation.id}?delete_memories=${all}`, 'DELETE'); done();})}>确认删除</button></Modal>;
}

type Policy = ModelPolicy;
export function ModelsPage({page}: {page: string}) {
  return page==='calls'?<ModelCalls/>:<ModelConfiguration page={page}/>;
}
function ModelConfiguration({page}: {page: string}) {
  const providers = useResource<Provider[]>('/api/providers'), models = useResource<Model[]>('/api/models'), policies = useResource<Policy[]>('/api/model-policies');
  const refresh = () => {providers.refresh(); models.refresh(); policies.refresh();};
  const [testing, setTesting] = useState<string | null>(null);
  const action = useAction(refresh), [providerModal, setProviderModal] = useState<Provider | 'new' | null>(null), [modelModal, setModelModal] = useState(false);
  return <div className="page-stack"><div className="pane-toolbar"><h2>{{providers:'服务连接', catalog:'模型目录', policies:'路由策略', calls:'调用记录'}[page]}</h2><div className="actions"><button className="button" onClick={refresh}>刷新状态</button>{page === 'providers' && <button className="button primary" onClick={() => setProviderModal('new')}>添加连接</button>}{page === 'catalog' && <button className="button primary" disabled={!providers.data?.length} onClick={() => setModelModal(true)}>添加模型</button>}</div></div><Notice error={action.error || providers.error || models.error || policies.error}/>
    {page === 'providers' && <><p className="muted">支持 OpenAI 兼容、Anthropic 与 Gemini 原生协议；密钥仅保存在服务器加密凭据库。</p><div className="card-grid">{providers.data?.map(p => <div className="data-card" key={p.id}><div className="pane-toolbar"><h3>{p.name}</h3><Badge value={p.enabled ? 'active' : 'unavailable'}/></div><p className="wrap">{p.base_url}</p><p className="muted">{protocolLabels[p.protocol||'openai']} · 密钥 {p.key_mask} · 配置 v{p.version}</p><p className="muted">{p.allow_private ? '已授权内网连接' : '仅公网 HTTPS'}</p><button className="button" onClick={() => setProviderModal(p)}>编辑连接</button></div>)}</div>{providers.data?.length === 0 && <Empty text="添加第一个模型服务连接"/>}</>}
    {page === 'catalog' && <><p className="muted">对话验证真实流式输出，抽取验证中文原文证据与 JSON，嵌入验证维度和数值，重排验证文档序号与分数。验证通过不代表中文质量达标。</p><div className="table-scroll"><table><thead><tr><th>模型名称</th><th>服务连接</th><th>用途</th><th>状态</th><th>操作</th></tr></thead><tbody>{models.data?.map(m => <tr key={m.id}><td>{m.name}<small className="block muted">上下文 {m.context_window}</small></td><td>{providers.data?.find(p => p.id === m.provider_id)?.name}</td><td>{purposes[m.purpose]}</td><td><Badge value={m.status}/></td><td><button className="button" disabled={action.busy} onClick={() => void action.act(async () => {setTesting(m.id); try {await api(`/api/models/${m.id}/test`, 'POST');} finally {setTesting(null); refresh();}})}>{testing === m.id ? '正在验证' : '验证能力'}</button>{m.purpose==='chat'&&<VisionControl modelId={m.id} enabled={m.status==='verified'}/>}</td></tr>)}</tbody></table></div>{models.data?.length === 0 && <Empty text="先添加模型，再按用途验证能力"/>}</>}
    {page === 'policies' && <>{providers.data && models.data && policies.data ? <ModelSetup providers={providers.data} models={models.data} policies={policies.data}/> : <p role="status" className="muted">正在检查模型配置…</p>}<p className="muted">默认使用固定模型。普通聊天可在下方明确开启自动选模与受控降级，其他用途保持固定配置。</p><div className="card-grid">{Object.entries(purposes).map(([purpose, title]) => {const policy = policies.data?.find(p => p.purpose === purpose); return <form className="data-card form-stack" key={purpose} onSubmit={e => {e.preventDefault(); const model_id = new FormData(e.currentTarget).get('model_id'); void action.act(() => api(`/api/model-policies/${purpose}`, 'PUT', {model_id, version:policy?.version || 0}));}}><h3>{title}</h3><label>固定模型<select key={[policy?.model_id || purpose,...(models.data || []).map(m=>m.id+':'+m.status),...(providers.data || []).map(p=>p.id+':'+p.enabled)].join('|')} name="model_id" defaultValue={policy?.model_id || ''} required><option value="">请选择已验证模型</option>{models.data?.filter(m => m.purpose === purpose && m.status === 'verified' && providers.data?.some(p => p.id === m.provider_id && p.enabled)).map(m => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label><button className="button" disabled={action.busy}>保存配置</button></form>;})}</div><p className="muted">长期记忆共用这里的抽取、嵌入与重排配置。已有记忆时更换嵌入模型需停写并由管理员重建索引；重复保存同一模型不会触发重建。</p><RoutingControls models={models.data || []} providers={providers.data || []} primary={policies.data?.find(p=>p.purpose==='chat')} request={api}/></>}
    {providerModal && <Modal title={providerModal === 'new' ? '添加连接' : '编辑连接'} dismissible={!action.busy} close={() => setProviderModal(null)}><ProviderForm provider={providerModal} busy={action.busy} error={action.error} save={body=>action.act(async()=>{await api(providerModal==='new'?'/api/providers':'/api/providers/'+providerModal.id,providerModal==='new'?'POST':'PATCH',body);setProviderModal(null);})}/></Modal>}
    {modelModal && <Modal title="添加模型" close={() => setModelModal(false)}><AddModelForm providers={providers.data || []} busy={action.busy} error={action.error} save={body => action.act(async () => {await api('/api/models', 'POST', body); setModelModal(false);})}/></Modal>}
  </div>;
}

type ProviderProtocol=NonNullable<Provider['protocol']>;
const protocolLabels:Record<ProviderProtocol,string>={openai:'OpenAI 兼容',anthropic:'Anthropic 原生',gemini:'Gemini 原生'};
const providerRoots:Record<ProviderProtocol,string>={openai:'',anthropic:'https://api.anthropic.com/v1',gemini:'https://generativelanguage.googleapis.com/v1beta'};
export function ProviderForm({provider,busy,error,save}:{provider:Provider|'new';busy:boolean;error:string;save(body:ProviderInput):Promise<unknown>}) {
  const original=provider==='new'?'openai':provider.protocol||'openai';
  const [protocol,setProtocol]=useState<ProviderProtocol>(original),[base,setBase]=useState(provider==='new'?'':provider.base_url);
  const changed=provider!=='new'&&protocol!==original;
  return <form className="form-stack" onSubmit={e=>{e.preventDefault();if(busy)return;const f=new FormData(e.currentTarget);void save({name:String(f.get('name')),protocol,base_url:base,api_key:String(f.get('api_key')),allow_private:f.has('allow_private'),enabled:f.has('enabled'),version:provider==='new'?null:provider.version});}}>
    <label>连接名称<input name="name" defaultValue={provider==='new'?'':provider.name} maxLength={100} disabled={busy} required/></label>
    <label>服务协议<select name="protocol" value={protocol} disabled={busy} onChange={e=>{const value=e.target.value as ProviderProtocol;setProtocol(value);if(!base||Object.values(providerRoots).includes(base))setBase(providerRoots[value]);}}>{Object.entries(protocolLabels).map(([key,title])=><option key={key} value={key}>{title}</option>)}</select></label>
    <label>接口根地址<input name="base_url" type="url" value={base} onChange={e=>setBase(e.target.value)} aria-describedby="provider-base-help" disabled={busy} placeholder={providerRoots[protocol]||'https://example.com/v1'} required/><small id="provider-base-help" className="muted">填写完整 API 根路径，不包含具体操作接口。{protocol==='anthropic'?'官方通常以 /v1 结尾，使用 Messages 协议。':protocol==='gemini'?'使用 Gemini API 的 /v1beta 等根路径，当前不接 Vertex AI。':'不会自动补 /v1，兼容服务须按实际根路径填写。'}模型从所选服务自动加载，仍需用途验证。</small></label>
    <label>API 密钥<input name="api_key" type="password" autoComplete="new-password" maxLength={2000} disabled={busy} required={provider==='new'||changed} placeholder={changed?'切换协议须填写对应新密钥':provider==='new'?'填写密钥':'留空保留原密钥'}/></label>
    <p className="muted">{protocol==='anthropic'?'当前接入文字、图片和记忆抽取；嵌入、重排和语音请选择其他连接。':protocol==='gemini'?'当前接入文字、图片、记忆抽取及嵌入；重排和语音请选择其他连接。':'各用途独立验证，不因兼容协议而假定全部能力可用。'}修改连接会使旧验证失效；已有嵌入索引的连接不可直接更换地址或协议。</p>
    <label className="checkbox"><input name="allow_private" type="checkbox" defaultChecked={provider!=='new'&&provider.allow_private} disabled={busy}/>明确允许连接服务器内网地址</label>
    <label className="checkbox"><input name="enabled" type="checkbox" defaultChecked={provider==='new'||provider.enabled} disabled={busy}/>启用连接</label>
    <Notice error={error}/><button className="button primary" disabled={busy}>{busy?'正在保存':'保存连接'}</button>
  </form>;
}

export function MemoryPage({page, session, onSession}: {page: string; session: Session; onSession(s: Session): void}) {
  const [q,setQ] = useState(''), [category,setCategory] = useState(''),[candidateStatus,setCandidateStatus]=useState('all');
  const url=page==='list'?`/api/memory-pages?q=${encodeURIComponent(q)}&category=${category}`:page==='candidates'?`/api/memory-candidate-pages?status=${candidateStatus}`:`/api/memory-operation-pages?kind=${page==='exports'?'export':'delete'}`;
  const records=useMemoryRecords<Memory|Candidate|Operation>(url);
  const list=page==='list'?records.data?.items as Memory[]|undefined:undefined;
  const candidates=page==='candidates'?records.data?.items as Candidate[]|undefined:undefined;
  const ops=['exports','deletions'].includes(page)?records.data?.items as Operation[]|undefined:undefined;
  const [reviewCandidate,setReviewCandidate]=useState<Candidate|null>(null);
  const refresh=records.refresh;
  const action=useAction(refresh), [editing,setEditing] = useState<Memory | 'new' | null>(null), [deleting,setDeleting] = useState<Memory | null>(null), [source,setSource] = useState<Memory | null>(null);
  useEffect(() => {const t=setInterval(() => {if(document.visibilityState==='visible')refresh();}, 5000); return () => clearInterval(t);}, [refresh]);
  return <div className="page-stack"><div className="pane-toolbar"><h2>{{list:'记忆列表',candidates:'候选记忆',exports:'导出记录',deletions:'删除进度'}[page]}</h2><div className="actions"><button className="button" disabled={records.busy} onClick={records.latest}>刷新状态</button>{page==='list' && <button className="button primary" onClick={()=>setEditing('new')}>新增记忆</button>}{page==='exports' && <><button className="button" disabled={action.busy} onClick={()=>void action.act(()=>api('/api/memory-exports','POST',{format:'markdown'}))}>导出 MD</button><button className="button" disabled={action.busy} onClick={()=>void action.act(()=>api('/api/memory-exports','POST',{format:'json'}))}>导出 JSON</button></>}</div></div><Notice error={action.error || records.error}/>
    {page==='list' && <><div className="filter-row"><input aria-label="搜索记忆" placeholder="搜索已保存的记忆" value={q} onChange={e=>{setQ(e.target.value);}}/><select aria-label="记忆类别" value={category} onChange={e=>{setCategory(e.target.value);}}><option value="">全部类别</option>{Object.entries(categories).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></div><div className="card-grid">{list?.map(m=><article className="data-card" key={m.id}><div className="pane-toolbar"><span className="muted">{categories[m.category]} · v{m.version}</span><Badge value={m.index_status}/></div><p className="memory-content">{m.content}</p><small className="muted">主体：{m.subject === 'self' ? '本人' : m.subject} · {date(m.created_at)}</small><MemoryTimeDetails value={m}/><div className="actions"><button className="button" onClick={()=>setSource(m)}>查看来源</button><button className="button" onClick={()=>setEditing(m)}>纠正记忆</button><button className="button danger" onClick={()=>setDeleting(m)}>删除记忆</button>{m.index_status==='failed' && <button className="button" disabled={action.busy} onClick={()=>void action.act(()=>api(`/api/memories/${m.id}/retry`,'POST'))}>重试索引</button>}</div></article>)}</div>{list?.length===0 && <Empty text="尚无匹配记忆。可以手动新增，或在对话后查看候选。"/>}</>}
    {reviewCandidate&&(reviewCandidate.origin?.kind==='agent'?<AgentCandidateReview candidate={reviewCandidate} close={()=>setReviewCandidate(null)} done={()=>{setReviewCandidate(null);refresh();}}/>:<DialogueCandidateReview candidate={reviewCandidate} close={()=>setReviewCandidate(null)} done={()=>{setReviewCandidate(null);refresh();}}/>)}{page==='candidates' && <><label className="checkbox"><input type="checkbox" checked={session.auto_memory} disabled={action.busy} onChange={e=>{const value=e.target.checked; void action.act(async()=>{onSession(await api<Session>('/api/auth/preferences','PATCH',{auto_memory:value}));});}}/>自动保存明确、无冲突的低风险自述</label><p className="muted">推测、敏感内容和可能的冲突需手动核对；助手生成的回答不会直接成为个人事实。</p><label>候选状态<select value={candidateStatus} onChange={e=>setCandidateStatus(e.target.value)}><option value="all">全部状态</option><option value="pending">等待核对</option><option value="confirmed">已经保存</option><option value="rejected">已经拒绝</option></select></label><div className="card-grid">{candidates?.map(c=><article className="data-card" key={c.id}><div className="pane-toolbar"><span className="muted">{categories[c.category]} · {c.subject}</span><Badge value={c.status}/></div><p>{c.content}</p><p className="muted">{c.reason}</p><small className="muted">{c.origin?.kind==='agent'?'子助手提议 · '+String(c.origin.agent_name||'协作节点'):'来源消息：'+c.source_id}</small>{c.status==='pending' && <div className="actions"><button className="button primary" disabled={action.busy} onClick={()=>setReviewCandidate(c)}>核对保存</button><button className="button" disabled={action.busy} onClick={()=>void action.act(()=>api(`/api/memory-candidates/${c.id}/actions`,'POST',{action:'reject'}))}>拒绝保存</button></div>}</article>)}</div>{candidates?.length===0 && <Empty text="尚无符合当前状态的候选记忆。"/>}</>}
    {['exports','deletions'].includes(page) && <><p className="muted">{page==='deletions' ? '删除请求提交后立即停止检索，后台继续清理。备份恢复前须重放独立删除记录。' : '导出下载时按最新删除状态生成，避免旧导出重新暴露已删除内容。'}</p>{ops?.map(o=><article className="data-card" key={o.id}><div className="pane-toolbar"><strong>{date(o.created_at)}</strong><Badge value={o.status}/></div><small className="muted">操作 {o.id}</small><Notice error={o.error || undefined}/>{o.status==='completed' && page==='exports' && <button className="button" onClick={()=>download(`/api/memory-exports/${o.id}/download`)}>下载文件</button>}{o.status==='failed' && page==='deletions' && <button className="button" disabled={action.busy} onClick={()=>void action.act(()=>api(`/api/memory-operations/${o.id}/retry`,'POST'))}>重试清理</button>}{page==='deletions' && o.status==='completed' && <p className="muted">在线清理完成；离线备份按部署保留策略到期。</p>}</article>)}{ops?.length===0 && <Empty/>}</>}
    <MemoryPager records={records}/>
    {editing && <MemoryEditor memory={editing} close={()=>setEditing(null)} done={()=>{setEditing(null);refresh();}}/>}
    {deleting && <Modal title="删除记忆" close={()=>setDeleting(null)}><p>{deleting.content}</p><p className="muted">将立即停止使用，并清理索引、历史修订及关联回复。对应的原始用户消息也会清除，避免再次抽取。</p><Notice error={action.error}/><button className="button danger" disabled={action.busy} onClick={()=>void action.act(async()=>{await api(`/api/memories/${deleting.id}`,'DELETE');setDeleting(null);})}>确认删除</button></Modal>}
    {source && <Sources memory={source} close={()=>setSource(null)}/>}
  </div>;
}
function Sources({memory,close}:{memory:Memory;close():void}) {
  const sources=useResource<{id:string;content:string;role:string}[]>(`/api/memories/${memory.id}/sources`);
  return <Modal title="记忆来源" close={close}><Notice error={sources.error}/>{sources.data?.map(s=><div className="data-card" key={s.id}><small className="muted wrap">{s.id}</small><p>{s.content}</p></div>)}<MemoryTimeDetails value={memory}/>{memory.revisions.length>0 && <details><summary>历史修订（{memory.revisions.length}）</summary>{memory.revisions.map((r,i)=><div className="revision-record" key={i}><p>v{String(r.version)} · {String(r.content)}</p>{typeof r.subject==='string'&&<p>主体：{r.subject==='self'?'本人':r.subject} · {typeof r.category==='string'?(categories[r.category]||r.category):'未记录类别'}</p>}<MemoryTimeDetails value={r}/></div>)}</details>}</Modal>;
}


export function MemoryEditor({memory,close,done}:{memory:Memory|'new';close():void;done():void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(false),help=useId();
  const errorNotice=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{if(error)errorNotice.current?.focus();},[error]);
  async function save(event:React.FormEvent<HTMLFormElement>){
    event.preventDefault();if(pending.current)return;
    const form=new FormData(event.currentTarget);
    const body:Record<string,unknown>={content:String(form.get('content')),category:String(form.get('category')),subject:String(form.get('subject')),version:memory==='new'?1:memory.version};
    for(const [key] of memoryTimeFields){
      // Preserve legacy values exactly unless the user actually edits that field.
      const raw=String(form.get(key)||'');
      if(memory!=='new'&&raw===(memory[key]??''))continue;
      body[key]=raw.trim()||null;
    }
    pending.current=true;setBusy(true);setError('');
    try{await api(memory==='new'?'/api/memories':'/api/memories/'+memory.id,memory==='new'?'POST':'PATCH',body);done();}
    catch(e){setError((e as Error).message+(e instanceof Error&&e.message.includes('422')?'；请核对时间格式、日期和先后顺序。':''));}
    finally{pending.current=false;setBusy(false);}
  }
  return <Modal title={memory==='new'?'新增记忆':'纠正记忆'} close={close} dismissible={!busy}><form className="form-stack" onSubmit={e=>void save(e)}>
    <label>记忆内容<textarea name="content" defaultValue={memory==='new'?'':memory.content} maxLength={4000} required rows={5} disabled={busy}/></label>
    <label>所属类别<select name="category" defaultValue={memory==='new'?'preferences':memory.category} disabled={busy}>{Object.entries(categories).map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></label>
    <label>事实主体<input name="subject" defaultValue={memory==='new'?'self':memory.subject} maxLength={150} required disabled={busy}/><small>self 表示本人；他人的经历请填写真实主体。</small></label>
    <fieldset disabled={busy}><legend>时间依据</legend><p id={help} className="muted">选填日期（如 2026-11-01），或带时区的时间（如 2026-11-01T09:00:00+08:00）。留空表示未说明。生效与结束描述事实适用期间，不会自动删除或覆盖其他记忆；不确定的日期不要猜测。</p><div className="form-stack">{memoryTimeFields.map(([key,name])=><label key={key}>{name}<input name={key} type="text" defaultValue={memory==='new'?'':memory[key]||''} maxLength={100} aria-describedby={help} placeholder="YYYY-MM-DD" autoComplete="off"/></label>)}</div></fieldset>
    {error&&<p ref={errorNotice} tabIndex={-1} role="alert" className="error">{error}</p>}<div className="actions"><button className="button" type="button" disabled={busy} onClick={close}>取消编辑</button><button className="button primary" disabled={busy}>{busy?'正在保存':'保存记忆'}</button></div>
  </form></Modal>;
}
