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
  for(const [specifier,url] of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${specifier}'`,`from '${url}'`).replaceAll(`from "${specifier}"`,`from "${url}"`);
  return 'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
}
const window=new Window({url:'http://localhost/#/settings/general'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {ConfigurationBundles}=await import(compile('../src/ConfigurationBundles.tsx',{'./api':api,'./ui':ui}));
const documentValue={format:'lingxi-configuration-bundle/v1',modules:{workspace:{workspace_name:'测试空间',default_timezone:'Asia/Shanghai',default_persona_id:null,history_budget_bytes:6000},media:{max_files:3,max_file_mib:10,max_total_mib:20,allowed_types:['text']},operations:{log_retention_days:7,daily_token_alert:100},model_budget:{daily_limit:5,execution_limit:2}}};
const current={document:documentValue,versions:{workspace:1,media:2,operations:3,model_budget:4}};
const checked={versions:current.versions,changes:[{module:'workspace',field:'workspace_name',before:'个人工作空间',after:'测试空间'},{module:'media',field:'allowed_types',before:['text','pdf'],after:['text']}],token:'opaque-bundle-token',expires_at:Date.now()/1000+300,note:'同一事务生效；缩短日志保留期会永久清理过期结构化日志。'};
const response=(data,status=200)=>new Response(JSON.stringify(data),{status});
async function mount(t,handler=()=>undefined){
  const requests=[];let applied=0;
  document.body.innerHTML='<main id="main-content" tabindex="-1"><div id="root"></div></main>';
  globalThis.fetch=async(url,options)=>{requests.push({url,...options});return await handler(url,options)||response(url.startsWith('/api/admin/configuration-revisions')?{items:[],next_before:null}:current);};
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(ConfigurationBundles,{onApplied:()=>applied++})));
  return {requests,applied:()=>applied};
}
async function click(text){const button=[...document.querySelectorAll('button')].find(e=>e.textContent===text);assert.ok(button,text);await act(async()=>button.click());}
async function input(value){await act(async()=>{const el=document.querySelector('dialog textarea');el.value=value;el.dispatchEvent(new window.Event('input',{bubbles:true}));el.dispatchEvent(new window.Event('change',{bubbles:true}));});}
async function open(value=documentValue){await click('导入整包');await input(JSON.stringify(value));}
async function acknowledge(){await act(async()=>document.querySelector('dialog input[type=checkbox]').click());}

const historyRow={id:'revision-one',number:1,action:'import',modules:['workspace','media'],created_at:1700000000,source_id:null,source_point:null};
const historyDetail={...historyRow,before_snapshot:current,after_snapshot:current};
function historical(handler=()=>undefined){return (url,options)=>{
  const supplied=handler(url,options);if(supplied)return supplied;
  if(url==='/api/admin/configuration-revisions')return response({items:[historyRow],next_before:null});
  if(url==='/api/admin/configuration-revisions/revision-one')return response(historyDetail);
};}

test('history preview binds selected scope, point and current versions before consent',async t=>{
  const state=await mount(t,historical(url=>url.endsWith('/previews')?response({...checked,action:'rollback',source_id:historyRow.id,source_point:'before'}):undefined));
  const trigger=[...document.querySelectorAll('button')].find(b=>b.textContent==='核对回退');trigger.focus();
  await click('核对回退');assert.equal(document.activeElement.textContent,'选择范围');
  assert.match(document.querySelector('dialog').textContent,/历史 #1/);
  await act(async()=>document.querySelector('dialog fieldset input').click());
  await click('预览回退');
  assert.deepEqual(JSON.parse(state.requests.find(r=>r.method==='POST').body),{point:'before',modules:['media'],versions:{media:2}});
  assert.equal(document.activeElement.textContent,'回退差异');
  assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent==='确认回退').disabled);
  await click('确认回退');assert.equal(state.requests.filter(r=>r.method==='PUT').length,0);
  await click('关闭');assert.equal(document.activeElement,trigger);
});

test('history rollback records only reviewed token, refreshes history and focuses main content',async t=>{
  const state=await mount(t,historical(url=>url.endsWith('/previews')?response(checked):undefined));
  await click('核对回退');await click('预览回退');await acknowledge();await click('确认回退');
  assert.deepEqual(JSON.parse(state.requests.find(r=>r.method==='PUT').body),{token:checked.token,acknowledgement:'atomic_configuration_changes'});
  assert.equal(state.applied(),1);assert.equal(document.activeElement.id,'main-content');
  assert.match(document.body.textContent,/历史配置已创建新版本/);
  assert.ok(state.requests.filter(r=>r.url==='/api/admin/configuration-revisions').length>=2);
});

