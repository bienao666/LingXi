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

const budget={version:3,daily_limit:8,execution_limit:2,utc_day:'2026-11-01',today_attempts:5,unsettled_attempts:1,recent_executions:[{kind:'chat',id:'safe-run',attempts:2,last_at:1}],note:'失败仍计数。次数不是费用封顶。'};
const usage={days:7,calls:0,failed:0,known_tokens:0,unknown_usage_calls:0,groups:[],daily:{known_tokens:0,unknown_usage_calls:0},note:'合成统计'};
const preferences={version:1,log_retention_days:90,daily_token_alert:0};
function handler(calls,mutation){return(u,o)=>{calls.push({u,o});if(u.includes('model-budgets')){
 if(o.method!=='GET')return mutation(u,o);
 return response(budget);
}return response(u.includes('preferences')?preferences:usage);};}

test('budget displays durable UTC usage without calling it a monetary cap',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'usage'},handler(calls,()=>response({})));
 assert.match(document.body.textContent,/已尝试 5 次/);assert.match(document.body.textContent,/尚未结算 1 次/);
 assert.match(document.body.textContent,/失败仍计数/);assert.match(document.body.textContent,/费用封顶/);
 assert.match(document.body.textContent,/safe-run/);assert.ok(calls.every(c=>c.o.method==='GET'));
});

test('budget preview and explicit acknowledgement bind exactly to server token',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'usage'},handler(calls,(u,o)=>response(u.endsWith('previews')?{token:'bound-preview',before:budget,after:{daily_limit:10,execution_limit:4},today_attempts:6,expires_at:9999999999,note:budget.note}:budget)));
 await value('input[type="number"]','10');
 // Select the second labelled field independently of later operations settings.
 const fields=document.querySelectorAll('section[aria-label="调用限额"] input[type="number"]');
 await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(fields[1],'4');fields[1].dispatchEvent(new window.Event('input',{bubbles:true}));});
 await submit();assert.equal(calls.filter(c=>c.o.method==='PUT').length,0);
 assert.equal(document.activeElement,document.querySelector('dialog p[tabindex="-1"]'));
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='保存限额').disabled,true);
 const sent=JSON.parse(calls.find(c=>c.o.method==='POST').o.body);assert.deepEqual(sent,{version:3,daily_limit:10,execution_limit:4});
 await act(async()=>document.querySelector('dialog input').click());await click('保存限额');
 assert.deepEqual(JSON.parse(calls.find(c=>c.o.method==='PUT').o.body),{token:'bound-preview',acknowledgement:'request_attempts_not_money'});
 assert.equal(document.querySelector('dialog'),null);assert.match(document.body.textContent,/已用次数不清空/);
});

test('quota confirmation conflict stays visible without silently retrying or widening limits',async t=>{
 const calls=[];await mount(t,OperationsPages,{page:'usage'},handler(calls,(u,o)=>o.method==='PUT'?response({detail:'限额预览已失效，请重新预览'},409):response({token:'expired-preview',before:budget,after:budget,today_attempts:5,expires_at:1,note:budget.note})));
 await submit();await act(async()=>document.querySelector('dialog input').click());await click('保存限额');
 assert.match(document.querySelector('dialog').textContent,/限额预览已失效/);
 assert.equal(calls.filter(c=>c.o.method==='PUT').length,1);await click('保留配置');
 assert.equal(document.querySelector('dialog'),null);
});

test('budget load failure offers retry and never displays an invented default',async t=>{
 let tries=0;await mount(t,OperationsPages,{page:'usage'},(u,o)=>u.includes('model-budgets')?(++tries===1?response({detail:'限额暂不可读'},503):response(budget)):response(u.includes('preferences')?preferences:usage));
 assert.match(document.body.textContent,/限额暂不可读/);assert.equal(document.querySelector('section[aria-label="调用限额"] form'),null);
 await act(async()=>document.querySelector('section[aria-label="调用限额"] button').click());assert.equal(tries,2);assert.match(document.body.textContent,/已尝试 5 次/);
});
