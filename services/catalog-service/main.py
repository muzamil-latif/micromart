import os
import json
from decimal import Decimal
from datetime import datetime
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field, condecimal
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL    = os.environ["REDIS_URL"]

CACHE_TTL          = 60       # seconds
CACHE_ALL_PRODUCTS = "products:all"
CACHE_PRODUCT_KEY  = "products:{id}"

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Product Catalog Service", version="1.0.0")

# ── DB & Cache lifecycle ──────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    app.state.redis = await aioredis.from_url(REDIS_URL, decode_responses=True)

    async with app.state.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id               SERIAL PRIMARY KEY,
                name             TEXT           NOT NULL,
                description      TEXT           NOT NULL DEFAULT '',
                price            NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
                stock_quantity   INTEGER        NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
                created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                updated_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
            )
        """)

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()
    await app.state.redis.close()

# ── Pydantic models ───────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    name:             str                                      = Field(..., min_length=1, max_length=255)
    description:      str                                      = Field("", max_length=2000)
    price:            condecimal(ge=Decimal("0"), max_digits=12, decimal_places=2)
    stock_quantity:   int                                      = Field(0, ge=0)

class StockUpdate(BaseModel):
    stock_quantity: int = Field(..., ge=0)

class ProductOut(BaseModel):
    id:             int
    name:           str
    description:    str
    price:          float
    stock_quantity: int
    created_at:     datetime
    updated_at:     datetime

# ── Cache helpers ─────────────────────────────────────────────────────────────

def _serialise(row: asyncpg.Record) -> dict:
    """Convert a DB record to a JSON-safe dict."""
    d = dict(row)
    d["price"]      = float(d["price"])
    d["created_at"] = d["created_at"].isoformat()
    d["updated_at"] = d["updated_at"].isoformat()
    return d

async def _cache_get(redis, key: str) -> Optional[list | dict]:
    raw = await redis.get(key)
    return json.loads(raw) if raw else None

async def _cache_set(redis, key: str, value):
    await redis.set(key, json.dumps(value), ex=CACHE_TTL)

async def _invalidate_product(redis, product_id: int):
    """Bust both the individual product cache and the all-products list cache."""
    await redis.delete(CACHE_PRODUCT_KEY.format(id=product_id))
    await redis.delete(CACHE_ALL_PRODUCTS)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/products", response_model=list[ProductOut])
async def list_products():
    """Return all products — served from Redis cache when available."""
    redis = app.state.redis

    cached = await _cache_get(redis, CACHE_ALL_PRODUCTS)
    if cached is not None:
        return cached

    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM products ORDER BY id"
        )

    data = [_serialise(r) for r in rows]
    await _cache_set(redis, CACHE_ALL_PRODUCTS, data)
    return data


@app.get("/products/{product_id}", response_model=ProductOut)
async def get_product(product_id: int):
    """Return a single product — served from Redis cache when available."""
    redis = app.state.redis
    cache_key = CACHE_PRODUCT_KEY.format(id=product_id)

    cached = await _cache_get(redis, cache_key)
    if cached is not None:
        return cached

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM products WHERE id = $1", product_id
        )

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    data = _serialise(row)
    await _cache_set(redis, cache_key, data)
    return data


@app.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(body: ProductCreate):
    """Create a new product and invalidate the list cache."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO products (name, description, price, stock_quantity)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            body.name, body.description, body.price, body.stock_quantity,
        )

    # A new product changes the full list — bust that cache only
    await app.state.redis.delete(CACHE_ALL_PRODUCTS)
    return _serialise(row)


@app.put("/products/{product_id}/stock", response_model=ProductOut)
async def update_stock(product_id: int, body: StockUpdate):
    """
    Update stock quantity for a product.
    Intended to be called by the Order service after a successful purchase.
    Invalidates both the individual product cache and the list cache.
    """
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE products
               SET stock_quantity = $1,
                   updated_at     = NOW()
             WHERE id = $2
            RETURNING *
            """,
            body.stock_quantity, product_id,
        )

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    await _invalidate_product(app.state.redis, product_id)
    return _serialise(row)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=False)