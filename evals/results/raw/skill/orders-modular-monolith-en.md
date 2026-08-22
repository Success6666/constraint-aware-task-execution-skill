## Modular Monolith Design

Build one deployable application and one database, organized into four business modules with strict ownership boundaries:

```text
order-platform/
  catalog/
    api/
    application/
    domain/
    persistence/
  ordering/
    api/
    application/
    domain/
    persistence/
  payment/
    api/
    application/
    domain/
    persistence/
  fulfillment/
    api/
    application/
    domain/
    persistence/
  shared/
    transaction/
    messaging/
    identity/
```

Each module exposes an application-level API to other modules. Modules must not access another module’s tables, repositories, or domain objects directly.

### Module Ownership

| Module | Owns | Does not own |
|---|---|---|
| Catalog | Products, variants, prices, availability | Orders, payment state, shipment state |
| Ordering | Carts, orders, order lines, order status, pricing snapshot | Product master data, payment authorization, shipment execution |
| Payment | Payment attempts, authorization/capture/refund state, provider references | Order line contents, inventory, shipment state |
| Fulfillment | Reservations, shipments, packages, delivery state | Product prices, payment credentials, order totals |

Use separate database schemas or table prefixes to make ownership enforceable:

```text
catalog.products
catalog.variants
catalog.prices

ordering.orders
ordering.order_lines
ordering.order_status_history

payment.payment_attempts
payment.payment_events

fulfillment.reservations
fulfillment.shipments
fulfillment.shipment_items
```

Foreign keys across schemas should be avoided where they would create ownership leakage. Store external identifiers such as `order_id`, `variant_id`, and `customer_id`, and validate them through module APIs.

## Module APIs

### Catalog

```text
getProduct(productId)
getSellableVariant(variantId)
getCurrentPrice(variantId)
checkAvailability(variantId, quantity)
```

Catalog returns immutable snapshots for ordering:

```text
ProductSnapshot {
  variantId
  sku
  name
  unitPrice
  currency
  taxCategory
}
```

Ordering stores this snapshot on each order line so later catalog changes cannot alter historical orders.

### Ordering

```text
createCart(customerId)
addItem(cartId, variantId, quantity)
placeOrder(cartId, paymentMethod)
getOrder(orderId)
cancelOrder(orderId)
```

Ordering is the owner of the order lifecycle:

```text
Draft -> PendingPayment -> Paid -> Preparing -> Shipped -> Completed
                     \-> PaymentFailed
Paid -> Cancelled
```

Only the ordering module may transition order status. Other modules report facts through commands or internal events.

### Payment

```text
authorizePayment(orderId, amount, currency, paymentMethodToken)
capturePayment(paymentAttemptId)
voidAuthorization(paymentAttemptId)
refundPayment(orderId, amount)
handleProviderWebhook(payload)
```

Payment stores provider tokens and references, never raw card data.

### Fulfillment

```text
reserveOrder(orderId, lines)
releaseReservation(orderId)
createShipment(orderId)
markShipmentDispatched(shipmentId, trackingNumber)
markShipmentDelivered(shipmentId)
```

Fulfillment creates shipment items from an order-line snapshot supplied by Ordering. It does not query Ordering tables directly.

## Transactions

Use local ACID transactions inside each module. A transaction may span multiple module-owned tables only when coordinated by an application service in the same process and database.

### Place Order

1. Ordering starts a transaction.
2. Read the cart and lock it.
3. Ask Catalog for current sellable variants and prices.
4. Validate quantities and availability.
5. Create the order and immutable order-line snapshots.
6. Set status to `PendingPayment`.
7. Write an `OrderPlaced` outbox message.
8. Commit.

The payment provider call must not occur inside this database transaction.

### Payment Authorization

1. Payment consumes `OrderPlaced`.
2. Create a payment attempt with `Pending` state.
3. Call the provider using an idempotency key derived from `orderId` and attempt number.
4. In a short transaction, persist the provider result.
5. Publish `PaymentAuthorized` or `PaymentFailed` through the outbox.
6. Ordering consumes the result and transitions the order.

### Reservation and Fulfillment

After payment authorization:

1. Ordering emits `OrderPaid`.
2. Fulfillment creates reservations in a transaction using `(order_id, variant_id)` as a uniqueness key.
3. If reservation succeeds, emit `InventoryReserved`.
4. Ordering transitions to `Preparing`.
5. Fulfillment creates the shipment and emits `ShipmentCreated`.

If reservation fails, emit `InventoryReservationFailed`; Ordering transitions the order to a compensating state and requests payment void/refund.

## Internal Messaging

Use in-process commands/events rather than network calls:

```text
Command: AuthorizePayment
Event: OrderPlaced
Event: PaymentAuthorized
Event: PaymentFailed
Event: OrderPaid
Event: InventoryReserved
Event: InventoryReservationFailed
Event: ShipmentDispatched
Event: ShipmentDelivered
```

Events are integration facts, not shared domain models. Each event has a version and stable identifiers:

```json
{
  "eventId": "uuid",
  "type": "PaymentAuthorized",
  "version": 1,
  "occurredAt": "timestamp",
  "orderId": "uuid",
  "paymentAttemptId": "uuid",
  "amount": 12500,
  "currency": "USD"
}
```

Implement an outbox table per module or a shared outbox with module ownership metadata. Consumers maintain an inbox table keyed by `event_id` to guarantee idempotent processing.

## Consistency Rules

- Order totals are calculated and owned by Ordering.
- Catalog prices are copied into order lines at placement time.
- Payment amount must equal the order’s authorized payable total.
- A payment webhook may be delivered repeatedly; processing must be idempotent.
- A reservation may be created only once for an order line.
- Shipment creation requires successful payment and reservation.
- Order cancellation must trigger payment void/refund and reservation release.
- Every state transition is recorded in a status-history table.
- Cross-module references are IDs, never object references or direct repository calls.

## Testing Strategy

### Unit Tests

Each domain module tests its own invariants in isolation:

- Catalog rejects inactive variants and invalid prices.
- Ordering calculates totals, taxes, discounts, and rounding correctly.
- Ordering rejects placing an empty or stale cart.
- Payment disallows capture before authorization.
- Payment prevents duplicate webhook effects.
- Fulfillment prevents over-reservation.
- Fulfillment disallows shipment creation without reservation.

### Application Tests

Use a real database or containerized database for transaction behavior:

- Placing an order persists line snapshots and an outbox event atomically.
- Failed order creation rolls back all ordering changes.
- Concurrent checkout cannot oversell inventory.
- Reprocessing an outbox event does not duplicate payments, reservations, or shipments.
- Payment success transitions the order exactly once.
- Payment failure leaves no fulfillment reservation.

### Contract Tests

Verify module APIs and event schemas:

- Ordering can consume the current `PaymentAuthorized` event.
- Payment rejects malformed authorization commands.
- Fulfillment handles event versioning and unknown optional fields.
- Catalog snapshot responses contain all fields required by Ordering.

### End-to-End Tests

Cover the primary workflows:

1. Add product to cart, place order, authorize payment, reserve inventory, create shipment.
2. Payment failure and retry.
3. Inventory shortage causing compensation.
4. Duplicate payment webhook delivery.
5. Cancellation before capture.
6. Refund after shipment failure.

## Operational Structure

Keep one deployment and one process, but expose module metrics separately:

```text
orders.placed
payments.authorization_success
payments.authorization_failure
fulfillment.reservation_failure
shipments.dispatched
```

Use module-scoped logging fields such as `module`, `order_id`, `payment_attempt_id`, and `shipment_id`. This preserves independent ownership and testability while keeping deployment, transactions, and local communication simple.