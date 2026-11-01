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
const {PluginEvents}=await import(compile('../src/PluginEvents.tsx',{'./api':api,'./ui':ui}));
const cap={id:'sample.updates',kind:'event',name:'项目动态',installation_id:'installed',plugin_id:'sample.events',plugin_version:'1.0.0',event:{event_types:['project.updated','project.completed'],transport:'signed-webhook',ttl_seconds:86400}};
const sub={id:'sub',installation_id:'installed',capability_id:'sample.updates',plugin_id:'sample.events',plugin_version:'1.0.0',status:'active',version:3,event_types:['project.updated'],hourly_limit:10,daily_limit:50,has_secret:true,available:true,reason:null,endpoint:'https://core.example/api/plugin-events/hooks/sub',updated_at:1};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(PluginEvents,{capabilities:[cap],changed(){}})));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}

test('event subscription requires selected types and separate explicit confirmation',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response([]);});await click('配置订阅');
 assert.equal(document.querySelector('dialog input[type=password]').value,'');
 assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.ok(!calls.some(c=>c.o.method==='POST'));
 await act(async()=>document.querySelector('dialog fieldset input').click());
 assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.match(document.querySelector('dialog').textContent,/不授予记忆库/);
});

test('editing uses actual version and blank secret preserves existing encrypted credential',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-event-subscriptions'&&o.method!=='POST'?[sub]:u==='/api/plugin-events'?[]:sub);});
 await click('编辑订阅');assert.equal(document.querySelector('dialog input[type=password]').value,'');
 assert.equal(document.querySelector('dialog input[type=password]').autocomplete,'off');
 await act(async()=>[...document.querySelectorAll('dialog input[type=checkbox]')].at(-1).click());await click('确认授权');
 const sent=JSON.parse(calls.find(c=>c.o.method==='POST').o.body);
 assert.deepEqual(sent,{installation_id:'installed',capability_id:'sample.updates',secret:'',event_types:['project.updated'],hourly_limit:10,daily_limit:50,version:3,acknowledged:true});
});

test('failed subscription keeps the form and never claims enabled status',async t=>{
 await mount(t,(u,o)=>o.method==='POST'?response({detail:'事件订阅已变化'},409):response(u==='/api/plugin-event-subscriptions'?[sub]:[]));
 await click('编辑订阅');await act(async()=>[...document.querySelectorAll('dialog input[type=checkbox]')].at(-1).click());await click('确认授权');
 assert.ok(document.querySelector('dialog'));assert.match(document.querySelector('dialog [role=alert]').textContent,/已变化/);
});

test('untrusted event text stays inert and acceptance never claims delivery',async t=>{
 await mount(t,u=>response(u==='/api/plugin-events'?[{id:'event',event_type:'project.updated',title:'<img src=x onerror=alert(1)>',summary:'<script>run()</script>',status:'accepted',version:1,created_at:1,expires_at:2,sequence:1}]:[sub]));
 assert.equal(document.querySelector('main script,section script,section img'),null);
 assert.match(document.body.textContent,/<script>run\(\)<\/script>/);
 assert.match(document.body.textContent,/已收事件/);assert.match(document.body.textContent,/回执仅表示接收/);
});

test('revocation and deletion wait for named confirmation with actual versions',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-events'?[{id:'event',event_type:'project.updated',title:'项目更新',summary:'合成资料',status:'accepted',version:4,created_at:1,expires_at:2,sequence:1}]:u==='/api/plugin-event-subscriptions'?[sub]:{});});
 await click('撤销订阅');assert.ok(!calls.some(c=>c.o.method==='POST'));await click('确认撤销');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/revoke')).o.body),{version:3});
 await click('删除事件');assert.match(document.querySelector('dialog').textContent,/去重标记继续保留/);await click('确认删除');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/delete')).o.body),{version:4});
});

test('an unavailable event source is shown without stale event bodies',async t=>{
 await mount(t,u=>response(u==='/api/plugin-events'?[{id:'event',event_type:'project.updated',title:'',summary:'',status:'invalidated',version:2,created_at:1,expires_at:2,sequence:1}]:[{...sub,status:'revoked',available:false,reason:'插件版本已撤销',has_secret:false}]));
 assert.match(document.body.textContent,/来源失效/);assert.match(document.body.textContent,/插件版本已撤销/);
 assert.ok(!document.body.textContent.includes('undefined'));
});
