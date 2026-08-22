# Document Question-Answering Service

## 1. Architecture

- **HTTP API:** FastAPI, synchronous query handling and asynchronous ingestion jobs.
- **Object storage:** S3-compatible storage for original documents.
- **Metadata and search database:** PostgreSQL with:
  - `pgvector` for embeddings
  - PostgreSQL full-text search for keyword retrieval
- **Background workers:** Celery, RQ, or a managed queue worker.
- **LLM and embedding clients:** Direct provider SDK calls.
- **Authentication:** JWT or API keys, with tenant isolation.
- **Observability:** Structured logs, request IDs, latency metrics, token usage, retrieval scores, and ingestion status.

### Request flow

1. Client uploads a document.
2. Service stores the original object and creates an ingestion job.
3. Worker extracts text and layout metadata.
4. Text is normalized and split into chunks.
5. Chunks are embedded and indexed.
6. User submits a question.
7. Service retrieves candidate chunks using vector and keyword search.
8. Results are reranked.
9. The answer model receives only the question and selected context.
10. The response includes an answer and precise citations.

---

## 2. Core data model

### `documents`

```text
id                  UUID primary key
tenant_id           UUID not null
filename            TEXT not null
content_type        TEXT not null
object_uri          TEXT not null
sha256              TEXT not null
version             INTEGER not null
status              enum(uploaded, processing, ready, failed)
page_count          INTEGER
created_at          TIMESTAMP
updated_at          TIMESTAMP
error_message       TEXT nullable
```

Unique constraint:

```text
(tenant_id, sha256)
```

### `document_chunks`

```text
id                  UUID primary key
document_id         UUID not null
tenant_id           UUID not null
chunk_index         INTEGER not null
text                TEXT not null
embedding           VECTOR(<embedding dimensions>)
page_start          INTEGER nullable
page_end            INTEGER nullable
char_start          INTEGER nullable
char_end            INTEGER nullable
section_path        TEXT nullable
source_locator      JSONB
token_count         INTEGER
created_at          TIMESTAMP
```

`source_locator` should preserve citation data such as:

```json
{
  "pages": [4, 5],
  "heading": "Security Requirements",
  "paragraphs": [12, 13],
  "bbox": [
    {"page": 4, "x": 72, "y": 180, "width": 450, "height": 90}
  ]
}
```

### `ingestion_jobs`

```text
id                  UUID primary key
tenant_id           UUID not null
document_id         UUID not null
status              enum(queued, running, complete, failed)
attempts            INTEGER
started_at          TIMESTAMP
completed_at        TIMESTAMP
error_message       TEXT nullable
```

All database queries must filter by `tenant_id`.

---

## 3. Ingestion pipeline

### Supported inputs

Initially support:

- PDF
- DOCX
- TXT
- Markdown
- HTML

Reject unsupported or encrypted documents with a clear ingestion error.

### Processing stages

1. **Validate**
   - Check content type, size limit, and malware scan result.
   - Compute SHA-256.
   - Deduplicate within the tenant.

2. **Extract**
   - Preserve page boundaries for PDFs.
   - Preserve headings, lists, tables, and paragraph order.
   - Extract OCR text for scanned PDFs when no usable text layer exists.
   - Store table content in readable text form while retaining page metadata.

3. **Normalize**
   - Normalize whitespace and Unicode.
   - Remove repeated headers and footers when confidently detected.
   - Preserve meaningful formatting and section hierarchy.
   - Do not merge text across pages without retaining page references.

4. **Chunk**
   - Split primarily at headings, paragraphs, list boundaries, and table boundaries.
   - Target approximately **500–800 tokens** per chunk.
   - Use **75–100 token overlap** only when splitting a logical section.
   - Keep a table together where possible.
   - Attach document title, section path, and page range to every chunk.
   - Assign deterministic `chunk_index` values.

5. **Embed**
   - Use one fixed embedding model for all indexed content.
   - Normalize vectors if required by the model.
   - Batch embedding requests.
   - Record the embedding model and version in index metadata.

6. **Index**
   - Insert chunks transactionally.
   - Create vector and full-text indexes.
   - Mark the document `ready` only after all chunks are searchable.

If any stage fails, mark the job and document as `failed`; retain the error and permit retrying.

---

## 4. Retrieval

### Query processing

1. Normalize the question.
2. Optionally rewrite only for retrieval, never for the final answer.
3. Generate a query embedding.
4. Run two searches:
   - Vector similarity search: top 30 chunks
   - PostgreSQL full-text search: top 30 chunks
5. Combine results using reciprocal rank fusion.
6. Remove duplicate or near-duplicate chunks.
7. Rerank the top 30 using a cross-encoder or equivalent reranker.
8. Send the top 6–10 chunks to the answer model, subject to a context-token limit.

### Filtering

Support filters for:

- `document_id`
- document version
- metadata
- tenant
- optional collection or project

Never retrieve chunks from another tenant.

