Use FastAPI with PostgreSQL as the durable source of truth, SQLAlchemy 2.x for persistence, Alembic for migrations, and pytest against an isolated PostgreSQL test database.

**File Layout**

```text
url-shortener/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── dependencies.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── aliases.py
│   │   └── redirects.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── models.py
│   │   └── session.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── aliases.py
│   ├── repositories/
│   │   ├── __init__.py
│   │   └── aliases.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── aliases.py
│   └── domain/
│       ├── __init__.py
│       ├── exceptions.py
│       └── short_codes.py
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 0001_create_aliases.py
├── tests/
│   ├── conftest.py
│   ├── factories.py
│   ├── integration/
│   │   ├── test_alias_api.py
│   │   ├── test_redirects.py
│   │   └── test_alias_repository.py
│   └── unit/
│       ├── test_alias_service.py
│       └── test_short_codes.py
├── alembic.ini
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── .env.example
└── README.md
```

**Data Model**

`aliases` table:

| Column | Type | Rules |
|---|---|---|
| `id` | UUID | Primary key |
| `code` | VARCHAR(32) | Unique, indexed, case-sensitive |
| `target_url` | TEXT | Required |
| `created_at` | TIMESTAMPTZ | Server-generated |
| `expires_at` | TIMESTAMPTZ | Nullable, indexed |
| `disabled_at` | TIMESTAMPTZ | Nullable |
| `redirect_count` | BIGINT | Default `0` |
| `last_accessed_at` | TIMESTAMPTZ | Nullable |

Important database rules:

- Unique constraint on `code` handles concurrent alias creation safely.
- Check constraint ensures `expires_at IS NULL OR expires_at > created_at`.
- Store all timestamps in UTC.
- Keep expired rows for consistent `410 Gone` behavior and future auditing.
- Treat `target_url` as immutable initially; changing destinations introduces security and cache-consistency concerns.

Click counting is useful but should not compromise redirects. Update it with a single atomic statement such as `redirect_count = redirect_count + 1`. For high traffic, move analytics to an event pipeline later while PostgreSQL remains authoritative for aliases.

**HTTP API**

```text
POST   /api/v1/aliases
GET    /api/v1/aliases/{code}
DELETE /api/v1/aliases/{code}
GET    /{code}
GET    /health/live
GET    /health/ready
```

Create request:

