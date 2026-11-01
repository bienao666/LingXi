import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const[name,value]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${name}'`,`from '${value}'`).replaceAll(`from "${name}"`,`from "${value}"`);return url(code);}
const window=new Window({url:'http://localhost/#/chat/conversation?c=conversation-one'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,location:window.location,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api}),setup=compile('../src/ModelSetup.tsx');
const {ChatPage,ChatActionCard,MessageFork,HistoryBrowser,ContextEvidence,AgentCandidateReview}=await import(compile('../src/pages.tsx',{'./AgentTools':compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui}),
 './api':api,'./ModelSetup':setup,'./AddModelForm':compile('../src/AddModelForm.tsx',{'./api':api,'./ModelSetup':setup}),
 './ModelCalls':compile('../src/ModelCalls.tsx',{'./api':api,'./ui':ui}),'./ConversationPersona':compile('../src/ConversationPersona.tsx',{'./api':api,'./ui':ui}),
 './VoicePages':compile('../src/VoicePages.tsx',{'./VoiceRealtime':compile('../src/VoiceRealtime.tsx',{'./api':api,'./ui':ui,'./voiceAudio':compile('../src/voiceAudio.ts')}),'./api':api,'./ui':ui,'./voiceAudio':compile('../src/voiceAudio.ts')}),
 './MediaPages':compile('../src/MediaPages.tsx',{'./api':api,'./ui':ui}),
 './MemoryPaging':compile('../src/MemoryPaging.tsx',{'./api':api}),
  'react-markdown':pathToFileURL(require.resolve('react-markdown')).href
}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const task={id:'action-one',run_id:'run-one',capability:'project.plan',target_id:'task-one',title:'演示计划',status:'queued',version:3,result:{},error:null};
const conversation={id:'conversation-one',title:'日常对话',version:1,created_at:1};
const defaults=uri=>uri.startsWith('/api/conversation-pages?')?{items:[conversation],next_cursor:null}:uri==='/api/conversations/conversation-one'?conversation:uri.startsWith('/api/message-pages?')?{items:[],next_cursor:null}:uri.endsWith('/persona')?{version:1,persona_id:null}:uri.startsWith('/api/voice/config')?{ready:false,asr:null,tts:null}:[];

const agentCandidate={id:'candidate-one',content:'<img src=x onerror=alert(1)> 项目目标',category:'projects',subject:'project:lingxi',reason:'目标需核对',origin:{kind:'agent',run_id:'agent-run',node_key:'n0',agent_name:'资料研究',model:'fixture',plugin_id:'sample.role',plugin_version:'1.2.0',citations:[{id:'task-goal',kind:'task_goal',content:'优先做好项目记忆'}]}};
async function mountCandidate(t,handler,candidate=agentCandidate){
 document.body.innerHTML='<div id="root"></div>';const root=createRoot(document.getElementById('root'));globalThis.fetch=async(uri,o)=>handler(uri,o);let done=0;
 t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
 await act(async()=>root.render(createElement(AgentCandidateReview,{candidate,close(){},done(){done++;}})));return()=>done;
}
test('agent memory requires source review and an explicit checkbox, renders untrusted text safely',async t=>{
 const writes=[];const done=await mountCandidate(t,(uri,o)=>o.method==='POST'?(writes.push(JSON.parse(o.body)),response({status:'confirmed'})):response({candidate:agentCandidate,review_digest:'a'.repeat(64),warning:'仅为未核验提议'}));
 const save=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认保存');
 assert.equal(save.disabled,true);assert.equal(document.querySelector('img'),null);assert.match(document.querySelector('dialog').textContent,/优先做好项目记忆/);assert.equal(writes.length,0);
 await act(async()=>document.querySelector('input[type=checkbox]').click());await click('确认保存');
 assert.deepEqual(writes,[{action:'confirm',review_digest:'a'.repeat(64)}]);assert.equal(done(),1);
});
test('invalidated memory proposal stays on the review page and reports real error',async t=>{
 const done=await mountCandidate(t,(uri,o)=>o.method==='POST'?response({detail:'候选来源已删除'},410):response({candidate:agentCandidate,review_digest:'b'.repeat(64),warning:'需确认'}));
 await act(async()=>document.querySelector('input[type=checkbox]').click());await click('确认保存');
 assert.equal(done(),0);assert.match(document.querySelector('[role=alert]').textContent,/候选来源已删除/);
});
test('failed source loading cannot save; unknown subject cannot be adopted',async t=>{
 let writes=0;await mountCandidate(t,(uri,o)=>{if(o.method==='POST')writes++;return response({detail:'来源失效'},410);});
 assert.match(document.querySelector('[role=alert]').textContent,/来源失效/);assert.equal(document.querySelector('input[type=checkbox]'),null);assert.equal(writes,0);
});
test('proposal review does not submit twice while saving',async t=>{
 let resolve,writes=0;await mountCandidate(t,(uri,o)=>o.method==='POST'?(writes++,new Promise(r=>resolve=r)):response({candidate:agentCandidate,review_digest:'c'.repeat(64),warning:'需核对'}));
 await act(async()=>document.querySelector('input[type=checkbox]').click());await click('确认保存');
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='正在保存').disabled,true);assert.equal(writes,1);
 await act(async()=>resolve(response({status:'confirmed'})));
});

