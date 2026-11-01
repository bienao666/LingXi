import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, ApiError, setCsrf } from './api';
import type { Session } from './api';
import { modules } from './navigation';
import '../styles/startup.css';

type Identity = {name: string};
type BootResult = {status: 'completed' | 'skipped' | 'destroyed' | 'failed' | 'timeout'};
type BootController = {destroy(): void; skip(): void; finished: Promise<BootResult>};
type BootOptions = {
  theme: 'dark'; interaction: 'auto'; speed: number; sound: boolean; speech: boolean;
  startAt: 'brand' | 'identity'; ready: Promise<void>;
  brand: {name: string; latin: string; tagline: string; logoUrl: string};
  identity: {name: string; code: string; level: string}; returnFocus: string;
  identityStep(context: {container: HTMLElement; signal: AbortSignal}): Promise<Identity>;
  onComplete(result: BootResult): void;
};
declare global { interface Window { RhineLabBoot?: {mount(host: HTMLElement, options: BootOptions): BootController} } }
type AuthState = {status: 'checking'} | {status: 'anonymous'} | {status: 'authenticated'; session: Session} | {status: 'error'; message: string};

export function StartupGate({onReady}: {onReady(session: Session): void}) {
  const [auth, setAuth] = useState<AuthState>({status:'checking'});
  const [startAt, setStartAt] = useState<'brand' | 'identity' | null>(null);
  const [slot, setSlot] = useState<HTMLElement | null>(null), [fallback, setFallback] = useState(false);
  const [busy, setBusy] = useState(false), [failure, setFailure] = useState(''), [confirmed, setConfirmed] = useState(false);
  const host = useRef<HTMLDivElement>(null), password = useRef<HTMLInputElement>(null), code = useRef<HTMLInputElement>(null), action = useRef<HTMLButtonElement>(null);
  const alive = useRef(true), operation = useRef(false), request = useRef<AbortController | null>(null);
  const resolveIdentity = useRef<((identity: Identity) => void) | null>(null), accepted = useRef<Session | null>(null);
  const readyCallback = useRef(onReady); readyCallback.current = onReady;
  const usernameId = useId(), passwordId = useId(), codeId = useId();

  async function authRequest<T>(url: string, method = 'GET', body?: unknown) {
    const controller = new AbortController(); request.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try { return await api<T>(url, method, body, {signal:controller.signal}); }
    finally { clearTimeout(timeout); if (request.current === controller) request.current = null; }
  }
  async function check() {
    if (operation.current) return;
    operation.current = true; setAuth({status:'checking'}); setFailure('');
    try {
      const session = await authRequest<Session>('/api/auth/me');
      if (alive.current) {
        setCsrf(session.csrf); setAuth({status:'authenticated',session});
        setStartAt(previous => previous ?? 'identity');
      }
    } catch(e) {
      if (alive.current) {
        setCsrf('');
        if (e instanceof ApiError && e.status === 401) {
          setAuth({status:'anonymous'}); setStartAt(previous => previous ?? 'brand');
        } else setAuth({status:'error',message:(e as Error).message});
      }
    } finally { operation.current = false; }
  }
  function accept(session: Session) {
    accepted.current = session; setCsrf(session.csrf); setConfirmed(true);
    setAuth({status:'authenticated',session});
    if (fallback) readyCallback.current(session);
    else resolveIdentity.current?.({name:session.username});
  }
  async function confirm(body?: {username: string; password: string; code: string}) {
    if (operation.current) return;
    operation.current = true; setBusy(true); setFailure('');
    try {
      const session = body ? await authRequest<Session>('/api/auth/login','POST',body) : await authRequest<Session>('/api/auth/me');
      if (alive.current) accept(session);
    } catch(e) {
      if (alive.current) {
        setFailure((e as Error).message);
        if (!body && e instanceof ApiError && e.status === 401) {setCsrf('');setAuth({status:'anonymous'});}
      }
    } finally {
      if (password.current) password.current.value = '';
      if (code.current) code.current.value = '';
      operation.current = false;
      if (alive.current) {
        setBusy(false);
        if (!accepted.current) requestAnimationFrame(() => {if (alive.current) (password.current || action.current)?.focus();});
      }
    }
  }

  useEffect(() => {
    alive.current = true;
    void check();
    return () => {alive.current = false;request.current?.abort();};
  }, []);

  useEffect(() => {
    if (!startAt) return;
    let disposed = false, controller: BootController | undefined;
    function useFallback() {if (!disposed) {setSlot(null);setConfirmed(false);setFallback(true);}}
    if (!window.RhineLabBoot || !host.current) useFallback();
    else {
      try {
        controller = window.RhineLabBoot.mount(host.current, {
          theme:'dark', interaction:'auto', speed:1, sound:false, speech:false,
          startAt,
          ready:Promise.resolve().then(() => {if (modules.length !== 10 || !document.getElementById('main-content')) throw new Error('页面框架未能载入');}),
          brand:{name:'灵犀',latin:'LINGXI',tagline:'与你并肩 · 一点就通',logoUrl:'/assets/lingxi.svg'},
          identity:{name:'个人工作空间',code:'LINGXI / WORKSPACE',level:'身份确认'}, returnFocus:'#main-content',
          identityStep:({container,signal}) => new Promise<Identity>(resolve => {
            resolveIdentity.current = resolve; setSlot(container);
            signal.addEventListener('abort',() => {resolveIdentity.current = null;}, {once:true});
          }),
          onComplete(result) {
            if (disposed) return;
            if ((result.status === 'completed' || result.status === 'skipped') && accepted.current) readyCallback.current(accepted.current);
            else useFallback();
          }
        });
        const root = host.current.querySelector('[data-boot-animation]');
        if (!root || getComputedStyle(root).getPropertyValue('--rlb-style-ready').trim() !== '1') {controller.destroy();useFallback();}
      } catch {useFallback();}
    }
    return () => {disposed = true;controller?.destroy();resolveIdentity.current = null;};
  }, [startAt]);

  const identity = <div className="identity-panel" aria-busy={busy || auth.status==='checking'}>
    <p className="identity-workspace">个人工作空间</p>
    {auth.status==='checking' && <p role="status" className="muted">正在检查登录状态…</p>}
    {auth.status==='error' && <><p role="alert" className="error">{auth.message}</p><button className="button primary" autoFocus onClick={() => void check()}>重试连接</button></>}
    {auth.status==='authenticated' && <><p className="identity-name">{auth.session.username}</p><p className="muted">{confirmed ? '身份已确认，正在进入工作空间…' : '登录会话有效，请确认后进入。'}</p><button ref={action} className="button primary" autoFocus disabled={busy || confirmed} onClick={() => void confirm()}>{busy ? '正在核验…' : confirmed ? '身份已确认' : '确认进入'}</button></>}
    {auth.status==='anonymous' && <form className="form-stack" onSubmit={e => {
      e.preventDefault();const form = new FormData(e.currentTarget);
      void confirm({username:String(form.get('username') || ''),password:String(form.get('password') || ''),code:String(form.get('code') || '').trim()});
    }} onKeyDown={e => {if (e.key==='Enter' && e.nativeEvent.isComposing) {e.preventDefault();e.stopPropagation();}}}>
      <label htmlFor={usernameId}>用户名<input id={usernameId} name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={100} autoFocus readOnly={busy}/></label>
      <label htmlFor={passwordId}>密码<input id={passwordId} ref={password} name="password" type="password" autoComplete="current-password" required maxLength={300} readOnly={busy}/></label>
      <label htmlFor={codeId}>验证代码<input id={codeId} ref={code} name="code" autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} maxLength={64} readOnly={busy} aria-describedby={`${codeId}-hint`}/></label>
      <p id={`${codeId}-hint`} className="muted">已启用双重验证时填写认证器的 6 位验证码或一次性恢复码；未启用可留空。</p>
      <button className="button primary" disabled={busy}>{busy ? '正在确认…' : '确认身份'}</button>
    </form>}
    {failure && <p className="error" role="alert">{failure}</p>}
    {auth.status==='anonymous' && <details className="identity-help"><summary>首次使用</summary><p>请在部署端运行 <code>docker compose run --rm core python -m lingxi.cli init-admin</code> 初始化管理员。</p></details>}
  </div>;

  return <div className="startup-guard"><div ref={host} id="boot-host"/>
    {slot && createPortal(identity,slot)}
    {(fallback || startAt === null) && <main className="identity-fallback" aria-labelledby="identity-title" onKeyDown={e => {
      if (e.key!=='Tab') return;
      const items=Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input,summary')).filter(el=>el.getClientRects().length);
      const index=items.indexOf(document.activeElement as HTMLElement);
      if (items.length && ((e.shiftKey && index<=0) || (!e.shiftKey && index===items.length-1))) {e.preventDefault();items[e.shiftKey?items.length-1:0].focus();}
    }}><div className="identity-fallback-brand"><img src="/assets/lingxi.svg" width="44" height="44" alt=""/><span>灵犀 · LINGXI</span></div><h1 id="identity-title">身份确认</h1><p className="muted">IDENTITY VERIFICATION</p>{identity}</main>}
  </div>;
}
