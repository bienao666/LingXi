import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';

const require=createRequire(import.meta.url);
const url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}) {
  let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{
    compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}
  }).outputText;
  for(const [name,value] of Object.entries({react:pathToFileURL(require.resolve('react')).href,
    'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))
    code=code.replaceAll(`from '${name}'`,`from '${value}'`).replaceAll(`from "${name}"`,`from "${value}"`);
  return url(code);
}
const window=new Window({url:'http://localhost/#/tasks/list'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location,sessionStorage:window.sessionStorage});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {ProactivePages,ProactiveInbox}=await import(compile('../src/ProactivePages.tsx',{'./api':api,'./ui':ui}));
const response=(value,status=200)=>new Response(JSON.stringify(value),{status});
const preferences={version:4,enabled:true,mode:'companion',timezone:'Asia/Shanghai',quiet_start:'22:00',quiet_end:'08:00',daily_limit:2,min_gap_hours:3,allowed_kinds:['follow_up','suggestion','interest'],paused_until:null,conversation_id:null,blocked_sources:[]};
const topic={id:'topic-one',title:'演示接续',source_type:'memory',source_id:'memory-one',source_version:2,kind:'follow_up',not_before:Date.now()/1000+3600,expires_at:Date.now()/1000+86400,conversation_id:null,status:'pending',version:3,reason:'等待合适时机',origin:'core',created_at:1};
const contact={id:'contact-one',topic_id:'topic-one',content:'之前的计划里，第一步是整理提纲。',reason:'来源版本有效且符合联系偏好',status:'stored',conversation_id:null,message_id:null,feedback:null,feedback_at:null,version:2,created_at:1};
const qqTarget={kind:'qq',binding_id:'binding-one',binding_version:3,channel_version:4};
const destination={target:qqTarget,label:'我的 QQ · QQ 87654321',online:false};
const qqDelivery={kind:'qq',status:'pending',channel_id:'qq-one',target_label:destination.label,sent_at:null,protocol_message_id:null,error:null,reviewed_at:null};
const empty=uri=>uri==='/api/contact-preferences'?preferences:uri.startsWith('/api/conversation-topics?')||uri.startsWith('/api/proactive-contacts')||uri.startsWith('/api/tasks?')?{items:[],next_cursor:null}:[];
async function mount(t,page,handler,inbox=false) {
  document.body.innerHTML='<div id="root"></div>';
  window.sessionStorage.clear();
  globalThis.fetch=async(uri,options)=>handler(uri,options);
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(inbox?ProactiveInbox:ProactivePages,{page})));
}
async function click(text,scope=document) {
  const button=[...scope.querySelectorAll('button')].find(el=>el.textContent===text);
  assert.ok(button,text);await act(async()=>button.click());
}
async function submit(scope=document) {await act(async()=>scope.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('contact history separates confirmed voice receipts from text and actual hearing',async t=>{
  await mount(t,'history',uri=>response(uri.startsWith('/api/proactive-contacts')?{items:[{...contact,voice:{status:'stopped',expires_at:1,confirmed_parts:1}}],next_cursor:null}:empty(uri)));
  const receipt=document.querySelector('[aria-label="语音回执"]');
  assert.ok(receipt);assert.match(receipt.textContent,/播报停止/);assert.match(receipt.textContent,/回执 1 段/);
  assert.match(receipt.textContent,/不代表用户实际听见/);assert.match(document.body.textContent,/第一步是整理提纲/);
});

test('contact preference changes require a visible confirmation and preserve server version',async t=>{
  const writes=[];let saved=false;
  await mount(t,'preferences',(uri,options)=>{
    if(options.method==='PUT'){saved=true;writes.push(JSON.parse(options.body));return response({...preferences,version:5,enabled:false});}
    return response(uri==='/api/contact-preferences'&&saved?{...preferences,version:5,enabled:false}:empty(uri));
  });
  await act(async()=>document.querySelector('input[name=enabled]').click());
  await submit();
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/停止普通主动交流/);
  await click('确认保存');
  assert.equal(writes[0].version,4);
  assert.equal(writes[0].enabled,false);
  assert.match(document.body.textContent,/联系偏好已保存/);
  assert.equal(document.activeElement?.textContent,'保存偏好');
});

test('preference conflict keeps the draft and reports an actionable error',async t=>{
  await mount(t,'preferences',(uri,options)=>response(options.method==='PUT'?{detail:'联系偏好已变化，请重新加载后保存'}:empty(uri),options.method==='PUT'?409:200));
  document.querySelector('input[name=daily_limit]').value='1';
  await submit();await click('确认保存');
  assert.match(document.querySelector('dialog').textContent,/重新加载后保存/);
  assert.match(document.querySelector('dialog').textContent,/每天最多 1 次/);
  await click('继续编辑');
  assert.equal(document.querySelector('input[name=daily_limit]').value,'1');
});

test('new topic cannot be submitted without a real authorized source',async t=>{
  await mount(t,'topics',uri=>response(empty(uri)));
  await click('新建话题');
  assert.equal(document.querySelector('select[name=source_id]').disabled,true);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='保存安排').disabled,true);
  assert.match(document.querySelector('dialog').textContent,/同一来源版本/);
});

