# Production implementation plan

## 1. Technology choices

- **Python 3.12**
- **FastAPI**
- **Pydantic v2**
- **SQLAlchemy 2.x**, async engine
- **PostgreSQL 16**
- **Alembic** migrations
- **asyncpg** PostgreSQL driver
- **pytest**, `pytest-asyncio`, and `httpx`
- **Uvicorn** locally; multiple Uvicorn workers behind a reverse proxy in production
- **No Redis**: all aliases and expiration data are stored in PostgreSQL

PostgreSQL is the source of truth. No alias state is held only in process memory.

---

## 2. Repository layout

```text
url-shortener/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── logging.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── dependencies.py
│   │   ├── health.py
│   │   └── urls.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── urls.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── url_service.py
│   └── core/
│       ├── __init__.py
│       ├── aliases.py
│       ├── errors.py
│       └── time.py
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 0001_create_short_urls.py
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_aliases.py
│   │   └── test_schemas.py
│   └── integration/
│       ├── test_create_url.py
│       ├── test_redirect.py
│       ├── test_expiration.py
│       └── test_concurrency.py
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 3. Database schema

Table: `short_urls`

```sql
CREATE TABLE short_urls (
    id BIGSERIAL PRIMARY KEY,
    alias VARCHAR(64) NOT NULL,
    destination_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NULL
);

CREATE UNIQUE INDEX uq_short_urls_alias
    ON short_urls (alias);

CREATE INDEX ix_short_urls_expires_at
    ON short_urls (expires_at)
    WHERE expires_at IS NOT NULL;
```

### Schema decisions

- `alias` is globally unique.
- Aliases are normalized to lowercase.
- `expires_at` is nullable:
  - `NULL`: never expires
  - timestamp: unavailable at or after that instant
- All timestamps are timezone-aware UTC values.
- Expiration is enforced during redirect lookup, not only by cleanup.
- An optional cleanup job may later delete expired rows, but correctness must not depend on it.

SQLAlchemy model:

```python
class ShortURL(Base):
    __tablename__ = "short_urls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alias: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
```

---

## 4. Alias rules

### Custom aliases

Accept aliases matching:

```text
^[a-z0-9][a-z0-9_-]{2,63}$
```

Rules:

- 3–64 characters
- Lowercase only after normalization
- Allowed characters: lowercase letters, digits, `_`, `-`
- Reject reserved routes such as:
  - `api`
  - `docs`
  - `redoc`
  - `openapi.json`
  - `healthz`
  - `readyz`

A custom alias collision returns `409 Conflict`.

### Generated aliases

Generate cryptographically random aliases using `secrets`, for example 8 characters from:

```text
abcdefghijklmnopqrstuvwxyz0123456789
```

Insertion must be attempted under the database unique constraint. On a uniqueness conflict, roll back that attempt and retry a bounded number of times, such as five.

Do not check availability and insert as separate correctness steps; concurrent requests can race.

---

## 5. API contract

### Create a short URL

```http
POST /api/v1/urls
Content-Type: application/json
```

Request:

```json
{
  "destination_url": "https://example.com/products/42",
  "alias": "product-42",
  "expires_at": "2030-01-01T00:00:00Z"
}
```

Fields:

- `destination_url`: required absolute `http` or `https` URL
- `alias`: optional custom alias
- `expires_at`: optional timezone-aware future timestamp

Response: `201 Created`

```json
{
  "alias": "product-42",
  "short_url": "https://short.example/product-42",
  "destination_url": "https://example.com/products/42",
  "created_at": "2025-01-01T12:00:00Z",
  "expires_at": "2030-01-01T00:00:00Z"
}
```

Headers:

```http
Location: https://short.example/product-42
```

Validation responses:

- `422 Unprocessable Entity`: malformed URL, invalid alias, naive timestamp, or expiration in the past
- `409 Conflict`: requested alias already exists
- `500`: unexpected database or infrastructure failure, without exposing internals

### Redirect

```http
GET /{alias}
```

Behavior:

- Existing and unexpired alias: `307 Temporary Redirect`
- Missing alias: `404 Not Found`
- Expired alias: `410 Gone`

The redirect response must include:

```http
Location: https://example.com/products/42
```

Use `307` so the original HTTP method semantics are preserved. Add a `HEAD /{alias}` route with identical lookup behavior and no response body.

Do not use a client-controlled redirect status.

### Health endpoints

```http
GET /healthz
GET /readyz
```

- `/healthz`: process is running
- `/readyz`: database connection succeeds

These endpoints should not expose database credentials or internal error details.

---

## 6. Pydantic schemas

```python
class CreateURLRequest(BaseModel):
    destination_url: AnyHttpUrl
    alias: str | None = None
    expires_at: AwareDatetime | None = None
```

Validation requirements:

- Permit only `http` and `https` schemes.
- Reject credentials in destination URLs unless explicitly supported.
- Enforce a maximum destination URL length, such as 2,048 or 8,192 characters.
- Normalize aliases using `casefold()` and validate the normalized value.
- Compare expiration against an injected UTC clock.
- Reject `expires_at <= now`.

Response schema:

```python
class CreateURLResponse(BaseModel):
    alias: str
    short_url: AnyHttpUrl
    destination_url: AnyHttpUrl
    created_at: datetime
    expires_at: datetime | None