test('explicit support setting remains sendable during generation without submitting a second ordinary prompt',async t=>{
 const previous=globalThis.EventSource;
 globalThis.EventSource=class {addEventListener(){}close(){}};
 t.after(()=>{globalThis.EventSource=previous;});
 const writes=[];
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{
  if(u==='/api/chat'){writes.push(JSON.parse(o.body));return response({id:'setting',status:'completed'});}
  if(u.startsWith('/api/chat-runs?'))return response([{id:'running',status:'running',config:{},memory_refs:[]}]);
  return response(defaults(u));
 });
 await input('关闭情绪适配。');await click('发送消息');
 assert.equal(writes.length,1);assert.equal(writes[0].content,'关闭情绪适配。');
 await input('请解释“关闭情绪适配”');
 assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='停止生成'));
 assert.equal([...document.querySelectorAll('button')].some(b=>b.textContent==='发送消息'),false);
});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>u==='/api/media-policy'?response({version:1,max_files:10,max_file_mib:20,max_total_mib:50,allowed_types:['text','markdown','csv','pdf','docx','xlsx','image','qq_audio']}):handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));return root;}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function input(text){const el=document.querySelector('#chat-input');await act(async()=>{const setter=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;setter.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}

test('chat dispatch is opt-in and retry preserves the mode and idempotency key',async t=>{
 const requests=[];
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{if(u==='/api/chat'){requests.push(JSON.parse(o.body));return response({detail:'连接暂不可用'},503);}return response(defaults(u));});
 const toggle=document.querySelector('.chat-assist input');assert.equal(toggle.checked,false);
 await act(async()=>toggle.click());assert.equal(toggle.checked,true);
 assert.equal([...document.querySelectorAll('button')].find(v=>v.textContent==='选择文件').disabled,true);
 await input('帮我规划一次演示');await click('发送消息');await click('发送消息');
 assert.equal(requests.length,2);assert.equal(requests[0].task_assist,true);assert.equal(requests[0].request_id,requests[1].request_id);
 assert.match(document.body.textContent,/连接暂不可用/);
 await act(async()=>toggle.click());await click('发送消息');
 assert.equal(requests[2].task_assist,false);assert.notEqual(requests[2].request_id,requests[1].request_id);
});

test('task card cancellation names the task and sends the actual version once',async t=>{
 const writes=[];let finish;
 await mount(t,ChatActionCard,{action:task,reload:async()=>{}},(u,o)=>{writes.push({u,body:JSON.parse(o.body)});return new Promise(r=>finish=r);});
 await click('取消任务');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/演示计划.*费用/);
 await click('确认取消');await click('正在取消');assert.equal(writes.length,1);assert.deepEqual(writes[0],{u:'/api/tasks/task-one/cancel',body:{version:3}});
 await act(async()=>finish(response({detail:'任务已变化，请重新加载'},409)));
 assert.match(document.querySelector('dialog').textContent,/任务已变化/);
});

