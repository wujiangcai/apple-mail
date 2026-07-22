# 小苹果

小苹果是一套自托管邮箱管理工具，后端基于 Flask 和 PostgreSQL，通过 Microsoft OAuth 与 Graph API 读取邮件。项目默认采用服务端凭据存储、数据库会话、CSRF 防护和回环监听，避免将邮箱密码或 refresh token 暴露给浏览器持久化存储。

## 功能

- 批量导入、查看、更新、删除和流式导出邮箱账号。
- 加密保存邮箱密码与 OAuth refresh token，账号列表只返回非敏感元数据。
- 读取邮件、提取验证码并检测 OpenAI 域名发件人的绑定邮件。
- 匿名取件码与邮箱租约：支持指定到期时间或永不过期，客户无需注册。
- 管理端可通过完整租用码精确反查对应邮箱和租约状态，便于售后核对。
- 独立的每日 systemd timer 维护全部有效邮箱的 Microsoft Graph refresh token。
- 数据库会话、严格 Cookie、同源校验、CSRF 校验和登录失败限流。
- Microsoft Graph 分页、响应大小上限、超时与临时错误分类。

## 架构

生产请求链路如下：

```text
Browser --HTTPS--> Caddy --HTTP/loopback--> Gunicorn (mail-manager user)
                                           |
                                           +--> Flask
                                                 |--> PostgreSQL
                                                 +--> Microsoft OAuth/Graph
```

Caddy 负责 TLS、请求体限制和安全响应头；Gunicorn 只监听 `127.0.0.1:5000`。邮箱凭据使用 Fernet 加密后保存到 PostgreSQL，浏览器只使用服务端账号 ID。

## 快速开始

环境要求：Python 3.12 或 3.13、PostgreSQL 17，以及运行前端测试时所需的 Node.js LTS。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .
npm ci
```

创建专用 PostgreSQL 角色和数据库。请将数据库密码替换为独立生成的随机值，并确保 PostgreSQL 只接受本机或受信网络连接。

```sql
CREATE ROLE mail_manager LOGIN PASSWORD 'replace-with-a-random-password';
CREATE DATABASE mail_manager OWNER mail_manager;
```

从样例创建本地环境文件，并限制其权限：

```bash
install -m 600 deploy/mail-manager.env.example .env
```

分别生成访问密码、会话密钥和 Fernet 凭据加密密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"  # ACCESS_PASSWORD
python -c "import secrets; print(secrets.token_urlsafe(48))"  # SESSION_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CREDENTIALS_ENCRYPTION_KEY
```

填写 `.env` 中的必填配置后，将其加载到当前 shell，并初始化数据库。schema 可以安全地连续执行。

```bash
set -a
. ./.env
set +a
flask --app 'mail_manager:create_app()' init-db
flask --app 'mail_manager:create_app()' run --host 127.0.0.1 --port 5000
```

开发环境如使用 HTTP，将 `PUBLIC_ORIGIN` 设为准确的本地 origin，并设置 `COOKIE_SECURE=false`。生产环境必须保持安全 Cookie 和 HTTPS。

管理员入口为 `/`。管理员导入邮箱后，可在“邮箱租约”中自动分配可用邮箱或指定账号 ID，并选择明确的到期日期时间或“永不过期”。取件码只在创建成功后显示一次，数据库只保存其 SHA-256 哈希；遗失后应撤销旧租约并重新创建。

永不过期仅表示租约持续到管理员手动撤销。客户每次兑换取件码形成的浏览器会话仍受 `PICKUP_SESSION_MINUTES` 限制，会话结束后可再次使用原取件码进入。

客户入口为 `/pickup`。客户使用取件码兑换 HttpOnly 短期会话，只能查看租约对应邮箱的收件箱、垃圾邮件和提取验证码，无法访问邮箱密码、Client ID、Refresh Token、账号列表或其他租约。租约到期或被管理员撤销后，取件会话立即失效。

## 配置

必填变量：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 连接 URL |
| `ACCESS_PASSWORD` | 管理端访问密码，至少 16 个字符 |
| `SESSION_SECRET` | 会话派生密钥，至少 32 个字符 |
| `CREDENTIALS_ENCRYPTION_KEY` | Fernet 密钥，用于加密邮箱凭据 |
| `PUBLIC_ORIGIN` | 浏览器访问的唯一 HTTPS origin，不含末尾 `/` |
| `RENTAL_MIN_MINUTES` | 最短租期，默认 5 分钟 |
| `RENTAL_MAX_MINUTES` | 最长租期，默认 10080 分钟（7 天） |
| `RENTAL_DEFAULT_MINUTES` | 管理端默认租期，默认 60 分钟 |
| `PICKUP_SESSION_MINUTES` | 单次取件会话最长时间，且不会超过租约到期时间 |

