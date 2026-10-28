# 莱茵生命开机动画 · rhine-lab-boot

一套独立的十阶段工业档案终端开场。当前预设使用灵犀品牌、默认静音；支持暖灰、暗墨、手动与自动模式。灵犀应用采用午夜蓝自动开场，在身份阶段等待真实认证。直接打开 [preview.html](preview.html) 可查看独立展示效果。

参数权威源为 [preset.json](preset.json)；[boot.js](boot.js) 中 PRESET_START / PRESET_END 之间是相同 JSON 的浏览器导出，[boot.css](boot.css) 导出视觉变量与组件配方。更改参数时三者同步，不需要生成工具或运行时 fetch。单独复制本目录不会依赖仓库其他文件。

## 下次如何指定

> 使用 rhine-lab-boot 开机动画，采用暖灰 light（或暗墨 dark）。完整复现十阶段、字体层级、细线底板、资料卡和揭幕，保留移动端重排与减少动效。品牌、标记与身份资料使用当前项目内容。默认两次确认，需自动时选择 interaction: "auto"。动画是展示，不承担鉴权。

## 完整流程

| ID | 阶段 | 基准时长 | 画面与文案 | 推进条件 |
| --- | --- | --- | --- | --- |
| brand | 品牌序幕 | 2000ms | 黑幕、原创六边形标记、莱茵生命 / RHINE LAB、字距收紧和扫光 | 时间 |
| wash | 洗白 | 600ms | 暖灰渐亮；暗墨只用低亮度灰幕 | 时间 |
| plate | 档案底板 | 800ms | 品牌、细线框、阶段格依次进入 | 时间 |
| access | 访问许可 | 1200ms | 需要访问许可 / ACCESS PERMISSION REQUIRED | 默认用户确认 |
| identity | 身份确认 | 1200ms | 身份资料确认 / IDENTITY VERIFICATION，姓名、编号、等级 | 默认用户确认 |
| request | 请求接收 | 1200ms | 接收访问需求 / ACCESS REQUEST RECEIVED | 时间 |
| read | 系统读取 | 1800ms | 开始读取权限 / PERMISSION READ STARTED，扫描环和数据纹理 | 最短展示时间 + 可选 ready |
| verified | 核验完成 | 1200ms | 读取完毕 · 核验通过 / READ COMPLETE · VERIFIED | 时间 |
| welcome | 欢迎 | 2000ms | WELCOME TO、品牌框、欢迎“身份名称”访问 | 时间 |
| handoff | 揭幕 | 800ms | 短暂色散、轻微后撤和失焦，随后移除遮罩 | 时间 |

自动模式的完整视觉基准时长为 12800ms。表中“时间”是最短展示时间；正常阶段还需等待当前语音与音效完成，有声播放总时长随实际播报延长。手动模式两步等待不设置强制倒计时，点击按钮、Enter 或空格记录确认；若音频尚未结束，按钮显示“已确认，播报结束后继续”并禁用重复提交。Enter / 空格只在确认按钮或动画根有焦点时确认；聚焦声音或跳过按钮时保留各按钮自己的语义。未提供 identityStep 时 Esc 和跳过立即退出；提供时只跳到身份阶段，未确认不得退出。

speed 仅缩放视觉最短停留、动画与过渡；语音与音效、用户等待和超时使用实际毫秒。减少动效时，各自动阶段的视觉最短停留不超过 600ms；有音频时仍等待播完，不截断或加速语音。仍展示全流程并保留两次确认，无闪白、色散、缩放、旋转、逐字、扫描和脉冲，过渡不超过 100ms。

授权、编号及权限等级都是展示资料；接入项目自行鉴权。底部标注“启动序列”，阶段格不代表真实任务完成百分比。只有 ready 成功或未传入 ready，才展示完成及欢迎。

## 最小接入

