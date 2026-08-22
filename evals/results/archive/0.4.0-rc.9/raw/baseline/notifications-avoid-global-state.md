## Proposed Design

Build the service as stateless workers around durable queues and a relational database. Tenant-specific configuration is loaded through injected repositories and cached in instance-scoped caches. No module-level registries, mutable singletons, or ambient tenant context.

```text
Client -> Notification API -> DB + Outbox -> Queue
                                            |
                                      Delivery Worker
                                      /      |       \
                                  Email     SMS      Push
                                            |
                                      Status Events
                                            |
                                  DB / Webhooks / Metrics
```

### Core Model

```text
Notification
- id: UUID
- tenant_id: UUID
- idempotency_key: string
- channel: email | sms | push
- recipient: encrypted string
- template_id / rendered_content
- status: queued | processing | delivered | failed | canceled
- provider_id: nullable
- attempt_count
- next_attempt_at
- created_at / updated_at

DeliveryAttempt
- id, notification_id, tenant_id
- attempt_number
- provider_id
- provider_message_id
- status: started | accepted | delivered | retryable_failure | permanent_failure
- error_code / sanitized_error
- started_at / completed_at

ProviderConfiguration
- id, tenant_id, channel, provider_type
- encrypted_credentials
- priority, weight, enabled
- routing_rules
- retry_policy
```

Use a unique constraint on `(tenant_id, idempotency_key)` and include `tenant_id` in every primary access path and index. Database authorization or row-level security should provide another tenant-isolation boundary.

### Provider Interface

```ts
interface NotificationProvider {
  readonly id: string;
  readonly channel: Channel;

  send(
    request: ProviderRequest,
    context: DeliveryContext,
  ): Promise<ProviderResult>;

  parseWebhook(
    request: WebhookRequest,
  ): Promise<ProviderStatusEvent>;
}

type ProviderResult =
  | { kind: "accepted"; providerMessageId: string }
  | { kind: "retryable"; code: string; retryAfterMs?: number }
  | { kind: "permanent-failure"; code: string };
```

Construct the application through a composition root:

```ts
const app = createNotificationService({
  notificationRepository,
  attemptRepository,
  outboxRepository,
  queue,
  providerFactory,
  tenantConfigRepository,
  clock,
  idGenerator,
  metrics,
  logger,
});
```

`providerFactory.create(config)` returns provider instances from immutable configuration. Tests inject fakes directly. A bounded cache may live inside `providerFactory`, but it should be owned by the application instance and support invalidation when tenant configuration changes.

### Routing

A `ProviderRouter` receives tenant ID, channel, notification metadata, and prior attempts. It:

1. Loads enabled providers for that tenant and channel.
2. Applies tenant routing rules such as region, message category, cost ceiling, or recipient prefix.
3. Excludes providers that permanently rejected the request or have an open circuit.
4. Selects by priority and weighted distribution.
5. Records the selected provider before sending.

Routing decisions should be deterministic when given an injected random source or routing key. This makes weighted routing testable and prevents retries from switching providers accidentally unless failover policy explicitly permits it.

### Delivery and Retries

Accepting a notification and publishing its queue message must be atomic through a transactional outbox. A dispatcher publishes outbox rows and marks them published idempotently.

Workers claim notifications using a lease or `SELECT ... FOR UPDATE SKIP LOCKED`. Each attempt has a unique key such as `(notification_id, attempt_number)`.

Retry only transient failures:

```text
delay = min(maxDelay, baseDelay * 2^attempt) + boundedJitter
```

Honor provider `Retry-After`, apply per-tenant and per-provider rate limits, and cap both attempts and total retry age. Authentication errors, invalid recipients, rejected content, and malformed requests usually fail permanently. Queue redelivery must be harmless because status transitions and provider-message IDs are persisted idempotently.

### Status Handling

Provider acceptance is not the same as delivery. Webhooks update attempts through a monotonic state machine:

```text
queued -> processing -> accepted -> delivered
                         |           |
                         +---------> failed
```

Store every normalized provider event with a unique `(provider_id, provider_event_id)` constraint. Verify webhook signatures and map provider-specific statuses into internal statuses. Ignore duplicates and prevent late events from regressing terminal states.

Clients can retrieve status through `GET /notifications/{id}` or subscribe to signed tenant webhooks. Client webhook delivery should use its own outbox and retry policy.

### Metrics and Observability

Use bounded labels only:

- `notifications_total{channel,status}`
- `delivery_attempts_total{channel,provider,outcome}`
- `delivery_latency_seconds{channel,provider}`
- `queue_delay_seconds{channel}`
- `provider_requests_in_flight{provider}`
- `outbox_pending`
- `webhook_events_total{provider,outcome}`

Do not put tenant IDs, notification IDs, recipients, or raw error messages in metric labels. Tenant-level usage belongs in queryable billing/analytics events. Logs should carry tenant and notification IDs as structured fields while redacting content, credentials, and recipients. Propagate trace IDs through API, outbox, queue, provider request, and webhook processing.

### Test Strategy

- Unit tests: routing rules, deterministic weighted selection, failover, retry classification, backoff, status transitions, redaction, and provider adapters.
- Contract tests: every provider adapter against shared send/result/webhook behavior.
- Repository tests: tenant isolation, idempotency constraints, leases, concurrent claims, and outbox transactions against the real database.
- Worker tests: duplicate queue delivery, crashes before and after provider calls, retry exhaustion, rate limiting, and stale leases.
- Integration tests: API to outbox to worker to fake provider to webhook.
- Security tests: cross-tenant access, forged webhooks, encrypted credentials, and sensitive-data leakage.
- Load tests: burst traffic, noisy tenants, provider degradation, and backlog recovery.

The unavoidable uncertainty is a crash after a provider accepts a request but before the worker persists the response. Prefer provider-supported idempotency keys derived from the notification ID. Without provider idempotency or reconciliation APIs, exactly-once external delivery cannot be guaranteed; the service should explicitly offer at-least-once processing with duplicate-risk controls.