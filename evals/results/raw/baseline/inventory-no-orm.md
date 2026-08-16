当前仓库只有 Git 元数据，没有现有代码。建议以 FastAPI + Psycopg 3 + PostgreSQL 构建，直接执行参数化 SQL，不引入 ORM。

**技术选型**

- Python 3.12
- FastAPI、Pydantic v2
- `psycopg[binary,pool]`：异步连接池和事务管理
- Alembic：只管理原生 SQL migration，不使用其 ORM能力
- Pytest、HTTPX、Testcontainers PostgreSQL
- Ruff、Mypy

**项目结构**

```text
app/
├── main.py
├── config.py
├── db/
│   ├── pool.py
│   └── migrations/
├── inventory/
│   ├── router.py
│   ├── schemas.py
│   ├── service.py
│   ├── repository.py
│   └── errors.py
└── common/
    ├── pagination.py
    └── exception_handlers.py
tests/
├── conftest.py
├── integration/
│   ├── test_products.py
│   ├── test_stock_adjustments.py
│   └── test_concurrency.py
└── unit/
```

路由只处理 HTTP 协议和校验，服务层定义事务边界，仓储层集中维护 SQL。

**数据模型**

```sql
CREATE TABLE products (
    id uuid PRIMARY KEY,
    sku varchar(64) NOT NULL UNIQUE,
    name varchar(255) NOT NULL,
    description text,
    unit_price numeric(12, 2) NOT NULL CHECK (unit_price >= 0),
    quantity integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    version bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stock_adjustments (
    id uuid PRIMARY KEY,
    product_id uuid NOT NULL REFERENCES products(id),
    delta integer NOT NULL CHECK (delta <> 0),
    quantity_before integer NOT NULL,
    quantity_after integer NOT NULL CHECK (quantity_after >= 0),
    reason varchar(255) NOT NULL,
    idempotency_key varchar(128) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_products_created_id
    ON products (created_at DESC, id DESC);

CREATE INDEX idx_adjustments_product_created
    ON stock_adjustments (product_id, created_at DESC, id DESC);
```

`stock_adjustments` 是不可变审计记录。库存不能通过普通商品更新接口直接修改。

**API**

```text
POST   /v1/products
GET    /v1/products/{product_id}
GET    /v1/products?limit=50&cursor=...
PATCH  /v1/products/{product_id}
DELETE /v1/products/{product_id}

POST   /v1/products/{product_id}/stock-adjustments
GET    /v1/products/{product_id}/stock-adjustments?limit=50&cursor=...
```

库存调整请求：

```json
{
  "delta": -3,
  "reason": "order:8b94",
  "idempotency_key": "order-8b94-reservation"
}
```

成功返回调整记录和最新库存。库存不足返回 `409 Conflict`，商品不存在返回 `404`，重复幂等键返回第一次操作的结果。

**事务实现**

一次库存调整必须在同一事务内完成：

```python
async with pool.connection() as conn:
    async with conn.transaction():
        existing = await conn.execute(
            """
            SELECT id, product_id, delta, quantity_before, quantity_after
            FROM stock_adjustments
            WHERE idempotency_key = %s
            """,
            (command.idempotency_key,),
        )

        if adjustment := await existing.fetchone():
            return adjustment

        result = await conn.execute(
            """
            SELECT id, quantity
            FROM products
            WHERE id = %s
            FOR UPDATE
            """,
            (product_id,),
        )
        product = await result.fetchone()

        if product is None:
            raise ProductNotFound(product_id)

        new_quantity = product["quantity"] + command.delta
        if new_quantity < 0:
            raise InsufficientStock()

        await conn.execute(
            """
            UPDATE products
            SET quantity = %s,
                version = version + 1,
                updated_at = now()
            WHERE id = %s
            """,
            (new_quantity, product_id),
        )

        await conn.execute(
            """
            INSERT INTO stock_adjustments (
                id, product_id, delta, quantity_before,
                quantity_after, reason, idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                adjustment_id,
                product_id,
                command.delta,
                product["quantity"],
                new_quantity,
                command.reason,
                command.idempotency_key,
            ),
        )
```

还需要捕获幂等键唯一约束冲突。并发请求可能在首次查询后同时进入事务，发生冲突时重新查询并返回已提交记录。

所有 SQL 值使用驱动参数绑定；排序字段、表名等不可接受客户端任意输入。

**分页**

使用基于 `(created_at, id)` 的游标分页，避免大偏移量查询变慢或并发写入造成重复数据：

```sql
SELECT id, sku, name, unit_price, quantity, created_at, updated_at
FROM products
WHERE (%s::timestamptz IS NULL)
   OR (created_at, id) < (%s, %s)
ORDER BY created_at DESC, id DESC
LIMIT %s;
```

实际查询取 `limit + 1` 条以判断 `has_more`。游标编码为 URL-safe Base64，并用 HMAC 签名，防止客户端篡改。`limit` 默认 50，上限 100。

响应形式：

```json
{
  "items": [],
  "next_cursor": "signed-cursor-or-null",
  "has_more": false
}
```

**集成测试**

每次测试会话启动真实 PostgreSQL 容器、执行 migration，再通过 HTTPX 调用 FastAPI：

- 商品创建、读取、修改和唯一 SKU 冲突。
- 空列表、分页边界、稳定排序、非法游标和最大页大小。
- 正数与负数库存调整。
- 库存不足时商品和审计记录均不发生变化。
- 相同幂等键重复提交只调整一次。
- 多个并发扣减请求不会出现负库存或丢失更新。
- 事务中第二条 SQL 失败时库存更新回滚。
- 参数中包含 SQL 特殊字符时不会改变查询语义。
- 应用启动和关闭时正确创建、释放连接池。

并发测试应使用独立连接同时发起请求，通过 barrier 尽量让事务重叠，最终断言：

```text
最终库存 = 初始库存 + 所有成功请求的 delta
审计记录数 = 成功的唯一幂等请求数
任意 quantity_after >= 0
```

**验收标准**

- 所有库存变更均生成审计记录并具备原子性。
- 并发扣减不会超卖、负库存或丢失更新。
- 重试请求不会重复调整库存。
- 列表接口查询命中组合索引，且不使用无界分页。
- API 中不存在 SQL 字符串拼接。
- 集成测试使用真实 PostgreSQL，覆盖提交、回滚、锁竞争和唯一约束冲突。
- OpenAPI 文档明确列出 `404`、`409`、`422` 等响应。