```

---

## 7. Service-layer behavior

`app/services/url_service.py` should contain the transactional logic.

### Creation flow

1. Validate and normalize the request.
2. If a custom alias was supplied:
   - Start a transaction.
   - Insert the row.
   - Convert unique-constraint failure into `409 Conflict`.
3. If no alias was supplied:
   - Generate a random alias.
   - Insert it.
   - Retry on unique-constraint conflict.
4. Commit the transaction.
5. Construct the public short URL from configuration.
6. Return the persisted values.

### Redirect flow

Use one query with expiration filtering:

```sql
SELECT alias, destination_url
FROM short_urls
WHERE alias = :alias
  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);
```

If no row is returned, distinguish between missing and expired only if the API intentionally exposes `410`; otherwise use a second existence check. The simpler public behavior is to return `404` for both missing and expired aliases. If `410` is required, perform:

1. Lookup active row.
2. If absent, lookup alias existence.
3. Return `410` when the row exists but is expired.

Use the database clock for the expiration predicate to avoid differences between application servers.

---

## 8. Application setup

`app/main.py` should:

- Create the FastAPI application.
- Register URL and health routers.
- Register exception handlers for:
  - validation errors
  - alias conflicts
  - not found
  - expired aliases
- Configure structured logging.
- Avoid creating database connections at import time.
- Close the engine during application shutdown.

`app/config.py` should load:

```text
DATABASE_URL
PUBLIC_BASE_URL
ENVIRONMENT
LOG_LEVEL
```

Example:

```text
DATABASE_URL=postgresql+asyncpg://shortener:secret@db:5432/shortener
PUBLIC_BASE_URL=https://short.example
```

`PUBLIC_BASE_URL` must be configured rather than inferred from an untrusted request header.

---

## 9. Security and reliability requirements

- Allow only `http` and `https` destinations.
- Do not fetch, resolve, or proxy destination URLs server-side.
- Set strict maximum request and URL lengths.
- Use parameterized SQL through SQLAlchemy.
- Do not expose raw database exceptions.
- Configure connection-pool size and overflow limits.
- Add request IDs and structured logs.
- Log creation, conflict, redirect, expiration, and database failures without logging sensitive request data unnecessarily.
- Apply rate limiting at the reverse proxy or API gateway because no in-memory or Redis limiter is available.
- Set appropriate proxy headers only from trusted proxies.
- Run database migrations before serving traffic.
- Use multiple application workers only after confirming database pool sizing.

---

## 10. Testing plan

### Unit tests

`test_aliases.py`

- Valid custom aliases
- Invalid characters
- Too-short and too-long aliases
- Case normalization
- Reserved aliases
- Generated alias length and alphabet
- Generated aliases do not contain ambiguous unsupported characters, if applicable

`test_schemas.py`

- Valid HTTP and HTTPS URLs
- Rejection of unsupported schemes
- URL length limit
- Valid future expiration
- Rejection of past expiration
- Rejection of naive timestamps

### Integration tests

`test_create_url.py`

- Create with generated alias
- Create with custom alias
- Response contains correct short URL
- `Location` header is correct
- Duplicate custom alias returns `409`
- Data remains available after a new application session
- Database rollback does not leave partial rows

`test_redirect.py`

- Existing alias returns `307`
- `Location` matches the stored destination
- `HEAD` returns redirect headers without a body
- Unknown alias returns `404`
- Reserved routes still reach their intended API or health handlers
- Destination URL is never fetched by the service

`test_expiration.py`

- Non-expiring alias redirects
- Future expiration redirects
- Alias at the exact expiration instant is unavailable
- Expired alias returns the documented status
- Database timestamps are handled as UTC
- Expired rows remain harmless even before cleanup

`test_concurrency.py`

- Concurrent requests for the same custom alias produce one success and conflicts for the rest
- Concurrent generated-alias creation produces unique aliases
- No duplicate aliases exist after concurrent creation

### Test infrastructure

- Run integration tests against PostgreSQL, preferably with a disposable test database.
- Apply Alembic migrations before tests.
- Roll back or recreate database state between tests.
- Do not use SQLite for database behavior tests because PostgreSQL uniqueness, timestamp, and transaction semantics are important.

---

## 11. Deployment and operations

### Dockerfile

- Use a slim Python base image.
- Install locked dependencies.
- Run as a non-root user.
- Start with:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker Compose

Provide PostgreSQL for local development:

- `db`: PostgreSQL
- `api`: FastAPI application
- Persistent database volume
- Health check for PostgreSQL
- API readiness dependent on successful database connectivity

### Production startup

1. Provision PostgreSQL with backups and monitoring.
2. Apply Alembic migrations.
3. Start the API behind TLS termination and a reverse proxy.
4. Configure `PUBLIC_BASE_URL`.
5. Monitor:
   - request count and latency
   - redirect status distribution
   - creation conflicts
   - database pool exhaustion
   - readiness failures
   - expiration volume

A periodic PostgreSQL cleanup task may remove expired rows:

```sql
DELETE FROM short_urls
WHERE expires_at IS NOT NULL
  AND expires_at <= CURRENT_TIMESTAMP;
```

This is strictly an operational optimization; redirect correctness remains enforced by the lookup query.
