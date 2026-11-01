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
  for (const [specifier,url] of Object.entries({
    react:pathToFileURL(require.resolve('react')).href,
    'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,
    ...imports
  })) code=code.replaceAll(`from '${specifier}'`,`from '${url}'`).replaceAll(`from "${specifier}"`,`from "${url}"`);
  return moduleUrl(code);
}
const window=new Window({url:'http://localhost/#/models/policies'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,
  FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const setupUrl=compile('../src/ModelSetup.tsx');
const apiUrl=compile('../src/api.ts');
const formUrl=compile('../src/AddModelForm.tsx',{'./api':apiUrl,'./ModelSetup':setupUrl});
const {AddModelForm}=await import(formUrl);
const {ModelSetup,RoutingControls}=await import(setupUrl);
const uiUrl=compile('../src/ui.tsx',{'./api':apiUrl});
const personaUrl=compile('../src/ConversationPersona.tsx',{'./api':apiUrl,'./ui':uiUrl});
const times=compile('../src/MemoryTimes.tsx');
const review=compile('../src/MemoryReview.tsx',{'./api':apiUrl,'./ui':uiUrl,'./MemoryTimes':times});
const {ModelsPage,ProviderForm}=await import(compile('../src/pages.tsx',{'./MemoryTimes':times,'./MemoryReview':review,'./AgentTools':compile('../src/AgentTools.tsx',{'./api':apiUrl,'./ui':uiUrl}),
  './VoicePages':compile('../src/VoicePages.tsx',{'./AgentTools':compile('../src/AgentTools.tsx',{'./api':apiUrl,'./ui':uiUrl}),'./VoiceRealtime':compile('../src/VoiceRealtime.tsx',{'./api':apiUrl,'./ui':uiUrl,'./voiceAudio':compile('../src/voiceAudio.ts')}),'./api':apiUrl,'./ui':uiUrl,'./voiceAudio':compile('../src/voiceAudio.ts')}),'./MediaPages':compile('../src/MediaPages.tsx',{'./api':apiUrl,'./ui':uiUrl,'./MediaStorage':compile('../src/MediaStorage.tsx',{'./api':apiUrl,'./ui':uiUrl})}),'./ModelSetup':setupUrl,'./api':apiUrl,'./AddModelForm':formUrl,'./ModelCalls':compile('../src/ModelCalls.tsx',{'./api':apiUrl,'./ui':uiUrl}),'./ConversationPersona':personaUrl,
  './MemoryPaging':compile('../src/MemoryPaging.tsx',{'./api':apiUrl}),
  'react-markdown':pathToFileURL(require.resolve('react-markdown')).href
}));
const provider={id:'p',name:'兼容服务',enabled:true,version:1};
const model=purpose=>({id:purpose,provider_id:'p',purpose,name:purpose+'-model',status:'verified',context_window:8192});
const policy=purpose=>({purpose,model_id:purpose,version:1});
async function mount(t,component,props) {
  document.body.innerHTML='<div id="root"></div>';
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(component,props)));
  return root;
}

test('fixed selection reflects saved policy when catalog loads later',async t=>{
  let finish;
  const catalog=new Promise(resolve=>{finish=resolve;});
  globalThis.fetch=async url=>{
    if(url==='/api/models')return await catalog;
    const data=url==='/api/providers'?[provider]:url==='/api/model-policies'?[policy('chat')]:url==='/api/model-routing'?routingDefault:[];
    return new Response(JSON.stringify(data),{status:200});
  };
  await mount(t,ModelsPage,{page:'policies'});
  await act(async()=>finish(new Response(JSON.stringify([model('chat')]),{status:200})));
  assert.equal(document.querySelector('select[name=model_id]').value,'chat');
});

test('empty setup guides user to connections without claiming ready',async t=>{
  await mount(t,ModelSetup,{providers:[],models:[],policies:[]});
  assert.match(document.body.textContent,/先配置日常对话模型/);
  assert.equal(document.querySelectorAll('li').length,4);
  for(const link of document.querySelectorAll('li a')) assert.equal(link.getAttribute('href'),'#/models/providers');
  assert.doesNotMatch(document.body.textContent,/配置就绪/);
});

