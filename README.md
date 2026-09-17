# Frontend Chat backend

A complete local backend for the Frontend Chat app.

## Run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
py -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API.

## API

- `POST /auth/register` creates a user and returns a JWT.
- `POST /auth/login` returns a JWT.
- `GET /me` returns the authenticated profile.
- `POST /conversations` creates a direct or group conversation.
- `GET /conversations` lists conversations for the current user.
- `GET /conversations/{id}/messages` reads paginated message history.
- `POST /conversations/{id}/messages` sends a message and broadcasts it over WebSocket.
- `WS /ws?token=<jwt>` receives live `message.created` events for joined conversations.
- `GET /health` reports service status.

The SQLite database is created automatically at `frontend_chat.db`.
