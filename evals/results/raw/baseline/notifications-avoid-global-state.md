Design it as an instance-scoped service with durable queues and an outbox, so delivery is reliable and tenant configuration is isolated.

## Architecture

```text
API
  -> NotificationService
      -> TenantRepository
      -> NotificationRepository
      -> OutboxRepository
      -> ProviderRouter
          -> EmailProvider(s)
          -> SMSProvider(s)
          -> PushProvider(s)
      -> RetryPolicy
      -> Clock
      -> Metrics
  -> Outbox Publisher
  -> Delivery Workers
      -> ProviderRouter
      -> StatusUpdater
```

Use dependency injection for every dependency. Keep configuration immutable and passed into service instances; avoid module-level caches, registries, counters, or mutable singletons.

## Core interfaces

```ts
type Channel = "email" | "sms" | "push";

type NotificationStatus =
  | "queued"
  | "sending"
  | "delivered"
  | "failed"
  | "retrying"
  | "cancelled";

interface NotificationRequest {
  tenantId: string;
  idempotencyKey: string;
  channel: Channel;
  recipient: string;
  templateId?: string;
  payload: Record<string, unknown>;
  metadata?: Record<string, string>;
}

interface Provider {
  readonly name: string;
  readonly channel: Channel;

  send(input: {
    tenantId: string;
    recipient: string;
    payload: Record<string, unknown>;
    idempotencyKey: string;
  }): Promise<{
    providerMessageId?: string;
    accepted: boolean;
  }>;
}

interface ProviderRouter {
  select(input: {
    tenantId: string;
    channel: Channel;
    recipient: string;
  }): Promise<Provider>;
}
```

Routing should consider:

- Tenant-specific provider preferences
- Channel support
- Provider health and circuit state
- Cost or priority
- Region/data-residency requirements
- Per-provider and per-tenant rate limits
- Fallback providers

The router should return a deterministic choice for a given attempt and record the selected provider.

## Persistence model

`notifications`

- `id`
- `tenant_id`
- `idempotency_key`
- `channel`
- `recipient`
- `template_id`
- `payload`
- `status`
- `attempt_count`
- `next_attempt_at`
- `provider`
- `provider_message_id`
- `last_error_code`
- `last_error_message`
- `created_at`
- `updated_at`
- `delivered_at`

Constraints:

```text
UNIQUE (tenant_id, idempotency_key)
INDEX (tenant_id, status, next_attempt_at)
```

`notification_events`

- `notification_id`
- `tenant_id`
- `type`
- `provider`
- `provider_message_id`
- `attempt`
- `details`
- `created_at`

`outbox`

- `id`
- `tenant_id`
- `aggregate_id`
- `event_type`
- `payload`
- `published_at`
- `created_at`

Create the notification and outbox record in one database transaction. A publisher later sends the outbox event to the queue.

## Delivery flow

1. API validates the tenant, recipient, channel, template, and payload.
2. Transaction inserts the notification as `queued`.
3. The same transaction inserts a `notification.created` outbox event.
4. Publisher sends the event to a durable queue.
5. Worker atomically claims the notification and changes it to `sending`.
6. Worker selects a provider and calls it with the tenant ID and idempotency key.
7. On acceptance, mark it `delivered` or `queued_for_confirmation`, depending on provider semantics.
8. Webhooks update final status when providers report delivery, bounce, rejection, or complaint.
9. On retryable failure, mark `retrying` and set `next_attempt_at`.
10. On permanent failure or exhausted retries, mark `failed`.

Use compare-and-set updates when claiming work:

```sql
UPDATE notifications
SET status = 'sending',
    attempt_count = attempt_count + 1,
    updated_at = :now
WHERE id = :id
  AND status IN ('queued', 'retrying')
  AND next_attempt_at <= :now;
```

## Retries

Classify errors explicitly:

- Retryable: timeout, connection failure, HTTP 429, HTTP 5xx
- Permanent: invalid recipient, unsupported channel, authentication failure, policy rejection
- Unknown: retry with a bounded policy, then fail