test('topic schedule uses the submitted dates and actual optimistic version',async t=>{
  const writes=[];
  await mount(t,'topics',(uri,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'话题状态已变化'},409);}
    return response(uri.startsWith('/api/conversation-topics?')?{items:[topic],next_cursor:null}:empty(uri));
  });
  await click('编辑安排');
  document.querySelector('input[name=not_before]').value='2027-01-02T12:00';
  document.querySelector('input[name=expires_at]').value='2027-01-03T13:00';
  await submit(document.querySelector('dialog'));
  assert.equal(writes[0].version,3);
  assert.equal(writes[0].not_before,new Date('2027-01-02T12:00').getTime()/1000);
  assert.match(document.querySelector('dialog').textContent,/话题状态已变化/);
});

test('closing a source topic is explicit and never deletes the memory',async t=>{
  const writes=[];
  await mount(t,'topics',(uri,options)=>{
    if(options.method==='POST'){writes.push({uri,body:JSON.parse(options.body)});return response({...topic,status:'closed',version:4});}
    return response(uri.startsWith('/api/conversation-topics?')?{items:[topic],next_cursor:null}:empty(uri));
  });
  await click('关闭话题');
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/演示接续/);
  await click('确认关闭');
  assert.equal(writes[0].uri,'/api/conversation-topics/topic-one/close');
  assert.deepEqual(writes[0].body,{version:3,block_source:true});
});

test('stored contact never implies reading and rejection waits for confirmation',async t=>{
  const writes=[];
  await mount(t,'history',(uri,options)=>{
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return response({...contact,version:3,feedback:'stop_topic'});}
    return response(uri.startsWith('/api/proactive-contacts?')?{items:[contact],next_cursor:null}:empty(uri));
  });
  assert.match(document.body.textContent,/尚无明确反馈/);
  assert.equal(writes.length,0);
  await click('不再提起');
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/不删除原记忆/);
  await click('确认设置');
  assert.deepEqual(writes[0],{version:2,feedback:'stop_topic'});
});

test('continue contact routes to its existing conversation without any model request',async t=>{
  const writes=[];
  await mount(t,'history',(uri,options)=>{
    if(options.method==='POST'){writes.push(uri);return response({conversation_id:'conversation-real'});}
    return response(uri.startsWith('/api/proactive-contacts?')?{items:[contact],next_cursor:null}:empty(uri));
  });
  await click('接续对话');
  assert.equal(window.location.hash,'#/chat/conversation?c=conversation-real');
  assert.deepEqual(writes,['/api/proactive-contacts/contact-one/continue']);
});

test('invalidated contact does not render stale content or allow continuing',async t=>{
  await mount(t,'history',uri=>response(uri.startsWith('/api/proactive-contacts?')?{items:[{...contact,status:'invalidated',content:'过期机密原文'}],next_cursor:null}:empty(uri)));
  assert.doesNotMatch(document.body.textContent,/过期机密原文/);
  assert.equal([...document.querySelectorAll('button')].some(b=>b.textContent==='接续对话'),false);
});

