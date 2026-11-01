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
const window=new Window({url:'http://localhost/#/settings/general'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api}),proxy=compile('../src/ProxySettings.tsx',{'./api':api});
const bundles=compile('../src/ConfigurationBundles.tsx',{'./api':api,'./ui':ui});
const {SystemSettings}=await import(compile('../src/SystemSettings.tsx',{'./api':api,'./ui':ui,'./ProxySettings':proxy,'./ConfigurationBundles':bundles}));
const config={workspace_name:'个人工作空间',default_timezone:'Asia/Shanghai',default_persona_id:null,history_budget_bytes:48000};
const current={version:0,config,updated_at:null};
const checked={before:current,after:{...config,workspace_name:'研究空间'},changes:[{field:'workspace_name',before:config.workspace_name,after:'研究空间'}],action:'edit',source_version:null,token:'preview-token-opaque',expires_at:Date.now()/1000+300,note:'新回合生效，已有授权保持原值。'};
const response=(data,status=200)=>new Response(JSON.stringify(data),{status});
async function mount(t,handler=()=>undefined){
  const requests=[];document.body.innerHTML='<div id="root"></div>';
  globalThis.fetch=async(url,options)=>{requests.push({url,...options});const supplied=await handler(url,options);if(supplied)return supplied;
    if(url==='/api/system-settings')return response(current);
    if(url==='/api/personas')return response([{id:'p1',name:'专注伙伴',version:2,enabled:true}]);
    if(url==='/api/admin/system-settings/revisions')return response({items:[],next_before:null});
    if(url==='/api/admin/proxy')return response({enabled:false,http_proxy:'',https_proxy:'',all_proxy:'',no_proxy:[],protected_no_proxy:[],version:0});
    if(url==='/api/admin/configuration-bundles')return response({versions:{workspace:0,media:1,operations:0,model_budget:0}});
    throw new Error('unexpected '+url);
  };
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(SystemSettings)));
  return requests;
}
async function click(text){const button=[...document.querySelectorAll('button')].find(e=>e.textContent===text);assert.ok(button,text);await act(async()=>button.click());}
async function change(selector,value){await act(async()=>{const el=document.querySelector(selector);assert.ok(el,selector);el.value=value;el.dispatchEvent(new window.Event('input',{bubbles:true}));el.dispatchEvent(new window.Event('change',{bubbles:true}));});}

test('configuration edits require server preview and explicit confirmation; cancel restores focus',async t=>{
  const requests=await mount(t,(url)=>url.endsWith('/previews')?response(checked):undefined);
  assert.match(document.body.textContent,/系统代理/);assert.equal(document.querySelector('input[name=workspace_name]').value,config.workspace_name);
  await change('input[name=workspace_name]','研究空间');
  assert.equal(requests.filter(r=>r.method!=='GET').length,0);
  const trigger=[...document.querySelectorAll('button')].find(b=>b.textContent==='预览更改');trigger.focus();
  await click('预览更改');assert.equal(requests.filter(r=>r.method==='PUT').length,0);
  assert.match(document.querySelector('dialog').textContent,/个人工作空间.*研究空间/);
  await click('取消更改');assert.equal(document.querySelector('dialog'),null);assert.equal(document.activeElement,trigger);
});

test('confirmed configuration commits token only and emits workspace refresh event',async t=>{
  let saved=false,events=0;const listener=()=>events++;window.addEventListener('lingxi:system-settings',listener);t.after(()=>window.removeEventListener('lingxi:system-settings',listener));
  const requests=await mount(t,(url,options)=>{
    if(url.endsWith('/previews'))return response(checked);
    if(options.method==='PUT'){saved=true;return response({version:1,config:checked.after,updated_at:1});}
    if(url==='/api/system-settings'&&saved)return response({version:1,config:checked.after,updated_at:1});
  });
  await change('input[name=workspace_name]','研究空间');await click('预览更改');await click('确认生效');
  assert.deepEqual(JSON.parse(requests.find(r=>r.method==='PUT').body),{token:checked.token,acknowledgement:'new_turns_only'});
  assert.equal(events,1);assert.equal(document.querySelector('dialog'),null);
  assert.match(document.body.textContent,/配置 v1 已生效/);assert.equal(document.querySelector('input[name=workspace_name]').value,'研究空间');
});

