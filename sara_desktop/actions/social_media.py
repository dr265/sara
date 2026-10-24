# actions/social_media.py
"""Social Media action - real content generation & scheduling from voice mode.

Calls the real backend engine (the same routes the Hub workspace uses):
  * generate -> real LLM post drafts (the workspace dry-run is temporarily
    forced ON so nothing is queued for publishing; the previous settings are
    restored afterwards);
  * schedule -> really schedules the given content on the user's connected
    accounts (this is consequential - the post WILL go out);
  * status   -> health, connected accounts and recent jobs.
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "Social Media agent"
DEFAULT_WORKSPACE = "demo_user"
MAX_CONTENT = 4000


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def _get_accounts() -> List[Dict[str, Any]]:
    try:
        resp = hub.get("/social/accounts", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            accounts = data.get("accounts")
            if isinstance(accounts, list):
                return accounts
    except Exception:
        pass
    return []


def _status() -> str:
    health = hub.health("/social/health")
    if not health:
        return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
    accounts = _get_accounts()
    connected = [a for a in accounts if str(a.get("status")) == "connected"]
    parts = [f"{NAME} is online"]
    if connected:
        platforms = ", ".join(sorted({str(a.get("platform_label") or a.get("platform")) for a in connected}))
        parts.append(f"{len(connected)} connected account(s): {platforms}")
    else:
        parts.append("no accounts connected yet")
    try:
        jobs_resp = hub.get("/social/jobs?limit=5", timeout=15)
        jobs = jobs_resp.json().get("jobs") if jobs_resp.status_code == 200 else []
    except Exception:
        jobs = []
    if jobs:
        parts.append(f"{len(jobs)} recent job(s)")
    dry = hub.health("/social/dry-run")
    if dry is not None:
        parts.append("dry-run " + ("ON (nothing is published)" if dry.get("dry_run") else "OFF (jobs publish for real)"))
    return ". ".join(parts) + "."


# --------------------------------------------------------------------------
# generate (never queues real posts)
# --------------------------------------------------------------------------

def _pilot_state() -> Dict[str, Any]:
    try:
        resp = hub.get("/social/pilot", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            state = data.get("pilot")
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {}


def _get_dry_run() -> bool:
    try:
        resp = hub.get("/social/dry-run", timeout=15)
        if resp.status_code == 200:
            return bool(resp.json().get("dry_run"))
    except Exception:
        pass
    return False


def _set_dry_run(value: bool) -> None:
    try:
        hub.post("/social/dry-run", {"dry_run": bool(value)}, timeout=15)
    except Exception:
        pass


def _configure_pilot(niche: str, schedule_text: str, enabled: bool) -> None:
    hub.post("/social/pilot",
             {"niche": niche, "schedule": schedule_text, "enabled": bool(enabled)},
             timeout=15)


def _generate(p: Dict[str, Any]) -> str:
    topic = str(p.get("topic") or p.get("niche") or p.get("prompt") or "").strip()
    original = _pilot_state()
    original_niche = str(original.get("niche") or "").strip()
    original_schedule = str(original.get("schedule_text") or "")
    original_enabled = bool(original.get("enabled"))
    niche = topic or original_niche
    if not niche:
        return ("Tell me the topic or niche for the posts - for example: "
                "'draft social posts about our new coffee subscription'.")
    try:
        count = int(p.get("count") or 3)
    except (TypeError, ValueError):
        count = 3
    count = max(1, min(10, count))

    original_dry = _get_dry_run()
    try:
        # Force drafts + keep the background pilot loop out of the way,
        # then restore the workspace exactly as it was.
        _set_dry_run(True)
        _configure_pilot(niche, original_schedule, False)
        resp = hub.post("/social/pilot/generate", {"count": count}, timeout=180)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    finally:
        try:
            _configure_pilot(original_niche, original_schedule, original_enabled)
            _set_dry_run(original_dry)
        except Exception:
            pass

    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        data = resp.json()
    except Exception:
        return "The Social agent returned an unreadable response."

    posts = data.get("posts") or []
    generated = int(data.get("generated") or len(posts))
    if not generated:
        return "The Social agent generated no posts - check the topic and try again."
    parts = [f'Generated {generated} draft post(s) for "{hub.brief(niche, 60)}" (kept as drafts)']
    if posts:
        preview = posts[0]
        text = preview.get("content") if isinstance(preview, dict) else preview
        parts.append("First: " + hub.brief(text, 220))
    parts.append("Review and publish from /hub/social-media")
    return ". ".join(parts) + "."


# --------------------------------------------------------------------------
# schedule (consequential - publishes/schedules for real)
# --------------------------------------------------------------------------

def _schedule(p: Dict[str, Any]) -> str:
    content = str(p.get("content") or p.get("text") or p.get("post") or "").strip()
    if len(content) < 3:
        return "Give me the post text to schedule."

    workspace = str(p.get("workspace_id") or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
    account_ids = p.get("account_ids")
    if isinstance(account_ids, str):
        account_ids = [a.strip() for a in account_ids.split(",") if a.strip()]
    if not isinstance(account_ids, list) or not account_ids:
        accounts = _get_accounts()
        account_ids = [str(a.get("id")) for a in accounts
                       if str(a.get("status")) == "connected" and a.get("id")]
        if not account_ids:
            return ("No social account is connected, so nothing can be scheduled. "
                    "Connect one on /hub/social-media first.")

    body: Dict[str, Any] = {
        "workspace_id": workspace,
        "account_ids": account_ids,
        "content": content[:MAX_CONTENT],
        "content_kind": str(p.get("content_kind") or "text"),
    }
    scheduled_for = str(p.get("scheduled_for") or "").strip()
    if scheduled_for:
        body["scheduled_for"] = scheduled_for
    recurrence = str(p.get("recurrence") or "").strip()
    if recurrence:
        body["recurrence"] = recurrence
    timezone = str(p.get("timezone") or "").strip()
    if timezone:
        body["timezone"] = timezone

    try:
        resp = hub.post("/social/schedule", body, timeout=60)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        data = resp.json()
    except Exception:
        return "The Social agent returned an unreadable response."

    when = scheduled_for or "as soon as the worker picks it up"
    return (f"Scheduled your post on {len(account_ids)} account(s) - {when}. "
            f"Track it under /hub/social-media (jobs list).")


def run_social_media(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    if not action:
        if p.get("content") or p.get("text") or p.get("post"):
            action = "schedule"
        elif p.get("topic") or p.get("niche"):
            action = "generate"
        else:
            action = "status"
    if action in ("generate", "draft", "drafts", "posts"):
        return _generate(p)
    if action in ("schedule", "post", "publish"):
        return _schedule(p)
    if action in ("status", "health"):
        return _status()
    return "Options: action=generate (draft posts), action=schedule (post for real), or action=status."


TOOL = {
    "name": "social_media",
    "description": (
        "Works with the real Social Media agent: generates AI draft posts for a topic/niche "
        "(kept as drafts - nothing is published) or schedules given content on the user's "
        "connected accounts (CONSEQUENTIAL - it really schedules/publishes). "
        "Use for 'draft some posts about X' / 'schedule this post'. For posting to specific "
        "apps use their dedicated tools. To OPEN the Social workspace (not act), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'generate' (drafts), 'schedule', or 'status'.",
            },
            "topic": {
                "type": "STRING",
                "description": "For generate: the topic/niche for the posts.",
            },
            "count": {
                "type": "NUMBER",
                "description": "For generate: how many posts (1-10, default 3).",
            },
            "content": {
                "type": "STRING",
                "description": "For schedule: the exact post text to publish.",
            },
            "scheduled_for": {
                "type": "STRING",
                "description": "For schedule: optional ISO datetime; omit to publish as soon as possible.",
            },
            "account_ids": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "For schedule: optional account ids; defaults to all connected accounts.",
            },
        },
        "required": [],
    },
    "handler": run_social_media,
}
