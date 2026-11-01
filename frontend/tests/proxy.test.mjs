import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act, createElement} from 'react';

const require=createRequire(import.meta.url);
const moduleUrl=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}) {
  let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}
  }).outputText;
  for(const [specifier,url] of Object.entries({react:pathToFileURL(require.resolve('react')).href,
    'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))
    code=code.replaceAll(`from '${specifier}'`,`from '${url}'`).replaceAll(`from "${specifier}"`,`from "${url}"`);
  return moduleUrl(code);
}
const window=new Window({url:'http://localhost/#/settings/general'});
Object.assign(globalThis,{window,document:window.document,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const {ProxySettings}=await import(compile('../src/ProxySettings.tsx',{'./api':compile('../src/api.ts')}));
const config={enabled:false,http_proxy:'http://172.20.192.1:7890',https_proxy:'http://172.20.192.1:7890',
  all_proxy:'socks5://172.20.192.1:7890',no_proxy:[],version:2,protected_no_proxy:['core','db','hindsight','localhost']};
const response=(body,status=200)=>new Response(JSON.stringify(body),{status});
async function mount(t,handler) {
  document.body.innerHTML='<div id="root"></div>';
  globalThis.fetch=async(url,options)=>handler(url,options);
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(ProxySettings)));
}
async function click(text) {
  const button=[...document.querySelectorAll('button')].find(el=>el.textContent===text);
  assert.ok(button,text);await act(async()=>button.click());
}

test('system config loads proxy settings and only saves after explicit submit',async t=>{
  const requests=[];
  await mount(t,(url,options)=>{requests.push({url,options});return response(options.method==='GET'?config:{...JSON.parse(options.body),version:3});});
  assert.equal(requests.length,1);
  assert.equal(document.querySelector('input[name=all_proxy]').value,config.all_proxy);
  await act(async()=>document.querySelector('input[type=checkbox]').click());
  assert.equal(requests.length,1);
  await click('保存配置');
  assert.equal(requests[1].options.method,'PUT');
  const saved=JSON.parse(requests[1].options.body);
  assert.equal(saved.enabled,true);assert.equal(saved.version,2);
  assert.match(document.body.textContent,/代理已启用/);assert.match(document.body.textContent,/配置 v3/);
});

test('testing uses draft without saving and clearly reports the route',async t=>{
  const requests=[];
  await mount(t,(url,options)=>{requests.push({url,options});return response(options.method==='GET'?config:{ok:true,status_code:200,duration_ms:123,route:'https_proxy'});});
  await act(async()=>document.querySelector('input[type=checkbox]').click());
  await click('测试连接');
  assert.equal(requests.length,2);assert.equal(requests[1].url,'/api/admin/proxy/test');
  assert.equal(JSON.parse(requests[1].options.body).enabled,true);
  assert.match(document.body.textContent,/连接成功.*HTTPS 代理/);
  assert.match(document.body.textContent,/配置 v2/);
});

test('save conflicts stay visible and never silently overwrite configuration',async t=>{
  let writes=0;
  await mount(t,(url,options)=>options.method==='GET'?response(config):(writes++,response({detail:'代理配置已变化，请重新加载后再保存'},409)));
  await click('保存配置');assert.equal(writes,1);
  assert.match(document.querySelector('[role=alert]').textContent,/重新加载/);
  assert.match(document.body.textContent,/配置 v2/);
});

test('failed initial read offers reload without an editable guessed configuration',async t=>{
  let reads=0;
  await mount(t,()=>++reads===1?response({detail:'服务暂不可用'},503):response(config));
  assert.equal(document.querySelector('form'),null);
  assert.match(document.querySelector('[role=alert]').textContent,/服务暂不可用/);
  await click('重新加载');assert.ok(document.querySelector('form'));assert.equal(reads,2);
});
