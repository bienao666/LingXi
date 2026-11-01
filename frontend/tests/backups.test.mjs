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
const {BackupPage}=await import(compile('../src/BackupPage.tsx',{'./api':api,'./ui':ui}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}


const empty={policy:{version:0,enabled:false,interval_hours:24,next_at:0},agent_online:false,maintenance:false,runs:[]};
const run={id:'a'.repeat(32),archive_id:'a'.repeat(32),kind:'backup',status:'succeeded',created_at:1,finished_at:2,digest:'f'.repeat(64),bytes:123,error:''};
test('backup page reports offline runner without promising a completed backup',async t=>{
 const calls=[];await mount(t,BackupPage,{},(u,o)=>{calls.push({u,o});return response(empty)});
 assert.match(document.body.textContent,/运行器离线/);assert.match(document.body.textContent,/没有备份记录/);assert.ok(calls.every(c=>c.o.method==='GET'));
});
test('creating backup requires explicit scope confirmation and a stable request id on retry',async t=>{
 const calls=[];await mount(t,BackupPage,{},(u,o)=>{calls.push({u,o});return o.method==='GET'?response(empty):response({detail:'fixture network failure'},503)});
 await click('创建备份');assert.equal(document.querySelector('dialog button.primary').disabled,true);assert.equal(calls.filter(c=>c.o.method==='POST').length,0);
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await click('确认备份');
 assert.match(document.querySelector('dialog').textContent,/fixture network failure/);await click('确认备份');
 const sent=calls.filter(c=>c.o.method==='POST').map(c=>JSON.parse(c.o.body));assert.equal(sent.length,2);assert.equal(sent[0].request_id,sent[1].request_id);assert.equal(sent[0].kind,'backup');assert.equal(sent[0].confirmed,true);
});
test('archive verification submits an identifier without uploading files or restoring data',async t=>{
 const calls=[];await mount(t,BackupPage,{},(u,o)=>{calls.push({u,o});return response(o.method==='GET'?{...empty,runs:[run]}:run)});
 await click('核验备份');assert.match(document.querySelector('dialog').textContent,/不覆盖任何业务数据/);
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await click('确认核验');
 const sent=JSON.parse(calls.find(c=>c.o.method==='POST').o.body);assert.equal(sent.kind,'verify');assert.equal(sent.archive_id,run.id);assert.ok(!calls.some(c=>c.u.includes('restore')));
});
test('schedule needs impact confirmation and preserves version conflicts',async t=>{
 const calls=[];await mount(t,BackupPage,{},(u,o)=>{calls.push({u,o});return o.method==='GET'?response(empty):response({detail:'配置已变化'},409)});
 await click('定时备份');assert.equal(document.querySelector('dialog button.primary').disabled,true);
 await act(async()=>document.querySelectorAll('dialog input[type="checkbox"]')[1].click());await submit();
 assert.match(document.querySelector('dialog').textContent,/配置已变化/);assert.equal(JSON.parse(calls.find(c=>c.o.method==='PUT').o.body).version,0);
});
