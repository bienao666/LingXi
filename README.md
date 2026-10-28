# 灵犀 AI 助手

服务器部署的个人助手。当前提供 React／TypeScript Web、FastAPI Core、持久化 worker、PostgreSQL 权威记录，以及 Hindsight 长期记忆适配器。沿用午夜蓝主题、四字菜单、47 个路由和十阶段开机动画。

**当前状态：首批代码与本机容器环境已搭建；真实模型接入、跨会话召回和 150 题中文质量仍待验证，尚不能标记“聊天与记忆闭环已交付”。** 完整目标见[设计方案](./灵犀AI助手设计方案.md)，本文件说明实际代码。

## 当前范围

| 模块 | 已有代码 |
|---|---|
| 身份认证 | 开场身份确认中登录；有效会话需确认进入；部署端初始化、退出／撤销、HttpOnly Cookie、CSRF／Origin、Argon2 |
| 模型管理 | OpenAI 兼容 Chat Completions；连接、密钥加密、模型测试；对话、抽取、嵌入、重排固定用途；调用记录 |
| 文字聊天 | 会话创建／重命名／搜索／历史／删除／MD 与 JSON 导出；持久化生成、SSE 通知、停止生成、刷新恢复、请求去重 |
| 长期记忆 | 七类权威记忆、候选确认、低风险自述自动保存开关、来源、版本纠正、删除进度、索引重试、导出 |
| 后台作业 | 事务 outbox、数据库作业、租约与 fencing、有限重试、删除记录独立保存与恢复重放 |
| 基础状态 | Core、数据库、Hindsight 就绪、模型配置缺口、队列、登录会话 |

QQ、语音、多模态附件、人格编辑、主动交流、Skills、工具执行、子助手、插件商店与完整运维均**待开发**，保留菜单。本批不提供消息分支、重新生成、自动模型路由或跨提供商降级。

会话上下文按保守预算保留近期原文，**尚未实现语义压缩与按需补读历史**，截取时显示说明。会话列表最多 200 条，消息单次最多 1000 条，调用记录展示最近 200 条，不宣称无限历史加载。

## WSL＋Docker 部署

在 Ubuntu 终端操作，Linux x86_64 为部署目标：

```bash
cd /mnt/c/project/project3
```

需要 Docker Engine、Compose v2。以下示例使用 `sudo docker`；已有 Docker 权限可去掉 sudo。基础镜像为：

```bash
sudo docker pull groonga/pgroonga:4.0.8-debian-17
sudo docker pull python:3.13.15-slim-bookworm
sudo docker pull node:24.20.0-bookworm-slim
sudo docker pull nginx:1.28.3-alpine
sudo docker pull ghcr.io/vectorize-io/hindsight:1.1.0-slim
```

使用 slim Hindsight，通过 Core 私有网关调用模型，不在容器里加载本地模型。数据库构建另安装固定 pgvector 扩展；构建仍需联网安装 npm、pip 和 apt 依赖。

首次生成 `.env`，**已有文件不要覆盖**：

```bash
sudo docker run --rm -v "$PWD:/app" -w /app python:3.13.15-slim-bookworm \
  sh -c 'pip install --no-cache-dir cryptography==46.0.5 && PYTHONPATH=backend python -m lingxi.cli configure'
sudo docker compose config --quiet
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
```

也可在已安装后端依赖的 Python 3.13.15 环境运行 `PYTHONPATH=backend python -m lingxi.cli configure`。命令独立生成数据库密码、Fernet 主密钥及内部 token；模型密钥不在模板内。不要分享 `.env` 或未脱敏的 `docker compose config`。

迁移由 Compose 自动执行；Core 就绪后启动其余服务。Hindsight 跳过启动时的外部 LLM 检查，允许先配置模型，**不代表模型可用**。

初始化唯一所有者，在终端交互输入用户名、密码：

```bash
sudo docker compose run --rm core python -m lingxi.cli init-admin
```

密码至少 12 字符，没有默认管理员密码；已有所有者时拒绝覆盖。打开 **http://localhost:8080** 登录，主机名须与 `.env` 中 `LINGXI_PUBLIC_ORIGIN` 一致；改用 127.0.0.1 时应同步修改配置并重建服务环境。

