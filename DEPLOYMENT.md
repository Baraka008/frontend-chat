# Production deployment

## Recommended architecture

Supabase is the production data, authentication, and realtime layer. The FastAPI app still needs an application host such as Render, Railway, Fly.io, or a container platform. GitHub stores and deploys the source; Supabase does not execute `uvicorn`.

## Supabase setup

1. Create a Supabase project.
2. Open SQL Editor and run `supabase/schema.sql`.
3. In Authentication, enable Email and Password. Enable Phone only after configuring an SMS provider.
4. Enable Realtime for `messages`, `notifications`, and `profiles`.
5. Keep the `service_role` key server-side only. Never put it in frontend code.

## FastAPI host setup

Set these variables on the application host:

```text
JWT_SECRET=<long-random-secret>
JWT_EXPIRE_MINUTES=10080
CORS_ORIGINS=https://your-frontend-domain.example
DATABASE_PATH=/var/data/frontend_chat.db
```

The current FastAPI service is SQLite-backed for local development. To use Supabase as the database instead of the local compatibility database, the next migration step is replacing the connection layer with the Supabase Postgres connection string or Supabase server client. The SQL schema is included now so the data model and RLS rules are versioned before that switch.

## Netlify frontend hosting

Netlify hosts the static frontend. The FastAPI API and WebSocket server must run separately on Render, Railway, Fly.io, or another application host.

1. In Netlify, choose **Add new site > Import an existing project** and select this GitHub repository.
2. Netlify will use `netlify.toml`: publish directory `frontend`, build command `python scripts/build_netlify.py`.
3. Add the environment variable `FRONTEND_CHAT_API_URL` with the public FastAPI origin, for example `https://frontend-chat-api.example.com`. Do not include a trailing slash.
4. Deploy the site. The build creates `frontend/config.js`, which points the browser client at the API.
5. Set the API host's `CORS_ORIGINS` to the exact Netlify URL, for example `https://your-site.netlify.app`.
6. Ensure the API host supports WebSocket upgrades at `/ws`. The browser will use `wss://` automatically when the API URL is HTTPS.

Netlify cannot run the current FastAPI process. Do not put `JWT_SECRET`, a database path, or a Supabase service-role key in Netlify variables. Only the public API origin belongs in `FRONTEND_CHAT_API_URL`.

## GitHub

The repository remote is already configured as `https://github.com/Baraka008/frontend-chat.git`. Push the reviewed changes from a machine with GitHub credentials:

```powershell
git add .
git commit -m "Build production chat experience"
git push origin main
```

Do not commit `.env`, database files, or Supabase secret keys. The public Supabase URL and anon key may be used in a future static client, but the anon key must still be protected by the RLS policies in `supabase/schema.sql`.
