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
const {SecurityPage}=await import(compile('../src/SecurityPage.tsx',{'./api':api,'./ui':ui}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

const state={version:0,enabled:false,recovery_remaining:0,pending:false,pending_expires_at:null};
const prepared={version:1,secret:'JBSWY3DPEHPK3PXP',otpauth_uri:'otpauth://synthetic-test',expires_at:9999999999};
const active={...state,version:2,enabled:true,recovery_remaining:10};
const recovery={status:active,recovery_codes:['aaaa-bbbb-cccc-dddd-eeee','1111-2222-3333-4444-5555']};

test('security status never requests or assumes setup credentials',async t=>{
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:[]);});
 assert.match(document.body.textContent,/尚未启用/);assert.equal(calls.length,2);assert.ok(calls.every(c=>c.o.method==='GET'));
 assert.equal(window.localStorage.length,0);assert.equal(window.sessionStorage.length,0);
});

test('enrollment requires password and remains unenabled until verified confirmation',async t=>{
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:u.endsWith('/setup')?prepared:u.endsWith('/confirm')?recovery:[]);});
 await click('启用验证');assert.equal(calls.filter(c=>c.o.method==='POST').length,0);
 await value('input[name="password"]','synthetic-password');await submit();assert.equal(document.querySelector('input[readonly]').value,prepared.secret);
 assert.equal(document.querySelector('dialog button.primary').disabled,true);assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
 await value('input[name="code"]','123456');await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await submit();
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/confirm')).o.body),{version:1,code:'123456'});
 assert.match(document.querySelector('dialog').textContent,/aaaa-bbbb/);assert.equal(document.querySelector('dialog button.primary').disabled,true);
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await click('完成保存');assert.equal(document.querySelector('dialog'),null);
 assert.ok(!document.body.textContent.includes(prepared.secret));assert.equal(window.localStorage.length,0);
});

test('setup network retry reuses request id without persisting password',async t=>{
 let attempts=0;const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:u.endsWith('/setup')?++attempts===1?{detail:'连接中断'}:prepared:[],u.endsWith('/setup')&&attempts===1?503:200);});
 await click('启用验证');await value('input[name="password"]','synthetic-password');await submit();assert.match(document.querySelector('dialog').textContent,/连接中断/);assert.equal(document.querySelector('input[name="password"]').value,'');
 await value('input[name="password"]','synthetic-password');await submit();
 const posts=calls.filter(c=>c.u.endsWith('/setup'));assert.equal(JSON.parse(posts[0].o.body).request_id,JSON.parse(posts[1].o.body).request_id);
});

test('disabling requires impact acknowledgement and both proofs; conflict retains dialog',async t=>{
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?active:u.endsWith('/disable')?{detail:'安全配置已变化'}:[],u.endsWith('/disable')?409:200);});
 await click('停用验证');assert.match(document.querySelector('dialog').textContent,/只通过密码登录/);assert.equal(document.querySelector('dialog button.danger').disabled,true);
 await value('input[name="password"]','synthetic-password');await value('input[name="code"]','aaaa-bbbb-cccc-dddd-eeee');await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await submit();
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/disable')).o.body),{version:2,password:'synthetic-password',code:'aaaa-bbbb-cccc-dddd-eeee'});
 assert.match(document.querySelector('dialog').textContent,/安全配置已变化/);assert.equal(document.querySelector('input[name="password"]').value,'');assert.equal(document.querySelector('input[name="code"]').value,'');
 assert.equal(document.querySelector('dialog input[type="checkbox"]').checked,false);assert.equal(document.querySelector('dialog button.danger').disabled,true);
});

test('recovery replacement explains invalidation and only displays returned codes',async t=>{
 await mount(t,SecurityPage,{},u=>response(u.endsWith('/security')?active:u.endsWith('/recovery')?recovery:[]));
 await click('更换恢复');assert.match(document.querySelector('dialog').textContent,/全部旧恢复码立即失效/);
 await value('input[name="password"]','synthetic-password');await value('input[name="code"]','123456');await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await submit();
 assert.match(document.querySelector('dialog').textContent,/恢复代码/);assert.match(document.querySelector('dialog').textContent,/1111-2222/);assert.equal(document.querySelector('dialog button[aria-label="关闭恢复代码"]').disabled,true);
});

test('current session revocation waits for confirmation then triggers identity gate',async t=>{
 let events=0;const calls=[];const listener=()=>events++;window.addEventListener('lingxi:unauthorized',listener);t.after(()=>window.removeEventListener('lingxi:unauthorized',listener));
 await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:o.method==='DELETE'?{status:'revoked'}:[{id:'session',created_at:1,expires_at:10,current:true}]);});
 await click('撤销会话');assert.equal(calls.filter(c=>c.o.method==='DELETE').length,0);assert.match(document.querySelector('dialog').textContent,/返回身份确认/);
 await click('确认撤销');assert.equal(events,1);assert.equal(calls.find(c=>c.o.method==='DELETE').u,'/api/auth/sessions/session');
});

test('unavailable security status never exposes enrollment actions',async t=>{
 await mount(t,SecurityPage,{},()=>response({detail:'安全状态暂不可用'},503));assert.match(document.body.textContent,/安全状态暂不可用/);assert.ok(![...document.querySelectorAll('button')].some(b=>b.textContent==='启用验证'));
});

