import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {Provider,ProviderModelCatalog,Message,Run} from './api';
import type {components} from './api.generated';
import {ErrorNotice,useResource} from './ui';
import {audioDevices,Microphone} from './voiceAudio';
import {VoiceRealtime} from './VoiceRealtime';
type Config=components['schemas']['VoiceSettingsView'];
type VoiceSession=components['schemas']['VoiceSessionView'];
type Playback=components['schemas']['PlaybackView'];
type PersonaVoice=components['schemas']['PersonaVoiceView'];
const defaults:Config={enabled:false,version:0,asr:{provider_id:'',model:''},tts:{provider_id:'',model:''},realtime_asr:{provider_id:'',model:''},realtime_tts:{provider_id:'',model:''},realtime_language_field:'language',language:'zh',voice:'alloy',speed:1,follow_persona:false,checks:{asr:false,tts:false}};

function AudioModelPicker({title,value,providers,onChange,disabled}:{title:string;value:{provider_id?:string;model?:string};providers:Provider[];onChange(v:{provider_id:string;model:string}):void;disabled:boolean}){
  const [names,setNames]=useState<string[]>([]),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  useEffect(()=>{setNames([]);setError('');if(!value.provider_id)return;let live=true;setLoading(true);void api<ProviderModelCatalog>(`/api/providers/${value.provider_id}/models`).then(r=>{if(live)setNames(r.models.map(m=>m.id));}).catch(e=>{if(live)setError(e.message);}).finally(()=>{if(live)setLoading(false);});return()=>{live=false;};},[value.provider_id]);
  return <fieldset className="form-stack"><legend>{title}</legend><label>服务连接<select value={value.provider_id} disabled={disabled} onChange={e=>onChange({provider_id:e.target.value,model:''})}><option value="">请选择服务连接</option>{providers.filter(p=>p.enabled).map(p=><option key={p.id} value={p.id} disabled={!!p.protocol&&p.protocol!=='openai'}>{p.name}{p.protocol&&p.protocol!=='openai'?'（语音未接入）':''}</option>)}</select></label><label>语音模型<select value={value.model} disabled={disabled||loading||!value.provider_id} onChange={e=>onChange({provider_id:value.provider_id||'',model:e.target.value})}><option value="">{loading?'正在加载模型…':'请选择供应商模型'}</option>{value.model&&!names.includes(value.model)&&<option value={value.model}>{value.model}（已保存）</option>}{names.map(n=><option key={n} value={n}>{n}</option>)}</select></label><ErrorNotice error={error}/></fieldset>;
}

