下面是一套可直接落地的图像处理 API 设计，采用 FastAPI + PostgreSQL + 对象存储 + 独立 Worker。任务由数据库承载，不依赖 Celery。

## 1. 核心架构

```text
Client
  |
  | POST /v1/jobs
  v
API Service
  |-- 写入 job / job_attempt
  |-- 上传原图到对象存储
  |-- 返回 job_id
  v
PostgreSQL 任务队列
  |
  | SELECT ... FOR UPDATE SKIP LOCKED
  v
Image Worker
  |-- 获取任务租约
  |-- 处理图像
  |-- 更新进度
  |-- 写入结果文件
  v
Object Storage
```

组件职责：

- API Service：鉴权、上传、任务创建、状态查询、结果下载。
- PostgreSQL：任务状态、进度、重试信息、租约和幂等控制。
- Worker：执行图像处理，支持水平扩展。
- 对象存储：保存原图和处理结果，不把大文件放进数据库。
- 定时 Reaper：回收超时任务，避免 Worker 崩溃后任务永久卡住。

## 2. 任务状态

```text
queued
  |
  v
running ---> retry_waiting ---> running
  |                              |
  |                              v
  |--------------------------> failed
  |
  v
succeeded

queued/running -> canceled
```

状态说明：

- `queued`：等待 Worker 获取。
- `running`：Worker 正在处理。
- `retry_waiting`：发生可重试错误，等待下一次执行。
- `succeeded`：处理完成，可获取结果。
- `failed`：达到最大重试次数或发生不可重试错误。
- `canceled`：用户取消任务。

## 3. API 设计

### 创建任务

```http
POST /v1/jobs
Authorization: Bearer <token>
Idempotency-Key: 7f4b...

Content-Type: multipart/form-data
```

参数：

```text
file: 原始图片
operation: resize | thumbnail | convert | watermark
width: 可选
height: 可选
format: jpeg | png | webp
```

响应：

```http
202 Accepted
```

```json
{
  "id": "job_01JABC...",
  "status": "queued",
  "progress": 0,
  "status_url": "/v1/jobs/job_01JABC...",
  "events_url": "/v1/jobs/job_01JABC.../events",
  "result_url": null,
  "created_at": "2026-08-16T10:00:00Z"
}
```

同一个 `Idempotency-Key` 在有效期内重复提交时，返回原任务，不重复处理。

### 查询任务

```http
GET /v1/jobs/{job_id}
```

响应：

```json
{
  "id": "job_01JABC...",
  "status": "running",
  "progress": 64,
  "stage": "encoding",
  "attempt": 1,
  "max_attempts": 3,
  "error": null,
  "created_at": "2026-08-16T10:00:00Z",
  "started_at": "2026-08-16T10:00:03Z",
  "finished_at": null,
  "result": null
}
```

失败响应示例：

```json
{
  "status": "failed",
  "progress": 42,
  "error": {
    "code": "UNSUPPORTED_FORMAT",
    "message": "图片格式不受支持",
    "retryable": false
  }
}
```

### 订阅进度

```http
GET /v1/jobs/{job_id}/events
Accept: text/event-stream
```

事件示例：

```text
event: progress
data: {"status":"running","progress":35,"stage":"decoding"}

event: progress
data: {"status":"running","progress":80,"stage":"encoding"}

event: completed
data: {"status":"succeeded","progress":100}
```

客户端断线后可以使用 `GET /v1/jobs/{job_id}` 继续查询，不依赖 SSE 的可靠投递。

### 获取结果

```http
GET /v1/jobs/{job_id}/result
```

成功时返回短期签名 URL：

```json
{
  "download_url": "https://storage.example.com/...",
  "expires_in": 900,
  "content_type": "image/webp",
  "size": 248391
}
```

任务未完成时返回 `409`，任务失败时返回 `422`。

### 取消任务

```http
POST /v1/jobs/{job_id}/cancel
```

只能取消 `queued`、`running` 或 `retry_waiting` 状态的任务。

## 4. 数据库表

```sql
CREATE TABLE jobs (
    id              UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    idempotency_key VARCHAR(128),
    status          VARCHAR(32) NOT NULL,
    operation       VARCHAR(32) NOT NULL,
    input_key       TEXT NOT NULL,
    result_key      TEXT,
    progress        SMALLINT NOT NULL DEFAULT 0,
    stage           VARCHAR(64),
    attempt         SMALLINT NOT NULL DEFAULT 0,
    max_attempts    SMALLINT NOT NULL DEFAULT 3,
    error_code      VARCHAR(64),
    error_message   TEXT,
    next_run_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until     TIMESTAMPTZ,
    lease_token     UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX jobs_queue_idx
ON jobs (status, next_run_at, created_at);
```

