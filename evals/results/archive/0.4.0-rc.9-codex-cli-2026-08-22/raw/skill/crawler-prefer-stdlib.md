Plan a Python CLI crawler around a small, testable core, using only the standard library.

## CLI

```text
crawler crawl URL [URL ...]
  --output PATH              SQLite database path
  --workers N                Maximum concurrent requests
  --rate RATE                Requests/second globally
  --per-host-rate RATE       Requests/second per host
  --timeout SECONDS
  --max-pages N
  --resume RUN_ID
  --no-cache
  --user-agent STRING

crawler status --output PATH [RUN_ID]
crawler export --output PATH --format json|csv PATH
crawler cache clear --output PATH
```

Exit nonzero only for invalid configuration or unrecoverable storage errors; individual URL failures should be recorded and reported at the end.

## Architecture

```text
CLI
 └── CrawlRunner
      ├── URL frontier and deduplication
      ├── Scheduler
      │    ├── global concurrency limit
      │    ├── global rate limiter
      │    └── per-host rate limiters
      ├── Fetcher
      │    └── urllib.request
      ├── CacheStore
      │    └── sqlite3
      ├── MetadataParser
      │    └── html.parser
      └── RunStore
           └── sqlite3 checkpoints and results
```

Keep each component independently usable in tests. Avoid a framework or dependency-injection layer unless the implementation needs it.

## Fetching and concurrency

Use `ThreadPoolExecutor` with a fixed worker count. This is practical for blocking `urllib` I/O and keeps the implementation compatible with standard Python installations.

For each URL:

1. Normalize and validate the URL.
2. Check the cache and completed-run records.
3. Acquire the global request-rate permit.
4. Acquire the host-specific permit.
5. Fetch with `urllib.request.urlopen`.
6. Enforce response size and timeout limits.
7. Parse metadata and links.
8. Persist the result and checkpoint before scheduling discovered links.

Bound concurrency in two places:

- `ThreadPoolExecutor(max_workers=N)` limits active fetches.
- A `threading.BoundedSemaphore(N)` can guard the actual request section if future scheduling changes could otherwise exceed the limit.

Use a lock-protected token-bucket or next-allowed-time limiter. A simple monotonic-clock implementation is sufficient:

- Store the next permitted timestamp.
- Under a lock, reserve the next slot.
- Sleep outside the lock until that time.
- Maintain one limiter globally and one per hostname.

Honor `Retry-After` for 429 and selected 5xx responses, with exponential backoff and jitter. Cap retries and persist the final error.

## URL and metadata handling

Normalize URLs before deduplication:

- Lowercase scheme and hostname.
- Remove fragments.
- Normalize default ports.
- Resolve relative links with `urllib.parse.urljoin`.
- Optionally restrict crawling to allowed hosts.

Parse common metadata with `html.parser.HTMLParser`:

- `<title>`
- `<meta name="description">`
- Open Graph fields (`og:title`, `og:description`, `og:image`, `og:url`)
- Twitter card fields
- Canonical URL
- Language
- HTTP status, content type, fetched timestamp, and final URL

Store parser output as a JSON object so new fields can be added without schema churn.

## SQLite schema

Use one SQLite file for cache, runs, and results. Enable WAL mode and reasonable busy timeouts.

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  config_json TEXT NOT NULL
);

CREATE TABLE urls (
  run_id TEXT NOT NULL,
  url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  state TEXT NOT NULL, -- queued, running, done, failed, skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  discovered_from TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, normalized_url)
);

CREATE TABLE results (
  run_id TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  status_code INTEGER,
  final_url TEXT,
  headers_json TEXT,
  metadata_json TEXT,
  error_type TEXT,
  error_message TEXT,
  PRIMARY KEY (run_id, normalized_url)
);

CREATE TABLE cache (
  cache_key TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  expires_at TEXT,
  status_code INTEGER,
  headers_json TEXT,
  body BLOB,
  body_hash TEXT
);
```

Use a cache key based on normalized URL plus relevant request options, such as user agent. Support TTL and conditional requests using `ETag` and `Last-Modified` when cached headers exist.

Do not keep large bodies in memory longer than necessary. Enforce a maximum body size and either store the body in SQLite or store only metadata if body retention is not required.

## Resumable runs

Every state transition should be transactional:

- Insert seed URLs as `queued`.
- Mark a URL `running` before fetching.
- Persist result and mark `done` or `failed` in one transaction.
- On startup with `--resume`, convert stale `running` rows back to `queued`.
- Skip `done` URLs unless `--refresh` or cache expiry requires refetching.
- Preserve discovered URLs and their provenance.

A run should be restartable after interruption without duplicating results. Commit frequently enough that losing the process does not lose substantial progress.

## Configuration and observability

Support CLI flags plus an optional JSON/TOML configuration file (`tomllib` where available). Validate worker counts, rates, timeouts, and limits at startup.

Log structured events with the standard `logging` module:

- run start/finish
- fetch attempt
- cache hit/miss
- retry and backoff
- parse failure
- checkpoint progress

Print a final summary: queued, completed, cached, failed, skipped, requests, and elapsed time.

## Testing strategy

Use `unittest`, `unittest.mock`, `tempfile`, and a local `http.server.ThreadingHTTPServer`.

Unit tests:

- URL normalization and scope filtering.
- Metadata parsing, malformed HTML, duplicate meta tags, and relative links.
- Cache TTL, cache key generation, conditional headers, and cache bypass.
- Token-bucket timing and per-host separation.
- Retry handling for 429/5xx and `Retry-After`.
- SQLite transactions and stale-running recovery.
- Idempotent result writes and duplicate URL suppression.

Integration tests:

- Crawl a local server with several linked pages.
- Assert maximum simultaneous requests using server-side counters.
- Assert global and per-host rate limits with timestamps.
- Kill or simulate interruption, resume, and verify no completed page is refetched.
- Verify cache hits avoid network requests.
- Verify one failing URL does not abort the run.
- Test CLI exit codes, status output, and JSON/CSV export.

For timing-sensitive tests, inject a clock/sleep function into the rate limiter instead of relying solely on wall-clock delays. Keep one small real-delay integration test for end-to-end confidence.

## Suggested implementation order

1. URL normalization and metadata parser.
2. SQLite schema, cache, and run-state persistence.
3. Fetcher with retries and size/timeout limits.
4. Rate limiters and worker scheduler.
5. Crawl orchestration and resumability.
6. CLI commands and exports.
7. Unit tests, then local-server integration tests.
8. Documentation with operational limits and example commands.