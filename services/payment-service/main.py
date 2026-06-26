import os
import uuid
import random
from datetime import datetime
from enum import Enum
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL     = os.environ["DATABASE_URL"]
SUCCESS_RATE     = 0.90   # 90% success, 10% decline

# Simulated decline reasons — mirrors real gateway response variety
DECLINE_REASONS = [
    "Insufficient funds",
    "Card declined by issuer",
    "Suspected fraud — transaction blocked",
    "Daily limit exceeded",
    "Invalid card number",
]

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Payment Processing Service", version="1.0.0")

# ── Enums ─────────────────────────────────────────────────────────────────────

class TransactionStatus(str, Enum):
    SUCCESS = "success"
    FAILED  = "failed"

# ── Pydantic models ───────────────────────────────────────────────────────────

class PayRequest(BaseModel):
    order_id:       str   = Field(..., min_length=1)
    user_id:        str   = Field(..., min_length=1)
    amount:         float = Field(..., gt=0)
    payment_method: str   = Field(..., min_length=1)   # opaque token / method string

class TransactionOut(BaseModel):
    transaction_id: str
    order_id:       str
    user_id:        str
    amount:         float
    payment_method: str
    status:         TransactionStatus
    failure_reason: Optional[str]
    payment_ref:    Optional[str]
    created_at:     datetime

# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with app.state.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT          PRIMARY KEY,
                order_id       TEXT          NOT NULL UNIQUE,
                user_id        TEXT          NOT NULL,
                amount         NUMERIC(12,2) NOT NULL,
                payment_method TEXT          NOT NULL,
                status         TEXT          NOT NULL,
                failure_reason TEXT,
                payment_ref    TEXT,
                created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_order_id
                ON transactions(order_id);
        """)

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _simulate_payment() -> tuple[bool, Optional[str]]:
    """Return (success, failure_reason). 90/10 split."""
    if random.random() < SUCCESS_RATE:
        return True, None
    return False, random.choice(DECLINE_REASONS)

def _row_to_dict(row: asyncpg.Record) -> dict:
    d = dict(row)
    d["amount"] = float(d["amount"])
    return d

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/payments/charge", response_model=TransactionOut)
async def charge(body: PayRequest):
    """
    Process a payment for an order.
    - Idempotent on order_id: a duplicate request returns the existing transaction.
    - Returns 200 on success with a payment_ref.
    - Returns 402 on simulated decline with a failure_reason.
    """
    pool = app.state.pool

    # Idempotency — return existing transaction for this order
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM transactions WHERE order_id = $1", body.order_id
        )
    if existing:
        data = _row_to_dict(existing)
        if data["status"] == TransactionStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=data["failure_reason"],
            )
        return TransactionOut(**data)

    # Simulate gateway response
    succeeded, failure_reason = _simulate_payment()

    transaction_id = str(uuid.uuid4())
    payment_ref    = f"pay_{uuid.uuid4().hex[:16]}" if succeeded else None
    tx_status      = TransactionStatus.SUCCESS if succeeded else TransactionStatus.FAILED

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO transactions
                (transaction_id, order_id, user_id, amount, payment_method,
                 status, failure_reason, payment_ref)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            transaction_id, body.order_id, body.user_id,
            body.amount, body.payment_method,
            tx_status, failure_reason, payment_ref,
        )

    data = _row_to_dict(row)

    if not succeeded:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=failure_reason,
        )

    return TransactionOut(**data)


@app.get("/transactions/{order_id}", response_model=TransactionOut)
async def get_transaction(order_id: str):
    """Return the transaction record for a given order."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM transactions WHERE order_id = $1", order_id
        )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No transaction found for order {order_id}",
        )
    return TransactionOut(**_row_to_dict(row))


@app.get("/transactions/{order_id}/status")
async def get_transaction_status(order_id: str):
    """Lightweight status check without full transaction payload."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT transaction_id, status, failure_reason, payment_ref FROM transactions WHERE order_id = $1",
            order_id,
        )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No transaction found for order {order_id}",
        )
    return dict(row)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=False)