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
const window=new Window({url:'http://localhost/#/assistants/personas'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,
  FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const apiUrl=compile('../src/api.ts');
const uiUrl=compile('../src/ui.tsx',{'./api':apiUrl});
const pluginAgents=compile('../src/PluginAgents.tsx',{'./api':apiUrl,'./ui':uiUrl});
const {AssistantPages}=await import(compile('../src/AssistantPages.tsx',{'./api':apiUrl,'./ui':uiUrl,'./PluginAgents':pluginAgents}));
const {ConversationPersona}=await import(compile('../src/ConversationPersona.tsx',{'./api':apiUrl,'./ui':uiUrl}));
const profile={id:'p1',kind:'persona',name:'并肩伙伴',description:'真实配置',enabled:true,version:1,spec:{style:'自然简洁',language:'简体中文',instructions:'',examples:[],voice:'',model_id:null},updated_at:1,readiness:'configured',issues:[]};
const support={mode:'balanced',emotion_enabled:true,auto_observe:false,observation_channels:['web_text'],observation_ttl_hours:1,notes:'',self_report:'今天疲惫',report_ttl_hours:4,version:1,report_expires_at:2000000000,evidence:'user_report'};
const response=(data,status=200)=>new Response(JSON.stringify(data),{status});
async function mount(t,component,props,handler) {
  document.body.innerHTML='<div id="root"></div>';
  globalThis.fetch=async(url,options)=>handler(url,options);
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});
  await act(async()=>root.render(createElement(component,props)));
  return root;
}
async function clickText(text) {
  const button=[...document.querySelectorAll('button')].find(el=>el.textContent===text);
  assert.ok(button,text);await act(async()=>button.click());
}
async function change(selector,value) {
  const input=document.querySelector(selector);
  assert.ok(input,selector);
  await act(async()=>{input.value=value;input.dispatchEvent(new window.Event('change',{bubbles:true}));});
}
const readData=url=>url==='/api/personas'?[profile]:[];

test('persona editor sends real version and keeps a conflicting edit open',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'personas'},(url,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'配置已变化，请重新加载后编辑'},409);}
    return response(readData(url));
  });
  await clickText('编辑配置');
  await clickText('保存配置');
  assert.equal(writes[0].version,1);
  assert.equal(writes[0].style,'自然简洁');
  assert.ok(document.querySelector('dialog'));
  assert.match(document.querySelector('dialog').textContent,/配置已变化/);
});

test('disabling persona requires confirmation and copies preserve source version',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'personas'},(url,options)=>{
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...profile,enabled:false,version:2});}
    return response(readData(url));
  });
  await clickText('停用配置');
  assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/不能用于新回合/);
  const confirm=[...document.querySelectorAll('dialog button')].find(b=>b.textContent==='停用配置');
  await act(async()=>confirm.click());
  assert.equal(writes.length,1);assert.equal(writes[0].enabled,false);assert.equal(writes[0].version,1);
});

test('saving an edit preserves the card and returns keyboard focus during reload',async t=>{
  let pending,reads=0;
  await mount(t,AssistantPages,{page:'personas'},(url,options)=>{
    if(options.method==='PUT')return response({...profile,version:2});
    if(url==='/api/personas'&&++reads>2)return new Promise(resolve=>{pending=resolve;});
    return response(readData(url));
  });
  const edit=[...document.querySelectorAll('button')].find(el=>el.textContent==='编辑配置');
  edit.focus();await act(async()=>edit.click());
  await clickText('保存配置');
  assert.equal(document.querySelector('dialog'),null);
  assert.equal(edit.isConnected,true);
  assert.equal(document.activeElement,edit);
  await act(async()=>pending(response([{...profile,version:2}])));
  assert.equal(document.activeElement,edit);
});

test('empty and unavailable assistant configuration never claims live dispatch',async t=>{
  await mount(t,AssistantPages,{page:'agents'},()=>response([]));
  assert.match(document.body.textContent,/由任务中心的协作任务自动选用/);
  assert.match(document.body.textContent,/尚无助手配置/);
});

