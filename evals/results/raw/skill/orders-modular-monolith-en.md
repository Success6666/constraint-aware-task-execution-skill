## Architecture

Build one deployable application with four internal business modules, one relational database, and a single composition root.

```text
order-platform/
├── bootstrap/                 # Application startup, dependency wiring, HTTP routes
├── shared-kernel/             # IDs, Money, Clock, transaction/event abstractions
├── catalog/
│   ├── api/                   # Public commands, queries, DTOs, events
│   ├── application/
│   ├── domain/
│   └── infrastructure/
├── ordering/
│   ├── api/
│   ├── application/
│   ├── domain/
│   └── infrastructure/
├── payment/
│   ├── api/
│   ├── application/
│   ├── domain/
│   └── infrastructure/
└── fulfillment/
    ├── api/
    ├── application/
    ├── domain/
    └── infrastructure/
```

Each module exposes only its `api` package. Domain entities, repositories, database mappings, and implementation classes remain private to their module. The composition root may reference every module; modules may reference another module only through its public API.

## Ownership

| Module | Owns | Does not own |
|---|---|---|
| Catalog | Products, variants, descriptions, active status, current prices | Order price snapshots, stock, payments |
| Ordering | Carts, orders, order lines, customer and address snapshots, order lifecycle | Product records, payment attempts, shipments |
| Payment | Payment intents, attempts, provider references, refunds | Order totals, order status, fulfillment |
| Fulfillment | Inventory, reservations, allocations, shipments, tracking | Catalog data, payment authorization |

Use separate database schemas or table prefixes:

```text
catalog.products
catalog.product_prices

ordering.orders
ordering.order_lines

payment.payment_intents
payment.payment_attempts
payment.refunds

fulfillment.inventory
fulfillment.reservations
fulfillment.shipments
```

A module must never read or modify another module’s tables. Cross-module access goes through application APIs, even though all modules share the same process and database.

## Public Module Contracts

```text
CatalogApi
  getPurchasableProducts(productIds)
    -> product ID, variant ID, title, current price, active status

OrderingApi
  placeOrder(command) -> OrderId
  markPaymentAuthorized(orderId, paymentId)
  markPaymentFailed(orderId, reason)
  cancelOrder(orderId, reason)
  getOrderPaymentDetails(orderId)

PaymentApi
  requestPayment(orderId, amount, paymentMethodToken) -> PaymentId
  refundPayment(paymentId, amount) -> RefundId

FulfillmentApi
  reserve(orderId, requestedItems) -> ReservationId
  releaseReservation(orderId)
  beginFulfillment(orderId)
  recordShipment(orderId, trackingDetails)
```

DTOs contain IDs and immutable values, never entities or persistence objects.

## Main Workflow

Order placement is one local database transaction:

```text
1. Ordering receives PlaceOrder.
2. Catalog validates products and returns current prices.
3. Fulfillment locks inventory rows and creates a reservation.
4. Ordering creates the order using product, price, customer, and address snapshots.
5. Ordering records OrderPlaced in the transactional event/outbox table.
6. Commit.
```

Catalog prices are copied into order lines. Later catalog changes therefore cannot alter an existing order.

Payment processing occurs after the order transaction:

```text
OrderPlaced
  -> Payment creates a payment intent
  -> external provider call
  -> Payment records success or failure
  -> Ordering transitions the order
  -> Fulfillment starts work only after payment authorization
```

Do not hold a database transaction open while calling a payment provider. Persist an intent first, perform the external call, then persist the result in a new transaction. Provider requests use the payment ID as their idempotency key.

## Transaction Rules

- One application command defines one transaction boundary.
- Repository methods never commit independently.
- Synchronous module API calls may join the caller’s transaction.
- External network calls always occur outside database transactions.
- Events are stored transactionally with the state change and dispatched only after commit.
- Every event handler is idempotent using an inbox or processed-event record.
- Inventory reservation locks the relevant stock rows or uses optimistic version checks.
- Orders use version columns to prevent conflicting state transitions.
- Unique constraints enforce one active reservation per order and one payment intent per order/payment request key.

The outbox dispatcher is an internal background component in the same application process. Its purpose is reliable post-commit processing, not distributed communication.

## State Models

```text
Order:
DRAFT -> PENDING_PAYMENT -> PAID -> FULFILLING -> SHIPPED -> COMPLETED
                         \-> PAYMENT_FAILED
DRAFT/PENDING_PAYMENT/PAID -> CANCELLED

Payment:
CREATED -> PROCESSING -> AUTHORIZED
                      \-> DECLINED
                      \-> UNKNOWN
AUTHORIZED -> PARTIALLY_REFUNDED -> REFUNDED

Reservation:
ACTIVE -> ALLOCATED
       \-> RELEASED
       \-> EXPIRED
```

Transitions belong to aggregate methods, such as `order.markPaid(...)`, rather than controllers or persistence code.

For ambiguous provider responses, keep the payment in `UNKNOWN` and reconcile it using the provider reference. Never assume failure and retry a charge blindly.

## Failure Handling

- Insufficient inventory aborts the complete order-placement transaction.
- Payment decline marks the order `PAYMENT_FAILED` and releases its reservation.
- Cancellation after authorization requests a refund and prevents fulfillment from starting.
- Shipment creation failure is retried from the outbox without charging again.
- Duplicate HTTP commands use a client-supplied idempotency key stored with the resulting order or payment.
- Expired reservations are released by an internal scheduled job.
- Refund and fulfillment failures remain visible as explicit states for operational retry.

## Tests

**Domain tests**

- Order state-transition rules.
- Price and total calculations.
- Payment and refund invariants.
- Reservation allocation and release behavior.

**Module integration tests**

Run each module against the real database engine:

- Repository mappings and schema constraints.
- Optimistic locking and concurrent stock reservation.
- Transaction rollback when order placement fails.
- Outbox records committed with aggregate changes.
- Idempotent command and event processing.

**Contract tests**

Test every public module API using its real implementation while treating internals as inaccessible. These tests protect DTO semantics and error behavior.

**Architecture tests**

- Only `bootstrap` can access module infrastructure packages.
- Modules cannot import another module’s domain or persistence packages.
- No module accesses tables owned by another module.
- `shared-kernel` contains only stable value types and technical primitives.

**End-to-end tests**

Cover:

1. Successful order, payment, allocation, and shipment.
2. Price changes after order placement.
3. Insufficient stock under concurrent requests.
4. Declined and timed-out payments.
5. Duplicate checkout and payment callbacks.
6. Cancellation before and after authorization.
7. Transaction rollback and outbox retry.
8. Application restart between payment intent creation and provider response.

This structure provides strong module isolation and independent domain ownership while retaining simple in-process calls, local transactions, one database, and one deployment unit.