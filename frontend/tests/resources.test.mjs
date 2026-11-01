import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';

const require=createRequire(import.meta.url);
const moduleUrl=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){
  let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  for(const [specifier,url] of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${specifier}'`,`from '${url}'`).replaceAll(`from "${specifier}"`,`from "${url}"`);
  return moduleUrl(code);
}
const window=new Window({url:'http://localhost/#/settings/resources'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {ResourceSettings}=await import(compile('../src/ResourceSettings.tsx',{'./api':api,'./ui':ui}));
const current={version:1,roots:['projects'],mounted:true,available:true,reason:null,mode:'directory_metadata_only',updated_at:1};
const checked={before:current,after:{roots:['shared']},token:'opaque-resource-preview',expires_at:Date.now()/1000+300,note:'旧资源任务和派生上下文停止使用；已发送内容无法撤回。'};
const response=(data,status=200)=>new Response(JSON.stringify(data),{status});
async function mount(t,handler=()=>undefined){
  const requests=[];document.body.innerHTML='<div id="root"></div>';
  globalThis.fetch=async(url,options)=>{requests.push({url,...options});const supplied=await handler(url,options);if(supplied)return supplied;
    if(url==='/api/resource-policy')return response(current);
    throw new Error('unexpected '+url);
  };
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(ResourceSettings)));
  return requests;
}
async function click(text){const button=[...document.querySelectorAll('button')].find(e=>e.textContent===text);assert.ok(button,text);await act(async()=>button.click());}
async function change(value){await act(async()=>{const el=document.querySelector('textarea');el.value=value;el.dispatchEvent(new window.Event('input',{bubbles:true}));el.dispatchEvent(new window.Event('change',{bubbles:true}));});}

test('scope edit previews before granting; cancelling returns focus',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response(checked):undefined);
  await change('shared\n another ');
  const trigger=[...document.querySelectorAll('button')].find(b=>b.textContent==='预览更改');trigger.focus();
  await click('预览更改');
  assert.deepEqual(JSON.parse(requests.find(r=>r.method==='POST').body),{version:1,roots:['shared','another'],read_roots:[]});
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
  assert.match(document.querySelector('dialog').textContent,/原有范围.*projects.*新的范围.*shared/);
  await click('取消更改');assert.equal(document.querySelector('dialog'),null);assert.equal(document.activeElement,trigger);
});

test('grant confirmation submits only reviewed token and explicit acknowledgement',async t=>{
  const requests=await mount(t,(url,options)=>url.endsWith('/previews')?response(checked):options.method==='PUT'?response({...current,version:2,roots:['shared']}):undefined);
  await click('预览更改');await click('确认授权');
  assert.deepEqual(JSON.parse(requests.find(r=>r.method==='PUT').body),{token:checked.token,acknowledgement:'invalidate_previous_resource_contexts'});
  assert.equal(document.querySelector('dialog'),null);assert.match(document.body.textContent,/资源授权 v2 已生效/);
});

test('revoke all has an explicit preview and cannot silently commit',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({...checked,after:{roots:[]}}):undefined);
  await click('撤销全部');assert.deepEqual(JSON.parse(requests.find(r=>r.method==='POST').body).roots,[]);
  assert.match(document.querySelector('dialog').textContent,/不允许访问任何目录/);
  assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent==='确认撤销'));
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('offline mount prevents expansion but leaves revoke enabled',async t=>{
  await mount(t,url=>url==='/api/resource-policy'?response({...current,mounted:false,available:false,reason:'资料卷不可用'}):undefined);
  assert.ok(document.querySelector('textarea').disabled);
  assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent==='预览更改').disabled);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='撤销全部').disabled,false);
  assert.match(document.body.textContent,/离线时仍可撤销/);
});

test('stale confirmation explains error and is never automatically replayed',async t=>{
  let writes=0;await mount(t,(url,options)=>url.endsWith('/previews')?response(checked):options.method==='PUT'?(writes++,response({detail:'资源授权已变化，请重新预览'},409)):undefined);
  await click('预览更改');await click('确认授权');
  assert.equal(writes,1);assert.match(document.querySelector('dialog [role=alert]').textContent,/重新预览/);
});

test('expired preview cannot be confirmed',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({...checked,expires_at:1}):undefined);
  await click('预览更改');assert.match(document.querySelector('dialog').textContent,/预览已过期/);
  await click('确认授权');assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('duplicate submits stay blocked while preview is pending',async t=>{
  let finish;const requests=await mount(t,url=>url.endsWith('/previews')?new Promise(resolve=>finish=resolve):undefined);
  const form=document.querySelector('form');
  await act(async()=>form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  await act(async()=>form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(requests.filter(r=>r.method==='POST').length,1);
  await act(async()=>finish(response(checked)));
  assert.ok(document.querySelector('dialog'));
});

test('failed initial read offers retry without guessed scope or enabled grant form',async t=>{
  let reads=0;await mount(t,url=>url==='/api/resource-policy'&&++reads===1?response({detail:'连接失败'},503):undefined);
  assert.equal(document.querySelector('textarea'),null);assert.match(document.querySelector('[role=alert]').textContent,/连接失败/);
  await click('重新加载');assert.equal(document.querySelector('textarea').value,'projects');
});

test('document roots are separately entered and reviewed; no implicit grant before confirmation',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({...checked,after:{roots:['projects'],read_roots:['projects/docs']}}):undefined);
  await act(async()=>{const input=document.querySelector('textarea[name=resource_read_roots]');input.value='projects/docs';input.dispatchEvent(new window.Event('input',{bubbles:true}));});
  await click('预览更改');
  const body=JSON.parse(requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body,{version:1,roots:['projects'],read_roots:['projects/docs']});
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
  assert.match(document.querySelector('dialog').textContent,/新正文范围.*projects\/docs/);
  await click('取消更改');
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('revoke all clears both existing metadata and document scope in the preview',async t=>{
  const requests=await mount(t,url=>url==='/api/resource-policy'?response({...current,read_roots:['projects/docs']}):url.endsWith('/previews')?response({...checked,after:{roots:[],read_roots:[]}}):undefined);
  assert.equal(document.querySelector('textarea[name=resource_read_roots]').value,'projects/docs');
  await click('撤销全部');
  const body=JSON.parse(requests.find(r=>r.method==='POST').body);
  assert.deepEqual(body,{version:1,roots:[],read_roots:[]});
  assert.match(document.querySelector('dialog').textContent,/新正文范围不允许读取正文/);
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});


test('resource skill link targets an existing menu and route is wired',()=>{
  const source=readFileSync(new URL('../src/ResourceSettings.tsx',import.meta.url),'utf8');
  assert.match(source,/href="#\/plugins\/skills"/);
  const main=readFileSync(new URL('../src/main.tsx',import.meta.url),'utf8');
  assert.match(main,/route.id === 'resources' \? <ResourceSettings\/>/);
});
