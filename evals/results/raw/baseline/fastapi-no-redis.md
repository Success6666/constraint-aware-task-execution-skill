Use PostgreSQL as the source of truth, SQLAlchemy 2.0 async for persistence, Alembic for migrations, and a small service layer that owns alias generation, expiration, and redirect behavior. Redis is unnecessary for correctness and should not be introduced as an implicit dependency.

## File Layout

```text
url-shortener/
├── pyproject.toml
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── README.md
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_create_short_links.py
├── src/
│   └── shortener/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── logging.py
│       ├── database.py
│       ├── dependencies.py
│       ├── exceptions.py
│       ├── models/
│       │   ├── __init__.py
│       │   └── short_link.py
│       ├── schemas/
│       │   ├── __init__.py
│       │   └── short_link.py
│       ├── repositories/
│       │   ├── __init__.py
│       │   └── short_links.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── aliases.py
│       │   └── short_links.py
│       └── api/
│           ├── __init__.py
│           ├── router.py
│           └── routes/
│               ├── __init__.py
│               ├── health.py
│               ├── links.py
│               └── redirects.py
└── tests/
    ├── conftest.py
    ├── factories.py
    ├── unit/
    │   ├── test_aliases.py
    │   └── test_short_link_service.py
    └── integration/
        ├── test_create_link.py
        ├── test_get_link.py
        ├── test_redirect.py
        ├── test_expiration.py
        └── test_concurrency.py
```

## Data Model

`short_links`:

```text
id             UUID primary key
alias          VARCHAR(64) unique, not null
target_url     TEXT not null
created_at     TIMESTAMPTZ not null
expires_at     TIMESTAMPTZ nullable
last_access_at TIMESTAMPTZ nullable
access_count   BIGINT not null default 0
```

Indexes and constraints:

```sql
CREATE UNIQUE INDEX uq_short_links_alias ON short_links (alias);
CREATE INDEX ix_short_links_expires_at
    ON short_links (expires_at)
    WHERE expires_at IS NOT NULL;
ALTER TABLE short_links
    ADD CONSTRAINT ck_short_links_alias_not_empty
    CHECK (length(alias) > 0);
```

Use UTC-aware timestamps throughout. Do not represent expiry with a boolean because it becomes stale. A link is expired when:

```python
expires_at is not None and expires_at <= now
```

Expired aliases should remain reserved unless an explicit product requirement allows reuse. Permanent reservation prevents an old link from unexpectedly redirecting to a new destination.

## HTTP API

### Create a link

```http
POST /api/v1/links
Content-Type: application/json

{
  "target_url": "https://example.com/a",
  "custom_alias": "docs",
  "expires_at": "2026-09-01T00:00:00Z"
}
```

Response: `201 Created`

```json
{
  "alias": "docs",
  "target_url": "https://example.com/a",
  "short_url": "https://sho.rt/docs",
  "created_at": "2026-08-16T12:00:00Z",
  "expires_at": "2026-09-01T00:00:00Z"
}
```

Behavior:

- Validate `http` and `https` target URLs.
- Reject credentials embedded in URLs unless deliberately supported.
- Normalize custom aliases according to a documented policy.
- Reject reserved aliases such as `api`, `docs`, `redoc`, `openapi.json`, and `health`.
- Return `409 Conflict` when a custom alias already exists.
- Return `422 Unprocessable Entity` for invalid URLs, aliases, or expiry times.
- Generate an alias when `custom_alias` is omitted.

### Retrieve metadata

```http
GET /api/v1/links/{alias}
```

Return `200` for an active link and `410 Gone` for an expired link. Avoid exposing the endpoint publicly if link metadata is considered private.

### Redirect

```http
GET /{alias}
```

- Active alias: `307 Temporary Redirect` with `Location`.
- Unknown alias: `404 Not Found`.
- Expired alias: `410 Gone`.
- Support `HEAD /{alias}` with the same status and headers.
- Use `308 Permanent Redirect` only if destinations are immutable and permanent caching is desired.

Register `/api/v1` and operational routes before the catch-all `/{alias}` route.

### Operational endpoints

```text
GET /health/live   process is running
GET /health/ready  database connection succeeds
```

## Alias Generation

Use a cryptographically secure random alias, for example 8 characters from a URL-safe alphabet that excludes ambiguous characters.

```python
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
```

The database unique constraint is the final concurrency control:

1. Generate an alias.
2. Attempt the insert.
3. On unique-constraint violation, roll back and retry.
4. Stop after a small bounded number of attempts.
5. Return `503 Service Unavailable` if generation repeatedly collides.

