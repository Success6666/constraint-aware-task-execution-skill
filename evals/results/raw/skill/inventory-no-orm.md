## 实现方案

采用 FastAPI、Pydantic、`psycopg 3`、`psycopg_pool.AsyncConnectionPool` 和 PostgreSQL。数据库迁移使用版本化 SQL 文件，集成测试使用 pytest、HTTPX 和 Testcontainers。

### 项目结构

```text
app/
  main.py
  config.py
  db.py
  api/items.py
  schemas/items.py
  services/inventory.py
  repositories/inventory.py
  errors.py
migrations/
  001_initial.sql
tests/
  integration/
    conftest.py
    test_items.py
    test_adjustments.py
    test_pagination.py
```

路由只处理 HTTP 协议，服务层控制事务和业务规则，仓储层集中保存参数化 SQL。

### 数据模型

```sql
CREATE TABLE inventory_items (
    id uuid PRIMARY KEY,
    sku text NOT NULL UNIQUE,
    name text NOT NULL,
    quantity bigint NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stock_adjustments (
    request_id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES inventory_items(id),
    delta bigint NOT NULL CHECK (delta <> 0),
    reason text NOT NULL,
    resulting_quantity bigint CHECK (resulting_quantity >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX inventory_items_page_idx
    ON inventory_items (created_at DESC, id DESC);

CREATE INDEX stock_adjustments_item_page_idx
    ON stock_adjustments (item_id, created_at DESC, request_id DESC);
```

商品创建时库存为零，后续库存变化全部通过调整接口完成，从而保留完整流水。

### API

- `POST /v1/items`：创建商品，返回 `201`
- `GET /v1/items/{item_id}`：查询商品
- `GET /v1/items?limit=50&cursor=...`：游标分页
- `POST /v1/items/{item_id}/adjustments`：增加或扣减库存
- `GET /v1/items/{item_id}/adjustments`：查询库存流水

调整请求：

```json
{
  "request_id": "24c9e94d-064a-4927-ab96-9abc87cdd41d",
  "delta": -3,
  "reason": "order:10042"
}
```

`request_id` 是幂等键。相同请求重复提交返回原结果；相同键但参数不同返回 `409`。

### 库存事务

使用 PostgreSQL默认的 `READ COMMITTED` 隔离级别。库存调整、流水创建和最终数量写入处于同一事务：

```python
async with pool.connection() as conn:
    async with conn.transaction():
        adjustment = await reserve_request(conn, command)

        if adjustment.is_replay:
            return adjustment

        row = await conn.execute(
            """
            UPDATE inventory_items
               SET quantity = quantity + %s,
                   updated_at = now()
             WHERE id = %s
               AND quantity + %s >= 0
         RETURNING id, sku, name, quantity, created_at, updated_at
            """,
            (command.delta, item_id, command.delta),
        )
        item = await row.fetchone()

        if item is None:
            await distinguish_missing_from_insufficient(conn, item_id)

        await finalize_adjustment(
            conn, command.request_id, item["quantity"]
        )
```

`reserve_request()` 通过以下语句处理并发重放：

```sql
INSERT INTO stock_adjustments (request_id, item_id, delta, reason)
VALUES (%s, %s, %s, %s)
ON CONFLICT (request_id) DO NOTHING
RETURNING request_id;
```

单条条件 `UPDATE` 会锁定目标行，并在获得锁后重新检查库存条件，避免先查后改造成超卖。库存不足返回 `409`，商品不存在返回 `404`，输入校验失败返回 `422`。数据库异常转换为稳定错误响应，不暴露 SQL 内容。

连接池在 FastAPI lifespan 中显式打开和关闭，符合 [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/) 和 [Psycopg 连接池](https://www.psycopg.org/psycopg3/docs/advanced/pool.html) 的生命周期模型。

### 分页

采用 `(created_at, id)` 键集分页：

```sql
SELECT id, sku, name, quantity, created_at, updated_at
FROM inventory_items
WHERE (%s::timestamptz IS NULL OR (created_at, id) < (%s, %s))
ORDER BY created_at DESC, id DESC
LIMIT %s;
```

查询 `limit + 1` 条判断是否存在下一页。游标包含版本、时间和 UUID，使用 URL-safe Base64 编码并严格解析；非法游标返回 `400`。`limit` 限制为 `1..100`。

PostgreSQL 明确指出大 `OFFSET` 仍需计算被跳过的记录，因此这里使用索引支持的键集分页，并以唯一键保证确定性顺序：[LIMIT/OFFSET](https://www.postgresql.org/docs/current/queries-limit.html)、[行值比较](https://www.postgresql.org/docs/current/functions-comparisons.html#ROW-WISE-COMPARISON)。

### 集成测试

使用固定 PostgreSQL 容器版本，启动后执行真实迁移。通过 `HTTPX AsyncClient`、`ASGITransport` 和 lifespan manager 调用应用；FastAPI 官方也推荐使用 HTTPX 编写[异步测试](https://fastapi.tiangolo.com/advanced/async-tests/)。

重点用例：

- 创建、查询、SKU 唯一约束和错误响应
- 正数和负数库存调整
- 库存不足时数量与流水全部回滚
- 调整完成前注入异常，验证库存和流水均未提交
- 相同幂等键串行及并发重放
- 相同幂等键携带不同参数
- 初始库存 10，并发执行 20 个 `-1` 请求，验证恰好 10 个成功、库存为零、流水为 10 条
- 相同时间戳记录的跨页查询
- 翻页期间插入新记录，验证已有结果无重复
- 非法游标、空页、最后一页和 `limit` 边界
- 异常请求后再次使用连接，验证没有泄漏失败事务

Testcontainers 可直接提供临时 PostgreSQL 环境，其用途和基本模式见[官方仓库](https://github.com/testcontainers/testcontainers-python)。核心验收标准是：任何并发顺序下库存不为负数，成功调整量与最终库存、流水记录完全一致。