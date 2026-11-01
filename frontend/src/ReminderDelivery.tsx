import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {ContactTargetOption, Reminder} from './api';
import type {components} from './api.generated';
import {ErrorNotice, formatTime, Modal, useResource} from './ui';

type Delivery = Required<components['schemas']['ReminderDeliveryView']>;
type Preview = Omit<components['schemas']['DeliveryPreview'],'after'> & {after:Delivery};
type Target = components['schemas']['ContactTarget'];
const statuses:Record<string,string>={station_only:'仅存站内',configured:'等待到期',pending:'等待发送',sending:'等待回执',accepted_by_protocol:'协议接受',unknown:'结果未知',failed:'协议失败',cancelled:'投递停止',expired:'等待过期'};
const targetKey=(t:Target)=>JSON.stringify(t.kind==='qq'?{kind:'qq',binding_id:t.binding_id,binding_version:t.binding_version,channel_version:t.channel_version}:{kind:'web'});

export function ReminderDelivery({row,close}:{row:Reminder;close():void}) {
  const path='/api/reminders/'+row.id+'/delivery';
  const current=useResource<Delivery>(path),options=useResource<ContactTargetOption[]>('/api/reminder-targets');
  const [target,setTarget]=useState<Target>({kind:'web'}),[hours,setHours]=useState(1),[preview,setPreview]=useState<Preview>(),[approved,setApproved]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[confirmAction,setConfirmAction]=useState<'cancel_without_resend'|'checked_no_resend'>();
  const [clock,setClock]=useState(Date.now()/1000);
  const pending=useRef(false),heading=useRef<HTMLHeadingElement>(null),previewButton=useRef<HTMLButtonElement>(null);
  useEffect(()=>{if(current.data){setTarget(current.data.target);setHours(current.data.expires_at?Math.min(24,Math.max(1,Math.round((current.data.expires_at-row.due_at)/3600))):1);}},[current.data]);
  useEffect(()=>{if(!preview)return;setClock(Date.now()/1000);const t=setInterval(()=>setClock(Date.now()/1000),1000);heading.current?.focus();return()=>clearInterval(t);},[preview]);
  useEffect(()=>{if(notice||confirmAction)heading.current?.focus();},[notice,confirmAction]);
  const chosen=options.data?.find(item=>targetKey(item.target)===targetKey(target));
  const invalid=target.kind==='qq'&&!chosen;
  async function run(work:()=>Promise<unknown>,message:string){
    if(pending.current)return;pending.current=true;setBusy(true);setError('');setNotice('');
    try{await work();setPreview(undefined);setApproved(false);setConfirmAction(undefined);setNotice(message);current.reload();}
    catch(e){setError((e as Error).message);}finally{pending.current=false;setBusy(false);}
  }
  return <Modal title="提醒投递" close={close} dismissible={!busy}><h3 ref={heading} tabIndex={-1}>{row.title}</h3>
    <ErrorNotice error={error||current.error||options.error}/>{notice&&<p role="status">{notice}</p>}
    {!current.data&&!current.error&&<p role="status">正在读取投递状态…</p>}
    {current.data&&<><p><span className="status-badge">{statuses[current.data.status]||current.data.status}</span> · {current.data.target_label}</p>
      <p className="muted">站内触发和 QQ 投递分别记录。协议接受不代表送达或已读；没有 QQ 回执不改变站内提醒状态，不自动重发或切换接收者。</p>
      {current.data.sent_at&&<p>开始发送：{formatTime(current.data.sent_at)}</p>}{current.data.protocol_message_id&&<p className="wrap">协议消息：{current.data.protocol_message_id}</p>}
      {current.data.reviewed_at&&<p>已核对结束等待：{formatTime(current.data.reviewed_at)}，未重发。</p>}<ErrorNotice error={current.data.error}/>
      {preview?<><h4>核对提醒</h4><p className="memory-content">{preview.title}</p><p className="memory-content">{preview.content||'没有补充内容'}</p><p>到期时间：{formatTime(preview.due_at)} · 原时区 {preview.timezone}</p><p>接收目标：{preview.after.target_label}</p><p>{preview.after.target.kind==='qq'?`到期后最迟等待至 ${formatTime(preview.after.expires_at!)}，离线到期后不补发。`:'仅保留站内触发，不再发送 QQ。'}</p>
        <p>这是你指定的一次性提醒，按设定时间处理，独立于普通主动交流的安静时段和每日额度。修改提醒内容、时间、平台或身份授权后，QQ 投递设置失效，需重新确认。不会发送其他记忆或聊天内容。</p>
        <label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已核对这条提醒的完整内容、接收目标和等待期限</label>
        {clock>=preview.expires_at&&<p role="status">预览已过期，请返回重新核对。</p>}
        <div className="actions"><button className="button" disabled={busy} onClick={()=>{setPreview(undefined);setApproved(false);setError('');queueMicrotask(()=>previewButton.current?.focus());}}>继续编辑</button><button className="button primary" disabled={busy||!approved||clock>=preview.expires_at} onClick={()=>void run(()=>api(path+'/confirm','POST',{token:preview.token,acknowledgement:preview.after.target.kind==='qq'?'send_this_reminder_to_selected_qq':'store_this_reminder_only'}),'投递设置已保存；尚未发送提醒。')}>{busy?'正在保存':'确认投递'}</button></div>
      </>:confirmAction?<><p>{confirmAction==='checked_no_resend'?'请在 QQ 核对实际情况。结束等待不修改协议回执，不把结果标为送达，也不重发。':'停止这条 QQ 提醒投递，站内提醒继续。已经开始的发送无法撤回，结果需核对。'}</p><label className="checkbox"><input type="checkbox" checked={approved} disabled={busy} onChange={e=>setApproved(e.target.checked)}/>我已核对本次操作及影响</label><div className="actions"><button className="button" disabled={busy} onClick={()=>{setConfirmAction(undefined);setApproved(false);}}>返回状态</button><button className="button primary" disabled={busy||!approved} onClick={()=>void run(()=>api(path+'/action','POST',{version:current.data!.version,acknowledgement:confirmAction}),'投递操作已保存，未重发提醒。')}>{busy?'正在保存':confirmAction==='checked_no_resend'?'结束等待':'停止投递'}</button></div></>:
      <>{row.status==='scheduled'&&row.due_at>clock&&<form className="form-stack" onSubmit={e=>{e.preventDefault();if(pending.current||invalid||!current.data)return;pending.current=true;setBusy(true);setError('');setNotice('');void api<Preview>(path+'/previews','POST',{reminder_version:row.version,delivery_version:current.data.version,target,wait_hours:hours}).then(value=>{setApproved(false);setPreview(value);}).catch(e=>setError(e.message)).finally(()=>{pending.current=false;setBusy(false);});}}>
        <label>接收渠道<select aria-label="提醒接收渠道" value={targetKey(target)} disabled={busy||!options.data} onChange={e=>{setTarget(JSON.parse(e.target.value));setError('');}}><option value={targetKey({kind:'web'})}>仅存站内</option>{invalid&&<option value={targetKey(target)}>原 QQ 授权已变化，请重新选择</option>}{options.data?.map(option=><option key={targetKey(option.target)} value={targetKey(option.target)}>{option.label} · {option.online?'当前在线':'当前离线'}</option>)}</select></label>
        <p className="muted">仅展示已确认且允许个人资料的 QQ 私聊绑定。到期正文发送须逐条确认；群聊不可选。没有可用账号时，请先在平台接入完成身份绑定。</p>
        {target.kind==='qq'&&<label>离线等待<select aria-label="离线等待期限" value={hours} disabled={busy} onChange={e=>setHours(Number(e.target.value))}>{Array.from({length:24},(_,i)=>i+1).map(h=><option key={h} value={h}>{h} 小时</option>)}</select><small>从提醒到期时间起算；超过期限放弃 QQ 发送，保留站内记录。</small></label>}
        <button ref={previewButton} className="button primary" disabled={busy||invalid||!options.data}>{busy?'正在预览':'预览投递'}</button>
      </form>}
      <div className="actions"><button className="button" disabled={busy} onClick={()=>{setError('');current.reload();options.reload();}}>刷新状态</button>{['configured','pending','sending'].includes(current.data.status)&&<button className="button" disabled={busy} onClick={()=>{setConfirmAction('cancel_without_resend');setApproved(false);setError('');}}>停止投递</button>}{['unknown','failed'].includes(current.data.status)&&!current.data.reviewed_at&&<button className="button" disabled={busy} onClick={()=>{setConfirmAction('checked_no_resend');setApproved(false);setError('');}}>核对投递</button>}</div></>}
    </>}
  </Modal>;
}
