# 商品搜索 API 设计

## 1. 技术方案

使用 PostgreSQL 构建搜索能力，不依赖 Elasticsearch：

- PostgreSQL `tsvector`：全文检索和词语匹配
- `zhparser` 或应用层中文分词器：中文分词
- `pg_trgm`：拼写容错、商品编码、SKU 和前缀匹配
- GIN 索引：全文检索
- B-Tree 索引：过滤、排序和分页
- Outbox 表：保证商品变更最终同步到搜索索引

适用范围：百万到千万级商品、常规电商搜索、秒级索引延迟要求。超大规模或复杂聚合场景再考虑独立搜索集群。

## 2. 数据模型

### 商品表

```sql
CREATE TABLE products (
    id              BIGINT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    brand_id        BIGINT,
    category_id     BIGINT,
    price           NUMERIC(12, 2) NOT NULL,
    stock           INTEGER NOT NULL DEFAULT 0,
    status          SMALLINT NOT NULL,
    sales_count     BIGINT NOT NULL DEFAULT 0,
    rating          NUMERIC(3, 2) NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_products_filter
ON products (status, category_id, brand_id);

CREATE INDEX idx_products_price
ON products (price);

CREATE INDEX idx_products_sales
ON products (sales_count DESC);
```

### 搜索文档表

搜索文档与商品表分离，避免搜索时拼接大量关联表。

```sql
CREATE TABLE product_search_documents (
    product_id      BIGINT PRIMARY KEY REFERENCES products(id),
    search_vector   TSVECTOR NOT NULL,
    search_text     TEXT NOT NULL,
    index_version   INTEGER NOT NULL DEFAULT 1,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_product_search_vector
ON product_search_documents
USING GIN (search_vector);

CREATE INDEX idx_product_search_text_trgm
ON product_search_documents
USING GIN (search_text gin_trgm_ops);
```

推荐权重：

| 字段 | 权重 |
|---|---|
| 商品名称 | A |
| 品牌名称 | B |
| 分类名称 | B |
| SKU、商品编码 | A |
| 搜索关键词 | B |
| 商品描述 | C |

文档内容示例：

```text
商品名称: 苹果 iPhone 15 Pro
品牌: Apple
分类: 手机
关键词: 苹果手机 智能手机 5G
SKU: IP15PRO256
描述: ...
```

构建 `tsvector` 时，对中文文本使用统一分词器。分词结果必须由写入索引和查询解析共用，避免索引词和查询词不一致。

## 3. API

### 搜索商品

```http
GET /v1/products/search
```

参数：

```text
q                  搜索关键词，必填，长度 1-200
category_id        分类 ID，可重复传递
brand_id           品牌 ID，可重复传递
min_price          最低价格
max_price          最高价格
in_stock           是否仅库存大于 0
status             商品状态，默认只搜索上架商品
sort               relevance | price_asc | price_desc | sales | newest
page_size          1-100，默认 20
cursor             下一页游标
```

请求示例：

```http
GET /v1/products/search?q=无线降噪耳机&category_id=10&in_stock=true&sort=relevance&page_size=20
```

响应：

```json
{
  "items": [
    {
      "id": 10001,
      "name": "XX 无线降噪耳机 Pro",
      "brand_id": 20,
      "category_id": 10,
      "price": 899.00,
      "stock": 32,
      "score": 12.481
    }
  ],
  "page": {
    "page_size": 20,
    "next_cursor": "eyJzY29yZSI6MTIuNDgxLCJpZCI6MTAwMDF9",
    "has_more": true
  },
  "took_ms": 8
}
```

搜索无结果时返回空数组，不返回 404。

### 管理索引

内部管理 API：

```http
POST /internal/search/index/products/{product_id}
POST /internal/search/index/rebuild
GET  /internal/search/index/status
```

权限要求：

- 仅内部服务访问
- 管理接口需要管理员权限
- 重建任务必须支持异步执行、进度查看和失败重试

## 4. 相关性排序

全文检索基础分使用 PostgreSQL `ts_rank_cd`：

```sql
ts_rank_cd(
    d.search_vector,
    query,
    32
)
```

综合得分：

```text
final_score =
    0.70 * text_score
  + 0.10 * normalized_sales_score
  + 0.08 * normalized_rating_score
  + 0.07 * stock_score
  + 0.05 * freshness_score
```

其中：

```text
normalized_sales_score = ln(1 + sales_count) / ln(1 + max_sales)
normalized_rating_score = rating / 5
stock_score             = 1, if stock > 0
freshness_score         = exp(-age_days / 180)
```

具体权重应通过线上点击率、加购率和转化率持续调整。

额外排序规则：

1. 商品名称完全匹配优先
2. 商品名称前缀匹配优先
3. 品牌、分类匹配次之
4. 描述字段匹配最低
5. 库存商品优先
6. 最终使用 `product_id` 作为稳定排序字段

## 5. 查询示例

