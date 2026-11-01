import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const [name,value]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${name}'`,`from '${value}'`).replaceAll(`from "${name}"`,`from "${value}"`);return url(code);}
const window=new Window({url:'http://localhost/#/settings/voice'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api}),actualAudio=compile('../src/voiceAudio.ts');
const {pcmWav}=await import(actualAudio);
const fakeAudio=url(`export class Microphone{async start(){globalThis.micStarts++;}async listen(device,callbacks){globalThis.micStarts++;globalThis.listeningCallbacks=callbacks;if(globalThis.listenGate)await globalThis.listenGate;}setDetection(mode){globalThis.detectionModes.push(mode);}async finish(){return new Blob(['test']);}discard(){globalThis.micDiscards++;}} export async function audioDevices(){return [];}`);
const {VoiceSettings,VoicePanel}=await import(compile('../src/VoicePages.tsx',{'./VoiceRealtime':compile('../src/VoiceRealtime.tsx',{'./api':api,'./ui':ui,'./voiceAudio':fakeAudio}),'./api':api,'./ui':ui,'./voiceAudio':fakeAudio}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const config={enabled:true,version:1,asr:{provider_id:'p',model:'asr'},tts:{provider_id:'p',model:'tts'},language:'zh',voice:'alloy',speed:1,checks:{asr:true,tts:true}};
const session={id:'voice',conversation_id:'conversation',status:'active',expires_at:Date.now()/1000+900,config_version:1};
async function mount(t,component,props,handler){document.body.innerHTML='<div id="root"></div>';globalThis.micStarts=0;globalThis.micDiscards=0;globalThis.detectionModes=[];globalThis.listeningCallbacks=null;globalThis.listenGate=null;globalThis.fetch=async(u,o)=>handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));return root;}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}

test('capture WAV has correct duration PCM fields and bounded resampling',async()=>{
 const blob=pcmWav([new Float32Array(48000).fill(.5)],48000),buffer=await blob.arrayBuffer(),v=new DataView(buffer);
 assert.equal(v.getUint32(24,true),16000);assert.equal(v.getUint16(22,true),1);assert.equal(v.getUint16(34,true),16);assert.equal(v.getUint32(40,true),32000);assert.equal(blob.size,32044);
 assert.throws(()=>pcmWav([new Float32Array(10)],48000),/时长/);assert.throws(()=>pcmWav([new Float32Array(16000*61)],16000),/时长/);
});

test('settings describe real capability checks and do not record on mount',async t=>{
 const paths=[];await mount(t,VoiceSettings,{},(u,o)=>{paths.push(u);return response(u==='/api/providers'?[{id:'p',name:'服务',enabled:true}]:u.endsWith('/models')?{models:[{id:'asr'},{id:'tts'}]}:config);});
 assert.equal(micStarts,0);assert.match(document.body.textContent,/人工智能合成/);assert.match(document.body.textContent,/接口已验证/);assert.ok(!paths.some(p=>p.includes('test-')));
});

const personaVoice={id:'p1',name:'伙伴音色',version:3,voice:'nova',enabled:true,verified:false,issue:''};
async function choosePersona(){const select=[...document.querySelectorAll('select')].find(s=>s.closest('label')?.textContent.startsWith('选择人格'));assert.ok(select);await act(async()=>{select.value='p1';select.dispatchEvent(new window.Event('change',{bubbles:true}));});}

test('persona voice requires explicit selection and validation with saved version',async t=>{
 const writes=[];const prior=URL.createObjectURL;URL.createObjectURL=()=> 'blob:persona-voice';t.after(()=>{URL.createObjectURL=prior;});
 await mount(t,VoiceSettings,{},(u,o)=>{writes.push({u,o});if(u.endsWith('/test'))return new Response(new Blob(['wav']));return response(u==='/api/providers'?[]:u==='/api/voice/personas'?[personaVoice]:config);});
 assert.equal(writes.filter(r=>r.o.method==='POST').length,0);assert.equal(micStarts,0);
 assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='验证音色').disabled,true);
 await choosePersona();assert.match(document.body.textContent,/伙伴音色 · v3 · nova/);await click('验证音色');
 assert.deepEqual(JSON.parse(writes.find(r=>r.u.endsWith('/test')).o.body),{version:3});
 assert.match(document.body.textContent,/请点击播放器试听/);assert.equal(micStarts,0);
});

