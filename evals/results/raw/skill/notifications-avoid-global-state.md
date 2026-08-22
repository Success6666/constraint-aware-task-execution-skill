# Multi-Tenant Notification Service Design

## 1. Scope

The service accepts notification requests, selects a provider per tenant and channel, delivers asynchronously, retries transient failures, records delivery status, and exposes metrics and status queries.

Supported channels can include:

- Email
- SMS
- Push
- Webhook

Each channel is implemented behind the same provider interface.

---

## 2. Architecture

```text
Client
  |
  v
Notification API
  |
  +--> Idempotency / validation
  +--> Notification database
  +--> Outbox
          |
          v
      Queue / scheduler
          |
          v
   Delivery workers
          |
          +--> Tenant configuration repository
          +--> Routing policy
          +--> Provider adapters
          |
          v
   Provider callbacks/webhooks
```

### Components

1. **API service**
   - Authenticates the caller.
   - Resolves `tenant_id` from authenticated context.
   - Validates requests.
   - Persists notification and delivery records.
   - Publishes an outbox event.

2. **Outbox publisher**
   - Reliably publishes pending delivery jobs to the queue.
   - Marks outbox records as published.
   - Safely retries publishing.

3. **Delivery workers**
   - Consume delivery jobs.
   - Load tenant routing and provider credentials.
   - Select a provider.
   - Execute delivery.
   - Persist status and schedule retries.

4. **Provider adapters**
   - Normalize provider APIs into a common interface.
   - Classify provider responses as success, retryable failure, or permanent failure.
   - Never expose provider-specific behavior to the core service.

5. **Callback handler**
   - Receives asynchronous provider delivery events.
   - Verifies signatures.
   - Updates delivery status idempotently.

6. **Metrics and logging**
   - Emit tenant-safe operational metrics.
   - Use structured logs with correlation identifiers.
   - Never log message bodies or credentials.

---

## 3. Core Interfaces

```typescript
type Channel = "email" | "sms" | "push" | "webhook";

type DeliveryResult =
  | {
      kind: "accepted";
      providerMessageId?: string;
      final: boolean;
    }
  | {
      kind: "retryable_failure";
      code: string;
      message: string;
      retryAfter?: Date;
    }
  | {
      kind: "permanent_failure";
      code: string;
      message: string;
    };

interface NotificationProvider {
  send(request: ProviderRequest): Promise<DeliveryResult>;
  parseCallback(request: CallbackRequest): ProviderCallback | null;
}

interface ProviderRequest {
  tenantId: string;
  notificationId: string;
  deliveryId: string;
  channel: Channel;
  recipient: string;
  subject?: string;
  body: string;
  metadata: Record<string, string>;
}

interface ProviderFactory {
  getProvider(
    tenantId: string,
    channel: Channel,
    providerName: string
  ): Promise<NotificationProvider>;
}
```

Providers are created through injected factories and tenant configuration. Credentials and clients are not stored in process-wide mutable registries.

---

## 4. Tenant Isolation

Every request, database query, queue message, log record, and metric includes a tenant context.

Required rules:

- Derive `tenant_id` from authentication, not from an untrusted request field.
- Apply tenant filtering to all repository methods.
- Use database row-level security or equivalent repository enforcement.
- Encrypt provider credentials at rest.
- Decrypt credentials only inside the provider factory.
- Do not share tenant configuration or provider clients across incompatible credentials.
- Authorize status reads and cancellation operations against the owning tenant.
- Apply per-tenant rate limits, quotas, and concurrency limits.

A worker must verify that the tenant in the queue message matches the tenant associated with the persisted delivery before sending.

---

## 5. Data Model

### `notifications`

| Column | Description |
|---|---|
| `id` | Internal notification ID |
| `tenant_id` | Owning tenant |
| `idempotency_key` | Client-provided deduplication key |
| `channel` | Notification channel |
| `recipient` | Destination |
| `subject` | Optional subject |
| `body` | Message content or encrypted reference |
| `metadata` | Application metadata |
| `status` | Overall notification status |
| `created_at` | Creation time |
| `updated_at` | Last update |

Unique constraint:

```text
(tenant_id, idempotency_key)
```

### `deliveries`

| Column | Description |
|---|---|
| `id` | Delivery ID |
| `notification_id` | Parent notification |
| `tenant_id` | Owning tenant |
| `provider_name` | Selected provider |
| `provider_message_id` | External provider ID |
| `status` | Delivery status |
| `attempt_count` | Number of send attempts |
| `next_attempt_at` | Retry schedule |
| `last_error_code` | Normalized error code |
| `last_error_message` | Sanitized error description |
| `created_at` | Creation time |
| `updated_at` | Last update |

