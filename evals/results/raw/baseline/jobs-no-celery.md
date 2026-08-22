Design a job-based image-processing API with an HTTP service that persists job state and publishes work to a durable queue. Workers consume queued jobs, process images, update progress, and store results in object storage.

## Architecture

Components:

- **API service**: validates requests, creates jobs, exposes status and result endpoints.
- **Database**: stores job metadata, state transitions, progress, attempts, and errors.
- **Durable queue**: Redis Streams, RabbitMQ, Amazon SQS, or a database-backed queue.
- **Worker processes**: independent processes using a standard client library; no Celery.
- **Object storage**: S3-compatible storage for inputs and outputs.
- **Optional event channel**: WebSocket or Server-Sent Events for live progress.

A typical flow:

```text
Client -> API -> Database
              -> Queue -> Worker -> Object Storage
                         |       |
                         +-> Database status updates
```

## Job model

```text
jobs
----
id                  UUID
idempotency_key     string, nullable
operation           string
input_object_key    string
output_object_key   string, nullable
status              enum
progress            integer 0..100
attempt             integer
max_attempts        integer
error_code          string, nullable
error_message       text, nullable
created_at          timestamp
started_at          timestamp, nullable
finished_at         timestamp, nullable
lease_until         timestamp, nullable
```

Statuses:

```text
queued
processing
succeeded
retrying
failed
cancelled
```

Only valid transitions should be accepted:

```text
queued -> processing
processing -> succeeded
processing -> retrying
processing -> failed
queued|processing -> cancelled
retrying -> processing
```

## API

### Submit a job

```http
POST /v1/image-jobs
Idempotency-Key: 9c4...
Content-Type: application/json
```

```json
{
  "operation": "resize",
  "input": {
    "object_key": "uploads/photo-123.jpg"
  },
  "parameters": {
    "width": 1200,
    "height": 800,
    "fit": "cover",
    "format": "webp",
    "quality": 85
  },
  "max_attempts": 3
}
```

Response:

```http
202 Accepted
Location: /v1/image-jobs/7d2...
```

```json
{
  "id": "7d2...",
  "status": "queued",
  "progress": 0,
  "created_at": "2026-08-22T10:00:00Z"
}
```

The idempotency key should return the original job if the same request is repeated.

### Get status

```http
GET /v1/image-jobs/{job_id}
```

```json
{
  "id": "7d2...",
  "operation": "resize",
  "status": "processing",
  "progress": 64,
  "attempt": 1,
  "max_attempts": 3,
  "created_at": "2026-08-22T10:00:00Z",
  "started_at": "2026-08-22T10:00:05Z",
  "error": null
}
```

For a completed job:

```json
{
  "id": "7d2...",
  "status": "succeeded",
  "progress": 100,
  "result": {
    "object_key": "results/7d2.webp",
    "content_type": "image/webp",
    "size_bytes": 183421,
    "download_url": "https://storage.example/..."
  }
}
```

Generate a short-lived signed download URL rather than exposing storage credentials.

### Retrieve the result

```http
GET /v1/image-jobs/{job_id}/result
```

Behavior:

- `302 Found` to a signed object-storage URL, or
- stream the image through the API.
- `409 Conflict` if the job is not complete.
- `404 Not Found` if no result exists.

### Cancel a job

```http
POST /v1/image-jobs/{job_id}/cancel
```

Cancellation is cooperative. A worker checks a cancellation flag between processing stages and terminates safely.

### Progress stream

```http
GET /v1/image-jobs/{job_id}/events
Accept: text/event-stream
```

Example:

```text
event: progress
data: {"status":"processing","progress":70}

event: completed
data: {"status":"succeeded","result":{"object_key":"results/7d2.webp"}}
```

Polling remains the fallback for clients that cannot use SSE.

## Queue and worker behavior

Use a queue with explicit acknowledgement and visibility timeouts.

Worker loop:

1. Claim a message with a lease.
2. Atomically change `queued` or `retrying` to `processing`.
3. Refresh `lease_until` periodically.
4. Process the image in stages, updating progress.
5. Write the output using a temporary object key.
6. Atomically mark the job `succeeded`.
7. Acknowledge the message.

Use deterministic output keys such as `results/{job_id}.{format}`. This makes retries idempotent. If an output already exists and its checksum matches, the worker can skip processing and finalize the job.

Example progress stages:

```text
0   accepted
10  input downloaded
25  decoded
40  transformation started
80  encoded
95  output uploaded
100 succeeded
```

Progress should be monotonic and throttled, for example no more than one database update per second or per 5 percentage points.

## Retry handling

Classify errors:

- **Transient**: network timeout, temporary storage failure, worker crash.
- **Permanent**: invalid image, unsupported format, invalid parameters, authorization failure.
- **Resource**: memory or CPU limit exceeded.

Retry only transient and explicitly retryable resource errors.

Use exponential backoff with jitter:

```text
delay = min(60s, 2^(attempt-1) * 5s) + random(0..2s)
```

After `max_attempts`, mark the job `failed` and retain a structured error:

```json
{
  "code": "INPUT_DOWNLOAD_TIMEOUT",
  "message": "Input could not be downloaded",
  "retryable": true,
  "attempt": 3
}
```

A separate reaper periodically finds jobs stuck in `processing` whose lease expired, then requeues them or marks them failed according to the attempt count.

## Reliability and security

- Authenticate every request and authorize access to the job owner.
- Validate image size, dimensions, MIME type, and decompression limits before processing.
- Run workers in isolated containers with CPU, memory, and execution-time limits.
- Never trust file extensions; inspect actual content.
- Store original and result objects with private ACLs.
- Add request rate limits and per-user concurrency quotas.
- Emit structured logs with `job_id`, `attempt`, and `worker_id`.
- Track queue depth, processing latency, retry count, failure rate, and stuck leases.
- Keep job records for a retention period, then delete metadata and objects.

This design can be implemented with FastAPI/Node/Go for the API, PostgreSQL for job state, Redis Streams/RabbitMQ/SQS for delivery, and S3-compatible storage, while keeping workers as ordinary processes managed by Docker, Kubernetes, systemd, or a process supervisor.