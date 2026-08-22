Design the service as a quarantine-first upload pipeline:

```text
Client -> Upload API -> Quarantine object storage
                    -> Validate bytes
                    -> Persist metadata + audit event
                    -> Promote to durable storage
Client <- Download API <- authorization + audit logging
```

## API

### Create upload

`POST /v1/files`

Request:

```http
Content-Type: multipart/form-data
file=<binary>
```

Response:

```json
{
  "id": "file_01J...",
  "status": "accepted",
  "filename": "report.pdf",
  "media_type": "application/pdf",
  "size": 182734,
  "sha256": "..."
}
```

Possible responses:

- `201 Accepted`
- `400` malformed request
- `413` size limit exceeded
- `415` unsupported or executable content
- `422` content cannot be safely classified

### Get metadata

`GET /v1/files/{id}`

```json
{
  "id": "file_01J...",
  "original_filename": "report.pdf",
  "detected_media_type": "application/pdf",
  "declared_media_type": "application/octet-stream",
  "size": 182734,
  "sha256": "...",
  "status": "available",
  "created_at": "2026-08-22T10:00:00Z"
}
```

Do not expose internal storage keys or quarantine paths.

### Download

`GET /v1/files/{id}/content`

Behavior:

- Authenticate and authorize access.
- Log the download attempt and result.
- Stream from durable storage or return a short-lived signed URL.
- Use the server-detected type, never the client-provided MIME type.
- Return:

```http
Content-Type: application/pdf
Content-Disposition: attachment; filename="report.pdf"
X-Content-Type-Options: nosniff
Cache-Control: private, no-store
```

Sanitize filenames and force attachment for untrusted types.

## Validation pipeline

1. Enforce request, per-user, and total size limits.
2. Stream bytes to quarantine storage while calculating SHA-256.
3. Ignore the supplied extension and MIME type for security decisions.
4. Detect the actual type from magic bytes and format parsers.
5. Reject known executable formats, including:

   - PE/COFF: `MZ`, PE headers, Windows installers
   - ELF: `0x7f ELF`
   - Mach-O and Fat Mach-O
   - Java class files and executable bytecode
   - WebAssembly modules, if not explicitly supported
   - Shell scripts and scripts with executable interpreters
   - Office documents containing macros or executable embedded objects
   - PDFs or archives containing disallowed active content, according to policy

6. Inspect archives recursively:

   - Limit nesting depth, entry count, expanded size, and compression ratio.
   - Reject executable entries, scripts, macros, installers, and path traversal.
   - Reject ambiguous or malformed polyglot files.
7. Run antivirus/malware scanning and optionally sandbox detonation for high-risk formats.
8. Compare detected type with the allowlist. A misleading extension or MIME type must never override detection.
9. Promote only validated objects from quarantine to durable storage.
10. On any failure, delete or expire the quarantine object and return `415` or `422`.

A conservative policy is an allowlist of business-required types, such as PDF, PNG, JPEG, and plain text. Unknown, encrypted, malformed, or ambiguous files should be rejected rather than accepted.

Use a validation result such as:

```json
{
  "status": "rejected",
  "reason_code": "EXECUTABLE_CONTENT",
  "detected_types": ["application/x-dosexec"],
  "scanner": "content-inspector-v3"
}
```

Avoid returning detailed scanner signatures to unauthenticated callers.

## Durable storage

Use:

- Object storage with versioning, encryption, retention, and lifecycle policies.
- A relational database for metadata and authorization.
- A transactional outbox for audit events and asynchronous scanner results.

Example tables:

```sql
files (
  id uuid primary key,
  owner_id uuid not null,
  object_key text unique,
  original_filename text not null,
  declared_media_type text,
  detected_media_type text,
  size_bytes bigint not null,
  sha256 char(64) not null,
  status text not null, -- quarantined, scanning, available, rejected, deleted
  created_at timestamptz not null,
  deleted_at timestamptz
);

file_audit_events (
  id bigint generated always as identity primary key,
  file_id uuid,
  actor_id uuid,
  action text not null,
  outcome text not null,
  reason_code text,
  request_id text not null,
  ip_hash text,
  user_agent_hash text,
  created_at timestamptz not null
);
```

Promote storage and metadata atomically from the application’s perspective: write the object, commit metadata, then publish the outbox event. A reconciler should detect orphaned objects and database rows.

## Audit logging

Record:

- Upload accepted, rejected, or failed
- Scan started and completed
- File downloaded, denied, expired, or missing
- Metadata access
- Deletion and retention-policy actions
- Actor, request ID, timestamp, outcome, and reason code

Make audit storage append-only, access-controlled, monitored, and retained according to policy. Never log file contents, secrets, or raw sensitive filenames unless required.

## Tests

Include unit, integration, and security tests.

### Validation tests

- Valid files with correct extension and MIME type.
- Executable bytes named `.pdf`, `.jpg`, `.txt`, and `.png`.
- Executable bytes declared as `application/pdf` or `image/png`.
- PE, ELF, Mach-O, script, bytecode, macro-enabled Office, and installer fixtures.
- Archives containing executables, nested archives, macros, symlinks, and traversal paths.
- Polyglot files with valid image/PDF headers plus executable payloads.
- Truncated, malformed, encrypted, and unknown files.
- Compression bombs and excessive archive nesting.
- Content whose first bytes are misleading but parser detection identifies another type.
- Scanner timeout, scanner failure, and quarantine cleanup.

### API and storage tests

- Authorization isolation between owners.
- Download headers and filename sanitization.
- No download is possible before promotion.
- Idempotency using a client request ID.
- Retry behavior after object-store or database failure.
- Reconciliation of orphaned objects.
- Audit event emitted exactly once per externally visible action.
- Signed download URLs expire and cannot access another file.

### Property and fuzz tests

Fuzz MIME sniffing, archive parsing, filename handling, and multipart boundaries. Assert that no input classified as executable or ambiguous reaches durable storage or download.