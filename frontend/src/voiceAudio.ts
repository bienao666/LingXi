/** Ephemeral microphone capture. Only the final PCM WAV is sent to Core. */
export function pcmWav(chunks: Float32Array[], sampleRate: number): Blob {
  const size=chunks.reduce((n,c)=>n+c.length,0);
  if(!Number.isFinite(sampleRate)||sampleRate<8000||sampleRate>96000||size<sampleRate*.1||size>sampleRate*60)throw new Error('录音时长须为 0.1–60 秒，请重新录制');
  const input=new Float32Array(size);let offset=0;
  for(const chunk of chunks){input.set(chunk,offset);offset+=chunk.length;}
  const rate=16000,length=Math.floor(size*rate/sampleRate),buffer=new ArrayBuffer(44+length*2),data=new DataView(buffer);
  const ascii=(at:number,text:string)=>{for(let i=0;i<text.length;i++)data.setUint8(at+i,text.charCodeAt(i));};
  ascii(0,'RIFF');data.setUint32(4,36+length*2,true);ascii(8,'WAVE');ascii(12,'fmt ');data.setUint32(16,16,true);data.setUint16(20,1,true);data.setUint16(22,1,true);data.setUint32(24,rate,true);data.setUint32(28,rate*2,true);data.setUint16(32,2,true);data.setUint16(34,16,true);ascii(36,'data');data.setUint32(40,length*2,true);
  for(let i=0;i<length;i++){const at=i*sampleRate/rate,a=Math.floor(at),fraction=at-a;const value=Math.max(-1,Math.min(1,input[a]*(1-fraction)+(input[Math.min(a+1,size-1)]||0)*fraction));data.setInt16(44+i*2,value<0?value*32768:value*32767,true);}
  return new Blob([buffer],{type:'audio/wav'});
}

export type DetectionMode='listening'|'playback'|'paused';
export type ListeningCallbacks={start():void;utterance(blob:Blob):void;error(message:string):void};

/** Local amplitude gate, not a speech/speaker/emotion classifier. No network. */
export class SpeechSegmenter {
  private pre:Float32Array[]=[];
  private frames:Float32Array[]=[];
  private preSize=0;
  private onset=0;
  private voiced=0;
  private quiet=0;
  private size=0;
  private active=false;
  private mode:DetectionMode='listening';
  constructor(private rate:number,private callbacks:ListeningCallbacks){
    if(!Number.isFinite(rate)||rate<8000||rate>96000)throw new Error('输入采样率不受支持');
  }
  reset(){this.pre=[];this.frames=[];this.preSize=0;this.onset=0;this.voiced=0;this.quiet=0;this.size=0;this.active=false;}
  setMode(mode:DetectionMode){if(mode==='paused'||this.mode==='paused')this.reset();this.mode=mode;}
  push(frame:Float32Array){
    if(this.mode==='paused'||!frame.length)return;
    if(frame.length>this.rate||frame.some(v=>!Number.isFinite(v))){this.reset();this.mode='paused';this.callbacks.error('音频输入异常，已暂停聆听');return;}
    const chunk=frame.slice(),seconds=chunk.length/this.rate;
    const rms=Math.sqrt(chunk.reduce((sum,v)=>sum+v*v,0)/chunk.length);
    const loud=rms>=(this.mode==='playback'?.06:.018);
    if(!this.active){
      this.pre.push(chunk);this.preSize+=chunk.length;
      while(this.pre.length>1&&this.preSize-this.pre[0].length>=this.rate*.3)this.preSize-=this.pre.shift()!.length;
      this.onset=loud?this.onset+seconds:0;
      if(this.onset<(this.mode==='playback'?.25:.18))return;
      this.active=true;this.frames=this.pre;this.size=this.preSize;this.pre=[];this.preSize=0;this.voiced=this.onset;this.quiet=0;
      this.callbacks.start();return;
    }
    this.frames.push(chunk);this.size+=chunk.length;
    this.voiced+=loud?seconds:0;this.quiet=loud?0:this.quiet+seconds;
    if(this.size>=this.rate*25){this.reset();this.mode='paused';this.callbacks.error('单段超过 25 秒，已丢弃并暂停；请分段表达');return;}
    if(this.quiet>=1.2){
      const frames=this.frames,valid=this.voiced>=.45;this.reset();
      if(valid){this.mode='paused';this.callbacks.utterance(pcmWav(frames,this.rate));}
    }
  }
}