test('completed plan stays a plan and untrusted strings never execute HTML',async t=>{
 await mount(t,ChatActionCard,{action:{...task,status:'succeeded',result:{summary:'<img src=x onerror=alert(1)>',steps:[{title:'<script>evil()</script>',action:'整理要点',completion:'得到提纲'}]}},reload:async()=>{}},()=>response({}));
 assert.match(document.body.textContent,/步骤尚未执行/);assert.equal(document.querySelector('script,img'),null);assert.match(document.body.textContent,/完成条件：得到提纲/);assert.equal([...document.querySelectorAll('button')].some(v=>v.textContent==='取消任务'),false);
});

test('invalidated task has no old result link or cancellation entry',async t=>{
 await mount(t,ChatActionCard,{action:{...task,title:'来源已失效',status:'invalidated',result:{}},reload:async()=>{}},()=>response({}));
 assert.match(document.body.textContent,/不再展示/);assert.equal(document.querySelector('a,button'),null);
});

test('reminder card describes station record without claiming push delivery',async t=>{
 await mount(t,ChatActionCard,{action:{...task,capability:'reminder.once',status:'fired',due_at:1793307600,timezone:'Asia/Shanghai'},reload:async()=>{}},()=>response({}));
 assert.match(document.body.textContent,/已记入站内/);assert.match(document.body.textContent,/Asia\/Shanghai/);assert.doesNotMatch(document.body.textContent,/已送达|已读|已推送/);
});

test('task query failure is shown and never replaced with fabricated success',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u)=>u.startsWith('/api/chat-actions')?response({detail:'任务状态暂不可用'},503):response(u.startsWith('/api/message-pages')?{items:[{id:'message-one',role:'assistant',content:'正常文字入口仍然可用',status:'completed',created_at:1,run_id:'old-run'}],next_cursor:null}:defaults(u)));
 assert.match(document.querySelector('[role=alert]').textContent,/任务状态暂不可用/);assert.equal(document.querySelector('.chat-task-card'),null);
 assert.match(document.body.textContent,/正常文字入口仍然可用/);
});

test('collaboration has explicit independent scopes and retry preserves them',async t=>{
 const requests=[];
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{if(u==='/api/chat'){requests.push(JSON.parse(o.body));return response({detail:'连接暂不可用'},503);}return response(defaults(u));});
 const labelInput=text=>[...document.querySelectorAll('label')].find(v=>v.textContent===text)?.querySelector('input');
 assert.equal(labelInput('助手协作').checked,false);
 await act(async()=>labelInput('助手协作').click());
 assert.equal(labelInput('任务协助').checked,false);
 assert.equal(labelInput('共享相关记忆').checked,false);assert.equal(labelInput('允许资料清单').checked,false);
 await act(async()=>labelInput('共享相关记忆').click());
 await input('分析演示目标和风险');await click('发送消息');await click('发送消息');
 assert.equal(requests.length,2);assert.equal(requests[0].task_assist,false);
 assert.deepEqual(requests[0].collaboration,{share_memory:true,propose_memory:false,allow_files:false,max_calls:12,tool_keys:[]});
 assert.equal(requests[0].request_id,requests[1].request_id);
 await act(async()=>labelInput('允许资料清单').click());await click('发送消息');
 assert.equal(requests[2].collaboration.allow_files,true);assert.notEqual(requests[2].request_id,requests[1].request_id);
 await act(async()=>labelInput('任务协助').click());
 assert.equal(labelInput('助手协作').checked,false);
 await click('发送消息');assert.equal(requests[3].collaboration,null);assert.equal(requests[3].task_assist,true);
});

const collaboration={decision:{mode:'delegate',reason:'两项可独立分析'},calls_used:4,max_calls:12,share_memory:false,allow_files:false,
 nodes:[{key:'n0',title:'范围分析',status:'succeeded',assistant:'资料研究',model:'fixture',capability:'reason',depends_on:[],memory_status:'not_allowed'},
 {key:'n1',title:'风险分析',status:'running',assistant:'项目规划',model:'fixture',capability:'reason',depends_on:['n0'],memory_status:'not_allowed'}]};

