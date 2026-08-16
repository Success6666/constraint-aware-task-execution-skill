# Order Platform: Modular Monolith Design

Build one deployable application and one relational database. Internally, divide it into four strongly isolated business modules:

```text
Clients
   |
HTTP / Application API
   |
+---------------------------------------------------+
|                 Modular Monolith                  |
|                                                   |
|  Catalog -> Ordering -> Payment -> Fulfillment    |
|                  |           |          |         |
|                  +---- Domain Events ----+         |
|                                                   |
|  Shared technical infrastructure only             |
+---------------------------------------------------+
                       |
                 PostgreSQL
          separate schema per module
```

Modules communicate through explicit application interfaces and domain events. They must never read or write another module’s tables.

## Module Ownership

| Module | Owns | Public operations | Consumes |
|---|---|---|---|
| Catalog | Products, SKUs, current prices, availability policy | Get product, quote items, validate purchasability | None |
| Ordering | Customers’ orders, order lines, totals, order lifecycle | Place, get, cancel order | Payment and fulfillment outcomes |
| Payment | Payment attempts, provider references, refunds | Request payment, refund payment | Order placed/cancelled |
| Fulfillment | Shipments, shipment lines, tracking, fulfillment status | Prepare, ship, deliver | Order paid/cancelled |

Important ownership rules:

- Ordering stores an immutable snapshot of SKU name, unit price, quantity, tax, and discounts at checkout.
- Catalog price changes do not mutate existing orders.
- Payment owns provider-specific identifiers and statuses.
- Ordering knows only the resulting payment state, not provider details.
- Fulfillment owns shipment and tracking data.
- Only Ordering determines the overall order status.
- Cross-module database foreign keys are prohibited. References such as `order_id` are logical identifiers.

## Suggested Structure

```text
src/
  platform/
    catalog/
      domain/
      application/
      infrastructure/
      api/
    ordering/
      domain/
      application/
      infrastructure/
      api/
    payment/
      domain/
      application/
      infrastructure/
      api/
    fulfillment/
      domain/
      application/
      infrastructure/
      api/
    bootstrap/
    shared/
      ids/
      clock/
      transactions/
      events/
```

`shared` contains technical primitives only. Do not place shared business entities, repositories, or a generic “common domain” there.

Each module exposes a narrow facade:

```text
CatalogFacade
  quote(items) -> CatalogQuote

OrderingFacade
  placeOrder(command) -> OrderId
  cancelOrder(orderId, reason)

PaymentFacade
  requestPayment(orderId, amount, paymentMethodToken)
  requestRefund(orderId, amount)

FulfillmentFacade
  prepareShipment(orderId, lines, address)
  markShipped(shipmentId, trackingNumber)
```

Other modules cannot import internal domain classes or repositories. Enforce this with package visibility and architecture tests.

## Data Model

Use one database with a schema per module:

```text
catalog.products
catalog.skus
catalog.prices

ordering.orders
ordering.order_lines
ordering.order_status_history

payment.payment_attempts
payment.refunds
payment.processed_messages

fulfillment.shipments
fulfillment.shipment_lines
fulfillment.processed_messages

platform.outbox_events
```

Representative order fields:

```text
orders
  id
  customer_id
  status
  currency
  subtotal
  tax
  total
  shipping_address_json
  version
  created_at
  updated_at

order_lines
  id
  order_id
  sku_id
  sku_name_snapshot
  unit_price
  quantity
  line_total
```

Use decimal or integer minor units for money, never floating-point values. Add optimistic version columns to aggregates that can receive concurrent updates.

## State Machines

Order:

```text
PENDING_PAYMENT
  -> PAID
  -> FULFILLING
  -> SHIPPED
  -> DELIVERED

PENDING_PAYMENT -> CANCELLED
PAID            -> CANCELLATION_PENDING -> CANCELLED
```

Payment:

```text
REQUESTED -> PROCESSING -> CAPTURED
                       -> DECLINED
                       -> FAILED

CAPTURED -> REFUND_PENDING -> REFUNDED
```

Fulfillment:

```text
PENDING -> PREPARING -> SHIPPED -> DELIVERED
        -> CANCELLED
```

Implement transitions as domain methods. Controllers and persistence code must not assign statuses directly.

## Checkout Transaction

`PlaceOrder` runs in one local database transaction:

