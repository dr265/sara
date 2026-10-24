"""SARA Sales Agent API — the Sales Command Center backend.

Every route is a thin validated wrapper over ``app.services.sales_agent``:
messages are parsed into intents, intents create REAL durable tasks, tasks
execute real discovery/sends, and every response is read back from persisted
records. The same shared API key protects these routes as the rest of the
platform.

Routes (prefix /api/sales):
  GET    /dashboard                 snapshot for the command center
  GET    /conversations             list (id, title, updated)
  POST   /conversations             new conversation
  GET    /conversations/{id}        full transcript + context
  DELETE /conversations/{id}        remove
  POST   /conversations/{id}/messages   send a user message -> intent -> task
  GET    /tasks                     task list (plan steps, status, result)
  GET    /tasks/{id}                one task
  POST   /tasks/{id}/cancel         cancel a queued task
  GET    /leads                     lead pipeline
  POST   /leads/{id}/stage          move a lead through the pipeline
  GET    /products                  verified product registry
  GET    /listings                  saved listing drafts
  GET    /campaigns                 campaign meta + approval state
  POST   /campaigns/{id}/approve    approve a previewed campaign (enables send)
  POST   /campaigns/{id}/send       execute an approved campaign (real sends)
  POST   /automation                {action: start|pause|resume|stop|emergency}
  POST   /attachments               upload a chat attachment (stored on disk)
  POST   /transcribe                voice note -> real Sarvam STT (or honest 422)
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from app.api.deps import require_api_key
from app.services import sales_agent as sales

log = logging.getLogger("sara.sales.api")

router = APIRouter(prefix="/api/sales", tags=["SARA Sales Agent"],
                   dependencies=[Depends(require_api_key)])

ATTACH_DIR = sales.ATTACH_DIR
_MAX_ATTACH_BYTES = 15 * 1024 * 1024
_MAX_AUDIO_BYTES = 15 * 1024 * 1024


# ── schemas ──────────────────────────────────────────────────────────────────

class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    attachment_ids: List[str] = Field(default_factory=list)


class StageIn(BaseModel):
    stage: str = Field(pattern="|".join(sales.LEAD_STAGES))


class AutomationIn(BaseModel):
    action: str = Field(pattern="^(start|pause|resume|stop|emergency_on|"
                                "emergency_off)$")
    interval_minutes: int = Field(default=60, ge=5, le=10080)


# ── dashboard / registry ─────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard():
    store = sales.get_sales_store()
    data = store.read()
    leads = list(data["leads"].values())
    by_stage: Dict[str, int] = {}
    for lead in leads:
        by_stage[lead.get("stage", "new")] = \
            by_stage.get(lead.get("stage", "new"), 0) + 1
    tasks = list(data["tasks"].values())
    campaigns = list(data["campaigns"].values())
    return {
        "worker": sales.get_sales_worker().status(),
        "integrations": sales.integration_health(),
        "leads": {"total": len(leads), "by_stage": by_stage},
        "campaigns": {
            "total": len(campaigns),
            "running": sum(1 for c in campaigns if c["status"] == "running"),
            "sent": sum(int(c.get("sent") or 0) for c in campaigns),
        },
        "tasks": {
            "queued": sum(1 for t in tasks if t["status"] == "queued"),
            "running": sum(1 for t in tasks if t["status"] == "running"),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
            "completed": sum(1 for t in tasks if t["status"] == "completed"),
        },
        "approvals_pending": sum(1 for c in campaigns if c["status"] == "preview"),
        "audit": data["audit"][-25:],
        "report": sales.status_report(),
    }


@router.get("/products")
async def products():
    return {"products": list(sales.get_sales_store().read()["products"].values())}


@router.get("/listings")
async def listings():
    return {"listings": sorted(
        sales.get_sales_store().read()["listings"].values(),
        key=lambda x: x.get("created_at", 0), reverse=True)}


# ── conversations ────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations():
    data = sales.get_sales_store().read()
    convs = sorted(data["conversations"].values(),
                   key=lambda c: c.get("updated_at", 0), reverse=True)
    return {"conversations": [
        {"id": c["id"], "title": c["title"], "updated_at": c["updated_at"],
         "messages": len(c["messages"])} for c in convs]}


@router.post("/conversations", status_code=201)
async def new_conversation():
    conv = sales.get_sales_store().mutate(
        lambda d: sales.SalesStore.new_conversation(
            sales.get_sales_store(), d, "New conversation"))
    return conv


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = sales.get_sales_store().read()["conversations"].get(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    def _drop(d: Dict[str, Any]) -> bool:
        return d["conversations"].pop(conv_id, None) is not None
    if not sales.get_sales_store().mutate(_drop):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"ok": True}


# ── the command path: message -> intent -> real task -> factual reply ────────

@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, req: MessageIn):
    store = sales.get_sales_store()
    data = store.read()
    conv = data["conversations"].get(conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    attach_names = []
    for aid in req.attachment_ids:
        meta = data.get("attachments", {}).get(aid)
        if meta:
            attach_names.append(meta["filename"])
    user_text = req.text
    if attach_names:
        user_text += f"\n[attachments: {', '.join(attach_names)}]"

    def _add_user(d: Dict[str, Any]) -> Dict[str, Any]:
        c = d["conversations"][conv_id]
        c.setdefault("context", {})
        return store.add_message(d, conv_id, "user", user_text)
    store.mutate(_add_user)

    parsed = sales.parse_intent(req.text, data["products"])
    intent, params = parsed["intent"], parsed["params"]
    product = sales.resolve_product(params, data["products"],
                                    conv.get("context", {}))
    if product:
        params["product"] = product

    reply_text, task_id, status = await _execute_intent(
        store, conv_id, intent, params, conv.get("context", {}))

    def _add_reply(d: Dict[str, Any]) -> None:
        c = d["conversations"][conv_id]
        if product:
            c["context"]["product"] = product
        if task_id:
            c["context"]["last_task_id"] = task_id
        store.add_message(d, conv_id, "assistant", reply_text,
                          intent=intent, task_id=task_id or "",
                          status=status, parser=parsed["parser"])
    store.mutate(_add_reply)
    return {"intent": intent, "parser": parsed["parser"],
            "task_id": task_id, "status": status, "reply": reply_text}


async def _execute_intent(store: sales.SalesStore, conv_id: str, intent: str,
                          params: Dict[str, Any],
                          context: Dict[str, Any]):
    """Map an intent to real work. Returns (reply, task_id, status)."""
    data = store.read()

    if intent == "product.list":
        names = "\n".join(
            f"• {p['name']} — {p['operational_status']}: {p['description']}"
            for p in data["products"].values())
        return (f"Verified product registry:\n{names}", "", "completed")

    if intent == "product.create_listing":
        if not params.get("product"):
            return ("Which product should I draft the listing for? "
                    "(e.g. AI Voice Calling Agent)", "", "needs_input")
        task = store.mutate(lambda d: sales.create_task(
            d, "listing", f"Listing draft: {params['product']}", params, conv_id))
        task = await sales.run_task(task["id"])
        return (_task_reply(task), task["id"], task["status"])

    if intent == "lead.discover":
        if not params.get("region"):
            return ("Which region should I search for leads? "
                    "Give me a city or district (e.g. Jaipur).",
                    "", "needs_input")
        task = store.mutate(lambda d: sales.create_task(
            d, "discover",
            f"Lead discovery: {params.get('industry') or 'businesses'} "
            f"around {params['region']}", params, conv_id))
        return (f"Lead discovery started for {params['region']} "
                f"(task {task['id']}). I'll report real OpenStreetMap results "
                "and website profiling as they land — watch the task panel.",
                task["id"], "running")

    if intent == "lead.qualify":
        task = store.mutate(lambda d: sales.create_task(
            d, "qualify", "Re-qualify stored leads", params, conv_id))
        task = await sales.run_task(task["id"])
        return (_task_reply(task), task["id"], task["status"])

    if intent in ("campaign.create", "campaign.preview"):
        if not params.get("product"):
            return ("Which product is this campaign for?", "", "needs_input")
        task = store.mutate(lambda d: sales.create_task(
            d, "campaign", f"Campaign staging: {params['product']}",
            params, conv_id))
        task = await sales.run_task(task["id"])
        if task["status"] == "completed":
            meta_id = task["refs"].get("campaign_id", "")
            return (task["result"] +
                    f"\n\nApprove it when ready (campaign {meta_id}) and I will "
                    "send through the connected mailbox — never before.",
                    task["id"], "awaiting_approval")
        return (_task_reply(task), task["id"], task["status"])

    if intent == "campaign.approve":
        meta_id = params.get("text", "").strip() or context.get("campaign_id", "")
        camps = store.read()["campaigns"]
        meta = camps.get(meta_id) or next(
            (c for c in sorted(camps.values(), key=lambda c: c["created_at"],
                               reverse=True) if c["status"] == "preview"), None)
        if meta is None:
            return ("No campaign is waiting for approval.", "", "failed")
        store.mutate(lambda d: (d["campaigns"][meta["id"]].update(
            {"status": "approved"}),
            store.audit(d, "campaign.approve", meta["id"])))
        return (f"Campaign {meta['id']} approved for sending. Say "
                "'start the approved campaign' (or call /campaigns/{id}/send) "
                "when you want the real sends to go out.",
                "", "approved")

    if intent == "campaign.start":
        camps = store.read()["campaigns"]
        meta = next((c for c in sorted(camps.values(),
                     key=lambda c: c["created_at"], reverse=True)
                     if c["status"] == "approved"), None)
        if meta is None:
            return ("No approved campaign is waiting to send. Approve one "
                    "first.", "", "failed")
        try:
            result = await sales.send_approved_campaign(meta["id"])
        except ValueError as exc:
            return (str(exc), "", "failed")
        return (f"Sending finished: {result['sent']} sent, "
                f"{result['failed']} failed. Provider responses are recorded "
                "per recipient in the Email Automation workspace.",
                "", "completed")

    if intent in ("automation.start", "automation.resume"):
        store.mutate(lambda d: (d["settings"]["automation"].update(
            {"mode": "continuous",
             "interval_minutes": int(params.get("limit") or 60),
             "next_tick_at": 0.0}),
            store.audit(d, "automation.start", "continuous mode on")))
        return ("Continuous mode ON: queued tasks run immediately and stored "
                "leads re-qualify on schedule. Outreach still requires your "
                "explicit campaign approval — automation never sends by "
                "itself.", "", "completed")

    if intent == "automation.pause":
        store.mutate(lambda d: (d["settings"]["automation"].update(
            {"mode": "paused"}), store.audit(d, "automation.pause", "")))
        return ("Automation paused. Queued tasks stay queued; nothing new "
                "starts until you resume.", "", "completed")

    if intent == "automation.stop":
        store.mutate(lambda d: (d["settings"].update({"emergency_stop": True}),
                                d["settings"]["automation"].update({"mode": "off"}),
                                store.audit(d, "automation.emergency_stop", "")))
        return ("EMERGENCY STOP engaged: no new outbound messages and no new "
                "campaign sends until you turn it off. Running tasks finish "
                "their current step and stop.", "", "completed")

    if intent == "sales.status" or intent == "sales.report":
        task = store.mutate(lambda d: sales.create_task(
            d, "report", "Sales status report", params, conv_id))
        task = await sales.run_task(task["id"])
        return (_task_reply(task), task["id"], task["status"])

    if intent == "integration.health":
        health = sales.integration_health()
        lines = "\n".join(f"• {k}: {'connected' if v['connected'] else 'NOT connected'} — {v['detail']}"
                          for k, v in health.items())
        return (f"Integration status (checked live):\n{lines}", "", "completed")

    # chat — grounded small talk / explanation, clearly labeled as non-executing
    return (f"I understood this as conversation, not an action (parser: "
            f"intent=chat). I can discover leads, draft listings, stage and "
            f"send approved campaigns, run reports and control automation — "
            f"tell me which one you want, in English, Hindi or Hinglish.",
            "", "completed")


def _task_reply(task: Dict[str, Any]) -> str:
    if task["status"] == "completed":
        return task["result"] or "Done."
    if task["status"] == "failed":
        return (f"Task failed: {task['error']}\nPlan state: " +
                "; ".join(f"{s['title']}={s['status']}" for s in task["plan"]))
    return f"Task {task['status']}."


# ── tasks ────────────────────────────────────────────────────────────────────

@router.get("/tasks")
async def list_tasks(limit: int = 50):
    tasks = sorted(sales.get_sales_store().read()["tasks"].values(),
                   key=lambda t: t.get("created_at", 0), reverse=True)
    return {"tasks": tasks[:max(1, min(limit, 200))]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = sales.get_sales_store().read()["tasks"].get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    def _cancel(d: Dict[str, Any]) -> Optional[str]:
        task = d["tasks"].get(task_id)
        if task is None:
            return None
        if task["status"] not in ("queued",):
            raise HTTPException(status_code=409,
                                detail=f"task is {task['status']} — only queued "
                                       "tasks can be cancelled")
        task["status"] = "cancelled"
        task["updated_at"] = sales._now()
        return task["status"]
    if sales.get_sales_store().mutate(_cancel) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True}


# ── leads / campaigns / automation ───────────────────────────────────────────

@router.get("/leads")
async def list_leads(stage: Optional[str] = None):
    leads = list(sales.get_sales_store().read()["leads"].values())
    if stage:
        leads = [l for l in leads if l.get("stage") == stage]
    leads.sort(key=lambda l: l.get("score", 0), reverse=True)
    return {"leads": leads[:300]}


@router.post("/leads/{lead_id}/stage")
async def set_lead_stage(lead_id: str, req: StageIn):
    def _set(d: Dict[str, Any]) -> bool:
        lead = d["leads"].get(lead_id)
        if lead is None:
            return False
        lead["stage"] = req.stage
        lead["updated_at"] = sales._now()
        return True
    if not sales.get_sales_store().mutate(_set):
        raise HTTPException(status_code=404, detail="lead not found")
    return {"ok": True}


@router.get("/campaigns")
async def list_campaigns():
    camps = sorted(sales.get_sales_store().read()["campaigns"].values(),
                   key=lambda c: c.get("created_at", 0), reverse=True)
    return {"campaigns": camps}


@router.post("/campaigns/{meta_id}/approve")
async def approve_campaign(meta_id: str):
    def _approve(d: Dict[str, Any]) -> bool:
        meta = d["campaigns"].get(meta_id)
        if meta is None or meta["status"] != "preview":
            return False
        meta["status"] = "approved"
        sales.get_sales_store().audit(d, "campaign.approve", meta_id)
        return True
    if not sales.get_sales_store().mutate(_approve):
        raise HTTPException(status_code=409,
                            detail="campaign not found or not in preview")
    return {"ok": True, "status": "approved"}


@router.post("/campaigns/{meta_id}/send")
async def send_campaign(meta_id: str):
    try:
        result = await sales.send_approved_campaign(meta_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="campaign not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@router.post("/automation")
async def automation(req: AutomationIn):
    def _apply(d: Dict[str, Any]) -> None:
        settings = d["settings"]
        if req.action == "start":
            settings["automation"].update(
                {"mode": "continuous",
                 "interval_minutes": req.interval_minutes,
                 "next_tick_at": 0.0})
        elif req.action == "pause":
            settings["automation"]["mode"] = "paused"
        elif req.action == "resume":
            settings["automation"]["mode"] = "continuous"
        elif req.action == "stop":
            settings["automation"]["mode"] = "off"
        elif req.action == "emergency_on":
            settings["emergency_stop"] = True
            settings["automation"]["mode"] = "off"
        elif req.action == "emergency_off":
            settings["emergency_stop"] = False
        sales.get_sales_store().audit(d, f"automation.{req.action}", "")
    sales.get_sales_store().mutate(_apply)
    return sales.get_sales_worker().status()


# ── attachments + voice notes ────────────────────────────────────────────────

@router.post("/attachments", status_code=201)
async def upload_attachment(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > _MAX_ATTACH_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 15 MB")
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{Path(file.filename or 'file').suffix[:12]}"
    (ATTACH_DIR / stored).write_bytes(raw)
    meta = {"id": f"att_{uuid.uuid4().hex[:10]}",
            "filename": (file.filename or "attachment")[:300],
            "stored_name": stored, "size": len(raw),
            "mime": file.content_type or "application/octet-stream",
            "created_at": sales._now()}
    sales.get_sales_store().mutate(
        lambda d: d.setdefault("attachments", {}).__setitem__(meta["id"], meta))
    return meta


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(raw) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio exceeds 15 MB")
    try:
        return await sales.transcribe_audio(raw, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
