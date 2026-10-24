"""Sara Bots API — the real-agent workspace surface (OpenDots-inspired UI's backend).

Every route is protected by the platform's existing shared API key
(``require_api_key``); reuse, not a second auth system. All agent data comes
from the honest registry (static AST discovery + real fleet probes) and all
execution goes through the audited runner.

Routes:
  GET  /api/sara-bots/bots                  registry snapshot (live probes)
  GET  /api/sara-bots/bots/{bot_id}         one bot (validated id)
  POST /api/sara-bots/bots/{bot_id}/run     start a REAL run (validated input)
  GET  /api/sara-bots/runs                  run history
  GET  /api/sara-bots/runs/{run_id}         one run record
  POST /api/sara-bots/runs/{run_id}/cancel  terminate a live local run
  POST /api/sara-bots/bots/{bot_id}/approval {action} approve|revoke|disable|enable
  GET  /api/sara-bots/root                  discovery-root info
  POST /api/sara-bots/root                  set + validate the root
  POST /api/sara-bots/rescan                re-run static discovery (executes nothing)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.services.sara_bots.registry import get_registry
from app.services.sara_bots.runner import (ConfirmationRequired, RateLimited,
                                           get_runner)

log = logging.getLogger("sara.bots.api")

router = APIRouter(prefix="/api/sara-bots", tags=["Sara Bots"],
                   dependencies=[Depends(require_api_key)])

#: Fleet probe cache — probing is real HTTP against our own routes, so it is
#: bounded (2s per probe, 8 concurrent, 15 probes) and cached 10s.
_FLEET_CACHE_TTL = 10.0
_fleet_cache: Dict[str, Any] = {"snapshot": None, "at": 0.0}


async def _fleet_snapshot(request: Request, refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    cached = _fleet_cache["snapshot"]
    if not refresh and cached and now - _fleet_cache["at"] < _FLEET_CACHE_TTL:
        return cached
    from app.agents.sara_ultron_agent.core import fleet

    base_url = str(request.base_url)
    api_key = request.headers.get("x-api-key", "")
    if not api_key and request.headers.get("authorization", "").lower().startswith("bearer "):
        api_key = request.headers["authorization"][7:].strip()
    snapshot = await fleet.probe_fleet(base_url, api_key=api_key)
    # The agency bot is probed through its own LLM health route (the fleet
    # does not cover it) — a real request, classified from its payload.
    snapshot["agents"].append(await _probe_agency(base_url, api_key))
    _fleet_cache["snapshot"] = snapshot
    _fleet_cache["at"] = now
    return snapshot


async def _probe_agency(base_url: str, api_key: str = "") -> Dict[str, Any]:
    import httpx

    entry = {
        "id": "agency-specialists",
        "name": "Agency Specialists",
        "status": "unreachable",
        "http_status": None,
        "latency_ms": None,
        "detail": "LLM health probe failed",
        "checked_at": time.time(),
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.5, trust_env=False,
                                     follow_redirects=False) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/api/agency/llm/health",
                headers={"X-API-Key": api_key} if api_key else {})
        entry["http_status"] = resp.status_code
        entry["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if resp.status_code == 200:
            payload = resp.json()
            providers = payload.get("any_available")
            entry["status"] = "online" if providers else "degraded"
            entry["detail"] = ("LLM failover chain online" if providers else
                               "no LLM provider available — add a key to backend/.env")
        elif resp.status_code == 401:
            entry["detail"] = "probe unauthorized (401) — caller key was not forwarded"
    except Exception as exc:  # noqa: BLE001 — probe failure is data
        entry["detail"] = f"{type(exc).__name__}: probe failed"
    return entry


def _bots_payload(request_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    reg = get_registry()
    reg.ensure_scanned()
    states = reg.bot_states(request_snapshot)
    bots = []
    # Non-functioning agents are pruned from the workspace: a hub bot whose
    # live probe says `unavailable` is not shown at all (its count is still
    # reported in the summary — nothing is hidden silently). Local actions
    # are never pruned: they are real, approvable and run in the worker.
    hidden_unavailable = 0
    for spec in reg.hub_bots():
        record = spec.to_dict()
        record.update(states.get(spec.id, {}))
        record["orb"] = {"hue": spec.hue}
        if record.get("state") == "unavailable":
            hidden_unavailable += 1
            continue
        bots.append(record)
    for action in reg.discovered_actions():
        spec = reg.local_bot(f"action.{action.name}")
        if spec is None:
            continue
        record = spec.to_dict()
        record.update(states.get(spec.id, {}))
        record["orb"] = {"hue": spec.hue}
        record["note"] = action.note
        bots.append(record)
    # The OpenMuse-powered primary agent leads the fleet when it exists —
    # a live view of the real custom bot (deep-links to its workspace).
    saramuse = reg.saramuse_bot()
    if saramuse is not None:
        record = saramuse.to_dict()
        record.update(states.get(saramuse.id, {}))
        record["orb"] = {"hue": saramuse.hue}
        bots.insert(0, record)
    counts: Dict[str, int] = {}
    for bot in bots:
        counts[bot["state"]] = counts.get(bot["state"], 0) + 1
    return {
        "bots": bots,
        "summary": {"total": len(bots), "by_state": counts,
                    "hidden_unavailable": hidden_unavailable,
                    "fleet_checked_at": request_snapshot.get("summary", {}).get("checked_at")},
        "scan": reg.scan_info(),
    }


# ── registry ─────────────────────────────────────────────────────────────────

@router.get("/bots")
async def list_bots(request: Request, probe: str = "auto"):
    reg = get_registry()
    reg.ensure_scanned()
    refresh = probe == "refresh"
    snapshot = await _fleet_snapshot(request, refresh=refresh)
    return _bots_payload(snapshot)


@router.get("/bots/{bot_id}")
async def get_bot(bot_id: str, request: Request):
    reg = get_registry()
    reg.ensure_scanned()
    spec = reg.bot(bot_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"bot '{bot_id}' not found")
    snapshot = await _fleet_snapshot(request)
    states = reg.bot_states(snapshot)
    record = spec.to_dict()
    record.update(states.get(spec.id, {}))
    record["orb"] = {"hue": spec.hue}
    record["recent_runs"] = get_runner().store.list(bot_id=bot_id, limit=10)
    return record


# ── execution ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=45.0, ge=5.0, le=180.0)
    confirm: bool = False


@router.post("/bots/{bot_id}/run", status_code=202)
async def run_bot(bot_id: str, req: RunRequest, request: Request):
    if len(json_dumps(req.params)) > 32_000:
        raise HTTPException(status_code=413, detail="params payload too large")
    api_key = request.headers.get("x-api-key", "")
    if not api_key and request.headers.get("authorization", "").lower().startswith("bearer "):
        api_key = request.headers["authorization"][7:].strip()
    try:
        record = await get_runner().start(
            bot_id=bot_id, params=req.params,
            base_url=str(request.base_url), api_key=api_key,
            timeout_s=req.timeout_s, confirm=req.confirm)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ConfirmationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return record


def json_dumps(value: Any) -> str:
    import json as _json
    return _json.dumps(value, default=str)


@router.get("/runs")
async def list_runs(bot_id: Optional[str] = None, limit: int = 50):
    return {"runs": get_runner().store.list(bot_id=bot_id, limit=min(limit, 200))}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    record = get_runner().store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return record


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    try:
        return get_runner().cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── discovery / approval workflow ────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    action: str = Field(pattern="^(approve|revoke|disable|enable)$")


@router.post("/bots/{bot_id}/approval")
async def bot_approval(bot_id: str, req: ApprovalRequest, request: Request):
    reg = get_registry()
    reg.ensure_scanned()
    spec = reg.bot(bot_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"bot '{bot_id}' not found")
    if spec.kind != "local" and req.action in ("approve", "revoke"):
        raise HTTPException(
            status_code=422,
            detail="approval gating applies to discovered local bots; hub bots "
                   "are enabled/disabled only")
    store = reg.store
    if req.action == "approve":
        store.approve(bot_id)
    elif req.action == "revoke":
        store.revoke(bot_id)
    elif req.action == "disable":
        store.disable(bot_id)
    elif req.action == "enable":
        store.enable(bot_id)
    return {"bot_id": bot_id, "action": req.action, "ok": True}


@router.get("/root")
async def root_info():
    reg = get_registry()
    reg.ensure_scanned()
    return reg.scan_info()


class RootRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)


@router.post("/root")
async def set_root(req: RootRequest):
    reg = get_registry()
    try:
        resolved = reg.store.set_root(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result = reg.rescan(resolved)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=str(result.get("error")))
    return {"ok": True, "root": str(resolved), "scan": reg.scan_info()}


@router.post("/rescan")
async def rescan():
    reg = get_registry()
    result = reg.rescan()
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=str(result.get("error")))
    return result
