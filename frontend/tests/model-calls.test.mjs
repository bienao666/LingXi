import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url);
function compile(file,imports={}){
  let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  for(const [name,target]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${name}'`,`from '${target}'`).replaceAll(`from "${name}"`,`from "${target}"`);
  return 'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
}
const window=new Window({url:'http://localhost/#/models/calls'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {ModelCalls}=await import(compile('../src/ModelCalls.tsx',{'./api':api,'./ui':ui}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const entry=(id='first')=>({id,created_at:1793400000,model_id:'former-model',purpose:'chat',status:'completed',duration_ms:20,usage:{},billing:{status:'unknown'},config:{model:'历史模型',provider_version:2},error:null,cost:null});
const page=(items=[entry()],next_cursor=null)=>({items,next_cursor,as_of:1793400010});
async function mount(t,fetcher){
  document.body.innerHTML='<div id="root"></div>';globalThis.fetch=fetcher;
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(ModelCalls)));
}
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
async function click(text){assert.ok(button(text),text);await act(async()=>button(text).click());}
async function change(label,value){const field=[...document.querySelectorAll('label')].find(el=>el.textContent.startsWith(label)).querySelector('input,select');await act(async()=>{if(field.tagName==='INPUT'){Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(field,value);field.dispatchEvent(new window.Event('input',{bubbles:true}));}else{field.value=value;field.dispatchEvent(new window.Event('change',{bubbles:true}));}});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('shows persisted versions, unknown usage and cost, and keyboard-scrollable table',async t=>{
  const urls=[];
  await mount(t,async url=>{urls.push(url);return response(url==='/api/models'?[]:page());});
  assert.match(document.body.textContent,/历史模型/);assert.match(document.body.textContent,/用量未知/);assert.match(document.body.textContent,/费用未知/);
  assert.match(document.body.textContent,/provider_version/);assert.equal(document.querySelector('div[role=region]').tabIndex,0);
  assert.equal(button('上一页').disabled,true);assert.equal(button('下一页').disabled,true);
  assert.ok(!urls.includes('/api/admin/model-calls'));assert.ok(!urls.includes('/api/providers'));
});

test('navigates forward and back with cursor, then refreshes latest',async t=>{
  const urls=[];
  await mount(t,async url=>{urls.push(url);return response(url==='/api/models'?[]:new URL(url,'http://test').searchParams.has('cursor')?page([entry('older')]):page([entry()], 'opaque+token/='));});
  await click('下一页');assert.match(document.body.textContent,/第 2 页/);assert.match(document.body.textContent,/older/);
  const query=new URL(urls.at(-1),'http://test').searchParams;assert.equal(query.get('cursor'),'opaque+token/=');
  await click('上一页');assert.match(document.body.textContent,/第 1 页/);assert.equal(button('上一页').disabled,true);
  await click('下一页');await click('最新记录');assert.match(document.body.textContent,/第 1 页/);assert.equal(new URL(urls.at(-1),'http://test').searchParams.has('cursor'),false);
});

test('draft filters do not request until applied; apply and clear reset paging',async t=>{
  const urls=[];
  await mount(t,async url=>{urls.push(url);return response(url==='/api/models'?[]:page([entry()], 'next'));});
  await click('下一页');const before=urls.length;
  await change('结果状态','failed');await change('时间范围','7');await change('调用用途','tts');
  assert.equal(urls.length,before);
  await submit();const query=new URL(urls.at(-1),'http://test').searchParams;
  assert.equal(query.get('status'),'failed');assert.equal(query.get('days'),'7');assert.equal(query.get('purpose'),'tts');assert.equal(query.has('cursor'),false);
  assert.match(document.body.textContent,/第 1 页/);await click('清除筛选');
  assert.equal(new URL(urls.at(-1),'http://test').searchParams.get('status'),'all');
});

test('expired cursor error offers retry and return latest without retaining wrong page',async t=>{
  let fail=true;const urls=[];
  await mount(t,async url=>{urls.push(url);if(url==='/api/models')return response([]);return new URL(url,'http://test').searchParams.has('cursor')&&fail?response({detail:'分页凭据已过期'},400):response(page([entry()], 'next'));});
  await click('下一页');assert.match(document.querySelector('[role=alert]').textContent,/过期/);assert.equal(document.querySelector('tbody'),null);
  fail=false;await click('重试加载');assert.match(document.body.textContent,/第 2 页/);
  assert.equal(new URL(urls.at(-1),'http://test').searchParams.get('cursor'),'next');
  await click('最新记录');assert.match(document.body.textContent,/第 1 页/);
});

test('custom historical model ID is trimmed and sent exactly without requiring the catalog',async t=>{
  const urls=[];
  await mount(t,async url=>{urls.push(url);return response(url==='/api/models'?[]:page());});
  await change('指定模型',' historical-id ');await submit();
  assert.equal(new URL(urls.at(-1),'http://test').searchParams.get('model_id'),'historical-id');
});

test('late response cannot replace current filters, and pending navigation is disabled',async t=>{
  let finish;
  await mount(t,async url=>{if(url==='/api/models')return response([]);const query=new URL(url,'http://test').searchParams;if(query.has('cursor'))return await new Promise(resolve=>finish=resolve);return response(page([entry()], 'next'));});
  await click('下一页');assert.equal(button('下一页').disabled,true);assert.equal(button('上一页').disabled,true);
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.match(document.body.textContent,/第 1 页/);
  await act(async()=>finish(response(page([entry('stale-page')]))));
  assert.doesNotMatch(document.body.textContent,/stale-page/);
});

test('catalog failure does not hide history; empty and estimated billing are honest',async t=>{
  let empty=false;
  await mount(t,async url=>url==='/api/models'?response({detail:'目录暂不可用'},503):response(empty?page([]):page([{...entry(),billing:{status:'estimated',currency:'CNY',amount:'0.0001',price:{version:1}},config:{model:'<script>unsafe</script>'}}])));
  assert.match(document.body.textContent,/模型目录暂不可用/);assert.match(document.body.textContent,/估算 CNY 0.0001/);
  assert.equal(document.querySelector('script'),null);assert.match(document.body.textContent,/<script>unsafe/);
  empty=true;await click('最新记录');assert.match(document.body.textContent,/暂无符合条件/);assert.equal(button('下一页').disabled,true);
});
