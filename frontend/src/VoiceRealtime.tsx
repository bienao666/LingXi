import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {Microphone,Pcm24Encoder} from './voiceAudio';
import {ErrorNotice} from './ui';

type Callbacks={ready?():void;partial(text:string):void;final(text:string):void;error(message:string):void};

/** One bounded, cancellable turn shared by manual capture and continuous VAD. */
export class RealtimeTranscription {
  private socket:WebSocket|null=null;
  private controller=new AbortController();
  private closed=false;
  private ready=false;
  private ending=false;
  private committed=false;
  private queue:ArrayBuffer[]=[];
  private bytes=0;
  private encoder?:Pcm24Encoder;
  private drainTimer?:ReturnType<typeof setTimeout>;
  private timer?:ReturnType<typeof setTimeout>;
  constructor(private callbacks:Callbacks){}
  start(sessionId:string){
    this.timer=setTimeout(()=>this.fail('实时转写等待超时，请检查连接后手动重试'),74000);
    void api<components['schemas']['TicketView']>('/api/voice/realtime-tickets','POST',{session_id:sessionId},{signal:this.controller.signal}).then(ticket=>{
      if(this.closed)return;
      const url=new URL(ticket.path,location.href);
      if(url.origin!==location.origin||url.pathname!=='/api/voice/realtime'||url.search||url.hash)throw new Error('实时转写地址无效');
      url.protocol=location.protocol==='https:'?'wss:':'ws:';
      const ws=new WebSocket(url);this.socket=ws;
      ws.onopen=()=>{if(!this.closed)ws.send(JSON.stringify({token:ticket.token}));else ws.close();};
      ws.onerror=()=>this.fail('实时转写连接失败，请检查网络和服务配置；不会自动重试');
      ws.onclose=()=>this.fail('实时转写连接已中断，本段未提交；请手动重试');
      ws.onmessage=event=>this.receive(event.data);
    }).catch(e=>{if(!this.closed)this.fail(e.message);});
  }
  samples(frames:Float32Array[],rate:number){
    if(this.closed||this.ending)return;
    try{this.encoder??=new Pcm24Encoder(rate);for(const frame of frames)for(const part of this.encoder.push(frame))this.write(part);}
    catch{this.fail('实时音频格式无效，本段已丢弃');}
  }
  write(part:ArrayBuffer){
    if(this.closed||this.ending)return;
    if(!part.byteLength||part.byteLength>9600||part.byteLength%2||this.bytes+part.byteLength>60*48000){this.fail('实时音频超过预算或格式无效');return;}
    this.bytes+=part.byteLength;this.queue.push(part);this.drain();
  }
  commit(){if(this.closed||this.ending)return;this.ending=true;this.drain();}
  cancel(){
    if(this.closed)return;this.closed=true;this.controller.abort();clearTimeout(this.timer);clearTimeout(this.drainTimer);
    this.queue=[];this.encoder=undefined;this.socket?.close();this.socket=null;
  }
  private fail(message:string){if(this.closed)return;this.cancel();this.callbacks.error(message);}
  private drain(){
    if(this.closed||!this.ready||this.drainTimer)return;
    const ws=this.socket;if(!ws||ws.readyState!==WebSocket.OPEN)return;
    try{
      // Pace preroll buffered during connection setup; no reconnect or replay.
      for(let n=0;n<4&&this.queue.length&&ws.bufferedAmount<96000;n++)ws.send(this.queue.shift()!);
      if(this.queue.length){this.drainTimer=setTimeout(()=>{this.drainTimer=undefined;this.drain();},20);return;}
      if(this.ending&&!this.committed){if(!this.bytes){this.fail('未采集到有效音频，本段未提交');return;}this.committed=true;ws.send(JSON.stringify({type:'commit'}));}
    }catch{this.fail('实时音频发送失败，本段已丢弃');}
  }
  private receive(raw:unknown){
    if(this.closed)return;
    let data:{type?:string;sample_rate?:number;text?:string;message?:string;status?:number};
    try{if(typeof raw!=='string'||raw.length>65536)throw new Error();data=JSON.parse(raw);if(!data||typeof data!=='object'||Array.isArray(data))throw new Error();}
    catch{this.fail('实时转写返回无效内容');return;}
    if(data.type==='ready'){
      if(this.ready||data.sample_rate!==24000){this.fail('实时转写音频格式或就绪回执无效');return;}
      this.ready=true;this.callbacks.ready?.();this.drain();
    }else if(data.type==='partial'||data.type==='completed'){
      if(!this.ready||typeof data.text!=='string'||data.text.length>12000||(data.type==='completed'&&(!this.committed||!data.text.trim()))){this.fail('实时转写未返回有效最终内容或回执');return;}
      if(data.type==='partial')this.callbacks.partial(data.text);
      else{const text=data.text;this.cancel();this.callbacks.final(text);}
    }else if(data.type==='error'){
      this.fail(typeof data.message==='string'?data.message:'实时转写失败，请检查服务兼容性');
      if(data.status===401)window.dispatchEvent(new Event('lingxi:unauthorized'));
    }
  }
}

