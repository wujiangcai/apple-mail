# 小苹果安全开源发布实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将生产服务器上的邮箱管理器重建为默认安全、可测试、可复现，并以 GPL-3.0-only 发布的公开 GitHub 项目。

**架构：** Flask 应用工厂组合 PostgreSQL 存储、服务器会话、Fernet 凭据、Microsoft Graph 客户端和面向账号 ID 的 API。浏览器使用 ES modules 和内存状态，经 HTTPS 反向代理访问仅监听回环地址的 Gunicorn；邮件 HTML 在无脚本、无同源权限的 sandbox iframe 中显示。

**技术栈：** Python 3.12/3.13、Flask 3.1、psycopg 3、cryptography、requests、Gunicorn、PostgreSQL 17、Vanilla ES modules、Vitest、jsdom、Caddy、systemd、GitHub Actions。

---

## 文件结构

- 创建：`.gitattributes`，固定文本文件 LF 换行。
- 创建：`.gitignore`，排除虚拟环境、缓存、秘密、数据库、日志、构建产物和 worktree。
- 创建：`pyproject.toml`、`requirements.lock`，定义 Python 包、运行依赖、开发依赖和锁定版本。
- 创建：`package.json`、`package-lock.json`，定义前端测试和静态检查。
- 创建：`mail_manager/__init__.py`，提供 Flask 应用工厂、蓝图注册、代理策略和安全响应头。
- 创建：`mail_manager/config.py`，解析并验证环境变量。
- 创建：`mail_manager/models.py`，定义账号、会话、邮件和领域错误。
- 创建：`mail_manager/db.py`、`mail_manager/schema.sql`，提供 PostgreSQL 连接、事务、初始化 CLI 和 schema。
- 创建：`mail_manager/crypto.py`，提供版本化 Fernet 凭据加解密。
- 创建：`mail_manager/auth.py`，提供登录限流、服务器会话、CSRF 和认证蓝图。
- 创建：`mail_manager/accounts.py`，提供账号服务、PostgreSQL repository 和账号 API。
- 创建：`mail_manager/graph.py`，提供 OAuth 刷新、Graph 分页和上游错误分类。
- 创建：`mail_manager/mail.py`，提供令牌协调、邮件读取、验证码、绑定检测和邮件 API。
- 创建：`mail_manager/static/index.html`、`styles.css`，承载现有视觉语言但不包含内联脚本或事件属性。
- 创建：`mail_manager/static/js/api.js`，提供带 CSRF、超时和统一错误的 API 客户端。
- 创建：`mail_manager/static/js/state.js`，提供不持久化秘密的内存状态。
- 创建：`mail_manager/static/js/render.js`、`mail-frame.js`、`pagination.js`，提供安全 DOM 渲染、隔离邮件和分页。
- 创建：`mail_manager/static/js/app.js`，组合登录、账号、导入导出、绑定、邮件和验证码交互。
- 创建：`tests/` 与 `tests/frontend/` 下的后端、前端、静态安全和 PostgreSQL 集成测试。
- 创建：`deploy/mail-manager.service`、`deploy/Caddyfile`、`deploy/mail-manager.env.example`，提供默认安全部署样例。
- 创建：`LICENSE`、`README.md`、`SECURITY.md`、`CONTRIBUTING.md`，提供 GPL 和项目文档。
- 创建：`.github/workflows/ci.yml`，运行 Python、Node、PostgreSQL、依赖审计和秘密扫描。

### 任务 1：建立可测试项目骨架

**文件：**
- 创建：`.gitattributes`
- 创建：`.gitignore`
- 创建：`pyproject.toml`
- 创建：`package.json`
- 创建：`mail_manager/__init__.py`
- 创建：`tests/test_app_factory.py`

- [ ] **步骤 1：编写失败的包导入测试**

```python
from flask import Flask


def test_create_app_returns_flask_application():
    from mail_manager import create_app

    app = create_app({"TESTING": True})

    assert isinstance(app, Flask)
    assert app.name == "mail_manager"
```

- [ ] **步骤 2：验证测试因包不存在而失败**

运行：`python -m pytest tests/test_app_factory.py -v`

预期：FAIL，包含 `ModuleNotFoundError: No module named 'mail_manager'`。

- [ ] **步骤 3：创建最小应用工厂和项目清单**

`mail_manager/__init__.py` 的最小实现：

```python
from flask import Flask


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="")
    if test_config:
        app.config.update(test_config)
    return app
```

`pyproject.toml` 使用项目名 `xiaopingguo-mail-manager`、版本 `1.0.0`、`requires-python = ">=3.12"`，运行依赖包含 Flask、psycopg binary、cryptography、requests 和 gunicorn；开发依赖包含 pytest、pytest-cov、requests-mock、ruff、pip-audit 和 pip-tools。