登录位于开机动画的“身份确认”阶段：未登录时输入用户名和密码；有效会话显示真实用户名，点击“确认进入”后才加载业务页面。每次打开、刷新、退出、会话失效或手动重播，都先检查服务器会话：未登录（401）从品牌序幕完整播放，有效会话直接进入身份确认；原 Hash 页面地址保留。动画重播同样遵循登录状态，并要求重新点击身份确认。跳过和 Escape 只跳过视觉过场，不能跳过身份步骤。会话检查期间显示“正在检查登录状态”，检查失败停留在静态身份页并提供“重试连接”；网络异常不视为未登录，重试成功后再决定播放起点。动画脚本或样式加载失败时显示相同身份组件的静态全屏页面。

等待输入不计入动画停滞超时；认证请求 15 秒超时后可重试。密码仅随登录请求发送，不写入动画参数、浏览器存储或日志；首次使用说明仍指向终端初始化。播放规则不再依赖标签页已播放标记，已有 lingxi.boot.seen 标记会被忽略。每次开场只确定一次起点，密码错误、登录成功或确认时会话失效都不会重启序幕。

默认仅绑定 `127.0.0.1:8080`。数据库、Core、Hindsight 不发布宿主端口；Hindsight 自带管理台关闭。生产访问需配置可信 HTTPS 反向代理、实际 `LINGXI_PUBLIC_ORIGIN`、`LINGXI_COOKIE_SECURE=true`，并配置 Web 监听及网络边界。当前 Compose 不自动申请证书，不默认公开服务。

## 配置模型与记忆验证

1. “模型管理 → 服务连接”：添加兼容基址与密钥。公网要求 HTTPS；可信内网服务才勾选内网授权；不跟随重定向。
2. “模型目录”：分别登记并测试日常对话、记忆抽取、向量嵌入、检索重排。同一文本模型可分别登记两个用途；兼容地址不代表支持全部用途。
3. “路由策略”：选择各用途通过验证的模型。文本使用 `/chat/completions`，嵌入使用 `/embeddings`，重排需要 Cohere／SiliconFlow 兼容 `/rerank`。基本连接测试不代替中文质量及结构化抽取验证。
4. 嵌入默认 1024 维，需中文多语言模型，重排也需中文能力。`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3` 仅为可替换示例，实际以服务支持为准。
5. “长期记忆”手动保存明确事实，或对话后核对候选。持久化成功与索引成功分开显示；索引失败可重试。
6. 新建其他会话询问该事实，查看实际记忆来源；再纠正／删除并复验。来源失效会取消在途生成，并使相关回复和事件失效。

密钥由服务器主密钥加密，前端仅显示掩码。Hindsight 持有内部 token，Core 按用途解析模型并注入提供商密钥；`/internal/model/v1/*` 不经 Web 反代公开。不要把密钥发送到聊天。

仅配置对话模型时仍可文字聊天；记忆模型未配置、检索故障时明确提示，不生成模拟召回。助手回答不直接成为用户事实。自动保存仅允许有原文证据的简单低风险自述；敏感、冲突和其他内容进入候选核对。

## 运维、备份与恢复

```bash
sudo docker compose ps
sudo docker compose logs --tail 80 core worker
sudo docker compose restart core worker
sudo docker compose down
```

`down` 保留卷，普通重启不要加 `-v`。日志需脱敏再分享。索引与清理作业有限重试；中断的生成标记失败，不自动重放模型调用。

| 位置 | 内容 |
|---|---|
| `lingxi_postgres` 卷 | 业务数据、会话、权威记忆、配置、作业；独立 Hindsight 数据库及索引 |
| `lingxi_deletion-ledger` 卷 | 追加且 fsync 的删除记录，不可随旧数据库备份回退 |
| `.env` | 数据库密码、加密主密钥、内部 token，须单独安全备份 |

备份前停止 Core／worker／Hindsight 写入，分别导出两个数据库，另备份删除卷与 `.env`。恢复期间所有旧实例停写。恢复数据库、放回**最新**删除记录，迁移并重放后开放访问：

```bash
sudo docker compose up -d db
sudo docker compose run --rm migrate
sudo docker compose run --rm core python -m lingxi.cli replay-deletions
sudo docker compose up -d
```

Core／worker 启动也会重放；删除记录损坏会阻止启动。导出下载按当前状态重新生成；已下载副本无法远程撤回，备份物理清除依赖部署保留策略。本批没有自动备份与一键恢复管理台。