test('persona validation error is visible and never claims successful verification',async t=>{
 await mount(t,VoiceSettings,{},(u,o)=>u.endsWith('/test')?response({detail:'人格已变化，请重新加载'},409):response(u==='/api/providers'?[]:u==='/api/voice/personas'?[personaVoice]:config));
 await choosePersona();await click('验证音色');assert.match(document.querySelector('[role=alert]').textContent,/人格已变化/);assert.equal(document.querySelector('audio'),null);
});

test('unsaved persona linkage prevents paid validation and saves explicit switch',async t=>{
 const writes=[];await mount(t,VoiceSettings,{},(u,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...config,...writes.at(-1),version:2});}return response(u==='/api/providers'?[]:u==='/api/voice/personas'?[personaVoice]:config);});
 await choosePersona();const checkbox=[...document.querySelectorAll('input[type=checkbox]')].find(c=>c.closest('label').textContent.includes('跟随人格'));
 assert.equal(checkbox.checked,false);await act(async()=>checkbox.click());assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='验证音色').disabled,true);
 await act(async()=>document.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));assert.equal(writes[0].follow_persona,true);assert.equal(writes[0].voice,'alloy');
});

test('invalid or disabled persona voice cannot trigger validation',async t=>{
 const writes=[];await mount(t,VoiceSettings,{},(u,o)=>{writes.push({u,o});return response(u==='/api/providers'?[]:u==='/api/voice/personas'?[{...personaVoice,issue:'音色标识无效'}]:config);});await choosePersona();
 assert.match(document.body.textContent,/音色标识无效/);await click('验证音色');assert.equal(writes.filter(r=>r.o.method==='POST').length,0);
});

test('voice opening never starts mic and ending keeps chat task',async t=>{
 const paths=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{paths.push({u,o});return response(session);});
 await click('开启语音');assert.equal(micStarts,0);assert.match(document.body.textContent,/不会自动打开麦克风/);await click('结束语音');assert.ok(paths.some(p=>p.o.method==='DELETE'));assert.ok(!paths.some(p=>p.u.includes('/cancel')));
});

test('transcript waits for user confirmation then sends through core once',async t=>{
 const writes=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{writes.push({u,o});return response(u.endsWith('/transcriptions')?{text:'我喜欢喝茶。'}:u.endsWith('/messages')?{id:'run'}:session);});
 await click('开启语音');await click('开始录音');assert.equal(micStarts,1);await click('结束录音');assert.equal(document.querySelector('textarea').value,'我喜欢喝茶。');assert.ok(!writes.some(r=>r.u.endsWith('/messages')));await click('确认发送');assert.equal(writes.filter(r=>r.u.endsWith('/messages')).length,1);assert.match(document.body.textContent,/正在生成/);
});

test('discarding transcript never submits chat or memory',async t=>{
 const paths=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{paths.push(u);return response(u.endsWith('/transcriptions')?{text:'临时内容'}:session);});await click('开启语音');await click('开始录音');await click('结束录音');await click('丢弃转写');assert.equal(document.querySelector('textarea'),null);assert.ok(!paths.some(p=>p.endsWith('/messages')||p.includes('memories')));
});

test('failed voice request preserves draft and retry request id',async t=>{
 const ids=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{if(u.endsWith('/messages')){ids.push(JSON.parse(o.body).request_id);return response({detail:'请求失败，请重试'},503);}return response(u.endsWith('/transcriptions')?{text:'保留草稿'}:session);});await click('开启语音');await click('开始录音');await click('结束录音');await click('确认发送');await click('确认发送');assert.equal(ids[0],ids[1]);assert.equal(document.querySelector('textarea').value,'保留草稿');assert.match(document.querySelector('[role=alert]').textContent,/请求失败/);
});

