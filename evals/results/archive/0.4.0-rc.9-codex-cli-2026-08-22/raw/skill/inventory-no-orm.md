Implement the API with FastAPI, `psycopg` 3’s async driver, and explicit SQL. Keep database access in a small repository layer; do not introduce model mapping or ORM abstractions.

### Database schema

```sql
CREATE TABLE products (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stock_adjustments (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES products(id),
    delta           INTEGER NOT NULL CHECK (delta <> 0),
    quantity_before INTEGER NOT NULL,
    quantity_after  INTEGER NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX stock_adjustments_product_created_idx
    ON stock_adjustments(product_id, created_at DESC, id DESC);

CREATE INDEX products_created_idx
    ON products(created_at DESC, id DESC);
```

The `products.quantity` column is the current balance. `stock_adjustments` provides an audit trail.

### Connection management

```python
# app/db.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from psycopg_pool import AsyncConnectionPool
from fastapi import FastAPI, Request

pool = AsyncConnectionPool(
    conninfo="postgresql://inventory:inventory@localhost/inventory",
    min_size=2,
    max_size=10,
    open=False,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    try:
        yield
    finally:
        await pool.close()

async def connection(request: Request) -> AsyncIterator:
    async with pool.connection() as conn:
        yield conn
```

Configure the pool through environment variables and inject `connection` with `Depends`.

### API surface

```text
POST   /products
GET    /products?limit=50&cursor=...
GET    /products/{product_id}
POST   /products/{product_id}/adjustments
GET    /products/{product_id}/adjustments?limit=50&cursor=...
```

Suggested payloads:

```json
POST /products
{
  "sku": "SKU-100",
  "name": "Keyboard",
  "quantity": 25
}
```

```json
POST /products/1/adjustments
{
  "delta": -3,
  "reason": "Order shipment"
}
```

Return `409 Conflict` when an adjustment would make stock negative, `404` for unknown products, `422` for invalid request data, and `201` for successful creation.

### Pagination

Use keyset pagination rather than offset pagination. Return an opaque cursor containing the final row’s `(created_at, id)`.

```sql
SELECT id, sku, name, quantity, created_at
FROM products
WHERE ($1::timestamptz IS NULL)
   OR (created_at, id) < ($1, $2)
ORDER BY created_at DESC, id DESC
LIMIT $3;
```

The cursor can be URL-safe base64-encoded JSON:

```json
{"created_at":"2026-08-22T10:20:30.123Z","id":42}
```

Response shape:

```json
{
  "items": [],
  "next_cursor": "eyJjcmVhdGVkX2F0Ijoi..."}
```

Always cap `limit` (for example, `1..100`).

### Transactional stock adjustment

The adjustment and balance update must happen in one transaction. Lock the product row before checking or changing quantity.

```python
# app/repositories/products.py
from psycopg import AsyncConnection
from fastapi import HTTPException

async def adjust_stock(
    conn: AsyncConnection,
    product_id: int,
    delta: int,
    reason: str | None,
) -> dict:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, quantity
                FROM products
                WHERE id = %s
                FOR UPDATE
                """,
                (product_id,),
            )
            product = await cur.fetchone()

            if product is None:
                raise HTTPException(404, "Product not found")

            quantity_before = product[1]
            quantity_after = quantity_before + delta

            if quantity_after < 0:
                raise HTTPException(409, "Insufficient stock")

            await cur.execute(
                """
                UPDATE products
                SET quantity = %s
                WHERE id = %s
                """,
                (quantity_after, product_id),
            )

            await cur.execute(
                """
                INSERT INTO stock_adjustments
                    (product_id, delta, quantity_before, quantity_after, reason)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, product_id, delta,
                          quantity_before, quantity_after, reason, created_at
                """,
                (
                    product_id,
                    delta,
                    quantity_before,
                    quantity_after,
                    reason,
                ),
            )
            row = await cur.fetchone()

    return {
        "id": row[0],
        "product_id": row[1],
        "delta": row[2],
        "quantity_before": row[3],
        "quantity_after": row[4],
        "reason": row[5],
        "created_at": row[6],
    }
```

`SELECT ... FOR UPDATE` serializes concurrent adjustments for the same product. Keep the transaction short and avoid network calls inside it.

### FastAPI routes

```python
# app/main.py
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection

@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    try:
        yield
    finally:
        await pool.close()

app = FastAPI(lifespan=lifespan)

@app.post("/products/{product_id}/adjustments", status_code=201)
async def create_adjustment(
    product_id: int,
    payload: AdjustmentRequest,
    conn: AsyncConnection = Depends(connection),
):
    return await adjust_stock(conn, product_id, payload.delta, payload.reason)
```

Use Pydantic request/response schemas, but keep them separate from SQL result tuples.

### Migrations

Use a SQL migration tool such as Alembic in SQL-only mode, `yoyo-migrations`, or a simple numbered migration runner. Migrations should contain plain SQL and run in CI before tests.

Example project layout:

```text
app/
  main.py
  db.py
  schemas.py
  repositories/
    products.py
tests/
  conftest.py
  test_products.py
  test_adjustments.py
migrations/
  001_initial.sql
pyproject.toml
```

### Integration tests

Run tests against a real PostgreSQL instance, not SQLite.

```python
# tests/conftest.py
import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def postgres():
    with PostgresContainer("postgres:16") as container:
        yield container

@pytest.fixture
async def client(postgres):
    # Set DATABASE_URL, run migrations, and create a fresh schema here.
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as ac:
        yield ac
```

Cover at least:

- Product creation and retrieval.
- Pagination ordering and `next_cursor`.
- Invalid limits and malformed cursors.
- Positive and negative stock adjustments.
- Rejection when stock would become negative.
- `404` for missing products.
- Audit-row contents (`before`, `after`, `delta`).
- Rollback when the adjustment insert fails.
- Concurrent adjustments to the same product, verifying the final quantity equals the serialized result.

For concurrency, launch two adjustment requests with `asyncio.gather`; the row lock should prevent lost updates. Use unique test schemas or truncate tables between tests, and run migrations once per test database.