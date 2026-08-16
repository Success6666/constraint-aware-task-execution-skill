## Architecture

A practical document QA service can be built as a small set of independently scalable components:

```text
             ┌──────────────┐
 Upload ───▶ │ Ingestion API│ ───▶ Object storage
             └──────┬───────┘
                    │ job
                    ▼
             ┌──────────────┐
             │ Worker Queue │
             └──────┬───────┘
                    ▼
      Parse → Normalize → Chunk → Embed
                    │
                    ▼
              Vector database
                    ▲
                    │ top-k retrieval
             ┌──────┴───────┐
 Question ─▶ │   Query API  │ ─▶ Answer + citations
             └──────────────┘
```

A straightforward stack:

- Python 3.12 and FastAPI
- PostgreSQL with `pgvector`
- S3-compatible object storage
- Redis plus Celery, Dramatiq, or RQ for ingestion jobs
- Provider SDKs directly for embeddings and answer generation
- PyMuPDF for PDF, `python-docx` for Word, and standard parsers for text/HTML

## Data Model

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, checksum)
);

CREATE TABLE chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    heading_path TEXT[],
    char_start INTEGER,
    char_end INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}',
    embedding VECTOR(1536) NOT NULL,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX chunks_embedding_idx
ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX chunks_tenant_document_idx
ON chunks (tenant_id, document_id);
```

Store enough provenance on every chunk to reconstruct a citation: document ID, filename, pages, headings, and source offsets.

## Ingestion

`POST /v1/documents` should:

1. Authenticate the caller and determine `tenant_id`.
2. Stream the file to object storage while computing a checksum.
3. Insert the document with status `queued`.
4. Enqueue an ingestion job.
5. Return `202 Accepted` with the document ID.

The worker then:

1. Extracts text and page/layout metadata.
2. Normalizes whitespace without destroying source offsets.
3. Splits the document by structural boundaries.
4. Creates embeddings in batches.
5. Inserts all chunks transactionally.
6. Marks the document `ready`, or records a structured failure.

Use checksum-based idempotency to prevent accidental duplicate ingestion.

## Chunking

Prefer structure-aware chunking:

1. Split at headings, paragraphs, list boundaries, and pages.
2. Combine small adjacent blocks until reaching roughly 400–800 tokens.
3. Split oversized blocks at sentence boundaries.
4. Include 50–100 tokens of overlap.
5. Preserve the heading path and page range separately from chunk text.

Avoid treating page boundaries as mandatory chunk boundaries. A paragraph spanning two pages should remain coherent, while its citation can still record both pages.

Each chunk should contain embedding text such as:

```text
Document: Employee Handbook
Section: Leave > Parental Leave

Employees may request...
```

Keep the original passage separately so generated citations quote clean source text rather than embedding-only prefixes.

## Retrieval

For each question:

1. Validate document and metadata filters against the caller’s tenant.
2. Generate the query embedding.
3. Retrieve approximately 30 vector candidates.
4. Optionally combine vector similarity with PostgreSQL full-text ranking.
5. Deduplicate highly overlapping chunks.
6. Rerank candidates with a cross-encoder or model-based reranker.
7. Send the best 5–10 passages to the answer model.

A hybrid score can be normalized before combination:

```text
final_score = 0.7 * vector_score + 0.3 * lexical_score
```

The exact weights should be evaluated against a representative question set rather than assumed globally.

Tenant filtering must occur inside the database query:

```sql
SELECT id, document_id, text, page_start, page_end, heading_path,
       1 - (embedding <=> $1) AS score
FROM chunks
WHERE tenant_id = $2
  AND ($3::uuid[] IS NULL OR document_id = ANY($3))
ORDER BY embedding <=> $1
LIMIT $4;
```

## Answer Generation and Citations

Assign each retrieved passage a stable reference label:

```text
[S1] document="handbook.pdf" pages="12-13" section="Parental Leave"
<passage>

