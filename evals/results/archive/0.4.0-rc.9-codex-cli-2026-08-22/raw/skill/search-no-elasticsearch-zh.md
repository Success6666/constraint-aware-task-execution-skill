## 方案概览

使用 PostgreSQL 内置能力实现：

- `tsvector` + `websearch_to_tsquery`：全文检索
- `ts_rank_cd`：相关性排序
- `pg_trgm`：商品名、品牌的模糊匹配和拼写容错
- GIN/B-tree 索引：高效过滤与检索
- Keyset Cursor：稳定分页
- Outbox + Worker：可靠索引更新

## 数据模型

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE products (
  id           BIGSERIAL PRIMARY KEY,
  sku          TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  brand        TEXT,
  description  TEXT,
  category_id  BIGINT,
  price        NUMERIC(12, 2) NOT NULL,
  stock        INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'active',
  sales_count  INTEGER NOT NULL DEFAULT 0,
  rating       NUMERIC(3, 2) NOT NULL DEFAULT 0,
  attributes   JSONB NOT NULL DEFAULT '{}',
  search_doc   TSVECTOR NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX products_search_gin_idx
  ON products USING GIN(search_doc);

CREATE INDEX products_name_trgm_idx
  ON products USING GIN(name gin_trgm_ops);

CREATE INDEX products_filter_idx
  ON products(status, category_id, price, id);

CREATE INDEX products_cursor_idx
  ON products (updated_at DESC, id DESC);
```

通过触发器或应用层生成 `search_doc`，并设置字段权重：

```sql
UPDATE products
SET search_doc =
    setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(brand, '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(description, '')), 'C');
```

中文分词要求较高时，可在写入前使用应用层分词器，将分词结果以空格连接后写入 `search_doc`。

## API

### 搜索商品

```http
GET /v1/products/search
```

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string | 搜索关键词 |
| `category_id` | integer | 分类过滤 |
| `brand` | string[] | 品牌过滤 |
| `min_price` | number | 最低价格 |
| `max_price` | number | 最高价格 |
| `in_stock` | boolean | 仅返回有库存商品 |
| `attributes` | object | JSON 属性过滤 |
| `sort` | string | `relevance`、`price_asc`、`price_desc`、`sales` |
| `limit` | integer | 1 至 100，默认 20 |
| `cursor` | string | 下一页游标 |

示例：

```http
GET /v1/products/search?q=无线耳机&brand=Acme&min_price=100&max_price=800&in_stock=true&limit=20
```

响应：

```json
{
  "items": [
    {
      "id": 101,
      "sku": "A-1001",
      "name": "Acme 无线降噪耳机",
      "brand": "Acme",
      "price": 599.00,
      "stock": 42,
      "rating": 4.8,
      "score": 0.927
    }
  ],
  "page": {
    "limit": 20,
    "next_cursor": "eyJzY29yZSI6MC45MjcsImlkIjoxMDF9"
  }
}
```

### 更新商品索引

商品写入与索引事件放在同一事务中：

```sql
CREATE TABLE search_outbox (
  id           BIGSERIAL PRIMARY KEY,
  product_id   BIGINT NOT NULL,
  event_type   TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);

CREATE INDEX search_outbox_pending_idx
  ON search_outbox(processed_at, id);
```

更新流程：

1. 修改 `products`。
2. 同事务写入 `search_outbox`。
3. Worker 批量消费事件。
4. 重新生成 `search_doc`。
5. 失败重试，超过次数进入死信表。
6. 定时任务执行全量校验和重建。

管理员接口：

```http
POST /v1/admin/search/reindex
```

```json
{
  "scope": "products",
  "product_ids": [101, 102]
}
```

返回：

```json
{
  "job_id": "reindex-20260822-001",
  "status": "queued"
}
```

## 查询与相关性排序

```sql
WITH query AS (
  SELECT websearch_to_tsquery('simple', :q) AS tsq
)
SELECT
  p.*,
  (
    0.65 * ts_rank_cd(p.search_doc, query.tsq) +
    0.20 * similarity(p.name, :q) +
    0.10 * LEAST(p.rating / 5.0, 1) +
    0.05 * LN(1 + p.sales_count)
  ) AS score
FROM products p, query
WHERE p.status = 'active'
  AND (:q = '' OR p.search_doc @@ query.tsq OR p.name % :q)
  AND (:category_id IS NULL OR p.category_id = :category_id)
  AND (:min_price IS NULL OR p.price >= :min_price)
  AND (:max_price IS NULL OR p.price <= :max_price)
  AND (:in_stock = false OR p.stock > 0)
ORDER BY score DESC, p.id DESC
LIMIT :limit;
```

过滤条件应在排序前应用。相关性排序时使用唯一的 `id` 作为并列排序键，保证分页稳定。

## 分页策略

优先使用 Cursor 分页，避免深页 `OFFSET` 性能下降。

游标内容至少包含：

```json
{
  "score": 0.927,
  "id": 101
}
```

下一页条件：

```sql
AND (
  score < :cursor_score
  OR (score = :cursor_score AND p.id < :cursor_id)
)
```

价格、销量排序同样使用对应排序字段加 `id` 作为游标。

## 错误响应

```json
{
  "error": {
    "code": "INVALID_PARAMETER",
    "message": "limit 必须介于 1 和 100 之间",
    "details": {
      "field": "limit"
    }
  }
}
```

建议错误码：

- `INVALID_PARAMETER`
- `QUERY_TOO_LONG`
- `REINDEX_ALREADY_RUNNING`
- `INTERNAL_ERROR`

## 测试设计

### 单元测试

- 查询参数解析与默认值
- 价格、库存、品牌过滤组合
- 排序字段白名单
- Cursor 编解码与非法游标
- 相关性分数计算
- 空关键词行为

### 集成测试

使用真实 PostgreSQL 测试容器验证：

- GIN 和 trigram 查询结果
- 关键词相关性排序
- 多条件过滤
- Cursor 翻页无重复、无遗漏
- 商品更新后索引最终一致
- Worker 重试与幂等
- 全量重建任务

### API 契约测试

验证：

- HTTP 状态码
- 响应 JSON Schema
- 分页字段
- 错误格式
- `limit > 100`、超长关键词等边界输入

### 性能测试

建立以下基线：

- 10 万、100 万、1000 万商品数据集
- 常见关键词与高频关键词
- 深分页请求
- 高并发读与持续索引更新并行

重点监控：

- P95/P99 延迟
- PostgreSQL CPU、IO、锁等待
- Outbox 积压量
- 索引更新延迟
- 慢查询比例

该设计只依赖 PostgreSQL，搜索、过滤、排序、分页和索引维护均可独立扩展。