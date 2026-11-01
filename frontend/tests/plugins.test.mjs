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
const window=new Window({url:'http://localhost/#/plugins/catalog'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const storePages=compile('../src/StorePages.tsx',{'./api':api,'./ui':ui});
const pluginOAuth=compile('../src/PluginOAuth.tsx',{'./api':api,'./ui':ui});
const pluginEvents=compile('../src/PluginEvents.tsx',{'./api':api,'./ui':ui});
const {PluginPages}=await import(compile('../src/PluginPages.tsx',{'./api':api,'./ui':ui,'./StorePages':storePages,'./PluginEvents':pluginEvents,'./PluginOAuth':pluginOAuth,'react-markdown':pathToFileURL(require.resolve('react-markdown')).href}));
const release={release_id:'release',plugin_id:'lingxi.writing',name:'写作伙伴',version:'1.1.0',description:'整理资料',publisher:'lingxi',source:'bundled',digest:'a'.repeat(64),permissions:['model.chat'],capabilities:[{id:'writing.outline',name:'提纲整理',kind:'skill'}],dependencies:[],external_services:[],costs:'模型可能计费',data_scope:'只处理明确提交资料',license:'MIT',core:'>=0.2.0,<0.3.0',compatible:true};
const installation={id:'installation',plugin_id:release.plugin_id,name:release.name,plugin_version:'1.0.0',release_id:'old',version:3,status:'enabled',grants:['model.chat'],dependency_lock:{},config:{},available:true,reason:null,digest:release.digest,source:'bundled',updated_at:1};
const preview={id:'preview',digest:'b'.repeat(64),expires_at:Date.now()/1000+600,status:'pending',plan:{location:'服务器 Core／worker',entries:[{release,previous_version:null,action:'install',new_permissions:['model.chat'],grants:['model.chat'],dependency_lock:{},config:{}}],permissions:['model.chat']}};
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
async function mount(t,page,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>u==='/api/plugin-oauth/flows'?response([]):handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(PluginPages,{page})));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}
const get=u=>u==='/api/plugin-oauth/flows'?[]:u==='/api/plugin-store'?null:u==='/api/plugin-catalog'?[release]:u.endsWith('/impact')?{dependents:[],active_invocations:0,pending_tasks:0,scheduled_reminders:0,delegated_file_nodes:0}:[];

test('catalog is honest about public store and install never occurs before approval',async t=>{
 const calls=[];await mount(t,'catalog',(u,o)=>{calls.push({u,o});return response(u.endsWith('/preview')?preview:get(u));});
 assert.match(document.body.textContent,/公开版本审核后上架/);
 await click('预览安装');assert.equal(document.querySelector('dialog button.primary').disabled,true);
 assert.match(document.querySelector('dialog').textContent,/只处理明确提交资料/);assert.match(document.querySelector('dialog').textContent,/可能计费/);
 assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
});

test('reject closes preview without performing install',async t=>{
 const calls=[];await mount(t,'catalog',(u,o)=>{calls.push(u);return response(u.endsWith('/preview')?preview:get(u));});
 await click('预览安装');await click('拒绝变更');assert.ok(calls.includes('/api/plugin-installations/previews/preview/reject'));assert.ok(!calls.some(u=>u.endsWith('/confirm')));assert.equal(document.querySelector('dialog'),null);
});

test('confirmation sends digest and exact permission set, blocks duplicate submission and retains errors',async t=>{
 let finish;const writes=[];await mount(t,'catalog',(u,o)=>{if(u.endsWith('/confirm')){writes.push(JSON.parse(o.body));return new Promise(r=>finish=r);}return response(u.endsWith('/preview')?preview:get(u));});
 await click('预览安装');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认安装');await click('正在登记');assert.equal(writes.length,1);
 assert.deepEqual(writes[0],{digest:preview.digest,approved_permissions:['model.chat']});await act(async()=>finish(response({detail:'预览已失效，请重新预览'},409)));assert.match(document.querySelector('dialog').textContent,/预览已失效/);
});

