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
const window=new Window({url:'http://localhost/#/settings/media'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {MediaStorage}=await import(compile('../src/MediaStorage.tsx',{'./api':api,'./ui':ui}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const root='/api/admin/media-storage';
const row={id:'scan-one',status:'review',days:2,created_at:1,expires_at:Date.now()/1000+300,finished_at:null,digest:'a'.repeat(64),items:[{key:'a'.repeat(32)+'.blob',bytes:1024,modified_at:1}],results:[],summary:{entries:12,recognized:10,tracked:8,retained:1,recognized_bytes:5000},error:null};
async function mount(t,rows,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>o.method==='GET'?response(u===root?{records:10,logical_bytes:51200,deleted_records:3,disk_status:'ready',free_bytes:null,total_bytes:null}:rows):handler(u,o);const instance=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>instance.unmount());await window.happyDOM.abort();});await act(async()=>instance.render(createElement(MediaStorage)));}
async function click(label){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===label);assert.ok(b,label);await act(async()=>b.click());}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}
test('storage scan submits no cleanup and retries same ID after a lost response',async t=>{
 const writes=[];let once=false;await mount(t,[],(u,o)=>{writes.push({u,body:JSON.parse(o.body)});if(!once){once=true;throw Error('offline');}return response({...row,status:'queued'});});
 assert.match(document.body.textContent,/运行卷可用 未知/);await submit();assert.match(document.body.textContent,/无法连接服务器/);await submit();assert.equal(writes.length,2);assert.equal(writes[0].u,root+'/scans');assert.equal(writes[0].body.request_id,writes[1].body.request_id);assert.equal(writes[0].body.days,2);
});
test('cleanup requires separate acknowledgement and exact preview digest',async t=>{
 const writes=[];await mount(t,[row],(u,o)=>{writes.push({u,body:JSON.parse(o.body)});return response({...row,status:'confirmed'});});await click('查看巡检');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/不可撤销/);const confirm=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认清理');assert.equal(confirm.disabled,true);assert.equal(document.activeElement.type,'checkbox');await act(async()=>document.activeElement.click());await click('确认清理');assert.deepEqual(writes[0],{u:root+'/runs/scan-one/confirm',body:{digest:'a'.repeat(64),acknowledgement:'remove_untracked_media_files'}});
});
test('cleanup source conflict remains visible without making success claims',async t=>{
 await mount(t,[row],()=>response({detail:'扫描不属于当前登录或内容已变化'},409));await click('查看巡检');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认清理');assert.match(document.querySelector('dialog').textContent,/内容已变化/);assert.doesNotMatch(document.querySelector('dialog').textContent,/已释放/);
});
test('partial scan and unknown results stay explicit and cannot be confirmed again',async t=>{
 const unknown={...row,status:'unknown',summary:{...row.summary,partial:true,has_more:true,reference_limited:true},results:[{...row.items[0],status:'unknown'}]};await mount(t,[unknown],()=>assert.fail('must not write'));await click('查看巡检');const text=document.querySelector('dialog').textContent;assert.match(text,/部分扫描/);assert.match(text,/核对预算已用尽/);assert.match(text,/结果待核/);assert.equal([...document.querySelectorAll('button')].filter(b=>b.textContent==='确认清理').length,0);
});
test('pending cleanup cancellation is explicit and preserves finished receipts',async t=>{
 const writes=[];await mount(t,[{...row,status:'cleaning',results:[{...row.items[0],status:'removed'}]}],(u,o)=>{writes.push(u);return response({...row,status:'cancelled'});});await click('查看巡检');assert.equal(writes.length,0);await click('取消后续');assert.deepEqual(writes,[root+'/runs/scan-one/cancel']);assert.match(document.querySelector('dialog').textContent,/已移除/);
});
test('rapid repeated confirmation creates one request while pending',async t=>{
 let release;const calls=[];await mount(t,[row],(u,o)=>{calls.push(u);return new Promise(resolve=>{release=()=>resolve(response({...row,status:'confirmed'}));});});await click('查看巡检');await act(async()=>document.querySelector('dialog input').click());const b=[...document.querySelectorAll('button')].find(v=>v.textContent==='确认清理');await act(async()=>{b.click();b.click();});assert.equal(calls.length,1);assert.equal(document.querySelector('[aria-label="关闭巡检详情"]').disabled,true);await act(async()=>release());
});