可选的尝试记录表：

```sql
CREATE TABLE job_attempts (
    id          BIGSERIAL PRIMARY KEY,
    job_id      UUID NOT NULL REFERENCES jobs(id),
    attempt     SMALLINT NOT NULL,
    worker_id   VARCHAR(128) NOT NULL,
    status      VARCHAR(32) NOT NULL,
    error_code  VARCHAR(64),
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
);
```

## 5. Worker 获取任务

关键点是使用数据库行锁和租约，允许多个 Worker 并行运行：

```sql
WITH next_job AS (
    SELECT id
    FROM jobs
    WHERE status IN ('queued', 'retry_waiting')
      AND next_run_at <= now()
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs
SET status = 'running',
    attempt = attempt + 1,
    lease_token = :lease_token,
    lease_until = now() + interval '5 minutes',
    started_at = COALESCE(started_at, now())
WHERE id IN (SELECT id FROM next_job)
RETURNING *;
```

Worker 处理期间每 10 秒续租：

```sql
UPDATE jobs
SET lease_until = now() + interval '5 minutes'
WHERE id = :job_id
  AND status = 'running'
  AND lease_token = :lease_token;
```

更新进度时必须校验 `lease_token`，防止旧 Worker 覆盖新 Worker 的状态。

## 6. 重试策略

错误分为两类：

可重试：

- 对象存储临时不可用；
- 网络超时；
- 图像处理进程崩溃；
- 数据库暂时不可用；
- 外部服务返回 `5xx`。

不可重试：

- 文件格式非法；
- 文件超过大小限制；
- 图片损坏；
- 参数校验失败；
- 无权限访问输入文件。

建议策略：

```text
delay = min(300, 2 ^ attempt * 5 + random(0, 5))
```

例如：

```text
第 1 次失败：等待 10 秒
第 2 次失败：等待 20 秒
第 3 次失败：标记 failed
```

重试时：

```sql
UPDATE jobs
SET status = CASE
        WHEN attempt < max_attempts THEN 'retry_waiting'
        ELSE 'failed'
    END,
    next_run_at = now() + :backoff,
    error_code = :error_code,
    error_message = :error_message
WHERE id = :job_id
  AND lease_token = :lease_token;
```

## 7. 伪代码

```python
async def process_job(job):
    try:
        await update_progress(job, 5, "downloading")

        source = await storage.download(job.input_key)
        await update_progress(job, 25, "decoding")

        image = decode_image_safely(
            source,
            max_pixels=40_000_000,
            allowed_formats={"jpeg", "png", "webp"}
        )

        await update_progress(job, 60, "processing")
        output = process_image(image, job.operation, job.parameters)

        await update_progress(job, 85, "uploading")
        result_key = await storage.upload(output)

        await mark_succeeded(job, result_key)

    except RetryableError as exc:
        await schedule_retry(job, exc)

    except PermanentError as exc:
        await mark_failed(job, exc)

    except Exception as exc:
        await schedule_retry(job, exc)
```

Reaper 每分钟执行：

```sql
UPDATE jobs
SET status = CASE
        WHEN attempt < max_attempts THEN 'retry_waiting'
        ELSE 'failed'
    END,
    next_run_at = now()
WHERE status = 'running'
  AND lease_until < now();
```

## 8. 安全和可靠性

- 限制上传大小、像素总数和解压后尺寸，防止图片炸弹。
- 不信任客户端 MIME 类型，使用文件头和解码器检测格式。
- 原图和结果使用随机对象存储 Key，禁止用户直接拼接路径。
- 结果下载使用短期签名 URL。
- 所有任务按 `tenant_id` 隔离。
- 对 API、上传和单租户并发数做限流。
- Worker 使用临时目录，任务完成后清理。
- 记录任务耗时、重试次数、失败原因和队列等待时间。
- 通过 `lease_token` 防止重复完成、旧 Worker 覆盖状态等竞态问题。

## 9. 部署建议

```text
api:        2+ instances
worker:     N instances, based on CPU/memory
postgres:   primary + backup
object store: S3-compatible storage
scheduler:  lightweight periodic process for reaper
```

Worker 应设计为无状态进程，可通过增加实例数量提升吞吐量；数据库只保存任务元数据，图像内容始终放在对象存储中。