test('disable names the plugin and uses actual installation version',async t=>{
 const calls=[];await mount(t,'installed',(u,o)=>{if(o.method==='POST'){calls.push(JSON.parse(o.body));return response({...installation,status:'disabled'});}return response(u==='/api/plugin-installations'?[installation]:get(u));});
 await click('停用插件');assert.equal(calls.length,0);assert.match(document.querySelector('dialog').textContent,/写作伙伴/);assert.match(document.querySelector('dialog').textContent,/模型费用不能撤销/);await click('确认停用');assert.deepEqual(calls,[{version:3}]);
});

test('dependency uninstall error remains actionable',async t=>{
 await mount(t,'installed',(u,o)=>response(o.method==='POST'?{detail:'存在依赖者，请先卸载 test.root'}:u==='/api/plugin-installations'?[installation]:get(u),o.method==='POST'?409:200));
 await click('卸载插件');await click('确认卸载');assert.match(document.querySelector('dialog').textContent,/存在依赖者/);
});

test('updates select an explicit rollback through immutable history',async t=>{
 const calls=[];await mount(t,'updates',(u,o)=>{calls.push({u,o});return response(u.endsWith('/history')?[{...installation,plugin_version:'0.9.0'}]:u.endsWith('/preview')?preview:u==='/api/plugin-installations'?[installation]:get(u));});
 await click('版本历史');await click('预览回滚');const body=JSON.parse(calls.find(c=>c.u.endsWith('/preview')).o.body);assert.deepEqual(body,{plugin_id:'lingxi.writing',plugin_version:'0.9.0',action:'rollback'});assert.match(document.querySelector('dialog').textContent,/不能撤销第三方行为/);
});

test('capability prerequisite is visible and unavailable cannot execute',async t=>{
 await mount(t,'skills',()=>response([{id:'writing.outline',name:'提纲整理',kind:'skill',permissions:['model.chat'],plugin_id:'lingxi.writing',installation_id:'installation',plugin_version:'1.1.0',available:false,reason:'请先验证模型'}]));assert.match(document.body.textContent,/请先验证模型/);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='使用技能').disabled,true);
});

test('successful output renders Markdown without executing untrusted HTML',async t=>{
 await mount(t,'invocations',()=>response([{id:'run',plugin_id:'lingxi.writing',plugin_version:'1.1.0',capability_id:'writing.outline',status:'succeeded',created_at:1,result:'**提纲**\n<script>alert(1)</script>',receipt:{note:'仅生成文字建议'},error:null}]));await click('查看结果');assert.equal(document.querySelector('dialog script'),null);assert.match(document.querySelector('dialog strong').textContent,/提纲/);assert.match(document.body.textContent,/费用未知/);
});

const apiEntry={url:'https://api.example.test/action',method:'POST',effect:'write',auth:'bearer',health_url:'',parameters:[{name:'title',type:'string',required:true,description:'仅提交资料'}]};
const apiCapability={id:'sample.request',name:'远程资料',kind:'api',api:apiEntry,permissions:['network.write'],installation_id:'installation',plugin_id:'sample.api',plugin_version:'1.0.0',available:true,reason:null};
const connection={id:'connection',installation_id:'installation',capability_id:apiCapability.id,plugin_id:'sample.api',status:'active',version:4,available:true,allow_private:false,has_secret:true,reason:null,...apiEntry};
const apiPreview={id:'api-preview',version:1,status:'preview',digest:'c'.repeat(64),expires_at:Date.now()/1000+300,plan:{url:apiEntry.url,method:'POST',effect:'write',input:{title:'合成资料'},costs:'未知',data_scope:'明确输入'}};
async function setValue(selector,value){const el=document.querySelector(selector);assert.ok(el,selector);await act(async()=>{Object.getOwnPropertyDescriptor(el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new window.Event('input',{bubbles:true}));el.dispatchEvent(new window.Event('change',{bubbles:true}));});}

