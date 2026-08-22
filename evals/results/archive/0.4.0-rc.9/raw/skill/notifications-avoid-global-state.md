A practical design is an asynchronous, multi-tenant service built around durable notification records, provider adapters, and queue-driven delivery workers.

## Architecture

```text
Client
  |
  v
Notification API ---> PostgreSQL
  |                      |
  | transactional        | notification, attempt,
  | outbox               | routing, tenant config
  v                      |
Outbox Relay ------------+
  |
  v
Delivery Queue ---> Delivery Workers ---> Email/SMS/Push Providers
                          |
                          +-- retry queue / dead-letter queue
                          +-- status events
                          +-- metrics
```

PostgreSQL is the source of truth. Queues distribute work but do not own delivery state.

## Core Model

`notification`

- `id`
- `tenant_id`
- `idempotency_key`
- `channel`: `email | sms | push`
- `recipient`
- `template_id`
- `payload`
- `status`: `accepted | queued | delivering | delivered | failed | cancelled`
- `scheduled_at`
- `created_at`, `updated_at`
- Unique constraint on `(tenant_id, idempotency_key)`

`delivery_attempt`

- `id`
- `notification_id`
- `attempt_number`
- `provider`
- `provider_message_id`
- `status`: `started | accepted | delivered | retryable_failure | permanent_failure`
- `error_code`, `error_message`
- `started_at`, `finished_at`
- Unique constraint on `(notification_id, attempt_number)`

`tenant_provider_config`

- `tenant_id`
- `channel`
- `provider`
- `priority`
- `weight`
- `enabled`
- `credentials_reference`
- `rate_limit`
- `routing_rules`

`outbox_event`

- `id`
- `aggregate_id`
- `event_type`
- `payload`
- `published_at`

Every query and uniqueness constraint includes tenant scope where relevant. Authentication resolves the caller to a trusted `tenant_id`; request bodies cannot override it.

## API

```http
POST /v1/notifications
Idempotency-Key: order-482-shipped
```

```json
{
  "channel": "email",
  "recipient": "user@example.com",
  "templateId": "order-shipped",
  "payload": {
    "orderId": "482"
  },
  "scheduledAt": null
}
```

Response:

```json
{
  "id": "ntf_123",
  "status": "accepted"
}
```

Additional endpoints:

```http
GET  /v1/notifications/{id}
GET  /v1/notifications/{id}/attempts
POST /v1/notifications/{id}/cancel
POST /v1/providers/{provider}/webhook
```

Creation inserts the notification and outbox event in one database transaction. Repeated idempotency keys return the original notification.

## Provider Routing

Each channel exposes a small adapter contract:

```ts
interface NotificationProvider {
  readonly name: string;
  readonly channel: Channel;

  send(request: ProviderRequest): Promise<ProviderResult>;
  parseWebhook(request: WebhookRequest): Promise<DeliveryEvent>;
}
```

The router receives immutable tenant configuration through constructor injection:

```ts
interface ProviderRouter {
  select(
    notification: Notification,
    previousAttempts: readonly DeliveryAttempt[]
  ): Promise<readonly ProviderCandidate[]>;
}
```

Routing order:

1. Remove disabled or unhealthy providers.
2. Apply tenant and channel rules.
3. Enforce provider and tenant rate limits.
4. Rank by priority, weight, cost, or region.
5. Exclude providers that already returned a permanent recipient failure.
6. Prefer another eligible provider after a provider-specific transient failure.

A circuit breaker may temporarily suppress a failing provider, but its state should live in a shared store such as Redis so all worker instances make consistent decisions.

## Delivery and Retries

Workers claim a queued notification using a database compare-and-set or row lock. Before each network call, they create an attempt record.

Provider results are normalized:

- `accepted`: provider accepted the message; await webhook or reconciliation.
- `retryable_failure`: timeout, throttling, provider outage, HTTP 5xx.
- `permanent_failure`: invalid address, rejected content, unsupported destination.
- `unknown`: request outcome is ambiguous; reconcile before blindly resending.

Use exponential backoff with full jitter:

```text
delay = random(0, min(maxDelay, baseDelay * 2^attempt))
```

Example policy: 30 seconds, 2 minutes, 10 minutes, 1 hour, then 6 hours, with tenant-configurable maximum age and attempt count.

A retry changes providers only when the error is provider-specific. Recipient-invalid errors fail immediately. Exhausted notifications move to `failed` and emit a dead-letter event for operations and optional replay.

Exactly-once external delivery is generally impossible. The service instead provides:

- idempotent notification creation;
- at-least-once queue processing;
- atomic attempt numbering;
- provider idempotency keys where supported;
- reconciliation for ambiguous outcomes;
- idempotent webhook handling keyed by provider event ID.

## Delivery Status

Status updates can arrive from synchronous sends, provider webhooks, or reconciliation jobs. Apply monotonic transitions so delayed events cannot regress a terminal state:

```text
accepted -> queued -> delivering -> delivered
                              \-> failed
accepted/queued -> cancelled
```

Provider webhook events are authenticated, stored before processing, and deduplicated. Unknown provider message IDs are retained for later reconciliation rather than discarded.

Clients may retrieve status through polling or subscribe to tenant-scoped webhooks. Outbound status webhooks use their own retry policy and delivery log.

## Dependency Ownership

Create application dependencies once per process, then pass them explicitly:

```ts
const app = createNotificationApp({
  notificationRepository,
  attemptRepository,
  outboxRepository,
  queue,
  clock,
  idGenerator,
  metrics,
  logger,
  tenantConfigRepository,
  providers: new Map([
    ["sendgrid", sendGridProvider],
    ["ses", sesProvider]
  ])
});
```

Request handlers and workers receive an application context. Tests construct isolated contexts with fake clocks, repositories, queues, and providers. Process-wide constants and immutable configuration are fine; runtime routing, health, credentials, and attempt state belong in injected services or durable stores.

## Metrics

Use bounded-cardinality labels such as tenant plan or region, not notification IDs, recipients, raw tenant IDs, or error messages.

Recommended metrics:

```text
notifications_accepted_total{channel}
delivery_attempts_total{channel,provider,outcome,error_class}
notification_terminal_total{channel,status}
delivery_latency_seconds{channel,provider}
queue_delay_seconds{channel}
queue_depth{channel}
provider_request_duration_seconds{provider,operation}
provider_circuit_state{provider}
webhook_events_total{provider,outcome}
outbox_unpublished_events
dead_letter_notifications_total{channel,reason}
```

Logs should carry structured `tenant_id`, `notification_id`, `attempt_id`, and trace IDs, with recipients and message payloads redacted. Traces should span API acceptance, outbox publication, queue wait, routing, and provider calls.

## Tests

Unit tests:

- routing priority, weighting, disabled providers, and failover;
- error classification and retry schedule;
- maximum attempts and notification age;
- monotonic status transitions;
- webhook authentication and deduplication;
- tenant isolation;
- idempotent creation;
- cancellation races;
- metric label behavior.

Integration tests:

- notification and outbox are committed atomically;
- relay republishes safely after a crash;
- concurrent workers cannot create the same attempt number;
- queue redelivery does not duplicate completed work;
- provider webhook updates the correct attempt;
- credentials and configuration remain tenant-scoped.

Contract tests run every adapter against a shared provider test suite. End-to-end tests use provider sandboxes or local HTTP stubs to cover success, throttling, timeout, delayed webhook, malformed webhook, failover, retry exhaustion, and reconciliation.

The most important failure tests terminate workers immediately before and after the provider call, outbox publication, and status commit. Those boundaries are where duplicate delivery and lost work defects usually appear.