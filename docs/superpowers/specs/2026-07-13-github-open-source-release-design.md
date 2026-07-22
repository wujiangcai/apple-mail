# 小苹果安全开源发布设计

## 目标

将服务器 `/opt/mail_manager` 中的邮箱管理应用整理为公开 GitHub 仓库 `小苹果`，采用 `GPL-3.0-only` 许可证。在保留账号导入导出、邮箱查看、验证码提取和绑定检测能力的同时，修复审计确认的高风险问题，并提供可复现、可测试、默认安全的部署方式。

## 发布边界

发布源码以当前 `server.py` 和 `static/mail.html` 为功能基线，但不直接镜像生产目录。仓库不包含生产数据库、真实环境变量、访问密码、OAuth 凭据、虚拟环境、Python 缓存、日志、临时认证文件或 `.bak` 文件。

发布内容包括应用源码、数据库初始化脚本、前后端测试、GPL 许可证、中文 README、安全策略、贡献指南、示例环境变量、systemd/Caddy 部署样例和 GitHub Actions CI。

## 目标架构

生产请求链路为：

```text
Browser --HTTPS--> Caddy --HTTP/loopback--> Gunicorn (mail-manager user)
                                           |
                                           +--> Flask application
                                                 |
                                                 +--> PostgreSQL
                                                 +--> Microsoft OAuth/Graph
```

Gunicorn 只监听 `127.0.0.1`，使用无登录权限的专用系统用户。Caddy 负责 TLS、安全响应头、请求体限制和基础连接超时。应用不会信任来自公网的转发头；仅在显式启用代理模式时按一个受信代理跳数处理转发信息。

## 代码结构

后端从单文件拆分为应用工厂和职责明确的模块：

```text
mail_manager/
  __init__.py       Flask 应用工厂与安全响应头
  config.py         环境变量校验
  db.py             PostgreSQL 连接与事务工具
  schema.sql        幂等数据库结构
  auth.py           登录、会话、CSRF 与限流
  accounts.py       账号 CRUD、导入、导出与密文处理
  graph.py          OAuth 刷新和 Microsoft Graph 客户端
  mail.py           取件、验证码和绑定判断
  static/
    index.html
    styles.css
    app.js
```

测试、部署和项目文件分别放入 `tests/`、`deploy/`、`.github/workflows/` 和仓库根目录。

## 凭据与会话

`ACCESS_PASSWORD`、`SESSION_SECRET`、`CREDENTIALS_ENCRYPTION_KEY`、`PG_PASS` 等敏感配置必须由环境提供。缺失时应用启动失败，不提供生产可用的默认密码。

邮箱密码和 refresh token 使用版本化 Fernet 密文存入 PostgreSQL。账号列表 API 默认只返回账号 ID、邮箱、绑定状态、令牌状态和更新时间。单账号查看凭据通过明确的按需接口获取，响应设置 `Cache-Control: no-store`；导出使用受保护的 POST 下载接口。前端仅在内存中短暂持有用户主动查看或导出的明文，不写入 `localStorage`、`sessionStorage` 或 URL。

登录成功后生成随机服务器会话，数据库只保存会话 ID 的哈希、到期时间和 CSRF 密钥。浏览器 Cookie 使用 `Secure`、`HttpOnly`、`SameSite=Strict`。退出登录会撤销服务器会话。所有状态变更请求必须携带由当前会话派生的 CSRF Header，并校验同源 `Origin`。

登录失败计数存入 PostgreSQL，按受信客户端地址限流并设置过期时间，不再使用 `/tmp` JSON 文件。请求体上限为 2 MiB，账号批量导入上限为 1000 条，绑定检测单批上限为 20 条。

## 邮件与令牌处理

前端取件只提交 `account_id`、邮箱文件夹和分页参数，后端从数据库读取并解密 OAuth 凭据。邮件接口统一使用 POST，敏感参数不进入查询串。

同一账号的令牌刷新使用 PostgreSQL 短事务租约串行化：领取租约后立即提交，OAuth 网络请求不占用数据库事务；刷新结果通过租约 token 做 CAS 持久化，过期租约可恢复且旧请求不能覆盖新 token。新 refresh token 持久化成功后才请求 Graph。只有 OAuth 明确返回 `invalid_grant` 时才把账号标记为令牌失效；网络超时、限流和上游 5xx 返回可重试错误，不改变有效状态。成功导入或刷新会恢复 `token_valid`。

