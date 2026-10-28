import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Window } from 'happy-dom';

const source = readFileSync(new URL('../assets/boot/boot.js', import.meta.url), 'utf8');
function setup(t) {
  const window = new Window({url:'http://localhost/'});
  let now = 0, sequence = 0;
  const timers = new Map();
  window.setTimeout = (fn, ms = 0) => {const id = ++sequence;timers.set(id,{at:now+ms,fn});return id;};
  window.clearTimeout = id => timers.delete(id);
  window.document.body.innerHTML = '<main id="workspace" tabindex="-1"></main><div id="boot"></div>';
  window.eval(source);
  t.after(() => {window.RhineLabBoot?.mount && window.document.querySelector('dialog')?.dispatchEvent(new window.Event('close'));window.happyDOM.abort();});
  const flush = async () => {for (let i=0;i<20;i++) await Promise.resolve();};
  async function advance(ms) {
    const until = now + ms;
    await flush();
    for (;;) {
      const next = [...timers].filter(([,v]) => v.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); now=next[1].at;next[1].fn();await flush();
    }
    now=until;await flush();
  }
  function mount(options={}) {
    return window.RhineLabBoot.mount(window.document.getElementById('boot'),{
      interaction:'auto',sound:false,speech:false,speed:3,ready:Promise.resolve(),...options
    });
  }
  return {window, document:window.document,mount,advance};
}

test('skip and Escape reach identity but cannot pass it, even beyond stall timeout', async t => {
  const {window,document,mount,advance}=setup(t);
  let resolveIdentity, result;
  const stages=[];
  const boot=mount({identityStep:()=>new Promise(resolve=>{resolveIdentity=resolve;}),onComplete:r=>{result=r;},
    onStageChange:s=>{stages.push(s.id);if(s.id==='welcome') assert.match(document.querySelector('.rlb-welcome-name').textContent,/真实用户名/);}});
  boot.skip();await advance(0);
  assert.equal(document.querySelector('dialog').dataset.bootStage,'identity');
  assert.equal(document.querySelectorAll('.rlb-tool')[1].hidden,true);
  boot.skip();
  document.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  await advance(120001);
  assert.equal(result,undefined);
  assert.equal(document.querySelector('dialog').dataset.bootStage,'identity');
  assert.ok(!stages.includes('welcome'));
  resolveIdentity({name:'真实用户名'});await advance(10000);
  assert.equal((await boot.finished).status,'completed');
  assert.ok(stages.includes('welcome'));
});

test('direct identity entry and reduced motion both preserve explicit confirmation', async t => {
  const {document,mount,advance}=setup(t);
  const stages=[];let resolveIdentity;
  const boot=mount({startAt:'identity',reducedMotion:'reduce',identityStep:()=>new Promise(r=>{resolveIdentity=r;}),onStageChange:s=>stages.push(s.id)});
  await advance(30000);
  assert.deepEqual(stages,['identity']);
  assert.equal(document.querySelector('dialog').dataset.bootMotion,'reduce');
  resolveIdentity({name:'owner'});await advance(0);boot.skip();
  assert.equal((await boot.finished).status,'skipped');
});

test('rejected identity fails and never welcomes the user', async t => {
  const {mount,advance}=setup(t);const stages=[];
  const boot=mount({startAt:'identity',identityStep:()=>Promise.reject(new Error('rejected')),onStageChange:s=>stages.push(s.id)});
  await advance(20000);
  assert.equal((await boot.finished).status,'failed');
  assert.ok(!stages.includes('welcome'));
});

test('destroy aborts identity work and restores inert siblings', async t => {
  const {document,mount,advance}=setup(t);let signal;
  const boot=mount({startAt:'identity',identityStep:context=>{signal=context.signal;return new Promise(()=>{});}});
  await advance(0);
  assert.ok(document.getElementById('workspace').hasAttribute('inert'));
  boot.destroy();
  assert.equal((await boot.finished).status,'destroyed');
  assert.equal(signal.aborted,true);
  assert.equal(document.querySelector('dialog'),null);
  assert.equal(document.getElementById('workspace').hasAttribute('inert'),false);
});

test('ready rejection cannot produce successful reveal after identity', async t => {
  const {mount,advance}=setup(t);const stages=[];
  const boot=mount({startAt:'identity',identityStep:async()=>({name:'owner'}),ready:Promise.reject(new Error('unavailable')),onStageChange:s=>stages.push(s.id)});
  await advance(30000);
  assert.equal((await boot.finished).status,'failed');
  assert.ok(!stages.includes('welcome'));
});

test('Enter and composing Escape do not resolve pending identity', async t => {
  const {window,document,mount,advance}=setup(t);
  const boot=mount({startAt:'identity',identityStep:({container})=>{container.innerHTML='<input name="username"><input name="password"><button>确认身份</button>';return new Promise(()=>{});}});
  await advance(0);
  for (const data of [{key:'Enter'},{key:'Escape',isComposing:true}]) {
    const event=new window.KeyboardEvent('keydown',{...data,bubbles:true,cancelable:true});
    document.querySelector('input').dispatchEvent(event);
    assert.equal(event.defaultPrevented,false);
  }
  await advance(20000);
  assert.equal(document.querySelector('dialog').dataset.bootStage,'identity');
  boot.destroy();
});

test('standalone display-only animation retains immediate skip', async t => {
  const {document,mount}=setup(t);const boot=mount();boot.skip();
  assert.equal((await boot.finished).status,'skipped');
  assert.equal(document.querySelector('dialog'),null);
});