```html
<link rel="stylesheet" href="./rhine-lab-boot/boot.css">
<div id="application">
  <button id="enter-workspace">进入工作区</button>
  <!-- 业务页面；动画不改变其原有主题。 -->
</div>
<div id="boot-host"></div>
<script src="./rhine-lab-boot/boot.js"></script>
<script>
  const boot = RhineLabBoot.mount(document.getElementById('boot-host'), {
    theme: 'light',
    interaction: 'manual',
    brand: {
      name: '莱茵生命',
      latin: 'RHINE LAB',
      tagline: 'RESEARCH · SYNTHESIS · LIFE'
    },
    identity: { name: '访客', code: 'OPERATOR', level: 'GUEST' },
    returnFocus: '#enter-workspace'
  });
  boot.finished.then(({ status }) => {
    console.log('开场结果', status);
  });
</script>
```

脚本为普通浏览器脚本，无构建、框架和网络字体依赖。Web / H5 项目使用现代 Chrome、Edge、Firefox 或 Safari；默认使用原生 dialog 顶层，旧浏览器有 inert 与焦点守卫降级，但更旧的浏览器需项目自行提供 dialog/inert 兼容方案。

原生 dialog 位于浏览器顶层；挂载点应在应用常驻 shell，避免初始化时被框架卸载。Vue / React 在挂载钩子或 effect 中调用，卸载时 boot.destroy()；React 严格模式的再次挂载不会保留旧实例。

页面带严格 CSP 时由宿主提供适用的 script/style 授权；运行时不使用 eval 或内联 HTML。定制样式写入外部 CSS，不覆盖普通页面的设计变量。

## 公共 API

RhineLabBoot.mount(container, options) 自动开始播放。container 必须是当前文档内已连接的元素；无效时抛 TypeError。每个文档只保留一个活动实例，任何新 mount 先销毁旧实例。

| 配置 | 默认 | 约定 |
| --- | --- | --- |
| theme | light | light / dark；未知值回到 light |
| speed | 1 | 0.35–3，数值越大越快 |
| interaction | manual | manual 两次确认 / auto 自动；未知值用 manual |
| brand | 见最小示例 | name / latin / tagline / logoUrl，文字最多 128 字符 |
| identity | 个人工作空间 / LINGXI / WORKSPACE / 身份确认 | name / code / level，最多 128 字符，允许中文与长英文换行 |
| sound | false | 语音与音效总开关。首次自动挂载需用户手势启用播放；在点击事件中 mount 且浏览器提供活动手势信息时，可从序幕播放 |
| volume | 0.12 | 0–0.3，限制尖锐提示声的音量 |
| speech | false | true 增加系统中文阶段语音；false 仅保留音效。需同时开启 sound，优先使用本机中文声音，无中文声音时保留音效 |
| speechRate | 1.1 | 0.8–1.8，系统语音语速，不随动画 speed 改变 |
| speechVolume | 0.65 | 0–1，系统语音音量，独立于提示音 volume |
| audio | 全为空 | tick / confirm / complete，可填写本地或 HTTP 音频 URL，默认实时合成 |
| reducedMotion | auto | auto 跟随系统；reduce 强制减少，不提供强制覆盖系统偏好的选项 |
| identityStep | 不提供 | 异步函数接收 {container, signal}，在身份阶段挂载宿主组件并等待 Promise<{name: string}>；确认后用真实姓名欢迎 |
| startAt | brand | 提供 identityStep 时可用 identity 直接进入身份阶段，否则从序幕开始 |
| ready | 不提供 | 可选初始化 Promise；在读取阶段等待，提前拒绝也会被安全接收 |
| readyTimeout | 30000 | 1000–120000ms，仅计算读取阶段最短停留结束后的等待时间 |
| stallTimeout | 15000 | 15000–120000ms，自动推进阶段的停滞保护，同时作为每段音频自请求播放起的独立等待上限；音频超时取消该段并降级继续，不返回启动 timeout |
| returnFocus | 挂载前焦点 | 可传元素或选择器；退出后目标仍存在且可聚焦时恢复 |
| onStageChange | 无 | 接收 { id, index, total }，index 从 0 开始 |
| onComplete | 无 | 接收与 finished 相同的结果；回调异常仅记录，不阻断清理 |

