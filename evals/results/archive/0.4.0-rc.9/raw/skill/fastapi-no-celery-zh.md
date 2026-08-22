建议采用“FastAPI API 服务 + PostgreSQL 持久化任务队列 + 独立 Worker + S3/MinIO 文件存储”的结构。任务领取使用 PostgreSQL `FOR UPDATE SKIP LOCKED`，不额外引入消息队列框架，部署和故障恢复都比较直接。

## 一、整体架构

```text
客户端
  │
  ├─ POST /reports              创建任务
  ├─ GET  /reports/{id}         查询状态与进度
  ├─ POST /reports/{id}/retry   手动重试
  └─ GET  /reports/{id}/download
               │
          FastAPI API
               │
          PostgreSQL
      任务状态、参数、进度
               │
        独立 Worker 进程
      领取任务 → 生成报表
               │
          S3 / MinIO
          存放生成结果
```

API 和 Worker 使用同一份业务代码，但运行在不同进程或容器中：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker
```

生产环境可以水平扩展多个 Worker。数据库行锁确保同一任务只被一个 Worker 领取。

## 二、任务状态模型

```text
PENDING ──→ RUNNING ──→ SUCCEEDED
   ↑            │
   │            ├──→ RETRY_WAIT ──→ PENDING
   │            └──→ FAILED
   │
FAILED ──手动重试──→ PENDING
```

建议状态：

```python
class ReportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

不要只在 Redis 或进程内存里保存状态。任务状态、重试次数和文件信息必须持久化，才能在服务重启后恢复。

## 三、数据库表

```sql
CREATE TYPE report_status AS ENUM (
    'pending',
    'running',
    'retry_wait',
    'succeeded',
    'failed',
    'cancelled'
);

CREATE TABLE report_jobs (
    id UUID PRIMARY KEY,
    report_type VARCHAR(64) NOT NULL,
    parameters JSONB NOT NULL,
    status report_status NOT NULL DEFAULT 'pending',

    progress SMALLINT NOT NULL DEFAULT 0
        CHECK (progress BETWEEN 0 AND 100),
    progress_message VARCHAR(500),

    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    worker_id VARCHAR(128),
    heartbeat_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,

    result_key VARCHAR(1024),
    result_filename VARCHAR(255),
    result_content_type VARCHAR(128),
    result_size BIGINT,

    error_code VARCHAR(64),
    error_message TEXT,

    idempotency_key VARCHAR(128),
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (created_by, idempotency_key)
);

CREATE INDEX ix_report_jobs_claim
ON report_jobs (next_attempt_at, created_at)
WHERE status IN ('pending', 'retry_wait');

CREATE INDEX ix_report_jobs_owner_created
ON report_jobs (created_by, created_at DESC);
```

`parameters` 中只保存报表条件，不保存密码、访问令牌等敏感信息。

## 四、API 设计

### 创建报表

```http
POST /v1/reports
Idempotency-Key: 6ec90ec1-...

{
  "report_type": "sales_summary",
  "parameters": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-15",
    "format": "xlsx"
  }
}
```

响应：

```json
{
  "id": "4bf7b034-7b95-47ab-b955-3935853b99bf",
  "status": "pending",
  "progress": 0,
  "created_at": "2026-08-16T10:00:00+08:00"
}
```

建议返回 `202 Accepted`。`Idempotency-Key` 用于避免客户端超时重发时创建重复任务。

### 查询进度

```http
GET /v1/reports/{report_id}
```

```json
{
  "id": "4bf7b034-7b95-47ab-b955-3935853b99bf",
  "status": "running",
  "progress": 46,
  "progress_message": "正在写入第 5/12 个工作表",
  "attempt": 1,
  "max_attempts": 3,
  "error": null,
  "download_url": null
}
```

初版使用轮询即可，推荐间隔 1～3 秒。确实需要实时推送时，再增加 SSE 接口。

### 手动重试

```http
POST /v1/reports/{report_id}/retry
```

仅允许 `failed` 状态重试。重试时：

- 清空错误和旧结果信息；
- 将状态改为 `pending`；
- `next_attempt_at = now()`；
- 可保留 `attempt` 作为历史总尝试次数，或创建新的任务记录并关联原任务。

更推荐创建新任务并增加 `retry_of` 字段，这样审计记录更完整。

### 下载结果

```http
GET /v1/reports/{report_id}/download
```

成功后返回 `302/307` 到一个短期有效的预签名地址。不要让 FastAPI 长时间代理大文件。

未完成时返回：

- `409 Conflict`：任务尚未成功；
- `404 Not Found`：任务不存在或不属于当前用户；
- `410 Gone`：结果已过期并清理。

## 五、Worker 的关键实现

### 原子领取任务

```python
CLAIM_SQL = """
WITH candidate AS (
    SELECT id
    FROM report_jobs
    WHERE status IN ('pending', 'retry_wait')
      AND next_attempt_at <= now()
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE report_jobs AS job
SET status = 'running',
    attempt = attempt + 1,
    worker_id = :worker_id,
    started_at = COALESCE(started_at, now()),
    heartbeat_at = now(),
    updated_at = now()
FROM candidate
WHERE job.id = candidate.id
RETURNING job.*;
"""
```

领取事务应很短：只修改状态，不在事务内生成报表。

Worker 主循环：

```python
async def worker_loop() -> None:
    while True:
        job = await repository.claim_next(worker_id=WORKER_ID)

        if job is None:
            await asyncio.sleep(1)
            continue

        await execute_job(job)
```

### 执行和进度更新