export class Microphone {
  private stream?: MediaStream;
  private context?: AudioContext;
  private node?: AudioWorkletNode;
  private chunks: Float32Array[]=[];
  private generation=0;
  private timer?: ReturnType<typeof setTimeout>;
  private count=0;
  private detector?:SpeechSegmenter;
  private listener?:ListeningCallbacks;
  private idle?:ReturnType<typeof setTimeout>;
  async start(device: string, limit:()=>void) {
    this.discard();
    const generation=++this.generation;
    if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||!window.AudioContext||!window.AudioWorkletNode)throw new Error('当前浏览器无法录音，请使用 HTTPS 或 localhost，并允许麦克风权限');
    let stream:MediaStream;
    try{stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,...(device?{deviceId:{exact:device}}:{})}});}catch{throw new Error('无法开启麦克风，请检查浏览器权限和输入设备');}
    if(generation!==this.generation){stream.getTracks().forEach(t=>t.stop());throw new Error('录音已取消');}
    this.stream=stream;
    try{
      const context=new AudioContext();this.context=context;
      await context.audioWorklet.addModule('/assets/voice-capture.js');
      if(generation!==this.generation)throw new Error('录音已取消');
      this.chunks=[];this.count=0;
      const node=new AudioWorkletNode(context,'lingxi-capture');this.node=node;
      node.port.onmessage=e=>{if(generation===this.generation&&e.data instanceof Float32Array&&this.count<context.sampleRate*60){this.chunks.push(e.data);this.count+=e.data.length;}};
      const mute=context.createGain();mute.gain.value=0;context.createMediaStreamSource(stream).connect(node);node.connect(mute);mute.connect(context.destination);await context.resume();
      if(generation!==this.generation)throw new Error('录音已取消');
      this.timer=setTimeout(limit,58000);
      return generation;
    }catch(e){if(generation===this.generation)this.discard();throw new Error((e as Error).message==='录音已取消'?'录音已取消':'录音组件加载失败，请刷新后重试或使用文字输入');}
  }
  async finish():Promise<Blob>{
    if(!this.node||!this.context)throw new Error('麦克风尚未准备好');
    clearTimeout(this.timer);
    const context=this.context,node=this.node;
    // Flush the worklet's final partial block before disconnecting.
    await new Promise<void>(resolve=>{const timeout=setTimeout(resolve,120);const old=node.port.onmessage;node.port.onmessage=e=>{old?.call(node.port,e);clearTimeout(timeout);resolve();};node.port.postMessage('flush');});
    const chunks=this.chunks.slice();this.discard();return pcmWav(chunks,context.sampleRate);
  }
  async listen(device:string,callbacks:ListeningCallbacks){
    const generation=await this.start(device,()=>{});
    if(generation!==this.generation||!this.context||!this.node)throw new Error('录音已取消');
    clearTimeout(this.timer);this.chunks=[];this.count=0;
    const context=this.context,node=this.node;
    const fail=(message:string)=>{if(generation!==this.generation)return;this.discard();callbacks.error(message);};
    this.listener={...callbacks,error:fail};
    this.detector=new SpeechSegmenter(context.sampleRate,this.listener);
    node.port.onmessage=e=>{if(generation===this.generation&&e.data instanceof Float32Array)this.detector?.push(e.data);};
    node.onprocessorerror=()=>fail('录音组件中断，已关闭麦克风');
    for(const track of this.stream!.getTracks())track.addEventListener('ended',()=>fail('麦克风已断开，请检查输入设备'));
    this.setDetection('listening');
  }
  async streamPCM(device:string,chunk:(data:ArrayBuffer)=>void,limit:()=>void,error:(message:string)=>void){
    const generation=await this.start(device,limit);
    if(generation!==this.generation||!this.context||!this.node)throw new Error('录音已取消');
    this.chunks=[];this.count=0;
    const encoder=new Pcm24Encoder(this.context.sampleRate);
    this.node.port.onmessage=e=>{if(generation===this.generation&&e.data instanceof Float32Array){try{for(const part of encoder.push(e.data))chunk(part);}catch{this.discard();error('实时音频发送失败，本段已丢弃');}}};
    this.node.onprocessorerror=()=>{this.discard();error('录音组件中断，本段已丢弃');};
    for(const track of this.stream!.getTracks())track.addEventListener('ended',()=>{if(generation===this.generation){this.discard();error('麦克风已断开，本段已丢弃');}});
  }
  async finishPCM(){
    const node=this.node;if(!node)throw new Error('麦克风尚未准备好');
    clearTimeout(this.timer);
    await new Promise<void>(resolve=>{const timeout=setTimeout(resolve,120);const old=node.port.onmessage;node.port.onmessage=e=>{old?.call(node.port,e);clearTimeout(timeout);resolve();};node.port.postMessage('flush');});
    this.discard();
  }
  setDetection(mode:DetectionMode){
    this.detector?.setMode(mode);clearTimeout(this.idle);
    if(this.detector&&mode!=='paused')this.idle=setTimeout(()=>this.listener?.error('60 秒没有有效对话，已关闭麦克风；可手动恢复'),60000);
  }
  discard(){++this.generation;clearTimeout(this.timer);clearTimeout(this.idle);this.detector?.reset();this.detector=undefined;this.listener=undefined;this.stream?.getTracks().forEach(t=>t.stop());this.node?.disconnect();if(this.context)void this.context.close().catch(()=>{});this.stream=undefined;this.node=undefined;this.context=undefined;this.chunks=[];this.count=0;}
}