test('offline immediately discards recording and ends voice only',async t=>{
 const paths=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{paths.push(u);return response(session);});await click('开启语音');await click('开始录音');await act(async()=>window.dispatchEvent(new window.Event('offline')));assert.ok(micDiscards>0);assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='开启语音'));assert.ok(!paths.some(p=>p.includes('/cancel')));
});

test('session opened after offline is immediately closed and never shown',async t=>{
 let finish;const writes=[];await mount(t,VoicePanel,{conversationId:'conversation',messages:[],runs:[],reload:async()=>{}},(u,o)=>{writes.push({u,o});return o.method==='DELETE'?response(session):new Promise(r=>finish=r);});await click('开启语音');await act(async()=>window.dispatchEvent(new window.Event('offline')));await act(async()=>finish(response(session)));assert.ok(writes.some(r=>r.o.method==='DELETE'));assert.ok(![...document.querySelectorAll('button')].some(b=>b.textContent==='开始录音'));assert.equal(micStarts,0);
});

test('blocked playback resumes same audio without another paid synthesis and stop cancels no task',async t=>{
 const writes=[],props={conversationId:'conversation',messages:[],runs:[],reload:async()=>{}};
 const plan={id:'playback',message_id:'reply',parts:['第一句。','第二句。'],confirmed_parts:0,status:'active'};
 const priorCreate=URL.createObjectURL,priorRevoke=URL.revokeObjectURL;URL.createObjectURL=()=> 'blob:voice-test';URL.revokeObjectURL=()=>{};t.after(()=>{URL.createObjectURL=priorCreate;URL.revokeObjectURL=priorRevoke;});
 const root=await mount(t,VoicePanel,props,(u,o)=>{writes.push({u,o});return u.includes('/audio/')?new Response(new Blob(['audio'])):response(u.endsWith('/transcriptions')?{text:'请回答'}:u.endsWith('/messages')?{id:'run'}:u.endsWith('/playbacks')?plan:u.endsWith('/progress')?{...plan,status:'stopped'}:session);});
 const player=document.querySelector('audio');let plays=0,pauses=0;player.play=async()=>{if(++plays===1)throw new Error('NotAllowedError');};player.pause=()=>{pauses++;};
 await click('开启语音');await click('开始录音');await click('结束录音');await click('确认发送');
 await act(async()=>root.render(createElement(VoicePanel,{...props,runs:[{id:'run',status:'completed'}],messages:[{id:'reply',run_id:'run',role:'assistant',status:'completed'}]})));
 assert.match(document.body.textContent,/浏览器阻止自动播放/);assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='继续播报'));
 await click('继续播报');assert.equal(writes.filter(r=>r.u.includes('/audio/')).length,1);await click('停止播报');
 assert.ok(pauses>0);assert.ok(writes.some(r=>r.u.endsWith('/progress')&&JSON.parse(r.o.body).stop));assert.ok(!writes.some(r=>r.u.endsWith('/cancel')));
});

async function renderCompletedReply(root,props){
 await act(async()=>root.render(createElement(VoicePanel,{...props,runs:[{id:'run',status:'completed'}],messages:[{id:'reply',run_id:'run',role:'assistant',status:'completed'}]})));
}

