"""
core/sara_muse_client.py — SaraMuse client for SARA Desktop.

SaraMuse (the OpenMuse-inspired powers surface) lives on the backend at
``/api/sara-bots/muse``: goals + milestones, page monitors that fetch real
public pages on a schedule, ideas derived from real records (accepting one
submits a REAL Sara Bots task), deterministic CSV spending analysis,
notifications and identity. This module is the desktop's typed access to
exactly those routes — every call is a real request; HTTP errors propagate
so callers can report them honestly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from core.agent_registry import BackendConnection

BASE_PATH = "/api/sara-bots/muse"


def _conn(conn: Optional[BackendConnection]) -> BackendConnection:
    return conn or BackendConnection()


def _get(conn: BackendConnection, path: str,
         timeout: float = 20.0) -> Any:
    resp = requests.get(f"{conn.base_url}{BASE_PATH}{path}",
                        headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _post(conn: BackendConnection, path: str, body: Optional[Dict[str, Any]] = None,
          timeout: float = 30.0) -> Any:
    resp = requests.post(f"{conn.base_url}{BASE_PATH}{path}",
                         headers=conn.headers(), timeout=timeout,
                         json=body or {})
    resp.raise_for_status()
    return resp.json()


# ── workspace ────────────────────────────────────────────────────────────

def snapshot(conn: Optional[BackendConnection] = None) -> Dict[str, Any]:
    return _get(_conn(conn), "/") or {}


# ── goals ────────────────────────────────────────────────────────────────

def add_goal(conn: Optional[BackendConnection], title: str,
             description: str = "", category: str = "Personal",
             milestones: Optional[List[str]] = None) -> Dict[str, Any]:
    return _post(_conn(conn), "/goals", {
        "title": str(title)[:160],
        "description": str(description)[:4000],
        "category": str(category)[:80] or "Personal",
        "milestones": [str(m)[:200] for m in (milestones or [])][:20],
    })


def update_goal(conn: Optional[BackendConnection], goal_id: str,
                status: str) -> Dict[str, Any]:
    return _post(_conn(conn), f"/goals/{goal_id}", {"status": status})


# ── monitors (page watches) ──────────────────────────────────────────────

def add_monitor(conn: Optional[BackendConnection], title: str, url: str,
                condition: str = "change", value: str = "",
                interval_minutes: int = 15) -> Dict[str, Any]:
    return _post(_conn(conn), "/monitors", {
        "title": str(title)[:160],
        "url": str(url)[:4096],
        "condition": condition if condition in
        ("change", "contains", "price_below") else "change",
        "value": str(value)[:300],
        "intervalMinutes": max(1, min(10080, int(interval_minutes or 15))),
    })


def control_monitor(conn: Optional[BackendConnection], monitor_id: str,
                    action: str) -> Dict[str, Any]:
    act = action if action in ("pause", "resume", "stop", "check") else "check"
    return _post(_conn(conn), f"/monitors/{monitor_id}/control",
                 {"action": act})


# ── ideas ────────────────────────────────────────────────────────────────

def refresh_ideas(conn: Optional[BackendConnection] = None) -> List[Dict[str, Any]]:
    payload = _post(_conn(conn), "/ideas/refresh")
    return list((payload or {}).get("ideas") or [])


def decide_idea(conn: Optional[BackendConnection], idea_id: str,
                action: str, prompt: Optional[str] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"action": action if action in
                            ("accept", "dismiss") else "dismiss"}
    if prompt:
        body["prompt"] = str(prompt)[:12000]
    return _post(_conn(conn), f"/ideas/{idea_id}", body)


# ── finance ──────────────────────────────────────────────────────────────

def analyze_finance(conn: Optional[BackendConnection],
                    csv_text: str) -> Dict[str, Any]:
    return _post(_conn(conn), "/finance/analyze",
                 {"csv": str(csv_text)[:500_000]})


# ── notifications ────────────────────────────────────────────────────────

def notifications(conn: Optional[BackendConnection] = None) -> List[Dict[str, Any]]:
    payload = _get(_conn(conn), "/notifications")
    return list((payload or {}).get("notifications") or [])


def mark_read(conn: Optional[BackendConnection], notif_id: str) -> Dict[str, Any]:
    return _post(_conn(conn), f"/notifications/{notif_id}/read")


# ── identity ─────────────────────────────────────────────────────────────

def set_identity(conn: Optional[BackendConnection], **fields: Any) -> Dict[str, Any]:
    body = {k: v for k, v in fields.items()
            if k in ("name", "tone", "avatar", "showChatUpdates")
            and v not in (None, "")}
    return _post(_conn(conn), "/identity", body)
