import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const source=readFileSync(new URL('../src/voiceAudio.ts',import.meta.url),'utf8');
const {SpeechSegmenter,Microphone}=await import('data:text/javascript;base64,'+Buffer.from(ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText).toString('base64'));

function signal(rate=16000){
 const events=[];const detector=new SpeechSegmenter(rate,{start:()=>events.push('start'),utterance:b=>events.push(b),error:e=>events.push(e)});
 function push(seconds,volume=0){for(let i=0;i<Math.round(seconds/.02);i++)detector.push(new Float32Array(rate*.02).fill(volume));}
 return {detector,events,push};
}

test('silence and brief noise never create a paid utterance',()=>{
 const {push,events}=signal();push(70);push(.1,.1);push(2);assert.deepEqual(events,[]);
});
test('pause segments once with a bounded preroll and no repeated silence',async()=>{
 const {push,events}=signal();push(5);push(.7,.04);push(1);assert.deepEqual(events,['start']);push(.3);
 assert.equal(events.length,2);const wav=events[1];assert.ok(wav instanceof Blob);assert.ok(wav.size>16000&&wav.size<16000*2*3+44);
 push(5);push(1,.1);assert.equal(events.length,2);
});
test('short post-onset noise is discarded instead of transcribed',()=>{
 const {push,events}=signal();push(.22,.04);push(2);assert.deepEqual(events,['start']);push(.7,.04);push(1.3);assert.equal(events.length,3);
});
test('continuous sound over budget discards all audio and pauses',()=>{
 const {push,events}=signal();push(26,.04);assert.equal(events[0],'start');assert.match(events[1],/25 秒/);assert.ok(!events.some(e=>e instanceof Blob));push(2);push(1,.04);assert.equal(events.length,2);
});
test('playback uses stricter threshold and persistent onset',()=>{
 const {detector,push,events}=signal();detector.setMode('playback');push(2,.03);push(.2,.08);push(2);assert.deepEqual(events,[]);
 push(.7,.08);push(1.3);assert.equal(events[0],'start');assert.ok(events[1] instanceof Blob);
});
test('switching from playback on onset retains the utterance head',()=>{
 const events=[];let detector;detector=new SpeechSegmenter(16000,{start:()=>{events.push('start');detector.setMode('listening');},utterance:b=>events.push(b),error:e=>events.push(e)});
 detector.setMode('playback');for(let i=0;i<35;i++)detector.push(new Float32Array(320).fill(.1));for(let i=0;i<65;i++)detector.push(new Float32Array(320));
 assert.equal(events.length,2);assert.ok(events[1] instanceof Blob);
});
test('pausing or resetting drops partial speech without creating a transcript',()=>{
 const {detector,push,events}=signal();push(.7,.04);detector.setMode('paused');push(2);assert.deepEqual(events,['start']);detector.setMode('listening');push(2);assert.deepEqual(events,['start']);
});
test('invalid PCM and unreasonable chunks stop detection',()=>{
 for(const frame of [new Float32Array([NaN]),new Float32Array([Infinity]),new Float32Array(16001)]){
  const {detector,events}=signal();detector.push(frame);assert.match(events[0],/音频输入异常/);
 }
 assert.throws(()=>new SpeechSegmenter(1,{}),/采样率/);
});
test('48 kHz capture gives the same segmentation and 16 kHz WAV output',async()=>{
 const {push,events}=signal(48000);push(.7,.04);push(1.3);const v=new DataView(await events[1].arrayBuffer());assert.equal(v.getUint32(24,true),16000);
});
test('worklet sends 1024-frame blocks, flushes only remaining input, no duplication',()=>{
 let Capture;const sent=[];const context={AudioWorkletProcessor:class{port={postMessage:v=>sent.push(v)};},Float32Array,registerProcessor:(name,type)=>{assert.equal(name,'lingxi-capture');Capture=type;}};
 vm.runInNewContext(readFileSync(new URL('../assets/voice-capture.js',import.meta.url),'utf8'),context);
 const capture=new Capture();for(let i=0;i<9;i++)capture.process([[new Float32Array(128).fill(i/10)]]);
 assert.equal(sent.length,1);assert.equal(sent[0].length,1024);capture.port.onmessage();assert.equal(sent.length,2);assert.equal(sent[1].length,128);capture.port.onmessage();assert.equal(sent.length,2);
});