test('API account management masks credentials and requires separate approval',async t=>{
 const calls=[];await mount(t,'connections',(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-capabilities'?[apiCapability]:u==='/api/plugin-connections'?[connection]:connection);});
 await click('编辑授权');const input=document.querySelector('dialog input[type=password]');assert.equal(input.value,'');assert.equal(input.autocomplete,'off');
 assert.equal([...document.querySelectorAll('dialog button')].find(b=>b.textContent==='确认授权').disabled,true);
 await act(async()=>document.querySelectorAll('dialog input[type=checkbox]')[1].click());await click('确认授权');
 const put=calls.find(c=>c.o.method==='PUT');assert.deepEqual(JSON.parse(put.o.body),{installation_id:'installation',capability_id:'sample.request',secret:'',allow_private:false,version:4,acknowledged:true});
});

test('connection version conflict keeps the draft and never silently retries',async t=>{
 const calls=[];await mount(t,'connections',(u,o)=>{calls.push({u,o});return response(o.method==='PUT'?{detail:'连接已变化，请重新加载'}:u==='/api/plugin-capabilities'?[apiCapability]:[connection],o.method==='PUT'?409:200);});
 await click('编辑授权');await setValue('dialog input[type=password]','draft-only-key');await act(async()=>document.querySelectorAll('dialog input[type=checkbox]')[1].click());await click('确认授权');
 assert.match(document.querySelector('dialog').textContent,/连接已变化/);assert.equal(document.querySelector('dialog input[type=password]').value,'draft-only-key');assert.equal(calls.filter(c=>c.o.method==='PUT').length,1);
});

test('API preview displays fixed request and never sends before explicit confirmation',async t=>{
 const calls=[];await mount(t,'skills',(u,o)=>{calls.push({u,o});return response(u.endsWith('/preview')?apiPreview:u.endsWith('/confirm')?{status:'queued'}:[apiCapability]);});
 await click('请求预览');assert.match(document.querySelector('dialog').textContent,/外部写入/);await click('生成预览');
 assert.match(document.querySelector('dialog pre').textContent,/合成资料/);assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认发送').disabled,true);
 await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认发送');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/confirm')).o.body),{digest:apiPreview.digest});assert.match(document.querySelector('dialog').textContent,/请求已登记/);
});

test('abandoning API preview cancels it without confirming or replaying',async t=>{
 const calls=[];await mount(t,'skills',(u,o)=>{calls.push({u,o});return response(u.endsWith('/preview')?apiPreview:[apiCapability]);});
 await click('请求预览');await click('生成预览');await click('放弃请求');assert.equal(document.querySelector('dialog'),null);
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/cancel')).o.body),{version:1});assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
});

test('unknown API results require human evidence and do not offer retry',async t=>{
 const r={id:'api-run',adapter:'api',plugin_id:'sample.api',capability_id:'sample.request',status:'unknown',version:5,created_at:1,plugin_version:'1.0.0',result:'',receipt:{},error:'外部状态待核对'};
 const calls=[];await mount(t,'invocations',(u,o)=>{calls.push({u,o});return response(o.method==='POST'?{...r,status:'resolved'}:[r]);});
 assert.ok(![...document.querySelectorAll('button')].some(b=>/重试|再次发送/.test(b.textContent)));await click('核对结果');assert.match(document.querySelector('dialog').textContent,/不表示系统独立验证/);
 await setValue('dialog textarea','第三方记录查询无对应操作');await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('登记结论');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/resolve')).o.body),{version:5,outcome:'not_applied',evidence:'第三方记录查询无对应操作',acknowledged:true});assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
});

test('API output renders plain JSON even if upstream includes markup',async t=>{
 await mount(t,'invocations',()=>response([{id:'api',adapter:'api',plugin_id:'sample.api',plugin_version:'1.0.0',status:'succeeded',created_at:1,result:'{"html":"<script>bad()</script>"}',receipt:{},error:null}]));await click('查看结果');assert.equal(document.querySelector('dialog script'),null);assert.match(document.querySelector('dialog pre').textContent,/<script>/);
});

