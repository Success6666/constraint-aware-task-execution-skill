Use FastAPI with `asyncpg` and explicit SQL. Keep HTTP handling, transaction orchestration, and SQL access separated, but avoid introducing an ORM or repository abstraction that hides transaction boundaries.

**Project layout**

```text
app/
  main.py
  db.py
  errors.py
  inventory/
    models.py       # Pydantic request/response models
    routes.py
    service.py      # transaction and business rules
    sql.py          # SQL constants or query functions
tests/
  integration/
    conftest.py
    test_inventory.py
migrations/
  001_inventory.sql
```

**Database schema**

```sql
CREATE TABLE products (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX products_created_id_idx
    ON products (created_at DESC, id DESC);

CREATE TABLE stock_adjustments (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL REFERENCES products(id),
    delta       INTEGER NOT NULL CHECK (delta <> 0),
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX stock_adjustments_product_created_idx
    ON stock_adjustments (product_id, created_at DESC, id DESC);
```

The adjustment table provides an audit trail; `products.quantity` is the current materialized stock level.

**Connection management**

Use an `asyncpg.Pool` created during FastAPI lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
    )
    yield
    await app.state.db.close()
```

Expose the pool through a dependency. Acquire a connection only for the duration of a request or transaction.

**API surface**

```text
POST   /products
GET    /products?limit=50&cursor=...
GET    /products/{product_id}
POST   /products/{product_id}/adjustments
GET    /products/{product_id}/adjustments?limit=50&cursor=...
```

Example payloads:

```json
POST /products
{
  "sku": "SKU-1001",
  "name": "Keyboard",
  "quantity": 20
}
```

```json
POST /products/1/adjustments
{
  "delta": -3,
  "reason": "damaged stock"
}
```

Responses should use Pydantic models and include `id`, `sku`, `name`, `quantity`, and timestamps. Return `409 Conflict` for duplicate SKUs or an adjustment that would make stock negative, `404` for missing products, and `422` for malformed input.

**Key transaction for stock adjustments**

Perform the lock, validation, update, and audit insert in one transaction:

```python
async def adjust_stock(pool, product_id: int, delta: int, reason: str):
    if delta == 0:
        raise InvalidAdjustment()

    async with pool.acquire() as conn:
        async with conn.transaction():
            product = await conn.fetchrow(
                """
                SELECT id, quantity
                FROM products
                WHERE id = $1
                FOR UPDATE
                """,
                product_id,
            )
            if product is None:
                raise ProductNotFound()

            new_quantity = product["quantity"] + delta
            if new_quantity < 0:
                raise InsufficientStock()

            updated = await conn.fetchrow(
                """
                UPDATE products
                SET quantity = $2, updated_at = now()
                WHERE id = $1
                RETURNING id, sku, name, quantity, created_at, updated_at
                """,
                product_id,
                new_quantity,
            )

            await conn.execute(
                """
                INSERT INTO stock_adjustments(product_id, delta, reason)
                VALUES ($1, $2, $3)
                """,
                product_id,
                delta,
                reason,
            )

            return updated
```

`SELECT ... FOR UPDATE` serializes concurrent adjustments for the same product. The transaction guarantees that the quantity update and audit record either both commit or both roll back.

For product creation, insert the initial quantity and optionally create an initial adjustment in the same transaction. Convert PostgreSQL unique-constraint violations into `409`.

**Pagination**

Prefer keyset pagination over offsets for stable performance:

```sql
SELECT id, sku, name, quantity, created_at, updated_at
FROM products
WHERE ($1::timestamptz IS NULL AND $2::bigint IS NULL)
   OR (created_at, id) < ($1, $2)
ORDER BY created_at DESC, id DESC
LIMIT $3;
```

Encode the last row’s `(created_at, id)` as an opaque base64 URL-safe cursor. Validate `limit` with `Query(50, ge=1, le=200)`. Return:

```json
{
  "items": [],
  "next_cursor": "opaque-token-or-null"
}
```

Apply the same pattern to adjustment history, using `(created_at, id)` scoped to a single `product_id`.

**Error handling**

Register handlers for:

- domain exceptions (`ProductNotFound`, `InsufficientStock`)
- `asyncpg.UniqueViolationError`
- database connectivity errors, mapped to `503`
- unexpected exceptions, logged with a request ID and returned as generic `500`

Do not expose SQL errors or connection details to clients.

**Integration tests**

Run PostgreSQL in CI and locally with Testcontainers or Docker Compose. Tests should use the real database and migrations, not SQLite or mocks.

Recommended cases:

1. Create a product and retrieve it.
2. Reject duplicate SKU with `409`.
3. Apply a positive adjustment and verify quantity plus audit row.
4. Reject an adjustment that would make quantity negative; verify neither table changed.
5. Run concurrent adjustments against one product and verify the final quantity equals the sum of successful deltas.
6. Verify cursor pagination has no duplicates or gaps across multiple pages.
7. Verify adjustment history pagination.
8. Verify missing product returns `404`.
9. Roll back when the audit insert fails (inject a database constraint failure in a test transaction).
10. Verify request validation rejects zero deltas and invalid limits.

A concurrency test can start multiple async tasks, each calling the adjustment endpoint, then assert the final quantity and count of audit records. Use a separate connection per task so row locking is exercised realistically.

This design keeps SQL explicit, makes transaction boundaries visible, supports safe concurrent stock changes, and provides production-like integration coverage without an ORM.