test('unverified realtime output keeps the ordinary TTS path',async t=>{
 const writes=[],props={conversationId:'conversation',messages:[],runs:[],reload:async()=>{}};
 const plan={id:'playback',message_id:'reply',parts:['第一句。'],confirmed_parts:0,status:'active'};
 const prior=URL.createObjectURL;URL.createObjectURL=()=> 'blob:ordinary';t.after(()=>URL.createObjectURL=prior);
 const root=await mount(t,VoicePanel,props,(u,o)=>{
  writes.push({u,o});
  if(u.endsWith('/voice/config'))return response({...config,realtime_tts:{provider_id:'p',model:'live'},checks:{asr:true,tts:true,realtime_tts:false}});
  if(u.endsWith('/audio/0'))return new Response(new Blob(['wav']));
  if(u.endsWith('/messages'))return response({id:'run'});
  if(u.endsWith('/transcriptions'))return response({text:'请回答'});
  return response(u.endsWith('/playbacks')?plan:session);
 });
 document.querySelector('audio').play=async()=>{};
 await click('开启语音');await click('开始录音');await click('结束录音');await click('确认发送');await renderCompletedReply(root,props);
 assert.ok(writes.some(r=>r.u.endsWith('/audio/0')));
 assert.ok(!writes.some(r=>r.u.includes('/native-audio/')));
});

test('verified realtime output uses native audio path',async t=>{
 const writes=[],props={conversationId:'conversation',messages:[],runs:[],reload:async()=>{}};
 const plan={id:'playback',message_id:'reply',parts:['第一句。'],confirmed_parts:0,status:'active'};
 const prior=URL.createObjectURL;URL.createObjectURL=()=> 'blob:native';t.after(()=>URL.createObjectURL=prior);
 const root=await mount(t,VoicePanel,props,(u,o)=>{
  writes.push({u,o});
  if(u.endsWith('/voice/config'))return response({...config,realtime_tts:{provider_id:'p',model:'live'},checks:{asr:true,tts:true,realtime_tts:true}});
  if(u.endsWith('/native-audio/0'))return new Response(new Blob(['wav']));
  if(u.endsWith('/audio/0'))throw new Error('ordinary TTS must not be used');
  if(u.endsWith('/messages'))return response({id:'run'});
  if(u.endsWith('/transcriptions'))return response({text:'请回答'});
  return response(u.endsWith('/playbacks')?plan:session);
 });
 document.querySelector('audio').play=async()=>{};
 await click('开启语音');await click('开始录音');await click('结束录音');await click('确认发送');await renderCompletedReply(root,props);
 assert.ok(writes.some(r=>r.u.endsWith('/native-audio/0')));
 assert.ok(!writes.some(r=>r.u.endsWith('/audio/0')));
});

test('native output failure stays visible and never falls back to ordinary TTS',async t=>{
 const writes=[],props={conversationId:'conversation',messages:[],runs:[],reload:async()=>{}};
 const plan={id:'playback',message_id:'reply',parts:['第一句。'],confirmed_parts:0,status:'active'};
 const prior=URL.createObjectURL;URL.createObjectURL=()=> 'blob:native-failure';t.after(()=>URL.createObjectURL=prior);
 const root=await mount(t,VoicePanel,props,(u,o)=>{
  writes.push({u,o});
  if(u.endsWith('/voice/config'))return response({...config,realtime_tts:{provider_id:'p',model:'live'},checks:{asr:true,tts:true,realtime_tts:true}});
  if(u.endsWith('/native-audio/0'))return response({detail:'字幕不一致'},409);
  if(u.endsWith('/audio/0'))throw new Error('ordinary fallback is forbidden');
  if(u.endsWith('/messages'))return response({id:'run'});
  if(u.endsWith('/transcriptions'))return response({text:'请回答'});
  return response(u.endsWith('/playbacks')?plan:session);
 });
 document.querySelector('audio').play=async()=>{};
 await click('开启语音');await click('开始录音');await click('结束录音');await click('确认发送');await renderCompletedReply(root,props);
 assert.ok(writes.some(r=>r.u.endsWith('/native-audio/0')));
 assert.ok(!writes.some(r=>r.u.endsWith('/audio/0')));
 assert.match(document.body.textContent,/字幕不一致/);
});


const continuousSession={...session,mode:'continuous',automatic_turns:0};
const voiceProps={conversationId:'conversation',messages:[],runs:[],reload:async()=>{}};
async function enableContinuous(){
 const checkbox=[...document.querySelectorAll('input[type=checkbox]')].find(e=>e.closest('label').textContent==='连续对话');
 assert.ok(checkbox);await act(async()=>checkbox.click());await click('开启语音');
}