```json
{
  "target_url": "https://example.com/articles/42",
  "custom_code": "article-42",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Create response, `201 Created`:

```json
{
  "code": "article-42",
  "short_url": "https://sho.rt/article-42",
  "target_url": "https://example.com/articles/42",
  "created_at": "2026-08-16T08:00:00Z",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Behavior:

- Omitted `custom_code`: generate a cryptographically random Base62 code, initially 8 characters.
- Duplicate custom code: `409 Conflict`.
- Invalid or past expiration: `422 Unprocessable Entity`.
- Unknown code: `404 Not Found`.
- Expired or disabled code: `410 Gone`.
- Active code: `307 Temporary Redirect` to preserve the request method. Use `302` instead if redirects are deliberately limited to browser-style GET requests.
- Deletion performs a soft delete by setting `disabled_at`; return `204 No Content`.
- Do not follow or fetch the target URL during creation. Validate syntax and permitted schemes only, avoiding an unnecessary SSRF path.
- Reject control characters, credentials embedded in URLs, and schemes other than `http` and `https`.
- Reserve operational paths such as `api`, `docs`, `openapi.json`, and `health` from use as codes.

**Module Responsibilities**

`config.py`
- Pydantic Settings configuration.
- Database URL, public base URL, code length, SQL logging, environment, and trusted proxy settings.

`db/session.py`
- Async SQLAlchemy engine and `async_sessionmaker`.
- Request-scoped transaction lifecycle.
- Pool sizing and connection health checks.

`api/aliases.py`
- Request parsing and response mapping.
- No persistence logic.

`api/redirects.py`
- Resolve the code, map domain states to HTTP responses, and return `RedirectResponse`.
- Register this catch-all route after `/api` and health routes.

`repositories/aliases.py`
- Database queries and atomic counter updates.
- Translate unique constraint violations into a repository conflict result.

`services/aliases.py`
- Creation, code generation, collision retry, expiration checks, and disable behavior.
- Accept an injectable clock so expiration tests remain deterministic.

`domain/short_codes.py`
- Code normalization policy, validation, reserved-name checks, and secure random generation.
- Preserve case consistently; avoid silently lowercasing user aliases unless the product explicitly defines aliases as case-insensitive.

`domain/exceptions.py`
- Typed errors such as `AliasNotFound`, `AliasExpired`, `AliasConflict`, and `AliasDisabled`.

**Creation Algorithm**

1. Validate and normalize the target URL without modifying its query string or fragment.
2. Validate `expires_at` against the injected current time.
3. If a custom code is supplied, validate it and attempt one insert.
4. Otherwise, generate a random code and insert it.
5. On a unique constraint violation for a generated code, retry up to five times.
6. Return the committed row and construct `short_url` from the configured public base URL.
7. If all generated-code attempts collide, return `503 Service Unavailable` and log the event.

The database constraint, rather than an application-level “check then insert,” is what prevents races.

**Application Lifecycle**

`main.py` should use an application factory:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    ...
```

This makes tests independent of process-global configuration. Startup should verify configuration but should not run migrations automatically. Deploy migrations as a distinct release step before starting new application instances.

Readiness should execute a lightweight database query. Liveness should only confirm that the process event loop is responsive.

**Testing Strategy**

Unit tests:

- Generated codes use the allowed alphabet and configured length.
- Custom-code validation covers length, reserved names, and invalid characters.
- Expiration at the exact boundary is treated consistently as expired.
- Generated collision retries work and have a bounded failure path.
- URL validation accepts HTTP(S) URLs and rejects dangerous schemes.
- Service behavior uses a fixed clock.

Integration tests with real PostgreSQL:

- Create generated and custom aliases.
- Reject duplicate codes.
- Concurrent requests for the same custom code produce one success and conflicts for the rest.
- Active aliases redirect with the expected status and `Location`.
- Unknown aliases return `404`.
- Expired and disabled aliases return `410`.
- Redirect count increments atomically.
- Database state persists across app instances.
- Transaction rollback isolates each test.
- API responses do not expose internal IDs or database errors.

Avoid substituting SQLite in persistence tests because its locking, constraint, timestamp, and concurrency behavior differs from PostgreSQL.

**Production Concerns**

- Run behind a reverse proxy with forwarded headers configured only for trusted proxies.
- Add structured JSON logs with request ID, code, status, and duration; do not log full destination URLs if they may contain sensitive query parameters.
- Add request-body limits and edge-level rate limiting for alias creation.
- Protect create, inspect, and delete endpoints with authentication; redirects normally remain public.
- Expose Prometheus-compatible metrics for request rates, redirect outcomes, database latency, and generated-code collision retries.
- Set explicit database pool limits and statement timeouts.
- Configure graceful shutdown and container health checks.
- Back up PostgreSQL and regularly verify restores.
- Use a multi-stage Docker build, a non-root runtime user, pinned dependencies, and CI migration checks.

**Implementation Sequence**

1. Establish configuration, application factory, database session, and health endpoints.
2. Add the alias model and initial Alembic migration.
3. Implement schemas, code generation, repository operations, and service rules.
4. Add create, inspect, disable, and redirect routes.
5. Add deterministic unit tests and PostgreSQL integration fixtures.
6. Add concurrency and expiration-boundary tests.
7. Containerize the application and database for local development.
8. Add CI steps for formatting, linting, type checking, tests, and migration validation.
9. Add authentication, operational metrics, structured logging, and deployment configuration.
10. Load-test the redirect path and tune database pooling before production release.

A suitable initial dependency set is FastAPI, Uvicorn, SQLAlchemy 2.x, asyncpg, Alembic, Pydantic Settings, pytest, pytest-asyncio, HTTPX, Ruff, and mypy.