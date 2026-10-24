# actions/whatsapp_agent.py
"""WhatsApp Agent action - talk to the real business agent from voice mode.

Calls the real backend pipeline (POST /whatsapp/api/demo or /api/owner) -
the exact same session/LLM/grounding code the live WhatsApp webhook runs.
The demonstration console works even before a transport (Unipile/Zernio)
is configured; this action reports that state honestly.
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "WhatsApp agent"
CHAT_TIMEOUT = 120


def _ask(path: str, text: str) -> str:
    try:
        resp = hub.post(path, {"text": text[:2000]}, timeout=CHAT_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        data = resp.json()
    except Exception:
        return "The WhatsApp agent returned an unreadable response."

    reply = str(data.get("reply") or "").strip()
    if not reply:
        return f"The WhatsApp agent answered without text ({hub.brief(data, 200)})."
    parts = [hub.brief(reply, 700)]
    if data.get("escalate_to_human"):
        parts.append("(the agent flagged this for a human handoff)")
    return " ".join(parts)


def _status() -> str:
    health = hub.health("/whatsapp/health")
    if not health:
        return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
    if health.get("configured"):
        transport = "live transport configured"
    else:
        transport = "no live transport yet (Unipile/Zernio keys needed for real WhatsApp traffic)"
    business = "business context loaded" if health.get("business_context_loaded") else "business context NOT loaded"
    return (f"{NAME} is online - {transport}; {business}. "
            "I can still chat with it through the demo console from here.")


def run_whatsapp_agent(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    text = str(p.get("text") or p.get("message") or p.get("query") or "").strip()

    if not action:
        action = "chat" if text else "status"
    if action in ("chat", "demo", "ask", "guest"):
        if not text:
            return "Tell me what to ask the WhatsApp agent."
        return _ask("/whatsapp/api/demo", text)
    if action in ("owner", "owner_console"):
        if not text:
            return "Tell me what to ask the owner console."
        return _ask("/whatsapp/api/owner", text)
    if action in ("status", "health"):
        return _status()
    return "Options: action=chat (guest demo), action=owner (owner console), or action=status."


TOOL = {
    "name": "whatsapp_agent",
    "description": (
        "Talks to SARA's WhatsApp business agent through its real pipeline "
        "(guest demo console or owner console) and reports its transport/configuration status. "
        "Use when the user asks to test/ask the WhatsApp agent something. "
        "This does NOT message anyone on WhatsApp - it runs the agent's reply pipeline locally. "
        "To OPEN the WhatsApp workspace (not chat), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'chat' (guest demo, default), 'owner' (owner console), or 'status'.",
            },
            "text": {
                "type": "STRING",
                "description": "What to say/ask the agent.",
            },
        },
        "required": [],
    },
    "handler": run_whatsapp_agent,
}
