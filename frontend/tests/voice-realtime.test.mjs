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
const window=new Window({url:'https://lingxi.test/#/settings/voice'});
Object.assign(globalThis,{window,document:window.document,location:window.location,Event:window.Event,HTMLElement:window.HTMLElement,IS_REACT_ACT_ENVIRONMENT:true});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const audio=url(`export class Microphone{discard(){globalThis.discards++;}async streamPCM(device,chunk,limit,error){globalThis.capture={device,chunk,limit,error};globalThis.starts++;}async finishPCM(){globalThis.finished++;}}`);
const {VoiceRealtime}=await import(compile('../src/VoiceRealtime.tsx',{'./api':api,'./ui':ui,'./voiceAudio':audio}));
const {Pcm24Encoder}=await import(compile('../src/voiceAudio.ts'));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
class Socket{
 static OPEN=1;
 readyState=0;bufferedAmount=0;sent=[];closed=0;
 constructor(url){this.url=String(url);globalThis.sockets.push(this);}
 send(data){this.sent.push(data);}
 close(){this.closed++;this.readyState=3;this.onclose?.();}
 open(){this.readyState=1;this.onopen?.();}
 emit(data){this.onmessage?.({data:JSON.stringify(data)});}
}
async function mount(t,props={},handler){document.body.innerHTML='<div id="root"></div>';Object.assign(globalThis,{starts:0,discards:0,finished:0,capture:null,sockets:[],WebSocket:Socket});const paths=[],finals=[],states=[];globalThis.fetch=async(u,o)=>{paths.push({u,o});return handler?handler(u,o):response({token:'single-use',expires_at:Date.now()/1000+30,path:'/api/voice/realtime'});};const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(VoiceRealtime,{sessionId:'session',onFinal:text=>finals.push(text),onBusy:value=>states.push(value),...props})));return {root,paths,finals,states};}
async function click(label){const button=[...document.querySelectorAll('button')].find(b=>b.textContent===label);assert.ok(button,label);await act(async()=>button.click());}
async function ready(){await act(async()=>{sockets[0].open();sockets[0].emit({type:'ready',sample_rate:24000});});}

test('native input uses same-origin ticket without secrets in URL and waits for explicit recording',async t=>{
 const {paths,finals}=await mount(t,{inputDevice:'mic-1'});assert.equal(paths.length,0);assert.equal(starts,0);
 await click('实时转写');assert.equal(starts,0);assert.equal(paths[0].u,'/api/voice/realtime-tickets');assert.deepEqual(JSON.parse(paths[0].o.body),{session_id:'session'});
 assert.equal(sockets[0].url,'wss://lingxi.test/api/voice/realtime');await ready();assert.equal(starts,1);assert.equal(capture.device,'mic-1');assert.deepEqual(JSON.parse(sockets[0].sent[0]),{token:'single-use'});
 capture.chunk(new ArrayBuffer(4800));assert.equal(sockets[0].sent[1].byteLength,4800);
 await act(async()=>sockets[0].emit({type:'partial',text:'还在修改'}));assert.match(document.body.textContent,/临时字幕.*还在修改/s);assert.deepEqual(finals,[]);
 await click('结束转写');assert.equal(finished,1);assert.equal(sockets[0].sent.at(-1),'{"type":"commit"}');
 await act(async()=>sockets[0].emit({type:'completed',text:'最终修订。'}));assert.deepEqual(finals,['最终修订。']);assert.equal(sockets[0].closed,1);assert.equal(paths.length,1);assert.ok(!document.body.textContent.includes('还在修改'));
});

test('cancel prevents late subtitles and final from submitting anything',async t=>{
 const {finals,paths}=await mount(t);await click('实时转写');await ready();await click('取消转写');
 await act(async()=>sockets[0].emit({type:'completed',text:'过期定稿'}));assert.deepEqual(finals,[]);assert.equal(paths.length,1);assert.ok(!document.body.textContent.includes('过期定稿'));assert.ok(discards>0);
});

test('cancel while ticket request pending never opens socket or microphone',async t=>{
 let finish;const {finals}=await mount(t,{},()=>new Promise(r=>finish=r));await click('实时转写');await click('取消转写');await act(async()=>finish(response({token:'late',path:'/api/voice/realtime'})));assert.equal(sockets.length,0);assert.equal(starts,0);assert.deepEqual(finals,[]);
});

