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
const window=new Window({url:'http://localhost/#/channels/rules'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api}),tools=compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui});
const {ChannelTasks}=await import(compile('../src/ChannelTasks.tsx',{'./api':api,'./ui':ui,'./AgentTools':tools}));
const task={id:'task-1',channel_id:'channel-1',target_key:'group:12345',sender_id:'87654321',goal:'整理项目建议',status:'pending',version:2,run_id:null,run_status:null,expires_at:Date.now()/1000+3600,created_at:1,notifications:{},error:null};
const agent={id:'preset:test',name:'资料研究',model:'synthetic',capabilities:['reason']};
const preview={version:3,digest:'a'.repeat(64),expires_at:Date.now()/1000+300,plan:{model:'synthetic',max_calls:12,audience:'本群所有成员可见',share_memory:false,resource_path:null,agents:[{id:agent.id,persona:{spec:{instructions:'仅本次已审核的公开风格'}}}],tools:[],attachments:[],costs:'合成服务，无实际调用'}};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,handler,rows=[task]){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(uri,o)=>{if(o.method!=='GET')return handler(uri,o);return response(uri.startsWith('/api/channel-tasks?')?rows:uri==='/api/agents'?[agent]:[]);};const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(ChannelTasks,{channelId:'channel-1'})));}
async function click(label){const button=[...document.querySelectorAll('button')].find(b=>b.textContent===label);assert.ok(button,label);await act(async()=>button.click());}
async function selectAgent(){const input=[...document.querySelectorAll('dialog fieldset input[type=checkbox]')][0];await act(async()=>input.click());}
async function submit(){await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('group task is an explicit two-step approval with full configuration and no memory grant',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push({uri,body:JSON.parse(o.body)});return response(uri.endsWith('/preview')?preview:{...task,run_id:'run-1',status:'running'});});
 await click('核对请求');assert.match(document.querySelector('dialog').textContent,/群成员可见/);const memory=[...document.querySelectorAll('dialog label')].find(l=>l.textContent.includes('允许检索任务相关个人记忆')).querySelector('input');assert.equal(memory.disabled,true);assert.equal(memory.checked,false);
 await selectAgent();await submit();assert.equal(writes.length,1);assert.equal(writes[0].body.share_memory,false);assert.deepEqual(writes[0].body.agent_ids,[agent.id]);assert.equal(writes[0].body.resource_path,null);assert.match(document.querySelector('dialog').textContent,/仅本次已审核的公开风格/);assert.match(document.querySelector('dialog').textContent,/每次远程操作仍需 Web 单独确认/);assert.equal(document.activeElement.type,'checkbox');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认执行').disabled,true);
 await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认执行');assert.equal(writes.length,2);assert.equal(writes[1].body.digest,preview.digest);assert.equal(writes[1].body.version,3);
});

test('no assistant selection cannot start preview and cancel never executes',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push(uri);return response(preview);});await click('核对请求');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='生成预览').disabled,true);await click('暂不执行');assert.equal(writes.length,0);
});

test('version conflict keeps selection visible and does not confirm silently',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push(uri);return response({detail:'请求状态已经变化，请重新加载'},409);});await click('核对请求');await selectAgent();await submit();assert.match(document.querySelector('dialog').textContent,/重新加载/);assert.equal(document.querySelector('dialog fieldset input').checked,true);assert.equal(writes.length,1);assert.equal(document.querySelector('dialog').textContent.includes('确认执行'),false);
});

test('editing a preview retains choices and uses its latest version',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push(JSON.parse(o.body));return response({...preview,version:writes.length+2});});await click('核对请求');await selectAgent();await submit();await click('继续编辑');assert.equal(document.activeElement,document.querySelector('dialog fieldset input'));assert.equal(document.querySelector('dialog fieldset input').checked,true);await submit();assert.equal(writes[1].version,3);assert.equal(writes.length,2);
});

test('rejection requires explicit confirmation and preserves receiver',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push({uri,body:JSON.parse(o.body)});return response({...task,status:'rejected'});});await click('拒绝请求');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/group:12345/);assert.match(document.querySelector('dialog').textContent,/已经发生的外部操作与 QQ 发送不能撤销/);await click('确认停止');assert.deepEqual(writes,[{uri:'/api/channel-tasks/task-1/cancel',body:{version:2}}]);
});

test('running task links to real detail and displays protocol acceptance accurately',async t=>{
 await mount(t,()=>response({}),[{...task,status:'completed',run_status:'completed',run_id:'run-1',notifications:{'1:result':['accepted_by_protocol']}}]);assert.equal(document.querySelector('a').getAttribute('href'),'#/tasks/list?mode=agents&run=run-1');assert.match(document.body.textContent,/协议已接受/);assert.match(document.body.textContent,/不等于已读/);assert.equal(document.body.textContent.includes('核对请求'),false);
});

test('confirmation failure stays in preview without replaying execution',async t=>{
 const writes=[];await mount(t,(uri,o)=>{writes.push(uri);return uri.endsWith('/preview')?response(preview):response({detail:'预览已过期，请重新核对'},409);});await click('核对请求');await selectAgent();await submit();await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认执行');assert.match(document.querySelector('dialog').textContent,/预览已过期/);assert.equal(writes.filter(v=>v.endsWith('/confirm')).length,1);assert.equal(writes.length,2);
});
