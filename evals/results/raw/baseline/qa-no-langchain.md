当前仓库是空的 Git 仓库，且工作区为只读，无法写入 `tmp-docs` 或提交文件。下面给出可直接落地的服务设计。

# 文档问答服务设计

## 1. 总体架构

```text
                    ┌──────────────────┐
                    │      HTTP API    │
                    │ Upload / Status  │
                    │ Ask / Documents  │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
       ┌──────▼──────┐               ┌──────▼──────┐
       │ Ingestion   │               │ QA Service  │
       │ Worker      │               │ Retrieval   │
       └──────┬──────┘               │ Prompting   │
              │                      │ Citations   │
              │                      └──────┬──────┘
              │                             │
 ┌────────────▼────────────┐       ┌────────▼────────┐
 │ Parser / Normalizer      │       │ Embedding Port  │
 │ Chunker                  │       │ LLM Port        │
 └────────────┬────────────┘       └────────┬────────┘
              │                             │
      ┌───────▼────────┐           ┌────────▼────────┐
      │ Object Storage │           │ Vector Database │
      │ Original Files │           │ pgvector/Qdrant │
      └────────────────┘           └─────────────────┘
```

推荐实现：

- HTTP：FastAPI 或 Go Gin。
- 任务队列：Redis Streams、RabbitMQ 或 Kafka。
- 元数据：PostgreSQL。
- 向量检索：PostgreSQL + pgvector；数据量较大时替换为 Qdrant。
- 原始文件：S3/MinIO。
- 文档解析：PDF 使用 PyMuPDF，DOCX 使用 python-docx，HTML 使用标准解析器。
- Embedding 和 LLM：通过内部接口封装 OpenAI、Azure、Ollama 或其他供应商。
- 不使用 LangChain，所有流程由自有模块编排。

## 2. 代码目录

```text
app/
  api/
    routes_documents.py
    routes_qa.py
    dependencies.py
  domain/
    models.py
    ports.py
    errors.py
  ingestion/
    parser.py
    normalizer.py
    chunker.py
    pipeline.py
  retrieval/
    vector_store.py
    keyword_store.py
    hybrid_search.py
    reranker.py
  generation/
    llm_client.py
    prompt_builder.py
    answer_service.py
    citation_validator.py
  storage/
    object_store.py
    repositories.py
  workers/
    ingestion_worker.py
  security/
    auth.py
    policy.py
  config.py
tests/
```

`domain/ports.py` 只定义接口，避免业务代码依赖具体模型或数据库：

```python
class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

class LLMProvider(Protocol):
    async def complete(self, messages: list[dict], **kwargs) -> str: ...

class VectorStore(Protocol):
    async def upsert(self, records: list[VectorRecord]) -> None: ...
    async def search(self, tenant_id: str, vector: list[float], top_k: int): ...
```

## 3. 文档摄取流程

1. 校验租户权限、文件大小、扩展名和 MIME 类型。
2. 计算 SHA-256，检查同一租户是否已经存在相同内容。
3. 文件保存到对象存储，数据库创建 `document` 和 `document_version`。
4. 发布异步任务 `ingest_document(version_id)`。
5. 解析文本并保留结构信息：
   - 页码
   - 段落
   - 标题层级
   - 表格
   - 原始字符区间
6. 标准化文本：
   - 统一换行和空白
   - 保留标题
   - 删除重复页眉页脚
   - 保留页码和段落映射
7. 分块。
8. 批量生成 Embedding。
9. 写入向量库和关键词索引。
10. 更新状态为 `ready`，失败则记录错误并支持重试。

状态：

```text
uploaded -> processing -> ready
                    └-> failed
```

任务必须使用 `version_id` 而不是文件名，保证文档更新后旧版本仍可追溯。

## 4. 分块策略

默认采用结构感知的递归分块，而不是简单按固定字符截断。

建议参数：

```text
目标大小：400~800 tokens
重叠：80~120 tokens
硬上限：1200 tokens
```

