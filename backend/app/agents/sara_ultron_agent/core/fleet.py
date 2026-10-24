"""Ultron fleet probe - real liveness for every agent SARA actually serves.

The Hub's Agent Control Center must never show "online" unless the backend
confirmed it. There is no central agent supervisor in SARA (``app/main.py``
mounts each agent with ``include_router``/``app.mount`` and that is the
registration surface - see ``core/catalog.py``), so the only honest source of
truth is to *ask* each agent.

Every entry in :data:`AGENT_PROBES` points at a health route that exists in the
live app; ``backend/tests/test_sara_ultron_phase3.py`` asserts this against the
running OpenAPI schema, so a probe path that stops existing fails the suite
instead of silently reporting a dead agent as unreachable.

Probes run over HTTP against the *same* base URL this request arrived on
(``request.base_url``), not a guessed port, so the check exercises the real
routing/middleware stack and cannot drift from the deployment. They are
deliberately short (2.5 s) and bounded (6 concurrent), so a wedged agent can
cost the control center at most one timeout window.

Nothing here fabricates a status. An agent with no health route is reported as
``no_probe``; that is the honest answer, not a green dot.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("sara.ultron.fleet")

#: Per-probe ceiling. A health route that takes longer than this is not healthy.
PROBE_TIMEOUT_S = 2.0

#: Cap on simultaneous probes so the control center cannot stampede the server.
#: With 15 probes this is two waves, so the worst case is ~4 s of wall clock
#: even if every single agent is wedged.
MAX_CONCURRENCY = 8

#: Longest detail string echoed back to the UI.
DETAIL_LIMIT = 160

#: Keys a health payload may legitimately contribute to the detail line.
_DETAIL_KEYS = ("status", "message", "detail", "agent", "gateway", "mode")

#: Redaction for anything that looks like a credential. Health endpoints are
#: written by other agents and are not guaranteed to be secret-free, and this
#: response is rendered in a browser, so the value is scrubbed before it leaves
#: the process rather than trusted not to leak.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+"),
)
_REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Remove anything credential-shaped from a string bound for the browser."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


#: Every agent the Hub lists, with the *real* health route that proves it is up.
#: ``probe=None`` means SARA exposes no liveness route for it - reported as
#: ``no_probe`` rather than guessed at.
AGENT_PROBES: List[Dict[str, Any]] = [
    {
        "id": "sara-ultron",
        "name": "SARA Ultron",
        "category": "General",
        "route": "/hub/sara-ultron",
        "probe": "/ultron/health",
    },
    {
        "id": "sara-auto",
        "name": "SARA Universal",
        "category": "General",
        "route": "/hub/sara-auto",
        "probe": "/sara-auto/health",
    },
    {
        "id": "backend-architect",
        "name": "Backend Architect",
        "category": "Engineering",
        "route": "/hub/backend-architect",
        "probe": "/backend-architect/health",
    },
    {
        "id": "frontend-developer",
        "name": "Frontend Developer",
        "category": "Engineering",
        "route": "/hub/frontend-developer",
        "probe": "/frontend-developer/health",
    },
    {
        "id": "ai-web-scraper",
        "name": "AI Web Scraper",
        "category": "Research",
        "route": "/hub/ai-web-scraper",
        "probe": "/api/agent/health",
    },
    {
        "id": "edulearner-ai",
        "name": "EduLearner AI",
        "category": "Education",
        "route": "/hub/edulearner-ai",
        "probe": "/api/edulearner/health",
    },
    {
        "id": "email-automation",
        "name": "Email Automation",
        "category": "Marketing",
        "route": "/hub/email-automation",
        "probe": "/email-agent/health",
    },
    {
        "id": "game-forge",
        "name": "Game Forge",
        "category": "Engineering",
        "route": "/hub/game-forge",
        "probe": "/api/gameforge/health",
    },
    {
        "id": "music-studio",
        "name": "Music Studio",
        "category": "Other",
        "route": "/hub/music-studio",
        "probe": "/api/music/health",
    },
    {
        "id": "social-media",
        "name": "Social Media Manager",
        "category": "Marketing",
        "route": "/hub/social-media",
        "probe": "/social/health",
    },
    {
        "id": "trading-agent",
        "name": "Trading Agent",
        "category": "Finance",
        "route": "/hub/trading-agent",
        "probe": "/trading/health",
    },
    {
        "id": "telegram-assistant",
        "name": "Telegram Assistant",
        "category": "Business",
        "route": "/hub/telegram-assistant",
        "probe": "/telegram/status",
    },
    {
        "id": "whatsapp-agent",
        "name": "SARA WhatsApp Engine",
        "category": "Business",
        "route": "/hub/whatsapp-agent",
        "probe": "/whatsapp/health",
    },
    {
        "id": "voice-agent",
        "name": "Voice Agent",
        "category": "Other",
        "route": "/hub/voice-agent",
        "probe": "/voice-agent/health",
    },
    {
        "id": "llm-gateway",
        "name": "LLM Failover Gateway",
        "category": "Infrastructure",
        "route": None,
        "probe": "/api/llm/health",
    },
]

#: Status values a probe can report. ``online`` is only ever set from a real 2xx.
ONLINE = "online"
DEGRADED = "degraded"
UNREACHABLE = "unreachable"
NO_PROBE = "no_probe"


def _detail_from_payload(payload: Any) -> str:
    """Extract a short, safe human line from a health payload.

    Health responses vary wildly between agents (some return a string, some a
    dict, some a nested provider list). Only whitelisted keys are read; the raw
    body is never echoed, because one of these agents could later start
    returning configuration.
    """
    if isinstance(payload, str):
        return redact(payload)[:DETAIL_LIMIT]
    if not isinstance(payload, dict):
        return ""
    parts: List[str] = []
    for key in _DETAIL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
        elif isinstance(value, bool):
            parts.append(f"{key}: {str(value).lower()}")
    return redact(" | ".join(parts))[:DETAIL_LIMIT]


def _classify(http_status: Optional[int], error: Optional[str],
              payload: Any) -> str:
    """Map one probe's outcome onto a status. 2xx is the only path to online."""
    if error:
        return UNREACHABLE
    if http_status is None:
        return UNREACHABLE
    if 200 <= http_status < 300:
        return ONLINE
    return DEGRADED


