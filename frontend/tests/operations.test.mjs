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
const costs=compile('../src/UsageCosts.tsx',{'./api':api,'./ui':ui});
const {OperationsPages,OperationsOverview}=await import(compile('../src/OperationsPages.tsx',{'./api':api,'./ui':ui,'./UsageCosts':costs}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

const preferences={version:4,log_retention_days:90,daily_token_alert:0};
const health={worker:{status:'unavailable',online:0,slots:0,busy:0,last_seen:null},queue:{pending:0},oldest_ready_seconds:0,disk:{status:'unavailable'},checked_at:1};
const bundle={id:'diagnostic',created_at:1,expires_at:9999999999,digest:'a'.repeat(64),status:'preview',bytes:100,download_url:null,preview:{format:'lingxi-diagnostics-v1',sections:['health','usage']}};
const usage={days:7,calls:2,failed:1,known_tokens:50,unknown_usage_calls:1,cost:null,groups:[],daily:{known_tokens:50,unknown_usage_calls:1,alert_threshold:40,alert:true},note:'费用未定价'};

test('overview never infers worker health from an empty queue',async t=>{
 await mount(t,OperationsOverview,{},u=>response(u.endsWith('/operations/health')?health:{core:'ready',database:'ready',hindsight:'ready',memory:'unavailable'}));
 assert.match(document.body.textContent,/尚未收到 worker 心跳/);assert.match(document.body.textContent,/在线 0/);assert.match(document.body.textContent,/暂不可用/);assert.ok(!document.body.textContent.includes('undefined'));
});

test('usage retains unknown accounting and shows soft UTC alert',async t=>{
 await mount(t,OperationsPages,{page:'usage'},u=>response(u.includes('model-budgets')?{version:0,daily_limit:0,execution_limit:0,today_attempts:0,unsettled_attempts:0,utc_day:'2026-11-01',recent_executions:[],note:'请求次数限制'}:u.includes('preferences')?preferences:usage));
 assert.match(document.body.textContent,/用量缺失 1 次/);assert.match(document.body.textContent,/调用费用未知/);assert.match(document.body.textContent,/已达到日用量告警/);assert.match(document.body.textContent,/不自动停用模型/);
});

test('trace pagination preserves cursor and filter resets it',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'traces'},u=>{calls.push(u);return response({items:[],next_cursor:'safe-cursor'});});
 await click('下一页');assert.ok(calls.some(u=>u.includes('cursor=safe-cursor')));
 const select=document.querySelector('select');await act(async()=>{select.value='task';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
 assert.equal(calls.at(-1),'/api/admin/traces?cursor=&kind=task');
 assert.equal([...document.querySelectorAll('button')].find(v=>v.textContent==='上一页').disabled,true);
});

test('trace details only render supplied metadata and absent accounting is explicit',async t=>{
 await mount(t,OperationsPages,{page:'traces'},u=>response(u.includes('/chat/')?{id:'run',kind:'chat',status:'completed',versions:{prompt_version:'v1'},events:[{seq:2,kind:'done',created_at:null}],calls:[],note:'仅展示阶段',truncated:false}:{items:[{id:'chat:run',entity_id:'run',kind:'chat',status:'completed',created_at:1}],next_cursor:null}));
 await click('查看追踪');assert.match(document.querySelector('dialog').textContent,/未记录独立时刻/);assert.match(document.querySelector('dialog').textContent,/不代表没有费用/);assert.match(document.querySelector('dialog').textContent,/v1/);
});

const graph={root:'chat:run',nodes:[
 {key:'chat:run',kind:'chat',id:'run',status:'completed',created_at:1,version:null,sequence:null},
 {key:'memory:fact',kind:'memory',id:'fact',status:'deleted',created_at:1,version:3,sequence:null},
 {key:'plugin:tool',kind:'plugin',id:'tool',status:'failed',created_at:1,version:1,sequence:null}
],edges:[{source:'chat:run',target:'memory:fact',relation:'used_memory',recorded_version:1},
 {source:'plugin:tool',target:'memory:fact',relation:'used_memory',recorded_version:2}],truncated:false,note:'真实持久关系，不含正文'};
const traceWithGraph=g=>({id:'run',kind:'chat',status:'completed',versions:{},events:[],calls:[],note:'阶段元数据',truncated:false,graph:g});
const traceListing={items:[{id:'chat:run',entity_id:'run',kind:'chat',status:'completed',created_at:1}],next_cursor:null};

test('relationship view navigates real nodes and distinguishes recorded and current memory versions',async t=>{
 await mount(t,OperationsPages,{page:'traces'},u=>response(u.includes('/chat/')?traceWithGraph(graph):traceListing));
 await click('查看追踪');const section=document.querySelector('section[aria-label="执行关系"]');
 assert.match(section.textContent,/当前显示 1 条/);assert.match(section.textContent,/当时引用版本：1/);
 assert.ok(!section.textContent.includes('当时引用版本：2'));
 await click('#2 长期记忆');assert.match(section.textContent,/当前版本：3/);assert.match(section.textContent,/当前状态：已删除/);
 assert.match(section.textContent,/当前显示 2 条/);assert.match(section.textContent,/不代表当前仍然有效/);
 const select=section.querySelector('select');await act(async()=>{select.value='all';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
 assert.equal(section.querySelector('article'),null);assert.match(section.textContent,/当前显示 2 条/);
 assert.ok(!section.textContent.includes('undefined'));
});

test('relationship limits and missing associations remain explicit',async t=>{
 await mount(t,OperationsPages,{page:'traces'},u=>response(u.includes('/chat/')?traceWithGraph({...graph,edges:[],truncated:true}):traceListing));
 await click('查看追踪');assert.match(document.querySelector('dialog').textContent,/暂无已记录的直接关联/);
 assert.match(document.querySelector('dialog').textContent,/当前关系不完整/);
});

test('failed trace lookup can retry without closing the dialog or posting mutations',async t=>{
 let tries=0;const methods=[];
 await mount(t,OperationsPages,{page:'traces'},(u,o)=>{methods.push(o.method);if(!u.includes('/chat/'))return response(traceListing);return ++tries===1?response({detail:'暂时无法读取'},503):response(traceWithGraph(graph));});
 await click('查看追踪');assert.match(document.querySelector('dialog').textContent,/暂时无法读取/);
 await click('重试加载');assert.match(document.querySelector('dialog').textContent,/执行关系/);assert.equal(tries,2);assert.ok(methods.every(v=>v==='GET'));
});

test('diagnostics require explicit preview approval and exact server digest',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'diagnostics'},(u,o)=>{calls.push({u,o});return response(u.endsWith('/preview')?bundle:u.endsWith('/confirm')?{...bundle,status:'confirmed'}:[]);});
 await submit();const dialog=document.querySelector('dialog');assert.match(dialog.textContent,/lingxi-diagnostics-v1/);
 assert.equal(dialog.querySelector('button.primary').disabled,true);assert.equal(calls.filter(c=>c.u.endsWith('/confirm')).length,0);
 await act(async()=>dialog.querySelector('input[type="checkbox"]').click());await click('确认导出');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/confirm')).o.body),{digest:bundle.digest});
 assert.equal(document.querySelector('dialog'),null);assert.ok(!calls.some(c=>c.u.endsWith('/download')));
});

