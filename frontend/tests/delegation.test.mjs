import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const [name,value]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${name}'`,`from '${value}'`).replaceAll(`from "${name}"`,`from "${value}"`);return url(code);}
const window=new Window({url:'http://localhost/#/channels/instances'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {AgentRuns}=await import(compile('../src/AgentRuns.tsx',{'./MediaPages':compile('../src/MediaPages.tsx',{'./api':api,'./ui':ui,'./MediaStorage':compile('../src/MediaStorage.tsx',{'./api':api,'./ui':ui})}),'./AgentTools':compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui}),'./api':api,'./ui':ui}));
const response=(value,status=200)=>new Response(JSON.stringify(value),{status});
const run={id:'run-one',title:'评审协作',goal:'评估项目方案',status:'running',version:4,config:{model:'fixture'},decision:{mode:'delegate',reason:'两项可独立分析',nodes:2},calls_used:2,max_calls:12,result:{},memory_refs:[],error:null,created_at:1,updated_at:1};
const node={id:'node-one',key:'n0',title:'范围分析',instruction:'比较范围',agent_id:'a',capability:'reason',depends_on:[],config:{name:'资料研究',model:'fixture',persona:{version:1},role:{version:2},plugin_id:'lingxi.builtin',plugin_version:'1.0.0'},context:{memory_status:'not_allowed'},result:{},status:'running',attempt:1,error:null};
async function mount(t,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(uri,options)=>uri==='/api/media-policy'?response({version:1,max_files:10,max_file_mib:20,max_total_mib:50,allowed_types:['text','markdown','csv','pdf','docx','xlsx','image','qq_audio']}):handler(uri,options);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(AgentRuns)));}
async function click(label,scope=document){const b=[...scope.querySelectorAll('button')].find(e=>e.textContent===label);assert.ok(b,label);await act(async()=>b.click());}
async function submit(){await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}
function defaultRead(uri){return response(uri==='/api/conversations'||uri==='/api/agent-tools'?[]:uri==='/api/agent-runs/run-one'?{...run,nodes:[node],events:[]}:{items:[run],next_cursor:null});}

test('creation requires permission preview and preserves the draft',async t=>{
 const writes=[];await mount(t,(uri,o)=>{if(o.method!=='GET'){writes.push(JSON.parse(o.body));return response(run);}return defaultRead(uri);});
 await click('新建协作');document.querySelector('[name=title]').value='我的评审';document.querySelector('[name=goal]').value='先核对目标';
 await submit();assert.equal(writes.length,0);assert.equal(document.activeElement.textContent,'确认创建');assert.match(document.querySelector('dialog').textContent,/不共享/);await click('继续编辑');assert.equal(document.querySelector('[name=goal]').value,'先核对目标');await submit();await click('确认创建');assert.equal(writes.length,1);assert.equal(writes[0].share_memory,false);assert.equal(writes[0].allow_files,false);assert.equal(writes[0].max_calls,12);assert.ok(writes[0].request_id);
});

test('proposing memory is a separate opt-in, survives editing and is disclosed before create',async t=>{
 const writes=[];await mount(t,(uri,o)=>o.method==='POST'?(writes.push(JSON.parse(o.body)),response(run)):defaultRead(uri));
 await click('新建协作');document.querySelector('[name=title]').value='项目目标';document.querySelector('[name=goal]').value='整理项目';
 assert.equal(document.querySelector('[name=propose_memory]').checked,false);
 await act(async()=>document.querySelector('[name=propose_memory]').click());await submit();
 assert.match(document.querySelector('dialog').textContent,/逐项核对保存/);assert.equal(writes.length,0);
 await click('继续编辑');assert.equal(document.querySelector('[name=propose_memory]').checked,true);assert.equal(document.querySelector('[name=share_memory]').checked,false);
 await submit();await click('确认创建');assert.equal(writes[0].propose_memory,true);assert.equal(writes[0].share_memory,false);
});
test('failed submission uses same idempotency id on retry and shows real error',async t=>{
 const ids=[];await mount(t,(uri,o)=>{if(o.method==='POST'){ids.push(JSON.parse(o.body).request_id);return response({detail:'模型尚未验证'},409);}return defaultRead(uri);});
 await click('新建协作');document.querySelector('[name=title]').value='评审';document.querySelector('[name=goal]').value='核对范围';await submit();await click('确认创建');assert.match(document.querySelector('[role=alert]').textContent,/模型尚未验证/);await click('确认创建');assert.equal(ids[0],ids[1]);assert.equal(ids.length,2);
});
test('inflight create prevents duplicate submissions',async t=>{
 let resolve;let count=0;await mount(t,(uri,o)=>o.method==='POST'?(count++,new Promise(r=>resolve=r)):defaultRead(uri));
 await click('新建协作');document.querySelector('[name=title]').value='x';document.querySelector('[name=goal]').value='y';await submit();await click('确认创建');assert.equal(document.querySelector('dialog button.primary').disabled,true);await act(async()=>document.querySelector('dialog button.primary').click());assert.equal(count,1);await act(async()=>resolve(response(run)));
});
test('cancel needs confirmation and sends current version only',async t=>{
 const writes=[];await mount(t,(uri,o)=>{if(o.method==='POST'){writes.push({uri,body:JSON.parse(o.body)});return response({...run,status:'cancelled'});}return defaultRead(uri);});
 await click('查看协作');await click('取消协作');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/模型费用不能撤销/);await click('确认取消');assert.equal(writes[0].uri,'/api/agent-runs/run-one/cancel');assert.equal(writes[0].body.version,4);
});
test('partial retry names preserved success and reports version conflicts',async t=>{
 await mount(t,(uri,o)=>{if(o.method==='POST')return response({detail:'任务状态已变化'},409);if(uri==='/api/agent-runs/run-one')return response({...run,status:'partial',nodes:[{...node,status:'succeeded'}],events:[]});return defaultRead(uri);});
 await click('查看协作');await click('重试失败');assert.match(document.querySelector('dialog').textContent,/保留成功节点/);await click('确认重试');assert.match(document.querySelector('[role=alert]').textContent,/状态已变化/);
});
test('budget exhausted prevents retry and untrusted output is rendered as text',async t=>{
 await mount(t,(uri,o)=>uri==='/api/agent-runs/run-one'?response({...run,status:'partial',calls_used:12,nodes:[node],events:[],result:{summary:'<img src=x onerror=alert(1)>'}}):defaultRead(uri));
 await click('查看协作');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='重试失败').disabled,true);assert.equal(document.querySelector('dialog img'),null);assert.match(document.querySelector('dialog').textContent,/调用预算已耗尽/);assert.match(document.querySelector('dialog').textContent,/未获授权/);
});