test('collaboration card shows actual nodes, grants, usage and safe result provenance',async t=>{
 await mount(t,ChatActionCard,{action:{...task,capability:'agent.delegate',status:'partial',collaboration,
  result:{summary:'<script>unsafe</script>',findings:[{text:'建议核对范围',evidence_ids:['n0']}],limitations:['未联网']}},reload:async()=>{}},()=>response({}));
 assert.match(document.body.textContent,/部分完成/);assert.match(document.body.textContent,/4\/12/);
 assert.match(document.body.textContent,/未共享记忆/);assert.match(document.body.textContent,/未允许资料读取/);
 assert.match(document.body.textContent,/风险分析/);assert.match(document.body.textContent,/依赖：n0/);
 assert.match(document.body.textContent,/节点：n0/);assert.equal(document.querySelector('script'),null);
 assert.equal(document.querySelector('a').getAttribute('href'),'#/tasks/list?mode=agents&run=task-one');
});

test('in-flight collaboration cancellation uses its own endpoint and actual version',async t=>{
 const writes=[];
 await mount(t,ChatActionCard,{action:{...task,capability:'agent.delegate',status:'summarizing',collaboration},reload:async()=>{}},(u,o)=>{writes.push({u,body:JSON.parse(o.body)});return response({});});
 await click('取消任务');assert.equal(writes.length,0);await click('确认取消');
 assert.deepEqual(writes,[{u:'/api/agent-runs/task-one/cancel',body:{version:3}}]);
});

const forkMessage={id:'message-one',role:'user',content:'原始问题',status:'completed',run_id:'run-one',created_at:1,attachments:[]};
test('editing opens a branch only after explicit submit and keeps retry id',async t=>{
 const requests=[],dest=[];
 await mount(t,MessageFork,{message:forkMessage,conversationId:'conversation-one',close:()=>{},done:id=>dest.push(id)},(u,o)=>{requests.push({u,body:JSON.parse(o.body)});return response({detail:'网络暂不可用'},503);});
 assert.equal(requests.length,0);assert.match(document.body.textContent,/不修改已有长期记忆/);
 await click('创建分支');await click('创建分支');
 assert.equal(requests.length,2);assert.equal(requests[0].body.request_id,requests[1].body.request_id);
 assert.equal(requests[0].body.mode,'edit');assert.equal(requests[0].body.message_id,'message-one');
 assert.equal(requests[0].body.content,'原始问题');assert.deepEqual(dest,[]);
});
test('regeneration never sends task or collaboration grants and suppresses double submit',async t=>{
 const requests=[],dest=[];let finish;
 await mount(t,MessageFork,{message:{...forkMessage,role:'assistant'},conversationId:'conversation-one',close:()=>{},done:id=>dest.push(id)},(u,o)=>{requests.push(JSON.parse(o.body));return new Promise(r=>finish=r);});
 assert.equal(document.querySelector('textarea'),null);assert.match(document.body.textContent,/不重新安排或执行/);
 await click('创建分支');await click('正在创建');assert.equal(requests.length,1);
 assert.equal(requests[0].mode,'regenerate');assert.equal('content' in requests[0],false);
 assert.equal('task_assist' in requests[0],false);assert.equal('collaboration' in requests[0],false);
 await act(async()=>finish(response({conversation_id:'branch-one',run_id:'new-run'})));
 assert.deepEqual(dest,['branch-one']);
});
test('fork rejection preserves dialog and displays recoverable source error',async t=>{
 await mount(t,MessageFork,{message:forkMessage,conversationId:'conversation-one',close:()=>{},done:()=>assert.fail('no navigation')},()=>response({detail:'原文已经失效，请刷新'},409));
 await click('创建分支');assert.match(document.querySelector('dialog').textContent,/原文已经失效/);
 assert.equal(document.querySelector('textarea').value,'原始问题');
});

test('proactive messages without an original model run have no regeneration action',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>response(u.startsWith('/api/message-pages?')?{items:[{...forkMessage,id:'proactive-message',role:'assistant',run_id:null}],next_cursor:null}:defaults(u)));
 assert.equal([...document.querySelectorAll('button')].filter(b=>b.textContent==='重新生成').length,0);
});
const original={message_id:'original-one',role:'user',content:'只读原文内容',start:0,end:6,total:2100,attachment_ids:['attachment-one']};