Do not implement “check then insert”; two concurrent requests can pass the check and select the same alias.

Custom aliases are inserted once and map unique-constraint violations to `409`.

An alternative is to encode a database-generated integer ID with Base62. Random aliases are preferable when sequential volume should not be exposed.

## Persistence and Transactions

`database.py` should provide:

- Async SQLAlchemy engine.
- `async_sessionmaker`.
- A per-request session dependency.
- Connection pool configuration.
- Development-only SQLite support if useful, while production and integration tests use PostgreSQL.

Repository responsibilities:

```text
insert(...)
find_by_alias(...)
increment_access(...)
delete_expired_batch(...)
```

Service responsibilities:

```text
validate business rules
generate aliases
handle collision retries
evaluate expiration against an injected clock
translate persistence outcomes into domain exceptions
```

Keep route handlers limited to HTTP input/output translation.

For redirect accounting, correctness of the redirect should not depend on updating analytics. A pragmatic initial implementation can issue one atomic update:

```sql
UPDATE short_links
SET access_count = access_count + 1,
    last_access_at = now()
WHERE id = :id;
```

This adds a database write to each redirect. If high redirect throughput is expected, omit synchronous analytics initially or move events to a durable queue later. Do not add an in-process counter because multiple workers would lose or fragment data.

## Expiration and Cleanup

Expiration must be enforced during lookup, so correctness never depends on a cleanup job.

Optional physical cleanup can run as a separate scheduled command:

```text
python -m shortener.cleanup --batch-size 1000
```

The command should delete in bounded batches and be invoked by Kubernetes CronJob, systemd timer, or another external scheduler. Do not start one cleanup loop inside every API worker.

If aliases must remain permanently reserved, either retain expired rows or move deleted aliases into a separate tombstone table before cleanup.

## Configuration

Use environment-backed settings:

```text
DATABASE_URL
PUBLIC_BASE_URL
ALIAS_LENGTH=8
ALIAS_MAX_ATTEMPTS=5
REDIRECT_STATUS_CODE=307
DB_POOL_SIZE
DB_MAX_OVERFLOW
LOG_LEVEL
TRUSTED_HOSTS
CORS_ORIGINS
```

Fail during startup when required configuration is missing. `PUBLIC_BASE_URL` must not be derived blindly from untrusted `Host` or forwarded headers.

## Error Model

Return a consistent JSON structure:

```json
{
  "error": {
    "code": "alias_already_exists",
    "message": "The requested alias is unavailable."
  }
}
```

Define domain exceptions such as:

```text
AliasAlreadyExists
AliasGenerationExhausted
LinkNotFound
LinkExpired
InvalidExpiration
ReservedAlias
```

Map them to HTTP responses in centralized FastAPI exception handlers.

## Test Strategy

Unit tests should cover:

- Alias alphabet, length, and secure generation.
- Reserved and malformed aliases.
- URL validation.
- Expiry boundary: `expires_at == now` is expired.
- Collision retry and retry exhaustion.
- Domain exception mapping.

Integration tests should use a real PostgreSQL instance and apply Alembic migrations:

- Create generated and custom aliases.
- Persist data across application instances.
- Redirect returns the exact `Location`.
- Query strings and fragments are preserved in the target.
- Unknown aliases return `404`.
- Expired aliases return `410`.
- Duplicate custom aliases return `409`.
- `HEAD` mirrors redirect behavior.
- Concurrent requests for one custom alias produce exactly one success.
- Forced generated-alias collisions retry correctly.
- Access counters update atomically.
- Readiness fails when the database is unavailable.

Use dependency injection for the clock and alias generator. This avoids sleeps and makes collision tests deterministic.

## Implementation Order

1. Establish settings, application factory, structured logging, and health routes.
2. Add async database lifecycle and SQLAlchemy model.
3. Create and validate the initial Alembic migration.
4. Implement schemas and domain exceptions.
5. Implement alias generation and repository operations.
6. Implement the service layer with collision retries and injected time.
7. Add create and metadata endpoints.
8. Add redirect and `HEAD` behavior.
9. Add unit and PostgreSQL integration tests.
10. Add container packaging, non-root execution, migration deployment, and graceful shutdown.
11. Add request limits, trusted-host handling, metrics, and tracing.
12. Load-test redirects and creation concurrency before setting pool and worker counts.

For deployment, run migrations as a distinct release step, then start multiple FastAPI workers behind a reverse proxy. The API instances remain stateless; PostgreSQL provides persistence and cross-worker consistency.