下面是一份可直接执行的生产级实现方案。当前工作区为 Git 仓库但处于只读沙箱，无法创建 `tmp-docs/fastapi-url-shortener-implementation-plan.md`；计划内容已完整列出。

## 1. 技术方案

- Python 3.12
- FastAPI + Pydantic v2
- SQLAlchemy 2.x Async
- PostgreSQL 16 作为生产数据库
- SQLite 仅用于快速单元测试
- Alembic 数据库迁移
- pytest + pytest-asyncio + httpx
- Uvicorn/Gunicorn
- Docker Compose

持久化依赖 PostgreSQL，应用进程不保存业务状态。过期判断直接基于数据库查询条件完成，清理任务只负责归档或删除，不参与重定向正确性。

## 2. 数据模型

表：`short_links`

| 字段 | 说明 |
|---|---|
| `id` | UUID 或 bigint 主键 |
| `code` | 短码，唯一，长度 3-64 |
| `target_url` | 原始 URL，最长 2048 |
| `expires_at` | UTC 过期时间，可为空 |
| `is_active` | 软禁用标志 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

核心索引：

```sql
UNIQUE (code)
INDEX (expires_at)
INDEX (is_active)
```

有效短链接查询条件：

```sql
is_active = true
AND (expires_at IS NULL OR expires_at > now())
```

创建时依赖数据库唯一约束处理并发：

- 自定义别名冲突：返回 `409 Conflict`
- 随机短码碰撞：有限次数重试
- 超过重试次数：返回 `503 Service Unavailable`

## 3. API 设计

### 创建短链接

`POST /api/v1/links`

请求：

```json
{
  "target_url": "https://example.com/docs",
  "alias": "docs",
  "expires_at": "2027-01-01T00:00:00Z"
}
```

响应 `201 Created`：

```json
{
  "id": "uuid",
  "code": "docs",
  "short_url": "https://short.example/docs",
  "target_url": "https://example.com/docs",
  "expires_at": "2027-01-01T00:00:00Z",
  "created_at": "2026-08-16T10:00:00Z"
}
```

状态码：

- `400`：URL、别名或过期时间非法
- `409`：别名已存在
- `422`：请求结构错误

### 重定向

`GET /{code}`

- 命中：返回 `307 Temporary Redirect`
- 可通过配置切换为 `308 Permanent Redirect`
- `Location` 保留原始请求的 query string
- 不存在、已禁用、已过期统一返回 `404`

### 查询元数据

`GET /api/v1/links/{code}`

返回短链接信息，不执行跳转。

### 禁用链接

`DELETE /api/v1/links/{code}`

- 成功：`204 No Content`
- 不存在：`404`

### 健康检查

- `GET /healthz`：进程存活检查
- `GET /readyz`：数据库连接检查，数据库不可用时返回 `503`

错误统一采用 RFC 7807 风格：

```json
{
  "type": "https://example.com/problems/link-not-found",
  "title": "Link not found",
  "status": 404,
  "detail": "The short link does not exist or is no longer active.",
  "request_id": "..."
}
```

## 4. 文件布局

```text
.
├── app/
│   ├── main.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── errors.py
│   ├── db/
│   │   ├── session.py
│   │   └── models.py
│   ├── api/
│   │   └── v1/
│   │       ├── router.py
│   │       ├── links.py
│   │       ├── redirect.py
│   │       ├── health.py
│   │       └── schemas.py
│   ├── repositories/
│   │   └── links.py
│   ├── services/
│   │   ├── link_service.py
│   │   └── code_generator.py
│   └── tasks/
│       └── expiry_cleanup.py
├── migrations/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_code_generator.py
│   │   ├── test_schemas.py
│   │   └── test_link_service.py
│   ├── integration/
│   │   ├── test_create_links.py
│   │   ├── test_redirects.py
│   │   └── test_expiration.py
│   └── test_contracts.py
├── scripts/
│   └── wait_for_db.py
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── alembic.ini
├── .env.example
└── README.md
```

## 5. 核心模块职责

- `config.py`：环境变量、数据库 URL、基础域名、重定向状态码
- `session.py`：异步 Engine、连接池、事务 session
- `models.py`：SQLAlchemy 模型和索引
- `code_generator.py`：安全随机短码、base62 编码、别名格式校验
- `repositories/links.py`：数据库读写、唯一约束异常转换
- `link_service.py`：创建、查询、禁用和过期业务规则
- `redirect.py`：只负责解析 code、调用 service、生成重定向响应
- `errors.py`：领域异常转换为统一 JSON 错误
- `expiry_cleanup.py`：可选清理或归档任务，不承担业务正确性

别名限制：

```text
[A-Za-z0-9_-]
长度 3-64
```

目标 URL 仅允许：

```text
http://
https://
```

拒绝 `javascript:`、`data:` 等协议，防止开放重定向和脚本注入风险。

## 6. 实施顺序

1. 初始化 `pyproject.toml`、配置、日志、异常模型和 Pydantic schemas。
2. 建立 SQLAlchemy 模型、数据库 session 和 Alembic 初始迁移。
3. 实现短码生成、别名校验和 repository。
4. 实现 service 层及创建、查询、禁用接口。
5. 实现短码重定向和过期判断。
6. 增加健康检查、request ID、结构化访问日志。
7. 完成 Docker Compose、迁移执行流程和部署文档。
8. 完成 PostgreSQL 集成测试、并发测试和安全测试。
9. 配置 CI：格式检查、类型检查、单元测试、PostgreSQL 集成测试。

## 7. 验收标准

必须自动化覆盖：

- 创建成功返回 `201`
- 自定义别名重复返回 `409`
- 随机短码生成满足字符集和长度要求
- 非 HTTP(S) URL 被拒绝
- 过去、当前边界和未来过期时间行为正确
- 过期链接返回 `404`
- 禁用链接返回 `404`
- 重定向返回正确 `Location`
- query string 完整保留
- 未知 code 返回 `404`
- 两个并发请求创建同一别名时只能一个成功
- 数据库连接失败时 `/readyz` 返回 `503`
- 事务失败不会留下半条记录
- 应用重启后短链接仍然存在
- Alembic 迁移可重复执行
- 错误响应不泄露数据库内部异常
- 日志不记录数据库密码或其他敏感配置
- Docker Compose 能启动应用并完成数据库初始化

此外，`README.md` 应包含配置项、迁移命令、启动命令、备份恢复、回滚策略和测试命令。