分块优先级：

1. 按标题层级分段。
2. 在段落边界切分。
3. 长段落按句子切分。
4. 最后才按 token 硬切。
5. 表格作为独立块，保存表头。
6. 代码块、列表和引用块尽量保持完整。

每个 chunk 保存：

```json
{
  "chunk_id": "chk_01J...",
  "document_id": "doc_01J...",
  "version_id": "ver_01J...",
  "text": "原文内容",
  "token_count": 612,
  "page_start": 3,
  "page_end": 4,
  "char_start": 1820,
  "char_end": 4512,
  "section_path": ["第三章", "系统架构"],
  "content_hash": "sha256..."
}
```

## 5. Embedding 与索引

Embedding 服务支持批量调用、超时、指数退避和限流。

向量表关键字段：

```text
chunk_id
tenant_id
version_id
embedding vector(N)
text
metadata jsonb
```

索引策略：

- pgvector：HNSW 或 IVFFlat。
- `tenant_id` 必须作为过滤条件，不能只在应用层过滤。
- Embedding 模型名和维度写入版本表，模型变化时创建新索引。
- 关键词索引使用 PostgreSQL `tsvector` 或独立搜索引擎。

## 6. 检索流程

采用混合检索：

```text
问题
 ├─ 查询规范化
 ├─ 生成问题向量
 ├─ 向量召回 top_k=30
 ├─ BM25/全文召回 top_k=30
 ├─ Reciprocal Rank Fusion 合并
 ├─ 可选 reranker 排序
 └─ 选择最终上下文 top_n=6~10
```

过滤条件：

- `tenant_id`
- 指定 `document_ids`
- 指定版本
- 用户可见性
- 文档状态必须为 `ready`

返回结果按以下字段排序：

```text
最终相关性 = 语义相关性 + 关键词相关性 + reranker 分数
```

对于低置信度问题，应返回“未找到足够依据”，而不是让模型补全事实。

## 7. 回答与引用

Prompt 只允许模型使用检索上下文：

```text
你只能依据 SOURCES 中的内容回答。
如果来源不足，请明确说明无法确定。
每个事实性结论后附 [S1]、[S2] 等引用标记。
不要伪造来源或引用编号。
```

上下文格式：

```text
[S1]
document_id: doc_x
version_id: ver_y
chunk_id: chk_z
page: 3-4
text: ...

[S2]
...
```

模型输出后执行引用校验：

1. 检查引用编号是否存在。
2. 检查引用是否属于实际注入的 chunk。
3. 删除不存在的引用。
4. 可选：对每个句子做 entailment 检查。
5. 没有有效引用的事实句标记为低可信度或触发重新生成。

回答响应：

```json
{
  "answer_id": "ans_01J...",
  "answer": "系统采用异步任务处理文档。[S1]",
  "citations": [
    {
      "id": "S1",
      "document_id": "doc_01J...",
      "version_id": "ver_01J...",
      "chunk_id": "chk_01J...",
      "file_name": "architecture.pdf",
      "page_start": 3,
      "page_end": 4,
      "char_start": 1820,
      "char_end": 4512,
      "quote": "系统采用异步任务处理文档。"
    }
  ],
  "retrieval": {
    "top_k": 8,
    "confidence": 0.87
  }
}
```

## 8. HTTP API

### 上传文档

```http
POST /v1/documents
Authorization: Bearer <token>
Content-Type: multipart/form-data
Idempotency-Key: <unique-key>
```

响应：

```json
{
  "document_id": "doc_01J...",
  "version_id": "ver_01J...",
  "status": "processing"
}
```

状态码：

- `202 Accepted`：已接收，异步处理。
- `400 Bad Request`：文件格式或参数错误。
- `413 Payload Too Large`：超过大小限制。
- `409 Conflict`：幂等键重复但请求内容不同。
- `415 Unsupported Media Type`：不支持的文件类型。

