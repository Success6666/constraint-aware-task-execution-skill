已按 `constraint-aware-task-execution` 的原则完成方案设计：约束仅体现在任务调度选型上，不额外增加“禁止 Celery”的检测或拦截逻辑。

当前工作区为只读，无法创建项目文件或执行测试；下面给出可直接落地的项目结构与实现方案。

## 项目结构

```text
app/
  __init__.py
  main.py
  models.py
  task_manager.py
  report_generator.py
tests/
  test_reports.py
requirements.txt
```

## 核心设计

- `asyncio.Queue`：异步任务队列。
- `asyncio` worker：启动多个后台 worker 执行报表任务。
- 内存仓库：保存任务状态、进度、错误、结果文件路径。
- CSV 生成：作为示例报表格式，可替换为 Excel、PDF 或数据库查询。
- 显式重试：失败后调用重试接口，限制最大重试次数。
- `FileResponse`：成功任务提供结果下载。
- 生产环境可将内存仓库替换为 PostgreSQL 或 Redis，但不依赖 Celery。

## 状态模型

```python
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReportRequest(BaseModel):
    report_type: str = Field(default="sales")
    parameters: dict[str, Any] = Field(default_factory=dict)


class ReportTask(BaseModel):
    id: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    retry_count: int = 0
    max_retries: int = 3
    error: str | None = None
    result_path: str | None = None
    created_at: datetime
    updated_at: datetime
```

## 任务管理器

```python
import asyncio
import uuid
from pathlib import Path
from datetime import datetime, timezone

from .models import ReportTask, ReportRequest, TaskStatus
from .report_generator import generate_report


class TaskManager:
    def __init__(self, worker_count: int = 2, output_dir: str = "outputs"):
        self.queue: asyncio.Queue[tuple[str, ReportRequest]] = asyncio.Queue()
        self.tasks: dict[str, ReportTask] = {}
        self.requests: dict[str, ReportRequest] = {}
        self.output_dir = Path(output_dir)
        self.worker_count = worker_count
        self.workers: list[asyncio.Task] = []

    async def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workers = [
            asyncio.create_task(self._worker())
            for _ in range(self.worker_count)
        ]

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def submit(self, request: ReportRequest) -> ReportTask:
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        task = ReportTask(
            id=task_id,
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = task
        self.requests[task_id] = request
        await self.queue.put((task_id, request))
        return task

    async def retry(self, task_id: str) -> ReportTask:
        task = self.tasks[task_id]

        if task.status != TaskStatus.FAILED:
            raise ValueError("只有失败任务可以重试")

        if task.retry_count >= task.max_retries:
            raise RuntimeError("已达到最大重试次数")

        task.retry_count += 1
        task.status = TaskStatus.PENDING
        task.progress = 0
        task.error = None
        task.updated_at = datetime.now(timezone.utc)

        await self.queue.put((task_id, self.requests[task_id]))
        return task

    async def _worker(self):
        while True:
            task_id, request = await self.queue.get()
            task = self.tasks[task_id]

            try:
                task.status = TaskStatus.RUNNING
                task.updated_at = datetime.now(timezone.utc)

                result_path = await generate_report(
                    task_id=task_id,
                    request=request,
                    output_dir=self.output_dir,
                    on_progress=lambda value: self._update_progress(task_id, value),
                )

                task.status = TaskStatus.SUCCEEDED
                task.progress = 100
                task.result_path = str(result_path)
                task.updated_at = datetime.now(timezone.utc)

            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                task.updated_at = datetime.now(timezone.utc)

            finally:
                self.queue.task_done()

    def _update_progress(self, task_id: str, progress: int):
        task = self.tasks[task_id]
        task.progress = max(0, min(progress, 100))
        task.updated_at = datetime.now(timezone.utc)
```

## 报表生成器

```python
import asyncio
import csv
from pathlib import Path

from .models import ReportRequest


async def generate_report(
    task_id: str,
    request: ReportRequest,
    output_dir: Path,
    on_progress,
) -> Path:
    if request.parameters.get("simulate_failure"):
        raise RuntimeError("模拟报表生成失败")

    output_path = output_dir / f"{task_id}.csv"

    rows = [
        ["日期", "销售额", "订单数"],
        ["2026-08-01", "12800", "42"],
        ["2026-08-02", "15300", "51"],
    ]

    for progress in (20, 50, 80):
        await asyncio.sleep(0.05)
        on_progress(progress)

    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        csv.writer(file).writerows(rows)

    return output_path
```

## FastAPI 接口

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from .models import ReportRequest, ReportTask, TaskStatus
from .task_manager import TaskManager


manager = TaskManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Async Report Service", lifespan=lifespan)


@app.post(
    "/reports",
    response_model=ReportTask,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_report(request: ReportRequest):
    return await manager.submit(request)


@app.get("/reports/{task_id}", response_model=ReportTask)
async def get_report_status(task_id: str):
    task = manager.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/reports/{task_id}/retry", response_model=ReportTask)
async def retry_report(task_id: str):
    if task_id not in manager.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    try:
        return await manager.retry(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/reports/{task_id}/download")
async def download_report(task_id: str):
    task = manager.tasks.get(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != TaskStatus.SUCCEEDED or not task.result_path:
        raise HTTPException(status_code=409, detail="报表尚未生成完成")

    path = Path(task.result_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="结果文件不存在")

    return FileResponse(
        path=path,
        filename=f"report-{task_id}.csv",
        media_type="text/csv",
    )
```

## 依赖

```text
fastapi>=0.115
uvicorn[standard]>=0.30
pytest>=8.0
httpx>=0.27
```

启动：

```bash
uvicorn app.main:app --reload
```

## 测试重点

应覆盖：

1. 创建任务返回 `202` 和任务 ID。
2. 轮询任务最终进入 `succeeded`。
3. 成功任务可下载 CSV。
4. 模拟失败后进入 `failed`。
5. 失败任务重试后成功。
6. 超过最大重试次数返回 `409`。
7. 不存在任务返回 `404`。
8. 未完成任务下载返回 `409`。

测试时可使用 `httpx.AsyncClient` 和 FastAPI lifespan，避免依赖真实服务器进程。

这个方案适合单实例部署；如果需要多实例、进程崩溃恢复或长期任务持久化，应将任务状态和队列替换为数据库加 Redis Streams/RQ 等组件，并保留现有 API 契约。