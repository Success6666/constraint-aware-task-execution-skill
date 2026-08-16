Use FastAPI with `psycopg` 3 and `psycopg_pool`. Keep SQL explicit, place transaction boundaries in the service layer, and use keyset pagination rather than offset pagination.

**Dependencies**

```toml
[project]
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "psycopg[binary,pool]",
  "pydantic-settings",
]

[project.optional-dependencies]
test = [
  "httpx",
  "pytest",
  "pytest-asyncio",
  "testcontainers[postgres]",
]
```

**Database schema**

```sql
CREATE TABLE inventory_items (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         text NOT NULL UNIQUE,
    name        text NOT NULL,
    quantity    integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    version     bigint NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX inventory_items_created_id_idx
    ON inventory_items (created_at, id);

CREATE TABLE stock_adjustments (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_id         bigint NOT NULL REFERENCES inventory_items(id),
    delta           integer NOT NULL CHECK (delta <> 0),
    quantity_after  integer NOT NULL CHECK (quantity_after >= 0),
    reason          text,
    request_id      uuid NOT NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX stock_adjustments_item_created_idx
    ON stock_adjustments (item_id, created_at DESC);
```

`request_id` makes stock adjustment retries idempotent. The history table also provides an audit trail.

**API**

```text
POST   /items
GET    /items/{id}
GET    /items?limit=50&cursor=...
POST   /items/{id}/adjustments
GET    /items/{id}/adjustments?limit=50&cursor=...
```

Example adjustment request:

```json
{
  "delta": -3,
  "reason": "order fulfillment",
  "request_id": "44d39739-dd60-4e06-a3f4-6aacaaab27af"
}
```

A negative adjustment that would take stock below zero returns `409 Conflict`.

**Connection lifecycle**

```python
# db.py
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

pool = AsyncConnectionPool(
    conninfo="postgresql://inventory:inventory@localhost/inventory",
    kwargs={"autocommit": False, "row_factory": dict_row},
    open=False,
)

@asynccontextmanager
async def lifespan(app):
    await pool.open()
    await pool.wait()
    yield
    await pool.close()
```

```python
app = FastAPI(lifespan=lifespan)
```

Do not let repositories open transactions implicitly. Pass a transaction-bound connection into repository functions so a service operation can span multiple statements atomically.

**Atomic stock adjustment**

```python
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation


class ItemNotFound(Exception):
    pass


class InsufficientStock(Exception):
    pass


async def adjust_stock(
    conn: AsyncConnection,
    *,
    item_id: int,
    delta: int,
    reason: str | None,
    request_id: UUID,
) -> dict:
    async with conn.transaction():
        existing = await conn.execute(
            """
            SELECT i.*
            FROM stock_adjustments a
            JOIN inventory_items i ON i.id = a.item_id
            WHERE a.request_id = %s AND a.item_id = %s
            """,
            (request_id, item_id),
        )
        if row := await existing.fetchone():
            return row

        result = await conn.execute(
            """
            UPDATE inventory_items
               SET quantity = quantity + %s,
                   version = version + 1,
                   updated_at = now()
             WHERE id = %s
               AND quantity + %s >= 0
         RETURNING *
            """,
            (delta, item_id, delta),
        )
        item = await result.fetchone()

        if item is None:
            found = await conn.execute(
                "SELECT 1 FROM inventory_items WHERE id = %s",
                (item_id,),
            )
            if await found.fetchone() is None:
                raise ItemNotFound
            raise InsufficientStock

        await conn.execute(
            """
            INSERT INTO stock_adjustments
                (item_id, delta, quantity_after, reason, request_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (item_id, delta, item["quantity"], reason, request_id),
        )

        return item
```

The conditional `UPDATE` is atomic under concurrent requests. There is no read-modify-write race and no need for a process-local lock.

For complete idempotency under simultaneous requests using the same `request_id`, insert or reserve the adjustment request first, or catch `UniqueViolation`, roll back, then read the already committed result in a new transaction. Also reject reuse of a `request_id` with different input values.

**Route transaction usage**

```python
@router.post(
    "/items/{item_id}/adjustments",
    response_model=ItemResponse,
)
async def create_adjustment(
    item_id: int,
    body: AdjustmentRequest,
) -> ItemResponse:
    async with pool.connection() as conn:
        try:
            item = await adjust_stock(
                conn,
                item_id=item_id,
                delta=body.delta,
                reason=body.reason,
                request_id=body.request_id,
            )
        except ItemNotFound:
            raise HTTPException(404, "Item not found")
        except InsufficientStock:
            raise HTTPException(409, "Insufficient stock")

    return ItemResponse.model_validate(item)
```

**Keyset pagination**

Encode `(created_at, id)` into an opaque URL-safe base64 cursor. Validate decoded values before passing them to SQL.

```sql
SELECT id, sku, name, quantity, version, created_at, updated_at
FROM inventory_items
WHERE (
    %(cursor_created_at)s IS NULL
    OR (created_at, id) > (%(cursor_created_at)s, %(cursor_id)s)
)
ORDER BY created_at, id
LIMIT %(fetch_limit)s;
```

Fetch `limit + 1` rows. Return only `limit`; if the extra row exists, create `next_cursor` from the last returned row.

```json
{
  "items": [],
  "next_cursor": "opaque-value-or-null"
}
```

Constrain `limit`, for example `1 <= limit <= 100`. Keyset pagination avoids duplicates and increasingly slow scans associated with large offsets.

**Suggested layout**

```text
app/
  main.py
  config.py
  db.py
  errors.py
  models.py
  pagination.py
  repositories/
    items.py
  services/
    inventory.py
  routes/
    items.py
migrations/
  001_initial.sql
tests/
  conftest.py
  test_items.py
  test_adjustments.py
  test_pagination.py
  test_concurrency.py
```

**Integration testing**

Start a real PostgreSQL container once per test session, apply SQL migrations, and override the application pool configuration. Clean tables between tests with `TRUNCATE ... RESTART IDENTITY CASCADE`.

Critical cases:

- Creating and retrieving an item.
- Duplicate SKU returns `409`.
- Pagination has stable ordering, no duplicates, and correct terminal cursor.
- Positive and negative adjustments update quantity and write audit rows.
- Adjustment cannot make quantity negative.
- Missing item returns `404`.
- Repeated `request_id` produces one adjustment.
- Reusing a request ID with different input returns `409`.
- Concurrent decrements never produce negative stock.
- Failure while inserting the audit record rolls back the quantity update.

Concurrency test outline:

```python
async def test_concurrent_decrements_do_not_oversell(client):
    item = await create_item(client, quantity=5)

    responses = await asyncio.gather(*[
        client.post(
            f"/items/{item['id']}/adjustments",
            json={
                "delta": -1,
                "request_id": str(uuid4()),
                "reason": "test",
            },
        )
        for _ in range(10)
    ])

    assert sum(r.status_code == 200 for r in responses) == 5
    assert sum(r.status_code == 409 for r in responses) == 5

    current = await client.get(f"/items/{item['id']}")
    assert current.json()["quantity"] == 0
```

Run migrations against the container using `psql`, a small ordered migration runner, or a non-ORM migration tool such as Flyway. The production and test environments should execute the exact same migration files.