/** Stateful resampling: retain fractional phase across worklet block boundaries. */
export class Pcm24Encoder {
  private last:Float32Array=new Float32Array();
  private consumed=0;
  private emitted=0;
  constructor(private rate:number){if(!Number.isFinite(rate)||rate<8000||rate>96000)throw new Error('输入采样率不受支持');}
  push(frame:Float32Array):ArrayBuffer[]{
    if(frame.length>this.rate||frame.some(v=>!Number.isFinite(v)))throw new Error('音频输入异常');
    if(!frame.length)return [];
    const input=new Float32Array(this.last.length+frame.length);input.set(this.last);input.set(frame,this.last.length);
    const values:number[]=[],base=this.consumed-this.last.length,limit=base+input.length-1;
    while(this.emitted*this.rate<limit*24000){const cursor=this.emitted*this.rate/24000-base,at=Math.floor(cursor),mix=cursor-at,v=Math.max(-1,Math.min(1,input[at]*(1-mix)+input[at+1]*mix));values.push(v<0?Math.round(v*32768):Math.round(v*32767));this.emitted++;}
    this.consumed+=frame.length;this.last=input.slice(-1);
    const parts:ArrayBuffer[]=[];
    for(let start=0;start<values.length;start+=4800){const size=Math.min(4800,values.length-start),part=new ArrayBuffer(size*2),view=new DataView(part);for(let i=0;i<size;i++)view.setInt16(i*2,values[start+i],true);parts.push(part);}
    return parts;
  }
}

export async function audioDevices(){
  if(!navigator.mediaDevices?.enumerateDevices)return [];
  return navigator.mediaDevices.enumerateDevices();
}
