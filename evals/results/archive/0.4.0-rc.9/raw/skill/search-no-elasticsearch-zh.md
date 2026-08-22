## 商品搜索 API 设计

采用 PostgreSQL 内置全文检索能力实现，不依赖 Elasticsearch：

- `tsvector` + GIN：全文检索
- `ts_rank_cd`：相关性排序
- `pg_trgm`：中文/模糊匹配补充
- 普通索引或组合索引：过滤、分页
- Outbox 事件表：可靠地更新搜索索引

### 1. 数据模型

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE products (
    id              BIGSERIAL PRIMARY KEY,
    sku             VARCHAR(64) NOT NULL UNIQUE,
    title           VARCHAR(255) NOT NULL,
    description     TEXT,
    category_id     BIGINT NOT NULL,
    brand           VARCHAR(100),
    price           NUMERIC(12, 2) NOT NULL,
    stock           INTEGER NOT NULL DEFAULT 0,
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
    sales_count     INTEGER NOT NULL DEFAULT 0,
    rating          NUMERIC(3, 2) NOT NULL DEFAULT 0,
    search_vector   TSVECTOR NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_products_search_vector
    ON products USING GIN (search_vector);

CREATE INDEX idx_products_title_trgm
    ON products USING GIN (title gin_trgm_ops);

CREATE INDEX idx_products_category_status
    ON products (category_id, status);

CREATE INDEX idx_products_price
    ON products (price);

CREATE INDEX idx_products_created_at
    ON products (created_at DESC);
```

`search_vector` 应对标题、品牌、分类名、描述设置不同权重：

```sql
UPDATE products p
SET search_vector =
      setweight(to_tsvector('simple', coalesce(p.title, '')), 'A')
   || setweight(to_tsvector('simple', coalesce(p.brand, '')), 'B')
   || setweight(to_tsvector('simple', coalesce(p.description, '')), 'C');
```

中文环境建议：

1. 使用应用层分词后，将词语以空格拼接后写入 `search_vector`。
2. 或安装 PostgreSQL 中文分词扩展，例如 `zhparser`，由部署环境统一管理。
3. 对短词、SKU、品牌名同时保留 `pg_trgm` 查询路径。

### 2. API

#### 搜索商品

```http
GET /api/v1/products/search
```

参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `q` | string | 搜索关键词，可为空 |
| `category_id` | integer | 分类过滤 |
| `brand` | string[] | 品牌过滤 |
| `min_price` | decimal | 最低价格 |
| `max_price` | decimal | 最高价格 |
| `in_stock` | boolean | 是否有库存 |
| `status` | string | 默认 `active` |
| `sort` | string | `relevance`、`price_asc`、`price_desc`、`newest`、`sales` |
| `page` | integer | 页码，从 1 开始 |
| `page_size` | integer | 默认 20，最大 100 |

响应：

```json
{
  "items": [
    {
      "id": 1001,
      "sku": "PHONE-001",
      "title": "智能降噪耳机",
      "brand": "Acme",
      "price": 399.00,
      "stock": 28,
      "score": 0.812
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 134,
  "has_next": true
}
```

#### 创建或更新商品

```http
POST /api/v1/products
PUT /api/v1/products/{id}
```

写入商品数据时同步生成 `search_vector`，并记录索引事件。

#### 删除商品

```http
DELETE /api/v1/products/{id}
```

建议采用软删除，将 `status` 设为 `deleted`，同时产生索引删除事件。

#### 重建索引

```http
POST /api/v1/products/reindex
```

仅供管理员或内部任务使用，支持：

```json
{
  "from_id": 1,
  "to_id": 100000
}
```

### 3. 查询实现

关键词查询：

```sql
WITH query AS (
    SELECT websearch_to_tsquery('simple', :q) AS tsq
)
SELECT
    p.id,
    p.sku,
    p.title,
    p.brand,
    p.price,
    p.stock,
    ts_rank_cd(p.search_vector, query.tsq) AS score
FROM products p, query
WHERE p.status = 'active'
  AND p.search_vector @@ query.tsq
  AND (:category_id IS NULL OR p.category_id = :category_id)
  AND (:min_price IS NULL OR p.price >= :min_price)
  AND (:max_price IS NULL OR p.price <= :max_price)
  AND (:in_stock = false OR p.stock > 0)
ORDER BY
    ts_rank_cd(p.search_vector, query.tsq) DESC,
    p.sales_count DESC,
    p.id DESC
LIMIT :page_size
OFFSET (:page - 1) * :page_size;
```

无关键词时使用业务排序：

```sql
ORDER BY
  CASE WHEN :sort = 'price_asc'  THEN p.price END ASC,
  CASE WHEN :sort = 'price_desc' THEN p.price END DESC,
  CASE WHEN :sort = 'newest'     THEN p.created_at END DESC,
  CASE WHEN :sort = 'sales'      THEN p.sales_count END DESC,
  p.id DESC;
```

短词或模糊查询可补充：

```sql
WHERE p.title % :q
ORDER BY similarity(p.title, :q) DESC
```

生产环境建议使用游标分页替代深页 `OFFSET`：

```http
GET /api/v1/products/search?q=耳机&cursor=eyJzY29yZSI6MC44...
```

游标至少包含：

```json
{
  "score": 0.812,
  "id": 1001
}
```

下一页条件：

```sql
AND (
  score < :last_score
  OR (score = :last_score AND p.id < :last_id)
)
```

### 4. 索引更新机制

推荐使用事务内 Outbox：

```sql
CREATE TABLE product_index_events (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL,
    event_type      VARCHAR(20) NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX idx_index_events_pending
    ON product_index_events (id)
    WHERE processed_at IS NULL;
```

商品更新事务同时执行：

```text
BEGIN
  更新 products
  插入 product_index_events
COMMIT
```

后台 worker：

1. 批量读取未处理事件。
2. 更新或删除商品索引字段。
3. 成功后设置 `processed_at`。
4. 失败重试，超过阈值进入错误表或告警。
5. 定时任务执行增量校验和全量重建。

如果搜索直接基于 `products.search_vector`，商品更新后可由数据库触发器维护：

```sql
CREATE FUNCTION products_search_vector_update()
RETURNS trigger AS $$
BEGIN
  NEW.search_vector :=
      setweight(to_tsvector('simple', coalesce(NEW.title, '')), 'A')
   || setweight(to_tsvector('simple', coalesce(NEW.brand, '')), 'B')
   || setweight(to_tsvector('simple', coalesce(NEW.description, '')), 'C');
  NEW.updated_at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

### 5. 服务分层

```text
HTTP Controller
    -> SearchService
        -> QueryBuilder
        -> ProductRepository
        -> Cursor/PageToken Codec

ProductCommandService
    -> ProductRepository
    -> OutboxRepository

IndexWorker
    -> OutboxRepository
    -> ProductIndexRepository
```

关键职责：

- Controller：参数校验、响应格式
- SearchService：组合关键词、过滤条件、排序和分页
- Repository：参数化 SQL，避免注入
- IndexWorker：异步处理更新事件
- ReindexJob：分批重建，避免长事务

### 6. 测试设计

#### 单元测试

- 关键词为空时不生成全文条件。
- `q` 正确转换为 `tsquery`。
- 分类、品牌、价格、库存过滤组合正确。
- 每种 `sort` 生成正确排序。
- 页码、`page_size` 边界值。
- 游标编码、解码和非法游标处理。
- 搜索结果的相关性排序和稳定的 `id` 次排序。

#### 集成测试

使用测试 PostgreSQL 数据库，插入固定商品数据，验证：

- 标题命中优先于描述命中。
- 多关键词搜索结果。
- 中文词、SKU、品牌和模糊词。
- 过滤后 `total`、`has_next` 正确。
- 分页无重复、无遗漏。
- 商品更新后搜索结果可见。
- 删除商品后不可搜索。
- Outbox 事件可重试且不会重复造成错误。
- 重建索引前后结果一致。

#### API 测试示例

```text
GET /products/search?q=降噪&sort=relevance
  -> 200
  -> 第一条标题包含“降噪”
  -> score >= 第二条 score

GET /products/search?min_price=100&max_price=500&in_stock=true
  -> 所有结果价格在区间内且 stock > 0

GET /products/search?page=2&page_size=2
  -> 返回第二页
  -> 与第一页没有重复 id

PUT /products/1001
  -> 修改标题
  -> 等待索引处理
  -> 新标题可被搜索
```

#### 性能测试

重点观察：

- 常见关键词查询 P95/P99 延迟
- 无关键词、多过滤条件查询
- 深页查询
- 批量索引更新吞吐
- GIN 索引大小和写入开销

测试数据至少覆盖几十万商品，并为常见查询建立基准。