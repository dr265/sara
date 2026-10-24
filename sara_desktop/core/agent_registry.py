"""
core/agent_registry.py — the unified agent registry for Jarvis Agentic Mode.

One place that knows every agent Jarvis can drive:

  * desktop actions   — actions/*.py on this machine (auto-discovered by
                        core.action_loader, already validated at startup)
  * backend agents    — served by the unified SARA backend (run_backend.py).
                        Discovered through the platform's OWN fleet endpoint
                        ``GET /ultron/agents``, which runs REAL HTTP health
                        probes of every Hub agent (backend core/fleet.py).
  * plugins           — core.plugin_loader drop-ins
  * agency agents     — the 267 specialist .md agents, matched locally
                        against backend/agency_inventory.json (read-only)
  * ultron natives    — plan/execute/replan/delegate capabilities

HONEST STATES (spec §4 — registering ≠ working)
    registered | ready | running | completed | failed | blocked | unavailable

    The raw probe word from the fleet (online / degraded / unreachable /
    no_probe) is preserved in ``status``; ``state`` is the normalized value
    the UI and router consume. Health payloads are additionally inspected for
    real config gaps (``configured:false``, ``smtp:false``, ``llm:offline``…)
    so a reachable-but-uncredentialed agent reports BLOCKED, not ready.

SCOPE — Jarvis side only. This module READS backend/.env (same parsing as
setup_keys.py) for the base URL and shared API key; it never modifies the
backend, the agents, or the website.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

log = logging.getLogger("sara.agent_registry")

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent


class AgentState(str, Enum):
    """Normalized lifecycle states required by the integration spec."""
    REGISTERED  = "registered"   # known; never proven live
    READY       = "ready"        # probed live, no blocking config gap
    RUNNING     = "running"      # currently executing a task
    COMPLETED   = "completed"    # finished its last task successfully
    FAILED      = "failed"       # its last execution errored
    BLOCKED     = "blocked"      # reachable but missing a credential
    UNAVAILABLE = "unavailable"  # endpoint unreachable right now


#: Fleet probe word (GET /ultron/agents) -> normalized state.
#: "degraded" is provisional: config_gap() may refine it.
_FLEET_STATE = {
    "online":      AgentState.READY,
    "healthy":     AgentState.READY,
    "degraded":    AgentState.BLOCKED,
    "unreachable": AgentState.UNAVAILABLE,
    "offline":     AgentState.UNAVAILABLE,
    "no_probe":    AgentState.REGISTERED,
    "unknown":     AgentState.REGISTERED,
}


@dataclass
class AgentInfo:
    """Everything the router and the desktop panel need about one agent."""
    #: Canonical unique id — ALSO the dict key inside the registry.
    id: str
    name: str
    category: str
    description: str
    #: Capability keywords the router matches spoken requests against.
    capabilities: List[str]
    source: str            # desktop_action | backend_agent | plugin | ultron_native | agency
    route: Optional[str] = None          # Hub route ("/hub/sara-ultron")
    #: Raw word from the last real probe (online/degraded/unreachable/no_probe).
    status: str = "unknown"
    #: Normalized spec state — see AgentState.
    state: str = AgentState.REGISTERED.value
    detail: str = ""                       # human line from the last check
    health_endpoint: Optional[str] = None  # absolute URL or None
    # ── execution interface ────────────────────────────────────────────────
    invoke_path: str = ""                  # POST path ("" = not directly invocable)
    invoke_async: bool = False             # POST returns a task_id to poll
    accepts: Dict[str, Any] = field(default_factory=dict)
    output_format: str = ""
    requires: List[str] = field(default_factory=list)
    cancellable: bool = False
    #: True when normal operation sends/publishes/deletes externally — the
    #: router gates those behind human confirmation (spec §11).
    consequential: bool = False
    tools: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_health_check: float = 0.0
    health_error: Optional[str] = None
    # ── runtime task state ─────────────────────────────────────────────────
    current_task_id: str = ""
    last_error: str = ""

    @property
    def invocable(self) -> bool:
        """May the router execute this agent right now?"""
        if self.id in NON_SELECTABLE or self.source == "ultron_native":
            return False
        if self.source in ("desktop_action", "plugin"):
            return self.state != AgentState.UNAVAILABLE.value
        if not self.invoke_path:
            return False      # probed but no usable execution endpoint
        return self.state in (AgentState.READY.value,
                              AgentState.RUNNING.value,
                              AgentState.COMPLETED.value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "description": self.description, "capabilities": list(self.capabilities),
            "source": self.source, "route": self.route,
            "status": self.status, "state": self.state, "detail": self.detail,
            "health_endpoint": self.health_endpoint,
            "invoke_path": self.invoke_path, "invoke_async": self.invoke_async,
            "accepts": self.accepts, "output_format": self.output_format,
            "requires": list(self.requires), "cancellable": self.cancellable,
            "consequential": self.consequential, "tools": list(self.tools),
            "invocable": self.invocable,
            "last_health_check": self.last_health_check,
            "health_error": self.health_error,
            "current_task_id": self.current_task_id,
            "last_error": self.last_error,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Static execution metadata, keyed by the Hub agent id the fleet endpoint
# returns. Capabilities, request shapes and consequence flags are NOT part of
# the fleet probe (it only reports liveness), so they are declared here — from
# the real backend routes verified in backend/app/*.py while this was written.
# Liveness stays 100% dynamic; this table only says HOW to invoke an agent
# that the probe already proved exists.
# ─────────────────────────────────────────────────────────────────────────────

INVOKE_SPEC: Dict[str, Dict[str, Any]] = {
    "sara-ultron": {
        "capabilities": ["orchestration", "multi-agent", "workflow", "planning",
                         "execute plan", "delegate", "complex task", "coordinate"],
        "invoke_path": "/ultron/run", "invoke_async": True,
        "accepts": {"goal": "string — natural-language goal (<=4000 chars), "
                            "optional 'mode': assisted|autonomous"},
        "output_format": "POST -> {task_id}; poll GET /ultron/status?task_id=… "
                         "{phase, plan, steps}; stop via POST /ultron/stop",
        "cancellable": True,
        "notes": "Preferred entry for open-ended multi-step requests: it plans, "
                 "executes real tools, re-plans around failures and delegates.",
    },
    "sara-auto": {
        "capabilities": ["automation", "autonomous", "task", "desktop", "goal"],
        "invoke_path": "/sara-auto/run", "invoke_async": True,
        "accepts": {"goal": "string", "mode": "'autonomous' (default)"},
        "output_format": "POST -> {task_id}; GET /sara-auto/status",
        "cancellable": True,
    },
    "ai-web-scraper": {
        "capabilities": ["scrape", "website", "web", "extract", "crawl",
                         "research", "summarize page", "data extraction"],
        "invoke_path": "/api/agent", "invoke_async": False,
        "accepts": {"url": "single URL (legacy)", "urls": "list of URLs",
                    "prompt": "REQUIRED — what to extract",
                    "mode": "single|crawl|pagination", "maxPages": "int (5)"},
        "output_format": "SSE stream; terminal event carries the extracted result",
    },
    "music-studio": {
        "capabilities": ["music", "song", "beat", "generate music", "audio"],
        "invoke_path": "/api/music/generate", "invoke_async": False,
        "accepts": {"prompt": "REQUIRED, 3..4000 chars — musical brief",
                    "genre": "e.g. Cinematic", "mood": "e.g. Epic",
                    "duration": "15..600 seconds", "vocal": "bool",
                    "variations": "1..4"},
        "output_format": "JSON with created track ids/urls (synchronous)",
        "requires": ["LLM key (health payload reports it)"],
    },
    "email-automation": {
        "capabilities": ["email", "campaign", "send email", "draft email",
                         "outreach", "newsletter", "mail"],
        "invoke_path": "/email-agent/campaigns", "invoke_async": False,
        "accepts": {"name": "campaign name", "subject": "email subject",
                    "context": "background material", "instructions": "tone/rules",
                    "objective": "campaign goal", "dry_run": "bool (default true — "
                    "draft without sending; set false only after confirmation)",
                    "provider": "smtp|gmail",
                    "then": "/campaigns/{id}/upload clients, /start, /pause"},
        "output_format": "campaign id + per-client Sent/Failed statuses",
        "requires": ["GMAIL_SENDER/SMTP_USERNAME + GMAIL_APP_PASSWORD to send"],
        "consequential": True,
        "notes": "Health 'smtp:true' only when a real send could authenticate.",
    },
    "social-media": {
        "capabilities": ["social", "post", "publish", "schedule", "content",
                         "instagram", "twitter", "linkedin", "facebook", "tiktok"],
        "invoke_path": "/social/schedule", "invoke_async": False,
        "accepts": {"account_ids": "REQUIRED list of connected account ids "
                                   "(GET /social/accounts)",
                    "content": "post text", "content_kind": "text|image|video",
                    "scheduled_for": "ISO time or null (now)",
                    "recurrence": "none|daily|weekly|…", "timezone": "UTC"},
        "output_format": "{status: scheduled, job info}; GET /social/jobs",
        "requires": ["Connected accounts (OAuth tokens)"],
        "consequential": True,
    },
    "whatsapp-agent": {
        "capabilities": ["whatsapp", "message", "send whatsapp", "chat", "text"],
        "invoke_path": "", "invoke_async": False,
        "accepts": {"transport": "outbound via configured transport "
                               "(Unipile/Zernio)"},
        "output_format": "engine API; transport outbound send",
        "requires": ["UNIPILE_DSN+token or ZERNIO_API_KEY, LLM_API_KEY"],
        "consequential": True,
        "notes": "Health 'configured:false' means no transport — sending is "
                 "impossible and the registry reports BLOCKED.",
    },
    "trading-agent": {
        "capabilities": ["trading", "market", "signals", "backtest", "stocks",
                         "analyze market"],
        "invoke_path": "", "invoke_async": False,
        "accepts": {"note": "exposed routes replay CSV/demo backtests "
                            "(/trading/backtest, /trading/text, /trading/demo) — "
                            "no free-text market analysis endpoint exists"},
        "output_format": "JSON backtest result (when fed CSV/demo data)",
        "consequential": True,
        "notes": "Visible and probed, but NOT selectable for spoken analysis "
                 "requests: no matching execution endpoint would 422.",
    },
    "telegram-assistant": {
        "capabilities": ["telegram", "broadcast", "group", "community", "bot"],
        "invoke_path": "", "invoke_async": False,
        "accepts": {"bot operations through the configured bot"},
        "output_format": "control API (/telegram/*)",
        "requires": ["bot token (status 'bot_token_set')"],
        "consequential": True,
    },
    "voice-agent": {
        "capabilities": ["call", "phone call", "voice call", "telephony", "twilio"],
        "invoke_path": "", "invoke_async": False,
        "accepts": {"call workflows via Twilio channels"},
        "output_format": "voice channel API",
        "requires": ["TWILIO_ACCOUNT_SID/AUTH_TOKEN + Twilio numbers"],
        "consequential": True,
    },
    "edulearner-ai": {
        "capabilities": ["learn", "study", "teach", "roadmap", "course",
                         "exam", "skill gap", "education", "practice"],
        "invoke_path": "", "invoke_async": False,
        "accepts": {"requests": "learner questions, plan/topic requests"},
        "output_format": "JSON (profile, gap analysis, roadmap, practice)",
        "requires": ["LLM key (health 'llm' field)"],
    },
    "game-forge": {
        "capabilities": ["game", "game dev", "prototype", "sprite", "assets"],
        "invoke_path": "/api/gameforge/games", "invoke_async": True,
        "accepts": {"prompt": "REQUIRED — what game to build",
                    "resolution": "e.g. 1280x720"},
        "output_format": "202 job id; GET /api/gameforge/jobs/{id}",
        "requires": ["LLM key in backend/.env", "pygame in backend venv"],
    },
    "backend-architect": {
        "capabilities": ["backend", "api design", "architecture", "database schema",
                         "code review", "repo"],
        "invoke_path": "/backend-architect/api/start", "invoke_async": True,
        "accepts": {"task": "REQUIRED — analysis task", "repo": "repo path ('.')"},
        "output_format": "session id; GET /backend-architect/api/events + report",
        "requires": ["LLM key (ARCHITECT_API_KEY / LLM_API_KEY)"],
    },
    "frontend-developer": {
        "capabilities": ["frontend", "react", "ui", "interface", "web design",
                         "next.js"],
        "invoke_path": "/frontend-developer/tasks", "invoke_async": True,
        "accepts": {"task payload (TaskCreate)": "per the agent's schema"},
        "output_format": "task id; GET /frontend-developer/tasks/{id}",
        "requires": ["agent login (current_user dependency) — an unauthorized "
                     "response is reported verbatim, never faked"],
    },
    "llm-gateway": {
        "capabilities": [],   # infrastructure, never selected for tasks
        "invoke_path": "", "invoke_async": False,
        "notes": "Provider failover health — infrastructure, not an agent.",
    },
    "agency-agents": {
        "capabilities": ["expert", "specialist", "analysis", "domain knowledge",
                         "academic", "finance", "healthcare", "marketing",
                         "research", "write report"],
        "invoke_path": "/api/agency/agents/{agent_id}/run", "invoke_async": False,
        "accepts": {"agent_id": "one of the 267 inventory ids (locally matched)",
                    "message": "REQUIRED — the request for the specialist",
                    "max_tokens": "int (2048)"},
        "output_format": "JSON reply from the specialist agent",
        "notes": "267 specialist .md agents; matched locally against "
                 "backend/agency_inventory.json before invocation.",
    },
}

#: Not selectable for task execution (infrastructure / conversational-only).
NON_SELECTABLE = {"llm-gateway"}

#: Meta-agents the backend fleet endpoint does not probe; their own health
#: route lives here so probe_config_gaps can still prove them honestly.
_META_HEALTH: Dict[str, str] = {
    "agency-agents": "/api/agency/llm/health",
}


# ─────────────────────────────────────────────────────────────────────────────
# Backend connection (read-only view of backend/.env — same parsing as setup_keys)
# ─────────────────────────────────────────────────────────────────────────────

def _read_env_files() -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for env_file in (PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"):
        if not env_file.exists():
            continue
        try:
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in merged:
                    merged[key] = value
        except Exception:
            pass
    return merged


class BackendConnection:
    """Base URL + shared API key for the unified SARA backend."""

    def __init__(self) -> None:
        env = _read_env_files()
        # One canonical resolver (core.backend_url): SARA_BACKEND_URL →
        # SARA_PORT → env files → documented default 8000. The launchers
        # export SARA_BACKEND_URL, so the desktop can never again probe a
        # different port than the one the backend actually bound.
        try:
            from core.backend_url import resolve_backend_url
            self.base_url = resolve_backend_url()
        except Exception:
            port = (os.environ.get("SARA_PORT") or env.get("SARA_PORT") or "8000")
            self.base_url = f"http://127.0.0.1:{port}".rstrip("/")
        self.api_key = (os.environ.get("SARA_API_KEY")
                        or env.get("SARA_API_KEY") or "").strip()

    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h


#: Health payload keys that, when falsey, mean "core capability missing".
_BLOCKING_KEYS = ("configured", "bot_token_set", "llm")
#: Health payload key where SMTP readiness lives (degraded, not fatal).
_DEGRADED_KEYS = ("smtp",)


def config_gap(payload: Any) -> str:
    """Inspect a real health payload for missing-credential blockers.

    Returns "" when nothing essential is missing, else a human reason.
    Read from each agent's own words: whatsapp ``configured:false``, email
    ``smtp:false`` (drafts still work → degraded note), telegram
    ``bot_token_set:false``, edulearner ``llm:"offline"``.
    """
    if not isinstance(payload, dict):
        return ""
    reasons: List[str] = []
    degraded: List[str] = []
    for key in _BLOCKING_KEYS:
        if key not in payload:
            continue
        val = payload[key]
        if isinstance(val, str):
            if val.strip().lower() not in ("ok", "online", "configured", "ready", "true"):
                reasons.append(f"{key}: {val}")
        elif val is False:
            reasons.append(f"{key} is not configured")
    for key in _DEGRADED_KEYS:
        if payload.get(key) is False:
            degraded.append("SMTP credentials missing — drafts only, sending blocked")
    if reasons:
        extra = f" (also: {'; '.join(degraded)})" if degraded else ""
        return "; ".join(reasons) + extra
    if degraded:
        return "degraded: " + "; ".join(degraded)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────

class UnifiedAgentRegistry:
    """Thread-safe registry of every agent Jarvis can invoke.

    Discovery order (all in-process, no website code touched):
      1. static INVOKE_SPEC entries + the agency meta-agent are registered
         first so they exist even when the backend is offline;
      2. desktop actions and plugins are discovered locally via the existing
         core.action_loader / core.plugin_loader;
      3. the backend fleet endpoint GET /ultron/agents supplies live probe
         results (real HTTP health checks run by the backend itself);
      4. local health probes fill config gaps the fleet's classifier cannot
         see (configured/smtp/llm payload fields).
    """

    def __init__(self, connection: Optional[BackendConnection] = None,
                 emit: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.conn = connection or BackendConnection()
        self.backend_url = self.conn.base_url      # kept for compatibility
        self.api_key = self.conn.api_key
        self._agents: Dict[str, AgentInfo] = {}
        self._lock = threading.RLock()
        self._discovery_complete = False
        self._emit = emit
        #: local action name -> handler (invoked by the router for local steps)
        self._local_runners: Dict[str, Callable[..., Any]] = {}

    # -- event sink --------------------------------------------------------

    def set_emit(self, emit: Callable[[str, Dict[str, Any]], None]) -> None:
        self._emit = emit

    def _fire(self, kind: str, payload: Dict[str, Any]) -> None:
        if self._emit:
            try:
                self._emit(kind, payload)
            except Exception:
                pass

    # -- registration ------------------------------------------------------

    def register(self, info: AgentInfo) -> None:
        with self._lock:
            self._agents[info.id] = info
        self._fire("agents_changed", {"reason": "registered", "id": info.id})

    def register_local_runner(self, name: str,
                              runner: Callable[..., Any]) -> None:
        """Bind a desktop-action handler for the router to execute."""
        with self._lock:
            self._local_runners[name] = runner

    def local_runner(self, name: str) -> Optional[Callable[..., Any]]:
        with self._lock:
            return self._local_runners.get(name)

    def _upsert_backend(self, entry: Dict[str, Any], probed: bool) -> None:
        """Merge one fleet endpoint row into the registry (id is the key)."""
        agent_id = str(entry.get("id") or "").strip()
        if not agent_id:
            return
        raw_status = str(entry.get("status") or "unknown")
        state = _FLEET_STATE.get(raw_status, AgentState.REGISTERED)
        spec = INVOKE_SPEC.get(agent_id, {})
        with self._lock:
            info = self._agents.get(agent_id)
            created = info is None
            if info is None:
                info = AgentInfo(
                    id=agent_id,
                    name=entry.get("name") or agent_id,
                    category=entry.get("category") or "Backend",
                    description=entry.get("short_description")
                                or entry.get("detail") or "",
                    capabilities=list(spec.get("capabilities", ["delegate", "chat"])),
                    source="backend_agent",
                )
                self._agents[agent_id] = info
            info.route = entry.get("route")
            info.status = raw_status
            # A live task must not be clobbered back to ready by a refresh.
            if info.state != AgentState.RUNNING.value:
                info.state = state.value
            info.detail = str(entry.get("detail") or "")
            probe = entry.get("probe")
            info.health_endpoint = (f"{self.backend_url}{probe}") if probe else None
            info.last_health_check = float(entry.get("checked_at") or time.time())
            info.health_error = None if raw_status == "online" else info.health_error
            # merge static execution metadata
            if spec:
                info.invoke_path = spec.get("invoke_path", info.invoke_path)
                info.invoke_async = bool(spec.get("invoke_async", info.invoke_async))
                info.accepts = spec.get("accepts", info.accepts)
                info.output_format = spec.get("output_format", info.output_format)
                info.requires = list(spec.get("requires", info.requires))
                info.cancellable = bool(spec.get("cancellable", info.cancellable))
                info.consequential = bool(spec.get("consequential",
                                                   info.consequential))
                if spec.get("capabilities"):
                    info.capabilities = list(spec["capabilities"])
                if spec.get("notes"):
                    info.metadata["notes"] = spec["notes"]
        if created and probed:
            log.info("Registry: discovered backend agent %s (%s)", agent_id, raw_status)
        self._fire("agents_changed", {"reason": "fleet", "id": agent_id,
                                      "state": info.state})

    # -- discovery ---------------------------------------------------------

    def discover_all(self, actions_dir: Path, plugins_dir: Path,
                     reserved_names: Set[str] | None = None,
                     action_registry: Any = None) -> Dict[str, AgentInfo]:
        """Run the full discovery pass. Never raises.

        ``action_registry`` may be the ActionRegistry main.py already built —
        when given, desktop actions are read from it instead of re-importing.
        """
        reserved = set(reserved_names or ())
        try:
            self._seed_static()
            self._discover_desktop_actions(actions_dir, reserved, action_registry)
            self._discover_plugins(plugins_dir, reserved)
            self._add_ultron_natives()
        except Exception:
            log.exception("Agent discovery failed partway — continuing")
        self._discovery_complete = True
        log.info("Registry discovery: %d entries.", len(self._agents))
        return dict(self._agents)

    def _seed_static(self) -> None:
        """Agents we must know even with no backend and no actions yet."""
        for agent_id, spec in INVOKE_SPEC.items():
            if agent_id in self._agents:
                continue
            accepts = spec.get("accepts", {})
            if not isinstance(accepts, dict):
                accepts = {"input": str(accepts)}
            self._agents[agent_id] = AgentInfo(
                id=agent_id,
                name=agent_id.replace("-", " ").title(),
                category="Backend",
                description=str(spec.get("notes", "")) or
                           f"Backend agent at {spec.get('invoke_path', 'the backend')}",
                capabilities=list(spec.get("capabilities", [])),
                source="backend_agent",
                invoke_path=str(spec.get("invoke_path", "")),
                invoke_async=bool(spec.get("invoke_async", False)),
                accepts=dict(accepts),
                output_format=str(spec.get("output_format", "")),
                requires=list(spec.get("requires", [])),
                cancellable=bool(spec.get("cancellable", False)),
                consequential=bool(spec.get("consequential", False)),
                state=AgentState.REGISTERED.value,
                detail="awaiting first live probe",
            )
            if spec.get("notes"):
                self._agents[agent_id].metadata["notes"] = spec["notes"]

    def _discover_desktop_actions(self, actions_dir: Path, reserved: Set[str],
                                  action_registry: Any = None) -> None:
        try:
            if action_registry is None:
                from core.action_loader import discover_actions
                action_registry = discover_actions(
                    actions_dir, reserved, lambda _m: None)
            for name in sorted(action_registry.names()):
                rec = action_registry._actions.get(name)
                if rec is None:
                    continue
                desc = (rec.description or "").strip()
                self._agents[f"desktop_action:{name}"] = AgentInfo(
                    id=f"desktop_action:{name}",
                    name=name.replace("_", " ").title(),
                    category="Desktop Automation",
                    description=desc,
                    capabilities=_capability_words(name, desc),
                    source="desktop_action",
                    tools=[name],
                    accepts=dict(rec.parameters or {}),
                    state=AgentState.READY.value,
                    detail="validated at startup by action_loader",
                    metadata={"file": rec.file},
                )
                if rec.handler is not None:
                    self._local_runners[name] = rec.handler
            log.info("Registry: %d desktop actions bound.",
                     sum(1 for a in self._agents.values()
                         if a.source == "desktop_action"))
        except Exception:
            log.exception("Desktop action discovery failed")

    def _discover_plugins(self, plugins_dir: Path, reserved: Set[str]) -> None:
        try:
            from core.plugin_loader import discover_plugins
            registry = discover_plugins(plugins_dir, set(reserved), lambda _m: None)
            for name, rec in getattr(registry, "_plugins", {}).items():
                desc = (getattr(rec, "description", "") or "").strip()
                self._agents[f"plugin:{name}"] = AgentInfo(
                    id=f"plugin:{name}",
                    name=name.replace("_", " ").title(),
                    category="Plugin",
                    description=desc,
                    capabilities=_capability_words(name, desc),
                    source="plugin",
                    tools=[name],
                    state=AgentState.READY.value,
                    detail="loaded by plugin_loader",
                    metadata={"file": getattr(rec, "file", "")},
                )
                handler = getattr(rec, "handler", None)
                if callable(handler):
                    self._local_runners[name] = handler
        except Exception:
            log.exception("Plugin discovery failed")

    def _add_ultron_natives(self) -> None:
        """Ultron's planning/delegation capabilities (informational entries)."""
        natives = [
            ("ultron_plan", "Ultron Planner", "Plan a goal into tool steps",
             ["plan", "decompose"]),
            ("ultron_execute", "Ultron Executor", "Execute planned steps for real",
             ["execute", "retry"]),
            ("ultron_replan", "Ultron Replanner", "Re-plan around failures",
             ["replan", "adapt"]),
            ("ultron_delegate", "Ultron Delegator", "Delegate to sibling agents",
             ["delegate", "route"]),
            ("ultron_report", "Ultron Reporter", "Report from real outputs",
             ["report", "summarize"]),
            ("ultron_remember", "Ultron Memory", "Store and recall experiences",
             ["remember", "recall"]),
        ]
        for tool_id, name, desc, caps in natives:
            self._agents[f"ultron_native:{tool_id}"] = AgentInfo(
                id=f"ultron_native:{tool_id}", name=name, category="Planning",
                description=desc, capabilities=caps, source="ultron_native",
                tools=[tool_id], state=AgentState.READY.value,
                detail="capability of sara-ultron (invoked through /ultron)",
            )

    # -- live probing ------------------------------------------------------

    def refresh(self, timeout: float = 6.0) -> Dict[str, int]:
        """Full refresh: fleet endpoint first, then local config-gap probes.

        Runs synchronously — callers put it on a worker thread. Never raises.
        Returns normalized state counts.
        """
        self.fetch_fleet(timeout=timeout)
        self.probe_config_gaps(timeout=3.0)
        return self.state_counts()

    def fetch_fleet(self, timeout: float = 6.0) -> bool:
        """GET /ultron/agents — the backend's own real health probes."""
        import requests
        try:
            resp = requests.get(f"{self.backend_url}/ultron/agents",
                                headers=self.conn.headers(), timeout=timeout)
            if resp.status_code != 200:
                log.warning("Fleet endpoint HTTP %s", resp.status_code)
                self._mark_backend_down()
                return False
            data = resp.json()
        except Exception as exc:
            log.info("Fleet endpoint unreachable (%s) — backend agents marked "
                     "unavailable until it returns.", type(exc).__name__)
            self._mark_backend_down()
            return False
        for entry in (data.get("agents") or []):
            self._upsert_backend(entry, probed=True)
        for entry in (data.get("unprobed_agents") or []):
            self._upsert_backend(entry, probed=False)
        # The fleet already ran real HTTP probes; honor its classification.
        self._fire("agents_changed", {"reason": "fleet_refresh"})
        return True

    def _mark_backend_down(self) -> None:
        with self._lock:
            for info in self._agents.values():
                if info.source in ("backend_agent",) and info.id in INVOKE_SPEC:
                    if info.state == AgentState.RUNNING.value:
                        continue   # a live task's fate is decided by its own poll
                    info.state = AgentState.UNAVAILABLE.value
                    info.detail = f"backend offline at {self.backend_url}"
                    info.last_health_check = time.time()
        self._fire("agents_changed", {"reason": "backend_down"})

    def probe_config_gaps(self, timeout: float = 3.0) -> None:
        """Second local probe pass: read health PAYLOADS for config gaps.

        The fleet classifier only knows HTTP codes; the registry also reads
        each agent's own readiness fields (configured / smtp / llm /
        bot_token_set) so a reachable agent with missing credentials is
        reported BLOCKED with the real reason. Meta-agents (the 267 agency
        specialists) are probed through their own health route, which the
        fleet endpoint does not cover.
        """
        import requests
        with self._lock:
            targets = []
            for info in self._agents.values():
                if info.source not in ("backend_agent",) or not info.health_endpoint:
                    continue
                if info.state == AgentState.UNAVAILABLE.value:
                    continue
                targets.append((info, info.health_endpoint))
            # meta-agent probe path fallback (fleet does not cover these)
            for agent_id, path in _META_HEALTH.items():
                info = self._agents.get(agent_id)
                if info is None:
                    continue
                if not info.health_endpoint:
                    info.health_endpoint = f"{self.backend_url}{path}"
                if info.state == AgentState.UNAVAILABLE.value:
                    continue
                targets.append((info, info.health_endpoint))
        for info, url in targets:
            try:
                resp = requests.get(url, headers=self.conn.headers(),
                                    timeout=timeout)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json() if resp.content else {}
            except Exception:
                payload = {}
            gap = config_gap(payload)
            with self._lock:
                info.last_health_check = time.time()
                if info.state == AgentState.RUNNING.value:
                    continue
                if gap:
                    info.state = AgentState.BLOCKED.value
                    info.detail = gap
                    info.status = "degraded"
                else:
                    if info.state == AgentState.BLOCKED.value:
                        info.state = AgentState.READY.value
                        info.status = "online"
                    info.detail = info.detail or "healthy"
            if gap:
                self._fire("agent_state", {"id": info.id, "state": "blocked",
                                           "detail": gap[:300]})

    # -- runtime task transitions -----------------------------------------

    def set_running(self, agent_id: str, task_id: str) -> None:
        with self._lock:
            info = self._agents.get(agent_id)
            if not info:
                return
            info.state = AgentState.RUNNING.value
            info.current_task_id = task_id
            info.last_error = ""
        self._fire("agent_state", {"id": agent_id, "state": "running",
                                   "task_id": task_id})

    def set_finished(self, agent_id: str, ok: bool, detail: str = "") -> None:
        """Record the REAL outcome of one execution."""
        with self._lock:
            info = self._agents.get(agent_id)
            if not info:
                return
            info.current_task_id = ""
            info.last_health_check = time.time()
            if ok:
                info.state = AgentState.COMPLETED.value
                info.detail = detail[:300] or info.detail
                info.last_error = ""
            else:
                info.state = AgentState.FAILED.value
                info.last_error = detail[:500]
        self._fire("agent_state", {"id": agent_id,
                                   "state": "completed" if ok else "failed",
                                   "detail": detail[:300]})

    def restore_ready(self, agent_id: str) -> None:
        """After a completed task, a probed backend agent goes back to READY."""
        with self._lock:
            info = self._agents.get(agent_id)
            if info and info.state == AgentState.COMPLETED.value:
                if info.source == "backend_agent" and info.health_endpoint:
                    info.state = AgentState.READY.value
                elif info.source in ("desktop_action", "plugin"):
                    info.state = AgentState.READY.value

    # -- read / selection --------------------------------------------------

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        with self._lock:
            return self._agents.get(agent_id)

    def get_all_agents(self) -> List[AgentInfo]:
        with self._lock:
            return list(self._agents.values())

    def selectable(self) -> List[AgentInfo]:
        """Agents the router may choose (spec §4: readiness-aware selection)."""
        with self._lock:
            return [a for a in self._agents.values() if a.invocable]

    def state_counts(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for a in self._agents.values():
                counts[a.state] = counts.get(a.state, 0) + 1
            return counts

    def active_task_agent(self) -> Optional[AgentInfo]:
        with self._lock:
            for a in self._agents.values():
                if a.state == AgentState.RUNNING.value:
                    return a
        return None

    def find_best_agent(self, task_description: str,
                        required_capabilities: List[str] | None = None
                        ) -> Optional[AgentInfo]:
        """Keyword scorer over READY/invocable agents — offline fallback used
        when the LLM router is unavailable (spec §10: keep routing reliable)."""
        with self._lock:
            pool = [a for a in self._agents.values() if a.invocable]
            if required_capabilities:
                pool = [a for a in pool
                        if all(cap in a.capabilities for cap in required_capabilities)]
            task_words = [w for w in task_description.lower().split() if len(w) > 2]
            best: Optional[tuple] = None
            for agent in pool:
                score = 0
                name_words = agent.name.lower().split()
                desc_words = agent.description.lower().split()
                cap_words = [c.lower() for c in agent.capabilities]
                for word in task_words:
                    if word in name_words:
                        score += 3
                    if any(word in dw for dw in desc_words):
                        score += 2
                    if any(word in cw for cw in cap_words):
                        score += 3
                    if word in agent.category.lower().split():
                        score += 1
                # prefer the supervisor for genuinely multi-part requests
                if agent.id == "sara-ultron" and score > 0:
                    score += 1
                if score > 0 and (best is None or score > best[0]):
                    best = (score, agent)
            return best[1] if best else None

    # -- agency meta-agent (267 specialists, local inventory match) --------

    def agency_inventory(self) -> List[Dict[str, Any]]:
        path = PROJECT_ROOT / "backend" / "agency_inventory.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def find_agency_agents(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Rank the real inventory against a request — local, offline, honest."""
        q = (query or "").lower()
        if not q.strip():
            return []
        tokens = {t for t in q.replace(",", " ").split() if len(t) > 2}
        scored: List[tuple] = []
        for entry in self.agency_inventory():
            hay = " ".join(str(entry.get(k, "")) for k in
                           ("name", "description", "division_label", "vibe")).lower()
            score = sum(1 for t in tokens if t in hay)
            if str(entry.get("division", "")).lower() in q:
                score += 2
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored[:limit]]

    # -- reporting ---------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            by_category: Dict[str, int] = {}
            by_source: Dict[str, int] = {}
            for agent in self._agents.values():
                by_category[agent.category] = by_category.get(agent.category, 0) + 1
                by_source[agent.source] = by_source.get(agent.source, 0) + 1
            counts = self.state_counts()
            return {
                "total_agents": len(self._agents),
                "by_category": by_category,
                "by_source": by_source,
                "by_state": counts,
                "discovery_complete": self._discovery_complete,
                "backend_url": self.backend_url,
                "api_key_set": bool(self.api_key),
            }

    def export_for_ui(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [a.to_dict() for a in self._agents.values()]


def _capability_words(name: str, description: str) -> List[str]:
    """Capability keywords for a local action: its name tokens plus the
    distinctive words of its description (lowercased, deduped, capped)."""
    words: List[str] = [w for w in name.replace("-", " ").split() if w]
    stop = {"a", "the", "and", "for", "with", "that", "this", "from", "your",
            "you", "when", "file", "using", "into"}
    for word in description.lower().replace(",", " ").split():
        if len(word) > 3 and word not in stop and word.isalpha():
            words.append(word)
        if len(words) >= 24:
            break
    seen: Set[str] = set()
    out: List[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Process-wide singleton (one runtime per Jarvis process)
# ─────────────────────────────────────────────────────────────────────────────

_global_registry: Optional[UnifiedAgentRegistry] = None
_registry_lock = threading.Lock()


def get_global_registry() -> UnifiedAgentRegistry:
    global _global_registry
    with _registry_lock:
        if _global_registry is None:
            _global_registry = UnifiedAgentRegistry()
        return _global_registry


def set_global_registry(registry: UnifiedAgentRegistry) -> None:
    global _global_registry
    with _registry_lock:
        _global_registry = registry










