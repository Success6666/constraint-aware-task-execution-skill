## Architecture

Use FastAPI with PostgreSQL as the source of truth. Redis is unnecessary because alias lookup is a single indexed database query and correctness must remain database-backed.

- FastAPI for HTTP/API behavior
- SQLAlchemy 2.x async ORM
- `asyncpg` PostgreSQL driver
- Alembic migrations
- Pydantic Settings for configuration
- `pytest`, `pytest-asyncio`, and `httpx` for tests
- Uvicorn or Gunicorn with Uvicorn workers for deployment
- Structured logging and request IDs
- Optional reverse-proxy rate limiting through Nginx, Envoy, or a cloud load balancer

SQLite may be supported for local development, but production tests and deployment should use PostgreSQL.

## Suggested File Layout

```text
url-shortener/
├── pyproject.toml
├── README.md
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 0001_create_links.py
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py
│   │   └── routes/
│   │       ├── health.py
│   │       └── links.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── security.py
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       └── link.py
│   ├── schemas/
│   │   └── link.py
│   ├── repositories/
│   │   └── link.py
│   ├── services/
│   │   └── link_service.py
│   └── errors.py
└── tests/
    ├── conftest.py
    ├── unit/
    │   └── test_link_service.py
    ├── integration/
    │   └── test_link_repository.py
    └── api/
        ├── test_health.py
        └── test_links.py
```

## Data Model

Table: `links`

```text
id             BIGINT or UUID primary key
alias          VARCHAR(32) not null unique
target_url     TEXT not null
created_at     TIMESTAMPTZ not null default now()
expires_at     TIMESTAMPTZ null
disabled_at    TIMESTAMPTZ null
click_count    BIGINT not null default 0
```

Constraints and indexes:

- Unique constraint on `alias`
- Check that `expires_at IS NULL OR expires_at > created_at`
- Index on `alias`
- Optional partial index for active links:

```sql
CREATE INDEX ix_links_active_alias
ON links(alias)
WHERE disabled_at IS NULL;
```

A redirect is valid only when:

```sql
disabled_at IS NULL
AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
```

Use UTC timestamps exclusively.

## API Contract

### Create a link

`POST /v1/links`

Request:

