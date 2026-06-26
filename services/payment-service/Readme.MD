# Payment Processing Microservice (Mock)

A FastAPI microservice that simulates a real payment gateway — 90% success rate, 10% decline — with full transaction persistence in PostgreSQL.

## Setup

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://user:password@localhost:5432/payments"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8005
```

Interactive docs at **http://localhost:8005/docs**

---

## API Reference

### `POST /payments/charge`
Process a payment for an order.

**Request body**
```json
{
  "order_id":       "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "user_id":        "user-42",
  "amount":         19.98,
  "payment_method": "tok_visa_4242"
}
```

**Response `200` — success**
```json
{
  "transaction_id": "a1b2c3d4-...",
  "order_id":       "3fa85f64-...",
  "user_id":        "user-42",
  "amount":         19.98,
  "payment_method": "tok_visa_4242",
  "status":         "success",
  "failure_reason": null,
  "payment_ref":    "pay_1a2b3c4d5e6f7a8b",
  "created_at":     "2024-01-01T12:00:00Z"
}
```

**Response `402` — declined (10% of new charges)**
```json
{ "detail": "Insufficient funds" }
```

**Idempotency**: Sending the same `order_id` twice returns the existing transaction rather than charging again. A previously declined order returns `402` consistently.

---

### `GET /transactions/{order_id}`
Returns the full transaction record for an order.

**Response `200`** — same shape as above  
**Error `404`** — no transaction for this order

---

### `GET /transactions/{order_id}/status`
Lightweight status check.

**Response `200`**
```json
{
  "transaction_id": "a1b2c3d4-...",
  "status":         "success",
  "failure_reason": null,
  "payment_ref":    "pay_1a2b3c4d5e6f7a8b"
}
```

---

## Simulated Decline Reasons

When a payment fails (10% probability), one of these reasons is returned:

- Insufficient funds
- Card declined by issuer
- Suspected fraud — transaction blocked
- Daily limit exceeded
- Invalid card number

---

## Database Schema

```sql
CREATE TABLE transactions (
    transaction_id TEXT          PRIMARY KEY,
    order_id       TEXT          NOT NULL UNIQUE,
    user_id        TEXT          NOT NULL,
    amount         NUMERIC(12,2) NOT NULL,
    payment_method TEXT          NOT NULL,
    status         TEXT          NOT NULL,       -- 'success' | 'failed'
    failure_reason TEXT,                          -- null on success
    payment_ref    TEXT,                          -- null on failure
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
```

`order_id` has a `UNIQUE` constraint — the enforcement point for idempotency at the DB level.

---

## Integration with Order Service

The Order service calls `POST /payments/charge`. On:
- **200** → extract `payment_ref`, mark order as `paid`
- **402** → mark order as `failed`, surface decline reason to the caller
- **5xx / timeout** → treat as transient; retry with same `order_id` (idempotent)