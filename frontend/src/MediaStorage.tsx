import React,{useEffect,useRef,useState} from 'react';
import {api} from './api';
import type {components} from './api.generated';
import {ErrorNotice,formatTime,Modal,useResource} from './ui';

type Run=components['schemas']['StorageRunView'];
type Overview=components['schemas']['StorageOverview'];
const root='/api/admin/media-storage';
const size=(n:number|null)=>n===null?'未知':`${(n/1024**2).toLocaleString('zh-CN',{maximumFractionDigits:2})} MiB`;
const names:Record<string,string>={queued:'等待扫描',scanning:'正在扫描',review:'等待核对',expired:'预览过期',confirmed:'等待清理',cleaning:'正在清理',completed:'处理完成',partial:'部分处理',unknown:'结果待核',failed:'处理失败',cancelled:'已经隔离',pending:'尚未处理',deleting:'删除待核',removed:'已移除',missing:'已经缺失',skipped:'保留未删'};
const active=(r:Run)=>['queued','scanning','confirmed','cleaning'].includes(r.status);

export function MediaStorage(){
  const data=useResource<Overview>(root),runs=useResource<Run[]>(root+'/runs');
  const [days,setDays]=useState(2),[selected,setSelected]=useState<string>(),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(false),request=useRef(''),opener=useRef<HTMLElement|null>(null),scanButton=useRef<HTMLButtonElement>(null);
  const selectedRun=runs.data?.find(r=>r.id===selected);
  const reload=()=>{data.reload();runs.reload();};
  useEffect(()=>{if(!runs.data?.some(active))return;const timer=window.setInterval(runs.reload,3000);return()=>window.clearInterval(timer);},[runs.data,runs.reload]);
  return <section className="data-card form-stack"><div className="pane-toolbar"><h3>存储巡检</h3><button className="button" disabled={busy} onClick={reload}>刷新巡检</button></div>
    <ErrorNotice error={error||data.error||runs.error}/>
    {data.data&&<><p>附件记录 {data.data.records} 条（已删除 {data.data.deleted_records}） · 记录累计大小 {size(data.data.logical_bytes)}</p><p>运行卷可用 {size(data.data.free_bytes)} / 总量 {size(data.data.total_bytes)}{data.data.disk_status==='warning'?' · 空间不足告警':data.data.disk_status==='unavailable'?' · 容量暂不可读':''}</p></>}
    <p className="muted">记录大小包含历史及已删除记录，不等于实际磁盘占用。运行卷容量不代表数据库和备份健康。扫描不会删除文件，只检查附件专用目录，最多 10000 个条目；超出时标为部分扫描。</p>
    <p>仅可预览后清理没有任何数据库记录或已知引用的旧残留，每批最多 20 个、200 MiB。不会遍历子目录、跟随链接、读取文件内容或清理数据库、备份、服务器资料。已有记录的附件请使用闲置回收。</p>
    <form className="inline-form" onSubmit={e=>{e.preventDefault();if(pending.current)return;opener.current=scanButton.current;pending.current=true;setBusy(true);setError('');request.current ||= crypto.randomUUID();void api<Run>(root+'/scans','POST',{request_id:request.current,days}).then(r=>{request.current='';setSelected(r.id);runs.reload();}).catch(e=>setError(e.message)).finally(()=>{pending.current=false;setBusy(false);});}}>
      <label>最短保留天数<input type="number" min={2} max={365} required value={days} disabled={busy||runs.data?.some(active)} onChange={e=>{setDays(Number(e.target.value));request.current='';}}/></label>
      <button ref={scanButton} className="button" disabled={busy||!runs.data||!!runs.error||runs.data.some(active)}>{busy?'正在提交':'开始扫描'}</button>
    </form>
    {!runs.data&&!runs.error&&<p role="status">正在读取巡检记录…</p>}
    {runs.data?.length===0&&<p>尚无巡检记录。不会自动扫描或清理。</p>}
    {runs.data?.map(r=><article className="data-card" key={r.id}><div className="pane-toolbar"><strong>{names[r.status]||r.status}</strong><button className="button" onClick={e=>{opener.current=e.currentTarget;setSelected(r.id);}}>查看巡检</button></div><p>{formatTime(r.created_at)} · 保留至少 {r.days} 天 · 本批候选 {r.items.length} 个</p>{r.summary.partial&&<p>达到扫描上限，结果仅代表已检查条目。</p>}{r.error&&<p className="error">{r.error}</p>}</article>)}
    {selectedRun&&<StorageReview row={selectedRun} unavailable={!!runs.error} returnFocus={opener.current} close={()=>setSelected(undefined)} saved={reload}/>}
  </section>;
}

