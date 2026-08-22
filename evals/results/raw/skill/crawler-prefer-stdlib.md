# CLI metadata crawler design

## Scope

Crawl a supplied set of HTTP(S) URLs and collect:

- Requested and final URL
- HTTP status
- Content type and length
- Page title
- Meta description
- Canonical URL
- Fetch timestamp
- Error classification
- Attempt count

The crawler does not discover links unless explicitly added later.

## CLI

```text
crawler crawl [URL ...]
  --input PATH              Read one URL per line; "-" means stdin
  --db PATH                 SQLite state/cache database
  --run ID                  Resume an existing run
  --workers N               Maximum simultaneous requests, default 8
  --rate R                  Maximum requests per second per host, default 2
  --timeout SECONDS         Request timeout, default 20
  --cache-ttl SECONDS       Fresh-cache duration, default 86400
  --max-attempts N          Retry limit, default 3
  --refresh                 Ignore fresh cached results
  --retry-failed            Retry previously failed jobs
  --output {jsonl,csv}      Result format, default jsonl
  --output-path PATH        Default stdout
```

Additional commands:

```text
crawler resume RUN_ID ...
crawler status RUN_ID
crawler export RUN_ID --output jsonl|csv
```

URLs may be supplied positionally, through `--input`, or both. Normalize and deduplicate them before scheduling.

Exit codes:

- `0`: all jobs completed successfully
- `2`: run completed with one or more failed URLs
- `1`: invalid arguments or unrecoverable crawler error

## Storage

Use SQLite from the standard library.

### `runs`

- `id`
- `created_at`
- `started_at`
- `finished_at`
- `status`: `running`, `completed`, `partial`, `aborted`
- serialized configuration

### `jobs`

- `run_id`
- normalized `url`
- `state`: `queued`, `running`, `succeeded`, `failed`
- `attempts`
- `next_attempt_at`
- `last_error`
- `updated_at`

Primary key: `(run_id, url)`.

### `cache`

- normalized URL
- final URL
- status
- content type
- title
- description
- canonical URL
- response headers needed for revalidation
- `etag`
- `last_modified`
- `fetched_at`
- `expires_at`

A cache entry is fresh while `expires_at > now`. Store metadata rather than full response bodies unless future requirements need reparsing.

## Run and resume behavior

1. Create a run and insert deduplicated URLs as `queued`.
2. On startup, any `running` jobs belonging to that run are reset to `queued`.
3. A resumed run skips `succeeded` jobs unless `--refresh` is specified.
4. Failed jobs are skipped unless `--retry-failed` is specified.
5. Each state update is committed transactionally.
6. Completion is determined only after no queued or running jobs remain.
7. Ctrl-C marks the run `aborted`; already committed results remain usable.

A new run can still use the shared cache, so resuming and caching are independent.

## Fetching

Use `urllib.request` and `urllib.parse`.

Accept only `http` and `https`. Set:

- A descriptive `User-Agent`
- `Accept: text/html,application/xhtml+xml`
- Configured timeout
- `If-None-Match` when a cached ETag exists
- `If-Modified-Since` when a cached modification date exists

Handle redirects manually, with a maximum of five hops. Apply URL validation and rate limiting to every redirected host.

Limit response processing to a configurable maximum body size, for example 2 MiB. Ignore unsupported content types while still recording the HTTP result.

Decode using:

1. Charset from `Content-Type`
2. HTML charset declaration
3. UTF-8 with replacement

Parse HTML using `html.parser.HTMLParser`. Extract the first:

- `<title>`
- `<meta name="description">`
- `<link rel="canonical">`

Normalize whitespace and truncate extracted fields to fixed limits.

## Concurrency

Use `concurrent.futures.ThreadPoolExecutor`.

- `--workers` is the hard maximum number of in-flight HTTP requests.
- Use a bounded work queue, sized at approximately `workers * 2`.
- Workers fetch jobs and return immutable result objects.
- A single database-writer path performs SQLite updates, avoiding concurrent write contention.
- Never hold a database transaction while waiting for network I/O.