test('continuous mode is opt-in and opening still does not open microphone',async t=>{
 const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return response(continuousSession);});
 assert.equal(document.querySelector('input[type=checkbox]').checked,false);
 await enableContinuous();assert.equal(micStarts,0);assert.equal(JSON.parse(writes[0].o.body).mode,'continuous');
 assert.match(document.body.textContent,/不自动保存记忆/);assert.match(document.body.textContent,/回声/);await click('开始聆听');assert.equal(micStarts,1);
 await click('暂停聆听');assert.ok(micDiscards>0);assert.equal(writes.filter(w=>w.u.endsWith('/messages')).length,0);
});

test('one segmented utterance automatically sends unconfirmed input without tools or memory calls',async t=>{
 const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return response(u.endsWith('/transcriptions')?{text:'我喜欢茶'}:u.endsWith('/messages')?{id:'auto-run'}:continuousSession);});
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 const sent=writes.filter(w=>w.u.endsWith('/messages'));assert.equal(sent.length,1);const body=JSON.parse(sent[0].o.body);
 assert.equal(body.automatic,true);assert.equal(body.content,'我喜欢茶');assert.ok(!body.task_assist);assert.ok(!writes.some(w=>w.u.includes('memories')));
 assert.ok(detectionModes.includes('paused'));assert.equal(document.querySelector('textarea'),null);
});

test('recognition error pauses and does not retry paid requests',async t=>{
 const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push(u);return u.endsWith('/transcriptions')?response({detail:'转写服务限流'},429):response(continuousSession);});
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 assert.match(document.querySelector('[role=alert]').textContent,/限流/);assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='开始聆听'));
 assert.equal(writes.filter(u=>u.endsWith('/transcriptions')).length,1);assert.ok(!writes.some(u=>u.endsWith('/messages')));
});

test('late recognition after pause cannot submit a message',async t=>{
 let finish;const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push(u);return u.endsWith('/transcriptions')?new Promise(resolve=>finish=resolve):response(continuousSession);});
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));await click('暂停聆听');
 await act(async()=>finish(response({text:'迟到的转写'})));assert.ok(!writes.some(u=>u.endsWith('/messages')));assert.equal(document.querySelector('textarea'),null);
});

test('ambiguous send failure preserves automatic flag and id on explicit retry',async t=>{
 const bodies=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{if(u.endsWith('/messages')){bodies.push(JSON.parse(o.body));return response({detail:'连接中断，请核对状态'},503);}return response(u.endsWith('/transcriptions')?{text:'保留转写'}:continuousSession);});
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 assert.equal(document.querySelector('textarea').disabled,true);await click('重试发送');assert.deepEqual(bodies[0],bodies[1]);assert.equal(bodies[0].automatic,true);
 assert.equal(micStarts,1);assert.match(document.body.textContent,/不能改变原转写/);
});

for(const command of ['暂停聆听','先别聊。','结束语音'])test('local voice stop command has no preference/tool side effects: '+command,async t=>{
 const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return response(u.endsWith('/transcriptions')?{text:command}:continuousSession);});
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 assert.ok(!writes.some(w=>w.u.endsWith('/messages')||w.u.includes('preferences')||w.u.includes('/cancel')));
 if(command==='结束语音')assert.ok(writes.some(w=>w.o.method==='DELETE'));else assert.match(document.body.textContent,/未修改长期联系偏好/);
});

test('microphone opening after session ends is discarded and never resurrected',async t=>{
 await mount(t,VoicePanel,voiceProps,()=>response(continuousSession));await enableContinuous();let finish;globalThis.listenGate=new Promise(r=>finish=r);
 await click('开始聆听');await click('结束语音');await act(async()=>finish());assert.ok(micDiscards>0);assert.ok(![...document.querySelectorAll('button')].some(b=>b.textContent==='暂停聆听'));
});

