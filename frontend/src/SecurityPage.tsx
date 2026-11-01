import React,{useEffect,useRef,useState} from 'react';
import {api,ApiError} from './api';
import {ErrorNotice,formatTime,Modal,useResource} from './ui';
import type {components} from './api.generated';

type State=components['schemas']['SecurityView'];
type Setup=components['schemas']['SetupView'];
type Recovery=components['schemas']['RecoveryView'];
type Login={id:string;created_at:number;expires_at:number;current:boolean};

export function SecurityPage(){
  const state=useResource<State>('/api/auth/security'),sessions=useResource<Login[]>('/api/auth/sessions');
  const [mode,setMode]=useState<'setup'|'disable'|'recovery'|'password'|null>(null),[codes,setCodes]=useState<string[]|null>(null),[selected,setSelected]=useState<Login|null>(null),[notice,setNotice]=useState('');
  function refresh(){state.reload();sessions.reload();}
  function complete(result?:Recovery){setMode(null);refresh();if(result)setCodes(result.recovery_codes);else setNotice('双重验证已停用，其他浏览器会话已退出。');}
  return <div className="page-stack"><div className="pane-toolbar"><h2>安全设置</h2><button className="button" onClick={refresh}>刷新状态</button></div><ErrorNotice error={state.error||sessions.error}/>{notice&&<p role="status">{notice}</p>}{!state.data&&!state.error&&<p role="status">正在检查安全配置…</p>}{state.data&&<article className="data-card"><div className="pane-toolbar"><h3>登录密码</h3><button className="button" onClick={()=>{setNotice('');setMode('password');}}>修改密码</button></div><p className="muted">核验当前密码后修改；已启用双重验证时还需新的验证码或未使用的恢复码。成功后所有浏览器退出，服务器任务继续执行。不会改动模型密钥或账号授权。</p></article>}{state.data&&<article className="data-card"><div className="pane-toolbar"><h3>双重验证</h3><span className="status-badge">{state.data.enabled?'已经启用':'尚未启用'}</span></div><p className="muted">基于时间的认证器验证码（TOTP）。启用后，登录需同时提交密码与验证码或一次性恢复码。服务器应保持系统时间同步。</p>{state.data.enabled?<><p>剩余恢复码 {state.data.recovery_remaining} 个。恢复码只显示一次，每个使用后失效。</p><div className="actions"><button className="button" onClick={()=>{setNotice('');setMode('recovery');}}>更换恢复</button><button className="button danger" onClick={()=>{setNotice('');setMode('disable');}}>停用验证</button></div></>:<>{state.data.pending&&<p role="status">当前会话有待完成的绑定，{formatTime(state.data.pending_expires_at!)} 到期。关闭页面不会自动启用；重新开始将替换未完成绑定。</p>}<button className="button primary" onClick={()=>{setNotice('');setMode('setup');}}>{state.data.pending?'重新绑定':'启用验证'}</button></>}</article>}<h3>登录会话</h3><p className="muted">可撤销其他浏览器登录。撤销当前会话会返回身份确认；不会取消已提交的服务器任务。</p><div className="card-grid">{sessions.data?.map(row=><article className="data-card" key={row.id}><div className="pane-toolbar"><strong>{row.current?'当前会话':'其他会话'}</strong><button className="button danger" onClick={()=>setSelected(row)}>撤销会话</button></div><p>创建 {formatTime(row.created_at)}</p><p>到期 {formatTime(row.expires_at)}</p></article>)}</div><details className="data-card"><summary>无法验证</summary><p>可使用未使用的恢复码登录。若认证器和全部恢复码均丢失，部署管理员需在服务器终端执行 <code className="plugin-digest">docker compose exec core python -m lingxi.cli reset-totp</code>，输入当前所有者密码并明确确认。该操作停用双重验证、撤销全部登录并记录审计；不会绕过密码。</p></details>{mode&&state.data&&(mode==='password'?<PasswordChange state={state.data} close={()=>setMode(null)} refresh={refresh}/>:mode==='setup'?<Enrollment state={state.data} close={()=>{setMode(null);refresh();}} done={complete}/>:<FactorChange mode={mode} state={state.data} close={()=>setMode(null)} done={complete}/>)}{codes&&<RecoveryCodes codes={codes} close={()=>setCodes(null)}/>} {selected&&<RevokeSession row={selected} close={()=>setSelected(null)} done={()=>{setSelected(null);sessions.reload();setNotice('登录会话已撤销。');}}/>}</div>;
}

