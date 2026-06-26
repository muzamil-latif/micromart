# Order Notification Microservice

A FastAPI microservice that consumes `OrderPlaced` events from RabbitMQ and simulates sending confirmation notifications (logged to console).

## Setup

```bash
pip install -r requirements.txt
export RABBITMQ_URL="amqp://guest:guest@localhost:5672/"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8006
```

---

## API Reference

### `GET /health`
Liveness / readiness probe for Kubernetes (or any load balancer).

**Response `200`**
```json
{ "status": "ok", "consumer": "running" }
```

`consumer` is `"stopped"` if the background RabbitMQ task has crashed, giving Kubernetes a signal to restart the pod.

---

## RabbitMQ Integration

| Property | Value |
|---|---|
| Exchange | `orders` (topic, durable) |
| Queue | `order_notifications` (durable) |
| Routing key | `order.placed` |
| Prefetch | 10 messages |
| Reconnect interval | 5 seconds |

The service declares the exchange and queue on startup, binding them with the routing key. Declarations are idempotent — the service starts cleanly whether RabbitMQ already has these resources or not.

### Message format (published by Order service)
```json
{
  "order_id":    "3fa85f64-...",
  "user_id":     "user-42",
  "status":      "paid",
  "items":       [...],
  "grand_total": 19.98,
  "payment_ref": "pay_1a2b3c4d5e6f7a8b",
  "created_at":  "2024-01-01T12:00:00Z",
  "updated_at":  "2024-01-01T12:00:01Z"
}
```

### Console output (simulated email)
```
2024-01-01T12:00:02  INFO      notifications  📧  Order 3fa85f64-... confirmed for user user-42. Total: $19.98
2024-01-01T12:00:02  INFO      notifications      status=paid  items=2  payment_ref=pay_1a2b3c4d5e6f7a8b
```

---

## Reliability behaviour

| Scenario | Behaviour |
|---|---|
| RabbitMQ not yet available at startup | Consumer task retries every 5 s; HTTP server starts immediately |
| RabbitMQ connection drops mid-run | `connect_robust` reconnects automatically; consumer resumes |
| Malformed / undecodable message | Logged as error, nacked without requeue (no infinite loop) |
| Graceful shutdown (SIGTERM) | Consumer task cancelled cleanly; in-flight messages finish processing |

---

## Complete service map

| Service | Port | Role |
|---|---|---|
| Auth | 8001 | User registration & JWT |
| Catalog | 8002 | Product listing & stock |
| Cart | 8003 | Per-user cart in Redis |
| Orders | 8004 | Order placement saga |
| Payments | 8005 | Mock payment gateway |
| **Notifications** | **8006** | **Event consumer (this service)** |