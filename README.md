# Frontend Chat

A complete local chat app with a Python backend and browser frontend.

## Run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
py -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the chat app or `http://127.0.0.1:8000/docs` for the interactive API.

## API

- `POST /auth/register` creates a user and returns a JWT.
- `POST /auth/login` returns a JWT.
- `GET /me` returns the authenticated profile.
- `PATCH /me/profile` updates name, username, bio, and avatar URL.
- `GET /users?q=...` searches users available for a new conversation.
- `GET /users/{id}` returns a profile with follower/following counts.
- `POST /users/{id}/follow` and `DELETE /users/{id}/follow` manage follows.
- `GET /notifications` returns recent notifications and the unread count.
- `POST /notifications/read-all` marks the current user's notifications as read.
- `POST /conversations` creates a direct or group conversation.
- `GET /conversations` lists conversations for the current user.
- `GET /conversations/{id}/messages` reads paginated message history.
- `POST /conversations/{id}/messages` sends text, reply, or attachment metadata and broadcasts it over WebSocket.
- `POST /conversations/{id}/read` marks incoming messages as read.
- `POST /messages/{id}/reactions` and `DELETE /messages/{id}/reactions` manage emoji reactions.
- `WS /ws?token=<jwt>` receives live `message.created` events for joined conversations.
- WebSocket also supports `typing.start`, `typing.stop`, `presence.changed`, `conversation.read`, `message.reaction`, and `message.reaction_removed` events.
- `GET /health` reports service status.

The SQLite database is created automatically at `frontend_chat.db`.

The frontend lives in `frontend/` and is served by the FastAPI app. It supports registration, login, conversation search, message history, sending messages, notifications, logout, and realtime WebSocket updates. The backend is designed around shared messaging primitives used by WhatsApp and Instagram DMs without depending on either platform.
