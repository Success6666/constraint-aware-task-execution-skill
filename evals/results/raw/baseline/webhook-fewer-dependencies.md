**Implementation Plan**

**1. Define the webhook contract**

- Endpoint: `POST /webhooks/{provider}`.
- Accept only the provider’s documented content type, typically `application/json`.
- Preserve the exact raw request body before parsing.
- Require provider event identifiers, event type, delivery timestamp, and signature headers.
- Enforce a request-size limit and reject unsupported methods or content types.
- Return a stable response body and status code so provider retries behave predictably.

**2. Verify signatures**

Use the provider’s documented scheme, typically:

```text
signature = HMAC-SHA256(secret, timestamp + "." + raw_body)
```

Verification requirements:

- Read the raw body bytes, not re-serialized JSON.
- Parse the signature header using a structured parser.
- Validate the timestamp against a configured tolerance, such as five minutes, to prevent replay.
- Compute the expected digest using the active secret.
- Compare signatures with a constant-time comparison.
- Support one active secret and one previous secret during key rotation.
- Reject missing, malformed, expired, or invalid signatures with `401` or `403`.
- Do not reveal whether a specific signature component or secret was correct.
- Never log the secret, raw authorization header, or sensitive payload fields.

If the provider does not sign timestamps, use an idempotency/replay record keyed by delivery ID and retain it for the provider’s retry horizon.

**3. Make acceptance asynchronous**

The receiver should perform only validation and durable enqueueing synchronously:

1. Capture request metadata and raw body.
2. Validate headers, size, timestamp, and signature.
3. Parse enough JSON to obtain the event ID and event type.
4. Insert the event into durable storage.
5. Commit the transaction.
6. Return `2xx`.

Business processing should run in a worker. This keeps provider requests fast and avoids provider timeouts while downstream work is running.

Return `2xx` only after the event and its audit record are durably committed. A process crash before commit should result in a provider retry.

**4. Implement idempotency**

Use a database uniqueness constraint on the provider and provider event ID:

```text
UNIQUE(provider, event_id)
```

Suggested event fields:

```text
provider
event_id
event_type
received_at
delivery_timestamp
signature_key_version
raw_payload_or_encrypted_payload
payload_hash
status              -- received, processing, processed, failed, dead_lettered
attempt_count
next_attempt_at
processed_at
last_error_code
last_error_message
```

Handling duplicate deliveries:

- If the event already exists and is `processed`, return `200`.
- If it exists in `received`, `processing`, or `failed`, return `200` because the delivery is already durably accepted.
- Do not enqueue a second business operation.
- If insertion races with another request, treat the uniqueness conflict as a duplicate and return `200`.
- Use a separate business-level idempotency key where the provider can emit different event IDs for the same logical operation.
- Make every downstream side effect idempotent as well, using the event ID or a deterministic operation key.

The idempotency record must be retained longer than the provider’s maximum retry period, plus an operational buffer.

**5. Add retry handling**

Use a worker with bounded retries and exponential backoff with jitter:

```text
delay = min(max_delay, base_delay * 2^attempt) + random_jitter
```

Classify failures:

- Transient dependency failures, timeouts, rate limits, and network errors: retry.
- Invalid event data, unsupported event types, authorization failures, and violated business rules: mark permanently failed.
- Repeated failures after the retry limit: move to a dead-letter state or queue.

Recommended defaults:

- Maximum attempts: 8 to 12.
- Maximum retry age: 24 to 72 hours.
- Exponential backoff with jitter.
- A lease or visibility timeout for claimed jobs.
- Lease expiry recovery so a crashed worker does not leave an event permanently stuck in `processing`.

The worker should atomically claim work, process it, and record the result. Do not hold a database transaction open across slow external calls unless required. For external side effects, record an operation key and make retries safe.

**6. Define HTTP response behavior**

| Condition | Response |
|---|---:|
| Valid, newly persisted event | `200` or `202` |
| Valid duplicate event | `200` |
| Invalid signature or timestamp | `401` or `403` |
| Malformed JSON or missing required identity | `400` |
| Unsupported event type, if safely recognized | `200` and audit as ignored |
| Temporary inability to persist or enqueue | `500` or `503` |
| Request too large | `413` |
| Rate limited locally | `429` |

Use `5xx` only when retrying the delivery is useful. Do not return `5xx` for a valid event whose processing later fails; once accepted, retry internally.

**7. Build audit logs**

Create an append-only audit trail for:

- Request received.
- Signature verification result.
- Event accepted or rejected.
- Duplicate detected.
- Processing started.
- Processing succeeded.
- Processing failed and retry scheduled.
- Event dead-lettered or manually replayed.
- Secret/key version used.
- Operator actions and replay reasons.

Each audit entry should include:

```text
audit_id
event_id
provider
action
occurred_at
request_id
worker_id
attempt
result
error_code
metadata
```

Audit requirements:

- Use structured logs with consistent field names.
- Include correlation IDs linking HTTP request, event, worker attempt, and downstream operation.
- Redact tokens, credentials, payment data, personal data, and full payloads unless there is a documented need.
- Prefer storing a payload hash and selected metadata; encrypt retained payloads if raw payload replay is required.
- Make audit records append-only for application users.
- Define retention and access controls separately for operational logs and compliance audit records.
- Ensure failed signature attempts are rate-limited and monitored without logging attacker-controlled sensitive content.

**8. Keep dependencies minimal**

Use existing platform capabilities:

- Standard HTTP server and JSON parser.
- Standard cryptographic library for HMAC and constant-time comparison.
- Existing relational database for uniqueness, transactions, and event state.
- Existing background worker, queue, scheduler, or database-backed polling mechanism.
- Existing structured logging and metrics facilities.

Avoid adding a webhook framework unless the provider’s signing protocol is unusually complex and the framework is already approved. A database-backed outbox or polling worker is sufficient when no queue is available.

**9. Verification strategy**

Unit tests should cover:

- Valid signatures.
- Invalid signatures.
- Modified bodies.
- Missing and malformed headers.
- Expired and future timestamps.
- Constant-time comparison wrapper usage.
- Current and previous key versions.
- Duplicate insertion and concurrent duplicate deliveries.
- All HTTP response classifications.
- Retry backoff and maximum retry age.
- Lease expiry and worker recovery.
- Permanent versus transient error classification.
- Audit redaction.

Integration tests should verify:

- Signature verification uses the exact raw body.
- Event insertion and audit insertion commit atomically.
- A persistence failure produces a retryable HTTP response.
- Duplicate requests create one event and one business operation.
- Worker retries do not duplicate downstream effects.
- Dead-letter transitions and manual replay behavior.
- Key rotation accepts old signatures only during the configured overlap period.

Operational verification should include:

- Metrics for received, rejected, duplicated, processed, retried, dead-lettered, and latency counts.
- Alerts for signature rejection spikes, queue age, retry growth, dead letters, and persistence failures.
- A replay procedure requiring authorization and preserving the original event ID.
- Load testing for provider retry bursts and duplicate deliveries.
- Security review covering secret storage, payload retention, endpoint exposure, rate limiting, and log redaction.

The resulting behavior is at-least-once delivery with durable acceptance, cryptographic authenticity, replay resistance, database-enforced idempotency, controlled internal retries, and traceable audit history.
