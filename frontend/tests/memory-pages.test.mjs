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
const stub=names=>'data:text/javascript;base64,'+Buffer.from(names.map(name=>`export const ${name}=${name==='modelPurposes'?'{}':'()=>null'};`).join('')).toString('base64');
const window=new Window({url:'http://localhost/#/memory/list'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts');
const paging=compile('../src/MemoryPaging.tsx',{'./api':api});
const {MemoryPage}=await import(compile('../src/pages.tsx',{
  './api':api,'./MemoryPaging':paging,'react-markdown':pathToFileURL(require.resolve('react-markdown')).href,
  './MediaPages':stub(['AttachmentList','ChatAttachments','VisionControl','MediaPreview']),
  './VoicePages':stub(['VoicePanel']),'./AgentTools':stub(['AgentToolScope']),
  './ModelSetup':stub(['ModelSetup','RoutingControls','modelPurposes']),
  './AddModelForm':stub(['AddModelForm']),'./ModelCalls':stub(['ModelCalls']),
  './ConversationPersona':stub(['ConversationPersona'])}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const memory=(id,content=id)=>({id,content,category:'preferences',subject:'self',status:'active',index_status:'ready',version:1,created_at:1793400000,revisions:[],source_ids:[]});
const page=(items=[],next_cursor=null)=>({items,next_cursor,as_of:1793400010});
async function mount(t,fetcher,initialPage='list'){
  document.body.innerHTML='<div id="root"></div>';globalThis.fetch=fetcher;
  const root=createRoot(document.getElementById('root'));
  const render=async page=>act(async()=>root.render(createElement(MemoryPage,{page,session:{auto_memory:false},onSession:()=>{}})));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await render(initialPage);return render;
}
const buttons=text=>[...document.querySelectorAll('button')].filter(b=>b.textContent===text);
async function click(text){assert.ok(buttons(text)[0],text);await act(async()=>buttons(text)[0].click());}

test('memory page loads only its own collection and traverses cursor history',async t=>{
  const urls=[];
  await mount(t,async url=>{urls.push(url);return response(new URL(url,'http://test').searchParams.has('cursor')?page([memory('older')]):page([memory('newer')],'cursor-token'));});
  assert.ok(urls.every(url=>url.startsWith('/api/memory-pages?')));
  assert.match(document.body.textContent,/newer/);
  await click('下一页');assert.match(document.body.textContent,/older/);assert.doesNotMatch(document.body.textContent,/newer/);
  assert.match(document.body.textContent,/第 2 页/);assert.equal(buttons('下一页')[0].disabled,true);
  await click('上一页');assert.match(document.body.textContent,/newer/);
});

test('candidate filter resets cursor and requests old pending records on the server',async t=>{
  const urls=[];const candidate={id:'candidate',content:'待核对',subject:'self',category:'projects',reason:'来源待核',source_id:'source',status:'pending'};
  await mount(t,async url=>{urls.push(url);return response(page([candidate],'next'));},'candidates');
  await click('下一页');
  await act(async()=>{const select=document.querySelector('select');select.value='pending';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
  const query=new URL(urls.at(-1),'http://test').searchParams;
  assert.equal(query.get('status'),'pending');assert.equal(query.has('cursor'),false);
  assert.match(document.body.textContent,/第 1 页/);
  assert.ok(urls.every(url=>url.startsWith('/api/memory-candidate-pages?')));
});

test('operation pages filter exports and deletions before pagination and handle loading',async t=>{
  const urls=[];const render=await mount(t,async url=>{urls.push(url);return response(page([{id:'op',kind:new URL(url,'http://test').searchParams.get('kind'),created_at:1793400000,status:'completed',result:{}}]));},'exports');
  assert.ok(buttons('下载文件')[0]);assert.equal(urls.at(-1),'/api/memory-operation-pages?kind=export');
  await render('deletions');assert.equal(urls.at(-1),'/api/memory-operation-pages?kind=delete');
  assert.match(document.body.textContent,/在线清理完成/);assert.equal(buttons('下载文件').length,0);
});

test('failed next page clears stale rows and can retry or return latest',async t=>{
  let fail=true;
  await mount(t,async url=>new URL(url,'http://test').searchParams.has('cursor')&&fail?response({detail:'分页凭据已过期'},400):response(page([memory('fact')],'next')));
  await click('下一页');assert.match(document.querySelector('[role=alert]').textContent,/已过期/);
  assert.equal(document.querySelectorAll('article').length,0);assert.equal(buttons('下一页')[0].disabled,true);
  fail=false;await click('重试加载');assert.match(document.body.textContent,/第 2 页/);
  await click('返回最新');assert.match(document.body.textContent,/第 1 页/);
});

test('late responses from an old section never replace the active page',async t=>{
  let finish;let pending=false;
  const render=await mount(t,async url=>{if(pending&&url.startsWith('/api/memory-pages'))return await new Promise(resolve=>finish=resolve);return response(page([memory('initial')],'next'));});
  pending=true;await click('下一页');assert.equal(buttons('下一页')[0].disabled,true);
  await render('exports');assert.match(document.body.textContent,/导出记录/);
  await act(async()=>finish(response(page([memory('late-sensitive')]))));
  assert.doesNotMatch(document.body.textContent,/late-sensitive/);
});

test('search changes hide old facts immediately and reset the cursor',async t=>{
  const urls=[];let finish;
  await mount(t,async url=>{urls.push(url);if(new URL(url,'http://test').searchParams.get('q'))return await new Promise(resolve=>finish=resolve);return response(page([memory('previous-fact')],'next'));});
  await click('下一页');
  await act(async()=>{const input=document.querySelector('input[aria-label="搜索记忆"]');Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'new search');input.dispatchEvent(new window.Event('input',{bubbles:true}));});
  assert.doesNotMatch(document.body.textContent,/previous-fact/);assert.equal(buttons('下一页')[0].disabled,true);
  assert.equal(new URL(urls.at(-1),'http://test').searchParams.has('cursor'),false);
  await act(async()=>finish(response(page([]))));assert.match(document.body.textContent,/尚无匹配记忆/);
});

test('confirmed candidate mutation refreshes the current page and preserves review controls',async t=>{
  const calls=[];let confirmed=false;
  const candidate={id:'c1',content:'用户原文',subject:'self',category:'profile',reason:'核对',source_id:'source',status:'pending'};
  await mount(t,async(url,options)=>{calls.push([url,options.method]);if(options.method==='POST'){confirmed=true;return response({...candidate,status:'confirmed'});}return response(page([{...candidate,status:confirmed?'confirmed':'pending'}]));},'candidates');
  await click('确认保存');assert.ok(calls.some(([url,method])=>url==='/api/memory-candidates/c1/actions'&&method==='POST'));
  assert.match(document.body.textContent,/已确认/);assert.equal(buttons('确认保存').length,0);
});