test('history conflict retains source and diff, does not automatically resubmit',async t=>{
  const state=await mount(t,historical((url,o)=>url.endsWith('/previews')?response(checked):o.method==='PUT'?response({detail:'配置已变化，未应用任何更改'},409):undefined));
  await click('核对回退');await click('预览回退');await acknowledge();await click('确认回退');
  assert.equal(document.activeElement.getAttribute('role'),'alert');assert.match(document.querySelector('dialog').textContent,/未应用任何更改/);
  assert.equal(state.requests.filter(r=>r.method==='PUT').length,1);
  await click('返回选择');assert.equal(document.querySelector('dialog select').value,'before');
});

test('history expired preview never submits a rollback',async t=>{
  const state=await mount(t,historical(url=>url.endsWith('/previews')?response({...checked,expires_at:1}):undefined));
  await click('核对回退');await click('预览回退');await acknowledge();await click('确认回退');
  assert.match(document.querySelector('dialog').textContent,/预览已过期/);
  assert.equal(state.requests.filter(r=>r.method==='PUT').length,0);
});

test('history source can select after snapshot and rejects an empty module selection locally',async t=>{
  const state=await mount(t,historical(url=>url.endsWith('/previews')?response(checked):undefined));
  await click('核对回退');
  await act(async()=>{const el=document.querySelector('dialog select');el.value='after';el.dispatchEvent(new window.Event('change',{bubbles:true}));for(const box of document.querySelectorAll('dialog fieldset input'))if(box.checked)box.click();});
  await click('预览回退');assert.match(document.querySelector('dialog [role=alert]').textContent,/至少选择一个/);
  assert.equal(state.requests.filter(r=>r.method==='POST').length,0);
  await act(async()=>document.querySelectorAll('dialog fieldset input')[2].click());await click('预览回退');
  assert.deepEqual(JSON.parse(state.requests.find(r=>r.method==='POST').body),{point:'after',modules:['operations'],versions:{operations:3}});
});

test('history failures remain retryable and do not invent an empty history',async t=>{
  let failed=true;
  await mount(t,historical(url=>url==='/api/admin/configuration-revisions'&&failed?response({detail:'历史读取失败'},503):undefined));
  assert.match(document.body.textContent,/历史读取失败/);assert.doesNotMatch(document.body.textContent,/尚无统一变更记录/);
  failed=false;await click('刷新历史');assert.match(document.body.textContent,/整包导入/);
});

test('renders four real module versions and requires reviewed explicit consent',async t=>{
  const state=await mount(t,url=>url.endsWith('/previews')?response(checked):undefined);
  assert.match(document.body.textContent,/空间默认v1媒体限制v2运维偏好v3调用限额v4/);
  const trigger=[...document.querySelectorAll('button')].find(b=>b.textContent==='导入整包');trigger.focus();
  await open();await click('预览整包');
  assert.equal(document.activeElement.textContent,'差异总览');
  assert.match(document.querySelector('dialog').textContent,/个人工作空间.*测试空间/);
  assert.match(document.querySelector('dialog').textContent,/纯文本、文本 PDF.*纯文本/);
  const confirm=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认整包');assert.ok(confirm.disabled);
  await click('确认整包');assert.equal(state.requests.filter(r=>r.method==='PUT').length,0);
  await click('关闭');assert.equal(document.activeElement,trigger);
});

test('scope excludes unchecked modules and uses target current versions only',async t=>{
  const state=await mount(t,url=>url.endsWith('/previews')?response(checked):undefined);
  await open();
  const boxes=[...document.querySelectorAll('fieldset input')];
  await act(async()=>{boxes[1].click();boxes[2].click();boxes[3].click();});
  await click('预览整包');
  const body=JSON.parse(state.requests.find(r=>r.method==='POST').body);
  assert.deepEqual(Object.keys(body.document.modules),['workspace']);
  assert.deepEqual(body.versions,{workspace:1});
  assert.equal(body.document.modules.workspace.default_persona_id,null);
});

