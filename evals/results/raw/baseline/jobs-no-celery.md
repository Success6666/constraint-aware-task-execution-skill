**Architecture**

Use:

- API service: accepts requests, validates inputs, creates jobs, reports status.
- PostgreSQL: durable job state, retry scheduling, idempotency, and worker coordination.
- Object storage such as S3: source images and generated results.
- Worker processes: claim jobs from PostgreSQL and execute image operations.
- Optional notification layer: PostgreSQL `LISTEN/NOTIFY` or short polling to reduce worker latency.

Celery is unnecessary. PostgreSQL provides a durable queue using row locking and leases.

**Job Lifecycle**

```text
QUEUED
  -> RUNNING
  -> SUCCEEDED
  -> FAILED_RETRYABLE
  -> QUEUED       retry
  -> FAILED       terminal
  -> CANCELED
```

A job should contain:

```text
id                  UUID
idempotency_key     string, unique per tenant
tenant_id           UUID
operation           string
input_object_key    string
parameters          JSONB
status              enum
progress            integer, 0-100
attempt             integer
max_attempts        integer
available_at        timestamp
lease_until         timestamp nullable
worker_id           string nullable
result_object_key   string nullable
error_code          string nullable
error_message       string nullable
created_at          timestamp
started_at          timestamp nullable
completed_at        timestamp nullable
```

Add an index on:

```text
(status, available_at)
```

and a unique constraint on:

```text
(tenant_id, idempotency_key)
```

**API**

`POST /v1/uploads`

Returns a presigned upload URL and an object identifier.

```json
{
  "object_id": "in_123",
  "upload_url": "https://storage.example/...",
  "expires_at": "2025-01-01T12:05:00Z"
}
```

`POST /v1/jobs`

```json
{
  "operation": "resize",
  "input_object_id": "in_123",
  "parameters": {
    "width": 1200,
    "height": 800,
    "fit": "cover",
    "format": "jpeg",
    "quality": 85
  }
}
```

Require `Idempotency-Key`. Return `202 Accepted`:

```json
{
  "job_id": "job_123",
  "status": "queued",
  "status_url": "/v1/jobs/job_123"
}
```

Repeated requests with the same tenant and idempotency key return the original job and do not enqueue duplicate work.

`GET /v1/jobs/{job_id}`

Queued response:

```json
{
  "job_id": "job_123",
  "status": "queued",
  "progress": 0,
  "attempt": 0
}
```

Running response:

```json
{
  "job_id": "job_123",
  "status": "running",
  "progress": 62,
  "attempt": 1
}
```

Successful response:

```json
{
  "job_id": "job_123",
  "status": "succeeded",
  "progress": 100,
  "result": {
    "object_id": "out_456",
    "download_url": "https://storage.example/...",
    "content_type": "image/jpeg",
    "size_bytes": 184233
  }
}
```

Failed response:

```json
{
  "job_id": "job_123",
  "status": "failed",
  "error": {
    "code": "UNSUPPORTED_FORMAT",
    "message": "The input image format is not supported."
  }
}
```

`POST /v1/jobs/{job_id}/cancel`

Allows cancellation while queued or running. Cancellation is cooperative; workers check a cancellation flag between processing stages.

`GET /v1/jobs/{job_id}/events`

Optionally provide Server-Sent Events for progress updates. Polling remains the mandatory fallback.

**Worker Claiming**

Workers repeatedly execute a transaction:

