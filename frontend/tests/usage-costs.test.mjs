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
const {ModelPrices,CostAmounts,CostBreakdowns}=await import(costs);
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function value(selector,text){const el=document.querySelector(selector);assert.ok(el);await act(async()=>{const proto=el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,text);el.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function submit(){await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

const prices=[{model_id:'chat',model:'示例模型',provider:'独立服务',provider_version:3,purpose:'chat',version:0,enabled:false,current:false,rates:{}}];
async function expand(){await act(async()=>{const el=document.querySelector('details');el.open=true;el.dispatchEvent(new window.Event('toggle'));});}

test('rate directory loads on demand and does not treat unknown as free',async t=>{
 const calls=[];await mount(t,ModelPrices,{onSaved(){}},(u,o)=>{calls.push({u,o});return response(prices);});
 assert.equal(calls.length,0);await expand();assert.equal(calls.length,1);
 await click('设置单价');assert.equal(document.querySelector('[name="input_per_million"]').value,'');
 assert.match(document.querySelector('dialog').textContent,/每百万 token/);assert.match(document.querySelector('dialog').textContent,/免费/);
 assert.equal(calls.filter(c=>c.o.method==='PUT').length,0);
});

test('price saving requires review acknowledgement and preserves exact decimal strings',async t=>{
 const calls=[];let saved=0;await mount(t,ModelPrices,{onSaved(){saved++;}},(u,o)=>{calls.push({u,o});return response(o.method==='PUT'?{}:prices);});
 await expand();await click('设置单价');await value('[name="input_per_million"]','0.000001');await value('[name="output_per_million"]','20');
 await submit();assert.equal(calls.filter(c=>c.o.method==='PUT').length,0);
 assert.equal(document.activeElement,document.querySelector('dialog p[tabindex="-1"]'));
 assert.match(document.querySelector('dialog').textContent,/不回填历史/);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='保存单价').disabled,true);
 await act(async()=>document.querySelector('dialog input').click());await click('保存单价');
 const body=JSON.parse(calls.find(c=>c.o.method==='PUT').o.body);
 assert.equal(body.input_per_million,'0.000001');assert.equal(body.provider_version,3);assert.equal(body.acknowledgement,'estimate_only');
 assert.equal(saved,1);assert.equal(document.querySelector('dialog'),null);assert.match(document.body.textContent,/历史账目和正在执行/);
});

test('price conflicts retain draft and error without automatic resubmission',async t=>{
 const calls=[];await mount(t,ModelPrices,{onSaved(){}},(u,o)=>{calls.push({u,o});return response(o.method==='PUT'?{detail:'单价已变化，请重新加载后核对'}:prices,o.method==='PUT'?409:200);});
 await expand();await click('设置单价');await value('[name="input_per_million"]','2');await value('[name="output_per_million"]','6');await submit();
 await act(async()=>document.querySelector('dialog input').click());await click('保存单价');
 assert.match(document.querySelector('dialog').textContent,/单价已变化/);await click('继续修改');
 assert.equal(document.querySelector('[name="input_per_million"]').value,'2');assert.equal(calls.filter(c=>c.o.method==='PUT').length,1);
});

test('cost amounts preserve zero, precision and separate currencies',async t=>{
 await mount(t,CostAmounts,{rows:[{currency:'CNY',amount:'0'},{currency:'USD',amount:'0.000000000001'}]},()=>response({}));
 assert.match(document.body.textContent,/CNY 0/);assert.match(document.body.textContent,/USD 0.000000000001/);assert.ok(!document.body.textContent.includes('未知'));
});

test('cost breakdown switches dimensions and declares incomplete groups',async t=>{
 await mount(t,CostBreakdowns,{usage:{breakdowns:{channel:{items:[{key:'qq',calls:2,currencies:[],unknown_cost_calls:2}]},task:{items:[{key:'safe-task-id',calls:1,currencies:[{currency:'CNY',amount:'0.1'}],unknown_cost_calls:0}],truncated:true}}}},()=>response({}));
 assert.match(document.body.textContent,/QQ 消息/);assert.match(document.body.textContent,/费用未知 2/);
 await act(async()=>{const el=document.querySelector('select');el.value='task';el.dispatchEvent(new window.Event('change',{bubbles:true}));});
 assert.match(document.body.textContent,/safe-task-id/);assert.match(document.body.textContent,/前 1000 项/);assert.match(document.body.textContent,/不能再次相加/);
});

test('failed directory load keeps an explicit recovery path',async t=>{
 await mount(t,ModelPrices,{onSaved(){}},()=>response({detail:'服务不可用'},503));await expand();
 assert.match(document.body.textContent,/服务不可用/);assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='重新加载'));
});
