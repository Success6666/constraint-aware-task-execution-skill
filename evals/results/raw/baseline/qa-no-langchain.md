## Architecture

Build the service as several small, independently testable components:

```text
HTTP API
  |
  +-- Ingestion service --> Object storage
  |                      --> Document parser
  |                      --> Chunker
  |                      --> Embedding worker --> Vector database
  |
  +-- Query service ------> Query embedding
                         --> Metadata/filter retrieval
                         --> Hybrid search
                         --> Reranking
                         --> Context assembly
                         --> LLM answer generation
                         --> Citation validation
```

A practical implementation could use:

- FastAPI for the HTTP API
- PostgreSQL with `pgvector` for metadata and vectors
- S3-compatible storage for original documents
- Redis or RabbitMQ for background jobs
- Provider-neutral embedding and generation interfaces
- Native SDKs and a small amount of application code; no LangChain

## Core data model

```sql
documents (
  id uuid primary key,
  tenant_id uuid not null,
  filename text not null,
  content_type text,
  object_key text not null,
  sha256 text not null,
  version integer not null,
  status text not null, -- queued, processing, ready, failed
  created_at timestamptz not null
);

document_pages (
  id uuid primary key,
  document_id uuid references documents(id),
  page_number integer,
  text text,
  char_start integer,
  char_end integer
);

chunks (
  id uuid primary key,
  document_id uuid references documents(id),
  page_id uuid references document_pages(id),
  chunk_index integer,
  text text not null,
  token_count integer,
  embedding vector(1536),
  metadata jsonb,
  unique(document_id, chunk_index)
);

answer_citations (
  id uuid primary key,
  answer_id uuid,
  chunk_id uuid references chunks(id),
  quote text,
  page_number integer,
  score float
);
```

Add indexes for:

- Vector similarity on `chunks.embedding`
- Full-text search on `chunks.text`
- `(tenant_id, document_id)` filtering
- Document status and ingestion job lookup

## Ingestion pipeline

1. Accept the upload and calculate a content hash.
2. Store the original file in object storage.
3. Create a `documents` row with status `queued`.
4. Enqueue an ingestion job.
5. Extract text while preserving page, section, paragraph, and table boundaries.
6. Normalize whitespace without destroying source offsets.
7. Split content into chunks.
8. Generate embeddings in batches.
9. Write chunks and embeddings transactionally.
10. Mark the document `ready`; retry transient failures and record permanent errors.

Parsing should be format-specific:

- PDF: page-aware text extraction, with OCR fallback
- DOCX: paragraphs, headings, and tables
- HTML: remove navigation and scripts, preserve headings and links
- Markdown: preserve heading hierarchy and code blocks
- Plain text: retain line boundaries

## Chunking

Use structure-aware, token-based chunking rather than fixed character slices.

Recommended defaults:

- Target: 400–700 tokens
- Hard maximum: 900 tokens
- Overlap: 50–100 tokens
- Never split inside a table row, code block, or short paragraph when avoidable
- Include heading ancestry in each chunk

Store enough metadata to reconstruct provenance:

```json
{
  "section_path": ["Installation", "Configuration"],
  "page_number": 12,
  "source_start": 48120,
  "source_end": 50311
}
```

For long documents, create parent sections and child chunks so retrieval can return a focused chunk while answer generation can optionally expand to its surrounding section.

## Retrieval

For each query:

1. Authenticate the caller and derive tenant/document permissions.
2. Normalize the query.
3. Generate a query embedding.
4. Run vector search with a generous candidate count, for example `top_k = 30`.
5. Run PostgreSQL full-text or BM25-style keyword search.
6. Fuse the result lists using reciprocal rank fusion.
7. Apply metadata and access-control filters before returning context.
8. Rerank the top 20–40 candidates with a cross-encoder or provider reranker.
9. Remove near-duplicate chunks.
10. Select a context budget based on the model's token limit.

Example vector query:

