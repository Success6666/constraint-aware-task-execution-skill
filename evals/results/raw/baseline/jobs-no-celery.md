Use a durable database for job state, object storage for images, and Redis Streams for dispatch. API servers never process images directly, and workers claim jobs through a Redis consumer group. No Celery is involved.

```text
Client
  | upload / status / result
  v
FastAPI service ---- PostgreSQL
  |                     |
  | enqueue job         | authoritative job state
  v                     |
Redis Streams ------ Image workers
                          |
                          v
                    S3-compatible storage
```

### API

#### Create a job

```http
POST /v1/image-jobs
Idempotency-Key: 4ef8...
Content-Type: multipart/form-data

image=@photo.jpg
operation=resize
parameters={"width":1200,"height":800,"fit":"cover"}
```

```json
{
  "id": "job_01K...",
  "status": "queued",
  "progress": 0,
  "attempt": 0,
  "created_at": "2026-08-16T10:00:00Z",
  "status_url": "/v1/image-jobs/job_01K..."
}
```

For large files, add a presigned-upload flow:

```http
POST /v1/uploads
POST /v1/image-jobs
```

The second request references the uploaded object key instead of carrying the image.

#### Get status

```http
GET /v1/image-jobs/{job_id}
```

```json
{
  "id": "job_01K...",
  "status": "processing",
  "progress": 64,
  "stage": "encoding",
  "attempt": 2,
  "max_attempts": 4,
  "created_at": "2026-08-16T10:00:00Z",
  "started_at": "2026-08-16T10:00:02Z",
  "updated_at": "2026-08-16T10:00:08Z"
}
```

#### Retrieve a result

```http
GET /v1/image-jobs/{job_id}/result
```

When complete:

```http
303 See Other
Location: https://objects.example.com/results/...?signature=...
```

Before completion, return `409 Conflict` with the current job status. Expired results return `410 Gone`.

#### Retry a permanently failed job

```http
POST /v1/image-jobs/{job_id}/retry
```

This creates a new job linked through `retried_from_job_id`. Keeping retries as separate records preserves audit history.

#### Cancel a job

```http
DELETE /v1/image-jobs/{job_id}
```

Queued jobs become `cancelled`. Processing jobs receive a cancellation request that workers check between stages.

### Job state model

```text
queued -> processing -> succeeded
   |          |
   |          +-> retry_wait -> queued
   |          +-> failed
   |          +-> cancelled
   +-> cancelled
```

Recommended statuses:

- `queued`
- `processing`
- `retry_wait`
- `succeeded`
- `failed`
- `cancelled`

PostgreSQL is authoritative. Redis only signals that work is available, so losing or rebuilding Redis does not lose job records.

### Job table

```sql
CREATE TABLE image_jobs (
    id                  uuid PRIMARY KEY,
    owner_id            uuid NOT NULL,
    status              text NOT NULL,
    operation           text NOT NULL,
    parameters          jsonb NOT NULL,
    input_object_key    text NOT NULL,
    output_object_key   text,
    progress            smallint NOT NULL DEFAULT 0,
    stage               text,
    attempt             integer NOT NULL DEFAULT 0,
    max_attempts        integer NOT NULL DEFAULT 4,
    next_attempt_at     timestamptz,
    lease_expires_at    timestamptz,
    worker_id           text,
    error_code          text,
    error_message       text,
    cancel_requested    boolean NOT NULL DEFAULT false,
    idempotency_key     text,
    retried_from_job_id uuid REFERENCES image_jobs(id),
    created_at          timestamptz NOT NULL,
    started_at          timestamptz,
    finished_at         timestamptz,
    updated_at          timestamptz NOT NULL,
    UNIQUE (owner_id, idempotency_key)
);
```

### Worker behavior

1. Read a job ID from the Redis Stream consumer group.
2. Atomically claim the corresponding row only if it is eligible.
3. Set a processing lease and increment `attempt`.
4. Download the source image.
5. Validate format, dimensions, decompression size, and operation limits.
6. Process into a temporary file.
7. Upload the result under a deterministic key such as `results/{job_id}`.
8. Mark the database row `succeeded`.
9. Acknowledge the Redis message.

Progress updates should be rate-limited, for example only when progress advances by 5% or after one second. This prevents frequent database writes.

### Retry handling

Classify failures explicitly:

- Retryable: storage timeout, transient database failure, worker interruption, temporary resource exhaustion.
- Permanent: corrupt image, unsupported format, invalid parameters, image exceeds configured limits.

Use exponential backoff with jitter:

```text
delay = min(300 seconds, 2^attempt * 5 seconds) + random jitter
```

A scheduler process periodically selects due `retry_wait` rows using `FOR UPDATE SKIP LOCKED` and republishes them to Redis Streams. After `max_attempts`, the job becomes `failed`.

Workers renew their leases during long operations. A recovery process finds expired processing leases and moves those jobs to `retry_wait`. Processing and upload must be idempotent because a worker can finish just as its lease expires.

### Reliability details

- Use an outbox table in the same transaction as job creation. A dispatcher publishes outbox entries to Redis and then marks them delivered.
- Treat queue delivery as at least once; never assume exactly once.
- Make output keys deterministic and conditionally update job state to prevent stale workers overwriting newer attempts.
- Store only object keys in PostgreSQL, not signed URLs.
- Restrict status and result access by `owner_id`.
- Validate image headers before full decoding and enforce pixel, file-size, memory, runtime, and output-size limits.
- Periodically delete expired input/output objects according to retention policy.
- Expose queue depth, processing duration, retry count, failure rate, lease recovery count, and jobs stuck by stage as metrics.

For live progress, keep polling as the baseline and optionally provide `GET /v1/image-jobs/{id}/events` using Server-Sent Events. The same persisted job state should power both mechanisms.