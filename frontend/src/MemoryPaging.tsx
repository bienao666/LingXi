import React,{useCallback,useEffect,useRef,useState} from 'react';
import {api} from './api';

type RecordPage<T>={items:T[];next_cursor:string|null;as_of:number};
type Position={url:string;cursor:string;stack:string[]};

export function useMemoryRecords<T>(url:string){
  const [position,setPosition]=useState<Position>({url,cursor:'',stack:[]});
  const [loaded,setLoaded]=useState<{url:string;cursor:string;data:RecordPage<T>}>();
  const [failure,setFailure]=useState<{url:string;cursor:string;message:string}>();
  const [revision,setRevision]=useState(0),[busy,setBusy]=useState(true);
  const pending=useRef(true);
  const current=position.url===url?position:{url,cursor:'',stack:[]};
  const cursor=current.cursor;
  const error=failure?.url===url&&failure.cursor===cursor?failure.message:'';
  const data=!error&&loaded?.url===url&&loaded.cursor===cursor?loaded.data:undefined;
  const refresh=useCallback(()=>{if(!pending.current)setRevision(value=>value+1);},[]);
  useEffect(()=>{
    let active=true;const controller=new AbortController();pending.current=true;setBusy(true);setFailure(undefined);
    const query=cursor?`${url}${url.includes('?')?'&':'?'}cursor=${encodeURIComponent(cursor)}`:url;
    void api<RecordPage<T>>(query,'GET',undefined,{signal:controller.signal})
      .then(value=>{if(active)setLoaded({url,cursor,data:value});})
      .catch(e=>{if(active)setFailure({url,cursor,message:e.message});})
      .finally(()=>{if(active){pending.current=false;setBusy(false);}});
    return()=>{active=false;controller.abort();};
  },[url,cursor,revision]);
  const latest=()=>{pending.current=true;setBusy(true);setPosition({url,cursor:'',stack:[]});setRevision(value=>value+1);};
  const move=(next:boolean)=>{
    if(pending.current)return;
    if(next&&data?.next_cursor){pending.current=true;setBusy(true);setPosition({url,cursor:data.next_cursor,stack:[...current.stack,cursor]});}
    else if(!next&&current.stack.length){pending.current=true;setBusy(true);setPosition({url,cursor:current.stack.at(-1)!,stack:current.stack.slice(0,-1)});}
  };
  return {data,error,busy,refresh,latest,move,page:current.stack.length+1,hasPrevious:!!current.stack.length};
}

export function MemoryPager({records}:{records:ReturnType<typeof useMemoryRecords>}){
  return <><p role="status" aria-live="polite">{records.busy?'正在读取记录…':records.data?`第 ${records.page} 页 · 本页 ${records.data.items.length} 条`:'记录尚未加载'}</p>
    {records.error&&<div className="actions"><button className="button" disabled={records.busy} onClick={records.refresh}>重试加载</button><button className="button" disabled={records.busy} onClick={records.latest}>返回最新</button></div>}
    <nav className="actions" aria-label="记忆记录分页"><button className="button" disabled={records.busy||!records.hasPrevious} onClick={()=>records.move(false)}>上一页</button><button className="button" disabled={records.busy||!records.data?.next_cursor} onClick={()=>records.move(true)}>下一页</button><button className="button" disabled={records.busy||!records.hasPrevious} onClick={records.latest}>返回最新</button></nav>
  </>;
}
