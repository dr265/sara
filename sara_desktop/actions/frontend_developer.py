# actions/frontend_developer.py
"""Frontend Developer action - create tasks in the Hub's workspace, by voice.

The Frontend Developer service is its own authenticated web app (mounted at
/frontend-developer). Task creation therefore needs a workspace login. When
the user has stored credentials (SARA_FRONTEND_DEV_USER / _PASSWORD in the
environment or backend/.env), this action signs in for real and creates the
task; otherwise it reports the honest blocker and where to fix it - it never
pretends a task was created.
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "Frontend Developer"
LOGIN_TIMEOUT = 20


def _login(base: str, username: str, password: str) -> str:
    """Returns a bearer token, or '' on failure (registering once if needed)."""
    try:
        resp = hub.post(f"{base}/auth/login",
                        {"username": username, "password": password}, timeout=LOGIN_TIMEOUT)
        if resp.status_code == 200:
            return str(resp.json().get("access_token") or "")
        if resp.status_code == 401:
            # Maybe the account does not exist yet - try a one-time registration.
            hub.post(f"{base}/auth/register",
                     {"username": username, "password": password}, timeout=LOGIN_TIMEOUT)
            resp = hub.post(f"{base}/auth/login",
                            {"username": username, "password": password}, timeout=LOGIN_TIMEOUT)
            if resp.status_code == 200:
                return str(resp.json().get("access_token") or "")
    except requests.exceptions.RequestException:
        raise
    return ""


def _create_task(base: str, title: str, description: str) -> str:
    username = hub.env_value("SARA_FRONTEND_DEV_USER")
    password = hub.env_value("SARA_FRONTEND_DEV_PASSWORD")
    if not (username and password):
        return ("The Frontend Developer workspace needs a login before I can create tasks. "
                "Either sign in at /hub/frontend-developer and add it there, or put "
                "SARA_FRONTEND_DEV_USER and SARA_FRONTEND_DEV_PASSWORD into backend/.env "
                "so I can do it for you. Nothing was created.")

    try:
        token = _login(base, username, password)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if not token:
        return ("The Frontend Developer login for the stored credentials failed. "
                "Check SARA_FRONTEND_DEV_USER / SARA_FRONTEND_DEV_PASSWORD in backend/.env. "
                "Nothing was created.")

    headers = hub.headers()
    headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            hub.url(f"{base}/tasks"),
            json={"title": title[:200], "description": description[:10000]},
            headers=headers,
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    return (f'Created Frontend Developer task "{title[:80]}" - '
            "it is on the board at /hub/frontend-developer.")


def run_frontend_developer(parameters: dict) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    title = str(p.get("title") or p.get("task") or p.get("name") or "").strip()
    description = str(p.get("description") or p.get("details") or "").strip()

    if not action:
        action = "task" if title else "status"

    if action in ("status", "health"):
        health = hub.health("/frontend-developer/health")
        if not health:
            return f"{NAME} is not reachable right now. {hub.BACKEND_HINT}"
        username = hub.env_value("SARA_FRONTEND_DEV_USER")
        creds_note = ("workspace credentials are stored, so I can create tasks"
                      if username else
                      "no workspace credentials stored yet - set SARA_FRONTEND_DEV_USER / "
                      "SARA_FRONTEND_DEV_PASSWORD in backend/.env to let me create tasks")
        return f"{NAME} is online ({creds_note}). Board: /hub/frontend-developer."

    if action in ("task", "create", "add", "ticket"):
        if not title:
            return "Give me a title for the task."
        return _create_task("/frontend-developer", title, description)

    return "Options: action=task (create - needs workspace credentials) or action=status."


TOOL = {
    "name": "frontend_developer",
    "description": (
        "Creates a task on SARA's Frontend Developer workspace board and reports the service "
        "status. Task creation needs workspace credentials (stored in backend/.env); without "
        "them it reports the honest blocker. Use for 'add a task for the frontend developer'. "
        "To OPEN the board (not create), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Optional: 'task' (default when a title is given) or 'status'.",
            },
            "title": {"type": "STRING", "description": "Short task title."},
            "description": {"type": "STRING", "description": "Optional task details."},
        },
        "required": [],
    },
    "handler": run_frontend_developer,
}
