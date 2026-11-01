# QQ 文档接收验证记录（v1.39）

本批实现数组文件消息的文档名兼容与文件 ID 取址，未登录真实 QQ，未调用实际付费模型。协议、持久化与隔离检查不能替代真实 NapCat 文件兼容或模型阅读质量验收。

## 实现范围

- 兼容 NapCat v4.18.41 文件片段的 `file` 文档名及既有 `name`。支持已有 HTTPS URL，或通过原认证连接解析本条输入的 `file_id`。
- 私聊调用 `get_private_file_url(file_id)`；群聊调用 `get_group_file_url(group_id, file_id)`，群号固定原受众。参数依据固定版本源码，不采用旧示例中已不在 payload schema 的额外参数。
- 只处理原连接、精确 echo、整数成功码、`status=ok` 的回执；仅采用 URL。路径、Base64 和其他字段不能扩大读取范围。
- 60 秒取址等待；返回 URL 经原精确域名、公共 HTTPS、DNS 固定及代理校验。仍禁止 HTTP、重定向和本地资源，不能保证所有真实 QQ 地址符合这些条件。
- 单个 20 MiB、单条 50 MiB、最多 10 个附件；验证声明大小，下载后核对实际字节。缺少大小时仍以实际下载预算为准。
- 混合文档全部取址后只创建一个持久下载作业，统一解析全部可用才聊天。正文作为附件证据，不作为工具授权或自动用户事实。
- 默认附件开关与域名不变；群聊仍须允许群及提及／命令。群上传、离线文件通知不自动读取。收发记录新增“正在取址”阶段。
- 文件 ID／URL 加密暂存；成功、失败、删除、撤权和恢复隔离清除临时数据。取址中断不重放；已取址的接收可在原有效期内恢复。

## 自动化证据

| 检查 | 结果 |
|---|---|
| Windows 相关后端（渠道、附件、音频、文档） | 198 通过，1 项依赖 PostgreSQL 的检查跳过 |
| Linux＋独立 PostgreSQL 相关后端（含文件回复、语音回复、主动投递、备份） | 325 通过 |
| 最终镜像文档专项，含新增恢复隔离场景 | 58 通过，Windows 同 58 项通过 |
| 最终前端整套 | 228 通过 |
| TypeScript、47 路由／10 阶段静态检查及构建 | 通过 |
| 独立空库 `0034 → 0033 → 0034` | 通过；有取址记录的降级拒绝 |
| 375／768／1024／1440px 及 1024×480 矮屏 | 弹窗无横向溢出，预览焦点及 Escape 返回正常 |

58 个专项覆盖默认关闭、绑定／群触发、去重、字段与尺寸、文件名及加密留存、私群精确参数、错误／过期／异域回执、来源失效、混合文档作业、实际大小不符、断线恢复、20 MiB 预算及备份恢复隔离。既有附件专项继续覆盖 DNS／系统代理、重定向、总下载量、解析、媒体删除、worker 租约及最终投递屏障。

## 跨进程联调

独立 Compose 环境使用真实管理 HTTP、认证反向 WebSocket、独立 worker、PostgreSQL，以及隔离的 HTTPS 文档源和 CONNECT 代理。测试信任根只挂载到测试容器的证书库，正式镜像及系统证书未修改。

1. Web API 创建隔离账号与合成模型配置；QQ 私聊完成既有身份绑定。
2. 只有 `file`、`file_id`、`file_size` 的输入产生精确取址动作；发送前数据库已保存请求状态，错误 echo 无效。
3. 取得 HTTPS 地址后先关闭连接和重启 Core，再启动 worker。原接收继续下载、解析、调用合成模型并回传文字；模型可见文档证据，不含签名地址或文件 ID。
4. 重投相同外部消息 ID 不重复下载、建消息或调用模型。
5. 第二次取址断线且无回执；Core／worker 重启后只报告中断，没有再次取址或下载。最终准确一次下载、一次合成模型调用。
6. 删除原文档后，原回答变为失效，附件内容不再可取。

首轮联调因测试信任根未加入 HTTPX 使用的证书库而失败；修正隔离环境的证书挂载后完整链路通过，没有降低 TLS 校验。新专项复跑前重建独立测试库，避免既有恢复用例故意回写的迁移版本影响验证。

证据保存在项目 `.preview/qq-documents-*.log`、`qq_documents_*.py`／`.sh`，及 `lingxi-document-rule-desktop.png`、`lingxi-document-rule-mobile.png`。测试配置与临时信任材料随隔离环境清理。

## 上游依据与待验证

- [文件消息转换源码](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/packages/napcat-onebot/api/msg.ts)
- [私聊文件取址](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/packages/napcat-onebot/action/file/GetPrivateFileUrl.ts)
- [群文件取址](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/packages/napcat-onebot/action/file/GetGroupFileUrl.ts)
- [文件标识缓存](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/packages/napcat-common/src/file-uuid.ts)

真实 QQ 仍须验证文件片段／通知上报形式、标识格式、Packet API 可用性、实际 URL 的 HTTPS 与域名、文档声明大小、群触发和账号连续运行。仅有群上传通知或不符合 HTTPS 策略时，当前明确提示不支持或保持忽略，不声称文件已读。通用产物回传、视频、扫描 PDF OCR 和真实模型质量不在本批完成范围。

## 本机部署核验

本机 `http://localhost:8080` 已部署 v1.39，数据库版本 `0034`。重启前确认无运行中用户作业或有效语音会话；保存 `173,493` 字节的 `lingxi-pre-v139.dump`，并验证其归档目录可读。Core／worker／Web 旧镜像以 `pre-v139` 标签保留。

Core 与 worker 各核对 103 个运行源码、迁移及契约文件摘要，OpenAPI 与代码一致，数据库附件／投递列与 ORM 一致。19 类业务记录数量及包含加密密钥的模型、路由、人格、语音和 QQ 配置摘要升级前后一致；未添加真实 QQ 实例或修改用户模型。

正式服务健康、worker 在线，未认证渠道／身份管理接口返回 401。正式 Web 的 `index-CuvdYZkC.js`、`index-ClG5fT7R.css` 及开场脚本与本次本地构建逐字节一致；同源入口和 nginx 钩子保护检查通过。临时容器、网络、数据卷和合成凭据配置清理，证据日志与截图保留；原实验目录保持不动。
