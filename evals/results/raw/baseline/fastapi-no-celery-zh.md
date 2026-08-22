# FastAPI 报表生成服务设计

## 1. 总体架构

采用以下组件：

- **FastAPI**：提交任务、查询进度、重试、下载结果
- **PostgreSQL**：任务状态、参数、重试次数、结果元数据
- **Redis Streams**：任务队列，支持消费组和消息确认
- **独立 Worker 进程**：消费任务并生成报表
- **S3/MinIO**：保存最终报表文件
- **Prometheus + 日志系统**：监控任务耗时、失败率、队列积压

```text
Client
  |
  v
FastAPI
  |------ PostgreSQL：任务状态
  |------ Redis Streams：发布任务
  |------ S3/MinIO：生成下载地址
                       ^
                       |
                    Report Worker
                       |
              PostgreSQL / Redis / S3
```

不建议只使用 `FastAPI BackgroundTasks`，因为它依赖 API 进程生命周期，进程重启或部署时任务可能丢失。Redis Streams + 独立 Worker 可以支持确认、重试和故障恢复。

---

# 2. 任务生命周期

任务状态定义：

```text
PENDING
  -> RUNNING
  -> SUCCEEDED

PENDING
  -> RUNNING
  -> RETRY_WAITING
  -> PENDING

PENDING
  -> RUNNING
  -> FAILED

RUNNING
  -> CANCELED      # 可选
```

状态约束：

- `PENDING`：任务已创建，等待 Worker 消费
- `RUNNING`：Worker 已领取并开始处理
- `RETRY_WAITING`：本次执行失败，等待重新入队
- `SUCCEEDED`：报表生成完成
- `FAILED`：达到最大重试次数或发生不可重试错误
- `CANCELED`：用户主动取消，建议作为扩展能力实现

每个任务保留：

- 当前状态
- 当前阶段
- 进度百分比
- 当前尝试次数
- 最大重试次数
- 最近错误
- 开始时间、结束时间
- 结果文件信息
- 请求幂等键

---

# 3. API 设计

## 3.1 创建报表任务

```http
POST /api/v1/reports
Idempotency-Key: 3a7e6e2d-...
Content-Type: application/json
```

请求：

```json
{
  "report_type": "sales",
  "parameters": {
    "start_date": "2025-01-01",
    "end_date": "2025-01-31",
    "department_ids": [10, 20],
    "format": "xlsx"
  },
  "max_retries": 3
}
```

响应：

```http
202 Accepted
Location: /api/v1/reports/01JABC...
```

```json
{
  "task_id": "01JABC...",
  "status": "PENDING",
  "status_url": "/api/v1/reports/01JABC...",
  "created_at": "2025-02-01T10:00:00Z"
}
```

要求：

- `report_type` 使用白名单校验
- 日期范围必须合法
- `max_retries` 限制在 `0~5`
- `format` 仅允许 `xlsx`、`csv`、`pdf`
- 报表参数保存原始 JSON，便于重试和审计
- 创建任务和发布队列消息必须保证最终一致性，见后文 Outbox 设计

---

## 3.2 查询任务进度

```http
GET /api/v1/reports/{task_id}
```

响应：

```json
{
  "task_id": "01JABC...",
  "report_type": "sales",
  "status": "RUNNING",
  "stage": "EXPORTING",
  "progress": 78,
  "attempt": 1,
  "max_retries": 3,
  "error": null,
  "created_at": "2025-02-01T10:00:00Z",
  "started_at": "2025-02-01T10:00:03Z",
  "finished_at": null,
  "result": null
}
```

成功时：

```json
{
  "task_id": "01JABC...",
  "status": "SUCCEEDED",
  "stage": "COMPLETED",
  "progress": 100,
  "attempt": 1,
  "max_retries": 3,
  "error": null,
  "result": {
    "filename": "sales-2025-01.xlsx",
    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size": 283920,
    "expires_at": "2025-02-08T10:00:00Z",
    "download_url": "/api/v1/reports/01JABC.../download"
  }
}
```

