"""SARA Ultron - HTTP/SSE surface.

Mounted at ``/ultron`` by ``app/main.py``, exactly like every other SARA agent
(``app.include_router(sara_ultron_agent.router)``).

Routes
------
  GET  /ultron/               agent metadata (registry entry)
  POST /ultron/run            start a goal -> task_id
  GET  /ultron/events         SSE stream of a run (?task_id=...)
  POST /ultron/approve        approve or deny a gated step
  POST /ultron/stop           stop one run
  POST /ultron/emergency-stop GLOBAL STOP: halt every run, cut every device link
  GET  /ultron/status         task status, phase, plan, steps and trace
  GET  /ultron/tasks          every retained task, for the progress board
  GET  /ultron/capabilities   live tool surface + delegation targets
  GET  /ultron/agents         live per-agent health, from real probes
  GET  /ultron/memory         stored experiences + learned rules
  POST /ultron/memory/rules   add / toggle / delete a learned rule
  POST /ultron/memory/forget  delete one stored experience by timestamp
  GET  /ultron/health         health ping
  GET  /ultron/ready          readiness: voice stack + device link + LLM health
  GET  /ultron/voice/status   what the local voice stack can do right now
  GET  /ultron/voice/state    derived six-state voice machine (what the rail shows)
  POST /ultron/voice/listen   record one utterance, transcribe locally
  POST /ultron/voice/speak    say a reply aloud (arms the echo guard)
  POST /ultron/voice/cancel   stop speaking, drop the echo guard
  GET  /ultron/devices        every paired device, with its derived state
  POST /ultron/pairing        mint a single-use, expiring pairing token + QR
  GET  /ultron/pairing/offer  safe view of an unredeemed offer (?t=token)
  POST /ultron/pairing/claim  redeem a pairing token for a device token
  POST /ultron/devices/{id}/revoke   cut one device off, immediately
  WS   /ultron/link           authenticated device link (heartbeat + events)
  WS   /ultron/ws             authenticated UI stream (bus events, read-only)

Auth
----
The state-changing routes (``/run``, ``/approve``, ``/stop`` and the three
``/voice`` writes) require SARA's shared API key via
``app.api.deps.require_api_key`` - the same dependency the Email Automation
agent uses. The read routes stay open, matching ``app/api/sara_auto_agent.py``;
they are only reachable with a ``task_id`` that an authenticated ``/run`` already
issued (a UUID4), so they expose nothing that cannot already be reached through
the authenticated surface. ``/voice/status`` is open because it reports
capability only and is needed to render the mic button's state.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import (API_KEY, current_tenant, optional_tenant,
                          require_api_key)
from .core.tenancy import LOCAL_TENANT_ID

#: Auto-pairing is on by default because the whole point is that the operator
#: should not have to hand-copy a token on every restart. Turn it OFF for any
#: backend reachable from the public internet; the QR flow is unaffected.
AUTO_PAIR_ENABLED = os.getenv("SARA_AUTOPAIR", "1").strip() not in (
    "0", "false", "no", "off")

from .core.adapters import RawWindow
from .core.alwayson import always_on
from .core.awareness import context as context_manager
from .core.bus import bus
from .core.catalog import AGENT_CATALOG, AGENT_METADATA, DELEGATABLE_AGENTS
from .core.devices import DEVICE_KINDS, DEVICE_SCOPES, registry as device_registry
from .core.fleet import AGENT_PROBES, probe_fleet
from .core.observer import observer
from .core.permissions import GATES, PermissionDenied, permissions
from .core.references import resolve as resolve_reference
from .core.session import TERMINAL_PHASES, store
from .core.tools import NATIVE_TOOL_NAMES, get_memory, get_registry
from .core.vision import vision
from .core.voice import get_voice
from .core.voice_state import VOICE_STATES, tracker as voice_tracker
from .orchestrator import run_session

log = logging.getLogger("sara.ultron.api")

router = APIRouter(prefix="/ultron", tags=["SARA Ultron"])

#: SSE keepalive interval, so proxies do not close an idle stream.
KEEPALIVE_S = 15


def _owner_scope(tenant) -> str:
    """Owner id for the local surfaces that predate tenancy.

    The readiness gate, the device list and the pairing claim are reachable
    without an identity on purpose: tooling probes ``/ready`` with no headers,
    and the phone's pair page (served by this same backend) has no API key to
    send. Per this module's design rule - "no identity presented -> the local
    tenant" - an unauthenticated caller is scoped to the LOCAL tenant, exactly
    as these routes behaved before tenancy existed; a signed-in caller keeps
    their own tenant. Routers that mint tokens or stop work still refuse
    unauthenticated callers outright via ``current_tenant``.
    """
    return tenant.id or LOCAL_TENANT_ID

#: Poll interval while draining the event queue.
POLL_S = 0.08

#: Hard ceiling on an uploaded audio clip. At 16 kHz mono 16-bit that is
#: roughly 5.5 minutes, far beyond the 30 s utterance limit, so it exists to
#: stop a hostile (or broken) client from filling memory, not to shape normal
#: traffic.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    goal: str
    mode: str = "autonomous"      # "autonomous" | "assisted" (approve every step)


class ApproveRequest(BaseModel):
    task_id: str
    call_id: Optional[str] = None
    action: str = "APPROVE"       # "APPROVE" | "DENY"


class StopRequest(BaseModel):
    task_id: str


class VoiceListenRequest(BaseModel):
    timeout: float = 8.0          # seconds to wait for speech to start
    phrase_limit: float = 12.0    # seconds of speech to accept before cutting


class VoiceSpeakRequest(BaseModel):
    text: str


class MemoryRuleRequest(BaseModel):
    action: str = "list"            # "list" | "add" | "toggle" | "delete"
    rule: Optional[str] = None
    rule_id: Optional[str] = None
    category: Optional[str] = None


class MemoryForgetRequest(BaseModel):
    timestamp: float
    goal: Optional[str] = None      # secondary confirmation, optional


class PairingRequest(BaseModel):
    kind: str = "phone"             # "phone" | "tablet" | "desktop"
    label: str = ""                 # human name shown in the device list
    ttl_s: int = 120                # capped at 900 server-side
    pair_base_url: Optional[str] = None  # what the QR code points at


class PairingClaimRequest(BaseModel):
    token: str
    kind: str = "phone"
    label: str = ""
    platform: str = ""


class AutoPairRequest(BaseModel):
    kind: str = "pc_agent"
    label: str = ""
    platform: str = ""


class RevokeRequest(BaseModel):
    reason: str = "revoked by operator"


class PermissionRequest(BaseModel):
    allowed: bool
    reason: str = ""


class ContextReport(BaseModel):
    """A context observation pushed in by a desktop runtime client."""

    title: str = ""
    process: str = ""
    pid: int = 0
    hwnd: int = 0
    url: Optional[str] = None
    task: Optional[str] = None
    last_action: Optional[str] = None
    last_action_ok: Optional[bool] = None


class VisionRequest(BaseModel):
    question: str = ""
    force: bool = False


class ObserverRequest(BaseModel):
    running: bool = True
    interval_s: float = 1.0


class AlwaysOnRequest(BaseModel):
    speaking: bool = False
    goal: Optional[str] = None


class AlwaysOnStopRequest(BaseModel):
    reason: str = "operator"


class ResolveRequest(BaseModel):
    text: str


class TaskRequest(BaseModel):
    task: str = ""


class ActionRequest(BaseModel):
    action: str
    verified: Optional[bool] = None   # None = attempted, outcome unknown


class EmergencyStopRequest(BaseModel):
    revoke_devices: bool = False    # opt-in: kill device tokens, not just links


def _make_always_on_handler(seed_goal: str = ""):
    """Build the default handler the always-on loop hands utterances to.

    Kept in the API layer, not in ``core/alwayson.py``, so the loop stays free
    of a dependency on the orchestrator (which already depends on ``core``).
    The handler does the three things a follow-up needs and nothing more:

    1. resolve any contextual reference against the live context,
    2. attach the resolved target to the goal,
    3. start a real Ultron run - or, if the reference was ambiguous, return the
       clarification question *instead of running anything at all*.

    That last point is the important one. "Skip the ad" with no YouTube context
    must ask, not click.
    """
    from .core.references import is_reference_only

    def handle(utterance: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        text = (utterance or "").strip()
        if not text:
            return {"ok": False, "reason": "empty", "speak": ""}

        # The wake word is stripped when present but is not required.
        for prefix in ("ultron,", "ultron"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip(" ,.")
                break

        goal = seed_goal.strip()
        if is_reference_only(text) or _mentions_reference(text):
            elements = (vision.latest() or {}).get("elements")
            resolution = resolve_reference(text, elements)
            if not resolution.resolved:
                # Ask, and speak the question. Nothing is executed.
                return {
                    "ok": False,
                    "reason": "ambiguous_reference",
                    "question": resolution.question,
                    "detail": resolution.reason,
                    "speak": resolution.question or "Which one did you mean?",
                }
            target = resolution.target or {}
            described = (target.get("app_name") or target.get("item")
                         or target.get("site") or "the previous window")
            goal = f"{text} (in context: {described})"

        if not goal:
            return {"ok": False, "reason": "nothing to do", "speak": ""}

        try:
            session = store.create(goal, mode="autonomous",
                                   owner_id=LOCAL_TENANT_ID)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=run_session, args=(session,), daemon=True,
                         name=f"ultron-alwayson-{session.task_id[:8]}").start()
        context_manager.set_task(goal)
        return {
            "ok": True,
            "task_id": session.task_id,
            "goal": goal,
            "summary": f"started task {session.task_id[:8]}",
        }

    return handle


def _mentions_reference(text: str) -> bool:
    """True when the utterance contains a word a resolver understands.

    Imported lazily to keep the module graph flat; the real implementation lives
    with the resolver.
    """
    from .core.references import _has_reference

    return _has_reference(text or "")


def _require_session(task_id: str):
    session = store.get(task_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/")
async def agent_info():
    """Registry entry for SARA Ultron - the canonical agent identity."""
    return {**AGENT_METADATA, "native_tools": sorted(NATIVE_TOOL_NAMES)}


@router.post("/run", dependencies=[Depends(require_api_key)])
async def run_task(body: RunRequest, tenant=Depends(current_tenant)):
    """Start an Ultron run. Returns immediately with a task_id to stream."""
    goal = (body.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Goal cannot be empty")
    if len(goal) > 4000:
        raise HTTPException(status_code=400, detail="Goal is too long (max 4000 characters)")

    mode = "assisted" if body.mode == "assisted" else "autonomous"
    session = store.create(goal, mode=mode, owner_id=tenant.id)
    threading.Thread(
        target=run_session, args=(session,), daemon=True,
        name=f"ultron-{session.task_id[:8]}",
    ).start()
    log.info("Ultron run %s started (mode=%s)", session.task_id[:8], mode)
    bus.publish("task." + session.task_id, "created", task_id=session.task_id,
                goal=session.goal, mode=mode)
    return {"task_id": session.task_id, "status": "started",
            "goal": session.goal, "mode": session.mode,
            "agent": AGENT_METADATA["id"]}


@router.get("/events")
async def stream_events(task_id: str):
    """Server-sent event stream for one run; closes on the `done` event."""
    session = _require_session(task_id)

    async def _gen():
        last_beat = time.time()
        while True:
            try:
                ev = session.event_q.get_nowait()
            except queue.Empty:
                if time.time() - last_beat > KEEPALIVE_S:
                    last_beat = time.time()
                    yield ": keepalive\n\n"
                await asyncio.sleep(POLL_S)
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            # Mirror every event onto the process-wide bus so a UI that is not
            # watching this specific run (the command center's device rail, the
            # task board, a paired phone) still sees the run move.
            bus.publish("task." + task_id, str(ev.get("type") or "event"), **ev)
            if ev.get("type") == "done":
                break

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.post("/approve", dependencies=[Depends(require_api_key)])
async def approve_step(body: ApproveRequest):
    """Approve or deny a step that is waiting on the approval gate."""
    session = _require_session(body.task_id)
    granted = str(body.action).upper() != "DENY"
    session.approve_q.put((body.call_id or "", granted))
    return {"status": "ok", "action": "APPROVE" if granted else "DENY"}


@router.post("/stop", dependencies=[Depends(require_api_key)])
async def stop_task(body: StopRequest):
    """Emergency stop. Also unblocks any pending approval wait."""
    session = _require_session(body.task_id)
    session.stop.set()
    session.approve_q.put(("", False))
    session.log("[ULTRON] Stop requested by user.")
    return {"status": "stopping", "task_id": body.task_id}


@router.get("/status")
async def get_status(task_id: str):
    """Current state of a run: phase, plan, step results and the recent trace."""
    session = _require_session(task_id)
    return session.snapshot()


@router.get("/tasks", dependencies=[Depends(require_api_key)])
async def tasks():
    """Every retained task, as a progress board.

    Authenticated because a task carries its goal text, which is the user's
    private content. Progress is computed from real fields - terminal phases
    come from the store's own phase set, and completion ratio from real step
    results - so a queued task reads 0% and a finished one reads 100% whether
    or not that is flattering.
    """
    rows = []
    for snapshot in store.snapshots():
        plan = snapshot.get("plan") or []
        steps = snapshot.get("steps") or []
        # The executor's StepResult serialises as {"ok": bool, "skipped": bool} -
        # it has no "status" field. Counting `s.get("status")` therefore matched
        # nothing and every task read 0%, including finished ones.
        finished = sum(1 for s in steps if s.get("ok") or s.get("skipped"))
        failed = sum(1 for s in steps if not s.get("ok") and not s.get("skipped"))
        phase = snapshot.get("phase")
        rows.append({
            "task_id": snapshot.get("task_id"),
            "goal": (snapshot.get("goal") or "")[:160],
            "status": snapshot.get("status"),
            "phase": phase,
            "terminal": phase in TERMINAL_PHASES,
            "mode": snapshot.get("mode"),
            "current_tool": snapshot.get("current_tool"),
            "steps_done": finished,
            "steps_failed": failed,
            "steps_total": len(plan),
            "progress": round(finished / len(plan), 3) if plan else 0.0,
            "replans": snapshot.get("replans", 0),
            "created": snapshot.get("created"),
            "elapsed_s": round(time.time() - float(snapshot.get("created") or 0), 1),
        })
    active = [r for r in rows if not r["terminal"]]
    return {
        "tasks": rows,
        "summary": {
            "total": len(rows),
            "active": len(active),
            "finished": len(rows) - len(active),
        },
        "checked_at": time.time(),
    }


@router.get("/agents")
async def agents(request: Request):
    """Live status of every agent in the Hub, from real health probes.

    Nothing here is assumed. Each agent is probed over HTTP against the base
    URL this request arrived on; a non-2xx is ``degraded``, a timeout or
    connection error is ``unreachable``, and an agent with no health route is
    ``no_probe``. Ultron's own entry additionally carries its live run state
    (``live.active_runs`` / ``live.current``) read from the real session store.

    Auth: open, matching ``/capabilities``. The fleet is a status board - it
    exposes no goal text, no trace and no memory content. Ultron's live-run
    summary is limited to the phase, the current tool and a truncated goal, and
    ``/status`` (the route that returns full traces) stays reachable only with
    a task_id that an authenticated ``/run`` issued.
    """
    snapshots = store.snapshots()
    # base_url comes from the inbound request, so the probes hit the same
    # deployment the caller is talking to - no port is assumed anywhere.
    fleet = await probe_fleet(str(request.base_url), snapshots)
    # The catalog is the Hub's inventory; the probes are the truth. Agents the
    # catalog lists but the probe table does not cover are surfaced explicitly
    # rather than dropped, so a missing probe path is visible instead of
    # looking like the agent does not exist.
    probed_ids = {a["id"] for a in fleet["agents"]}
    fleet["unprobed_agents"] = [
        {"id": a["id"], "name": a["name"], "category": a.get("category"),
         "route": a.get("route"),
         "status": "no_probe",
         "probe": None,
         "detail": "No health route exists for this agent yet."}
        for a in AGENT_CATALOG if a["id"] not in probed_ids
    ]
    return fleet


@router.get("/memory")
async def memory_state():
    """What Ultron actually has stored, plus the rules it applies.

    Reads the same on-disk store the orchestrator writes to, so the panel in
    the UI is the memory, not a copy of it. ``recent`` is bounded and the rule
    bodies are truncated - this is a management surface, not a dump.
    """
    memory = get_memory()
    snapshot = memory.snapshot()
    experiences = memory.experiences()
    recent = [
        {
            "goal": str(e.get("goal", ""))[:240],
            "success": bool(e.get("success")),
            "errors": str(e.get("errors", ""))[:240],
            "timestamp": e.get("timestamp"),
            "steps": len(e.get("plan") or []) if isinstance(e.get("plan"), list) else 0,
        }
        for e in experiences[-15:]
    ]
    recent.reverse()
    rules = [
        {
            "id": r.get("id"),
            "rule": str(r.get("rule", ""))[:400],
            "category": r.get("category", "general"),
            "origin": r.get("origin", "unknown"),
            "active": bool(r.get("active", True)),
            "created_at": r.get("created_at"),
        }
        for r in memory.list_rules_full()
    ]
    return {**snapshot, "recent": recent, "rule_records": rules,
            "store": str(memory.memory_dir)}


@router.post("/memory/rules", dependencies=[Depends(require_api_key)])
async def memory_rules(body: MemoryRuleRequest):
    """Manage Ultron's learned rules. Every action reports what really changed.

    Returns ``changed: false`` with a reason when nothing was modified, rather
    than a blanket success - a UI that says "saved" for a no-op rule is worse
    than one that says the rule already existed.
    """
    memory = get_memory()
    action = (body.action or "list").strip().lower()

    if action == "list":
        return {"changed": False, "action": "list",
                "rules": memory.list_rules_full()}

    if action == "add":
        rule = (body.rule or "").strip()
        if not rule:
            raise HTTPException(status_code=400, detail="A non-empty 'rule' is required.")
        created = memory.learn_rule(rule, category=body.category or "user_correction",
                                    origin="user_correction")
        return {"changed": created, "action": "add",
                "detail": "Rule stored." if created else "An identical rule already existed.",
                "rules": memory.list_rules_full()}

    rule_id = (body.rule_id or "").strip()
    if not rule_id:
        raise HTTPException(status_code=400, detail="A non-empty 'rule_id' is required.")
    if action == "toggle":
        ok = memory.toggle_rule(rule_id)
    elif action == "delete":
        ok = memory.delete_rule(rule_id)
    else:
        raise HTTPException(status_code=400,
                            detail=f"Unknown action '{action}'. Use list | add | toggle | delete.")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Rule id '{rule_id}' not found.")
    return {"changed": True, "action": action, "rule_id": rule_id,
            "rules": memory.list_rules_full()}


@router.post("/memory/forget", dependencies=[Depends(require_api_key)])
async def memory_forget(body: MemoryForgetRequest):
    """Delete one stored experience by its exact timestamp.

    Memory management needs a delete that cannot take out the wrong record: a
    run's timestamp is written by the store itself, so it is stable, whereas a
    list index shifts the moment another run is recorded. A miss returns 404
    with the reason rather than a cheerful no-op.
    """
    if get_memory().forget(body.timestamp, (body.goal or "").strip()):
        return {"changed": True, "timestamp": body.timestamp}
    raise HTTPException(
        status_code=404,
        detail="No stored experience matched that timestamp"
               + (" and goal" if body.goal else "") + ".",
    )


@router.get("/capabilities")
async def capabilities(tenant=Depends(current_tenant)):
    """Live tool surface, delegation targets, memory state and LLM health."""
    tools: List[str] = []
    error = ""
    try:
        tools = get_registry().names()
    except Exception as exc:  # noqa: BLE001 - report degradation, never 500
        log.warning("Ultron capabilities: tool registry unavailable: %s", type(exc).__name__)
        error = f"{type(exc).__name__}: {exc}"

    native = [t for t in tools if t in NATIVE_TOOL_NAMES]
    desktop = [t for t in tools if t not in NATIVE_TOOL_NAMES]

    try:
        from app.core.llm_router import health_snapshot
        llm_active = health_snapshot().get("active")
    except Exception:  # noqa: BLE001
        llm_active = None

    try:
        memory_state = get_memory().snapshot()
    except Exception:  # noqa: BLE001
        memory_state = {"available": False}

    # Split the surface by where each tool actually comes from, so the Hub shows
    # "Ultron native / SARA desktop / Brahma-Echo" separately instead of one flat
    # list that hides the fact the agent is three sources deep.
    by_source: dict = {"ultron": [], "desktop": [], "brahma": []}
    gated: List[str] = []
    brahma_state: dict = {"available": 0, "unavailable": 0, "source_dir": None}
    try:
        registry = get_registry()
    except Exception:  # noqa: BLE001
        registry = None
    if registry is not None:
        for name in tools:
            by_source.setdefault(registry.source(name), []).append(name)
            if registry.is_gated(name):
                gated.append(name)
        try:
            brahma_state = registry.brahma_stats()
        except Exception:  # noqa: BLE001
            pass

    return {
        "agent": AGENT_METADATA["name"],
        "id": AGENT_METADATA["id"],
        "version": AGENT_METADATA["version"],
        "status": "online" if tools and llm_active else ("degraded" if tools else "offline"),
        "core": "plan-execute-replan loop over SARA's shared tool registry",
        "tools": tools,
        "native_tools": native,
        "desktop_actions": desktop,
        "tools_by_source": by_source,
        "approval_gated_tools": sorted(gated),
        "brahma": {
            "available": brahma_state.get("available", 0),
            "unavailable": brahma_state.get("unavailable", 0),
            "source_dir": brahma_state.get("source_dir"),
        },
        "capabilities": AGENT_METADATA["capabilities"],
        "delegation": {
            "enabled": True,
            "agents": [a["agent_id"] for a in DELEGATABLE_AGENTS],
            "channel": "POST /api/v1/chat (SARA's existing inter-agent endpoint)",
        },
        "agents": AGENT_CATALOG,
        "memory": memory_state,
        "llm_active_provider": llm_active,
        "active_sessions": store.count(owner_id=tenant.id),
        "error": error,
    }


@router.get("/health")
async def health():
    """Liveness. Proves the process is up, nothing more."""
    return {
        "status": "online",
        "agent": AGENT_METADATA["name"],
        "id": AGENT_METADATA["id"],
        "version": AGENT_METADATA["version"],
        "core": "plan-execute-replan",
    }


@router.get("/ready")
async def ready(tenant=Depends(optional_tenant)):
    """Readiness. Reports every subsystem honestly, including the broken ones.

    ``/health`` answers "is this process alive"; ``/ready`` answers "can it
    actually do the job". Each dependency is probed for real and reported
    individually, so a deployment gate can key on ``ready`` without a single
    boolean hiding three different failures.

    ``ready`` is ``True`` when the planning surface and the LLM router are both
    usable. Voice and device links are *reported*, not required: Ultron can plan
    and execute with no microphone and no paired phone, and a health gate that
    refused to come up over a missing mic would be worse than useless.

    Read-only and identity-optional: a probe with no key is scoped to the local
    tenant (see ``_owner_scope``), so its device counts stay local.
    """
    tools: List[str] = []
    tool_error = ""
    try:
        tools = get_registry().names()
    except Exception as exc:  # noqa: BLE001
        tool_error = f"{type(exc).__name__}: {exc}"

    llm_active: Optional[str] = None
    llm_error = ""
    try:
        from app.core.llm_router import health_snapshot

        llm_active = health_snapshot().get("active")
    except Exception as exc:  # noqa: BLE001
        llm_error = f"{type(exc).__name__}: {exc}"

    voice_status = await asyncio.to_thread(get_voice().status)
    voice_dict = voice_status.as_dict()
    voice_state = voice_tracker.snapshot(voice_dict, store.snapshots())
    device_summary = device_registry.summary(owner_id=_owner_scope(tenant))

    checks = {
        "tool_registry": {
            "ok": bool(tools),
            "detail": f"{len(tools)} tools" if tools else (tool_error or "no tools"),
        },
        "llm_router": {
            "ok": bool(llm_active),
            "detail": llm_active or (llm_error or "no active provider"),
        },
        "voice": {
            "ok": bool(voice_dict.get("available")),
            "detail": voice_state["reason"],
        },
        "device_link": {
            # "Ready" for a link means it is wired up, not that a device is
            # paired. No phone paired is a normal state, not a failure.
            "ok": True,
            "detail": f"{device_summary['total']} paired, "
                      f"{device_summary['by_state'].get('online', 0)} online",
        },
    }
    blocking = [name for name in ("tool_registry", "llm_router") if not checks[name]["ok"]]
    return {
        "ready": not blocking,
        "blocking": blocking,
        "checks": checks,
        "voice_state": voice_state["state"],
        "devices": device_summary,
        "checked_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------
@router.get("/voice/status")
async def voice_status():
    """What the local voice stack can do right now.

    Read-only and deliberately cheap. It confirms a microphone exists and
    reports whether a transcriber and a speech synthesiser are *available*,
    but it does not record, does not start the speech engine, and does not load
    the Whisper model - ``pyttsx3.init()`` spawns a COM process and the model
    is ~140 MB, neither of which a capability probe should pay for on every
    page load. The ``voices`` list is therefore empty until the first real
    ``/voice/speak``; ``speech_synth`` is still accurate either way.
    """
    status = await asyncio.to_thread(get_voice().status)
    return status.as_dict()


@router.post("/voice/transcribe", dependencies=[Depends(require_api_key)])
async def voice_transcribe(request: Request):
    """Transcribe audio the *browser* recorded, as a raw WAV body.

    Why this route exists: ``/voice/listen`` records with the backend host's
    microphone, which works on a laptop but can never work on a cloud host -
    Render has no audio input device at all. The browser is where the user's
    microphone actually is, so the mic button captures through ``getUserMedia``
    and uploads the result here for the same local Whisper pass.

    Content type is checked, the body is size-capped, and the WAV container is
    parsed before the model is touched - a malformed clip fails in
    microseconds instead of after a ~140 MB model load.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type not in ("audio/wav", "audio/x-wav", "audio/wave", "application/wav"):
        raise HTTPException(
            status_code=415,
            detail=f"Expected audio/wav, got {content_type or 'nothing'}.",
        )

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio clip is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio body.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio clip is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    result = await asyncio.to_thread(get_voice().transcribe, raw)
    if not result.get("ok"):
        voice_tracker.note_error(
            f"Transcription failed: {result.get('error', 'unknown reason')}"
        )
        raise HTTPException(status_code=503, detail=result)
    voice_tracker.note_success()
    bus.publish("voice", "transcribed", seconds=result.get("seconds"),
                language=result.get("language"), source="browser")
    return {**result, "source": "browser"}


