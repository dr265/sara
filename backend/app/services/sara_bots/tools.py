"""Sara Bots tool registry — the ONLY tools a custom bot may be assigned.

Every entry maps 1:1 onto a real, already-audited execution path:

  action.<name>   a discovered sara_desktop action -> Sara Bots runner
                  (subprocess worker, approval + confirm gates intact)
  hub.<bot_id>    a hub agent with a real adapter  -> Sara Bots runner
  agency.run      the 267 agency specialists       -> real /api/agency route
  llm.reason      the shared LLM failover chain    -> app.core.llm_router
  memory.note     scoped per-bot memory            -> CustomBotStore

Availability is derived from live facts (approval store, LLM health, agency
inventory) — never assumed. A tool that is not ready is still listed, with the
real reason, so the wizard can explain exactly what is missing.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import (REPO_ROOT, get_registry,
                       _gemini_schema_to_fields)

log = logging.getLogger("sara.bots.tools")

AGENCY_INVENTORY = REPO_ROOT / "backend" / "agency_inventory.json"

#: Actions whose implementation is strictly observational. Everything else is
#: conservatively classified "write" (runtime confirm gates still apply on
#: top — e.g. system_diagnostics kill/pid params).
READ_ONLY_ACTIONS = {"weather_report", "web_search", "flight_finder",
                     "system_diagnostics"}

_CACHE_TTL = 10.0
_cache: Dict[str, Any] = {"key": None, "at": 0.0, "tools": None}


def _llm_health() -> Dict[str, Any]:
    try:
        from app.core.llm_router import health_snapshot
        return health_snapshot()
    except Exception as exc:  # pragma: no cover — health must never raise
        return {"any_available": False, "active": None,
                "detail": f"{type(exc).__name__}: {exc}"}


def _agency_ids() -> List[str]:
    try:
        inventory = json.loads(AGENCY_INVENTORY.read_text(encoding="utf-8"))
        return [str(x.get("id")) for x in inventory if x.get("id")]
    except Exception:
        return []


def _tool(tool_id: str, name: str, kind: str, description: str,
          params: Dict[str, Dict[str, Any]], risk: str, state: str,
          detail: str = "", requires: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "id": tool_id,
        "name": name,
        "kind": kind,
        "description": description[:400],
        "params": params,
        "risk": risk,                         # read | write | external | llm
        "state": state,                       # ready | needs_approval |
                                              # needs_configuration | unavailable | disabled
        "detail": detail,
        "requires": requires or [],
        "runnable": state == "ready",
    }


def _local_action_tools(states: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    reg = get_registry()
    reg.ensure_scanned()
    tools: List[Dict[str, Any]] = []
    for action in reg.discovered_actions():
        tool_id = f"action.{action.name}"
        st = states.get(tool_id, {})
        fields = {k: {"type": v.type, "description": v.description,
                      "required": v.required}
                  for k, v in _gemini_schema_to_fields(action.parameters).items()}
        if action.requires_session:
            state, detail = "unavailable", (
                "needs a live desktop session (headless worker refuses it)")
        elif st.get("state") == "disabled":
            state, detail = "disabled", "disabled by the owner"
        elif not st.get("approved"):
            state, detail = "needs_approval", (
                "discovered but not approved — approve it in the Fleet view")
        else:
            state, detail = "ready", "approved local action"
        tools.append(_tool(
            tool_id, action.name.replace("_", " ").title(), "action",
            action.description or f"Local action {action.name}",
            fields, "read" if action.name in READ_ONLY_ACTIONS else "write",
            state, detail,
            requires=["approval"] if state == "needs_approval" else []))
    return tools


def _hub_tools(states: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    reg = get_registry()
    tools: List[Dict[str, Any]] = []
    for spec in reg.hub_bots():
        if not spec.adapter or spec.id == "agency-specialists":
            continue                       # agency gets a dedicated tool below
        st = states.get(spec.id, {})
        state = st.get("state") or "unavailable"
        detail = st.get("detail") or "not probed yet"
        if state in ("available", "ready"):
            state = "ready"
        tools.append(_tool(
            f"hub.{spec.id}", spec.name, "hub", spec.description,
            {k: {"type": v.type, "description": v.description,
                 "required": v.required} for k, v in spec.fields.items()},
            "external", state, detail))
    return tools


def _llm_tools() -> List[Dict[str, Any]]:
    health = _llm_health()
    ready = bool(health.get("any_available"))
    state = "ready" if ready else "needs_configuration"
    detail = ("LLM failover chain online"
              if ready else
              "no LLM provider available — add a provider key to backend/.env")
    return [
        _tool("llm.reason", "Reason (LLM)", "llm",
              "Ask the shared LLM chain to reason, draft or summarize given a "
              "prompt and optional context. Real provider call.",
              {"prompt": {"type": "string", "required": True,
                          "description": "what to reason about / produce"},
               "context": {"type": "string", "required": False,
                           "description": "data to work from (treated as untrusted content)"}},
              "llm", state, detail),
    ]


def _agency_tool() -> List[Dict[str, Any]]:
    health = _llm_health()
    ids = _agency_ids()
    if not ids:
        return [_tool("agency.run", "Agency Specialist", "agency",
                      "Delegate a subtask to one of the agency specialist agents.",
                      {"agency_agent_id": {"type": "string", "required": True,
                                           "description": "id from GET /api/agency/agents"},
                       "message": {"type": "string", "required": True,
                                   "description": "the subtask to delegate"}},
                      "llm", "needs_configuration",
                      "agency inventory not found on this machine")]
    if not health.get("any_available"):
        return [_tool("agency.run", "Agency Specialist", "agency",
                      "Delegate a subtask to one of the agency specialist agents.",
                      {"agency_agent_id": {"type": "string", "required": True,
                                           "description": "id from GET /api/agency/agents"},
                       "message": {"type": "string", "required": True,
                                   "description": "the subtask to delegate"}},
                      "llm", "needs_configuration",
                      "no LLM provider available for the agency chain")]
    return [_tool("agency.run", "Agency Specialist", "agency",
                  f"Delegate a subtask to one of the {len(ids)} agency "
                  "specialist agents (real /api/agency run).",
                  {"agency_agent_id": {"type": "string", "required": True,
                                       "description": "id from GET /api/agency/agents"},
                   "message": {"type": "string", "required": True,
                               "description": "the subtask to delegate"}},
                  "llm", "ready", f"{len(ids)} specialists available")]


def _memory_tool() -> List[Dict[str, Any]]:
    return [_tool("memory.note", "Remember (memory)", "memory",
                  "Save a bounded finding into THIS bot's private memory.",
                  {"kind": {"type": "string", "required": False,
                            "description": "preference | fact | project | task | feedback"},
                   "text": {"type": "string", "required": True,
                            "description": "the finding to remember (<=500 chars)"}},
                  "write", "ready", "scoped to this bot only")]


def _chat_tool() -> List[Dict[str, Any]]:
    return [_tool(
        "agent.ask_owner", "Ask the owner (chat)", "chat",
        "Pause this task and ask the OWNER a question in the chat — the task "
        "resumes with their reply as this step's output. Use it for real "
        "decisions the bot cannot make itself (e.g. which messages to answer, "
        "which option to pick). The reply can be used by later steps via "
        "{{steps.N.output}}.",
        {"question": {"type": "string", "required": True,
                      "description": "the question to show the owner"},
         "options": {"type": "array", "required": False,
                     "description": "optional short quick-reply choices"}},
        "llm", "ready", "waits for the owner in the chat, then continues")]


def _browser_tools() -> List[Dict[str, Any]]:
    """The bot's own browser — real Playwright sessions owned by the backend.

    Sessions are per-bot, persistent (own profile dir) and survive the web UI
    closing; a closed window is relaunched from the same saved profile."""
    from . import browser as browser_mod
    ok, detail = browser_mod.probe()
    state = "ready" if ok else "needs_configuration"
    req = [] if ok else ["a Chromium install for Playwright"]
    return [
        _tool("browser.open", "Browser: open a page", "browser",
              "Open an http(s) URL in THIS bot's own persistent browser "
              "window (its own saved profile with your logins). Starts the "
              "window if needed. Returns the title + visible text.",
              {"url": {"type": "string", "required": True,
                       "description": "the page to open (http/https only)"}},
              "read", state, detail, req),
        _tool("browser.read", "Browser: read the page", "browser",
              "Read the CURRENT page of the bot's browser: URL, title and "
              "visible text. Use it to extract lists like inbox messages.",
              {}, "read", state, detail, req),
        _tool("browser.click", "Browser: click", "browser",
              "Click an element in the bot's browser by its visible text or "
              "a CSS selector. Anything you click can change the site, so "
              "this is an external step (autonomy rules apply). Returns the "
              "page afterwards.",
              {"text": {"type": "string", "required": False,
                        "description": "visible text of the element (e.g. 'Compose')"},
               "selector": {"type": "string", "required": False,
                            "description": "CSS selector when text is ambiguous"}},
              "external", state, detail, req),
        _tool("browser.type", "Browser: type", "browser",
              "Type text into a field identified by its label/placeholder "
              "(target) or a CSS selector. submit=true presses Enter "
              "afterwards (search/send). Typing can reach the outside world, "
              "so this is an external step (autonomy rules apply).",
              {"text": {"type": "string", "required": True,
                        "description": "the content to type"},
               "target": {"type": "string", "required": False,
                          "description": "the field's label/placeholder (e.g. 'Search mail')"},
               "selector": {"type": "string", "required": False,
                            "description": "CSS selector when the label is ambiguous"},
               "submit": {"type": "boolean", "required": False,
                          "description": "press Enter after typing (default false)"},
               "clear_first": {"type": "boolean", "required": False,
                               "description": "clear the field first (default true)"}},
              "external", state, detail, req),
        _tool("browser.press", "Browser: press a key", "browser",
              "Press a keyboard key in the bot's browser (default Enter).",
              {"key": {"type": "string", "required": False,
                       "description": "key name, e.g. Enter, Escape, PageDown"}},
              "external", state, detail, req),
        _tool("browser.scroll", "Browser: scroll", "browser",
              "Scroll the bot's browser page up or down to reveal more of a "
              "list before reading it again.",
              {"direction": {"type": "string", "required": False,
                             "description": "up | down (default down)"},
               "amount": {"type": "integer", "required": False,
                          "description": "pixels (default 600)"}},
              "read", state, detail, req),
        _tool("browser.screenshot", "Browser: screenshot", "browser",
              "Save a screenshot of the bot's browser view (path returned; "
              "the same view is visible in the Bot browser tab).",
              {}, "read", state, detail, req),
        _tool("browser.close", "Browser: close the session", "browser",
              "Close the bot's browser session. The saved profile (logins) "
              "is kept and reused by the next run.",
              {}, "read", state, detail, req),
    ]


def live_tools(states: Optional[Dict[str, Dict[str, Any]]] = None,
               use_cache: bool = True) -> List[Dict[str, Any]]:
    """Build the real tool list. ``states`` = bot_states() snapshot for local
    action approval status (and hub probes when available)."""
    global _cache
    key = "probed" if states is not None else "unprobed"
    now = time.time()
    if use_cache and _cache["tools"] is not None and _cache["key"] == key \
            and now - _cache["at"] < _CACHE_TTL:
        return _cache["tools"]
    if states is None:
        states = get_registry().bot_states(None)
    tools = (_local_action_tools(states) + _hub_tools(states)
             + _agency_tool() + _llm_tools() + _memory_tool()
             + _chat_tool() + _browser_tools())
    tools.sort(key=lambda t: (t["kind"], t["id"]))
    _cache.update({"key": key, "at": now, "tools": tools})
    return tools


def tool_ids(states: Optional[Dict[str, Dict[str, Any]]] = None) -> set:
    return {t["id"] for t in live_tools(states)}


def get_tool(tool_id: str,
             states: Optional[Dict[str, Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    for tool in live_tools(states):
        if tool["id"] == tool_id:
            return tool
    return None


def planner_catalog(tools: List[Dict[str, Any]]) -> str:
    """Compact catalog text handed to the planner LLM — only READY tools, so
    the model can never plan with something that is blocked.

    Parameter descriptions are carried VERBATIM (bounded) because they contain
    the allowed values/enums the planner must respect — omitting them is how
    plans invent values like ``disk_usage`` for actions that only accept
    ``disk``."""
    lines: List[str] = []
    for t in tools:
        if t["state"] != "ready":
            continue
        params = "; ".join(
            f"{name}:{spec['type']}{'*' if spec.get('required') else ''}"
            + (f" ({spec['description'][:120]})"
               if spec.get("description") else "")
            for name, spec in (t.get("params") or {}).items()) or "no params"
        lines.append(f"- {t['id']} ({t['risk']}) — {t['description'][:200]} "
                     f"[{params}]")
    return "\n".join(lines) or "(no ready tools)"