test('history browser searches by POST and reads original ranges without any execution',async t=>{
 const requests=[];
 await mount(t,HistoryBrowser,{conversationId:'conversation-one',close:()=>{}},(u,o)=>{requests.push({u,method:o.method,body:o.body});return response(u.endsWith('/search')?{items:[{message_id:'original-one',role:'user',created_at:1,preview:'原文预览',characters:2100}],total:1,next_offset:null}:original);});
 assert.equal(requests[0].method,'POST');assert.deepEqual(JSON.parse(requests[0].body),{query:'',offset:0,limit:10});
 await click('读取原文');assert.match(document.body.textContent,/只读原文内容/);assert.match(document.body.textContent,/关联 1 个附件/);
 await click('下一片段');assert.ok(requests.some(v=>v.u.includes('start=6&length=2000')));
 assert.equal(requests.some(v=>v.u.startsWith('/api/chat')||v.u.startsWith('/api/tasks')),false);
});

test('history search presents empty and recoverable failed states',async t=>{
 await mount(t,HistoryBrowser,{conversationId:'conversation-one',close:()=>{}},()=>response({items:[],total:0,next_offset:null}));
 assert.match(document.body.textContent,/没有匹配的已完成原文/);
 assert.equal([...document.querySelectorAll('button')].find(v=>v.textContent==='下一页').disabled,true);
});

test('history failure never displays a fabricated original',async t=>{
 await mount(t,HistoryBrowser,{conversationId:'conversation-one',close:()=>{}},()=>response({detail:'原文权限已经失效'},404));
 assert.match(document.querySelector('[role=alert]').textContent,/权限已经失效/);
 assert.equal(document.querySelector('.history-original'),null);
});

test('context evidence labels exact roles, coverage and model reads as data',async t=>{
 await mount(t,ContextEvidence,{runId:'run-one',conversationId:'conversation-one',close:()=>{}},u=>response(u.endsWith('/context')?{status:'partial',notice:'不是长期记忆',counts:{older:15,recent:3,examined:6,partial_sources:2,quoted:1,reread:1},quotes:[{message_id:'original-one',role:'assistant',content:'<script>untrusted()</script>',start:0,end:12,total:2100}],reads:[]}:original));
 assert.match(document.body.textContent,/覆盖有限/);assert.match(document.body.textContent,/提供候选 6 条/);assert.match(document.body.textContent,/助手原文/);
 assert.equal(document.querySelector('script'),null);await click('读取原文');assert.match(document.body.textContent,/只读原文内容/);
});

test('context failure supports retry and never claims successful recall',async t=>{
 let calls=0;
 await mount(t,ContextEvidence,{runId:'run-one',conversationId:'conversation-one',close:()=>{}},()=>++calls===1?response({detail:'连接暂时不可用'},503):response({status:'unavailable',notice:'未能补充历史',counts:{},quotes:[],reads:[]}));
 await click('重新加载');assert.equal(calls,2);assert.match(document.body.textContent,/压缩暂不可用/);assert.match(document.body.textContent,/额外调用一次当前对话模型/);assert.equal(document.querySelector('.history-original'),null);
});

test('chat exposes source review while preserving the existing page shell',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>response(u.startsWith('/api/chat-runs?')?[{id:'run-one',status:'completed',config:{history_truncated:true},memory_status:'ready',memory_refs:[],next_seq:1}]:u.endsWith('/context')?{status:'not_needed',notice:'本轮未压缩'}:defaults(u)));
 assert.ok([...document.querySelectorAll('button')].some(v=>v.textContent==='历史原文'));
 await click('查看依据');assert.match(document.querySelector('dialog').textContent,/本轮未压缩/);
});