Graph 客户端设置连接和读取超时，处理 `@odata.nextLink`，并限制每次返回的总邮件数。绑定判断仅接受发件域名等于 `openai.com` 或其子域名，不依赖可伪造的主题文本。

## 前端安全

账号和邮件元数据使用 DOM API 与 `textContent` 渲染，不再通过模板字符串写入 `innerHTML`，也不使用内联事件处理器。

邮件 HTML 在无 `allow-scripts`、无 `allow-same-origin` 的 sandbox iframe 中显示。`srcdoc` 注入严格 CSP：默认禁止资源，仅允许必要的内联样式和 `data:` 图片，并设置 `referrerpolicy=no-referrer`。纯文本邮件使用 `textContent`。

内联 JavaScript 和主要 CSS 拆分为静态文件。主页面启用 `default-src 'self'`、`script-src 'self'`、`object-src 'none'`、`base-uri 'none'`、`frame-ancestors 'none'` 等 CSP，并设置 HSTS、nosniff、Referrer Policy 和 Permissions Policy。敏感 API 响应统一使用 `Cache-Control: no-store`。

异步变更操作必须 `await` 后端结果、检查 HTTP 状态并在失败时保持原状态。前端以服务端为唯一数据源，不再维护可覆盖服务器状态的持久化副本。邮件分页变量改为可变状态并增加边界校验。

## 数据库与部署

数据库结构通过显式 CLI 命令初始化，systemd 在启动应用前执行该命令。部署不依赖模块导入副作用。数据库账号使用最小权限，PostgreSQL 仅监听回环地址。

systemd 样例启用 `NoNewPrivileges`、`PrivateTmp`、`PrivateDevices`、`ProtectSystem=strict`、`ProtectHome=true`、能力集清空和资源上限。秘密通过权限为 `0600` 的环境文件或 systemd credentials 注入，unit 文件不包含实际值。

Caddy 样例只代理到回环 Gunicorn，并配置 HTTPS、安全头、请求体上限和超时。README 明确禁止直接暴露 Gunicorn 端口。

## 测试与持续集成

后端使用 pytest 和 Flask 测试客户端，Microsoft OAuth/Graph 全部使用 mock。覆盖认证、会话撤销、CSRF、权限边界、账号导入限制、凭据加解密、令牌错误分类、令牌轮换持久化、并发锁、邮件分页、绑定域名判断和验证码提取。

前端使用 Vitest 与 jsdom，覆盖安全文本渲染、邮件 iframe 属性、无持久化凭据、API 错误回滚、分页和批量边界。静态安全测试禁止敏感字段进入 GET URL、禁止危险 `innerHTML` sink、禁止内联事件属性。

GitHub Actions 在 Python 3.12 和 3.13 上运行后端测试，在当前 Node.js LTS 上运行前端测试，并执行依赖审计、Python 编译检查和秘密扫描。只有全部检查通过才发布首个公开提交。

## 许可证与文档

仓库使用完整 GNU GPL v3 文本，SPDX 标识为 `GPL-3.0-only`。README 包含功能、架构、快速开始、环境变量、数据库初始化、开发测试、生产部署、升级和安全说明。`SECURITY.md` 提供私下报告漏洞的流程，`CONTRIBUTING.md` 说明测试和提交要求。

仓库描述使用“小苹果邮箱助手：基于 Flask、PostgreSQL 与 Microsoft Graph 的安全邮箱管理工具”。仓库公开后不自动部署或修改现有生产服务；生产迁移作为独立步骤执行。

## 非目标

本次发布不实现多用户 RBAC、SMTP 发信、分布式任务队列、云厂商密钥管理、移动客户端或生产数据库自动迁移。现有生产服务器不会在发布过程中重启或切换到新代码。

## 验收标准

1. Git 历史和工作树不包含真实凭据、生产备份、虚拟环境或缓存文件。
2. 未认证请求不能读取账号、邮件或导出数据，状态变更请求通过会话与 CSRF 双重校验。
3. 浏览器存储和请求 URL 中不存在邮箱密码、refresh token 或访问密码。
4. 恶意账号字段、邮件主题和邮件正文不能在主应用源执行脚本。
5. OAuth 临时错误不会使有效令牌失效，并发刷新不会覆盖较新的 refresh token。
6. Gunicorn 部署样例只监听回环地址，systemd 不以 root 运行且不在 unit 中保存秘密。
7. 后端、前端、静态安全和秘密扫描全部通过。
8. GitHub 公共仓库名称为 `小苹果`，默认分支为 `main`，许可证识别为 GPL-3.0。
