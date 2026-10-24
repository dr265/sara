# actions/sara_auto.py
"""SARA Auto action - the Hub's autonomous desktop-capable agent, by voice.

Calls the real backend agent (POST /sara-auto/run, GET /sara-auto/status) -
the same routes the Hub workspace uses. SARA Auto runs its tool loop with
real outputs fed back to the model; this action polls to a terminal status
and reports the real result honestly.
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "SARA Auto"
DEFAULT_WAIT = 240
POLL_INTERVAL = 3.0
TERMINAL_STATUSES = {"completed", "completed_with_issues", "failed", "stopped"}


def _status_of(snap: Dict[str, Any]) -> str:
    return str(snap.get("status") or snap.get("phase") or "").strip().lower()


def _trace_tail(snap: Dict[str, Any], lines: int = 2) -> str:
    trace = snap.get("trace")
    if not isinstance(trace, list) or not trace:
        return ""
    tail = [str(t) for t in trace[-lines:] if str(t).strip()]
    return hub.brief(" | ".join(tail), 400)


def run_sara_auto(parameters: dict) -> str:
    p = parameters or {}
    goal = str(p.get("goal") or p.get("task") or p.get("query") or "").strip()
    mode = str(p.get("mode") or "").strip().lower()
    if mode not in ("assisted", "autonomous"):
        mode = "autonomous"

    if not goal:
        return ("Tell me the goal for SARA Auto - for example: 'use SARA Auto to "
                "organize my Downloads folder into subfolders'.")
    goal = goal[:4000]

    try:
        resp = hub.post("/sara-auto/run", {"goal": goal, "mode": mode}, timeout=30)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)

    try:
        started = resp.json()
    except Exception:
        return "SARA Auto returned an unreadable response - the run was not confirmed."
    task_id = str(started.get("task_id") or "").strip()
    if not task_id:
        return f"SARA Auto accepted the run but returned no task id ({hub.brief(started, 200)})."

    try:
        wait = int(p.get("wait_seconds") or DEFAULT_WAIT)
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    wait = max(15, min(600, wait))

    def fetch():
        try:
            r = hub.get(f"/sara-auto/status?task_id={task_id}", timeout=15)
        except requests.exceptions.RequestException as exc:
            return False, {}, hub.unreachable(NAME, exc)
        if r.status_code >= 400:
            return False, {}, hub.http_failure(NAME, r)
        try:
            return True, r.json(), ""
        except Exception:
            return False, {}, "SARA Auto returned an unreadable status."

    snap, finished, note = hub.poll(
        fetch, lambda s: _status_of(s) in TERMINAL_STATUSES, wait, POLL_INTERVAL)

    status = _status_of(snap)
    steps = snap.get("steps")
    step_note = ""
    if isinstance(steps, list) and steps:
        step_note = f"{len(steps)} steps"
    tail = _trace_tail(snap)

    if finished:
        msg = f"SARA Auto finished: {status or 'completed'}"
        msg += f" ({step_note})." if step_note else "."
        if tail:
            msg += f" Last activity: {tail}"
        return msg
    if note == "still running":
        return (f"SARA Auto is still running after {wait} seconds"
                f" ({status or 'in progress'}{', ' + step_note if step_note else ''}). "
                f"Task id {task_id[:8]} - ask again later, or watch it on /hub/sara-auto.")
    return f"SARA Auto stopped with a problem: {note} Task id {task_id[:8]}."


TOOL = {
    "name": "sara_auto",
    "description": (
        "Runs SARA Auto - the Hub's autonomous agent with a real tool loop "
        "(desktop actions, files, system) - on an open-ended goal. "
        "Use when the user says 'use SARA Auto' or wants an autonomous multi-step job "
        "on this computer. For complex research/orchestration prefer sara_ultron; "
        "for a single quick chore use the dedicated tool. "
        "To OPEN the SARA Auto workspace page (not run it), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The natural-language goal to achieve (max 4000 characters).",
            },
            "mode": {
                "type": "STRING",
                "description": "Optional: 'autonomous' (default) or 'assisted' (approve steps).",
            },
            "wait_seconds": {
                "type": "NUMBER",
                "description": "How long to wait for completion before reporting progress (15-600, default 240).",
            },
        },
        "required": ["goal"],
    },
    "handler": run_sara_auto,
}
