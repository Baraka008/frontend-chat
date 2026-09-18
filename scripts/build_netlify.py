import os
from pathlib import Path

api_url = os.getenv("FRONTEND_CHAT_API_URL", "").strip().rstrip("/")
if not api_url:
    raise SystemExit("FRONTEND_CHAT_API_URL must be set in Netlify environment variables")

Path("frontend/config.js").write_text(
    "window.FRONTEND_CHAT_API = " + repr(api_url) + ";\n",
    encoding="utf-8",
)
