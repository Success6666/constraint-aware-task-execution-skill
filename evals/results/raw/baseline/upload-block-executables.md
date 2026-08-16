## Architecture

Use three durable stores:

- **Object storage**: immutable file bytes, versioning enabled, server-side encryption.
- **Relational database**: metadata, upload state, hashes, ownership, scan results.
- **Append-only audit store**: security and lifecycle events, exported to tamper-resistant retention storage.

Files enter a private **quarantine bucket** and are never downloadable until validation succeeds. Approved files are copied server-side to a separate private **clean bucket**. Clients never choose object keys or receive direct quarantine access.

## API

### Create upload

```http
POST /v1/files
Content-Type: application/json
Idempotency-Key: 76a...
Authorization: Bearer ...
```

```json
{
  "filename": "quarterly-report.pdf",
  "declaredContentType": "application/pdf",
  "size": 482910
}
```

Response:

```json
{
  "fileId": "fil_01J...",
  "status": "awaiting_upload",
  "upload": {
    "url": "https://object-store/...",
    "method": "PUT",
    "headers": {
      "Content-Type": "application/octet-stream"
    },
    "expiresAt": "2026-08-16T12:05:00Z"
  }
}
```

The signed upload policy enforces the exact object key and maximum size.

### Complete upload

```http
POST /v1/files/{fileId}/complete
Idempotency-Key: 19c...
```

Returns `202 Accepted` while validation runs:

```json
{
  "fileId": "fil_01J...",
  "status": "scanning"
}
```

Completion verifies that the stored object exists and matches the reserved size. It then queues scanning using a transactional outbox.

### Metadata

```http
GET /v1/files/{fileId}
```

```json
{
  "id": "fil_01J...",
  "filename": "quarterly-report.pdf",
  "size": 482910,
  "sha256": "7e9...",
  "detectedContentType": "application/pdf",
  "status": "available",
  "createdAt": "2026-08-16T12:00:00Z"
}
```

Do not expose internal object keys, scanner diagnostics, or quarantine reasons to unauthorized users.

### Download

```http
GET /v1/files/{fileId}/content
```

After authorization and an `available` status check, return either a short-lived signed clean-object URL or stream the object. Use:

```http
Content-Disposition: attachment; filename="quarterly-report.pdf"
X-Content-Type-Options: nosniff
Content-Security-Policy: sandbox
Cache-Control: private, no-store
```

Never serve uploaded files inline from the application’s origin. Prefer a separate download origin without cookies.

### Delete

```http
DELETE /v1/files/{fileId}
```

Perform a soft delete immediately, revoke downloads, and enqueue durable object deletion according to retention policy.

### Audit access

```http
GET /v1/files/{fileId}/audit?cursor=...
```

Restrict this endpoint to owners with appropriate permissions and administrators.

## Metadata Model

`files`:

```text
id                    UUID/ULID primary key
tenant_id             tenant boundary
owner_id
original_filename
declared_content_type nullable
detected_content_type nullable
expected_size
actual_size           nullable
sha256                nullable
quarantine_object_key
clean_object_key      nullable
status                awaiting_upload | scanning | available | rejected | failed | deleted
rejection_code        nullable, non-sensitive enum
scanner_version       nullable
created_at
updated_at
available_at          nullable
deleted_at            nullable
row_version
```

Add unique constraints on `(tenant_id, idempotency_key, operation)` and indexes on ownership, status, creation time, and hash. Treat filenames as display metadata: strip path components, control characters, bidi overrides, and invalid Unicode; generate storage keys independently.

## Executable Rejection

Extension and client MIME type are only hints. Validation must inspect the actual bytes in an isolated worker with no network access, read-only inputs, strict CPU/memory/time limits, and a non-privileged identity.

Reject when any layer identifies executable or active content:

1. **Binary identification**
   Inspect magic bytes and structural headers across the entire supported format, including PE/DOS, ELF, Mach-O, Java class/JAR, WebAssembly, shared libraries, object files, boot images, APK/DEX, MSI, and executable script formats.

2. **Content parsing**
   Parse only explicitly allowed formats with maintained format-aware libraries. A claimed PDF must parse as a PDF; an image must fully decode as the expected image type. Reject malformed, ambiguous, truncated, encrypted, or unsupported files.

