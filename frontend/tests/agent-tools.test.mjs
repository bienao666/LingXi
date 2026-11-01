import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const[k,v]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${k}'`,`from '${v}'`).replaceAll(`from "${k}"`,`from "${v}"`);return url(code);}
const window=new Window({url:'http://localhost/#/plugins/connections'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {AgentToolScope,AgentToolReview}=await import(compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui}));

const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const preview={id:'invoke',digest:'fixed',expires_at:9999999999,plan:{plugin_id:'sample.api',plugin_version:'1.0.0',capability_id:'sample.write',url:'https://service.example/action',effect:'write',costs:'未知',data_scope:'本次参数',input:{title:'<img src=x onerror=alert(1)>'}}};
async function mount(t,Component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(Component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function review(t,handler,props={}){await mount(t,AgentToolReview,{runId:'run',nodeId:'node',version:7,close(){},done(){},...props},handler);}
async function accept(){await act(async()=>document.querySelector('dialog input[type=checkbox]').click());}

test('scope fetches exact authorized tools and selection never creates a preview',async t=>{
 const calls=[];let selected;
 await mount(t,AgentToolScope,{value:[],change(v){selected=v;}},(u,o)=>{calls.push({u,o});return response([{key:'fixed-key',name:'登记工具',plugin_id:'sample.api',plugin_version:'1.0.0',adapter:'api',tool:'',effect:'write',url:'https://service.example',costs:'未知',data_scope:'本次参数'}]);});
 assert.match(document.body.textContent,/每个远程请求仍须核对/);await act(async()=>document.querySelector('input').click());
 assert.deepEqual(selected,['fixed-key']);assert.equal(calls.length,1);assert.equal(calls[0].u,'/api/agent-tools');
});

test('scope unavailable selections can be cleared and catalog errors are visible',async t=>{
 let selected;await mount(t,AgentToolScope,{value:['removed'],change(v){selected=v;}},()=>response([]));
 assert.match(document.body.textContent,/部分所选工具已不可用/);await click('清除选择');assert.deepEqual(selected,[]);
});

test('opening review sends nothing; preview pins run version and requires separate consent',async t=>{
 const calls=[];let done=0;await review(t,(u,o)=>{calls.push({u,o});return response(preview);},{done(){done++;}});
 assert.equal(calls.length,0);await click('生成预览');assert.equal(calls.length,1);
 assert.equal(calls[0].u,'/api/agent-runs/run/nodes/node/preview');assert.deepEqual(JSON.parse(calls[0].o.body),{version:7});
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认执行').disabled,true);
 assert.equal(document.querySelector('img'),null);assert.match(document.querySelector('pre').textContent,/<img/);
 await accept();await click('确认执行');assert.equal(done,1);assert.equal(calls.length,2);
 assert.equal(calls[1].u,'/api/plugin-invocations/invoke/confirm');assert.deepEqual(JSON.parse(calls[1].o.body),{digest:'fixed'});
});

test('stale proposal version reports conflict without a confirm button',async t=>{
 await review(t,()=>response({detail:'协作状态已变化，请重新加载'},409));await click('生成预览');
 assert.match(document.querySelector('[role=alert]').textContent,/重新加载/);assert.equal(document.querySelector('input[type=checkbox]'),null);
});

test('preview and confirmation double clicks send each request only once',async t=>{
 let resolve,pending=[],done=0;await review(t,(u,o)=>{pending.push(u);return new Promise(r=>resolve=()=>r(response(preview)));},{done(){done++;}});
 const button=document.querySelector('button.primary');await act(async()=>{button.click();button.click();});assert.equal(pending.length,1);
 await act(async()=>resolve());await accept();const confirm=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认执行');
 await act(async()=>{confirm.click();confirm.click();});assert.equal(pending.length,2);assert.equal(done,0);
 await act(async()=>resolve());assert.equal(done,1);
});

test('cancel closes preview without executing and failed confirm preserves visible parameters',async t=>{
 let posts=0,closed=0;await review(t,()=>{posts++;return posts===1?response(preview):response({detail:'来源已撤销'},410);},{close(){closed++;}});
 await click('生成预览');await accept();await click('确认执行');assert.match(document.querySelector('[role=alert]').textContent,/来源已撤销/);
 assert.match(document.querySelector('pre').textContent,/title/);await click('暂不执行');assert.equal(posts,2);assert.equal(closed,1);
});

test('native close of nested tool dialog leaves its parent detail open',async t=>{
 const {Modal}=await import(ui);let childClosed=0,parentClosed=0;
 await mount(t,Modal,{title:'协作详情',close(){parentClosed++;},children:createElement(AgentToolReview,{runId:'run',nodeId:'node',version:1,close(){childClosed++;},done(){}})},()=>response(preview));
 const child=document.querySelector('dialog dialog');assert.ok(child);
 await act(async()=>child.dispatchEvent(new window.Event('close')));
 assert.equal(childClosed,1);assert.equal(parentClosed,0);
});

test('nested dialog keyboard wraps inside the current dialog only',async t=>{
 const {Modal}=await import(ui);
 await mount(t,Modal,{title:'协作详情',close(){},children:createElement(AgentToolReview,{runId:'run',nodeId:'node',version:1,close(){},done(){}})},()=>response(preview));
 const buttons=[...document.querySelectorAll('dialog dialog button')];
 for(const b of buttons)b.getClientRects=()=>[{width:100,height:40}];
 const first=buttons[0],last=buttons.at(-1);first.focus();
 await act(async()=>first.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true,cancelable:true})));
 assert.equal(document.activeElement,last);
 await act(async()=>last.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true})));
 assert.equal(document.activeElement,first);
});
