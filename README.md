# Frontend Chat

A local FastAPI + browser chat prototype with registration, conversations, messages, reactions, notifications and WebSocket updates.

## Local setup

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
py -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the app and `http://127.0.0.1:8000/docs` for API documentation.

## Environment and security

- Set `JWT_SECRET` to a random secret of at least 32 characters for production.
- Set `APP_ENV=production` in production. The app refuses to start with a weak or missing production JWT secret.
- Set `CORS_ORIGINS` to the exact trusted frontend origins. Wildcard CORS is rejected in production.
- Never commit `.env` or share JWT secrets.

## Current storage limitation

The current API implementation uses SQLite. It is suitable for local development and single-instance demos, not a horizontally scaled production chat service. A production deployment needs a durable persistent disk at minimum, and ideally a PostgreSQL repository adapter plus shared realtime/pub-sub infrastructure. The Supabase SQL schema is a reference and is not yet wired into the current Python data-access layer.

## Deployment

The FastAPI app serves the frontend from `frontend/` and should run as a persistent web service on Render or Railway, not as a static Vercel deployment. Use `uvicorn app.main:app --host 0.0.0.0 --port $PORT` and configure a persistent storage path for SQLite for demo use.

## API overview

- `POST /auth/register` and `POST /auth/login`
- `GET /me`, `PATCH /me/profile`, `PATCH /me/settings`
- User search, profiles and follow/unfollow
- Conversations, paginated messages and reactions
- Notifications and read receipts
- `WS /ws?token=<jwt>` for live conversation events
- `GET /health`

## Checks

GitHub Actions runs Python bytecode compilation and the pytest suite when a tests directory is present.