test('support preferences clear report only after confirmation and explicit save',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...support,...writes.at(-1),version:2});}
    return response(support);
  });
  await act(async()=>document.querySelector('input[type=checkbox]').click());
  assert.ok(document.querySelector('dialog'));assert.equal(writes.length,0);
  await clickText('关闭适配');
  assert.equal(writes.length,0);
  assert.equal(document.querySelector('input[type=checkbox]').checked,false);
  await clickText('保存偏好');
  assert.equal(writes[0].version,1);assert.equal(writes[0].self_report,'');assert.equal(writes[0].emotion_enabled,false);
  assert.match(document.body.textContent,/支持偏好已保存/);
});

test('renewing the same self report requires an explicit checkbox and save',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...support,version:writes.length+1});}
    return response(support);
  });
  const renew=()=>[...document.querySelectorAll('label')].find(el=>el.textContent.includes('重新确认当前感受')).querySelector('input');
  assert.equal(renew().checked,false);
  await clickText('保存偏好');assert.equal(writes[0].renew_report,false);
  await act(async()=>renew().click());assert.equal(writes.length,1);
  await clickText('保存偏好');assert.equal(writes[1].renew_report,true);
  assert.equal(renew().checked,false);
});

test('stale support form keeps its edits and never silently renews after a conflict',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'支持偏好已变化，请重新加载后编辑'},409);}
    return response(support);
  });
  await clickText('保存偏好');
  assert.equal(writes[0].version,1);assert.equal(writes[0].renew_report,false);
  assert.match(document.body.textContent,/支持偏好已变化/);
  assert.equal(document.querySelector('textarea').value,support.notes);
  const help=[...document.querySelectorAll('details')].find(el=>el.querySelector('summary')?.textContent==='聊天设置');
  assert.equal(help.querySelectorAll('li').length,7);
  assert.match(help.textContent,/独立 Web/);assert.match(help.textContent,/语音/);
  assert.match(document.body.textContent,/过期的旧表单也不会使感受重新生效/);
});

test('automatic observations stay opt-in until save and display cost and scope',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...support,...writes.at(-1),version:2});}
    return response(support);
  });
  const checkbox=[...document.querySelectorAll('input[type=checkbox]')].find(el=>el.parentElement.textContent.includes('开启自动情绪线索'));
  assert.equal(checkbox.checked,false);
  await act(async()=>checkbox.click());
  assert.equal(writes.length,0);
  assert.match(document.body.textContent,/额外费用/);
  assert.match(document.body.textContent,/不进入长期记忆或子助手/);
  await clickText('保存偏好');
  assert.equal(writes[0].auto_observe,true);
  assert.equal(writes[0].observation_ttl_hours,1);
  assert.deepEqual(writes[0].observation_channels,['web_text']);
});

test('additional observation entries require explicit saved selection and preserve failed edits',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({detail:'记忆抽取模型不可用，请验证后重试'},409);}
    return response({...support,auto_observe:true});
  });
  const boxes=[...document.querySelectorAll('fieldset input')];
  assert.equal(boxes.length,3);
  assert.deepEqual(boxes.map(x=>x.checked),[true,false,false]);
  await act(async()=>{boxes[1].click();boxes[2].click();});
  assert.equal(writes.length,0);
  assert.match(document.body.textContent,/群聊、陌生人及隔离身份不参与/);
  await clickText('保存偏好');
  assert.deepEqual(writes[0].observation_channels,['web_text','confirmed_voice','qq_private']);
  assert.match(document.body.textContent,/记忆抽取模型不可用/);
  assert.deepEqual([...document.querySelectorAll('fieldset input')].map(x=>x.checked),[true,true,true]);
});

test('observation entry controls disable with auto analysis and label source provenance',async t=>{
  await mount(t,AssistantPages,{page:'support'},url=>response(url==='/api/emotion-observations'?
    [{...hint,source_kind:'qq_private'}]:support));
  assert.equal(document.querySelector('fieldset').disabled,true);
  assert.match(document.body.textContent,/来源入口：绑定私聊 · 仅用于原会话/);
});

test('turning off automatic observations requires confirmation and preserves manual report',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({...support,...writes.at(-1),version:2});}
    return response({...support,auto_observe:true});
  });
  const box=[...document.querySelectorAll('input[type=checkbox]')].find(el=>el.parentElement.textContent.includes('开启自动情绪线索'));
  await act(async()=>box.click());
  assert.equal(writes.length,0);assert.equal(box.checked,true);
  await clickText('关闭线索');assert.equal(writes.length,0);
  await clickText('保存偏好');
  assert.equal(writes[0].auto_observe,false);assert.equal(writes[0].self_report,support.self_report);
});

