下面是一套可落地的实现：FastAPI 负责接口和任务登记，Redis Streams 负责任务分发，独立 Worker 负责生成报表，PostgreSQL 保存任务状态，文件系统或对象存储保存结果文件。

## 1. 总体架构

```text
Client
  |
  v
FastAPI
  |-- PostgreSQL：任务元数据、状态、重试次数
  |-- Redis Streams：待执行任务队列
  |-- Object Storage：报表文件
  |
Worker x N
  |-- 消费 Redis Stream
  |-- 执行报表生成器
  |-- 更新进度和任务状态
```

推荐技术栈：

- FastAPI
- SQLAlchemy 2.0 Async + asyncpg
- Redis asyncio client
- Pydantic v2
- httpx + pytest
- S3/MinIO，开发环境可使用本地目录
- `openpyxl`、`pandas`、`weasyprint` 等报表生成库

## 2. 任务状态

```text
PENDING -> RUNNING -> SUCCEEDED
                   |
                   -> FAILED
                   |
                   -> RETRY_WAITING -> RUNNING
```

状态字段建议：

```python
class ReportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAITING = "retry_waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
```

任务表：

```sql
CREATE TABLE report_tasks (
    id UUID PRIMARY KEY,
    report_type VARCHAR(100) NOT NULL,
    params JSONB NOT NULL,
    status VARCHAR(30) NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    result_key VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_report_tasks_status
ON report_tasks(status);
```

任务状态更新必须带条件，避免重复 Worker 覆盖结果：

```sql
UPDATE report_tasks
SET status = 'succeeded',
    progress = 100,
    result_key = :result_key,
    finished_at = NOW(),
    updated_at = NOW()
WHERE id = :task_id
  AND status = 'running';
```

## 3. API 设计

### 创建报表

```http
POST /api/v1/reports
Content-Type: application/json
```

请求：

```json
{
  "report_type": "sales",
  "params": {
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "department_id": 10
  },
  "max_retries": 3
}
```

响应：

```json
{
  "task_id": "8d9d2e1f-3b73-4f19-a9cb-8f08d7e5e21a",
  "status": "pending"
}
```

### 查询进度

```http
GET /api/v1/reports/{task_id}
```

响应：

```json
{
  "task_id": "...",
  "status": "running",
  "progress": 65,
  "retry_count": 0,
  "error_message": null,
  "download_url": null
}
```

### 手动重试

```http
POST /api/v1/reports/{task_id}/retry
```

只允许对 `failed` 任务调用，并且不能超过最大重试次数。

### 下载结果

```http
GET /api/v1/reports/{task_id}/download
```

- 未完成：返回 `409`
- 任务失败：返回 `404` 或 `409`
- 成功：返回文件流，或返回对象存储短期签名 URL

```python
@router.get("/{task_id}/download")
async def download_report(task_id: UUID, db: AsyncSession = Depends(get_db)):
    task = await report_repo.get(db, task_id)

    if not task:
        raise HTTPException(404, "任务不存在")

    if task.status != ReportStatus.SUCCEEDED:
        raise HTTPException(409, "报表尚未生成")

    return FileResponse(
        path=storage.resolve(task.result_key),
        filename=f"{task_id}.xlsx",
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )
```

## 4. 创建任务实现

```python
@router.post("", response_model=CreateReportResponse, status_code=202)
async def create_report(
    payload: CreateReportRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    task = ReportTask(
        id=uuid4(),
        report_type=payload.report_type,
        params=payload.params,
        status=ReportStatus.PENDING,
        max_retries=payload.max_retries,
    )

    db.add(task)
    await db.commit()

    await redis.xadd(
        "report_tasks",
        {
            "task_id": str(task.id),
            "report_type": task.report_type,
        },
    )

    return CreateReportResponse(
        task_id=task.id,
        status=task.status,
    )
```

数据库提交成功后再发送队列消息，仍可能出现“数据库成功、消息发送失败”。生产环境建议增加 Outbox 表：

```text
report_tasks + task_outbox
事务内同时写入
后台 dispatcher 持续发送未投递消息
```

这样可以避免任务丢失。

## 5. Worker 设计

Worker 使用 Redis Stream Consumer Group：

```python
STREAM = "report_tasks"
GROUP = "report_workers"
CONSUMER = f"worker-{uuid4()}"

async def worker_loop():
    await ensure_consumer_group()

    while True:
        messages = await redis.xreadgroup(
            groupname=GROUP,
            consumername=CONSUMER,
            streams={STREAM: ">"},
            count=1,
            block=5000,
        )

        for _, entries in messages:
            for message_id, data in entries:
                try:
                    await process_task(data["task_id"])
                    await redis.xack(STREAM, GROUP, message_id)
                except Exception:
                    # process_task 内部负责状态和重试
                    await redis.xack(STREAM, GROUP, message_id)
```

任务执行：

```python
async def process_task(task_id: str):
    task = await repo.claim(task_id)

    if not task:
        return  # 已被其他 Worker 领取或已经完成

    try:
        await repo.update_progress(task_id, 5)

        generator = REPORT_GENERATORS[task.report_type]
        result_path = await generator.generate(
            params=task.params,
            progress=lambda value: repo.update_progress(task_id, value),
        )

        result_key = await storage.save(result_path, task_id)
        await repo.mark_success(task_id, result_key)

    except RetryableReportError as exc:
        await handle_retry(task, str(exc))

    except Exception as exc:
        await repo.mark_failed(task_id, str(exc))
```

