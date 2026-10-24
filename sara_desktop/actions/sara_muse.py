"""SaraMuse — goals, page monitors, ideas, spending analysis, notifications.

Bridges the desktop to the real SaraMuse service on the backend
(``/api/sara-bots/muse`` — see ``core/sara_muse_client``). Every action is a
real API call; a failure is returned verbatim, never dressed up.
"""

from __future__ import annotations

import json

TOOL = {
    "name": "sara_muse",
    "description": (
        "SaraMuse — the user's goals, page monitors, ideas, spending analysis "
        "and notifications. Use when the user asks to: add a goal or "
        "milestone, watch/monitor a webpage or price, see or refresh ideas, "
        "accept a suggestion, analyse a spending CSV, check muse "
        "notifications, or see the muse overview. "
        "Actions: snapshot | add_goal | add_monitor | monitor_control | "
        "refresh_ideas | decide_idea | finance_analyze | notifications | "
        "mark_read | identity."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "snapshot — overview (default) | add_goal (needs title) | "
                    "add_monitor (needs url; optional title, condition: "
                    "change|contains|price_below, value, interval_minutes) | "
                    "monitor_control (needs monitor_id + control: "
                    "pause|resume|stop|check) | refresh_ideas | decide_idea "
                    "(needs idea_id + decision: accept|dismiss) | "
                    "finance_analyze (needs csv_path) | notifications | "
                    "mark_read (needs notification_id) | identity "
                    "(optional name, tone: warm|concise|thoughtful)"
                ),
            },
            "title": {"type": "STRING", "description": "Goal or monitor title."},
            "description": {"type": "STRING", "description": "Goal description."},
            "milestones": {
                "type": "ARRAY", "items": {"type": "STRING"},
                "description": "Optional goal milestone titles.",
            },
            "url": {"type": "STRING", "description": "Page URL to monitor."},
            "condition": {
                "type": "STRING",
                "description": "Monitor condition: change | contains | price_below",
            },
            "value": {"type": "STRING", "description": "Monitor compare value."},
            "interval_minutes": {
                "type": "INTEGER", "description": "Monitor check interval (minutes).",
            },
            "monitor_id": {"type": "STRING"},
            "control": {"type": "STRING", "description": "pause | resume | stop | check"},
            "idea_id": {"type": "STRING"},
            "decision": {"type": "STRING", "description": "accept | dismiss"},
            "csv_path": {"type": "STRING", "description": "Local CSV file path."},
            "notification_id": {"type": "STRING"},
            "name": {"type": "STRING", "description": "Muse identity name."},
            "tone": {"type": "STRING", "description": "warm | concise | thoughtful"},
        },
        "required": [],
    },
    "handler": None,   # bound below (module-level name required by loader)
}


def _fmt_overview(snap: dict) -> str:
    goals = snap.get("goals") or []
    monitors = snap.get("monitors") or []
    ideas = snap.get("ideas") or []
    notifs = snap.get("notifications") or []
    unread = [n for n in notifs if not n.get("read")]
    active_goals = [g for g in goals if g.get("status") == "active"]
    active_mon = [m for m in monitors if m.get("status") == "active"]
    lines = [
        f"SaraMuse: {len(goals)} goal(s) ({len(active_goals)} active), "
        f"{len(monitors)} monitor(s) ({len(active_mon)} active), "
        f"{len(ideas)} idea(s), {len(unread)} unread notification(s).",
    ]
    for g in active_goals[:4]:
        lines.append(f"  goal: {str(g.get('title') or '')[:70]}")
    for m in active_mon[:4]:
        lines.append(f"  watch: {str(m.get('title') or '')[:50]} → "
                     f"{str(m.get('url') or '')[:60]}")
    for n in unread[:3]:
        lines.append(f"  notif: {str(n.get('title') or n.get('message') or n.get('text') or '')[:80]}")
    return "\n".join(lines)


