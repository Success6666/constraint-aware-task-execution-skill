## API

### Upload

`POST /v1/files`

- Accepts `multipart/form-data`.
- Streams input directly to quarantine storage.
- Enforces request, file-size, filename, and rate limits.
- Computes SHA-256 while streaming.
- Ignores client-provided extension and MIME type for security decisions.
- Scans content before making it downloadable.

Response:

```json
{
  "id": "file_01J...",
  "status": "available",
  "size": 184203,
  "sha256": "…",
  "detectedType": "image/png",
  "originalName": "avatar.png",
  "createdAt": "2026-08-22T10:00:00Z"
}
```

Possible statuses:

- `pending_scan`
- `available`
- `rejected`
- `deleted`

### Metadata

`GET /v1/files/{fileId}`

Returns metadata, ownership, scan status, detected type, size, checksum, and timestamps. It never returns storage-provider keys.

### Download

`GET /v1/files/{fileId}/content`

1. Authenticate and authorize access.
2. Confirm status is `available`.
3. Stream from durable storage or issue a short-lived signed URL.
4. Set safe headers:

```http
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="avatar.png"
X-Content-Type-Options: nosniff
Cache-Control: private, no-store
```

Files are stored with execution disabled and are served from a non-executable domain.

### Audit

`GET /v1/files/{fileId}/audit`

Audit events are append-only:

```json
{
  "event": "file.rejected",
  "fileId": "file_01J...",
  "actorId": "user_123",
  "reason": "executable_content",
  "ip": "…",
  "userAgent": "…",
  "createdAt": "2026-08-22T10:00:02Z"
}
```

Record upload, scan started/completed, rejection, download, metadata access, deletion, and authorization failures. Restrict audit reads to administrators or compliance roles.

## Durable storage

Use:

- PostgreSQL for metadata and authorization.
- S3-compatible object storage for content.
- Separate buckets or prefixes for `quarantine` and `available`.
- Encryption at rest, TLS in transit, object versioning, lifecycle cleanup, and replication.
- Random opaque object keys; never use user filenames as keys.
- Database transaction/outbox pattern so audit events are not lost.
- Idempotency keys on upload requests.

Suggested metadata fields:

```text
id
owner_id
original_name
size_bytes
sha256
client_content_type
detected_content_type
storage_key
status
scan_reason
created_at
scanned_at
deleted_at
```

## Executable-content rejection

Treat the filename and client MIME type as hints only. Use an allowlist plus content inspection:

1. Read enough bytes to identify file signatures using a maintained `libmagic`-style detector.
2. Reject executable signatures such as:
   - Windows PE: `MZ` with valid PE headers
   - ELF
   - Mach-O
   - Java class files
   - WebAssembly
   - DOS/boot binaries
   - scripts with a shebang
3. Inspect container formats:
   - ZIP, 7z, tar, ISO, and similar archives
   - Recursively scan entries
   - Reject executable entries and active content such as macros
   - Enforce recursion, entry-count, compression-ratio, and expanded-size limits
4. Detect polyglots and mismatched signatures where practical.
5. Optionally run antivirus/sandbox scanning as an additional signal.
6. Only promote the object from quarantine after every scanner passes.

A safer default is to allow only explicitly supported non-executable types, for example PNG, JPEG, PDF without active content, and plain text.

Rejected content remains inaccessible. Delete it after a short retention period unless incident investigation requires preservation.

## Failure handling

- Scanner timeout or unavailable: leave status `pending_scan`; never make the file available.
- Partial upload: abort multipart upload and remove quarantine data.
- Database failure after storage write: reconcile orphaned objects asynchronously.
- Duplicate checksum: optionally deduplicate storage while preserving separate ownership metadata.
- All rejection responses should be generic to avoid exposing scanner details; detailed reasons belong in audit records.

## Tests

### Unit tests

- SHA-256 and byte-count calculation.
- Signature detection independent of extension and MIME.
- PE, ELF, Mach-O, script, Java, and WebAssembly fixtures.
- Renamed executables such as `virus.jpg` with `image/jpeg`.
- Executables uploaded with benign extensions and misleading MIME headers.
- Nested archives containing executables.
- Archive bombs, path traversal entries, excessive nesting, and oversized expansion.
- Polyglot and truncated-file handling.
- Filename normalization and control-character rejection.
- Scanner timeout and error behavior.

### Integration tests

- Upload is quarantined and unavailable before scanning.
- Clean files become downloadable only after promotion.
- Rejected files cannot be downloaded.
- Durable storage keys are opaque and non-executable.
- Authorization prevents cross-tenant metadata and content access.
- Audit events are generated for every required action.
- Idempotent retries do not create inconsistent metadata.
- Orphan reconciliation removes abandoned objects.
- Signed download URLs expire and cannot bypass authorization.

### Security tests

- Content cannot be executed through the download endpoint.
- `Content-Type` and `Content-Disposition` headers prevent browser interpretation.
- MIME spoofing does not bypass scanning.
- Archive nesting cannot exhaust memory, CPU, or disk.
- Audit records cannot be modified by normal application users.