function StorageReview({row,unavailable,returnFocus,close,saved}:{row:Run;unavailable:boolean;returnFocus:HTMLElement|null;close():void;saved():void}){
  const [ack,setAck]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(false),acknowledgement=useRef<HTMLInputElement>(null);
  useEffect(()=>{setAck(false);if(row.status==='review')acknowledgement.current?.focus();},[row.id,row.digest,row.status]);
  return <Modal title="巡检详情" close={close} dismissible={!busy} returnFocus={returnFocus}><div className="page-stack"><p>{names[row.status]||row.status} · 最短保留 {row.days} 天</p>
    <p>已检查 {row.summary.entries??0} 个条目，识别 {row.summary.recognized??0} 个普通文件、{size(row.summary.recognized_bytes??0)}；已有附件记录 {row.summary.tracked??0} 个，其他来源引用保留 {row.summary.retained??0} 个。</p>
    {row.summary.partial&&<p className="error">部分扫描：达到 10000 个条目上限，不代表整个卷已经检查。</p>}
    {row.summary.has_more&&<p>还有未纳入本批的候选，当前不会追加删除范围。</p>}
    {row.summary.reference_limited&&<p>来源核对预算已用尽，部分旧文件未核对；清理仅限本批已核对候选。</p>}
    <p className="muted">仅列附件存储键，不读取或展示残留内容；不把残留认作用户文件。文件状态变化时间与修改时间都须达到保留期。确认和发出删除前复查来源、文件身份及变更，变化项保留。</p>
    {row.items.length>0?<ul>{row.items.map(i=><li className="wrap" key={i.key}>{i.key} · {size(i.bytes)} · 修改于 {formatTime(i.modified_at)}</li>)}</ul>:<p>本批没有可清理候选。</p>}
    {row.results.length>0&&<section aria-label="逐项结果"><h3>逐项结果</h3>{row.results.map(r=><p className="wrap" key={r.key}>{r.key} · {names[r.status]||r.status} · {size(r.bytes)}</p>)}<p>“已移除”代表本次移除了目录项，不保证实际释放同等磁盘空间；“已经缺失”不计作本次删除。未知结果不自动重试。</p></section>}
    <ErrorNotice error={error||row.error||(unavailable?'巡检记录读取失败，请关闭并刷新后再确认。':'')}/>
    {(active(row)||row.status==='review')&&<button className="button" disabled={busy||unavailable} onClick={()=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');void api(root+'/runs/'+row.id+'/cancel','POST').then(saved).catch(e=>setError(e.message)).finally(()=>{pending.current=false;setBusy(false);});}}>取消后续</button>}
    {row.status==='review'&&row.items.length>0&&<><p>预览截止 {formatTime(row.expires_at)}。这是不可撤销的磁盘残留删除，不进入回收站。只清理本批固定范围，不自动执行下一批。</p>
      <label className="checkbox"><input ref={acknowledgement} type="checkbox" disabled={busy||unavailable} checked={ack} onChange={e=>setAck(e.target.checked)}/>我已核对范围，确认永久移除这些无记录、无引用的旧残留</label>
      <button className="button danger" disabled={busy||!ack||unavailable} onClick={()=>{if(pending.current)return;pending.current=true;setBusy(true);setError('');void api(root+'/runs/'+row.id+'/confirm','POST',{digest:row.digest,acknowledgement:'remove_untracked_media_files'}).then(()=>{setAck(false);saved();}).catch(e=>setError(e.message)).finally(()=>{pending.current=false;setBusy(false);});}}>{busy?'正在确认':'确认清理'}</button></>}
    <button className="button" disabled={busy} onClick={close}>返回页面</button>
  </div></Modal>;
}
