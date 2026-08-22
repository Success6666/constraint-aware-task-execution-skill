# Notification Service Design

## Goals

Support multi-tenant notification delivery through email, SMS, push, and webhooks with:

- Tenant-specific provider configuration and routing
- Idempotent submission
- Asynchronous delivery
- Provider failover
- Bounded retries with backoff and jitter
- Delivery status tracking
- Provider callbacks and polling where required
- Per-tenant metrics, quotas, and isolation
- Deterministic tests without global mutable state

## High-Level Architecture

```text
Client
  |
  v
Notification API
  |
  +--> Tenant authentication and quota checks
  +--> Idempotency validation
  +--> Notification persistence
  +--> Transactional outbox
             |
             v
        Message broker
             |
             v
       Delivery workers
             |
  +----------+----------+
  |                     |
Provider router     Status updater
  |                     |
  v                     v
Provider adapters   Provider webhooks/pollers
```

Recommended components:

- **API service**: validates and accepts notification requests.
- **Notification database**: durable source of truth.
- **Outbox publisher**: publishes pending work after database commit.
- **Message broker**: distributes delivery attempts.
- **Delivery workers**: perform provider calls and record outcomes.
- **Provider adapters**: normalize provider-specific APIs and errors.
- **Webhook receiver**: processes asynchronous provider status events.
- **Metrics and tracing**: emit tenant- and provider-scoped telemetry.

## Notification API

### Create notification

```http
POST /v1/notifications
Authorization: Bearer <tenant-token>
Idempotency-Key: <client-generated-key>
Content-Type: application/json
```

```json
{
  "channel": "email",
  "recipient": {
    "address": "user@example.com"
  },
  "template": {
    "id": "invoice-ready",
    "version": 3,
    "variables": {
      "invoice_id": "inv_123"
    }
  },
  "routing": {
    "preferred_provider": "provider-a",
    "allow_failover": true
  },
  "metadata": {
    "order_id": "order_456"
  },
  "expires_at": "2026-01-01T00:00:00Z"
}
```

Response:

```json
{
  "id": "ntf_123",
  "tenant_id": "tenant_abc",
  "status": "accepted",
  "created_at": "2025-01-01T12:00:00Z"
}
```

The API returns `202 Accepted`. It does not wait for provider delivery.

### Get status

```http
GET /v1/notifications/{notification_id}
```

```json
{
  "id": "ntf_123",
  "status": "delivered",
  "channel": "email",
  "attempts": 2,
  "provider": "provider-b",
  "created_at": "...",
  "updated_at": "...",
  "delivered_at": "..."
}
```

### Optional operations

```http
POST /v1/notifications/{id}/cancel
GET  /v1/tenants/{tenant_id}/usage
```

Cancellation only prevents future attempts. It cannot reliably retract a provider request already accepted.

## Tenant Isolation

Every request and database operation must carry a validated `tenant_id`.

Use:

- Tenant-scoped authentication claims
- Repository methods requiring `tenant_id`
- Composite keys and indexes beginning with `tenant_id`
- Row-level security where supported
- Tenant-specific quotas and rate limits
- Tenant-specific provider credentials encrypted at rest
- Logs and metrics containing tenant identifiers only where permitted
- No process-wide mutable tenant configuration

Configuration should be provided through injected interfaces:

```text
TenantConfigStore
QuotaStore
ProviderCredentialStore
RoutingPolicyStore
```

Use immutable configuration snapshots during a delivery attempt. Changes affect new attempts, while an existing attempt retains its selected provider unless failover policy explicitly allows re-routing.

## Data Model

### `notifications`

```text
id                 UUID primary key
tenant_id          UUID not null
idempotency_key    VARCHAR not null
channel            VARCHAR not null
recipient          JSON encrypted or tokenized
template_id        VARCHAR nullable
template_version   INTEGER nullable
payload            JSON encrypted or minimized
status             VARCHAR not null
selected_provider  VARCHAR nullable
attempt_count      INTEGER not null default 0
max_attempts       INTEGER not null
expires_at         TIMESTAMP nullable
created_at         TIMESTAMP not null
updated_at         TIMESTAMP not null
delivered_at       TIMESTAMP nullable
last_error_code    VARCHAR nullable
last_error_message VARCHAR nullable
```

Unique constraint:

```text
(tenant_id, idempotency_key)
```

### `delivery_attempts`

