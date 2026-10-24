"""Desktop agent API routes, served by the unified SARA backend.

The engine (tools, executor, memory, planner) lives in full_ai_v3.py; this module
exposes it over HTTP/WebSocket so everything runs on ONE backend port.

Import is LAZY on purpose: importing full_ai_v3 probes the desktop (pyautogui,
uiautomation, speech_recognition + comtypes), calibrates the microphone and can
take tens of seconds — which must not delay backend startup for the agents
(WhatsApp/Telegram/etc.) that never use it. The engine loads on the first
desktop-agent request and then stays cached.
"""
import asyncio
import os
import secrets
import threading
import uuid
from typing import Any, Dict
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter(tags=["Desktop Agent"])

# Bearer token required on every route (value lives in backend/.env as FULLAI_API_TOKEN)
FULLAI_API_TOKEN = os.environ.get("FULLAI_API_TOKEN") or ""
if not FULLAI_API_TOKEN:
    FULLAI_API_TOKEN = secrets.token_urlsafe(24)
    print("WARNING: FULLAI_API_TOKEN not set; generated an ephemeral token. "
          "Add it to backend/.env to keep it stable across restarts.")

# Human-confirmation registry (for CONFIRM-level tools invoked over the API)
PENDING_CONFIRMS: Dict[str, Dict[str, Any]] = {}

# --------------------------------------------------------------------------- #
# Lazy engine loader — full_ai_v3 is heavy (desktop probes + mic calibration),
# so it is imported only on the first request that actually needs it.
# --------------------------------------------------------------------------- #
_engine_module: Any = None
_engine_lock = threading.Lock()


def _engine() -> Any:
    """Import and cache the full_ai_v3 engine module (thread-safe)."""
    global _engine_module
    if _engine_module is None:
        with _engine_lock:
            if _engine_module is None:
                import full_ai_v3 as m
                _engine_module = m
                _restore_engine_logging(m)
    return _engine_module


def _restore_engine_logging(m: Any) -> None:
    """Uvicorn's logging config disables loggers that already exist; the engine is imported lazily
    *after* that, so re-enable its logger and file handler or engine diagnostics vanish silently."""
    try:
        if hasattr(m, "_engine_logging"):
            m._engine_logging()
    except Exception:
        pass


async def _engine_async() -> Any:
    """Load the engine off the event loop so the first request doesn't stall it.

    Also binds the engine's event bus to the loop serving this request: Bus.emit() hops from the
    engine's worker threads onto that loop, and an unbound bus silently drops every event (which is
    why the workspace telemetry used to sit at "System idle" while a plan was running).
    """
    m = await asyncio.to_thread(_engine)
    try:
        m.BUS.bind(asyncio.get_running_loop())
    except Exception:
        pass
    return m


