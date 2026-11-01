import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {Microphone} from './voiceAudio';
import {ErrorNotice} from './ui';

type Phase='idle'|'connecting'|'recording'|'finishing';
export function VoiceRealtime({sessionId='',inputDevice='',disabled=false,onBeforeStart,onBusy,onFinal}:{sessionId?:string;inputDevice?:string;disabled?:boolean;onBeforeStart?():Promise<void>;onBusy?(busy:boolean):void;onFinal(text:string):void}){
  const [phase,setPhase]=useState<Phase>('idle'),[partial,setPartial]=useState(''),[error,setError]=useState('');
  const mic=useRef(new Microphone()),socket=useRef<WebSocket|null>(null),generation=useRef(0),alive=useRef(false),timer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
  const callbacks=useRef({onBeforeStart,onBusy,onFinal});callbacks.current={onBeforeStart,onBusy,onFinal};
  function clear(){++generation.current;clearTimeout(timer.current);mic.current.discard();socket.current?.close();socket.current=null;if(alive.current){setPhase('idle');setPartial('');callbacks.current.onBusy?.(false);}}
  function fail(message:string){clear();if(alive.current)setError(message);}
  useEffect(()=>{alive.current=true;const hide=()=>{if(document.hidden)clear();};const offline=()=>clear();document.addEventListener('visibilitychange',hide);window.addEventListener('offline',offline);return()=>{clear();alive.current=false;document.removeEventListener('visibilitychange',hide);window.removeEventListener('offline',offline);};},[sessionId]);
  async function finish(){
    const current=socket.current,id=generation.current;if(!current||current.readyState!==WebSocket.OPEN)return;
    setPhase('finishing');
    try{await mic.current.finishPCM();if(id!==generation.current)return;current.send(JSON.stringify({type:'commit'}));}
    catch(e){if(id===generation.current)fail((e as Error).message);}
  }
  async function start(){
    if(disabled||phase!=='idle')return;
    clear();const id=generation.current;setError('');setPartial('');setPhase('connecting');callbacks.current.onBusy?.(true);
    try{
      await callbacks.current.onBeforeStart?.();
      if(id!==generation.current||!alive.current)return;
      const ticket=await api<components['schemas']['TicketView']>('/api/voice/realtime-tickets','POST',{session_id:sessionId});
      if(id!==generation.current||!alive.current)return;
      const url=new URL(ticket.path,location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
      const ws=new WebSocket(url);socket.current=ws;
      const valid=()=>id===generation.current&&alive.current;
      timer.current=setTimeout(()=>{if(valid())fail('实时转写等待超时，请检查连接后手动重试');},74000);
      ws.onopen=()=>{if(valid())ws.send(JSON.stringify({token:ticket.token}));else ws.close();};
      ws.onerror=()=>{if(valid())fail('实时转写连接失败，请检查网络和服务配置；不会自动重试');};
      ws.onclose=()=>{if(valid())fail('实时转写连接已中断，本段未提交；请手动重试');};
      ws.onmessage=event=>{
        if(!valid())return;
        let data:{type?:string;sample_rate?:number;text?:string;message?:string;status?:number};
        try{if(typeof event.data!=='string'||event.data.length>65536)throw new Error();data=JSON.parse(event.data);if(!data||typeof data!=='object'||Array.isArray(data))throw new Error();}catch{fail('实时转写返回无效内容');return;}
        if(data.type==='ready'){
          if(data.sample_rate!==24000){fail('实时转写音频格式不兼容');return;}
          void mic.current.streamPCM(inputDevice,part=>{if(!valid()||ws.readyState!==WebSocket.OPEN)throw new Error();if(ws.bufferedAmount>96000)throw new Error();ws.send(part);},()=>{if(valid())void finish();},message=>{if(valid())fail(message);}).then(()=>{if(valid())setPhase('recording');else mic.current.discard();}).catch(e=>{if(valid())fail(e.message);});
        }else if(data.type==='partial'){
          if(typeof data.text!=='string'||data.text.length>12000){fail('实时字幕超过预算');return;}
          setPartial(data.text);
        }else if(data.type==='completed'){
          if(typeof data.text!=='string'||!data.text.trim()||data.text.length>12000){fail('实时转写未返回有效最终内容');return;}
          const text=data.text;clear();callbacks.current.onFinal(text);
        }else if(data.type==='error'){
          fail(typeof data.message==='string'?data.message:'实时转写失败，请检查服务兼容性');
          if(data.status===401)window.dispatchEvent(new Event('lingxi:unauthorized'));
        }
      };
    }catch(e){if(id===generation.current)fail((e as Error).message);}
  }
  return <div className="form-stack voice-realtime"><div className="actions"><button type="button" className="button" disabled={phase==='idle'?disabled:phase!=='recording'} onClick={()=>void (phase==='recording'?finish():start())}>{phase==='idle'?'实时转写':phase==='connecting'?'正在连接':phase==='recording'?'结束转写':'等待定稿'}</button>{phase!=='idle'&&<button type="button" className="button" onClick={clear}>取消转写</button>}</div><p className="muted">边说边显示临时字幕，结束后核对定稿。仅在点击后开启麦克风，最长 58 秒，可能计费；取消不提交聊天。</p><ErrorNotice error={error}/>{phase!=='idle'&&<p className="muted" role="status">{phase==='recording'?'正在实时转写…':phase==='finishing'?'正在核对最终转写…':'正在连接转写服务…'}</p>}{partial&&<div className="data-card"><strong>临时字幕</strong><p className="voice-live-caption">{partial}</p></div>}</div>;
}