test('diagnostic confirmation failure retains preview for review',async t=>{
 await mount(t,OperationsPages,{page:'diagnostics'},u=>response(u.endsWith('/preview')?bundle:u.endsWith('/confirm')?{detail:'诊断文件已过期，请重新生成'}:[],u.endsWith('/confirm')?410:200));
 await submit();await act(async()=>document.querySelector('dialog input').click());await click('确认导出');
 assert.match(document.querySelector('dialog').textContent,/诊断文件已过期/);assert.match(document.querySelector('dialog').textContent,/lingxi-diagnostics-v1/);
});

test('download session failure is visible without navigating away',async t=>{
 await mount(t,OperationsPages,{page:'diagnostics'},u=>response(u.endsWith('/download')?{detail:'诊断文件绑定生成预览时的登录会话'}:[{...bundle,status:'confirmed',download_url:'/api/admin/diagnostics/diagnostic/download'}],u.endsWith('/download')?403:200));
 const href=window.location.href;await click('下载诊断');assert.match(document.body.textContent,/绑定生成预览时的登录会话/);assert.equal(window.location.href,href);
});

test('preferences require preview and retain draft after version conflict',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'logs'},(u,o)=>{calls.push({u,o});return response(o.method==='PUT'?{detail:'运维配置已变化，请重新加载后核对'}:u.includes('preferences')?preferences:{items:[],next_cursor:null},o.method==='PUT'?409:200);});
 await value('input[type="number"]','7');await submit();assert.equal(calls.filter(c=>c.o.method==='PUT').length,0);assert.match(document.querySelector('dialog').textContent,/清理后无法/);
 await click('保存更改');assert.match(document.querySelector('dialog').textContent,/运维配置已变化/);assert.equal(document.querySelector('input[type="number"]').value,'7');
 assert.deepEqual(JSON.parse(calls.find(c=>c.o.method==='PUT').o.body),{version:4,log_retention_days:7,daily_token_alert:0});
});
