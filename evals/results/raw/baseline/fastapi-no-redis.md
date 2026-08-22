I’ll structure this as an implementation specification: runtime boundaries, repository layout, database contract, endpoint semantics, concurrency behavior, and a verification matrix. The key decisions will be explicit enough that implementation can proceed without inventing behavior later.
**Architecture**

- FastAPI application with async SQLAlchemy 2.x and PostgreSQL.
- `asyncpg` database driver.
- Alembic for schema migrations.
- No Redis or other external cache. Redirects always read from PostgreSQL.
- Stateless application instances so the service can scale horizontally.
- Reverse proxy or load balancer terminates TLS and provides rate limiting.
- Expiration enforcement happens during redirect lookup; cleanup is only an operational optimization.

**Project Layout**

```text
url-shortener/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── router.py
│   │   └── routes/
│   │       ├── health.py
│   │       ├── links.py
│   │       └── redirects.py
│   ├── core/
│   │   ├── config.py
│   │   ├── errors.py
│   │   └── logging.py
│   ├── db/
│   │   ├── base.py
│   │   ├── models.py
│   │   └── session.py
│   ├── repositories/
│   │   └── links.py
│   ├── schemas/
│   │   ├── errors.py
│   │   └── links.py
│   ├── services/
│   │   ├── alias_generator.py
│   │   ├── link_service.py
│   │   └── redirect_service.py
│   └── dependencies.py
├── alembic/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_alias_generator.py
│   │   ├── test_link_service.py
│   │   └── test_schemas.py
│   ├── integration/
│   │   ├── test_links_api.py
│   │   ├── test_redirects_api.py
│   │   └── test_expiration.py
│   └── contract/
│       └── test_openapi.py
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── README.md
```

**Database Schema**

Use a single immutable `links` table:

```sql
CREATE TABLE links (
    id UUID PRIMARY KEY,
    alias VARCHAR(32) NOT NULL,
    target_url TEXT NOT NULL,
    expires_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_links_alias ON links (alias);

CREATE INDEX ix_links_expires_at
    ON links (expires_at)
    WHERE expires_at IS NOT NULL;
```

Design decisions:

- Store aliases normalized to lowercase.
- Alias uniqueness is case-insensitive by normalization.
- Never recycle an alias after expiration.
- `expires_at IS NULL` means no expiration.
- Store all timestamps in UTC as timezone-aware PostgreSQL timestamps.
- Use UUIDs for internal IDs; the ID is never exposed as the short URL.
- Add a database check constraint for the alias format if desired:

```sql
CHECK (alias ~ '^[a-z0-9_-]{3,32}$')
```

The application should not update `target_url`, `alias`, or `expires_at` after creation unless an authenticated management API is explicitly added later.

**Configuration**

Use `pydantic-settings` with environment variables:

```text
DATABASE_URL=postgresql+asyncpg://user:password@db:5432/shortener
PUBLIC_BASE_URL=https://sho.rt
ALIAS_LENGTH=8
MAX_ALIAS_LENGTH=32
MAX_TARGET_URL_LENGTH=2048
MAX_TTL_SECONDS=31536000
LOG_LEVEL=INFO
```

Configuration should validate:

- `PUBLIC_BASE_URL` uses HTTPS in production.
- `MAX_TTL_SECONDS` is positive.
- Database URLs are present and valid.
- Target URL and alias length limits are bounded.

**API Contract**

`POST /v1/links`

Request:

```json
{
  "target_url": "https://example.com/articles/42",
  "alias": "article-42",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Fields:

- `target_url`: required absolute `http` or `https` URL.
- `alias`: optional custom alias.
- `expires_at`: optional timezone-aware RFC 3339 timestamp.

Response `201 Created`:

```json
{
  "alias": "article-42",
  "target_url": "https://example.com/articles/42",
  "short_url": "https://sho.rt/article-42",
  "expires_at": "2026-12-31T23:59:59Z",
  "created_at": "2025-01-01T12:00:00Z"
}
```

Headers:

```text
Location: https://sho.rt/article-42
```

Validation behavior:

- Reject non-HTTP schemes with `422 Unprocessable Entity`.
- Reject malformed, relative, credential-bearing, or overlong URLs.
- Reject naive `expires_at` values.
- Reject expiration at or before creation time.
- Reject expiration beyond `MAX_TTL_SECONDS`.
- Reject reserved aliases such as `v1`, `healthz`, `readyz`, `docs`, `redoc`, and `openapi.json`.
- Return `409 Conflict` when a requested alias already exists.
- Generate an alias when one is not supplied.

`GET /{alias}`

- Return `307 Temporary Redirect` with the stored target URL.
- A `307` preserves the original HTTP method and request body.
- Use `Cache-Control: no-store` by default so clients and intermediary caches do not retain expired redirects.
- Add an optional configurable `max-age` only if expiration-aware cache behavior is implemented carefully.

Responses:

- `307` when the alias exists and is active.
- `404` when the alias does not exist.
- `410 Gone` when the alias exists but has expired.
- `422` for malformed aliases.

`HEAD /{alias}`

- Match `GET /{alias}` status behavior without a response body.
- Return the same `Location` header for active aliases.

`GET /healthz`

- Liveness endpoint.
- Does not access the database.

`GET /readyz`

- Readiness endpoint.
- Executes a lightweight database query such as `SELECT 1`.
- Returns `503` when the database is unavailable.

**Alias Generation**

`alias_generator.py` should:

- Generate cryptographically random aliases using `secrets`.
- Use a lowercase base62 or lowercase alphanumeric alphabet.
- Default to eight characters.
- Avoid reserved aliases.
- Retry on database unique-constraint conflicts.
- Limit retries, for example to five attempts, then return `503 Service Unavailable`.

Generated aliases must be checked through the database unique index. An application-side existence check alone is insufficient because concurrent requests can race.

**Creation Flow**

`link_service.py` should implement:

1. Validate and normalize the request through Pydantic schemas.
2. Generate a candidate alias if the caller did not provide one.
3. Insert the link in a transaction.
4. Catch the PostgreSQL unique-constraint error for `alias`.
5. Retry only generated aliases after a collision.
6. Return the committed record and generated public URL.

For custom aliases, a uniqueness conflict should immediately return `409`.

Do not perform a separate “does alias exist?” query before insertion. The unique index is the concurrency control mechanism.

**Redirect Flow**

`redirect_service.py` should:

1. Validate the alias format before querying.
2. Load the link by its unique alias.
3. Return `404` if no row exists.
4. Compare `expires_at` against the current UTC time.
5. Return `410` when `expires_at <= now`.
6. Return `307` and the target URL otherwise.

Expiration must be enforced in application behavior even if the expired row remains in the database. Redirect correctness must not depend on a cleanup job.

An alternative database query can fetch the row with:

```sql
SELECT *
FROM links
WHERE alias = :alias;
```

Then perform the expiration check using an injected clock. Injecting a clock makes expiration tests deterministic.

**Error Format**

Use a consistent JSON error shape:

```json
{
  "type": "https://sho.rt/errors/alias-conflict",
  "title": "Alias already exists",
  "status": 409,
  "detail": "The requested alias is already assigned.",
  "instance": "/v1/links"
}
```

Define typed application exceptions for:

- Invalid target URL.
- Invalid alias.
- Reserved alias.
- Alias conflict.
- Link not found.
- Link expired.
- Database unavailable.

Do not expose raw database exceptions or connection details.

**Repository Boundary**

`repositories/links.py` should contain database operations only:

- `insert_link(...)`
- `get_by_alias(...)`
- `delete_expired_links_before(...)` for maintenance
- Optional `count_expiring_links(...)` for operational metrics

The repository should not construct HTTP responses or decide status codes. Services translate repository results into domain behavior, and routes translate domain behavior into HTTP responses.

**Cleanup**

Expiration cleanup is optional and must not be required for correctness.

Run a scheduled job, Kubernetes CronJob, or database maintenance task periodically:

```sql
DELETE FROM links
WHERE expires_at IS NOT NULL
  AND expires_at < CURRENT_TIMESTAMP - INTERVAL '30 days';
