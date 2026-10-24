# actions/email_automation.py
"""Email Automation action - create real campaign drafts from voice mode.

Calls the real backend agent (POST /email-agent/campaigns, GET health /
campaigns) - the same routes the Hub workspace uses. Campaigns are created
in DRY-RUN by default: no email is ever sent from voice mode. Sending stays
a human step in the workspace (that is also where recipients are managed).
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "Email Automation"
MAX_FIELD = 2000


def _create(p: Dict[str, Any]) -> str:
    name = str(p.get("name") or "").strip()[:120]
    context = str(p.get("context") or "").strip()[:MAX_FIELD]
    objective = str(p.get("objective") or "").strip()[:MAX_FIELD]
    instructions = str(p.get("instructions") or "").strip()[:MAX_FIELD]
    subject = str(p.get("subject") or "").strip()[:300]
    tone = str(p.get("tone") or "").strip()[:60]

    if not (context or objective or instructions):
        return ("Tell me what the campaign is about - for example: 'create an email "
                "campaign that introduces our new coffee subscription to old customers'.")

    body: Dict[str, Any] = {
        "name": name or "Voice campaign",
        "context": context,
        "instructions": instructions,
        "objective": objective,
        "subject": subject,
        "dry_run": True,  # voice mode never sends; sending is a workspace step
    }
    if tone:
        body["tone"] = tone

    try:
        resp = hub.post("/email-agent/campaigns", body, timeout=30)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        created = resp.json()
    except Exception:
        return "The Email agent returned an unreadable response."

    campaign_id = str(created.get("id") or "").strip()
    return (f'Created email campaign draft "{body["name"]}"'
            + (f" (id {campaign_id[:8]})" if campaign_id else "")
            + " in dry-run mode. Add recipients and press Start on "
              "/hub/email-automation when you are ready to send.")


def _status() -> str:
    health = hub.health("/email-agent/health")
    if not health:
        return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
    parts = ["Email Automation is online"]
    parts.append("LLM: " + ("yes" if health.get("llm") else "no"))
    if health.get("gmail_connected"):
        parts.append(f"sending via connected Gmail account ({health.get('gmail_email') or 'connected'})")
    elif health.get("smtp"):
        parts.append("SMTP sender configured")
    else:
        parts.append("sender not fully configured - drafts work, sending is blocked")
    running = health.get("running_campaigns") or []
    if running:
        parts.append(f"{len(running)} campaign(s) running")
    campaigns = _recent()
    if campaigns:
        parts.append(f"{len(campaigns)} recent campaign(s)")
    return ". ".join(parts) + "."


def _recent(limit: int = 5) -> List[Dict[str, Any]]:
    try:
        resp = hub.get("/email-agent/campaigns", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data[:limit]
    except Exception:
        pass
    return []


def _list() -> str:
    rows = _recent()
    if not rows:
        return "No email campaigns exist yet. Ask me to create one."
    lines = []
    for c in rows:
        counts = c.get("counts") or {}
        sent = counts.get("sent") or counts.get("sent_ok") or 0
        lines.append(f"{c.get('name') or 'campaign'} [{c.get('status') or 'unknown'}"
                     + (f", {sent} sent" if sent else "") + "]")
    return "Recent email campaigns: " + "; ".join(lines) + "."


def run_email_automation(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    if not action:
        action = "status" if not (p.get("context") or p.get("objective") or p.get("instructions")) else "create"
    if action in ("create", "draft", "new"):
        return _create(p)
    if action in ("status", "health"):
        return _status()
    if action in ("list", "campaigns"):
        return _list()
    return "Options: action=create (draft a campaign), action=list, or action=status."


TOOL = {
    "name": "email_automation",
    "description": (
        "Creates a REAL draft campaign in SARA's Email Automation agent (dry-run - "
        "nothing is sent from voice mode) and reports sender/LLM status. "
        "Use when the user asks to set up / draft an email campaign or asks about the email agent. "
        "Sending, recipients and scheduling stay human steps in /hub/email-automation. "
        "For a one-off email (send now), use the send_message or voice_agent tools instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'create' (default when details are given), 'list', or 'status'.",
            },
            "name": {"type": "STRING", "description": "Optional campaign name."},
            "context": {
                "type": "STRING",
                "description": "What the campaign is about (product, audience, offer).",
            },
            "objective": {"type": "STRING", "description": "Optional goal, e.g. 'book a demo call'."},
            "instructions": {"type": "STRING", "description": "Optional extra writing instructions."},
            "subject": {"type": "STRING", "description": "Optional preferred subject line."},
            "tone": {"type": "STRING", "description": "Optional tone, e.g. friendly, formal."},
        },
        "required": [],
    },
    "handler": run_email_automation,
}
