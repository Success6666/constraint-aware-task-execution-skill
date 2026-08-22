**Design**

Build a Python CLI using only the standard library. The crawler should separate source-specific metadata extraction from scheduling, persistence, caching, and output.

**CLI**

```text
metadata-crawler crawl
  --input INPUT
  --output OUTPUT
  --cache CACHE_DIR
  --state STATE_PATH
  --workers 8
  --rate 5
  --burst 1
  --timeout 30
  --retries 3
  --resume
  --force
  --format jsonl|json|csv

metadata-crawler inspect-state --state STATE_PATH
metadata-crawler clear-cache --cache CACHE_DIR
```

Input may be a newline-delimited list of URLs or source identifiers. Define a source adapter interface so additional metadata providers can be added without changing crawler mechanics:

```python
class SourceAdapter(Protocol):
    def canonicalize(self, item: str) -> str: ...
    def fetch_metadata(self, item: str, context: RequestContext) -> Metadata: ...
```

Use `urllib.request` for HTTP, `urllib.parse` for URL handling, `json`, `sqlite3`, `hashlib`, `time`, `threading`, and `concurrent.futures`.

## Processing Pipeline

1. Read and canonicalize input items.
2. Deduplicate items by canonical identifier.
3. Check durable run state.
4. Check the cache.
5. Schedule uncached items through a bounded worker pool.
6. Apply per-host rate limiting before every request.
7. Retry transient failures with exponential backoff.
8. Persist each result immediately.
9. Emit final output from durable result storage.

Use `ThreadPoolExecutor` for I/O-bound crawling. The number of submitted tasks must be bounded rather than submitting an unbounded input stream at once.

A simple implementation can maintain at most `workers * 2` futures:

```text
while input remains or futures remain:
    fill the future set up to workers * 2
    wait for at least one future to finish
    persist each completed result
    refill the future set
```

The worker function must not own global output ordering. Results are persisted as they complete, with the original input sequence retained as metadata.

## Bounded Concurrency

Expose a global `--workers` limit. For stronger protection, also enforce per-host concurrency:

```text
global active requests <= workers
active requests for one host <= host_limit
```

Use semaphores:

- One global semaphore sized to `workers`.
- One lazily-created host semaphore, commonly sized to `1`.
- Acquire the global semaphore, then host semaphore, around the network operation.
- Always release both in `finally` blocks.

Do not create one thread per item. Do not use an unbounded queue.

## Rate Limiting

Use a monotonic-clock token bucket per host.

Configuration:

```text
rate: tokens per second
capacity: burst size
```

Before a request:

1. Lock the host bucket.
2. Refill tokens using `time.monotonic()`.
3. If at least one token exists, consume it.
4. Otherwise calculate the required sleep duration.
5. Release the lock, sleep, and retry acquisition.

Do not sleep while holding the bucket lock.

Maintain separate buckets by `(scheme, hostname, effective_port)`. Honor server-provided `Retry-After` values after HTTP 429 or 503 responses, but cap the delay with a configurable maximum.

Rate limiting should apply to retries as well as initial requests.

## HTTP Behavior

Use an explicit opener with:

- A descriptive `User-Agent`.
- Configurable timeout.
- Redirect handling through `urllib`.
- Response-body size limits.
- Allowed content types where applicable.
- UTF-8 decoding with replacement for malformed content.

Classify responses:

- `2xx`: success.
- `3xx`: normally handled by the opener; otherwise permanent failure.
- `400`, `401`, `403`, `404`: permanent failure unless the adapter explicitly overrides this.
- `408`, `425`, `429`, `500`, `502`, `503`, `504`: retryable.
- DNS, connection reset, and timeout errors: retryable.
- Parsing and validation errors: permanent failure.

Retries should use:

```text
delay = min(max_delay, base_delay * 2**attempt + random_jitter)
```

Use a local random generator to avoid synchronized retries across workers.

## Metadata Model

Every item should produce a durable record with fields equivalent to:

```text
item_id
canonical_item
source
status              # success, failed, skipped
metadata            # JSON object or null
error_type
error_message
http_status
attempts
first_seen_at
completed_at
last_attempt_at
content_hash
cache_key
run_id
```

Keep error messages bounded and avoid storing credentials, authorization headers, or full response bodies.

Metadata should be JSON-serializable. Adapters should validate required fields before returning results.

## Cache

Use SQLite rather than a directory of ad hoc JSON files. It provides atomic updates, indexing, and good standard-library support.

Cache key:

```text
sha256(
    adapter_name +
    "\0" +
    adapter_version +
    "\0" +
    canonical_item +
    "\0" +
    request-relevant-options
)
```

Store:

```text
cache_key PRIMARY KEY
adapter
adapter_version
canonical_item
metadata_json
status
created_at
expires_at
etag
last_modified
content_hash
```

Cache rules:

