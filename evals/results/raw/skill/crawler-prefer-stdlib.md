## Proposed Design

Build a Python CLI using the standard library. Keep the crawler engine independent from the specific metadata source so URL construction and response parsing can be adapted without changing scheduling, caching, or persistence.

### CLI

```text
metadata-crawl crawl INPUT
  --output PATH
  --state PATH
  --cache-dir PATH
  --concurrency 8
  --rate 5
  --burst 5
  --timeout 30
  --retries 4
  --resume
  --refresh
  --max-age 86400
```

`INPUT` is a newline-delimited list of identifiers or URLs. Results use JSON Lines so each completed item can be written atomically without retaining the full run in memory.

Useful commands:

```text
metadata-crawl crawl ...
metadata-crawl status --state crawl.db
metadata-crawl retry-failed --state crawl.db
metadata-crawl clear-cache --cache-dir .cache
```

### Standard-Library Components

- `argparse`: CLI parsing
- `urllib.request`: HTTP requests
- `concurrent.futures.ThreadPoolExecutor`: bounded I/O concurrency
- `threading.Condition`: shared rate limiter
- `sqlite3`: run state and resumability
- `hashlib`: stable cache keys
- `json`: metadata, cache records, and JSONL output
- `pathlib`: paths
- `logging`: structured operational messages
- `email.utils`: HTTP date parsing and `Retry-After`
- `tempfile` plus `os.replace`: atomic cache writes

A pluggable HTTP client interface can leave room for a third-party client later if requirements grow to HTTP/2, asynchronous I/O, or complex connection management.

## Execution Flow

1. Parse and normalize input into stable work-item keys.
2. Insert new items into SQLite with `INSERT OR IGNORE`.
3. Reset stale `running` items to `pending` when resuming.
4. Submit at most `concurrency` items to the thread pool.
5. Before each network request:
   - Check the cache.
   - Acquire a rate-limit token.
6. Fetch and parse metadata.
7. Write the cache entry atomically.
8. Append the result to JSONL.
9. Mark the item complete in SQLite only after the output write succeeds.
10. Retry transient failures with bounded exponential backoff and jitter.
11. Record permanent or exhausted failures for later retry.

The coordinator should keep only a small bounded number of futures in flight, rather than submitting the entire input at once.

## Rate Limiting

Use a thread-safe token bucket based on `time.monotonic()`:

- `rate`: tokens replenished per second
- `burst`: maximum accumulated tokens
- Every request consumes one token
- Waiting threads sleep through a shared `Condition`
- A response with `429` or `503` honors `Retry-After`
- Optionally maintain one limiter per host if input can target multiple services

Concurrency and request rate remain separate controls. Eight workers may be allowed while the crawler still sends only two requests per second.

## Caching

Key each entry with:

```text
sha256(normalized_method + normalized_url + relevant_headers)
```

Store a small JSON envelope containing:

```json
{
  "url": "...",
  "fetched_at": 1786800000,
  "status": 200,
  "etag": "...",
  "last_modified": "...",
  "headers": {},
  "body_file": "..."
}
```

Behavior:

- Return fresh entries immediately.
- For stale entries, send `If-None-Match` or `If-Modified-Since`.
- On `304`, refresh the timestamp and reuse the body.
- Cache successful responses by default.
- Cache `404` for a shorter configurable period.
- Do not cache transient server or transport failures.
- Use temporary files followed by `os.replace()` to prevent partial entries.

## Resumable State

Use SQLite rather than deriving state solely from output files.

Suggested schema:

```sql
CREATE TABLE items (
    item_key       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    status         TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT,
    completed_at   TEXT,
    next_attempt_at TEXT,
    output_offset  INTEGER,
    error_type     TEXT,
    error_message  TEXT
);
```

Statuses: `pending`, `running`, `complete`, and `failed`.

Commit state transitions individually or in small batches. On startup, consider `running` rows abandoned if their lease timestamp is older than a configured threshold. Stable item keys make repeated inputs idempotent.

For stronger output recovery, include `item_key` in every JSONL record. A startup reconciliation pass can mark a state row complete when its output record exists but the final database update was interrupted.

## Retry Policy

Retry only failures likely to be temporary:

- Connection resets and timeouts
- HTTP `408`, `425`, `429`
- HTTP `500`, `502`, `503`, `504`

Do not normally retry parsing failures or most other `4xx` responses.

Use capped exponential backoff with jitter:

```text
delay = min(max_delay, base_delay * 2**attempt) * random.uniform(0.5, 1.5)
```

Persist `attempts` and `next_attempt_at` so stopping the process does not reset retry behavior.

## Internal Modules

```text
metadata_crawler/
  __main__.py       CLI entry point
  crawler.py        coordinator and bounded work scheduling
  client.py         HTTP transport and retry classification
  rate_limit.py     token bucket
  cache.py          filesystem cache and revalidation
  state.py          SQLite repository
  sources.py        URL construction and metadata parsing
  output.py         synchronized JSONL writer
```

Keep interfaces small. For example, inject a callable transport into the crawler so tests can avoid real network access.

## Tests

Use `unittest`, `unittest.mock`, and a local `http.server.ThreadingHTTPServer`.

Core unit tests:

- Token bucket never exceeds the configured rate or burst.
- Cache keys are stable and distinguish relevant request variants.
- Fresh cache hits avoid HTTP calls.
- Stale entries perform conditional requests.
- Atomic cache writes do not expose partial records.
- Retry classification distinguishes transient and permanent failures.
- Backoff respects its cap and `Retry-After`.
- State transitions and attempt counts persist correctly.
- Resume resets abandoned work but skips completed items.
- Duplicate inputs produce one work item.
- Output is valid under concurrent completion.

Integration tests:

- Local server records request timestamps to verify rate limiting.
- Server returns `429`, then success, to verify delayed retry.
- Server returns `304` to verify cache revalidation.
- Terminate a run midway, restart it, and verify exactly one final record per item.
- Simulate malformed responses and exhausted retries.
- Run with more inputs than the work queue bound to verify memory remains bounded.

Inject the clock, sleep function, random jitter source, and HTTP transport where practical. This keeps rate-limit and retry tests deterministic and fast.

## Delivery Sequence

1. Implement CLI, SQLite state, and sequential fetching.
2. Add atomic JSONL output and resume reconciliation.
3. Add filesystem caching and conditional requests.
4. Add retry classification and persisted backoff.
5. Add bounded thread-pool scheduling and the token bucket.
6. Add local-server integration tests.
7. Add graceful shutdown: stop accepting work, finish or abandon in-flight leases cleanly, flush output, and close SQLite.