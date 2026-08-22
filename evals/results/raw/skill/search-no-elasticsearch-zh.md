# 商品搜索 API 设计

## 1. 技术方案

使用 **PostgreSQL** 作为商品数据与搜索引擎：

- `tsvector` + `GIN`：中文分词后的全文检索
- `pg_trgm` + `GIN/GiST`：前缀、模糊匹配和拼写相近查询
- SQL 组合计算相关性、业务权重和过滤条件
- Redis 可选作热点查询缓存，但不作为搜索数据源
- 通过 Outbox + 异步 Worker 更新搜索索引，避免商品写入与索引更新强耦合

中文分词建议使用 PostgreSQL 中文分词扩展，或在应用层使用统一分词器生成 token 后写入 `search_vector`。生产环境必须固定分词器版本，避免同一商品在不同节点生成不同索引。

## 2. 数据模型

### 商品表

```sql
CREATE TABLE products (
  id              BIGINT PRIMARY KEY,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  category_id     BIGINT,
  brand_id        BIGINT,
  price           NUMERIC(12,2) NOT NULL,
  stock           INTEGER NOT NULL DEFAULT 0,
  status          SMALLINT NOT NULL, -- 1=上架，0=下架
  sales_count     BIGINT NOT NULL DEFAULT 0,
  rating          NUMERIC(3,2) NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL,
  search_vector   TSVECTOR NOT NULL
);

CREATE INDEX products_search_vector_gin
  ON products USING GIN (search_vector);
CREATE INDEX products_title_trgm_gin
  ON products USING GIN (title gin_trgm_ops);
CREATE INDEX products_category_idx ON products(category_id);
CREATE INDEX products_brand_idx ON products(brand_id);
CREATE INDEX products_price_idx ON products(price);
CREATE INDEX products_status_idx ON products(status);
```

### Outbox 表

```sql
CREATE TABLE product_search_outbox (
  id           BIGSERIAL PRIMARY KEY,
  product_id   BIGINT NOT NULL,
  event_type   VARCHAR(32) NOT NULL, -- upsert/delete
  version      BIGINT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);
CREATE INDEX outbox_pending_idx
  ON product_search_outbox(created_at)
  WHERE processed_at IS NULL;
```

商品变更和 Outbox 事件在同一个事务中提交，保证不会出现商品已更新但没有索引任务的情况。

## 3. 索引内容与权重

将字段按重要性映射不同权重：

```sql
setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(brand_name, '')), 'A') ||
setweight(to_tsvector('simple', coalesce(category_name, '')), 'B') ||
setweight(to_tsvector('simple', coalesce(description, '')), 'C')
```

实际中文项目中，`simple` 应替换为确定的中文分词配置或预分词 token。

建议相关性由以下部分组成：

```text
score =
  0.60 * text_rank
+ 0.15 * title_exact_or_prefix
+ 0.10 * popularity_score
+ 0.10 * sales_score
+ 0.05 * rating_score
```

其中业务分数先归一化到 `[0,1]`，避免销量或价格量纲影响全文相关性。无关键词时使用业务排序，不计算全文 rank。

## 4. API

### 搜索

`GET /v1/products/search`

请求参数：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `q` | string | 否 | 搜索词，长度 1–100 |
| `category_id` | long | 否 | 分类过滤 |
| `brand_ids` | array | 否 | 品牌过滤 |
| `min_price` / `max_price` | decimal | 否 | 价格区间 |
| `in_stock` | boolean | 否 | 是否仅库存大于 0 |
| `status` | string | 否 | 默认只返回上架商品 |
| `sort` | enum | 否 | `relevance`、`price_asc`、`price_desc`、`sales`、`newest` |
| `cursor` | string | 否 | 游标分页位置 |
| `limit` | int | 否 | 默认 20，最大 100 |

响应：

```json
{
  "items": [
    {
      "id": 1001,
      "title": "无线降噪耳机",
      "price": 399.00,
      "brand_id": 20,
      "category_id": 8,
      "stock": 12,
      "score": 0.932
    }
  ],
  "next_cursor": "eyJzY29yZSI6MC45...",
  "has_more": true,
  "total": null
}
```

`total` 默认不返回精确值，避免大结果集 `COUNT(*)` 造成额外开销；如业务确实需要，可增加 `include_total=true`，并限制其使用频率。

### 商品索引更新

内部接口：

- `PUT /internal/v1/search/products/{id}`：重新索引单个商品
- `DELETE /internal/v1/search/products/{id}`：删除商品索引
- `POST /internal/v1/search/rebuild`：按批次重建索引
- `GET /internal/v1/search/tasks/{id}`：查询重建任务状态

内部接口要求服务鉴权、幂等和操作审计，不能直接暴露给公网。

## 5. 查询实现

### 有关键词

