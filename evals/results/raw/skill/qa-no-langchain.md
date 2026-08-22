# Document Q&A Service Design

Build the service as a small Python application with explicit modules for ingestion, indexing, retrieval, answer generation, and HTTP transport.

## Architecture

```text
Client
  |
  v
FastAPI
  |
  +-- Document ingestion
  |     - upload or URL intake
  |     - text extraction
  |     - normalization
  |     - chunking
  |
  +-- Indexing
  |     - embedding generation
  |     - vector storage
  |     - document metadata
  |
  +-- Retrieval
  |     - query embedding
  |     - vector search
  |     - metadata filtering
  |     - optional reranking
  |
  +-- Answering
        - prompt construction
        - grounded generation
        - citation mapping
```

Recommended components:

- **FastAPI** for HTTP endpoints
- **PostgreSQL** for documents, chunks, jobs, and permissions
- **pgvector** for embeddings
- **PyMuPDF** for PDFs
- **python-docx** for DOCX files
- **BeautifulSoup** for HTML
- A direct embedding API client
- A direct chat-completions API client
- Background workers using Celery, RQ, or a simple queue

## Data Model

```sql
documents (
  id uuid primary key,
  tenant_id uuid not null,
  filename text,
  source_uri text,
  mime_type text,
  content_hash text not null,
  status text not null,
  created_at timestamptz not null
);

document_chunks (
  id uuid primary key,
  document_id uuid references documents(id),
  chunk_index integer not null,
  text text not null,
  token_count integer,
  page_number integer,
  section_title text,
  char_start integer,
  char_end integer,
  embedding vector(1536),
  metadata jsonb
);

qa_requests (
  id uuid primary key,
  tenant_id uuid not null,
  question text not null,
  answer text,
  citations jsonb,
  created_at timestamptz not null
);
```

Store the original document in object storage. Keep extracted text and chunks in the database.

## Ingestion Pipeline

1. Validate file type and size.
2. Compute a content hash for deduplication.
3. Store the original file.
4. Extract text while preserving page and section boundaries.
5. Normalize whitespace and encoding.
6. Split text into overlapping chunks.
7. Generate embeddings in batches.
8. Insert chunks and vectors.
9. Mark the document as indexed.

Use semantic boundaries first:

- headings
- paragraphs
- list items
- table rows
- page boundaries

Then enforce size limits.

Example chunking policy:

```text
target size: 500-800 tokens
overlap: 75-120 tokens
minimum chunk: 80 tokens
```

Avoid splitting in the middle of a sentence when possible. For every chunk, retain:

```json
{
  "document_id": "...",
  "page_number": 12,
  "section_title": "Refund Policy",
  "chunk_index": 8,
  "char_start": 48210,
  "char_end": 51402
}
```

## Retrieval

For each question:

1. Normalize the query.
2. Generate its embedding.
3. Search the tenant's vectors using cosine distance.
4. Retrieve an initial set, for example top 20.
5. Apply optional metadata filters.
6. Rerank the top results if necessary.
7. Keep the best 4-8 chunks within the model context budget.

Example query:

```sql
SELECT
  c.id,
  c.document_id,
  c.text,
  c.page_number,
  c.section_title,
  d.filename,
  1 - (c.embedding <=> :query_embedding) AS score
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE d.tenant_id = :tenant_id
ORDER BY c.embedding <=> :query_embedding
LIMIT 20;
```

A hybrid search can improve recall:

```text
final_score = 0.7 * vector_score + 0.3 * keyword_score
```

Use full-text search for exact identifiers, names, product codes, and legal clauses.

## Answer Generation

Construct a prompt from retrieved context:

```text
System:
You answer only from the supplied sources.
If the sources do not contain the answer, say that the information is unavailable.
Cite every factual claim using [source number].

Context:
[1] handbook.pdf, page 12
...
[2] policy.docx, section "Refunds"
...

Question:
{question}
```

Require structured output:

```json
{
  "answer": "The refund window is 30 days [1].",
  "citations": [
    {
      "source": 1,
      "chunk_id": "chunk-uuid",
      "document_id": "document-uuid",
      "filename": "handbook.pdf",
      "page": 12,
      "quote": "Customers may request a refund within 30 days..."
    }
  ],
  "confidence": 0.91
}
```

Validate that cited source numbers exist. If the model returns unsupported citations, either remove them or retry with a stricter prompt.

Citations should point to the smallest useful evidence unit and include:

- document name
- page or section
- chunk identifier
- optional character offsets
- short quoted excerpt

## HTTP API

### Upload a document

```http
POST /v1/documents
Content-Type: multipart/form-data
```

Response:

```json
{
  "id": "doc-uuid",
  "status": "queued",
  "filename": "handbook.pdf"
}
```

### Check indexing status

```http
GET /v1/documents/{document_id}
```

Response:

```json
{
  "id": "doc-uuid",
  "status": "indexed",
  "chunk_count": 42
}
```

Possible statuses:

```text
queued | processing | indexed | failed
```

### Ask a question

```http
POST /v1/qa
Content-Type: application/json
```

Request:

```json
{
  "question": "What is the refund period?",
  "document_ids": ["doc-uuid"],
  "top_k": 6,
  "include_quotes": true
}
```

Response:

```json
{
  "answer": "The refund period is 30 days [1].",
  "citations": [
    {
      "id": 1,
      "document_id": "doc-uuid",
      "filename": "handbook.pdf",
      "page": 12,
      "quote": "Customers may request a refund within 30 days."
    }
  ],
  "retrieval": {
    "matches": 6,
    "top_score": 0.89
  }
}
```

### Stream an answer

```http
POST /v1/qa:stream
Accept: text/event-stream
```

Emit events such as:

```text
event: token
data: {"text":"The refund"}

event: citation
data: {"id":1,"document_id":"doc-uuid","page":12}

event: done
data: {}
```

## Suggested Module Layout

```text
app/
  main.py
  api/
    documents.py
    qa.py
  ingestion/
    extractors.py
    normalize.py
    chunker.py
    pipeline.py
  embeddings/
    client.py
  retrieval/
    vector_store.py
    hybrid_search.py
    reranker.py
  generation/
    prompts.py
    answerer.py
    citations.py
  db/
    models.py
    session.py
  workers/
    tasks.py
```

Define narrow interfaces:

```python
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

class VectorStore(Protocol):
    async def upsert(self, chunks: list[Chunk]): ...
    async def search(self, vector: list[float], filters: dict, limit: int): ...

class Generator(Protocol):
    async def answer(self, question: str, contexts: list[Context]) -> Answer: ...
```

This keeps providers replaceable without coupling business logic to SDKs.

## Operational Requirements

- Tenant isolation in every document and retrieval query
- Idempotent ingestion using content hashes
- Batch embedding requests with retry and rate-limit handling
- Per-request timeouts
- Structured logs with request and document IDs
- Metrics for ingestion latency, retrieval scores, token usage, and citation coverage
- Store model names and prompt versions with each answer
- Redact secrets and sensitive document content from logs
- Enforce upload limits and malware scanning
- Delete vectors when a document is deleted

## Quality Tests

Include tests for:

- PDF/DOCX/HTML extraction
- Chunk size and overlap guarantees
- Duplicate document handling
- Tenant filtering
- Retrieval ranking
- Citation page and quote preservation
- “Not found in sources” behavior
- Malformed model output
- API authentication and upload limits

Evaluate with a fixed question set measuring:

```text
retrieval recall@k
answer correctness
citation precision
citation completeness
unsupported-claim rate
```

This design provides a direct, modular retrieval-augmented question-answering service with explicit provenance from uploaded documents through final citations.