```json
{
  "url": "https://example.com/article",
  "alias": "optional-custom-name",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Response: `201 Created`

```json
{
  "alias": "optional-custom-name",
  "short_url": "https://sho.rt/optional-custom-name",
  "target_url": "https://example.com/article",
  "created_at": "2026-08-22T10:00:00Z",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

Behavior:

- Generate a cryptographically secure random alias when none is supplied.
- Validate aliases with something like `[a-zA-Z0-9_-]{3,32}`.
- Reject reserved aliases such as `api`, `health`, `docs`, and `openapi.json`.
- Accept only `http` and `https` URLs.
- Normalize only what is safe; do not rewrite the target URL in a way that changes its meaning.
- Return `409 Conflict` for an alias collision.
- Return `422 Unprocessable Entity` for invalid URLs, aliases, or expiration values.

### Redirect

`GET /{alias}`

Behavior:

1. Query by alias using the active-link predicate.
2. Return `404 Not Found` for unknown, disabled, or expired aliases.
3. Return `307 Temporary Redirect` by default, or `301` if permanent redirects are explicitly part of the product contract.
4. Set the `Location` header to `target_url`.
5. Increment `click_count` asynchronously or with a separate best-effort update so redirect latency is not dominated by analytics.

Avoid following redirects inside the service.

### Disable a link

`DELETE /v1/links/{alias}`

Behavior:

- Soft-delete by setting `disabled_at`.
- Return `204 No Content`.
- Return `404` when the alias does not exist.
- Protect this endpoint with authentication or an owner token; do not expose unauthenticated deletion.

### Health endpoints

- `GET /health/live`: process is running
- `GET /health/ready`: process and database are ready

The readiness check should execute a lightweight database query with a short timeout.

## Service and Repository Responsibilities

`repositories/link.py`

- `get_active_by_alias(alias)`
- `get_by_alias(alias)`
- `insert(link)`
- `disable(alias)`
- `increment_click_count(link_id)`

`services/link_service.py`

- Alias generation and collision retries
- Validation and reserved-name checks
- Expiration policy
- Translation of database errors into domain exceptions
- Construction of public short URLs

Keep HTTP concerns in route modules and business rules in the service layer.

## Alias Generation and Concurrency

Generate aliases using `secrets` and a Base62 alphabet.

Recommended flow:

1. Generate a candidate alias.
2. Insert inside a transaction.
3. Catch the database unique-constraint violation.
4. Retry a bounded number of times, for example five.
5. Return `503` only if collision retries are exhausted.

Do not perform a separate “check then insert”; it is race-prone. PostgreSQL’s unique constraint is the authority.

Custom aliases should fail immediately with `409` on collision.

## Expiration Semantics

- `expires_at = null` means no expiration.
- Expiration is exclusive: a link is invalid when `expires_at <= now()`.
- Enforce the condition during lookup, not only in application code.
- A periodic cleanup job may permanently delete old expired rows, but cleanup is optional and must not determine redirect correctness.
- If expiration is user-controlled, enforce a maximum lifetime through configuration.

## Security and Operational Requirements

- Restrict redirect schemes to `http` and `https`.
- Set maximum URL and request-body sizes.
- Add trusted-host and CORS configuration explicitly.
- Never log authorization headers or full sensitive query strings.
- Add request IDs and structured JSON logs.
- Expose metrics for creation, collisions, redirects, expired links, and 404s.
- Configure database pool size, statement timeout, and connection timeout.
- Run migrations as a deployment step, not on every application startup.
- Use a reverse proxy for TLS termination, request limits, and rate limiting.
- If abuse prevention is required, implement it at the edge or with a separate durable database-backed mechanism; do not pretend an in-process counter is distributed.

## Testing Strategy

### Unit tests

Cover:

- URL validation
- Alias validation
- Reserved aliases
- Random alias length and alphabet
- Expiration boundary behavior
- Short URL construction
- Collision retry behavior
- Mapping repository/database errors to domain errors

### Repository integration tests

Run against PostgreSQL, preferably with Testcontainers:

- Insert and retrieve a link
- Unique alias enforcement
- Expired links excluded from active lookup
- Disabled links excluded from active lookup
- Concurrent inserts with the same alias
- Atomic click-count updates

### API tests

Using `httpx.AsyncClient`:

- `POST /v1/links` returns `201`
- Generated and custom aliases
- Invalid URL and expiration return `422`
- Alias collision returns `409`
- Redirect returns the expected status and `Location`
- Expired and disabled aliases return `404`
- Delete endpoint authorization and idempotency
- Health and readiness behavior
- Ensure redirect tests use `follow_redirects=False`

### End-to-end checks

In CI:

1. Start PostgreSQL.
2. Apply Alembic migrations.
3. Run unit, integration, and API tests.
4. Run formatting, linting, and type checks.
5. Build the container image.

Recommended tooling:

```text
ruff
mypy
pytest
pytest-asyncio
httpx
alembic
```

## Implementation Sequence

1. Add configuration, logging, and database session management.
2. Create the `links` model and initial Alembic migration.
3. Implement Pydantic request/response schemas.
4. Implement repository methods with active-link predicates.
5. Implement the service layer, including secure alias generation and retries.
6. Add creation, redirect, deletion, and health routes.
7. Add centralized exception handlers and request logging.
8. Add unit and PostgreSQL integration tests.
9. Add Docker and CI configuration.
10. Load-test alias creation and redirects, then tune PostgreSQL indexes and pool settings.

This structure keeps persistence, business rules, and HTTP behavior separate while ensuring expiration, alias uniqueness, and redirect correctness are enforced by the database-backed design.