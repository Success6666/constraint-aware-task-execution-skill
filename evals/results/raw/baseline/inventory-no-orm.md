**Recommended Stack**

- FastAPI with Pydantic v2 for request/response validation.
- `asyncpg` for asynchronous PostgreSQL access; no ORM or query builder.
- PostgreSQL migrations managed by Alembic in SQL-only mode, or a comparable SQL migration tool.
- `pytest`, `pytest-asyncio`, and HTTPX `AsyncClient` for integration tests.
- PostgreSQL Testcontainers or a dedicated disposable PostgreSQL instance for tests.

**Database Schema**

```sql
CREATE TABLE products (
    id              BIGSERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,
    quantity        INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    unit_price      NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stock_adjustments (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES products(id),
    delta           INTEGER NOT NULL CHECK (delta <> 0),
    quantity_before INTEGER NOT NULL CHECK (quantity_before >= 0),
    quantity_after  INTEGER NOT NULL CHECK (quantity_after >= 0),
    reason          TEXT NOT NULL,
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, idempotency_key)
);

CREATE INDEX products_created_id_idx
    ON products (created_at DESC, id DESC);

CREATE INDEX stock_adjustments_product_created_idx
    ON stock_adjustments (product_id, created_at DESC, id DESC);
```

The `quantity >= 0` constraint protects the invariant at the database level. The adjustment table provides an auditable history.

**Connection and Transaction Management**

Create one application-wide `asyncpg.Pool` during FastAPI startup and close it during shutdown. Inject the pool through a dependency.

Use explicit transactions for all writes:

```python
async with pool.acquire() as connection:
    async with connection.transaction():
        ...
```

For multi-step operations, every query must use the same acquired connection. Never acquire separate connections within one logical transaction.

Configure a statement timeout and use parameterized SQL exclusively:

```sql
SET LOCAL statement_timeout = '5s';
```

Do not interpolate user input into SQL identifiers or values.

**API Endpoints**

```text
POST   /products
GET    /products
GET    /products/{product_id}
PATCH  /products/{product_id}
DELETE /products/{product_id}
POST   /products/{product_id}/stock-adjustments
GET    /products/{product_id}/stock-adjustments
```

Example request models:

```json
POST /products
{
  "sku": "SKU-1001",
  "name": "Keyboard",
  "description": "Mechanical keyboard",
  "unit_price": "89.99",
  "initial_quantity": 25
}
```

```json
POST /products/{id}/stock-adjustments
{
  "delta": -3,
  "reason": "Customer order"
}
```

Support an optional `Idempotency-Key` header for stock adjustments. Require it for clients that may retry requests.

**Product Creation**

Run product creation in a transaction:

1. Insert the product.
2. If `initial_quantity` is nonzero, insert an initial stock adjustment with:
   - `delta = initial_quantity`
   - `quantity_before = 0`
   - `quantity_after = initial_quantity`
   - `reason = 'initial stock'`
3. Commit and return the product.

Translate duplicate SKU violations into `409 Conflict`.

**Product Listing and Pagination**

Use keyset pagination rather than offset pagination. It remains stable and efficient as data changes.

Query parameters:

```text
limit: integer, default 50, maximum 100
cursor: opaque cursor, optional
sku: optional exact or prefix filter
search: optional name search
```

Use `(created_at, id)` as the ordering key:

```sql
SELECT id, sku, name, description, quantity, unit_price,
       created_at, updated_at
FROM products
WHERE
    ($1::timestamptz IS NULL
     OR (created_at, id) < ($1, $2))
    AND ($3::text IS NULL OR sku ILIKE $3 || '%')
    AND ($4::text IS NULL OR name ILIKE '%' || $4 || '%')
ORDER BY created_at DESC, id DESC
LIMIT $5;
```

The cursor should be a URL-safe encoded JSON object containing the last row’s `created_at` and `id`. Treat malformed cursors as `400 Bad Request`.

Return:

```json
{
  "items": [],
  "next_cursor": "opaque-value"
}
```

Use a separate count endpoint or omit total counts from normal listing responses. `COUNT(*)` on every request adds cost and is unnecessary for cursor pagination.

**Stock Adjustment Transaction**

The adjustment operation must be atomic and serialize concurrent changes for one product:

```sql
SELECT quantity
FROM products
WHERE id = $1
FOR UPDATE;
```

Within the same transaction:

1. Lock the product row.
2. Check whether `(product_id, idempotency_key)` already exists.
3. If it exists, return the previously stored adjustment and current product state.
4. Calculate `new_quantity = quantity + delta`.
5. Reject the operation with `409 Conflict` if `new_quantity < 0`.
6. Update the product quantity and `updated_at`.
7. Insert the stock adjustment record.
8. Commit.