```

The retention period preserves expired aliases and their records for a defined period. If aliases must remain permanently reserved, use a separate `retired_aliases` table before deleting link data:

```sql
CREATE TABLE retired_aliases (
    alias VARCHAR(32) PRIMARY KEY,
    retired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

For the simplest persistent-alias policy, retain expired rows indefinitely and avoid the extra table.

**Security and Operations**

- Enforce HTTPS at the edge.
- Set maximum request body and URL lengths.
- Reject URLs containing credentials such as `https://user:pass@example.com`.
- Do not fetch or validate the target URL server-side; redirects must not create SSRF behavior.
- Apply rate limits to link creation at the reverse proxy or API gateway.
- Add structured logs containing request ID, alias, status code, and latency.
- Do not log full target URLs if they may contain sensitive query parameters.
- Expose metrics for creation count, alias conflicts, redirects, 404s, 410s, database latency, and database errors.
- Use connection pooling with bounded pool size and overflow.
- Handle graceful shutdown so in-flight requests complete before the process exits.
- Run migrations as a deployment step before serving traffic.
- Use separate database credentials for migrations and runtime access where practical.

**Testing Strategy**

Use PostgreSQL for integration tests rather than SQLite. SQLite differs in timestamp behavior, constraints, and concurrency semantics.

Fixtures should provide:

- A disposable PostgreSQL database, preferably through Testcontainers.
- An async SQLAlchemy session per test.
- A FastAPI app configured with test settings.
- An injected controllable clock.
- An `httpx.AsyncClient` using the ASGI transport.

Unit tests:

- Valid HTTP and HTTPS targets are accepted.
- Relative URLs and unsupported schemes are rejected.
- Credential-bearing URLs are rejected.
- Naive expiration timestamps are rejected.
- Past and current expiration timestamps are rejected.
- Maximum TTL is enforced.
- Aliases are lowercased.
- Invalid alias characters and lengths are rejected.
- Reserved aliases are rejected.
- Generated aliases have the expected format and length.
- Alias generation retries after a collision.

Integration tests:

- Creating a link persists it in PostgreSQL.
- Creating the same custom alias twice returns `409`.
- Two concurrent creations with the same custom alias result in exactly one success.
- Generated aliases are unique across many creations.
- Active aliases return `307` and the exact `Location`.
- `HEAD` returns the correct status and `Location` without a body.
- Missing aliases return `404`.
- Expired aliases return `410`.
- An alias expiring exactly at the current clock time is treated as expired.
- Non-expiring aliases continue redirecting.
- Expired links remain reserved and cannot be recreated.
- Database failures produce a controlled `503` response.
- Health and readiness endpoints have distinct database behavior.
- OpenAPI contains the documented request, response, and error schemas.

Concurrency tests should use separate database sessions and simultaneous requests. They must verify the database constraint, not just application-level behavior.

Contract tests should assert:

- `201`, `307`, `404`, `409`, `410`, `422`, and `503` response shapes.
- `Location` header behavior.
- No redirect response body is unexpectedly generated.
- OpenAPI remains compatible with the published API.

**Verification and Delivery Gates**

CI should run:

1. Formatting and linting with Ruff.
2. Type checking with mypy or Pyright.
3. Unit tests.
4. PostgreSQL integration tests.
5. Migration upgrade tests from an empty database.
6. OpenAPI contract tests.
7. Container build validation.

Before production deployment, verify:

- A new database can be migrated from zero.
- Multiple application instances can create links concurrently.
- Restarting the application does not lose aliases.
- Expired links return `410` after restart.
- Database outages produce readiness failure without corrupting data.
- Reverse-proxy rate limits and HTTPS redirects are active.
- Logs, metrics, and database connection health are observable.
