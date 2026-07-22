# 部署指南

## 1. 环境要求

- Python 3.12 或 3.13；
- PostgreSQL 17；
- Node.js LTS（构建/测试前端）；
- 生产环境推荐 Linux、Gunicorn、Caddy 和 systemd。

## 2. Windows 本地开发

```powershell
cd C:\path\to\apple-mail
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
npm ci
```

准备 PostgreSQL 数据库并设置环境变量。HTTP 开发环境：

```env
PUBLIC_ORIGIN=http://127.0.0.1:5000
COOKIE_SECURE=false
```

生成密钥：

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(24))"
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

初始化并启动：

```powershell
.venv\Scripts\flask.exe --app "mail_manager:create_app()" init-db
.venv\Scripts\flask.exe --app "mail_manager:create_app()" run --host 127.0.0.1 --port 5000
```

管理员入口 `/`，客户入口 `/pickup`。

## 3. 生产环境变量

必填：

```env
DATABASE_URL=postgresql://mail_manager:随机密码@127.0.0.1:5432/mail_manager
ACCESS_PASSWORD=至少16字符
SESSION_SECRET=至少32字符
CREDENTIALS_ENCRYPTION_KEY=Fernet密钥
PUBLIC_ORIGIN=https://mail.example.com
```

租约与会话：

```env
RENTAL_MIN_MINUTES=5
RENTAL_MAX_MINUTES=10080
RENTAL_DEFAULT_MINUTES=60
PICKUP_SESSION_MINUTES=60
```

`RENTAL_MIN_MINUTES` 和 `RENTAL_MAX_MINUTES` 约束指定到期时间。永久租约不受最大租期限制，但必须手动撤销。`PICKUP_SESSION_MINUTES` 始终限制单次客户会话。

生产环境保持：

```env
COOKIE_SECURE=true
TRUST_PROXY=true
TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

## 4. Linux 生产部署

1. 创建不可登录的 `mail-manager` 系统用户；
2. 将代码放入 `/opt/mail-manager`；
3. 使用锁定依赖创建虚拟环境；
4. 将 `deploy/mail-manager.env.example` 安装到 `/etc/mail-manager/mail-manager.env`，权限 `0600`，所有者 `root:root`；
5. 安装 `deploy/mail-manager.service`；
6. 配置 `deploy/Caddyfile` 并申请 HTTPS；
7. 执行数据库备份和 `init-db`；
8. 启动 systemd 服务并检查日志。

不要让 Gunicorn 监听公网地址；只监听 `127.0.0.1:5000`，由 Caddy 对外提供 HTTPS、安全响应头和请求体限制。

## 5. 数据库备份

```bash
pg_dump --format=custom --no-owner \
  --file "mail-manager-$(date +%F-%H%M%S).dump" \
  "$DATABASE_URL"
```

备份必须与 `CREDENTIALS_ENCRYPTION_KEY` 分开保存。丢失加密密钥后，数据库中的邮箱凭据无法恢复。定期在隔离数据库验证 `pg_restore`。

## 6. 测试和审计

```bash
python -m pytest tests -m "not postgres"
python -m ruff check .
python -m compileall -q mail_manager tests
npm test
python -m pip_audit -r requirements.lock
npm audit --audit-level=high
```

PostgreSQL 集成测试必须使用专用测试库：

```env
TEST_DATABASE_URL=postgresql://mail_manager_test:随机密码@127.0.0.1:5432/mail_manager_test
```

禁止指向生产数据库。

## 7. 每日 Microsoft Graph 授权维护

Access token 是短期令牌，系统按邮件请求即时获取且不持久化。每日任务维护的是所有 `token_valid` 邮箱的 refresh token：调用 Microsoft OAuth 验证并轮换 refresh token，更新 `last_refresh_time`，发现 `invalid_grant` 时将邮箱标记为无效。

安装定时服务：

```bash
install -o root -g root -m 644 deploy/mail-manager-refresh.service /etc/systemd/system/
install -o root -g root -m 644 deploy/mail-manager-refresh.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mail-manager-refresh.timer
```

查看计划和最近结果：

```bash
systemctl list-timers mail-manager-refresh.timer
systemctl status mail-manager-refresh.service
journalctl -u mail-manager-refresh.service
```

手动执行：

```bash
systemctl start mail-manager-refresh.service
```

任务每天运行一次，带随机延迟并支持关机错过后补跑。PostgreSQL advisory lock 防止多个任务并发。不要把调度器放进 Gunicorn worker。

相关配置：

```env
TOKEN_REFRESH_WORKERS=2
TOKEN_REFRESH_BATCH_SIZE=100
TOKEN_REFRESH_MAX_RUNTIME_SECONDS=3600
```

## 8. 日志和监控

重点监控：

- Gunicorn 5xx；
- Microsoft Graph 429、502、503；
- 连续登录/取件码失败；
- 邮箱 token 失效；
- 永久租约数量和长期未访问租约；
- 数据库连接、磁盘和备份状态。

审计表不保存邮件正文和验证码，若需集中日志，也不要把请求体或敏感 Header 全量写入日志。