const hint={id:'e1',conversation_id:'c1',source_id:'m1',status:'ready',label:'stress',evidence:'inferred',quote:'我现在喘不过气',version:3,created_at:1,expires_at:2000000000,error:null};
test('observations distinguish inference and correction has versioned confirmation',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([hint]);
    if(options.method==='POST'){writes.push(JSON.parse(options.body));return response({...hint,version:4,evidence:'user_correction'});}
    return response(support);
  });
  assert.match(document.body.textContent,/系统推测 · 可能判断有误/);
  assert.equal(document.querySelector('a[href*="?c="]').getAttribute('href'),'#/chat/conversation?c=c1');
  await clickText('纠正感受');assert.equal(writes.length,0);
  await change('dialog select','calm');await clickText('保存纠正');
  assert.deepEqual(writes,[{version:3,action:'correct',label:'calm'}]);
  assert.equal(document.querySelector('dialog'),null);
});

test('rejected feedback errors keep the dialog and source evidence visible',async t=>{
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([hint]);
    if(options.method==='POST')return response({detail:'线索已变化，请重新加载'},409);
    return response(support);
  });
  await clickText('撤回线索');await clickText('确认撤回');
  assert.match(document.querySelector('dialog').textContent,/请重新加载/);
  assert.match(document.body.textContent,/我现在喘不过气/);
});

test('deletion uses a version fence and only proceeds after explicit confirmation',async t=>{
  const writes=[];
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return response([hint]);
    if(options.method==='DELETE'){writes.push(url);return response({...hint,status:'deleted',quote:''});}
    return response(support);
  });
  await clickText('删除线索');assert.equal(writes.length,0);
  assert.match(document.querySelector('dialog').textContent,/原始聊天不会被删除/);
  await clickText('确认删除');
  assert.deepEqual(writes,['/api/emotion-observations/e1?version=3']);
});

test('confirmed deletion never shows cached evidence when the following reload fails',async t=>{
  let deleted=false;
  await mount(t,AssistantPages,{page:'support'},(url,options)=>{
    if(url==='/api/emotion-observations')return deleted?response({detail:'连接中断，请重新加载'},503):response([hint]);
    if(options.method==='DELETE'){deleted=true;return response({...hint,version:4,status:'deleted',quote:'',label:'',evidence:''});}
    return response(support);
  });
  assert.match(document.body.textContent,/我现在喘不过气/);
  await clickText('删除线索');await clickText('确认删除');
  assert.doesNotMatch(document.body.textContent,/我现在喘不过气/);
  assert.match(document.body.textContent,/连接中断/);
  assert.equal(document.activeElement.textContent,'加载线索');
});

test('conversation selection sends version only after explicit switch and preserves memory notice',async t=>{
  const writes=[];
  await mount(t,ConversationPersona,{conversationId:'c1',disabled:false},(url,options)=>{
    if(url==='/api/personas')return response([profile]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({});}
    return response({persona_id:null,version:3,persona_name:'并肩伙伴',persona_version:0});
  });
  await change('select','p1');assert.equal(writes.length,0);
  await clickText('切换人格');
  assert.deepEqual(writes,[{persona_id:'p1',version:3}]);
  assert.match(document.body.textContent,/同一套对话与长期记忆/);
  assert.match(document.body.textContent,/会话人格已更新/);
});

test('in-flight chat prevents persona selection and writes',async t=>{
  await mount(t,ConversationPersona,{conversationId:'c1',disabled:true},url=>response(url==='/api/personas'?[profile]:{persona_id:null,version:0}));
  assert.equal(document.querySelector('select').disabled,true);
  assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='切换人格').disabled,true);
  assert.match(document.body.textContent,/结束后可切换/);
});

test('conversation may explicitly return to workspace persona inheritance',async t=>{
  const writes=[];
  await mount(t,ConversationPersona,{conversationId:'c1',disabled:false},(url,options)=>{
    if(url==='/api/personas')return response([profile]);
    if(options.method==='PUT'){writes.push(JSON.parse(options.body));return response({});}
    return response({persona_id:'p1',version:4,persona_name:'专注伙伴',persona_version:2,inherit_default:false});
  });
  await change('select','@workspace');assert.equal(writes.length,0);
  await clickText('切换人格');assert.deepEqual(writes,[{persona_id:null,inherit_default:true,version:4}]);
});