test('server errors remain visible and do not auto retry paid requests',async t=>{
 const {finals,paths}=await mount(t);await click('实时转写');await ready();await act(async()=>sockets[0].emit({type:'error',message:'实时模型不兼容'}));assert.match(document.querySelector('[role=alert]').textContent,/实时模型不兼容/);assert.equal(paths.length,1);assert.deepEqual(finals,[]);assert.equal(sockets[0].closed,1);
});

test('socket authentication revocation uses the unified identity gate',async t=>{
 let count=0;const listener=()=>count++;window.addEventListener('lingxi:unauthorized',listener);t.after(()=>window.removeEventListener('lingxi:unauthorized',listener));
 const {finals}=await mount(t);await click('实时转写');await ready();await act(async()=>sockets[0].emit({type:'error',message:'登录已失效',status:401}));assert.equal(count,1);assert.deepEqual(finals,[]);assert.equal(sockets[0].closed,1);
});

test('malformed server data stops capture without accepting any transcript',async t=>{
 const {finals}=await mount(t);await click('实时转写');await ready();await act(async()=>sockets[0].emit(null));assert.match(document.querySelector('[role=alert]').textContent,/无效内容/);assert.deepEqual(finals,[]);assert.equal(sockets[0].closed,1);
});

test('unauthorized ticket never opens microphone and invokes authentication handling',async t=>{
 let unauthorized=0;const handler=()=>unauthorized++;window.addEventListener('lingxi:unauthorized',handler);t.after(()=>window.removeEventListener('lingxi:unauthorized',handler));
 await mount(t,{},()=>response({detail:'请先登录'},401));await click('实时转写');assert.equal(starts,0);assert.equal(sockets.length,0);assert.equal(unauthorized,1);assert.match(document.querySelector('[role=alert]').textContent,/请先登录/);
});

test('offline closes socket and ignores trailing completion',async t=>{
 const {finals}=await mount(t);await click('实时转写');await ready();await act(async()=>window.dispatchEvent(new window.Event('offline')));await act(async()=>sockets[0].emit({type:'completed',text:'迟到'}));assert.equal(sockets[0].closed,1);assert.deepEqual(finals,[]);assert.equal(document.querySelector('button').textContent,'实时转写');
});

test('unexpected socket close preserves error without automatically reopening',async t=>{
 const {paths}=await mount(t);await click('实时转写');await ready();await act(async()=>sockets[0].onclose());assert.match(document.querySelector('[role=alert]').textContent,/中断.*未提交/);assert.equal(paths.length,1);assert.equal(starts,1);
});

test('turn timeout commits existing audio, final received only once',async t=>{
 const {finals}=await mount(t);await click('实时转写');await ready();await act(async()=>capture.limit());assert.equal(finished,1);assert.equal(sockets[0].sent.at(-1),'{"type":"commit"}');await act(async()=>{sockets[0].emit({type:'completed',text:'定稿'});sockets[0].emit({type:'completed',text:'重复'});});assert.deepEqual(finals,['定稿']);
});

test('PCM24 resampling preserves phase across arbitrary worklet boundaries',()=>{
 for(const rate of [8000,16000,24000,44100,48000,96000]){
  const samples=Float32Array.from({length:rate},(_,i)=>Math.sin(i*.021)),single=new Pcm24Encoder(rate).push(samples);
  const encoder=new Pcm24Encoder(rate),parts=[];for(let i=0;i<samples.length;i+=257)parts.push(...encoder.push(samples.slice(i,i+257)));
  const bytes=parts.reduce((sum,v)=>sum+v.byteLength,0);assert.ok(bytes>=47990&&bytes<=48000,`${rate}: ${bytes}`);assert.ok(parts.every(p=>p.byteLength<=9600&&p.byteLength%2===0));
  const joined=Buffer.concat(parts.map(p=>Buffer.from(p))),whole=Buffer.concat(single.map(p=>Buffer.from(p)));assert.equal(joined.length,whole.length);for(let i=0;i<joined.length;i+=2)assert.ok(Math.abs(joined.readInt16LE(i)-whole.readInt16LE(i))<=1);
 }
});

test('PCM24 rejects invalid samples, bounds clipping and ignores empty frames',()=>{
 assert.throws(()=>new Pcm24Encoder(0));const encoder=new Pcm24Encoder(24000);assert.deepEqual(encoder.push(new Float32Array()),[]);assert.throws(()=>encoder.push(new Float32Array([NaN])));assert.throws(()=>encoder.push(new Float32Array(24001)));const [part]=encoder.push(new Float32Array([-2,2,0]));const data=new DataView(part);assert.equal(data.getInt16(0,true),-32768);assert.equal(data.getInt16(2,true),32767);
});
