Use PostgreSQL as the source of truth, SQLAlchemy 2.x with async sessions, and Alembic for schema migrations. Keep redirect resolution in the database so aliases remain correct across multiple application instances. No external cache is required; an optional bounded in-process cache can be added later as a performance optimization, but it must never determine correctness.

**Project layout**

```text
url-shortener/
├─ pyproject.toml
├─ README.md
├─ .env.example
├─ Dockerfile
├─ docker-compose.yml
├─ alembic.ini
├─ migrations/
│  ├─ env.py
│  ├─ script.py.mako
│  └─ versions/
│     └─ 0001_create_short_urls.py
├─ app/
│  ├─ __init__.py
│  ├─ main.py
│  ├─ api/
│  │  ├─ __init__.py
│  │  ├─ deps.py
│  │  └─ routes/
│  │     ├─ __init__.py
│  │     ├─ urls.py
│  │     └─ health.py
│  ├─ core/
│  │  ├─ config.py
│  │  ├─ logging.py
│  │  └─ errors.py
│  ├─ db/
│  │  ├─ session.py
│  │  └─ base.py
│  ├─ models/
│  │  └─ short_url.py
│  ├─ schemas/
│  │  └─ short_url.py
│  ├─ repositories/
│  │  └─ short_url.py
│  ├─ services/
│  │  └─ short_url.py
│  └─ utils/
│     ├─ alias.py
│     └─ time.py
└─ tests/
   ├─ conftest.py
   ├─ unit/
   │  ├─ test_alias.py
   │  └─ test_service.py
   └─ integration/
      ├─ test_create_url.py
      ├─ test_redirect.py
      ├─ test_expiration.py
      └─ test_health.py
```

**Data model**

Table: `short_urls`

- `id`: `BIGINT` or UUID primary key
- `alias`: case-sensitive or normalized `VARCHAR(32)`, unique, indexed
- `target_url`: `TEXT`, validated as HTTP/HTTPS
- `expires_at`: nullable UTC timestamp with timezone
- `created_at`: UTC timestamp
- `updated_at`: UTC timestamp
- `is_active`: boolean default `true`
- `redirect_count`: bigint default `0`

Recommended constraints and indexes:

```sql
UNIQUE (alias)
INDEX (expires_at)
CHECK (char_length(alias) BETWEEN 3 AND 32)
CHECK (target_url <> '')
```

Normalize aliases consistently, for example lowercase and URL-safe characters only. Reserve operational paths such as `docs`, `health`, `metrics`, and `api`.

Expiration is evaluated at read time:

```text
active = is_active AND (expires_at IS NULL OR expires_at > now())
```

Do not rely on a cleanup job for correctness. A periodic job may delete or archive expired rows later.

**API contract**

`POST /api/v1/urls`

Request:

