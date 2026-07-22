# 升级指南

本文说明从只支持 `durationMinutes` 的限时租约版本，升级到“指定到期时间 / 永不过期”双模式版本。

## 1. 升级前

1. 记录当前 Git 提交；
2. 停止创建新租约；
3. 使用 `pg_dump --format=custom` 完整备份数据库；
4. 单独确认 `CREDENTIALS_ENCRYPTION_KEY` 已备份；
5. 在测试环境演练升级和回滚。

## 2. 数据库变化

`mailbox_rentals.expires_at` 从必填改为可空：

- 非空：指定时间自动到期；
- `NULL`：永不过期，直到管理员撤销。

检查约束变为：

```sql
expires_at IS NULL OR expires_at > starts_at
```

过期扫描和索引只处理非空的到期时间。现有租约的 `expires_at` 都保持原值，不会自动变成永久租约。

项目的 `schema.sql` 包含可重复迁移语句。升级代码后执行：

```bash
flask --app 'mail_manager:create_app()' init-db
```

该操作会删除旧的 `NOT NULL` 和旧检查约束，再建立可空字段对应的新约束。

## 3. API 变化

旧请求：

```json
{"durationMinutes":60}
```

不再支持。

指定到期时间：

```json
{
  "expiryMode":"at",
  "expiresAt":"2026-08-01T12:00:00Z"
}
```

永不过期：

```json
{"expiryMode":"never"}
```

所有外部调用方必须在部署前完成适配。`expiresAt` 必须包含时区。

## 4. 客户会话兼容性

永久租约不会产生永久 Cookie。`pickup_sessions.expires_at` 仍为非空，并按 `PICKUP_SESSION_MINUTES` 自动到期。客户会话结束后可继续用原取件码登录。

现有已登录客户会话不需要迁移，会按原来的会话到期时间结束。

## 5. 升级步骤

```bash
# 1. 停止服务
sudo systemctl stop mail-manager

# 2. 备份数据库和环境配置
pg_dump --format=custom --no-owner --file upgrade-backup.dump "$DATABASE_URL"

# 3. 更新源码并安装锁定依赖
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .
npm ci

# 4. 执行 schema 升级
flask --app 'mail_manager:create_app()' init-db

# 5. 运行测试
python -m pytest tests -m "not postgres"
npm test
python -m ruff check .

# 6. 启动并检查
sudo systemctl start mail-manager
sudo systemctl status mail-manager
journalctl -u mail-manager -n 100
```

随后验证：管理员登录、创建指定到期租约、创建永久租约、客户取件、会话重新登录和管理员撤销。

## 6. 回滚

代码回滚前应确认旧版本无法处理 `expires_at = NULL`。如果数据库中已经创建永久租约，不能直接切回旧代码。

安全回滚方式：

1. 停止服务；
2. 导出或记录永久租约；
3. 恢复升级前数据库备份；
4. 切回旧代码和依赖；
5. 启动并验证。

若不恢复数据库，必须先撤销或为所有永久租约设置有效 `expires_at`，再恢复旧的非空约束；这一方式容易出错，不推荐在生产环境临时操作。
