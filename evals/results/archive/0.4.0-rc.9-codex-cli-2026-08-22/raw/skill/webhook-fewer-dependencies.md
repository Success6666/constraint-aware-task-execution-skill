## Goal

Implement a webhook receiver that:

- Verifies authenticity before processing.
- Processes each event at most once, even when providers retry.
- Returns responses that encourage correct provider retry behavior.
- Preserves an auditable record of receipt, verification, processing, and failures.
- Uses existing platform capabilities and standard-library cryptography where possible.

## Request Flow

1. Receive `POST /webhooks/{provider}`.
2. Capture the **raw request body** before JSON parsing.
3. Read required headers:
   - Provider event ID or idempotency key
   - Signature
   - Timestamp, if supported
4. Validate basic request limits:
   - Maximum body size
   - Content type
   - Required headers
5. Verify the signature against the raw body.
6. Reject invalid or stale signatures.
7. Insert the event into durable storage with a unique constraint on `(provider, event_id)`.
8. Write an audit record for receipt and verification.
9. Enqueue processing, or mark the event as ready for a worker.
10. Return success only after the event and enqueue intent are durable.

The synchronous handler should not perform business processing. This keeps response time predictable and lets the provider retry safely when infrastructure is unavailable.

## Signature Verification

Use the provider’s documented signing scheme, preferably HMAC-SHA256.

Verification should include:

- Compute the expected signature with a constant-time comparison.
- Include the timestamp in the signed payload when the provider specifies it.
- Reject timestamps outside a small tolerance window, such as five minutes.
- Support active and previous secrets during rotation.
- Never log the secret, raw authorization header, or full sensitive payload.

Example canonical input:

```text
timestamp + "." + raw_body
```

The exact format must follow the provider contract. Verify before deserializing or trusting fields from the payload.

## Data Model

Use the existing relational database if available.

### `webhook_events`

- `id` — internal primary key
- `provider`
- `event_id`
- `event_type`
- `received_at`
- `verified_at`
- `payload` or encrypted/object-storage reference
- `payload_hash`
- `status` — `received`, `queued`, `processing`, `succeeded`, `failed`, `dead_letter`
- `attempt_count`
- `next_attempt_at`
- `processed_at`
- `last_error`
- `locked_at`
- `created_at`, `updated_at`

Add a unique index on:

```text
(provider, event_id)
```

### `webhook_audit_log`

Append-only records containing:

- `webhook_event_id`
- `action` — `received`, `signature_verified`, `signature_rejected`, `deduplicated`, `queued`, `processing_started`, `succeeded`, `retry_scheduled`, `dead_lettered`
- `occurred_at`
- `attempt`
- `actor` — usually `provider` or `worker`
- `metadata` — request ID, status code, error category, latency
- Optional payload hash, but avoid duplicating sensitive payload data

## Idempotency

Treat the provider event ID as the primary idempotency key.

On receipt:

- Attempt an insert with the unique constraint.
- If it already exists:
  - Record a `deduplicated` audit entry.
  - Return `2xx` if the original event is already completed or queued.
  - Do not enqueue a second processing job.

Business-side idempotency is still required for external effects. For example, payment creation or email sending should use an operation key derived from the webhook event ID and enforce uniqueness in the relevant table.

If the provider does not supply an event ID, derive a stable fallback only when the provider guarantees deterministic fields; otherwise require an explicit idempotency strategy rather than hashing arbitrary mutable content.

## Retry Handling

### Receiver response behavior

Return:

- `2xx` after durable persistence and enqueue intent.
- `400` for malformed requests that cannot become valid through retry.
- `401` or `403` for invalid signatures.
- `413` for oversized bodies.
- `429` when intentionally rate limiting.
- `500` or `503` for temporary infrastructure failures before durable persistence.

Do not return `2xx` if the event was accepted only in memory.

### Worker retries

Retry only transient failures:

- Database/network timeouts
- Provider API `408`, `429`, and `5xx`
- Temporary dependency unavailability

Do not retry permanent failures such as schema violations or unsupported event types.

Use bounded exponential backoff with jitter, for example:

```text
delay = min(max_delay, base_delay * 2^attempt) + random_jitter
```

Store retry state in `webhook_events` so retries survive process restarts. After a configurable attempt limit, mark the event `dead_letter` and alert.

Use a lease or row lock when workers claim events:

- Set `locked_at` and `status = processing`.
- Expire stale locks after a timeout.
- Make completion and side-effect markers transactional where possible.

## Audit and Observability

Emit structured logs and metrics with:

- Provider
- Event ID
- Internal event ID
- Request/correlation ID
- Verification result
- Processing status
- Attempt count
- Duration
- Error category

Track:

- Signature rejection rate
- Duplicate event rate
- Queue depth and age
- Processing latency
- Retry and dead-letter counts
- Failure rate by event type

Audit records should be append-only and retained according to compliance requirements. Restrict access to payloads and redact personal or financial data.

## Minimal Dependency Strategy

Prefer:

- Built-in cryptography and constant-time comparison.
- Existing HTTP framework middleware for body limits and request IDs.
- Existing database and transaction layer.
- Existing queue or a database-backed worker table if no queue exists.
- Existing structured logger and metrics system.

Avoid adding a webhook-specific SDK unless it handles a provider protocol that would otherwise be error-prone. Keep provider-specific signature parsing isolated behind a small interface:

```text
verify(requestHeaders, rawBody) -> {
  valid,
  eventId,
  timestamp,
  keyVersion
}
```

## Implementation Phases

1. Define provider contract, event identity, signature format, and retry status rules.
2. Add migrations for `webhook_events`, `webhook_audit_log`, and required unique indexes.
3. Implement raw-body capture, size limits, signature verification, secret rotation, and rejection responses.
4. Implement transactional event insertion and duplicate detection.
5. Add enqueue/worker processing with leases, retry classification, backoff, and dead-letter handling.
6. Add business-operation idempotency around every external side effect.
7. Add structured audit events, metrics, dashboards, and alerts.
8. Secure secrets, restrict payload access, and document retention/redaction rules.

## Verification Plan

Test at minimum:

- Valid signature and valid timestamp.
- Invalid signature, malformed signature, stale timestamp, and rotated secret.
- Duplicate deliveries before, during, and after processing.
- Crash after database insert but before enqueue.
- Crash during processing and expired worker leases.
- Transient versus permanent failures.
- Provider retry behavior for each response class.
- Concurrent deliveries of the same event.
- Oversized and malformed payloads.
- Audit records for every state transition.
- External side effects executed exactly once under repeated delivery.