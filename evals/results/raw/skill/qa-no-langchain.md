当前工作区为只读挂载，无法写入任务书或源码；因此本次交付为完整的可实现设计。

## 1. 总体架构

```text
客户端
  |
HTTP API
  |-- POST /documents        文档摄取
  |-- GET  /documents        文档列表
  |-- POST /query            问答检索
  |-- GET  /health           健康检查
  |
应用服务
  |-- 文档解析器
  |-- 文本分块器
  |-- EmbeddingProvider
  |-- Retriever
  |-- AnswerComposer
  |-- CitationBuilder
  |
持久化层
  |-- documents
  |-- document_chunks
  |-- embeddings
```

推荐实现：

- HTTP：FastAPI 或 Python 标准库 HTTP 服务
- 持久化：PostgreSQL + pgvector；小规模部署可使用 SQLite
- 文档解析：纯文本、Markdown、PDF、DOCX 分别使用对应解析器
- Embedding：OpenAI Embeddings、Sentence Transformers 或其他独立向量模型
- 检索：向量相似度检索，可叠加 BM25 关键词检索
- 生成：通过独立 LLM 客户端封装，不引入 LangChain

## 2. 核心数据模型

### documents

```sql
CREATE TABLE documents (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    mime_type VARCHAR(100) NOT NULL,
    checksum CHAR(64) NOT NULL UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
```

### document_chunks

```sql
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page_number INTEGER,
    heading_path TEXT[],
    embedding VECTOR(1536),
    metadata JSONB NOT NULL DEFAULT '{}',
    UNIQUE(document_id, chunk_index)
);
```

索引：

```sql
CREATE INDEX idx_chunks_document
ON document_chunks(document_id);

CREATE INDEX idx_chunks_embedding
ON document_chunks
USING hnsw (embedding vector_cosine_ops);
```

## 3. 文档摄取流程

```text
上传文档
  -> MIME 类型校验
  -> 文件大小限制
  -> 内容解析
  -> 清理不可见字符
  -> 按标题/段落切分
  -> 滑动窗口分块
  -> 生成 embedding
  -> 写入文档和分块
  -> 返回 document_id
```

分块建议：

- 默认块大小：600 至 1,000 tokens
- 重叠：80 至 150 tokens
- 优先按照标题、段落、列表边界切分
- 每块保留页码、章节路径和字符偏移
- 对超长表格或代码块使用专门切分策略

## 4. 检索流程

```text
用户问题
  -> 问题 embedding
  -> 向量召回 Top-K
  -> 可选 BM25 召回
  -> 融合排序
  -> 最低相关性阈值过滤
  -> 生成答案上下文
  -> 构建引用
  -> 返回答案和 citations
```

建议使用混合检索：

```text
final_score = 0.7 * vector_score + 0.3 * keyword_score
```

对于没有达到阈值的结果，应返回“未找到足够相关内容”，避免生成无依据答案。

## 5. HTTP API

### 健康检查

```http
GET /health
```

响应：

```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

### 摄取文档

```http
POST /documents
Content-Type: multipart/form-data
```

字段：

```text
file: 文档文件
metadata: 可选 JSON
```

响应：

```json
{
  "document_id": "8d8b...",
  "name": "architecture.md",
  "chunk_count": 14,
  "status": "indexed"
}
```

### 查询文档

```http
POST /query
Content-Type: application/json
```

请求：

```json
{
  "question": "系统如何生成引用？",
  "top_k": 5,
  "document_ids": []
}
```

响应：

```json
{
  "answer": "系统在检索到相关分块后，根据分块的文档 ID、块 ID 和原文位置生成引用。",
  "citations": [
    {
      "document_id": "8d8b...",
      "chunk_id": "2a31...",
      "document_name": "architecture.md",
      "chunk_index": 3,
      "start_offset": 820,
      "end_offset": 1260,
      "page_number": 4,
      "quote": "根据分块的文档 ID、块 ID 和原文位置生成引用。",
      "score": 0.91
    }
  ]
}
```

### 文档列表

```http
GET /documents?limit=20&offset=0
```

响应：

```json
{
  "items": [
    {
      "document_id": "8d8b...",
      "name": "architecture.md",
      "chunk_count": 14,
      "created_at": "2026-08-16T10:00:00Z"
    }
  ],
  "total": 1
}
```

## 6. 推荐代码结构

```text
document_qa/
├── api/
│   ├── routes_documents.py
│   ├── routes_query.py
│   └── schemas.py
├── ingestion/
│   ├── parsers.py
│   ├── chunker.py
│   └── pipeline.py
├── retrieval/
│   ├── embeddings.py
│   ├── vector_store.py
│   ├── hybrid_search.py
│   └── citations.py
├── generation/
│   └── answer_composer.py
├── storage/
│   ├── models.py
│   └── database.py
├── config.py
└── main.py
```

各模块之间通过明确接口通信，Embedding、向量数据库和生成模型都可以替换，不需要绑定到某个编排框架。

## 7. 安全与可靠性

- 限制上传文件大小和允许的 MIME 类型
- 文件名只作为元数据保存，不直接拼接文件路径
- 使用参数化 SQL，避免 SQL 注入
- 对返回内容进行 JSON 编码，避免 XSS
- API 增加认证和租户隔离
- 文档和查询日志禁止记录敏感原文
- 设置查询超时、最大 `top_k` 和最大上下文长度
- 对重复文件使用 SHA-256 去重
- 所有引用必须来自实际召回的文档块
- 对生成答案保留检索分数和模型版本，便于审计

## 8. 验收标准

1. 文档可上传并完成解析、分块和 embedding。
2. 服务重启后文档和向量仍然存在。
3. 查询返回答案、相关性分数和可定位引用。
4. 低相关性问题不会生成无依据内容。
5. API 支持健康检查、摄取、列表和查询。
6. 测试覆盖分块、embedding、检索排序、引用格式和 API 错误处理。
7. 依赖中不包含 LangChain。