建议采用 **FastAPI + PostgreSQL 持久化队列 + 独立 Worker + S3/MinIO 对象存储**。任务通过 PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 抢占，减少基础设施依赖，同时支持多 Worker 横向扩展。

**整体流程**

```text
客户端上传图片
    ↓
API 保存原图到对象存储，创建 jobs 记录
    ↓
Worker 抢占任务并生成租约
    ↓
处理图片，周期性更新进度和心跳
    ↓
结果写入对象存储，更新任务状态
    ↓
客户端查询状态并获取临时下载地址
```

**核心接口**

```http
POST /v1/image-jobs
Content-Type: multipart/form-data
Idempotency-Key: <uuid>
```

参数示例：

```json
{
  "operation": "resize",
  "parameters": {
    "width": 1280,
    "height": 720,
    "fit": "cover",
    "format": "webp",
    "quality": 85
  }
}
```

返回 `202 Accepted`：

```json
{
  "id": "job_01K...",
  "status": "queued",
  "progress": 0,
  "created_at": "2026-08-16T10:00:00Z",
  "links": {
    "status": "/v1/image-jobs/job_01K...",
    "result": "/v1/image-jobs/job_01K.../result"
  }
}
```

其他接口：

```http
GET    /v1/image-jobs/{job_id}
GET    /v1/image-jobs/{job_id}/result
POST   /v1/image-jobs/{job_id}/retry
DELETE /v1/image-jobs/{job_id}
GET    /v1/image-jobs?status=failed&cursor=...
```

状态查询返回：

```json
{
  "id": "job_01K...",
  "status": "running",
  "progress": 62,
  "stage": "encoding",
  "attempt": 2,
  "max_attempts": 4,
  "created_at": "2026-08-16T10:00:00Z",
  "started_at": "2026-08-16T10:00:03Z",
  "updated_at": "2026-08-16T10:00:09Z"
}
```

成功后，结果接口返回文件信息及短期签名下载地址。任务尚未完成时返回 `409 JOB_NOT_COMPLETED`，结果已过期时返回 `410 RESULT_EXPIRED`。

**状态机**

```text
queued → running → succeeded
            ├──→ retrying → queued
            ├──→ failed
            └──→ canceled
```

终态为 `succeeded`、`failed`、`canceled`。数据库层使用状态条件更新，防止重复执行和非法状态跳转。

**数据模型**

`image_jobs` 主要字段：

```text
id, owner_id, idempotency_key
status, progress, stage
operation, parameters_json
source_object_key, result_object_key
attempt_count, max_attempts, next_attempt_at
lease_owner, lease_expires_at, heartbeat_at
error_code, error_message
created_at, started_at, completed_at, expires_at
```

另建 `job_attempts` 保存每次执行的开始时间、耗时、Worker、异常类别和退出结果，便于审计及排障。

Worker 抢占任务：

```sql
SELECT id
FROM image_jobs
WHERE status IN ('queued', 'retrying')
  AND next_attempt_at <= now()
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

抢占后设置 `lease_owner` 和 `lease_expires_at`。后台回收器将心跳超时的任务重新排队，避免 Worker 崩溃导致任务永久卡住。

**重试策略**

- 网络抖动、对象存储超时、临时资源不足：自动重试。
- 文件损坏、不支持的格式、参数非法、图片尺寸超限：直接失败。
- 指数退避并增加随机抖动，例如 `5s、20s、80s`。
- 达到最大次数后进入 `failed`。
- 手动重试创建新的 attempt，但保留原始任务和错误历史。
- 处理结果采用确定性对象键，数据库完成状态更新前可安全重复执行。

**进度处理**

进度按阶段分配权重，例如：

```text
读取与校验  0-10
解码       10-30
图像处理   30-75
编码       75-95
上传结果   95-100
```

Worker 每隔至少 500ms 或进度变化超过 2% 才写数据库，避免高频更新。需要实时体验时，可增加 SSE 接口；数据库仍是最终状态来源。

**安全边界**

- 限制上传大小、像素总量、帧数和处理时长。
- 根据实际解码结果识别格式，不信任扩展名和客户端 MIME。
- 防范解压炸弹、畸形图片及超大 EXIF 元数据。
- Worker 使用低权限账户，并设置 CPU、内存和并发限制。
- 对象存储保持私有，通过短期签名地址下载。
- 参数使用严格白名单模型，错误信息不返回内部路径和堆栈。
- 所有任务查询必须校验资源所有权。
- 使用 `Idempotency-Key` 避免客户端超时重发造成重复任务。

当前工作区为空且为只读环境，因此未创建工程文件或提交代码。