const routingDefault={version:0,mode:'fixed',candidate_ids:[],allow_fallback:false,cross_provider:false,minimum_context:8192,recipients:[],current:true,notice:''};
test('routing strategy settings and evidence require explicit confirmation',async t=>{
  const requests=[];
  const request=async(url,method='GET',value)=>{
    requests.push({method,value});
    if(method==='GET')return routingDefault;
    if(method==='POST')return {preview_digest:'c'.repeat(64),preview_expires_at:2000000000,recipients:[],notice:'已过滤授权',
      selection:{strategy:'estimated_cost',applied:'priority',selected_model_id:'chat',reason:'部分价格缺失，保留候选顺序',
        evidence:[{model_id:'chat',status:'missing_price',samples:0},{model_id:'backup',status:'ready',reference_cost:'0.000000000002',currency:'USD',price_version:3,samples:0}]}};
    return {...value,version:1,current:true};
  };
  await mount(t,RoutingControls,{models:[model('chat'),model('backup')],providers:[provider],primary:policy('chat'),request});
  assert.equal(document.querySelector('select[aria-label="比较策略"]'),null);
  await choose('select[aria-label="选模方式"]','automatic');
  assert.equal(document.querySelector('select[aria-label="比较策略"]').value,'priority');
  await choose('select[aria-label="比较策略"]','estimated_cost');
  await choose('select[aria-label="参考币种"]','USD');
  assert.equal(document.querySelector('input[aria-label="参考输入"]').value,'1000');
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.match(document.querySelector('[aria-label="选择依据"]').textContent,/价格缺失.*候选顺序/);
  assert.match(document.body.textContent,/0.000000000002 USD/);
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
  await clickText('确认策略');
  const saved=requests.find(r=>r.method==='PUT').value;
  assert.equal(saved.selection_strategy,'estimated_cost');assert.equal(saved.reference_currency,'USD');
  assert.equal(saved.reference_input_tokens,1000);assert.equal(saved.reference_output_tokens,1000);
});

test('strategy edits invalidate preview and latency explains its limits',async t=>{
  const request=async(url,method='GET')=>method==='GET'?{...routingDefault,mode:'automatic',selection_strategy:'estimated_cost'}:
    {preview_digest:'a'.repeat(64),recipients:[],notice:'预览',selection:{strategy:'estimated_cost',applied:'priority',reason:'证据不足',evidence:[]}};
  await mount(t,RoutingControls,{models:[model('chat')],providers:[provider],primary:policy('chat'),request});
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.ok(document.querySelector('[aria-label="策略预览"]'));
  await choose('select[aria-label="比较策略"]','observed_latency');
  assert.equal(document.querySelector('[aria-label="策略预览"]'),null);
  assert.equal(document.querySelector('[aria-label="参考输入"]'),null);
  assert.match(document.body.textContent,/至少需 3 个/);
  assert.match(document.body.textContent,/不额外调用模型测速/);
});

test('latency evidence never labels missing measurements as zero',async t=>{
  const request=async(url,method='GET')=>method==='GET'?{...routingDefault,mode:'automatic',selection_strategy:'observed_latency'}:
    {preview_digest:'a'.repeat(64),recipients:[],notice:'预览',selection:{strategy:'observed_latency',applied:'priority',reason:'样本不足',
      evidence:[{model_id:'chat',status:'insufficient_samples',samples:0,median_ms:null}]}};
  await mount(t,RoutingControls,{models:[model('chat')],providers:[provider],primary:policy('chat'),request});
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.match(document.body.textContent,/有效样本 0 个 · 总耗时中位数 未知/);
  assert.doesNotMatch(document.body.textContent,/中位数 0 ms/);
});

