下面是一套不依赖 Celery、可以直接落地的 FastAPI 报表生成服务设计。核心思路是：

- FastAPI 只负责接收请求和查询状态。
- Redis 负责可靠排队、进度事件和重试延迟。
- 独立 Worker 负责生成报表。
- PostgreSQL 保存任务元数据和状态。
- MinIO/S3 保存生成后的文件。

## 一、整体架构

```text
客户端
  |
  v
FastAPI
  |-- PostgreSQL：任务状态、参数、错误信息、结果元数据
  |-- Redis Stream：任务队列、消费确认、失败重投
  |-- Redis Hash/PubSub：实时进度
  |
  v
Report Worker
  |-- 查询数据库
  |-- 分阶段生成报表
  |-- 上传文件到 MinIO/S3
  |-- 更新任务状态
```

不建议直接使用 `BackgroundTasks` 承担核心任务，因为它依附于 Web 进程：

- 进程重启后任务可能丢失；
- 无法跨实例调度；
- 不方便实现可靠重试；
- 无法限制 Worker 并发数。

`BackgroundTasks` 可以用于发送通知等非核心操作，但报表任务应进入持久化队列。

## 二、任务状态

```text
PENDING
  |
  v
RUNNING -----> RETRY_WAITING -----> PENDING
  |                  |
  |                  v
  +-------------> FAILED

RUNNING -----> SUCCEEDED
RUNNING -----> CANCELED
```

建议字段：

```sql
CREATE TABLE report_tasks (
    id              UUID PRIMARY KEY,
    report_type     VARCHAR(100) NOT NULL,
    parameters      JSONB NOT NULL,
    status          VARCHAR(30) NOT NULL,
    progress        INTEGER NOT NULL DEFAULT 0,
    current_step    VARCHAR(200),
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    error_code      VARCHAR(100),
    error_message   TEXT,
    result_key      VARCHAR(500),
    result_filename  VARCHAR(255),
    idempotency_key  VARCHAR(255),
    created_at      TIMESTAMP NOT NULL,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    next_retry_at   TIMESTAMP
);

CREATE UNIQUE INDEX uq_report_idempotency
ON report_tasks(idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

任务状态更新必须使用条件更新，避免多个 Worker 同时处理同一任务：

```sql
UPDATE report_tasks
SET status = 'RUNNING',
    started_at = NOW()
WHERE id = :task_id
  AND status IN ('PENDING', 'RETRY_WAITING');
```

只有更新行数为 `1` 的 Worker 才能继续执行。

## 三、API 设计

### 1. 创建异步任务

```http
POST /api/v1/reports
Idempotency-Key: 20260822-user1-sales
Content-Type: application/json
```

```json
{
  "report_type": "sales",
  "parameters": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-22",
    "department_id": 10,
    "format": "xlsx"
  }
}
```

响应：

```json
{
  "task_id": "0f4d7f8a-2f98-4d8b-b2e9-5f4e12a2b9c3",
  "status": "PENDING",
  "status_url": "/api/v1/reports/0f4d7f8a-2f98-4d8b-b2e9-5f4e12a2b9c3"
}
```

重复使用相同 `Idempotency-Key` 时，返回原任务，而不是重复生成。

### 2. 查询进度

```http
GET /api/v1/reports/{task_id}
```

```json
{
  "task_id": "0f4d7f8a-2f98-4d8b-b2e9-5f4e12a2b9c3",
  "status": "RUNNING",
  "progress": 65,
  "current_step": "生成明细工作表",
  "retry_count": 0,
  "error": null,
  "download_url": null
}
```

### 3. 下载结果

```http
GET /api/v1/reports/{task_id}/download
```

行为：

- `SUCCEEDED`：返回短期有效的预签名 URL，或者由 FastAPI 流式下载；
- `PENDING/RUNNING`：返回 `409`；
- `FAILED`：返回 `410` 或任务错误信息；
- 无权限访问：返回 `404`，避免泄露任务是否存在。

建议使用对象存储预签名 URL，避免 FastAPI 承担大文件传输。

### 4. 手动重试

```http
POST /api/v1/reports/{task_id}/retry
```

只允许对 `FAILED` 任务重试，并重新设置：

```text
status = PENDING
retry_count = 0
error_message = NULL
```

### 5. 实时进度，可选

```http
GET /api/v1/reports/{task_id}/events
```

采用 SSE：

```text
event: progress
data: {"progress": 40, "current_step": "读取订单数据"}

event: progress
data: {"progress": 100, "current_step": "生成完成"}

event: completed
data: {"task_id": "..."}
```

轮询接口仍然要保留，因为 SSE 可能受到代理、移动网络或浏览器限制。

## 四、FastAPI 接口示例

```python
from uuid import UUID, uuid4
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/reports")


