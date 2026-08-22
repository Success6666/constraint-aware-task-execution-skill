Design the upload service as a quarantine-based pipeline:

```text
Client
  -> POST /v1/files
      -> API validates request and streams to temporary storage
      -> create metadata row: QUARANTINED
      -> content inspection
          - signature/type detection
          - executable format detection
          - archive inspection
          - malware scan, if available
      -> promote only CLEAN objects to durable storage
      -> append audit event
```

## API

### `POST /v1/files`

Multipart upload or streamed binary body.

Request metadata:

```json
{
  "filename": "report.pdf",
  "declared_content_type": "application/pdf",
  "purpose": "invoice"
}
```

Response:

```json
{
  "id": "file_01J...",
  "status": "quarantined",
  "size": 483920,
  "sha256": "...",
  "created_at": "2026-08-16T10:15:00Z"
}
```

The API must never trust the filename or declared MIME type. Upload limits should be enforced while streaming, including maximum size and request timeout.

### `GET /v1/files/{id}`

Returns metadata and current status:

```json
{
  "id": "file_01J...",
  "filename": "report.pdf",
  "detected_media_type": "application/pdf",
  "size": 483920,
  "sha256": "...",
  "status": "clean",
  "created_at": "2026-08-16T10:15:00Z"
}
```

### `GET /v1/files/{id}/download`

Only `clean` files are downloadable. The handler should:

- authorize access before revealing metadata or bytes;
- stream from object storage;
- set `Content-Type` from the detected type, not the user-supplied type;
- set `Content-Disposition: attachment` with a sanitized filename;
- set `X-Content-Type-Options: nosniff`;
- avoid redirecting to an unauthenticated public object URL.

Quarantined, rejected, missing, or unauthorized files should not disclose whether an object exists beyond the intended authorization model.

## Storage and metadata

Use durable object storage for bytes and a relational database for metadata.

Object keys should be generated identifiers, never user filenames:

```text
quarantine/{file_id}
objects/{sha256}/{file_id}
```

Store at least:

- file ID and owner/tenant;
- original filename, sanitized display filename;
- declared and detected media types;
- byte size and SHA-256 checksum;
- storage key, storage version/ETag;
- status: `quarantined`, `scanning`, `clean`, `rejected`, `deleted`;
- rejection reason code;
- scanner/policy version;
- timestamps and retention/deletion data.

Use object-store encryption, private buckets, versioning, lifecycle cleanup for abandoned quarantine objects, and database transactions/outbox events so metadata and audit records remain durable.

A unique checksum can support deduplication, but authorization must remain per file record rather than being inferred from shared content.

## Rejecting executable content

Executable detection should be based on bytes and structure, with the upload treated as untrusted until inspection completes.

The inspection policy should:

1. Detect binary signatures and parse recognized formats:
   - Windows PE: `MZ` plus valid PE header;
   - ELF;
   - Mach-O and fat Mach-O;
   - Java class files;
   - WebAssembly;
   - other organization-approved executable formats.

2. Detect script/interpreter content:
   - shebang lines such as `#!/bin/sh`, `#!/usr/bin/python`;
   - known script formats when their parser identifies executable behavior.

3. Inspect archives recursively:
   - ZIP, TAR, 7z, gzip, and similar formats;
   - reject if an entry is executable;
   - enforce recursion depth, expanded-size, entry-count, and compression-ratio limits;
   - reject encrypted archives that cannot be inspected;
   - reject malformed or ambiguous archive content.

4. Compare multiple independent signals:
   - magic-byte detection;
   - structural parser result;
   - declared MIME type;
   - filename extension.

The extension and MIME type are only metadata. A file named `photo.jpg` with `application/jpeg` that contains a PE executable must be rejected. Conversely, a benign file with an unknown extension should not be accepted merely because its MIME type says `image/png`.

For ambiguous or unrecognized content, choose an explicit policy: reject by default for high-risk tenants, or route to manual review. Do not mark a file clean until all required scanners finish. Keep rejected bytes in quarantine only for a short, controlled retention period, or delete them immediately according to compliance requirements.

A useful result model is:

```json
{
  "decision": "reject",
  "reason_code": "EXECUTABLE_SIGNATURE",
  "detected_type": "application/vnd.microsoft.portable-executable",
  "scanner_version": "content-policy-3"
}
```

## Audit logs

Use an append-only audit table or dedicated log stream. Each event should include:

- event ID and timestamp;
- actor/service identity;
- tenant and file ID;
- action: upload, scan started, scan completed, rejected, downloaded, deleted;
- result and reason code;
- request ID, IP/device context where appropriate;
- policy and scanner versions.

Do not put file contents or secrets in the audit log. Make events idempotent and durable through an outbox or transactional event write. Restrict audit-log access and retain it independently from object deletion.

## Tests

### API and lifecycle

- successful upload transitions from `quarantined` to `clean`;
- download is denied while quarantined or scanning;
- rejected files cannot be downloaded;
- unauthorized users cannot read metadata or content;
- size, timeout, malformed multipart, and storage-failure behavior;
- retrying scan completion does not duplicate audit events;
- object-store and database failures leave recoverable state.

### Content-policy cases

- valid PDF/image/text files are accepted;
- PE renamed to `.jpg`, `.pdf`, or `.txt`;
- ELF and Mach-O binaries with benign extensions;
- executable MIME type paired with a harmless extension;
- shell, Python, PowerShell, and other shebang scripts;
- Java class and WebAssembly samples;
- ZIP/TAR containing an executable under a misleading nested name;
- archives with nested archives;
- encrypted, malformed, truncated, decompression-bomb, and path-traversal archives;
- polyglot files where one interpretation is executable;
- files whose magic bytes disagree with the declared MIME type;
- large files streamed without loading the entire object into memory.

### Security and durability

- checksum is stable and verified after storage;
- generated storage keys prevent path traversal;
- downloaded names are sanitized;
- `Content-Disposition` and `nosniff` headers are correct;
- audit events contain actor, file, result, and policy version;
- quarantine objects are cleaned up after rejection or timeout;
- scanner-policy upgrades can rescan existing files and revoke download eligibility.

The key invariant is: **only an object that has passed byte-level inspection and all required scanners may be promoted to durable downloadable storage.**