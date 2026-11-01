import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice,formatTime,useResource} from './ui';

type State=components['schemas']['NoticeState'];
type Preference=components['schemas']['NoticePreferenceView'];
type Claim=components['schemas']['NoticeClaimView'];
const ROOT='/api/browser-notifications';
const EVENT='lingxi:browser-notices';
const key=(scope:string)=>`lingxi.browser-notices.${scope}`;
const supported=()=>window.isSecureContext&&typeof window.Notification==='function';
function opted(p:Preference){try{return p.enabled&&localStorage.getItem(key(p.scope))===String(p.version);}catch{return false;}}
const labels:Record<string,string>={candidate:'等待时机',attempting:'结果未知',requested:'请求展示',failed:'展示失败',clicked:'点击提示',invalidated:'已经失效',expired:'已经过期'};

/** Mounted only inside the authenticated workspace. No push or microphone access. */
export function BrowserNoticeAgent(){
  useEffect(()=>{
    if(!supported())return;
    let alive=true,busy=false,generation=0;
    const controller=new AbortController(),opened=new Map<string,Notification>();
    function closeAll(){for(const n of opened.values())n.close();opened.clear();}
    function reset(){++generation;closeAll();void poll();}
    const fail=(message:string)=>window.dispatchEvent(new CustomEvent(EVENT+':error',{detail:message}));
    const ack=(id:string,status:'requested'|'failed'|'clicked')=>api(`${ROOT}/${encodeURIComponent(id)}/ack`,'POST',{status},{signal:controller.signal});
    async function poll(){
      if(!alive||busy)return;
      busy=true;const epoch=generation;
      try{
        const state=await api<State>(ROOT+'/state','GET',undefined,{signal:controller.signal});
        if(!alive||epoch!==generation)return;
        for(const[id,n]of opened){if(!state.receipts.some(r=>r.id===id&&r.eligible)){n.close();opened.delete(id);}}
        const allowed=()=>alive&&epoch===generation&&document.visibilityState==='hidden'&&window.Notification.permission==='granted';
        if(!opted(state.preference)||window.Notification.permission!=='granted'){closeAll();return;}
        if(!allowed())return;
        const claim=await api<Claim>(ROOT+'/claims','POST',{consent_version:state.preference.version,background:true},{signal:controller.signal});
        if(!allowed()||!claim.id||!claim.expires_at||claim.expires_at*1000<=Date.now()||claim.path!=='#/proactive/history')return;
        // A source or preference can change during the claim round trip. Recheck
        // the persisted attempt immediately before constructing a notification.
        const fresh=await api<State>(ROOT+'/state','GET',undefined,{signal:controller.signal});
        if(!allowed()||!opted(fresh.preference)||fresh.preference.version!==state.preference.version
          ||!fresh.receipts.some(r=>r.id===claim.id&&r.status==='attempting'&&r.eligible))return;
        const id=claim.id;
        try{
          const n=new window.Notification('灵犀 · 新的交流',{body:'有一条新的站内交流，方便时打开灵犀查看。',
            tag:'lingxi-'+id,silent:true,requireInteraction:false});
          opened.set(id,n);
          n.onclose=()=>opened.delete(id);
          n.onclick=()=>{
            n.close();opened.delete(id);
            if(!alive)return;
            window.focus();location.hash='#/proactive/history';
            void ack(id,'clicked').catch(()=>{});
          };
          n.onerror=()=>{n.close();opened.delete(id);fail('浏览器未能展示通知；本次不自动重发，站内交流仍可查看。');void ack(id,'failed').catch(()=>{});};
          // Constructor completion is a request to the browser, not proof of
          // display, delivery or reading; acknowledgement failure never replays.
          void ack(id,'requested').catch(()=>fail('通知展示回执未保存，结果未知，不自动重发。'));
        }catch{
          fail('当前浏览器无法展示系统通知；请使用站内提示或明确授权的 QQ。');
          void ack(id,'failed').catch(()=>{});
        }
      }catch{
        if(alive){closeAll();fail('通知状态暂不可核对，已暂停本次展示；保留站内入口。');}
      }finally{busy=false;}
    }
    const timer=window.setInterval(()=>void poll(),15000);
    document.addEventListener('visibilitychange',reset);
    window.addEventListener('offline',reset);
    window.addEventListener('storage',reset);
    window.addEventListener(EVENT,reset);
    window.addEventListener('lingxi:contact-preferences',reset);
    void poll();
    return()=>{alive=false;++generation;controller.abort();window.clearInterval(timer);closeAll();
      document.removeEventListener('visibilitychange',reset);window.removeEventListener('offline',reset);
      window.removeEventListener('storage',reset);window.removeEventListener(EVENT,reset);
      window.removeEventListener('lingxi:contact-preferences',reset);};
  },[]);
  return null;
}