const mcpCapability={...apiCapability,kind:'mcp',api:undefined,mcp:{url:'https://mcp.example.test/mcp',transport:'streamable-http',auth:'bearer'},id:'sample.remote',name:'远程工具'};
const mcpConnection={...connection,capability_id:mcpCapability.id};
const mcpTool={name:'notes.add',description:'<script>untrusted</script>',inputSchema:{type:'object',properties:{title:{type:'string'}},required:['title']}};
const mcpCatalog={connection_id:'connection',version:2,digest:'d'.repeat(64),tools:[mcpTool],approved_tools:[],available:false,discovered_at:1};

test('MCP discovery and grants are separate human actions, descriptions remain plain text',async t=>{
 const calls=[];await mount(t,'connections',(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-capabilities'?[mcpCapability]:u==='/api/plugin-connections'?[mcpConnection]:mcpCatalog);});
 await click('工具授权');assert.equal(document.querySelector('dialog script'),null);
 assert.match(document.querySelector('dialog').textContent,/不授予记忆库/);
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认工具').disabled,true);
 assert.ok(!calls.some(c=>c.o.method==='POST'));await click('发现工具');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/discover')).o.body),{version:4});
 assert.ok(!calls.some(c=>c.u.endsWith('/approve')));
 await act(async()=>{document.querySelectorAll('dialog input[type=checkbox]')[0].click();});
 await act(async()=>{document.querySelectorAll('dialog input[type=checkbox]')[1].click();});
 await click('确认工具');assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/approve')).o.body),{version:2,digest:mcpCatalog.digest,tools:['notes.add'],acknowledged:true});
});

test('MCP approval conflict keeps selected tools and never silently approves a new directory',async t=>{
 const calls=[];await mount(t,'connections',(u,o)=>{calls.push({u,o});return response(u.endsWith('/approve')?{detail:'工具目录已变化'}:u==='/api/plugin-capabilities'?[mcpCapability]:u==='/api/plugin-connections'?[mcpConnection]:mcpCatalog,u.endsWith('/approve')?409:200);});
 await click('工具授权');await act(async()=>{document.querySelectorAll('dialog input[type=checkbox]')[0].click();});await act(async()=>{document.querySelectorAll('dialog input[type=checkbox]')[1].click();});await click('确认工具');
 assert.match(document.querySelector('dialog').textContent,/工具目录已变化/);assert.equal(document.querySelector('dialog input[type=checkbox]').checked,true);assert.equal(calls.filter(c=>c.u.endsWith('/approve')).length,1);
});

test('MCP call selects only approved tool and binds preview without immediate execution',async t=>{
 const calls=[];await mount(t,'skills',(u,o)=>{calls.push({u,o});return response(u==='/api/plugin-capabilities'?[mcpCapability]:u==='/api/plugin-connections'?[mcpConnection]:u.endsWith('/tools')?{...mcpCatalog,available:true,approved_tools:['notes.add']}:u.endsWith('/mcp-preview')?apiPreview:{status:'queued'});});
 await click('请求预览');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='填写输入').disabled,true);
 await act(async()=>{const el=document.querySelector('dialog select');el.value='notes.add';el.dispatchEvent(new window.Event('change',{bubbles:true}));});await click('填写输入');
 assert.match(document.querySelector('dialog').textContent,/notes.add/);await setValue('dialog textarea','{"title":"合成资料"}');await click('生成预览');
 assert.deepEqual(JSON.parse(calls.find(c=>c.u.endsWith('/mcp-preview')).o.body).tool,'notes.add');assert.ok(!calls.some(c=>c.u.endsWith('/confirm')));
 await click('放弃请求');assert.ok(calls.some(c=>c.u.endsWith('/cancel')));
});

test('unknown MCP calls show manual resolution and render returned content as inert JSON',async t=>{
 await mount(t,'invocations',()=>response([{id:'mcp',adapter:'mcp',plugin_id:'sample.mcp',plugin_version:'1.0.0',status:'unknown',created_at:1,result:'{"content":[{"text":"<script>x()</script>"}]}',receipt:{},error:null}]));
 assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='核对结果'));
 await click('查看结果');assert.equal(document.querySelector('dialog script'),null);assert.match(document.querySelector('dialog pre').textContent,/<script>/);
});