3. **Polyglot and embedded-content detection**
   Reject conflicting signatures, executable overlays, appended payloads, embedded launch actions, macros, OLE objects, JavaScript, or other active content. Merely finding a valid image or PDF header is insufficient.

4. **Archives and containers**
   Safest policy: reject archives entirely. If required, recursively scan every entry with limits on depth, expanded bytes, entry count, compression ratio, duplicate paths, symlinks, and traversal paths. Reject the whole upload if any entry is executable, encrypted, ambiguous, or unscannable.

5. **Scripts and text**
   Reject shell, PowerShell, batch, JavaScript, VBScript, Python, Perl, PHP, and similar source based on shebangs, syntax/content classifiers, and policy, even when named `.txt`.

6. **Malware scanning**
   Run a current malware engine as an additional control. A clean antivirus result does not override format-policy rejection.

A strong allowlist is more defensible than a blacklist. For example, accept only fully decoded JPEG/PNG and sanitized, reparsed PDF files. For formats that can carry active content, transform them into a safe canonical representation and scan the generated output. Fail closed on scanner errors, timeouts, version mismatches, and unknown formats.

Calculate SHA-256 while streaming from storage. Re-read the object for scanning, then copy the exact scanned object version to clean storage. Record source version IDs and use conditional copy operations to prevent replacement between scanning and promotion.

## Audit Log

Record immutable events such as:

```text
UPLOAD_RESERVED
UPLOAD_COMPLETED
SCAN_STARTED
SCAN_PASSED
SCAN_REJECTED
SCAN_FAILED
FILE_PROMOTED
METADATA_READ
DOWNLOAD_AUTHORIZED
DOWNLOAD_COMPLETED
DOWNLOAD_DENIED
FILE_DELETED
```

Each event contains:

```text
event_id, occurred_at, tenant_id, actor_id, actor_type,
file_id, action, outcome, request_id, source_ip,
user_agent, object_version, sha256, scanner_version,
reason_code, previous_event_hash
```

Exclude file contents, credentials, signed URLs, and raw sensitive scanner output. Restrict audit access, define retention explicitly, and export logs to immutable/WORM storage. A hash chain helps reveal modification but does not replace access controls and immutable retention.

## Operational Guarantees

- Authenticate every operation and authorize by tenant and owner.
- Enforce per-file, per-user, and tenant quotas before issuing upload URLs.
- Stream processing; never load an entire file into application memory.
- Use idempotency keys for create and completion.
- Use database transactions plus an outbox for scan and deletion jobs.
- Make workers retryable and state transitions conditional.
- Reconcile abandoned uploads, missing objects, and stuck scans.
- Apply lifecycle rules to purge rejected quarantine objects quickly.
- Rate-limit upload, metadata, download, and audit endpoints.
- Keep object storage private and disable public ACLs globally.

## Tests

Unit tests should cover state transitions, authorization, filename normalization, signature classification, parser failures, quota enforcement, idempotency, and audit redaction.

Security fixtures must include:

- An executable renamed to `.jpg` with `image/jpeg`.
- PE, ELF, Mach-O, WASM, JAR, APK, MSI, scripts, and shared libraries.
- A valid image prefix followed by an executable payload.
- PDF/image polyglots and trailing executable overlays.
- Office documents with macros or embedded objects.
- Scripts named `.txt`, including whitespace before a shebang.
- Malformed, truncated, encrypted, and parser-bomb files.
- Nested archives, zip bombs, traversal paths, symlinks, and executable archive members.
- MIME/extension/content disagreements.
- Files changed between completion and scanning.
- Scanner timeout, crash, unavailable engine, and unknown result.

Integration tests should use real database and object-storage implementations and verify quarantine isolation, exact object-version promotion, download denial before approval, retries without duplicate events, deletion, and audit persistence.

End-to-end tests should prove that every executable fixture is rejected and cannot be downloaded, while representative valid allowlisted files survive upload, validation, restart, and download byte-for-byte. Fuzz the format parsers and run the malicious fixture corpus on every scanner or policy update.
