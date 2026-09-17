from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
from fastapi.staticfiles import StaticFiles
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


def ensure_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS follows (
                follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                following_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (follower_id, following_id),
                CHECK (follower_id != following_id)
            );
            CREATE TABLE IF NOT EXISTS message_reactions (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                emoji TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS message_receipts (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                delivered_at TEXT,
                read_at TEXT,
                PRIMARY KEY (message_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS messages_conversation_idx
                ON messages(conversation_id, id DESC);
            CREATE INDEX IF NOT EXISTS notifications_user_idx
                ON notifications(user_id, is_read, id DESC);
            CREATE INDEX IF NOT EXISTS follows_following_idx
                ON follows(following_id, follower_id);
            """
        )
        ensure_columns(
            db,
            "users",
            {"username": "TEXT", "avatar_url": "TEXT", "bio": "TEXT", "last_seen": "TEXT", "is_online": "INTEGER NOT NULL DEFAULT 0"},
        )
        ensure_columns(
            db,
            "messages",
            {"reply_to_id": "INTEGER", "attachment_url": "TEXT", "attachment_type": "TEXT"},
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
    return {
        "id": row["id"],
        "name": row["name"],
        "username": row["username"],
        "email": row["email"],
        "avatar_url": row["avatar_url"],
        "bio": row["bio"],
        "last_seen": row["last_seen"],
        "is_online": bool(row["is_online"]),
        "created_at": row["created_at"],
    }


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
    reply_to_id: int | None = Field(default=None, ge=1)
    attachment_url: str | None = Field(default=None, max_length=2000)
    attachment_type: str | None = Field(default=None, max_length=80)

    @field_validator("body")
    @classmethod
    def clean_body(cls, value: str) -> str:
        body = value.strip()
        if not body:
            raise ValueError("Message cannot be empty")
        return body


class ProfileRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    username: str | None = Field(default=None, min_length=3, max_length=30)
    bio: str | None = Field(default=None, max_length=160)
    avatar_url: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "username", "bio")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else value


class ReactionRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


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

    def is_online(self, user_id: int) -> bool:
        return bool(self.connections.get(user_id))

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
app.mount("/frontend", StaticFiles(directory=BASE_DIR / "frontend"), name="frontend")


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


@app.patch("/me/profile")
def update_profile(payload: ProfileRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return {"user": user_view(user)}
    if "username" in updates:
        updates["username"] = updates["username"].lower() if updates["username"] else None
        with connection() as db:
            duplicate = db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (updates["username"], user["id"])).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="Username is already taken")
    assignments = ", ".join(f"{field} = ?" for field in updates)
    with connection() as db:
        db.execute(f"UPDATE users SET {assignments} WHERE id = ?", [*updates.values(), user["id"]])
        updated = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return {"user": user_view(updated)}


@app.get("/users/{user_id}")
def get_user_profile(user_id: int, _: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        profile = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not profile:
            raise HTTPException(status_code=404, detail="User not found")
        followers = db.execute("SELECT COUNT(*) FROM follows WHERE following_id = ?", (user_id,)).fetchone()[0]
        following = db.execute("SELECT COUNT(*) FROM follows WHERE follower_id = ?", (user_id,)).fetchone()[0]
    return {"user": user_view(profile), "followers_count": followers, "following_count": following}


@app.post("/users/{user_id}/follow", status_code=201)
def follow_user(user_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, bool]:
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")
    with connection() as db:
        if not db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        db.execute("INSERT OR IGNORE INTO follows(follower_id, following_id, created_at) VALUES (?, ?, ?)", (user["id"], user_id, now_iso()))
    return {"following": True}


@app.delete("/users/{user_id}/follow")
def unfollow_user(user_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, bool]:
    with connection() as db:
        db.execute("DELETE FROM follows WHERE follower_id = ? AND following_id = ?", (user["id"], user_id))
    return {"following": False}


@app.get("/users")
def search_users(q: str = Query(default="", max_length=80), user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with connection() as db:
        pattern = f"%{q.strip().lower()}%"
        rows = db.execute(
            """SELECT * FROM users
                WHERE id != ? AND (LOWER(name) LIKE ? OR LOWER(email) LIKE ? OR LOWER(COALESCE(username, '')) LIKE ?)
                ORDER BY name LIMIT 20""",
            (user["id"], pattern, pattern, pattern),
        ).fetchall()
    return [user_view(row) for row in rows]


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


@app.get("/notifications")
def list_notifications(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            """SELECT n.id, n.body, n.conversation_id, n.message_id, n.is_read, n.created_at,
                      u.name AS actor_name
                 FROM notifications n JOIN users u ON u.id = n.actor_id
                WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 30""",
            (user["id"],),
        ).fetchall()
        unread_count = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user["id"],)
        ).fetchone()[0]
    return {"items": [{**dict(row), "is_read": bool(row["is_read"])} for row in rows], "unread_count": unread_count}


@app.post("/notifications/read-all")
def mark_notifications_read(user: sqlite3.Row = Depends(current_user)) -> dict[str, int]:
    with connection() as db:
        db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user["id"],))
    return {"unread_count": 0}


@app.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: int, limit: int = Query(default=50, ge=1, le=100), before_id: int | None = Query(default=None, ge=1), user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with connection() as db:
        require_participant(db, conversation_id, user["id"])
        query = """SELECT m.id, m.body, m.created_at, m.sender_id, m.reply_to_id, m.attachment_url, m.attachment_type, u.name AS sender_name
                   FROM messages m JOIN users u ON u.id = m.sender_id
                  WHERE m.conversation_id = ?"""
        params: list[Any] = [conversation_id]
        if before_id is not None:
            query += " AND m.id < ?"
            params.append(before_id)
        query += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        result = []
        for row in reversed(rows):
            reactions = db.execute(
                "SELECT emoji, COUNT(*) AS count FROM message_reactions WHERE message_id = ? GROUP BY emoji",
                (row["id"],),
            ).fetchall()
            receipts = db.execute(
                "SELECT delivered_at, read_at FROM message_receipts WHERE message_id = ? AND user_id = ?",
                (row["id"], user["id"]),
            ).fetchone()
            result.append({**dict(row), "reactions": [dict(reaction) for reaction in reactions], "receipt": dict(receipts) if receipts else None})
    return result


@app.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(conversation_id: int, payload: MessageRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        require_participant(db, conversation_id, user["id"])
        if payload.reply_to_id:
            reply = db.execute("SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?", (payload.reply_to_id, conversation_id)).fetchone()
            if not reply:
                raise HTTPException(status_code=400, detail="Reply target is not in this conversation")
        cursor = db.execute(
            """INSERT INTO messages(conversation_id, sender_id, body, reply_to_id, attachment_url, attachment_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (conversation_id, user["id"], payload.body, payload.reply_to_id, payload.attachment_url, payload.attachment_type, now_iso()),
        )
        row = db.execute(
            "SELECT id, body, created_at, sender_id, reply_to_id, attachment_url, attachment_type FROM messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        participant_rows = db.execute("SELECT user_id FROM participants WHERE conversation_id = ?", (conversation_id,)).fetchall()
        recipient_ids = [person[0] for person in participant_rows if person[0] != user["id"]]
        delivery_time = now_iso()
        db.executemany(
            "INSERT INTO message_receipts(message_id, user_id, delivered_at) VALUES (?, ?, ?)",
            [(row["id"], recipient_id, delivery_time if manager.is_online(recipient_id) else None) for recipient_id in recipient_ids],
        )
        db.executemany(
            """INSERT INTO notifications(user_id, actor_id, conversation_id, message_id, body, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(recipient_id, user["id"], conversation_id, row["id"], payload.body, row["created_at"]) for recipient_id in recipient_ids],
        )
    message = {**dict(row), "sender_name": user["name"], "conversation_id": conversation_id, "reactions": []}
    await manager.broadcast([person[0] for person in participant_rows], {"type": "message.created", "message": message})
    for recipient_id in recipient_ids:
        await manager.broadcast(
            [recipient_id],
            {
                "type": "notification.created",
                "notification": {
                    "body": payload.body,
                    "actor_name": user["name"],
                    "conversation_id": conversation_id,
                    "message_id": row["id"],
                    "created_at": row["created_at"],
                    "is_read": False,
                },
            },
        )
    return message


@app.post("/conversations/{conversation_id}/read")
async def mark_conversation_read(conversation_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, int]:
    with connection() as db:
        require_participant(db, conversation_id, user["id"])
        timestamp = now_iso()
        db.execute(
            "UPDATE message_receipts SET delivered_at = COALESCE(delivered_at, ?), read_at = ? WHERE user_id = ? AND message_id IN (SELECT id FROM messages WHERE conversation_id = ?)",
            (timestamp, timestamp, user["id"], conversation_id),
        )
        participant_ids = [row[0] for row in db.execute("SELECT user_id FROM participants WHERE conversation_id = ? AND user_id != ?", (conversation_id, user["id"])).fetchall()]
    await manager.broadcast(participant_ids, {"type": "conversation.read", "conversation_id": conversation_id, "user_id": user["id"], "read_at": timestamp})
    return {"marked_read": 1}


@app.post("/messages/{message_id}/reactions")
async def react_to_message(message_id: int, payload: ReactionRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        message = db.execute("SELECT conversation_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")
        require_participant(db, message["conversation_id"], user["id"])
        db.execute(
            "INSERT INTO message_reactions(message_id, user_id, emoji, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(message_id, user_id) DO UPDATE SET emoji = excluded.emoji, created_at = excluded.created_at",
            (message_id, user["id"], payload.emoji, now_iso()),
        )
        participant_ids = [row[0] for row in db.execute("SELECT user_id FROM participants WHERE conversation_id = ?", (message["conversation_id"],)).fetchall()]
    event = {"type": "message.reaction", "message_id": message_id, "conversation_id": message["conversation_id"], "user_id": user["id"], "emoji": payload.emoji}
    await manager.broadcast(participant_ids, event)
    return event


@app.delete("/messages/{message_id}/reactions")
async def remove_message_reaction(message_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with connection() as db:
        message = db.execute("SELECT conversation_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")
        require_participant(db, message["conversation_id"], user["id"])
        db.execute("DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?", (message_id, user["id"]))
        participant_ids = [row[0] for row in db.execute("SELECT user_id FROM participants WHERE conversation_id = ?", (message["conversation_id"],)).fetchall()]
    event = {"type": "message.reaction_removed", "message_id": message_id, "conversation_id": message["conversation_id"], "user_id": user["id"]}
    await manager.broadcast(participant_ids, event)
    return event


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        user_id = decode_token(token)
    except HTTPException:
        await websocket.close(code=1008)
        return
    with connection() as db:
        if not db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            await websocket.close(code=1008)
            return
        db.execute("UPDATE users SET is_online = 1 WHERE id = ?", (user_id,))
        peer_ids = [
            row[0]
            for row in db.execute(
                """SELECT DISTINCT p2.user_id FROM participants p1
                   JOIN participants p2 ON p2.conversation_id = p1.conversation_id
                  WHERE p1.user_id = ? AND p2.user_id != ?""",
                (user_id, user_id),
            ).fetchall()
        ]
        db.execute(
            "UPDATE message_receipts SET delivered_at = ? WHERE user_id = ? AND delivered_at IS NULL",
            (now_iso(), user_id),
        )
    await manager.connect(user_id, websocket)
    await manager.broadcast(peer_ids, {"type": "presence.changed", "user_id": user_id, "is_online": True})
    try:
        while True:
            raw_event = await websocket.receive_text()
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in {"typing.start", "typing.stop"}:
                continue
            conversation_id = event.get("conversation_id")
            if not isinstance(conversation_id, int):
                continue
            with connection() as db:
                is_member = db.execute(
                    "SELECT 1 FROM participants WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                ).fetchone()
                recipients = [row[0] for row in db.execute(
                    "SELECT user_id FROM participants WHERE conversation_id = ? AND user_id != ?",
                    (conversation_id, user_id),
                ).fetchall()]
            if is_member:
                await manager.broadcast(
                    recipients,
                    {"type": event["type"], "conversation_id": conversation_id, "user_id": user_id},
                )
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)
        if not manager.is_online(user_id):
            with connection() as db:
                db.execute("UPDATE users SET is_online = 0, last_seen = ? WHERE id = ?", (now_iso(), user_id))
            await manager.broadcast(peer_ids, {"type": "presence.changed", "user_id": user_id, "is_online": False})


app.mount("/", StaticFiles(directory=BASE_DIR / "frontend", html=True), name="app")