export function BrowserNotificationSettings(){
  const state=useResource<State>(ROOT+'/state');
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[revision,setRevision]=useState(0);
  const alive=useRef(true);
  useEffect(()=>{alive.current=true;const update=()=>{setRevision(x=>x+1);state.reload();};
    const issue=(e:Event)=>setError(String((e as CustomEvent).detail||'通知状态暂不可用'));
    window.addEventListener(EVENT,update);window.addEventListener('storage',update);window.addEventListener(EVENT+':error',issue);
    return()=>{alive.current=false;window.removeEventListener(EVENT,update);window.removeEventListener('storage',update);window.removeEventListener(EVENT+':error',issue);};},[state.reload]);
  void revision;
  const p=state.data?.preference,available=supported();
  const permission=available?window.Notification.permission:'unsupported';
  const enabled=!!p&&opted(p)&&permission==='granted';
  async function save(enable:boolean){
    if(!p||busy)return;
    setBusy(true);setError('');setNotice('');
    try{
      if(enable){
        if(!available)throw new Error('当前浏览器不支持页面系统通知，请使用安全上下文中的受支持浏览器。');
        const result=window.Notification.permission==='default'?await window.Notification.requestPermission():window.Notification.permission;
        if(!alive.current)return;
        if(result!=='granted')throw new Error('未获得浏览器通知权限；不会重复申请，请在浏览器站点设置中手动修改。');
      }
      const saved=await api<Preference>(ROOT+'/preferences','PUT',{version:p.version,enabled:enable,acknowledgement:'browser_running_only'});
      if(!alive.current)return;
      try{if(enable)localStorage.setItem(key(saved.scope),String(saved.version));else localStorage.removeItem(key(saved.scope));}
      catch{throw new Error('当前浏览器无法保存通知选择；不会在此端自动展示通知。');}
      window.dispatchEvent(new Event(EVENT));state.reload();
      setNotice(enable?'本浏览器已开启；只接收开启后新建且仍新鲜的 Web 主动交流。':'已关闭系统通知，其他标签页和浏览器也会停止新展示。');
    }catch(e){if(alive.current)setError((e as Error).message);}finally{if(alive.current)setBusy(false);}
  }
  return <section className="data-card page-stack" aria-label="浏览通知"><div className="pane-toolbar"><h3>浏览通知</h3><button type="button" className="button" disabled={busy} onClick={state.reload}>刷新状态</button></div>
    <p className="muted">由你点击开启后才申请系统通知权限。仅在此页面仍运行、标签页位于后台时检查；关闭浏览器后不承诺推送。只显示固定提示，不在锁屏泄露交流正文，不播音。</p>
    <p>浏览器权限：{({granted:'已经允许',denied:'已经拒绝',default:'尚未申请',unsupported:'暂不支持'} as Record<string,string>)[permission]} · 本浏览器：{enabled?'通知开启':'通知关闭'}</p>
    <ErrorNotice error={error||state.error}/>{notice&&<p role="status">{notice}</p>}
    {p&&<div className="actions"><button type="button" className="button primary" disabled={busy||!available||enabled||permission==='denied'} onClick={()=>void save(true)}>{busy?'正在处理':'开启通知'}</button><button type="button" className="button" disabled={busy||!p.enabled} onClick={()=>void save(false)}>关闭通知</button></div>}
    <p className="muted">遵守联系偏好、安静时段、来源有效期及统一额度；每条交流仅尝试一次，跨浏览器去重。不会补发旧交流、在 QQ 投递后再通知，或将未回应推断为情绪。每端须主动开启，系统权限变更以浏览器设置为准。</p>
    {state.data&&<><p className="muted">{state.data.note}</p><details><summary>最近尝试</summary>{state.data.receipts.length?<ul>{state.data.receipts.map(r=><li key={r.id}>{labels[r.status]||r.status} · {formatTime(r.attempted_at||r.created_at)}{!r.eligible?' · 不再展示':''}</li>)}</ul>:<p>暂无通知候选或尝试。此处最多列出最近 20 项。</p>}</details></>}
  </section>;
}
