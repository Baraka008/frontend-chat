# Repository audit — first pass

## Findings
- FastAPI backend plus browser frontend; not a Vercel-only deployment. Keep the API on Render, Railway, or Fly.io and host static assets on Vercel if desired.
- README describes both SQLite local mode and Supabase production schema. Ensure production configuration cannot silently fall back to local SQLite.
- Verify JWT secret strength, password hashing, CORS allowlist, upload validation, WebSocket authentication, and Supabase Row Level Security.
- Add CI for Python formatting, static analysis, and tests; run migrations against a disposable test database.

## Verification
README and root structure inspected; Python tests were not run.