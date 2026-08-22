Design a stateless, dependency-injected notification service with durable persistence and provider adapters.

### Core components

- **API layer**: accepts notification requests and tenant credentials.
- **Notification service**: validates input, resolves tenant policy, creates delivery records, and enqueues work.
- **Router**: selects providers using tenant/channel configuration, health, priority, and capability.
- **Worker**: claims queued deliveries, invokes providers, records results, and schedules retries.
- **Provider adapters**: uniform interface for email, SMS, push, webhook, etc.
- **Metrics/event publisher**: emits counters, latency, retry, and status events.
- **Persistence**: stores tenants, policies, notifications, attempts, and idempotency keys.
- **Queue**: durable delayed queue for asynchronous delivery.

### Data model

```text
tenants(
  id, name, status, created_at
)

tenant_channels(
  tenant_id, channel, provider, priority, enabled, config_encrypted
)

notifications(
  id, tenant_id, idempotency_key, channel, recipient, payload,
  status, created_at, completed_at
)

delivery_attempts(
  id, notification_id, provider, attempt_number,
  status, error_code, error_message, started_at, completed_at
)

notification_events(
  id, notification_id, tenant_id, type, data, created_at
)
```

Statuses:

```text
queued -> sending -> delivered
                 -> retry_scheduled -> sending
                 -> failed
                 -> canceled
```

Use `(tenant_id, idempotency_key)` as a unique constraint so clients can safely retry submissions.

### Provider interface

```ts
type DeliveryResult =
  | { kind: "delivered"; providerMessageId?: string }
  | { kind: "retryable_error"; code: string; message: string }
  | { kind: "permanent_error"; code: string; message: string };

interface NotificationProvider {
  readonly name: string;
  supports(channel: string): boolean;

  send(input: {
    tenantId: string;
    recipient: string;
    payload: unknown;
    config: unknown;
    idempotencyKey: string;
  }): Promise<DeliveryResult>;
}
```

Providers are registered in an application-scoped registry created at startup:

```ts
class ProviderRegistry {
  constructor(private readonly providers: NotificationProvider[]) {}

  forChannel(channel: string) {
    return this.providers.filter(p => p.supports(channel));
  }
}
```

No mutable module-level registry or singleton state is required.

### Routing

For each tenant and channel:

1. Load enabled provider routes ordered by priority.
2. Filter providers by channel capability.
3. Exclude providers in an open circuit breaker or temporary cooldown.
4. Select the first eligible provider.
5. On a retryable failure, try the next provider when policy allows.
6. Persist the selected provider and every attempt.

Routing policy should be tenant-specific and versioned, for example:

```json
{
  "channel": "email",
  "routes": [
    { "provider": "ses", "priority": 1 },
    { "provider": "sendgrid", "priority": 2 }
  ],
  "maxAttempts": 5,
  "backoff": "exponential"
}
```

### Retry behavior

Classify failures explicitly:

- **Retryable**: timeout, rate limit, connection failure, provider 5xx.
- **Permanent**: invalid recipient, authentication failure, unsupported payload.
- **Unknown**: retry once, then mark failed unless provider guarantees safety.

Use exponential backoff with jitter:

```text
delay = min(maxDelay, baseDelay * 2^(attempt - 1)) + random(0, jitter)
```

Workers must use an atomic claim:

```sql
UPDATE notifications
SET status = 'sending'
WHERE id = :id AND status IN ('queued', 'retry_scheduled')
RETURNING *;
```

This prevents duplicate concurrent sends. Provider calls should include the notification idempotency key whenever supported.

### Delivery status

Persist status transitions and publish events:

```text
notification.queued
notification.sent
notification.delivered
notification.retry_scheduled
notification.failed
notification.canceled
```

Provider webhooks update final states using a signed, tenant-aware endpoint. Webhook handling must be idempotent by provider event ID.

Expose:

```http
POST /v1/notifications
GET  /v1/notifications/{id}
POST /v1/notifications/{id}/cancel
GET  /v1/metrics
```

Every request derives `tenantId` from authenticated claims, never from a client-controlled body field.

### Metrics

Emit counters and histograms tagged with bounded-cardinality dimensions:

```text
notifications_submitted_total{tenant,channel}
notifications_delivered_total{tenant,channel,provider}
notifications_failed_total{tenant,channel,reason}
notification_attempts_total{provider,result}
notification_delivery_latency_seconds{channel,provider}
notification_queue_age_seconds{channel}
```

Avoid recipient IDs, notification IDs, or arbitrary error strings as metric labels. Add tracing with tenant ID and notification ID in span attributes, subject to privacy rules.

### Dependency injection

```ts
class NotificationService {
  constructor(
    private readonly notifications: NotificationRepository,
    private readonly policies: PolicyRepository,
    private readonly queue: DeliveryQueue,
    private readonly clock: Clock
  ) {}

  async submit(cmd: SubmitNotification) {
    const existing = await this.notifications.findByIdempotency(
      cmd.tenantId,
      cmd.idempotencyKey
    );
    if (existing) return existing;

    const policy = await this.policies.forTenantChannel(cmd.tenantId, cmd.channel);
    const notification = await this.notifications.create({
      ...cmd,
      policyVersion: policy.version,
      status: "queued",
      createdAt: this.clock.now()
    });

    await this.queue.enqueue(notification.id);
    return notification;
  }
}
```

Repositories, queue clients, clock, metrics, and provider implementations are passed into constructors. Request-local state stays in method scope; durable state belongs in the database or queue.

### Tests

- Tenant isolation: one tenant cannot read or mutate another tenant’s notifications.
- Idempotent submission with repeated idempotency keys.
- Routing priority and fallback provider selection.
- Provider capability filtering.
- Retry classification and backoff calculation.
- Maximum-attempt enforcement.
- Concurrent worker claiming.
- Duplicate webhook/event handling.
- Status transition validity.
- Cancellation before and during delivery.
- Metrics emitted with approved labels.
- Provider adapter contract tests using fake provider implementations.
- End-to-end test with an in-memory queue and test database.

Use deterministic clocks, injected random/jitter sources, fake repositories, and fake providers so tests remain isolated and parallel-safe.