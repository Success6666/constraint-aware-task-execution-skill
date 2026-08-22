# FastAPI Inventory API Design

## Stack

- FastAPI
- Pydantic v2
- `psycopg` 3 with `psycopg_pool.AsyncConnectionPool`
- PostgreSQL
- pytest with an isolated disposable PostgreSQL database
- No ORM; all persistence uses parameterized SQL

## Data model

```sql
CREATE TABLE products (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE inventory (
    product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    quantity BIGINT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stock_adjustments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    delta BIGINT NOT NULL CHECK (delta <> 0),
    resulting_quantity BIGINT NOT NULL CHECK (resulting_quantity >= 0),
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX stock_adjustments_product_created_idx
    ON stock_adjustments (product_id, created_at DESC, id DESC);

CREATE INDEX products_created_idx
    ON products (created_at DESC, id DESC);
```

Every product receives an inventory row with quantity `0` when created.

## API endpoints

### Create product

`POST /products`

Request:

```json
{
  "sku": "SKU-100",
  "name": "Widget"
}
```

Response: `201 Created`

```json
{
  "id": 1,
  "sku": "SKU-100",
  "name": "Widget",
  "quantity": 0,
  "created_at": "2025-01-01T12:00:00Z"
}
```

Duplicate SKUs return `409 Conflict`.

### List products

`GET /products?limit=50&cursor=<cursor>`

- `limit`: integer from `1` to `100`, default `50`
- Uses keyset pagination, not `OFFSET`
- Sort order: `created_at DESC, id DESC`

Query:

```sql
SELECT p.id, p.sku, p.name, p.created_at, i.quantity
FROM products p
JOIN inventory i ON i.product_id = p.id
WHERE (p.created_at, p.id) < (%s, %s)
ORDER BY p.created_at DESC, p.id DESC
LIMIT %s;
```

The cursor contains the last row’s timestamp and ID, encoded as opaque base64url JSON. It should also include a hash of the active filters so a cursor cannot be reused with different filters.

Response:

```json
{
  "items": [],
  "next_cursor": "..."
}
```

`next_cursor` is `null` on the final page.

### Get inventory

`GET /products/{sku}/inventory`

Response:

```json
{
  "sku": "SKU-100",
  "quantity": 42,
  "updated_at": "2025-01-01T12:00:00Z"
}
```

Unknown products return `404 Not Found`.

### Adjust stock

`POST /products/{sku}/adjustments`

Required header:

```text
Idempotency-Key: unique-client-generated-value
```

Request:

```json
{
  "delta": -3,
  "reason": "Damaged units"
}
```

Response: `201 Created`

```json
{
  "id": 7,
  "sku": "SKU-100",
  "delta": -3,
  "resulting_quantity": 39,
  "reason": "Damaged units",
  "created_at": "2025-01-01T12:00:00Z"
}
```

Rules:

- `delta` cannot be zero.
- Quantity can never become negative.
- A duplicate idempotency key returns the original result.
- Reusing an idempotency key with different product or adjustment data returns `409 Conflict`.
- Insufficient stock returns `409 Conflict`.

### List adjustment history

`GET /products/{sku}/adjustments?limit=50&cursor=<cursor>`

Uses the same keyset strategy with:

```sql
WHERE product_id = %s
  AND (created_at, id) < (%s, %s)
ORDER BY created_at DESC, id DESC
LIMIT %s;
```

## Transactional stock adjustment

Use one PostgreSQL transaction for the complete adjustment.

```sql
BEGIN;

SELECT p.id
FROM products p
WHERE p.sku = %s
FOR UPDATE;
```

Then lock the inventory row:

```sql
SELECT quantity
FROM inventory
WHERE product_id = %s
FOR UPDATE;
```

Check the new quantity in application code:

```text
new_quantity = current_quantity + delta
if new_quantity < 0:
    raise insufficient_stock
```

Check whether the idempotency key already exists. If it exists:

- Return the existing adjustment if all request fields match.
- Return `409` if the product, delta, or reason differs.

For a new adjustment:

```sql
UPDATE inventory
SET quantity = %s,
    updated_at = now()
WHERE product_id = %s;
```

```sql
INSERT INTO stock_adjustments (
    product_id,
    delta,
    resulting_quantity,
    reason,
    idempotency_key
)
VALUES (%s, %s, %s, %s, %s)
RETURNING id, created_at;
```

Commit only after both statements succeed.

The row lock serializes concurrent changes to the same product. PostgreSQL `READ COMMITTED` isolation is sufficient because the inventory row is explicitly locked.

## Database access design

Use an application-wide asynchronous connection pool:

- Acquire one connection per request operation.
- Wrap every write in `async with connection.transaction()`.
- Pass values as query parameters.
- Never build SQL using user-provided string interpolation.
- Keep transactions short and do not perform external calls inside them.
- Map PostgreSQL constraint errors to API errors:
  - unique violation → `409`
  - foreign key violation → `404` or `409`, depending on the operation
  - check violation → `400`

The service layer should contain the transaction boundaries and business rules; route handlers should only validate input, invoke the service, and map results to response models.

## Validation and error responses

Use consistent responses:

```json
{
  "detail": "Insufficient stock"
}
```

Recommended status codes:

- `201`: product or adjustment created
- `200`: successful reads and idempotent replay
- `400`: invalid cursor or invalid request
- `404`: product not found
- `409`: duplicate SKU, insufficient stock, or idempotency conflict
- `422`: Pydantic validation failure

Pydantic constraints:

- SKU and name must be non-empty.
- `delta` must be a nonzero integer.
- `reason` must be non-empty and bounded in length.
- `limit` must be between `1` and `100`.

## Integration tests

Run the API against a real PostgreSQL instance with the schema applied before tests. Tests should exercise HTTP endpoints and verify database state.

Required cases:

1. Create a product and verify quantity starts at zero.
2. Reject duplicate SKUs.
3. Retrieve inventory for an existing product.
4. Return `404` for an unknown product.
5. List products with multiple pages.
6. Verify keyset pagination has no duplicates or missing rows.
7. Verify pagination remains stable when records are inserted between requests.
8. Create a positive stock adjustment.
9. Create a negative adjustment.
10. Reject an adjustment that would make stock negative.
11. Verify a rejected adjustment changes neither inventory nor history.
12. Replay an adjustment with the same idempotency key and verify only one ledger row exists.
13. Reuse an idempotency key with different data and expect `409`.
14. List adjustment history with pagination.
15. Execute concurrent adjustments against the same product and verify:
    - all successful changes are reflected exactly once;
    - no quantity becomes negative;
    - the final quantity equals the sum of committed deltas.
16. Roll back a transaction after a simulated database failure and verify no partial inventory or ledger update remains.
17. Verify database constraints reject invalid quantities and zero deltas.

The concurrency tests must use separate database connections and synchronize their requests so both operations overlap, proving that row locking—not application timing—provides correctness.
