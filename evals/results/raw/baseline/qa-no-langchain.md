# Document Question-Answering Service

## 1. Architecture

Use a modular service with explicit pipeline stages:

```text
HTTP API
  |
  +-- PostgreSQL + pgvector
  +-- Object storage for original documents
  +-- Queue for asynchronous ingestion
  +-- Ingestion workers
        1. Parse / OCR
        2. Normalize
        3. Chunk
        4. Embed
        5. Index
  |
  +-- Query service
        1. Query normalization
        2. Hybrid retrieval
        3. Reranking
        4. Context assembly
        5. Answer generation
        6. Citation validation
```

Recommended implementation:

- Python with FastAPI for HTTP.
- PostgreSQL with `pgvector` for metadata, chunks, and vector search.
- S3-compatible object storage for source documents.
- Redis, RabbitMQ, or SQS for ingestion jobs.
- A provider abstraction for embedding, reranking, and generation models.
- No framework-specific orchestration dependency; pipeline stages are ordinary application services.

The system should be multi-tenant. Every document, chunk, query, and authorization check must include `tenant_id`.

## 2. Core Data Model

### Documents

```text
documents
- id
- tenant_id
- external_id
- filename
- media_type
- object_uri
- content_hash
- size_bytes
- language
- status: pending | processing | ready | failed | deleted
- parser_version
- chunker_version
- embedding_model
- created_at
- updated_at
- error_code
- error_message
```

Add a unique constraint on:

```text
(tenant_id, content_hash, parser_version, chunker_version, embedding_model)
```

This makes ingestion idempotent.

### Pages or source units

```text
document_pages
- id
- document_id
- page_number
- text
- character_start
- character_end
- source_metadata JSONB
```

`source_metadata` can contain PDF coordinates, HTML element paths, table identifiers, or OCR confidence.

### Chunks

```text
chunks
- id
- document_id
- tenant_id
- ordinal
- text
- token_count
- page_start
- page_end
- character_start
- character_end
- heading_path JSONB
- content_type: paragraph | heading | table | list | code | caption
- metadata JSONB
- embedding vector
- search_text tsvector
- created_at
```

Create:

- An HNSW or IVFFlat vector index.
- A PostgreSQL full-text index over `search_text`.
- B-tree indexes on `tenant_id`, `document_id`, and status-related fields.

### Ingestion jobs

```text
ingestion_jobs
- id
- tenant_id
- document_id
- stage
- status
- attempt_count
- started_at
- completed_at
- error_message
```

Workers must be retryable and safe to run more than once.

## 3. Ingestion

### Upload flow

1. Client requests an upload or directly uploads to object storage.
2. API records document metadata and returns an ingestion job ID.
3. Worker downloads the object, verifies the content hash, and processes it asynchronously.
4. API exposes status polling and optionally webhook notification.

Supported initial formats:

- PDF
- DOCX
- HTML
- Markdown
- TXT
- CSV
- Images through OCR

Reject unsupported media types explicitly. Enforce maximum file size, page count, decompressed size, and processing time.

### Parsing

The parser should preserve:

- Page numbers.
- Headings and section hierarchy.
- Lists.
- Tables.
- Code blocks.
- Source character offsets.
- PDF bounding boxes where available.
- OCR confidence where applicable.

Normalize text by:

- Converting line endings.
- Removing repeated headers and footers when confidently detected.
- Repairing hyphenated line breaks.
- Collapsing excessive whitespace.
- Preserving paragraph and section boundaries.
- Keeping table content in a stable row-oriented representation.

Never discard the original object or raw extracted text. It is required for debugging and citation verification.

### Tables

Represent tables in a deterministic format, for example:

```text
Table: Quarterly revenue

Quarter | Revenue | Growth
Q1      | 120      | 4%
Q2      | 140      | 17%
```

For large tables, create:

- A table-level summary chunk.
- Row-group chunks containing column headers.
- Optional cell-level metadata for precise citations.

## 4. Chunking

Use structure-aware chunking rather than splitting every document at arbitrary character boundaries.

Recommended defaults:

- Target: 400-700 tokens.
- Hard maximum: 900 tokens.
- Overlap: 50-100 tokens.
- Never split inside a sentence if avoidable.
- Keep headings attached to their following content.
- Keep list items together where possible.
- Keep table headers in every table chunk.
- Preserve page and character offsets.

Chunking algorithm:

1. Build a document tree from headings and block elements.
2. Group adjacent blocks under the same heading.
3. Split oversized groups on paragraph boundaries.
4. Split oversized paragraphs on sentence boundaries.
5. Add controlled overlap from the preceding chunk.
6. Store the heading path and exact source offsets.

Do not use overlap as a substitute for good boundaries. It should improve recall without producing large duplicated contexts.

Every chunk must be independently understandable enough for retrieval, so prepend its heading path during embedding when useful:

```text
Section: Security > Authentication

<chunk text>
```

Store the original chunk text separately from any embedding-only prefix.

## 5. Embeddings

Define an embedding provider interface:

```python
class EmbeddingProvider:
    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_query(self, text: str) -> list[float]:
        ...
```

Requirements:

- Use the same embedding family for document and query vectors unless the model explicitly supports separate representations.
- Normalize vectors if using cosine similarity.
- Store model name and dimensions with each indexed document version.
- Batch embedding requests.
- Retry transient provider failures with exponential backoff.
- Apply rate limiting and maximum batch size.
- Re-index documents when the model changes.

Do not mix embeddings from incompatible models in the same vector index.

## 6. Retrieval

Use tenant-filtered hybrid retrieval.

### Candidate generation

For each query:

1. Generate a query embedding.
2. Run vector similarity search for the top 50-100 chunks.
3. Run PostgreSQL full-text or BM25-style lexical search for the top 50-100 chunks.
4. Merge results with Reciprocal Rank Fusion.

Example RRF score:

```text
rrf_score(chunk) = Σ 1 / (60 + rank_in_retriever)
```

Apply filters before ranking:

- Tenant.
- Collection or namespace.
- Document IDs.
- Document metadata.
- Language.
- Access permissions.

### Reranking

Rerank the merged top 30-50 candidates with a cross-encoder or provider reranker. Retain approximately 5-12 chunks depending on context size.

Use diversity controls:

- Limit chunks per document unless the query clearly requires one document.
- Prefer adjacent chunks when they form a coherent passage.
- Avoid returning duplicate overlapping chunks.
- Preserve document and page coverage.

Return retrieval diagnostics internally:

```text
chunk_id
vector_score
lexical_score
fusion_score
rerank_score
```

These are useful for evaluation and debugging but need not be exposed publicly.

## 7. Answer Generation

The generation service receives:

```text
- User question
- Retrieved passages
- Citation identifiers
- Conversation history, if enabled
- Tenant-specific answer policy
```

Use a strict grounded-answer instruction:

```text
Answer only from the supplied sources.
Every factual claim must be supported by one or more citations.
If the sources do not contain the answer, say that the information is unavailable.
Do not infer precise values, dates, or policies that are not stated.
Distinguish conflicting sources explicitly.
```

Require structured model output:

```json
{
  "answer": "string",
  "citations": [
    {
      "citation_id": "C1",
      "claim": "string",
      "chunk_id": "string",
      "quote": "string"
    }
  ],
  "insufficient_evidence": false
}
```

Validate the response before returning it:

- Every citation references a retrieved chunk.
- Every citation quote is an exact substring of the source chunk.
- Citation IDs are unique.
- Claims marked factual have citations.
- The response does not contain unsupported source-like claims.
- The answer obeys tenant and safety policies.

If validation fails, retry once with a correction prompt or return a controlled error rather than silently returning ungrounded text.

For high-assurance use cases, use extractive answers or quote-first generation for dates, amounts, identifiers, and policy clauses.

## 8. Citations

Citations should resolve to human-readable source locations:

```json
{
  "citation_id": "C1",
  "document_id": "doc_123",
  "filename": "handbook.pdf",
  "page_start": 12,
  "page_end": 12,
  "heading_path": ["Benefits", "Leave"],
  "quote": "Employees receive..."
}
```

The API should provide a citation URL or endpoint that opens:

- The source document.
- The relevant page.
- The highlighted quote when page coordinates exist.

Citation generation must use stored offsets, not regenerated text searches, because normalization may change whitespace.

## 9. HTTP API

### Upload

```http
POST /v1/documents
Authorization: Bearer <token>
Content-Type: multipart/form-data
Idempotency-Key: <key>
```

Response:

```json
{
  "document_id": "doc_123",
  "ingestion_job_id": "job_456",
  "status": "pending"
}
```

### Ingestion status

```http
GET /v1/ingestion/{job_id}
```

Response:

```json
{
  "job_id": "job_456",
  "document_id": "doc_123",
  "status": "processing",
  "stage": "embedding",
  "progress": 0.72,
  "error": null
}
```