反向代理部署时设置 `TRUST_PROXY=true`，并将 `TRUSTED_PROXY_CIDRS` 严格限制为 Caddy 到达应用时使用的地址。不要把它配置为公网网段。

## 文档

- [管理员使用指南](docs/ADMIN_GUIDE.md)
- [客户取件指南](docs/CUSTOMER_GUIDE.md)
- [API 文档](docs/API.md)
- [部署指南](docs/DEPLOYMENT.md)
- [升级指南](docs/UPGRADE.md)

## 测试

运行不依赖外部服务的后端测试：

```bash
python -m pytest tests -m "not postgres"
python -m ruff check .
python -m compileall -q mail_manager tests
```

PostgreSQL 集成测试只应指向专用测试数据库：

```bash
export TEST_DATABASE_URL='postgresql://mail_manager_test:replace-me@127.0.0.1:5432/mail_manager_test'
python -m pytest tests/test_postgres_integration.py -v
```

该测试会写入并清理随机命名的数据。禁止将 `TEST_DATABASE_URL` 指向生产数据库。前端测试和依赖审计：

```bash
npm test
python -m pip_audit -r requirements.lock
npm audit --audit-level=high
```

## 生产部署

1. 创建无登录权限的 `mail-manager` 系统用户，将仓库部署到 `/opt/mail-manager`，并由该用户读取代码与虚拟环境。
2. 将 `deploy/mail-manager.env.example` 安装到 `/etc/mail-manager/mail-manager.env`，所有者设为 `root:root`，权限设为 `0600`，再填写真实值。
3. 安装 `deploy/mail-manager.service` 到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 和 `systemctl enable --now mail-manager`。
4. 将 `deploy/Caddyfile` 纳入 Caddy 配置，设置 `MAIL_MANAGER_DOMAIN`，验证配置后重载 Caddy。
5. 通过 `systemctl status mail-manager`、`journalctl -u mail-manager` 和 HTTPS 健康访问确认服务状态。

环境文件应由 root 独占读取。创建目标目录后使用以下命令安装样例，再通过 root 权限填写真实值：

```bash
install -d -o root -g root -m 700 /etc/mail-manager
install -o root -g root -m 600 deploy/mail-manager.env.example /etc/mail-manager/mail-manager.env
```

systemd 样例在每次启动前执行 `init-db`，以专用用户运行 Gunicorn，并设置文件系统、设备、能力集与资源限制。若安装路径不同，请同步修改 unit 中的绝对路径。

禁止将 Gunicorn 直接暴露到公网。不要把 `--bind 127.0.0.1:5000` 改为 `0.0.0.0`，也不要绕过 Caddy 的 HTTPS、2 MiB 请求体上限和安全响应头。

## 备份

升级前和定期执行 PostgreSQL 逻辑备份，并将备份加密保存到与应用主机隔离的位置：

```bash
pg_dump --format=custom --no-owner --file "mail-manager-$(date +%F-%H%M%S).dump" "$DATABASE_URL"
pg_restore --list mail-manager-YYYY-MM-DD-HHMMSS.dump
```

定期在隔离数据库中演练 `pg_restore`，仅有备份文件而没有恢复验证不构成可靠备份。`CREDENTIALS_ENCRYPTION_KEY` 必须通过独立的秘密管理渠道备份；丢失该密钥后，数据库中的邮箱凭据无法恢复。

## 升级

1. 记录当前 Git 提交并完成数据库备份。
2. 获取目标版本，重新执行带哈希的 `requirements.lock` 安装和 `npm ci`。
3. 执行 `flask --app 'mail_manager:create_app()' init-db`，然后运行后端与前端测试。
4. 重启服务并检查 journal、登录、账号列表与邮件读取。
5. 应用回滚时先阅读版本说明；代码可以切回已记录提交，数据库则应根据 schema 兼容性决定是否从备份恢复。

## 许可

本项目采用 [GNU General Public License v3.0 only](LICENSE)，SPDX 标识为 `GPL-3.0-only`。本分支已对上游项目进行多租户取件平台改造；分发修改版本时需保留许可证、标明修改，并按 GPLv3 提供对应源代码。