test('routing defaults to fixed and never saves before explicit preview confirmation',async t=>{
  const requests=[];
  const request=async(url,method='GET',value)=>{
    requests.push({url,method,value});
    if(method==='GET')return routingDefault;
    if(method==='POST')return {preview_digest:'a'.repeat(64),preview_expires_at:2000000000,recipients:[{model_id:'chat',model:'chat-model',provider_name:'已授权服务',base_url:'https://api.example/custom',provider_version:1,protocol:'openai',context_window:8192}],notice:'资料只发送给所列接收者'};
    return {...value,version:1,current:true};
  };
  await mount(t,RoutingControls,{models:[model('chat')],providers:[provider],primary:policy('chat'),request});
  assert.equal(document.querySelector('select[aria-label="选模方式"]').value,'fixed');
  assert.equal(document.querySelector('input[type=checkbox]').checked,false);
  await choose('select[aria-label="选模方式"]','automatic');
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(requests.filter(r=>r.method==='PUT').length,0);
  assert.match(document.body.textContent,/https:\/\/api.example\/custom/);
  await clickText('确认策略');
  assert.equal(requests.filter(r=>r.method==='PUT').length,1);
  const submitted=requests.find(r=>r.method==='PUT').value;
  assert.deepEqual(submitted.candidate_ids,['chat']);
  assert.equal(submitted.cross_provider,false);assert.equal(submitted.preview_digest,'a'.repeat(64));
  assert.match(document.body.textContent,/策略已保存/);
});

test('editing or cancelling routing preview never authorizes stale recipients',async t=>{
  const requests=[];
  const request=async(url,method='GET',value)=>{requests.push(method);return method==='GET'?routingDefault:{preview_digest:'b'.repeat(64),preview_expires_at:2000000000,recipients:[],notice:'预览'};};
  await mount(t,RoutingControls,{models:[model('chat')],providers:[provider],primary:policy('chat'),request});
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.ok(document.querySelector('[aria-label="策略预览"]'));
  await choose('select[aria-label="选模方式"]','automatic');
  assert.equal(document.querySelector('[aria-label="策略预览"]'),null);
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  await clickText('取消预览');
  assert.equal(document.querySelector('[aria-label="策略预览"]'),null);assert.ok(!requests.includes('PUT'));
});

test('routing failure retains error and provides connection retry',async t=>{
  let count=0;
  const request=async()=>{count++;if(count===1)throw Error('路由连接失败');return routingDefault;};
  await mount(t,RoutingControls,{models:[],providers:[],request});
  assert.match(document.querySelector('[role=alert]').textContent,/连接失败/);
  await clickText('重新加载');assert.equal(count,2);
  assert.ok(document.querySelector('select[aria-label="选模方式"]'));
});

test('stale routing and confirmation failures do not claim saved',async t=>{
  const request=async(url,method='GET')=>{
    if(method==='GET')return {...routingDefault,version:2,current:false,notice:'代理已变化'};
    if(method==='POST')return {preview_digest:'a'.repeat(64),preview_expires_at:1,recipients:[],notice:'请核对'};
    throw Error('预览已经过期');
  };
  await mount(t,RoutingControls,{models:[],providers:[],request});
  assert.match(document.body.textContent,/代理已变化/);
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  await clickText('确认策略');
  assert.match(document.querySelector('[role=alert]').textContent,/过期/);
  assert.doesNotMatch(document.body.textContent,/策略已保存/);
  assert.equal(document.querySelector('[aria-label="策略预览"]'),null);
});

const catalogResponse=(id,names)=>new Response(JSON.stringify({provider_id:id,provider_version:1,models:names.map(id=>({id}))}),{status:200});
async function choose(selector,value) {
  const element=document.querySelector(selector);
  await act(async()=>{element.value=value;element.dispatchEvent(new window.Event('change',{bubbles:true}));});
}
async function clickText(text) {
  const button=[...document.querySelectorAll('button')].find(el=>el.textContent===text);
  assert.ok(button,text);
  await act(async()=>button.click());
}

test('add form automatically loads selected provider models and saves selected ID',async t=>{
  const requests=[],saves=[];
  globalThis.fetch=async url=>{requests.push(url);return catalogResponse('p',['alpha','beta']);};
  await mount(t,AddModelForm,{providers:[provider],busy:false,error:'',save:async body=>saves.push(body)});
  assert.deepEqual(requests,['/api/providers/p/models']);
  assert.match(document.body.textContent,/已获取 2 个模型/);
  await choose('select[name=name]','beta');
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.deepEqual(saves,[{provider_id:'p',name:'beta',purpose:'chat',context_window:8192}]);
});

