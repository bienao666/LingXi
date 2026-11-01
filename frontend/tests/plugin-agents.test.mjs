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
const window=new Window({url:'http://localhost/#/assistants/agents'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {PluginAgents}=await import(compile('../src/PluginAgents.tsx',{'./api':api,'./ui':ui}));
const cap={id:'sample.reviewer',name:'风险审阅',kind:'agent',installation_id:'installed',plugin_id:'sample.roles',plugin_version:'1.0.0',agent:{expertise:'核对项目风险',instructions:'区分事实与建议',completion:'附来源清单',tools:['reason'],context_categories:['projects','procedures']}};
const binding={id:'binding',installation_id:'installed',capability_id:cap.id,plugin_id:'sample.roles',plugin_version:'1.0.0',role_name:'风险审阅',status:'enabled',version:3,persona_id:'persona',model_id:null,tools:['reason'],context_categories:['projects'],available:true,reason:null,updated_at:1};
const installation={id:'installed',version:7,available:true};
const persona={id:'persona',name:'并肩伙伴',enabled:true};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const defaults=u=>u==='/api/plugin-capabilities'?[cap]:u==='/api/plugin-installations'?[installation]:u==='/api/personas'?[persona]:[];
async function mount(t,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(PluginAgents)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
async function accept(){await act(async()=>[...document.querySelectorAll('dialog input[type=checkbox]')].at(-1).click());}

test('plugin role installation does not silently activate or grant memory',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(defaults(u));});await click('配置角色');
 assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.equal(document.querySelector('dialog select').value,'');
 assert.ok([...document.querySelectorAll('dialog fieldset')][1].querySelectorAll('input:not(:checked)').length===2);
 assert.ok(!calls.some(c=>c.o.method==='POST'));assert.match(document.body.textContent,/不提供完整记忆库/);
});

test('role editing sends actual binding and installation versions after explicit consent',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-agent-bindings'?[binding]:defaults(u));});
 await click('编辑角色');await accept();await click('确认启用');
 const request=JSON.parse(calls.find(c=>c.o.method==='POST').o.body);
 assert.deepEqual(request,{installation_id:'installed',installation_version:7,capability_id:cap.id,version:3,persona_id:'persona',model_id:null,tools:['reason'],context_categories:['projects'],acknowledged:true});
});

test('version conflict preserves role draft without retrying',async t=>{
 let writes=0;await mount(t,(u,o)=>{if(o.method==='POST'){writes++;return response({detail:'角色配置已变化'},409);}return response(u==='/api/plugin-agent-bindings'?[binding]:defaults(u));});
 await click('编辑角色');await accept();await click('确认启用');
 assert.equal(writes,1);assert.ok(document.querySelector('dialog'));assert.match(document.querySelector('dialog [role=alert]').textContent,/已变化/);
 assert.equal(document.querySelector('dialog select').value,'persona');
});

test('role revoke waits for named confirmation and current version',async t=>{
 const calls=[];await mount(t,(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-agent-bindings'?[binding]:defaults(u));});
 await click('撤销角色');assert.ok(!calls.some(c=>c.o.method==='POST'));assert.match(document.querySelector('dialog').textContent,/风险审阅/);
 await click('确认撤销');const sent=calls.find(c=>c.o.method==='POST');assert.equal(sent.u,'/api/plugin-agent-bindings/binding/revoke');assert.deepEqual(JSON.parse(sent.o.body),{version:3});
});

test('changing selected memory clears consent and does not auto-enable new categories',async t=>{
 await mount(t,u=>response(u==='/api/plugin-agent-bindings'?[binding]:defaults(u)));await click('编辑角色');await accept();
 assert.equal(document.querySelector('dialog button.primary').disabled,false);
 await act(async()=>[...document.querySelectorAll('dialog fieldset')][1].querySelectorAll('input')[1].click());
 assert.equal(document.querySelector('dialog button.primary').disabled,true);
});

test('untrusted role text stays inert and stale template is clearly unavailable',async t=>{
 await mount(t,u=>response(u==='/api/plugin-capabilities'?[{...cap,name:'<img src=x onerror=alert(1)>',agent:{...cap.agent,instructions:'<script>run()</script>'}}]:u==='/api/plugin-agent-bindings'?[{...binding,available:false,reason:'插件版本已变化'}]:defaults(u)));
 assert.match(document.body.textContent,/插件版本已变化/);assert.match(document.body.textContent,/<script>run\(\)<\/script>/);
 assert.equal(document.querySelector('section script, section img'),null);assert.match(document.body.textContent,/需要核对/);
 assert.equal(document.querySelector('a[href="#/tasks/list?mode=agents"]').textContent,'助手协作');
});
