当前仓库为空，仅包含 `.git`，没有可复用的 FastAPI 代码。建议按以下方案从零实现。

## 一、技术方案

- Web 框架：FastAPI
- ORM：SQLAlchemy 2.x Async
- 数据库：
  - 生产：PostgreSQL
  - 本地测试：SQLite
- 数据库迁移：Alembic
- 数据校验：Pydantic v2
- 服务器：Uvicorn/Gunicorn
- 测试：pytest、pytest-asyncio、httpx
- 容器化：Docker Compose
- 不使用 Redis，状态全部持久化到数据库

核心数据表 `short_urls`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `alias` | VARCHAR(64) | 短链接别名，唯一 |
| `target_url` | TEXT | 原始 URL |
| `expires_at` | TIMESTAMP WITH TIME ZONE | 过期时间，可为空 |
| `created_at` | TIMESTAMP WITH TIME ZONE | 创建时间 |
| `updated_at` | TIMESTAMP WITH TIME ZONE | 更新时间 |
| `is_active` | BOOLEAN | 是否主动禁用 |
| `access_count` | BIGINT | 可选访问计数 |
| `last_accessed_at` | TIMESTAMP WITH TIME ZONE | 最近访问时间 |

索引：

```text
UNIQUE(alias)
INDEX(expires_at)
INDEX(is_active, expires_at)
```

过期判断统一使用 UTC：

```text
有效条件：
is_active = true
AND (expires_at IS NULL OR expires_at > current_utc_time)
```

## 二、API 设计

### 1. 创建短链接

```http
POST /api/v1/short-urls
Content-Type: application/json
```

请求：

```json
{
  "target_url": "https://example.com/article/123",
  "alias": "article-123",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

规则：

- `target_url` 仅允许 `http` 和 `https`
- 限制 URL 最大长度，例如 4096
- `alias` 可选
- 未传 `alias` 时生成随机 Base62 别名
- 别名只允许 `[A-Za-z0-9_-]`
- 别名长度建议 4 到 64
- 过期时间必须晚于当前时间
- 创建时通过数据库唯一约束保证并发安全

响应：

```json
{
  "alias": "article-123",
  "short_url": "https://short.example.com/article-123",
  "target_url": "https://example.com/article/123",
  "expires_at": "2026-12-31T23:59:59Z",
  "created_at": "2026-08-16T10:00:00Z"
}
```

状态码：

- `201 Created`
- `400 Bad Request`：参数非法
- `409 Conflict`：别名已存在

### 2. 重定向

```http
GET /{alias}
```

行为：

1. 查询别名
2. 检查 `is_active`
3. 检查 `expires_at`
4. 更新访问统计
5. 返回重定向

建议使用：

```text
307 Temporary Redirect
```

这样不会改变客户端原始请求方法，行为比 `301` 更安全可控。

状态码：

- `307`：重定向成功
- `404`：别名不存在
- `410 Gone`：别名存在但已过期或被禁用

访问统计更新不应阻塞跳转，可以采用以下两种方式之一：

- 第一版：在同一事务中原子递增，简单可靠
- 高并发版本：使用数据库异步写入或独立统计表，避免修改主记录

### 3. 查询短链接

```http
GET /api/v1/short-urls/{alias}
```

返回短链接详情。是否允许公开查询应通过配置决定，生产环境建议需要认证。

### 4. 禁用短链接

```http
DELETE /api/v1/short-urls/{alias}
```

建议采用软删除：

```text
is_active = false
```

避免直接删除导致审计和历史数据丢失。

### 5. 健康检查

```http
GET /health/live
GET /health/ready
```

- `live`：进程是否存活
- `ready`：数据库连接是否可用

## 三、推荐目录结构

```text
fastapi-no-redis/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py
│   │       ├── short_urls.py
│   │       └── health.py
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       ├── __init__.py
│   │       └── short_url.py
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── short_url.py
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── short_url_service.py
│   │   ├── alias_generator.py
│   │   └── expiration_service.py
│   │
│   ├── repositories/
│   │   ├── __init__.py
│   │   └── short_url_repository.py
│   │
│   ├── exceptions/
│   │   ├── __init__.py
│   │   └── short_url.py
│   │
│   └── observability/
│       ├── __init__.py
│       ├── logging.py
│       └── metrics.py
│
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 0001_create_short_urls.py
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_alias_generator.py
│   │   ├── test_schemas.py
│   │   └── test_expiration.py
│   └── integration/
│       ├── test_create_short_url.py
│       ├── test_redirect.py
│       ├── test_expiration.py
│       └── test_health.py
│
├── scripts/
│   └── cleanup_expired.py
│
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── alembic.ini
├── .env.example
├── .gitignore
└── README.md
```

## 四、模块职责

### `api`

只负责：

- HTTP 路由
- 参数接收
- 依赖注入
- 响应状态码
- 异常转换

不在路由函数中直接编写数据库查询。

### `services`

负责业务规则：

- 创建短链接
- 判断过期
- 生成别名
- 处理别名冲突
- 组织事务

### `repositories`

负责数据库访问：

- 查询别名
- 插入记录
- 软删除
- 更新访问统计
- 批量查询过期记录

### `schemas`

负责请求和响应模型，避免 ORM 对象直接暴露给客户端。

## 五、关键实现细节

### 别名生成

使用密码学安全随机源：

```python
secrets.choice(BASE62_ALPHABET)
```

默认长度建议 7 或 8。

生成后插入数据库。如果发生唯一键冲突：

1. 重新生成
2. 最多重试 3 次
3. 超过次数返回 `503 Service Unavailable`

自定义别名不应无限重试，直接返回 `409`。

### 事务边界

创建操作：

```text
开始事务
  校验 URL 和过期时间
  生成或验证 alias
  插入记录
