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
const window=new Window({url:'http://localhost/#/data/files'});
Object.assign(globalThis,{window,document:window.document,HTMLElement:window.HTMLElement,FormData:window.FormData,IS_REACT_ACT_ENVIRONMENT:true,location:window.location});
const {createRoot}=await import('react-dom/client');
const api=compile('../src/api.ts'),ui=compile('../src/ui.tsx',{'./api':api});
const {MediaFiles,FileUploader,AttachmentList,VisionControl,MediaPolicySettings,MediaSettings,MediaRetentionSettings}=await import(compile('../src/MediaPages.tsx',{'./api':api,'./ui':ui,'./MediaStorage':compile('../src/MediaStorage.tsx',{'./api':api,'./ui':ui})}));
const response=(v,status=200)=>new Response(JSON.stringify(v),{status});
const item={id:'media-one',filename:'资料.md',suffix:'.md',size:200,status:'ready',version:2,error:null,cleanup_status:'not_needed',created_at:1,kind:'document',parts_count:2};
const defaultPolicy={version:1,max_files:10,max_file_mib:20,max_total_mib:50,allowed_types:['text','markdown','csv','pdf','docx','xlsx','image','qq_audio']};
async function mount(t,component,props,handler,policy=defaultPolicy,retention=true){document.body.innerHTML='<div id="root"></div>';globalThis.fetch=async(u,o)=>u==='/api/admin/media-storage'&&o.method==='GET'?response({records:0,logical_bytes:0,deleted_records:0,free_bytes:1024,total_bytes:2048,disk_status:'ready'}):u==='/api/admin/media-storage/runs'?response([]):u==='/api/media-policy'&&policy!==null?response(policy):u==='/api/admin/media-retention'&&o.method==='GET'&&retention?response({version:0,enabled:false,days:7,last_checked:0,last_marked:0}):u==='/api/admin/media-retention/runs'&&retention?response([]):handler(u,o);const root=createRoot(document.getElementById('root'));t.after(async()=>{await act(async()=>root.unmount());await window.happyDOM.abort();});await act(async()=>root.render(createElement(component,props)));}
async function click(text){const b=[...document.querySelectorAll('button')].find(v=>v.textContent===text);assert.ok(b,text);await act(async()=>b.click());}

test('attachment deletion names impact and keeps version conflict visible',async t=>{
const writes=[];await mount(t,MediaFiles,{},(u,o)=>{if(o.method==='DELETE'){writes.push(JSON.parse(o.body));return response({detail:'附件状态已变化'},409);}return response({items:[item],next_cursor:null});});
await click('删除附件');assert.equal(writes.length,0);assert.match(document.querySelector('dialog').textContent,/作废引用它的回答/);await click('确认删除');assert.deepEqual(writes,[{version:2}]);assert.match(document.querySelector('[role=alert]').textContent,/已变化/);
});
test('attachment preview keeps source locations and untrusted text as text',async t=>{
await mount(t,AttachmentList,{items:[item]},()=>response({...item,parts:[{location:'第 2 行',text:'<img src=x onerror=alert(1)>'}],notes:['正文提取']}));
await act(async()=>document.querySelector('.attachment-open').click());assert.match(document.querySelector('dialog').textContent,/第 2 行/);assert.equal(document.querySelector('dialog img'),null);assert.match(document.querySelector('dialog pre').textContent,/<img/);
});
test('deleted file never offers content download',async t=>{
await mount(t,AttachmentList,{items:[{...item,status:'deleted'}]},()=>response({...item,status:'deleted',parts:[],notes:[]}));await act(async()=>document.querySelector('.attachment-open').click());assert.equal(document.querySelector('dialog a'),null);assert.match(document.querySelector('dialog').textContent,/已删除/);
});

