"""Sara Bots "custom worker" API — the My Bots layer (wizard, tasks, memory,
schedules, approvals, factory).

Auth + tenancy: every route resolves the caller through ``current_tenant``
(the same resolver as the rest of the platform) and only sees records whose
``owner`` matches that tenant — a signed-in user's bots, tasks, approvals,
schedules and memory are theirs alone; the machine operator (SARA_API_KEY)
keeps the ``local`` tenant. Same ``/api/sara-bots`` prefix. Every route is a
thin validated wrapper over the real stores/engine — no route fabricates
state:

  GET    /tools                       live tool registry (real states)
  GET    /custom                      list custom bots (+live tool states)
  POST   /custom                      create (validated against live tools)
  GET    /custom/{id}                 detail (tools, tasks, schedules, memory)
  PATCH  /custom/{id}                 update (partial, re-validated)
  DELETE /custom/{id}                 delete (cascades schedules; cancels tasks)
  POST   /custom/{id}/pause|resume    bot status
  POST   /custom/{id}/duplicate       copy without memory
  GET    /custom/{id}/readiness       exact blockers to become runnable
  POST   /custom/{id}/tasks           submit a goal (durable task)
  GET    /tasks                       task list (filter by bot/status)
  GET    /tasks/{id}                  one task (plan, steps, events, outputs)
  POST   /tasks/{id}/cancel|resume    owner controls
  POST   /tasks/{id}/reply            answer a task waiting for the owner
  GET    /custom/{id}/browser         live state of the bot's own browser
  POST   /custom/{id}/browser/close   close the session (profile kept)
  GET    /approvals                   approval inbox (enriched)
  POST   /approvals/{id}/decide       approve | deny
  GET    /custom/{id}/memory          this bot's memory ONLY
  POST   /custom/{id}/memory          owner note
  DELETE /custom/{id}/memory/{mem}    remove one entry
  POST   /custom/{id}/memory/clear    wipe this bot's memory
  GET    /schedules                   list (filter by bot)
  POST   /schedules                   create
  PATCH  /schedules/{id}              update interval/goal
  POST   /schedules/{id}/toggle       enable/disable
  DELETE /schedules/{id}              delete
  POST   /factory/propose             description -> validated proposal
  GET    /engine                      worker status (real facts)
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, HTTPException,
                     UploadFile)
from pydantic import BaseModel, Field

from app.agents.sara_ultron_agent.core.tenancy import scope_key
from app.api.deps import current_tenant
from app.services.sara_bots.custom import (BOT_CATEGORIES, MEMORY_KINDS,
                                           get_custom_store, validate_config)
from app.services.sara_bots.engine import get_engine
from app.services.sara_bots.scheduler import get_schedule_store
from app.services.sara_bots.tasks import get_task_store
from app.services.sara_bots.tools import live_tools, tool_ids
from app.services.sara_bots.skills import skill_ids_for_owner

log = logging.getLogger("sara.bots.custom.api")

router = APIRouter(prefix="/api/sara-bots", tags=["Sara Bots"])

#: ids are generated as <prefix>_<hex>; allow lowercase alnum in the body so a
#: well-formed-but-unknown id answers 404 while paths/../ answer 422.
_ID_RE = re.compile(r"^[a-z]{2,5}_[a-z0-9]{6,40}$")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _checked(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value):
        raise HTTPException(status_code=422, detail=f"invalid {what} id")
    return value


def _scope(tenant) -> str:
    """Storage owner key for the caller (Clerk user id, or ``local``)."""
    return scope_key(tenant)


def _bot_or_404(bot_id: str, tenant) -> Dict[str, Any]:
    bot = get_custom_store().get(_checked(bot_id, "bot"))
    if bot is None or bot.get("owner") != _scope(tenant):
        # A bot that exists but belongs to someone else answers exactly like a
        # missing one — cross-tenant existence is never leaked.
        raise HTTPException(status_code=404, detail=f"bot '{bot_id}' not found")
    return bot


def _task_or_404(task_id: str, tenant) -> Dict[str, Any]:
    task = get_task_store().get(_checked(task_id, "task"))
    if task is None or task.get("owner") != _scope(tenant):
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    return task


def _approval_or_404(approval_id: str, tenant) -> Dict[str, Any]:
    approval = get_task_store().get_approval(_checked(approval_id, "approval"))
    if approval is None or approval.get("owner") != _scope(tenant):
        raise HTTPException(status_code=404,
                            detail=f"approval '{approval_id}' not found")
    return approval


def _schedule_or_404(schedule_id: str, tenant) -> Dict[str, Any]:
    schedule = get_schedule_store().get(_checked(schedule_id, "schedule"))
    if schedule is None or schedule.get("owner") != _scope(tenant):
        raise HTTPException(status_code=404,
                            detail=f"schedule '{schedule_id}' not found")
    return schedule


def _resolve_tools(bot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The bot's assigned tools, each with its LIVE state + the live params."""
    by_id = {t["id"]: t for t in live_tools()}
    out: List[Dict[str, Any]] = []
    for tool_id in bot.get("tools", []):
        tool = by_id.get(tool_id)
        if tool is None:
            out.append({"id": tool_id, "name": tool_id, "state": "unavailable",
                        "detail": "no longer present in the live registry",
                        "risk": "write", "params": {}})
        else:
            out.append(tool)
    return out


