# Product Catalog Microservice

A FastAPI microservice for managing a product catalog, with PostgreSQL for persistence and Redis for caching.

## Setup

```bash
pip install -r requirements.txt
```

Set the two required environment variables:

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/mydb"
export REDIS_URL="redis://localhost:6379"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8002
```

Interactive docs available at **http://localhost:8002/docs**

---

## API Reference

### `GET /products`
Returns all products. Response is cached in Redis for 60 seconds.

**Response `200`**
```json
[
  {
    "id": 1,
    "name": "Widget",
    "description": "A fine widget",
    "price": 9.99,
    "stock_quantity": 100,
    "created_at": "2024-01-01T12:00:00Z",
    "updated_at": "2024-01-01T12:00:00Z"
  }
]
```

---

### `GET /products/{id}`
Returns a single product by ID. Response is cached in Redis for 60 seconds.

**Response `200`** — product object (same shape as above)
**Error `404`** — product not found

---

### `POST /products`
Creates a new product. Invalidates the list cache.

**Request body**
```json
{
  "name": "Widget",
  "description": "A fine widget",
  "price": 9.99,
  "stock_quantity": 100
}
```

**Response `201`** — created product object
**Validation** — `price` must be ≥ 0; `stock_quantity` must be ≥ 0.

---

### `PUT /products/{id}/stock`
Updates the stock quantity for a product. Called by the Order service after a purchase.
Invalidates both the individual product cache and the all-products list cache.

**Request body**
```json
{ "stock_quantity": 95 }
```

**Response `200`** — updated product object
**Error `404`** — product not found

---

## Caching Strategy

| Event | Cache effect |
|---|---|
| `GET /products` | Reads `products:all`; writes it on miss |
| `GET /products/{id}` | Reads `products:{id}`; writes it on miss |
| `POST /products` | Deletes `products:all` |
| `PUT /products/{id}/stock` | Deletes `products:{id}` **and** `products:all` |

TTL is 60 seconds (configurable via `CACHE_TTL` in `main.py`).

---

## Database

Table auto-created on startup:

```sql
CREATE TABLE IF NOT EXISTS products (
    id             SERIAL PRIMARY KEY,
    name           TEXT           NOT NULL,
    description    TEXT           NOT NULL DEFAULT '',
    price          NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    stock_quantity INTEGER        NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
```