返回控制器：

- skip()：未提供身份步骤或已确认时立即返回 skipped 并清理；未确认时跳到身份阶段并等待，阶段内重复跳过无效。跳过不会取消业务 ready。
- destroy()：立即返回 destroyed 并清理，适合路由切换或组件卸载。
- finished：只 resolve 一次的 Promise，结果为 { status, stage, error? }；不 reject。status 为 completed / skipped / destroyed / failed / timeout，stage 为最后进入的阶段 ID，error 为可选错误说明。
- 重播：再次调用 mount，不复用旧时间线。该接口没有隐式存储和自动重播。

全局导出只有 RhineLabBoot，包含 mount 与 version。业务不应修改脚本内部导出参数；需要长期改变默认视觉时修改本目录 JSON 及同步导出。

## 灵犀身份步骤

宿主通过 `identityStep({container, signal})` 把 React Portal 挂入身份卡槽。等待期间不推进、不计算停滞超时，跳过按钮隐藏；宿主仅在登录或有效会话重新校验成功后 resolve `{name: session.username}`。拒绝或销毁不会显示成功欢迎，退出时 signal 被中止。Tab 循环包含可见、可用的表单、按钮、链接及 summary；输入法组合按键不触发动画快捷键。

动画控制展示和等待，服务端仍独立鉴权。灵犀 `StartupGate` 管理 `/api/auth/me`、登录错误、15 秒请求超时和静态降级；业务组件只在身份确认与开场完成后挂载。动画资源缺失、运行失败或停滞时，宿主保留身份组件，不能据此放行。密码不能传入动画配置或存储。已有会话也要求手动点击确认。宿主先显示会话检查状态，只有取得服务器的有效身份或 401 后才挂载动画：有效会话从 identity 开始，401 从 brand 开始，均采用 1 倍速；打开、刷新、退出、会话失效和手动重播采用同一规则，不使用标签页已播放标记。网络或服务错误先提供静态重试入口，恢复后再决定起点。每次开场只选择一次，后续登录成功或密码重试不会重新挂载时间线。

## 真实初始化、失败与清理

```js
const initializing = initializeWorkspace(); // 项目提供的真实初始化任务
const boot = RhineLabBoot.mount(document.getElementById('boot-host'), {
  interaction: 'auto',
  ready: initializing,
  onComplete(result) {
    if (result.status === 'failed' || result.status === 'timeout') {
      showWorkspaceError(result); // 项目负责实际错误页与重试
    }
  }
});
```

- Promise 在 mount 时就被观察，提前失败不会制造未处理拒绝；流程到 read 才消费结果。
- read 阶段需同时满足最短展示、当前音频结束和业务就绪。readyTimeout 从读取最短展示结束后开始，独立于音频等待计时；用户确认、已知音频及业务等待期间暂停通用停滞保护。
- read 阶段一旦发现业务失败或等待超时，立即停止读取音频并进入错误画面，不进入“核验通过”和欢迎。错误提示至少展示 800ms（减少动效时 100ms），并等待错误短句结束后退出。
- 音频有各自的 stallTimeout 上限，从请求播放起计时，不随 speed 缩放。超时停止并清理该段后继续；语音超时后降级为仅音效。无已知等待的自动阶段超时仍返回 timeout。
- 所有退出路径停止音频、计时器、等待与监听器，移除 dialog，恢复原 inert、滚动样式和焦点；不清除宿主已有的 inert，不修改宿主主题。
- 宿主意外移除挂载点或直接关闭 dialog 时按 destroyed 清理；组件卸载仍应显式调用 destroy()。
- 运行时不持久化是否播放，首次／刷新策略由宿主管理；身份步骤不能代替服务端权限检查。

logoUrl 接收图片资源 URL，不接受 SVG/HTML 字符串；相对 URL 按当前页面解析。默认标记相对 boot.js 所在目录加载，自定义标记失败后回到默认。图片仍无法读取时移除图形，文字和退出操作保持可用。