test('continuous reply playback resumes listening after conservative receipt',async t=>{
 const writes=[],plan={id:'play',message_id:'auto-reply',parts:['第一句。'],confirmed_parts:0,status:'active'};
 const oldCreate=URL.createObjectURL;URL.createObjectURL=()=> 'blob:continuous';t.after(()=>URL.createObjectURL=oldCreate);
 const root=await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return u.includes('/audio/')?new Response(new Blob(['wav'])):response(u.endsWith('/transcriptions')?{text:'请回答'}:u.endsWith('/messages')?{id:'auto-run'}:u.endsWith('/playbacks')?plan:u.endsWith('/progress')?{...plan,confirmed_parts:JSON.parse(o.body).completed_parts}:continuousSession);});
 const player=document.querySelector('audio');let pauses=0;player.play=async()=>{};player.pause=()=>pauses++;
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 await act(async()=>root.render(createElement(VoicePanel,{...voiceProps,runs:[{id:'auto-run',status:'completed'}],messages:[{id:'auto-reply',run_id:'auto-run',role:'assistant',status:'completed'}]})));
 assert.equal(detectionModes.at(-1),'playback');await act(async()=>player.onended());assert.equal(detectionModes.at(-1),'listening');assert.match(document.body.textContent,/继续聆听/);
 assert.ok(writes.some(w=>w.u.endsWith('/progress')&&JSON.parse(w.o.body).completed_parts===1));
});

test('continuous autoplay rejection closes microphone and requires manual resume',async t=>{
 const writes=[],plan={id:'play',message_id:'auto-reply',parts:['回答。'],confirmed_parts:0,status:'active'};
 const oldCreate=URL.createObjectURL;URL.createObjectURL=()=> 'blob:blocked';t.after(()=>URL.createObjectURL=oldCreate);
 const root=await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push(u);return u.includes('/audio/')?new Response(new Blob(['wav'])):response(u.endsWith('/transcriptions')?{text:'请回答'}:u.endsWith('/messages')?{id:'auto-run'}:u.endsWith('/playbacks')?plan:continuousSession);});
 const player=document.querySelector('audio');player.play=async()=>{throw Error('autoplay');};player.pause=()=>{};
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 await act(async()=>root.render(createElement(VoicePanel,{...voiceProps,runs:[{id:'auto-run',status:'completed'}],messages:[{id:'auto-reply',run_id:'auto-run',role:'assistant',status:'completed'}]})));
 assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='开始聆听'));assert.ok(micDiscards>0);assert.equal(writes.filter(u=>u.includes('/audio/')).length,1);
});


test('barge-in stops playback before slow server receipt, without crediting unheard words',async t=>{
 const writes=[],plan={id:'play',message_id:'auto-reply',parts:['未听完的句子。'],confirmed_parts:0,status:'active'};
 const oldCreate=URL.createObjectURL;URL.createObjectURL=()=> 'blob:barge';t.after(()=>URL.createObjectURL=oldCreate);
 let receipt;
 const root=await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});if(u.endsWith('/progress'))return new Promise(resolve=>receipt=resolve);return u.includes('/audio/')?new Response(new Blob(['wav'])):response(u.endsWith('/transcriptions')?{text:'请回答'}:u.endsWith('/messages')?{id:'auto-run'}:u.endsWith('/playbacks')?plan:continuousSession);});
 const player=document.querySelector('audio');let pauses=0;player.play=async()=>{};player.pause=()=>pauses++;
 await enableContinuous();await click('开始聆听');await act(async()=>listeningCallbacks.utterance(new Blob(['pcm'])));
 await act(async()=>root.render(createElement(VoicePanel,{...voiceProps,runs:[{id:'auto-run',status:'completed'}],messages:[{id:'auto-reply',run_id:'auto-run',role:'assistant',status:'completed'}]})));
 const before=pauses;await act(async()=>listeningCallbacks.start());assert.ok(pauses>before);assert.equal(detectionModes.at(-1),'listening');
 const ack=JSON.parse(writes.find(w=>w.u.endsWith('/progress')).o.body);assert.deepEqual(ack,{completed_parts:0,stop:true});assert.ok(!writes.some(w=>w.u.endsWith('/cancel')));
 await act(async()=>receipt(response({...plan,status:'stopped'})));
});

