# OAuth 账号连接

版本：v1.30。当前提供固定服务的 OAuth 2.0 授权码＋PKCE S256，可用于 API 和远程 MCP 连接。只连接用户明确选择的工作区账号，不替代插件安装、工具授权或逐次请求确认。

## 插件合约

在 API 或 MCP 入口将 `auth` 设为 `oauth2`，声明 `oauth`。以下仅为合约示例，不是可直接使用的第三方服务。

```json
{
  "url": "https://api.example.com/notes",
  "method": "GET",
  "effect": "read",
  "auth": "oauth2",
  "oauth": {
    "authorization_url": "https://accounts.example.com/authorize",
    "token_url": "https://accounts.example.com/token",
    "revocation_url": "https://accounts.example.com/revoke",
    "issuer": "https://accounts.example.com",
    "scopes": ["notes.read"],
    "client_auth": "none",
    "resource": ""
  }
}
```

- 所有地址固定，不接受脚本、模板、用户凭据、查询参数或自动发现。不同服务端点可以不同源，用户必须核对每个接收者。
- `issuer` 标识授权服务；回调带 `iss` 时必须精确匹配。暂不支持 OpenID Connect 身份令牌及登录绑定。
- `client_auth` 支持公共客户端 `none`、`client_secret_basic`、`client_secret_post`。专用客户端由用户在服务方注册；不将密钥写入插件包或商店。
- `scopes` 为本次请求的完整范围，不在运行时追加。服务方返回的范围可缩小，不能扩大；无 `scope` 时按 OAuth 规范沿用请求范围。
- `resource` 可选，供支持资源指示参数的服务使用。首批不支持动态客户端注册、设备码、隐式流程、客户端凭据流程、PAR、私钥 JWT 或 mTLS。
- 原有无凭据、Bearer、API Key 清单序列化保持原样，已有发布摘要和签名不因新增 OAuth 字段改变。

## 注册与授权

1. 安装已经审核签名的插件，在“插件中心 → 账号授权”选择对应能力。
2. 在服务方注册专用客户端。登记回调地址为部署 `LINGXI_PUBLIC_ORIGIN` 加 `/api/plugin-oauth/callback`；完整地址也会在生成授权入口后展示。
3. 核对授权、令牌、撤销、服务标识与目标资源地址，填写客户端标识。仅机密客户端填写密钥，不从旧记录回填。
4. 核对 scope 和服务器内网设置，勾选确认，再点击“生成授权”。此步骤保存短期流程，不交换令牌、不代表已经连接。
5. 点击“前往授权”到服务方操作；回调在服务器完成一次核验后返回账号授权页面。仍需完成灵犀的开场身份确认。
6. 查看真实连接状态、授予范围、到期时间、刷新能力和授权记录。MCP 另需发现并确认工具；业务请求仍逐次预览确认。

生产 `PUBLIC_ORIGIN` 必须为 HTTPS 固定源；仅 localhost／回环地址允许本机 HTTP。回调来源地址不能由浏览器参数覆盖。内网放行同时覆盖业务、授权、令牌、撤销及资源入口；所有服务器 HTTP 请求沿用系统代理、DNS 检查和固定目标网络客户端，不跟随重定向。

当前固定授权 URL 不支持供应商自定义查询参数（例如特有的离线访问参数）。接入此类服务需先扩展受控合约和测试；不能通过手工改链接或密钥覆盖绕过。服务端必须支持 S256 PKCE、标准表单令牌请求、Bearer 令牌和有界 JSON 响应，`expires_in` 必须为 60 秒至一年内的整数或数字字符串。实际供应商兼容性需要逐家验证。

## 一次性核验与凭据

每次流程固定当前工作区、所有者、登录会话、插件及安装版本、入口摘要、连接版本、回调地址和内网设置，十分钟有效。同一连接的新流程取消旧流程；取消或到期清除加密的客户端密钥和 verifier。登录失效或切换登录会话后，旧回调不能提交授权，需重新发起。

