# File Upload API Design

## 1. Security and storage model

Use a quarantine-first workflow:

1. Receive the upload into isolated quarantine storage.
2. Calculate a cryptographic hash while streaming.
3. Validate size, structure, declared metadata, and actual content.
4. Scan for executable or active content.
5. Store only accepted objects in durable object storage.
6. Persist metadata and audit events.
7. Make downloads available only after the object reaches `accepted`.

Quarantine objects are inaccessible to clients and are deleted after a short retention period, whether accepted or rejected.

### Durable storage

Use object storage with:

- Server-side encryption.
- Versioning enabled.
- Private buckets or containers.
- Random, non-user-controlled object keys.
- Integrity checks using SHA-256.
- Replication or equivalent durability guarantees.
- Lifecycle cleanup for quarantined and deleted objects.

The database is the source of truth for object state. Object storage is not directly exposed.

## 2. Upload states

```text
pending
scanning
accepted
rejected
deleted
```

Only `accepted` objects can be downloaded.

A background reconciler handles interrupted transitions, such as an accepted database record whose object copy did not complete.

## 3. API

### Create upload

```http
POST /v1/files
Authorization: Bearer <token>
Content-Type: multipart/form-data
Idempotency-Key: <unique-key>
```

Multipart fields:

- `file`: file contents
- `description`: optional description
- `tags`: optional structured tags

The server must not trust:

- The filename extension.
- The client-provided MIME type.
- Client-provided file size.
- Client-provided hashes.

Response:

```http
202 Accepted
```

```json
{
  "id": "01HR...",
  "status": "scanning",
  "filename": "report.pdf",
  "size": 184320,
  "sha256": "…"
}
```

The request is rejected immediately for invalid authentication, size limits, malformed multipart data, or transport errors.

### Get metadata

```http
GET /v1/files/{fileId}
Authorization: Bearer <token>
```

Response:

```json
{
  "id": "01HR...",
  "status": "accepted",
  "originalFilename": "report.pdf",
  "detectedMediaType": "application/pdf",
  "size": 184320,
  "sha256": "…",
  "createdAt": "2025-01-01T12:00:00Z",
  "acceptedAt": "2025-01-01T12:00:08Z",
  "createdBy": "user-123",
  "tags": ["report"]
}
```

For rejected files, expose only a safe reason such as:

```json
{
  "id": "01HR...",
  "status": "rejected",
  "reason": "executable_or_active_content"
}
```

Do not expose scanner signatures or internal detection details to untrusted callers.

### Download

```http
GET /v1/files/{fileId}/content
Authorization: Bearer <token>
```

Requirements:

- Authorize access to the file before issuing content.
- Return `404` for nonexistent or inaccessible objects where appropriate.
- Permit downloads only for `accepted` files.
- Stream from private storage or issue a short-lived, authorization-bound signed URL.
- Set:

```http
Content-Disposition: attachment; filename="safe-name.ext"
Content-Type: <server-detected-safe-type>
X-Content-Type-Options: nosniff
Cache-Control: private, no-store
```

The original filename must be sanitized for header injection, path traversal, and control characters.

### Delete

```http
DELETE /v1/files/{fileId}
Authorization: Bearer <token>
```

Mark the record deleted, revoke access immediately, and enqueue object deletion according to retention policy.

## 4. Metadata schema

A file record should contain:

```text
id
tenant_id
created_by
original_filename
normalized_filename
declared_media_type
detected_media_type
size_bytes
sha256
storage_key
status
rejection_reason
created_at
accepted_at
deleted_at
retention_until
```

Use a unique constraint on `(tenant_id, idempotency_key)` for idempotent uploads.

Do not use the original filename or user-provided identifiers as the storage key.

## 5. Executable-content rejection

The upload decision must be based on actual bytes and parsed structure, not filename or MIME type.

### Required inspection pipeline

Run all of the following before acceptance:

1. **Size and resource limits**
   - Maximum file size.
   - Maximum decompressed size.
   - Maximum archive nesting depth.
   - Maximum file count in archives.
   - Scan time and memory limits.

2. **Content detection**
   - Detect type from magic bytes and structure using a maintained file-identification library.
   - Compare detected type with the declared MIME type and extension.
   - Reject mismatches when the detected type is executable, active, ambiguous, or outside the allowlist.

3. **Executable format detection**
   Reject native executable formats, including:

   - Windows PE files, DLLs, and drivers.
   - ELF binaries.
   - Mach-O binaries.
   - WebAssembly modules when not explicitly supported.
   - DOS and other recognized binary executable formats.
   - Scripts identified by shebang, interpreter syntax, or parser analysis.
   - Batch, shell, PowerShell, JavaScript, VBScript, and similar executable scripts.