- Successful metadata is reusable until TTL expiration.
- Permanent failures may be cached briefly with a separate failure TTL.
- Transient failures should not normally be cached.
- `--force` bypasses cache reads and refreshes the entry.
- Cache writes use a transaction.
- Enable WAL mode and a busy timeout.
- Prefer one SQLite connection per thread, or serialize all database access through a persistence thread.

If conditional requests are supported, send `If-None-Match` and `If-Modified-Since`; a `304` response refreshes cache expiry without replacing metadata.

## Resumable Runs

Use a run database, also SQLite-backed, with:

```text
runs(
    run_id PRIMARY KEY,
    input_fingerprint,
    configuration_json,
    started_at,
    completed_at,
    status
)

items(
    run_id,
    sequence,
    original_item,
    canonical_item,
    state,
    cache_key,
    attempts,
    result_json,
    error_json,
    updated_at,
    PRIMARY KEY(run_id, sequence)
)
```

At startup:

1. Compute an input fingerprint from canonicalized input plus relevant crawl configuration.
2. With `--resume`, locate the matching incomplete run.
3. Treat `success`, `cached`, and `failed_permanent` items as completed.
4. Requeue `pending`, `running`, and `failed_retryable` items.
5. Mark stale `running` items as pending using a lease timestamp.
6. Persist each item transition transactionally.

Recommended states:

```text
pending
running
success
cached
failed_retryable
failed_permanent
```

A process interrupted during a write must leave either the previous state or the complete new state. Never write result JSON and completion state as separate non-transactional operations.

By default, preserve the original input sequence in final output. This makes resumed output deterministic even though processing order is concurrent.

## Output

Write results as JSON Lines during crawling when practical, but treat SQLite as the source of truth. To avoid partially-written JSON or CSV:

1. Persist all results durably.
2. Generate the requested final format from the state database.
3. Write to a temporary destination.
4. Flush and atomically replace the destination.

For stdout, emit completed records as they become available and document that stdout order is completion order. For deterministic order, provide `--ordered`, which buffers output or performs a final ordered export.

Exit codes:

```text
0  all items succeeded or were served from cache
1  completed with one or more item failures
2  invalid arguments or input
3  unrecoverable configuration or storage failure
```

## Configuration and Observability

Support CLI flags and optionally a JSON configuration file. CLI values override configuration-file values.

Log to stderr using `logging`:

- Run start and completion.
- Counts of pending, cached, successful, and failed items.
- Retry events at warning level.
- Per-item failures with identifiers.
- Rate-limit waits at debug level.
- Cache hit/miss counts.
- Elapsed time and throughput.

Never log response bodies, secrets, cookies, or complete authorization URLs.

Handle `SIGINT` and `SIGTERM` by:

1. Stopping new scheduling.
2. Allowing active requests to finish up to a short grace period.
3. Marking unfinished leases as resumable.
4. Committing all completed results.
5. Exiting with a nonzero status.

## Adapter Contract

Adapters should receive a request context containing:

```text
timeout
headers
cache metadata
attempt number
logger
```

They should return normalized metadata or raise typed exceptions:

```text
RetryableFetchError
PermanentFetchError
ParseError
RateLimitError
```

The crawler, rather than individual adapters, owns retries, rate limits, persistence, and resume behavior. Adapters may expose request-specific cache validators and response parsing logic.

## Verification Strategy

Unit tests should cover:

- Canonicalization and deduplication.
- Stable cache-key generation.
- Cache hit, miss, expiry, force refresh, and version invalidation.
- Token-bucket timing and burst limits.
- Per-host request serialization.
- Global worker bounds.
- Retry classification and exponential backoff.
- `Retry-After` handling.
- Response-size and timeout enforcement.
- JSON, malformed, empty, and unexpected metadata responses.
- Atomic state transitions.
- Resume behavior for pending, running, retryable, permanent, and completed items.
- Deterministic ordered output.
- Correct exit codes.

Use a local test HTTP server built with `http.server.ThreadingHTTPServer`. It should simulate:

- Successful responses.
- Delays for concurrency tests.
- 429 and 503 responses.
- Retryable network failures.
- Redirects.
- Conditional `304` responses.
- Malformed payloads.
- Oversized responses.

Integration tests should run a complete crawl twice and verify that the second run uses the cache, then interrupt a crawl and verify that `--resume` processes only unfinished items.

Concurrency tests should record active requests and assert:

```text
max_global_active <= workers
max_host_active <= host_limit
```

Rate-limit tests should use a fake monotonic clock and fake sleeper where possible, avoiding slow real-time tests.

Run static checks, unit tests, integration tests, and a subprocess test for CLI exit codes. The implementation is complete when repeated runs are idempotent, interruption does not lose completed results, cache behavior is observable, concurrency and request rates never exceed configured limits, and all durable state remains readable after forced termination.