`state` 只存摘要，PKCE verifier 加密保存；交换前先持久化“正在核验”并清除临时凭据，随后才能发送授权码。进程中断、超时或网络未知时，不重放此授权码。迟到结果必须重新通过原登录、版本及生命周期检查，不能恢复撤销的连接。

访问令牌、刷新令牌及专用客户端密钥由独立服务器密钥加密保存，不返回普通 API、导出、诊断包或模型上下文。回调只返回不含授权码和 state 的站内结果地址；Nginx 关闭该路径的访问与请求错误日志，Core 默认关闭访问日志，响应使用 no-store／no-referrer。自建的外层负载均衡、WAF 和日志采集也应对该完整回调路径禁记查询及 Referer。

## 刷新、撤销与恢复

健康检查、MCP 发现及 worker 业务请求前，令牌将到期且存在刷新令牌时自动刷新。刷新有单独的持久化发送状态，不增加授权范围、不改变正常业务预览版本；轮换后保存新刷新令牌，未返回新刷新令牌时保留旧值。范围缩小则增加连接版本，使旧预览和工具目录授权失效。

刷新时并发调用暂时不可用，不重复发送同一刷新令牌。超时、协议失败、范围扩大、刷新进程中断或迟到结果不能核验时，清除本地凭据并要求重新授权；不会把业务请求自动重放为“恢复”。没有刷新令牌时，到期后重新授权。正常业务的 401、超时或结果未知不触发重发。

“撤销授权”先提交本地撤销，清除访问、刷新和客户端密钥，取消待处理流程与未发送请求。声明撤销端点且仍有令牌时，再发送一次 RFC 7009 请求；只报告“已接受撤销请求”或“结果未知”，不声称第三方已退出全部登录。未提供端点、已无凭据或网络未知时，用户需到服务方核对。远端处理期间不允许新发起此连接；中断后不自动重试撤销。

停用、卸载、插件／依赖更新和商店信任撤销清除对应 OAuth 凭据及流程，并阻止新调用；不会自动请求第三方注销账号。再次启用需重新授权。已有外部操作保留真实回执，不因撤销而声称未执行。

迁移 `0025` 增加独立授权和流程表，有 OAuth 版本、记录或发送屏障时禁止直接降级。恢复旧备份时统一清除所有恢复出的 OAuth 凭据与待处理流程，并撤销登录及外部连接，须重新授权；不恢复旧刷新令牌执行权。

## 接口与状态

| 接口 | 用途 |
| --- | --- |
| `POST /api/plugin-oauth/start` | 确认客户端、scope、内网、安装及连接版本后创建流程 |
| `GET /api/plugin-oauth/flows` | 最近 20 条脱敏流程状态 |
| `POST /api/plugin-oauth/flows/{id}/cancel` | 取消本次流程，清除临时凭据 |
| `GET /api/plugin-oauth/callback` | 服务方回调，一次性核验后返回站内页面 |
| `/api/plugin-connections` | 既有连接列表，新增脱敏 OAuth 状态与到期信息 |
| `POST /api/plugin-connections/{id}/revoke` | 既有版本检查与撤销入口，OAuth 先本地后远端 |

流程状态为 pending／exchanging／succeeded／failed／expired／cancelled；授权为 active／refreshing／reauthorize／revoked。页面不显示上游错误正文、授权码、state、verifier 或令牌。模型不能调用这些接口替用户完成确认。

参考：[RFC 6749](https://www.rfc-editor.org/rfc/rfc6749)、[PKCE RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)、[撤销 RFC 7009](https://www.rfc-editor.org/rfc/rfc7009)、[Issuer RFC 9207](https://www.rfc-editor.org/rfc/rfc9207)、[OAuth 安全最佳实践 RFC 9700](https://www.rfc-editor.org/rfc/rfc9700)。