4. **Active document detection**
   Reject or disarm formats containing active content. The simplest secure policy is rejection of:

   - Macro-enabled Office documents.
   - Office documents containing VBA or executable relationships.
   - PDFs containing JavaScript, launch actions, embedded executables, or unsafe active actions.
   - HTML and script-capable SVG.
   - Shortcut files and other files that execute commands.
   - Archives containing any rejected executable or active-content member.

5. **Archive inspection**
   - Recursively inspect ZIP, TAR, 7z, and supported archive formats.
   - Reject executable members even when their names use harmless extensions.
   - Reject encrypted archives that cannot be inspected.
   - Reject malformed archives and decompression bombs.

6. **Polyglot and ambiguity handling**
   - Check for executable signatures at offsets beyond byte zero.
   - Parse the file according to its detected format.
   - Reject files that validly parse as both a permitted format and an executable or active format.
   - Reject any file whose content cannot be classified with high confidence.

7. **Malware scanning**
   - Run an antivirus or malware scanner and optional YARA rules.
   - A malware-clean result does not override the executable-content policy.
   - Any scanner error results in `rejected` or a controlled retry, never acceptance.

### Format policy

Use an explicit allowlist of supported safe formats rather than attempting to support every file type. Each allowed format needs a parser or validator that confirms:

- The complete file structure is valid.
- No executable or active components are present.
- Resource limits are respected.

For example, a service may allow ordinary JPEG, PNG, and non-active PDF files while rejecting scripts, binaries, macro-enabled documents, HTML, active SVG, and opaque archives.

The decision rule is:

```text
accept only if:
  format is explicitly allowed
  AND structure is valid
  AND no executable or active content is found
  AND malware scanning succeeds
  AND all resource limits pass
```

Any uncertainty results in rejection.

## 6. Audit logging

Create append-only audit records for:

- Upload requested.
- Upload completed.
- Scan started.
- Scan completed.
- Upload accepted.
- Upload rejected.
- Download authorized.
- Download completed or failed.
- Metadata viewed or changed.
- File deleted.
- Access denied.
- Administrative override, if overrides exist.

Each event should include:

```text
event_id
timestamp
tenant_id
actor_id
action
file_id
request_id
source_ip
user_agent
result
reason
sha256
previous_event_hash
event_hash
```

Chain events with hashes or store them in an append-only, access-controlled audit system. Audit records must not contain file contents or secrets.

## 7. Authorization

Enforce authorization for every operation:

- Tenant isolation.
- File ownership or explicit sharing.
- Role-based delete and administrative access.
- Separate permission for metadata and content download.
- No authorization decisions based solely on a filename.
- Rate limits on uploads, scans, metadata requests, and downloads.

## 8. Verification plan

### Unit tests

Test:

- Filename and MIME-type normalization.
- SHA-256 calculation.
- Storage-key generation.
- Filename header sanitization.
- State transitions.
- Authorization rules.
- Idempotency behavior.
- Audit event creation and hash chaining.
- Size, nesting, file-count, and decompression limits.

### Content-security tests

Include files with:

- A native executable renamed to `.jpg`, `.pdf`, or `.txt`.
- A native executable uploaded with `image/jpeg` or `application/pdf`.
- Scripts renamed to harmless extensions.
- A script with no extension and a shebang.
- PE, ELF, and Mach-O binaries.
- Macro-enabled and macro-containing Office documents.
- PDFs containing JavaScript or launch actions.
- Active SVG and HTML.
- Archives containing executables with misleading names.
- Nested archives containing executables.
- Encrypted archives.
- Polyglot files containing both an allowed signature and executable content.
- Malformed files and decompression bombs.
- Files with executable signatures at nonzero offsets.

Every such case must be rejected and must not become downloadable.

### API integration tests

Verify:

- Upload returns `scanning` before acceptance.
- Rejected files cannot be downloaded.
- Accepted files can be downloaded only by authorized users.
- MIME type and extension spoofing do not bypass scanning.
- Duplicate idempotency keys do not create duplicate records.
- Failed scans do not leave accessible objects.
- Interrupted object/database transitions are reconciled safely.
- Deleted files immediately stop being downloadable.
- Audit records are created for successful and denied operations.

### Property and regression tests

Maintain a corpus of known malicious, executable, malformed, and polyglot samples. Run it on every scanner, parser, or allowlist change, and fail the deployment if any previously rejected sample becomes accepted.