失败时：

```json
{
  "task_id": "01JABC...",
  "status": "FAILED",
  "stage": "FAILED",
  "progress": 35,
  "attempt": 4,
  "max_retries": 3,
  "error": {
    "code": "DATA_SOURCE_TIMEOUT",
    "message": "数据源请求超时",
    "retryable": false
  }
}
```

错误信息对外只返回安全摘要，详细堆栈写入日志，不直接暴露给客户端。

---

## 3.3 手动重试任务

```http
POST /api/v1/reports/{task_id}/retry
```

允许条件：

- 当前状态为 `FAILED`
- 任务没有超过业务允许的重试期限
- 任务参数仍然有效

响应：

```json
{
  "task_id": "01JABC...",
  "status": "PENDING",
  "attempt": 0,
  "message": "任务已重新提交"
}
```

建议手动重试创建新的执行批次，但保留原任务 ID，增加：

```text
execution_no: 1, 2, 3...
```

如果业务需要完整审计，可以单独建立 `report_task_attempts` 表。

---

## 3.4 下载结果

```http
GET /api/v1/reports/{task_id}/download
```

处理逻辑：

1. 校验用户是否有任务访问权限
2. 校验任务状态为 `SUCCEEDED`
3. 校验结果文件仍存在
4. 返回短期有效的 S3/MinIO 预签名 URL，或由 API 流式转发

响应建议：

```http
302 Found
Location: https://storage.example.com/presigned-url
```

不建议让 API 进程长时间转发大文件。预签名 URL 有效期建议为 5 分钟。

当任务未完成时：

```http
409 Conflict
```

```json
{
  "code": "REPORT_NOT_READY",
  "message": "报表尚未生成完成"
}
```

当文件已过期或被清理时：

```http
410 Gone
```

---

## 3.5 可选：取消任务

```http
POST /api/v1/reports/{task_id}/cancel
```

取消策略：

- `PENDING`：直接改为 `CANCELED`
- `RUNNING`：写入取消标记，Worker 在阶段边界检查
- 已完成任务不可取消

不要强制杀死正在执行的线程或进程，应通过 Redis/数据库取消标记实现协作式取消。

---

# 4. 数据模型

## 4.1 `report_tasks`

```sql
CREATE TABLE report_tasks (
    id                  VARCHAR(26) PRIMARY KEY,
    owner_id            VARCHAR(128) NOT NULL,
    report_type         VARCHAR(64) NOT NULL,
    parameters          JSONB NOT NULL,

    status              VARCHAR(32) NOT NULL,
    stage               VARCHAR(64) NOT NULL DEFAULT 'QUEUED',
    progress            SMALLINT NOT NULL DEFAULT 0,

    attempt             INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 3,
    next_retry_at       TIMESTAMPTZ,

    error_code          VARCHAR(128),
    error_message       TEXT,

    result_key          TEXT,
    result_filename     VARCHAR(512),
    result_content_type VARCHAR(128),
    result_size         BIGINT,
    result_expires_at   TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_report_tasks_owner_created
    ON report_tasks(owner_id, created_at DESC);

CREATE INDEX idx_report_tasks_status_retry
    ON report_tasks(status, next_retry_at);
```

约束：

```sql
ALTER TABLE report_tasks
ADD CONSTRAINT report_tasks_progress_range
CHECK (progress >= 0 AND progress <= 100);
```

## 4.2 幂等键表

```sql
CREATE TABLE idempotency_keys (
    owner_id       VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(256) NOT NULL,
    request_hash   VARCHAR(64) NOT NULL,
    task_id        VARCHAR(26) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, idempotency_key)
);
```

同一个用户使用相同 `Idempotency-Key`：

- 请求体哈希相同：返回原任务
- 请求体哈希不同：返回 `409 Conflict`

---

# 5. 队列设计

使用 Redis Streams：

```text
Stream: report:tasks
Consumer group: report-workers
```

消息格式：