export function VoiceSettings(){
  const resource=useResource<Config>('/api/voice/config'),providers=useResource<Provider[]>('/api/providers'),personas=useResource<PersonaVoice[]>('/api/voice/personas');
  const [personaId,setPersonaId]=useState('');
  const personaList=Array.isArray(personas.data)?personas.data:[],selectedPersona=personaList.find(p=>p.id===personaId);
  const [config,setConfig]=useState<Config>(defaults),[dirty,setDirty]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[recording,setRecording]=useState(false);
  const mic=useRef(new Microphone()),alive=useRef(true),testAudio=useRef<HTMLAudioElement>(null),[audio,setAudio]=useState('');
  const controller=useRef<AbortController|null>(null);
  useEffect(()=>{if(resource.data){setConfig(resource.data);setDirty(false);}},[resource.data]);
  useEffect(()=>()=>{alive.current=false;mic.current.discard();controller.current?.abort();testAudio.current?.pause();},[]);
  useEffect(()=>{const stop=()=>{if(document.hidden){mic.current.discard();setRecording(false);testAudio.current?.pause();controller.current?.abort();setBusy(false);}};document.addEventListener('visibilitychange',stop);return()=>document.removeEventListener('visibilitychange',stop);},[]);
  useEffect(()=>()=>{if(audio)URL.revokeObjectURL(audio);},[audio]);
  const change=(next:Partial<Config>)=>{setConfig(c=>({...c,...next}));setDirty(true);setNotice('');};
  async function testRecord(){setError('');try{if(!recording){setBusy(true);await mic.current.start('',()=>void testRecordEnd());if(alive.current)setRecording(true);setBusy(false);}else await testRecordEnd();}catch(e){setError((e as Error).message);setRecording(false);setBusy(false);}}
  async function testRecordEnd(){setBusy(true);setRecording(false);try{const data=await mic.current.finish();const ctrl=new AbortController();controller.current=ctrl;const r=await api<{text:string}>('/api/voice/config/test-asr','POST',undefined,{binary:data,signal:ctrl.signal});if(alive.current){setNotice('转写验证结果：'+r.text);resource.reload();}}catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy(false);}}
  return <div className="page-stack"><div className="pane-toolbar"><h2>语音配置</h2><button className="button" disabled={busy||recording} onClick={resource.reload}>重新加载</button></div><p className="muted">配置转写和合成模型后，可在对话中显式开启语音。录音只作临时处理；声音由人工智能合成。</p><ErrorNotice error={error||resource.error||providers.error||personas.error}/>{notice&&<p role="status">{notice}</p>}<form className="form-stack" onSubmit={e=>{e.preventDefault();setBusy(true);setError('');void api<Config>('/api/voice/config','PUT',config).then(r=>{if(alive.current){setConfig(r);setDirty(false);setNotice('语音配置已保存；配置变化后请重新验证能力。');}}).catch(e=>{if(alive.current)setError(e.message);}).finally(()=>{if(alive.current)setBusy(false);});}}>
    <label className="checkbox"><input type="checkbox" checked={config.enabled} disabled={busy||recording} onChange={e=>change({enabled:e.target.checked})}/>启用浏览器语音</label><div className="card-grid voice-settings-grid">{(['asr','tts']as const).map(kind=><div className="data-card" key={kind}><AudioModelPicker title={kind==='asr'?'语音转写':'语音合成'} value={config[kind]||{}} providers={providers.data||[]} disabled={busy||recording} onChange={v=>change({[kind]:v})}/><p className="muted">{!dirty&&config.checks?.[kind]?'接口已验证':'等待验证'} · {kind==='asr'?'PCM WAV → 中文转写':'文字 → PCM WAV'}</p></div>)}</div>
    <div className="data-card form-stack"><h3>实时转写</h3><AudioModelPicker title="实时识别" value={config.realtime_asr||{}} providers={providers.data||[]} disabled={busy||recording} onChange={v=>change({realtime_asr:v})}/><label>语言协议<select value={config.realtime_language_field||'language'} disabled={busy||recording} onChange={e=>change({realtime_language_field:e.target.value as 'language'|'languages'})}><option value="language">language（单语言）</option><option value="languages">languages（语言数组）</option></select></label><p className="muted">可选的原生 WebSocket 转写接口，普通聊天兼容接口不代表支持此能力。按供应商协议选择语言格式；例如 gpt-live-transcribe 使用 languages。{!dirty&&config.checks?.realtime_asr?'实时接口已验证':'实时接口等待验证'}</p></div>
    <div className="card-grid"><label>识别语言<select value={config.language} disabled={busy||recording} onChange={e=>change({language:e.target.value})}><option value="zh">中文</option><option value="en">英语</option><option value="ja">日语</option></select></label><label>合成音色<input value={config.voice} disabled={busy||recording} maxLength={80} pattern="[a-zA-Z0-9_-]+" onChange={e=>change({voice:e.target.value})}/><small className="muted">填写服务支持的音色标识，例如 alloy。</small></label><label>合成语速<input type="number" min="0.25" max="4" step="0.05" value={config.speed} disabled={busy||recording} onChange={e=>change({speed:Number(e.target.value)})}/></label></div><label className="checkbox"><input type="checkbox" checked={!!config.follow_persona} disabled={busy||recording} onChange={e=>change({follow_persona:e.target.checked})}/>跟随人格默认音色</label><small className="muted">默认关闭。开启后，新语音回合固定所选人格的音色；人格未填音色时沿用系统默认。未验证的音色停止播报并保留文字，不静默替换音色。</small><div className="actions"><button className="button primary" disabled={busy||recording||!resource.data}>{busy?'正在处理':'保存配置'}</button></div></form>
    <div className="data-card form-stack"><h3>能力验证</h3><p className="muted">先保存配置，再录制一句话验证转写，或生成合成示例。验证会请求所选服务，可能计费；通过仅表示接口兼容。</p>{dirty&&<p role="status" className="muted">配置尚未保存，保存后再验证。</p>}<div className="actions"><button className="button" disabled={busy||dirty||!config.asr?.model} onClick={()=>void testRecord()}>{recording?'结束转写':'录音验证'}</button><button className="button" disabled={busy||recording||dirty||!config.tts?.model} onClick={()=>{setBusy(true);setError('');const ctrl=new AbortController();controller.current=ctrl;void api<Blob>('/api/voice/config/test-tts','POST',undefined,{response:'blob',signal:ctrl.signal}).then(blob=>{if(alive.current){setAudio(URL.createObjectURL(blob));setNotice('合成接口已验证，请点击播放器试听。');resource.reload();}}).catch(e=>{if(alive.current)setError(e.message);}).finally(()=>{if(alive.current)setBusy(false);});}}>合成示例</button></div>{recording&&<p role="status">正在录音，最长 58 秒。请说一句话，然后结束转写。</p>}{audio&&<audio className="voice-audio" ref={testAudio} controls src={audio} aria-label="人工智能合成语音示例"/>}</div>
    <div className="data-card form-stack"><h3>实时验证</h3><p className="muted">保存配置后，点击实时转写并说一句话，结束后返回定稿才标记接口验证通过。字幕和音频不保存为聊天或记忆。</p><VoiceRealtime disabled={busy||recording||dirty||!config.realtime_asr?.model} onBusy={setBusy} onFinal={text=>{setNotice('实时转写验证结果：'+text);resource.reload();}}/><AudioModelPicker title="实时合成" value={config.realtime_tts||{}} providers={providers.data||[]} disabled={busy||recording} onChange={v=>change({realtime_tts:v})}/><p className="muted">实时合成需单独配置并验证。已验证后，语音会话的核心回复逐句通过原生音频合成；字幕核对失败会保留文字且不自动改用其他模型。{!dirty&&config.checks?.realtime_tts?'实时合成已验证':'实时合成等待验证'}</p><button type="button" className="button" disabled={busy||recording||dirty||!config.realtime_tts?.model} onClick={()=>{setBusy(true);setError('');const ctrl=new AbortController();controller.current=ctrl;void api<Blob>('/api/voice/config/test-realtime-tts','POST',undefined,{response:'blob',signal:ctrl.signal}).then(blob=>{if(alive.current){setAudio(URL.createObjectURL(blob));setNotice('实时合成接口已验证，请点击播放器试听。');resource.reload();}}).catch(e=>{if(alive.current)setError((e as Error).message);}).finally(()=>{if(alive.current)setBusy(false);});}}>验证实时合成</button></div>
    <div className="data-card form-stack"><div className="pane-toolbar"><h3>人格音色</h3><button className="button" disabled={busy||recording} onClick={personas.reload}>重新加载</button></div><p className="muted">先在<a href="#/assistants/personas">人格管理</a>填写默认音色，再选择人格验证。此操作调用当前合成服务，可能计费；仅验证接口兼容，不证明音色质量，也不会开启麦克风或自动播音。</p><label>选择人格<select value={personaId} disabled={busy||recording} onChange={e=>setPersonaId(e.target.value)}><option value="">请选择已配置音色的人格</option>{personaList.map(p=><option key={p.id} value={p.id} disabled={!p.enabled}>{p.name} · {p.voice}{!p.enabled?'（已停用）':''}</option>)}</select></label>{!personaList.length&&<p className="muted">尚无人格音色，可先在人格管理设置。</p>}{selectedPersona&&<p role="status">{selectedPersona.name} · v{selectedPersona.version} · {selectedPersona.voice} · {selectedPersona.issue||(selectedPersona.verified&&!dirty?'接口已验证':'等待验证')}</p>}<div className="actions"><button className="button" disabled={busy||recording||dirty||!config.tts?.model||!selectedPersona||!selectedPersona.enabled||!!selectedPersona.issue} onClick={()=>{if(!selectedPersona)return;setBusy(true);setError('');const ctrl=new AbortController();controller.current=ctrl;void api<Blob>(`/api/voice/personas/${selectedPersona.id}/test`,'POST',{version:selectedPersona.version},{response:'blob',signal:ctrl.signal}).then(blob=>{if(alive.current){setAudio(URL.createObjectURL(blob));setNotice('人格音色接口已验证，请点击播放器试听。');personas.reload();resource.reload();}}).catch(e=>{if(alive.current)setError(e.message);}).finally(()=>{if(alive.current)setBusy(false);});}}>验证音色</button></div></div>
  </div>;
}