test('chat attachments can enter collaboration while simple task assistance stays text only',async t=>{
 const requests=[],file={id:'media-one',filename:'项目.md',suffix:'.md',kind:'document',status:'ready',size:100,version:1};
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{
   if(u==='/api/chat'){requests.push(JSON.parse(o.body));return response({detail:'协议测试失败'},503);}
   if(u.startsWith('/api/media'))return response({items:[file],next_cursor:null});
   return response(defaults(u));
 });
 await click('选择文件');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('完成选择');
 const labelInput=text=>[...document.querySelectorAll('label')].find(v=>v.textContent===text)?.querySelector('input');
 assert.equal(labelInput('任务协助').disabled,true);assert.equal(labelInput('助手协作').disabled,false);
 await act(async()=>labelInput('助手协作').click());
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='选择文件').disabled,false);
 await input('请分工分析附件');await click('发送消息');
 assert.deepEqual(requests[0].media_ids,['media-one']);assert.ok(requests[0].collaboration);assert.equal(requests[0].task_assist,false);
 assert.match(document.body.textContent,/图片需要子助手模型通过视觉验证/);
});


const historyMessage=(id,content=id)=>({id,role:'user',content,status:'completed',created_at:1,run_id:null});
test('reply selection is text only, focuses composer and excludes execution modes',async t=>{
 const requests=[];const original={...historyMessage('source','<img src=x onerror=alert(1)> 历史建议'),role:'assistant'};
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{
  if(u==='/api/chat'){requests.push(JSON.parse(o.body));return response({detail:'暂时不可用'},503);}
  return response(u.startsWith('/api/message-pages?')?{items:[original],next_cursor:null}:defaults(u));
 });
 const mode=text=>[...document.querySelectorAll('label')].find(v=>v.textContent===text).querySelector('input');
 await act(async()=>mode('任务协助').click());await click('回复引用');
 assert.equal(document.activeElement.id,'chat-input');assert.equal(mode('任务协助').checked,false);
 assert.equal(mode('任务协助').disabled,true);assert.equal(mode('助手协作').disabled,true);
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='开启语音').disabled,true);
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='选择文件').disabled,false);
 const quote=document.querySelector('[aria-label="当前引用"]');assert.match(quote.textContent,/历史建议/);assert.equal(quote.querySelector('img'),null);
 await input('先别聊');await click('发送消息');await click('发送消息');
 assert.equal(requests.length,2);assert.equal(requests[0].reply_to_message_id,'source');assert.equal(requests[0].task_assist,false);assert.equal(requests[0].collaboration,null);
 assert.equal(requests[0].request_id,requests[1].request_id);assert.ok(document.querySelector('[aria-label="当前引用"]'));
 assert.equal(document.querySelector('#chat-input').value,'先别聊');
 await click('取消引用');assert.equal(document.querySelector('[aria-label="当前引用"]'),null);
 assert.equal(mode('任务协助').disabled,false);await click('发送消息');
 assert.equal('reply_to_message_id' in requests[2],false);assert.notEqual(requests[2].request_id,requests[1].request_id);
});

test('changing selected reference changes retry key; successful send clears it',async t=>{
 const requests=[];const originals=[historyMessage('one','原文一'),historyMessage('two','原文二')];let succeed=false;
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{
  if(u==='/api/chat'){requests.push(JSON.parse(o.body));return succeed?response({id:'sent'}):response({detail:'断线'},503);}
  return response(u.startsWith('/api/message-pages?')?{items:originals,next_cursor:null}:defaults(u));
 });
 await click('回复引用');await input('解释这句');await click('发送消息');
 await act(async()=>[...document.querySelectorAll('button')].filter(b=>b.textContent==='回复引用')[1].click());
 succeed=true;await click('发送消息');
 assert.equal(requests[1].reply_to_message_id,'two');assert.notEqual(requests[1].request_id,requests[0].request_id);
 assert.equal(document.querySelector('[aria-label="当前引用"]'),null);assert.equal(document.querySelector('#chat-input').value,'');
});

