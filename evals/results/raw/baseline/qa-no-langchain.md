Build this as a small, explicit pipeline with independently testable stages. Avoid framework-managed “chains”; each stage is a normal function or service interface.

```text
Client
  -> HTTP API (FastAPI)
  -> Ingestion service
       -> object storage (original files)
       -> parser / OCR
       -> chunker
       -> embedding provider
       -> PostgreSQL + pgvector
  -> Query service
       -> embed query
       -> vector + metadata retrieval
       -> optional reranker
       -> LLM answer generation
       -> cited response
```

Core records:

```sql
documents (
  id uuid primary key,
  tenant_id uuid not null,
  filename text,
  mime_type text,
  storage_uri text,
  content_hash text,
  status text,
  created_at timestamptz
);

chunks (
  id uuid primary key,
  document_id uuid references documents(id),
  tenant_id uuid not null,
  ordinal int not null,
  text text not null,
  token_count int,
  page_start int,
  page_end int,
  section_title text,
  metadata jsonb,
  embedding vector(1536)
);

create index chunks_embedding_idx
  on chunks using hnsw (embedding vector_cosine_ops);
```

Ingestion flow:

1. Accept a file or source URL and save the original to object storage.
2. Extract normalized text, preserving page numbers, headings, offsets, and source URLs.
3. Split by document structure first, then enforce a token limit, such as 500 tokens with 75-token overlap.
4. Generate embeddings in batches.
5. Store chunks and embeddings transactionally; mark the document `ready` only after completion.

Use provider-neutral interfaces:

```python
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class Generator(Protocol):
    def generate(self, prompt: str) -> str: ...

class DocumentParser(Protocol):
    def parse(self, source: bytes, mime_type: str) -> list[ParsedSection]: ...
```

Retrieval should always scope by `tenant_id`, optionally filter document IDs or metadata, retrieve 20-50 chunks, optionally rerank to 5-8, then provide only those chunks to the generator. Give every prompt excerpt a stable source label such as `C1`, `C2`.

The answer prompt should require grounded output:

```text
Answer only from the provided excerpts.
Cite each factual claim using [C1], [C2], etc.
Say that the source material does not establish an answer when needed.
Do not invent citations.
```

Return structured citations rather than trying to parse prose later:

```json
{
  "answer": "The retention period is 30 days [C2].",
  "citations": [
    {
      "id": "C2",
      "document_id": "…",
      "filename": "policy.pdf",
      "pages": [4],
      "section": "Data Retention",
      "quote": "…retained for 30 days…"
    }
  ],
  "retrieval": { "chunks_considered": 30, "chunks_used": 6 }
}
```

HTTP API:

```text
POST   /v1/documents                 upload or register a source
GET    /v1/documents/{id}            ingestion status and metadata
DELETE /v1/documents/{id}            delete source, chunks, embeddings
POST   /v1/questions                 answer against tenant documents
POST   /v1/search                    return retrieved chunks without generation
GET    /healthz
```

`POST /v1/questions`:

```json
{
  "question": "How long is data retained?",
  "document_ids": ["optional-document-id"],
  "filters": { "category": "policy" },
  "top_k": 8
}
```

Important operational choices: deduplicate uploads by content hash, make ingestion asynchronous through a queue/worker, record embedding model/version on every chunk, cache query embeddings, enforce authorization before retrieval, and log retrieval IDs plus latency without logging sensitive document text by default. Test chunk boundaries, metadata propagation, tenant isolation, vector-query filtering, and citation-to-source correctness separately.