```sql
WITH search_query AS (
    SELECT
        websearch_to_tsquery('simple', :query_text) AS query
),
matched AS (
    SELECT
        p.*,
        ts_rank_cd(
            d.search_vector,
            q.query,
            32
        ) AS text_score
    FROM products p
    JOIN product_search_documents d
      ON d.product_id = p.id
    CROSS JOIN search_query q
    WHERE
        p.status = 1
        AND d.search_vector @@ q.query
        AND (:category_ids IS NULL OR p.category_id = ANY(:category_ids))
        AND (:brand_ids IS NULL OR p.brand_id = ANY(:brand_ids))
        AND (:min_price IS NULL OR p.price >= :min_price)
        AND (:max_price IS NULL OR p.price <= :max_price)
        AND (:in_stock = false OR p.stock > 0)
)
SELECT *
FROM matched
ORDER BY
    text_score DESC,
    sales_count DESC,
    product_id ASC
LIMIT :limit;
```

生产实现中应避免 `OR :param IS NULL` 造成计划不稳定，按过滤参数动态生成固定 SQL 模板。

## 6. 分页设计

默认使用游标分页，不使用深分页 `OFFSET`。

游标包含：

```json
{
  "sort": "relevance",
  "score": 12481000,
  "product_id": 10001,
  "query_hash": "..."
}
```

分数使用整数化值，例如：

```text
score_key = floor(final_score * 1,000,000)
```

相关性排序条件：

```sql
WHERE
    score_key < :cursor_score
    OR (
        score_key = :cursor_score
        AND product_id > :cursor_product_id
    )
ORDER BY score_key DESC, product_id ASC
```

游标必须绑定：

- 查询关键词
- 所有过滤条件
- 排序方式
- 索引版本

防止客户端修改游标后复用到其他查询。

`price_asc` 等非相关性排序使用：

```sql
ORDER BY price ASC, product_id ASC
```

游标只保存排序字段和 `product_id`。

## 7. 索引更新

采用事务写入加 Outbox：

```sql
CREATE TABLE search_index_outbox (
    id              BIGSERIAL PRIMARY KEY,
    product_id      BIGINT NOT NULL,
    event_type      TEXT NOT NULL,
    event_version   BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, event_version)
);
```

商品更新事务：

```text
BEGIN
1. 更新 products
2. 写入 search_index_outbox
3. COMMIT
```

后台索引消费者：

```text
1. 批量领取 pending 事件
2. 读取商品及关联的品牌、分类、关键词
3. 使用统一分词器生成 search_vector
4. UPSERT product_search_documents
5. 标记事件为 completed
6. 失败时增加 retry_count，并使用指数退避
```

删除商品时：

```text
1. 删除或标记 product_search_documents
2. 商品状态更新为下架
3. 写入删除事件
```

搜索查询还必须过滤商品状态，避免索引延迟期间返回已下架商品。

### 全量重建

使用新版本索引：

```text
1. 创建 product_search_documents_v2
2. 批量扫描商品生成索引
3. 校验文档数量和抽样搜索结果
4. 切换查询使用的索引版本
5. 继续消费增量事件
6. 删除旧版本
```

全量重建不能阻塞商品写入和正常搜索。

## 8. 容错和一致性

- 商品主数据以 `products` 为准
- 搜索索引允许最终一致
- 商品下架优先通过主表过滤立即生效
- Outbox 消费采用幂等 UPSERT
- 事件按 `event_version` 防止旧事件覆盖新数据
- 连续失败事件进入死信队列
- 监控以下指标：
  - 索引延迟
  - Outbox pending 数量
  - 索引失败率
  - 搜索 P95/P99 延迟
  - 空结果率
  - 搜索点击率
  - 搜索转化率

## 9. 测试方案

### 单元测试

覆盖：

- 查询参数校验
- 价格区间校验
- `page_size` 上限
- 查询词分词结果
- 相关性得分计算
- 综合排序规则
- 游标编码、解码和非法游标
- 游标与查询条件不匹配
- 索引事件幂等处理
- 事件版本覆盖规则

### 集成测试

使用真实 PostgreSQL 和测试扩展，验证：

- 中文全文检索
- 英文和数字混合检索
- SKU 精确匹配
- 拼写或部分匹配
- 分类、品牌、价格、库存过滤
- 多条件组合过滤
- 相关性排序
- 各种排序模式
- 下一页游标结果不重复、不遗漏
- 商品更新后最终出现在搜索结果中
- 商品下架后不会被返回
- 重复消费 Outbox 不产生错误数据
- 全量重建后索引结果一致

### API 测试

验证：

- 正常搜索
- 空查询
- 超长查询
- 非法价格
- 非法排序方式
- 非法游标
- 空结果
- 默认分页大小和最大分页大小
- 权限不足时拒绝管理接口

### 性能测试

准备至少：

- 100 万商品数据
- 真实中文商品名称分布
- 热门词和长尾词
- 多过滤条件组合
- 深层游标分页

目标基线：

```text
P95 < 100 ms
P99 < 300 ms
索引增量延迟 < 5 秒
重复分页结果率为 0
```

性能测试同时检查 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)`，确保使用 GIN、B-Tree 索引，并避免全表扫描。

## 10. 推荐默认行为

- 默认只搜索上架商品
- 默认按相关性排序
- 默认 `page_size=20`
- 最大 `page_size=100`
- 使用游标分页
- 中文和英文共用统一查询解析流程
- 商品状态过滤在数据库查询中执行
- 索引更新通过 Outbox 异步处理
- 采用版本化索引支持无停机重建
