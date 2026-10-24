# actions/backend_architect.py
"""Backend Architect action - run the analysis agent from voice mode.

Calls the real backend agent (POST /backend-architect/api/start, then
streams GET /backend-architect/api/events) - the same routes the Hub
workspace uses. The agent inspects the repo, gathers evidence and writes an
evidence-backed report file; this action waits, relays real log lines and
reports the final status honestly (including "needs approval").
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "Backend Architect"
DEFAULT_WAIT = 180
READ_TIMEOUT = 60


def analyse_repo(parameters: dict) -> str:
    p = parameters or {}
    task = str(p.get("task") or p.get("goal") or p.get("query") or "").strip()
    if len(task) < 3:
        return ("Tell me the architecture task - for example: 'review the authentication "
                "module and report risks'.")
    task = task[:2000]
    repo = str(p.get("repo") or p.get("path") or ".").strip() or "."

    try:
        resp = hub.post("/backend-architect/api/start",
                        {"task": task, "repo": repo}, timeout=30)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        started = resp.json()
    except Exception:
        return "Backend Architect returned an unreadable response - the run was not confirmed."
    session_id = str(started.get("session_id") or "").strip()
    if not session_id:
        return f"Backend Architect accepted the task but returned no session id ({hub.brief(started, 200)})."

    try:
        wait = int(p.get("wait_seconds") or DEFAULT_WAIT)
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    wait = max(15, min(600, wait))

    logs: List[str] = []
    approvals = 0
    final_status = ""
    deadline = time.time() + wait

    try:
        with requests.get(
            hub.url(f"/backend-architect/api/events?session_id={session_id}"),
            headers=hub.headers(),
            stream=True,
            timeout=(15, READ_TIMEOUT),
        ) as stream:
            if stream.status_code >= 400:
                return hub.http_failure(NAME, stream)
            for raw in stream.iter_lines(decode_unicode=True):
                if time.time() > deadline:
                    break
                if raw is None:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = str(event.get("type") or "")
                if etype == "done":
                    final_status = str(event.get("status") or "").strip().lower()
                    break
                if etype == "log":
                    msg = str(event.get("message") or "").strip()
                    if msg:
                        logs.append(msg)
                elif etype == "approval_request":
                    approvals += 1
    except requests.exceptions.ReadTimeout:
        pass  # agent is thinking with no events - report what we have
    except requests.exceptions.RequestException as exc:
        if not logs and not final_status:
            return hub.unreachable(NAME, exc)

    tail = ""
    if logs:
        tail = hub.brief(" | ".join(logs[-2:]), 400)

    if final_status in ("completed", "done", "success"):
        msg = "Backend Architect finished its analysis."
        if tail:
            msg += f" Last log: {tail}"
        return msg
    if final_status in ("failed", "error"):
        if logs and len(" ".join(logs)) > 200:
            return ("Backend Architect produced an analysis but did not self-finalize "
                    "(step limit) - open /hub/backend-architect for the full evidence-backed report. "
                    f"Last log: {tail}")
        return f"Backend Architect failed: {tail or 'see the workspace for details'}."
    if approvals:
        return (f"Backend Architect is waiting for your approval before continuing - "
                f"open /hub/backend-architect and approve (session {session_id[:8]}). "
                + (f"Last log: {tail}" if tail else ""))
    if final_status:
        msg = f"Backend Architect ended with status: {final_status}."
        if tail:
            msg += f" Last log: {tail}"
        return msg
    if logs:
        return (f"Backend Architect is still working after {wait} seconds - last log: {tail}. "
                f"Session {session_id[:8]} - watch or approve it on /hub/backend-architect.")
    return (f"Backend Architect started (session {session_id[:8]}) but produced no events yet. "
            "Check /hub/backend-architect shortly.")


TOOL = {
    "name": "backend_architect",
    "description": (
        "Runs SARA's Backend Architect agent on a codebase task: it inspects a repo, gathers "
        "evidence and writes an architecture report. Use for 'review my backend', 'analyze the "
        "auth module', 'check the API design'. It may ask for approval mid-run - that stays a "
        "human step on the workspace page. "
        "To OPEN the workspace (not run it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "The analysis task, e.g. 'review the authentication module and report risks'.",
            },
            "repo": {
                "type": "STRING",
                "description": "Optional repo/project path (default '.').",
            },
            "wait_seconds": {
                "type": "NUMBER",
                "description": "How long to wait before reporting progress (15-600, default 180).",
            },
        },
        "required": ["task"],
    },
    "handler": analyse_repo,
}
