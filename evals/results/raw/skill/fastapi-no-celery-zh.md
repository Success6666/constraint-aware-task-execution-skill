# FastAPI 报表生成服务设计

## 1. 总体方案

采用 **FastAPI + PostgreSQL + Redis + 独立 Worker + 对象存储**：

- **FastAPI**：鉴权、创建任务、查询状态、重试、下载结果。
- **PostgreSQL**：持久化任务、进度、错误和结果元数据。
- **Redis Streams**：任务队列；不使用 Celery。
- **Worker**：独立进程或容器，从 Redis Streams 消费任务并生成报表。
- **对象存储**：保存 CSV/XLSX/PDF 等结果文件；开发环境可使用 MinIO，生产环境使用 S3 兼容存储。
- **SSE 或轮询**：客户端查询进度；首版使用轮询即可，实时体验可增加 SSE。

推荐部署：

```text
Client
  │
  ▼
FastAPI ───── PostgreSQL
  │             │
  ├────────── Redis Streams
  │             │
  └────────── Object Storage
                ▲
              Worker
```

## 2. 任务生命周期

任务状态：

```text
PENDING → RUNNING → SUCCEEDED
                  └→ FAILED
                  └→ RETRY_WAITING → RUNNING
CANCELLED（可选）
```

创建任务时：

1. 校验报表类型、参数、输出格式。
2. 在数据库插入任务，状态为 `PENDING`。
3. 将 `task_id` 写入 Redis Stream。
4. 返回 `202 Accepted` 和任务信息。

Worker 执行时：

1. 使用 Redis consumer group 消费消息。
2. 通过数据库原子更新将任务从 `PENDING` 改为 `RUNNING`。
3. 按阶段更新进度和当前步骤。
4. 成功后上传结果文件，写入文件元数据，状态改为 `SUCCEEDED`。
5. 失败后保存错误信息和堆栈摘要；根据策略重试或标记为 `FAILED`。
6. 使用 ACK 确认消息；未 ACK 的消息由恢复任务重新领取。

## 3. 数据模型

### `report_tasks`

```text
id                 UUID PRIMARY KEY
report_type        VARCHAR(64) NOT NULL
parameters         JSONB NOT NULL
format             VARCHAR(16) NOT NULL
status             VARCHAR(24) NOT NULL
progress           SMALLINT NOT NULL DEFAULT 0
current_step       VARCHAR(128)
attempt             INTEGER NOT NULL DEFAULT 0
max_attempts       INTEGER NOT NULL DEFAULT 3
error_code         VARCHAR(64)
error_message      TEXT
result_object_key  VARCHAR(512)
result_filename    VARCHAR(255)
result_size        BIGINT
created_at         TIMESTAMP WITH TIME ZONE
started_at         TIMESTAMP WITH TIME ZONE
finished_at        TIMESTAMP WITH TIME ZONE
updated_at         TIMESTAMP WITH TIME ZONE
```

### `report_task_events`（推荐）

记录状态变化、进度变化和错误，便于审计与排查：

```text
task_id       UUID
from_status   VARCHAR(24)
to_status     VARCHAR(24)
progress      SMALLINT
message       TEXT
created_at    TIMESTAMP WITH TIME ZONE
```

关键约束：

- `progress` 范围为 `0..100`。
- 成功任务必须存在结果对象键。
- 任务状态更新使用事务和条件更新，避免重复 Worker 覆盖状态。
- 参数中禁止保存密码、Token 等敏感信息。

## 4. API 设计

### 创建任务

```http
POST /api/v1/reports
Content-Type: application/json
```

请求：

```json
{
  "report_type": "sales_summary",
  "parameters": {
    "start_date": "2025-01-01",
    "end_date": "2025-01-31",
    "department_id": 10
  },
  "format": "xlsx"
}
```

响应 `202`：

```json
{
  "task_id": "2a0c...",
  "status": "PENDING",
  "progress": 0,
  "status_url": "/api/v1/reports/2a0c...",
  "download_url": null
}
```

### 查询任务

```http
GET /api/v1/reports/{task_id}
```

响应：

```json
{
  "task_id": "2a0c...",
  "status": "RUNNING",
  "progress": 65,
  "current_step": "写入 Excel",
  "attempt": 1,
  "max_attempts": 3,
  "error": null,
  "download_url": null,
  "created_at": "2025-01-01T10:00:00Z",
  "updated_at": "2025-01-01T10:01:20Z"
}
```

### 手动重试

```http
POST /api/v1/reports/{task_id}/retry
```

规则：

- 仅允许 `FAILED` 状态重试。
- 未超过最大尝试次数时重新入队。
- 如需强制重试，可由管理员使用独立权限，并重置或提高上限。
- 使用幂等检查，避免重复创建队列消息。

响应 `202`：

```json
{
  "task_id": "2a0c...",
  "status": "RETRY_WAITING",
  "attempt": 2
}
```

### 下载结果

```http
GET /api/v1/reports/{task_id}/download
```

- 非成功状态返回 `409`。
- 任务不存在返回 `404`。
- 校验当前用户是否拥有任务权限。
- 由 API 返回短时效预签名 URL，或通过流式响应代理文件。
- 推荐预签名 URL，降低 API 带宽压力。

### 健康检查

```http
GET /health/live
GET /health/ready
```

`ready` 应检查 PostgreSQL、Redis 和对象存储连接。

## 5. Worker 与队列实现

Redis Stream 示例：

