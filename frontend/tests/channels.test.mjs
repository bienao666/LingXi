import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
import {Window} from 'happy-dom';
import {act,createElement} from 'react';
const require=createRequire(import.meta.url),url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
function compile(file,imports={}){let code=ts.transpileModule(readFileSync(new URL(file,import.meta.url),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;for(const [name,value]of Object.entries({react:pathToFileURL(require.resolve('react')).href,'react/jsx-runtime':pathToFileURL(require.resolve('react/jsx-runtime')).href,...imports}))code=code.replaceAll(`from '${name}'`,`from '${value}'`).replaceAll(`from "${name}"`,`from "${value}"`);return url(code);}
const window=new Window({url:'http://localhost/#/channels/instances'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const agentTools=compile('../src/AgentTools.tsx',{'./api':api,'./ui':ui});
const channelTasks=compile('../src/ChannelTasks.tsx',{'./api':api,'./ui':ui,'./AgentTools':agentTools});
const {ChannelPages}=await import(compile('../src/ChannelPages.tsx',{'./api':api,'./ui':ui,'./ChannelTasks':channelTasks}));
const channel={id:'channel-one',name:'测试 QQ',bot_id:'12345678',version:3,enabled:true,rules:{private_enabled:true,allow_unbound:false,group_enabled:false,group_ids:[],command:'/灵犀'},connection_state:'offline',login_state:'unknown',last_seen:null,protocol_version:'',capabilities:{},gateway_path:'/gateway/onebot/v11/ws/channel-one',token_mask:'••••••••'};
const link={id:'link-one',channel_id:channel.id,external_user_id:'87654321',version:2,status:'proved',allow_memory:true,expires_at:Date.now()/1000+600};
const response=(value,status=200)=>new Response(JSON.stringify(value),{status});
async function mount(t,page,handler){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(uri,options)=>String(uri).startsWith('/api/channel-tasks?')?response([]):handler(uri,options);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(ChannelPages,{page})));}
async function click(label,scope=document){const b=[...scope.querySelectorAll('button')].find(e=>e.textContent===label);assert.ok(b,label);await act(async()=>b.click());}
async function submit(){await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));}

test('new instance saves only after named preview and preserves token draft in memory',async t=>{
 const writes=[];await mount(t,'instances',(uri,o)=>{if(o.method!=='GET'){writes.push(JSON.parse(o.body));return response(channel);}return response([]);});
 await click('新增实例');for(const [field,value] of Object.entries({name:'新账号',bot_id:'12345678',access_token:'private-token-only-in-memory-123456789'}))document.querySelector(`[name=${field}]`).value=value;
 await submit();assert.equal(writes.length,0);assert.equal(document.activeElement.textContent,'确认保存');assert.match(document.querySelector('dialog').textContent,/新账号/);await click('继续编辑');assert.equal(document.querySelector('[name=name]').value,'新账号');assert.equal(document.querySelector('[name=access_token]').type,'password');await submit();await click('确认保存');assert.equal(writes.length,1);assert.equal(writes[0].bot_id,'12345678');assert.equal(window.localStorage.length,0);
});
test('instance version conflict remains visible and never silently overwrites',async t=>{
 const writes=[];await mount(t,'instances',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({detail:'配置已经变化，请重新加载后编辑'},409);}return response([channel]);});
 await click('编辑实例');document.querySelector('[name=name]').value='修改后的名称';await submit();await click('确认保存');assert.equal(writes[0].version,3);assert.match(document.querySelector('dialog').textContent,/重新加载/);await click('继续编辑');assert.equal(document.querySelector('[name=name]').value,'修改后的名称');
});
test('group rule changes show scope confirmation and real version',async t=>{
 const writes=[];await mount(t,'rules',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...channel,version:4});}return response([channel]);});
 await click('编辑规则');document.querySelector('[name=group_ids]').value='12345,67890';await act(async()=>document.querySelector('[name=group_enabled]').click());await submit();assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/群聊.*开启/);await click('确认保存');assert.deepEqual(writes[0].group_ids,['12345','67890']);assert.equal(writes[0].version,3);
});
test('identity confirmation explicitly names QQ and private memory scope',async t=>{
 const writes=[];await mount(t,'identities',(uri,o)=>{if(o.method==='POST'){writes.push({uri,body:JSON.parse(o.body)});return response({...link,status:'active'});}return response(uri==='/api/channels'?[channel]:[link]);});
 await click('确认绑定');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/87654321/);assert.match(document.querySelector('dialog').textContent,/允许检索与保存个人记忆/);await click('确认绑定',document.querySelector('dialog'));assert.equal(writes[0].uri,'/api/identity-links/link-one/confirm');assert.equal(writes[0].body.version,2);
});
test('identity revocation requires confirmation and keeps history separate',async t=>{
 const writes=[];await mount(t,'identities',(uri,o)=>{if(o.method==='POST'){writes.push(uri);return response({...link,status:'revoked'});}return response(uri==='/api/channels'?[channel]:[{...link,status:'active'}]);});
 await click('撤销绑定');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/原个人记忆保留/);await click('确认撤销');assert.deepEqual(writes,['/api/identity-links/link-one/revoke']);
});
test('unknown send review cancels waiting only after confirmation and never retries',async t=>{
 const writes=[];await mount(t,'instances',(uri,o)=>{if(o.method==='POST'){writes.push({uri,body:JSON.parse(o.body)});return response({});}return response(uri==='/api/channels'?[channel]:[{id:'in-one',sender_id:'87654321',target_key:'private:87654321',external_message_id:'1',status:'unknown',created_at:1,error:null,deliveries:[{id:'delivery-one',part:0,status:'unknown',error:'连接中断',reviewed_at:null,protocol_message_id:null}]}]);});
 await click('收发记录');assert.match(document.querySelector('dialog').textContent,/不等于送达或已读/);await click('结束等待');assert.equal(writes.length,0);const dialogs=[...document.querySelectorAll('dialog')];assert.match(dialogs.at(-1).textContent,/不会重发/);await click('确认结束',dialogs.at(-1));assert.deepEqual(writes,[{uri:'/api/channels/channel-one/deliveries/delivery-one/review',body:{acknowledgement:'checked_no_resend'}}]);
});