@router.post("/voice/listen", dependencies=[Depends(require_api_key)])
async def voice_listen(body: VoiceListenRequest):
    """Record one utterance and transcribe it locally with Whisper.

    Run off the event loop: recording is a blocking microphone read for up to
    the requested timeout, and inline it would stall every other request on the
    server for that whole window.
    """
    result = await asyncio.to_thread(
        get_voice().listen, body.timeout, body.phrase_limit
    )
    if not result.get("ok"):
        # 503, not 400: a missing or busy device is a capability problem, not a
        # malformed request, and the caller should be able to retry unchanged.
        voice_tracker.note_error(
            f"Listening failed: {result.get('error', 'unknown reason')}"
        )
        raise HTTPException(status_code=503, detail=result)
    voice_tracker.note_success()
    bus.publish("voice", "heard", seconds=result.get("seconds"),
                language=result.get("language"))
    return result


@router.post("/voice/speak", dependencies=[Depends(require_api_key)])
async def voice_speak(body: VoiceSpeakRequest):
    """Say a reply aloud, then hold the echo guard for its duration."""
    result = await asyncio.to_thread(get_voice().speak, body.text)
    if not result.get("ok"):
        voice_tracker.note_error(
            f"Speech failed: {result.get('error', 'unknown reason')}"
        )
        raise HTTPException(status_code=503, detail=result)
    voice_tracker.note_success()
    bus.publish("voice", "spoke", chars=len(body.text))
    return result