class ReportCreate(BaseModel):
    report_type: str = Field(min_length=1, max_length=100)
    parameters: dict


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_report(
    body: ReportCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    if idempotency_key:
        old = await report_repo.get_by_idempotency_key(
            db, current_user.id, idempotency_key
        )
        if old:
            return serialize_task(old)

    task = await report_repo.create(
        db,
        task_id=uuid4(),
        user_id=current_user.id,
        report_type=body.report_type,
        parameters=body.parameters,
        idempotency_key=idempotency_key,
        status="PENDING",
        max_retries=3,
    )

    await task_queue.enqueue(str(task.id))
    return serialize_task(task)


@router.get("/{task_id}")
async def get_report(
    task_id: UUID,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    task = await report_repo.get_owned_task(db, task_id, current_user.id)
    if not task:
        raise HTTPException(status_code=404, detail="report not found")
    return serialize_task(task)


@router.get("/{task_id}/download")
async def download_report(
    task_id: UUID,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    task = await report_repo.get_owned_task(db, task_id, current_user.id)

    if not task:
        raise HTTPException(status_code=404, detail="report not found")
    if task.status != "SUCCEEDED":
        raise HTTPException(
            status_code=409,
            detail={"status": task.status, "message": "report is not ready"},
        )

    url = await object_storage.create_presigned_url(
        task.result_key,
        expires_seconds=300,
    )
    return {"download_url": url}
```

## 五、Redis 队列

推荐使用 Redis Streams：

```python
class RedisTaskQueue:
    stream = "report_tasks"
    group = "report_workers"

    async def enqueue(self, task_id: str) -> None:
        await redis.xadd(
            self.stream,
            {"task_id": task_id},
            maxlen=100_000,
            approximate=True,
        )

    async def consume(self, consumer: str):
        return await redis.xreadgroup(
            groupname=self.group,
            consumername=consumer,
            streams={self.stream: ">"},
            count=1,
            block=5000,
        )

    async def ack(self, message_id: str) -> None:
        await redis.xack(self.stream, self.group, message_id)
```

启动时创建 Consumer Group：

```python
try:
    await redis.xgroup_create(
        name="report_tasks",
        groupname="report_workers",
        id="0",
        mkstream=True,
    )
except ResponseError as exc:
    if "BUSYGROUP" not in str(exc):
        raise
```

生产环境还要处理 Pending Entries：

- Worker 崩溃后，消息会留在 Pending List；
- 定时任务使用 `XAUTOCLAIM` 接管超时消息；
- 接管前检查数据库状态，防止重复执行。

## 六、Worker 实现

```python
class ReportWorker:
    def __init__(self, queue, repo, storage, generators):
        self.queue = queue
        self.repo = repo
        self.storage = storage
        self.generators = generators

    async def run_forever(self):
        consumer = f"worker-{uuid4()}"

        while True:
            messages = await self.queue.consume(consumer)

            if not messages:
                continue

            for _, entries in messages:
                for message_id, fields in entries:
                    task_id = fields[b"task_id"].decode()

                    try:
                        await self.handle(task_id)
                        await self.queue.ack(message_id)
                    except Exception:
                        # handle 内部已经完成状态更新
                        # 未确认的消息由超时接管逻辑处理
                        logger.exception("report task failed", extra={"task_id": task_id})

    async def handle(self, task_id: str):
        task = await self.repo.claim_task(task_id)
        if not task:
            return  # 已经被其他 Worker 处理，或任务已结束

        generator = self.generators.get(task.report_type)
        if generator is None:
            await self.repo.mark_failed(
                task.id,
                error_code="UNKNOWN_REPORT_TYPE",
                error_message="unsupported report type",
            )
            return

        try:
            async def progress(value: int, step: str):
                await self.repo.update_progress(task.id, value, step)
                await redis.publish(
                    f"report:{task.id}:progress",
                    json.dumps({"progress": value, "current_step": step}),
                )

            result = await generator.generate(
                parameters=task.parameters,
                progress=progress,
            )

            result_key = await self.storage.upload(
                result.path,
                content_type=result.content_type,
            )

            await self.repo.mark_succeeded(
                task.id,
                result_key=result_key,
                result_filename=result.filename,
            )

        except RetryableReportError as exc:
            await self.repo.schedule_retry(
                task.id,
                error_code=exc.code,
                error_message=str(exc),
            )

        except Exception as exc:
            await self.repo.mark_failed(
                task.id,
                error_code="REPORT_GENERATION_ERROR",
                error_message=str(exc),
            )
```

## 七、失败重试

只重试明确的临时性错误：

```python
class RetryableReportError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
```

建议退避算法：

```python
delay = min(3600, 30 * (2 ** retry_count)) + random.uniform(0, 10)
```

例如：

```text
第 1 次：30 秒
第 2 次：60 秒
第 3 次：120 秒
```

`retry_count >= max_retries` 后进入 `FAILED`，并写入错误信息。

实现方式有两种：

1. 把 `next_retry_at` 存在 PostgreSQL，定时扫描并重新入队；
2. 使用 Redis ZSET：

```text
ZADD report_retry_queue <unix_timestamp> <task_id>
```

后台调度器定期执行：

```python
async def retry_scheduler():
    while True:
        now = time.time()
        task_ids = await redis.zrangebyscore(
            "report_retry_queue", 0, now, start=0, num=100
        )

        for task_id in task_ids:
            removed = await redis.zrem("report_retry_queue", task_id)
            if removed:
                await queue.enqueue(task_id.decode())

        await asyncio.sleep(2)
```

必须保证 `ZREM` 和入队操作具有幂等性。

## 八、应用启动与关闭

FastAPI 使用 lifespan 管理资源：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await redis.connect()
    await queue.ensure_group()

    yield

    await redis.close()
    await db.close()
```

Worker 建议作为独立进程运行：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.worker
```

容器部署：

```yaml
services:
  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000

  worker:
    build: .
    command: python -m app.worker
    deploy:
      replicas: 3

  postgres:
    image: postgres:16

  redis:
    image: redis:7

  minio:
    image: minio/minio
```

不要在 API 进程中启动多个 Worker，除非已经明确接受 Web 进程和任务进程耦合的运维风险。

## 九、报表生成器接口

不同报表类型使用统一协议：

```python
class ReportResult:
    path: str
    filename: str
    content_type: str


class ReportGenerator(Protocol):
    async def generate(
        self,
        parameters: dict,
        progress: Callable[[int, str], Awaitable[None]],
    ) -> ReportResult:
        ...
```

按类型注册：

```python
generators = {
    "sales": SalesReportGenerator(),
    "inventory": InventoryReportGenerator(),
    "finance": FinanceReportGenerator(),
}
```

生成器内部按阶段上报进度：

```python
await progress(10, "校验参数")
await progress(35, "查询数据")
await progress(70, "写入工作表")
await progress(95, "上传文件")
```

对于大数据量报表，避免一次性加载到内存：

- 使用数据库分页；
- 使用流式游标；
- 使用临时文件；
- 限制单任务最大行数和执行时间；
- 对同一用户设置并发上限。

## 十、测试方案

### 单元测试

测试内容：

- 参数校验；
- 状态流转；
- 进度更新；
- 重试次数和退避时间；
- 非临时错误不重试；
- 幂等键；
- 文件上传失败；
- 未授权下载。

示例：

```python
@pytest.mark.asyncio
async def test_retry_until_failed(repo):
    task = await repo.create(max_retries=2)

    await repo.schedule_retry(task.id, "TIMEOUT", "db timeout")
    assert (await repo.get(task.id)).status == "RETRY_WAITING"

    await repo.schedule_retry(task.id, "TIMEOUT", "db timeout")
    await repo.schedule_retry(task.id, "TIMEOUT", "db timeout")

    result = await repo.get(task.id)
    assert result.status == "FAILED"
    assert result.retry_count == 2
```

### API 测试

使用：

- `pytest`
- `pytest-asyncio`
- `httpx.AsyncClient`
- `fakeredis`
- 测试数据库或 Testcontainers

```python
@pytest.mark.asyncio
async def test_create_report(client, mock_queue):
    response = await client.post(
        "/api/v1/reports",
        headers={"Idempotency-Key": "same-key"},
        json={
            "report_type": "sales",
            "parameters": {"format": "xlsx"},
        },
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    mock_queue.enqueue.assert_awaited_once_with(task_id)
```

必须额外测试：

- 相同幂等键只创建一个任务；
- 两个 Worker 竞争同一任务时只有一个成功 claim；
- Worker 崩溃后 Pending 消息能被接管；
- 任务完成后下载 URL 可用；
- 重试后最终成功；
- 超过最大重试次数后进入失败状态。

### 集成测试

通过 Testcontainers 启动：

```text
PostgreSQL + Redis + MinIO + API + Worker
```

执行真实流程：

```text
提交任务 -> Worker 消费 -> 查询进度 -> 生成文件 -> 下载文件
```

## 十一、必须考虑的生产问题

- 下载接口必须校验任务归属；
- 文件名不能直接使用用户输入；
- 报表参数需要白名单校验；
- Redis 和数据库连接设置超时；
- Worker 设置优雅退出和任务超时；
- 记录 `task_id`、`trace_id`、`retry_count`；
- 失败日志中不要输出敏感报表参数；
- 文件设置生命周期，定期清理过期结果；
- 对 API、Redis、数据库增加监控指标；
- 监控队列长度、Pending 消息数、平均生成时长、失败率；
- 同一任务的状态更新必须幂等；
- 生成结果上传成功但数据库更新失败时，需要补偿扫描；
- 数据库使用乐观锁或条件更新，避免重复生成。

一个实际可用的最小技术栈是：

```text
FastAPI
SQLAlchemy 2.x + asyncpg
Redis 7 + redis.asyncio
PostgreSQL 16
MinIO/S3
pytest + pytest-asyncio + httpx + fakeredis
```

这套方案保留了异步任务、进度查询、失败重试、结果下载和完整测试能力，同时避免引入 Celery 及其 Broker/Worker 管理复杂度。