function microphoneEnvironment(t,{permission,resume}={}){
 const saved={};for(const key of ['window','navigator','AudioContext','AudioWorkletNode'])saved[key]=Object.getOwnPropertyDescriptor(globalThis,key);
 const tracks=[{stopped:false,stop(){this.stopped=true;},addEventListener(){}}];let node,context;
 class Context{sampleRate=16000;audioWorklet={addModule:async()=>{}};destination={};constructor(){context=this;}createGain(){return {gain:{value:1},connect(){}};}createMediaStreamSource(){return {connect(){}};}async resume(){if(resume)await resume;}async close(){this.closed=true;}}
 class Node{constructor(){node=this;}port={onmessage:null,postMessage:()=>{}};connect(){}disconnect(){this.disconnected=true;}}
 const env={window:{isSecureContext:true,AudioContext:Context,AudioWorkletNode:Node},navigator:{mediaDevices:{getUserMedia:async()=>{if(permission)await permission;return {getTracks:()=>tracks};}}},AudioContext:Context,AudioWorkletNode:Node};
 for(const [key,value]of Object.entries(env))Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
 t.after(()=>{for(const [key,descriptor]of Object.entries(saved))if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];});
 return {tracks,get node(){return node;},get context(){return context;}};
}
test('microphone continuous stream pauses buffers, resumes explicitly, releases hardware',async t=>{
 const env=microphoneEnvironment(t),events=[],mic=new Microphone();t.after(()=>mic.discard());
 await mic.listen('',{start:()=>events.push('start'),utterance:b=>events.push(b),error:e=>events.push(e)});
 const push=(count,volume)=>{for(let i=0;i<count;i++)env.node.port.onmessage({data:new Float32Array(320).fill(volume)});};
 mic.setDetection('paused');push(100,.1);assert.equal(events.length,0);mic.setDetection('listening');push(35,.04);push(65,0);assert.equal(events.length,2);
 mic.discard();assert.equal(env.tracks[0].stopped,true);assert.equal(env.context.closed,true);assert.equal(env.node.disconnected,true);push(100,.1);assert.equal(events.length,2);
});
test('late microphone permission after cancellation stops tracks without capture',async t=>{
 let resolve;const env=microphoneEnvironment(t,{permission:new Promise(r=>resolve=r)}),mic=new Microphone();const pending=mic.listen('',{start(){},utterance(){},error(){}});mic.discard();resolve();await assert.rejects(pending,/已取消/);assert.equal(env.tracks[0].stopped,true);assert.equal(env.node,undefined);
});
test('cancellation while AudioContext resumes cannot resurrect listening',async t=>{
 let resolve;const env=microphoneEnvironment(t,{resume:new Promise(r=>resolve=r)}),mic=new Microphone();const pending=mic.listen('',{start(){},utterance(){},error(){}});
 await Promise.resolve();await Promise.resolve();mic.discard();resolve();await assert.rejects(pending,/已取消/);assert.equal(env.tracks[0].stopped,true);
});
test('idle microphone closes and requires explicit resume after 60 seconds',async t=>{
 t.mock.timers.enable({apis:['setTimeout']});const env=microphoneEnvironment(t),events=[],mic=new Microphone();t.after(()=>mic.discard());
 await mic.listen('',{start(){},utterance(){},error:e=>events.push(e)});t.mock.timers.tick(60000);assert.match(events[0],/60 秒/);assert.equal(env.tracks[0].stopped,true);assert.equal(events.length,1);
});
