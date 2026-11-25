
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pwdlib import PasswordHash
from pydantic import BaseModel, Field


# Configuration
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/users.db")
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-secret-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60

password_hash = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)

app = FastAPI(title="Security Service")


# Database
@contextmanager
def get_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@app.on_event("startup")
def init_database():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        """)


# Request models
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


# Token functions
def create_access_token(user_id: str):
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": user_id,
        "exp": expires
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
):
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        payload = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )

        user_id = payload.get("sub")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    with get_db() as db:
        user = db.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return dict(user)


# POST /v1/user - registration
@app.post("/v1/user", status_code=201)
def register(request: RegisterRequest):
    user_id = str(uuid4())
    hashed_password = password_hash.hash(request.password)

    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO users (id, username, password_hash)
                VALUES (?, ?, ?)
                """,
                (user_id, request.username, hashed_password)
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Username already exists"
        )

    return {
        "id": user_id,
        "username": request.username
    }


# POST /v1/token - login
@app.post("/v1/token")
def login(request: LoginRequest):
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE username = ?",
            (request.username,)
        ).fetchone()

    if user is None or not password_hash.verify(
        request.password,
        user["password_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password"
        )

    token = create_access_token(user["id"])

    return {
        "access_token": token,
        "token_type": "bearer"
    }


# GET /v1/token/validation - token validation
@app.get("/v1/token/validation")
@app.get("/v1/token/validation/", include_in_schema=False)
def validate_token(user=Depends(get_current_user)):
    return {"valid": True}


# GET /v1/user - current user information
@app.get("/v1/user")
def get_user(user=Depends(get_current_user)):
    return user


@app.get("/health")
def health():
    return {"status": "ok"}