test('collaboration file selection survives preview editing and is included only on confirmation',async t=>{
 const file={id:'media-one',filename:'项目预算.md',suffix:'.md',kind:'document',status:'ready',size:120,version:1};
 const writes=[];await mount(t,(uri,o)=>{
   if(o.method==='POST'){writes.push(JSON.parse(o.body));return response(run);}
   if(uri.startsWith('/api/media'))return response({items:[file],next_cursor:null});
   return defaultRead(uri);
 });
 await click('新建协作');document.querySelector('[name=title]').value='材料分析';document.querySelector('[name=goal]').value='比较材料';
 await click('选择文件');const picker=[...document.querySelectorAll('dialog')].at(-1);
 await act(async()=>picker.querySelector('input[type=checkbox]').click());await click('完成选择',picker);
 await submit();assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/提交附件 1 份/);
 await click('继续编辑');assert.match(document.querySelector('dialog').textContent,/项目预算.md/);
 await submit();await click('确认创建');assert.deepEqual(writes[0].media_ids,['media-one']);
});

test('attachment parse in progress prevents preview and does not trigger model calls',async t=>{
 let writes=0;const pending={id:'media-pending',filename:'大文档.pdf',kind:'document',status:'pending',size:100,version:1};
 await mount(t,(uri,o)=>o.method==='POST'?(writes++,response(pending)):defaultRead(uri));
 await click('新建协作');const upload=document.querySelector('input[type=file]');
 const file=new window.File(['content'],'大文档.pdf',{type:'application/pdf'});
 await act(async()=>{Object.defineProperty(upload,'files',{configurable:true,value:[file]});upload.dispatchEvent(new window.Event('change',{bubbles:true}));});
 assert.equal(writes,1);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='预览创建').disabled,true);
 await submit();assert.equal([...document.querySelectorAll('button')].some(b=>b.textContent==='确认创建'),false);
 assert.match(document.querySelector('dialog').textContent,/等待附件解析完成/);
});

test('node attachment source uses a nested read-only dialog and returns focus',async t=>{
 const item={id:'media-one',filename:'预算.md',kind:'document',status:'ready',size:30,version:1,parts_count:1,parts:[{location:'第 1 行',text:'预算 42 万'}],notes:[]};
 await mount(t,(uri,o)=>uri==='/api/agent-runs/run-one'?response({...run,nodes:[{...node,context:{memory_status:'not_allowed',attachments:[item]}}],events:[]}):uri==='/api/media/media-one'?response(item):defaultRead(uri));
 await click('查看协作');[...document.querySelectorAll('button')].find(b=>b.textContent==='预算.md · 来源').focus();await click('预算.md · 来源');assert.equal(document.querySelectorAll('dialog').length,2);
 assert.match([...document.querySelectorAll('dialog')].at(-1).textContent,/第 1 行/);
 await act(async()=>[...document.querySelectorAll('dialog')].at(-1).dispatchEvent(new window.Event('close')));
 assert.equal(document.querySelectorAll('dialog').length,1);assert.equal(document.activeElement.textContent,'预算.md · 来源');
});