`package.json` 设置 `type: module`，并定义 `test: vitest run`、`test:watch: vitest`、`audit: npm audit --audit-level=high`，开发依赖为 Vitest、jsdom 和 ESLint。

- [ ] **步骤 4：安装开发依赖并验证测试通过**

运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
npm install
.\.venv\Scripts\python -m pytest tests/test_app_factory.py -v
```

预期：`1 passed`。

- [ ] **步骤 5：提交骨架**

```powershell
git add .gitattributes .gitignore pyproject.toml package.json package-lock.json mail_manager tests/test_app_factory.py
git commit -m "build: scaffold secure mail manager"
```

### 任务 2：实现严格配置校验

**文件：**
- 创建：`mail_manager/config.py`
- 创建：`tests/test_config.py`
- 修改：`mail_manager/__init__.py`

- [ ] **步骤 1：编写缺失秘密和合法配置测试**

```python
import pytest

from mail_manager.config import Settings


VALID_ENV = {
    "DATABASE_URL": "postgresql://mail:test@127.0.0.1/mail_test",
    "ACCESS_PASSWORD": "a-strong-access-password",
    "SESSION_SECRET": "s" * 64,
    "CREDENTIALS_ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "PUBLIC_ORIGIN": "https://mail.example.com",
}


@pytest.mark.parametrize("missing", VALID_ENV)
def test_settings_reject_missing_required_value(missing):
    env = VALID_ENV | {missing: ""}
    with pytest.raises(ValueError, match=missing):
        Settings.from_mapping(env)


def test_settings_apply_safe_limits():
    settings = Settings.from_mapping(VALID_ENV)
    assert settings.cookie_secure is True
    assert settings.max_content_length == 2 * 1024 * 1024
    assert settings.import_limit == 1000
    assert settings.bound_check_limit == 20
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_config.py -v`

预期：FAIL，`mail_manager.config` 不存在。

- [ ] **步骤 3：实现不可变 Settings**

实现 `Settings.from_mapping()` 和 `Settings.from_environ()`，校验 PostgreSQL URL、至少 16 字符访问密码、至少 32 字符会话秘密、有效 Fernet key、HTTP/HTTPS 单一 origin，以及布尔代理开关。将 `MAX_CONTENT_LENGTH`、Cookie 名称和三个批量限制映射进 Flask config。

- [ ] **步骤 4：让应用工厂使用 Settings，并允许测试显式注入**

接口固定为：

```python
def create_app(
    test_config: dict | None = None,
    *,
    settings: Settings | None = None,
) -> Flask:
    ...
```

测试配置不隐式绕过验证；测试必须传入 `Settings.from_mapping(VALID_ENV)`。

- [ ] **步骤 5：运行配置和工厂测试**

运行：`.\.venv\Scripts\python -m pytest tests/test_config.py tests/test_app_factory.py -v`

预期：全部 PASS。

- [ ] **步骤 6：提交配置边界**

```powershell
git add mail_manager/config.py mail_manager/__init__.py tests/test_config.py tests/test_app_factory.py
git commit -m "feat: require secure runtime configuration"
```

### 任务 3：建立领域模型、数据库 schema 与初始化 CLI

**文件：**
- 创建：`mail_manager/models.py`
- 创建：`mail_manager/db.py`
- 创建：`mail_manager/schema.sql`
- 创建：`tests/test_db.py`
- 修改：`mail_manager/__init__.py`

- [ ] **步骤 1：编写 schema 和 CLI 失败测试**

```python
from pathlib import Path


