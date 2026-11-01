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
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {TaskPages}=await import(compile('../src/TaskPages.tsx',{'./MediaPages':compile('../src/MediaPages.tsx',{'./api':api,'./ui':ui,'./MediaStorage':compile('../src/MediaStorage.tsx',{'./api':api,'./ui':ui})}),'./api':api,'./ui':ui,'./AgentRuns':compile('../src/AgentRuns.tsx',{'./MediaPages':compile('../src/MediaPages.tsx',{'./api':api,'./ui':ui,'./MediaStorage':compile('../src/MediaStorage.tsx',{'./api':api,'./ui':ui})}),'./AgentTools':compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui}),'./api':api,'./ui':ui})}));
const response=(value,status=200)=>new Response(JSON.stringify(value),{status});
const task={id:'task-1',title:'准备演示',skill_id:'project.plan',skill_version:'1.0.0',status:'queued',
  version:3,created_at:1,updated_at:1,cancel_requested:false,error:null,input:{goal:'准备演示'},config:{},result:{},
  conversation_id:null,result_message_id:null,attempts:[],events:[{seq:1,kind:'queued',created_at:1,data:{}}]};
const reminder={id:'reminder-1',title:'参加演示',content:'带上提纲',due_at:Date.now()/1000+3600,
  timezone:'Asia/Shanghai',version:4,status:'scheduled',created_at:1,updated_at:1,conversation_id:null,error:null,receipt:null};
const skill={id:'project.plan',name:'项目规划',version:'1.0.0',plugin_id:'lingxi.builtin',available:true,
  reason:null,side_effects:'可能产生模型费用',output_description:'仅生成计划，尚未执行',input_schema:{}};
async function mount(t,page,handler) {
  document.body.innerHTML='<div id="root"></div>';
  globalThis.fetch=async(uri,options)=>handler(uri,options);
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(TaskPages,{page})));
}
async function click(text,scope=document) {
  const button=[...scope.querySelectorAll('button')].find(el=>el.textContent===text);
  assert.ok(button,text);await act(async()=>button.click());
}
const empty=uri=>uri.startsWith('/api/tasks?')||uri.startsWith('/api/reminders?')?{items:[],next_cursor:null}:[];

test('new task blocks unconfigured capability and explains the prerequisite',async t=>{
  await mount(t,'list',uri=>response(uri==='/api/skills'?[{...skill,available:false,reason:'请先配置模型'}]:empty(uri)));
  await click('新建任务');
  assert.match(document.querySelector('dialog').textContent,/请先配置模型/);
  assert.equal([...document.querySelectorAll('button')].find(el=>el.textContent==='提交任务').disabled,true);
  assert.match(document.body.textContent,/计划中的行动不会自动执行/);
});

test('task network retry retains idempotency key and does not double submit while pending',async t=>{
  const writes=[];let finish;
  await mount(t,'list',(uri,options)=>{
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return new Promise(resolve=>finish=resolve);}
    return response(uri==='/api/skills'?[skill]:empty(uri));
  });
  await click('新建任务');
  document.querySelector('input[name=title]').value='准备演示';
  document.querySelector('textarea[name=goal]').value='规划一次演示';
  await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(writes.length,1);
  await act(async()=>finish(response({detail:'暂时无法连接'},503)));
  assert.match(document.querySelector('dialog').textContent,/暂时无法连接/);
  await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(writes[1].request_id,writes[0].request_id);
  assert.equal(writes[0].input.goal,'规划一次演示');
  await act(async()=>finish(response({detail:'暂时无法连接'},503)));
});

test('task cancellation requires named confirmation and uses the actual version',async t=>{
  const writes=[];
  await mount(t,'list',(uri,options)=>{
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return response({detail:'任务状态已变化'},409);}
    return response(uri.startsWith('/api/tasks?')?{items:[task],next_cursor:null}:uri==='/api/tasks/task-1'?task:[]);
  });
  await click('查看任务');await click('取消任务');
  assert.equal(writes.length,0);
  assert.equal(document.querySelectorAll('dialog').length,2);
  assert.match(document.querySelectorAll('dialog')[1].textContent,/准备演示/);
  await click('确认取消');
  assert.deepEqual(writes,[{version:3}]);
  assert.match(document.querySelectorAll('dialog')[1].textContent,/任务状态已变化/);
});

test('task details distinguish a generated plan from executed work',async t=>{
  const completed={...task,status:'succeeded',result:{summary:'演示计划',steps:[{title:'整理','action':'梳理内容','completion':'形成提纲'}],
    assumptions:['时间待定'],note:'仅交付计划，步骤尚未执行。',verification:'structure_only'}};
  await mount(t,'list',uri=>response(uri.startsWith('/api/tasks?')?{items:[completed],next_cursor:null}:uri==='/api/tasks/task-1'?completed:[]));
  await click('查看任务');
  assert.match(document.querySelector('dialog').textContent,/步骤尚未执行/);
  assert.match(document.querySelector('dialog').textContent,/完成条件：形成提纲/);
  assert.equal([...document.querySelectorAll('button')].some(el=>el.textContent==='重试任务'),false);
});

test('reminder conflict retains input and sends the absolute date with explicit browser zone',async t=>{
  const writes=[];
  await mount(t,'reminders',(uri,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'提醒已变化'},409);}
    return response(uri.startsWith('/api/reminders?')?{items:[reminder],next_cursor:null}:[]);
  });
  await click('编辑提醒');
  const dateInput=document.querySelector('input[type=datetime-local]');
  const changedDate=new Date(Date.now()+7200000);
  dateInput.value=new Date(changedDate.getTime()-changedDate.getTimezoneOffset()*60000).toISOString().slice(0,16);
  const value=dateInput.value;
  await click('保存提醒');
  assert.equal(writes[0].version,4);
  assert.equal(writes[0].due_at,new Date(value).getTime()/1000);
  assert.equal(writes[0].timezone,Intl.DateTimeFormat().resolvedOptions().timeZone);
  assert.equal(document.querySelector('input[name=title]').value,'参加演示');
  assert.match(document.querySelector('dialog').textContent,/提醒已变化/);
});

test('reminder cancellation is confirmed and stored receipt never implies reading',async t=>{
  const writes=[];
  await mount(t,'reminders',(uri,options)=>{
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return response({...reminder,status:'cancelled'});}
    return response(uri.startsWith('/api/reminders?')?{items:[reminder],next_cursor:null}:[]);
  });
  await click('取消提醒');assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/参加演示/);
  await click('取消提醒',document.querySelector('dialog'));
  assert.deepEqual(writes,[{version:4}]);
  assert.match(document.body.textContent,/不代表推送送达或已读/);
});

test('fired reminder acknowledgement is explicit and does not send any other channel',async t=>{
  const writes=[];
  await mount(t,'reminders',(uri,options)=>{
    if(options.method==='POST'){writes.push({uri,body:JSON.parse(options.body)});return response({});}
    return response(uri.startsWith('/api/reminders?')?{items:[{...reminder,status:'fired',receipt:{status:'stored',stored_at:2,acknowledged_at:null}}],next_cursor:null}:[]);
  });
  assert.match(document.body.textContent,/尚未确认/);
  assert.equal(writes.length,0);
  await click('标记确认');
  assert.deepEqual(writes,[{uri:'/api/reminders/reminder-1/acknowledge',body:{version:4}}]);
});