test('attachment download scope waits for preview and requires a domain before enabling',async t=>{
 const writes=[];await mount(t,'rules',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...channel,version:4});}return response([channel]);});
 await click('编辑规则');await act(async()=>document.querySelector('[name=attachments_enabled]').click());await submit();assert.match(document.querySelector('dialog').textContent,/填写明确允许/);assert.equal(writes.length,0);
 document.querySelector('[name=attachment_hosts]').value='multimedia.nt.qq.com.cn\ngchat.qpic.cn';await submit();assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/HTTPS 域名/);assert.match(document.querySelector('dialog').textContent,/不下载本地路径/);await click('继续编辑');assert.equal(document.querySelector('[name=attachments_enabled]').checked,true);assert.equal(document.querySelector('[name=attachment_hosts]').value,'multimedia.nt.qq.com.cn\ngchat.qpic.cn');await submit();await click('确认保存');assert.equal(writes[0].attachments_enabled,true);assert.deepEqual(writes[0].attachment_hosts,['multimedia.nt.qq.com.cn','gchat.qpic.cn']);assert.equal(writes[0].version,3);
});

test('attachment records expose metadata and errors without signed URLs or filenames',async t=>{
 await mount(t,'instances',(uri,o)=>response(uri==='/api/channels'?[channel]:[{id:'in-one',sender_id:'87654321',target_key:'private:87654321',external_message_id:'2',status:'ingesting',created_at:1,error:null,attachments:[{id:'a1',kind:'image',status:'ready',size:1024,error:null},{id:'a2',kind:'file',status:'failed',size:0,error:'下载已过期'}],deliveries:[]}]));
 await click('收发记录');const text=document.querySelector('dialog').textContent;assert.match(text,/正在接收附件/);assert.match(text,/图片 · 解析完成/);assert.match(text,/1.0 KiB/);assert.match(text,/下载已过期/);assert.doesNotMatch(text,/signature|https:\/\//);
});

test('document lookup explains constrained scope and shows resolving state',async t=>{
 await mount(t,'instances',(uri,o)=>response(uri==='/api/channels'?[channel]:[{id:'document-input',sender_id:'87654321',target_key:'private:87654321',external_message_id:'9',status:'ingesting',created_at:1,error:null,attachments:[{id:'doc1',kind:'file',status:'resolving',size:0,error:null}],deliveries:[]}]));
 assert.match(document.body.textContent,/文件 ID 通过原连接取址/);
 assert.match(document.body.textContent,/群上传与离线文件通知不自动读取/);
 await click('收发记录');assert.match(document.querySelector('dialog').textContent,/文档 · 正在取址/);
 assert.doesNotMatch(document.querySelector('dialog').textContent,/document_state|encrypted_url|file_id|base64/);
});

test('QQ voice scope is default off and requires explicit preview before saving',async t=>{
 const writes=[];await mount(t,'rules',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...channel,version:4});}return response([channel]);});
 await click('编辑规则');assert.equal(document.querySelector('[name=voice_enabled]').checked,false);
 await act(async()=>document.querySelector('[name=voice_enabled]').click());await submit();assert.equal(writes.length,0);
 assert.match(document.querySelector('dialog').textContent,/QQ 语音接收开启/);assert.match(document.querySelector('dialog').textContent,/不自动写入长期记忆/);
 await click('继续编辑');assert.equal(document.querySelector('[name=voice_enabled]').checked,true);await submit();await click('确认保存');
 assert.equal(writes.length,1);assert.equal(writes[0].voice_enabled,true);assert.equal(writes[0].version,3);
});