test('commit sends only bound preview token then reloads parent and focuses content',async t=>{
  let events=0;const listener=()=>events++;window.addEventListener('lingxi:system-settings',listener);t.after(()=>window.removeEventListener('lingxi:system-settings',listener));
  const state=await mount(t,url=>url.endsWith('/previews')?response(checked):undefined);
  await open();await click('预览整包');await acknowledge();await click('确认整包');
  assert.deepEqual(JSON.parse(state.requests.find(r=>r.method==='PUT').body),{token:checked.token,acknowledgement:'atomic_configuration_changes'});
  assert.equal(state.applied(),1);assert.equal(events,1);assert.equal(document.querySelector('dialog'),null);
  assert.equal(document.activeElement.id,'main-content');assert.match(document.body.textContent,/配置包已生效/);
});

test('version conflict retains reviewed diff without automatic retry; back keeps original input',async t=>{
  const state=await mount(t,(url,o)=>url.endsWith('/previews')?response(checked):o.method==='PUT'?response({detail:'所选模块的配置已变化；未应用任何更改'},409):undefined);
  await open();await click('预览整包');await acknowledge();await click('确认整包');
  assert.match(document.querySelector('dialog [role=alert]').textContent,/未应用任何更改/);
  assert.equal(document.activeElement.getAttribute('role'),'alert');
  assert.equal(state.requests.filter(r=>r.method==='PUT').length,1);
  await click('返回修改');assert.equal(document.querySelector('textarea').value,JSON.stringify(documentValue));
  assert.ok(state.requests.filter(r=>r.url==='/api/admin/configuration-bundles'&&r.method==='GET').length>=2);
});

test('expired preview cannot submit even after acknowledgement',async t=>{
  const state=await mount(t,url=>url.endsWith('/previews')?response({...checked,expires_at:1}):undefined);
  await open();await click('预览整包');await acknowledge();await click('确认整包');
  assert.match(document.querySelector('dialog').textContent,/预览已过期/);assert.equal(state.requests.filter(r=>r.method==='PUT').length,0);
});

test('invalid JSON and empty selected scope do not reach server',async t=>{
  const state=await mount(t);
  await click('导入整包');await input('bad-json');await click('预览整包');assert.match(document.querySelector('[role=alert]').textContent,/JSON 格式不正确/);
  await input(JSON.stringify(documentValue));await act(async()=>{for(const box of document.querySelectorAll('fieldset input'))box.click();});
  await click('预览整包');assert.match(document.querySelector('[role=alert]').textContent,/至少选择一个/);
  assert.equal(state.requests.filter(r=>r.method==='POST').length,0);
});

test('partial packages allow only supplied modules, unknown keys still go to strict server validation',async t=>{
  const state=await mount(t,url=>url.endsWith('/previews')?response({detail:'字段不兼容'},422):undefined);
  await open({...documentValue,modules:{operations:documentValue.modules.operations,typo_grant:{secret:'bad'}}});
  const boxes=[...document.querySelectorAll('fieldset input')];assert.deepEqual(boxes.map(v=>v.disabled),[true,true,false,true]);
  await click('预览整包');const body=JSON.parse(state.requests.find(r=>r.method==='POST').body);
  assert.ok(body.document.modules.typo_grant);assert.deepEqual(body.versions,{operations:3});assert.match(document.querySelector('[role=alert]').textContent,/字段不兼容/);
});

test('failed state read offers retry and cannot enable import with guessed versions',async t=>{
  let count=0;await mount(t,url=>url==='/api/admin/configuration-bundles'&&++count===1?response({detail:'状态不可用'},503):undefined);
  assert.match(document.querySelector('[role=alert]').textContent,/状态不可用/);
  assert.ok([...document.querySelectorAll('button')].find(v=>v.textContent==='导入整包').disabled);
  await click('刷新版本');assert.equal([...document.querySelectorAll('button')].find(v=>v.textContent==='导入整包').disabled,false);
});

test('double submit while preview pending sends once and forbids dismissing pending change',async t=>{
  let resolve;const pending=new Promise(r=>{resolve=r;});
  const state=await mount(t,url=>url.endsWith('/previews')?pending:undefined);
  await open();await click('预览整包');
  await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(state.requests.filter(r=>r.method==='POST').length,1);
  assert.ok([...document.querySelectorAll('button')].find(v=>v.textContent==='关闭').disabled);
  await act(async()=>resolve(response(checked)));
  assert.equal(document.querySelector('dialog').getAttribute('aria-label'),'核对整包');
});
