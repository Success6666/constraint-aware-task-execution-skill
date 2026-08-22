# Order Platform Design

Build one deployable application with one primary database, divided into four modules with strict ownership:

- **Catalog**: products, variants, prices, availability
- **Ordering**: carts, orders, order state, order lines
- **Payment**: payment intents, authorization, capture, refunds, provider webhooks
- **Fulfillment**: shipments, allocation, packing, dispatch, delivery status

Modules communicate through explicit application interfaces and domain events. They must not access one another’s tables or internal classes directly.

## Architecture

Use a modular monolith with:

- One process and deployment
- One PostgreSQL database
- Separate schema or table namespaces per module
- Synchronous calls for required command outcomes
- Transactional outbox events for asynchronous workflows
- Background workers running in the same application
- Provider adapters for payment and shipping integrations

Recommended dependency direction:

```text
Ordering -> Catalog
Ordering -> Payment
Ordering -> Fulfillment

Catalog      -> shared kernel only
Payment      -> shared kernel only
Fulfillment  -> shared kernel only
```

Ordering owns the overall workflow but does not own catalog, payment, or fulfillment state.

The shared kernel should contain only technical primitives and stable value objects such as:

- `Money`
- `Currency`
- `OrderId`
- `CustomerId`
- `CorrelationId`
- Clock and ID-generation interfaces
- Domain event and outbox abstractions

It must not contain business entities or module-specific services.

## Ownership Boundaries

### Catalog

Owns:

- Products
- Variants or SKUs
- Product descriptions
- Product images
- Price lists
- Tax category references
- Saleability and catalog status

Public operations:

```text
getSellableVariant(variantId)
getCurrentPrice(variantId, currency)
validateItems(items)
```

Catalog must return immutable purchase data for order creation:

```text
PurchaseSnapshot {
  variantId
  sku
  name
  unitPrice
  currency
  taxCategory
}
```

Catalog does not know about carts, orders, payments, shipments, or customers.

Price and product changes affect future orders only. Existing order lines retain their snapshots.

### Ordering

Owns:

- Cart
- Cart lines
- Order
- Order lines
- Customer-provided addresses
- Order totals
- Order lifecycle
- Idempotency for order commands

Order states:

```text
DRAFT
PENDING_PAYMENT
PAYMENT_AUTHORIZED
CONFIRMED
FULFILLING
SHIPPED
DELIVERED
CANCELLED
REFUND_PENDING
REFUNDED
```

Ordering is the source of truth for the customer-visible order status.

It stores:

- Product and price snapshots
- Quantity
- Discounts applied
- Tax and shipping totals
- Billing and shipping address snapshots
- Payment reference
- Fulfillment reference

Ordering does not store payment credentials, provider response payloads as business state, inventory ownership, or shipment tracking internals.

### Payment

Owns:

- Payment intents
- Payment attempts
- Provider references
- Authorization and capture state
- Refunds
- Provider webhook processing
- Payment idempotency

Payment states:

```text
CREATED
REQUIRES_ACTION
AUTHORIZED
CAPTURED
FAILED
CANCELLED
REFUND_PENDING
PARTIALLY_REFUNDED
REFUNDED
```

Payment must never accept raw card data. Use provider tokens, hosted fields, or client-side payment methods.

Public operations:

```text
createPaymentIntent(orderId, amount, currency, idempotencyKey)
authorizePayment(paymentId)
capturePayment(paymentId, amount)
cancelAuthorization(paymentId)
refundPayment(paymentId, amount)
```

Payment emits events such as:

```text
PaymentAuthorized
PaymentAuthorizationFailed
PaymentCaptured
PaymentCaptureFailed
PaymentRefunded
```

### Fulfillment

Owns:

- Fulfillment orders
- Fulfillment lines
- Allocation or reservation state
- Shipments
- Carrier references
- Tracking status
- Delivery updates

Fulfillment states:

```text
PENDING
ALLOCATED
PICKING
PACKED
SHIPPED
DELIVERED
CANCELLED
```

Public operations:

```text
createFulfillment(orderSnapshot)
cancelFulfillment(fulfillmentId)
getFulfillmentStatus(fulfillmentId)
```

Fulfillment receives an immutable order snapshot. It does not query Ordering tables to reconstruct the order.

## Database Design

Use separate schemas or strict table prefixes:

```text
catalog.*
ordering.*
payment.*
fulfillment.*
platform_outbox
```

Every module owns its tables and migrations.

Important constraints:

- Monetary values use integer minor units plus ISO currency.
- Quantities use positive integers or a documented decimal type.
- Order lines are immutable after confirmation.
- Order totals are stored and recomputed during creation.
- Foreign keys may reference shared identifiers, but cross-module foreign keys should be avoided when they create ownership coupling.
- Unique constraints enforce idempotency.

Examples:

```text
ordering.orders
- id
- customer_id
- status
- currency
- subtotal_minor
- tax_minor
- shipping_minor
- total_minor
- payment_id
- fulfillment_id
- version
- created_at
- updated_at

ordering.order_lines
- id
- order_id
- variant_id
- sku_snapshot
- name_snapshot
- unit_price_minor
- quantity
- tax_minor
- line_total_minor

payment.payment_intents
- id
- order_id
- amount_minor
- currency
- status
- provider
- provider_payment_id
- version

fulfillment.fulfillments
- id
- order_id
- status
- shipping_address_snapshot
- version

platform_outbox
- id
- aggregate_type
- aggregate_id
- event_type
- payload
- occurred_at
- published_at
- attempt_count
- next_attempt_at
```

Use optimistic locking with a `version` column for aggregates changed by commands or workers.

## Transaction Boundaries

Each command handler owns one database transaction. A transaction may update multiple tables only within the owning module, except for platform infrastructure tables such as the outbox.

### Create Order

1. Validate request and idempotency key.
2. Load cart.
3. Call Catalog to validate and price each item.
4. Create order lines with immutable snapshots.
5. Calculate totals.
6. Create the order in `PENDING_PAYMENT`.
7. Mark the cart checked out.
8. Write `OrderCreated` to the outbox.
9. Commit.

The order and outbox record commit atomically.

Do not hold a database transaction open while calling an external payment provider.

### Start Payment

1. Load order.
2. Verify it is payable.
3. Create a payment intent in `CREATED`.
4. Commit.
5. Call the payment provider.
6. Store the provider result in a separate transaction.
7. Emit the appropriate payment event.

If the provider call times out, leave the payment in a retryable state and reconcile using provider lookup. Do not blindly create a second payment.

### Authorize Payment

Payment owns the external provider interaction.

1. Acquire payment-intent lock or verify version.
2. Confirm the intent is still actionable.
3. Call the provider using a stable provider idempotency key.
4. Persist the result.
5. Emit `PaymentAuthorized` or `PaymentAuthorizationFailed`.
6. Commit.

### Handle Payment Authorized

Ordering consumes `PaymentAuthorized`:

1. Load order.
2. Verify the event matches the expected order and amount.
3. Transition `PENDING_PAYMENT` to `PAYMENT_AUTHORIZED`.
4. Emit `OrderPaymentAuthorized`.
5. Commit.

A separate handler may then request fulfillment creation. The event consumer must be idempotent.

### Create Fulfillment

1. Consume the order/payment-ready event.
2. Ask Fulfillment to create a fulfillment from the order snapshot.
3. Fulfillment persists the fulfillment and emits `FulfillmentCreated`.
4. Ordering receives that event and transitions the order to `CONFIRMED`.

If fulfillment creation fails, the order remains in `PAYMENT_AUTHORIZED` and a compensating workflow can cancel the authorization.

### Capture Payment

Choose one explicit policy:

- Capture immediately after successful authorization, or
- Capture only when fulfillment is ready to ship

For physical goods, capture on fulfillment readiness is usually preferable if supported by the provider. The policy must be represented in configuration and tested.

Capture flow:

1. Fulfillment emits `FulfillmentReadyForCapture`.
2. Payment captures the authorized amount.
3. Payment emits `PaymentCaptured`.
4. Ordering transitions the order to `CONFIRMED` or `FULFILLING`.
5. Fulfillment proceeds to shipment.

### Cancellation

Cancellation is an orchestration command:

1. Ordering validates that the order is cancellable.
2. Ordering requests fulfillment cancellation if fulfillment exists.
3. Payment cancels authorization or creates a refund, depending on payment state.
4. Each module emits its result.
5. Ordering transitions to `CANCELLED` only when required compensations succeed.

The workflow must support partial failure and retries. Never mark an order cancelled merely because a cancellation request was sent.

## Domain Events

Events are internal integration contracts. Use versioned event names and payloads.

Examples:

```text
OrderCreated
PaymentAuthorizationRequested
PaymentAuthorized
PaymentAuthorizationFailed
FulfillmentCreated
FulfillmentReadyForCapture
PaymentCaptured
ShipmentDispatched
OrderDelivered
OrderCancellationRequested
PaymentRefunded
OrderCancelled
```

Events should include:

```text
eventId
eventType
eventVersion
aggregateId
occurredAt
correlationId
causationId
payload
```

Event consumers must be idempotent using an inbox table:

```text
module_inbox
- consumer_name
- event_id
- processed_at
```

Processing an event should:

1. Insert the event ID into the inbox within the consumer transaction.
2. Skip processing if it already exists.
3. Apply the state change.
4. Write resulting outbox events.
5. Commit.

## Outbox and Workers

Every externally visible event is written to the outbox in the same transaction as its state change.

A worker:

1. Claims unpublished or retryable records.
2. Publishes them to the internal event dispatcher.
3. Marks them published.
4. Retries failures with exponential backoff.
5. Moves permanently failing messages to a dead-letter state.
6. Emits metrics and structured logs.

Publishing is at-least-once. Idempotent consumers are mandatory.

External provider calls also require:

- Stable idempotency keys
- Request and response correlation IDs
- Timeout limits
- Retry policy based on error type
- Reconciliation jobs for unknown outcomes

## Public Application APIs

Expose commands and queries through an API layer, but route them into module application services.

Example endpoints:

```text
POST /carts/{cartId}/items
POST /orders
GET  /orders/{orderId}
POST /orders/{orderId}/payments
POST /payments/{paymentId}/authorize
POST /orders/{orderId}/cancel
GET  /orders/{orderId}/fulfillment
POST /webhooks/payments/{provider}
```

Use idempotency keys for:

```text
POST /orders
POST /payments/{id}/authorize
POST /payments/{id}/capture
POST /orders/{id}/cancel
```

Authorization rules must verify that the caller owns the customer or has an administrative role. Provider webhooks must be authenticated and validated against provider signatures.

## State Invariants

Enforce these in application services and domain objects:

- An order cannot be confirmed without successful payment authorization or an explicitly configured payment method.
- An order cannot be fulfilled before confirmation.
- A payment cannot be captured above its authorized amount.
- A refund cannot exceed the captured amount minus previous refunds.
- A terminal order state cannot transition back to an active state.
- Order totals must equal the sum of persisted line, tax, discount, and shipping components.
- Order lines cannot change after payment authorization.
- Fulfillment cannot ship cancelled lines.
- Duplicate webhook delivery cannot change state twice.
- Duplicate command submission returns the original result.
- Currency cannot change after order creation.
- Every payment and fulfillment reference must belong to the same order.

## Error Handling

Classify errors as:

- Validation errors: return `4xx`, no retry
- Authorization errors: return `4xx`, no retry
- Conflict or stale version: return `409`, client retry after refresh
- Provider rejection: persist failure, usually no automatic retry
- Provider timeout or network failure: retry or reconcile
- Internal transient failure: retry with bounded backoff
- Permanent processing failure: dead-letter and alert

Do not expose provider credentials, raw payment details, stack traces, or internal module data in API responses.

## Observability

Every request, command, event, worker job, and provider call should include:

- Correlation ID
- Causation ID where applicable
- Aggregate ID
- Module name
- Operation name
- Duration
- Outcome

Metrics should include:

- Orders by lifecycle state
- Payment authorization and capture success rates
- Fulfillment processing latency
- Outbox backlog
- Event retry counts
- Dead-letter count
- Duplicate command and webhook counts
- Reconciliation discrepancies

## Testing Strategy

### Unit Tests

Test each module’s domain logic without a database:

- Catalog saleability and pricing
- Order total calculation
- Order state transitions
- Payment amount and refund rules
- Fulfillment state transitions
- Cancellation eligibility
- Idempotency behavior
- Invalid transition rejection

### Module Integration Tests

Run against a real or containerized PostgreSQL instance and verify:

- Repository mappings
- Constraints and indexes
- Transaction rollback
- Optimistic locking
- Outbox insertion
- Inbox deduplication
- Module-specific migrations
- Concurrent update behavior

Each module’s tests should assert that it cannot depend on another module’s persistence implementation.

### Contract Tests

Test stable interfaces between modules:

- Catalog returns valid purchase snapshots
- Ordering passes a complete fulfillment snapshot
- Payment events contain order ID, amount, and currency
- Fulfillment events contain fulfillment and order references
- Event versions remain backward compatible

### Workflow Tests

Exercise complete business scenarios:

1. Create order successfully.
2. Payment authorization succeeds.
3. Fulfillment is created.
4. Payment is captured.
5. Shipment is dispatched.
6. Order is delivered.

Also test:

- Invalid product
- Price change during checkout
- Payment decline
- Provider timeout followed by reconciliation
- Duplicate authorization request
- Duplicate webhook
- Fulfillment creation failure
- Capture failure
- Cancellation before authorization
- Cancellation after authorization
- Refund after capture
- Partial refund
- Duplicate event delivery
- Worker crash after external call but before local commit
- Concurrent cancellation and capture

### API Tests

Verify:

- Authentication and authorization
- Request validation
- Idempotency responses
- Correct HTTP status codes
- No leakage of internal state
- Webhook signature validation
- Pagination and filtering for order queries

### Property and Invariant Tests

Generate combinations of quantities, prices, discounts, refunds, and state transitions to prove:

- Totals never become negative unexpectedly
- Refunds never exceed captures
- Invalid state transitions are always rejected
- Replaying any event produces the same final state
- Replaying any idempotent command returns the same result

### Operational Tests

Verify:

- Outbox retry and dead-letter behavior
- Worker restart recovery
- Provider reconciliation
- Database transaction rollback
- Deployment with pending events
- Migration backward compatibility
- Backup and restore
- Graceful shutdown with in-flight work

## Delivery Sequence

Implement in this order:

1. Shared primitives, database transaction abstraction, IDs, money, event envelope, outbox, and inbox.
2. Catalog read and pricing APIs.
3. Ordering carts, order creation, snapshots, totals, and state machine.
4. Payment intents, provider adapter, idempotency, webhooks, and reconciliation.
5. Fulfillment creation, shipment lifecycle, and carrier adapter.
6. Event-driven orchestration and compensation workflows.
7. API authorization, observability, background workers, and operational tooling.
8. Full workflow, failure-mode, concurrency, and migration tests.

The resulting system remains a single deployable modular monolith while preserving clear ownership, transactional correctness, recoverable integration behavior, and a migration path for independently scaling a module later if operational needs justify it.
