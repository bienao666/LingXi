import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const[k,v]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${k}'`,`from '${v}'`).replaceAll(`from "${k}"`,`from "${v}"`);return url(code);}
const window=new Window({url:'http://localhost/#/plugins/connections'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {OAuthConnectionEditor,OAuthHistory}=await import(compile('../src/PluginOAuth.tsx',{'./api':api,'./ui':ui}));
const capability={id:'sample.remote',name:'资料服务',installation_id:'installation',plugin_id:'sample.oauth'};
const entry={authorization_url:'https://accounts.example/authorize',token_url:'https://accounts.example/token',revocation_url:'https://accounts.example/revoke',issuer:'https://accounts.example',scopes:['files.read'],client_auth:'none',resource:'https://api.example'};
const started={id:'flow',authorization_url:'https://accounts.example/authorize?state=synthetic',redirect_uri:'http://localhost:8080/api/plugin-oauth/callback',expires_at:Date.now()/1000+600,scopes:['files.read']};
const row={id:'connection',version:4};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,handler,history=false,props={}){document.body.innerHTML='<div id="root"></div>';window.location.hash='#/plugins/connections';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(history?OAuthHistory:OAuthConnectionEditor,history?{changed(){}}:{capability,entry,row,close(){},done(){},...props})));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function fill(selector,value){const input=document.querySelector(selector);assert.ok(input,selector);await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new window.Event('input',{bubbles:true}));});}
async function accept(){await act(async()=>[...document.querySelectorAll('dialog input[type=checkbox]')].at(-1).click());}
const installed=[{id:'installation',version:7}];

test('OAuth shows endpoints and scopes, never navigates or starts before consent',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(installed);});
 assert.match(document.body.textContent,/files.read/);assert.match(document.body.textContent,/https:\/\/accounts.example\/token/);
 assert.equal(document.querySelector('button.primary').disabled,true);assert.equal(document.querySelector('input[type=password]'),null);
 assert.ok(!calls.some(c=>c.o.method==='POST'));assert.equal(document.querySelector('a'),null);
});

test('OAuth start pins installation and connection, then requires deliberate external navigation',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(o.method==='POST'?started:installed);});
 await fill('dialog input:not([type])','registered-client');await accept();await click('生成授权');
 const body=JSON.parse(calls.find(c=>c.o.method==='POST').o.body);
 assert.deepEqual(body,{installation_id:'installation',capability_id:'sample.remote',installation_version:7,connection_version:4,client_id:'registered-client',client_secret:'',allow_private:false,acknowledged:true});
 assert.equal(document.querySelector('a').textContent,'前往授权');assert.equal(document.querySelector('a').getAttribute('href'),started.authorization_url);
 assert.equal(document.querySelector('a').getAttribute('rel'),'noreferrer');assert.match(document.body.textContent,/尚未连接账号/);
 assert.match(document.body.textContent,/完整回调地址/);assert.match(document.body.textContent,/http:\/\/localhost:8080\/api\/plugin-oauth\/callback/);
});

test('changing OAuth client or network scope resets confirmation',async t=>{
 await mount(t,()=>response(installed));await fill('dialog input:not([type])','first');await accept();
 assert.equal(document.querySelector('button.primary').disabled,false);await fill('dialog input:not([type])','second');
 assert.equal(document.querySelector('button.primary').disabled,true);await accept();
 await act(async()=>document.querySelector('dialog input[type=checkbox]').click());assert.equal(document.querySelector('button.primary').disabled,true);
});

test('confidential client secret stays masked and clears after start',async t=>{
 await mount(t,(u,o)=>response(o.method==='POST'?started:installed),false,{entry:{...entry,client_auth:'client_secret_basic'}});
 const password=document.querySelector('input[type=password]');assert.equal(password.autocomplete,'off');assert.equal(password.value,'');
 await fill('dialog input:not([type])','client');await fill('dialog input[type=password]','synthetic-secret');await accept();await click('生成授权');
 assert.equal(document.querySelector('input[type=password]'),null);assert.ok(!document.body.textContent.includes('synthetic-secret'));
});

test('version conflict retains OAuth draft, sends once and does not redirect',async t=>{
 let count=0;await mount(t,(u,o)=>{if(o.method==='POST'){count++;return response({detail:'账号连接已变化，请重新加载'},409);}return response(installed);});
 await fill('dialog input:not([type])','draft-client');await accept();await click('生成授权');
 assert.equal(count,1);assert.equal(document.querySelector('dialog input:not([type])').value,'draft-client');
 assert.match(document.querySelector('[role=alert]').textContent,/重新加载/);assert.equal(document.querySelector('a'),null);
});

test('abandoning generated authorization cancels flow without sending tokens',async t=>{
 const calls=[];let done=false;await mount(t,(u,o)=>{calls.push({u,o});return response(o.method==='POST'?started:installed);},false,{done(){done=true;}});
 await fill('dialog input:not([type])','client');await accept();await click('生成授权');await click('放弃授权');
 assert.ok(calls.some(c=>c.u==='/api/plugin-oauth/flows/flow/cancel'));assert.equal(done,true);
 assert.ok(!calls.some(c=>/callback|token/.test(c.u)));
});

test('authorization history cancel names the connection and requires confirmation',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response([{id:'flow',connection_id:'named-connection',status:'pending',created_at:1,expires_at:9999999999}]);},true);
 await click('取消授权');assert.match(document.querySelector('dialog').textContent,/named-connec/);
 assert.ok(!calls.some(c=>c.o.method==='POST'));await click('确认取消');assert.equal(calls.filter(c=>c.o.method==='POST').length,1);
});