Use an integer delta, allowing positive and negative values but rejecting zero. Use PostgreSQL `NUMERIC` for money and return monetary values as decimal strings or validated decimal fields.

The locking query ensures that two simultaneous adjustments cannot both read the same quantity and oversell stock.

For idempotency, the insert should also be protected by the unique constraint. If a concurrent request races on the same key, handle the unique violation by selecting and returning the existing adjustment after the transaction state is recovered, or perform the idempotency lookup while holding the product lock so requests for the same product serialize.

**Stock History Pagination**

Expose adjustment history with the same cursor strategy:

```sql
SELECT id, product_id, delta, quantity_before, quantity_after,
       reason, idempotency_key, created_at
FROM stock_adjustments
WHERE product_id = $1
  AND (
      $2::timestamptz IS NULL
      OR (created_at, id) < ($2, $3)
  )
ORDER BY created_at DESC, id DESC
LIMIT $4;
```

History is append-only. Do not allow updates or deletes to adjustment records.

**Updates and Deletes**

`PATCH /products/{id}` may change `name`, `description`, and `unit_price`, but not `quantity`. Quantity changes must use the adjustment endpoint so that every stock mutation is auditable.

Use a transaction for updates and return `404` when the product does not exist. Decide explicitly whether deletion is allowed for products with stock history. The safer policy is to reject deletion with `409 Conflict` when adjustments exist, or use a soft-delete column if historical products must remain queryable.

**Error Contract**

Return a consistent structure:

```json
{
  "error": {
    "code": "INSUFFICIENT_STOCK",
    "message": "Stock cannot become negative"
  }
}
```

Recommended status codes:

- `400`: malformed cursor or invalid request semantics.
- `404`: product or adjustment resource not found.
- `409`: duplicate SKU, insufficient stock, or conflicting deletion.
- `422`: Pydantic validation failure.
- `500`: unexpected server error, with database details excluded from the response.

Add exception handlers for PostgreSQL constraint violations and map known constraint names to stable API error codes.

**Application Structure**

Keep boundaries explicit:

- API/router layer: HTTP parsing, authentication, status codes.
- Service layer: transaction boundaries and business rules.
- Repository layer: parameterized SQL and row mapping.
- Database layer: pool creation, connection helpers, migrations.
- Schema layer: Pydantic request and response models.

Repositories should accept an `asyncpg.Connection` when called inside a transaction. This prevents accidental transaction splitting.

Use typed row-to-model conversion rather than returning raw driver records from route handlers.

**Integration Tests**

Run tests against real PostgreSQL, not SQLite or mocks.

Cover:

1. Product creation returns the persisted product.
2. Duplicate SKU returns `409`.
3. Product listing honors limit and cursor ordering.
4. Invalid and tampered cursors return `400`.
5. Product retrieval and updates work.
6. Missing products return `404`.
7. Positive stock adjustment updates quantity and creates history.
8. Negative adjustment succeeds when sufficient stock exists.
9. Negative adjustment that would make stock negative returns `409` and changes nothing.
10. Zero delta returns `422`.
11. Adjustment history contains correct before and after quantities.
12. Repeating the same idempotent request returns the original result and creates one history row.
13. Concurrent adjustments serialize correctly and never produce negative stock.
14. Concurrent adjustments whose combined delta exceeds stock result in one successful transaction and one `409`.
15. Transaction rollback leaves both product quantity and history unchanged after an induced failure.
16. Database constraints reject invalid quantities even if application validation is bypassed.
17. Product deletion follows the documented history policy.

For concurrency testing, create a product with a known quantity, synchronize multiple async tasks with an `asyncio.Barrier`, submit adjustments simultaneously, then assert:

```text
final_quantity = initial_quantity + sum(successful_deltas)
final_quantity >= 0
number_of_history_rows = number_of_successful_adjustments
```

Use isolated database schemas or truncate tables between tests. Run migrations as part of the test fixture setup and verify the test suite against the same PostgreSQL major version used in deployment.

**Verification and Operational Requirements**

- Run formatting, linting, static type checking, and the complete integration suite in CI.
- Test migrations both from an empty database and from a representative prior schema.
- Add structured logs containing request ID, product ID, adjustment ID, and transaction outcome, without logging sensitive request data.
- Expose health checks for application readiness and database connectivity.
- Set pool size based on PostgreSQL connection capacity, with bounded acquisition timeout.
- Document the transaction and idempotency behavior in the OpenAPI description.
- Add metrics for request latency, database errors, stock conflicts, and transaction rollbacks.
