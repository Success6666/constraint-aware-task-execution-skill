## 方案概览

使用 PostgreSQL 作为商品主库和搜索索引，不依赖 Elasticsearch：

- 全文检索：`tsvector` + `websearch_to_tsquery`
- 模糊匹配：`pg_trgm`
- 相关性排序：`ts_rank_cd` + 模糊匹配分数 + 业务权重
- 过滤：SQL 条件组合
- 分页：优先使用游标分页
- 索引更新：事务内更新搜索字段，配合 Outbox 异步重建
- 一致性：商品写入和索引事件处于同一事务

## 数据表

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE products (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    brand           TEXT,
    category_id     BIGINT,
    price           NUMERIC(12, 2) NOT NULL,
    stock           INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    popularity      NUMERIC(12, 4) NOT NULL DEFAULT 0,
    search_vector   TSVECTOR NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX products_search_vector_idx
    ON products USING GIN(search_vector);

CREATE INDEX products_title_trgm_idx
    ON products USING GIN(title gin_trgm_ops);

CREATE INDEX products_filter_idx
    ON products(status, category_id, price, id);

CREATE TABLE search_outbox (
    id          BIGSERIAL PRIMARY KEY,
    product_id  BIGINT NOT NULL,
    event_type  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
```

搜索字段可由标题、品牌、描述组成，并设置不同权重：

```sql
UPDATE products
SET search_vector =
    setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(brand, '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(description, '')), 'C');
```

生产环境建议使用中文分词扩展，或在应用层将中文切词后写入 `search_vector`。如果不能安装扩展，可采用“关键词表 + `pg_trgm`”的降级方案。

## API

### 搜索商品

```http
GET /v1/products/search
```

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string | 搜索词，可为空 |
| `category_id` | integer | 分类过滤 |
| `brand` | string | 品牌过滤 |
| `min_price` | number | 最低价格 |
| `max_price` | number | 最高价格 |
| `in_stock` | boolean | 是否有库存 |
| `sort` | string | `relevance`、`price_asc`、`price_desc`、`newest` |
| `limit` | integer | 默认 20，最大 100 |
| `cursor` | string | 游标 |

响应：

```json
{
  "items": [
    {
      "id": 1001,
      "title": "轻薄防水跑鞋",
      "brand": "Example",
      "price": 399.00,
      "stock": 28,
      "score": 0.873
    }
  ],
  "page": {
    "limit": 20,
    "next_cursor": "eyJzY29yZSI6MC44NzMsImlkIjoxMDAxfQ=="
  },
  "facets": {
    "brands": [
      { "value": "Example", "count": 42 }
    ],
    "categories": [
      { "value": 12, "count": 18 }
    ]
  }
}
```

### 商品写入

```http
POST /v1/products
PUT /v1/products/{id}
DELETE /v1/products/{id}
```

写入商品时：

1. 更新商品字段；
2. 同步计算 `search_vector`；
3. 同一事务插入 `search_outbox` 事件；
4. 提交后由后台任务处理增量索引或缓存刷新。

## 核心查询

```sql
WITH query AS (
    SELECT websearch_to_tsquery('simple', :q) AS tsq
)
SELECT
    p.id,
    p.title,
    p.brand,
    p.price,
    p.stock,
    (
        ts_rank_cd(p.search_vector, query.tsq) * 0.75 +
        similarity(p.title, :q) * 0.15 +
        LEAST(p.popularity / 100000.0, 1) * 0.10
    ) AS score
FROM products p
CROSS JOIN query
WHERE p.status = 'active'
  AND (
      :q = ''
      OR p.search_vector @@ query.tsq
      OR p.title % :q
  )
  AND (:category_id IS NULL OR p.category_id = :category_id)
  AND (:brand IS NULL OR p.brand = :brand)
  AND (:min_price IS NULL OR p.price >= :min_price)
  AND (:max_price IS NULL OR p.price <= :max_price)
  AND (:in_stock = false OR p.stock > 0)
ORDER BY score DESC, p.id DESC
LIMIT :limit;
```

无关键词时，`score` 可替换为销量、更新时间或运营权重。

## 分页

避免深度 `OFFSET`，使用稳定游标：

```sql
WHERE (
    score < :last_score
    OR (score = :last_score AND p.id < :last_id)
)
ORDER BY score DESC, p.id DESC
LIMIT :limit;
```

游标应包含：

```json
{
  "score": 0.873,
  "id": 1001
}
```

并使用 Base64 编码，必要时增加签名防止客户端篡改。

## 索引更新策略

- 商品创建、修改、删除：写入 Outbox。
- 后台 Worker 使用 `FOR UPDATE SKIP LOCKED` 批量消费。
- 失败事件重试，超过次数进入死信表。
- 定时执行全量校验，修复遗漏索引。
- 搜索读取使用数据库只读副本时，要明确接受复制延迟。
- 对高流量热门查询增加 Redis 缓存，缓存键包含查询词、过滤条件和排序方式。

## 测试设计

### 单元测试

- 查询词为空、单词、多词、特殊字符。
- 标题匹配优先于描述匹配。
- 价格、品牌、分类、库存过滤组合。
- 各种排序方式。
- 游标生成和解析。
- 非法参数、超出最大 `limit`、负价格。

### 集成测试

使用真实 PostgreSQL 容器测试：

- 商品写入后可被搜索。
- 修改标题后旧关键词不再命中。
- 删除商品后不可搜索。
- Outbox 重试和幂等消费。
- 全文匹配与模糊匹配结果。
- 分页无重复、无遗漏。

### 性能测试

准备至少百万级商品数据，验证：

- 常见关键词 P95 延迟；
- 过滤条件组合下的查询计划；
- 深分页性能；
- 批量索引更新吞吐；
- 使用 `EXPLAIN (ANALYZE, BUFFERS)` 确认 GIN 和过滤索引生效。

该方案适合中等规模商品目录；当数据量或查询吞吐继续增长时，可以将 PostgreSQL 搜索模块替换为专用搜索服务，但 API 契约无需改变。