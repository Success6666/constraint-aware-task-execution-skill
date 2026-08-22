# Modular Monolith Design

Build one deployable application and one database, organized into four modules with enforced dependency and ownership boundaries:

```text
Catalog
  ↑ queried by
Ordering
  ↔ Payment
  ↔ Fulfillment
```

Modules communicate through public application interfaces and internal domain events. They must not access another module’s tables or entities directly.

## Module ownership

### Catalog

Owns:

- Products
- Product variants/SKUs
- Product descriptions and attributes
- Active/inactive status
- Sellable price
- Tax category
- Catalog-level availability metadata

Does not own:

- Orders
- Order line items
- Payments
- Inventory reservations
- Shipment state

Public operations:

```text
getSellableItem(sku) -> SellableItemSnapshot
getCatalogItems(query) -> Page<CatalogItem>
changePrice(sku, price)
activateItem(sku)
deactivateItem(sku)
```

`SellableItemSnapshot` includes:

```text
sku
name
unitPrice
currency
taxCategory
catalogVersion
```

Ordering copies this information into order lines. Historical orders must never depend on future catalog changes.

---

### Ordering

Owns:

- Carts, if carts are supported
- Orders
- Order lines
- Customer and shipping snapshots
- Order totals
- Order state
- Idempotency keys for order commands

Example order states:

```text
PENDING_RESERVATION
PENDING_PAYMENT
CONFIRMED
FULFILLING
SHIPPED
COMPLETED
CANCELLED
PAYMENT_FAILED
RESERVATION_FAILED
```

Ordering is the customer-facing workflow owner. It does not write payment or fulfillment records.

Public operations:

```text
placeOrder(command, idempotencyKey) -> OrderResult
getOrder(orderId) -> OrderView
cancelOrder(orderId) -> void
```

`placeOrder`:

1. Validate the request.
2. Ask Catalog for current sellable item snapshots.
3. Calculate totals using the returned snapshots.
4. Create the order and immutable order lines.
5. Store the shipping and customer snapshots.
6. Commit the order in `PENDING_RESERVATION`.
7. Publish `OrderPlaced`.

The order response may return immediately with a pending status. Payment and fulfillment progress through the workflow.

---

### Payment

Owns:

- Payment attempts
- Payment provider references
- Authorization and capture state
- Refunds
- Provider webhook records
- Payment idempotency keys

Example payment states:

```text
PENDING
AUTHORIZED
CAPTURED
FAILED
VOIDED
REFUNDED
```

Public internal operations:

```text
authorizePayment(orderId, amount, currency, idempotencyKey)
capturePayment(orderId)
voidAuthorization(orderId)
refundPayment(orderId, amount)
handleProviderWebhook(payload)
```

Payment owns all interaction with the external payment provider. Other modules receive payment outcomes through events and must not call the provider.

---

### Fulfillment

Owns:

- Inventory quantities
- Inventory reservations
- Reservation expiration
- Shipment records
- Carrier/tracking information
- Fulfillment state

Example reservation states:

```text
PENDING
RESERVED
COMMITTED
RELEASED
EXPIRED
```

Example shipment states:

```text
NOT_READY
READY
SHIPPED
DELIVERED
CANCELLED
```

Public internal operations:

```text
reserveInventory(orderId, lines)
commitReservation(orderId)
releaseReservation(orderId)
createShipment(orderId)
recordShipment(orderId, trackingDetails)
```

Fulfillment is the only module allowed to change inventory or reservation data.

## Database ownership

A shared database is acceptable, but tables are logically module-private.

Example ownership:

```text
catalog_products              Catalog
catalog_prices                Catalog
orders                        Ordering
order_lines                   Ordering
order_idempotency_keys        Ordering
payments                      Payment
payment_attempts              Payment
payment_webhooks              Payment
inventory_items                Fulfillment
inventory_reservations        Fulfillment
shipments                     Fulfillment
outbox_messages               Owning module
inbox_messages                Owning module
```

Rules:

- A module may only write its own tables.
- A module may not import another module’s persistence entities.
- Cross-module reads use a public query interface or event-maintained projection.
- Foreign keys across module-owned tables are avoided.
- Database migrations are grouped by module.
- Domain objects remain inside their owning module.

## Order workflow

### 1. Place order

Ordering transaction:

```text
BEGIN
  insert order
  insert order lines with catalog snapshots
  insert order idempotency record
  insert outbox OrderPlaced event
COMMIT
```

Initial state:

```text
PENDING_RESERVATION
```

If the same idempotency key is retried, return the original order result without creating another order.

### 2. Reserve inventory

Fulfillment consumes `OrderPlaced`.

Fulfillment transaction:

```text
BEGIN
  lock inventory rows for requested SKUs
  verify available quantities
  create reservation
  decrement available quantity
  insert outbox InventoryReserved or InventoryReservationFailed
COMMIT
```

Inventory locking must prevent two concurrent orders from reserving the same units.

If inventory is unavailable, publish `InventoryReservationFailed`. Ordering changes the order to `RESERVATION_FAILED`.

### 3. Authorize payment

Payment consumes `InventoryReserved`.

The external provider call must not occur inside a database transaction:

1. Create or load a payment attempt with a unique idempotency key.
2. Commit `PENDING`.
3. Call the provider using that idempotency key.
4. Store the result in a new transaction.
5. Publish `PaymentAuthorized` or `PaymentFailed`.

On authorization failure:

- Payment publishes `PaymentFailed`.
- Ordering changes the order to `PAYMENT_FAILED`.
- Fulfillment releases the reservation.

On authorization success:

- Payment publishes `PaymentAuthorized`.
- Ordering changes the order to `CONFIRMED`.
- Fulfillment commits the reservation and prepares fulfillment.

