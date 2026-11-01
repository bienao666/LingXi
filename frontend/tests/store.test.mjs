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
const {StorePages,StoreConnectionPanel}=await import(compile('../src/StorePages.tsx',{'./api':api,'./ui':ui}));
const reviewer={username:'reviewer',role:'reviewer',namespace:null,audiences:[],csrf:'store-csrf-only'};
const publisher={username:'publisher',role:'publisher',namespace:'sample',audiences:['space-a'],csrf:'publisher-csrf'};
const release={id:'release',plugin_id:'sample.writer',version:'1.0.0',manifest:{name:'测试技能',description:'Synthetic fixture'},digest:'a'.repeat(64),visibility:'public',audience:'',status:'pending',version_number:7,checks:{schema:'passed'},review_note:'',submitted_at:1,published_at:null,artifact:{}};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('store login remains independent and never sends core csrf',async t=>{
 const calls=[];await mount(t,StorePages,{module:'store-review',page:'queue'},(u,o)=>{calls.push({u,o});return response({detail:'请登录独立商店账号'},401);});
 assert.match(document.body.textContent,/商店登录/);assert.ok(!calls.some(c=>c.u.startsWith('/api/auth')));assert.ok(!('X-CSRF-Token'in calls[0].o.headers));
 assert.equal(window.localStorage.length,0);assert.equal(window.sessionStorage.length,0);
});

test('publisher cannot use review controls',async t=>{
 await mount(t,StorePages,{module:'store-review',page:'queue'},()=>response(publisher));
 assert.match(document.body.textContent,/当前身份没有此后台权限/);assert.ok(!document.querySelector('button.primary'));
});

test('review requires named confirmation, reason and current version',async t=>{
 let finish;const calls=[];await mount(t,StorePages,{module:'store-review',page:'queue'},(u,o)=>{calls.push({u,o});if(u.endsWith('/decision'))return new Promise(r=>finish=r);return response(u.endsWith('/auth/me')?reviewer:[release]);});
 await click('审核上架');assert.ok(document.querySelector('dialog'));assert.equal(calls.filter(c=>c.u.endsWith('/decision')).length,0);
 await value('textarea','核对权限和用途后同意测试版本');await submit();
 const request=calls.find(c=>c.u.endsWith('/decision'));assert.deepEqual(JSON.parse(request.o.body),{version:7,action:'approve',note:'核对权限和用途后同意测试版本'});assert.equal(request.o.headers['X-Store-CSRF'],'store-csrf-only');
 assert.equal(document.querySelector('dialog button.primary').disabled,true);
 await act(async()=>finish(response({detail:'审核状态已变化，请重新查看'},409)));
 assert.match(document.querySelector('dialog').textContent,/审核状态已变化/);assert.equal(document.querySelector('textarea').value,'核对权限和用途后同意测试版本');
});

test('revocation explicitly explains terminal state and no fee reversal',async t=>{
 await mount(t,StorePages,{module:'store-review',page:'releases'},u=>response(u.endsWith('/auth/me')?reviewer:[{...release,status:'published'}]));
 await click('安全撤销');assert.match(document.querySelector('dialog').textContent,/撤销不可恢复/);assert.match(document.querySelector('dialog').textContent,/不能撤回已发送的内容或费用/);
});

test('publication waits for scope preview and explicit checkbox',async t=>{
 const calls=[];await mount(t,StorePages,{module:'store-developer',page:'plugins'},(u,o)=>{calls.push({u,o});return response(u.endsWith('/auth/me')?publisher:[]);});
 await click('提交版本');await submit();assert.match(document.querySelector('dialog').textContent,/公开审核队列/);assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.ok(!calls.some(c=>c.o.method==='POST'));
});

test('core trust configuration is only saved after preview approval',async t=>{
 const calls=[];await mount(t,StoreConnectionPanel,{},(u,o)=>{calls.push({u,o});return response(null);});
 await value('input[type="url"]','https://store.example');await value('textarea','public-root-fixture');
 await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
 assert.match(document.querySelector('dialog').textContent,/固定错误的根公钥/);assert.equal(calls.filter(c=>c.o.method==='PUT').length,0);
});

test('store outage shows recoverable error without logging out core',async t=>{
 let unauthorized=0;const listener=()=>unauthorized++;window.addEventListener('lingxi:unauthorized',listener);t.after(()=>window.removeEventListener('lingxi:unauthorized',listener));
 await mount(t,StorePages,{module:'store-review',page:'queue'},()=>response({detail:'独立商店暂不可用'},503));
 assert.match(document.body.textContent,/独立商店暂不可用/);assert.equal(unauthorized,0);assert.match(document.body.textContent,/重试连接/);
});