```sql
WITH query AS (
  SELECT websearch_to_tsquery('simple', :q) AS tsq
)
SELECT
  p.id, p.title, p.price, p.brand_id, p.category_id, p.stock,
  ts_rank_cd(p.search_vector, query.tsq) AS text_rank,
  (
    0.60 * ts_rank_cd(p.search_vector, query.tsq)
    + 0.15 * CASE
        WHEN lower(p.title) = lower(:q) THEN 1
        WHEN lower(p.title) LIKE lower(:q) || '%' THEN 0.7
        ELSE 0
      END
    + 0.10 * normalized_sales
    + 0.10 * normalized_rating
  ) AS score
FROM products p, query
WHERE p.status = 1
  AND p.search_vector @@ query.tsq
  AND (:category_id IS NULL OR p.category_id = :category_id)
  AND (:in_stock = false OR p.stock > 0)
  AND (:min_price IS NULL OR p.price >= :min_price)
  AND (:max_price IS NULL OR p.price <= :max_price)
ORDER BY score DESC, p.id DESC
LIMIT :limit;
```

对只有模糊词、拼写错误或全文查询无结果的请求，可使用 `pg_trgm` 进行降级匹配：

```sql
WHERE similarity(title, :q) >= 0.25
ORDER BY similarity(title, :q) DESC, id DESC
```

降级阈值应通过测试数据调优，且必须继续应用上架状态和所有过滤条件。

### 无关键词

跳过全文条件，使用 `newest`、`sales` 或业务默认排序，并保留全部过滤条件。

## 6. 分页

采用 **keyset/cursor pagination**，不使用深页 `OFFSET`：

- 相关性排序：游标包含 `score` 和 `id`
- 价格排序：游标包含 `price` 和 `id`
- 时间排序：游标包含 `updated_at` 和 `id`
- 游标使用 Base64 编码并签名，客户端不可修改
- 排序必须有唯一的 `id` 作为最终 tie-breaker

这样可避免数据变化导致重复或漏项，并保持深分页性能稳定。

## 7. 索引更新流程

1. 商品服务在商品新增、修改、上下架、库存影响搜索条件时写入 Outbox。
2. Worker 批量读取未处理事件，按 `product_id` 合并重复事件。
3. Worker 读取最新商品数据，生成 `search_vector` 并更新商品记录。
4. 使用 `version` 或 `updated_at` 防止旧事件覆盖新数据。
5. 成功后标记 Outbox；失败则指数退避重试。
6. 删除商品时执行软删除或从可搜索状态移除。
7. 提供全量重建：新建临时索引/表，批量导入并校验数量后切换；重建期间继续消费增量事件。

索引更新应具备：

- 至少一次投递、幂等消费
- 延迟指标：事件创建到可搜索的 P50/P95
- 待处理数量、失败数量、重试次数监控
- 定期抽样校验数据库商品与可搜索结果

## 8. 错误与限制

- `q` 为空时执行无关键词搜索。
- 参数非法返回 `400`；游标失效返回 `400`。
- 结果为空返回空数组，不返回错误。
- 查询超时返回 `503`，并记录慢查询日志。
- 限制单次 `limit <= 100`、关键词长度、过滤数组大小和请求频率。
- 对 SQL 全部使用参数绑定，禁止拼接用户输入。

## 9. 测试方案

### 单元测试

- 分词、字段权重和相关性计算
- 精确词、前缀词、多个词、特殊字符和空关键词
- 价格、分类、品牌、库存、上下架过滤
- 每种排序及相同排序值时的 `id` 稳定排序
- 游标编码、解码、过期和篡改检测
- Outbox 合并、重复消费、乱序事件和版本控制

### 集成测试

使用真实 PostgreSQL 测试实例验证：

- GIN/Trigram 索引被使用
- 全文匹配和模糊降级
- 过滤条件组合
- 索引更新最终可见
- 删除和重新上架
- 重建期间增量更新不丢失

使用 `EXPLAIN (ANALYZE, BUFFERS)` 检查典型查询没有全表扫描，并为商品规模增长建立性能基线。

### API 契约测试

验证：

- 请求参数和错误码
- 响应字段、游标分页和无重复结果
- 默认只返回上架商品
- `sort`、过滤条件和关键词组合
- 权限、限流和内部索引接口不可公网访问

### 相关性测试

准备带人工标注的查询集，计算 `Precision@10`、`Recall@10`、`MRR@10` 或 `NDCG@10`，对比：

1. 仅全文 rank
2. 全文 rank + 字段权重
3. 全文 rank + 业务排序
4. 模糊降级结果

上线前设置最低指标和回归样本，任何分词器、权重或排序公式变更都必须重新评估。

## 10. 推荐交付顺序

1. 商品表、`search_vector`、全文查询和基础过滤。
2. 游标分页及稳定排序。
3. Outbox、增量 Worker 和重试机制。
4. 模糊匹配、业务相关性和缓存。
5. 全量重建、监控、相关性评测和压力测试。
