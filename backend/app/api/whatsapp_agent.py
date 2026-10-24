"""WhatsApp hotel agent routes, served by the unified SARA backend.

Mounted by app.main under these paths:
  GET  /whatsapp/health
  POST /whatsapp/webhooks/unipile     Unipile transport
  POST /whatsapp/webhooks/zernio      Zernio transport (WA_MESSENGER=zernio)
  POST /whatsapp/admin/sessions/{chat_id}/resume
  DELETE /whatsapp/admin/sessions/{chat_id}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import BaseModel

from app.core.agent_switch import is_running, set_running
from app.agents.sara_whatsapp import (
    LLM_MODEL,
    UNIPILE_ACCOUNT_ID,
    UNIPILE_ACCESS_TOKEN,
    UNIPILE_DSN,
    WA_MESSENGER,
    WEBHOOK_SECRET,
    ZERNIO_ACCOUNT_ID,
    ZERNIO_API_KEY,
    ZERNIO_WEBHOOK_SECRET,
    WhatsAppHotelAgent,
    parse_unipile_event,
    parse_zernio_event,
    render_reply,
)

log = logging.getLogger("sara.whatsapp.routes")

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp Hotel Agent"])

agent = WhatsAppHotelAgent()

OWNER_CONSOLE_DIRECTIVE = """<owner_console>
You are now talking to the BUSINESS OWNER / operator in the control console -- NOT a guest.
The owner runs this WhatsApp agent for their business. Behave like a sharp operator
assistant: follow the owner's commands, answer questions about the business data, the
guest chats and facts listed in the overview below, and the knowledge base. Never treat
the owner as a customer, never push booking flows on them, and never invent data that is
not present in the context -- if something is missing, say so and tell the owner where to
add it (business data file, persona editor, knowledge upload). Keep replies concise and
skip numbered guest menus.
</owner_console>"""


class PersonaUpdate(BaseModel):
    persona: str


class KnowledgeUpload(BaseModel):
    filename: str
    content_base64: str
    mime: str = ""


class SheetsConnectRequest(BaseModel):
    label: str = "My Google Workspace"


class SheetsSyncRequest(BaseModel):
    target: str = "whatsapp-data"


@router.get("/knowledge/documents")
async def knowledge_documents() -> dict[str, Any]:
    return {"documents": agent.knowledge.list_documents()}


@router.post("/knowledge/upload")
async def knowledge_upload(payload: KnowledgeUpload) -> dict[str, Any]:
    import base64

    try:
        data = base64.b64decode(payload.content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 content")
    try:
        result = agent.knowledge.ingest(payload.filename, data, payload.mime)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", **result}


@router.delete("/knowledge/documents/{doc_id}")
async def knowledge_delete(doc_id: str) -> dict[str, Any]:
    return {"deleted": agent.knowledge.delete_document(doc_id)}


@router.get("/knowledge/search")
async def knowledge_search(q: str = "") -> dict[str, Any]:
    return {"results": agent.knowledge.search(q)}


@router.post("/integrations/sheets/connect")
async def sheets_connect(payload: SheetsConnectRequest) -> dict[str, Any]:
    try:
        return agent.sheets.start_authorization(payload.label)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/integrations/sheets/callback")
async def sheets_callback(code: str = "", state: str = "", error: str = "") -> Any:
    """Public Google OAuth redirect target — renders a close-this-window page."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;text-align:center;padding-top:60px'>"
            f"<h2 style='color:#c0392b'>Authorization failed</h2><p>{error}</p>"
            f"<p><a href='/hub/whatsapp-agent'>Back to the WhatsApp agent</a></p></body></html>"
        )
    import base64
    import json as _json

    try:
        state_data = _json.loads(base64.urlsafe_b64decode(state + "==").decode())
        connection_id = state_data.get("c", "")
    except Exception:
        connection_id = ""
    if not code or not connection_id:
        return HTMLResponse("<html><body style='font-family:sans-serif;text-align:center;padding-top:60px'><h2>Missing code or state</h2></body></html>")
    public = (os.getenv("SARA_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
    redirect_uri = public + "/whatsapp/integrations/sheets/callback"
    try:
        result = agent.sheets.exchange_code(connection_id, code, redirect_uri)
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;text-align:center;padding-top:60px;background:#0b0d10;color:#f3efe6'>"
            f"<div style='display:inline-block;border:1px solid rgba(255,255,255,.15);border-radius:16px;padding:32px 48px'>"
            f"<div style='font-size:40px'>✅</div><h2>Google Sheets connected</h2>"
            f"<p style='color:#9aa1ab'>Account: {result.get('account', 'connected')}</p>"
            f"<p>You can close this window and return to the WhatsApp agent.</p>"
            f"<p><a href='/hub/whatsapp-agent' style='color:#2fbf71'>← Back to the WhatsApp agent</a></p>"
            f"</div></body></html>"
        )
    except RuntimeError as exc:
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;text-align:center;padding-top:60px'>"
            f"<h2 style='color:#c0392b'>Connection failed</h2><p>{exc}</p></body></html>"
        )


