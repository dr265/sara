"""Sara Bots factory — plain-English description -> validated bot proposal.

The model proposes; the SERVER validates. The proposal is built exclusively
from tools that are READY right now (an unapproved local action can never be
sneaked into a new bot by the LLM), and anything the model asked for but that
does not exist is reported back honestly in ``dropped`` / ``missing`` — the
wizard shows it before the user saves.

The user is always the final authority: the proposal is only a pre-filled
form; creation goes through the normal validated POST /custom path.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from .custom import AUTONOMY_MODES, BOT_CATEGORIES, PLAN_MODES
from .planner import _extract_json_object
from .tools import live_tools

log = logging.getLogger("sara.bots.factory")

FACTORY_TIMEOUT_S = 90.0
_MAX_TOOLS_IN_PROPOSAL = 8

_SYSTEM = (
    "You are the bot factory inside a self-hosted agent platform. From a "
    "plain-English description you design ONE autonomous worker bot.\n"
    "Reply with ONE JSON object, nothing else:\n"
    "{\n"
    "  \"name\": str (2-60 chars),\n"
    "  \"description\": str (what the bot is for),\n"
    "  \"goal\": str (the default goal it works on),\n"
    "  \"instructions\": str (how it should behave, in second person),\n"
    "  \"category\": one of " + json.dumps(list(BOT_CATEGORIES)) + ",\n"
    "  \"autonomy\": one of " + json.dumps(list(AUTONOMY_MODES)) +
    " (assist = ask before every write/external step, supervised = ask before "
    "external steps, autonomous = run within the assigned tools),\n"
    "  \"plan_mode\": \"auto\" (LLM plans the steps) or \"direct\" (a single "
    "pre-configured tool call),\n"
    "  \"tools\": [ids from the READY TOOLS list, most useful first, max 8],\n"
    "  \"direct_tool\": str or null (only for direct mode),\n"
    "  \"direct_params\": object or null (only for direct mode),\n"
    "  \"output_format\": str (what a finished task should look like),\n"
    "  \"memory_enabled\": bool,\n"
    "  \"schedule\": {\"interval_minutes\": int (5-10080), \"goal\": str} or null,\n"
    "  \"missing\": [capability strings the listed tools CANNOT provide]\n"
    "}\n"
    "Rules: use ONLY tool ids from the list; never invent tools; if a "
    "capability is not covered, say so in \"missing\" instead of faking it; "
    "prefer read-only tools plus one concrete action; if one tool fully "
    "covers the job use direct mode."
)


def _clean_proposal(raw: Dict[str, Any],
                    ready: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Server-side validation/normalisation of whatever the model produced."""
    dropped: List[Dict[str, str]] = []

    name = str(raw.get("name", "")).strip()[:60] or "New Bot"
    description = str(raw.get("description", "")).strip()[:400]
    goal = str(raw.get("goal", "")).strip()[:2000]
    instructions = str(raw.get("instructions", "")).strip()[:8000]
    output_format = str(raw.get("output_format", "")).strip()[:200]

    category = str(raw.get("category", "custom")).strip().lower()
    if category not in BOT_CATEGORIES:
        category = "custom"
    autonomy = str(raw.get("autonomy", "supervised")).strip().lower()
    if autonomy not in AUTONOMY_MODES:
        autonomy = "supervised"
    plan_mode = str(raw.get("plan_mode", "auto")).strip().lower()
    if plan_mode not in PLAN_MODES:
        plan_mode = "auto"

    tools_raw = raw.get("tools") or []
    if not isinstance(tools_raw, list):
        tools_raw = []
    tools: List[str] = []
    for entry in tools_raw:
        tool_id = str(entry).strip()
        if tool_id in ready:
            if tool_id not in tools:
                tools.append(tool_id)
        elif tool_id:
            # requested but not usable right now — report the real reason
            from .tools import get_tool
            tool = get_tool(tool_id)
            reason = (f"{tool['state']}: {tool['detail']}" if tool
                      else "not present in the live registry")
            dropped.append({"tool": tool_id, "reason": reason})
        if len(tools) >= _MAX_TOOLS_IN_PROPOSAL:
            break

    direct_tool = str(raw.get("direct_tool") or "").strip()
    direct_params = raw.get("direct_params") or {}
    if plan_mode == "direct" and (direct_tool not in tools or
                                  not isinstance(direct_params, dict)):
        plan_mode = "auto"          # model misdesigned it — fall back safely
        direct_tool, direct_params = "", {}
        dropped.append({"tool": "direct mode misconfigured",
                        "reason": "fell back to LLM planning (auto)"})
    if plan_mode != "direct":
        direct_tool, direct_params = "", {}

    schedule = raw.get("schedule")
    clean_schedule: Dict[str, Any] | None = None
    if isinstance(schedule, dict):
        try:
            minutes = int(schedule.get("interval_minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        sched_goal = str(schedule.get("goal") or goal).strip()[:2000]
        if 5 <= minutes <= 10080 and sched_goal:
            clean_schedule = {"interval_minutes": minutes,
                              "goal": sched_goal}

    missing = [str(m)[:200] for m in (raw.get("missing") or [])
               if isinstance(m, str) and m.strip()][:10]

    return {
        "name": name,
        "description": description or goal or "Custom worker bot",
        "goal": goal or description or name,
        "instructions": instructions,
        "category": category,
        "autonomy": autonomy,
        "plan_mode": plan_mode,
        "tools": tools,
        "direct_tool": direct_tool,
        "direct_params": direct_params,
        "output_format": output_format,
        "memory_enabled": bool(raw.get("memory_enabled", True)),
        "schedule": clean_schedule,
        "missing": missing,
        "dropped": dropped,
    }


async def propose(description: str,
                  model: str | None = None) -> Dict[str, Any]:
    """description -> {"ok", "proposal"} or {"ok": False, "error"}."""
    from app.core.llm_router import llm_chat

    description = (description or "").strip()
    if not description:
        return {"ok": False, "error": "describe what the bot should do"}
    if len(description) > 2000:
        return {"ok": False, "error": "description exceeds 2000 characters"}

    ready = {t["id"]: t for t in live_tools() if t["state"] == "ready"}
    if not ready:
        return {"ok": False,
                "error": "no tools are ready right now — approve local "
                         "actions or configure a provider first"}
    catalog = "\n".join(
        f"- {t['id']} ({t['risk']}) — {t['description'][:140]} "
        f"[{', '.join((t.get('params') or {}).keys()) or 'no params'}]"
        for t in ready.values())
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",
         "content": f"USER DESCRIPTION:\n{description}\n\nREADY TOOLS:\n"
                    f"{catalog}"},
    ]
    try:
        reply = await llm_chat(messages, model=model or None,
                               max_tokens=1600, temperature=0.3,
                               timeout=FACTORY_TIMEOUT_S, strict=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False,
                "error": f"LLM unavailable: {type(exc).__name__}: "
                         f"{str(exc)[:300]}"}
    parsed = _extract_json_object(reply or "")
    if parsed is None:
        return {"ok": False, "error": "the model reply was not valid JSON"}
    proposal = _clean_proposal(parsed, ready)
    if not proposal["tools"]:
        return {"ok": False,
                "error": "the model could not match this description to any "
                         "ready tool — refine the description",
                "proposal": proposal}
    return {"ok": True, "proposal": proposal,
            "ready_tools": len(ready)}