function PasswordChange({state,close,refresh}:{state:State;close():void;refresh():void}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[approved,setApproved]=useState(false);
  const pending=useRef(false),errorTarget=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{if(error)errorTarget.current?.focus();},[error]);
  async function submit(form:HTMLFormElement){
    if(pending.current||!approved)return;
    const data=new FormData(form),password=String(data.get('password')),next=String(data.get('new_password')),confirmation=String(data.get('confirmation'));
    setError('');
    if(next!==confirmation||!next.trim()){setError('两次新密码须一致，且不能只含空白。');return;}
    if(password===next){setError('新密码不能与当前密码相同。');return;}
    pending.current=true;setBusy(true);
    try {
      await api<components['schemas']['PasswordChangeView']>('/api/auth/password','PUT',{
        version:state.version,password,new_password:next,confirmation,code:String(data.get('code')||''),acknowledgement:'revoke_all_logins'});
      form.reset();close();window.dispatchEvent(new window.Event('lingxi:unauthorized'));
    } catch(e){
      setError(e instanceof ApiError&&e.status===0?'连接中断，修改结果尚未确认。请用新密码重新登录核对，不会自动重试。':(e as Error).message);
    } finally {form.reset();setApproved(false);pending.current=false;setBusy(false);}
  }
  return <Modal title="修改密码" close={close} dismissible={!busy}><form className="form-stack" onSubmit={e=>{e.preventDefault();void submit(e.currentTarget);}}>
    <p>修改后包括当前浏览器在内的全部登录会话将失效，需使用新密码重新确认身份。已提交任务继续运行；双重验证及未使用的恢复码保留，未完成的认证器绑定取消。</p>
    <label>当前密码<input name="password" type="password" autoComplete="current-password" required minLength={1} maxLength={300} disabled={busy}/></label>
    <label>新的密码<input name="new_password" type="password" autoComplete="new-password" required minLength={12} maxLength={300} disabled={busy} aria-describedby="password-rules"/></label>
    <label>确认密码<input name="confirmation" type="password" autoComplete="new-password" required minLength={12} maxLength={300} disabled={busy}/></label>
    <p id="password-rules" className="muted">12–300 个字符，建议使用唯一的长密码；须与当前密码不同，首尾空格原样保留。密码不会写入浏览器存储或配置导出。</p>
    {state.enabled&&<label>验证代码<input name="code" autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} required maxLength={64} disabled={busy}/></label>}
    <label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已保存新密码，了解修改后全部浏览器都需要重新登录。</label>
    {error&&<p ref={errorTarget} role="alert" tabIndex={-1} className="error">{error}</p>}
    <div className="actions"><button type="button" className="button" disabled={busy} onClick={refresh}>刷新核对</button><button className="button primary" disabled={busy||!approved}>{busy?'正在核验':'确认修改'}</button></div>
  </form></Modal>;
}