test('dismissing the browser notice does not acknowledge or infer a mood',async t=>{
  const writes=[];
  await mount(t,'history',(uri,options)=>{if(options.method!=='GET')writes.push(uri);return response({items:[contact],next_cursor:null});},true);
  assert.match(document.body.textContent,/方便时可以接着聊/);
  await click('暂时收起');
  assert.equal(document.querySelector('aside'),null);
  assert.deepEqual(writes,[]);
  assert.equal(window.sessionStorage.getItem('lingxi.contact.dismissed'),'contact-one');
});

test('explicit preference feedback refreshes and removes a now-disallowed notice',async t=>{
  let allowed=true;
  await mount(t,'history',()=>response({items:allowed?[contact]:[],next_cursor:null}),true);
  assert.ok(document.querySelector('aside'));
  allowed=false;
  await act(async()=>window.dispatchEvent(new window.Event('lingxi:contact-preferences')));
  assert.equal(document.querySelector('aside'),null);
});

async function chooseQQ() {
  const select=document.querySelector('select[aria-label="联系渠道"]');
  await act(async()=>{select.value=JSON.stringify(qqTarget);select.dispatchEvent(new window.Event('change',{bubbles:true}));});
}

test('QQ destination requires distinct unchecked personal memory consent and clears Web target',async t=>{
  const writes=[];
  await mount(t,'preferences',(uri,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'保持草稿'},409);}
    return response(uri==='/api/proactive-targets'?[destination]:uri==='/api/contact-preferences'?{...preferences,conversation_id:'web-one'}:empty(uri));
  });
  await chooseQQ();assert.equal(document.querySelector('select[name=conversation_id]').disabled,true);
  await submit();assert.equal(writes.length,0);
  const dialog=document.querySelector('dialog');assert.match(dialog.textContent,/我的 QQ/);
  assert.equal(dialog.querySelector('input[type=checkbox]').checked,false);
  const confirm=[...dialog.querySelectorAll('button')].find(b=>b.textContent==='确认保存');
  assert.equal(confirm.disabled,true);
  await act(async()=>dialog.querySelector('input[type=checkbox]').click());
  await click('确认保存',dialog);
  assert.equal(writes.length,1);assert.equal(writes[0].qq_acknowledged,true);
  assert.equal(writes[0].conversation_id,null);assert.deepEqual(writes[0].delivery,qqTarget);
});

test('stale QQ target has no automatic Web fallback and requires a new selection',async t=>{
  await mount(t,'preferences',uri=>response(uri==='/api/contact-preferences'?{...preferences,delivery:qqTarget}:empty(uri)));
  assert.match(document.body.textContent,/原 QQ 目标已变化/);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='保存偏好').disabled,true);
  assert.equal(document.querySelector('select[aria-label="联系渠道"]').value,JSON.stringify(qqTarget));
});

test('pending QQ contact cannot continue an unsent proposal',async t=>{
  await mount(t,'history',uri=>response(uri.startsWith('/api/proactive-contacts?')?{items:[{...contact,delivery:qqDelivery}],next_cursor:null}:empty(uri)));
  assert.match(document.body.textContent,/等待发送/);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='接续对话').disabled,true);
});

test('unknown QQ review uses versioned acknowledgment and never offers resend',async t=>{
  const writes=[];
  await mount(t,'history',(uri,options)=>{
    if(options.method==='POST'){writes.push({uri,body:JSON.parse(options.body)});return response({detail:'联系记录已变化，请重新加载'},409);}
    return response(uri.startsWith('/api/proactive-contacts?')?{items:[{...contact,delivery:{...qqDelivery,status:'unknown'}}],next_cursor:null}:empty(uri));
  });
  await click('核对投递');const dialog=document.querySelector('dialog');
  assert.match(dialog.textContent,/不补发/);
  assert.equal([...dialog.querySelectorAll('button')].find(b=>b.textContent==='结束等待').disabled,true);
  await act(async()=>dialog.querySelector('input[type=checkbox]').click());
  await click('结束等待',dialog);
  assert.deepEqual(writes,[{uri:'/api/proactive-contacts/contact-one/delivery-review',body:{version:2,acknowledgement:'checked_no_resend'}}]);
  assert.match(dialog.textContent,/重新加载/);
});

