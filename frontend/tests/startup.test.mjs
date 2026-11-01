import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act, createElement} from 'react';

// Execute the real TSX and API helper; only CSS and the visual timeline are replaced.
const require=createRequire(import.meta.url);
const moduleUrl=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file) {
  return ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}
  }).outputText;
}
let component=compile('../src/StartupGate.tsx').replace(/import ['"]\.\.\/styles\/startup\.css['"];?/,'');
for (const [specifier,url] of Object.entries({
  react:pathToFileURL(require.resolve('react')).href,
  'react-dom':pathToFileURL(require.resolve('react-dom')).href,
  'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,
  './api':moduleUrl(compile('../src/api.ts')),
  './navigation':moduleUrl(compile('../src/navigation.ts'))
})) {
  component=component.replaceAll(`from '${specifier}'`,`from '${url}'`).replaceAll(`from "${specifier}"`,`from "${url}"`);
}
const window=new Window({url:'http://localhost/#/memory/list'});
Object.assign(globalThis,{
  window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,
  getComputedStyle:window.getComputedStyle.bind(window),requestAnimationFrame:window.requestAnimationFrame.bind(window),
  IS_REACT_ACT_ENVIRONMENT:true
});
const {createRoot}=await import('react-dom/client');
const {StartupGate}=await import(moduleUrl(component));
const session={id:'owner',username:'真实用户',csrf:'test-csrf',auto_memory:true};
const response=(status,body)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
const me=()=>response(200,session);
const anonymous=()=>response(401,{detail:'请先登录'});

async function setup(t,handler,{animation=true}={}) {
  const requests=[],mounts=[],ready=[],identities=[];
  document.body.innerHTML='<main id="main-content" tabindex="-1" hidden></main><div id="test-root"></div>';
  window.sessionStorage.clear();
  globalThis.fetch=(url,options)=>{requests.push({url,options});return Promise.resolve().then(()=>handler(url,options));};
  window.RhineLabBoot=animation ? {mount(host,options){
    mounts.push(options);
    const root=document.createElement('dialog');root.dataset.bootAnimation='test';
    root.style.setProperty('--rlb-style-ready','1');host.append(root);
    const controller=new AbortController();
    options.identityStep({container:root,signal:controller.signal}).then(identity=>identities.push(identity));
    return {destroy(){controller.abort();root.remove();},skip(){},finished:new Promise(()=>{})};
  }} : undefined;
  const root=createRoot(document.getElementById('test-root'));
  let key=0;
  async function render(){await act(async()=>root.render(createElement(StartupGate,{key:key++,onReady:s=>ready.push(s)})));}
  async function click(text){
    const button=[...document.querySelectorAll('button')].find(el=>el.textContent===text);
    assert.ok(button,`Missing button: ${text}`);
    await act(async()=>button.click());
  }
  async function login(password='test-only-password') {
    document.querySelector('[name=username]').value='owner';
    document.querySelector('[name=password]').value=password;
    await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  }
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await render();
  return {requests,mounts,ready,identities,render,click,login,clear:()=>act(async()=>root.render(null))};
}

test('wait for session result before mounting animation or loading business data',async t=>{
  let resolve;
  const qa=await setup(t,()=>new Promise(r=>{resolve=r;}));
  assert.match(document.body.textContent,/正在检查登录状态/);
  assert.equal(qa.mounts.length,0);assert.equal(qa.ready.length,0);
  assert.deepEqual(qa.requests.map(r=>r.url),['/api/auth/me']);
  await act(async()=>resolve(me()));
  assert.equal(qa.mounts[0].startAt,'identity');
});

test('anonymous opens and refreshes always start at brand, ignoring old played marker',async t=>{
  const qa=await setup(t,anonymous);
  assert.equal(qa.mounts[0].startAt,'brand');assert.equal(qa.mounts[0].speed,1);
  window.sessionStorage.setItem('lingxi.boot.seen','true');
  await qa.render();
  assert.deepEqual(qa.mounts.map(o=>o.startAt),['brand','brand']);
  assert.equal(qa.ready.length,0);
});

test('valid session and replay start at identity and require a fresh explicit confirmation',async t=>{
  const qa=await setup(t,me);
  assert.equal(qa.mounts[0].startAt,'identity');
  assert.match(document.body.textContent,/真实用户/);assert.equal(qa.ready.length,0);
  await qa.render();
  assert.deepEqual(qa.mounts.map(o=>o.startAt),['identity','identity']);
  assert.equal(qa.ready.length,0);
  await qa.click('确认进入');
  assert.equal(qa.requests.length,3);
  assert.equal(qa.mounts.length,2);assert.equal(qa.ready.length,0);
  assert.deepEqual(qa.identities,[{name:'真实用户'}]);
  await act(async()=>qa.mounts[1].onComplete({status:'completed'}));
  assert.deepEqual(qa.ready,[session]);
});

for (const [label,next,startAt] of [['anonymous',anonymous,'brand'],['authenticated',me,'identity']]) {
  test(`session service failure stays static; retry chooses ${label} start`,async t=>{
    let attempts=0;
    const qa=await setup(t,()=>++attempts===1 ? response(503,{detail:'身份服务暂不可用'}) : next());
    assert.equal(qa.mounts.length,0);assert.match(document.body.textContent,/身份服务暂不可用/);
    await qa.click('重试连接');
    assert.equal(qa.mounts.length,1);assert.equal(qa.mounts[0].startAt,startAt);assert.equal(qa.ready.length,0);
  });
}

test('network failure does not count as anonymous and can recover to valid identity',async t=>{
  let attempts=0;
  const qa=await setup(t,()=>{if(++attempts===1) throw new TypeError('offline');return me();});
  assert.equal(qa.mounts.length,0);assert.match(document.body.textContent,/无法连接服务器/);
  await qa.click('重试连接');assert.equal(qa.mounts[0].startAt,'identity');
});

test('wrong password and subsequent success stay in the same animation instance',async t=>{
  let attempts=0;
  const qa=await setup(t,(url)=>url.endsWith('/me') ? anonymous() : ++attempts===1 ? response(401,{detail:'用户名或密码不正确'}) : me());
  await qa.login('wrong-password');
  assert.match(document.body.textContent,/用户名或密码不正确/);
  assert.equal(document.querySelector('[name=password]').value,'');assert.equal(qa.mounts.length,1);
  await qa.login();
  assert.equal(qa.mounts.length,1);assert.equal(qa.ready.length,0);
  assert.deepEqual(qa.identities,[{name:'真实用户'}]);
  await act(async()=>qa.mounts[0].onComplete({status:'completed'}));
  assert.deepEqual(qa.ready,[session]);
});

test('identity sends TOTP or recovery proof and clears it after failure without entering',async t=>{
  const qa=await setup(t,url=>url.endsWith('/me')?anonymous():response(401,{detail:'双重验证未通过'}));
  document.querySelector('[name=code]').value='aaaa-bbbb-cccc-dddd-eeee';
  await qa.login();
  const loginRequest=qa.requests.find(r=>r.url.endsWith('/login'));
  assert.equal(JSON.parse(loginRequest.options.body).code,'aaaa-bbbb-cccc-dddd-eeee');
  assert.equal(document.querySelector('[name=code]').value,'');assert.equal(document.querySelector('[name=password]').value,'');
  assert.match(document.body.textContent,/双重验证未通过/);assert.equal(qa.ready.length,0);assert.equal(qa.mounts.length,1);
  assert.equal(window.localStorage.length,0);
});

test('session revoked during confirmation switches to password form without restarting intro',async t=>{
  let attempts=0;
  const qa=await setup(t,()=>++attempts===1 ? me() : anonymous());
  await qa.click('确认进入');
  assert.ok(document.querySelector('[name=password]'));
  assert.equal(qa.mounts.length,1);assert.equal(qa.mounts[0].startAt,'identity');assert.equal(qa.ready.length,0);
});

test('missing animation retains static identity and explicit server confirmation',async t=>{
  const qa=await setup(t,me,{animation:false});
  assert.equal(qa.mounts.length,0);assert.equal(qa.ready.length,0);
  await qa.click('确认进入');assert.deepEqual(qa.ready,[session]);
});

test('unmount during session check aborts the request and prevents a late animation',async t=>{
  let resolve;
  const qa=await setup(t,()=>new Promise(r=>{resolve=r;}));
  const signal=qa.requests[0].options.signal;
  await qa.clear();assert.equal(signal.aborted,true);
  await act(async()=>resolve(me()));
  assert.equal(qa.mounts.length,0);assert.equal(qa.ready.length,0);
});