提交事务
```

重定向操作：

```text
查询有效记录
原子更新 access_count
提交
返回 307
```

必须依赖数据库唯一约束，而不是先查询再插入，否则并发请求会产生竞态。

### 过期清理

重定向路径必须实时检查过期时间，不能只依赖后台清理任务。

后台清理脚本：

```sql
UPDATE short_urls
SET is_active = false,
    updated_at = CURRENT_TIMESTAMP
WHERE is_active = true
  AND expires_at IS NOT NULL
  AND expires_at <= CURRENT_TIMESTAMP;
```

该脚本由：

- Kubernetes CronJob
- Linux cron
- CI/CD 定时任务

之一调用。应用本身不强制依赖后台调度器，避免多副本重复执行。

### 错误格式

统一响应：

```json
{
  "detail": {
    "code": "ALIAS_ALREADY_EXISTS",
    "message": "The alias is already in use"
  }
}
```

建议错误码：

```text
INVALID_URL
INVALID_ALIAS
INVALID_EXPIRATION
ALIAS_ALREADY_EXISTS
SHORT_URL_NOT_FOUND
SHORT_URL_EXPIRED
SHORT_URL_DISABLED
```

## 六、测试计划

### 单元测试

覆盖：

- Base62 别名长度和字符集
- URL scheme 校验
- URL 长度限制
- 过期时间校验
- 永不过期链接
- 当前时间等于 `expires_at` 时视为过期
- 别名格式校验

### 集成测试

覆盖：

- 创建随机别名
- 创建自定义别名
- 重复别名返回 `409`
- 不存在别名返回 `404`
- 过期别名返回 `410`
- 禁用别名返回 `410`
- 有效别名返回 `307`
- `Location` header 正确
- 访问计数递增
- 健康检查和数据库不可用状态

### 并发测试

至少验证：

- 多请求同时使用同一自定义别名时只有一个成功
- 自动生成别名不会覆盖已有记录
- 访问计数不会因并发更新丢失

### 测试数据库

测试中使用独立 SQLite 数据库或临时 PostgreSQL 容器，禁止连接生产数据库。

## 七、配置与部署

`.env.example`：

```env
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://shortener:password@db:5432/shortener
PUBLIC_BASE_URL=https://short.example.com
DEFAULT_ALIAS_LENGTH=8
MAX_URL_LENGTH=4096
LOG_LEVEL=INFO
```

Docker Compose 服务：

```text
api
db
```

生产部署建议：

- PostgreSQL 使用独立持久化卷
- API 使用多个副本
- 反向代理负责 TLS、压缩和基础限流
- 日志输出到 stdout
- 使用结构化 JSON 日志
- 不把数据库密码写入镜像
- Alembic 迁移作为部署步骤执行
- 设置数据库连接池上限和超时
- 对外部 URL 不主动发起请求，避免 SSRF 风险

## 八、实施顺序

1. 创建 `pyproject.toml`、配置模块和基础 FastAPI 应用。
2. 添加 SQLAlchemy 异步数据库连接和 `short_urls` 模型。
3. 配置 Alembic 并创建首个迁移。
4. 实现 Pydantic 请求、响应模型。
5. 实现别名生成器和 URL 校验。
6. 实现 repository 和 service 层。
7. 实现创建、查询、删除、重定向接口。
8. 添加统一异常处理和健康检查。
9. 添加过期清理脚本。
10. 编写单元测试、集成测试和并发测试。
11. 添加 Dockerfile、Compose 和部署文档。
12. 运行格式化、静态检查、迁移验证和完整测试。

## 九、验收标准

- 数据重启后短链接和别名仍然存在。
- 自定义别名全局唯一。
- 并发创建不会覆盖已有别名。
- 过期链接不能重定向，并返回 `410`。
- 有效链接返回 `307` 且 `Location` 正确。
- 不依赖 Redis 或其他非持久化状态服务。
- 所有数据库变更可通过 Alembic 重放。
- 核心 API、过期逻辑和并发场景均有自动化测试。
- Docker Compose 可启动完整本地环境。
- 生产配置不包含硬编码密钥。

当前环境是只读仓库，因此无法按要求将计划写入 `tmp-docs`；上述内容可直接作为实施计划和验收基线。