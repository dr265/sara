# actions/sara_ultron.py
"""SARA Ultron action - run the Hub's planner/executor from voice mode.

Calls the real backend agent (POST /ultron/run, then GET /ultron/status),
the same routes the Hub workspace uses. The run is real: Ultron plans,
executes tools and re-plans; this action polls until a terminal phase and
reports the real outcome - or honestly says it is still running.
"""
from __future__ import annotations

from typing import Any, Dict

import requests

from core import hub_agents_client as hub

NAME = "SARA Ultron"
DEFAULT_WAIT = 240
POLL_INTERVAL = 3.0
TERMINAL_PHASES = {"completed", "completed_with_issues", "failed", "stopped"}


def _phase_of(snap: Dict[str, Any]) -> str:
    return str(snap.get("phase") or snap.get("status") or "").strip().lower()


def _status_bits(snap: Dict[str, Any]) -> str:
    parts = []
    phase = str(snap.get("phase") or snap.get("status") or "").strip()
    if phase:
        parts.append(phase)
    steps = snap.get("steps")
    if isinstance(steps, list) and steps:
        failed = 0
        for s in steps:
            if isinstance(s, dict):
                st = str(s.get("status") or s.get("state") or "").lower()
                if st in ("failed", "error", "denied"):
                    failed += 1
        parts.append(f"{len(steps)} steps" + (f", {failed} failed" if failed else ""))
    return ", ".join(parts)


def _result_text(snap: Dict[str, Any]) -> str:
    for key in ("summary", "result", "answer", "report", "output", "final"):
        val = snap.get(key)
        if isinstance(val, str) and val.strip():
            return hub.brief(val, 400)
        if isinstance(val, (dict, list)) and val:
            return hub.brief(val, 400)
    steps = snap.get("steps")
    if isinstance(steps, list) and steps:
        last = steps[-1]
        if isinstance(last, dict):
            for key in ("output", "result", "summary", "detail"):
                val = last.get(key)
                if isinstance(val, str) and val.strip():
                    return hub.brief(val, 400)
    return ""


def run_ultron(parameters: dict) -> str:
    p = parameters or {}
    goal = str(p.get("goal") or p.get("task") or p.get("query") or "").strip()
    mode = str(p.get("mode") or "").strip().lower()
    if mode not in ("assisted", "autonomous"):
        mode = "autonomous"

    if not goal:
        return ("Tell me the goal for Ultron - for example: 'run Ultron: research "
                "the best budget laptops and write a summary file'.")
    goal = goal[:4000]

    try:
        resp = hub.post("/ultron/run", {"goal": goal, "mode": mode}, timeout=30)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)

    try:
        started = resp.json()
    except Exception:
        return "Ultron returned an unreadable response - the run was not confirmed."
    task_id = str(started.get("task_id") or "").strip()
    if not task_id:
        return f"Ultron accepted the run but returned no task id ({hub.brief(started, 200)})."

    try:
        wait = int(p.get("wait_seconds") or DEFAULT_WAIT)
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    wait = max(15, min(600, wait))

    def fetch():
        try:
            r = hub.get(f"/ultron/status?task_id={task_id}", timeout=15)
        except requests.exceptions.RequestException as exc:
            return False, {}, hub.unreachable(NAME, exc)
        if r.status_code >= 400:
            return False, {}, hub.http_failure(NAME, r)
        try:
            return True, r.json(), ""
        except Exception:
            return False, {}, "Ultron returned an unreadable status."

    snap, finished, note = hub.poll(
        fetch, lambda s: _phase_of(s) in TERMINAL_PHASES, wait, POLL_INTERVAL)

    bits = _status_bits(snap)
    result = _result_text(snap)
    if finished:
        msg = f"Ultron finished ({bits or _phase_of(snap) or 'completed'})."
        if result:
            msg += f" Result: {result}"
        return msg
    if note == "still running":
        return (f"Ultron is still running after {wait} seconds ({bits or 'in progress'}). "
                f"Task id {task_id[:8]} - ask again later, or watch it on /hub/sara-ultron.")
    return f"Ultron stopped with a problem: {note} Task id {task_id[:8]}."


TOOL = {
    "name": "sara_ultron",
    "description": (
        "Runs SARA Ultron - the Hub's autonomous planner/executor - on an open-ended multi-step goal. "
        "It plans, executes real tools, re-plans around failures and reports the outcome. "
        "Use for complex requests that need several steps, research or orchestration. "
        "For a single quick desktop chore, use the dedicated tool instead. "
        "To OPEN the Ultron workspace page (not run it), use open_agency_workspace."
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
                "description": "Optional: 'autonomous' (default) or 'assisted' (steps need approval in the workspace).",
            },
            "wait_seconds": {
                "type": "NUMBER",
                "description": "How long to wait for completion before reporting progress (15-600, default 240).",
            },
        },
        "required": ["goal"],
    },
    "handler": run_ultron,
}