```text
id                 UUID primary key
tenant_id          UUID not null
notification_id    UUID not null
attempt_number     INTEGER not null
provider           VARCHAR not null
provider_message_id VARCHAR nullable
status             VARCHAR not null
error_code         VARCHAR nullable
error_class        VARCHAR nullable
started_at         TIMESTAMP not null
completed_at       TIMESTAMP nullable
next_retry_at      TIMESTAMP nullable
```

Unique constraint:

```text
(notification_id, attempt_number)
```

### `outbox_events`

```text
id                 UUID primary key
tenant_id          UUID not null
aggregate_id       UUID not null
event_type         VARCHAR not null
payload            JSON not null
created_at         TIMESTAMP not null
published_at       TIMESTAMP nullable
attempts           INTEGER not null default 0
```

### `provider_events`

```text
provider           VARCHAR not null
provider_event_id  VARCHAR not null
tenant_id          UUID nullable
notification_id    UUID nullable
payload            JSON not null
received_at        TIMESTAMP not null
processed_at       TIMESTAMP nullable
```

Unique constraint:

```text
(provider, provider_event_id)
```

This makes webhook processing idempotent.

## Status State Machine

External statuses:

```text
accepted
queued
sending
delivered
failed
expired
cancelled
```

Internal attempt statuses:

```text
started
succeeded
retryable_failure
permanent_failure
timed_out
```

Allowed notification transitions:

```text
accepted -> queued
queued   -> sending
sending  -> delivered
sending  -> queued
sending  -> failed
sending  -> expired
accepted -> cancelled
queued   -> cancelled
```

Terminal states:

```text
delivered, failed, expired, cancelled
```

Provider callbacks must never downgrade a terminal state. State updates should use conditional database updates or optimistic locking.

## Provider Abstraction

```text
interface NotificationProvider {
    send(context, DeliveryRequest) -> SendResult
    classifyError(error) -> ErrorClassification
    verifyWebhook(request) -> VerifiedEvent
    parseWebhook(event) -> ProviderStatusEvent
}
```

```text
DeliveryRequest {
    tenant_id
    notification_id
    channel
    recipient
    rendered_content
    metadata
}

SendResult {
    provider_message_id
    accepted_at
    initial_status
}
```

Adapters translate provider-specific behavior into stable internal values:

```text
ErrorClassification:
    success
    retryable
    permanent
    rate_limited
    unknown
```

Provider credentials must be resolved per tenant and injected into the adapter call. They must never appear in logs, events, or client responses.

## Routing

A routing policy evaluates:

1. Requested channel
2. Tenant-allowed providers
3. Explicit preferred provider
4. Provider health and circuit state
5. Cost and priority
6. Geographic or regulatory constraints
7. Tenant-specific quotas
8. Failover eligibility

Example policy:

```text
providers:
  email:
    - provider-a priority 10
    - provider-b priority 20
  sms:
    - provider-c priority 10
    - provider-d priority 20
```

Provider selection is recorded in each attempt. A retry normally uses the same provider. Failover occurs only when:

- The error is classified as provider-level retryable
- The routing policy allows failover
- The current provider circuit is open or unavailable
- The notification has remaining attempts

Recipient-invalid and content-invalid errors must not fail over.

## Retry Policy

Use bounded exponential backoff with jitter:

```text
delay = min(max_delay, base_delay * 2^(attempt_number - 1))
delay = random_between(delay * 0.5, delay * 1.5)
```

Example defaults:

```text
base_delay: 30 seconds
max_delay: 1 hour
max_attempts: 5
```

Retry only for:

- Timeouts
- Temporary network failures
- Provider `5xx` responses
- Provider rate limits
- Explicit temporary-unavailable responses

Do not retry:

- Invalid recipient
- Authentication failure
- Invalid credentials
- Rejected content
- Unsupported channel
- Policy or quota violations

Every retry must be represented by a new `delivery_attempts` row. Workers must claim work with a lease or broker visibility timeout so a crashed worker can be recovered.

## Delivery Workflow

1. Validate tenant access, channel, recipient, template, and payload.
2. Check tenant quota and rate limits.
3. Insert notification and outbox event in one transaction.
4. Return the notification ID.
5. Publish the outbox event.
6. Worker loads the notification with tenant scoping.
7. Atomically claim the next attempt.
8. Render content using the stored template version.
9. Select provider using the tenant routing policy.
10. Call the provider with a bounded timeout.
11. Persist provider ID and normalized result.
12. Schedule a retry or mark the notification terminal.
13. Emit status events and metrics.

The outbox publisher must safely support duplicate publication. Consumers must be idempotent.

