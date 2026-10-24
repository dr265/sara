# actions/telegram_assistant.py
"""Telegram Assistant action - real control of the Hub's Telegram bot.

Calls the real control API (GET /telegram/status, POST /telegram/run|pause) -
the same routes the Hub workspace uses. "Run" really turns the hosted
Telegram agent ON (it stays on across restarts); the bot token is managed in
the workspace, never here.
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "Telegram assistant"


def _status_text(data: Dict[str, Any]) -> str:
    running = data.get("running")
    token_set = data.get("bot_token_set")
    username = data.get("bot_username") or ""
    parts = [f"{NAME} is {'running' if running else 'paused'}"]
    if token_set:
        parts.append(f"bot @{username} connected" if username else "bot token set")
    else:
        parts.append("no bot token yet - add one on /hub/telegram-assistant")
    return ". ".join(parts) + "."


def _get_status() -> Dict[str, Any]:
    try:
        resp = hub.get("/telegram/status", timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _set_running(run: bool) -> str:
    path = "/telegram/run" if run else "/telegram/pause"
    try:
        resp = hub.post(path, {}, timeout=15)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    status = _get_status()
    if status:
        return _status_text(status)
    return f"{NAME} was {'turned on' if run else 'paused'} (confirmed by the backend)."


def run_telegram_assistant(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    if action in ("run", "start", "on", "enable"):
        return _set_running(True)
    if action in ("pause", "stop", "off", "disable"):
        return _set_running(False)
    if action in ("", "status", "health"):
        health = hub.health("/telegram/status")
        if not health:
            return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
        return _status_text(health)
    return "Options: action=run (turn it on), action=pause, or action=status."


TOOL = {
    "name": "telegram_assistant",
    "description": (
        "Controls SARA's hosted Telegram assistant: report status, turn it ON, or pause it. "
        "Use when the user asks about the Telegram bot/assistant state or to start/stop it. "
        "It does not send Telegram messages itself (the bot answers users in Telegram). "
        "To OPEN the Telegram workspace (not control it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'status' (default), 'run' (turn on), or 'pause'.",
            },
        },
        "required": [],
    },
    "handler": run_telegram_assistant,
}