test('QQ audio preview labels unconfirmed ASR and downloads converted audio without autoplay',async t=>{
const audio={...item,filename:'QQ语音.wav',suffix:'.wav',kind:'audio',parts_count:1};
await mount(t,AttachmentList,{items:[audio]},()=>response({...audio,parts:[{location:'QQ语音转写（未经确认）',text:'明天怎样准备？'}],notes:['此附件保存转换后的 PCM WAV。']}));
await act(async()=>document.querySelector('.attachment-open').click());assert.match(document.querySelector('dialog').textContent,/未经确认/);assert.equal(document.querySelector('dialog a').textContent,'下载音频');assert.equal(document.querySelector('audio'),null);
});
test('vision remains unverified after a real failed response',async t=>{
await mount(t,VisionControl,{modelId:'chat',enabled:true},(u,o)=>response(o.method==='POST'?{model_id:'chat',status:'failed',error:'模型未识别图片'}:{model_id:'chat',status:'unverified'}));await click('验证视觉');assert.match(document.querySelector('[role=alert]').textContent,/未识别图片/);assert.match(document.body.textContent,/未验证/);
});
test('vision cannot start before verified text capability',async t=>{
let writes=0;await mount(t,VisionControl,{modelId:'chat',enabled:false},(u,o)=>{if(o.method==='POST')writes++;return response({model_id:'chat',status:'unverified'});});assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='验证视觉').disabled,true);assert.equal(writes,0);assert.match(document.body.textContent,/先通过对话验证/);
});
test('upload posts binary with csrf and retry reuses the same request id',async t=>{
const requests=[];await mount(t,FileUploader,{onUploaded:()=>{}},(u,o)=>{requests.push({u,o});return response({detail:'上游暂不可用'},503);});
const input=document.querySelector('input[type=file]');Object.defineProperty(input,'files',{value:[new window.File(['abc'],'笔记.md')]});await act(async()=>input.dispatchEvent(new window.Event('change',{bubbles:true})));await click('重试上传');assert.equal(requests.length,2);assert.equal(requests[0].u,requests[1].u);assert.equal(requests[0].o.headers['Content-Type'],'application/octet-stream');assert.ok(requests[0].o.body instanceof window.File);
});
test('invalid upload type is explained before any network request',async t=>{
let calls=0;await mount(t,FileUploader,{onUploaded:()=>{}},()=>{calls++;return response(item);});const input=document.querySelector('input[type=file]');Object.defineProperty(input,'files',{value:[new window.File(['abc'],'script.html')]});await act(async()=>input.dispatchEvent(new window.Event('change',{bubbles:true})));assert.equal(calls,0);assert.match(document.querySelector('[role=alert]').textContent,/尚未支持/);
});

test('closing pending deletion never strands a hidden error dialog',async t=>{
let finish;await mount(t,MediaFiles,{},(u,o)=>o.method==='DELETE'?new Promise(r=>finish=r):response({items:[item],next_cursor:null}));await click('删除附件');await click('确认删除');assert.equal(document.querySelector('dialog button.danger').disabled,true);await click('关闭');assert.equal(document.querySelector('dialog'),null);await act(async()=>finish(response({detail:'删除失败请重试'},503)));assert.match(document.querySelector('[role=alert]').textContent,/删除失败/);
});

test('lower workspace cap rejects upload before binary request',async t=>{
let writes=0;await mount(t,FileUploader,{onUploaded:()=>{}},()=>{writes++;return response(item);},{...defaultPolicy,max_file_mib:1,max_total_mib:1});
const input=document.querySelector('input[type=file]');Object.defineProperty(input,'files',{value:[new window.File([new Uint8Array(1024**2+1)],'large.txt')]});await act(async()=>input.dispatchEvent(new window.Event('change',{bubbles:true})));assert.equal(writes,0);assert.match(document.querySelector('[role=alert]').textContent,/单个 1 MiB/);
});

test('policy connection failure blocks uploads and offers retry',async t=>{
let attempts=0,writes=0;await mount(t,FileUploader,{onUploaded:()=>{}},(u,o)=>{if(o.method!=='GET')writes++;attempts++;return attempts===1?response({detail:'连接失败'},503):response(defaultPolicy);},null);
assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='上传附件').disabled,true);await click('重试读取');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='上传附件').disabled,false);assert.equal(writes,0);
});

test('policy edit previews exact values, requires acknowledgement and keeps conflict visible',async t=>{
const writes=[];await mount(t,MediaPolicySettings,{},(u,o)=>{writes.push({u,o});if(o.method==='POST')return response({before:defaultPolicy,after:{max_files:2,max_file_mib:3,max_total_mib:5},token:'opaque-policy-preview',expires_at:Date.now()/1000+300,note:'已有文件不删除，QQ 下载中重新检查。'});return response({detail:'附件策略已变化，请重新预览'},409);});
await click('调整限制');const controls=[...document.querySelectorAll('dialog input[type=number]')];await act(async()=>controls.forEach((input,i)=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,[2,3,5][i]);input.dispatchEvent(new window.Event('input',{bubbles:true}));input.dispatchEvent(new window.Event('change',{bubbles:true}));}));
await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
assert.equal(writes.length,1);assert.equal(writes[0].u,'/api/admin/media-policy/previews');assert.equal(document.activeElement.tagName,'P');assert.match(document.querySelector('dialog').textContent,/已有文件不删除/);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认保存').disabled,true);
await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');assert.equal(writes.length,2);assert.deepEqual(JSON.parse(writes[1].o.body),{token:'opaque-policy-preview',acknowledgement:'new_submissions_only'});assert.match(document.querySelector('[role=alert]').textContent,/已变化/);assert.ok(document.querySelector('dialog'));
});