@router.post("/voice/cancel", dependencies=[Depends(require_api_key)])
async def voice_cancel():
    """Stop speaking immediately and drop the echo guard."""
    result = await asyncio.to_thread(get_voice().cancel_speech)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


# ---------------------------------------------------------------------------
# Voice state - the machine the hologram is bound to
# ---------------------------------------------------------------------------
@router.get("/voice/state")
async def voice_state():
    """The derived voice state, plus every input it was derived from.

    The command center's voice rail renders this and nothing else, so the rail
    can never show a state the backend did not compute. Each contributing
    signal is echoed back (``inputs``) so a surprising state is explainable
    rather than mysterious - e.g. an IDLE rail with ``active_runs: 0`` next to a
    running task would be visible immediately.
    """
    status = await asyncio.to_thread(get_voice().status)
    voice_dict = status.as_dict()
    snapshots = store.snapshots()
    state = voice_tracker.snapshot(voice_dict, snapshots)
    state["states"] = list(VOICE_STATES)
    state["inputs"] = {
        "voice_stack": voice_dict,
        "runs": [
            {
                "task_id": s.get("task_id"),
                "status": s.get("status"),
                "phase": s.get("phase"),
            }
            for s in snapshots if s.get("status") in ("queued", "running")
        ],
        "last_error": voice_tracker.last_error,
    }
    bus.publish("voice", "state", state=state["state"], reason=state["reason"])
    return state