def require_token(authorization: str = Header(default=""), x_api_key: str = Header(default="")):
    provided = (authorization[7:] if authorization.lower().startswith("bearer ") else authorization).strip()
    if not provided:
        provided = x_api_key.strip()
    if not provided or not hmac.compare_digest(provided, FULLAI_API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid API token")


def run_tool_with_policy(m, t, args: dict):
    """Run a tool, or queue a human confirmation when its level exceeds the auto threshold."""
    if m.AUTH.locked:
        raise m.PolicyViolation("Agent is locked. Use /unlock first.")
    if t.level <= m.AUTH.auto_max:
        return t.run(args)
    cid = uuid.uuid4().hex[:12]
    prompt = t.confirm_prompt or f"Proceed with {t.name.replace('_', ' ')}?"
    try:
        prompt = prompt.format(**{k: str(v)[:70] for k, v in args.items()})
    except Exception:
        pass
    PENDING_CONFIRMS[cid] = {"tool": t.name, "args": dict(args), "prompt": prompt}
    m.BUS.emit("confirmation_required", confirmation_id=cid, action=t.name,
               target=str(args.get("text") or args.get("app") or args.get("url") or "")[:80],
               why=prompt, impact=t.description, level=int(t.level))
    return {"confirmation_required": cid, "prompt": prompt}


class GoalIn(BaseModel):
    goal: str


class ToolCallIn(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)


class ConfirmIn(BaseModel):
    confirmation_id: str
    approved: bool = True


class VisionIn(BaseModel):
    """A frame the user explicitly shared from their browser (live screen share)."""
    image: str
    question: str = ""


class FrameIn(BaseModel):
    image: str


@router.get("/capabilities", dependencies=[Depends(require_token)])
async def capabilities():
    m = await _engine_async()
    return {"capabilities": m.CAPS, "registered_tools": sorted(m.REG), "context": m.MEM.brief()}


@router.post("/tasks", dependencies=[Depends(require_token)])
async def create_task(b: GoalIn):
    m = await _engine_async()
    if m.AUTH.locked:
        raise HTTPException(status_code=423, detail="Agent is locked. Use /unlock first.")
    if m.AGENT.paused:
        return {"status": "paused", "say": "System is paused.", "plan": []}
    steps, say = await asyncio.to_thread(m.llm_plan, m.MEM.resolve(b.goal))
    if steps:
        m.MEM.last_goal, m.MEM.last_plan = b.goal, list(steps)
        asyncio.create_task(asyncio.to_thread(m.EXEC.run_plan, b.goal, steps))
    return {"status": "started" if steps else "no_plan", "say": say, "plan": [s.dump() for s in steps]}


@router.post("/tools/call", dependencies=[Depends(require_token)])
async def tools_call(b: ToolCallIn):
    m = await _engine_async()
    t = m.REG.get(b.tool)
    if not t:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {b.tool}")
    if m.AUTH.locked:
        raise HTTPException(status_code=423, detail="Agent is locked. Use /unlock first.")
    try:
        out = await asyncio.to_thread(run_tool_with_policy, m, t, b.args)
        return {"output": out}
    except m.PolicyViolation as e:
        raise HTTPException(status_code=403, detail=str(e))
    except m.ActionFailed as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/confirm", dependencies=[Depends(require_token)])
async def confirm(b: ConfirmIn):
    m = await _engine_async()
    pending = PENDING_CONFIRMS.pop(b.confirmation_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Unknown or expired confirmation")
    if not b.approved:
        m.BUS.emit("step_failed", tool=pending["tool"], step=0, error="cancelled by user")
        return {"status": "denied"}
    t = m.REG.get(pending["tool"])
    if not t:
        raise HTTPException(status_code=404, detail="Tool no longer registered")
    try:
        out = await asyncio.to_thread(t.run, pending["args"])
        m.BUS.emit("step_completed", tool=t.name, step=0, output=out)
        return {"status": "approved", "output": out}
    except Exception as e:
        m.BUS.emit("step_failed", tool=t.name, step=0, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vision/analyze", dependencies=[Depends(require_token)])
async def vision_analyze(b: VisionIn):
    """Answer a question about a frame the user shared live (screen share from the workspace page).

    Runs through the engine's LLM failover chain with vision-capable models first, and publishes the
    result on the WebSocket bus so every viewer sees what SARA saw.
    """
    m = await _engine_async()
    if m.AUTH.locked:
        raise HTTPException(status_code=423, detail="Agent is locked. Use /unlock first.")
    if not b.image.startswith("data:image"):
        raise HTTPException(status_code=400, detail="image must be a data:image/... URL")
    try:
        answer = await asyncio.to_thread(m.vision_analyze, b.image, b.question)
    except m.ActionFailed as e:
        m.BUS.emit("vision_failed", question=b.question[:160], error=str(e)[:300])
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        m.BUS.emit("vision_failed", question=b.question[:160], error=str(e)[:300])
        raise HTTPException(status_code=500, detail=str(e))
    m.BUS.emit("vision_result", question=b.question[:160], answer=answer[:800])
    return {"answer": answer}


@router.post("/vision/frame", dependencies=[Depends(require_token)])
async def vision_frame(b: FrameIn):
    """Relay one live-share frame onto the bus so every connected viewer sees the same screen."""
    m = await _engine_async()
    if not b.image.startswith("data:image"):
        raise HTTPException(status_code=400, detail="image must be a data:image/... URL")
    m.BUS.emit("stream_frame", image=b.image, source="client_share")
    return {"status": "relayed", "bytes": len(b.image)}


@router.post("/emergency-stop", dependencies=[Depends(require_token)])
async def emergency_stop():
    m = await _engine_async()
    m.AUTH.locked = True
    m.EXEC.cancel.set()
    PENDING_CONFIRMS.clear()
    m.BUS.emit("emergency_stop")
    return {"status": "stopped"}


@router.post("/unlock", dependencies=[Depends(require_token)])
async def unlock():
    m = await _engine_async()
    m.AUTH.locked = False
    m.EXEC.cancel.clear()
    m.BUS.emit("unlocked")
    return {"status": "unlocked"}


@router.post("/pause", dependencies=[Depends(require_token)])
async def pause():
    m = await _engine_async()
    m.AGENT.paused = True
    m.BUS.emit("paused")
    return {"status": "paused"}


@router.post("/resume", dependencies=[Depends(require_token)])
async def resume():
    m = await _engine_async()
    m.AGENT.paused = False
    m.BUS.emit("resumed")
    return {"status": "resumed"}


@router.websocket("/ws")
async def ws(sock: WebSocket, token: str = Query(default="")):
    if token != FULLAI_API_TOKEN:
        await sock.close(code=4401)
        return
    m = await _engine_async()
    await sock.accept()
    qq = m.BUS.sub()
    await sock.send_json({"kind": "hello", "app": "SARA Desktop Agent",
                          "capabilities": m.CAPS, "tools": sorted(m.REG)})
    try:
        while True:
            await sock.send_json(await qq.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        m.BUS.unsub(qq)