def sara_muse(parameters: dict | None = None, response=None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "snapshot").strip().lower()
    try:
        from core.sara_muse_client import (add_goal, add_monitor,
                                           analyze_finance, control_monitor,
                                           decide_idea, mark_read,
                                           notifications, refresh_ideas,
                                           set_identity, snapshot)
        import requests as _requests
    except Exception as exc:
        return f"SaraMuse is unavailable on this desktop: {exc}"

    try:
        if action == "snapshot":
            return _fmt_overview(snapshot())

        if action == "add_goal":
            title = str(params.get("title") or "").strip()
            if not title:
                return "A goal needs a title — what should the goal be called?"
            goal = add_goal(None, title,
                            str(params.get("description") or ""),
                            str(params.get("category") or "Personal"),
                            [str(m) for m in (params.get("milestones") or [])])
            return (f"Goal created: '{str(goal.get('title') or title)[:70]}' "
                    f"(id {goal.get('id', '?')}, "
                    f"{len(goal.get('milestones') or [])} milestone(s)).")

        if action == "add_monitor":
            url = str(params.get("url") or "").strip()
            if not url:
                return "A page monitor needs the exact URL to watch."
            monitor = add_monitor(
                None, str(params.get("title") or url)[:160], url,
                str(params.get("condition") or "change"),
                str(params.get("value") or ""),
                int(params.get("interval_minutes") or 15))
            return (f"Monitor created: '{str(monitor.get('title') or url)[:60]}' "
                    f"→ {url[:90]} "
                    f"(every {monitor.get('intervalMinutes', '?')} min, "
                    f"id {monitor.get('id', '?')}).")

        if action == "monitor_control":
            monitor_id = str(params.get("monitor_id") or "").strip()
            if not monitor_id:
                return "Which monitor? I need its id (see the muse overview)."
            out = control_monitor(None, monitor_id,
                                  str(params.get("control") or "check"))
            return (f"Monitor {monitor_id}: "
                    f"{str(out.get('status') or params.get('control') or 'done')}.")

        if action == "refresh_ideas":
            ideas = refresh_ideas()
            if not ideas:
                return "Muse refreshed ideas — none are pending right now."
            lines = [f"{len(ideas)} idea(s):"]
            for idea in ideas[:5]:
                lines.append(f"  [{idea.get('id', '?')}] "
                             f"{str(idea.get('title') or idea.get('text') or '')[:90]}")
            return "\n".join(lines)

        if action == "decide_idea":
            idea_id = str(params.get("idea_id") or "").strip()
            if not idea_id:
                return "Which idea? I need its id (run refresh_ideas first)."
            out = decide_idea(None, idea_id,
                              str(params.get("decision") or "dismiss"))
            return (f"Idea {idea_id}: {str(out.get('status') or params.get('decision'))}. "
                    + ("A real task was submitted to the SaraMuse bot."
                       if str(params.get("decision")) == "accept" else ""))

        if action == "finance_analyze":
            csv_path = str(params.get("csv_path") or "").strip()
            if not csv_path:
                return "Give me the CSV file path to analyse (with columns like date, description, amount)."
            try:
                text = open(csv_path, "r", encoding="utf-8", errors="replace").read()
            except Exception as exc:
                return f"Could not read '{csv_path}': {exc}"
            payload = analyze_finance(None, text)
            data = payload.get("analysis") or {}
            return (f"Spending analysed: {data.get('count', '?')} transactions, "
                    f"{data.get('spending', '?')} spent"
                    + (f", {data.get('income')} income"
                       if data.get("income") is not None else "")
                    + (f", top category: {data.get('top_category') or data.get('topCategory')}"
                       if (data.get("top_category") or data.get("topCategory")) else "")
                    + ".")

        if action == "notifications":
            notifs = notifications()
            if not notifs:
                return "No muse notifications."
            lines = [f"{len(notifs)} notification(s):"]
            for n in notifs[:6]:
                mark = "•" if n.get("read") else "NEW"
                lines.append(f"  [{mark}] {str(n.get('title') or n.get('message') or n.get('text') or '')[:90]}")
            return "\n".join(lines)

        if action == "mark_read":
            notif_id = str(params.get("notification_id") or "").strip()
            if not notif_id:
                return "Which notification? I need its id."
            mark_read(None, notif_id)
            return f"Notification {notif_id} marked read."

        if action == "identity":
            out = set_identity(None, name=params.get("name"),
                               tone=params.get("tone"))
            return f"Muse identity updated: {json.dumps(out, default=str)[:200]}"

        return (f"Unknown SaraMuse action '{action}'. Use snapshot, add_goal, "
                f"add_monitor, monitor_control, refresh_ideas, decide_idea, "
                f"finance_analyze, notifications, mark_read or identity.")
    except _requests.HTTPError as exc:
        detail = ""
        try:
            detail = str(exc.response.json().get("detail") or "")[:200]
        except Exception:
            detail = str(exc)[:200]
        return f"SaraMuse rejected the request: {detail or exc}"
    except Exception as exc:
        return f"SaraMuse call failed: {type(exc).__name__}: {exc}"


TOOL["handler"] = sara_muse
