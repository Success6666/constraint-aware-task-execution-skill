**Implementation Plan**

1. **Define the webhook contract**
   - Document endpoint, accepted methods/content types, maximum payload size, timeout, and response codes.
   - Standardize headers such as:
     - `X-Webhook-Id` or provider event ID
     - `X-Webhook-Timestamp`
     - `X-Webhook-Signature`
   - Require a unique event ID and a bounded timestamp tolerance, such as five minutes.

2. **Build the request pipeline**

   Process requests in this order:

   1. Enforce method, content type, body-size, and rate limits.
   2. Read the raw request body without parsing or reserializing it.
   3. Validate timestamp freshness.
   4. Verify the signature using a constant-time comparison.
   5. Parse and schema-validate the payload.
   6. Perform an atomic idempotency claim.
   7. Record an audit event.
   8. Enqueue or dispatch the business operation.
   9. Return the provider-appropriate success response.

   Keep signature verification before JSON parsing because formatting changes can invalidate signatures.

3. **Signature verification**
   - Store secrets in the existing secret manager or environment configuration; do not add a new secrets dependency.
   - Compute the expected HMAC over the provider-defined signing string, typically including timestamp, event ID, and raw body.
   - Support key rotation by accepting a current and previous secret during a transition period.
   - Reject missing, malformed, stale, or invalid signatures with a generic `401`/`403` response.
   - Never log secrets, signatures, or full sensitive payloads.

4. **Idempotency**
   - Use the existing database or key-value store rather than introducing a dedicated package.
   - Create a table/key with:
     - provider/event ID as a unique key
     - status: `processing`, `succeeded`, `failed`, or `retryable`
     - payload hash
     - first-seen and completion timestamps
     - attempt count
     - response/result metadata
   - Claim the event with an atomic insert or compare-and-set operation.
   - For an already completed event, return the previously recorded success result.
   - For an event stuck in `processing`, use a lease/timeout so it can be recovered safely.
   - Treat the same ID with a different payload hash as a security/integrity error and alert on it.

5. **Retry and delivery handling**
   - Acknowledge only after durable acceptance. If work is queued, return success once the queue write is committed.
   - Return retryable status codes for transient infrastructure failures, such as `500`, `502`, `503`, or `429`, according to the provider contract.
   - Return non-retryable `4xx` responses for invalid signatures, malformed payloads, unsupported event types, or policy violations.
   - Implement bounded internal retries for downstream calls with exponential backoff and jitter.
   - Add a dead-letter path after the retry limit, preserving the event ID and failure reason.
   - Ensure downstream side effects use the same idempotency key or an outbox pattern.

6. **Audit logging**
   - Emit structured audit records for:
     - received request
     - verification result
     - idempotency decision
     - processing attempts
     - downstream result
     - final outcome or dead-letter transition
   - Include event ID, provider, event type, correlation/request ID, timestamps, attempt count, latency, and outcome.
   - Redact or hash sensitive fields; keep full payloads only if explicitly required and protected.
   - Make audit records append-only where possible and define retention/cleanup rules.

7. **Dependency strategy**
   - Use the platform’s standard HTTP handling, cryptography/HMAC library, JSON parser, database client, queue client, logging, and metrics facilities.
   - Avoid adding webhook-specific frameworks, retry libraries, or idempotency packages unless the existing stack lacks the required primitive.
   - Encapsulate provider-specific signature parsing behind a small adapter so another provider does not affect core processing.

8. **Testing**
   - Unit tests for valid signatures, malformed signatures, timestamp expiry, key rotation, raw-body handling, and constant-time comparison behavior.
   - Idempotency concurrency tests with duplicate requests arriving simultaneously.
   - Retry tests covering transient failures, permanent failures, backoff limits, and dead-lettering.
   - Contract tests for every supported event type and provider response expectation.
   - Security tests for replay attempts, payload tampering, oversized bodies, and event-ID/payload mismatches.
   - End-to-end tests proving that retries cannot duplicate business side effects.

9. **Operational rollout**
   - Add metrics for verification failures, duplicate rate, processing latency, retry counts, dead letters, queue depth, and downstream errors.
   - Add alerts for signature-failure spikes, stuck `processing` records, and dead-letter growth.
   - Roll out behind a feature flag or shadow mode if the provider permits it.
   - Document secret rotation, replay investigation, manual reprocessing, and dead-letter recovery procedures.

**Suggested module boundaries**

- `WebhookController`: HTTP validation and response mapping
- `SignatureVerifier`: provider-specific verification
- `IdempotencyStore`: atomic claim, completion, lease recovery
- `WebhookDispatcher`: queueing and retry policy
- `AuditLogger`: structured, redacted audit events
- `EventHandlers`: business logic per event type

This keeps the receiver small, testable, and dependency-light while making duplicate delivery, replay, and operational recovery explicit.