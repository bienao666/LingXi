/* Browser export of preset.json. Keep the marked preset synchronized; no fetch/build required. */
(function (global) {
  'use strict';
  const PRESET = /* PRESET_START */{
  "id": "rhine-lab-boot",
  "version": "1.2.0",
  "defaults": {
    "theme": "light",
    "speed": 1,
    "interaction": "auto",
    "sound": false,
    "volume": 0.12,
    "speech": false,
    "speechRate": 1.1,
    "speechVolume": 0.65,
    "readyTimeout": 30000,
    "stallTimeout": 15000,
    "reducedMotion": "auto",
    "brand": {
      "name": "灵犀",
      "latin": "LINGXI",
      "tagline": "与你并肩 · 一点就通",
      "logoUrl": "./assets/lingxi.svg"
    },
    "identity": {
      "name": "个人工作空间",
      "code": "LINGXI / WORKSPACE",
      "level": "身份确认"
    },
    "audio": {
      "tick": "",
      "confirm": "",
      "complete": ""
    }
  },
  "typography": {
    "display": "\"Bahnschrift\", \"DIN Alternate\", \"Arial Narrow\", \"Microsoft YaHei\", \"PingFang SC\", sans-serif",
    "mono": "\"Cascadia Mono\", \"SFMono-Regular\", Consolas, Menlo, monospace",
    "body": "\"Microsoft YaHei\", \"PingFang SC\", \"Hiragino Sans GB\", \"Noto Sans CJK SC\", sans-serif",
    "titleSize": "40px",
    "titleWeight": "400",
    "titleLine": "1.4",
    "titleTracking": "0.16em",
    "subtitleSize": "13px",
    "subtitleTracking": "0.28em",
    "bodySize": "14px",
    "bodyLine": "1.65",
    "metaSize": "12px",
    "brandSize": "30px",
    "brandWeight": "700",
    "brandLine": "1.12",
    "brandTracking": "-0.02em",
    "subtitleLine": "1.8",
    "metaLine": "1.6",
    "metaTracking": "0.12em",
    "welcomeSize": "28px",
    "welcomeWeight": "700",
    "welcomeLine": "1.3",
    "welcomeTracking": "0.1em",
    "welcomeNameSize": "20px"
  },
  "layout": {
    "mobilePadding": "16px",
    "tabletPadding": "28px",
    "desktopPadding": "48px",
    "contentMax": "960px",
    "controlMin": "44px",
    "cardMax": "420px",
    "radius": "2px",
    "layer": "1000"
  },
  "motion": {
    "fast": 100,
    "transition": 320,
    "enter": 560,
    "curve": "cubic-bezier(0.16, 0.84, 0.24, 1)"
  },
  "themes": {
    "light": {
      "bg": "#e5e3df",
      "surface": "#eceae6",
      "ink": "#151513",
      "secondary": "#484742",
      "muted": "#605e59",
      "border": "#7a7871",
      "hair": "rgba(21,21,19,0.12)",
      "accent": "#a84800",
      "signal": "#ff7500",
      "box": "#151513",
      "boxInk": "#ffffff",
      "focus": "#914000",
      "wash": "#f5f3ee",
      "glitchA": "#af3529",
      "glitchB": "#22658c"
    },
    "dark": {
      "bg": "#0b0b0c",
      "surface": "#171719",
      "ink": "#f2f1ee",
      "secondary": "#cecdc8",
      "muted": "#a6a49e",
      "border": "#817f79",
      "hair": "rgba(242,241,238,0.14)",
      "accent": "#ffad69",
      "signal": "#ff7500",
      "box": "#f2f1ee",
      "boxInk": "#0b0b0c",
      "focus": "#ffad69",
      "wash": "#343438",
      "glitchA": "#ff8c80",
      "glitchB": "#81c9ed"
    }
  },
  "stages": [
    {
      "id": "brand",
      "duration": 2000,
      "transition": "tighten-sweep",
      "zh": "灵犀",
      "en": "LINGXI",
      "log": "灵犀界面启动",
      "trigger": "time"
    },
    {
      "id": "wash",
      "duration": 600,
      "transition": "wash",
      "zh": "唤醒界面",
      "en": "AWAKENING WORKSPACE",
      "log": "载入本地界面",
      "trigger": "time"
    },
    {
      "id": "plate",
      "duration": 800,
      "transition": "stagger",
      "zh": "个人空间",
      "en": "PERSONAL WORKSPACE",
      "log": "界面启动序列已建立",
      "trigger": "time"
    },
    {
      "id": "access",
      "duration": 1200,
      "transition": "type",
      "zh": "载入导航",
      "en": "LOADING NAVIGATION",
      "log": "读取本地菜单配置",
      "button": "继续浏览",
      "trigger": "confirm"
    },
    {
      "id": "identity",
      "duration": 1200,
      "transition": "type",
      "zh": "身份确认",
      "en": "IDENTITY VERIFICATION",
      "log": "确认身份后进入个人工作空间",
      "button": "确认身份",
      "trigger": "confirm"
    },
    {
      "id": "request",
      "duration": 1200,
      "transition": "type",
      "zh": "准备视图",
      "en": "PREPARING VIEW",
      "log": "准备页面布局",
      "trigger": "time"
    },
    {
      "id": "read",
      "duration": 1800,
      "transition": "scan",
      "zh": "读取界面",
      "en": "LOADING INTERFACE",
      "log": "等待本地界面就绪",
      "trigger": "ready"
    },
    {
      "id": "verified",
      "duration": 1200,
      "transition": "settle",
      "zh": "界面就绪",
      "en": "INTERFACE READY",
      "log": "本地框架载入完成",
      "trigger": "time"
    },
    {
      "id": "welcome",
      "duration": 2000,
      "transition": "welcome",
      "zh": "欢迎回来",
      "en": "WELCOME TO",
      "log": "欢迎进入灵犀工作空间",
      "trigger": "time"
    },
    {
      "id": "handoff",
      "duration": 800,
      "transition": "glitch-fade",
      "zh": "进入灵犀",
      "en": "ENTERING LINGXI",
      "log": "界面启动序列完成",
      "trigger": "time"
    }
  ],
  "responsive": {
    "mobileMax": 767,
    "tabletMax": 1023,
    "compactMaxHeight": 520,
    "mobile": {
      "titleSize": "clamp(24px,6.4vw,32px)",
      "titleTracking": "0.08em",
      "subtitleSize": "11px",
      "subtitleTracking": "0.12em",
      "brandSize": "22px",
      "welcomeSize": "22px",
      "welcomeNameSize": "16px"
    },
    "compact": {
      "brandSize": "18px"
    }
  },
  "components": {
    "cardLabelWidth": "80px",
    "cardGap": "16px",
    "cardRowPadding": "10px 4px",
    "actionPadding": "12px 28px",
    "actionMinWidth": "180px",
    "focusWidth": "3px",
    "focusOffset": "4px"
  },
  "effects": {
    "introBg": "#050506",
    "introInk": "#f2f1ee",
    "introSecondary": "#cecdc8",
    "introMuted": "#a6a49e",
    "introFocus": "#ffad69",
    "tightenDuration": 1500,
    "streakDuration": 600,
    "glitchDuration": 320,
    "ringDuration": 7000
  }
}/* PRESET_END */;
  const scriptBase = document.currentScript && document.currentScript.src
    ? new URL('.', document.currentScript.src).href : document.baseURI;
  let active = null;
  let serial = 0;
  const cancelled = Symbol('cancelled');
  const clamp = (value, min, max, fallback) => {
    const n = Number(value);
    return Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : fallback;
  };
  const text = (value, fallback, max = 128) => String(value == null ? fallback : value).slice(0, max);
  function imageUrl(value, base) {
    if (!value || String(value).includes('<')) return '';
    try {
      const url = new URL(String(value), base);
      return /^(https?:|file:|blob:)$/.test(url.protocol) ? url.href : '';
    } catch (_) { return ''; }
  }
  function mount(container, options = {}) {
    if (!container || container.nodeType !== 1 || !container.isConnected ||
        container.ownerDocument !== document || !document.body) {
      throw new TypeError('mount requires a connected element in the current document.');
    }
    if (options == null || typeof options !== 'object') options = {};
    if (active) active.destroy();
    const d = PRESET.defaults;
    const config = {
      theme: options.theme === 'dark' ? 'dark' : 'light',
      speed: clamp(options.speed, 0.35, 3, d.speed),
      interaction: options.interaction === 'auto' ? 'auto' : 'manual',
      sound: options.sound === undefined ? d.sound : options.sound === true,
      volume: clamp(options.volume, 0, 0.3, d.volume),
      speech: options.speech === undefined ? d.speech : options.speech === true,
      speechRate: clamp(options.speechRate, 0.8, 1.8, d.speechRate),
      speechVolume: clamp(options.speechVolume, 0, 1, d.speechVolume),
      readyTimeout: clamp(options.readyTimeout, 1000, 120000, d.readyTimeout),
      stallTimeout: clamp(options.stallTimeout, 15000, 120000, d.stallTimeout),
      reducedMotion: ['auto', 'reduce'].includes(options.reducedMotion) ? options.reducedMotion : 'auto',
      brand: Object.assign({}, d.brand, options.brand),
      identity: Object.assign({}, d.identity, options.identity),
      audio: Object.assign({}, d.audio, options.audio)
    };
    for (const key of ['name', 'latin', 'tagline']) config.brand[key] = text(config.brand[key], d.brand[key]);
    for (const key of ['name', 'code', 'level']) config.identity[key] = text(config.identity[key], d.identity[key]);
    config.brand.logoUrl = imageUrl(config.brand.logoUrl, document.baseURI);
    const oldFocus = document.activeElement;
    let returnFocus = options.returnFocus || oldFocus;
    if (typeof returnFocus === 'string') {
      try { returnFocus = document.querySelector(returnFocus); } catch (_) { returnFocus = oldFocus; }
    }
    const timers = new Map(), listeners = [], inertRecords = [], audioTasks = new Set();
    const mq = global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null;
    let ended = false, stageId = 'brand', watchdog = null, awaitingAction = null, soundContext = null;
    let unlocked = userGesture(), soundEnabled = config.sound, readyTimer = null;
    const speech = global.speechSynthesis;
    let speechVoice = null, utterance = null, audioStage = null;
    let currentStage = null, voicedStage = null, speechFailed = false;
    let rejectStopped;
    const stopped = new Promise((_, reject) => { rejectStopped = reject; });
    stopped.catch(() => {});
    let resolveFinished;
    const finished = new Promise(resolve => { resolveFinished = resolve; });
    const id = 'rlb-' + (++serial);
    const identityStep = typeof options.identityStep === 'function' ? options.identityStep : null;
    const identityAbort = new AbortController();
    const identityIndex = PRESET.stages.findIndex(stage => stage.id === 'identity');
    let identityPassed = false, fastForwardIdentity = false, requestIdentityJump;
    const identityJump = new Promise(resolve => { requestIdentityJump = resolve; });
    const root = document.createElement('dialog');
    root.dataset.bootAnimation = PRESET.id;
    root.dataset.bootTheme = config.theme;
    root.className = 'rlb-root';
    root.setAttribute('aria-labelledby', id + '-label');
    root.setAttribute('aria-modal', 'true');
    root.style.setProperty('--rlb-speed', String(config.speed));
    const reduce = () => config.reducedMotion === 'reduce' || !!(mq && mq.matches);
    function updateMotion() { root.dataset.bootMotion = reduce() ? 'reduce' : 'full'; }
    updateMotion();
    function node(tag, className, value, parent) {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (value != null) el.textContent = value;
      if (parent) parent.appendChild(el);
      return el;
    }
    function on(target, event, fn, opts) {
      target.addEventListener(event, fn, opts);
      listeners.push(() => target.removeEventListener(event, fn, opts));
    }
    function later(fn, ms) {
      const timer = setTimeout(() => { timers.delete(timer); if (!ended) fn(); }, ms);
      timers.set(timer, null);
      return timer;
    }
    function cancelTimer(timer) {
      if (timer == null) return;
      clearTimeout(timer);
      const reject = timers.get(timer);
      timers.delete(timer);
      if (reject) reject(cancelled);
    }
    function delay(ms) {
      if (ended) return Promise.reject(cancelled);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { timers.delete(timer); resolve(); }, ms);
        timers.set(timer, reject);
      });
    }
    const scaled = ms => Math.round(ms / config.speed);
    function emit(fn, payload) {
      if (typeof fn !== 'function') return;
      try { fn(payload); } catch (error) { console.error('[RhineLabBoot] callback failed', error); }
    }
    function disarm() { cancelTimer(watchdog); watchdog = null; }
    function arm() {
      disarm();
      watchdog = later(() => exit('timeout', '动画阶段超过停滞保护时限'), config.stallTimeout);
    }
    const scrollRecords = [document.documentElement, document.body].map(el => ({
      el, value: el.style.getPropertyValue('overflow'), priority: el.style.getPropertyPriority('overflow')
    }));
    function stopAudio() {
      for (const task of [...audioTasks]) task.finish('cancelled');
    }
    function exit(status, error) {
      if (ended) return;
      ended = true;
      identityAbort.abort();
      if (awaitingAction) awaitingAction();
      awaitingAction = null;
      rejectStopped(cancelled);
      if (audioStage) audioStage.closed = true;
      stopAudio();
      if (soundContext) { try { soundContext.close().catch(() => {}); } catch (_) {} soundContext = null; }
      for (const [timer, reject] of timers) { clearTimeout(timer); if (reject) reject(cancelled); }
      timers.clear();
      for (const remove of listeners) remove();
      for (const item of inertRecords) {
        if (item.had) item.el.setAttribute('inert', item.value); else item.el.removeAttribute('inert');
      }
      for (const record of scrollRecords) {
        if (record.value) record.el.style.setProperty('overflow', record.value, record.priority);
        else record.el.style.removeProperty('overflow');
      }
      try { if (root.open && typeof root.close === 'function') root.close(); } catch (_) {}
      root.remove();
      if (active === controller) active = null;
      try {
        if (returnFocus && returnFocus.isConnected && !returnFocus.closest('[inert]') && typeof returnFocus.focus === 'function') {
          returnFocus.focus({preventScroll:true});
        }
      } catch (_) {}
      const result = {status, stage:stageId};
      if (error) result.error = String(error.message || error);
      resolveFinished(result);
      emit(options.onComplete, result);
    }
    function skip() {
      if (identityStep && !identityPassed) {
        if (stageId === 'identity') return;
        fastForwardIdentity = true; disarm(); stopAudio();
        if (awaitingAction) awaitingAction();
        requestIdentityJump();
        return;
      }
      exit('skipped');
    }
    const controller = {skip, destroy:() => exit('destroyed'), finished};
    const label = node('h1', 'rlb-sr', '灵犀界面启动', root);
    label.id = id + '-label';
    const decorative = node('div', 'rlb-decoration', null, root);
    decorative.setAttribute('aria-hidden', 'true');
    node('div', 'rlb-columns', null, decorative);
    node('div', 'rlb-wash', null, decorative);
    node('div', 'rlb-sweep', null, decorative);
    node('div', 'rlb-streak', null, decorative);
    const shell = node('div', 'rlb-shell', null, root);
    const header = node('header', 'rlb-header', null, shell);
    const brand = node('div', 'rlb-brand', null, header);
    const mark = node('img', 'rlb-mark', null, brand);
    mark.alt = ''; mark.setAttribute('aria-hidden', 'true');
    const defaultLogo = new URL('mark.svg', scriptBase).href;
    mark.src = config.brand.logoUrl || defaultLogo;
    on(mark, 'error', () => {
      if (mark.src !== defaultLogo) mark.src = defaultLogo; else mark.hidden = true;
    });
    const brandText = node('div', 'rlb-brand-text', null, brand);
    node('p', 'rlb-brand-latin', config.brand.latin, brandText);
    node('p', 'rlb-brand-name', config.brand.name, brandText);
    node('p', 'rlb-tagline', config.brand.tagline, brandText);
    const controls = node('div', 'rlb-controls', null, header);
    const soundButton = node('button', 'rlb-tool', '声音：关', controls);
    soundButton.type = 'button'; soundButton.setAttribute('aria-pressed', 'false');
    const skipButton = node('button', 'rlb-tool', '跳过开场 ↗', controls);
    skipButton.type = 'button';
    const content = node('div', 'rlb-content', null, shell);
    const overview = node('div', 'rlb-overview', null, content);
    node('span', 'rlb-eyebrow', 'LINGXI WORKSPACE / 01', overview);
    const slats = node('div', 'rlb-slats', null, overview);
    slats.setAttribute('aria-hidden', 'true');
    PRESET.stages.forEach(() => node('i', '', null, slats));
    const center = node('section', 'rlb-center', null, content);
    const heroMark = node('img', 'rlb-hero-mark', null, center);
    heroMark.src = mark.src; heroMark.alt = ''; heroMark.setAttribute('aria-hidden','true');
    on(heroMark,'error',() => { if (heroMark.src !== defaultLogo) heroMark.src = defaultLogo; else heroMark.hidden = true; });
    const ring = node('div', 'rlb-reticle', null, center); ring.setAttribute('aria-hidden', 'true');
    const badge = node('p', 'rlb-eyebrow rlb-phase', '', center);
    const title = node('h2', 'rlb-title', '', center);
    const subtitle = node('p', 'rlb-subtitle', '', center); subtitle.lang = 'en';
    node('div', 'rlb-rule', null, center).setAttribute('aria-hidden', 'true');
    const card = node('dl', 'rlb-card', null, center); card.hidden = true;
    for (const [name, value] of [['空间名称',config.identity.name],['界面编号',config.identity.code],['展示模式',config.identity.level]]) {
      const row = node('div', 'rlb-card-row', null, card);
      node('dt', '', name, row); node('dd', '', value, row);
    }
    const identitySlot = node('div', 'rlb-identity-slot', null, center); identitySlot.hidden = true;
    const welcome = node('div', 'rlb-welcome', null, center); welcome.hidden = true;
    node('p', 'rlb-welcome-box', config.brand.latin, welcome);
    const welcomeName = node('p', 'rlb-welcome-name', '欢迎 ' + config.identity.name + ' 访问', welcome);
    const data = node('p', 'rlb-data', '7A 02 · C8 16 · 4F 90 · 01 6B', center);
    data.setAttribute('aria-hidden','true');
    const actionArea = node('div', 'rlb-action-area', null, content);
    const log = node('p', 'rlb-log', '', actionArea);
    const action = node('button', 'rlb-action', '', actionArea);
    action.type = 'button'; action.hidden = true;
    const hint = node('p', 'rlb-hint', 'Enter / 空格确认 · Esc 跳过', actionArea);
    hint.hidden = true;
    const footer = node('footer', 'rlb-footer', null, shell);
    const counter = node('p', 'rlb-counter', '', footer);
    node('p', 'rlb-footer-note', '灵犀空间 · 界面开场', footer);
    const progress = node('div', 'rlb-progress', null, shell);
    progress.setAttribute('aria-hidden', 'true');
    const bar = node('i', '', null, progress);
    const live = node('p', 'rlb-sr', '', root);
    live.setAttribute('role','status'); live.setAttribute('aria-live','polite'); live.setAttribute('aria-atomic','true');

    function userGesture(event) {
      return !!((event && event.isTrusted) || (global.navigator && global.navigator.userActivation && global.navigator.userActivation.isActive));
    }
    function unlock(event) {
      if (unlocked || ended || !userGesture(event) || soundButton.contains(event.target) || skipButton.contains(event.target)) return;
      unlocked = true;
      refreshSound();
      if (!action.contains(event.target)) cue('tick');
      speakStage(currentStage);
    }
    function refreshSound() {
      const on = soundEnabled && unlocked;
      const voiceUnavailable = config.speech && (!speechVoice || speechFailed || typeof global.SpeechSynthesisUtterance !== 'function');
      soundButton.textContent = on ? (voiceUnavailable ? '声音：开（仅音效）' : '声音：开') : (soundEnabled ? '点击开启声音' : '声音：关');
      soundButton.setAttribute('aria-pressed', String(soundEnabled));
      soundButton.title = config.speech ? (voiceUnavailable ? '未能使用中文系统语音，提示音仍可播放' : '中文阶段语音与提示音') : '合成提示音';
    }
    // Every sound belongs to its current stage, including sounds started after a gesture.
    function trackAudio(cleanup) {
      const session = audioStage;
      if (ended || !session || session.closed) return null;
      let done = false, timer = null;
      const task = {finish(reason = 'ended') {
        if (done) return;
        done = true;
        cancelTimer(timer);
        try { cleanup(reason); } catch (_) { /* Cleanup cannot block the sequence. */ }
        audioTasks.delete(task); session.tasks.delete(task);
        for (const wake of session.waiters) wake();
      }};
      audioTasks.add(task); session.tasks.add(task);
      // Known audio waits have their own deadline; the visual watchdog must not win.
      disarm();
      timer = later(() => task.finish('timeout'), config.stallTimeout);
      return task;
    }
    async function waitForStageAudio(session) {
      disarm();
      let wake = null;
      try {
        while (!ended && !session.closed && session.tasks.size) {
          await Promise.race([new Promise(resolve => {
            wake = resolve; session.waiters.add(resolve);
          }), stopped]);
          session.waiters.delete(wake); wake = null;
        }
        if (ended) throw cancelled;
        // Closing synchronously prevents late voiceschanged events reviving an old stage.
        session.closed = true;
      } finally {
        if (wake) session.waiters.delete(wake);
      }
    }
    function findVoice() {
      if (!config.speech || !speech || typeof speech.getVoices !== 'function') return;
      try {
        const voices = speech.getVoices().filter(voice => /^zh(?:-|_|$)/i.test(voice.lang));
        speechVoice = voices.find(voice => voice.localService) || voices[0] || null;
      } catch (_) { speechVoice = null; }
      refreshSound();
    }
    function speakStage(stage) {
      if (!stage || stage !== currentStage || !audioStage || audioStage.closed || ended || !soundEnabled || !unlocked || !config.speech || !speechVoice || speechFailed || config.speechVolume === 0 ||
          typeof global.SpeechSynthesisUtterance !== 'function' || voicedStage === stage.id) return;
      // 只播报关键阶段，不给短过场排队；不接管宿主已经在使用的语音队列。
      if (!['brand','access','identity','request','read','verified','welcome','error'].includes(stage.id)) return;
      if (!utterance && (speech.speaking || speech.pending)) return;
      if (utterance) return;
      const shortLines = {request:'准备界面',verified:'界面就绪',error:'未就绪'};
      const phrase = stage.id === 'brand' ? config.brand.name : stage.id === 'welcome' ? '欢迎访问' + config.brand.name : shortLines[stage.id] || stage.zh;
      const line = new global.SpeechSynthesisUtterance(phrase);
      line.lang = speechVoice.lang; line.voice = speechVoice;
      line.rate = config.speechRate; line.volume = config.speechVolume;
      utterance = line; voicedStage = stage.id;
      const task = trackAudio(reason => {
        line.onend = null; line.onerror = null;
        if (utterance !== line) return;
        utterance = null;
        if (reason === 'cancelled' || reason === 'timeout') {
          try { speech.cancel(); } catch (_) {}
        }
        if (reason === 'timeout' || reason === 'error') { speechFailed = true; refreshSound(); }
      });
      line.onend = () => task.finish();
      line.onerror = () => {
        if (utterance !== line || ended) return;
        task.finish('error');
      };
      try { speech.speak(line); } catch (_) { task.finish('error'); }
    }
    function cue(kind) {
      if (ended || !audioStage || audioStage.closed || !soundEnabled || !unlocked || config.volume === 0) return;
      const customUrl = imageUrl(config.audio[kind], document.baseURI);
      if (customUrl) {
        let task = null;
        try {
          const audio = new global.Audio(customUrl);
          audio.volume = config.volume;
          const finish = () => task.finish();
          const fail = () => task.finish('error');
          task = trackAudio(() => {
            audio.removeEventListener('ended', finish); audio.removeEventListener('error', fail); audio.removeEventListener('abort', fail);
            audio.pause(); audio.removeAttribute('src'); audio.load();
          });
          audio.addEventListener('ended', finish);
          audio.addEventListener('error', fail);
          audio.addEventListener('abort', fail);
          const playing = audio.play(); if (playing) playing.catch(fail);
        } catch (_) { if (task) task.finish('error'); }
        return;
      }
      let task = null;
      try {
        const AC = global.AudioContext || global.webkitAudioContext;
        if (!AC) return;
        if (!soundContext) soundContext = new AC();
        const oscillator = soundContext.createOscillator(), gain = soundContext.createGain();
        task = trackAudio(() => {
          oscillator.onended = null;
          try { oscillator.stop(); } catch (_) {}
          try { oscillator.disconnect(); } catch (_) {}
          gain.disconnect();
        });
        if (soundContext.state === 'suspended') soundContext.resume().catch(() => task.finish('error'));
        const now = soundContext.currentTime;
        oscillator.type = 'sine';
        oscillator.frequency.setValueAtTime(kind === 'complete' ? 880 : kind === 'confirm' ? 660 : 1100, now);
        oscillator.frequency.exponentialRampToValueAtTime(kind === 'complete' ? 1320 : 880, now + 0.09);
        gain.gain.setValueAtTime(0.0001, now);
        gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, config.volume), now + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.13);
        oscillator.connect(gain); gain.connect(soundContext.destination);
        oscillator.onended = () => task.finish();
        oscillator.start(now); oscillator.stop(now + 0.15);
      } catch (_) { if (task) task.finish('error'); }
    }
    on(root,'pointerdown',unlock,{capture:true});
    on(root,'keydown',unlock,{capture:true});
    on(soundButton,'click',event => {
      if (!userGesture(event)) return;
      soundEnabled = unlocked ? !soundEnabled : true;
      unlocked = true; speechFailed = false; voicedStage = null;
      findVoice(); refreshSound();
      if (soundEnabled) { cue('confirm'); speakStage(currentStage); } else stopAudio();
    });
    on(skipButton,'click',controller.skip);
    on(action,'click',() => { if (awaitingAction) awaitingAction(); });
    on(root,'cancel',event => { event.preventDefault(); controller.skip(); });
    on(root,'close',controller.destroy);
    on(document,'keydown',event => {
      if (ended) return;
      if (event.isComposing) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); controller.skip(); return; }
      if (event.key === 'Tab') {
        const focusable = [...root.querySelectorAll('button, input, select, textarea, summary, a[href], [tabindex]')]
          .filter(el => !el.disabled && el.tabIndex >= 0 && !el.closest('[hidden], [inert]') && el.getClientRects().length);
        if (!focusable.length) return;
        const index = focusable.indexOf(document.activeElement);
        const next = event.shiftKey ? (index <= 0 ? focusable.length - 1 : index - 1) : (index + 1) % focusable.length;
        event.preventDefault(); focusable[next].focus(); return;
      }
      if ((event.key === 'Enter' || event.key === ' ') && awaitingAction &&
          (document.activeElement === root || document.activeElement === action)) {
        event.preventDefault(); awaitingAction();
      }
    },true);
    on(document,'focusin',event => {
      if (!ended && !root.contains(event.target)) (skipButton.hidden ? soundButton : skipButton).focus({preventScroll:true});
    },true);
    if (mq && typeof mq.addEventListener === 'function') on(mq,'change',updateMotion);
    if (config.speech && speech && typeof speech.addEventListener === 'function') {
      on(speech,'voiceschanged',() => { findVoice(); speakStage(currentStage); });
    }
    findVoice(); refreshSound();

    // Observe a supplied promise immediately so early rejection is always handled.
    const ready = Object.prototype.hasOwnProperty.call(options, 'ready')
      ? Promise.resolve(options.ready).then(() => ({ok:true}), error => ({ok:false,error:error || '系统初始化失败'}))
      : null;
    async function waitForAction() {
      disarm();
      await new Promise(resolve => {
        awaitingAction = () => {
          awaitingAction = null;
          if (!ended) {
            cue('confirm');
            action.disabled = true;
            if (audioStage.tasks.size) {
              action.textContent = '已确认，播报结束后继续';
              hint.textContent = '正在等待当前音频结束 · Esc 跳过';
              live.textContent = '已确认，播报结束后继续';
            }
          }
          resolve();
        };
      });
      if (ended) throw cancelled;
    }
    async function waitForReady(minimum) {
      if (!ready) return;
      disarm();
      let waiting = true, outcome;
      try {
        outcome = await Promise.race([
          ready,
          stopped,
          minimum.then(() => new Promise(resolve => {
            if (waiting && !ended) readyTimer = later(() => resolve({timeout:true}), config.readyTimeout);
          }))
        ]);
      } finally {
        waiting = false;
        cancelTimer(readyTimer); readyTimer = null;
      }
      if (ended) throw cancelled;
      if (outcome.timeout) {
        await fail('timeout','系统初始化超时，正在返回应用');
        throw cancelled;
      }
      if (!outcome.ok) {
        await fail('failed','系统初始化未完成，正在返回应用',outcome.error);
        throw cancelled;
      }
    }
    async function fail(status, message, error) {
      if (audioStage) audioStage.closed = true;
      stopAudio();
      const session = audioStage = {tasks:new Set(),waiters:new Set(),closed:false};
      root.dataset.bootStage = 'error';
      title.textContent = '暂时无法完成启动';
      subtitle.textContent = status === 'timeout' ? 'INITIALIZATION TIMEOUT' : 'INITIALIZATION INCOMPLETE';
      log.textContent = message; live.textContent = message;
      currentStage = {id:'error',zh:'系统初始化未完成'};
      speakStage(currentStage);
      await delay(reduce() ? 100 : 800);
      await waitForStageAudio(session);
      exit(status,error || message);
    }
    function setStage(stage,index) {
      if (ended) throw cancelled;
      audioStage = {tasks:new Set(),waiters:new Set(),closed:false};
      currentStage = stage;
      stageId = stage.id;
      root.dataset.bootStage = stage.id;
      root.style.setProperty('--rlb-stage-duration', scaled(stage.duration) + 'ms');
      badge.textContent = 'SEQUENCE / ' + String(index + 1).padStart(2,'0');
      title.textContent = stage.id === 'brand' ? config.brand.name : stage.zh;
      subtitle.textContent = stage.id === 'brand' ? config.brand.latin : stage.en;
      log.textContent = stage.log;
      counter.textContent = '[ ' + String(index + 1).padStart(2,'0') + ' / ' + PRESET.stages.length + ' ] ' + stage.id.toUpperCase();
      bar.style.width = ((index + 1) / PRESET.stages.length * 100) + '%';
      for (let i = 0; i < slats.children.length; i++) {
        slats.children[i].className = i === index ? 'rlb-current' : i < index ? 'rlb-past' : '';
      }
      card.hidden = stage.id !== 'identity' || !!identityStep;
      identitySlot.hidden = stage.id !== 'identity' || !identityStep;
      skipButton.hidden = stage.id === 'identity' && !!identityStep && !identityPassed;
      welcome.hidden = stage.id !== 'welcome' && stage.id !== 'handoff';
      action.hidden = (stage.id === 'identity' && !!identityStep) || !(stage.trigger === 'confirm' && config.interaction === 'manual');
      action.disabled = false;
      hint.hidden = action.hidden;
      hint.textContent = 'Enter / 空格确认 · Esc 跳过';
      if (!action.hidden) action.textContent = stage.button;
      if (root.contains(document.activeElement) && document.activeElement === action && action.hidden) skipButton.focus({preventScroll:true});
      if (skipButton.hidden && document.activeElement === skipButton) soundButton.focus({preventScroll:true});
      // Announce whole phrases; visual character reveal is a single masked node.
      live.textContent = stage.id === 'brand' ? config.brand.name : stage.zh;
      if (!action.hidden) {
        action.focus({preventScroll:true});
        action.scrollIntoView({block:'nearest',inline:'nearest'});
      }
      cue(stage.id === 'welcome' ? 'complete' : 'tick');
      speakStage(stage);
      emit(options.onStageChange,{id:stage.id,index,total:PRESET.stages.length});
    }
    async function run() {
      try {
        for (let index=options.startAt === 'identity' && identityStep ? identityIndex : 0;index<PRESET.stages.length;index++) {
          if (ended) return;
          if (fastForwardIdentity && !identityPassed && index < identityIndex) index = identityIndex;
          arm();
          const stage = PRESET.stages[index];
          setStage(stage,index);
          if (ended) return;
          const session = audioStage;
          if (stage.id === 'identity' && identityStep) {
            disarm();
            const identity = await Promise.race([
              Promise.resolve(identityStep({container:identitySlot,signal:identityAbort.signal})), stopped
            ]);
            if (ended) return;
            if (!identity || typeof identity.name !== 'string' || !identity.name.trim()) throw new Error('身份确认未完成');
            config.identity.name = text(identity.name, config.identity.name);
            welcomeName.textContent = '欢迎 ' + config.identity.name + ' 访问';
            identityPassed = true; identitySlot.hidden = true;
            log.textContent = '身份已确认，正在准备工作空间';
            live.textContent = '身份已确认';
          } else if (stage.trigger === 'confirm' && config.interaction === 'manual') {
            await waitForAction();
          } else {
            const dwell = reduce() ? Math.min(600,stage.duration) : scaled(stage.duration);
            const minimum = delay(dwell);
            if (stage.trigger === 'ready') await Promise.all([minimum, waitForReady(minimum)]);
            else if (identityStep && index < identityIndex) await Promise.race([minimum,identityJump,stopped]);
            else await minimum;
          }
          if (!(fastForwardIdentity && !identityPassed && index < identityIndex)) await waitForStageAudio(session);
        }
        exit('completed');
      } catch (error) {
        if (error !== cancelled && !ended) exit('failed',error);
      }
    }
    try {
      container.appendChild(root);
      // A host route may remove its mount point before calling its cleanup hook.
      if (typeof MutationObserver === 'function') {
        const observer = new MutationObserver(() => { if (!root.isConnected) controller.destroy(); });
        observer.observe(document.body,{childList:true,subtree:true});
        listeners.push(() => observer.disconnect());
      }
      // Inert only sibling branches: never inert a container ancestor of the dialog.
      let branch = root;
      while (branch && branch !== document.body) {
        const parent = branch.parentElement;
        if (!parent) break;
        for (const el of parent.children) {
          if (el !== branch) {
            inertRecords.push({el,had:el.hasAttribute('inert'),value:el.getAttribute('inert')});
            el.setAttribute('inert','');
          }
        }
        branch = parent;
      }
      for (const record of scrollRecords) record.el.style.setProperty('overflow','hidden');
      if (typeof root.showModal === 'function') root.showModal();
      else { root.setAttribute('open',''); root.setAttribute('role','dialog'); }
      active = controller;
      skipButton.focus({preventScroll:true});
      run();
    } catch (error) { exit('failed',error); }
    return controller;
  }
  global.RhineLabBoot = Object.freeze({mount,version:PRESET.version});
})(window);