本地音频同样按当前页面解析，跨域媒体遵循浏览器策略。首次手势前不创建音频上下文；声音默认关闭，显式启用后首次自动挂载等待用户手势启用播放，用户可以随时开关。设置 sound: false 可默认静音，设置 speech: false 可仅播放音效。合成提示音与自定义音频均等待实际播放结束后再推进；加载失败、拒播、取消或超过等待上限时释放该段等待并继续。退出会暂停、卸载自定义音频并停止合成声源。

### 中文阶段语音与有声启动

```js
launchButton.addEventListener('click', () => {
  RhineLabBoot.mount(bootHost, {
    theme: 'dark', sound: true, speech: true,
    volume: 0.12, speechVolume: 0.65, speechRate: 1.1
  });
});
```

页面自动挂载时，浏览器通常不允许自动播放声音。请求 sound: true 会显示“点击开启声音”；点击该按钮即可开启，首次点击不会误变为关闭。若用户先点击确认或内容区，也可启用已请求的声音。跳过按钮不会触发提示音。

speech 使用浏览器 SpeechSynthesis，优先选择 localService 的中文声音，不下载语音文件、不加载第三方 TTS 服务。声音列表延迟出现时监听 voiceschanged；系统没有中文声音、接口不可用或合成失败时，按钮显示“声音：开（仅音效）”。系统声音的实际可用性、是否离线及音色由浏览器和操作系统决定。

品牌、访问许可、身份确认、请求、读取、核验、欢迎和异常阶段提供短句；短过场不播报。品牌与欢迎语跟随配置的品牌名。正常阶段等本阶段的语音、启动提示音及确认提示音全部结束后再切换，不累积跨阶段播报队列。最短展示时间与音频并行计时，取两者中较晚的完成时间；手动阶段取用户确认与音频中较晚的完成时间。有声模式不承诺固定总时长，3× 与减少动态效果仍保留完整播报。

音频未解锁、接口不可用或无中文声音时，不为不存在的播放增加等待；声音列表延迟出现时仅补播当前仍开放的阶段。静音立即取消音频并释放等待，仍保留最短展示或用户确认条件；跳过、销毁与重播立即退出并清理。业务失败与超时可以打断读取播报，但会等待错误画面的短句结束后退出。

浏览器语音共用一个队列；宿主已在播报时，本动画不加入队列。接入项目应避免与其他语音模块同时播放，以便安全停止本实例正在播报的句子。

## 字体与 Card System

| 角色 | 桌面参数 | 手机参数 |
| --- | --- | --- |
| 品牌英文 | 30px / 700 / 1.12，字距 -0.02em，窄体栈 | 22px；矮屏 18px |
| 主状态中文 | 40px / 400 / 1.4，字距 0.16em | clamp(24px,6.4vw,32px)，字距 0.08em |
| 英文副标题 | 13px / 400 / 1.8，字距 0.28em，等宽栈 | 11px，字距 0.12em |
| 身份与正文 | 14px / 400 / 1.65 | 同桌面，长内容换行 |
| 阶段小字 | 12px / 400 / 1.6 | 同桌面；页脚装饰读数 10px |
| 欢迎品牌框 | 28px / 700 / 1.3，字距 0.1em | 22px |
| 欢迎姓名 | 20px / 400 | 16px |

- 窄体推荐 Bahnschrift、DIN Alternate，回退 Arial Narrow 与系统中文；等宽推荐 Cascadia Mono、SFMono-Regular、Consolas、Menlo。
- 中文栈为 Microsoft YaHei、PingFang SC、Hiragino Sans GB、Noto Sans CJK SC、sans-serif。字体不附带、不联网；项目提供真实 400 / 500 / 700 字重可提高跨平台一致性，系统回退会改变字宽。
- 资料卡最大 420px，不使用普通后台圆角卡：1px 上下边线、逐行细线，标签列 80px、值列弹性，行内留白 10px；值右对齐，数字等宽。
- 组件外观只在动画根作用域内生效。按钮 min 44×44px，主操作深浅反相、方正 2px 圆角；工具按钮为轻边框，悬停加清晰描边，焦点 3px 外圈。每个阶段最多一个确认主操作。
- 完整模式的常规状态过渡 320ms，进入 560ms，统一缓动 cubic-bezier(0.16,0.84,0.24,1)，均随 speed 缩放。帧动画、字号和间距不使用全屏缩放冒充适配。