test('QQ voice conversion and ASR records show actual stage without exposing waveform or transcript',async t=>{
 await mount(t,'instances',(uri,o)=>response(uri==='/api/channels'?[channel]:[{id:'in-voice',sender_id:'87654321',target_key:'private:87654321',external_message_id:'3',status:'ingesting',created_at:1,error:null,attachments:[{id:'v1',kind:'record',status:'transcribing',size:64044,error:null}],deliveries:[]}]));
 await click('收发记录');assert.match(document.querySelector('dialog').textContent,/语音 · 正在识别/);assert.doesNotMatch(document.querySelector('dialog').textContent,/base64|transcript|file:\/\//);
});


test('QQ speech replies have independent explicit consent and bounded fallback preview',async t=>{
 const writes=[];await mount(t,'rules',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...channel,version:4});}return response([channel]);});
 await click('编辑规则');assert.equal(document.querySelector('[name=voice_reply_enabled]').checked,false);
 await act(async()=>document.querySelector('[name=voice_reply_enabled]').click());await submit();assert.equal(writes.length,0);
 assert.match(document.querySelector('dialog').textContent,/QQ 语音回复开启/);assert.match(document.querySelector('dialog').textContent,/不用于主动消息/);
 await click('继续编辑');assert.equal(document.querySelector('[name=voice_reply_enabled]').checked,true);assert.equal(document.querySelector('[name=voice_enabled]').checked,false);
 await submit();await click('确认保存');assert.equal(writes[0].voice_reply_enabled,true);assert.equal(writes[0].voice_enabled,false);
});

test('speech records distinguish synthesis failure from protocol acceptance',async t=>{
 await mount(t,'instances',(uri,o)=>response(uri==='/api/channels'?[channel]:[{id:'in-one',sender_id:'87654321',target_key:'private:87654321',external_message_id:'4',status:'completed',created_at:1,error:null,attachments:[],deliveries:[{id:'d1',part:0,kind:'text',status:'accepted_by_protocol',speech:{},error:null,reviewed_at:null,protocol_message_id:'-1'},{id:'d2',part:1,kind:'record',status:'cancelled',speech:{phase:'failed',model:'tts-model',voice:'alloy'},error:'合成失败，文字仍保留',reviewed_at:null,protocol_message_id:null}]}]));
 await click('收发记录');const text=document.querySelector('dialog').textContent;assert.match(text,/文字 · 协议已接受/);assert.match(text,/语音 · 已经停止/);assert.match(text,/合成状态：合成失败/);assert.match(text,/音色 alloy/);assert.doesNotMatch(text,/base64|encrypted_audio|已听/);
});


test('long answer files require explicit rule preview and retain independent consent',async t=>{
 const writes=[];await mount(t,'rules',(uri,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({...channel,version:4});}return response([channel]);});
 await click('编辑规则');assert.equal(document.querySelector('[name=file_reply_enabled]').checked,false);
 await act(async()=>document.querySelector('[name=file_reply_enabled]').click());await submit();assert.equal(writes.length,0);
 assert.match(document.querySelector('dialog').textContent,/长文文件回复开启/);assert.match(document.querySelector('dialog').textContent,/群内文件对该群可见/);
 await click('继续编辑');assert.equal(document.querySelector('[name=file_reply_enabled]').checked,true);assert.equal(document.querySelector('[name=voice_reply_enabled]').checked,false);
 await submit();await click('确认保存');assert.equal(writes[0].file_reply_enabled,true);assert.equal(writes[0].attachments_enabled,false);
});

test('file records expose size and file receipt without claiming user download',async t=>{
 await mount(t,'instances',(uri,o)=>response(uri==='/api/channels'?[channel]:[{id:'in-file',sender_id:'87654321',target_key:'private:87654321',external_message_id:'8',status:'completed',created_at:1,error:null,attachments:[],deliveries:[{id:'df',part:1,kind:'file',status:'accepted_by_protocol',file_info:{name:'lingxi-answer-synthetic.md',bytes:20480},protocol_file_id:'/synthetic-uuid',speech:{},error:null,reviewed_at:null,protocol_message_id:null}]}]));
 await click('收发记录');const text=document.querySelector('dialog').textContent;assert.match(text,/文件 · 协议已接受/);assert.match(text,/协议文件 \/synthetic-uuid/);assert.match(text,/20.0 KiB/);assert.match(text,/不等于收件人已下载/);assert.doesNotMatch(text,/base64|digest|文件已送达/);
});