def _bot_view(bot: Dict[str, Any]) -> Dict[str, Any]:
    view = dict(bot)
    view["resolved_tools"] = _resolve_tools(bot)
    ready = sum(1 for t in view["resolved_tools"] if t["state"] == "ready")
    view["ready_tools"] = ready
    view["tool_count"] = len(view["resolved_tools"])
    return view


# ── tools ────────────────────────────────────────────────────────────────────
def _bots_attachment_dir() -> Path:
    d = Path(os.getenv("SARA_BOT_ATTACHMENTS_DIR",
                        str(Path(__file__).resolve().parents[2] / "sara_bot_attachments")))
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/custom/attachments", status_code=201)
async def upload_task_attachment(file: UploadFile = File(...),
                                tenant=Depends(current_tenant)) -> Dict[str, Any]:
    """Store a file the owner wants to hand to a bot with a task.

    Returns the on-disk path so it can be referenced in the task goal (e.g.
    for action.file_processor) and surfaced in the task record.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file exceeds 15 MB")
    original = Path(file.filename or "attachment").name
    suffix = Path(original).suffix.lower()
    stored = f"{uuid.uuid4().hex}{suffix}"
    dest = _bots_attachment_dir() / stored
    dest.write_bytes(data)
    return {"path": str(dest), "filename": original, "size": len(data)}


@router.get("/tools")
def list_tools(tenant=Depends(current_tenant)) -> Dict[str, Any]:
    tools = live_tools()
    counts: Dict[str, int] = {}
    for tool in tools:
        counts[tool["state"]] = counts.get(tool["state"], 0) + 1
    return {"tools": tools, "summary": {"total": len(tools), "by_state": counts}}


# ── custom bots ──────────────────────────────────────────────────────────────
class CustomBotBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    goal: Optional[str] = None
    instructions: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None
    model: Optional[str] = None
    output_format: Optional[str] = None
    tools: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    autonomy: Optional[str] = None
    plan_mode: Optional[str] = None
    direct_tool: Optional[str] = None
    direct_params: Optional[Dict[str, Any]] = None
    memory_enabled: Optional[bool] = None
    hue: Optional[int] = None
    status: Optional[str] = None


@router.get("/custom")
def list_custom(tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_custom_store()
    tasks = get_task_store()
    bots = [_bot_view(b) for b in store.list(owner=_scope(tenant))]
    for bot in bots:
        active = tasks.list(dot_id=bot["id"], active_only=True, limit=50)
        bot["active_tasks"] = len(active)
        recent = tasks.list(dot_id=bot["id"], limit=1)
        bot["last_task"] = recent[0] if recent else None
    return {"bots": bots, "engine": get_engine().status()}


@router.post("/custom", status_code=201)
def create_custom(body: CustomBotBody,
                  tenant=Depends(current_tenant)) -> Dict[str, Any]:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        config = validate_config(payload, tool_ids(),
                                 skill_ids=skill_ids_for_owner(_scope(tenant)))
        bot = get_custom_store().create(config, owner=_scope(tenant))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _bot_view(bot)


@router.get("/custom/{bot_id}")
def get_custom(bot_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    view = _bot_view(bot)
    tasks = get_task_store()
    view["tasks"] = tasks.list(dot_id=bot["id"], limit=20)
    view["approvals_pending"] = [
        a for a in tasks.list_approvals(status="pending", dot_id=bot["id"])]
    view["schedules"] = get_schedule_store().list(dot_id=bot["id"])
    view["memory"] = get_custom_store().memory(bot["id"])[-50:]
    return view


@router.patch("/custom/{bot_id}")
def update_custom(bot_id: str, body: CustomBotBody,
                  tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_custom_store()
    bot = _bot_or_404(bot_id, tenant)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        config = validate_config(payload, tool_ids(),
                                 skill_ids=skill_ids_for_owner(_scope(tenant)),
                                 existing=bot)
        updated = store.update(bot["id"], config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _bot_view(updated)


@router.delete("/custom/{bot_id}")
async def delete_custom(bot_id: str,
                        tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_custom_store()
    bot = _bot_or_404(bot_id, tenant)
    tasks = get_task_store()
    cancelled = 0
    for task in tasks.list(dot_id=bot["id"], active_only=True, limit=100):
        try:
            get_engine().cancel(task["id"])
            cancelled += 1
        except Exception:  # noqa: BLE001 — deletion must not fail on this
            log.exception("sara-bots: could not cancel task %s on delete",
                          task["id"])
    schedules = get_schedule_store().delete_for_bot(bot["id"])
    # A deleted bot must not leave its Chromium window behind.
    closed_browser = False
    try:
        from app.services.sara_bots.browser import close_session
        closed_browser = bool((await close_session(bot["id"])).get("closed"))
    except Exception:  # noqa: BLE001 — deletion must not fail on this
        log.exception("sara-bots: could not close browser of %s on delete",
                      bot["id"])
    try:
        store.delete(bot["id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": bot["id"], "cancelled_tasks": cancelled,
            "deleted_schedules": schedules,
            "closed_browser": closed_browser}


@router.post("/custom/{bot_id}/pause")
def pause_custom(bot_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    return _bot_view(get_custom_store().set_status(bot["id"], "paused"))


@router.post("/custom/{bot_id}/resume")
def resume_custom(bot_id: str,
                  tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    return _bot_view(get_custom_store().set_status(bot["id"], "active"))


class DuplicateBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)


@router.post("/custom/{bot_id}/duplicate", status_code=201)
def duplicate_custom(bot_id: str, body: DuplicateBody,
                     tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    try:
        clone = get_custom_store().duplicate(bot["id"], body.name or "")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _bot_view(clone)


@router.get("/custom/{bot_id}/readiness")
def readiness(bot_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    """Exactly what blocks this bot from running right now — derived from the
    live registry, never assumed."""
    bot = _bot_or_404(bot_id, tenant)
    tools = _resolve_tools(bot)
    ready = [t for t in tools if t["state"] == "ready"]
    blockers: List[Dict[str, str]] = []
    hints: List[str] = []
    if bot["status"] != "active":
        blockers.append({"kind": "bot_status",
                         "detail": f"bot is {bot['status']} — resume it"})
    if not ready:
        blockers.append({"kind": "no_ready_tools",
                         "detail": "every assigned tool is currently blocked"})
    for tool in tools:
        if tool["state"] == "needs_approval":
            hints.append(f"Approve '{tool['id']}' once in the Fleet view "
                         "(it stays approved for future tasks).")
        elif tool["state"] == "needs_configuration":
            hints.append(f"Configure '{tool['id']}': {tool['detail']}")
        elif tool["state"] == "unavailable":
            hints.append(f"'{tool['id']}' is unavailable: {tool['detail']}")
    if bot.get("plan_mode") == "direct":
        direct = next((t for t in tools if t["id"] == bot.get("direct_tool")),
                      None)
        if direct is None or direct["state"] != "ready":
            blockers.append({"kind": "direct_tool_not_ready",
                             "detail": f"direct tool "
                                       f"'{bot.get('direct_tool')}' is not "
                                       "ready"})
    return {"bot_id": bot["id"], "runnable": not blockers,
            "ready_tools": [t["id"] for t in ready],
            "blockers": blockers, "hints": hints,
            "note": ("tasks still pass the autonomy/approval gates at "
                     "execution time — readiness only means the tools exist "
                     "and are usable")}


# ── tasks ────────────────────────────────────────────────────────────────────
class TaskBody(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    idempotency_key: Optional[str] = Field(default=None, max_length=120)


@router.post("/custom/{bot_id}/tasks", status_code=202)
def submit_task(bot_id: str, body: TaskBody,
                tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    try:
        task = get_engine().submit(bot, body.goal, kind="user",
                                   idempotency_key=body.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return task


@router.get("/tasks")
def list_tasks(dot_id: str = "", status: str = "", limit: int = 50,
               tenant=Depends(current_tenant)) -> Dict[str, Any]:
    scope = _scope(tenant)
    tasks = get_task_store()
    items = tasks.list(dot_id=dot_id.strip() or None,
                       status=status.strip() or None,
                       limit=max(1, min(limit, 200)),
                       owner=scope)
    bots = {b["id"]: b["name"]
            for b in get_custom_store().list(owner=scope)}
    for task in items:
        task["bot_name"] = bots.get(task.get("dot_id"), "(deleted bot)")
    return {"tasks": items,
            "pending_approvals": len(tasks.list_approvals(status="pending",
                                                          owner=scope))}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    task = _task_or_404(task_id, tenant)
    bot = get_custom_store().get(task.get("dot_id") or "")
    task["bot_name"] = bot["name"] if bot else "(deleted bot)"
    return task


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str,
                tenant=Depends(current_tenant)) -> Dict[str, Any]:
    task = _task_or_404(task_id, tenant)
    try:
        return get_engine().cancel(task["id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/tasks/{task_id}/resume")
def resume_task(task_id: str,
                tenant=Depends(current_tenant)) -> Dict[str, Any]:
    task = _task_or_404(task_id, tenant)
    try:
        return get_engine().resume(task["id"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/tasks/{task_id}/reply", status_code=202)
def reply_task(task_id: str, body: ReplyBody,
               tenant=Depends(current_tenant)) -> Dict[str, Any]:
    """The owner's chat reply to a task parked in ``waiting_input``.

    The answer becomes the parked step's output and the task is re-queued —
    the worker continues from exactly where it paused."""
    _task_or_404(task_id, tenant)
    try:
        task = get_task_store().reply(task_id, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if task is None:
        raise HTTPException(status_code=404,
                            detail=f"task '{task_id}' not found")
    return task


# ── the bot's own browser ────────────────────────────────────────────────────
@router.get("/custom/{bot_id}/browser")
async def browser_status(bot_id: str, shot: bool = False,
                         tenant=Depends(current_tenant)) -> Dict[str, Any]:
    """Live state of THIS bot's own browser session.

    The session is owned by the backend with a per-bot saved profile, so
    closing the web UI (or the browser window) never loses it — see
    services/sara_bots/browser.py. ``shot=true`` embeds a fresh screenshot."""
    bot = _bot_or_404(bot_id, tenant)
    from app.services.sara_bots.browser import session_info
    return await session_info(bot["id"], with_shot=shot)


@router.post("/custom/{bot_id}/browser/close")
async def browser_close(bot_id: str,
                        tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    from app.services.sara_bots.browser import close_session
    return await close_session(bot["id"])


# ── approvals ────────────────────────────────────────────────────────────────
@router.get("/approvals")
def list_approvals(status: str = "pending", dot_id: str = "",
                   tenant=Depends(current_tenant)) -> Dict[str, Any]:
    scope = _scope(tenant)
    tasks = get_task_store()
    approvals = tasks.list_approvals(status=status.strip() or None,
                                     dot_id=dot_id.strip() or None,
                                     owner=scope)
    bots = {b["id"]: b["name"]
            for b in get_custom_store().list(owner=scope)}
    for approval in approvals:
        task = tasks.get(approval.get("task_id") or "")
        approval["bot_name"] = bots.get(approval.get("dot_id"),
                                        "(deleted bot)")
        approval["goal"] = (task or {}).get("goal", "")
        approval["task_status"] = (task or {}).get("status", "missing")
    return {"approvals": approvals,
            "pending": len(tasks.list_approvals(status="pending",
                                                owner=scope))}


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|deny)$")


@router.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: DecisionBody,
                    tenant=Depends(current_tenant)) -> Dict[str, Any]:
    tasks = get_task_store()
    approval = _approval_or_404(approval_id, tenant)
    try:
        decided = tasks.decide_approval(approval["id"], body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"approval": decided,
            "task": tasks.get(approval["task_id"])}


# ── memory ───────────────────────────────────────────────────────────────────
class MemoryBody(BaseModel):
    kind: str = Field(default="fact", max_length=20)
    text: str = Field(min_length=1, max_length=500)


@router.get("/custom/{bot_id}/memory")
def get_memory(bot_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    return {"bot_id": bot["id"], "memory": get_custom_store().memory(bot["id"])}


@router.post("/custom/{bot_id}/memory", status_code=201)
def add_memory(bot_id: str, body: MemoryBody,
               tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    if body.kind not in MEMORY_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"kind must be one of "
                                   f"{', '.join(MEMORY_KINDS)}")
    try:
        entry = get_custom_store().memory_add(bot["id"], body.kind, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"entry": entry}


@router.delete("/custom/{bot_id}/memory/{memory_id}")
def delete_memory(bot_id: str, memory_id: str,
                  tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    store = get_custom_store()
    mem_id = _checked(memory_id, "memory")
    entries = store.memory(bot["id"])
    kept = [e for e in entries if e.get("id") != mem_id]
    if len(kept) == len(entries):
        raise HTTPException(status_code=404, detail="memory entry not found")
    store.memory_replace(bot["id"], kept)
    return {"deleted": mem_id, "remaining": len(kept)}


@router.post("/custom/{bot_id}/memory/clear")
def clear_memory(bot_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(bot_id, tenant)
    get_custom_store().memory_clear(bot["id"])
    return {"cleared": bot["id"]}


# ── schedules ────────────────────────────────────────────────────────────────
class ScheduleBody(BaseModel):
    dot_id: str
    goal: str = Field(min_length=1, max_length=2000)
    interval_minutes: int
    enabled: bool = True


@router.get("/schedules")
def list_schedules(dot_id: str = "",
                   tenant=Depends(current_tenant)) -> Dict[str, Any]:
    scope = _scope(tenant)
    schedules = get_schedule_store().list(dot_id=dot_id.strip() or None,
                                          owner=scope)
    bots = {b["id"]: b["name"]
            for b in get_custom_store().list(owner=scope)}
    for schedule in schedules:
        schedule["bot_name"] = bots.get(schedule.get("dot_id"),
                                        "(deleted bot)")
    return {"schedules": schedules}


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleBody,
                    tenant=Depends(current_tenant)) -> Dict[str, Any]:
    bot = _bot_or_404(body.dot_id, tenant)
    try:
        schedule = get_schedule_store().create(bot["id"], body.goal,
                                               body.interval_minutes,
                                               enabled=body.enabled,
                                               owner=bot["owner"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return schedule


class SchedulePatch(BaseModel):
    goal: Optional[str] = Field(default=None, max_length=2000)
    interval_minutes: Optional[int] = None


@router.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, body: SchedulePatch,
                    tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_schedule_store()
    sid = _checked(schedule_id, "schedule")
    _schedule_or_404(sid, tenant)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        return store.update(sid, **fields)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: str,
                    tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_schedule_store()
    sid = _checked(schedule_id, "schedule")
    _schedule_or_404(sid, tenant)
    try:
        return store.toggle(sid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: str,
                    tenant=Depends(current_tenant)) -> Dict[str, Any]:
    store = get_schedule_store()
    sid = _checked(schedule_id, "schedule")
    _schedule_or_404(sid, tenant)
    try:
        return {"deleted": store.delete(sid)["id"]}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── factory ──────────────────────────────────────────────────────────────────
class FactoryBody(BaseModel):
    description: str = Field(min_length=3, max_length=2000)
    model: Optional[str] = Field(default=None, max_length=100)


@router.post("/factory/propose")
async def factory_propose(body: FactoryBody,
                          tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.factory import propose

    result = await propose(body.description, model=body.model)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=str(result.get("error")))
    return result


# ── engine status ────────────────────────────────────────────────────────────
@router.get("/engine")
def engine_status(tenant=Depends(current_tenant)) -> Dict[str, Any]:
    return get_engine().status()


# -- skills (reusable SKILL.md-style instruction packages) --
@router.get("/skills")
def list_skills(tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.skills import list_skills as _list
    return {"skills": _list(_scope(tenant))}


class SkillBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    required_tools: Optional[List[str]] = None
    version: Optional[str] = None
    enabled: Optional[bool] = None


@router.post("/skills", status_code=201)
def create_skill(body: SkillBody,
                 tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.skills import create_skill as _create
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return _create(_scope(tenant), payload, tool_ids())
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.skills import get_skill as _get
    sk = _get(skill_id, _scope(tenant))
    if sk is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return sk


@router.patch("/skills/{skill_id}")
def update_skill(skill_id: str, body: SkillBody,
                 tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.skills import update_skill as _update
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        return _update(skill_id, _scope(tenant), payload, tool_ids())
    except KeyError:
        raise HTTPException(status_code=404, detail="skill not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, tenant=Depends(current_tenant)) -> Dict[str, Any]:
    from app.services.sara_bots.skills import delete_skill as _delete
    try:
        ok = _delete(skill_id, _scope(tenant))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="skill not found")
    return {"deleted": skill_id}
