"""Run/pause/status control for the Telegram agent (guest-facing bot).

The Telegram booking agent itself is the WSGI app mounted at /agent (see
app/core/telegram_host.py). These lightweight FastAPI routes give the frontend:

* a persisted RUN / PAUSE switch for it (when paused, inbound guest webhooks at
  /agent/webhooks/telegram/* are answered with {"status": "paused"} and no work
  is done — the owner console at /agent/conversations/owner keeps working), and
* a "bring your own bot" flow: POST a Telegram bot token, it is verified
  against Telegram (getMe), persisted in telegram_bot_token.json, and the agent
  then runs on THAT token (the .env default is only a fallback). The token
  itself is never returned by any route and never logged.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.agent_switch import is_running, set_running
from app.core.telegram_bot_token import (
    active_bot_info,
    clear_bot_token,
    get_bot_token,
    save_bot_token,
)

router = APIRouter(prefix="/telegram", tags=["Telegram Agent"])


class BotTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


_ENV_BOT_CACHE: dict = {}


def _env_default_bot() -> dict:
    """Resolve (and cache for 5 min) the .env default bot's identity via getMe."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {}
    now = time.time()
    bot = _ENV_BOT_CACHE.get("bot")
    if bot and now - _ENV_BOT_CACHE.get("at", 0) < 300:
        return bot
    bot = _telegram_get_me(token)
    if bot:
        _ENV_BOT_CACHE.update({"bot": bot, "at": now})
    return bot or {}


def _resolved_bot_info() -> dict:
    """active_bot_info() plus the real username of the .env default bot."""
    info = active_bot_info()
    if (
        not info["using_custom_token"]
        and info["configured"]
        and not info["bot_username"]
    ):
        env_bot = _env_default_bot()
        if env_bot:
            info["bot_username"] = env_bot["username"]
            info["bot_name"] = env_bot["name"]
            info["bot_id"] = env_bot.get("id")
    return info


def _telegram_get_me(token: str) -> dict[str, Any] | None:
    """Ask Telegram who owns this token. Returns bot metadata or None if invalid."""
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        data = resp.json()
    except Exception as exc:  # network / timeout / bad response
        raise HTTPException(status_code=502, detail=f"Could not reach Telegram: {exc}")
    if resp.status_code == 401 or not data.get("ok"):
        return None
    result = data.get("result") or {}
    username = result.get("username") or ""
    first = result.get("first_name") or ""
    last = result.get("last_name") or ""
    if not result.get("is_bot") or not username:
        return None
    return {
        "id": result.get("id"),
        "username": username,
        "name": " ".join(part for part in (first, last) if part) or username,
    }


@router.get("/status")
def telegram_status() -> dict[str, Any]:
    info = _resolved_bot_info()
    return {
        "agent": "telegram",
        "running": is_running("telegram"),
        "bot_token_set": info["configured"],
        "bot_username": info["bot_username"],
        "bot_name": info["bot_name"],
        "using_custom_token": info["using_custom_token"],
        "inbound_endpoint": "/agent/webhooks/telegram/{connection_id}",
    }


@router.get("/bot-token")
def bot_token_status() -> dict[str, Any]:
    """Metadata about the active bot (never the token itself)."""
    return _resolved_bot_info()


@router.post("/bot-token")
def set_bot_token(payload: BotTokenIn) -> dict[str, Any]:
    """Verify a token with Telegram, persist it, and start the agent on it.

    A valid token must look like "<bot_id>:<secret>" and pass Telegram's getMe.
    """
    token = payload.token.strip()
    if ":" not in token or len(token) < 20:
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like a Telegram bot token (expected 123456789:AAH... from @BotFather).",
        )

    bot = _telegram_get_me(token)
    if bot is None:
        raise HTTPException(
            status_code=401,
            detail="Telegram rejected this token. Copy the exact token from @BotFather → API Token.",
        )

    save_bot_token(token, bot["username"], bot["name"], bot.get("id"))

    # A different bot has different pending updates — drop the poller's offset.
    from app.core.agent_pollers import reset_telegram_poller

    reset_telegram_poller()

    # "…then the bot starts working" — authorization turns the agent on.
    state = set_running("telegram", True)
    return {
        "authorized": True,
        "bot": bot,
        "running": state.get("running", True),
        "active": active_bot_info(),
    }


@router.delete("/bot-token")
def reset_bot_token() -> dict[str, Any]:
    """Remove the UI-authorized bot and fall back to the .env default token."""
    cleared = clear_bot_token()
    from app.core.agent_pollers import reset_telegram_poller

    reset_telegram_poller()
    info = active_bot_info()
    return {
        "cleared": cleared,
        "message": "Fell back to the .env default bot." if info["configured"] else "No bot token configured.",
        "active": info,
    }


@router.post("/run")
def run_telegram() -> dict[str, Any]:
    """Turn the Telegram agent ON (persisted — stays on across restarts)."""
    return set_running("telegram", True)


@router.post("/pause")
def pause_telegram() -> dict[str, Any]:
    """Turn the Telegram agent OFF without shutting the backend down."""
    return set_running("telegram", False)
