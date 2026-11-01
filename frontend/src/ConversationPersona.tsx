import React, {useEffect, useState} from 'react';
import {api} from './api';
import type {AssistantProfile, PersonaSelection} from './api';
import {ErrorNotice, useResource} from './ui';

export function ConversationPersona({conversationId,disabled}:{conversationId:string;disabled:boolean}) {
  const personas=useResource<AssistantProfile[]>('/api/personas'),selected=useResource<PersonaSelection>(`/api/conversations/${conversationId}/persona`);
  const [value,setValue]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  useEffect(()=>{if(selected.data)setValue(selected.data.inherit_default?'@workspace':selected.data.persona_id||'');},[selected.data]);
  useEffect(()=>{setMessage('');setError('');},[conversationId]);
  return <form className="persona-selection" onSubmit={e=>{
    e.preventDefault();if(busy||disabled||!selected.data)return;setBusy(true);setError('');setMessage('');
    void api(`/api/conversations/${conversationId}/persona`,'PUT',{persona_id:value==='@workspace'?null:value||null,...(value==='@workspace'?{inherit_default:true}:{}),version:selected.data.version})
    .then(()=>{selected.reload();setMessage('会话人格已更新，下个回合生效。');}).catch(e=>setError(e.message)).finally(()=>setBusy(false));
  }}><label>会话人格<select value={value} disabled={busy||disabled||!selected.data||!personas.data} onChange={e=>{setValue(e.target.value);setMessage('');}}><option value="@workspace">继承工作空间</option><option value="">内置 · 并肩伙伴</option>{personas.data?.filter(p=>p.enabled||p.id===selected.data?.persona_id).map(p=><option value={p.id} key={p.id} disabled={!p.enabled}>{p.name} · v{p.version}{p.enabled?'':'（已停用，请切换）'}</option>)}</select></label><button className="button" disabled={busy||disabled||!selected.data||!personas.data}>{busy?'正在保存':'切换人格'}</button><small className="muted">{disabled?'当前回合使用已固定的人格，结束后可切换。':`当前人格：${selected.data?.persona_name||'读取中'}；切换保留同一套对话与长期记忆。`}</small><ErrorNotice error={error||selected.error||personas.error}/>{message&&<p role="status">{message}</p>}</form>;
}