function proactiveClock(t){
 let instant=Date.now(),tick;
 const interval=globalThis.setInterval,clock=Date.now,create=URL.createObjectURL;
 globalThis.setInterval=(fn,ms,...args)=>{if(ms===1000)tick=fn;return interval(fn,60000,...args);};
 Date.now=()=>instant;URL.createObjectURL=()=> 'blob:proactive';
 t.after(()=>{globalThis.setInterval=interval;Date.now=clock;URL.createObjectURL=create;});
 return async()=>{instant+=4000;assert.ok(tick,'proactive poll installed');await act(async()=>{await tick();});};
}
async function proactiveSwitch(){
 const input=[...document.querySelectorAll('input[type=checkbox]')].find(e=>e.closest('label').textContent==='主动开口');
 assert.ok(input);await act(async()=>input.click());
}
const proactiveSession={...continuousSession,allow_proactive:true};
const proactivePlan={id:'contact-play',message_id:'contact-message',parts:['接着昨天的项目。','这是建议。'],confirmed_parts:0,status:'active',proactive:true,contact_id:'contact'};

test('proactive speech is off by default and never opens microphone',async t=>{
 const writes=[];await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return response(session);});
 const input=[...document.querySelectorAll('input')].find(e=>e.closest('label')?.textContent==='主动开口');
 assert.equal(input.checked,false);await click('开启语音');assert.equal(JSON.parse(writes[0].o.body).allow_proactive,false);assert.equal(micStarts,0);
});

test('consented fresh topic speaks once and credits only ended sentences',async t=>{
 const tick=proactiveClock(t),writes=[];
 await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return u.includes('/audio/')?new Response(new Blob(['wav'])):response(u.endsWith('/proactive-playback')?{playback:proactivePlan}:u.endsWith('/progress')?{...proactivePlan,confirmed_parts:JSON.parse(o.body).completed_parts}:proactiveSession);});
 const player=document.querySelector('audio');let plays=0;player.play=async()=>{plays++;};player.pause=()=>{};
 await proactiveSwitch();await click('开启语音');await tick();assert.equal(plays,1);assert.equal(micStarts,0);
 assert.ok(!writes.some(w=>w.u.endsWith('/progress')));await act(async()=>player.onended());
 assert.equal(plays,2);await click('停止播报');
 const receipts=writes.filter(w=>w.u.endsWith('/progress')).map(w=>JSON.parse(w.o.body));
 assert.deepEqual(receipts,[{completed_parts:1,stop:false},{completed_parts:1,stop:true}]);
 assert.equal(writes.filter(w=>w.u.endsWith('/proactive-playback')).length,1);
});

for(const activity of ['speech','switch','text'])test('late proactive claim cannot speak after '+activity,async t=>{
 const tick=proactiveClock(t),writes=[];let finish;
 const root=await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return u.endsWith('/proactive-playback')?new Promise(resolve=>finish=resolve):response(u.endsWith('/proactive')?{enabled:false}:proactiveSession);});
 await proactiveSwitch();await enableContinuous();await click('开始聆听');
 // Keep the request in flight while a new user action invalidates its timing.
 await tick();assert.ok(finish);
 if(activity==='speech')await act(async()=>listeningCallbacks.start());
 if(activity==='switch')await proactiveSwitch();
 if(activity==='text')await act(async()=>root.render(createElement(VoicePanel,{...voiceProps,userBusy:true})));
 await act(async()=>finish(response({playback:proactivePlan})));
 assert.ok(!writes.some(w=>w.u.includes('/audio/')));
 assert.ok(writes.some(w=>w.u.endsWith('/progress')&&JSON.parse(w.o.body).stop));
});

