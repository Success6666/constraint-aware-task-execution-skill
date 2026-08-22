Design the order platform as a single deployable modular monolith with strict internal module boundaries.

## Module Structure

```text
src/
  catalog/
    api/
    application/
    domain/
    infrastructure/
  ordering/
    api/
    application/
    domain/
    infrastructure/
  payment/
    api/
    application/
    domain/
    infrastructure/
  fulfillment/
    api/
    application/
    domain/
    infrastructure/
  shared/
    kernel/
    events/
    persistence/
```

Each module exposes a small application-facing API. Other modules may depend only on that API and published events, never on internal domain objects, repositories, or tables.

### Catalog

Owns:

- Products, variants, prices, availability
- Product publication state
- Catalog read models

Exposes operations such as:

```text
getSellableProduct(productId)
reserveInventory(items)
releaseInventory(reservationId)
```

Catalog remains the authority for product and inventory data. Ordering stores an immutable snapshot of product name, SKU, unit price, and tax information at checkout.

### Ordering

Owns:

- Orders and order lines
- Order state transitions
- Totals and pricing snapshots
- Customer-facing order history

Typical order states:

```text
PENDING_PAYMENT
PAID
ALLOCATING
READY_FOR_FULFILLMENT
SHIPPED
DELIVERED
CANCELLED
```

Only the ordering module may change order state. Other modules request transitions through commands or events.

### Payment

Owns:

- Payment attempts
- Provider references
- Authorization, capture, refund, and failure state
- Idempotency keys

It does not update orders directly. It emits results such as:

```text
PaymentAuthorized
PaymentFailed
PaymentRefunded
```

Provider adapters are isolated behind an internal interface so payment-provider changes do not leak into ordering.

### Fulfillment

Owns:

- Shipment records
- Warehouse allocation
- Packing and shipping state
- Tracking numbers

It reacts to `OrderPaid` or `ReadyForFulfillment` events and emits:

```text
FulfillmentAllocated
ShipmentCreated
OrderShipped
OrderDelivered
```

## Ownership Boundaries

Use one database instance initially, but enforce logical ownership:

```text
catalog_*       -> catalog
order_*         -> ordering
payment_*       -> payment
fulfillment_*   -> fulfillment
```

Rules:

- A module may write only its own tables.
- Cross-module reads go through module APIs or replicated read models.
- No foreign keys across module-owned tables.
- No shared ORM entities.
- Shared code is limited to technical primitives such as IDs, clocks, money types, event envelopes, and transaction abstractions.
- Domain rules remain inside the owning module.

For example, payment may receive `orderId`, but it must not load or mutate an `Order` entity.

## Transaction Strategy

Use local ACID transactions for changes within a module.

### Checkout

1. Ordering validates the cart through Catalog.
2. Catalog reserves inventory in its transaction.
3. Ordering creates the order and price snapshot in its transaction.
4. Ordering publishes `OrderPlaced`.

If inventory reservation and order creation must be coordinated atomically, use a single application transaction spanning module application services while still preserving write ownership. The transaction may call Catalog’s public command API, but it must not bypass Catalog’s domain logic.

### Payment

Payment processing is asynchronous and idempotent:

1. Consume `OrderPlaced`.
2. Create a payment attempt with a unique `(orderId, idempotencyKey)`.
3. Call the provider.
4. Persist the result and publish `PaymentAuthorized` or `PaymentFailed`.

Use an inbox table for consumed messages and an outbox table for events created during local transactions.

### Fulfillment

1. Consume `PaymentAuthorized`.
2. Allocate stock and create fulfillment work.
3. Persist allocation and publish `FulfillmentAllocated`.
4. Ordering consumes that event and transitions the order to `READY_FOR_FULFILLMENT`.

Retries must be safe. Every handler should tolerate duplicate delivery.

## Event and Failure Handling

Use an in-process event bus behind a stable interface. Events are persisted through the transactional outbox:

```text
business transaction
  -> write module state
  -> write outbox event
commit
  -> dispatcher publishes event
```

Use:

- Inbox records for deduplication
- Retry policies with bounded backoff
- Dead-letter storage for exhausted messages
- Correlation IDs across checkout, payment, and fulfillment
- Explicit compensating actions, such as releasing inventory after payment failure

Avoid distributed transactions and two-phase commit. Cross-module workflows are coordinated with events and compensating commands.

## API Layer

Expose a single HTTP API or GraphQL endpoint, routed internally to module application services:

```text
POST   /orders
GET    /orders/{id}
POST   /orders/{id}/payments
POST   /orders/{id}/cancel
GET    /products/{id}
GET    /orders/{id}/shipment
```

Controllers should perform authentication, validation, and mapping only. Business decisions belong in application and domain layers.

## Testing Strategy

### Unit Tests

Test domain invariants without infrastructure:

- Invalid order state transitions are rejected.
- Order totals use captured prices.
- Payment attempts are idempotent.
- Inventory cannot be over-reserved.
- Shipment cannot be created before payment authorization.

### Module Integration Tests

Run each module against a real or containerized database:

- Repository mappings
- Transaction rollback behavior
- Outbox and inbox persistence
- Public module API contracts
- Provider adapter behavior using fakes or test servers

### Contract Tests

Verify the interfaces between modules:

- Ordering accepts catalog product snapshots.
- Payment publishes events with required identifiers and amounts.
- Fulfillment handles duplicate payment events.
- Event schemas remain backward compatible.

### Workflow Tests

Exercise the complete monolith through the public API:

- Successful checkout through delivery
- Payment failure releases inventory
- Duplicate payment callback does not duplicate capture
- Fulfillment retry eventually creates one shipment
- Order cancellation before and after payment
- Out-of-stock checkout

### Architectural Tests

Automate boundary enforcement:

- Catalog cannot import ordering internals.
- Modules cannot access another module’s repositories or entities.
- Only owning packages reference their table mappings.
- Shared kernel cannot depend on business modules.

## Operational Shape

Deploy one application and one database initially. Scale the whole process horizontally behind a load balancer, using a shared message/outbox dispatcher and a centralized database. Keep module boundaries strong enough that a later extraction is possible, but treat extraction as an optimization—not the default architecture.