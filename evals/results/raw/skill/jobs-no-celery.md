# Image Processing API Design

## 1. Architecture

- **API service:** FastAPI or equivalent HTTP service.
- **Database:** PostgreSQL for job state, progress, retry scheduling, and idempotency.
- **Object storage:** S3-compatible storage for inputs and outputs.
- **Workers:** Separate worker processes polling PostgreSQL for available jobs.
- **No in-process background execution:** API instances only enqueue jobs and return immediately.

PostgreSQL acts as the durable job queue using row locking and `SKIP LOCKED`, avoiding an additional broker.

## 2. API

### Upload input

```http
POST /v1/uploads
Authorization: Bearer <token>
```

Response:

```json
{
  "upload_id": "upl_123",
  "object_key": "inputs/upl_123/source.jpg",
  "upload_url": "https://storage.example/..."
}
```

The client uploads the image directly to the returned URL.

### Create job

```http
POST /v1/jobs
Authorization: Bearer <token>
Idempotency-Key: 7f7d...
Content-Type: application/json
```

```json
{
  "input_object_key": "inputs/upl_123/source.jpg",
  "operation": "resize",
  "options": {
    "width": 1200,
    "height": 800,
    "fit": "cover",
    "format": "webp",
    "quality": 85
  }
}
```

Response:

```http
202 Accepted
```

```json
{
  "job_id": "job_123",
  "status": "queued",
  "progress": {
    "percent": 0,
    "stage": "queued"
  },
  "status_url": "/v1/jobs/job_123",
  "result_url": null
}
```

Repeated requests with the same `Idempotency-Key` return the original job.

### Get status

```http
GET /v1/jobs/{job_id}
Authorization: Bearer <token>
```

Example:

```json
{
  "job_id": "job_123",
  "status": "processing",
  "progress": {
    "percent": 62,
    "stage": "encoding"
  },
  "attempt": 1,
  "max_attempts": 3,
  "created_at": "2025-01-01T12:00:00Z",
  "updated_at": "2025-01-01T12:00:14Z",
  "error": null,
  "result_url": null
}
```

Terminal success:

```json
{
  "job_id": "job_123",
  "status": "succeeded",
  "progress": {
    "percent": 100,
    "stage": "completed"
  },
  "result_url": "/v1/jobs/job_123/result"
}
```

### Retrieve result

```http
GET /v1/jobs/{job_id}/result
Authorization: Bearer <token>
```

The API verifies ownership and returns a short-lived signed object-storage URL:

```json
{
  "download_url": "https://storage.example/...",
  "expires_in": 900,
  "content_type": "image/webp"
}
```

## 3. Job states

```text
queued
  -> processing
  -> succeeded

processing
  -> retry_wait
  -> failed

retry_wait
  -> processing
  -> failed
```

Optional terminal cancellation can be added later, but workers must at least reject jobs whose input or authorization is invalid.

## 4. Database schema

### `jobs`

```sql
CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    input_object_key TEXT NOT NULL,
    output_object_key TEXT,
    operation TEXT NOT NULL,
    options JSONB NOT NULL DEFAULT '{}',

    status TEXT NOT NULL,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    progress_stage TEXT NOT NULL DEFAULT 'queued',

    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    lease_token UUID,
    lease_until TIMESTAMPTZ,

    error_code TEXT,
    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    UNIQUE (owner_id, idempotency_key)
);

CREATE INDEX jobs_claim_idx
ON jobs (available_at, created_at)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX jobs_owner_idx
ON jobs (owner_id, created_at DESC);
```

## 5. Worker behavior

Workers repeatedly claim jobs in a transaction:

1. Select one available `queued` or `retry_wait` job using `FOR UPDATE SKIP LOCKED`.
2. Also reclaim `processing` jobs whose lease expired.
3. Increment `attempt`.
4. Set:
   - `status = 'processing'`
   - `lease_token = random UUID`
   - `lease_until = now() + 2 minutes`
   - `progress_stage = 'starting'`
5. Commit before doing image work.

Only the worker possessing the lease token may update or complete that job.

Workers renew the lease periodically while processing. Progress updates use the lease token and enforce monotonic progress:

```sql
progress_percent = GREATEST(progress_percent, :percent)
```

Recommended stages:

```text
queued
downloading
validating
decoding
processing
encoding
uploading
completed
```

## 6. Retry handling

Retry only transient failures, such as:

- Temporary object-storage errors
- Network timeouts
- Worker process termination
- Temporary database failures

Do not retry:

- Invalid image data
- Unsupported operation
- Invalid options
- Excessive dimensions or decompression-bomb limits
- Authorization or missing-input errors

For a retryable failure:

```text
delay = min(300 seconds, 2^(attempt - 1) * 10 seconds) + random jitter
```

Set:

- `status = retry_wait`
- `available_at = now() + delay`
- `error_code` and `error_message`

After `max_attempts`, set:

```text
status = failed
progress_stage = failed
completed_at = now()
```

Return a stable error response:

```json
{
  "status": "failed",
  "error": {
    "code": "PROCESSING_FAILED",
    "message": "The image could not be processed."
  }
}
```

Do not expose internal stack traces.

## 7. Result handling

Workers should:

1. Download the input to temporary storage.
2. Validate MIME type, file size, dimensions, and decoded pixel count.
3. Process the image.
4. Write the output to a deterministic key:

```text
outputs/{owner_id}/{job_id}/result.{format}
```

5. Upload using a temporary object or unique key.
6. Atomically update the job to `succeeded` with the output key.

If a worker crashes after upload but before database completion, the retry reuses the same deterministic output key and safely overwrites or verifies it.

Outputs should have lifecycle expiration policies appropriate to the product.

## 8. Security and limits

- Authenticate every endpoint.
- Restrict jobs and results to their owning account.
- Allow only configured image formats and operations.
- Enforce input size, dimensions, pixel count, processing time, and output size limits.
- Do not fetch arbitrary user-supplied URLs; accept only uploaded object keys.
- Store objects privately.
- Use short-lived signed URLs for downloads.
- Run image decoding in isolated worker processes with resource limits.

## 9. Verification

Implement tests for:

- Job creation and idempotency.
- Status transitions and progress updates.
- Concurrent workers claiming distinct jobs.
- Lease expiration and worker recovery.
- Retry backoff and maximum-attempt behavior.
- Permanent versus transient error classification.
- Successful result upload and retrieval.
- Missing, malformed, oversized, and unsupported images.
- Authorization between different users.
- Duplicate completion attempts.
- API contract responses and HTTP status codes.

Run an integration test with multiple workers, terminate one during processing, and verify that the job is reclaimed and either succeeds or reaches the configured failure state.
