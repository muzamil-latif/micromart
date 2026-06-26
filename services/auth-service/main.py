import os
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET   = os.environ["JWT_SECRET"]
ALGORITHM    = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

# ── App & Security ────────────────────────────────────────────────────────────

app = FastAPI(title="User Auth Service", version="1.0.0")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

# ── DB lifecycle ──────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with app.state.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id        SERIAL PRIMARY KEY,
                name      TEXT        NOT NULL,
                email     TEXT        NOT NULL UNIQUE,
                password  TEXT        NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()

# ── Pydantic models ───────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name:     str        = Field(..., min_length=1, max_length=120)
    email:    EmailStr
    password: str        = Field(..., min_length=8)

class LoginRequest(BaseModel):
    email:    EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"

class UserInfo(BaseModel):
    id:         int
    name:       str
    email:      str
    created_at: datetime

# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain[:72])

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain[:72], hashed)

def create_token(user_id: int, email: str) -> str:
    payload = {
        "sub":   str(user_id),
        "email": email,
        "exp":   datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """Create a new user account."""
    hashed = hash_password(body.password)
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO users (name, email, password) VALUES ($1, $2, $3) RETURNING id",
                body.name, body.email, hashed,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists",
        )
    return {"id": row["id"], "message": "User registered successfully"}


@app.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Authenticate and receive a JWT token."""
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, password FROM users WHERE email = $1",
            body.email,
        )

    if not row or not verify_password(body.password, row["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_token(row["id"], row["email"])
    return TokenResponse(access_token=token)


@app.get("/verify", response_model=UserInfo)
async def verify(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Verify a Bearer token and return the associated user info."""
    payload = decode_token(credentials.credentials)
    user_id = int(payload["sub"])

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, email, created_at FROM users WHERE id = $1",
            user_id,
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserInfo(**dict(row))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
# updated