```json
{
  "task_id": "01JABC...",
  "execution_id": "01JEXEC..."
}
```

发布任务：

```text
XADD report:tasks * task_id 01JABC... execution_id 01JEXEC...
```

Worker 消费：

```text
XREADGROUP GROUP report-workers worker-1 COUNT 1 BLOCK 5000 STREAMS report:tasks >
```

处理完成后确认：

```text
XACK report:tasks report-workers message-id
```

## 消费流程

```text
读取消息
  |
  v
数据库原子抢占任务
  |
  +-- 已成功/已取消/已处理 -> XACK
  |
  +-- 抢占成功 -> 执行报表
                       |
             +---------+---------+
             |                   |
          成功                 失败
             |                   |
     保存结果并 SUCCEEDED     判断是否可重试
                                 |
                    +------------+------------+
                    |                         |
                 可重试                    不可重试
                    |                         |
              RETRY_WAITING                 FAILED
                    |
              延迟后重新入队
```

## 防止重复执行

Redis Stream 的消息可能因 Worker 崩溃而重复投递，因此不能依赖“只消费一次”。

Worker 开始时必须执行类似逻辑：

```sql
UPDATE report_tasks
SET status = 'RUNNING',
    attempt = attempt + 1,
    started_at = COALESCE(started_at, now()),
    updated_at = now()
WHERE id = :task_id
  AND status IN ('PENDING', 'RETRY_WAITING')
RETURNING *;
```

如果没有返回记录，说明任务已经被其他 Worker 处理或已经结束，应直接确认消息。

此外，结果对象使用确定性 Key：

```text
reports/{owner_id}/{task_id}/{execution_id}.{format}
```

即使 Worker 重复生成，也不会覆盖其他任务。

---

# 6. Outbox 解决事务一致性

不能先写数据库再直接发 Redis，也不能先发 Redis 再写数据库，否则可能出现：

- 数据库有任务，但 Redis 发布失败
- Redis 有消息，但数据库事务回滚

建议使用 Outbox：

```sql
CREATE TABLE task_outbox (
    id          BIGSERIAL PRIMARY KEY,
    task_id     VARCHAR(26) NOT NULL,
    event_type  VARCHAR(64) NOT NULL,
    payload     JSONB NOT NULL,
    published_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_outbox_unpublished
    ON task_outbox(published_at)
    WHERE published_at IS NULL;
```

创建任务时，在同一个数据库事务中：

1. 插入 `report_tasks`
2. 插入 `idempotency_keys`
3. 插入 `task_outbox`

独立 Publisher 周期性执行：

1. 读取未发布 Outbox 记录
2. 发布到 Redis Stream
3. 更新 `published_at`
4. 发布失败则下次继续

Outbox Publisher 自身重复发布是允许的，Worker 通过任务状态和执行 ID 做幂等处理。

---

# 7. Worker 实现原则

## 7.1 Worker 进程模型

建议独立部署：

```text
API containers: 2+
Worker containers: 2+
Outbox publisher: 1+
Retry scheduler: 1+
```

Worker 可以使用：

- `redis.asyncio`
- `asyncpg` 或 SQLAlchemy Async
- `httpx`
- `boto3`/`aioboto3` 或 MinIO SDK

报表生成涉及 pandas、Excel、PDF 等 CPU 密集操作时，不要直接阻塞事件循环：

```python
result = await asyncio.to_thread(generate_report, params)
```

如果计算量较大，使用进程池：

```python
result = await process_pool.run(generate_report, params)
```

数据库、HTTP、对象存储操作使用异步客户端。

## 7.2 阶段和进度

统一阶段定义：

```text
VALIDATING       5
FETCHING_DATA    25
TRANSFORMING     55
EXPORTING        85
UPLOADING        95
COMPLETED        100
```

进度更新原则：

- 只有 Worker 能推进状态
- 进度只能递增，除非任务被重试并明确重置
- 更新失败不应导致报表任务失败
- 数据处理循环中按批次更新，避免每条记录写一次数据库

