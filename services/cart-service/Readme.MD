# Shopping Cart Microservice

A FastAPI microservice that manages per-user shopping carts in Redis, with live product validation and pricing fetched from the Catalog service.

## Setup

```bash
pip install -r requirements.txt
```

Set the required environment variables:

```bash
export REDIS_URL="redis://localhost:6379"
export CATALOG_SERVICE_URL="http://localhost:8002"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8003
```

Interactive docs available at **http://localhost:8003/docs**

---

## API Reference

### `POST /cart/{user_id}/add`
Add a product to the cart, or increment its quantity if already present.
Validates the product against the Catalog service before adding.

**Request body**
```json
{ "product_id": 1, "quantity": 2 }
```

**Response `200`** — full enriched cart
```json
{
  "user_id": "user-42",
  "items": [
    {
      "product_id": 1,
      "quantity": 2,
      "name": "Widget",
      "price": 9.99,
      "stock_quantity": 98,
      "line_total": 19.98
    }
  ],
  "grand_total": 19.98
}
```
**Error `404`** — product not found in catalog  
**Error `502`** — catalog service unreachable  

---

### `GET /cart/{user_id}`
Returns the current cart with live prices from the Catalog service.
Returns an empty cart (not a 404) if the user has no active cart.

**Response `200`** — same shape as above; `items: []` and `grand_total: 0.0` for an empty cart.

---

### `DELETE /cart/{user_id}/remove/{product_id}`
Removes a product from the cart entirely (all quantity).

**Response `200`** — updated cart after removal  
**Error `404`** — product not in cart  

---

### `DELETE /cart/{user_id}/clear`
Clears the entire cart. Idempotent — succeeds even if already empty.
Intended to be called by the Order service after a successful purchase.

**Response `200`**
```json
{ "user_id": "user-42", "message": "Cart cleared" }
```

---

## Redis Data Model

Each cart is stored as a **Redis Hash**:

```
Key:   cart:{user_id}
Fields: { "<product_id>": "<quantity>", ... }
TTL:   7 days (refreshed on every add)
```

Example:
```
HGETALL cart:user-42
  1  ->  2
  5  ->  1
```

`HINCRBY` is used for add operations, so concurrent increments are atomic.

---

## Service Dependencies

| Dependency | Purpose |
|---|---|
| Redis | Cart storage (hash per user, 7-day TTL) |
| Catalog service (`CATALOG_SERVICE_URL`) | Product validation on add; live name, price, stock on get |

Products that are deleted from the catalog after being added to a cart are **silently dropped** from the enriched response rather than causing a hard error.

---

## Inter-service Contract

The cart service expects the Catalog service to expose:

```
GET /products/{id}
→ 200 { id, name, price, stock_quantity, ... }
→ 404 if not found
```