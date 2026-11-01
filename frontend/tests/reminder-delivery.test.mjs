import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url);
const url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){
  let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  for(const [k,v] of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${k}'`,`from '${v}'`).replaceAll(`from "${k}"`,`from "${v}"`);
  return url(code);
}
const window=new Window({url:'http://localhost/#/tasks/reminders'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {ReminderDelivery}=await import(compile('../src/ReminderDelivery.tsx',{'./api':api,'./ui':ui}));
const row={id:'reminder-one',version:2,status:'scheduled',title:'项目演示',content:'携带提纲',due_at:Date.now()/1000+3600};
const target={kind:'qq',binding_id:'binding',binding_version:2,channel_version:1};
const base={version:0,reminder_version:2,target:{kind:'web'},target_label:'仅存站内',status:'station_only',expires_at:null};
const proposal={token:'opaque-confirm-token',expires_at:Date.now()/1000+300,title:row.title,content:row.content,due_at:row.due_at,timezone:'Asia/Shanghai',after:{...base,version:1,target,target_label:'测试 QQ · QQ 87654321',status:'configured',expires_at:row.due_at+3600}};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,handler){
  document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(uri,opt)=>handler(uri,opt);
  const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(ReminderDelivery,{row,close:()=>{}})));
}
async function click(text){const b=[...document.querySelectorAll('button')].find(x=>x.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function choose(){const s=document.querySelector('select[aria-label="提醒接收渠道"]');await act(async()=>{s.value=JSON.stringify(target);s.dispatchEvent(new window.Event('change',{bubbles:true}));});}
async function preview(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}
function read(uri){return uri==='/api/reminder-targets'?[{target,label:'测试 QQ · QQ 87654321',online:false}]:base;}

test('reminder preview shows exact content and target before explicit confirmation',async t=>{
  const writes=[];let configured=false;
  await mount(t,(uri,opt)=>{if(opt.method==='POST'){writes.push({uri,body:JSON.parse(opt.body)});if(uri.endsWith('/previews'))return response(proposal);configured=true;return response(proposal.after);}return response(configured&&uri.endsWith('/delivery')?proposal.after:read(uri));});
  await choose();await preview();assert.equal(writes.length,1);
  assert.match(document.querySelector('dialog').textContent,/携带提纲/);assert.match(document.querySelector('dialog').textContent,/QQ 87654321/);
  assert.equal(document.activeElement.textContent,row.title);
  assert.equal([...document.querySelectorAll('button')].find(x=>x.textContent==='确认投递').disabled,true);
  await act(async()=>document.querySelector('input[type=checkbox]').click());
  await click('确认投递');assert.equal(writes.length,2);
  assert.deepEqual(writes[1].body,{token:proposal.token,acknowledgement:'send_this_reminder_to_selected_qq'});
  assert.match(document.body.textContent,/尚未发送提醒/);
  assert.equal(document.activeElement.textContent,row.title);
});

test('lost confirmation can be retried with the same token without another preview',async t=>{
  const confirmations=[];
  await mount(t,(uri,opt)=>{if(opt.method==='POST'){if(uri.endsWith('/previews'))return response(proposal);confirmations.push(JSON.parse(opt.body));return response({detail:'暂时无法连接，请重试'},503);}return response(read(uri));});
  await choose();await preview();await act(async()=>document.querySelector('input[type=checkbox]').click());
  await click('确认投递');await click('确认投递');
  assert.equal(confirmations.length,2);assert.deepEqual(confirmations[0],confirmations[1]);
  assert.match(document.body.textContent,/暂时无法连接/);
});

test('unknown reminder result requires review and stays unknown after acknowledgement',async t=>{
  let reviewed=false;const writes=[];
  await mount(t,(uri,opt)=>{const data={...base,version:4,target,status:'unknown',target_label:'QQ 87654321',reviewed_at:reviewed?1:null};if(opt.method==='POST'){reviewed=true;writes.push(JSON.parse(opt.body));return response({...data,reviewed_at:1});}return response(uri.endsWith('/delivery')?data:read(uri));});
  await click('核对投递');assert.equal(writes.length,0);
  await act(async()=>document.querySelector('input[type=checkbox]').click());await click('结束等待');
  assert.deepEqual(writes,[{version:4,acknowledgement:'checked_no_resend'}]);
  assert.match(document.body.textContent,/结果未知/);assert.match(document.body.textContent,/未重发/);
});

test('expired preview and stale receiver cannot be confirmed',async t=>{
  await mount(t,(uri,opt)=>response(opt.method==='POST'?{...proposal,expires_at:1}:read(uri)));
  await choose();await preview();await act(async()=>document.querySelector('input[type=checkbox]').click());
  assert.match(document.body.textContent,/预览已过期/);
  assert.equal([...document.querySelectorAll('button')].find(x=>x.textContent==='确认投递').disabled,true);
});
