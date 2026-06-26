import os
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

import asyncpg
import aio_pika
import httpx
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL        = os.environ["DATABASE_URL"]
RABBITMQ_URL        = os.environ["RABBITMQ_URL"]
CART_SERVICE_URL    = os.environ["CART_SERVICE_URL"].rstrip("/")
PAYMENT_SERVICE_URL = os.environ["PAYMENT_SERVICE_URL"].rstrip("/")
CATALOG_SERVICE_URL = os.environ["CATALOG_SERVICE_URL"].rstrip("/")

EXCHANGE_NAME = "orders"
ROUTING_KEY   = "order.placed"

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Order Management Service", version="1.0.0")

# ── Order status enum ─────────────────────────────────────────────────────────

class OrderStatus(str, Enum):
    PENDING    = "pending"
    PAID       = "paid"
    FAILED     = "failed"
    CANCELLED  = "cancelled"

# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateOrderRequest(BaseModel):
    user_id:          str = Field(..., min_length=1)
    payment_method:   str = Field(..., min_length=1)   # opaque token/method passed to Payment service

class OrderItem(BaseModel):
    product_id:  int
    name:        str
    quantity:    int
    unit_price:  float
    line_total:  float

class OrderOut(BaseModel):
    order_id:       str
    user_id:        str
    status:         OrderStatus
    items:          list[OrderItem]
    grand_total:    float
    payment_ref:    Optional[str]
    created_at:     datetime
    updated_at:     datetime

class OrderStatusOut(BaseModel):
    order_id:    str
    status:      OrderStatus
    payment_ref: Optional[str]
    updated_at:  datetime

# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # Postgres
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with app.state.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id    TEXT        PRIMARY KEY,
                user_id     TEXT        NOT NULL,
                status      TEXT        NOT NULL DEFAULT 'pending',
                items       JSONB       NOT NULL DEFAULT '[]',
                grand_total NUMERIC(12,2) NOT NULL DEFAULT 0,
                payment_ref TEXT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
        """)

    # RabbitMQ
    app.state.rabbit_conn     = await aio_pika.connect_robust(RABBITMQ_URL)
    app.state.rabbit_channel  = await app.state.rabbit_conn.channel()
    app.state.exchange        = await app.state.rabbit_channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
    )

    # Shared HTTP client for all downstream service calls
    app.state.http = httpx.AsyncClient(timeout=10.0)

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()
    await app.state.rabbit_conn.close()
    await app.state.http.aclose()

# ── Service call helpers ──────────────────────────────────────────────────────

async def _get_cart(http: httpx.AsyncClient, user_id: str) -> dict:
    """Fetch the user's current cart from the Cart service."""
    try:
        resp = await http.get(f"{CART_SERVICE_URL}/cart/{user_id}")
    except httpx.RequestError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Cart service unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Cart service returned {resp.status_code}")
    return resp.json()


async def _clear_cart(http: httpx.AsyncClient, user_id: str) -> None:
    """Tell the Cart service to clear the user's cart after a successful order."""
    try:
        await http.delete(f"{CART_SERVICE_URL}/cart/{user_id}/clear")
    except httpx.RequestError:
        # Best-effort; don't roll back a paid order over a cart clear failure
        pass


async def _process_payment(
    http: httpx.AsyncClient,
    order_id: str,
    user_id: str,
    amount: float,
    payment_method: str,
) -> str:
    """
    Call the Payment service.  Returns the payment reference on success.
    Raises 402 if the payment is declined, 502 on connectivity issues.
    """
    payload = {
        "order_id":       order_id,
        "user_id":        user_id,
        "amount":         amount,
        "payment_method": payment_method,
    }
    try:
        resp = await http.post(f"{PAYMENT_SERVICE_URL}/payments/charge", json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Payment service unreachable: {exc}")

    if resp.status_code == 402:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, resp.json().get("detail", "Payment declined"))
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Payment service returned {resp.status_code}")

    return resp.json()["payment_ref"]