test('saved references render safe provenance, unavailable references reveal no old content',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>response(u.startsWith('/api/message-pages?')?{items:[
  {...historyMessage('redacted',''),status:'redacted'},
  {...historyMessage('one','当前提问'),reply_to:{message_id:'source',status:'available',role:'assistant',content:'<img src=x onerror=alert(1)> 来源',truncated:true,characters:1600}},
  {...historyMessage('two','后续提问'),reply_to:{message_id:'gone',status:'unavailable',content:'',truncated:false,characters:0}}
 ],next_cursor:null}:defaults(u)));
 assert.match(document.querySelector('.messages').textContent,/助手历史输出，不等于用户事实/);
 assert.match(document.querySelector('.messages').textContent,/已截取前 1200 字符/);
 assert.match(document.querySelector('.messages').textContent,/引用失效/);assert.equal(document.querySelector('.messages img'),null);
 assert.match(document.querySelector('.messages').textContent,/这条消息已不可用/);assert.doesNotMatch(document.querySelector('.messages').textContent,/正在组织回复/);
});

test('conversation switch clears reference and cannot leak it to the next chat',async t=>{
 const root=await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>response(u.startsWith('/api/message-pages?')?{items:[historyMessage('old-source')],next_cursor:null}:u==='/api/conversations/other'?{...conversation,id:'other'}:defaults(u)));
 await click('回复引用');assert.ok(document.querySelector('[aria-label="当前引用"]'));
 await act(async()=>root.render(createElement(ChatPage,{hash:'#/chat/conversation?c=other'})));
 assert.equal(document.querySelector('[aria-label="当前引用"]'),null);
});

test('running generation disables reply selection including control phrases',async t=>{
 const previous=globalThis.EventSource;globalThis.EventSource=class{addEventListener(){}close(){}};t.after(()=>globalThis.EventSource=previous);
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>response(u.startsWith('/api/message-pages?')?{items:[historyMessage('source')],next_cursor:null}:u.startsWith('/api/chat-runs?')?[{id:'busy',status:'running',config:{},memory_refs:[]}]:defaults(u)));
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='回复引用').disabled,true);
});
test('old deep link retains title and actions even outside directory results',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>response(u.startsWith('/api/conversation-pages?')?{items:[],next_cursor:null}:defaults(u)));
 assert.match(document.querySelector('.chat-main .pane-toolbar').textContent,/日常对话/);
 assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='重命名'));
});

test('directory appends deduplicated cursor pages and keeps first page on retry',async t=>{
 let attempts=0;await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>{
  if(u.startsWith('/api/conversation-pages?')){
   if(u.includes('cursor=')){if(++attempts===1)return response({detail:'目录暂不可用'},503);return response({items:[conversation,{...conversation,id:'old',title:'旧会话'}],next_cursor:null});}
   return response({items:[conversation],next_cursor:'directory-token'});
  }return response(defaults(u));
 });
 await click('更多对话');assert.match(document.body.textContent,/目录暂不可用/);assert.equal(document.querySelectorAll('.conversation-link').length,1);
 await click('重试列表');assert.equal(document.querySelectorAll('.conversation-link').length,2);assert.match(document.body.textContent,/旧会话/);
});

test('message pages navigate back and latest without accumulating all history or remounting voice',async t=>{
 const requests=[];await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>{
  requests.push(u);if(u.startsWith('/api/message-pages?'))return response(u.includes('cursor=')?{items:[historyMessage('old','很早的消息')],next_cursor:'even-older'}:{items:[historyMessage('latest','最新的消息')],next_cursor:'older'});
  return response(defaults(u));
 });
 const voiceRequests=requests.filter(u=>u.startsWith('/api/voice/config')).length;
 await click('加载更早消息');assert.match(document.querySelector('.messages').textContent,/很早的消息/);assert.doesNotMatch(document.querySelector('.messages').textContent,/最新的消息/);
 assert.ok(requests.filter(u=>u==='/api/message-pages?conversation_id=conversation-one').length>=2);
 assert.equal(requests.filter(u=>u.startsWith('/api/voice/config')).length,voiceRequests);
 await click('较新消息');assert.match(document.querySelector('.messages').textContent,/最新的消息/);
 await click('加载更早消息');await click('返回最新');assert.match(document.querySelector('.messages').textContent,/最新的消息/);
});

test('failed older page leaves back and latest navigation usable without fake welcome',async t=>{
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>u.startsWith('/api/message-pages?')?(u.includes('cursor=')?response({detail:'分页凭据过期'},400):response({items:[historyMessage('latest')],next_cursor:'older'})):response(defaults(u)));
 await click('加载更早消息');assert.match(document.body.textContent,/分页凭据过期/);assert.equal(document.querySelector('.chat-welcome'),null);
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='较新消息').disabled,false);
 await click('返回最新');assert.match(document.querySelector('.messages').textContent,/latest/);
});

