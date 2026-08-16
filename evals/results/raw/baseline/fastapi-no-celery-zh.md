建议采用以下生产可落地方案：

**技术架构**

```text
FastAPI API
    │
    ├── PostgreSQL：任务状态、进度、重试记录
    ├── Redis + ARQ：异步任务调度
    └── S3/MinIO：报表结果存储
                         ▲
                         │
                    ARQ Worker
```

ARQ 原生支持 asyncio、延迟执行和自动重试，适合 FastAPI。任务状态以 PostgreSQL 为准，Redis 只负责调度，避免队列数据和业务状态耦合。开发环境可使用 SQLite 和本地文件目录。

**任务模型**

`report_jobs` 表主要字段：

- `id`：UUID
- `status`：`queued/running/succeeded/failed`
- `progress`：0-100，单调递增
- `request_payload`、`idempotency_key`
- `attempts`、`max_attempts`
- `error_code`、`error_message`
- `result_key`、`result_size`、`checksum`
- `started_at`、`finished_at`、`expires_at`
- `worker_heartbeat`
- `created_at`、`updated_at`

对 `idempotency_key` 建唯一索引，避免客户端重复提交产生多个任务。

**接口设计**

```http
POST /api/v1/reports
GET  /api/v1/reports/{job_id}
POST /api/v1/reports/{job_id}/retry
GET  /api/v1/reports/{job_id}/download
GET  /health/live
GET  /health/ready
```

创建任务返回 `202 Accepted`：

```json
{
  "job_id": "019...",
  "status": "queued",
  "progress": 0
}
```

查询接口返回当前进度、尝试次数和结构化错误。下载接口只允许下载 `succeeded` 状态的任务，通过服务流式返回文件或生成短期签名 URL。

**执行流程**

1. API 校验报表类型、查询条件和数据规模。
2. 数据库事务内创建任务，通过事务提交钩子投递 ARQ。
3. Worker 原子认领任务并更新为 `running`。
4. 查询数据时使用分页或服务端游标，生成文件时使用流式写入。
5. 进度按阶段或固定时间间隔写库，避免高频更新。
6. 先写临时对象，完成后原子发布最终文件。
7. 成功后保存大小和 SHA-256；失败时记录结构化错误。
8. 可重试异常执行指数退避；达到上限后进入 `failed`。
9. Worker 心跳超时后由恢复任务重新入队。
10. 定时清理过期任务和结果文件。

手动重试仅允许最终失败的任务，并使用数据库条件更新防止并发重复重试。

**安全边界**

- 报表类型使用白名单映射，禁止客户端提交 SQL、模板路径或任意 URL。
- 所有查询使用参数绑定。
- 下载按任务所有者鉴权；不存在或无权限统一返回 `404`。
- 对时间跨度、结果行数、并发任务数和文件大小设置上限。
- 对下载文件名进行清洗，存储层只使用服务端生成的对象键。
- CSV 对以 `= + - @` 开头的单元格进行公式注入防护。
- 错误响应不返回堆栈、SQL 或存储路径。

**工程结构**

```text
app/
  api/routes/reports.py
  core/config.py
  db/models.py
  db/repositories.py
  schemas/reports.py
  services/report_service.py
  reports/generators.py
  storage/base.py
  storage/local.py
  workers/tasks.py
  main.py
tests/
  unit/
  integration/
alembic/
docker-compose.yml
pyproject.toml
```

**测试范围**

- 创建任务返回 `202`，重复幂等键只创建一个任务。
- 状态正确流转，进度不会倒退。
- Worker 异常后自动重试，达到上限后失败。
- 手动重试的状态和次数正确。
- Worker 重启后能够恢复超时任务。
- 多 Worker 并发时同一任务只执行一次。
- 成功结果可以流式下载，内容、文件名和校验和正确。
- 未完成、失败、过期、越权任务不能下载。
- SQL 注入、路径穿越、CSV 公式注入和超大参数被拦截。
- PostgreSQL、Redis、Worker、API 完整集成链路通过。

当前仓库只有空的 `.git`。运行环境是只读沙箱，首次创建任务书即被策略拒绝，因此本轮无法落盘实现、安装依赖、运行测试或提交版本。外部 Edge 调研也被环境策略拦截。