async def _probe_one(client: Any, semaphore: asyncio.Semaphore, base_url: str,
                     entry: Dict[str, Any], api_key: str = "") -> Dict[str, Any]:
    """Probe a single agent. Never raises: every outcome becomes a status."""
    result: Dict[str, Any] = {
        "id": entry["id"],
        "name": entry["name"],
        "category": entry.get("category"),
        "route": entry.get("route"),
        "probe": entry.get("probe"),
        "status": NO_PROBE,
        "http_status": None,
        "latency_ms": None,
        "detail": "This agent exposes no health route; SARA cannot confirm its state.",
    }
    probe = entry.get("probe")
    if not probe:
        return result

    url = f"{base_url.rstrip('/')}{probe}"
    started = time.perf_counter()
    http_status: Optional[int] = None
    error: Optional[str] = None
    payload: Any = None
    async with semaphore:
        try:
            headers = {"X-API-Key": api_key} if api_key else {}
            response = await client.get(url, headers=headers)
            http_status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = response.text[:DETAIL_LIMIT]
        except Exception as exc:  # noqa: BLE001 - a failed probe is data, not a crash
            error = f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]
            log.debug("Fleet probe %s failed: %s", entry["id"], error)

    result["http_status"] = http_status
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["status"] = _classify(http_status, error, payload)
    if error:
        result["detail"] = error
    else:
        detail = _detail_from_payload(payload)
        result["detail"] = detail or f"HTTP {http_status}"
    return result


def _live_run_summary(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, redacted view of one Ultron run for the control center."""
    steps = snapshot.get("steps") or []
    last = steps[-1] if steps else {}
    return {
        "task_id": snapshot.get("task_id"),
        "goal": (snapshot.get("goal") or "")[:240],
        "status": snapshot.get("status"),
        "phase": snapshot.get("phase"),
        "current_tool": snapshot.get("current_tool") or last.get("tool"),
        "steps_done": len(steps),
        "plan_steps": len(snapshot.get("plan") or []),
        "elapsed_s": round(time.time() - float(snapshot.get("created") or 0), 1),
    }


async def probe_fleet(base_url: str,
                      sessions: Optional[List[Dict[str, Any]]] = None,
                     api_key: str = "") -> Dict[str, Any]:
    """Probe every agent and merge in Ultron's own live run state.

    ``base_url`` comes from the inbound request, so probes hit the deployment
    the caller is actually using. Returns the fleet plus a real summary count -
    the UI renders these numbers directly, so they are computed from the probes
    that ran, never assumed.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    # trust_env=False: a developer's HTTP_PROXY must not intercept loopback
    # probes, which would report every agent unreachable on a proxied machine.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(PROBE_TIMEOUT_S, connect=1.5),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        agents = await asyncio.gather(
            *(_probe_one(client, semaphore, base_url, entry, api_key) for entry in AGENT_PROBES)
        )

    agents = list(agents)

    # Ultron itself reports live run state, which no health route can show.
    live = sessions or []
    by_id = {a["id"]: a for a in agents}
    ultron = by_id.get("sara-ultron")
    if ultron is not None:
        active = [r for r in live if r.get("status") in ("queued", "running")]
        ultron["live"] = {
            "active_runs": len(active),
            "total_sessions": len(live),
            "current": _live_run_summary(active[0]) if active else None,
        }
    gateway = by_id.get("llm-gateway")
    if gateway is not None:
        # The gateway entry is infrastructure, not an agent: its probe already
        # carries the provider list, so nothing extra is fabricated here.
        gateway["infrastructure"] = True

    counts: Dict[str, int] = {ONLINE: 0, DEGRADED: 0, UNREACHABLE: 0, NO_PROBE: 0}
    for agent in agents:
        counts[agent["status"]] = counts.get(agent["status"], 0) + 1

    probed = sum(1 for a in agents if a.get("probe"))
    return {
        "agents": agents,
        "summary": {
            "total": len(agents),
            "probed": probed,
            **counts,
            "checked_at": time.time(),
            "base_url": base_url.rstrip("/"),
        },
        "live_runs": [_live_run_summary(s) for s in live
                      if s.get("status") in ("queued", "running")],
    }


def probe_map() -> Dict[str, str]:
    """``id -> probe path`` for the agents that expose one. Used by tests."""
    return {e["id"]: e["probe"] for e in AGENT_PROBES if e.get("probe")}