async function passwordForm({factor=false}={}){
 await click('修改密码');await value('input[name="password"]','synthetic-current-password');
 await value('input[name="new_password"]','synthetic-new-password');await value('input[name="confirmation"]','synthetic-new-password');
 if(factor)await value('input[name="code"]','synthetic-recovery-code');
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());
}

test('password change requires explicit all-session acknowledgement and never assumes disabled factor proof',async t=>{
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:[]);});
 await click('修改密码');assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.match(document.querySelector('dialog').textContent,/全部登录会话将失效/);assert.equal(document.querySelector('input[name="code"]'),null);
 assert.equal(document.querySelector('input[name="new_password"]').autocomplete,'new-password');
 await submit();assert.ok(calls.every(c=>c.o.method==='GET'));
 assert.equal(window.localStorage.length,0);assert.equal(window.sessionStorage.length,0);
});

test('password mismatch stays local and focuses the accessible error',async t=>{
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?state:[]);});
 await passwordForm();await value('input[name="confirmation"]','does-not-match');await submit();
 assert.match(document.querySelector('[role="alert"]').textContent,/两次新密码须一致/);
 assert.equal(document.activeElement,document.querySelector('[role="alert"]'));assert.ok(calls.every(c=>c.o.method==='GET'));
});

test('successful password rotation submits current version and explicit proof, then enters identity gate',async t=>{
 let events=0;const listener=()=>events++;window.addEventListener('lingxi:unauthorized',listener);t.after(()=>window.removeEventListener('lingxi:unauthorized',listener));
 const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?active:u.endsWith('/password')?{status:'reauthentication_required'}:[]);});
 await passwordForm({factor:true});await submit();
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/password')).o.body),{version:2,password:'synthetic-current-password',new_password:'synthetic-new-password',confirmation:'synthetic-new-password',code:'synthetic-recovery-code',acknowledgement:'revoke_all_logins'});
 assert.equal(events,1);assert.equal(document.querySelector('dialog'),null);assert.equal(document.querySelector('input[type="password"]'),null);
 assert.equal(window.localStorage.length,0);assert.equal(window.sessionStorage.length,0);
});

test('wrong password and version conflicts clear secrets and require fresh acknowledgement',async t=>{
 let status=401;const calls=[];await mount(t,SecurityPage,{},(u,o)=>{calls.push({u,o});return response(u.endsWith('/security')?active:u.endsWith('/password')?{detail:status===401?'当前密码不正确':'安全配置已变化'}:[],u.endsWith('/password')?status:200);});
 await passwordForm({factor:true});await submit();assert.match(document.querySelector('[role="alert"]').textContent,/当前密码不正确/);
 for(const input of document.querySelectorAll('dialog input:not([type="checkbox"])'))assert.equal(input.value,'');
 assert.equal(document.querySelector('dialog button.primary').disabled,true);assert.equal(document.querySelector('dialog input[type="checkbox"]').checked,false);
 status=409;await value('input[name="password"]','current-again');await value('input[name="new_password"]','new-password-again');await value('input[name="confirmation"]','new-password-again');await value('input[name="code"]','fresh-proof');
 await act(async()=>document.querySelector('dialog input[type="checkbox"]').click());await submit();
 assert.match(document.querySelector('[role="alert"]').textContent,/安全配置已变化/);assert.equal(calls.filter(c=>c.u.endsWith('/password')).length,2);
 await click('刷新核对');assert.equal(calls.filter(c=>c.u.endsWith('/password')).length,2);
});

test('pending password submission blocks duplicates and dismissing until outcome arrives',async t=>{
 let resolve;let attempts=0;await mount(t,SecurityPage,{},(u)=>u.endsWith('/password')?(attempts++,new Promise(r=>resolve=r)):response(u.endsWith('/security')?state:[]));
 await passwordForm();await submit();await submit();assert.equal(attempts,1);
 assert.equal(document.querySelector('dialog button[aria-label="关闭修改密码"]').disabled,true);
 await act(async()=>resolve(response({detail:'请稍后重试'},429)));assert.match(document.querySelector('[role="alert"]').textContent,/请稍后重试/);
 assert.equal(document.querySelector('dialog button[aria-label="关闭修改密码"]').disabled,false);
});

test('connection failure does not retry or claim password was unchanged',async t=>{
 let attempts=0;await mount(t,SecurityPage,{},u=>{if(u.endsWith('/password')){attempts++;throw new TypeError('network interrupted');}return response(u.endsWith('/security')?state:[]);});
 await passwordForm();await submit();assert.equal(attempts,1);assert.match(document.querySelector('[role="alert"]').textContent,/修改结果尚未确认/);
 assert.equal(document.querySelector('input[name="password"]').value,'');assert.equal(document.querySelector('dialog button.primary').disabled,true);
});

test('revoked auth reads trigger identity gate but rejected current-password proof stays in its form',async t=>{
 let events=0,revoked=false;const listener=()=>events++;window.addEventListener('lingxi:unauthorized',listener);t.after(()=>window.removeEventListener('lingxi:unauthorized',listener));
 await mount(t,SecurityPage,{},u=>response(u.endsWith('/password')||revoked?{detail:'请重新核对'}:u.endsWith('/security')?state:[],u.endsWith('/password')||revoked?401:200));
 await passwordForm();await submit();assert.equal(events,0);assert.ok(document.querySelector('dialog'));
 await click('关闭');revoked=true;await click('刷新状态');assert.ok(events>=1);
});