建议使用 Redis 做高频进度缓存，数据库做持久化：

```text
report:progress:{task_id}
```

Worker 每 1~2 秒更新一次 Redis，每 5~10 秒或阶段切换时更新 PostgreSQL。

查询接口优先读取 Redis，Redis 缺失时读取 PostgreSQL。

---

# 8. 重试策略

## 8.1 可重试错误

典型可重试错误：

- 数据库连接暂时失败
- HTTP 429
- HTTP 502、503、504
- 对象存储临时不可用
- 网络超时
- Worker 内部临时资源不足

不可重试错误：

- 参数校验失败
- 报表类型不存在
- 数据不存在且业务定义为正常空结果
- SQL 或模板错误
- 权限错误
- 输出格式不支持
- 数据格式无法解析

异常应归类为结构化错误：

```python
class ReportError(Exception):
    code: str
    message: str
    retryable: bool
```

## 8.2 指数退避

建议：

```text
delay = min(60 * 2^(attempt - 1), 3600) + random(0, 30)
```

例如：

```text
第 1 次失败：60~90 秒
第 2 次失败：120~150 秒
第 3 次失败：240~270 秒
```

不要让 Worker 阻塞等待。将任务标记为 `RETRY_WAITING`，由 Retry Scheduler 在 `next_retry_at <= now()` 时重新写入 Redis Stream。

## 8.3 超时与异常恢复

Worker 必须设置：

- 单任务最大执行时间，例如 30 分钟
- 外部 HTTP 连接和读取超时
- 数据库查询超时
- 对象存储上传超时

Worker 启动时扫描超时任务：

```sql
UPDATE report_tasks
SET status = 'RETRY_WAITING',
    error_code = 'WORKER_TIMEOUT',
    error_message = '任务执行超时',
    next_retry_at = now()
WHERE status = 'RUNNING'
  AND updated_at < now() - INTERVAL '30 minutes';
```

实际实现中应结合 Worker heartbeat，避免误判正在执行的任务。

---

# 9. 任务状态并发控制

所有状态转换都必须通过条件更新：

```sql
UPDATE report_tasks
SET status = 'SUCCEEDED',
    stage = 'COMPLETED',
    progress = 100,
    result_key = :result_key,
    result_filename = :filename,
    result_size = :size,
    finished_at = now(),
    updated_at = now()
WHERE id = :task_id
  AND status = 'RUNNING';
```

如果更新行数为 0，则不能继续覆盖任务状态。

建议加入乐观锁：

```sql
ALTER TABLE report_tasks
ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
```

更新时：

```sql
UPDATE report_tasks
SET progress = :progress,
    stage = :stage,
    version = version + 1
WHERE id = :task_id
  AND version = :old_version;
```

对于简单实现，状态条件更新已经足够；高并发场景再引入 `version`。

---

# 10. 结果文件处理

## 文件命名

```text
{report_type}-{start_date}-{end_date}.{format}
```

对象存储 Key：

```text
reports/{owner_id}/{task_id}/{execution_id}.{format}
```

避免使用用户输入直接拼接路径。

## 上传流程

1. 在临时目录生成文件
2. 校验文件大小和 MIME 类型
3. 上传到 MinIO/S3
4. 校验上传结果
5. 写入数据库结果元数据
6. 删除本地临时文件

上传失败时按错误类型重试。

## 清理策略

定时任务清理：

- 已过期的对象存储文件
- 超过保存期限的任务记录
- Worker 生成但未完成提交的孤儿文件
- Redis 中过期的进度键

建议保留：

```text
任务元数据：30~90 天
结果文件：7~30 天
Redis 进度：24 小时
```

---

# 11. FastAPI 应用分层

建议分为以下逻辑层：

```text
API Layer
  - 请求校验
  - 认证授权
  - HTTP 状态码

Application Layer
  - 创建任务
  - 查询任务
  - 重试任务
  - 下载任务

Repository Layer
  - PostgreSQL 查询和事务

Queue Layer
  - Redis Stream 发布和消费

Report Layer
  - 报表类型注册
  - 数据获取
  - 数据转换
  - 文件导出

Storage Layer
  - S3/MinIO 上传
  - 预签名 URL
```

