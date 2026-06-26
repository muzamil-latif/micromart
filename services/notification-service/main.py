import os
import json
import logging
import asyncio
from datetime import datetime, timezone

import aio_pika
from fastapi import FastAPI
import uvicorn

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("notifications")

# ── Config ────────────────────────────────────────────────────────────────────

RABBITMQ_URL  = os.environ["RABBITMQ_URL"]
EXCHANGE_NAME = "orders"
QUEUE_NAME    = "order_notifications"
ROUTING_KEY   = "order.placed"

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Order Notification Service", version="1.0.0")

# ── Message handler ───────────────────────────────────────────────────────────

async def handle_order_placed(message: aio_pika.IncomingMessage) -> None:
    """
    Process an OrderPlaced event from RabbitMQ.
    Acks on success, nacks without requeue on unrecoverable parse errors
    so poison messages don't loop forever.
    """
    async with message.process(requeue=False):
        try:
            payload = json.loads(message.body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("Failed to decode message body: %s — dropping message", exc)
            return

        order_id    = payload.get("order_id", "<unknown>")
        user_id     = payload.get("user_id",  "<unknown>")
        grand_total = payload.get("grand_total", 0.0)
        status      = payload.get("status", "")

        # Simulated email / notification send
        log.info(
            "📧  Order %s confirmed for user %s. Total: $%.2f",
            order_id, user_id, grand_total,
        )

        # Additional detail lines — useful in dev, easy to strip in prod
        item_count = len(payload.get("items", []))
        log.info(
            "    status=%s  items=%d  payment_ref=%s",
            status, item_count, payload.get("payment_ref", "—"),
        )


# ── RabbitMQ consumer (runs as a background asyncio task) ─────────────────────

async def consume_forever() -> None:
    """
    Connect to RabbitMQ with automatic reconnection, declare the shared
    exchange + queue, and consume messages indefinitely.
    """
    while True:
        try:
            log.info("Connecting to RabbitMQ …")
            connection = await aio_pika.connect_robust(
                RABBITMQ_URL,
                reconnect_interval=5,
            )

            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=10)

                # Declare exchange (must match the Order service declaration)
                exchange = await channel.declare_exchange(
                    EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
                )

                # Durable queue survives broker restarts
                queue = await channel.declare_queue(QUEUE_NAME, durable=True)
                await queue.bind(exchange, routing_key=ROUTING_KEY)

                log.info(
                    "Listening on queue '%s' (exchange='%s', key='%s') …",
                    QUEUE_NAME, EXCHANGE_NAME, ROUTING_KEY,
                )

                await queue.consume(handle_order_placed)

                # Block here until the connection drops
                await asyncio.Future()

        except asyncio.CancelledError:
            log.info("Consumer task cancelled — shutting down.")
            break
        except Exception as exc:
            log.error("RabbitMQ connection lost: %s — retrying in 5 s …", exc)
            await asyncio.sleep(5)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    task = asyncio.create_task(consume_forever(), name="rabbitmq-consumer")
    app.state.consumer_task = task
    log.info("Notification service started.")


@app.on_event("shutdown")
async def shutdown() -> None:
    task: asyncio.Task = app.state.consumer_task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("Notification service stopped.")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Kubernetes liveness / readiness probe."""
    consumer_task: asyncio.Task = app.state.consumer_task
    consumer_alive = not consumer_task.done()
    return {
        "status":   "ok",
        "consumer": "running" if consumer_alive else "stopped",
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=False)