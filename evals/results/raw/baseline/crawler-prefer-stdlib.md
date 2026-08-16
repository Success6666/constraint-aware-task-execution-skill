Assumption: Python 3.11+ CLI crawling HTTP endpoints and extracting structured metadata. The design keeps the crawl engine transport-agnostic so HTML, JSON, or custom metadata parsers can be added later.

**Architecture**

```text
CLI
 └─ Crawl coordinator
     ├─ Persistent frontier / run state (SQLite)
     ├─ Bounded worker pool (ThreadPoolExecutor)
     ├─ Per-host rate limiter
     ├─ HTTP client (urllib.request)
     ├─ Cache (SQLite metadata + response files)
     ├─ Parser / link discovery
     └─ Result writer
```

Use standard-library components:

- `argparse` for the CLI
- `concurrent.futures.ThreadPoolExecutor` for bounded I/O concurrency
- `urllib.request` for HTTP
- `sqlite3` for frontier, cache metadata, and checkpoints
- `hashlib` for cache keys
- `pathlib` and atomic `os.replace` for response files
- `threading.Condition` plus `time.monotonic` for rate limiting
- `logging` for diagnostics
- `unittest` and `http.server` for tests

**CLI Surface**

```text
crawler crawl SEED...
  --db PATH
  --cache-dir PATH
  --output PATH
  --workers 8
  --rate 2.0
  --burst 2
  --timeout 20
  --max-retries 3
  --max-depth 3
  --max-items N
  --cache-ttl 86400
  --user-agent VALUE
  --resume RUN_ID
  --retry-failed
  --offline
  --format jsonl|csv

crawler status RUN_ID --db PATH
crawler runs --db PATH
crawler cache prune --older-than SECONDS
```

JSON Lines is the preferred output format because each completed result can be appended independently.

**Persistent State**

Use SQLite in WAL mode. Suggested tables:

```sql
runs(
  id TEXT PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  config_json TEXT
);

frontier(
  run_id TEXT,
  url TEXT,
  normalized_url TEXT,
  depth INTEGER,
  state TEXT,             -- pending, leased, complete, failed, skipped
  attempts INTEGER,
  next_attempt_at REAL,
  lease_expires_at REAL,
  discovered_from TEXT,
  last_error TEXT,
  PRIMARY KEY(run_id, normalized_url)
);

results(
  run_id TEXT,
  normalized_url TEXT,
  fetched_at TEXT,
  status_code INTEGER,
  content_type TEXT,
  metadata_json TEXT,
  PRIMARY KEY(run_id, normalized_url)
);

cache(
  cache_key TEXT PRIMARY KEY,
  url TEXT,
  status_code INTEGER,
  headers_json TEXT,
  body_path TEXT,
  fetched_at REAL,
  expires_at REAL,
  etag TEXT,
  last_modified TEXT
);
```

SQLite is the source of truth. The output file is a projection that can be regenerated from `results`, preventing duplicate output after a crash.

**Concurrency**

The coordinator owns all scheduling and database writes. Workers only fetch and parse tasks, then return immutable result objects through futures.

- Keep at most `workers` futures in flight.
- Lease frontier entries before submission.
- Commit each completed result and newly discovered URL in one transaction.
- Reset expired `leased` rows to `pending` at startup.
- Deduplicate on normalized URL using the database primary key.
- Bound downloaded bodies with a configurable byte limit.
- Handle `SIGINT` by stopping new submissions, waiting briefly for active work, committing state, and marking the run `interrupted`.

A thread pool is appropriate because `urllib` requests are blocking and the work is primarily network I/O. Avoid mixing `asyncio` with blocking standard-library HTTP calls.

**Rate Limiting**

Implement a thread-safe token bucket per origin, keyed by `(scheme, host, port)`:

- `rate` controls token replenishment per second.
- `burst` controls bucket capacity.
- Workers acquire a token immediately before a network request.
- Use `time.monotonic()` to avoid wall-clock changes.
- A `Condition` allows waiting workers to sleep until the next token is available.
- Respect `Retry-After` for `429` and `503`.
- Apply exponential backoff with bounded jitter for transient failures.
- Do not retry most other `4xx` responses.

The global worker limit bounds total concurrency; an optional `--per-host-workers` semaphore can additionally prevent one host from occupying the entire pool.

**Caching**

Cache keys should include the normalized URL and request representation-affecting headers, such as `Accept`.

Cache behavior:

1. Return a fresh cached response without network access.
2. For stale entries with `ETag` or `Last-Modified`, issue a conditional request.
3. On `304`, refresh timestamps and reuse the cached body.
4. Write bodies to temporary files, then atomically rename them.
5. Store only successful responses by default; optionally retain short-lived negative entries for `404` and `410`.
6. In `--offline` mode, treat cache misses as explicit failures.

Set a maximum cache size and prune by last access or age. Avoid storing large bodies directly in SQLite.

**Resumability**

A run is resumable because every URL transition is persisted:

```text
pending -> leased -> complete
                  -> failed
```

On resume:

- Load the original run configuration.
- Reject incompatible changes such as normalization rules or crawl scope.
- Convert expired leases back to `pending`.
- Continue entries ordered by `next_attempt_at`, depth, then insertion order.
- Preserve retry counts and discovered relationships.
- Mark the run complete only when no pending, eligible failed, or active leased entries remain.

Generate run IDs with `uuid.uuid4()`. Store configuration as canonical JSON for comparison and diagnostics.

**Core Module Layout**

```text
crawler/
  __main__.py       CLI entry point
  cli.py            Argument parsing and command dispatch
  coordinator.py    Scheduling and lifecycle
  frontier.py       SQLite schema and transactions
  fetch.py          urllib transport and retry classification
  limiter.py        Token bucket and host semaphores
  cache.py          Cache lookup, validation, and body storage
  normalize.py      URL canonicalization and scope checks
  parse.py          Metadata extraction and URL discovery
  models.py         Frozen dataclasses and enums
  output.py         JSONL/CSV export
```

Keep interfaces narrow. For example, inject a `Transport.fetch(request)` and clock functions into the crawler so tests do not depend on real time or public networks.

**Testing Strategy**

Use `unittest`, temporary directories, and a local `ThreadingHTTPServer`.

Unit tests:

- URL normalization and deduplication
- Token refill, burst behavior, and independent host limits using a fake clock
- Cache freshness and key generation
- Conditional request handling
- Retry classification and `Retry-After` parsing
- Frontier state transitions and expired lease recovery
- Scope, depth, and item-limit enforcement

Integration tests:

- Concurrency never exceeds configured bounds
- Request spacing satisfies the rate limit within a small tolerance
- `429`, `503`, timeouts, and dropped connections retry correctly
- Cached responses avoid subsequent network requests
- `ETag` produces a conditional request and handles `304`
- An interrupted crawl resumes without duplicate fetches or results
- Corrupt or missing cache body files degrade to a cache miss
- SIGINT leaves the database resumable
- JSONL and CSV exports are deterministic

Property-style tests can remain dependency-free by generating randomized URL and frontier inputs with `random` and fixed seeds.

**Implementation Order**

1. Define immutable models, URL normalization, and SQLite schema.
2. Implement the synchronous fetcher and local-server tests.
3. Add cache lookup and conditional requests.
4. Add the token bucket with fake-clock tests.
5. Build the bounded coordinator and frontier transactions.
6. Add retry scheduling, leases, interruption, and resume.
7. Add parsers, scope controls, and result export.
8. Run fault-injection integration tests and document operational limits.

The main correctness invariant is: a URL result and its discovered frontier entries are committed atomically, while no network operation occurs inside a database transaction.