更换嵌入模型：备份，停止 Core／worker／Hindsight，等待作业租约到期，再为通过验证、同维度的新模型建立独立索引代次：

```bash
sudo docker compose stop core worker hindsight
sudo docker compose run --rm --no-deps core python -m lingxi.cli reindex
sudo docker compose up -d core hindsight worker
```

按提示输入模型 ID。新索引完成前不复用旧向量。**改变维度尚未提供自动迁移**，不能只修改环境变量后复用旧库；需专门的 Hindsight 表和索引迁移。旧代索引清理、备份过期由部署端管理。

## 代码与检查

```text
backend/lingxi/       API、认证、模型网关、记忆、worker、CLI
backend/migrations/   冻结初始 schema 与增量迁移
backend/tests/        协议、隔离与一致性回归
backend/evals/        中文 150 题与真实 API 评测器
backend/openapi.json  API 契约
frontend/src/         React 业务页、菜单与生成类型
frontend/assets/      午夜蓝主题、图标与独立开机动画
deploy/               Nginx、Dockerfile、数据库初始化
compose.yaml          服务编排
```

Windows 开发检查：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.lock
npm ci
$env:PYTHONPATH='backend'
.venv/Scripts/python.exe -m pytest backend/tests -q
.venv/Scripts/python.exe -m lingxi.cli openapi
npm run api:types
npm run check
npm test
npm run build
```

`npm run dev` 提供 5173 入口，默认 API 代理到 8000；需要另启开发 Core／worker、开发库，并设置对应 origin。普通使用推荐 8080 同源部署入口；隔离 UI 验收账户不是部署管理员。

测试默认临时 SQLite，仅作快速回归。可用 `LINGXI_TEST_DATABASE_URL` 指定**独立 PostgreSQL 测试库**；测试会重建表，禁止指向个人业务库。更新 API 后重新生成类型，不维护第二套旧前端。

中文评测见 [评测说明](./backend/evals/README.md)：在空的专用部署上执行，输出真实结果并人工判分。它测试用户确认资料的检索与生命周期，不能代替原文抽取质量。

## 2026-10-28 实测记录

- WSL Docker 中已构建并启动 Web、Core、worker、PostgreSQL、Hindsight `1.1.0-slim`；业务迁移版本 `0002`。
- Hindsight 独立数据库迁移、1024 维 pgvector 与 PGroonga 初始化完成，基础就绪检查通过；不等同于真实模型保留／召回成功。
- 21 项回归在临时 SQLite、真实 PostgreSQL 测试库分别通过。流式 HTTP 使用协议夹具，不作为实际模型接入验收。
- 类型、静态菜单／动画检查与 Vite 构建通过。开场脚本保持独立原生资源，由构建插件复制；非 module 提示不影响其加载。
- 身份开场回归：`npm test` 覆盖会话检查等待、401／有效会话分流、刷新／重播、错误与网络重试、密码重试、会话撤销、静态降级、卸载取消，以及身份等待、跳过／Escape、减少动效、真实姓名欢迎、销毁清理、初始化拒绝、输入法按键和独立动画兼容；后台认证仍使用原有接口与 21 项回归。
- 浏览器验证了部署入口与开场、隔离环境登录／记忆纠正／未配置模型错误，及记忆／聊天的 375、768、1024、1440px 布局；更多浏览器、完整键盘与 200% 缩放仍待验收。
- 真实模型未配置，跨会话效果、真实生成取消／断线、完整索引恢复、150 题质量均待验证，未宣称全部功能通过。

## 故障排查

- 不能登录：先初始化所有者；检查 Cookie Secure、HTTPS、实际主机名与 origin。
- 页面可用但不回复：确认对话模型验证且被选中、worker 运行；查看队列和脱敏错误。
- 记忆持续等待／失败：检查三个记忆用途、嵌入维度、Hindsight 与结构化输出，修复后重试索引。
- 镜像标签不一致：当前使用 `1.1.0-slim`，检查 `.env` 的 `HINDSIGHT_IMAGE`，无需重新拉完整镜像。
- 服务地址不通：容器内 localhost 指向容器自己；使用 Docker 可达地址，内网需明确授权。
- 恢复旧内容：停止检索和写入，放回最新独立删除记录，成功重放再开放，不要删除记录绕过错误。