```text
XGROUP CREATE report_tasks report-workers $ MKSTREAM
XADD report_tasks * task_id=<uuid>
XREADGROUP GROUP report-workers worker-1 COUNT 1 BLOCK 5000 STREAMS report_tasks >
XACK report_tasks report-workers <message-id>
```

Worker 要点：

- 使用独立进程，不在 FastAPI Web 进程内运行长任务。
- 每个任务设置最大执行时间和资源限制。
- 消费到消息后先抢占任务锁，再执行。
- 使用 Redis 分布式锁或数据库条件更新保证同一任务只有一个执行者。
- Worker 重启后扫描 `RUNNING` 且超时未更新的任务，将其重新置为 `RETRY_WAITING` 并重新入队。
- 使用 Redis pending entries 检测长时间未 ACK 的消息。
- 报表生成逻辑按 `report_type` 注册：

```python
REPORT_HANDLERS = {
    "sales_summary": SalesSummaryReport,
    "inventory": InventoryReport,
}
```

每个 Handler 提供：

```python
async def generate(parameters, output_format, progress_callback) -> Result:
    ...
```

`progress_callback(progress, step)` 负责持久化进度；不要在每一行数据处理时写数据库，可按百分比变化或固定时间间隔节流。

## 6. 重试策略

自动重试仅针对临时性错误：

- 数据库连接短暂失败。
- 对象存储暂时不可用。
- 外部服务超时。
- Worker 临时崩溃。

不应重试：

- 参数校验失败。
- 无权限。
- 报表类型不存在。
- 数据业务规则错误。
- 文件格式不支持。

建议策略：

```text
最大尝试次数：3
退避：30s、2min、10min
抖动：±20%
```

重试前更新 `attempt`，并记录 `error_code`、`error_message`。结果上传成功但状态更新失败时，使用确定性的对象键，例如：

```text
reports/{task_id}/result.xlsx
```

这样重复执行不会产生大量孤立文件。

## 7. 可靠性与幂等性

- 创建接口支持 `Idempotency-Key`，避免客户端超时后重复创建任务。
- 使用唯一索引 `(owner_id, idempotency_key)`。
- 状态变更采用明确的状态机，不允许任意状态跳转。
- 数据库提交和入队不是天然原子操作；采用 **事务 Outbox**：
  1. 创建任务与 Outbox 记录在同一事务提交。
  2. Dispatcher 扫描未投递 Outbox，写入 Redis。
  3. 成功后标记已投递。
- Worker 的结果写入、任务状态更新要具备幂等性。
- 下载 URL 使用短过期时间，并按权限重新生成。

## 8. FastAPI 分层

```text
API 路由层
  └── Pydantic 请求/响应模型
      └── Service 业务层
          ├── TaskRepository
          ├── QueuePublisher
          ├── StorageClient
          └── ReportRegistry
```

建议：

- 使用 SQLAlchemy 2.x AsyncSession 或 SQLModel。
- 使用 Alembic 管理数据库迁移。
- 使用 Pydantic Settings 管理配置。
- API 与 Worker 共用领域模型、任务状态机和报表 Handler，但分别启动。
- 认证可使用 JWT；所有任务查询和下载都必须校验租户或用户归属。

## 9. 测试方案

### 单元测试

覆盖：

- 参数校验和日期边界。
- 状态机合法与非法转换。
- 重试判定、退避和最大次数。
- 进度计算与节流。
- 报表 Handler 的核心计算。
- 文件名、格式和对象键生成。

### API 测试

使用 `httpx.AsyncClient`：

- 创建任务返回 `202`。
- 查询不存在任务返回 `404`。
- 查询不同用户任务返回 `403` 或统一的资源不存在响应。
- 成功任务可获取下载地址。
- 未完成任务下载返回 `409`。
- 重试只允许符合条件的任务。
- 重复 `Idempotency-Key` 返回同一任务。

### Worker 集成测试

使用 Testcontainers 或 Docker Compose 启动 PostgreSQL、Redis 和 MinIO，验证：

1. 创建任务后最终生成文件。
2. Worker 异常后任务按策略重试。
3. 永久性错误不会反复重试。
4. Worker 崩溃或消息未 ACK 后任务可恢复。
5. 重复消费不会生成错误的重复结果。
6. 进度从 `0` 最终到 `100`，失败时保留错误信息。

### 端到端测试

测试完整流程：

```text
POST 创建 → 轮询状态 → SUCCEEDED → 下载并校验文件内容
```

同时测试超时、空数据、大数据量、非法参数和权限隔离。

## 10. 生产运行指标

至少监控：

- 创建任务数、成功率、失败率、重试率。
- 各报表类型平均和 P95/P99 生成时长。
- 队列积压量和最老消息年龄。
- `RUNNING` 超时任务数。
- Worker CPU、内存和并发数。
- 结果文件上传失败数。
- API 延迟和 `4xx/5xx` 数量。

日志必须包含 `task_id`、`attempt`、`worker_id` 和 `report_type`，但不得记录敏感参数或完整报表数据。

## 11. 推荐实现顺序

1. 定义任务状态机、Pydantic 模型和数据库表。
2. 实现创建、查询、下载三个 API。
3. 实现 Redis Streams Producer、Worker 和 ACK/恢复机制。
4. 接入对象存储和一个最小报表 Handler。
5. 加入自动重试、手动重试和 Outbox。
6. 补充 API、Worker、端到端测试。
7. 增加认证、限流、监控和部署配置。

该方案在不依赖 Celery 的前提下，将 Web 请求、任务调度、报表生成和文件分发解耦，能够支持水平扩展、任务恢复、可追踪进度和可靠下载。
