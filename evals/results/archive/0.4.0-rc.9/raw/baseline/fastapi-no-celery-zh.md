建议采用“FastAPI API + PostgreSQL 任务队列 + 独立 Worker + S3/MinIO”架构。任务状态以 PostgreSQL 为准，利用 `FOR UPDATE SKIP LOCKED` 抢占任务，无需 Celery，也不会因 API 进程重启而丢任务。

## 架构

```text
Client
  │ POST /reports
  ▼
FastAPI ───── PostgreSQL（任务、进度、重试、租约）
  │
  │ GET /reports/{id}
  │ GET /reports/{id}/download
  ▼
Worker 进程 ── 生成 CSV/XLSX/PDF ── S3/MinIO
```

API 与 Worker 使用同一代码镜像、不同启动命令：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker
```

## 数据模型

```sql
CREATE TYPE report_status AS ENUM (
  'pending', 'running', 'retrying', 'succeeded', 'failed', 'cancelled'
);

CREATE TABLE report_jobs (
  id UUID PRIMARY KEY,
  report_type VARCHAR(64) NOT NULL,
  parameters JSONB NOT NULL,
  status report_status NOT NULL DEFAULT 'pending',
  progress SMALLINT NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  attempts INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_by VARCHAR(128),
  lease_expires_at TIMESTAMPTZ,
  error_code VARCHAR(64),
  error_message TEXT,
  result_key TEXT,
  result_filename TEXT,
  result_content_type TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX report_jobs_claim_idx
ON report_jobs (next_run_at, created_at)
WHERE status IN ('pending', 'retrying');
```

`parameters` 中只保存查询条件和业务 ID，不保存 SQL、访问令牌或大块数据。

## API

```text
POST   /v1/reports
GET    /v1/reports/{job_id}
POST   /v1/reports/{job_id}/retry
DELETE /v1/reports/{job_id}
GET    /v1/reports/{job_id}/download
```

创建任务：

```json
POST /v1/reports
{
  "report_type": "monthly_sales",
  "parameters": {
    "month": "2026-08",
    "department_ids": [10, 20]
  }
}
```

返回 `202 Accepted`：

```json
{
  "id": "b697…",
  "status": "pending",
  "progress": 0,
  "status_url": "/v1/reports/b697…"
}
```

建议支持 `Idempotency-Key`，并以 `(tenant_id, idempotency_key)` 唯一约束防止重复提交。下载接口验证租户和权限后返回 302 到短期预签名 URL，避免 FastAPI 转发大文件。

## Worker 核心机制

任务抢占必须在短事务内完成：

```sql
WITH candidate AS (
  SELECT id
  FROM report_jobs
  WHERE status IN ('pending', 'retrying')
    AND next_run_at <= now()
  ORDER BY next_run_at, created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE report_jobs j
SET status = 'running',
    attempts = attempts + 1,
    locked_by = :worker_id,
    lease_expires_at = now() + interval '2 minutes',
    started_at = COALESCE(started_at, now()),
    updated_at = now()
FROM candidate
WHERE j.id = candidate.id
RETURNING j.*;
```

处理期间每 30 秒续租，并按阶段更新进度，例如：

```text
5  校验参数
15 查询数据
40 分批读取
70 生成文件
90 上传对象存储
100 完成
```

大数据应使用数据库游标或分页读取，CSV/XLSX 使用流式写入临时文件，上传成功后再把任务置为 `succeeded`。不要把完整报表留在内存中。

## 失败与重试

仅自动重试临时故障，例如数据库连接中断、对象存储超时和限流。参数错误、权限错误、模板错误直接失败。

```python
delay = min(300, 2 ** attempts * 5) + random.uniform(0, 3)
```

失败时：

- `attempts < max_attempts`：置为 `retrying`，写入 `next_run_at`
- 达到上限：置为 `failed`，记录脱敏后的错误摘要
- Worker 崩溃：巡检器将租约过期的 `running` 任务重新置为 `retrying`
- 手动重试：仅允许 `failed` 状态，并清理原错误信息
- 生成逻辑保持幂等，结果对象键使用 `reports/{tenant_id}/{job_id}/output.xlsx`

进度和状态更新应包含 `WHERE locked_by = :worker_id`，防止租约失效后的旧 Worker 覆盖新 Worker 的结果。

## 代码边界

```text
app/
  main.py              FastAPI 生命周期与路由
  api/reports.py       HTTP 接口
  schemas/reports.py   Pydantic 请求/响应
  models/report_job.py SQLAlchemy 模型
  services/reports.py  创建、查询、授权、重试
  reports/registry.py  report_type 到生成器的白名单映射
  reports/sales.py     具体报表生成器
  storage.py           S3/MinIO 接口
  worker.py            抢占、续租、执行、退避
  recovery.py          过期租约恢复
tests/
```

生成器统一协议：

```python
class ReportGenerator(Protocol):
    async def generate(
        self,
        parameters: dict,
        output_path: Path,
        progress: Callable[[int], Awaitable[None]],
    ) -> ReportArtifact: ...
```

## 测试策略

- 单元测试：参数校验、生成器、退避计算、错误分类、状态转换
- API 测试：提交、查询、越权访问、手动重试、下载前置条件
- Worker 集成测试：抢占并发、生成成功、自动重试、达到上限
- 故障测试：Worker 被终止后租约恢复、上传成功但状态写回失败
- 存储测试：使用 MinIO 或 fake storage，验证对象键和预签名下载
- 数据库测试：使用真实 PostgreSQL 容器，因为 SQLite 不支持等价的 `SKIP LOCKED`

关键并发测试应同时启动两个 Worker，断言同一任务只被成功领取一次。

## 部署要求

至少运行 2 个 API 实例、2 个 Worker 实例和 1 个租约恢复进程。配置连接池上限、单 Worker 并发数、任务超时、报表行数上限、文件大小上限及对象生命周期清理。指标至少包括队列长度、等待时间、执行耗时、成功率、重试率和租约过期数，并以 `job_id` 贯穿结构化日志。