## Idempotency

For `(tenant_id, idempotency_key)`:

- Same key and equivalent request: return the original notification.
- Same key and different request: return `409 Conflict`.
- Concurrent submissions: rely on the database uniqueness constraint and re-read the existing record after conflict.
- Idempotency records should be retained for a documented period, such as 24 hours or longer than the maximum retry window.

Provider sends can still be duplicated after a worker crash between the provider call and database commit. Mitigate this by:

- Passing a stable notification/attempt id as the provider idempotency key where supported.
- Recording provider message IDs.
- Designing duplicate sends as an explicit residual risk where providers lack idempotency support.

## Webhooks and Asynchronous Status

Webhook endpoint:

```http
POST /v1/providers/{provider}/events
```

Processing:

1. Verify signature and timestamp.
2. Parse the provider event.
3. Deduplicate by `(provider, provider_event_id)`.
4. Resolve the tenant and notification.
5. Validate that the provider message ID matches the recorded attempt.
6. Apply a monotonic status transition.
7. Record processing time and emit metrics.
8. Return success only after durable acceptance.

Unknown or invalid events should be retained for investigation without changing delivery state.

## Reliability Controls

- Per-provider timeout and connection limits
- Circuit breaker per provider and channel
- Per-tenant and global rate limits
- Dead-letter queue for malformed or exhausted messages
- Lease expiration for stuck attempts
- Scheduled reconciliation for provider statuses where supported
- Alerting on queue age, retry spikes, provider failures, and webhook lag
- Clock timestamps generated server-side
- Payload encryption or tokenization for sensitive recipient data
- Audit trail for routing and status changes

At-least-once processing is the default. Exactly-once delivery cannot be guaranteed across an external provider boundary.

## Metrics

Emit counters, gauges, and histograms with bounded-cardinality labels.

Recommended metrics:

```text
notifications_accepted_total{tenant,channel}
notifications_completed_total{tenant,channel,status}
delivery_attempts_total{tenant,channel,provider,result}
delivery_latency_seconds{channel,provider}
retry_scheduled_total{tenant,channel,provider,reason}
provider_request_latency_seconds{provider,operation}
provider_errors_total{provider,error_class}
provider_circuit_state{provider,channel}
queue_depth{channel}
queue_oldest_age_seconds{channel}
webhook_events_total{provider,result}
tenant_quota_rejections_total{tenant,channel}
```

Do not use notification IDs, recipient addresses, or unbounded provider error text as metric labels.

## Testing Strategy

### Unit tests

- Request validation and normalization
- Tenant authorization and isolation
- Idempotency behavior, including concurrent requests
- Routing priority, preferred provider, health, quotas, and failover
- Error classification
- Retry delay calculation and attempt limits
- State transition rules
- Webhook signature verification and event parsing
- Duplicate webhook handling
- Template rendering with stored versions
- Sensitive-data redaction

### Integration tests

- API plus database transaction and outbox creation
- Outbox publishing and duplicate delivery
- Worker lease recovery
- Provider adapter contract tests
- Retry scheduling and dead-letter behavior
- Provider callback updates
- Row-level tenant isolation
- Database uniqueness constraints
- Metrics emission

### End-to-end tests

- Accepted notification becomes delivered
- Primary provider failure routes to fallback
- Permanent failure stops retries
- Rate limiting eventually succeeds
- Duplicate client request returns one notification
- Duplicate provider webhook changes state only once
- Expired notifications are not sent
- Cancellation prevents queued delivery

### Property and resilience tests

- Randomized status events never produce illegal state transitions
- Replaying any event sequence is idempotent
- Worker crashes before and after provider calls
- Broker duplicates messages
- Database deadlocks and transient provider outages
- Time advances deterministically for retry tests

Inject clocks, random-number generators, UUID generators, repositories, broker clients, and provider adapters. Each test constructs its own service dependencies and state container; no global mutable registries are required.

## Verification Criteria

The implementation is ready when:

- A tenant cannot read or mutate another tenant’s notifications.
- Duplicate submissions do not create duplicate logical notifications.
- Provider failures are classified and retried according to policy.
- Failover occurs only for eligible errors.
- Every attempt and status transition is durable and auditable.
- Duplicate broker messages and webhooks are harmless.
- Stuck workers are recoverable.
- Metrics identify tenant, channel, and provider health without high-cardinality labels.
- Tests cover successful delivery, retries, failover, permanent failures, isolation, idempotency, and crash recovery.
