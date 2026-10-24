"""Persisted Telegram bot-token override.

The Telegram agent normally uses TELEGRAM_BOT_TOKEN from backend/.env. A user
may instead plug in their OWN bot token from the UI ("put token -> authorized
-> the bot runs on that token only"). That override is stored in
<backend>/telegram_bot_token.json so it survives restarts, and every consumer
(the direct adapter and the getUpdates poller) resolves the token through
get_bot_token() — stored override first, then the .env default.

Security: the token is never returned by any API response and is never logged.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / "telegram_bot_token.json"


def _read() -> dict:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_bot_token() -> str:
    """Active bot token: stored UI override first, then the .env default."""
    data = _read()
    token = data.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def active_bot_info() -> dict:
    """Public metadata about the active bot — never the token itself."""
    data = _read()
    custom = data.get("token")
    return {
        "configured": bool(get_bot_token()),
        "using_custom_token": bool(custom and str(custom).strip()),
        "env_default_present": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "bot_username": data.get("bot_username") or "",
        "bot_name": data.get("bot_name") or "",
        "bot_id": data.get("bot_id"),
        "set_at": data.get("set_at") or "",
    }


def save_bot_token(token: str, bot_username: str, bot_name: str, bot_id: int | None = None) -> None:
    """Persist a UI-authorized token as the active bot."""
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "token": token.strip(),
                "bot_username": bot_username,
                "bot_name": bot_name,
                "bot_id": bot_id,
                "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_bot_token() -> bool:
    """Remove the UI override so the agent falls back to the .env default."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        return True
    return False