Use exponential backoff with jitter:

```text
delay = min(maxDelay, baseDelay * 2^(attempt - 1)) + random(0, jitter)
```

Example policy:

```text
maxAttempts: 5
baseDelay: 30 seconds
maxDelay: 1 hour
jitter: 20%
```

Honor provider `Retry-After` values. Enforce tenant-level quotas and queue fairness so one tenant cannot starve others.

Provider calls must be idempotent. Persist the provider idempotency key and treat duplicate responses as success when the provider confirms the original request.

## Delivery status

Expose:

```text
GET /v1/notifications/{id}
```

Return current status, attempt count, provider, timestamps, and a sanitized failure reason.

Recommended state transitions:

```text
queued -> sending
sending -> delivered
sending -> retrying
sending -> failed
retrying -> sending
queued/retrying -> cancelled
```

Reject invalid transitions in the repository layer. Store all provider callbacks as events before applying them, making webhook handling replayable and deduplicated.

Webhook handling should verify signatures, identify the tenant from stored provider metadata, and deduplicate by provider event ID.

## Metrics

Record metrics with bounded-cardinality labels:

- `notifications_created_total{channel}`
- `notifications_delivered_total{channel,provider}`
- `notifications_failed_total{channel,reason}`
- `notification_delivery_latency_seconds{channel}`
- `notification_attempts_total{channel,provider}`
- `notification_retries_total{channel,provider}`
- `notification_queue_age_seconds{tenant_tier}`
- `provider_requests_total{provider,channel,result}`
- `provider_request_latency_seconds{provider,channel}`
- `provider_rate_limit_total{provider}`

Avoid tenant IDs, recipient addresses, notification IDs, or arbitrary error strings as metric labels. Put those details in logs/traces instead.

Use structured logs containing `tenantId`, `notificationId`, `attempt`, `provider`, and correlation ID, with recipient data redacted or hashed.

## Tenant isolation and security

- Authorize every request against `tenantId`.
- Encrypt sensitive payloads at rest.
- Store provider credentials in a secrets manager, scoped by tenant/provider.
- Apply per-tenant quotas, rate limits, and concurrency limits.
- Prevent cross-tenant access in repository queries and cache keys.
- Validate webhook ownership and signatures.
- Make retention configurable per tenant and channel.

## Avoiding global mutable state

Use an application composition root:

```ts
const app = createNotificationApp({
  clock: systemClock,
  db,
  queue,
  metrics,
  providers: [sendgridProvider, twilioProvider],
  router: new RoutingProviderRouter({ tenantConfigRepo, healthRepo }),
  retryPolicy: defaultRetryPolicy,
});
```

Each service receives its dependencies through constructors. Health state, rate-limit buckets, and caches should be owned by explicit instances, preferably backed by Redis or another shared store when workers are distributed. Tests can inject fake clocks, repositories, queues, providers, and metrics collectors.

## Tests

Unit tests:

- Routing preference and fallback behavior
- Tenant isolation
- Retry classification and backoff
- State-transition validation
- Idempotency handling
- Webhook signature and deduplication
- Rate-limit enforcement
- Payload/template validation

Integration tests:

- Transactional notification plus outbox creation
- Publisher-to-worker delivery
- Worker claim concurrency
- Provider timeout, 429, 5xx, and permanent errors
- Webhook-to-final-status updates
- Retry exhaustion

Contract tests:

- Provider adapter request/response mapping
- Provider idempotency behavior
- Webhook event mapping

Property-based tests:

- Retry delays are bounded and non-negative
- Invalid state transitions never succeed
- Reprocessing the same event is side-effect free

Operational tests:

- Queue backlog recovery
- Provider outage and failover
- Worker restart during `sending`
- Duplicate queue messages
- Database transaction rollback

This yields at-least-once processing with durable status tracking, deterministic tenant-aware routing, bounded retries, and no reliance on process-global mutable state.