```sql
SELECT id
FROM jobs
WHERE (
  status = 'queued'
  AND available_at <= now()
)
OR (
  status = 'running'
  AND lease_until < now()
)
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

For a queued job:

1. Increment `attempt`.
2. Set `status = 'running'`.
3. Set `worker_id`.
4. Set `started_at`.
5. Set `lease_until = now() + interval '2 minutes'`.
6. Commit.

For a recovered expired job:

1. Treat the previous worker as lost.
2. Requeue if attempts remain.
3. Otherwise mark it failed.

Workers renew the lease periodically, for example every 30 seconds. The lease duration must exceed the renewal interval by a comfortable margin.

**Processing Contract**

Each operation is implemented as a deterministic handler:

```text
validate_input(job)
download_input(job.input_object_key)
decode_image()
process_stages()
write_result_to_temporary_object()
verify_result()
promote_temporary_object()
mark_success()
```

Use a temporary result key containing the job ID. Promote or rename it only after successful validation. This prevents partial output from being returned.

Handlers should report progress at meaningful stage boundaries, for example:

- Download: 0-10
- Decode: 10-20
- Transform: 20-80
- Encode: 80-95
- Upload and verify: 95-100

Progress updates must be monotonic and written with an atomic conditional update so stale workers cannot reduce progress.

**Retries**

Classify errors explicitly:

Retryable:

- Storage timeout
- Temporary network failure
- Worker process crash
- Database connection failure
- Temporary resource exhaustion

Non-retryable:

- Invalid parameters
- Unsupported image format
- Corrupt input
- Image exceeds configured limits
- Permission failure
- Deterministic processing error

Use exponential backoff with jitter:

```text
delay = min(15 minutes, 2^attempt * 5 seconds) + random_jitter
```

On retry:

```text
status = queued
available_at = now() + delay
lease_until = null
worker_id = null
error_code = last error
```

After `max_attempts`, mark the job `failed` and retain the final error. Publish a metric and optionally place the job in a dead-letter reporting table.

Retries must be safe because a worker may finish successfully immediately before crashing. Before creating output, check whether a valid result already exists for the job. Marking success should use a conditional update such as:

```text
UPDATE jobs
SET status = 'succeeded', progress = 100, result_object_key = ...
WHERE id = :id AND status = 'running' AND worker_id = :worker
```

**Result Retrieval**

Store results in private object storage. `GET /v1/jobs/{id}` returns a short-lived signed download URL only when the job succeeded.

Results should include:

```text
object key
content type
byte size
checksum
created timestamp
expiration timestamp
```

Use lifecycle policies to delete source and result objects after the retention period. Do not proxy large image downloads through the API unless required.

**Security and Limits**

- Authenticate every request and scope jobs by tenant.
- Validate MIME type using file signatures, not only extensions.
- Enforce maximum file size, pixel count, dimensions, and decompression ratio.
- Process images in isolated workers with CPU, memory, and execution-time limits.
- Prevent SSRF by accepting only uploaded object identifiers, not arbitrary URLs.
- Strip metadata by default, especially location metadata.
- Encrypt objects at rest and use TLS in transit.
- Apply per-tenant quotas and rate limits.
- Avoid returning internal exception details to clients.

**Consistency and Cleanup**

Create the job row and its dispatch signal in the same database transaction. Since the job itself is the durable queue, no separate queue message can be lost.

Use a periodic reconciler to:

- Requeue expired leases.
- Mark abandoned jobs failed after the retry limit.
- Delete temporary result objects.
- Detect jobs whose output exists but whose database update was interrupted.
- Remove expired uploads and results.

**Observability**

Record:

- Jobs submitted, succeeded, failed, retried, canceled.
- Queue depth and oldest queued job age.
- Processing duration by operation.
- Retry counts and failure codes.
- Worker lease expirations.
- Storage and memory failures.
- Per-tenant throughput and quota usage.

Include `job_id`, `tenant_id`, `attempt`, and `worker_id` in structured logs.

**Verification**

Test:

1. Job creation returns `202` and duplicate idempotency requests return the same job.
2. Workers claim jobs without double processing under concurrency.
3. Progress moves monotonically from 0 to 100.
4. Worker crashes cause lease recovery.
5. Retryable errors back off and eventually succeed or become terminal failures.
6. Non-retryable errors do not retry.
7. Duplicate worker completion cannot overwrite a newer state.
8. Cancellation works before execution and during processing.
9. Corrupt, oversized, and malicious images are rejected safely.
10. Result URLs are unavailable before success and expire correctly.
11. Temporary outputs are never exposed as final results.
12. Database, object storage, and worker restarts preserve correct job state.
13. Tenant authorization prevents cross-tenant job or result access.
14. Load tests confirm acceptable queue latency and bounded worker resource usage.