```python
async def execute_job(job: ReportJob) -> None:
    try:
        generator = registry[job.report_type]

        artifact = await generator.generate(
            parameters=job.parameters,
            progress=lambda value, message: repository.update_progress(
                job.id, value, message, WORKER_ID
            ),
        )

        stored = await object_storage.upload(
            key=f"reports/{job.created_by}/{job.id}/{artifact.filename}",
            file_path=artifact.path,
            content_type=artifact.content_type,
        )

        await repository.mark_succeeded(
            job.id,
            result_key=stored.key,
            filename=artifact.filename,
            content_type=artifact.content_type,
            size=stored.size,
        )
    except RetryableReportError as exc:
        await repository.schedule_retry(job, exc)
    except Exception as exc:
        await repository.mark_failed(job.id, public_error(exc))
```

报表生成库通常是同步且消耗 CPU/内存的，不应直接阻塞事件循环：

```python
result = await asyncio.to_thread(build_xlsx, parameters, progress_callback)
```

如果单个报表计算量很大，可在 Worker 内使用受限进程池，但要严格控制并发，防止多个大型报表耗尽内存。

### 自动重试

只重试临时故障，例如数据库连接超时、上游接口 `429/503`、对象存储短暂不可用。参数错误和模板错误应直接失败。

退避时间可以采用：

```python
delay_seconds = min(300, 2 ** attempt * 5) + random.uniform(0, 3)
```

更新逻辑：

```sql
UPDATE report_jobs
SET status = CASE
        WHEN attempt < max_attempts THEN 'retry_wait'::report_status
        ELSE 'failed'::report_status
    END,
    next_attempt_at = CASE
        WHEN attempt < max_attempts THEN now() + :delay
        ELSE next_attempt_at
    END,
    error_code = :error_code,
    error_message = :error_message,
    worker_id = NULL,
    updated_at = now()
WHERE id = :job_id
  AND status = 'running';
```

### Worker 崩溃恢复

Worker 执行时每隔约 15 秒更新 `heartbeat_at`。另一个定时协程回收长时间失去心跳的任务：

```sql
UPDATE report_jobs
SET status = CASE
        WHEN attempt < max_attempts THEN 'retry_wait'::report_status
        ELSE 'failed'::report_status
    END,
    next_attempt_at = now(),
    worker_id = NULL,
    error_code = 'worker_lost',
    error_message = 'Worker heartbeat expired',
    updated_at = now()
WHERE status = 'running'
  AND heartbeat_at < now() - interval '60 seconds';
```

生成过程必须可重复执行。上传文件使用包含任务 ID 的确定性对象键，数据库成功更新前重复上传也不会产生大量孤立文件。

## 六、代码组织

```text
app/
├── main.py
├── api/
│   └── reports.py
├── domain/
│   ├── models.py
│   ├── schemas.py
│   └── errors.py
├── services/
│   ├── report_service.py
│   └── generators/
│       ├── base.py
│       └── sales_summary.py
├── infrastructure/
│   ├── database.py
│   ├── repositories.py
│   └── object_storage.py
├── worker.py
└── settings.py

tests/
├── unit/
│   ├── test_retry_policy.py
│   └── test_sales_generator.py
├── integration/
│   ├── test_report_api.py
│   └── test_worker.py
└── conftest.py
```

生成器通过注册表扩展：

```python
registry: dict[str, ReportGenerator] = {
    "sales_summary": SalesSummaryGenerator(),
}
```

这样新增报表只需增加生成器，不需要在 Worker 中堆积条件分支。

## 七、测试方案

使用 `pytest`、`pytest-asyncio`、`httpx.AsyncClient`。集成测试连接独立 PostgreSQL；对象存储使用测试桶或接口级 fake。

必须覆盖：

1. 创建任务返回 `202`，数据库产生 `pending` 记录。
2. 相同用户和幂等键不会创建重复任务。
3. 两个 Worker 并发领取时不会拿到同一任务。
4. Worker 成功后状态为 `succeeded`，进度为 100。
5. 临时故障进入 `retry_wait`，达到上限后变为 `failed`。
6. 永久错误不会自动重试。
7. 失去心跳的任务可被回收。
8. 未完成任务不能下载。
9. 用户不能查询或下载其他用户的报表。
10. 下载接口生成短期预签名地址。
11. 生成器输入边界、空数据和大数据分页。
12. Worker 被中断后，任务最终可以重新执行。

并发领取测试的核心断言：

```python
jobs = await asyncio.gather(
    repository.claim_next("worker-a"),
    repository.claim_next("worker-b"),
)

claimed_ids = [job.id for job in jobs if job]
assert len(claimed_ids) == len(set(claimed_ids))
```

## 八、上线要点

- API、Worker、PostgreSQL、MinIO/S3 分别部署。
- Worker 设置优雅退出：停止领取新任务，等待当前任务完成到超时。
- 限制每个用户的并发任务数和报表时间范围。
- 对源数据分页读取，XLSX 使用流式或只写模式，CSV 流式写入临时文件。
- 临时文件放在任务独立目录，完成或失败后清理。
- 结果设置保留期限，例如 7 天，并由定时清理任务删除。
- 指标至少包括队列长度、等待时间、执行时间、失败率、重试次数和失联任务数。
- 日志统一包含 `report_id`、`worker_id`、`attempt` 和 `created_by`。
- 错误响应只暴露稳定的错误码和安全信息，完整堆栈保留在服务端日志中。

这套方案不依赖常驻 API 进程执行后台任务，支持多 Worker 并发、进程重启恢复、自动与手动重试，并且从一开始就具备可测试的任务状态边界。