function Enrollment({state,close,done}:{state:State;close():void;done(result:Recovery):void}){
  const [prepared,setPrepared]=useState<Setup|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[approved,setApproved]=useState(false);
  const request=useRef(crypto.randomUUID()),pending=useRef(false);
  async function execute<T>(action:()=>Promise<T>,next:(value:T)=>void){
    if(pending.current)return;pending.current=true;setBusy(true);setError('');
    try{next(await action());}catch(e){setError((e as Error).message);}finally{pending.current=false;setBusy(false);}
  }
  return <Modal title="绑定验证" close={close} dismissible={!busy}>{!prepared?<form key="prepare" className="form-stack" onSubmit={e=>{e.preventDefault();const form=e.currentTarget,password=String(new FormData(form).get('password'));void execute(()=>api<Setup>('/api/auth/totp/setup','POST',{version:state.version,password,request_id:request.current}),setPrepared).finally(()=>form.reset());}}><p>输入当前密码生成绑定密钥。将密钥手动添加到认证器后，仍须提交验证码才会启用。</p><label>当前密码<input name="password" type="password" autoComplete="current-password" required maxLength={300} disabled={busy}/></label><ErrorNotice error={error}/><button className="button primary" disabled={busy}>{busy?'正在准备':'准备绑定'}</button></form>:<form key="verify" className="form-stack" onSubmit={e=>{e.preventDefault();if(!approved)return;const form=e.currentTarget,code=String(new FormData(form).get('code'));void execute(()=>api<Recovery>('/api/auth/totp/confirm','POST',{version:prepared.version,code}),done).finally(()=>{form.reset();setApproved(false);});}}><p>在认证器中手动添加密钥：名称填写“灵犀”，类型选“基于时间”。SHA1、6 位、每 30 秒更新。绑定于 {formatTime(prepared.expires_at)} 到期。</p><label>绑定密钥<input className="security-secret" value={prepared.secret} readOnly autoComplete="off" spellCheck={false}/></label><p className="muted">此密钥可生成登录验证码，请勿发送给他人；不保存到浏览器存储，也不会进入诊断导出。</p><label>认证代码<input name="code" autoFocus inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" minLength={6} maxLength={6} required disabled={busy}/></label><label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我理解启用后其他会话将退出，下一步必须离线保存恢复码。</label><ErrorNotice error={error}/><div className="actions"><button type="button" className="button" disabled={busy} onClick={()=>void execute(()=>api<State>('/api/auth/totp/cancel','POST',{version:prepared.version}),close)}>取消绑定</button><button className="button primary" disabled={busy||!approved}>{busy?'正在核验':'确认启用'}</button></div></form>}</Modal>;
}

function FactorChange({mode,state,close,done}:{mode:'disable'|'recovery';state:State;close():void;done(result?:Recovery):void}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[approved,setApproved]=useState(false);
  const pending=useRef(false);
  return <Modal title={mode==='disable'?'停用验证':'更换恢复'} close={close} dismissible={!busy}><form className="form-stack" onSubmit={e=>{e.preventDefault();if(pending.current||!approved)return;pending.current=true;setBusy(true);setError('');const form=e.currentTarget,data=new FormData(form);void api<State|Recovery>(`/api/auth/totp/${mode}`,'POST',{version:state.version,password:String(data.get('password')),code:String(data.get('code')).trim()}).then(value=>done(mode==='recovery'?value as Recovery:undefined)).catch(e=>setError(e.message)).finally(()=>{form.reset();setApproved(false);pending.current=false;setBusy(false);});}}><p>{mode==='disable'?'停用后，账号将只通过密码登录，所有旧恢复码和认证器密钥均失效。':'更换后，全部旧恢复码立即失效，新恢复码仅显示一次；绑定的认证器保持有效。'}</p><p>此操作会退出其他浏览器会话。请使用未使用的验证码或恢复码，刚用于登录的验证码不能重复使用。</p><label>当前密码<input name="password" type="password" autoComplete="current-password" required maxLength={300} disabled={busy}/></label><label>验证代码<input name="code" autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} required maxLength={64} disabled={busy}/></label><label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已核对操作影响，确认{mode==='disable'?'停用双重验证':'替换全部旧恢复码'}。</label><ErrorNotice error={error}/><button className={`button ${mode==='disable'?'danger':'primary'}`} disabled={busy||!approved}>{busy?'正在核验':mode==='disable'?'确认停用':'确认更换'}</button></form></Modal>;
}

function RecoveryCodes({codes,close}:{codes:string[];close():void}){
  const [saved,setSaved]=useState(false);
  return <Modal title="恢复代码" close={close} dismissible={saved}><p>请立即离线保存到安全位置。每个恢复码只能用一次，使用时仍需账号密码。关闭后不能再次查看；未保存可通过新的认证器验证码和密码重新更换。</p><div className="security-codes">{codes.map(code=><code key={code}>{code}</code>)}</div><label className="checkbox"><input type="checkbox" checked={saved} onChange={e=>setSaved(e.target.checked)}/>我已将恢复码保存在安全位置。</label><div className="actions"><button className="button primary" disabled={!saved} onClick={close}>完成保存</button></div></Modal>;
}

function RevokeSession({row,close,done}:{row:Login;close():void;done():void}){
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  return <Modal title="撤销会话" close={close} dismissible={!busy}><p>将撤销{row.current?'当前':'另一个'}浏览器会话（创建于 {formatTime(row.created_at)}）。{row.current?'你将返回身份确认页面。':'该浏览器下一次请求时需重新登录。'}服务器任务继续执行。</p><ErrorNotice error={error}/><div className="actions"><button className="button" disabled={busy} onClick={close}>保留会话</button><button className="button danger" disabled={busy} onClick={()=>{if(busy)return;setBusy(true);setError('');void api(`/api/auth/sessions/${encodeURIComponent(row.id)}`,'DELETE').then(()=>{if(row.current)window.dispatchEvent(new window.Event('lingxi:unauthorized'));done();}).catch(e=>setError(e.message)).finally(()=>setBusy(false));}}>确认撤销</button></div></Modal>;
}