test('dismissed policy preview sends no update',async t=>{
let writes=0;await mount(t,MediaPolicySettings,{},()=>{writes++;return response({before:defaultPolicy,after:defaultPolicy,token:'policy-preview',expires_at:Date.now()/1000+300,note:'不删除已有文件'});});await click('调整限制');await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));await click('关闭');assert.equal(writes,1);assert.equal(document.querySelector('dialog'),null);
});

test('media settings is a working page with boundaries and file navigation',async t=>{
await mount(t,MediaSettings,{},()=>response(defaultPolicy));
assert.equal(document.querySelector('h2').textContent,'媒体配置');
assert.equal(document.querySelector('a').getAttribute('href'),'#/data/files');
assert.match(document.body.textContent,/浏览器实时麦克风/);assert.match(document.body.textContent,/OCR 尚未接入/);
assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='调整限制'));
});

test('disabled format is rejected before binary upload and accept matches policy',async t=>{
let writes=0;await mount(t,FileUploader,{onUploaded:()=>{}},()=>{writes++;return response(item);},{...defaultPolicy,allowed_types:['image']});
const input=document.querySelector('input[type=file]');assert.equal(input.accept,'.png,.jpg,.jpeg,.webp');
Object.defineProperty(input,'files',{value:[new window.File(['abc'],'笔记.md')]});await act(async()=>input.dispatchEvent(new window.Event('change',{bubbles:true})));
assert.equal(writes,0);assert.match(document.querySelector('[role=alert]').textContent,/未允许此附件类型/);
});

test('type toggles preview exact selection and cancellation never saves',async t=>{
const writes=[];await mount(t,MediaPolicySettings,{},(u,o)=>{writes.push({u,body:JSON.parse(o.body)});return response({before:defaultPolicy,after:{...defaultPolicy,allowed_types:[]},token:'format-preview',expires_at:Date.now()/1000+300,note:'已有文件不删除'});});
await click('调整限制');assert.equal(document.querySelectorAll('fieldset input[type=checkbox]').length,8);
await act(async()=>{for(const box of document.querySelectorAll('fieldset input[type=checkbox]'))box.click();});
await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
assert.deepEqual(writes[0].body.allowed_types,[]);assert.match(document.querySelector('dialog').textContent,/新的类型：全部关闭/);
await click('关闭');assert.equal(writes.length,1);
});

test('expired preview never confirms even with acknowledgement',async t=>{
let writes=0;await mount(t,MediaPolicySettings,{},()=>{writes++;return response({before:defaultPolicy,after:defaultPolicy,token:'expired-format-preview',expires_at:Date.now()/1000-1,note:'已过期'});});
await click('调整限制');await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
await act(async()=>document.querySelector('dialog input[type=checkbox]').click());
assert.match(document.querySelector('dialog').textContent,/预览已过期/);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认保存').disabled,true);assert.equal(writes,1);
});

test('media settings announces save and reloads accepted types',async t=>{
const writes=[];await mount(t,MediaPolicySettings,{},(u,o)=>{writes.push(o.method);return response(o.method==='POST'?{before:defaultPolicy,after:defaultPolicy,token:'format-save',expires_at:Date.now()/1000+300,note:'新附件生效'}:defaultPolicy);});
await click('调整限制');await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');
assert.deepEqual(writes,['POST','PUT']);assert.equal(document.querySelector('dialog'),null);assert.match(document.querySelector('[role=status]').textContent,/已保存/);
});

test('no web type explains disabled upload and links to media settings',async t=>{
let writes=0;await mount(t,FileUploader,{onUploaded:()=>{}},()=>{writes++;return response(item);},{...defaultPolicy,allowed_types:['qq_audio']});
assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='上传附件').disabled,true);
assert.match(document.body.textContent,/当前未允许 Web 文件上传/);
assert.equal(document.querySelector('a').getAttribute('href'),'#/settings/media');assert.equal(writes,0);
});

const retention={version:0,enabled:false,days:7,last_checked:0,last_marked:0};
const sweepPreview={id:'sweep',days:7,items:[{...item}],bytes:200,has_more:true,token:'sweep-preview-token',expires_at:Date.now()/1000+300};

