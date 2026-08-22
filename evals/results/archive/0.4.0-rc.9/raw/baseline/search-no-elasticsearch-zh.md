推荐使用 **PostgreSQL 16 + 应用层中文分词 + PostgreSQL FTS/pg_trgm**。商品数据、过滤条件和搜索索引在同一数据库中，避免 Elasticsearch 带来的双写一致性与运维成本。

## 1. 总体架构

```text
商品写入 API
    │
    ├─ 同一事务更新 products
    └─ 写入 search_outbox
              │
         异步索引任务
              │ 分词、同义词展开、归一化
              ▼
       product_search_index
              │
商品搜索 API ── PostgreSQL FTS + 条件过滤 + 游标分页
```

中文分词在应用层完成，可使用 jieba、HanLP 等。分词结果用空格连接后写入 `tsvector`，数据库使用 `simple` 配置，避免依赖数据库特有的中文分词扩展。

## 2. 数据模型

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE products (
  id           bigint PRIMARY KEY,
  name         text NOT NULL,
  subtitle     text,
  category_id  bigint NOT NULL,
  brand_id     bigint,
  price        numeric(12, 2) NOT NULL,
  sales_count  bigint NOT NULL DEFAULT 0,
  rating       numeric(3, 2) NOT NULL DEFAULT 0,
  status       smallint NOT NULL,
  attributes   jsonb NOT NULL DEFAULT '{}',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE product_search_index (
  product_id      bigint PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
  name_tokens     tsvector NOT NULL,
  subtitle_tokens tsvector NOT NULL,
  attribute_tokens tsvector NOT NULL,
  normalized_name text NOT NULL,
  indexed_version timestamptz NOT NULL
);

CREATE INDEX idx_product_search_document
ON product_search_index USING gin (
  (setweight(name_tokens, 'A') ||
   setweight(subtitle_tokens, 'B') ||
   setweight(attribute_tokens, 'C'))
);

CREATE INDEX idx_product_search_name_trgm
ON product_search_index USING gin (normalized_name gin_trgm_ops);

CREATE INDEX idx_products_filter
ON products (status, category_id, brand_id, price, id);

CREATE TABLE search_outbox (
  product_id bigint PRIMARY KEY,
  product_version timestamptz NOT NULL,
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now()
);
```

商品更新与 `search_outbox` 写入必须处于同一事务。任务处理后仅在 `indexed_version >= product_version` 时删除事件，保证乱序重试不会覆盖新索引。

## 3. 搜索 API

```http
POST /v1/products/search
Content-Type: application/json
```

```json
{
  "query": "无线降噪耳机",
  "filters": {
    "categoryIds": [1201],
    "brandIds": [10, 20],
    "price": {"min": 300, "max": 1500},
    "attributes": {"color": ["黑色"], "connection": ["蓝牙"]},
    "inStock": true
  },
  "sort": "relevance",
  "pageSize": 20,
  "cursor": null
}
```

```json
{
  "items": [
    {
      "id": 12345,
      "name": "无线蓝牙降噪耳机",
      "price": 799,
      "score": 0.8731,
      "highlights": {"name": "无线蓝牙<em>降噪耳机</em>"}
    }
  ],
  "nextCursor": "eyJzY29yZSI6MC44NzMxLCJpZCI6MTIzNDV9",
  "total": 286,
  "totalRelation": "eq",
  "tookMs": 18
}
```

约束：

- `pageSize`：默认 20，最大 100。
- `sort`：`relevance`、`price_asc`、`price_desc`、`sales_desc`、`newest`。
- `query` 为空时只执行过滤，默认按销量或更新时间排序。
- 未知过滤字段返回 `400`，不允许客户端直接传 SQL 字段或排序表达式。
- `total` 可设置计算上限，例如超过 10,000 后返回 `totalRelation: "gte"`。

## 4. 查询与相关性

分词后用 `websearch_to_tsquery('simple', :tokens)` 构造查询。基础评分：

```text
score =
  0.70 × 全文匹配排名
+ 0.15 × 商品名相似度
+ 0.10 × 销量归一化得分
+ 0.05 × 评分归一化得分
```

核心 SQL 可采用：

```sql
WITH candidates AS (
  SELECT p.*, i.normalized_name,
         ts_rank_cd(
           setweight(i.name_tokens, 'A') ||
           setweight(i.subtitle_tokens, 'B') ||
           setweight(i.attribute_tokens, 'C'),
           :query
         ) AS text_rank
  FROM products p
  JOIN product_search_index i ON i.product_id = p.id
  WHERE p.status = 1
    AND (:category_ids IS NULL OR p.category_id = ANY(:category_ids))
    AND (:brand_ids IS NULL OR p.brand_id = ANY(:brand_ids))
    AND (:min_price IS NULL OR p.price >= :min_price)
    AND (:max_price IS NULL OR p.price <= :max_price)
    AND (
      :query IS NULL OR
      (i.name_tokens || i.subtitle_tokens || i.attribute_tokens) @@ :query
    )
)
SELECT *,
       0.70 * text_rank
     + 0.15 * similarity(normalized_name, :normalized_query)
     + 0.10 * LEAST(1, ln(1 + sales_count) / 15)
     + 0.05 * rating / 5 AS score
FROM candidates
ORDER BY score DESC, id DESC
LIMIT :page_size_plus_one;
```

同义词应在查询阶段有限展开，例如“手机 → 移动电话”，并限制词数和展开数量，防止查询膨胀。拼写容错仅在全文无结果或结果过少时启用 `pg_trgm`，否则容易降低精确搜索性能。

## 5. 分页

不使用大偏移量 `OFFSET`。相关性排序的游标包含：

```json
{"score": 0.8731, "id": 12345, "queryHash": "..."}
```

后续页增加条件：

```sql
WHERE (score, id) < (:last_score, :last_id)
ORDER BY score DESC, id DESC
```

游标应使用服务端密钥签名，并包含查询条件哈希，防止游标被用于另一组过滤条件。若要求多页结果绝对稳定，还应加入 `searchSnapshot` 或索引版本。

## 6. 索引更新

- 新增、修改、删除商品时写入 outbox。
- Worker 批量领取事件，生成名称、描述、品牌、类目、关键属性的分词结果。
- Upsert 时比较 `indexed_version`，旧事件不能覆盖新事件。
- 失败采用指数退避，超过阈值进入死信表并告警。
- 提供管理接口：`POST /internal/search/reindex/products/{id}`。
- 全量重建写入新索引表，校验数量与抽样结果后通过事务切换视图，避免清空在线索引。
- 监控索引延迟、失败率、空结果率、P95/P99 延迟和慢查询。

## 7. 测试策略

- 单元测试：归一化、中文分词、同义词、评分函数、游标编解码。
- API 测试：组合过滤、边界价格、非法参数、空查询、各种排序和分页连续性。
- 相关性测试：维护固定查询集及期望前 N 名，计算 Precision@K、MRR、NDCG。
- 索引一致性测试：重复事件、乱序事件、删除事件、Worker 崩溃重试。
- 集成测试：使用真实 PostgreSQL，不用内存数据库替代 FTS、GIN 和 JSONB 行为。
- 性能测试：按生产数据量构造商品和属性分布，验证常用查询 P95，并覆盖罕见词、热门词和高基数过滤。
- 安全测试：超长查询、大量同义词、非法排序字段、游标篡改及通配符滥用。

该方案适合中等规模商品库。若数据达到数千万级且写入和查询压力很高，可保持 API、outbox 和排序协议不变，将检索层替换为 OpenSearch 之外的专用方案，如 Typesense 或 Meilisearch，但会重新引入独立索引的一致性与运维问题。