报表类型采用注册表，避免在接口中堆叠条件分支：

```python
REPORT_GENERATORS = {
    "sales": SalesReportGenerator(),
    "inventory": InventoryReportGenerator(),
    "finance": FinanceReportGenerator(),
}
```

统一接口：

```python
class ReportGenerator(Protocol):
    async def generate(
        self,
        params: dict,
        progress: ProgressReporter,
    ) -> GeneratedReport:
        ...
```

返回结构：

```python
@dataclass
class GeneratedReport:
    local_path: str
    filename: str
    content_type: str
    size: int
```

---

# 12. 认证、权限与安全

必须具备：

- JWT 或内部网关传递的用户身份
- 任务归属校验：只能查询和下载自己的任务
- 管理员可查看全部任务
- `report_type` 白名单
- 参数结构校验
- 请求体大小限制
- 单用户并发任务数量限制
- 单任务最大时间和文件大小限制
- 防止路径穿越
- 日志中脱敏用户参数和敏感数据
- 下载链接短期有效
- 对象存储 Bucket 默认私有

建议限流：

```text
创建任务：每用户每分钟 10 次
查询接口：每用户每秒 10 次
手动重试：每用户每小时 20 次
```

---

# 13. HTTP 状态码约定

```text
202 Accepted   创建任务成功
200 OK         查询成功、重试成功
302 Found      重定向到下载地址
400 Bad Request 参数格式错误
401 Unauthorized 未认证
403 Forbidden  无权限
404 Not Found  任务不存在
409 Conflict   幂等键冲突、任务状态不允许操作
410 Gone       结果文件已过期
429 Too Many Requests 超出限流
500 Internal Server Error 未知服务错误
```

统一错误格式：

```json
{
  "code": "INVALID_REPORT_PARAMETERS",
  "message": "报表参数不合法",
  "request_id": "req_01JABC"
}
```

---

# 14. 部署方式

使用容器部署：

```text
api
worker
outbox-publisher
retry-scheduler
postgres
redis
minio
```

API 和 Worker 分开扩缩容：

- API 按请求量扩容
- Worker 按 Redis 队列积压扩容
- CPU 密集型报表使用单独 Worker 队列
- 大文件或长任务使用专用队列

可以按任务类型拆分 Stream：

```text
report:tasks:normal
report:tasks:heavy
report:tasks:pdf
```

Worker 使用不同消费组，避免大任务阻塞普通任务。

健康检查：

```http
GET /health/live
GET /health/ready
```

`ready` 至少检查：

- PostgreSQL 可连接
- Redis 可连接
- 对象存储配置完整

不要在健康检查中执行复杂报表操作。

---

# 15. 可观测性

指标：

```text
report_tasks_created_total
report_tasks_succeeded_total
report_tasks_failed_total
report_tasks_retried_total
report_task_duration_seconds
report_task_queue_wait_seconds
report_task_generation_seconds
report_task_upload_seconds
report_queue_depth
report_worker_active
report_result_size_bytes
```

日志字段：

```json
{
  "request_id": "req_123",
  "task_id": "01JABC",
  "execution_id": "01JEXEC",
  "report_type": "sales",
  "worker_id": "worker-2",
  "attempt": 1,
  "status": "FAILED",
  "error_code": "DATA_SOURCE_TIMEOUT"
}
```

必须实现：

- 请求日志和任务日志关联
- 每次重试记录原因
- Worker 领取、开始、完成、失败均记录日志
- 长任务支持 heartbeat
- 队列积压告警
- 失败率和超时率告警

---

# 16. 测试方案

## 16.1 API 单元测试

覆盖：