The scheduler should claim jobs transactionally:

```text
BEGIN IMMEDIATE
select one eligible queued job
update it to running
COMMIT
```

This prevents duplicate processing if the design later uses multiple scheduler processes.

## Rate limiting

Implement a thread-safe per-host limiter using monotonic time.

For each `(scheme, hostname, effective_port)`:

- Permit at most one request every `1 / rate` seconds.
- Protect limiter state with a lock.
- Compute the next permitted timestamp.
- Sleep outside the lock.
- Use monotonic time, never wall-clock time.

Optionally add a global limiter if a global request rate is configured. Acquire the concurrency slot and rate-limit permission before each HTTP request, including redirects and retries.

Retry delays must not bypass the limiter.

## Retries

Retry:

- Network timeouts
- Connection failures
- HTTP `408`, `425`, `429`, `500`, `502`, `503`, and `504`

Do not retry ordinary `4xx` responses.

Use bounded exponential backoff with jitter:

```text
delay = min(max_delay, base_delay * 2^(attempt - 1)) + random_jitter
```

Honor a valid `Retry-After` value, capped by the configured maximum delay.

After the final retry, store a structured failure containing category, message, and attempt count.

A valid HTTP response, including a `404`, is a completed fetch and should not be classified as a transport failure.

## Cache behavior

For each URL:

1. If `--refresh` is absent and the entry is fresh, return cached metadata without a request.
2. If stale, send conditional headers.
3. On `304 Not Modified`, retain metadata and refresh cache timestamps.
4. On `200`, replace metadata and validators.
5. On another response or failure, preserve the old cache entry unless the new result is authoritative.
6. Cache writes and job completion updates occur in one SQLite transaction.

The cache TTL should be recorded per entry so changing the CLI default does not alter existing entries unexpectedly.

## Output

Emit one result per input URL, in deterministic input order.

JSONL example:

```json
{"url":"https://example.com","final_url":"https://www.example.com/","status":200,"content_type":"text/html","title":"Example","description":"...","canonical_url":"https://example.com/","fetched_at":"2025-01-01T12:00:00Z","error":null}
```

CSV should use stable column order and quote fields according to RFC 4180 conventions.

Output only after the corresponding result is committed, or reconstruct results from the database for `export`.

## Suggested implementation boundaries

- `cli`: argument parsing, exit codes, command dispatch
- `models`: URL, job, cache, and result records
- `store`: SQLite schema, migrations, transactions, job claiming
- `limiter`: per-host and optional global limiters
- `fetcher`: HTTP requests, redirects, retries, decoding
- `parser`: metadata extraction
- `runner`: scheduling, cancellation, progress, run lifecycle
- `output`: JSONL, CSV, and deterministic ordering

Use `dataclasses`, `enum`, `sqlite3`, `urllib`, `threading`, `time`, `random`, `json`, `csv`, and `unittest`.

## Verification

Tests should include:

1. URL normalization and deduplication.
2. HTML title, description, canonical, charset, and malformed-markup parsing.
3. Unsupported schemes and invalid URLs.
4. Fresh cache avoiding network access.
5. Stale cache sending validators.
6. Correct `304` handling.
7. Cache replacement after a successful `200`.
8. Per-host rate-limit spacing.
9. Concurrent requests never exceeding `--workers`.
10. Redirect limits and redirect rate limiting.
11. Retry behavior for transient failures and `Retry-After`.
12. No retries for ordinary `4xx` responses.
13. Response-size limits and decoding.
14. Interrupted runs resetting `running` jobs.
15. Resume skipping completed jobs.
16. Retry behavior for failed jobs.
17. Transactional result persistence.
18. Deterministic JSONL and CSV output.
19. CLI exit codes for complete and partial runs.

Use `http.server.ThreadingHTTPServer` with controllable handlers for integration tests, and inject clock/sleep functions into rate-limit tests so they run quickly and deterministically.