test('protocol acceptance shows receipt and continues only its exact conversation',async t=>{
  await mount(t,'history',uri=>response(uri.startsWith('/api/proactive-contacts?')?{items:[{...contact,conversation_id:'qq-conversation',delivery:{...qqDelivery,status:'accepted_by_protocol',protocol_message_id:'1234'}}],next_cursor:null}:empty(uri)));
  assert.match(document.body.textContent,/协议接受/);
  assert.match(document.body.textContent,/1234/);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='接续对话').disabled,false);
});

test('successful unknown review preserves status and returns focus to history when trigger disappears',async t=>{
  let reviewed=false;
  await mount(t,'history',(uri,options)=>{
    if(options.method==='POST'){reviewed=true;return response({...qqDelivery,status:'unknown',reviewed_at:123});}
    return response(uri.startsWith('/api/proactive-contacts?')?{items:[{...contact,delivery:{...qqDelivery,status:'unknown',reviewed_at:reviewed?123:null}}],next_cursor:null}:empty(uri));
  });
  await click('核对投递');
  await act(async()=>document.querySelector('dialog input[type=checkbox]').click());
  await click('结束等待');
  assert.equal(document.querySelector('dialog'),null);
  assert.equal(document.activeElement?.textContent,'联系记录');
  assert.match(document.body.textContent,/结果未知/);
  assert.equal([...document.querySelectorAll('button')].some(b=>b.textContent==='核对投递'),false);
});

test('model suggestion opt-in discloses source forwarding and cost before saving',async t=>{
  const writes=[];
  await mount(t,'preferences',(uri,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...preferences,model_generation:true,version:5});}
    return response(empty(uri));
  });
  assert.equal(document.querySelector('input[name=model_generation]').checked,false);
  await act(async()=>document.querySelector('input[name=model_generation]').click());
  await submit();
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/模型构思（可能产生费用/);
  assert.match(document.querySelector('dialog').textContent,/授权来源将发送/);
  await click('确认保存');
  assert.equal(writes[0].model_generation,true);
});

test('failed suggestion retries only after confirmation with both current versions',async t=>{
  const writes=[];let queued=false;
  const generation={id:'draft-one',status:'failed',version:4,attempt:1,model:'fixture-chat',error:'引用校验未通过'};
  await mount(t,'topics',(uri,options)=>{
    if(options.method==='POST'){writes.push(JSON.parse(options.body));queued=true;return response({...generation,status:'queued',version:5,attempt:2});}
    return response(uri.startsWith('/api/conversation-topics?')?{items:[{...topic,generation:{...generation,status:queued?'queued':'failed'}}],next_cursor:null}:empty(uri));
  });
  assert.match(document.body.textContent,/引用校验未通过/);
  await click('重试构思');
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/可能产生模型费用/);
  await click('确认重试');
  assert.deepEqual(writes,[{version:3,draft_version:4,acknowledgement:'retry_one_model_call'}]);
  assert.equal(document.activeElement?.textContent,'待续话题');
  assert.match(document.body.textContent,/构思已重新安排/);
  assert.equal([...document.querySelectorAll('button')].some(b=>b.textContent==='重试构思'),false);
});

test('suggestion retry conflict keeps the dialog and never hides current error',async t=>{
  await mount(t,'topics',(uri,options)=>response(options.method==='POST'?{detail:'构思状态已变化，请重新加载'}:
    uri.startsWith('/api/conversation-topics?')?{items:[{...topic,generation:{id:'draft-one',status:'invalidated',version:4,attempt:1,model:'<img src=x onerror=alert(1)>',error:'授权已变化'}}],next_cursor:null}:empty(uri),options.method==='POST'?409:200));
  assert.equal(document.querySelector('img'),null);
  await click('重试构思');await click('确认重试');
  assert.match(document.querySelector('dialog').textContent,/构思状态已变化/);
  assert.equal([...document.querySelector('dialog').querySelectorAll('button')].find(b=>b.textContent==='确认重试').disabled,false);
});