1. Validate the command.
2. Ask Catalog for a quote through `CatalogFacade`.
3. Construct the Order aggregate from the returned snapshot.
4. Persist the order and lines in Ordering-owned tables.
5. Insert an `OrderPlaced` event into the outbox.
6. Commit.
7. Return the order ID.

Catalog validation can participate in the same local transaction because this is one application and database. Ordering still accesses it only through the Catalog facade.

Do not call an external payment provider inside this transaction.

## Payment Flow

After commit, the in-process outbox dispatcher handles `OrderPlaced`:

1. Payment creates a payment attempt idempotently.
2. It commits the attempt before contacting the external provider.
3. A worker calls the provider using the attempt ID as the idempotency key.
4. Payment records the provider result and publishes either:
   - `PaymentCaptured`
   - `PaymentDeclined`
   - `PaymentFailed`
5. Ordering consumes that event and changes its own order state.
6. `PaymentCaptured` also causes Fulfillment to create a shipment.

Although dispatch is in-process, the durable outbox prevents committed work from being lost on application restart. This is not a distributed microservice architecture.

## Transaction Rules

- One command modifies one module’s aggregate in one database transaction.
- Cross-module workflows use committed events and compensating actions.
- Event handlers open new transactions.
- Every handler is idempotent using `(handler_name, event_id)` or a unique business key.
- Outbox publication occurs in the same transaction as the originating state change.
- External network calls happen outside database transactions.
- Provider webhooks are deduplicated by provider event ID.
- Use optimistic locking to prevent concurrent cancellation/payment updates from silently overwriting each other.

Example cancellation:

- Unpaid order: Ordering cancels immediately.
- Paid order: Ordering enters `CANCELLATION_PENDING` and emits `RefundRequested`.
- Payment completes the refund and emits `PaymentRefunded`.
- Ordering then marks the order `CANCELLED`.
- Fulfillment rejects cancellation after shipment unless a returns workflow is introduced.

## API Surface

```http
GET    /catalog/products
GET    /catalog/products/{productId}

POST   /orders
GET    /orders/{orderId}
POST   /orders/{orderId}/cancel

POST   /payments/webhooks/{provider}

GET    /orders/{orderId}/shipment
POST   /admin/shipments/{shipmentId}/ship
POST   /admin/shipments/{shipmentId}/deliver
```

Require an `Idempotency-Key` for order creation and other retryable commands. Store the key, request fingerprint, and resulting resource ID.

## Boundary Enforcement

Add automated architecture rules:

- Modules may depend on `shared` technical primitives.
- A module’s `api` or `application` package is its only importable surface.
- Domain packages cannot depend on controllers, ORM, or provider SDKs.
- Repositories cannot query another module’s schema.
- No cross-schema foreign keys.
- Payment provider implementations depend on a port defined by Payment.
- Fulfillment cannot directly update an order.

These rules are essential; directory names alone do not create a modular monolith.

## Testing Strategy

**Domain unit tests**

- Order total calculation and immutable price snapshots
- Every valid and invalid state transition
- Payment and refund transition rules
- Fulfillment transition rules
- Money and quantity invariants

**Module integration tests**

Run against a real PostgreSQL instance:

- Repository mappings and constraints
- Transaction rollback behavior
- Optimistic locking
- Outbox insertion in the same transaction
- Idempotent command and event processing

**Boundary tests**

- Forbidden imports between module internals
- No cross-schema queries or foreign keys
- Domain layer has no framework dependencies

**Workflow tests**

- Place order -> capture payment -> prepare shipment
- Declined payment leaves fulfillment untouched
- Duplicate events do not duplicate payment attempts or shipments
- Provider timeout followed by retry
- Cancel before payment
- Cancel after capture triggers refund
- Crash after commit but before dispatch resumes from the outbox

**API tests**

- Validation and authorization
- Idempotency-key replay and conflict behavior
- Stable error response contracts
- Payment webhook signature verification

Use fakes for domain tests, containers for database integration tests, and a stub HTTP server for payment-provider contract tests. Keep a small end-to-end suite through the public HTTP API.

## Deployment

Deploy a single application artifact with:

- HTTP request handling
- An in-process or separately started worker mode using the same codebase
- One PostgreSQL database
- Schema migrations grouped by module
- Module-specific metrics and structured logs
- Correlation IDs containing `order_id`, `payment_attempt_id`, and `event_id`

There should be no independent module deployment, network API between modules, separate database per module, or distributed transaction coordinator. The boundaries preserve the option to extract a module later, but extraction is not part of this design.