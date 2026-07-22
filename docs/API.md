# API 文档

所有 API 都是同源 JSON 接口。管理员和客户使用不同的 HttpOnly Cookie。除公开登录/状态接口外，POST、PATCH、PUT、DELETE 请求必须携带准确的 `Origin` 和登录响应返回的 `X-CSRF-Token`。

## 管理员认证

### `POST /api/login`

```json
{"password":"管理员访问密码"}
```

成功返回 `csrfToken` 并设置 `mail_session`。

### `GET /api/check-auth`

返回管理员会话状态。

### `POST /api/logout`

撤销管理员会话。

## 邮箱管理

原有接口包括：

- `GET /api/accounts`
- `POST /api/accounts/import`
- `POST /api/accounts/<id>/reveal`
- `POST /api/accounts/export`
- `PATCH /api/accounts/<id>`
- `DELETE /api/accounts/<id>`

账号列表元数据额外包含：

```json
{"rentable":true,"rented":false,"hasRentalHistory":true}
```

`hasRentalHistory=true` 表示账号存在任意历史租约，单删或批量删除将返回：

```http
409 Conflict
```

```json
{"error":"account has rental history"}
```

创建租约的 `accountId` 是内部 API 字段；管理页面使用邮箱下拉选项自动提交，管理员无需手工输入。

`PATCH` 支持布尔字段 `bound` 和 `rentable`。客户会话不能访问这些接口。

## 租约管理

### `GET /api/rentals`

返回租约数组：

```json
{
  "id": 18,
  "accountId": 7,
  "email": "customer@example.com",
  "startsAt": "2026-07-22T12:00:00Z",
  "expiresAt": null,
  "expiryMode": "never",
  "endedAt": null,
  "revokedAt": null,
  "lastAccessAt": "2026-07-22T12:10:00Z",
  "accessCount": 2,
  "status": "active"
}
```

`status` 为 `active`、`expired` 或 `revoked`。

### `POST /api/rentals`——指定到期时间

```json
{
  "accountId": 7,
  "expiryMode": "at",
  "expiresAt": "2026-08-01T12:00:00Z"
}
```

`accountId` 可省略，省略后自动分配。`expiresAt` 必须是带时区的 ISO 8601；服务端转换为 UTC。

### `POST /api/rentals`——永不过期

```json
{
  "expiryMode": "never"
}
```

永不过期租约的 `expiresAt` 为 `null`，直到管理员撤销。

成功状态为 `201`，响应额外包含只出现一次的：

```json
{"accessCode":"..."}
```

旧版 `durationMinutes` 创建参数不再支持，调用方必须迁移到 `expiryMode`。

### `POST /api/rentals/lookup`

通过完整租用码精确反查邮箱：

```json
{"accessCode":"完整租用码"}
```

成功返回租约元数据，其中包含 `email`。有效、已过期和已撤销租约均可查询。不存在返回 `404`；响应设置 `Cache-Control: no-store`。禁止使用 GET 查询参数传递租用码。

### `POST /api/rentals/<id>/reveal-code`

按需返回当前租用码：

```json
{"accessCode":"..."}
```

仅有效租约可查看，响应设置 `Cache-Control: no-store`。旧版本创建且没有密文的租约返回 `409`，需要重置。

### `POST /api/rentals/<id>/reset-code`

原子生成新租用码并返回：

```json
{"accessCode":"..."}
```

旧码哈希被替换，所有关联客户会话同时删除。租约邮箱、期限和访问统计保留。响应不缓存。

### `POST /api/rentals/<id>/revoke`

撤销租约并删除关联取件会话。

## 客户取件

### `POST /api/pickup/login`

```json
{"code":"管理员提供的取件码"}
```

成功设置 `pickup_session`：

```json
{
  "authenticated": true,
  "csrfToken": "...",
  "email": "customer@example.com",
  "expiresAt": "2026-07-22T13:00:00Z",
  "rentalExpiresAt": null,
  "permanent": true
}
```

其中 `expiresAt` 是当前短期会话期限；`rentalExpiresAt` 是租约期限，永久租约为 `null`。

### `GET /api/pickup/check-auth`

返回相同的认证元数据，未登录时返回：

```json
{"authenticated":false}
```

### `POST /api/pickup/mail`

```json
{"folder":"inbox","limit":100}
```

`folder` 只允许 `inbox` 和 `junkemail`。接口不接受账号 ID，邮箱由取件会话决定。

### `POST /api/pickup/verify-code`

```json
{"keyword":"openai","maxResults":100}
```

`keyword` 可省略。成功返回：

```json
{"code":"123456"}
```

### `POST /api/pickup/logout`

撤销当前客户会话。

## 常见状态码

- `400`：请求结构或时间参数无效；
- `401`：未认证、取件码无效或过期；
- `403`：Origin 或 CSRF 校验失败；
- `404`：资源或验证码不存在；
- `409`：没有可出租邮箱、邮箱授权失效或刷新繁忙；
- `429`：登录尝试或上游服务被限流；
- `502/503`：Microsoft Graph 响应异常或不可用。

所有 `/api/` 响应设置 `Cache-Control: no-store`，错误信息经过脱敏处理。