### Ask a question

```http
POST /v1/answers
Content-Type: application/json
```

Request:

```json
{
  "query": "What is the annual leave policy?",
  "collection_ids": ["collection_a"],
  "document_ids": [],
  "conversation_id": null,
  "top_k": 8,
  "include_debug": false
}
```

Response:

```json
{
  "answer_id": "ans_789",
  "answer": "Employees receive ... [C1].",
  "citations": [
    {
      "citation_id": "C1",
      "document_id": "doc_123",
      "filename": "handbook.pdf",
      "page_start": 12,
      "page_end": 12,
      "heading_path": ["Benefits", "Leave"],
      "quote": "Employees receive..."
    }
  ],
  "insufficient_evidence": false
}
```

### Search-only endpoint

```http
POST /v1/search
```

Return ranked chunks without generation. This supports debugging, UI previews, and retrieval evaluation.

### Document management

```http
GET    /v1/documents
GET    /v1/documents/{document_id}
DELETE /v1/documents/{document_id}
POST   /v1/documents/{document_id}/reindex
```

Deletion must remove the document from retrieval immediately, then asynchronously delete vectors, chunks, and object storage according to retention policy.

## 10. Authorization and Security

- Authenticate every request.
- Scope access by tenant and user permissions.
- Apply authorization filters during retrieval, not after retrieval.
- Encrypt objects and database connections.
- Virus-scan uploads.
- Treat document text as untrusted input.
- Defend against prompt injection by clearly delimiting retrieved content and treating it as data.
- Never allow retrieved text to override system or tenant instructions.
- Redact secrets and sensitive logs.
- Log document IDs and request IDs, but avoid logging full document text by default.
- Add request quotas for upload size, query rate, token usage, and concurrent jobs.
- Enforce retention and deletion policies.

## 11. Reliability and Operations

Use:

- Idempotent stage transitions.
- Dead-letter handling for permanently failed jobs.
- Per-provider timeouts.
- Circuit breakers for external model services.
- Job heartbeats and stale-job recovery.
- Request cancellation and generation timeouts.
- Streaming responses only after the retrieval set has been finalized.
- Versioned parser, chunker, and embedding configurations.

Expose metrics for:

- Ingestion latency by stage.
- Failure and retry counts.
- Queue depth.
- Parsing quality and OCR failures.
- Embedding latency and token usage.
- Retrieval latency.
- Recall and reranker scores.
- Generation latency.
- Citation validation failures.
- Insufficient-evidence rate.
- Feedback and answer quality.

## 12. Verification

### Unit tests

Test:

- Parser output for each supported format.
- Header/footer removal.
- Sentence and table boundaries.
- Chunk overlap and maximum sizes.
- Offset preservation.
- Idempotent ingestion.
- Tenant filtering.
- Citation quote validation.
- Malformed model output handling.
- Retry and timeout behavior.

### Integration tests

Verify:

1. Upload a document.
2. Poll until ingestion is ready.
3. Query using exact and paraphrased wording.
4. Confirm relevant chunks are retrieved.
5. Confirm answers contain valid citations.
6. Delete the document.
7. Confirm it can no longer be retrieved.
8. Confirm one tenant cannot access another tenant’s documents.

### Evaluation set

Maintain a versioned dataset containing:

- Questions.
- Expected source documents.
- Expected pages or chunks.
- Expected answer facts.
- Unanswerable questions.
- Adversarial and prompt-injection documents.
- Cross-document and conflicting-policy questions.

Measure:

- Retrieval recall@k.
- MRR or nDCG.
- Citation precision.
- Citation completeness.
- Answer faithfulness.
- Answer correctness.
- Abstention accuracy.
- P50/P95 latency.
- Cost per query.

Release a new parser, chunker, embedding model, reranker, or prompt only after comparing it against the evaluation set and checking for regressions.

## 13. Recommended Initial Defaults

- Chunk target: 600 tokens.
- Chunk maximum: 900 tokens.
- Overlap: 80 tokens.
- Vector candidates: 75.
- Lexical candidates: 75.
- RRF fusion constant: 60.
- Reranker input: top 40.
- Final context: 8 chunks, limited by token budget.
- Answer temperature: 0 or near zero.
- Maximum answer length: configured per tenant.
- Abstain when retrieval confidence is low or citations cannot support the answer.

The primary correctness contract is: **the service either returns an answer whose factual claims are traceable to exact source passages, or explicitly states that the available documents do not provide sufficient evidence.**