# ---------------------------------------------------------------------------
# Devices and pairing
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    """One desktop command, sent by the website or the command center."""

    tool: str = Field(..., description="Desktop tool name, e.g. open_app")
    args: Dict[str, Any] = Field(default_factory=dict)
    #: Set by the caller only after the user actually approved a gated call.
    #: Ignored for anything the policy considers routine.
    confirmed: bool = Field(default=False)
    timeout_s: float = Field(default=45.0, ge=1.0, le=300.0)


@router.post("/execute")
async def execute_on_pc(body: ExecuteRequest,
                        _: Any = Depends(require_api_key)):
    """Run one desktop command on the paired Windows PC and return its result.

    This is the endpoint the website's control surface uses. It does not
    invent actions: it hands the call to the bridge, which sends it to the
    machine and returns what the machine actually did.

    Authorization is enforced twice on purpose. The central policy decides
    whether confirmation is needed, and the Windows agent re-checks
    independently before running anything - so neither a compromised backend
    nor a spoofed ``confirmed: true`` can delete a disk unattended.
    """
    from .core.execution_bridge import (
        ConfirmationRequired, NoLocalAgent, bridge as _bridge,
    )
    from .core.risk import classify, confirmation_prompt

    decision = classify(body.tool, body.args)

    # A gated call with no confirmation returns 409 with the exact prompt, so
    # the UI can show something specific and the user can approve THIS call.
    if decision.requires_confirmation and not body.confirmed:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "requires_confirmation": True,
                "risk": decision.tier,
                "prompt": confirmation_prompt(body.tool, body.args),
                "impact": decision.impact,
                "executed": False,
            },
        )

    if not _bridge.is_online():
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "executed": False,
                "error": "No Windows PC is connected. Pair this machine first; "
                         "nothing was executed.",
                "how_to_enable": "POST /ultron/pairing with kind='pc_agent', "
                                 "then run sara_desktop/local_agent.py on the PC.",
            },
        )

    try:
        outcome = await _bridge.dispatch(
            body.tool, body.args,
            confirmed=body.confirmed, timeout_s=body.timeout_s,
        )
    except NoLocalAgent as exc:
        return JSONResponse(status_code=503,
                            content={"ok": False, "executed": False, "error": str(exc)})
    except TimeoutError as exc:
        return JSONResponse(status_code=504,
                            content={"ok": False, "executed": None, "error": str(exc)})

    return {
        "ok": bool(outcome.get("ok")),
        "executed": True,
        "risk": decision.tier,
        "tool": outcome.get("tool", body.tool),
        "output": outcome.get("output", ""),
        "verified": outcome.get("verified"),
        "error": outcome.get("error", ""),
        "duration_s": outcome.get("duration_s"),
        "command_id": outcome.get("command_id"),
    }


@router.get("/execution")
async def execution_status(_: Any = Depends(require_api_key)):
    """Can this backend actually drive a PC right now?

    Answered from the bridge's live state rather than from configuration, so
    the site can never show "PC online" for a machine that is not connected.
    """
    from .core.execution_bridge import bridge as _bridge

    snap = _bridge.snapshot()
    online = snap["windows_agents_online"] > 0
    return {
        "pc_control_available": online,
        "windows_agents": snap["agents"],
        "pending_commands": snap["pending_commands"],
        "pending": snap["pending"],
        "note": snap["note"],
        "how_to_enable": (
            "Pair this machine: POST /ultron/pairing with kind='pc_agent', then "
            "run sara_desktop/local_agent.py on the PC with the device token."
        ),
    }