### 查询摄取状态

```http
GET /v1/documents/{document_id}/versions/{version_id}
```

响应：

```json
{
  "document_id": "doc_01J...",
  "version_id": "ver_01J...",
  "status": "ready",
  "chunk_count": 42,
  "indexed_at": "2026-08-16T10:00:00Z",
  "error": null
}
```

### 文档问答

```http
POST /v1/qa
Content-Type: application/json
```

请求：

```json
{
  "question": "系统如何处理文档？",
  "document_ids": ["doc_01J..."],
  "top_k": 8,
  "include_quotes": true
}
```

响应为前述回答与引用结构。

建议增加：

```http
GET /v1/documents
DELETE /v1/documents/{document_id}
GET /v1/answers/{answer_id}
```

## 9. 数据库核心表

```text
documents(
  id, tenant_id, name, created_by, created_at, deleted_at
)

document_versions(
  id, document_id, content_hash, object_key,
  parser_version, embedding_model, status,
  created_at, completed_at, error_message
)

chunks(
  id, tenant_id, document_id, version_id,
  text, page_start, page_end, char_start, char_end,
  section_path, content_hash, metadata
)

chunk_embeddings(
  chunk_id, model, dimensions, embedding
)

qa_sessions(
  id, tenant_id, user_id, question, answer,
  model, latency_ms, created_at
)

audit_logs(
  id, tenant_id, actor_id, action, resource_id,
  request_id, metadata, created_at
)
```

所有业务查询都必须带 `tenant_id`。数据库层可额外启用 Row-Level Security。

## 10. 安全设计

- 使用 OIDC/JWT，校验 issuer、audience、过期时间和签名。
- 文件大小、扩展名、真实 MIME、压缩炸弹和恶意宏检查。
- 文件解析在隔离 worker 中运行，限制 CPU、内存、运行时间和网络访问。
- 不允许通过 URL 直接抓取文件，避免 SSRF；如必须支持 URL，使用域名白名单和私网地址拦截。
- 文件名和对象存储 key 不直接拼接用户输入。
- 对问题和文档内容做提示注入隔离，文档内容只能作为数据，不得覆盖系统指令。
- API 限流、并发限制、请求体限制和超时控制。
- 日志中不记录完整文档、完整问题或访问令牌。
- 删除文档时同步删除对象、向量、关键词索引和缓存。
- 引用展示必须再次执行权限检查，防止通过 chunk ID 越权读取。

## 11. 可观测性

指标：

```text
ingestion_success_total
ingestion_failure_total
ingestion_duration_seconds
embedding_latency_seconds
retrieval_latency_seconds
llm_latency_seconds
qa_error_total
citation_validation_failure_total
```

每个请求使用 `request_id`，异步任务使用 `trace_id`。记录：

- 文档解析耗时和块数
- Embedding 批次数量
- 召回数量和最终上下文数量
- LLM token 使用量
- 引用有效率
- 用户反馈或人工评分

## 12. 测试与验收

- 单元测试：解析器、分块边界、token 上限、引用校验、租户过滤。
- 集成测试：上传文档到 `ready` 的完整链路。
- 检索测试：已知问题的 Recall@K、MRR、nDCG。
- 生成测试：答案不得出现无来源事实，引用必须可定位。
- 安全测试：越权访问、恶意文件、SSRF、提示注入、超大请求、重复幂等键。
- 压测：并发上传、批量 Embedding、问答 P95 延迟。
- 回归数据集：固定一组文档、问题和期望引用，模型或分块策略变更时自动运行。

建议的上线门槛：

```text
引用有效率 >= 99%
租户越权测试 = 0
摄取成功率 >= 99%
问答接口 P95 延迟 <= 5 秒
关键回归集 Recall@5 不低于基线
```

该设计完全由自有模块编排，模型、Embedding 服务和向量数据库均通过接口隔离，可在不改业务流程的情况下替换具体供应商。