```json
{
  "target_url": "https://example.com/article",
  "alias": "optional-custom-alias",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Response `201 Created`:

```json
{
  "alias": "abc123",
  "short_url": "https://short.example/abc123",
  "target_url": "https://example.com/article",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Behavior:

- Generate a cryptographically random alias when omitted.
- Retry generation on a unique-key conflict.
- Return `409 Conflict` when a requested alias already exists.
- Reject expiration timestamps in the past with `422 Unprocessable Entity`.
- Enforce maximum URL length and alias length through Pydantic settings.

`GET /{alias}`

- Query only active, non-expired records.
- Return `307 Temporary Redirect` or `302 Found`; use `307` if preserving the original method matters.
- Set the `Location` header to `target_url`.
- Return `404` for unknown aliases.
- Return `410 Gone` for known but expired/inactive aliases if that distinction is part of the public contract; otherwise use `404` consistently.
- Increment `redirect_count` with a lightweight atomic SQL update, or emit the metric asynchronously if exact counts are not required.

`DELETE /api/v1/urls/{alias}`

- Mark `is_active = false` rather than deleting immediately.
- Return `204 No Content`.
- Make the endpoint authenticated in production, even if authentication is initially stubbed behind a dependency.

`GET /health/live`

- Process-only liveness check; no database dependency.

`GET /health/ready`

- Execute a short database connectivity query.
- Return `503` when the database is unavailable.

**Layer responsibilities**

`app/main.py`

- Create the FastAPI application.
- Register routers, exception handlers, middleware, and startup/shutdown hooks.
- Configure trusted hosts, proxy headers, and request IDs.

`app/core/config.py`

- Use `pydantic-settings`.
- Include `DATABASE_URL`, `PUBLIC_BASE_URL`, alias length, URL length, redirect status, and environment name.
- Fail fast on missing production configuration.

`app/db/session.py`

- Build the async SQLAlchemy engine and session factory.
- Configure pool size, overflow, timeout, and `pool_pre_ping`.
- Expose a request-scoped `AsyncSession` dependency.

`app/models/short_url.py`

- Define the ORM model and database-level constraints.
- Use timezone-aware `datetime`.

`app/schemas/short_url.py`

- Request/response DTOs.
- Validate URL scheme, size, alias syntax, and expiration semantics.
- Serialize timestamps as ISO-8601 UTC.

`app/repositories/short_url.py`

- Encapsulate SQL queries:
  - `get_by_alias`
  - `create`
  - `deactivate`
  - `increment_redirect_count`
  - optional `get_expired_batch`
- Keep transaction handling explicit.

`app/services/short_url.py`

- Implement alias generation and collision retries.
- Enforce business rules.
- Construct the public short URL from `PUBLIC_BASE_URL`.
- Translate repository conflicts into domain exceptions.

`app/api/routes/urls.py`

- Keep handlers thin.
- Inject the service and session.
- Map domain exceptions to stable HTTP responses.

**Alias generation**

Use a URL-safe alphabet such as lowercase letters and digits. Generate 7–10 characters with `secrets.choice`, not a predictable hash. Collision handling must be database-backed:

1. Generate an alias.
2. Attempt `INSERT`.
3. On unique violation, roll back the failed transaction and retry up to a bounded number of attempts.
4. Return `503` if the system cannot allocate an alias after retries.

Custom aliases should be validated with a strict regex such as:

```text
^[a-z0-9][a-z0-9_-]{2,31}$
```

**Transaction and concurrency rules**

- Creation and deactivation run inside transactions.
- The unique index, not an application-side existence check, arbitrates alias ownership.
- Redirect lookup and expiration filtering happen in one SQL query.
- Use `SELECT ... FOR UPDATE` only where necessary; redirect reads should remain inexpensive.
- Keep migrations backward-compatible and run them before application rollout.

**Testing strategy**

`tests/conftest.py`

- Create an isolated test database or transaction-per-test fixture.
- Override the FastAPI session dependency.
- Use `httpx.AsyncClient` with ASGI transport.

Unit tests:

- Alias format and length.
- Reserved alias rejection.
- URL and expiration validation.
- Retry behavior after simulated unique conflicts.
- Public URL construction.

Integration tests:

- Create with generated alias returns `201`.
- Create with custom alias persists and can be resolved.
- Duplicate custom alias returns `409`.
- Redirect returns the configured status and `Location`.
- Unknown alias returns `404`.
- Expired alias returns the chosen expired response and never redirects.
- Deactivated alias no longer redirects.
- Redirect count increments atomically.
- Readiness endpoint reflects database availability.
- Concurrent creation attempts cannot produce duplicate aliases.

Use PostgreSQL in CI rather than SQLite for behavior that depends on timestamp semantics, unique violations, and production SQL dialects.

**Operational requirements**

- Structured JSON logs with request ID, alias, status code, and latency.
- Metrics for creations, conflicts, redirects, expired lookups, and database errors.
- OpenTelemetry tracing if already used by the platform.
- Timeouts on database operations and HTTP requests.
- Graceful shutdown that stops accepting traffic before disposing the engine.
- Container runs migrations as a release step, not on every worker startup.
- Add rate limiting at the gateway or application layer if the create endpoint is publicly exposed; this does not require a shared cache.

**Dependency baseline**

```text
fastapi
uvicorn[standard]
sqlalchemy[asyncio]
asyncpg
alembic
pydantic-settings
httpx
pytest
pytest-asyncio
```

This structure keeps persistence, expiration, redirect behavior, and test seams explicit while remaining deployable as multiple stateless FastAPI workers backed by one PostgreSQL database.