### `outbox_events`

| Column | Description |
|---|---|
| `id` | Event ID |
| `tenant_id` | Tenant |
| `aggregate_id` | Delivery ID |
| `event_type` | For example, `delivery.created` |
| `payload` | Queue payload |
| `published_at` | Publication timestamp |
| `attempt_count` | Publish attempts |
| `next_attempt_at` | Next publish time |

### `provider_configs`

| Column | Description |
|---|---|
| `tenant_id` | Tenant |
| `channel` | Channel |
| `provider_name` | Provider |
| `credentials` | Encrypted credentials |
| `priority` | Routing priority |
| `enabled` | Whether usable |
| `limits` | Provider-specific limits |

---

## 6. API

### Create notification

```http
POST /v1/notifications
Idempotency-Key: client-generated-key
```

```json
{
  "channel": "email",
  "recipient": "user@example.com",
  "subject": "Your receipt",
  "body": "Thank you for your purchase.",
  "metadata": {
    "order_id": "ord_123"
  }
}
```

Response:

```json
{
  "notification_id": "ntf_123",
  "status": "queued",
  "created_at": "2025-01-01T12:00:00Z"
}
```

Repeated requests with the same tenant and idempotency key return the original result without creating another delivery.

### Get notification

```http
GET /v1/notifications/{notification_id}
```

Response includes:

- Notification status
- Delivery status
- Provider name
- Attempt count
- Timestamps
- Sanitized failure information

### Cancel notification

```http
POST /v1/notifications/{notification_id}/cancel
```

Cancellation is allowed only while the delivery is `queued` or `retry_scheduled`. An in-flight provider request may still complete, so cancellation is not advertised as guaranteed after sending begins.

### Provider callback

```http
POST /v1/providers/{provider}/callbacks
```

The handler:

1. Verifies the provider signature.
2. Maps the provider event to a delivery.
3. Applies the event idempotently.
4. Updates status according to the transition rules.

---

## 7. Delivery State Machine

### Delivery statuses

```text
queued
  -> sending
  -> accepted
  -> delivered
  -> failed
  -> retry_scheduled
  -> cancelled
```

Allowed transitions:

- `queued -> sending`
- `sending -> accepted`
- `sending -> delivered`
- `sending -> retry_scheduled`
- `sending -> failed`
- `sending -> cancelled`
- `retry_scheduled -> sending`
- `queued -> cancelled`
- `accepted -> delivered`
- `accepted -> failed`

Provider callbacks must not move a delivery backward. Terminal states are `delivered`, `failed`, and `cancelled`.

The overall notification status is derived:

- `queued`: all deliveries are queued or retrying
- `sending`: at least one delivery is sending
- `delivered`: a delivery is delivered
- `failed`: all possible delivery attempts are terminal failures
- `cancelled`: explicitly cancelled before completion

---

## 8. Provider Routing

Routing is tenant- and channel-specific.

Example policy:

```text
1. Use enabled providers configured for the tenant and channel.
2. Sort by explicit priority.
3. Exclude providers temporarily disabled by health or quota state.
4. Select the first eligible provider.
5. On provider-level outage, optionally fail over to the next provider.
6. Do not fail over after an ambiguous send result unless provider idempotency is supported.
```

Provider selection should be deterministic for a delivery. Persist the selected provider before sending so retries do not unexpectedly switch providers.

Failover is safe only when:

- The original provider definitively rejected the request, or
- The provider supports an idempotency key and the request can be safely replayed.

Use a delivery-scoped idempotency key such as:

```text
{tenant_id}:{delivery_id}
```

---

## 9. Retry Policy

Retries apply only to transient failures, including:

- Network timeouts
- Connection failures
- Provider rate limits
- HTTP 429
- Provider 5xx responses
- Temporary provider-unavailable responses

Do not retry:

- Invalid recipient
- Authentication or configuration errors
- Suppressed or blocked destination
- Invalid message content
- Permanent provider rejection

Default policy:

```text
maximum attempts: 5
initial delay: 30 seconds
maximum delay: 1 hour
backoff: exponential
jitter: full jitter
```

```text
delay = min(max_delay, initial_delay * 2^(attempt - 1))
actual_delay = random(0, delay)
```

Respect provider `Retry-After` values when present, bounded by service limits.

Each retry must atomically:

