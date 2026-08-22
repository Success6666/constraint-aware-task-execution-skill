Design the API as a durable job system with PostgreSQL for state, Redis Streams for dispatch, object storage for images, and stateless API/worker processes.

## Components

- **API service**: accepts requests, validates input, exposes status and result endpoints.
- **PostgreSQL**: source of truth for jobs, attempts, progress, and errors.
- **Redis Streams**: transports pending work to workers.
- **Object storage**: stores original and processed images; return signed URLs.
- **Workers**: execute image operations in isolated temporary directories.

Use an outbox table so creating a job and publishing work cannot get out of sync.

## Job lifecycle

```text
queued -> running -> succeeded
   |         |
   |         +-> retry_wait -> queued
   |
   +-> cancelled

running -> failed
```

A job is terminal when it reaches `succeeded`, `failed`, or `cancelled`.

## API

### Create a job

```http
POST /v1/image-jobs
Idempotency-Key: 5c1f...
Content-Type: application/json
```

```json
{
  "input": {
    "object_key": "uploads/user-42/original.png"
  },
  "operations": [
    { "type": "resize", "width": 1200, "height": 800, "fit": "cover" },
    { "type": "convert", "format": "webp", "quality": 82 }
  ],
  "priority": "normal",
  "callback_url": "https://example.com/hooks/image-jobs"
}
```

Response:

```http
202 Accepted
Location: /v1/image-jobs/01J...
```

```json
{
  "id": "01J...",
  "status": "queued",
  "progress": 0,
  "created_at": "2026-08-22T10:00:00Z"
}
```

The upload can be handled separately with a presigned URL:

```http
POST /v1/uploads
```

This avoids sending large image bodies through the API service.

### Get status

```http
GET /v1/image-jobs/{job_id}
```

```json
{
  "id": "01J...",
  "status": "running",
  "progress": 64,
  "stage": "encoding",
  "attempt": 1,
  "max_attempts": 4,
  "created_at": "2026-08-22T10:00:00Z",
  "started_at": "2026-08-22T10:00:04Z",
  "updated_at": "2026-08-22T10:00:19Z"
}
```

For failures:

```json
{
  "status": "failed",
  "error": {
    "code": "UNSUPPORTED_FORMAT",
    "message": "Input format is not supported",
    "retryable": false
  }
}
```

### Progress events

```http
GET /v1/image-jobs/{job_id}/events
Accept: text/event-stream
```

```text
event: progress
data: {"status":"running","progress":40,"stage":"decoding"}

event: progress
data: {"status":"running","progress":64,"stage":"encoding"}

event: completed
data: {"status":"succeeded","result_url":"/v1/image-jobs/01J.../result"}
```

Persist the latest progress in PostgreSQL; Redis can be used for low-latency fanout.

### Retrieve the result

```http
GET /v1/image-jobs/{job_id}/result
```

Response:

```json
{
  "format": "webp",
  "size_bytes": 184220,
  "width": 1200,
  "height": 800,
  "download_url": "https://object-store/...signed...",
  "expires_at": "2026-08-22T11:00:00Z"
}
```

### Cancel a job

```http
POST /v1/image-jobs/{job_id}/cancel
```

Cancellation is cooperative. Workers check a cancellation flag between operations and before uploading the result.

## Database schema

```sql
image_jobs (
  id              uuid primary key,
  tenant_id       uuid not null,
  status          text not null,
  input_key       text not null,
  operations      jsonb not null,
  result_key      text,
  progress        integer not null default 0,
  stage           text,
  attempt         integer not null default 0,
  max_attempts    integer not null default 4,
  next_run_at     timestamptz,
  lease_until     timestamptz,
  idempotency_key text,
  error_code      text,
  error_message   text,
  created_at      timestamptz not null,
  started_at      timestamptz,
  finished_at     timestamptz,
  updated_at      timestamptz not null
);

job_outbox (
  id          bigserial primary key,
  job_id      uuid not null,
  event_type  text not null,
  payload     jsonb not null,
  published_at timestamptz
);

job_attempts (
  id          bigserial primary key,
  job_id      uuid not null,
  attempt     integer not null,
  started_at  timestamptz,
  finished_at timestamptz,
  error_code  text,
  error_message text
);
```

Add a unique constraint on `(tenant_id, idempotency_key)`.

## Worker processing

1. Read a message from Redis Stream `image-jobs`.
2. Atomically claim the job if it is `queued` and `next_run_at <= now()`.
3. Set `status = running`, increment `attempt`, and create a lease.
4. Download the source image to a temporary directory.
5. Execute operations, updating progress after each stage.
6. Upload the result to a new object key.
7. In one database transaction:
   - set `status = succeeded`
   - store `result_key` and metadata
   - set `progress = 100`
   - write a completion event
8. Acknowledge the Redis message.

Workers heartbeat the lease periodically. If a worker crashes, a reaper finds expired leases and requeues the job.

## Retry policy

Classify errors explicitly:

- **Retryable**: network timeout, temporary object-store failure, worker crash, resource exhaustion.
- **Non-retryable**: invalid parameters, corrupt image, unsupported format, policy violations.
- **Unknown**: retry once, then mark failed for inspection.

Use exponential backoff with jitter:

```text
delay = min(5 minutes, 2^(attempt-1) * 5 seconds) + random_jitter
```

When retrying:

```text
status = retry_wait
next_run_at = now() + delay
```

Do not retry after `attempt >= max_attempts`.

Make result writes idempotent by using a deterministic key such as:

```text
results/{job_id}/attempt-{attempt}.webp
```

Only the successful attempt is exposed through `result_key`.

## Progress model

Represent progress as weighted stages:

```text
download 10%
decode    15%
transform 50%
encode    20%
upload     5%
```

Store both:

- `progress`: integer from 0 to 100
- `stage`: human-readable machine value such as `transform` or `upload`

Progress updates should be monotonic and throttled, for example no more than once every 250 ms or 1 percentage point.

## Operational safeguards

- Enforce per-tenant concurrency and image-size limits.
- Validate dimensions, MIME type, and decompression limits before processing.
- Run workers with CPU, memory, and execution-time limits.
- Keep API instances stateless so they can scale independently.
- Expose metrics for queue depth, job latency, retry count, failure rate, and processing duration.
- Sign result URLs with short expiration times.
- Authenticate every endpoint and scope jobs by tenant.

This provides durable background processing, observable progress, crash recovery, bounded retries, and secure result retrieval without relying on the excluded task framework.