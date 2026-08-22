## Implementation Plan

Assume an HTTP service backed by an existing relational database. Use the platform’s standard HTTP, cryptography, logging, and database libraries; add a dependency only if the current stack cannot safely provide a required primitive.

### 1. Define the webhook contract

Document per provider:

- Endpoint and HTTP method.
- Signature, timestamp, and event ID headers.
- Exact signing format and hashing algorithm.
- Expected response codes and retry behavior.
- Maximum request size and accepted content type.
- Secret rotation procedure.

Preserve the raw request body because signatures must usually be verified against the exact received bytes, before JSON parsing or transformation.

### 2. Implement request authentication

Create a small provider-specific verifier interface:

```text
verify(headers, rawBody, candidateSecrets) -> verification result
```

Verification flow:

1. Reject requests exceeding the configured size limit.
2. Read the raw body once.
3. Parse the signature and signed timestamp.
4. Reject timestamps outside a short configurable tolerance, such as five minutes.
5. Compute the expected HMAC using the standard cryptography library.
6. Compare signatures with a constant-time comparison.
7. During secret rotation, accept the active and immediately previous secret.
8. Parse the payload only after successful verification.

Return a generic `401` or `400`; do not expose expected signatures or secret details.

### 3. Persist receipt and enforce idempotency

Use the provider’s immutable event ID as the idempotency key. Scope it by provider or tenant to avoid collisions.

Suggested table:

```sql
webhook_events
--------------
id
provider
external_event_id
event_type
payload
payload_hash
status              -- received, processing, succeeded, retryable, failed
attempt_count
next_attempt_at
last_error_code
last_error_message
received_at
processing_started_at
completed_at
created_at
updated_at

UNIQUE (provider, external_event_id)
```

Receipt transaction:

1. Verify the signature.
2. Attempt to insert the event with status `received`.
3. If the unique constraint reports a duplicate, return the same successful acknowledgement without processing it again.
4. Commit before acknowledging the request.

The database unique constraint is the authoritative concurrency control. Do not rely on an in-memory “seen events” cache.

If event IDs are not guaranteed, use a documented fallback key derived from stable provider fields. A payload hash alone is risky because two legitimate identical payloads may represent separate events.

### 4. Decouple receipt from processing

Keep the HTTP path short:

```text
Receive -> verify -> persist/deduplicate -> acknowledge
                                  |
                                  v
                         background processor
```

A background worker claims persisted events in batches. With a relational database, this can use transactional row locking such as `FOR UPDATE SKIP LOCKED`; this avoids requiring a message broker for the initial implementation.

Claiming an event should atomically change its state from `received` or due `retryable` to `processing`. Business-side effects and status updates should be designed so a worker crash cannot silently lose an event.

### 5. Make business processing idempotent

Transport deduplication does not prevent duplicate side effects after a crash. Each handler should therefore use a stable operation key, normally the webhook event ID.

Examples:

- Store the event ID on the created or updated domain record.
- Add a unique constraint for event-driven operations.
- Use an idempotency key when calling downstream services that support one.
- Apply state transitions conditionally, such as updating only when the incoming event version is newer.

Wrap local domain changes and the final `succeeded` status update in one database transaction where practical.

### 6. Add retry handling

Classify processing failures:

- **Retryable:** timeouts, temporary database failures, rate limits, and downstream `5xx` responses.
- **Permanent:** invalid payload shape, unsupported event type, missing required domain entity where retry cannot help, and rejected business rules.

For retryable failures:

```text
delay = min(baseDelay * 2^attempt, maxDelay) + random jitter
```

Suggested defaults:

- Base delay: 5 seconds.
- Maximum delay: 15 minutes.
- Maximum attempts: 8.
- Store `attempt_count`, `next_attempt_at`, and a sanitized error summary.

After the final attempt, set status to `failed` and alert through the application’s existing monitoring path. Provide an operator action to replay a failed event; replay must use the same idempotency protections.

Also recover events left in `processing` beyond a lease timeout, since that normally indicates worker termination.

### 7. Add structured audit logs

Record lifecycle events such as:

- Request received.
- Signature accepted or rejected.
- Event stored or identified as duplicate.
- Processing attempt started.
- Processing succeeded.
- Retry scheduled.
- Event permanently failed.
- Manual replay requested.

Each record should include:

```text
timestamp, provider, external_event_id, internal_event_id,
event_type, action, result, attempt, correlation_id, duration_ms
```

Never log secrets, signature values, authorization headers, or full payloads by default. Keep the original payload in access-controlled storage if it is required for debugging or compliance. Audit entries should be append-only from the application’s perspective and follow the existing log retention policy.

### 8. Operational endpoints and metrics

Add narrowly scoped operational visibility:

- Health check for the HTTP service and database.
- Counts of received, duplicate, succeeded, retrying, and failed events.
- Processing latency and queue age.
- Signature rejection rate.
- Oldest pending event.
- Alerting for permanent failures and growing backlog.

Protect any event inspection or replay endpoint with existing administrative authentication and record every replay in the audit log.

### 9. Delivery sequence

1. Implement raw-body capture and provider signature verification.
2. Add the event table and transactional deduplication.
3. Return fast acknowledgements after durable persistence.
4. Add the database-backed worker and handler dispatch by event type.
5. Make each business handler idempotent.
6. Add retry classification, backoff, leases, and terminal failure handling.
7. Add structured audit records, metrics, alerts, and administrative replay.
8. Roll out behind provider-specific configuration, beginning with a test endpoint or sandbox secret.

### 10. Verification

Cover these cases with automated tests:

- Valid and invalid signatures.
- Altered body, missing headers, stale timestamp, and rotated secrets.
- Duplicate requests, including concurrent duplicates.
- Successful acknowledgement only after the receipt transaction commits.
- Worker crash after claiming and after applying a side effect.
- Retryable versus permanent failures.
- Backoff limits and maximum-attempt behavior.
- Recovery of expired processing leases.
- Duplicate replay without duplicate business effects.
- Payloads and secrets absent from logs.
- Unsupported event types handled and audited predictably.

The completion criterion is stronger than “duplicate HTTP requests are ignored”: repeated delivery, concurrent delivery, worker crashes, and manual replay must all produce at most one intended business effect.