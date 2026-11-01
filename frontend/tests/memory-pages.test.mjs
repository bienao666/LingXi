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
const ui=compile('../src/ui.tsx',{'./api':api});
const paging=compile('../src/MemoryPaging.tsx',{'./api':api});
const times=compile('../src/MemoryTimes.tsx');
const review=compile('../src/MemoryReview.tsx',{'./api':api,'./ui':ui,'./MemoryTimes':times});
const {MemoryPage}=await import(compile('../src/pages.tsx',{'./MemoryTimes':times,'./MemoryReview':review,
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
  document.body.innerHTML='<main id="main-content" tabindex="-1"><div id="root"></div></main>';globalThis.fetch=fetcher;
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
  const candidate={id:'c1',content:'用户原文',subject:'self',category:'profile',reason:'核对',source_id:'source',status:'pending',origin:{}};
  await mount(t,async(url,options)=>{calls.push([url,options.method]);
    if(url.includes('/sources'))return response({candidate,source:{content:'这是用户原文',created_at:1},related:[],related_count:0,matched_count:0,truncated:false,warning:'请核对'});
    if(url.endsWith('/review'))return response({before:null,after:candidate,resolution:'keep',token:'review-token',expires_at:2000000000,warning:'不会覆盖'});
    if(url.endsWith('/actions')){confirmed=true;assert.equal(JSON.parse(options.body).review_token,'review-token');return response({...candidate,status:'confirmed'});}
    return response(page([{...candidate,status:confirmed?'confirmed':'pending'}]));},'candidates');
  assert.equal(buttons('确认保存').length,0);await click('核对保存');await click('预览保存');
  assert.equal(buttons('确认保存')[0].disabled,true);
  await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');assert.ok(calls.some(([url,method])=>url==='/api/memory-candidates/c1/actions'&&method==='POST'));
  assert.match(document.body.textContent,/已确认/);assert.equal(buttons('确认保存').length,0);
  assert.equal(document.activeElement.id,'main-content');
});

test('candidate review compares a chosen old item but never inherits its dates',async t=>{
  const prior={...memory('old-fact'),source_ids:[],event_at:'2020-01-01',version:4};
  const candidate={id:'c1',content:'新的用户原文',subject:'self',category:'preferences',reason:'可能相关',source_id:'new-source',status:'pending',origin:{}};
  let submitted;
  await mount(t,async(url,options)=>{
    if(url.includes('/sources'))return response({candidate,source:{content:'新的用户原文，请记住',created_at:1},related:[prior],related_count:1,matched_count:1,truncated:false,warning:'同类不代表矛盾'});
    if(url.endsWith('/review')){submitted=JSON.parse(options.body);return response({before:prior,after:{...candidate,event_at:null},resolution:'replace',token:'token',expires_at:2000000000,warning:'旧版本保留'});}
    return response(page([candidate]));},'candidates');
  await click('核对保存');assert.match(document.body.textContent,/不代表矛盾/);
  await act(async()=>document.querySelectorAll('dialog input[type=radio]')[1].click());await click('预览保存');
  assert.equal(document.activeElement.textContent,'纠正旧项');
  assert.equal(submitted.resolution,'replace');assert.equal(submitted.target_version,4);assert.equal(submitted.event_at,null);
  assert.match(document.querySelector('[aria-label="保存预览"]').textContent,/2020-01-01/);assert.match(document.querySelector('[aria-label="保存预览"]').textContent,/时间未说明/);
  await click('返回修改');assert.equal(buttons('确认保存').length,0);assert.equal(document.querySelector('[name=event_at]').value,'');
  assert.equal(document.activeElement.textContent,'待存内容');
});

test('stale preview stops confirmation, focuses error and requires source reread',async t=>{
  const candidate={id:'c1',content:'用户原文',subject:'self',category:'profile',reason:'核对',source_id:'source',status:'pending',origin:{}};
  await mount(t,async(url,options)=>{
    if(url.includes('/sources'))return response({candidate,source:{content:'用户原文',created_at:1},related:[],related_count:0,matched_count:0,warning:'核对'});
    if(url.endsWith('/review'))return response({before:null,after:candidate,resolution:'keep',token:'token',expires_at:2000000000,warning:'核对'});
    if(url.endsWith('/actions'))return response({detail:'原文已变化'},409);
    return response(page([candidate]));},'candidates');
  await click('核对保存');await click('预览保存');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');
  assert.match(document.querySelector('dialog [role=alert]').textContent,/原文已变化/);assert.equal(document.activeElement,document.querySelector('dialog [role=alert]'));
  assert.equal(buttons('确认保存').length,0);await click('重读来源');assert.equal(buttons('预览保存').length,1);
});

test('save response lost retries the same preview without duplicate submissions',async t=>{
  const candidate={id:'c1',content:'用户原文',subject:'self',category:'profile',reason:'核对',source_id:'source',status:'pending',origin:{}};
  let finish,attempts=0;const tokens=[];
  await mount(t,async(url,options)=>{
    if(url.includes('/sources'))return response({candidate,source:{content:'用户原文',created_at:1},related:[],related_count:0,matched_count:0,warning:'核对'});
    if(url.endsWith('/review'))return response({before:null,after:candidate,resolution:'keep',token:'fixed-token',expires_at:2000000000,warning:'核对'});
    if(url.endsWith('/actions')){tokens.push(JSON.parse(options.body).review_token);attempts++;if(attempts===1)return await new Promise(resolve=>finish=resolve);return response({...candidate,status:'confirmed'});}
    return response(page([candidate]));},'candidates');
  await click('核对保存');await click('预览保存');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');
  assert.equal(buttons('正在保存')[0].disabled,true);assert.equal(document.querySelector('dialog button[aria-label=关闭核对记忆]').disabled,true);
  await act(async()=>finish(response({detail:'暂时无法连接'},503)));await click('确认保存');assert.deepEqual(tokens,['fixed-token','fixed-token']);
});

async function setInput(selector,value){
  await act(async()=>{const input=document.querySelector(selector);assert.ok(input,selector);Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new window.Event('input',{bubbles:true}));});
}
async function submitEditor(){await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('memory editing preserves untouched legacy times and sends explicit clear only for edited fields',async t=>{
  const calls=[];
  await mount(t,async(url,options)=>{if(options.method==='PATCH'){calls.push(JSON.parse(options.body));return response({});}return response(page([{...memory('temporal'),event_at:'去年春天',valid_from:'2024-01-01',valid_to:'2024-12-31'}]));});
  assert.match(document.body.textContent,/事件时间：去年春天/);
  await click('纠正记忆');
  assert.equal(document.querySelector('[name=event_at]').value,'去年春天');
  assert.equal(document.querySelector('[name=valid_to]').value,'2024-12-31');
  await setInput('[name=valid_to]','');await submitEditor();
  assert.equal(calls.length,1);assert.equal(calls[0].valid_to,null);
  assert.equal(Object.hasOwn(calls[0],'event_at'),false);assert.equal(Object.hasOwn(calls[0],'valid_from'),false);
  assert.equal(calls[0].version,1);assert.equal(document.querySelector('dialog'),null);
});

test('new memory submits explicit dates and empty dates as unknown with a bounded pending state',async t=>{
  let finish;const calls=[];
  await mount(t,async(url,options)=>{if(options.method==='POST'){calls.push(JSON.parse(options.body));return await new Promise(resolve=>finish=resolve);}return response(page([]));});
  await click('新增记忆');document.querySelector('textarea[name=content]').value='真实用户手动事实';await setInput('[name=event_at]','2026-11-01');
  await submitEditor();await submitEditor();
  assert.equal(calls.length,1);assert.equal(calls[0].event_at,'2026-11-01');assert.equal(calls[0].valid_from,null);
  assert.equal(buttons('正在保存')[0].disabled,true);assert.equal(document.querySelector('dialog button[aria-label=关闭新增记忆]').disabled,true);
  assert.match(document.body.textContent,/不会自动删除或覆盖/);
  await act(async()=>finish(response({})));assert.equal(document.querySelector('dialog'),null);
});

test('time validation failure stays in editor and can be corrected without losing input',async t=>{
  let attempts=0;
  await mount(t,async(url,options)=>options.method==='POST'?(++attempts===1?response({detail:[]},422):response({})):response(page([])));
  await click('新增记忆');document.querySelector('textarea[name=content]').value='真实用户手动事实';await setInput('[name=valid_from]','2026-02-30');await submitEditor();
  assert.ok(document.querySelector('dialog'));assert.match(document.querySelector('dialog [role=alert]').textContent,/时间格式、日期和先后顺序/);
  assert.equal(document.activeElement,document.querySelector('dialog [role=alert]'));
  assert.equal(document.querySelector('[name=valid_from]').value,'2026-02-30');
  await setInput('[name=valid_from]','2026-02-28');await submitEditor();assert.equal(document.querySelector('dialog'),null);
});
