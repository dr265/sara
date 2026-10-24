# actions/voice_agent.py
"""Voice Agent action - run the Hub's Voice Agent engine from voice mode.

Calls the real backend engine:
  * command -> POST /api/voice/execute, which parses the request with the
    LLM and then REALLY acts: it sends an email (Gmail/SMTP) or places a real
    Twilio phone call. This is consequential - use only on explicit request.
  * status  -> GET /voice-agent/health (telephony provider + capabilities).
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "Voice Agent"
EXECUTE_TIMEOUT = 120


def _execute(command: str) -> str:
    body: Dict[str, Any] = {"user_id": "sara-desktop", "command": command[:2000]}
    try:
        resp = hub.post("/api/voice/execute", body, timeout=EXECUTE_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        data = resp.json()
    except Exception:
        return "The Voice Agent returned an unreadable response."
    status = str(data.get("status") or "").lower()
    message = str(data.get("message") or "").strip()
    if status == "success":
        return message or "The Voice Agent executed the command."
    if message:
        return f"The Voice Agent could not do that: {message}"
    return f"The Voice Agent returned: {hub.brief(data, 300)}"


def _status() -> str:
    health = hub.health("/voice-agent/health")
    if not health:
        return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
    provider = health.get("telephony_provider") or "unknown"
    caps = health.get("telephony_capabilities") or {}
    enabled = [name.replace("_", " ") for name, ok in caps.items() if ok]
    parts = [f"{NAME} engine is online (telephony: {provider})"]
    if enabled:
        parts.append("capabilities: " + ", ".join(enabled))
    parts.append("I can execute a spoken command through it - ask me to "
                 "'tell the voice agent to email someone' or 'to call someone'.")
    return ". ".join(parts) + "."


def run_voice_agent(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    command = str(p.get("command") or p.get("text") or p.get("query") or "").strip()

    if not action:
        action = "command" if command else "status"
    if action in ("command", "execute", "run"):
        if not command:
            return "Tell me the command for the Voice Agent (e.g. 'email john@example.com about the meeting')."
        return _execute(command)
    if action in ("status", "health"):
        return _status()
    return "Options: action=command (execute - real send/call) or action=status."


TOOL = {
    "name": "voice_agent",
    "description": (
        "Sends a command to SARA's Voice Agent engine, which REALLY executes it: "
        "it can send an email or place a real phone call via Twilio. "
        "Use ONLY when the user explicitly asks to have the voice agent call/message someone. "
        "The engine decides email vs call from the command text. "
        "To OPEN the Voice workspace (not use it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'command' (default when command given) or 'status'.",
            },
            "command": {
                "type": "STRING",
                "description": "The natural-language command, e.g. 'call Rakesh at +91...' or 'email mary@acme.com about tomorrow's demo'.",
            },
        },
        "required": [],
    },
    "handler": run_voice_agent,
}
