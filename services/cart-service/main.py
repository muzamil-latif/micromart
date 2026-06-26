import os
import json
from typing import Optional

import redis.asyncio as aioredis
import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL           = os.environ["REDIS_URL"]
CATALOG_SERVICE_URL = os.environ["CATALOG_SERVICE_URL"].rstrip("/")

CART_KEY     = "cart:{user_id}"          # Redis hash: field=product_id, value=quantity
CART_TTL     = 60 * 60 * 24 * 7         # 7 days — carts expire if abandoned

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Shopping Cart Service", version="1.0.0")

# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    app.state.redis  = await aioredis.from_url(REDIS_URL, decode_responses=True)
    app.state.http   = httpx.AsyncClient(base_url=CATALOG_SERVICE_URL, timeout=5.0)

@app.on_event("shutdown")
async def shutdown():
    await app.state.redis.close()
    await app.state.http.aclose()

# ── Pydantic models ───────────────────────────────────────────────────────────

class AddItemRequest(BaseModel):
    product_id: int = Field(..., gt=0)
    quantity:   int = Field(..., gt=0)

class CartItem(BaseModel):
    product_id:     int
    quantity:       int
    name:           str
    price:          float
    stock_quantity: int
    line_total:     float

class CartResponse(BaseModel):
    user_id:     str
    items:       list[CartItem]
    grand_total: float

# ── Helpers ───────────────────────────────────────────────────────────────────

def _cart_key(user_id: str) -> str:
    return CART_KEY.format(user_id=user_id)

async def _fetch_product(http: httpx.AsyncClient, product_id: int) -> dict:
    """
    Call the Catalog service to validate the product exists and fetch its details.
    Raises 404 if not found, 502 on any connectivity failure.
    """
    try:
        resp = await http.get(f"/products/{product_id}")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach Catalog service: {exc}",
        )

    if resp.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found in catalog",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Catalog service returned {resp.status_code}",
        )
    return resp.json()

async def _enrich_cart(
    http: httpx.AsyncClient,
    raw: dict[str, str],        # {product_id_str: quantity_str}
) -> tuple[list[CartItem], float]:
    """
    Fetch live product details for every item in the cart and compute totals.
    Items whose products have since been deleted are silently dropped.
    """
    items: list[CartItem] = []
    grand_total = 0.0

    for pid_str, qty_str in raw.items():
        product_id = int(pid_str)
        quantity   = int(qty_str)
        try:
            product = await _fetch_product(http, product_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue        # product removed from catalog — skip silently
            raise

        line_total = round(product["price"] * quantity, 2)
        grand_total = round(grand_total + line_total, 2)
        items.append(CartItem(
            product_id=product_id,
            quantity=quantity,
            name=product["name"],
            price=product["price"],
            stock_quantity=product["stock_quantity"],
            line_total=line_total,
        ))

    return items, grand_total

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post(
    "/cart/{user_id}/add",
    response_model=CartResponse,
    status_code=status.HTTP_200_OK,
)
async def add_item(user_id: str, body: AddItemRequest):
    """
    Add (or increment) a product in the user's cart.
    Validates the product exists in the Catalog service before adding.
    Quantity is *incremented* if the product is already in the cart.
    """
    # Validate product exists first
    await _fetch_product(app.state.http, body.product_id)

    key = _cart_key(user_id)
    redis = app.state.redis

    # HINCRBY atomically increments (or creates) the field
    await redis.hincrby(key, str(body.product_id), body.quantity)
    await redis.expire(key, CART_TTL)   # refresh TTL on every activity

    raw = await redis.hgetall(key)
    items, grand_total = await _enrich_cart(app.state.http, raw)
    return CartResponse(user_id=user_id, items=items, grand_total=grand_total)


@app.get("/cart/{user_id}", response_model=CartResponse)
async def get_cart(user_id: str):
    """
    Return the current cart contents with live prices from the Catalog service.
    Returns an empty cart if the user has no active cart.
    """
    raw = await app.state.redis.hgetall(_cart_key(user_id))
    if not raw:
        return CartResponse(user_id=user_id, items=[], grand_total=0.0)

    items, grand_total = await _enrich_cart(app.state.http, raw)
    return CartResponse(user_id=user_id, items=items, grand_total=grand_total)


@app.delete("/cart/{user_id}/remove/{product_id}", response_model=CartResponse)
async def remove_item(user_id: str, product_id: int):
    """
    Remove a single product from the user's cart entirely.
    Returns 404 if the product is not in the cart.
    """
    key   = _cart_key(user_id)
    redis = app.state.redis

    removed = await redis.hdel(key, str(product_id))
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} is not in the cart",
        )

    raw = await redis.hgetall(key)
    items, grand_total = await _enrich_cart(app.state.http, raw)
    return CartResponse(user_id=user_id, items=items, grand_total=grand_total)


@app.delete("/cart/{user_id}/clear", status_code=status.HTTP_200_OK)
async def clear_cart(user_id: str):
    """
    Delete the entire cart for a user.
    Intended to be called by the Order service after a successful order is placed.
    Idempotent — succeeds even if the cart is already empty.
    """
    await app.state.redis.delete(_cart_key(user_id))
    return {"user_id": user_id, "message": "Cart cleared"}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=False)