@router.get("/integrations/sheets/status")
async def sheets_status() -> dict[str, Any]:
    return agent.sheets.status()


@router.post("/integrations/sheets/sync")
async def sheets_sync(payload: SheetsSyncRequest) -> dict[str, Any]:
    """Push knowledge + guest conversations to Google Sheets."""
    try:
        rows: list[list[Any]] = []
        # knowledge facts → rows
        for f in agent.knowledge.list_documents():
            rows.append(["doc", f["filename"], f.get("size", 0), f.get("created_at", "")])
        # guest conversations → rows
        for chat in agent.memory.all_chats(limit=100):
            try:
                state = json.loads(chat.get("state") or "{}")
            except Exception:
                state = {}
            rows.append(["chat", chat.get("external_chat_id", ""),
                         state.get("guest_name") or "", state.get("room_type") or "",
                         state.get("check_in") or "", state.get("check_out") or "",
                         (chat.get("summary") or "")[:200]])
        if not rows:
            raise HTTPException(status_code=422, detail="nothing to sync yet — add knowledge or chats first")
        headers = ["type", "id/name", "size/guest", "detail", "from", "to", "note"]
        result = agent.sheets.sync([headers] + rows, target=payload.target)
        agent.sheets.record_sync(payload.target, len(rows), True)
        return {"status": "ok", **result}
    except RuntimeError as exc:
        agent.sheets.record_sync(payload.target, 0, False, str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/integrations/sheets/history")
async def sheets_history() -> dict[str, Any]:
    return {"history": agent.sheets.history()}


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """Current agent configuration — persona + business data status."""
    info = agent.persona.info()
    return {
        "persona": info["persona"],
        "persona_source": info["source"],
        "business_context_loaded": agent.business._path.exists(),
        "model": LLM_MODEL,
        "unipile_configured": agent.configured,
        "unipile_dsn": UNIPILE_DSN,
    }


class BusinessContextUpdate(BaseModel):
    business_context: dict[str, Any]


@router.get("/config/business-context")
async def get_business_context() -> dict[str, Any]:
    try:
        data = json.loads(agent.business._path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    return {"business_context": data, "file": str(agent.business._path)}


@router.put("/config/business-context")
async def update_business_context(payload: BusinessContextUpdate) -> dict[str, Any]:
    """Replace services/prices/policies at runtime (no restart; hot-reloaded)."""
    if not isinstance(payload.business_context, dict):
        raise HTTPException(status_code=422, detail="business_context must be an object")
    agent.business._path.write_text(
        json.dumps(payload.business_context, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    agent.business.load()  # force reload
    return {"status": "ok", "business_context_loaded": True}


@router.put("/config/persona")
async def update_persona(payload: PersonaUpdate) -> dict[str, Any]:
    """Change what the agent is for — hotel, salon, general assistant, anything.

    Saved to whatsapp_persona.json and hot-applied to the next reply.
    """
    persona = agent.persona.save(payload.persona)
    return {"status": "ok", "persona": persona, "source": "persona.json"}


@router.delete("/config/persona")
async def reset_persona() -> dict[str, Any]:
    persona = agent.persona.reset()
    return {"status": "ok", "persona": persona, "source": "env (WA_AGENT_PERSONA)"}


@router.get("/memory")
async def memory() -> dict[str, Any]:
    """Persistent guest memory — facts, chats and recent history (all WhatsApp tenants)."""
    return {
        "tenant": agent.memory.tenant_id,
        "facts": agent.memory.facts(limit=100),
        "chats": agent.memory.all_chats(limit=50),
    }


@router.get("/memory/{chat_id}")
async def memory_for_chat(chat_id: str) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "context": agent.memory.memory_context(chat_id),
        "history": agent.memory.history(chat_id, limit=30),
    }


@router.delete("/memory/{chat_id}")
async def forget_chat(chat_id: str) -> dict[str, Any]:
    """Forget a guest: drop their persisted conversation + messages from the DB."""
    agent.sessions.reset(chat_id)
    agent.memory.forget(chat_id)
    return {"chat_id": chat_id, "forgotten": True}


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "configured": agent.configured,
        "business_context_loaded": agent.business._path.exists(),
    }


class DemoMessage(BaseModel):
    text: str


class OwnerMessage(BaseModel):
    text: str


@router.post("/api/owner")
async def owner_chat(payload: OwnerMessage) -> dict[str, Any]:
    """Owner-console chat -- the business owner talks to the agent (not a guest).

    Uses chat_id "owner-console" so owner conversations persist separately from
    guest chats, and injects a live overview of stored guest chats + facts +
    knowledge documents so the owner can ask real questions ("who messaged
    recently?", "what do you know about Maya?", "what is loaded?").
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    chat_id = "owner-console"
    session = agent.sessions.get(chat_id)
    async with session.lock:
        if session.handed_off:
            session.handed_off = False
            agent.memory.set_paused(chat_id, paused=False)

        from app.agents.sara_whatsapp import merge_state, resolve_menu_choice

        user_text = resolve_menu_choice(text, session.last_options)
        agent.memory.add_message(chat_id, "in", user_text)

        # Live overview of everything the agent knows right now.
        chats = agent.memory.all_chats(limit=25)
        facts = agent.memory.facts(limit=30)
        docs = agent.knowledge.list_documents(limit=10)
        overview_lines: list[str] = []
        if chats:
            overview_lines.append("Recent guest chats (external chat id -> latest summary):")
            for c in chats:
                overview_lines.append(
                    f"- {c.get('external_chat_id')} -> {str(c.get('summary') or 'no summary yet')[:120]}"
                    f" (updated {c.get('updated_at')})"
                )
        else:
            overview_lines.append("No guest WhatsApp chats stored yet.")
        if facts:
            overview_lines.append("Known guest facts:")
            for f in facts[:20]:
                overview_lines.append(f"- {f.get('entity')}: {f.get('key')} = {f.get('value')}")
        if docs:
            overview_lines.append("Knowledge base documents:")
            for d in docs:
                overview_lines.append(f"- {d.get('filename')}")
        else:
            overview_lines.append("Knowledge base is empty (upload a price list / FAQ in the Knowledge tab).")

        guest_memory = "[owner console overview]\n" + "\n".join(overview_lines)
        knowledge_text = agent.knowledge.knowledge_context(user_text)
        persona = (agent.persona.load() or "").strip() + "\n\n" + OWNER_CONSOLE_DIRECTIVE.strip()

        resp = await agent.llm.ask(
            agent.business.load(),
            session,
            user_text,
            guest_memory=guest_memory,
            agent_persona=persona,
            knowledge=knowledge_text,
        )

        session.state = merge_state(session.state, resp.state)
        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": resp.model_dump_json()})
        session.history = session.history[-12:]
        session.last_options = resp.ui_options
        session.updated_at = time.time()

        agent.memory.save_state(
            chat_id,
            session.state.model_dump(),
            summary=f"owner: {user_text[:200]}",
        )
        agent.memory.add_message(chat_id, "out", resp.reply, intent=resp.intent)

        return {
            "reply": resp.reply,
            "intent": resp.intent,
            "state": resp.state.model_dump(),
            "ui_options": resp.ui_options,
        }


@router.post("/api/demo")
async def demo_chat(payload: DemoMessage) -> dict[str, Any]:
    """Live test panel — real agent reply without Unipile.

    Runs the exact same session/LLM pipeline as the webhook (deduped by chat id)
    so the workspace page can demo the agent end-to-end before Unipile keys exist.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    chat_id = "web-demo"
    session = agent.sessions.get(chat_id)
    async with session.lock:
        if session.handed_off:
            session.handed_off = False  # demo console is always the operator
            agent.memory.set_paused(chat_id, paused=False)

        from app.agents.sara_whatsapp import merge_state, resolve_menu_choice

        user_text = resolve_menu_choice(text, session.last_options)
        agent.memory.add_message(chat_id, "in", user_text)
        agent.memory.extract_facts(chat_id, user_text)

        guest_memory = agent.memory.memory_context(chat_id)
        knowledge_text = agent.knowledge.knowledge_context(user_text)  # same grounding as the live webhook
        resp = await agent.llm.ask(
            agent.business.load(),
            session,
            user_text,
            guest_memory=guest_memory,
            agent_persona=agent.persona.load(),
            knowledge=knowledge_text,
        )

        session.state = merge_state(session.state, resp.state)
        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": resp.model_dump_json()})
        session.history = session.history[-12:]
        session.last_options = resp.ui_options
        session.updated_at = time.time()  # keep the demo session alive between calls

        agent.memory.save_state(
            chat_id,
            session.state.model_dump(),
            summary=f"{resp.intent}: {user_text[:200]}",
        )
        agent.memory.add_message(chat_id, "out", render_reply(resp), intent=resp.intent)

        return {
            "reply": render_reply(resp),
            "intent": resp.intent,
            "escalate_to_human": resp.escalate_to_human,
            "state": resp.state.model_dump(),
            "ui_options": resp.ui_options,
        }


@router.post("/webhooks/unipile")
async def unipile_webhook(
    request: Request,
    background: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict[str, str]:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event = parse_unipile_event(payload if isinstance(payload, dict) else {})
    if event is None:
        return {"status": "ignored"}
    if agent.seen_messages.seen(event["message_id"]):
        return {"status": "duplicate"}
    if not is_running("whatsapp"):
        return {"status": "paused"}

    # Respond to Unipile immediately; do the LLM work in the background.
    background.add_task(
        agent.handle_inbound,
        event["chat_id"],
        event["text"],
        event["sender_name"],
        event["sender_number"],
    )
    return {"status": "accepted"}


@router.post("/webhooks/zernio")
async def zernio_webhook(
    request: Request,
    background: BackgroundTasks,
    x_zernio_signature: str | None = Header(default=None, alias="X-Zernio-Signature"),
) -> dict[str, str]:
    """Zernio inbox webhook (event: message.received).

    Signature: lowercase hex HMAC-SHA256 of the raw body keyed by
    ZERNIO_WEBHOOK_SECRET, delivered as X-Zernio-Signature.
    """
    if ZERNIO_WEBHOOK_SECRET:
        raw = await request.body()
        if not x_zernio_signature:
            raise HTTPException(status_code=401, detail="missing X-Zernio-Signature")
        computed = hmac.new(ZERNIO_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, x_zernio_signature.strip().lower()):
            raise HTTPException(status_code=400, detail="invalid signature")
        try:
            payload = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
    else:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")

    event = parse_zernio_event(payload if isinstance(payload, dict) else {})
    if event is None:
        return {"status": "ignored"}
    if agent.seen_messages.seen(event["message_id"]):
        return {"status": "duplicate"}
    if not is_running("whatsapp"):
        return {"status": "paused"}

    # Remember which Zernio account owns this conversation so replies can be sent.
    if not agent.zernio.account_id and event.get("account_id"):
        agent.zernio.account_id = event["account_id"]

    # Acknowledge fast; do the LLM work in the background (Zernio retries after 5s).
    background.add_task(
        agent.handle_inbound,
        event["chat_id"],
        event["text"],
        event["sender_name"],
        event["sender_number"],
    )
    return {"status": "accepted"}


@router.post("/run")
async def run_agent() -> dict[str, Any]:
    """Turn the WhatsApp agent ON (persisted — stays on across restarts)."""
    return set_running("whatsapp", True)


@router.post("/pause")
async def pause_agent() -> dict[str, Any]:
    """Turn the WhatsApp agent OFF without shutting the backend down."""
    return set_running("whatsapp", False)


@router.get("/messenger")
async def messenger_status() -> dict[str, Any]:
    """Which transport is active and what is configured for each."""
    return {
        "active": agent.messenger_name,
        "running": is_running("whatsapp"),
        "env": WA_MESSENGER,
        "unipile": {
            "dsn": UNIPILE_DSN,
            "account_id": UNIPILE_ACCOUNT_ID,
            "configured": bool(UNIPILE_DSN and UNIPILE_ACCESS_TOKEN),
        },
        "zernio": {
            "account_id": agent.zernio.account_id or ZERNIO_ACCOUNT_ID,
            "configured": bool(ZERNIO_API_KEY),
            "webhook_secret_set": bool(ZERNIO_WEBHOOK_SECRET),
        },
    }


@router.post("/admin/sessions/{chat_id}/resume")
async def resume_session(
    chat_id: str, x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret")
) -> dict[str, Any]:
    """Staff finished handling the chat; hand it back to SARA."""
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    return {"chat_id": chat_id, "resumed": agent.sessions.resume(chat_id)}


@router.delete("/admin/sessions/{chat_id}")
async def reset_session(
    chat_id: str, x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret")
) -> dict[str, Any]:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    agent.sessions.reset(chat_id)
    return {"chat_id": chat_id, "reset": True}