- 创建任务参数校验
- 默认 `max_retries`
- 幂等键重复请求
- 相同幂等键但请求体不同
- 查询不存在任务
- 用户访问他人任务
- 成功任务下载
- 未完成任务下载
- 过期结果下载
- 失败任务手动重试
- 不允许重试的状态

典型断言：

```text
POST /reports -> 202
GET /reports/{id} -> 200
POST /reports/{id}/retry -> 200
GET /reports/{id}/download -> 302
```

## 16.2 Service 层测试

覆盖：

- 创建任务与 Outbox 同事务
- 状态转换合法性
- 状态转换并发冲突
- 进度只递增
- 达到最大重试次数后转为 FAILED
- 可重试和不可重试异常分类
- 结果元数据保存失败
- 对象上传成功但数据库提交失败

## 16.3 Worker 测试

覆盖：

- 正常生成报表
- Worker 重复收到同一消息
- Worker 执行过程中崩溃
- Redis 消息未 ACK
- 数据源超时后重试
- 参数错误不重试
- 上传失败后重试
- 超过执行时间
- 取消标记生效
- 生成文件后数据库更新失败的孤儿文件清理

## 16.4 集成测试

使用真实或容器化的：

- PostgreSQL
- Redis
- MinIO

验证完整链路：

```text
提交任务
  -> Outbox 发布
  -> Worker 消费
  -> 生成文件
  -> 上传 MinIO
  -> 更新 PostgreSQL
  -> 获取下载地址
  -> 下载并校验文件内容
```

校验：

- Excel 能被正常打开
- CSV 编码正确
- 文件大小和数据库记录一致
- 过期后无法下载

## 16.5 可靠性测试

至少验证以下场景：

1. API 在创建任务后立即重启，Outbox 仍能发布任务
2. Worker 领取任务后强制退出，任务最终会恢复
3. Redis 短暂不可用，任务不会丢失
4. PostgreSQL 短暂不可用，Worker 不会错误确认消息
5. 同一任务被两个 Worker 同时消费，只有一个 Worker 获得执行权
6. 重试任务不会产生错误的重复结果
7. 多次重复提交相同幂等键只创建一个任务

## 16.6 性能测试

模拟：

- 每秒 10~50 个任务提交
- 同时 1000 个进度查询
- 100 个并发 Worker
- 10MB、100MB、500MB 报表文件
- 长任务和短任务混合

关注：

- API P95/P99 响应时间
- 队列等待时间
- Worker 吞吐量
- PostgreSQL 连接池占用
- Redis Stream 积压
- 对象存储上传耗时

---

# 17. 推荐验收标准

功能验收：

- 创建任务接口返回 `202`
- 能查询阶段和进度
- 成功后可下载文件
- 临时故障自动重试
- 永久错误不会无限重试
- 支持手动重试
- 重复请求不会创建重复任务
- API 重启不导致任务丢失
- Worker 重启后任务可恢复
- 用户无法访问其他用户的任务

可靠性验收：

- 消息至少一次投递下结果仍然幂等
- Outbox 发布失败可恢复
- 结果文件与数据库元数据一致
- 超时任务能够被检测和处理
- 任务状态不会出现非法回退

运维验收：

- 有队列积压、失败率、耗时监控
- 有结构化日志和 `request_id`
- 有健康检查
- 有结果文件清理任务
- API、Worker 可以独立扩容

---

# 18. 最终推荐技术组合

```text
Python 3.12+
FastAPI
Pydantic v2
SQLAlchemy Async + asyncpg
PostgreSQL
redis.asyncio
Redis Streams
boto3/MinIO
pytest
pytest-asyncio
httpx
testcontainers
Prometheus
```

核心原则是：

1. **数据库保存权威状态**
2. **Redis Streams 负责可靠投递**
3. **Outbox 保证任务创建和入队最终一致**
4. **Worker 使用条件更新实现幂等**
5. **重试由任务状态和调度器驱动**
6. **结果文件放对象存储，不占用 API 本地磁盘**
7. **CPU 密集报表放线程池或进程池**
8. **通过集成测试验证完整任务链路和故障恢复**