@router.get("/policy")
async def authorization_policy(_: Any = Depends(require_api_key)):
    """The authorization policy, so the UI can explain itself.

    Exposing this is not decoration: Phase 9 is only credible if the user can
    see exactly which operations will ask them and which will not.
    """
    from .core.risk import policy_snapshot

    return policy_snapshot()


@router.on_event("startup")
async def _bind_execution_loop() -> None:
    """Hand the server's event loop to the execution bridge.

    The executor runs synchronously in a worker thread, so it cannot own a
    loop. Binding the server loop once at startup is what lets a desktop tool
    still be dispatched to the Windows agent and waited on, without making the
    executor - or any of its existing callers - async.
    """
    import asyncio as _aio

    from .core.execution_bridge import bridge as _bridge

    _bridge.bind_loop(_aio.get_running_loop())
    log.info("Execution bridge bound to the server event loop.")


@router.get("/devices")
async def devices(kind: Optional[str] = None,
                tenant=Depends(optional_tenant)):
    """Every paired device, with a state derived from its last heartbeat.

    Read-only and open, matching ``/capabilities``: a device list is a status
    board. It exposes labels, kinds, scopes and connection ages - never a token,
    never a token hash, and never a client-supplied string that is not on the
    allowlist in ``core/devices.py``. An unauthenticated caller is scoped to
    the local tenant (``_owner_scope``); a signed-in caller sees only theirs.
    """
    owner = _owner_scope(tenant)
    return {
        "devices": device_registry.list(kind, owner_id=owner),
        "summary": device_registry.summary(owner_id=owner),
        "kinds": list(DEVICE_KINDS),
        "scopes": {k: list(v) for k, v in DEVICE_SCOPES.items()},
        "policy": {
            "pairing_ttl_s": 120,
            "online_window_s": 20,
            "stale_window_s": 90,
            "note": "Device tokens are stored hashed; revoking one takes effect "
                    "on its very next request.",
        },
    }


@router.post("/pairing", dependencies=[Depends(require_api_key)])
async def create_pairing(body: PairingRequest, request: Request):
    """Mint a single-use pairing token and its QR code.

    The token is returned exactly once and only its SHA-256 is retained, so it
    cannot be recovered afterwards - not by this process, not by anyone reading
    its memory. It expires in two minutes and is destroyed the moment it is
    redeemed.

    ``pair_base_url`` lets the QR open the frontend redeem page instead of the
    backend route. It defaults to the base URL this request arrived on, so no
    hostname is hardcoded anywhere.
    """
    try:
        offer = device_registry.create_offer(
            kind=body.kind,
            label=body.label,
            backend_url=(body.pair_base_url or str(request.base_url)),
            ttl_s=body.ttl_s,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bus.publish("device", "pairing_offered", device_kind=offer["kind"],
                label=offer["label"], expires_in_s=offer["expires_in_s"])
    return offer


@router.get("/pair/claim")
async def pair_claim_page(t: str = ""):
    """Redeem page for the QR code, served BY the backend.

    This route existed only as the string a QR encoded
    (``PairingOffer.payload``) with no handler behind it, so scanning a pairing
    QR returned 404 and phone pairing could never work. It is served from the
    backend rather than the frontend on purpose: the QR carries the backend URL,
    and a phone that can reach the backend must be able to finish pairing even
    if it cannot reach the web UI (different network, VPN, captive portal).

    The page is one self-contained document with no external assets, because
    everything else about a QR handshake assumes a phone with no session and no
    bundler. It posts the token, then shows the result plainly.
    """
    token = (t or "").strip()
    if not token:
        return HTMLResponse(_render_pair_page(
            "missing", detail="No pairing token in this link."),
            status_code=400)

    offer = device_registry.pending_offer(token)
    if offer is None:
        # Same shape as the API: an unknown, expired and already-used token are
        # indistinguishable, so this page cannot be used to test whether a given
        # token ever existed.
        return HTMLResponse(_render_pair_page(
            "invalid",
            detail=("This pairing link is invalid, expired, or already used. "
                    "Mint a new one on the command center and scan again.")),
            status_code=410)

    return HTMLResponse(_render_pair_page(
        "ready", token=token,
        detail=f"Ready to pair a {offer['kind']} named {offer['label']}."))


@router.get("/pairing/offer")
async def pairing_offer(t: str):
    """Safe view of an unredeemed offer, for a redeem page's countdown.

    Deliberately reveals nothing about *other* offers: an unknown token returns
    ``{"valid": false}`` rather than 404, so this endpoint cannot be used to
    enumerate live tokens.
    """
    offer = device_registry.pending_offer(t)
    if offer is None:
        return {"valid": False}
    return {"valid": True, **offer}


#: Networks a local agent may auto-pair from.
#:
#: Written out explicitly rather than using ``ipaddress.is_private``, which
#: also returns True for the documentation blocks (192.0.2.0/24, 203.0.113.0/24)
#: and for reserved space. Those are not LANs, and a security boundary should
#: say exactly what it admits.
_AUTOPAIR_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",     # IPv4 loopback
        "::1/128",         # IPv6 loopback
        "10.0.0.0/8",      # RFC1918
        "172.16.0.0/12",   # RFC1918 (includes the Docker bridge)
        "192.168.0.0/16",  # RFC1918
        "169.254.0.0/16",  # link-local, incl. cloud metadata 169.254.169.254
    )
)


#: Every value interpolated into the page below is reduced to this alphabet.
#: The token is generated by ``secrets.token_urlsafe`` and the label is already
#: stripped of non-printable characters, so this costs nothing - but a template
#: that puts request-derived values into HTML and JavaScript must not depend on
#: that. Note it strips quotes, so a substituted value can never close the
#: string literal it sits in.
_PAIR_PAGE_ESCAPE = re.compile(r"[^A-Za-z0-9 _\-.]")

