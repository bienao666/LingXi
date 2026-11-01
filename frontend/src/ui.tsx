import React, {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api';

export function useResource<T>(url: string) {
  const [data,setData]=useState<T>(),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  const loadedUrl=useRef(url);
  const reload=useCallback(()=>setRevision(x=>x+1),[]);
  useEffect(()=>{
    let active=true; const controller=new AbortController();
    if(loadedUrl.current!==url)setData(undefined);
    loadedUrl.current=url;setError('');
    void api<T>(url,'GET',undefined,{signal:controller.signal}).then(value=>{if(active)setData(value);})
      .catch(e=>{if(active)setError((e as Error).message);});
    return ()=>{active=false;controller.abort();};
  },[url,revision]);
  return {data,error,reload};
}

export function Modal({title,close,children,dismissible=true,returnFocus}:{title:string;close():void;children:React.ReactNode;dismissible?:boolean;returnFocus?:HTMLElement|null|(()=>HTMLElement|null)}) {
  const ref=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const dialog=ref.current;const prior=returnFocus===undefined?document.activeElement as HTMLElement|null:returnFocus;
    dialog?.showModal();return ()=>{dialog?.close();const target=typeof returnFocus==='function'?returnFocus():prior;if(target instanceof HTMLElement&&target.isConnected)target.focus();};},[]);
  return <dialog ref={ref} className="business-dialog" onClose={e=>{if(e.target===e.currentTarget)close();}} onCancel={e=>{e.stopPropagation();if(!dismissible)e.preventDefault();}} onKeyDown={e=>{
    if(e.key!=='Tab'||(e.target as Element).closest('dialog')!==e.currentTarget)return;
    const controls=Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])'))
      .filter(el=>el.closest('dialog')===e.currentTarget&&el.tabIndex>=0&&el.getClientRects().length>0);
    const first=controls[0],last=controls.at(-1);
    if(!first){e.preventDefault();return;}
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }} aria-label={title}>
    <div className="dialog-heading"><h2>{title}</h2><button type="button" className="button" disabled={!dismissible} onClick={close} aria-label={`关闭${title}`}>关闭</button></div>{children}
  </dialog>;
}

export function ErrorNotice({error}:{error?:string|null}) {return error?<p role="alert" className="error">{error}</p>:null;}
export const formatTime=(time:number)=>new Date(time*1000).toLocaleString('zh-CN');