## 配色与材质

| 语义 | 暖灰 light | 暗墨 dark |
| --- | --- | --- |
| 页面 / 表面 | #e5e3df / #eceae6 | #0b0b0c / #171719 |
| 主字 / 次字 | #151513 / #484742 | #f2f1ee / #cecdc8 |
| 辅助字 | #605e59 | #a6a49e |
| 控件边界 | #7a7871 | #817f79 |
| 可读强调 / 装饰信号 | #a84800 / #ff7500 | #ffad69 / #ff7500 |
| 品牌框 / 反色字 | #151513 / #ffffff | #f2f1ee / #0b0b0c |
| 洗白表面 | #f5f3ee | #343438 |

细线纹理是装饰，不承担输入控件边界；其低透明度不用于承载重要信息。亮橙只用作阶段格、标记交点和少量信号；浅色的小字强调使用更深橙。色散仅在终幕使用，禁止叠加持续霓虹、整屏噪声闪烁或强玻璃模糊。

## 移动端与可访问性验收

| 视口 | 必查内容 |
| --- | --- |
| 375px | 16px 留白；工具区单独一行；中文标题和英文副标题换行，资料卡与确认按钮不越界 |
| 768px | 28px 留白；品牌与工具分列；资料卡保持 420px 上限 |
| 1024px | 48px 留白；完整品牌与档案结构，中央区域最大 960px |
| 1440px | 内容不无限拉宽，细线框贴边，标题、阶段、操作形成稳定层级 |
| 320px | 最长英文不裁切；工具可折行，姓名与编号可断词 |
| 横屏 / 高度 ≤520px | 隐藏标语、元信息与扫描装饰；内容可纵向滚动，所有主操作可滚动到达 |
| 200% 缩放 | 按有效 CSS 视口重排；检查长中文、长英文、字号与按钮，不整体缩小画布 |

使用 100dvh 和安全区内边距。内容区为可滚动的弹性布局，标题与资料卡不绝对定位；主操作位于同一滚动区的末尾，不覆盖资料。需要滚动时由用户操作，确认按钮出现时聚焦它以保证可达。

挂载使用 dialog 顶层、同级分支 inert、Tab / Shift+Tab 圈定和焦点守卫；退出恢复挂载前状态。跳过、声音与确认均为 button，工具不被全局 Enter / 空格误触发。读屏使用完整阶段 live 文本，装饰与图形隐藏，不逐字重复播报。

所有正文、辅助信息和按钮文字按两主题 AA 对比度验收；主标题按大字标准、细线装饰不当作文字边界。首次进入焦点在跳过按钮，确认阶段焦点转至确认按钮。宿主 returnFocus 应提供稳定可操作目标。

验收同时覆盖：自动完成、两次确认、全阶段跳过、早期 ready 拒绝、读取时成功／失败／超时、重复 mount/destroy、音频缺失与拒播、图片缺失、系统减少动效动态切换、原滚动样式及 inert 保留。音频专项覆盖长语音、提前／重复确认、长自定义提示音、3× 与减少动效完整播报、延迟声音列表、缺失结束事件及静音释放等待；欢迎与错误短句播完后再退出。预览同时通过 file:// 与 HTTP 子目录打开，确认没有固定根路由或网络字体请求。

## 复刻边界

必须保留：暖灰或暗墨层级、窄体品牌与等宽读数、十阶段和两次确认语义、独立退出、资料卡细线结构、少量橙色信号。可调：品牌、身份、图标、两主题、速度与自动模式；项目就绪通过 ready 接入。

不要混入其他暗色主题变量、官方素材、真声录音、不可跳过的长动画或虚假的真实鉴权。完整复制和素材边界见 [SOURCE.md](SOURCE.md)。可独立运行，不修改页面默认设计规则。