_PAIR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SARA · Pair device</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
      background:#070b12;color:#e8f6ff;
      font:16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
 .card{max-width:30rem;width:100%;border:1px solid #1d3350;border-radius:14px;
       padding:26px;background:#0c1420}
 h1{margin:0 0 6px;font-size:1.15rem;letter-spacing:.14em}
 .dim{color:#7f97ad;font-size:.9rem;margin:0 0 18px}
 button{width:100%;padding:14px;border:0;border-radius:10px;cursor:pointer;
        font:600 1rem/1 inherit;letter-spacing:.06em;
        background:linear-gradient(180deg,#00d4ff,#0090c8);color:#00131c}
 button:disabled{opacity:.45;cursor:default}
 code{display:block;word-break:break-all;background:#08111c;border:1px solid #1d3350;
      border-radius:8px;padding:10px;margin-top:10px;font-size:.78rem;color:#8fd8f5}
 .ok{color:#22d3a5}.bad{color:#ff2d6f}
</style></head><body><div class="card">
<h1>SARA · PAIR DEVICE</h1>
<p class="dim">@@DETAIL@@</p>
<div id="out"></div>
<script>
var TOKEN = "@@TOKEN@@", STATE = "@@STATE@@";
var out = document.getElementById("out");
function show(cls, html) {
  out.innerHTML = '<p class="' + cls + '">' + html + '</p>';
}
if (STATE === "ready") {
  var btn = document.createElement("button");
  btn.textContent = "PAIR THIS DEVICE";
  out.appendChild(btn);
  btn.onclick = function () {
    btn.disabled = true; btn.textContent = "PAIRING…";
    fetch("/ultron/pairing/claim", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: TOKEN, label: navigator.platform || "device"})
    }).then(function (r) { return r.json().then(function (b) {
      return {status: r.status, body: b};
    }); }).then(function (res) {
      if (res.status === 200 && res.body.device_token) {
        show("ok", "Paired. You can close this page and return to SARA.");
        var c = document.createElement("code");
        c.textContent = "device token: " + res.body.device_token;
        out.appendChild(c);
        var n = document.createElement("code");
        n.textContent = "Store it as SARA_DEVICE_TOKEN if you run the agent by hand.";
        out.appendChild(n);
      } else {
        btn.disabled = false; btn.textContent = "PAIR THIS DEVICE";
        show("bad", "Pairing failed: " +
            (res.body.detail || ("HTTP " + res.status)));
      }
    }).catch(function (e) {
      btn.disabled = false; btn.textContent = "PAIR THIS DEVICE";
      show("bad", "Could not reach the backend: " + e);
    });
  };
} else if (STATE === "invalid") {
  show("bad", "This link is no longer usable.");
} else {
  show("bad", "Nothing to pair.");
}
</script></div></body></html>
"""


def _render_pair_page(state: str, token: str = "", detail: str = "") -> str:
    """Fill the template by substitution, not str.format.

    The page contains CSS and JavaScript blocks, so brace escaping would be
    both noisy and easy to get wrong - and a missed escape is a 500 at runtime,
    not a visible mistake. Substituting three explicit sentinels has no such
    failure mode.
    """
    return (_PAIR_PAGE
            .replace("@@STATE@@", _PAIR_PAGE_ESCAPE.sub("", state))
            # The token lands inside a JavaScript string literal, so it is
            # sanitised like every other substituted value rather than trusted
            # to be well-formed because secrets.token_urlsafe generated it.
            .replace("@@TOKEN@@", _PAIR_PAGE_ESCAPE.sub("", token))
            .replace("@@DETAIL@@", _PAIR_PAGE_ESCAPE.sub("", detail)))


def _is_local_address(host: str) -> bool:
    """True for loopback and RFC1918 peers only.

    This is the entire security boundary for auto-pairing, so it is written to
    fail closed: anything not a recognisable local address - including a proxy
    reporting an empty or unparseable host - is rejected.
    """
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in net for net in _AUTOPAIR_NETWORKS)


@router.post("/pairing/auto")
async def auto_pair(body: AutoPairRequest, request: Request,
                   tenant=Depends(current_tenant)):
    """Pair THIS machine with no human step.

    The desktop agent dials out to this endpoint on startup so the user never
    has to mint a token, copy it into an environment variable and restart. It is
    safe because of ``_is_local_address``: only a peer on loopback or a private
    network can reach it, and reaching it still proves nothing beyond "this
    request came from the operator's own machine or LAN".

    Deliberate limitation, stated rather than hidden: a containerised backend
    sees the agent as a bridge gateway (e.g. 172.17.0.1), which is private, so
    this succeeds there too - but so would any other container on that bridge.
    Do not expose the backend to the public internet with auto-pair on; set
    ``SARA_AUTOPAIR=0`` there, and the ordinary QR flow still works.
    """
    if not AUTO_PAIR_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=("Auto-pairing is disabled (SARA_AUTOPAIR=0). Pair this "
                    "machine with POST /ultron/pairing and redeem the token."))

    address = request.client.host if request.client else ""
    if not _is_local_address(address):
        raise HTTPException(
            status_code=403,
            detail=("Auto-pairing is only available from this machine or a "
                    f"private network. Your address was rejected (peer={address!r}). "
                    "Use the QR pairing flow instead."))

    try:
        result = device_registry.auto_pair(
            owner_id=tenant.id,
            kind=body.kind, label=body.label, platform=body.platform,
            address=address,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not result.get("reused"):
        bus.publish("device." + result["device"]["device_id"], "paired",
                    device_kind=result["device"]["kind"],
                    label=result["device"]["label"], how="auto")
        bus.publish("device", "device_paired", device=result["device"])
    return result


@router.post("/pairing/claim")
async def claim_pairing(body: PairingClaimRequest, request: Request,
                       tenant=Depends(optional_tenant)):
    """Redeem a pairing token for a long-lived device token.

    This is the only unauthenticated write in the pairing flow, which is why it
    is the only one that is throttled: eight bad attempts from one address
    triggers a two-minute lockout, and every failure returns the same message so
    a caller cannot distinguish "never existed" from "already used".

    It must stay callable without a key: the client is the phone's pair page
    (served by this backend), which has nothing to present but the token. The
    single-use, expiring token IS the credential - only someone holding it can
    complete a pairing - and the new device is scoped to the local tenant, the
    tenant whose operator minted the QR (``_owner_scope``).
    """
    address = (request.client.host if request.client else "unknown")
    try:
        result = device_registry.claim(
            owner_id=_owner_scope(tenant),
            token=body.token,
            kind=body.kind,
            label=body.label,
            platform=body.platform,
            address=address,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    bus.publish("device." + result["device"]["device_id"], "paired",
                device_kind=result["device"]["kind"], label=result["device"]["label"])
    bus.publish("device", "device_paired", device=result["device"])
    return result


@router.post("/devices/{device_id}/revoke", dependencies=[Depends(require_api_key)])
async def revoke_device(device_id: str, body: RevokeRequest,
                       tenant=Depends(current_tenant)):
    """Cut one device off. Takes effect on its very next request.

    Revocation is sticky and is checked before the scope check, so a revoked
    token can never be traded for a new connection.
    """
    if not device_registry.revoke(device_id, body.reason,
                                  owner_id=tenant.id):
        raise HTTPException(status_code=404, detail=f"No device {device_id!r}.")
    bus.publish("device." + device_id, "revoked", reason=body.reason)
    return {"revoked": True, "device_id": device_id,
            "device": device_registry.get(device_id).as_dict()}


@router.post("/emergency-stop", dependencies=[Depends(require_api_key)])
async def emergency_stop(body: EmergencyStopRequest):
    """GLOBAL STOP: halt every run and cut every device link.

    This is the panic button the command center's STOP ALL control calls. It is
    deliberately server-side rather than a loop of per-task DELETE requests
    from the browser: a stop that depends on the UI staying responsive is not a
    stop.

    Device *links* are cut unconditionally. Device *tokens* are only revoked
    when the caller explicitly asks, because revoking them forces every paired
    phone through the QR dance again and that is not something a stop button
    should do by surprise.
    """
    stopped: List[str] = []
    for snapshot in store.snapshots():
        if snapshot.get("phase") in TERMINAL_PHASES:
            continue
        session = store.get(str(snapshot.get("task_id")))
        if session is None:
            continue
        session.stop.set()
        # Unblock any run parked on the approval gate.
        session.approve_q.put(("", False))
        session.log("[ULTRON] Global stop requested by operator.")
        stopped.append(session.task_id)

    links_cut = []
    for device in device_registry.list():
        if device["state"] == "online":
            links_cut.append(device["device_id"])
            device_registry.set_connected(device["device_id"], False)

    revoked = (
        device_registry.revoke_all("global stop") if body.revoke_devices else 0
    )
    bus.publish("system", "stop_all", tasks=stopped, links_cut=links_cut,
                devices_revoked=revoked)
    log.warning("GLOBAL STOP: halted %d task(s), cut %d link(s), revoked %d device(s)",
                len(stopped), len(links_cut), revoked)
    return {
        "stopped_tasks": stopped,
        "stopped_count": len(stopped),
        "links_cut": links_cut,
        "devices_revoked": revoked,
        "note": "Runs already in a terminal phase were left alone.",
    }


# ---------------------------------------------------------------------------
# Device-scoped writes
# ---------------------------------------------------------------------------
def _require_device(token: str, scope: str) -> Any:
    """Authenticate a device token and check one scope, or raise.

    The device token is a *different* credential from the SARA API key: it is
    per-device, revocable, and carries only the scopes above. Rejecting a
    missing scope with 403 (rather than 401) is deliberate - the token was
    valid, the permission was not.
    """
    device = device_registry.authenticate(token)
    if device is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid, expired or revoked device token.",
        )
    if not device.has(scope):
        raise HTTPException(
            status_code=403,
            detail=f"Device {device.device_id} ({device.kind}) lacks scope '{scope}'.",
        )
    return device


@router.post("/tasks", dependencies=[Depends(require_api_key)])
async def submit_task(body: RunRequest):
    """Start a run on behalf of an authenticated caller.

    Used by the command center's own controls and by a paired device carrying
    the ``task.submit`` scope. It is the same code path as ``/run`` - one
    implementation, so a phone and the web console cannot drift apart.
    """
    return await run_task(body)


@router.post("/device/tasks")
async def device_submit_task(
    body: RunRequest,
    x_ultron_device_token: str = Header(default="", alias="X-Ultron-Device-Token"),
):
    """Start a run from a paired device (scope ``task.submit``).

    A phone is a first-class way to give Ultron an objective, so this is a real
    run with a real task_id and a real SSE stream - not a message that lands in
    a void.
    """
    device = _require_device(x_ultron_device_token, "task.submit")
    result = await run_task(body)
    bus.publish("device." + device.device_id, "task_submitted",
                task_id=result["task_id"])
    return {**result, "submitted_by": device.device_id}


@router.post("/device/voice/listen")
async def device_voice_listen(
    body: VoiceListenRequest,
    x_ultron_device_token: str = Header(default="", alias="X-Ultron-Device-Token"),
):
    """Listen from a paired phone (scope ``voice.listen``).

    Same recogniser, same echo guard, same Whisper pass as the desktop mic - the
    request is forwarded rather than reimplemented so the two paths cannot
    diverge.
    """
    _require_device(x_ultron_device_token, "voice.listen")
    return await voice_listen(body)


@router.post("/device/voice/speak")
async def device_voice_speak(
    body: VoiceSpeakRequest,
    x_ultron_device_token: str = Header(default="", alias="X-Ultron-Device-Token"),
):
    """Speak a reply out of the host machine (scope ``voice.speak``).

    The audio comes from the backend's own sound device, because that is where
    the voice stack lives. The phone is the control surface, not the speaker.
    """
    _require_device(x_ultron_device_token, "voice.speak")
    return await voice_speak(body)


# ---------------------------------------------------------------------------
# Context Awareness Engine
# ---------------------------------------------------------------------------
def _require_permission(gate: str, what: str = "") -> None:
    """Turn a permission gate into an HTTP answer rather than a traceback.

    ``PermissionDenied`` subclasses ``PermissionError``, and Starlette does not
    map either to 403 - without this a denied gate reaches the client as a 500
    with a stack trace, which tells the user nothing and looks like a crash
    rather than a switch they threw. 403 for a gate they have not granted, 400
    for a gate that does not exist, which is a different mistake entirely.
    """
    try:
        permissions.require(gate, what)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PermissionError as exc:  # unknown gate name
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/awareness")
async def awareness_state():
    """What the user is looking at right now, and what they were looking at.

    A status board: window titles, application names, URLs - all of which the
    user granted via ``/permissions/screen``. Never window contents,
    keystrokes or image data.

    ``blocked`` is the honest signal. With screen access off, the observer is
    not running and this says so rather than serving a stale context as if it
    were live.
    """
    payload = context_manager.snapshot()
    payload["observer"] = observer.status.as_dict()
    payload["vision"] = vision.latest()
    payload["vision_stats"] = vision.stats()
    payload["always_on"] = always_on.snapshot()
    payload["permissions"] = permissions.snapshot()
    return payload


@router.get("/awareness/history", dependencies=[Depends(require_api_key)])
async def awareness_history(limit: int = 20):
    """The bounded context timeline, newest first. In memory; never persisted."""
    return {
        "timeline": context_manager.timeline(limit=max(1, min(limit, 50))),
        "note": ("History is in-memory and bounded; it is not a surveillance "
                 "record and nothing here is written to disk."),
    }


@router.post("/awareness/report", dependencies=[Depends(require_api_key)])
async def report_context(body: ContextReport):
    """Accept a context observation from a desktop runtime client.

    How a runtime on another machine feeds awareness: it reads the foreground
    window itself and posts the result. The ``screen`` permission is required
    server-side too, because the context it produces drives Ultron exactly as a
    local observation would.
    """
    _require_permission("screen", "report screen context")
    if not (body.title or "").strip():
        return {"changed": False, "reason": "empty title"}
    window = RawWindow(title=body.title, process=body.process, pid=body.pid,
                       hwnd=body.hwnd, url=body.url)
    result = context_manager.observe(window, source="runtime")
    if body.task is not None:
        context_manager.set_task(body.task)
    if body.last_action is not None:
        context_manager.record_action(body.last_action, body.last_action_ok)
    return result


@router.post("/awareness/task", dependencies=[Depends(require_api_key)])
async def set_context_task(body: TaskRequest):
    """Set or clear the active task shown as CURRENT TASK."""
    task = (body.task or "").strip()[:200]
    context_manager.set_task(task)
    return {"task": task, "current": context_manager.snapshot().get("current")}


@router.post("/awareness/action", dependencies=[Depends(require_api_key)])
async def record_context_action(body: ActionRequest):
    """Record LAST ACTION. ``verified=False`` renders differently from success."""
    action = (body.action or "").strip()[:200]
    if not action:
        raise HTTPException(status_code=400, detail="An 'action' is required.")
    return context_manager.record_action(action, body.verified)


@router.post("/awareness/back", dependencies=[Depends(require_api_key)])
async def switch_back():
    """"Go back to the previous window." Appended as a new event, not a rewind."""
    restored = context_manager.switch_back()
    if restored is None:
        raise HTTPException(status_code=404, detail="There is no previous window.")
    return {"restored": True, "current": context_manager.snapshot().get("current")}


@router.post("/awareness/resolve", dependencies=[Depends(require_api_key)])
async def resolve_reference_route(body: ResolveRequest):
    """Resolve "this", "it", "the second one", "the tab I was using".

    Returns ``resolved: false`` plus a short question when the context cannot
    settle it. That is the point of the endpoint: a follow-up should ask rather
    than act on a guess.
    """
    current = context_manager.snapshot().get("current") or {}
    latest = vision.latest() or {}
    revision = int(current.get("revision") or 0)
    stale = int(latest.get("revision") or -1) != revision
    # Only pass the element list when it describes the window in front of the
    # user *right now*. A tree from a window they have since left still looks
    # perfectly valid, and resolving "the second one" against it produces a
    # confident answer to the wrong question - which is the one outcome this
    # endpoint exists to prevent.
    elements = None if stale else latest.get("elements")
    result = resolve_reference(body.text, elements).as_dict()
    result["context_revision"] = revision
    result["elements_stale"] = stale
    return result


# ---------------------------------------------------------------------------
# Screen understanding
# ---------------------------------------------------------------------------
@router.post("/vision/analyse", dependencies=[Depends(require_api_key)])
async def vision_analyse(body: VisionRequest):
    """Answer a question about the screen as cheaply as possible.

    Accessibility tree first, then what the adapters already know, and a
    screenshot only when neither can answer. An unchanged context is a no-op:
    the same question twice returns ``changed: false`` without re-reading.
    """
    snapshot = context_manager.snapshot()
    result = vision.analyse(snapshot, question=body.question, force=body.force)
    context_manager.set_vision(result.as_dict())
    return result.as_dict()


@router.get("/vision")
async def vision_state():
    """The last vision read and what it cost. Never any image data."""
    return {"latest": vision.latest(), "stats": vision.stats()}


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
@router.get("/permissions")
async def permission_state():
    """The three independent switches, plus a short audit of what changed."""
    return {**permissions.snapshot(), "gate_names": list(GATES),
            "audit": permissions.audit(10),
            "observer_running": observer.running,
            "always_on_running": always_on.running}


@router.post("/permissions/{gate}", dependencies=[Depends(require_api_key)])
async def set_permission(gate: str, body: PermissionRequest):
    """Grant or revoke one capability, with immediate effect.

    Revoking ``screen`` stops the observer and ``microphone`` stops the
    always-on loop *in this handler*, not on the next tick - a switch the user
    has just thrown must not still be live a second later.
    """
    try:
        result = permissions.set(gate, body.allowed, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    side_effect = ""
    if not body.allowed:
        if gate == "screen" and observer.running:
            observer.stop()
            side_effect = "observer stopped"
        elif gate == "microphone" and always_on.running:
            always_on.stop("microphone permission revoked")
            side_effect = "always-on loop stopped"

    return {"gate": gate, **result, "side_effect": side_effect,
            "always_on": always_on.running, "observer": observer.running}


@router.post("/observer", dependencies=[Depends(require_api_key)])
async def control_observer(body: ObserverRequest):
    """Start or stop the foreground-window observer."""
    _require_permission("screen", "observe the active window")
    if not body.running:
        return observer.stop("operator")
    # Restart with the requested cadence rather than ignoring it.
    if observer.running and observer.interval_s != body.interval_s:
        observer.stop("interval change")
    observer.interval_s = max(0.25, min(body.interval_s, 30.0))
    return observer.start()


# ---------------------------------------------------------------------------
# Always-on microphone
# ---------------------------------------------------------------------------
@router.get("/voice/always-on")
async def always_on_state():
    """Live state of the persistent listening loop."""
    return always_on.snapshot()


@router.post("/voice/always-on", dependencies=[Depends(require_api_key)])
async def start_always_on(body: AlwaysOnRequest):
    """Turn the microphone ON and keep it on until told otherwise.

    One call, and it does not stop after one command. A ``goal`` seeds the task
    the loop reports; otherwise whatever is heard starts a normal Ultron run.
    """
    if body.goal:
        seed = body.goal
        always_on.set_handler(_make_always_on_handler(seed))
    elif always_on.handler_missing:
        always_on.set_handler(_make_always_on_handler(""))
    started = always_on.start()
    if body.goal and started.get("started"):
        context_manager.set_task(body.goal)
    return started


@router.post("/voice/always-on/stop", dependencies=[Depends(require_api_key)])
async def stop_always_on(body: AlwaysOnStopRequest):
    """Turn the microphone OFF. Also stops any speech in flight."""
    return always_on.stop(body.reason)


@router.post("/voice/always-on/barge-in", dependencies=[Depends(require_api_key)])
async def barge_in(body: AlwaysOnRequest):
    """Cut speech short because the operator started talking."""
    if not body.speaking:
        return {"barge_in": False, "reason": "nothing was speaking"}
    return always_on.barge_in()


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------
def _api_key_ok(provided: str) -> bool:
    """Constant-time check of the shared API key for a WebSocket handshake.

    ``require_api_key`` is an HTTP dependency, so it cannot guard an upgrade. The
    token arrives as a query parameter, which is the only option a browser
    WebSocket offers - see the deployment notes on terminating TLS in front of
    this, because query strings end up in proxy logs.
    """
    if not API_KEY:
        return False
    return bool(provided) and hmac.compare_digest(provided.strip(), API_KEY)


@router.websocket("/link")
async def device_link(websocket: WebSocket):
    """Authenticated device link: heartbeat in, events and control out.

    Protocol (JSON text frames both ways):

      -> {"type": "hello", "name": "...", "platform": "..."}
      <- {"type": "welcome", "device": {...}, "scopes": [...]}
      -> {"type": "heartbeat"}                      (every <=20 s)
      <- {"type": "pong", "seq": n}
      -> {"type": "submit", "goal": "..."}          (needs task.submit)
      <- {"type": "task", "task_id": "..."}
      <- {"type": "event", ...}                     (bus events, scoped to you)
      -> {"type": "info", "focus": "chrome"}        (allowlisted client fields)

    The socket closes the moment the token is revoked, which is what makes
    revocation immediate rather than eventual.
    """
    token = websocket.query_params.get("token", "")
    device = device_registry.authenticate(token)
    if device is None:
        # 1008 = policy violation. The reason deliberately says nothing about
        # which part of the token was wrong.
        await websocket.close(code=1008, reason="invalid device token")
        return

    await websocket.accept()
    device_registry.set_connected(device.device_id, True)
    device_registry.heartbeat(device.device_id)
    bus.publish("device." + device.device_id, "link_open",
                label=device.label, device_kind=device.kind)

    async def _send(payload: Dict[str, Any]) -> None:
        await websocket.send_json(payload)

    await _send({
        "type": "welcome",
        "device": device.as_dict(),
        "scopes": list(device.scopes),
        "heartbeat_s": 20,
        "note": "Send a heartbeat at least every 20s or the device decays to "
                "'stale' on the command center.",
    })

    # A machine claiming to be the Windows executor registers with the bridge,
    # which is what lets plan steps reach a real desktop instead of executing
    # in the Linux container. Registration is additive: a phone link still
    # works exactly as before, it simply is not an executor.
    from .core.execution_bridge import ExecutionBridge, LocalAgent
    from .core.execution_bridge import bridge as _bridge

    _is_windows_executor = bool({"pc.execute", "computer.*"} & set(device.scopes))
    _local_agent: Optional[LocalAgent] = None
    if _is_windows_executor:
        _local_agent = LocalAgent(
            device_id=device.device_id,
            label=device.label,
            send=_send,
            scopes=set(device.scopes),
        )
        _bridge.register_agent(_local_agent)
        await _send({"type": "executor_ready", "device_id": device.device_id,
                     "note": "You may now receive commands to execute."})

    # Two subscriptions on purpose: this device's own channel, plus the
    # ``system`` channel that carries GLOBAL STOP. Deliberately NOT ``*`` - the
    # bus also carries every other task's live events, including other users'
    # goal text, and a phone must not see those.
    own_events = bus.subscribe("device." + device.device_id)
    system_events = bus.subscribe("system")
    pusher: Optional[asyncio.Task] = None

    async def _pump() -> None:
        """Forward bus events to this device while the socket is open."""
        while True:
            event = await asyncio.to_thread(system_events.get, 1.0)
            if event is None:
                event = own_events.get()
            if event is None:
                # Idle tick doubles as the server-side keepalive.
                try:
                    await websocket.send_json({"type": "tick"})
                except Exception:  # noqa: BLE001 - socket already gone
                    return
                continue
            try:
                await _send({"type": "event", **event})
            except Exception:  # noqa: BLE001
                return

    pusher = asyncio.create_task(_pump())

    try:
        while True:
            raw = await websocket.receive_json()
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "").strip().lower()

            if kind == "heartbeat":
                device_registry.heartbeat(device.device_id, raw.get("info") or {})
                await _send({"type": "pong", "t": time.time()})

            elif kind == "info":
                updated = device_registry.heartbeat(device.device_id, raw)
                await _send({"type": "info_ack",
                             "device": updated.as_dict() if updated else None})

            elif kind == "submit":
                goal = str(raw.get("goal") or "").strip()
                if not device.has("task.submit"):
                    await _send({"type": "error",
                                 "error": "This device lacks scope 'task.submit'."})
                elif not goal:
                    await _send({"type": "error", "error": "Empty goal."})
                else:
                    created = await run_task(
                        RunRequest(goal=goal, mode=str(raw.get("mode") or "autonomous"))
                    )
                    await _send({"type": "task", **created})

            elif kind == "cancel_speech":
                if device.has("voice.cancel"):
                    result = await asyncio.to_thread(get_voice().cancel_speech)
                    await _send({"type": "speak_cancelled", **result})
                else:
                    await _send({"type": "error",
                                 "error": "This device lacks scope 'voice.cancel'."})

            elif kind == "result":
                # A Windows agent finished a command the execution bridge
                # dispatched to it. ``settle`` drops any result naming an
                # unknown or already-settled command_id, which is what makes a
                # replayed or duplicated frame inert instead of a second
                # execution.
                from .core.execution_bridge import bridge as _bridge

                accepted = await _bridge.settle(
                    {**raw, "device_id": device.device_id}
                )
                await _send({
                    "type": "result_ack",
                    "command_id": raw.get("command_id"),
                    "accepted": bool(accepted),
                    **({} if accepted else
                       {"reason": "unknown or already-settled command_id"}),
                })

            else:
                await _send({"type": "error",
                             "error": f"Unknown frame type {kind!r}."})

            # Revocation is enforced on every frame, not just at connect time,
            # so a revoke lands on the socket within one round trip.
            current = device_registry.get(device.device_id)
            if current is None or current.revoked:
                await _send({"type": "revoked", "detail": device.revoked_reason})
                await websocket.close(code=1008, reason="device revoked")
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the server
        log.warning("Device link %s ended: %s: %s", device.device_id,
                    type(exc).__name__, exc)
    finally:
        if pusher is not None:
            pusher.cancel()
        own_events.close()
        system_events.close()
        device_registry.set_connected(device.device_id, False)
        # Dropping the agent here is what makes a lost connection visible as
        # "offline" instead of silently queued commands.
        from .core.execution_bridge import bridge as _bridge
        _bridge.unregister_agent(device.device_id)
        bus.publish("device." + device.device_id, "link_closed", label=device.label)


@router.websocket("/ws")
async def ui_stream(websocket: WebSocket):
    """Read-only realtime feed for the command center.

    Authenticated with the shared SARA API key, because the bus carries live
    task events - which include goal text.

    Sends a short replay buffer on connect so a page that loaded a moment late
    is not missing the device-paired event it was waiting for, then streams
    live. Events are pushed verbatim; no filtering, because the caller is
    already trusted with the data.
    """
    token = websocket.query_params.get("token", "")
    if not _api_key_ok(token):
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept()
    subscription = bus.subscribe("*")
    for event in bus.recent("*", limit=25):
        await websocket.send_json({"type": "replay", **event})

    try:
        while True:
            # Nothing is expected from the browser; this loop exists only to
            # forward, and to notice a disconnect promptly.
            event = await asyncio.to_thread(subscription.get, 1.0)
            if event is None:
                try:
                    await websocket.send_json({"type": "tick"})
                except Exception:  # noqa: BLE001
                    break
                continue
            await websocket.send_json({"type": "event", **event})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("UI stream ended: %s: %s", type(exc).__name__, exc)
    finally:
        subscription.close()