test('late history response cannot overwrite explicit return to latest',async t=>{
 let finish;await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>u.startsWith('/api/message-pages?')?(u.includes('cursor=')?new Promise(r=>finish=r):response({items:[historyMessage('latest')],next_cursor:'older'})):response(defaults(u)));
 await click('加载更早消息');await click('返回最新');
 await act(async()=>finish(response({items:[historyMessage('obsolete')],next_cursor:null})));
 assert.match(document.querySelector('.messages').textContent,/latest/);assert.doesNotMatch(document.querySelector('.messages').textContent,/obsolete/);
});

test('sending from an older page returns to latest with a single chat submission',async t=>{
 let writes=0;await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},(u,o)=>{
  if(u==='/api/chat'){writes++;return response({id:'new-run',status:'completed'});}
  if(u.startsWith('/api/message-pages?'))return response({items:[historyMessage(u.includes('cursor=')?'old':'latest')],next_cursor:u.includes('cursor=')?null:'older'});
  return response(defaults(u));
 });
 await click('加载更早消息');await input('接着聊');await click('发送消息');
 assert.equal(writes,1);assert.match(document.querySelector('.messages').textContent,/latest/);assert.doesNotMatch(document.querySelector('.messages').textContent,/正在浏览历史/);
});

test('conversation switch ignores old detail and message responses',async t=>{
 let finish;const root=await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>{
  if(u==='/api/conversations/conversation-one')return new Promise(r=>finish=r);
  if(u==='/api/conversations/conversation-two')return response({...conversation,id:'conversation-two',title:'第二个对话'});
  if(u.startsWith('/api/message-pages?'))return response({items:[historyMessage(u.includes('conversation-two')?'two':'one')],next_cursor:null});
  return response(defaults(u));
 });
 await act(async()=>root.render(createElement(ChatPage,{hash:'#/chat/conversation?c=conversation-two'})));
 await act(async()=>finish(response(conversation)));
 assert.match(document.querySelector('.chat-main .pane-toolbar').textContent,/第二个对话/);assert.match(document.querySelector('.messages').textContent,/two/);assert.doesNotMatch(document.querySelector('.messages').textContent,/one/);
});

test('search change discards a late directory page from the previous query',async t=>{
 let finish;await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>{
  if(u.startsWith('/api/conversation-pages?')){
   if(u.includes('cursor='))return new Promise(r=>finish=r);
   return response(u.includes('q=new')?{items:[{...conversation,id:'new',title:'新搜索结果'}],next_cursor:null}:{items:[conversation],next_cursor:'older'});
  }return response(defaults(u));
 });
 await click('更多对话');const el=document.querySelector('input[aria-label="搜索对话"]');
 await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(el,'new');el.dispatchEvent(new window.Event('input',{bubbles:true}));});
 await act(async()=>finish(response({items:[{...conversation,id:'stale',title:'过时结果'}],next_cursor:null})));
 assert.match(document.querySelector('.conversation-items').textContent,/新搜索结果/);assert.doesNotMatch(document.querySelector('.conversation-items').textContent,/过时结果/);
});


test('slow message polling is coalesced instead of spawning overlapping page reads',async t=>{
 const prior=globalThis.setInterval;let poll,finish,calls=0;
 globalThis.setInterval=(fn,ms,...args)=>{if(ms===1500)poll=fn;return prior(fn,ms,...args);};
 t.after(()=>{globalThis.setInterval=prior;});
 await mount(t,ChatPage,{hash:'#/chat/conversation?c=conversation-one'},u=>{
  if(u.startsWith('/api/message-pages?')){calls++;return new Promise(r=>finish=r);}
  return response(defaults(u));
 });
 await act(async()=>{poll();poll();});assert.equal(calls,1);
 await act(async()=>finish(response({items:[historyMessage('latest')],next_cursor:null})));
 assert.match(document.querySelector('.messages').textContent,/latest/);
});
