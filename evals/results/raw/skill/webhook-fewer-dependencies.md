# Webhook Receiver Implementation Plan

## 1. Request flow

1. Accept `POST /webhooks/{provider}`.
2. Read and preserve the exact raw request body.
3. Enforce:
   - Maximum body size.
   - Content type.
   - Request timeout.
4. Parse required signature headers.
5. Validate the timestamp against a small clock-skew window, such as ±5 minutes.
6. Verify the signature over the provider-defined signed message, commonly:

   ```text
   timestamp + "." + raw_body
   ```

   Use the standard library’s HMAC and constant-time comparison.
7. Determine the provider event ID from the verified payload.
8. Atomically insert the event into the database using a unique key on:

   ```text
   (provider, event_id)
   ```

9. Record an audit entry for receipt and verification.
10. Return success only after the event is durably stored.
11. Process stored events asynchronously using a database-backed worker.

Do not parse, normalize, reserialize, or modify the body before signature verification.

## 2. HTTP behavior

| Condition | Response |
|---|---:|
| Valid event newly stored | `202 Accepted` |
| Valid duplicate event | `200 OK` or `202 Accepted` |
| Missing or invalid signature | `401 Unauthorized` |
| Invalid timestamp or replay | `401 Unauthorized` |
| Malformed payload after signature verification | `400 Bad Request` |
| Payload too large | `413 Payload Too Large` |
| Temporary storage failure | `500 Internal Server Error` |
| Rate limiting | `429 Too Many Requests` |

Never acknowledge an event with `2xx` before durable persistence.

## 3. Signature verification

Implement a provider-specific verifier interface:

```text
verify(provider, rawBody, headers, currentTime) -> verified event metadata
```

The verifier should:

- Load the active secret from configuration or a secret manager.
- Support current and previous secrets during rotation.
- Reject missing, malformed, expired, or future-dated timestamps.
- Compute the expected MAC using the provider’s required algorithm.
- Compare signatures in constant time.
- Avoid logging secrets, signatures, or full payloads.
- Record which secret version verified the request, without recording the secret itself.

If the provider supplies multiple signatures, accept the request if any currently valid configured secret verifies it.

## 4. Persistence model

A relational database is sufficient; no separate queue is required initially.

### `webhook_events`

```text
id                 internal UUID
provider           string
event_id           string
event_type         string nullable
received_at        timestamp
verified_at        timestamp
payload            encrypted or protected JSON/blob
payload_hash       string
status             pending | processing | succeeded | retryable | failed
attempt_count      integer
next_attempt_at    timestamp nullable
lease_until        timestamp nullable
last_error         text nullable
completed_at       timestamp nullable
created_at         timestamp
updated_at         timestamp
UNIQUE(provider, event_id)
```

### `webhook_audit`

```text
id                 monotonically increasing ID or UUID
event_id           internal event reference nullable
provider           string
action             received | verified | rejected | duplicate |
                   processing_started | processing_succeeded |
                   processing_retry | processing_failed
occurred_at        timestamp
request_id         string
details            structured JSON
```

### Optional `webhook_outbox`

Use this only if processing must publish reliably to another system. Insert the outbox record in the same transaction as the webhook event, then deliver it asynchronously.

## 5. Idempotency

Use the provider’s event ID as the primary idempotency key.

Within one database transaction:

1. Insert the event with `UNIQUE(provider, event_id)`.
2. If the insert succeeds, create its audit record.
3. If the unique constraint conflicts:
   - Do not process the payload again.
   - Record a duplicate audit entry.
   - Return a successful response if the signature was valid.

If a provider has no stable event ID, derive a deterministic identifier from a verified provider identifier or a cryptographic hash of the canonical verified request. Prefer provider IDs whenever available.

Handlers must also be idempotent because a worker can crash after performing an external side effect but before marking the event complete. Use the event ID as an idempotency key for downstream APIs where supported.

## 6. Worker and retry handling

A worker claims pending work using a transaction and a lease:

1. Select an event where:
   - `status = pending`, or
   - `status = retryable` and `next_attempt_at <= now`, or
   - `status = processing` and `lease_until < now`.
2. Atomically set:
   - `status = processing`
   - increment `attempt_count`
   - assign `lease_until`
3. Process the event.
4. Mark it `succeeded` on completion.
5. On a temporary failure, set `retryable` and calculate the next attempt.
6. On a permanent failure or exhausted retry limit, set `failed`.

Use exponential backoff with jitter, for example:

```text
delay = min(max_delay, base_delay * 2^(attempt_count - 1))
delay += random_jitter
```

Classify failures explicitly:

- Retryable: timeouts, connection failures, rate limits, `5xx`, temporary dependency failures.
- Permanent: invalid business data, unsupported event type, authorization failure, schema violation.
- Unknown: retry initially, then move to `failed` after the configured limit.

A failed event must remain inspectable and replayable after correction.

## 7. Audit logging

Record structured, append-only audit events for:

- Request received.
- Signature accepted or rejected.
- Timestamp/replay rejection.
- New event stored.
- Duplicate detected.
- Processing started.
- Processing succeeded.
- Retry scheduled.
- Processing permanently failed.
- Manual replay or administrative action.

Each record should include:

- Internal event ID where available.
- Provider and event ID.
- Request/correlation ID.
- Timestamp.
- Action.
- Attempt number.
- Error category and safe error message.
- Relevant status transition.

Do not include secrets, authorization headers, full signatures, or unredacted sensitive payloads. Retain payloads and audit records according to the applicable retention policy.

## 8. Security and operational controls

- Authenticate only through the provider signature; do not trust caller-supplied event IDs before verification.
- Reject oversized bodies before expensive processing.
- Use TLS.
- Restrict administrative replay and inspection operations.
- Encrypt sensitive payloads at rest where required.
- Apply database indexes on `(provider, event_id)`, `status`, and `next_attempt_at`.
- Emit metrics for:
  - Received events.
  - Verification failures.
  - Duplicates.
  - Processing latency.
  - Retry counts.
  - Permanent failures.
  - Queue age.
- Alert on verification-failure spikes, processing backlog, and repeated permanent failures.

## 9. Verification plan

### Unit tests

- Correct signature is accepted.
- Modified body is rejected.
- Modified timestamp is rejected.
- Expired and future timestamps are rejected.
- Secret rotation accepts old and new configured secrets.
- Missing and malformed headers are rejected.
- Constant-time comparison path is used.
- Duplicate event insertion is safely handled.

### Integration tests

- Valid request is persisted before `2xx`.
- Storage failure returns `5xx`.
- Concurrent deliveries create one event and one processing execution.
- Worker lease recovery handles crashes.
- Retryable failures follow backoff.
- Permanent failures stop retrying.
- Duplicate deliveries do not repeat downstream side effects.
- Audit entries exist for every state transition.

### End-to-end tests

- Send a provider-compatible signed payload.
- Confirm successful acknowledgment.
- Redeliver the same payload and confirm no duplicate side effect.
- Force dependency failures and confirm retries.
- Replay a failed event after remediation.
- Confirm sensitive values are absent from logs and audit details.
