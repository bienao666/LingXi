import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { groups, modules, store, routes } from './navigation';
import type { NavModule } from './navigation';
import { paths } from './icons';
import { api, setCsrf } from './api';
import type { Session } from './api';
import { ChatPage, ModelsPage, MemoryPage, SystemPage } from './pages';
import { StartupGate } from './StartupGate';
import '../styles/app.css';
import '../styles/business.css';

export function Icon({name}: {name: string}) { return <span className="icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" dangerouslySetInnerHTML={{ __html: paths[name] || paths.chat }} /></span>; }
function read(type: 'localStorage' | 'sessionStorage', key: string) { try { return window[type].getItem(key); } catch { return null; } }
function write(type: 'localStorage' | 'sessionStorage', key: string, value: string) { try { window[type].setItem(key, value); } catch { /* usable without browser storage */ } }

function App() {
  const [hash, setHash] = useState(location.hash || '#/chat/conversation');
  const route = routes.find(r => r.href === hash.split('?')[0]);
  const [session, setSession] = useState<Session | null>(null);
  const [entered, setEntered] = useState(false);
  const [gate, setGate] = useState(0);
  const gateActive = useRef(true);
  const [authError, setAuthError] = useState('');
  const [collapsed, collapse] = useState(read('localStorage', 'lingxi.sidebar.collapsed') === 'true');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const search = useRef<HTMLDialogElement>(null), drawer = useRef<HTMLDialogElement>(null), profile = useRef<HTMLDialogElement>(null);
  const searchInput = useRef<HTMLInputElement>(null), main = useRef<HTMLElement>(null);
  const matches = routes.filter(r => `${r.label} ${r.module.label} ${r.keywords} ${r.description}`.toLowerCase().includes(query.toLowerCase())).slice(0, 25);
  function startGate() {
    gateActive.current = true; setEntered(false); setAuthError('');
    search.current?.close(); drawer.current?.close(); profile.current?.close();
    setGate(old => old+1);
  }
  useEffect(() => {
    const logout = () => {if (gateActive.current) return;setSession(null);setCsrf('');startGate();};
    window.addEventListener('lingxi:unauthorized',logout);
    return () => window.removeEventListener('lingxi:unauthorized',logout);
  }, []);
  useEffect(() => {
    const change = () => {setHash(location.hash); search.current?.close(); drawer.current?.close(); if (!gateActive.current) main.current?.focus({ preventScroll: true });};
    window.addEventListener('hashchange', change); if (!location.hash) history.replaceState(null, '', '#/chat/conversation');
    return () => window.removeEventListener('hashchange', change);
  }, []);
  useEffect(() => {document.title = `灵犀 · ${route?.label || '页面未找到'}`;}, [route]);
  function openSearch() {if (gateActive.current) return; drawer.current?.close(); setQuery(''); search.current?.showModal(); searchInput.current?.focus();}
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {e.preventDefault(); openSearch();}
      if (search.current?.open && ['ArrowDown', 'ArrowUp'].includes(e.key)) {
        const links = Array.from(search.current.querySelectorAll<HTMLAnchorElement>('.search-result')); if (!links.length) return;
        e.preventDefault(); const index = links.indexOf(document.activeElement as HTMLAnchorElement);
        links[(index + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length]?.focus();
      }
      if (search.current?.open && e.key === 'Enter' && document.activeElement === searchInput.current) {e.preventDefault(); search.current.querySelector<HTMLAnchorElement>('.search-result')?.click();}
    };
    document.addEventListener('keydown', key); return () => document.removeEventListener('keydown', key);
  }, []);
  function navModule(m: NavModule, prefix: string) {
    const active = route?.module.id === m.id, open = expanded.has(m.id);
    return <div className={`nav-module ${active ? 'is-active' : ''}`} key={m.id}>
      <div className="nav-module-header"><a className="nav-module-link" title={m.label} href={`#/${m.id}/${m.pages[0].id}`} aria-current={active ? 'true' : undefined}><Icon name={m.icon}/><span className="nav-module-label">{m.label}</span></a>
      {m.pages.length > 1 && <button className="nav-expand" aria-label={`${open ? '收起' : '展开'}${m.label}`} aria-expanded={open} aria-controls={`${prefix}-${m.id}`} onClick={() => setExpanded(old => {const next = new Set(old); if (next.has(m.id)) next.delete(m.id); else next.add(m.id); return next;})}><Icon name="chevron"/></button>}</div>
      <ul className="nav-children" id={`${prefix}-${m.id}`} hidden={!open}>{m.pages.map(p => <li key={p.id}><a className="nav-child-link" href={`#/${m.id}/${p.id}`} aria-current={route?.module.id === m.id && route.id === p.id ? 'page' : undefined}>{p.label}</a></li>)}</ul>
    </div>;
  }
  function navigation(prefix: string) {return <><nav className="main-navigation" aria-label="主导航">{groups.map(g => <section className="nav-group" key={g.id}><h2 className="nav-group-title">{g.label}</h2>{modules.filter(m => m.group.id === g.id).map(m => navModule(m, prefix))}</section>)}</nav><div className="store-navigation">{store.map(m => navModule(m, prefix))}</div></>;}
  const content = !route ? <div className="empty-state"><h2>页面未找到</h2><a className="route-recovery" href="#/chat/conversation">返回对话</a></div> : route.module.id === 'chat' ? <ChatPage hash={hash}/> : route.module.id === 'models' ? <ModelsPage page={route.id}/> : route.module.id === 'memory' ? <MemoryPage page={route.id} session={session!} onSession={setSession}/> : route.module.id === 'settings' && ['overview', 'security'].includes(route.id) ? <SystemPage page={route.id}/> : <div className="empty-state"><div className="empty-emblem"><span className="emblem-orbit"/><img src="/assets/lingxi.svg" alt="" width="64" height="64"/></div><h2>{route.label}</h2><p>{route.description}</p><span className="status-badge">待开发</span></div>;
  return <>
    <a className="skip-link" href="#main-content" onClick={e => {e.preventDefault(); main.current?.focus();}}>跳至主要内容</a>
    <div className={`app-shell ${collapsed ? 'is-collapsed' : ''}`} id="app-shell" hidden={!entered} inert={!entered}>
      <aside className="sidebar" aria-label="工作空间侧栏"><a className="brand" href="#/chat/conversation"><img src="/assets/lingxi.svg" alt="" width="40" height="40"/><span className="brand-copy"><strong>灵犀<span className="brand-dot">.</span></strong><small>与你并肩，一点就通</small></span></a><div className="workspace-label"><span className="workspace-monogram">L</span><span>个人工作空间</span></div>{navigation('desktop')}<button className="profile-button" onClick={() => profile.current?.showModal()}><span className="profile-avatar">灵</span><span className="profile-copy"><strong>{session?.username || '个人空间'}</strong><small>Lingxi Workspace</small></span><Icon name="more"/></button></aside>
      <div className="workspace"><header className="topbar"><button className="icon-button desktop-toggle" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} aria-expanded={!collapsed} onClick={() => {collapse(!collapsed); write('localStorage', 'lingxi.sidebar.collapsed', String(!collapsed));}}><Icon name="panel"/></button><button className="icon-button mobile-toggle" aria-label="打开导航" onClick={() => drawer.current?.showModal()}><Icon name="menu"/></button><div className="breadcrumb"><span>{route?.module.label || '页面导航'}</span><Icon name="chevron"/><span className="breadcrumb-page">{route?.label || '未知页面'}</span></div><div className="topbar-actions"><button className="search-trigger" onClick={openSearch} aria-label="搜索菜单"><Icon name="search"/><span className="search-label">搜索菜单</span><kbd className="search-shortcut">Ctrl K</kbd></button><button className="icon-button" id="replay-boot" aria-label="重播开机动画" onClick={() => startGate()}><Icon name="sparkles"/></button><a className="icon-button settings-link" href="#/settings/overview" aria-label="系统概览"><Icon name="settings"/></a></div></header>
      <main className="main-content" id="main-content" tabIndex={-1} aria-labelledby="page-title" ref={main}><h1 className="sr-only" id="page-title">{route ? `${route.module.label} · ${route.label}` : '页面未找到'}</h1><nav className="section-navigation" id="section-navigation" aria-label="当前模块页面">{route?.module.pages.map(p => <a className="section-link" key={p.id} href={`#/${route.module.id}/${p.id}`} aria-current={route.id === p.id ? 'page' : undefined}>{p.label}</a>)}</nav><section className="page-slot business-slot" id="page-slot">{entered && session ? content : null}</section><footer className="workspace-footer"><span>与你并肩，从每一次对话开始。</span><span>首批实现 · v0.2.0</span></footer></main></div>
    </div>
    <dialog className="navigation-drawer" ref={drawer} aria-label="灵犀导航"><div className="drawer-header"><strong>灵犀导航</strong><button className="icon-button" aria-label="关闭导航" onClick={() => drawer.current?.close()}><Icon name="close"/></button></div>{navigation('mobile')}</dialog>
    <dialog className="search-dialog" ref={search} aria-label="搜索菜单"><div className="search-dialog-header"><Icon name="search"/><input aria-label="搜索菜单" value={query} ref={searchInput} onChange={e => setQuery(e.target.value)} placeholder="搜索菜单或功能…"/><button className="icon-button" aria-label="关闭搜索" onClick={() => search.current?.close()}><Icon name="close"/></button></div><div className="search-results">{matches.map(r => <a className="search-result" key={r.href} href={r.href} onClick={() => search.current?.close()}><Icon name={r.module.icon}/><span className="search-result-copy"><strong>{r.label}</strong><small>{r.module.group.label} / {r.module.label}</small></span></a>)}{!matches.length && <p className="search-empty">没有找到相关页面</p>}</div><div className="search-dialog-footer" role="status">找到 {matches.length} 个页面 · ↑↓ 选择，Enter 进入</div></dialog>
    <dialog className="profile-dialog" ref={profile} aria-label="个人空间"><div className="dialog-heading"><h2>个人空间</h2><button className="icon-button" aria-label="关闭个人空间" onClick={() => profile.current?.close()}><Icon name="close"/></button></div><p>{session ? `已登录：${session.username}` : '请先登录后使用业务功能'}</p>{authError && <p role="alert" className="error">{authError}</p>}{session && <button className="button" onClick={() => void api('/api/auth/logout', 'POST').then(() => {setSession(null); setCsrf(''); startGate();}).catch(e => setAuthError(e.message))}>退出登录</button>}</dialog>
    {!entered && <StartupGate key={gate} onReady={s => {
      setSession(s); setCsrf(s.csrf); gateActive.current = false; setEntered(true);
      requestAnimationFrame(() => main.current?.focus({preventScroll:true}));
    }}/>}<p className="sr-only" role="status" aria-live="polite">{route?.label}</p>
  </>;
}

createRoot(document.getElementById('root')!).render(<App/>);
