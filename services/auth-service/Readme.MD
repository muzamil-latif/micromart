# User Auth Microservice

A FastAPI microservice providing JWT-based user authentication backed by PostgreSQL.

## Setup

```bash
pip install -r requirements.txt
```

Set the two required environment variables:

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/mydb"
export JWT_SECRET="your-secret-key-change-me"
```

## Running

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8001
```

Interactive docs available at **http://localhost:8001/docs**

---

## API Reference

### `POST /register`
Create a new user account.

**Request body**
```json
{ "name": "Alice", "email": "alice@example.com", "password": "s3cr3tpassword" }
```

**Response `201`**
```json
{ "id": 1, "message": "User registered successfully" }
```

**Error `409`** — email already taken.

---

### `POST /login`
Authenticate and receive a JWT token.

**Request body**
```json
{ "email": "alice@example.com", "password": "s3cr3tpassword" }
```

**Response `200`**
```json
{ "access_token": "<jwt>", "token_type": "bearer" }
```

**Error `401`** — invalid credentials.

---

### `GET /verify`
Validate a Bearer token and return user info.

**Header**
```
Authorization: Bearer <token>
```

**Response `200`**
```json
{
  "id": 1,
  "name": "Alice",
  "email": "alice@example.com",
  "created_at": "2024-01-01T12:00:00Z"
}
```

**Error `401`** — token invalid, expired, or user not found.

---

## Database

The service auto-creates the `users` table on startup if it doesn't exist:

```sql
CREATE TABLE IF NOT EXISTS users (
    id         SERIAL PRIMARY KEY,
    name       TEXT        NOT NULL,
    email      TEXT        NOT NULL UNIQUE,
    password   TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```