领取任务时使用条件更新：

```python
async def claim(task_id: str):
    result = await session.execute(
        update(ReportTask)
        .where(
            ReportTask.id == task_id,
            ReportTask.status.in_([
                ReportStatus.PENDING,
                ReportStatus.RETRY_WAITING,
            ]),
        )
        .values(
            status=ReportStatus.RUNNING,
            started_at=func.now(),
            updated_at=func.now(),
        )
        .returning(ReportTask)
    )
    return result.scalar_one_or_none()
```

## 6. 失败重试

只重试临时性错误，例如：

- 数据库连接短暂失败
- 上游 HTTP 503
- 对象存储暂时不可用

不应重试：

- 参数校验失败
- 报表类型不存在
- SQL 语义错误
- 权限错误

指数退避：

```python
async def handle_retry(task, error: str):
    next_retry = task.retry_count + 1

    if next_retry > task.max_retries:
        await repo.mark_failed(task.id, error)
        return

    delay = min(60, 2 ** next_retry)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    await repo.mark_retry_waiting(
        task.id,
        retry_count=next_retry,
        error_message=error,
        retry_at=retry_at,
    )
```

可以增加一个 scheduler，定期扫描：

```sql
SELECT id
FROM report_tasks
WHERE status = 'retry_waiting'
  AND retry_at <= NOW()
LIMIT 100;
```

扫描到后重新写入 Redis Stream。

为了防止重复生成，结果路径使用确定性命名：

```text
reports/{task_id}/result.xlsx
```

写入采用临时文件加原子替换：

```text
result.xlsx.tmp -> result.xlsx
```

## 7. 进度更新

报表生成器不要直接操作数据库，只接收进度回调：

```python
class SalesReportGenerator:
    async def generate(self, params, progress):
        rows = await load_sales_data(params)

        total = max(len(rows), 1)
        workbook = Workbook()

        for index, row in enumerate(rows, start=1):
            write_row(workbook, row)

            if index % 100 == 0 or index == total:
                await progress(10 + int(index / total * 80))

        path = build_temp_path()
        workbook.save(path)
        await progress(95)
        return path
```

进度更新建议：

- 限制为每 1 秒最多一次
- 只允许单调递增
- Worker 心跳单独保存 `heartbeat_at`
- 运行超时任务由 watchdog 标记为失败或重新入队

## 8. 项目结构

```text
app/
  main.py
  api/
    reports.py
  core/
    config.py
    redis.py
    storage.py
  db/
    models.py
    session.py
    repositories.py
  reports/
    base.py
    sales.py
    registry.py
  workers/
    main.py
    processor.py
tests/
  test_reports_api.py
  test_report_repository.py
  test_worker_processor.py
  test_retry.py
docker-compose.yml
```

启动方式：

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.workers.main
```

生产环境让 API 和 Worker 分开扩容：

```text
API:    2~4 个进程
Worker: 按 CPU、内存和报表耗时独立扩容
Redis:  主从或托管 Redis
Postgres: 托管数据库
```

## 9. 测试方案

### API 单元测试

覆盖：

- 创建任务返回 `202`
- 查询不存在任务返回 `404`
- 查询运行中任务包含进度
- 成功任务可下载
- 未完成任务下载返回 `409`
- 失败任务可手动重试
- 超过最大重试次数后不能再次重试

```python
@pytest.mark.anyio
async def test_create_report(client, fake_redis, db_session):
    response = await client.post(
        "/api/v1/reports",
        json={
            "report_type": "sales",
            "params": {"department_id": 10},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert "task_id" in body
```

### Worker 测试

使用假的报表生成器：

```python
async def test_worker_success(repo, storage):
    generator = FakeGenerator()
    task = await create_pending_task()

    await process_task(str(task.id))

    updated = await repo.get(task.id)
    assert updated.status == ReportStatus.SUCCEEDED
    assert updated.progress == 100
```

### 重试测试

```python
async def test_retry_until_failed(repo):
    generator = AlwaysTemporaryFailGenerator()

    await process_task(task_id)
    await process_task(task_id)
    await process_task(task_id)
    await process_task(task_id)

    task = await repo.get(task_id)
    assert task.status == ReportStatus.FAILED
    assert task.retry_count == task.max_retries
```

### 集成测试

使用 Docker Testcontainers 或 CI 服务启动：

- PostgreSQL
- Redis
- MinIO

测试完整链路：

```text
POST 创建任务
  -> Worker 消费
  -> 状态变为 running
  -> 进度增加
  -> 状态变为 succeeded
  -> 下载文件并校验内容
```

## 10. 必须补充的生产能力

- 接口鉴权和租户隔离
- 任务创建幂等键，例如 `Idempotency-Key`
- 单租户并发数限制
- 报表参数白名单和最大时间范围
- 文件下载权限校验
- 任务超时 watchdog
- 结构化日志和 trace_id
- Prometheus 指标：
  - `report_created_total`
  - `report_success_total`
  - `report_failure_total`
  - `report_duration_seconds`
  - `report_queue_lag_seconds`
- 定期清理过期文件和历史任务

这套方案的核心是：API 不执行耗时工作，Worker 可横向扩展，任务状态持久化，队列消息可重投，重试策略明确，下载结果可追踪，且每一层都能独立测试。