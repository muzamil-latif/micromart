# Order Management Microservice

A FastAPI microservice that orchestrates the full order placement flow: cart → payment → stock → event.

## Setup

```bash
pip install -r requirements.txt
```

Set the required environment variables:

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/orders"
export RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
export CART_SERVICE_URL="http://localhost:8003"
export PAYMENT_SERVICE_URL="http://localhost:8005"
export CATALOG_SERVICE_URL="http://localhost:8002"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8004
```

Interactive docs at **http://localhost:8004/docs**

---

## API Reference

### `POST /orders`
Place an order for a user. Runs the full placement saga.

**Request body**
```json
{
  "user_id": "user-42",
  "payment_method": "tok_visa_4242"
}
```

**Response `201`**
```json
{
  "order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "user_id": "user-42",
  "status": "paid",
  "items": [
    {
      "product_id": 1,
      "name": "Widget",
      "quantity": 2,
      "unit_price": 9.99,
      "line_total": 19.98
    }
  ],
  "grand_total": 19.98,
  "payment_ref": "pay_abc123",
  "created_at": "2024-01-01T12:00:00Z",
  "updated_at": "2024-01-01T12:00:01Z"
}
```

**Error codes**
| Code | Reason |
|------|--------|
| `400` | Cart is empty |
| `402` | Payment declined |
| `502` | Cart, Payment, or Catalog service unreachable |

---

### `GET /orders/{user_id}`
Returns full order history for a user, most recent first.

**Response `200`** — array of order objects (same shape as above)

---

### `GET /orders/{order_id}/status`
Returns the current status of a specific order.

**Response `200`**
```json
{
  "order_id": "3fa85f64-...",
  "status": "paid",
  "payment_ref": "pay_abc123",
  "updated_at": "2024-01-01T12:00:01Z"
}
```

**Error `404`** — order not found

---

## Order Placement Flow

```
POST /orders
  │
  ├─ 1. GET  cart/{user_id}          → Cart service
  │         (empty cart → 400)
  │
  ├─ 2. INSERT orders (status=pending)  → Postgres
  │
  ├─ 3. POST payments/charge         → Payment service
  │         (declined → mark failed, return 402)
  │
  ├─ 4. UPDATE orders (status=paid)  → Postgres
  │
  ├─ 5. DELETE cart/{user_id}/clear  → Cart service   [best-effort]
  │
  ├─ 6. PUT products/{id}/stock      → Catalog service [best-effort]
  │
  └─ 7. Publish OrderPlaced event    → RabbitMQ exchange "orders", key "order.placed"
```

Steps 5 and 6 are best-effort: a failure there does **not** roll back the order — payment has already cleared. Log and handle via compensating events in production.

---

## RabbitMQ Event

Exchange: `orders` (topic, durable)  
Routing key: `order.placed`

**Payload** — the full order object as JSON:
```json
{
  "order_id": "...",
  "user_id": "...",
  "status": "paid",
  "items": [...],
  "grand_total": 19.98,
  "payment_ref": "pay_abc123",
  "created_at": "...",
  "updated_at": "..."
}
```

Messages are `PERSISTENT` (survive broker restarts).

---

## Database Schema

```sql
CREATE TABLE orders (
    order_id    TEXT          PRIMARY KEY,
    user_id     TEXT          NOT NULL,
    status      TEXT          NOT NULL DEFAULT 'pending',
    items       JSONB         NOT NULL DEFAULT '[]',
    grand_total NUMERIC(12,2) NOT NULL DEFAULT 0,
    payment_ref TEXT,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_orders_user_id ON orders(user_id);
```

Order statuses: `pending` → `paid` | `failed`

## Inter-service Contract

| Service | Endpoint called | Purpose |
|---------|----------------|---------|
| Cart | `GET /cart/{user_id}` | Fetch items & totals |
| Cart | `DELETE /cart/{user_id}/clear` | Clear after payment |
| Payment | `POST /payments/charge` | Charge `{ order_id, user_id, amount, payment_method }` → `{ payment_ref }` |
| Catalog | `PUT /products/{id}/stock` | Decrement stock `{ stock_quantity }` |