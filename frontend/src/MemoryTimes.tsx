import React from 'react';

export const memoryTimeFields = [['event_at','事件时间'],['valid_from','生效时间'],['valid_to','结束时间']] as const;
export function MemoryTimeDetails({value}:{value:Record<string,unknown>}) {
  const fields=memoryTimeFields.filter(([key])=>typeof value[key]==='string'&&value[key]);
  return fields.length?<div className="muted memory-content" aria-label="时间依据">{fields.map(([key,name])=><p key={key}>{name}：<time dateTime={/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(String(value[key]))?String(value[key]):undefined}>{String(value[key])}</time></p>)}</div>:null;
}