test('stale confirmation remains visible and never auto-retries or overwrites',async t=>{
  let writes=0;await mount(t,(url,options)=>url.endsWith('/previews')?response(checked):options.method==='PUT'?(writes++,response({detail:'系统配置已变化，请重新预览'},409)):undefined);
  await click('预览更改');await click('确认生效');assert.equal(writes,1);
  assert.match(document.querySelector('dialog [role=alert]').textContent,/重新预览/);assert.ok(document.querySelector('dialog'));
});

test('expired preview cannot be submitted',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({...checked,expires_at:Date.now()/1000-5}):undefined);
  await click('预览更改');assert.match(document.querySelector('dialog').textContent,/预览已过期/);
  const button=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认生效');assert.ok(button.disabled);await act(async()=>button.click());
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('unchanged configuration preserves the specific server repair message',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({detail:'配置没有变化，无需提交'},422):undefined);
  await click('预览更改');assert.match(document.querySelector('[role=alert]').textContent,/配置没有变化/);
  assert.equal(document.querySelector('dialog'),null);assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('import validates JSON locally and still requires server-reviewed preview',async t=>{
  const requests=await mount(t,url=>url.endsWith('/previews')?response({...checked,action:'import'}):undefined);
  await click('导入配置');await change('dialog textarea','broken JSON');await click('预览导入');
  assert.match(document.querySelector('dialog [role=alert]').textContent,/JSON 格式/);assert.equal(requests.filter(r=>r.method==='POST').length,0);
  await change('dialog textarea',JSON.stringify({format:'lingxi-workspace-defaults/v1',config:checked.after}));await click('预览导入');
  assert.match(document.querySelector('dialog').getAttribute('aria-label'),/确认配置/);
  const payload=JSON.parse(requests.find(r=>r.method==='POST').body);assert.equal(payload.action,'import');assert.equal(payload.version,0);
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
});

test('rollback uses history version in preview; current version cannot be selected',async t=>{
  const requests=await mount(t,url=>url==='/api/admin/system-settings/revisions'?response({items:[{version:1,config:checked.after,created_at:1,action:'edit',source_version:null},{version:0,config,created_at:0,action:'initial',source_version:null}],next_before:null}):url.endsWith('/previews')?response({...checked,action:'rollback',source_version:1}):undefined);
  const buttons=[...document.querySelectorAll('button[aria-label]')];assert.ok(buttons.find(b=>b.getAttribute('aria-label')==='预览回退至 v0').disabled);
  await act(async()=>buttons.find(b=>b.getAttribute('aria-label')==='预览回退至 v1').click());
  assert.deepEqual(JSON.parse(requests.find(r=>r.method==='POST').body),{version:0,action:'rollback',source_version:1});
  assert.match(document.querySelector('dialog').textContent,/来源 v1/);
});

test('failed configuration read offers retry without guessed editable defaults',async t=>{
  let reads=0;await mount(t,url=>url==='/api/system-settings'&&++reads===1?response({detail:'服务不可用'},503):undefined);
  assert.equal(document.querySelector('input[name=workspace_name]'),null);assert.match(document.querySelector('[role=alert]').textContent,/服务不可用/);
  await click('重新加载');assert.ok(document.querySelector('input[name=workspace_name]'));
});

test('workspace settings load only after identity gate and no page introduction returns',()=>{
  const main=readFileSync(new URL('../src/main.tsx',import.meta.url),'utf8');
  assert.match(main,/if \(!entered \|\| !session\)/);assert.match(main,/workspaceName/);
  assert.doesNotMatch(main,/module-intro|界面预览/);
});

test('delayed preview restores the original trigger even if disabling it removed focus',async t=>{
  let resolve;const waiting=new Promise(r=>{resolve=r;});
  await mount(t,url=>url.endsWith('/previews')?waiting:undefined);
  const trigger=[...document.querySelectorAll('button')].find(b=>b.textContent==='预览更改');trigger.focus();
  await click('预览更改');assert.ok(trigger.disabled);await act(async()=>trigger.blur());
  await act(async()=>resolve(response(checked)));
  await click('取消更改');assert.equal(document.activeElement,trigger);
});