```sql
select
  c.id,
  c.text,
  c.metadata,
  1 - (c.embedding <=> :query_embedding) as score
from chunks c
join documents d on d.id = c.document_id
where d.tenant_id = :tenant_id
  and d.status = 'ready'
  and (:document_ids is null or d.id = any(:document_ids))
order by c.embedding <=> :query_embedding
limit 30;
```

Keep retrieval and generation separate so retrieval quality can be evaluated independently.

## Answer generation and citations

Construct the prompt with explicitly labeled sources:

```text
Answer the question using only the supplied sources.
If the sources do not establish the answer, say so.
Cite every material claim using [S1], [S2], etc.
Do not invent citations.

[S1]
Document: employee-handbook.pdf
Page: 12
Chunk ID: ...

Content:
...
```

The model should return structured JSON:

```json
{
  "answer": " ... ",
  "citations": [
    {
      "source_id": "S1",
      "quote": " ... ",
      "chunk_id": "..."
    }
  ],
  "confidence": "high"
}
```

Validate the response server-side:

- Every citation ID must exist in the supplied context.
- Every cited chunk must belong to the authorized tenant.
- The quote must be a substring or normalized substring of the chunk.
- Reject or repair malformed JSON.
- Optionally run an entailment check between each claim and its cited chunk.
- Return “insufficient evidence” when retrieval scores are below a threshold.

Expose citations with document name, page, section, chunk ID, and an optional source URL. Store the exact retrieved context and model version for auditability.

## HTTP API

### Upload

```http
POST /v1/documents
Content-Type: multipart/form-data
Authorization: Bearer ...
```

Response:

```json
{
  "document_id": "uuid",
  "status": "queued"
}
```

### Ingestion status

```http
GET /v1/documents/{document_id}
```

```json
{
  "document_id": "uuid",
  "status": "ready",
  "pages": 18,
  "chunks": 74,
  "version": 1
}
```

### Ask a question

```http
POST /v1/answers
Content-Type: application/json
```

```json
{
  "question": "What is the retention period?",
  "document_ids": ["uuid"],
  "top_k": 8,
  "include_debug": false
}
```

Response:

```json
{
  "answer_id": "uuid",
  "answer": "Records are retained for seven years.",
  "citations": [
    {
      "document_id": "uuid",
      "filename": "policy.pdf",
      "page": 12,
      "quote": "Records must be retained for seven years."
    }
  ],
  "retrieval": {
    "results_used": 4,
    "confidence": "high"
  }
}
```

Additional endpoints:

- `DELETE /v1/documents/{id}`
- `GET /v1/documents`
- `POST /v1/search`
- `GET /v1/answers/{answer_id}`
- `GET /healthz`
- `GET /readyz`

Use idempotency keys for uploads and answer requests. Return `202 Accepted` for asynchronous ingestion.

## Internal interfaces

Keep provider integrations behind small interfaces:

```python
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

class Generator(Protocol):
    async def answer(self, prompt: str) -> str: ...

class Reranker(Protocol):
    async def score(self, query: str, passages: list[str]) -> list[float]: ...
```

The application owns orchestration, retries, validation, and persistence. Providers only implement model calls.

## Reliability and security

- Enforce tenant isolation in every query.
- Treat uploaded documents as untrusted input and strip prompt-like instructions from system behavior.
- Apply file-size, page-count, token, and request-rate limits.
- Scan uploads for malware.
- Encrypt object storage and database connections.
- Redact sensitive values from logs.
- Add job retries with exponential backoff and a dead-letter queue.
- Version parsers, chunking settings, embedding models, and prompts.
- Re-ingest when the embedding model or chunking version changes.
- Stream answer tokens only after retrieval and citation validation, or stream a provisional answer followed by validated citations.

## Evaluation

Maintain a benchmark containing:

- Questions with known supporting chunks
- Unanswerable questions
- Multi-document questions
- Permission-filtering cases
- Table and OCR cases

Measure retrieval recall, reranker precision, citation precision, citation completeness, unsupported-claim rate, latency, and cost. Test ingestion and retrieval independently before tuning the generation prompt.