test('busy user defers contact without claiming or synthesizing',async t=>{
 const tick=proactiveClock(t),writes=[];
 await mount(t,VoicePanel,{...voiceProps,userBusy:true},(u,o)=>{writes.push(u);return response(proactiveSession);});
 await proactiveSwitch();await click('开启语音');await tick();assert.ok(!writes.some(u=>u.endsWith('/proactive-playback')||u.includes('/audio/')));
});

test('source refusal stops active speech on status check and never credits unfinished part',async t=>{
 const tick=proactiveClock(t),writes=[];
 await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});return u.includes('/audio/')?new Response(new Blob(['wav'])):u==='/api/voice/playbacks/contact-play'?response({detail:'用户已拒绝此话题'},409):response(u.endsWith('/proactive-playback')?{playback:proactivePlan}:proactiveSession);});
 const player=document.querySelector('audio');let pauses=0;player.play=async()=>{};player.pause=()=>pauses++;
 await proactiveSwitch();await click('开启语音');await tick();const before=pauses;await tick();
 assert.ok(pauses>before);assert.match(document.body.textContent,/拒绝/);
 assert.deepEqual(JSON.parse(writes.find(w=>w.u.endsWith('/progress')).o.body),{completed_parts:0,stop:true});
});

test('manual resume rechecks rejected contact without replaying buffered audio',async t=>{
 const tick=proactiveClock(t),writes=[];
 await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push(u);return u.includes('/audio/')?new Response(new Blob(['wav'])):u==='/api/voice/playbacks/contact-play'?response({detail:'来源已删除'},409):response(u.endsWith('/proactive-playback')?{playback:proactivePlan}:proactiveSession);});
 const player=document.querySelector('audio');let plays=0;player.play=async()=>{plays++;throw Error('autoplay');};player.pause=()=>{};
 await proactiveSwitch();await click('开启语音');await tick();assert.equal(plays,1);await click('继续播报');
 assert.equal(plays,1);assert.match(document.body.textContent,/来源已删除/);assert.equal(writes.filter(u=>u.includes('/audio/')).length,1);
});

test('stale message-page snapshot does not cancel a fresh proactive contact',async t=>{
 const tick=proactiveClock(t),writes=[];
 const root=await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push(u);return u.includes('/audio/')?new Response(new Blob(['wav'])):response(u.endsWith('/proactive-playback')?{playback:proactivePlan}:proactiveSession);});
 const player=document.querySelector('audio');player.play=async()=>{};player.pause=()=>{};
 await proactiveSwitch();await click('开启语音');await tick();
 await act(async()=>root.render(createElement(VoicePanel,{...voiceProps,messages:[]})));
 assert.ok(!writes.some(u=>u.endsWith('/progress')));assert.ok(!document.querySelector('[role=alert]'));
});

for(const waitAt of ['audio','output-device'])test('barge-in discards late proactive '+waitAt+' completion before play',async t=>{
 const tick=proactiveClock(t),writes=[];let finish;
 await mount(t,VoicePanel,voiceProps,(u,o)=>{writes.push({u,o});if(u.includes('/audio/'))return waitAt==='audio'?new Promise(resolve=>finish=()=>resolve(new Response(new Blob(['wav'])))):new Response(new Blob(['wav']));return response(u.endsWith('/proactive-playback')?{playback:proactivePlan}:proactiveSession);});
 const player=document.querySelector('audio');let plays=0;player.play=async()=>{plays++;};player.pause=()=>{};
 if(waitAt==='output-device')player.setSinkId=()=>new Promise(resolve=>finish=resolve);
 await proactiveSwitch();await enableContinuous();await click('开始聆听');await tick();assert.ok(finish);
 await act(async()=>listeningCallbacks.start());await act(async()=>finish());assert.equal(plays,0);
 const receipt=writes.find(w=>w.u.endsWith('/progress'));assert.deepEqual(JSON.parse(receipt.o.body),{completed_parts:0,stop:true});
});
