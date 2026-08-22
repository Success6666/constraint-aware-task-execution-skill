Build the crawler as a small Python package using `argparse`, `asyncio`, `urllib`, `sqlite3`, and standard logging. Keep fetching, scheduling, persistence, and output behind narrow interfaces so the CLI remains testable.

**CLI**

```text
crawler
  crawl <input> --output results.jsonl
    --workers 8
    --rate 2/s
    --timeout 15
    --cache .crawler-cache.db
    --resume
    --retry 3
    --user-agent ...
  status --cache .crawler-cache.db
  export --cache .crawler-cache.db --format jsonl
```

Input should be newline-delimited URLs, with normalization and duplicate removal before scheduling. Exit nonzero for configuration errors; preserve partial results for fetch failures.

**Core pipeline**

1. Read and normalize URLs.
2. Load prior URL state from SQLite when `--resume` is enabled.
3. Enqueue only pending or retryable URLs.
4. Run bounded workers via `asyncio.Queue(maxsize=...)`.
5. Apply one shared rate limiter before each request.
6. Fetch metadata with `urllib.request`, enforcing timeout, redirect limits, response-size limits, and accepted content types.
7. Parse only the required metadata: status, final URL, title, description, canonical URL, selected Open Graph/Twitter fields, content length, and timestamps.
8. Persist each result transactionally as soon as it completes.
9. Emit JSON Lines or a summary after completion.

Use a producer/worker arrangement:

```text
input -> normalize -> state filter -> bounded queue
                                      |
                            N async workers
                                      |
                         rate limiter + fetch
                                      |
                         parse -> SQLite commit
```

A bounded queue prevents unbounded memory growth. Each worker owns no mutable global state except shared scheduler primitives.

**Concurrency and rate limiting**

- `asyncio.Semaphore(workers)` or a fixed number of queue workers bounds in-flight requests.
- Implement a shared token-bucket or minimum-interval limiter using `asyncio.Lock` and `time.monotonic()`.
- Rate-limit attempts, including retries.
- Add jittered exponential backoff:

```text
delay = min(base * 2**attempt, max_delay) + random_jitter
```

- Honor `Retry-After` for HTTP 429/503 when it is valid and bounded.
- Classify errors:
  - permanent: malformed URL, unsupported scheme, most 4xx responses;
  - retryable: timeouts, connection errors, 429, 500-599.
- Cancel workers cleanly on SIGINT, leaving committed records resumable.

**Caching and resume state**

Use SQLite rather than a directory of files:

```sql
CREATE TABLE pages (
  url TEXT PRIMARY KEY,
  normalized_url TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending/running/succeeded/failed
  attempts INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER,
  final_url TEXT,
  metadata_json TEXT,
  error TEXT,
  fetched_at TEXT,
  next_attempt_at TEXT,
  content_hash TEXT
);

CREATE INDEX pages_status_idx ON pages(status);
```

Before fetching, treat a successful record as fresh when `fetched_at` is within `--cache-ttl`; return it without a network request. Store metadata and errors atomically. On startup, convert stale `running` records back to `pending`, which makes interrupted runs recoverable.

Optionally maintain a separate `responses` table keyed by normalized URL plus validator fields (`ETag`, `Last-Modified`) for conditional requests. Keep raw bodies disabled by default; if enabled, enforce a byte limit and store compressed content only when justified.

**Parsing**

Use `html.parser.HTMLParser` with a small state machine:

- capture `<title>`;
- collect `<meta name=description>`;
- collect `property` values for Open Graph;
- collect `name` values for Twitter cards;
- capture `<link rel=canonical>`.

Normalize whitespace, decode HTML entities, cap field lengths, and tolerate malformed markup. Do not execute JavaScript or follow embedded resources.

**Observability**

Log structured events containing URL, attempt, elapsed time, outcome, HTTP status, and error class. Provide periodic progress to stderr:

```text
completed=120 pending=34 succeeded=108 failed=12 rate=1.9/s
```

Keep stdout reserved for machine-readable output.

**Testing strategy**

Use only `unittest`, `unittest.mock`, and a local `http.server`.

- URL normalization and deduplication.
- Metadata parser fixtures: normal, malformed, duplicate tags, missing title, charset/entity handling.
- Rate limiter: no request starts before its interval; concurrent callers remain serialized.
- Worker bound: instrument the fetcher and assert maximum simultaneous calls.
- Retry classification, exponential backoff, `Retry-After`, and attempt limits.
- Cache hit/miss and TTL expiry.
- Resume behavior after simulated interruption, including recovery of `running` rows.
- SQLite transactional persistence and idempotent reruns.
- HTTP integration cases: redirects, 404, 429, 500, timeout, oversized body, invalid content type.
- CLI parsing, exit codes, JSONL output, and SIGINT cleanup.

Inject a `Fetcher`, `Clock`, `Sleeper`, and RNG into the crawler so timing and retries are deterministic in unit tests. Add one end-to-end test that crawls a local test server with multiple workers, interrupts midway, then resumes and verifies no unnecessary refetches.