test('retention starts disabled, edit needs preview and acknowledgement before update',async t=>{
const writes=[];await mount(t,MediaRetentionSettings,{},(u,o)=>{writes.push({u,body:JSON.parse(o.body)});return response(o.method==='POST'?{before:retention,after:{enabled:false,days:7},token:'retention-preview',expires_at:Date.now()/1000+300}:{...retention,version:1});});
assert.match(document.body.textContent,/关闭自动回收/);assert.match(document.body.textContent,/保存规则不立即删除/);
await click('调整回收');assert.equal(writes.length,0);await act(async()=>document.querySelector('dialog form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true})));
assert.equal(writes.length,1);assert.equal(writes[0].u,'/api/admin/media-retention/previews');assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认保存').disabled,true);
await act(async()=>document.querySelector('dialog input[type=checkbox]').click());await click('确认保存');assert.equal(writes.length,2);assert.deepEqual(writes[1].body,{token:'retention-preview',acknowledgement:'delete_old_unreferenced_web_uploads'});assert.equal(document.querySelector('dialog'),null);assert.match(document.body.textContent,/未立即删除文件/);
});

test('sweep lists exact files and requires acknowledgement, cancellation never deletes',async t=>{
const requests=[];await mount(t,MediaRetentionSettings,{},(u,o)=>{requests.push(u);return response(sweepPreview);});await click('回收预览');
assert.match(document.querySelector('dialog').textContent,/资料.md/);assert.match(document.querySelector('dialog').textContent,/还有更多候选/);assert.match(document.querySelector('dialog').textContent,/不追加其他文件/);
assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认回收').disabled,true);await click('关闭');assert.deepEqual(requests,['/api/admin/media-retention/sweep-previews']);
});

test('sweep double click sends one request and reports skipped files',async t=>{
let finish;const writes=[];await mount(t,MediaRetentionSettings,{},(u,o)=>{writes.push(u);return u.endsWith('sweeps')?new Promise(r=>finish=r):response(sweepPreview);});await click('回收预览');await act(async()=>document.querySelector('dialog input').click());
const button=[...document.querySelectorAll('button')].find(b=>b.textContent==='确认回收');await act(async()=>{button.click();button.click();});assert.equal(writes.filter(p=>p.endsWith('sweeps')).length,1);
await act(async()=>finish(response({id:'r',kind:'manual',marked:0,skipped:1,bytes:0,created_at:1})));assert.equal(document.querySelector('dialog'),null);assert.match(document.body.textContent,/跳过 1 个/);
});

test('expired sweep and preview failure never permit deletion',async t=>{
await mount(t,MediaRetentionSettings,{},()=>response({...sweepPreview,expires_at:1}));await click('回收预览');await act(async()=>document.querySelector('dialog input').click());assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='确认回收').disabled,true);assert.match(document.querySelector('dialog').textContent,/已过期/);
});

test('sweep conflict stays visible and requires fresh preview',async t=>{
await mount(t,MediaRetentionSettings,{},(u,o)=>u.endsWith('sweeps')?response({detail:'回收配置已变化，请重新预览'},409):response(sweepPreview));await click('回收预览');await act(async()=>document.querySelector('dialog input').click());await click('确认回收');assert.match(document.querySelector('[role=alert]').textContent,/已变化/);assert.ok(document.querySelector('dialog'));
});

test('file protection has separate version and confirmed scope, conflict is recoverable',async t=>{
const writes=[];await mount(t,MediaFiles,{},(u,o)=>{if(o.method==='PUT'){writes.push(JSON.parse(o.body));return response({detail:'附件或保留设置已变化'},409);}return response({items:[{...item,protection_version:3,protected:true}],next_cursor:null});});await click('解除保留');assert.match(document.querySelector('dialog').textContent,/后续自动或手动回收/);assert.equal(writes.length,0);await click('确认设置');assert.deepEqual(writes,[{version:3,media_version:2,protected:false}]);assert.match(document.querySelector('[role=alert]').textContent,/已变化/);
});

test('retention unavailable disables both entry actions and offers retry',async t=>{
await mount(t,MediaRetentionSettings,{},u=>u.endsWith('/runs')?response([]):response({detail:'连接失败'},503),defaultPolicy,false);assert.equal([...document.querySelectorAll('button')].find(b=>b.textContent==='回收预览').disabled,true);assert.match(document.querySelector('[role=alert]').textContent,/连接失败/);assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='重试读取'));
});