export function VoicePanel({conversationId,messages,runs,reload,userBusy=false}:{conversationId:string;messages:Message[];runs:Run[];reload():Promise<void>;userBusy?:boolean}){
  const [session,setSession]=useState<VoiceSession|null>(null),[busy,setBusy]=useState(false),[recording,setRecording]=useState(false),[draft,setDraft]=useState(''),[error,setError]=useState(''),[status,setStatus]=useState(''),[speaking,setSpeaking]=useState(false),[blocked,setBlocked]=useState(false);
  const [continuous,setContinuous]=useState(false),[listening,setListening]=useState(false),[realtimeBusy,setRealtimeBusy]=useState(false);
  const [allowProactive,setAllowProactive]=useState(false),[proactiveBusy,setProactiveBusy]=useState(false);
  const voiceIdle=useRef(Date.now()),activityEpoch=useRef(0),userSpeaking=useRef(false),proactiveRequest=useRef(false),voiceAvailability=useRef(false);
  const voiceClaimController=useRef<AbortController|null>(null);
  const [devices,setDevices]=useState<MediaDeviceInfo[]>([]),[inputDevice,setInputDevice]=useState(''),[outputDevice,setOutputDevice]=useState('');
  const mic=useRef(new Microphone()),continuousMic=useRef(new Microphone()),audio=useRef<HTMLAudioElement>(null),alive=useRef(true),sessionRef=useRef<VoiceSession|null>(null),playback=useRef<Playback|null>(null),control=useRef<AbortController|null>(null),playGeneration=useRef(0),audioURL=useRef('');
  const pending=useRef<{content:string;id:string;automatic:boolean}|null>(null),initiated=useRef(''),awaiting=useRef(false),listeningRef=useRef(false),voiceGeneration=useRef(0),inputControl=useRef<AbortController|null>(null);
  const openGeneration=useRef(0),cancelPlay=useRef<(()=>void)|null>(null);
  const active=runs.find(r=>['queued','running'].includes(r.status));
  const [speechLabel,setSpeechLabel]=useState('');
  const [nativeConfig,setNativeConfig]=useState<Config|null>(null);
  const speakRef=useRef<(p:Playback)=>Promise<void>>(async()=>{});
  function voiceActivity(){voiceIdle.current=Date.now();activityEpoch.current++;}
  voiceAvailability.current=!!session?.allow_proactive&&!busy&&!recording&&!realtimeBusy&&!draft&&!speaking&&!blocked&&!active&&!userBusy&&!awaiting.current&&!userSpeaking.current;
  function pauseListening(message?:string){
    voiceActivity();userSpeaking.current=false;
    ++voiceGeneration.current;listeningRef.current=false;continuousMic.current.discard();inputControl.current?.abort();
    if(alive.current){setListening(false);if(message)setStatus(message);}
  }
  function failListening(message:string){pauseListening('聆听已暂停，检查后手动恢复；不会自动重试。');if(alive.current)setError(message);}
  async function stopPlayback(){
    voiceActivity();
    ++playGeneration.current;control.current?.abort();cancelPlay.current?.();audio.current?.pause();
    if(audioURL.current){URL.revokeObjectURL(audioURL.current);audioURL.current='';}
    setSpeaking(false);setBlocked(false);const previous=playback.current;playback.current=null;
    if(previous&&sessionRef.current)await api(`/api/voice/playbacks/${previous.id}/progress`,'POST',{completed_parts:previous.confirmed_parts,stop:true});
  }
  async function resumePlayback(){
    const plan=playback.current,generation=playGeneration.current,sid=sessionRef.current?.id;
    if(!plan||!sid)return;
    setError('');
    try{
      if(plan.proactive){
        const current=await api<Playback>(`/api/voice/playbacks/${plan.id}`);
        if(current.status!=='active')throw new Error('主动播报已停止，请阅读文字记录');
      }
      if(generation!==playGeneration.current||sessionRef.current?.id!==sid||document.hidden)return;
      await audio.current?.play();
      if(generation!==playGeneration.current||sessionRef.current?.id!==sid){audio.current?.pause();return;}
      setBlocked(false);setSpeaking(true);
    }catch(e){
      if(generation!==playGeneration.current)return;
      if(plan.proactive)await stopPlayback().catch(()=>{});
      setError((e as Error).message);
    }
  }
  function endSession(){
    voiceClaimController.current?.abort();voiceActivity();userSpeaking.current=false;
    ++openGeneration.current;const s=sessionRef.current;sessionRef.current=null;++playGeneration.current;
    pauseListening();control.current?.abort();cancelPlay.current?.();mic.current.discard();audio.current?.pause();
    if(audioURL.current){URL.revokeObjectURL(audioURL.current);audioURL.current='';}
    playback.current=null;initiated.current='';awaiting.current=false;pending.current=null;
    if(alive.current){setSession(null);setAllowProactive(false);setProactiveBusy(false);setSpeechLabel('');setRecording(false);setSpeaking(false);setBlocked(false);setBusy(false);setDraft('');setStatus('语音已结束，文字对话及后台任务继续保留。');}
    if(s)void api(`/api/voice/sessions/${s.id}`,'DELETE').catch(()=>{});
  }
  useEffect(()=>{alive.current=true;const stop=()=>{if(document.hidden)endSession();};const offline=()=>endSession();document.addEventListener('visibilitychange',stop);window.addEventListener('offline',offline);return()=>{alive.current=false;endSession();document.removeEventListener('visibilitychange',stop);window.removeEventListener('offline',offline);};},[conversationId]);
  useEffect(()=>{
    if(!session)return;
    const expiry=setTimeout(()=>endSession(),Math.max(0,session.expires_at*1000-Date.now()));
    const timer=setInterval(()=>{void api<VoiceSession>(`/api/voice/sessions/${session.id}/heartbeat`,'POST').catch(e=>{if(sessionRef.current?.id===session.id){setError(e.message);endSession();}});},20000);
    return()=>{clearInterval(timer);clearTimeout(expiry);};
  },[session?.id]);
  async function startSession(){
    if(busy)return;const generation=++openGeneration.current;setBusy(true);setError('');
    try{
      const s=await api<VoiceSession>('/api/voice/sessions','POST',{conversation_id:conversationId,mode:continuous?'continuous':'manual',allow_proactive:allowProactive});
      if(!alive.current||generation!==openGeneration.current||document.hidden){void api(`/api/voice/sessions/${s.id}`,'DELETE').catch(()=>{});return;}
      voiceActivity();sessionRef.current=s;setSession(s);setStatus(s.mode==='continuous'?'连续对话已就绪，点击开始聆听才开启麦克风。':'语音已开启，点击开始录音；不会自动打开麦克风。');
      const available=await audioDevices();if(sessionRef.current?.id===s.id)setDevices(available);
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current&&generation===openGeneration.current)setBusy(false);}
  }
  async function finishRecording(){
    voiceActivity();userSpeaking.current=false;    setRecording(false);setBusy(true);setError('');const s=sessionRef.current;
    try{
      const blob=await mic.current.finish();if(!s||sessionRef.current?.id!==s.id)return;
      const controller=new AbortController();control.current=controller;setStatus('正在转写，请稍候…');
      const result=await api<{text:string}>(`/api/voice/sessions/${s.id}/transcriptions`,'POST',undefined,{binary:blob,signal:controller.signal});
      if(sessionRef.current?.id===s.id){setDraft(result.text);setStatus('请核对转写内容，确认发送后才进入聊天和记忆流程。');}
    }catch(e){if(alive.current&&sessionRef.current)setError((e as Error).message);}finally{if(alive.current)setBusy(false);}
  }
  async function startRecording(){
    setError('');setBusy(true);
    try{await stopPlayback();await mic.current.start(inputDevice,()=>void finishRecording());if(sessionRef.current&&alive.current){setRecording(true);setStatus('正在录音，最长 58 秒；结束后先核对文字。');setDevices(await audioDevices());}else mic.current.discard();}
    catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy(false);}
  }
  async function sendTranscript(content:string,automatic:boolean){
    const s=sessionRef.current;if(!s||!content.trim())return;
    if(!pending.current||pending.current.content!==content||pending.current.automatic!==automatic)pending.current={id:crypto.randomUUID(),content,automatic};
    const request=pending.current;awaiting.current=true;continuousMic.current.setDetection('paused');setBusy(true);setError('');
    try{
      await stopPlayback();if(sessionRef.current?.id!==s.id)return;
      const run=await api<Run>(`/api/voice/sessions/${s.id}/messages`,'POST',{content:content.trim(),request_id:request.id,...(automatic?{automatic:true}:{})});
      if(sessionRef.current?.id===s.id){initiated.current=run.id;pending.current=null;setDraft('');setStatus('正在生成回复…');await reload();}
    }catch(e){if(sessionRef.current?.id===s.id){awaiting.current=false;setDraft(content);failListening((e as Error).message);}}
    finally{if(alive.current&&sessionRef.current?.id===s.id)setBusy(false);}
  }
  async function automaticTurn(blob:Blob,generation:number){
    voiceActivity();userSpeaking.current=false;    const s=sessionRef.current;if(!s||generation!==voiceGeneration.current||!listeningRef.current)return;
    continuousMic.current.setDetection('paused');setBusy(true);setError('');setStatus('正在转写；本段将自动发送，不自动保存记忆。');
    const controller=new AbortController();inputControl.current=controller;
    try{
      const result=await api<{text:string}>(`/api/voice/sessions/${s.id}/transcriptions`,'POST',undefined,{binary:blob,signal:controller.signal});
      if(sessionRef.current?.id!==s.id||generation!==voiceGeneration.current||!listeningRef.current)return;
      const text=result.text.trim();if(!text)throw new Error('未识别到有效内容，请手动恢复后重说');
      if(['暂停聆听','暂停聆听。','结束语音','结束语音。','先别聊','先别聊。'].includes(text)){
        if(text.startsWith('结束语音'))endSession();else{pauseListening('已按语音指令暂停聆听；未修改长期联系偏好。');if(text.startsWith('先别聊'))await changeProactive(false);}return;
      }
      setDraft(text);await sendTranscript(text,true);
    }catch(e){if(sessionRef.current?.id===s.id&&generation===voiceGeneration.current)failListening((e as Error).message);}
    finally{if(alive.current&&sessionRef.current?.id===s.id)setBusy(false);}
  }
  async function startListening(){
    if(busy||active||draft||awaiting.current||listeningRef.current)return;
    const s=sessionRef.current;if(!s||s.mode!=='continuous')return;
    const generation=++voiceGeneration.current;setBusy(true);setError('');listeningRef.current=true;
    try{
      await continuousMic.current.listen(inputDevice,{
        start:()=>{
          if(generation!==voiceGeneration.current||sessionRef.current?.id!==s.id)return;
          voiceActivity();userSpeaking.current=true;
          // Stop locally before waiting for the progress receipt. Never cancel tasks.
          void stopPlayback().catch(e=>{if(generation===voiceGeneration.current)failListening(e.message);});
          continuousMic.current.setDetection('listening');setStatus('正在聆听，停顿后自动转写发送…');
        },
        utterance:blob=>{void automaticTurn(blob,generation);},
        error:message=>{if(generation===voiceGeneration.current&&sessionRef.current?.id===s.id)failListening(message);}
      });
      if(generation!==voiceGeneration.current||sessionRef.current?.id!==s.id||document.hidden){continuousMic.current.discard();return;}
      setListening(true);if(playback.current)continuousMic.current.setDetection('playback');
      setStatus('麦克风已开启；停顿约 1.2 秒后自动发送，播报时说话可打断。');
      const available=await audioDevices();if(generation===voiceGeneration.current)setDevices(available);
    }catch(e){if(generation===voiceGeneration.current&&sessionRef.current?.id===s.id)failListening((e as Error).message);}
    finally{if(alive.current&&sessionRef.current?.id===s.id)setBusy(false);}
  }
  async function changeProactive(enabled:boolean){
    const s=sessionRef.current;voiceActivity();setAllowProactive(enabled);setError('');
    if(!s)return;
    sessionRef.current={...s,allow_proactive:enabled};setSession(sessionRef.current);setProactiveBusy(true);
    if(!enabled&&playback.current?.proactive)void stopPlayback().catch(()=>{});
    try{await api(`/api/voice/sessions/${s.id}/proactive`,'PUT',{enabled});}
    catch(e){if(sessionRef.current?.id===s.id){sessionRef.current={...sessionRef.current,allow_proactive:false};setSession(sessionRef.current);setAllowProactive(false);setError((e as Error).message);}}
    finally{if(alive.current)setProactiveBusy(false);}
  }
  useEffect(()=>{if(busy||recording||draft||speaking||active||userBusy)voiceActivity();},[busy,recording,draft,speaking,active?.id,userBusy]);
  useEffect(()=>{
    if(!session?.allow_proactive)return;
    const sid=session.id;
    const check=async()=>{
      if(proactiveRequest.current||sessionRef.current?.id!==sid||document.hidden)return;
      const p=playback.current;
      if(p?.proactive){
        proactiveRequest.current=true;
        try{const current=await api<Playback>(`/api/voice/playbacks/${p.id}`);if(playback.current?.id===p.id&&current.status!=='active'){await stopPlayback();setStatus('主动播报已停止，文字记录仍可查看。');}}
        catch(e){if(playback.current?.id===p.id){await stopPlayback().catch(()=>{});failListening((e as Error).message);}}
        finally{proactiveRequest.current=false;}
        return;
      }
      if(!voiceAvailability.current||userSpeaking.current||playback.current){voiceIdle.current=Date.now();return;}
      const idle=Date.now()-voiceIdle.current;if(idle<3000)return;
      const epoch=activityEpoch.current;proactiveRequest.current=true;
      const controller=new AbortController();voiceClaimController.current=controller;
      try{
        const result=await api<{playback?:Playback|null;notice?:string}>(`/api/voice/sessions/${sid}/proactive-playback`,'POST',{idle_ms:Math.min(idle,3600000)},{signal:controller.signal});
        const plan=result.playback;
        if(!plan){if(result.notice&&sessionRef.current?.id===sid)setError(result.notice);return;}
        if(sessionRef.current?.id!==sid||document.hidden||epoch!==activityEpoch.current||!voiceAvailability.current||playback.current){
          if(sessionRef.current?.id===sid)await api(`/api/voice/playbacks/${plan.id}/progress`,'POST',{completed_parts:0,stop:true}).catch(()=>{});
          return;
        }
        setStatus('灵犀有一条有来源的接续；可随时打断或关闭主动开口。');void speakRef.current(plan);
      }catch(e){if(sessionRef.current?.id===sid&&!controller.signal.aborted){endSession();setError('主动语音连接失败，语音已结束；文字和后台任务仍保留。'+(e as Error).message);}}
      finally{proactiveRequest.current=false;if(voiceClaimController.current===controller)voiceClaimController.current=null;}
    };
    const timer=setInterval(()=>void check(),1000);return()=>{clearInterval(timer);};
  },[session?.id,session?.allow_proactive]);
  speakRef.current=async plan=>{
    const generation=++playGeneration.current;playback.current=plan;setSpeaking(true);setBlocked(false);
    if(listeningRef.current)continuousMic.current.setDetection('playback');
    setSpeechLabel(plan.voice?`本轮音色：${plan.voice}${plan.voice_source==='persona'?` · ${plan.persona_name} v${plan.persona_version}`:' · 系统默认'}`:'');
    try{
      let useNative=!!nativeConfig?.checks?.realtime_tts;
      if(nativeConfig===null){const latest=await api<Config>('/api/voice/config');if(alive.current)setNativeConfig(latest);useNative=!!latest.checks?.realtime_tts;}
      for(let i=plan.confirmed_parts;i<plan.parts.length;i++){
        if(generation!==playGeneration.current||!sessionRef.current)return;
        const controller=new AbortController();control.current=controller;setStatus(`正在合成 ${i+1} / ${plan.parts.length} 段`);
        const audioPath=useNative?`/api/voice/playbacks/${plan.id}/native-audio/${i}`:`/api/voice/playbacks/${plan.id}/audio/${i}`;
        const blob=await api<Blob>(audioPath,'POST',undefined,{response:'blob',signal:controller.signal});
        if(generation!==playGeneration.current||!sessionRef.current)return;
        if(audioURL.current)URL.revokeObjectURL(audioURL.current);audioURL.current=URL.createObjectURL(blob);
        const player=audio.current;if(!player)throw new Error('播放器不可用，请阅读文字回复');player.src=audioURL.current;
        if('setSinkId'in player)await (player as HTMLAudioElement&{setSinkId(id:string):Promise<void>}).setSinkId(outputDevice);else if(outputDevice)throw new Error('当前浏览器不支持选择输出设备，请选择系统默认');
        if(generation!==playGeneration.current||!sessionRef.current)return;
        await new Promise<void>((resolve,reject)=>{
          const cleanup=()=>{player.onended=null;player.onerror=null;player.onpause=null;cancelPlay.current=null;};
          cancelPlay.current=()=>{cleanup();reject(new Error('播报已停止'));};
          player.onended=()=>{cleanup();resolve();};player.onerror=()=>{cleanup();reject(new Error('音频播放失败，请阅读文字或重新开启语音'));};
          player.onpause=()=>{if(generation!==playGeneration.current){cleanup();reject(new Error('播报已停止'));}else if(!player.ended){pauseListening('播报已暂停，聆听同时关闭；点击继续播报恢复声音。');setBlocked(true);setSpeaking(false);}};
          void player.play().then(()=>{if(generation!==playGeneration.current){player.pause();cleanup();reject(new Error('播报已停止'));}else setStatus(plan.parts[i]);}).catch(()=>{
            if(generation===playGeneration.current){pauseListening('自动播放受限，麦克风已关闭；请手动继续。');setBlocked(true);setSpeaking(false);setError('浏览器阻止自动播放，请点击继续播报或阅读文字');}
          });
        });
        if(generation!==playGeneration.current||!sessionRef.current)return;
        const next=await api<Playback>(`/api/voice/playbacks/${plan.id}/progress`,'POST',{completed_parts:i+1,stop:false});
        if(generation!==playGeneration.current||!sessionRef.current)return;playback.current=next;plan=next;
      }
      if(generation===playGeneration.current){playback.current=null;if(listeningRef.current){continuousMic.current.setDetection('listening');setStatus('播报完成，继续聆听；也可以安静等待。');}else setStatus('播报完成，点击开始录音继续。');}
    }catch(e){if(generation===playGeneration.current&&alive.current){await stopPlayback().catch(()=>{});failListening((e as Error).message);}}
    finally{if(generation===playGeneration.current&&alive.current)setSpeaking(false);}
  };
  useEffect(()=>{
    const run=runs.find(r=>r.id===initiated.current);if(!run||!session)return;
    if(['failed','cancelled','invalidated'].includes(run.status)){initiated.current='';awaiting.current=false;pauseListening('回复未完成，聆听已暂停；请查看文字状态。');return;}
    const message=messages.find(m=>m.run_id===run.id&&m.role==='assistant'&&m.status==='completed');if(run.status!=='completed'||!message)return;
    initiated.current='';awaiting.current=false;const s=session.id;
    void api<Playback>(`/api/voice/sessions/${s}/playbacks`,'POST',{message_id:message.id}).then(plan=>{if(sessionRef.current?.id===s)void speakRef.current(plan);}).catch(e=>{if(alive.current&&sessionRef.current?.id===s)failListening(e.message);});
  },[messages,runs,session?.id]);
  useEffect(()=>{
    const p=playback.current;
    // A newly claimed contact may precede the next message-page poll. Absence
    // from that snapshot is not deletion; proactive status is checked separately.
    if(p&&((!p.proactive&&!messages.some(m=>m.id===p.message_id&&m.status==='completed'))||
      messages.some(m=>m.id===p.message_id&&m.status!=='completed'))){void stopPlayback().catch(()=>{});failListening('回复依据已变化，停止播报。');}
  },[messages]);
  // Another entry point can submit a text turn while this microphone is open.
  useEffect(()=>{if(active&&active.id!==initiated.current&&listeningRef.current)pauseListening('文字回复正在生成，聆听已暂停。');},[active?.id]);
  return <section className="voice-panel" aria-label="浏览器语音">
    <div className="pane-toolbar"><strong>语音会话</strong><div className="actions">{!session?<button type="button" className="button" disabled={busy||!!active} onClick={()=>void startSession()}>{busy?'正在开启':'开启语音'}</button>:<button type="button" className="button" onClick={endSession}>结束语音</button>}<a href="#/settings/voice">语音配置</a></div></div>
    <label className="checkbox"><input type="checkbox" checked={session?session.mode==='continuous':continuous} disabled={!!session||busy} onChange={e=>setContinuous(e.target.checked)}/>连续对话</label>
    <label className="checkbox"><input type="checkbox" checked={session?!!session.allow_proactive:allowProactive} disabled={proactiveBusy||busy} onChange={e=>void changeProactive(e.target.checked)}/>主动开口</label>
    <p className="muted">默认关闭，仅对当前语音会话生效。开启后可在自然停顿分享有效话题；不会因沉默反复开口，不补播旧联系。遵守联系偏好与安静时段；隐藏页面、结束或关闭后停止。合成可能计费。</p>
    {(continuous||session?.mode==='continuous')&&<p className="muted">开启聆听后，停顿自动转写发送（可能计费），不设固定聊天次数。未经逐句核对的转写不自动保存记忆，不执行工具或修改联系偏好。建议佩戴耳机；本地音量检测不能保证区分人声与回声。单段最长 25 秒，静候 60 秒关闭麦克风。</p>}
    <ErrorNotice error={error}/>{status&&<p className="muted" role="status">{status}</p>}
    {session&&<>
      <p className="muted">人工智能合成语音 · 逐段播报 · 页面隐藏或离线时结束 · 最长 15 分钟</p>
      {speechLabel&&<p className="muted">{speechLabel}</p>}
      <div className="voice-device-row"><label>输入设备<select value={inputDevice} disabled={busy||recording||realtimeBusy||listening} onChange={e=>setInputDevice(e.target.value)}><option value="">系统默认</option>{devices.filter(d=>d.kind==='audioinput'&&d.deviceId).map((d,i)=><option key={d.deviceId} value={d.deviceId}>{d.label||`麦克风 ${i+1}`}</option>)}</select></label><label>输出设备<select value={outputDevice} disabled={speaking||listening} onChange={e=>setOutputDevice(e.target.value)}><option value="">系统默认</option>{devices.filter(d=>d.kind==='audiooutput'&&d.deviceId).map((d,i)=><option key={d.deviceId} value={d.deviceId}>{d.label||`扬声器 ${i+1}`}</option>)}</select></label></div>
      <div className="actions">{session.mode==='continuous'?
        <button type="button" className="button" disabled={!listening&&(busy||!!active||!!draft)} onClick={()=>listening?pauseListening('麦克风已关闭；已提交的回复继续处理。'):void startListening()}>{listening?'暂停聆听':'开始聆听'}</button>:
        <button type="button" className="button" disabled={busy||realtimeBusy||!!active||!!draft} onClick={()=>void (recording?finishRecording():startRecording())}>{recording?'结束录音':speaking?'打断录音':'开始录音'}</button>}
        {recording&&<button type="button" className="button" onClick={()=>{mic.current.discard();setRecording(false);setStatus('本段录音已丢弃。');}}>丢弃录音</button>}
        {(speaking||blocked)&&<button type="button" className="button" onClick={()=>{pauseListening();void stopPlayback().then(()=>setStatus('已停止播报，麦克风已关闭，文字回复仍保留。')).catch(e=>setError(e.message));}}>停止播报</button>}
        {blocked&&<button type="button" className="button" onClick={()=>void resumePlayback()}>继续播报</button>}
      </div>
      {session.mode!=='continuous'&&<VoiceRealtime sessionId={session.id} inputDevice={inputDevice} disabled={busy||recording||!!active||!!draft} onBeforeStart={async()=>{voiceActivity();await stopPlayback();}} onBusy={setRealtimeBusy} onFinal={text=>{setDraft(text);setStatus('请核对最终转写，确认发送后才进入聊天和记忆流程。');}}/>}
      {draft&&<div className="form-stack"><label>{pending.current?.automatic?'待核对发送状态':'核对转写'}<textarea value={draft} maxLength={12000} rows={3} disabled={busy||!!pending.current?.automatic} onChange={e=>setDraft(e.target.value)}/></label>
        {pending.current?.automatic&&<p className="muted">自动发送结果待核对。重试沿用同一请求，不能改变原转写或将它改为已确认事实；丢弃草稿不会取消已提交的回复。</p>}
        <div className="actions"><button type="button" className="button primary" disabled={busy||!!active||!draft.trim()} onClick={()=>void sendTranscript(draft,!!pending.current?.automatic)}>{pending.current?.automatic?'重试发送':'确认发送'}</button><button type="button" className="button" disabled={busy} onClick={()=>{setDraft('');pending.current=null;setStatus('临时转写已丢弃。');}}>丢弃转写</button></div>
      </div>}
    </>}
    <audio ref={audio} className="sr-only" aria-label="灵犀合成语音"/>
  </section>;
}