type Phase='idle'|'connecting'|'recording'|'finishing';
export function VoiceRealtime({sessionId='',inputDevice='',disabled=false,onBeforeStart,onBusy,onFinal}:{sessionId?:string;inputDevice?:string;disabled?:boolean;onBeforeStart?():Promise<void>;onBusy?(busy:boolean):void;onFinal(text:string):void}){
  const [phase,setPhase]=useState<Phase>('idle'),[partial,setPartial]=useState(''),[error,setError]=useState('');
  const mic=useRef(new Microphone()),stream=useRef<RealtimeTranscription|null>(null),generation=useRef(0),alive=useRef(false);
  const callbacks=useRef({onBeforeStart,onBusy,onFinal});callbacks.current={onBeforeStart,onBusy,onFinal};
  function clear(){++generation.current;mic.current.discard();stream.current?.cancel();stream.current=null;if(alive.current){setPhase('idle');setPartial('');callbacks.current.onBusy?.(false);}}
  function fail(message:string){clear();if(alive.current)setError(message);}
  useEffect(()=>{alive.current=true;const hide=()=>{if(document.hidden)clear();};const offline=()=>clear();document.addEventListener('visibilitychange',hide);window.addEventListener('offline',offline);return()=>{clear();alive.current=false;document.removeEventListener('visibilitychange',hide);window.removeEventListener('offline',offline);};},[sessionId]);
  async function finish(){
    const current=stream.current,id=generation.current;if(!current)return;
    setPhase('finishing');
    try{await mic.current.finishPCM();if(id===generation.current)current.commit();}
    catch(e){if(id===generation.current)fail((e as Error).message);}
  }
  async function start(){
    if(disabled||phase!=='idle')return;
    clear();const id=generation.current;setError('');setPartial('');setPhase('connecting');callbacks.current.onBusy?.(true);
    try{
      await callbacks.current.onBeforeStart?.();
      if(id!==generation.current||!alive.current)return;
      const valid=()=>id===generation.current&&alive.current;
      const current=new RealtimeTranscription({
        ready:()=>{void mic.current.streamPCM(inputDevice,part=>{if(valid())current.write(part);},()=>{if(valid())void finish();},message=>{if(valid())fail(message);}).then(()=>{if(valid())setPhase('recording');else mic.current.discard();}).catch(e=>{if(valid())fail(e.message);});},
        partial:text=>{if(valid())setPartial(text);},
        final:text=>{if(valid()){clear();callbacks.current.onFinal(text);}},
        error:message=>{if(valid())fail(message);}
      });stream.current=current;current.start(sessionId);
    }catch(e){if(id===generation.current)fail((e as Error).message);}
  }
  return <div className="form-stack voice-realtime"><div className="actions"><button type="button" className="button" disabled={phase==='idle'?disabled:phase!=='recording'} onClick={()=>void (phase==='recording'?finish():start())}>{phase==='idle'?'实时转写':phase==='connecting'?'正在连接':phase==='recording'?'结束转写':'等待定稿'}</button>{phase!=='idle'&&<button type="button" className="button" onClick={clear}>取消转写</button>}</div><p className="muted">边说边显示临时字幕，结束后核对定稿。仅在点击后开启麦克风，最长 58 秒，可能计费；取消不提交聊天。</p><ErrorNotice error={error}/>{phase!=='idle'&&<p className="muted" role="status">{phase==='recording'?'正在实时转写…':phase==='finishing'?'正在核对最终转写…':'正在连接转写服务…'}</p>}{partial&&<div className="data-card"><strong>临时字幕</strong><p className="voice-live-caption">{partial}</p></div>}</div>;
}