async def _decrement_stock(
    http: httpx.AsyncClient,
    product_id: int,
    new_quantity: int,
) -> None:
    """Update product stock in the Catalog service. Best-effort after payment."""
    try:
        await http.put(
            f"{CATALOG_SERVICE_URL}/products/{product_id}/stock",
            json={"stock_quantity": new_quantity},
        )
    except httpx.RequestError:
        pass   # log in production; don't fail the order


async def _publish_event(exchange, order: dict) -> None:
    """Publish an OrderPlaced event to RabbitMQ."""
    message = aio_pika.Message(
        body=json.dumps(order, default=str).encode(),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
    )
    await exchange.publish(message, routing_key=ROUTING_KEY)

# ── DB helpers ────────────────────────────────────────────────────────────────

def _row_to_order(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["items"] = json.loads(d["items"]) if isinstance(d["items"], str) else d["items"]
    return d

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(body: CreateOrderRequest):
    """
    Full order placement flow:
      1. Fetch cart → validate it's non-empty
      2. Reserve an order_id and persist as 'pending'
      3. Charge via Payment service
      4. Update order to 'paid', clear cart, adjust stock, publish event
      On payment failure the order row is marked 'failed' for auditability.
    """
    http     = app.state.http
    pool     = app.state.pool
    exchange = app.state.exchange

    # 1. Fetch cart ────────────────────────────────────────────────────────────
    cart = await _get_cart(http, body.user_id)
    if not cart.get("items"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cart is empty — nothing to order")

    cart_items  = cart["items"]
    grand_total = float(cart["grand_total"])

    # Normalise items to our internal shape
    order_items = [
        {
            "product_id": it["product_id"],
            "name":       it["name"],
            "quantity":   it["quantity"],
            "unit_price": it["price"],
            "line_total": it["line_total"],
        }
        for it in cart_items
    ]

    # 2. Persist order as PENDING ──────────────────────────────────────────────
    order_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO orders (order_id, user_id, status, items, grand_total)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            order_id, body.user_id, OrderStatus.PENDING,
            json.dumps(order_items), grand_total,
        )

    # 3. Process payment ───────────────────────────────────────────────────────
    try:
        payment_ref = await _process_payment(
            http, order_id, body.user_id, grand_total, body.payment_method
        )
    except HTTPException as exc:
        # Mark failed for audit; re-raise original error to the caller
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status=$1, updated_at=NOW() WHERE order_id=$2",
                OrderStatus.FAILED, order_id,
            )
        raise exc

    # 4. Mark PAID, clear cart, adjust stock, publish event ───────────────────
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET status      = $1,
                   payment_ref = $2,
                   updated_at  = NOW()
             WHERE order_id = $3
            RETURNING *
            """,
            OrderStatus.PAID, payment_ref, order_id,
        )

    order_dict = _row_to_order(row)

    # Clear cart (best-effort)
    await _clear_cart(http, body.user_id)

    # Decrement stock for each item (best-effort; catalog service owns stock)
    for it in cart_items:
        new_stock = it["stock_quantity"] - it["quantity"]
        if new_stock >= 0:
            await _decrement_stock(http, it["product_id"], new_stock)

    # Publish OrderPlaced event
    await _publish_event(exchange, order_dict)

    return OrderOut(**order_dict)


@app.get("/orders/{user_id}", response_model=list[OrderOut])
async def get_order_history(user_id: str):
    """Return all orders for a user, most recent first."""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM orders WHERE user_id=$1 ORDER BY created_at DESC",
            user_id,
        )
    return [OrderOut(**_row_to_order(r)) for r in rows]


@app.get("/orders/{order_id}/status", response_model=OrderStatusOut)
async def get_order_status(order_id: str):
    """Return the current status of a specific order."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT order_id, status, payment_ref, updated_at FROM orders WHERE order_id=$1",
            order_id,
        )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Order {order_id} not found")
    return OrderStatusOut(**dict(row))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8004, reload=False)