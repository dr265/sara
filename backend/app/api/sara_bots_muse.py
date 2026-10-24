"""SaraMuse API — the OpenMuse-inspired powers surface (goals, watches,
ideas, finance, notifications, identity).

Everything here is backed by the real muse store and the Sara Bots engine:
accepting an idea submits a REAL engine task to the live SaraMuse bot,
monitors fetch REAL public pages on schedule, and finance analysis is a
deterministic port of OpenMuse's ``analyzeSpending``. No simulated work.

Routes (all under /api/sara-bots/muse, same shared API key as the rest):
  GET  /                         workspace snapshot (goals, monitors, ideas…)
  POST /goals                    create a goal (+ milestones)
  POST /goals/{id}               patch status
  POST /goals/{id}/milestones/{milestone_id}   toggle done
  POST /monitors                 create a page watch (SSRF-guarded)
  POST /monitors/{id}/control    pause | resume | stop | check
  POST /ideas/refresh            derive suggestions from real records
  POST /ideas/{id}               accept | dismiss (accept runs a real task)
  POST /finance/analyze          deterministic CSV spending analysis
  GET  /notifications            list notifications
  POST /notifications/{id}/read  mark one read
  POST /identity                 update agent identity (name/tone/avatar)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.services.sara_bots import muse

log = logging.getLogger("sara.bots.muse.api")

router = APIRouter(prefix="/api/sara-bots/muse", tags=["SaraMuse"],
                   dependencies=[Depends(require_api_key)])


# ── snapshot ─────────────────────────────────────────────────────────────────

@router.get("")
@router.get("/")
async def workspace_snapshot():
    return muse.snapshot()


# ── goals ────────────────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    category: str = Field(default="Personal", max_length=80)
    milestones: List[str] = Field(default_factory=list, max_length=20)


class GoalPatch(BaseModel):
    status: Optional[str] = Field(
        default=None, pattern="^(active|paused|completed)$")


@router.post("/goals", status_code=201)
async def create_goal(req: GoalRequest):
    for m in req.milestones:
        if not m.strip() or len(m) > 200:
            raise HTTPException(status_code=422,
                                detail="each milestone needs 1–200 characters")
    try:
        return muse.get_muse_store().create_goal(
            req.title.strip(), req.description, req.category,
            [m.strip() for m in req.milestones if m.strip()])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/goals/{goal_id}")
async def update_goal(goal_id: str, req: GoalPatch):
    try:
        return muse.get_muse_store().update_goal(
            goal_id, {"status": req.status} if req.status else {})
    except KeyError:
        raise HTTPException(status_code=404, detail=f"goal '{goal_id}' not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/goals/{goal_id}/milestones/{milestone_id}")
async def set_milestone(goal_id: str, milestone_id: str,
                        body: Dict[str, Any]):
    done = body.get("done")
    if not isinstance(done, bool):
        raise HTTPException(status_code=422, detail="'done' must be a boolean")
    try:
        return muse.get_muse_store().set_milestone_done(goal_id, milestone_id, done)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc}")


# ── monitors (page watches) ──────────────────────────────────────────────────

class MonitorRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1, max_length=4096)
    condition: str = Field(default="change",
                           pattern="^(change|contains|price_below)$")
    value: str = Field(default="", max_length=300)
    intervalMinutes: int = Field(default=15, ge=1, le=10080)


@router.post("/monitors", status_code=201)
async def create_monitor(req: MonitorRequest):
    try:
        return muse.get_muse_store().create_monitor(
            req.title.strip(), req.url.strip(), req.condition,
            req.value, req.intervalMinutes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class MonitorControl(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop|check)$")


@router.post("/monitors/{monitor_id}/control")
async def control_monitor(monitor_id: str, req: MonitorControl):
    store = muse.get_muse_store()
    monitor = next((m for m in store.monitors() if m["id"] == monitor_id), None)
    if monitor is None:
        raise HTTPException(status_code=404,
                            detail=f"monitor '{monitor_id}' not found")
    if monitor.get("status") == "stopped" and req.action != "stop":
        raise HTTPException(
            status_code=409,
            detail="Create a new watch to restart this stopped monitor")
    if req.action in ("pause", "stop"):
        return store.update_monitor(monitor_id, {
            "status": "paused" if req.action == "pause" else "stopped",
            "nextCheckAt": __import__("time").time(),
        })
    if req.action == "resume":
        return store.update_monitor(monitor_id, {
            "status": "active", "error": None, "failures": 0,
            "nextCheckAt": __import__("time").time(),
        })
    # check — run one real observation right now
    try:
        return await muse.get_monitor_worker().check(monitor_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="monitor not found")


# ── ideas ────────────────────────────────────────────────────────────────────

@router.post("/ideas/refresh")
async def refresh_ideas():
    return {"ideas": muse.refresh_ideas()}


class IdeaDecision(BaseModel):
    action: str = Field(pattern="^(accept|dismiss)$")
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=12000)


@router.post("/ideas/{idea_id}")
async def decide_idea(idea_id: str, req: IdeaDecision):
    try:
        return muse.decide_idea(idea_id, req.action, req.prompt)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"idea '{idea_id}' not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ── finance ──────────────────────────────────────────────────────────────────

class FinanceRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=500_000)


@router.post("/finance/analyze")
async def analyze_finance(req: FinanceRequest):
    try:
        data = muse.analyze_spending(req.csv)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Persist as an artifact so the tracker survives reloads (real record).
    artifact = muse.get_muse_store().put_artifact(
        f"finance:{__import__('time').time_ns()}", "finance",
        "Spending tracker",
        f"{data['count']} transactions · {data['spending']:.2f} spent",
        data)
    return {"analysis": data, "artifact": artifact}


# ── notifications ────────────────────────────────────────────────────────────

@router.get("/notifications")
async def list_notifications():
    return {"notifications": muse.get_muse_store().notifications()}


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: str):
    try:
        return muse.get_muse_store().mark_read(notif_id)
    except KeyError:
        raise HTTPException(status_code=404,
                            detail=f"notification '{notif_id}' not found")


# ── identity ─────────────────────────────────────────────────────────────────

class IdentityRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    tone: Optional[str] = Field(default=None, pattern="^(warm|concise|thoughtful)$")
    avatar: Optional[str] = Field(default=None, pattern="^(sky|sand|lilac)$")
    showChatUpdates: Optional[bool] = None


@router.post("/identity")
async def set_identity(req: IdentityRequest):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(status_code=422, detail="nothing to update")
    return muse.get_muse_store().set_identity(patch)
