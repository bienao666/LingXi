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
const window=new Window({url:'http://localhost/#/proactive/preferences'});
Object.assign(globalThis,{window,document:window.document,localStorage:window.localStorage,location:window.location,HTMLElement:window.HTMLElement,Event:window.Event,CustomEvent:window.CustomEvent,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {BrowserNoticeAgent,BrowserNotificationSettings}=await import(compile('../src/BrowserNotifications.tsx',{'./api':api,'./ui':ui}));
const ROOT='/api/browser-notifications';
const preference={enabled:true,version:1,scope:'owner-scope'};
const receipt={id:'notice-one',status:'candidate',created_at:Date.now()/1000,expires_at:Date.now()/1000+120,attempted_at:null,eligible:true};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const localKey='lingxi.browser-notices.owner-scope';
let instances=[],ticks=[],permissionRequests=0,throwConstruction=false,root;
class FakeNotification{
 static permission='granted';
 static async requestPermission(){permissionRequests++;return this.permission='granted';}
 constructor(title,options){if(throwConstruction)throw new Error('unsupported constructor');this.title=title;this.options=options;this.closed=false;instances.push(this);}
 close(){this.closed=true;this.onclose?.();}
}
async function mount(t,component,handler,{permission='granted',hidden=true,optin=true,support=true}={}){
 document.body.innerHTML='<div id="root"></div>';window.localStorage.clear();
 if(optin)window.localStorage.setItem(localKey,'1');
 Object.defineProperty(window,'isSecureContext',{value:true,configurable:true});
 Object.defineProperty(window,'Notification',{value:support?FakeNotification:undefined,writable:true,configurable:true});
 Object.defineProperty(document,'visibilityState',{value:hidden?'hidden':'visible',writable:true,configurable:true});
 instances=[];ticks=[];permissionRequests=0;throwConstruction=false;FakeNotification.permission=permission;
 const originalInterval=window.setInterval,originalClear=window.clearInterval;
 window.setInterval=fn=>{ticks.push(fn);return ticks.length;};window.clearInterval=()=>{};
 globalThis.fetch=async(u,o)=>handler(u,o);
 root=createRoot(document.getElementById('root'));
 t.after(async()=>{await act(async()=>root.unmount());window.setInterval=originalInterval;window.clearInterval=originalClear;await window.happyDOM.abort();});
 await act(async()=>root.render(createElement(component)));
}
function protocol(options={}){
 let claimed=false;const calls=[];
 const handler=(u,o)=>{calls.push({u,o});
  if(u===ROOT+'/state')return response({preference,receipts:[{...receipt,status:claimed?'attempting':'candidate',eligible:!(claimed&&options.invalidate)}],note:'请求展示不等于送达'});
  if(u===ROOT+'/claims'){claimed=true;return response({id:receipt.id,expires_at:receipt.expires_at,path:options.path||'#/proactive/history'});}
  return response({...receipt,status:JSON.parse(o.body).status});
 };return {calls,handler};
}
async function click(name){const el=[...document.querySelectorAll('button')].find(b=>b.textContent===name);assert.ok(el,name);await act(async()=>el.click());}

test('background notification uses fixed private text and never reports delivered',async t=>{
 const p=protocol();await mount(t,BrowserNoticeAgent,p.handler);
 assert.equal(instances.length,1);assert.equal(permissionRequests,0);
 assert.equal(instances[0].title,'灵犀 · 新的交流');assert.equal(instances[0].options.silent,true);
 assert.equal(instances[0].options.requireInteraction,false);assert.match(instances[0].options.body,/站内交流/);
 assert.ok(!JSON.stringify(instances[0].options).includes(receipt.id+'content'));
 assert.deepEqual(JSON.parse(p.calls.find(x=>x.u===ROOT+'/claims').o.body),{consent_version:1,background:true});
 assert.deepEqual(JSON.parse(p.calls.find(x=>x.u.endsWith('/ack')).o.body),{status:'requested'});
});

for(const [name,config]of Object.entries({foreground:{hidden:false},no_optin:{optin:false},denied:{permission:'denied'},unrequested:{permission:'default'},unsupported:{support:false}}))test(name+' never claims or prompts',async t=>{
 const p=protocol();await mount(t,BrowserNoticeAgent,p.handler,config);
 assert.equal(instances.length,0);assert.equal(permissionRequests,0);assert.ok(p.calls.every(x=>x.o.method==='GET'));
});

test('source invalidation during claim roundtrip prevents display',async t=>{
 const p=protocol({invalidate:true});await mount(t,BrowserNoticeAgent,p.handler);assert.equal(instances.length,0);
 assert.equal(p.calls.filter(x=>x.u===ROOT+'/state').length,2);
});

test('unexpected notification destination never navigates or displays',async t=>{
 const p=protocol({path:'https://foreign.invalid'});await mount(t,BrowserNoticeAgent,p.handler);assert.equal(instances.length,0);
});

test('foreground change while a claim is pending consumes no late display',async t=>{
 let resolve;const p=protocol();await mount(t,BrowserNoticeAgent,(u,o)=>u===ROOT+'/claims'?new Promise(r=>resolve=r):p.handler(u,o));
 await act(async()=>{document.visibilityState='visible';document.dispatchEvent(new window.Event('visibilitychange'));resolve(response({id:receipt.id,expires_at:receipt.expires_at,path:'#/proactive/history'}));});
 assert.equal(instances.length,0);
});

test('permission and consent are rechecked after the final state request',async t=>{
 let reads=0;const p=protocol();await mount(t,BrowserNoticeAgent,(u,o)=>{
  if(u===ROOT+'/state'&&++reads===2)FakeNotification.permission='denied';return p.handler(u,o);
 });assert.equal(instances.length,0);
});

test('constructor failure acknowledges failure without replay',async t=>{
 const p=protocol();await mount(t,BrowserNoticeAgent,(u,o)=>{if(u===ROOT+'/claims')throwConstruction=true;return p.handler(u,o);});
 assert.equal(instances.length,0);assert.deepEqual(JSON.parse(p.calls.find(x=>x.u.endsWith('/ack')).o.body),{status:'failed'});
});

test('uncertain acknowledgement does not reconstruct the same notification',async t=>{
 let claimed=false;const p=protocol();await mount(t,BrowserNoticeAgent,(u,o)=>{
  if(u===ROOT+'/claims'){if(claimed)return response({id:null});claimed=true;}
  if(u.endsWith('/ack'))return response({detail:'network uncertain'},503);
  return p.handler(u,o);
 });await act(async()=>ticks[0]());assert.equal(instances.length,1);
});

test('invalidated state closes an already displayed generic hint',async t=>{
 let withdrawn=false;const p=protocol();await mount(t,BrowserNoticeAgent,(u,o)=>withdrawn&&u===ROOT+'/state'?response({preference:{...preference,enabled:false},receipts:[]}):p.handler(u,o));
 assert.equal(instances.length,1);withdrawn=true;await act(async()=>ticks[0]());assert.equal(instances[0].closed,true);
});

test('unmount closes visible hints and rejects a pending claim result',async t=>{
 const p=protocol();await mount(t,BrowserNoticeAgent,p.handler);await act(async()=>root.render(null));assert.equal(instances[0].closed,true);
});

test('settings only requests permission after a user click then persists opt-in',async t=>{
 let enabled=false;const calls=[];
 await mount(t,BrowserNotificationSettings,(u,o)=>{calls.push({u,o});if(o.method==='PUT'){enabled=true;return response(preference);}return response({preference:{...preference,enabled,version:enabled?1:0},receipts:[],note:'请求展示不等于送达，只在页面运行时检查'});},{permission:'default',optin:false,hidden:false});
 assert.equal(permissionRequests,0);assert.equal(calls.filter(x=>x.o.method==='PUT').length,0);
 await click('开启通知');assert.equal(permissionRequests,1);assert.equal(window.localStorage.getItem(localKey),'1');
 assert.match(document.body.textContent,/本浏览器已开启/);assert.match(document.body.textContent,/请求展示/);
});

test('denied permission leaves settings reversible and never writes consent',async t=>{
 const calls=[];await mount(t,BrowserNotificationSettings,(u,o)=>{calls.push({u,o});return response({preference,receipts:[],note:'不保证送达'});},{permission:'denied',optin:false});
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='开启通知').disabled,true);
 assert.equal(permissionRequests,0);assert.ok(calls.every(x=>x.o.method==='GET'));assert.match(document.body.textContent,/已经拒绝/);
});
