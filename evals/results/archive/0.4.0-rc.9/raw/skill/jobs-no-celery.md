Use FastAPI for HTTP, PostgreSQL as the source of truth, Redis + RQ for background execution, and S3-compatible object storage for inputs and processed results.

```text
Client
  |
  v
FastAPI ───── PostgreSQL
  |           job metadata/status
  |
  +──── Redis/RQ ─── Image workers
                         |
                         v
                   Object storage
```

## API

### Create a job

`POST /v1/image-jobs`

Accept either multipart upload or an object-storage key. For large images, prefer direct-to-storage uploads using presigned URLs.

```json
{
  "input_key": "uploads/8a/input.jpg",
  "operations": [
    {"type": "resize", "width": 1200, "height": 800, "fit": "cover"},
    {"type": "convert", "format": "webp", "quality": 85}
  ]
}
```

Response:

```http
HTTP/1.1 202 Accepted
Location: /v1/image-jobs/01J...

{
  "id": "01J...",
  "status": "queued",
  "progress": 0,
  "created_at": "2026-08-16T10:30:00Z"
}
```

Support an `Idempotency-Key` header so client retries do not create duplicate jobs.

### Get status

`GET /v1/image-jobs/{job_id}`

```json
{
  "id": "01J...",
  "status": "processing",
  "progress": 60,
  "current_step": "converting",
  "attempt": 2,
  "max_attempts": 4,
  "created_at": "2026-08-16T10:30:00Z",
  "started_at": "2026-08-16T10:30:03Z",
  "updated_at": "2026-08-16T10:30:08Z"
}
```

### Retrieve result

`GET /v1/image-jobs/{job_id}/result`

- `200`: return metadata and a short-lived signed download URL.
- `202`: job is not finished.
- `409`: job failed or was cancelled.
- `404`: unknown job or inaccessible to this tenant.

```json
{
  "job_id": "01J...",
  "content_type": "image/webp",
  "size_bytes": 248391,
  "width": 1200,
  "height": 800,
  "checksum": "sha256:...",
  "download_url": "https://storage.example/...",
  "expires_at": "2026-08-16T10:45:00Z"
}
```

### Other operations

```text
POST   /v1/image-jobs/{id}/retry
DELETE /v1/image-jobs/{id}
GET    /v1/image-jobs?status=failed&cursor=...
```

Manual retry should only accept terminal failed jobs and create a new execution attempt without changing the original input or operation specification.

## Job model

```sql
image_jobs (
  id                 uuid primary key,
  tenant_id          uuid not null,
  idempotency_key    text,
  status             text not null,
  progress           smallint not null default 0,
  current_step       text,
  input_key          text not null,
  operations         jsonb not null,
  result_key         text,
  result_metadata    jsonb,
  attempt            integer not null default 0,
  max_attempts       integer not null default 4,
  next_attempt_at    timestamptz,
  error_code         text,
  error_message      text,
  created_at         timestamptz not null,
  started_at         timestamptz,
  completed_at       timestamptz,
  updated_at         timestamptz not null,
  version            integer not null default 0,

  unique (tenant_id, idempotency_key)
);
```

Status transitions:

```text
queued -> processing -> succeeded
                    -> retry_scheduled -> queued
                    -> failed
                    -> cancelled
```

PostgreSQL remains authoritative. Queue records only indicate that work is available.

## Worker behavior

1. Load the job and atomically claim it using a status/version condition.
2. Increment `attempt` and set `status = processing`.
3. Download the input to isolated temporary storage.
4. Validate decoded image dimensions and format, not only file extension or MIME type.
5. Execute each operation, updating progress after meaningful stages.
6. Write output under a deterministic key such as `results/{job_id}/{spec_hash}`.
7. Calculate metadata and checksum.
8. Atomically set `status = succeeded`, `progress = 100`, and save the result key.
9. Remove temporary files in a `finally` block.

Progress writes should be throttled, for example to one update per second or when progress advances by at least 5%, to avoid excessive database traffic.

## Retry handling

Classify failures:

- Retryable: storage timeouts, transient database errors, worker termination, temporary resource exhaustion.
- Permanent: unsupported format, corrupt input, invalid operation, image exceeding configured limits.

Use exponential backoff with jitter:

```text
delay = min(5 minutes, 5 seconds * 2^(attempt - 1)) + random jitter
```

A scheduler process moves due `retry_scheduled` jobs back onto the Redis queue. After `max_attempts`, mark the job `failed` and expose a stable error code such as `INPUT_CORRUPT` or `STORAGE_UNAVAILABLE`.

Processing must be idempotent. A worker receiving the same job twice should either reuse the deterministic output or overwrite it atomically before completing the database transition.

Use a lease timestamp or heartbeat for `processing` jobs. A recovery process can requeue jobs whose worker lease expired, covering worker crashes after dequeue.

## Operational safeguards

- Enforce limits on upload size, decoded pixel count, operation count, execution time, and output size.
- Keep original error details in internal logs; return sanitized error messages through the API.
- Scope every query and storage key by tenant.
- Scan or decode uploads in isolated worker processes with CPU and memory limits.
- Apply per-tenant API and queue quotas.
- Set lifecycle rules to delete abandoned uploads and expired results.
- Record structured logs and metrics for queue latency, execution time, retries, failures, and stuck jobs.
- Optionally provide signed webhooks for terminal state changes while retaining status polling as the reliable baseline.