test('provider form selects native protocol defaults but preserves custom API root',async t=>{
  const saved=[];
  await mount(t,ProviderForm,{provider:'new',busy:false,error:'',save:async body=>saved.push(body)});
  assert.equal(document.querySelector('select[name=protocol]').value,'openai');
  await choose('select[name=protocol]','anthropic');
  assert.equal(document.querySelector('input[name=base_url]').value,'https://api.anthropic.com/v1');
  assert.match(document.body.textContent,/不会|当前接入/);
  await choose('select[name=protocol]','gemini');
  assert.equal(document.querySelector('input[name=base_url]').value,'https://generativelanguage.googleapis.com/v1beta');
  const input=document.querySelector('input[name=base_url]');
  await act(async()=>{
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'https://custom.example/api');
    input.dispatchEvent(new window.Event('input',{bubbles:true}));
  });
  await choose('select[name=protocol]','anthropic');
  assert.equal(input.value,'https://custom.example/api');
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(saved[0].protocol,'anthropic');assert.equal(saved[0].base_url,'https://custom.example/api');
});

test('editing protocol requires a new key and busy form prevents duplicate save',async t=>{
  const props={provider:{...provider,protocol:'openai',base_url:'https://custom.example/api'},busy:false,error:'版本冲突，请重新打开',save:async()=>assert.fail('busy must not save')};
  const root=await mount(t,ProviderForm,props);
  assert.equal(document.querySelector('input[name=api_key]').required,false);
  await choose('select[name=protocol]','gemini');
  assert.equal(document.querySelector('input[name=api_key]').required,true);
  assert.equal(document.querySelector('input[name=base_url]').value,'https://custom.example/api');
  assert.match(document.querySelector('[role=alert]').textContent,/版本冲突/);
  await act(async()=>root.render(createElement(ProviderForm,{...props,busy:true})));
  assert.ok([...document.querySelectorAll('input,select,button')].every(el=>el.disabled));
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
});

test('native model selection filters declared purposes and clears stale selection',async t=>{
  const saves=[];
  globalThis.fetch=async()=>new Response(JSON.stringify({provider_id:'p',provider_version:1,models:[
    {id:'models/chat',purposes:['chat','extraction']},{id:'models/embed',purposes:['embedding']},{id:'models/unsupported',purposes:[]}
  ]}),{status:200});
  await mount(t,AddModelForm,{providers:[{...provider,protocol:'gemini'}],busy:false,error:'',save:async body=>saves.push(body)});
  assert.deepEqual([...document.querySelectorAll('select[name=name] option')].map(el=>el.value),['','models/chat']);
  assert.equal(document.querySelector('select[name=purpose] option[value=rerank]').disabled,true);
  await choose('select[name=name]','models/chat');
  await choose('select[name=purpose]','embedding');
  assert.equal(document.querySelector('select[name=name]').value,'');
  assert.deepEqual([...document.querySelectorAll('select[name=name] option')].map(el=>el.value),['','models/embed']);
  await choose('select[name=name]','models/embed');
  await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(saves[0].purpose,'embedding');
  assert.equal(saves[0].name,'models/embed');
  assert.equal(document.querySelector('input[name=name]'),null);
  assert.doesNotMatch(document.body.textContent,/刷新列表|手动填写/);
});

test('switching providers clears selected ID and ignores late responses',async t=>{
  let finishOld;
  const requests=[];
  globalThis.fetch=(url,options)=>{
    requests.push({url,signal:options.signal});
    if(url==='/api/providers/p/models') return new Promise(resolve=>{finishOld=()=>resolve(catalogResponse('p',['old-model']));});
    return Promise.resolve(catalogResponse('second',['new-model']));
  };
  await mount(t,AddModelForm,{providers:[provider,{...provider,id:'second',name:'另一个服务'}],busy:false,error:'',save:async()=>{}});
  await choose('select[name=provider_id]','second');
  assert.equal(requests[0].signal.aborted,true);
  await act(async()=>finishOld());
  const select=document.querySelector('select[name=name]');
  assert.match(select.textContent,/new-model/);assert.doesNotMatch(select.textContent,/old-model/);
  assert.equal(select.value,'');
  assert.equal([...document.querySelectorAll('button')].find(el=>el.textContent==='添加模型').disabled,true);
});

