from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "frontend_chat.db"))
JWT_SECRET = os.getenv("JWT_SECRET", "frontend-chat-development-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                is_group INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participants (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conversation_idx
                ON messages(conversation_id, id DESC);
            """
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, encoded_salt, encoded_digest = stored.split("$", 2)
        salt = base64.urlsafe_b64decode(encoded_salt.encode())
        expected = base64.urlsafe_b64decode(encoded_digest.encode())
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": expires}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def user_view(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "email": row["email"], "created_at": row["created_at"]}


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ConversationRequest(BaseModel):
    participant_ids: list[int] = Field(min_length=1, max_length=50)
    title: str | None = Field(default=None, max_length=120)


class MessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def clean_body(cls, value: str) -> str:
        body = value.strip()
        if not body:
            raise ValueError("Message cannot be empty")
        return body


security = HTTPBearer(auto_error=False)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> sqlite3.Row:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization required")
    user_id = decode_token(credentials.credentials)
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


def require_participant(db: sqlite3.Connection, conversation_id: int, user_id: int) -> None:
    found = db.execute(
        "SELECT 1 FROM participants WHERE conversation_id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()
    if not found:
        raise HTTPException(status_code=404, detail="Conversation not found")


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[int, set[WebSocket]] = {}

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        sockets = self.connections.get(user_id, set())
        sockets.discard(websocket)
        if not sockets:
            self.connections.pop(user_id, None)

    async def broadcast(self, user_ids: list[int], event: dict[str, Any]) -> None:
        for user_id in user_ids:
            for websocket in list(self.connections.get(user_id, set())):
                try:
                    await websocket.send_json(event)
                except Exception:
                    self.disconnect(user_id, websocket)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield


app = FastAPI(title="Frontend Chat API", version="1.0.0", lifespan=lifespan)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "Frontend Chat"}


@app.post("/auth/register", status_code=201)
def register(payload: RegisterRequest) -> dict[str, Any]:
    email = str(payload.email).lower()
    with connection() as db:
        try:
            cursor = db.execute(
                "INSERT INTO users(name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (payload.name, email, hash_password(payload.password), now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Email is already registered") from exc
        user = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"user": user_view(user), "token": create_token(user["id"])}


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (str(payload.email).lower(),)).fetchone()
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"user": user_view(user), "token": create_token(user["id"])}


@app.get("/me")
def me(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    return {"user": user_view(user)}


@app.post("/conversations", status_code=201)
def create_conversation(payload: ConversationRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    participant_ids = sorted(set(payload.participant_ids + [user["id"]]))
    with connection() as db:
        placeholders = ",".join("?" for _ in participant_ids)
        valid_count = db.execute(f"SELECT COUNT(*) FROM users WHERE id IN ({placeholders})", participant_ids).fetchone()[0]
        if valid_count != len(participant_ids):
            raise HTTPException(status_code=400, detail="One or more participants do not exist")
        cursor = db.execute(
            "INSERT INTO conversations(title, is_group, created_at) VALUES (?, ?, ?)",
            (payload.title, len(participant_ids) > 2, now_iso()),
        )
        conversation_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO participants(conversation_id, user_id, joined_at) VALUES (?, ?, ?)",
            [(conversation_id, participant_id, now_iso()) for participant_id in participant_ids],
        )
    return {"id": conversation_id, "title": payload.title, "is_group": len(participant_ids) > 2, "participant_ids": participant_ids}


@app.get("/conversations")
def list_conversations(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """SELECT c.id, c.title, c.is_group, c.created_at,
                      (SELECT body FROM messages WHERE conversation_id = c.id ORDER BY id DESC LIMIT 1) AS last_message,
                      (SELECT created_at FROM messages WHERE conversation_id = c.id ORDER BY id DESC LIMIT 1) AS last_message_at
               FROM conversations c JOIN participants p ON p.conversation_id = c.id
              WHERE p.user_id = ? ORDER BY COALESCE(last_message_at, c.created_at) DESC""",
            (user["id"],),
        ).fetchall()
        result = []
        for row in rows:
            people = db.execute("SELECT user_id FROM participants WHERE conversation_id = ?", (row["id"],)).fetchall()
            result.append({**dict(row), "is_group": bool(row["is_group"]), "participant_ids": [person[0] for person in people]})
    return result


@app.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: int, limit: int = Query(default=50, ge=1, le=100), before_id: int | None = Query(default=None, ge=1), user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with connection() as db:
        require_participant(db, conversation_id, user["id"])
        query = """SELECT m.id, m.body, m.created_at, m.sender_id, u.name AS sender_name
                   FROM messages m JOIN users u ON u.id = m.sender_id
                  WHERE m.conversation_id = ?"""
        params: list[Any] = [conversation_id]
        if before_id is not None:
            query += " AND m.id < ?"
            params.append(before_id)
        query += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
    return [dict(row) for row in reversed(rows)]


@app.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: int, payload: MessageRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        require_participant(db, conversation_id, user["id"])
        cursor = db.execute(
            "INSERT INTO messages(conversation_id, sender_id, body, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, user["id"], payload.body, now_iso()),
        )
        row = db.execute(
            "SELECT id, body, created_at, sender_id FROM messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        participant_rows = db.execute("SELECT user_id FROM participants WHERE conversation_id = ?", (conversation_id,)).fetchall()
    message = {**dict(row), "sender_name": user["name"], "conversation_id": conversation_id}
    await manager.broadcast([person[0] for person in participant_rows], {"type": "message.created", "message": message})
    return message


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        user_id = decode_token(token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)