1. Increment `attempt_count`.
2. Set `status = retry_scheduled`.
3. Set `next_attempt_at`.
4. Record a normalized error.
5. Enqueue or schedule the next job.

Use a lease or row lock while processing to prevent concurrent sends for the same delivery. Expired leases are recoverable by another worker.

---

## 10. Reliability and Idempotency

The service uses at-least-once job delivery, so handlers must be idempotent.

Required safeguards:

- Unique tenant-scoped idempotency key for API requests.
- Unique delivery ID.
- Atomic status transitions with optimistic versioning or row locks.
- Provider idempotency key where supported.
- Callback deduplication using provider event ID.
- Outbox pattern for database-to-queue consistency.
- Dead-letter queue for jobs exceeding processing limits.
- Reconciliation job for deliveries stuck in `sending` or `accepted`.

Ambiguous provider outcomes must be recorded as `accepted` or `unknown` according to provider capabilities and reconciled through callbacks or provider lookup rather than blindly retried.

---

## 11. Metrics

Expose metrics with bounded-cardinality labels.

### Counters

- `notifications_created_total`
- `deliveries_attempted_total`
- `deliveries_succeeded_total`
- `deliveries_failed_total`
- `delivery_retries_total`
- `provider_callbacks_total`
- `provider_errors_total`

### Histograms

- `delivery_latency_seconds`
- `provider_request_latency_seconds`
- `queue_wait_seconds`
- `retry_delay_seconds`

### Gauges

- `queued_deliveries`
- `in_flight_deliveries`
- `dead_lettered_deliveries`
- `provider_health_status`
- `tenant_rate_limit_utilization`

Recommended labels:

```text
channel
provider
result
error_class
```

Use tenant labels only when tenant count and cardinality are controlled. Tenant-specific reporting can instead be stored in an analytics table.

Alerts should cover:

- Increasing provider failure rate
- Queue age above threshold
- Retry volume spikes
- Stuck deliveries
- Callback verification failures
- Dead-letter growth
- Tenant quota exhaustion

---

## 12. Security and Privacy

- Authenticate API callers and authorize tenant membership.
- Validate and normalize recipients per channel.
- Encrypt credentials and sensitive message content at rest.
- Use TLS for all external communication.
- Verify callback signatures and reject replayed events.
- Redact message bodies, credentials, recipient details, and provider tokens from logs.
- Apply content-size limits.
- Support retention and deletion policies per tenant.
- Keep audit records for configuration changes, sends, cancellations, and status changes.

---

## 13. Testing Strategy

### Unit tests

- Request validation.
- Tenant authorization.
- Idempotency behavior.
- Routing priority and provider eligibility.
- Retry classification.
- Backoff and jitter bounds.
- State-transition validity.
- Overall notification status derivation.
- Callback event mapping.
- Provider adapter request/response translation.

### Repository and database tests

- Tenant isolation on reads and writes.
- Unique idempotency constraint.
- Atomic claim/lease behavior.
- Concurrent worker protection.
- Outbox transaction behavior.
- Callback deduplication.
- Optimistic version conflict handling.

### Provider contract tests

For every adapter, verify:

- Correct request construction.
- Credential handling.
- Success mapping.
- Permanent error mapping.
- Retryable error mapping.
- Timeout behavior.
- Callback signature verification.
- Callback status mapping.

### Integration tests

- API request creates notification, delivery, and outbox event.
- Outbox publication creates a worker job.
- Worker successfully delivers through a fake provider.
- Retryable failure schedules a retry.
- Permanent failure becomes terminal.
- Provider callback changes `accepted` to `delivered`.
- Duplicate API requests do not duplicate delivery.
- Duplicate callbacks do not corrupt status.
- Provider failover obeys routing rules.
- Cancellation prevents queued delivery.

### End-to-end tests

Run against a disposable database, queue, and fake provider server:

1. Create a notification.
2. Consume the queued job.
3. Return a provider success.
4. Verify persisted status and metrics.
5. Simulate timeout and retries.
6. Simulate callback completion.
7. Verify tenant A cannot access tenant B data.

### Load and resilience tests

- Sustained per-tenant traffic.
- Queue backlog recovery.
- Provider rate limiting.
- Worker crash during send.
- Database failover.
- Duplicate queue delivery.
- Delayed or out-of-order callbacks.
- Large tenant versus small tenant fairness.

Success criteria should include no cross-tenant data access, no duplicate logical notifications under repeated idempotency keys, bounded retry behavior, and correct terminal status under duplicate or out-of-order events.