### 4. Fulfill

After `OrderConfirmed`:

1. Fulfillment commits the reservation.
2. Fulfillment creates a shipment.
3. Warehouse or carrier updates shipment state.
4. Fulfillment publishes `ShipmentCreated`, `ShipmentShipped`, and `ShipmentDelivered`.

Ordering updates its customer-visible state to `FULFILLING`, `SHIPPED`, and `COMPLETED`.

## Events and reliable delivery

Use transactional outbox processing inside the monolith.

Each module transaction writes its state change and its outgoing event to the same outbox table or module-owned outbox partition:

```text
eventId
eventType
aggregateType
aggregateId
payload
occurredAt
attemptCount
publishedAt
```

A background dispatcher publishes committed outbox records to the in-process event bus.

Consumers must be idempotent. Track consumed event IDs in an inbox table or equivalent consumer record:

```text
consumerName
eventId
processedAt
```

Required events:

```text
OrderPlaced
InventoryReserved
InventoryReservationFailed
PaymentAuthorized
PaymentFailed
ReservationReleased
OrderConfirmed
OrderCancelled
ShipmentCreated
ShipmentShipped
ShipmentDelivered
```

Events should contain facts and identifiers, not references to mutable domain objects.

## Transaction rules

Use local transactions only:

- One command changes one module’s owned state.
- One transaction may update multiple tables only if they belong to the same module.
- Outbox insertion is part of the same transaction as the state change.
- External calls occur outside database transactions.
- Retries use idempotency keys.
- Compensation handles cross-module failure.

There is no distributed transaction between Ordering, Payment, and Fulfillment.

## Consistency and failure handling

### Duplicate commands

Use unique idempotency keys for:

- Place order
- Payment authorization
- Payment capture
- Refund
- Provider webhooks

Repeated commands return the existing result or safely replay the same transition.

### Event retry

Failed event handlers remain retryable. The handler must be safe to execute more than once.

After repeated failures, record the event for operational review without losing it.

### Payment timeout

If the provider response is lost:

- Keep the payment attempt pending.
- Query the provider using the same idempotency key.
- Do not create a second payment attempt.
- Do not release inventory until the payment outcome is known or a timeout policy is applied.

### Cancellation

Cancellation is allowed only in states where fulfillment has not committed the reservation.

Cancellation flow:

1. Ordering validates the state and marks the order cancellation-pending.
2. Payment voids an authorization or refunds a capture.
3. Fulfillment releases the reservation.
4. Ordering marks the order `CANCELLED` after required compensations succeed.

## Internal structure

Each module should contain:

```text
api/
application/
domain/
persistence/
events/
```

Dependencies should point inward:

```text
api -> application -> domain
application -> module-owned persistence
events -> application handlers
```

Persistence implementations must not be exposed outside the module. Cross-module dependencies target interfaces such as:

```text
CatalogReader
PaymentCommandHandler
FulfillmentCommandHandler
```

The application layer coordinates use cases; domain entities enforce local invariants.

## Key invariants

Catalog:

- SKU is unique.
- Only active items can be sold.
- Prices are valid for their currency.

Ordering:

- An order has at least one line.
- Line prices are immutable after order creation.
- Totals equal the sum of stored line snapshots and charges.
- Terminal orders cannot transition to unrelated states.
- An idempotency key maps to only one order.

Payment:

- A payment cannot be authorized twice.
- Capture cannot exceed authorization.
- Refund cannot exceed captured amount.
- A webhook cannot move a payment backward.

Fulfillment:

- Available inventory cannot become negative.
- A reservation cannot exceed available inventory.
- A reservation is released or committed only once.
- A shipment belongs to exactly one order.

## Testing strategy

### Unit tests

Test each module’s domain rules independently:

- Order total calculation and state transitions
- Catalog sellability and pricing rules
- Payment transition rules
- Inventory reservation and release
- Shipment transitions
- Idempotency behavior

### Module application tests

Use the module’s public application API with an in-memory or test database:

- Place an order snapshots catalog data.
- Catalog price changes do not alter existing orders.
- Repeated order commands return the same order.
- Payment retries do not create duplicate provider charges.
- Inventory reservations are released on payment failure.
- Invalid state transitions are rejected.

### Integration tests

Run against the real database engine:

- State and outbox record commit atomically.
- A rollback leaves no order or event.
- Concurrent orders cannot oversell inventory.
- Event consumers safely process duplicate events.
- Outbox dispatch retries failed publication.
- Provider webhook processing is idempotent.

### End-to-end workflow tests

Cover:

1. Successful order:
   ```text
   OrderPlaced
   -> InventoryReserved
   -> PaymentAuthorized
   -> OrderConfirmed
   -> ShipmentCreated
   -> ShipmentDelivered
   ```

2. Inventory failure:
   ```text
   OrderPlaced
   -> InventoryReservationFailed
   -> RESERVATION_FAILED
   ```

3. Payment failure:
   ```text
   OrderPlaced
   -> InventoryReserved
   -> PaymentFailed
   -> ReservationReleased
   -> PAYMENT_FAILED
   ```

4. Provider timeout followed by successful status lookup.

5. Duplicate commands and duplicate events.

6. Cancellation before and after fulfillment commitment.

### Architecture tests

Verify that:

- Ordering cannot import Payment or Fulfillment persistence classes.
- Modules cannot query another module’s tables.
- Only Catalog changes catalog tables.
- Only Ordering changes order tables.
- Only Payment changes payment tables.
- Only Fulfillment changes inventory and shipment tables.
- Cross-module communication uses declared interfaces or events.

This produces one independently deployable modular monolith with clear ownership, local ACID transactions, reliable cross-module workflows, and tests that verify both business behavior and architectural boundaries.
