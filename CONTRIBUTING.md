# 贡献指南

## 开发流程

1. 从最新默认分支创建短生命周期分支，每个变更只解决一个明确问题。
2. 对功能和缺陷修复使用红-绿-重构：先提交能复现需求的失败测试，再写最小实现使其通过，最后在绿灯下整理代码。
3. 运行 `python -m pytest tests -m "not postgres"`、`python -m ruff check .`、`python -m compileall -q mail_manager tests` 和 `npm test`。
4. 涉及数据库 repository、schema、事务或 advisory lock 时，还需使用专用数据库运行 PostgreSQL 集成测试。
5. 拉取请求说明行为变化、测试证据、部署影响和兼容性；不要只提供文件清单。

## 安全要求

任何提交都不得包含真实秘密、邮箱数据、token、Cookie、数据库备份、日志、`.env`、虚拟环境、缓存或 `.bak` 文件。测试使用明显的虚构域名和随机凭据。发现漏洞时遵循 `SECURITY.md` 的私密报告流程，不要先创建公开 Issue。

## 代码与许可

Python 支持版本为 3.12 和 3.13。保持接口边界清晰，SQL 使用参数化查询，浏览器不得持久化邮箱密码或 refresh token。依赖变更必须同步更新带哈希的 `requirements.lock` 或 `package-lock.json`，并通过依赖审计。

提交贡献即表示你有权按 `GPL-3.0-only` 提供该内容，并同意项目在相同许可证下分发。引入第三方代码或素材前必须确认其许可证与 GPLv3 兼容，并保留必要的版权和许可声明。