[S2] document="benefits.pdf" page="4" section="Eligibility"
<passage>
```

The model should be instructed to:

- Answer only from supplied passages.
- Cite factual statements using `[S1]`, `[S2]`, and so on.
- State that the available documents do not contain the answer when evidence is insufficient.
- Never invent citation labels.

Do not trust model-produced citation details. After generation:

1. Parse citation labels from the answer.
2. Reject unknown labels.
3. Translate valid labels into structured citation objects from server-owned metadata.
4. Optionally verify that every substantive answer sentence has supporting evidence.

Example response:

```json
{
  "answer": "Employees are eligible for 12 weeks of parental leave after six months of employment [S1]. Benefits continue during the leave [S2].",
  "citations": [
    {
      "id": "S1",
      "document_id": "d8f2...",
      "filename": "handbook.pdf",
      "page_start": 12,
      "page_end": 13,
      "section": ["Leave", "Parental Leave"],
      "quote": "Employees who have completed six months..."
    },
    {
      "id": "S2",
      "document_id": "91ac...",
      "filename": "benefits.pdf",
      "page_start": 4,
      "page_end": 4,
      "section": ["Coverage During Leave"],
      "quote": "Medical and dental coverage will continue..."
    }
  ],
  "retrieval": {
    "searched_documents": 2
  }
}
```

## HTTP API

```http
POST   /v1/documents
GET    /v1/documents/{document_id}
DELETE /v1/documents/{document_id}
POST   /v1/questions
GET    /v1/health
```

Upload:

```http
POST /v1/documents
Content-Type: multipart/form-data

file=<binary>
metadata={"department":"legal"}
```

Status:

```json
{
  "id": "d8f2...",
  "status": "processing",
  "filename": "handbook.pdf",
  "error": null
}
```

Question request:

```json
{
  "question": "How much parental leave is available?",
  "document_ids": ["d8f2...", "91ac..."],
  "filters": {
    "department": "hr"
  },
  "max_citations": 6
}
```

Use `200` for completed synchronous answers, `202` for accepted ingestion, `404` for inaccessible resources as well as missing ones, `409` for conflicting ingestion state, and `422` for invalid requests.

## Core Service Interfaces

Keep model and storage dependencies behind small interfaces:

```python
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class SourceChunk:
    id: str
    document_id: str
    text: str
    filename: str
    page_start: int | None
    page_end: int | None
    heading_path: tuple[str, ...]
    score: float


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Retriever(Protocol):
    async def search(
        self,
        *,
        tenant_id: str,
        query_vector: list[float],
        document_ids: Sequence[str] | None,
        limit: int,
    ) -> list[SourceChunk]: ...


class AnswerGenerator(Protocol):
    async def answer(
        self,
        question: str,
        sources: Sequence[SourceChunk],
    ) -> str: ...
```

This keeps provider changes, local models, and retrieval experiments isolated without introducing a large orchestration framework.

## Operational Requirements

- Enforce tenant isolation in every storage query.
- Limit file size, page count, question length, and retrieved context size.
- Scan uploads and reject unsupported or encrypted documents.
- Encrypt stored documents and database backups.
- Retry embedding requests with bounded exponential backoff.
- Record embedding model and chunking version for reproducible reindexing.
- Delete source files and chunks together using an asynchronous deletion workflow.
- Log document IDs and timings, but not raw document or question content by default.
- Track ingestion latency, retrieval latency, answer latency, token usage, empty retrievals, and citation coverage.

## Evaluation

Create a test set containing questions, expected source passages, and acceptable answers. Measure:

- Retrieval recall at `k`
- Mean reciprocal rank
- Answer correctness
- Citation precision and completeness
- Unsupported-claim rate
- Refusal accuracy when the answer is absent
- Latency and cost per question

The most important tests are isolation tests, ingestion idempotency, page-accurate citations, deletion behavior, and questions whose answer does not exist in the indexed documents.