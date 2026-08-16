Use FastAPI with `psycopg` 3’s async connection pool, SQL migrations, and explicit SQL. Keep transaction boundaries in the service layer so stock updates and movement records commit or roll back together.

**Suggested structure**

```text
app/
  main.py
  db.py
  models.py
  repositories/products.py
  services/inventory.py
  routers/products.py
migrations/001_inventory.sql
tests/
  conftest.py
  test_products.py
  test_stock_adjustments.py
```

**Schema**

```sql
CREATE TABLE products (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku text NOT NULL UNIQUE,
    name text NOT NULL,
    description text,
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
    stock_quantity integer NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stock_movements (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id bigint NOT NULL REFERENCES products(id),
    delta integer NOT NULL CHECK (delta <> 0),
    resulting_quantity integer NOT NULL CHECK (resulting_quantity >= 0),
    reason text NOT NULL,
    request_id uuid UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX products_page_idx ON products (id);
CREATE INDEX stock_movements_product_idx
    ON stock_movements (product_id, id DESC);
```

`request_id` makes retries idempotent. Clients should reuse it when retrying the same adjustment.

**API**

```text
POST   /products
GET    /products/{id}
GET    /products?limit=50&after_id=123
PATCH  /products/{id}
POST   /products/{id}/stock-adjustments
GET    /products/{id}/stock-movements?limit=50&before_id=456
```

Use cursor pagination instead of offsets:

```sql
SELECT id, sku, name, description, unit_price, stock_quantity, created_at, updated_at
FROM products
WHERE id > %(after_id)s
ORDER BY id
LIMIT %(fetch_limit)s;
```

Fetch `limit + 1` rows; the extra row determines whether to return a `next_cursor`. Enforce a limit such as `1..100`.

**Stock transaction**

```python
async def adjust_stock(pool, product_id, delta, reason, request_id):
    async with pool.connection() as conn:
        async with conn.transaction():
            if request_id:
                existing = await conn.execute(
                    """
                    SELECT product_id, delta, resulting_quantity
                    FROM stock_movements
                    WHERE request_id = %s
                    """,
                    (request_id,),
                )
                row = await existing.fetchone()
                if row:
                    if row["product_id"] != product_id or row["delta"] != delta:
                        raise IdempotencyConflict()
                    return row["resulting_quantity"]

            result = await conn.execute(
                """
                UPDATE products
                SET stock_quantity = stock_quantity + %s,
                    updated_at = now()
                WHERE id = %s
                  AND stock_quantity + %s >= 0
                RETURNING stock_quantity
                """,
                (delta, product_id, delta),
            )
            updated = await result.fetchone()

            if updated is None:
                exists = await conn.execute(
                    "SELECT 1 FROM products WHERE id = %s", (product_id,)
                )
                if await exists.fetchone() is None:
                    raise ProductNotFound()
                raise InsufficientStock()

            await conn.execute(
                """
                INSERT INTO stock_movements
                    (product_id, delta, resulting_quantity, reason, request_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (product_id, delta, updated["stock_quantity"], reason, request_id),
            )
            return updated["stock_quantity"]
```

The conditional `UPDATE` prevents negative inventory and lost updates without requiring an application-level lock. Map missing products to `404`, insufficient stock to `409`, duplicate SKU or conflicting idempotency keys to `409`, and validation failures to `422`.

Create the pool in FastAPI’s lifespan handler and inject it through `Depends`. Pydantic request models should use `Decimal` for prices and reject zero stock deltas.

**Integration tests**

Run tests against a real PostgreSQL container using `pytest`, `pytest-asyncio`, `httpx.AsyncClient`, and Testcontainers. Apply migrations once per test session and truncate tables between tests.

Cover:

- Product creation, duplicate SKU, retrieval, and updates.
- Stable pagination with correct cursor and limit behavior.
- Positive and negative stock adjustments.
- Rejection of adjustments that would produce negative stock.
- Movement and product quantity committing together.
- Rollback when movement insertion fails.
- Replaying the same `request_id` without applying stock twice.
- Conflicting reuse of a `request_id`.
- Concurrent decrements, asserting successful adjustments never exceed available stock.
- Decimal price serialization and database constraint behavior.

For the concurrency test, create stock of `10`, issue twenty simultaneous `-1` requests with distinct request IDs, and assert exactly ten succeed, ten return `409`, final stock is zero, and exactly ten movement rows exist.