test('catalog failures disable selection without refresh or manual entry controls',async t=>{
  let attempts=0;
  globalThis.fetch=async()=>{attempts++;return new Response(JSON.stringify({detail:'接口不支持模型目录'}),{status:502});};
  await mount(t,AddModelForm,{providers:[provider],busy:false,error:'',save:async()=>assert.fail('Must not save without catalog')});
  assert.match(document.querySelector('[role=alert]').textContent,/接口不支持模型目录/);
  assert.doesNotMatch(document.body.textContent,/刷新列表|手动填写|可重试/);
  assert.equal(document.querySelector('input[name=name]'),null);
  assert.equal(document.querySelector('select[name=name]').disabled,true);
  assert.equal(document.querySelector('button[type=button]'),null);
  assert.equal(attempts,1);
  assert.equal([...document.querySelectorAll('button')].find(el=>el.textContent==='添加模型').disabled,true);
});

test('empty catalog does not invent models and disabled connections do not fetch',async t=>{
  let requests=0;
  globalThis.fetch=async()=>{requests++;return catalogResponse('p',[]);};
  const root=await mount(t,AddModelForm,{providers:[{...provider,enabled:false}],busy:false,error:'',save:async()=>{}});
  assert.equal(requests,0);assert.match(document.body.textContent,/请先添加并启用/);
  await act(async()=>root.render(createElement(AddModelForm,{key:'enabled',providers:[provider],busy:false,error:'',save:async()=>{}})));
  assert.equal(requests,1);assert.match(document.body.textContent,/列表为空/);
  assert.equal(document.querySelectorAll('select[name=name] option').length,1);
});

test('verified but unselected model is not ready; configured chat does not imply memory ready',async t=>{
  await mount(t,ModelSetup,{providers:[provider],models:[model('chat'),model('embedding')],policies:[policy('chat')]});
  const rows=document.querySelectorAll('li');
  assert.match(rows[0].textContent,/配置就绪/);
  assert.match(rows[2].textContent,/等待选用/);
  assert.match(document.body.textContent,/长期记忆还需/);
});

test('disabled and failed connections do not grant readiness',async t=>{
  await mount(t,ModelSetup,{providers:[{...provider,enabled:false}],models:[model('chat')],policies:[policy('chat')]});
  assert.doesNotMatch(document.body.textContent,/配置就绪/);
});

test('fully configured models still distinguish configuration from quality validation',async t=>{
  const purposes=['chat','extraction','embedding','rerank'];
  await mount(t,ModelSetup,{providers:[provider],models:purposes.map(model),policies:purposes.map(policy)});
  assert.equal([...document.querySelectorAll('li')].filter(row=>row.textContent.includes('配置就绪')).length,4);
  assert.match(document.body.textContent,/实际索引与召回仍需联调验证/);
});

test('capability test prevents duplicate submits and refreshes failed model state',async t=>{
  let finish,failed=false,calls=0;
  globalThis.fetch=async (url,options)=>{
    if (options.method==='POST') {
      calls++;
      return new Promise(resolve=>{finish=()=>{failed=true;resolve(new Response(JSON.stringify({detail:'模型服务返回 HTTP 429；请检查额度'}),{status:502}));};});
    }
    const data=url==='/api/providers'?[provider]:url==='/api/models'?[{...model('chat'),status:failed?'failed':'unverified'}]:[];
    return new Response(JSON.stringify(data),{status:200});
  };
  await mount(t,ModelsPage,{page:'catalog'});
  const button=[...document.querySelectorAll('button')].find(el=>el.textContent==='验证能力');
  assert.ok(button);
  await act(async()=>button.click());
  assert.equal(button.disabled,true);assert.equal(button.textContent,'正在验证');
  await act(async()=>button.click());assert.equal(calls,1);
  await act(async()=>finish());
  assert.match(document.querySelector('[role=alert]').textContent,/429.*额度/);
  assert.match(document.querySelector('tbody').textContent,/处理失败/);
  assert.equal(button.disabled,false);assert.equal(button.textContent,'验证能力');
});
