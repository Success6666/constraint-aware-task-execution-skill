## Implementation Plan

Assume an existing HTTP service and relational database. Use the project’s HTTP framework, standard cryptography library, and database driver. Avoid adding queues, caches, or signature packages unless existing infrastructure requires them.

### 1. Define the Webhook Contract

Create a provider-specific configuration containing:

- Endpoint path and event source
- Signing secret reference
- Signature and timestamp header names
- Signature algorithm, preferably HMAC-SHA256
- Allowed timestamp skew, such as five minutes
- Provider event ID field
- Maximum request-body size

Keep secrets in the existing secret manager or environment configuration. Never store or log them.

### 2. Receive and Authenticate

Implement a thin HTTP handler:

1. Accept only the required method, normally `POST`.
2. Read the raw request bytes with a strict size limit.
3. Extract the timestamp and signature headers.
4. Reject missing or malformed authentication data.
5. Reject timestamps outside the configured skew window.
6. Compute the expected signature over the provider’s exact signed payload, commonly:
   ```text
   timestamp + "." + raw_body
   ```
7. Decode signatures into bytes and compare them using a constant-time function.
8. Parse JSON only after signature verification.
9. Validate the minimum event envelope: provider event ID, type, and payload.

Do not reconstruct or reserialize JSON before verification. Signature checks must use the original request bytes.

Return:

- `401` for missing or invalid signatures
- `400` for malformed authenticated payloads
- `413` for oversized requests
- `2xx` once the event has been durably accepted
- `5xx` only when acceptance failed and the provider should retry

### 3. Add Durable Idempotency

Create a `webhook_events` table with fields such as:

```text
id
source
external_event_id
event_type
payload
payload_hash
status
attempt_count
next_attempt_at
last_error
received_at
processing_started_at
processed_at
created_at
updated_at
```

Add a unique constraint on:

```text
(source, external_event_id)
```

Acceptance flow:

1. Begin a transaction.
2. Insert the verified event with status `pending`.
3. On unique-key conflict, load the existing record.
4. If its payload hash differs, record a security or integrity warning.
5. Commit before returning success.

Duplicate delivery should return the same successful acknowledgment without repeating business effects.

If the provider lacks a stable event ID, derive a documented fallback key from immutable signed fields. This is weaker than a provider-issued identifier and should be monitored for collisions.

### 4. Separate Acceptance From Processing

Process accepted events outside the request handler using the project’s existing background-job mechanism.

If none exists, start with a database-backed worker:

- Claim eligible `pending` or `retry` rows using a short transaction.
- Use row locking such as `FOR UPDATE SKIP LOCKED`, where supported.
- Transition the row to `processing`.
- Commit the claim before executing business logic.
- Record completion or schedule another attempt afterward.

This avoids introducing a message broker while still supporting multiple workers.

Each downstream business operation must also be idempotent. Use the webhook event record ID or external event ID as an idempotency key when writing side effects.

### 5. Implement Retry Handling

Classify processing failures:

- **Retryable:** timeouts, connection failures, rate limits, temporary dependency failures.
- **Permanent:** unsupported event type, invalid domain state, nonrecoverable validation failure.

For retryable failures:

```text
delay = min(base_delay * 2^attempt_count, maximum_delay) + random_jitter
```

Use bounded exponential backoff, for example:

- Base delay: 5 seconds
- Maximum delay: 1 hour
- Maximum attempts: 10

After the maximum attempt count, mark the event `dead_letter`. Provide an operator action to replay it after the underlying issue is corrected.

Recover events left in `processing` beyond a lease timeout. This handles worker crashes without requiring distributed lock infrastructure.

### 6. Add Append-Only Audit Logs

Create a separate `webhook_audit_log` table:

```text
id
webhook_event_id
action
from_status
to_status
attempt_number
http_status
error_code
error_summary
metadata
created_at
```

Record important transitions:

- Request received
- Signature rejected
- Event accepted
- Duplicate detected
- Processing started
- Processing succeeded
- Retry scheduled
- Permanent failure
- Dead-lettered
- Manually replayed

Redact authorization headers, signing secrets, personal data, and sensitive payload fields. Prefer structured error codes over full exception dumps.

Invalid requests may not have a `webhook_event_id`; retain a request correlation ID, source, timestamp, and rejection reason instead.

### 7. Observability and Operations

Add structured logs and metrics for:

- Requests received, accepted, rejected, and duplicated
- Signature failures
- Processing latency and age of oldest pending event
- Retry counts
- Dead-letter count
- Payload mismatch on duplicate IDs

Add alerts for sustained signature failures, growing backlog, and dead-letter events. Provide a small administrative command or protected endpoint to inspect and replay failed events.

### 8. Testing

Implement focused tests at four levels:

- **Signature tests:** valid signature, altered body, wrong secret, malformed encoding, expired timestamp, and constant-time comparison path.
- **HTTP tests:** oversized body, malformed JSON, missing event ID, duplicate delivery, and database failure.
- **Worker tests:** successful processing, retryable failure, permanent failure, backoff calculation, maximum attempts, and abandoned-claim recovery.
- **Concurrency tests:** simultaneous delivery of the same event and multiple workers claiming pending rows.

Use captured provider fixtures with secrets replaced by test values. Add an end-to-end test proving that repeated identical deliveries produce exactly one business side effect.

### 9. Delivery Sequence

1. Add schema migrations and repository methods.
2. Implement raw-body capture and signature verification.
3. Implement transactional event acceptance and duplicate handling.
4. Add the database-backed worker and business-event dispatcher.
5. Add retry scheduling and dead-letter behavior.
6. Add audit records, metrics, and redaction.
7. Add replay tooling and operational documentation.
8. Run concurrency, failure-recovery, and end-to-end tests.

The feature is complete when authenticated events are durably acknowledged, duplicate deliveries cannot repeat business effects, temporary failures retry automatically, terminal failures are recoverable, and every state transition is auditable without exposing secrets.