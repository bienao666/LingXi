import {test}from'node:test';
import assert from'node:assert/strict';
import {readFileSync}from'node:fs';
import {createRequire}from'node:module';
import {pathToFileURL}from'node:url';
import ts from'typescript';
import {Window}from'happy-dom';
import {act,createElement}from'react';
const require=createRequire(import.meta.url),url=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const[k,v]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${k}'`,`from '${v}'`).replaceAll(`from "${k}"`,`from "${v}"`);return url(code);}
const window=new Window({url:'http://localhost/#/store-review/queue'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {MigrationPage}=await import(compile('../src/MigrationPage.tsx',{'./api':api,'./ui':ui}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

const empty={mode:'normal',instance_id:null,runs:[]};
const preview={target_label:'新服务器',target_origin:'https://target.example.com',target_id:'b'.repeat(32),expires_at:Date.now()/1000+300,token:'encrypted-preview',note:'终端导出会冻结源端；目标需要重新授权。'};
const run={id:'a'.repeat(32),direction:'outbound',status:'planned',version:1,target_label:'新服务器',target_origin:'https://target.example.com',created_at:1,finished_at:0,manifest_digest:''};

test('migration empty state never pretends to transfer data and exposes only a preview action',async t=>{
 const calls=[];await mount(t,MigrationPage,{},(u,o)=>{calls.push({u,o});return response(empty)});
 assert.match(document.body.textContent,/尚无迁移记录/);assert.match(document.body.textContent,/主密钥须单独安全传递/);
 assert.ok(calls.every(c=>c.o.method==='GET'));assert.equal(document.querySelector('input[type="password"]'),null);
});
test('target preview requires explicit confirmation, retries use the same request id, and secrets are not persisted',async t=>{
 const calls=[];await mount(t,MigrationPage,{},(u,o)=>{calls.push({u,o});return response(o.method==='GET'?empty:u.endsWith('/previews')?preview:{detail:'fixture conflict'},o.method==='GET'||u.endsWith('/previews')?200:409)});
 await click('迁移预检');await value('textarea','encrypted-target-ticket');await submit();
 assert.match(document.querySelector('dialog').textContent,/https:\/\/target.example.com/);
 assert.equal([...document.querySelectorAll('dialog button')].find(b=>b.textContent==='确认迁移').disabled,true);
 assert.equal(calls.filter(c=>c.o.method==='POST').length,1);
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await submit();await submit();
 const sent=calls.filter(c=>c.u==='/api/workspace-migrations'&&c.o.method==='POST').map(c=>JSON.parse(c.o.body));
 assert.equal(sent.length,2);assert.equal(sent[0].request_id,sent[1].request_id);assert.equal(sent[0].acknowledgement,'freeze_source_before_transfer');
 assert.match(document.querySelector('dialog').textContent,/fixture conflict/);assert.equal(window.localStorage.length,0);assert.equal(window.sessionStorage.length,0);
});
test('changing target invalidates the prior preview and acknowledgement',async t=>{
 const calls=[];await mount(t,MigrationPage,{},(u,o)=>{calls.push({u,o});return response(o.method==='GET'?empty:preview)});
 await click('迁移预检');await value('textarea','first-ticket');await submit();
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await value('textarea','different-ticket');
 assert.equal(document.querySelector('dialog input[type="checkbox"]'),null);await submit();
 assert.equal(calls.filter(c=>c.u.endsWith('/previews')).length,2);
 assert.ok(!calls.some(c=>c.u==='/api/workspace-migrations'&&c.o.method==='POST'));
});
test('expired preview cannot confirm and must be rechecked',async t=>{
 await mount(t,MigrationPage,{},(u,o)=>response(o.method==='GET'?empty:{...preview,expires_at:Date.now()/1000-1}));
 await click('迁移预检');await value('textarea','ticket');await submit();assert.match(document.querySelector('dialog').textContent,/预览已过期/);
 assert.equal(document.querySelector('dialog input[type="checkbox"]').disabled,true);await click('重新核验');assert.equal(document.querySelector('dialog input[type="checkbox"]'),null);
});
test('cancel sends the exact record version and preserves server conflicts',async t=>{
 const calls=[];await mount(t,MigrationPage,{},(u,o)=>{calls.push({u,o});return response(o.method==='GET'?{...empty,runs:[run]}:{detail:'已经开始停写'},o.method==='GET'?200:409)});
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='迁移预检').disabled,true);
 await click('取消迁移');await click('确认取消');assert.match(document.querySelector('dialog').textContent,/已经开始停写/);
 assert.deepEqual(JSON.parse(calls.find(c=>c.o.method==='POST').o.body),{version:1});
});
test('frozen source and pending target do not offer mutating actions',async t=>{
 await mount(t,MigrationPage,{},()=>response({mode:'source_frozen',instance_id:'source',runs:[{...run,status:'exported'}]}));
 assert.match(document.body.textContent,/源端停写/);assert.match(document.body.textContent,/不要重新启用旧任务/);
 assert.ok(![...document.querySelectorAll('button')].some(b=>b.textContent==='取消迁移'));
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='迁移预检').disabled,true);
});
test('loading failures remain visible and retry reads real state',async t=>{
 let failed=true;await mount(t,MigrationPage,{},()=>response(failed?{detail:'连接失败'}:empty,failed?503:200));
 assert.match(document.body.textContent,/连接失败/);failed=false;await click('重试连接');assert.match(document.body.textContent,/尚无迁移记录/);
});