def test_schema_contains_required_tables():
    sql = Path("mail_manager/schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS accounts" in sql
    assert "CREATE TABLE IF NOT EXISTS auth_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS login_failures" in sql


def test_init_db_cli_executes_complete_schema(app, monkeypatch):
    executed = []
    monkeypatch.setattr("mail_manager.db.execute_schema", executed.append)
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0
    assert len(executed) == 1
    assert "accounts" in executed[0]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_db.py -v`

预期：FAIL，schema 和 CLI 不存在。

- [ ] **步骤 3：实现 schema 与数据库工具**

`accounts` 使用 bigint ID、规范化邮箱唯一键、版本化密码密文和 refresh token 密文、client ID、bound、bound_at、token_valid、last_refresh_time 和时间戳。`auth_sessions` 保存 session SHA-256、过期时间和创建时间。`login_failures` 保存 client_key、failure_count、locked_until、updated_at。

`db.py` 提供 `connect(database_url)`、`transaction(connection)`、`execute_schema(sql)`、`init_db_command()`，全部使用上下文管理器确定性提交、回滚和关闭。

- [ ] **步骤 4：运行数据库单元测试**

运行：`.\.venv\Scripts\python -m pytest tests/test_db.py -v`

预期：全部 PASS。

- [ ] **步骤 5：提交数据库基础**

```powershell
git add mail_manager/models.py mail_manager/db.py mail_manager/schema.sql mail_manager/__init__.py tests/test_db.py
git commit -m "feat: add explicit postgres schema lifecycle"
```

### 任务 4：加密邮箱凭据

**文件：**
- 创建：`mail_manager/crypto.py`
- 创建：`tests/test_crypto.py`

- [ ] **步骤 1：编写密文版本和篡改测试**

```python
import pytest
from cryptography.fernet import Fernet, InvalidToken

from mail_manager.crypto import CredentialCipher


def test_ciphertext_is_versioned_and_round_trips():
    cipher = CredentialCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt("refresh-secret")
    assert encrypted.startswith("v1:")
    assert "refresh-secret" not in encrypted
    assert cipher.decrypt(encrypted) == "refresh-secret"


def test_cipher_rejects_unknown_version():
    cipher = CredentialCipher(Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        cipher.decrypt("v2:not-supported")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_crypto.py -v`

预期：FAIL，`CredentialCipher` 不存在。

- [ ] **步骤 3：实现 CredentialCipher**

实现 `encrypt(str) -> str`、`decrypt(str) -> str`，密文格式固定为 `v1:` 加 Fernet token。空密码允许加密为空字符串，空 refresh token 在账号导入层拒绝。

- [ ] **步骤 4：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_crypto.py -v`

预期：全部 PASS。

```powershell
git add mail_manager/crypto.py tests/test_crypto.py
git commit -m "feat: encrypt stored mailbox credentials"
```

### 任务 5：实现服务器会话、CSRF 和登录限流

**文件：**
- 创建：`mail_manager/auth.py`
- 创建：`tests/fakes.py`
- 创建：`tests/test_auth.py`
- 修改：`mail_manager/__init__.py`

- [ ] **步骤 1：编写登录、撤销和 CSRF 失败测试**

```python
def test_login_sets_secure_server_session(client, auth_repo):
    response = client.post("/api/login", json={"password": "a-strong-access-password"})
    assert response.status_code == 200
    assert "Secure" in response.headers["Set-Cookie"]
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=Strict" in response.headers["Set-Cookie"]
    assert response.get_json()["csrfToken"]
    assert len(auth_repo.sessions) == 1


def test_logout_revokes_session(client, logged_in):
    response = client.post("/api/logout", headers=logged_in.csrf_headers)
    assert response.status_code == 200
    assert client.get("/api/check-auth").get_json()["authenticated"] is False


def test_state_change_rejects_missing_csrf(client, logged_in):
    response = client.post("/api/logout")
    assert response.status_code == 403
```

- [ ] **步骤 2：编写伪造 X-Forwarded-For 和限流测试**

```python
def test_direct_request_ignores_forwarded_for(client, auth_repo):
    for value in ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5"]:
        client.post("/api/login", json={"password": "wrong"}, headers={"X-Forwarded-For": value})
    assert auth_repo.failure_keys == {"127.0.0.1"}
```

- [ ] **步骤 3：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_auth.py -v`

预期：FAIL，认证蓝图不存在。

- [ ] **步骤 4：实现认证服务与 repository**

实现 SHA-256 session ID 存储、HMAC 派生 CSRF token、常量时间密码比较、8 小时到期、POST-only logout、同源 Origin 校验、每地址 5 次失败锁定 15 分钟。只有 `TRUST_PROXY=true` 时启用 `ProxyFix(x_for=1, x_proto=1)`；默认使用 `remote_addr`。

- [ ] **步骤 5：运行认证测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_auth.py -v`

预期：全部 PASS。

```powershell
git add mail_manager/auth.py mail_manager/__init__.py tests/fakes.py tests/test_auth.py
git commit -m "feat: add revocable sessions and csrf protection"
```

### 任务 6：实现安全账号管理 API

**文件：**
- 创建：`mail_manager/accounts.py`
- 创建：`tests/test_accounts.py`
- 修改：`mail_manager/models.py`
- 修改：`mail_manager/__init__.py`

- [ ] **步骤 1：编写元数据、导入上限和按需查看测试**

```python
def test_account_list_never_returns_secrets(auth_client, account_repo):
    account_repo.add_plain("user@example.com", "mail-password", "client", "refresh-token")
    payload = auth_client.get("/api/accounts").get_json()
    assert payload[0]["email"] == "user@example.com"
    assert "password" not in payload[0]
    assert "refreshToken" not in payload[0]


def test_import_rejects_more_than_configured_limit(auth_client):
    accounts = [{"email": f"u{i}@example.com", "clientId": "c", "refreshToken": "r"} for i in range(1001)]
    response = auth_client.post("/api/accounts/import", json={"accounts": accounts})
    assert response.status_code == 413


def test_reveal_is_post_no_store_and_csrf_protected(auth_client, account_repo):
    account_id = account_repo.add_plain("user@example.com", "p", "c", "r")
    response = auth_client.post(f"/api/accounts/{account_id}/reveal")
    assert response.get_json()["refreshToken"] == "r"
    assert response.headers["Cache-Control"] == "no-store"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_accounts.py -v`

预期：FAIL，账号蓝图不存在。

- [ ] **步骤 3：实现 AccountsService 和 PostgreSQL repository**

固定 API：`GET /api/accounts`、`POST /api/accounts/import`、`POST /api/accounts/{id}/reveal`、`POST /api/accounts/export`、`PATCH /api/accounts/{id}`、`DELETE /api/accounts/{id}`、`POST /api/accounts/delete-batch`。导入按规范化邮箱 upsert，并在新 token 成功写入时设置 `token_valid=true`。

- [ ] **步骤 4：验证事务、错误码和导出格式**

增加测试确保批量导入全有或全无、重复邮箱更新密文、导出内容只出现在 POST 响应且带 attachment/no-store、未知 ID 返回 404。

- [ ] **步骤 5：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_accounts.py -v`

预期：全部 PASS。

```powershell
git add mail_manager/accounts.py mail_manager/models.py mail_manager/__init__.py tests/test_accounts.py
git commit -m "feat: expose metadata-first account api"
```

### 任务 7：实现可测试 Microsoft Graph 客户端

**文件：**
- 创建：`mail_manager/graph.py`
- 创建：`tests/test_graph.py`

- [ ] **步骤 1：编写 OAuth 错误分类测试**

```python
import pytest
from mail_manager.graph import GraphClient, InvalidGrant, UpstreamRateLimited


def test_invalid_grant_is_distinct(requests_mock):
    requests_mock.post(GraphClient.TOKEN_URL, status_code=400, json={"error": "invalid_grant"})
    with pytest.raises(InvalidGrant):
        GraphClient().refresh("client", "refresh")


def test_rate_limit_is_retryable(requests_mock):
    requests_mock.post(GraphClient.TOKEN_URL, status_code=429, headers={"Retry-After": "10"})
    with pytest.raises(UpstreamRateLimited) as error:
        GraphClient().refresh("client", "refresh")
    assert error.value.retry_after == 10
```

- [ ] **步骤 2：编写分页与 nextLink 主机校验测试**

测试两页 Graph 响应合并到指定总量，并拒绝 hostname 不是 `graph.microsoft.com` 的 `@odata.nextLink`。

- [ ] **步骤 3：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_graph.py -v`

预期：FAIL，GraphClient 不存在。

- [ ] **步骤 4：实现 GraphClient**

使用 `requests.Session`，连接/读取超时固定为 `(5, 15)`；OAuth scope 仅保留 `offline_access openid profile Mail.Read`。实现 `refresh()`、`list_messages(access_token, folder, limit)`，分类 `InvalidGrant`、`UpstreamRateLimited`、`UpstreamUnavailable` 和协议错误。

- [ ] **步骤 5：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_graph.py -v`

预期：全部 PASS。

```powershell
git add mail_manager/graph.py tests/test_graph.py
git commit -m "feat: add bounded microsoft graph client"
```

### 任务 8：实现无丢失的令牌协调与邮件服务

**文件：**
- 创建：`mail_manager/mail.py`
- 创建：`tests/test_mail.py`
- 修改：`mail_manager/accounts.py`
- 修改：`mail_manager/models.py`

- [ ] **步骤 1：编写刷新先持久化后取件测试**

```python
def test_rotated_token_is_persisted_before_graph_read(service_factory):
    events = []
    mail_service, account_repo, graph = service_factory(events)
    account_id = account_repo.add_plain("u@example.com", "p", "client", "old-token")
    graph.refresh_result = ("access", "new-token")
    graph.list_error = RuntimeError("graph unavailable")

    with pytest.raises(RuntimeError):
        mail_service.list_messages(account_id, "inbox", 50)

    assert account_repo.revealed(account_id).refresh_token == "new-token"
    assert account_repo.metadata(account_id).token_valid is True
    assert events[:3] == ["lock", "refresh", "persist-token"]
```

- [ ] **步骤 2：编写错误状态和串行锁测试**

测试 `InvalidGrant` 才把 `token_valid` 置 false；429、超时和 5xx 保持原状态。使用带屏障的 fake repository 启动两个线程，断言同一 account ID 的刷新租约最多只有一个持有者，不同 account ID 可以并行刷新。

- [ ] **步骤 3：编写绑定域名和验证码测试**

```python
@pytest.mark.parametrize("sender", ["noreply@openai.com", "verify@tm.openai.com"])
def test_trusted_openai_sender(sender):
    assert is_openai_sender(sender) is True


@pytest.mark.parametrize("sender", ["openai.com@evil.test", "user@notopenai.com"])
def test_spoofed_openai_sender(sender):
    assert is_openai_sender(sender) is False
```

- [ ] **步骤 4：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_mail.py -v`

预期：FAIL，MailService 和辅助函数不存在。

- [ ] **步骤 5：实现 MailService 和 PostgreSQL 短事务租约**

固定 API：`POST /api/accounts/{id}/mail`、`POST /api/accounts/{id}/verify-code`、`POST /api/accounts/check-bound`、`POST /api/accounts/random-unbound`。前端只提交 ID、folder、limit、keyword；后端用短事务领取带过期时间的账号刷新租约，在事务外完成 OAuth，再用租约 token 做 CAS 加密持久化，成功提交后读取 Graph 邮件。旧租约不得覆盖较新的刷新结果。

绑定检测每批最多 20 个 ID、并发最多 4，结果顺序与请求一致；邮件总量最多 100。验证码只从主题和 body preview 提取，返回来源邮件元数据。

- [ ] **步骤 6：运行测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_mail.py -v`

预期：全部 PASS。

```powershell
git add mail_manager/mail.py mail_manager/accounts.py mail_manager/models.py tests/test_mail.py
git commit -m "feat: serialize token rotation and mail access"
```

### 任务 9：组合 API 并设置浏览器安全边界

**文件：**
- 修改：`mail_manager/__init__.py`
- 创建：`tests/test_security.py`

- [ ] **步骤 1：编写安全头和缓存测试**

```python
def test_index_has_strict_browser_headers(client):
    response = client.get("/")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_sensitive_api_is_never_cached(auth_client):
    response = auth_client.get("/api/accounts")
    assert response.headers["Cache-Control"] == "no-store"
```

- [ ] **步骤 2：编写未认证和 JSON 错误测试**

测试除 login/check-auth 外所有 API 返回 JSON 401；超出 2 MiB 返回 JSON 413；上游 429 映射 503 并保留 `Retry-After`；内部异常不向客户端返回数据库 URL或 token。

- [ ] **步骤 3：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_security.py -v`

预期：FAIL，安全头或统一错误处理缺失。

- [ ] **步骤 4：实现蓝图组合、静态入口、安全头和错误处理**

CSP 固定为 `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`。HTTPS 响应增加 HSTS；API 一律 no-store。

- [ ] **步骤 5：运行全部后端单元测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests -m "not postgres" -v`

预期：全部 PASS。

```powershell
git add mail_manager/__init__.py tests/test_security.py
git commit -m "feat: enforce application security boundaries"
```

### 任务 10：建立无秘密持久化的前端 API 与状态层

**文件：**
- 创建：`mail_manager/static/js/api.js`
- 创建：`mail_manager/static/js/state.js`
- 创建：`tests/frontend/api.test.js`
- 创建：`tests/frontend/state.test.js`

- [ ] **步骤 1：编写 CSRF、超时和无 localStorage 测试**

```javascript
import { beforeEach, expect, test, vi } from "vitest";
import { ApiClient } from "../../mail_manager/static/js/api.js";

beforeEach(() => { global.fetch = vi.fn(); });

test("state-changing requests send in-memory CSRF token", async () => {
  fetch.mockResolvedValue(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
  const api = new ApiClient({ csrfToken: "csrf", timeoutMs: 1000 });
  await api.post("/api/accounts/import", { accounts: [] });
  expect(fetch.mock.calls[0][1].headers["X-CSRF-Token"]).toBe("csrf");
});

test("mail request contains account id but no refresh token", async () => {
  fetch.mockResolvedValue(new Response("{}", { status: 200 }));
  const api = new ApiClient({ csrfToken: "csrf" });
  await api.post("/api/accounts/7/mail", { folder: "inbox" });
  expect(fetch.mock.calls[0][0]).toBe("/api/accounts/7/mail");
  expect(fetch.mock.calls[0][0]).not.toContain("refresh_token");
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`npm test -- tests/frontend/api.test.js tests/frontend/state.test.js`

预期：FAIL，模块不存在。

- [ ] **步骤 3：实现 ApiClient 和 createState**

`ApiClient` 提供 `get/post/patch/delete`，统一 JSON 解析、`AbortController` 超时、401 事件、非 2xx `ApiError` 和 CSRF header。`createState()` 仅保存账号元数据、分页、当前邮箱和临时 reveal 值，并禁止任何 Web Storage 调用。

- [ ] **步骤 4：运行测试并提交**

运行：`npm test -- tests/frontend/api.test.js tests/frontend/state.test.js`

预期：全部 PASS。

```powershell
git add mail_manager/static/js/api.js mail_manager/static/js/state.js tests/frontend/api.test.js tests/frontend/state.test.js
git commit -m "feat: add ephemeral frontend data layer"
```

### 任务 11：实现安全 DOM 与隔离邮件正文

**文件：**
- 创建：`mail_manager/static/js/render.js`
- 创建：`mail_manager/static/js/mail-frame.js`
- 创建：`mail_manager/static/js/pagination.js`
- 创建：`tests/frontend/render.test.js`
- 创建：`tests/frontend/mail-frame.test.js`
- 创建：`tests/frontend/pagination.test.js`

- [ ] **步骤 1：编写恶意字段文本渲染测试**

```javascript
import { expect, test } from "vitest";
import { renderAccountRow } from "../../mail_manager/static/js/render.js";

test("account metadata is rendered as text", () => {
  document.body.innerHTML = "<table><tbody id='rows'></tbody></table>";
  renderAccountRow(document.querySelector("#rows"), {
    id: 1,
    email: "<img src=x onerror=alert(1)>",
    bound: false,
    tokenValid: true,
  });
  expect(document.querySelector("img")).toBeNull();
  expect(document.body.textContent).toContain("<img src=x onerror=alert(1)>");
});
```

- [ ] **步骤 2：编写 sandbox iframe 测试**

```javascript
import { createMailFrame } from "../../mail_manager/static/js/mail-frame.js";

test("mail HTML receives an opaque scriptless sandbox", () => {
  const frame = createMailFrame("<img src=x onerror=alert(1)><script>alert(2)</script>");
  expect(frame.getAttribute("sandbox")).toBe("");
  expect(frame.referrerPolicy).toBe("no-referrer");
  expect(frame.srcdoc).toContain("default-src 'none'");
  expect(frame.srcdoc).toContain("script-src 'none'");
});
```

- [ ] **步骤 3：运行测试验证失败**

运行：`npm test -- tests/frontend/render.test.js tests/frontend/mail-frame.test.js tests/frontend/pagination.test.js`

预期：FAIL，渲染模块不存在。

- [ ] **步骤 4：实现 DOM 构建、邮件 frame 和纯分页函数**

所有普通字段通过 `document.createElement()` 和 `textContent`。邮件 iframe 使用空 sandbox、`referrerPolicy=no-referrer`，srcdoc 头部固定 `default-src 'none'; script-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'`。分页函数将字符串输入转为整数并夹在有效范围内。

- [ ] **步骤 5：运行测试并提交**

运行：`npm test -- tests/frontend/render.test.js tests/frontend/mail-frame.test.js tests/frontend/pagination.test.js`

预期：全部 PASS。

```powershell
git add mail_manager/static/js/render.js mail_manager/static/js/mail-frame.js mail_manager/static/js/pagination.js tests/frontend
git commit -m "feat: render untrusted mail in an isolated frame"
```

### 任务 12：迁移完整前端功能并消除异步竞态

**文件：**
- 创建：`mail_manager/static/index.html`
- 创建：`mail_manager/static/styles.css`
- 创建：`mail_manager/static/js/app.js`
- 创建：`tests/frontend/app.test.js`
- 创建：`tests/frontend/static-security.test.js`

- [ ] **步骤 1：编写导入、删除失败回滚和静态安全测试**

```javascript
test("failed delete leaves account in state", async () => {
  const state = createState([{ id: 7, email: "u@example.com" }]);
  const api = { delete: vi.fn().mockRejectedValue(new Error("failed")) };
  await expect(deleteAccount({ api, state, id: 7 })).rejects.toThrow("failed");
  expect(state.accounts.map((item) => item.id)).toEqual([7]);
});

test("published HTML has no inline event handlers", async () => {
  const html = await fs.readFile("mail_manager/static/index.html", "utf8");
  expect(html).not.toMatch(/\son[a-z]+\s*=/i);
  expect(html).not.toMatch(/<script(?![^>]+src=)/i);
});
```

静态 JS 扫描额外断言不存在 `localStorage`、`sessionStorage`、`.innerHTML`、`insertAdjacentHTML`、`refresh_token=` 和 `password=` URL 片段。

- [ ] **步骤 2：运行测试验证失败**

运行：`npm test -- tests/frontend/app.test.js tests/frontend/static-security.test.js`

预期：FAIL，页面和控制器不存在。

- [ ] **步骤 3：实现页面结构与 app 控制器**

保留当前仪表盘、账号表格、搜索、分页、文件/粘贴导入、导出、批量删除、绑定检测、收件箱/垃圾箱、邮件详情和验证码交互。所有事件通过 `addEventListener` 或容器事件委托绑定；变更操作先 await API，成功后重新获取服务端元数据，失败时显示错误且不改本地状态。

复制/导出秘密时调用 reveal/export POST；单次 reveal 在 60 秒后从内存清除。页面不加载第三方 CDN，图标使用本地 SVG 或文本。

- [ ] **步骤 4：运行全部前端测试**

运行：`npm test`

预期：全部 PASS。

- [ ] **步骤 5：提交安全前端**

```powershell
git add mail_manager/static tests/frontend
git commit -m "feat: migrate secure mailbox dashboard"
```

### 任务 13：添加真实 PostgreSQL 集成测试与可复现锁文件

**文件：**
- 创建：`tests/test_postgres_integration.py`
- 创建：`requirements.lock`
- 修改：`pyproject.toml`
- 修改：`package-lock.json`

- [ ] **步骤 1：编写标记为 postgres 的集成测试**

测试在独立数据库执行 schema、加密账号 round-trip、session 创建/撤销、登录失败 upsert、同 account 租约互斥、不同 account 刷新并行、token invalid_grant 状态迁移和批量 repository 事务回滚。测试从 `TEST_DATABASE_URL` 读取连接，变量缺失时明确 skip。

- [ ] **步骤 2：运行本地单元套件确保不依赖 PostgreSQL**

运行：`.\.venv\Scripts\python -m pytest tests -m "not postgres" -v`

预期：全部 PASS，PostgreSQL 测试未运行。

- [ ] **步骤 3：生成带哈希的 Python 锁和 npm 锁**

运行：

```powershell
.\.venv\Scripts\python -m piptools compile --extra dev --generate-hashes --output-file requirements.lock pyproject.toml
npm install --package-lock-only
```

预期：命令退出 0，锁文件不包含本地绝对路径。

- [ ] **步骤 4：从锁文件重建临时环境验证可复现**

运行：

```powershell
python -m venv .lock-verify
.\.lock-verify\Scripts\python -m pip install --require-hashes -r requirements.lock
.\.lock-verify\Scripts\python -m pip install -e . --no-deps
.\.lock-verify\Scripts\python -m pytest tests -m "not postgres" -q
```

预期：测试全部 PASS；`.lock-verify/` 保持在 `.gitignore` 中且不进入提交。

- [ ] **步骤 5：提交集成测试和锁文件**

```powershell
git add pyproject.toml requirements.lock package.json package-lock.json tests/test_postgres_integration.py
git commit -m "test: add postgres integration coverage"
```

### 任务 14：添加 GPL、部署样例和项目文档

**文件：**
- 创建：`LICENSE`
- 创建：`README.md`
- 创建：`SECURITY.md`
- 创建：`CONTRIBUTING.md`
- 创建：`deploy/mail-manager.service`
- 创建：`deploy/Caddyfile`
- 创建：`deploy/mail-manager.env.example`
- 创建：`tests/test_distribution_files.py`

- [ ] **步骤 1：编写部署安全断言测试**

```python
from pathlib import Path


def test_systemd_unit_is_non_root_and_loopback_only():
    unit = Path("deploy/mail-manager.service").read_text(encoding="utf-8")
    assert "User=mail-manager" in unit
    assert "127.0.0.1:5000" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "Environment=ACCESS_PASSWORD=" not in unit


def test_license_is_gpl_v3():
    license_text = Path("LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 29 June 2007" in license_text
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_distribution_files.py -v`

预期：FAIL，发布文件不存在。

- [ ] **步骤 3：写入完整 GPL-3.0 文本和文档**

LICENSE 使用 GNU 官方 GPL v3 完整文本。README 给出架构、功能、生成三个秘密的命令、开发安装、init-db、测试、Caddy/systemd 部署、升级、备份和禁止公开 Gunicorn 的说明。SECURITY 使用 GitHub Private Vulnerability Reporting；CONTRIBUTING 要求红绿测试、无秘密提交和 GPL 兼容贡献。

- [ ] **步骤 4：实现 hardened systemd 与 Caddy 样例**

systemd 使用 `User=mail-manager`、回环 Gunicorn、`ExecStartPre` init-db、`EnvironmentFile=/etc/mail-manager/mail-manager.env`、完整 hardening 和资源上限。Caddy 使用 `{$MAIL_MANAGER_DOMAIN:mail.example.com}`、反代 `127.0.0.1:5000`、2 MiB 请求体、超时和安全头。env example 的秘密值保持为空，并给出生成命令注释。

- [ ] **步骤 5：运行分发测试并提交**

运行：`.\.venv\Scripts\python -m pytest tests/test_distribution_files.py -v`

预期：全部 PASS。

```powershell
git add LICENSE README.md SECURITY.md CONTRIBUTING.md deploy tests/test_distribution_files.py
git commit -m "docs: add gpl release and hardened deployment"
```

### 任务 15：添加 CI、审计和发布门禁

**文件：**
- 创建：`.github/workflows/ci.yml`
- 创建：`.github/dependabot.yml`
- 创建：`tests/test_ci_config.py`

- [ ] **步骤 1：编写 CI 配置测试**

测试 workflow 包含 Python 3.12/3.13、Node 当前 LTS、PostgreSQL 17 service、`pytest` 单元与 postgres 集成、Vitest、ruff、pip-audit、npm audit、compileall 和 gitleaks；workflow 仅使用固定 major/tag 的公开 actions。

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ci_config.py -v`

预期：FAIL，CI 文件不存在。

- [ ] **步骤 3：实现 GitHub Actions 和 Dependabot**

CI 安装 `requirements.lock`、`npm ci`，为 PostgreSQL job 设置 `TEST_DATABASE_URL`，运行全部测试和审计。gitleaks 扫描完整 Git 历史。Dependabot 每周检查 pip、npm 和 github-actions。

- [ ] **步骤 4：运行本地发布门禁**

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m compileall -q mail_manager tests
.\.venv\Scripts\python -m pytest tests -m "not postgres" --cov=mail_manager --cov-fail-under=85
npm test
.\.venv\Scripts\python -m pip_audit -r requirements.lock
npm audit --audit-level=high
git grep -n -I -E "(apple2024|mailpass2024|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
```

预期：ruff、compileall、pytest、Vitest 和两个审计退出 0；秘密 grep 无匹配并以 1 退出。

- [ ] **步骤 5：提交 CI**

```powershell
git add .github tests/test_ci_config.py
git commit -m "ci: enforce tests audits and secret scanning"
```

### 任务 16：最终审查并发布到 GitHub

**文件：**
- 修改：计划执行期间发现的缺陷文件
- 不创建：生产秘密或服务器备份

- [ ] **步骤 1：调用 requesting-code-review 审查完整分支**

审查范围从规格提交 `855a4ad` 到当前 HEAD，重点复查 XSS、凭据边界、会话、CSRF、token 竞态、PostgreSQL 事务、部署权限和测试缺口。修复每个必须修复项并重跑对应测试。

- [ ] **步骤 2：运行完整本地验证**

运行任务 15 的完整门禁，并执行：

```powershell
git status --short
git log --oneline --decorate -20
git ls-files | Select-String -Pattern "(^|/)(venv|\.venv|__pycache__|.*\.bak)(/|$)"
```

预期：工作树为空；没有被跟踪的环境、缓存或备份。

- [ ] **步骤 3：安装 portable GitHub CLI**

```powershell
$release = Invoke-RestMethod -Headers @{"User-Agent"="xiaopingguo-release"} -Uri "https://api.github.com/repos/cli/cli/releases/latest"
$asset = $release.assets | Where-Object { $_.name -match "windows_amd64\.zip$" } | Select-Object -First 1
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\xiaopingguo-tools" | Out-Null
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$env:TEMP\gh.zip"
Expand-Archive -LiteralPath "$env:TEMP\gh.zip" -DestinationPath "$env:LOCALAPPDATA\xiaopingguo-tools" -Force
$gh = Get-ChildItem "$env:LOCALAPPDATA\xiaopingguo-tools" -Recurse -Filter gh.exe | Select-Object -First 1 -ExpandProperty FullName
& $gh --version
```

预期：输出 GitHub CLI 版本。下载和解压只写用户本地工具目录。

- [ ] **步骤 4：浏览器登录并核实账号**

运行：`& $gh auth login --hostname github.com --git-protocol https --web`

用户在浏览器完成授权后运行：

```powershell
& $gh auth status
& $gh api user --jq ".login"
```

预期：认证成功，并显示仓库所有者账号。

- [ ] **步骤 5：以私有暂存方式创建并推送仓库**

```powershell
& $gh repo create "小苹果" --private --source . --remote origin --push --description "小苹果邮箱助手：基于 Flask、PostgreSQL 与 Microsoft Graph 的安全邮箱管理工具"
& $gh repo view --json name,visibility,url
```

预期：名称为 `小苹果`，visibility 为 PRIVATE。若 GitHub 拒绝非 ASCII 名称，停止发布并向用户报告平台返回的原始校验错误，不擅自改名。

- [ ] **步骤 6：等待真实 PostgreSQL CI 通过**

```powershell
& $gh run list --limit 1
& $gh run watch --exit-status
```

预期：Python 两个版本、PostgreSQL 集成、前端、审计和秘密扫描全部成功。

- [ ] **步骤 7：公开仓库并验证 GPL 识别**

```powershell
& $gh repo edit --visibility public --accept-visibility-change-consequences
& $gh repo edit --enable-vulnerability-alerts --enable-automated-security-fixes
& $gh repo view --json name,visibility,licenseInfo,url,defaultBranchRef
```

预期：visibility 为 PUBLIC，默认分支 `main`，licenseInfo SPDX 为 `GPL-3.0`。

- [ ] **步骤 8：创建首个版本标签并验证远端**

```powershell
git tag -a v1.0.0 -m "小苹果 v1.0.0"
git push origin v1.0.0
& $gh release create v1.0.0 --title "小苹果 v1.0.0" --notes-from-tag
git ls-remote --heads --tags origin
```

预期：`main`、`v1.0.0` 和 GitHub Release 均存在；最终向用户提供公开仓库 URL 和 CI 运行 URL。