### Retrieval thresholds

Return an explicit “not enough information” response when:

- no candidate passes the minimum similarity threshold, or
- reranked evidence is not sufficiently relevant.

Thresholds should be calibrated against an evaluation set rather than treated as universal constants.

---

## 5. Answer generation

The answer model receives:

```text
System instructions:
- Answer only from the supplied sources.
- Do not invent facts.
- If the sources do not support the answer, say so.
- Place a citation after every factual claim that depends on a source.
- Distinguish explicitly between sourced facts and uncertainty.

Question:
{user question}

Sources:
[1] {document title}, pages {page range}, chunk {id}
{text}

[2] ...
```

The service should:

- Require citations for every substantive answer claim.
- Prefer concise answers.
- Preserve uncertainty and conflicting source statements.
- Refuse to answer outside the retrieved evidence.
- Return the source excerpts used for auditing.
- Optionally stream answer tokens, but citations must be complete and valid in the final response.

A post-generation citation check should verify that every citation refers to a retrieved chunk. Invalid citations should cause regeneration or a structured failure, not be returned silently.

---

## 6. Citation format

Use stable citation identifiers based on document and chunk identity:

```json
{
  "document_id": "8d...",
  "chunk_id": "c2...",
  "label": "Employee Handbook, p. 12",
  "page_start": 12,
  "page_end": 12,
  "section": "Leave Policy",
  "quote": "Employees may carry over up to five days..."
}
```

Example answer:

```text
Employees may carry over up to five unused leave days into the next year. [1]
```

Citation labels must resolve through an API endpoint and should remain stable across unrelated document ingestions.

---

## 7. HTTP API

### Upload a document

```http
POST /v1/documents
Content-Type: multipart/form-data
Authorization: Bearer ...
```

Response:

```json
{
  "document_id": "8d...",
  "status": "processing",
  "filename": "handbook.pdf"
}
```

### Get document status

```http
GET /v1/documents/{document_id}
```

Response:

```json
{
  "document_id": "8d...",
  "status": "ready",
  "chunk_count": 142,
  "page_count": 38
}
```

### Ask a question

```http
POST /v1/qa
Content-Type: application/json
```

Request:

```json
{
  "question": "How many leave days can employees carry over?",
  "document_ids": ["8d..."],
  "filters": {},
  "top_k": 8,
  "include_sources": true
}
```

Response:

```json
{
  "answer": "Employees may carry over up to five unused leave days into the next year. [1]",
  "citations": [
    {
      "id": 1,
      "document_id": "8d...",
      "chunk_id": "c2...",
      "label": "Employee Handbook, p. 12",
      "page_start": 12,
      "page_end": 12,
      "quote": "Employees may carry over up to five days..."
    }
  ],
  "retrieval": {
    "candidate_count": 60,
    "selected_count": 3
  },
  "request_id": "req_..."
}
```

### List documents

```http
GET /v1/documents?status=ready&limit=50&cursor=...
```

### Delete a document

```http
DELETE /v1/documents/{document_id}
```

Deletion must remove the object, metadata, chunks, embeddings, and search records.

### Common errors

- `400`: malformed request or unsupported document
- `401/403`: authentication or tenant access failure
- `404`: unknown resource
- `409`: duplicate document
- `413`: document too large
- `422`: ingestion failed
- `429`: rate limit exceeded
- `503`: dependent model or storage service unavailable

---

## 8. Operational requirements

- Enforce upload size, page count, token, and query-rate limits.
- Encrypt documents at rest and use TLS in transit.
- Treat document text as untrusted input; prevent prompt instructions inside documents from overriding system instructions.
- Redact secrets from logs.
- Version parser, chunker, embedding model, and answer model.
- Keep ingestion and query timeouts bounded.
- Support retries with idempotent job identifiers.
- Track model cost, retrieval latency, generation latency, and citation-validation failures.
- Use database transactions so partially indexed documents are never marked ready.

---

## 9. Verification

### Unit tests

- Text normalization
- Header/footer removal
- Page and section metadata preservation
- Chunk size and overlap rules
- Deterministic chunk ordering
- Tenant isolation
- Citation formatting and validation
- Idempotent ingestion

### Integration tests

- Upload each supported file type
- OCR fallback for scanned PDFs
- End-to-end ingestion to searchable status
- Vector plus keyword retrieval
- Document deletion
- API authentication and authorization
- Failure and retry behavior

### Evaluation set

Create questions labeled with:

- Expected answer
- Supporting document chunks
- Unanswerable status where applicable

Measure:

- Retrieval recall@k
- MRR or nDCG
- Answer correctness
- Citation precision
- Citation recall
- Abstention accuracy
- p50/p95 query latency
- Ingestion throughput

Release acceptance criteria should include:

- No cross-tenant retrieval in security tests
- Every factual answer claim has a valid citation
- Unanswerable questions are not answered with fabricated content
- All supported formats ingest successfully
- Citation page and section metadata match the source document.
