"""
SARA Autonomous Agent — real tool-calling brain (JARVIS-powered).

This is a full replacement of the old keyword-routing "agent". There is no
hardcoded step planner and no fabricated success reports: the LLM itself
decides which of the sara_desktop actions to call, the calls are actually
executed through the action registry, their real outputs go back into the
conversation, and the final report is generated from what really happened.

Routes:
  POST /sara-auto/run         - start a task (returns task_id)
  GET  /sara-auto/events      - SSE log stream (pass ?task_id=...)
  POST /sara-auto/approve     - approve or deny a pending confirmation
  POST /sara-auto/stop        - emergency stop
  GET  /sara-auto/status      - task status + trace
  GET  /sara-auto/capabilities- live capability list
  GET  /sara-auto/health      - health ping

Events on the SSE stream:
  meta            {model, provider, tools:[names]}        - once, at start
  log             {message}
  tool_call       {call_id, name, args}                   - dispatch begins
  approval_request{call_id, name, prompt}
  tool_result     {call_id, name, ok, output}
  done            {status, summary, steps}
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/sara-auto", tags=["SARA Autonomous Agent"])

# ---------------------------------------------------------------------------
# sara_desktop import path (inserted early so core.* is importable everywhere)
# ---------------------------------------------------------------------------
DESKTOP_DIR = Path(__file__).resolve().parents[3] / "sara_desktop"
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

MAX_TOOL_ROUNDS = 20          # hard ceiling on LLM<->tool round trips
APPROVAL_TIMEOUT_S = 300      # how long an approval may sit unanswered
TOOL_OUTPUT_LIMIT = 3500      # chars of tool output fed back to the LLM
EVENT_OUTPUT_LIMIT = 1200     # chars of tool output shown in the UI event

SYSTEM_PROMPT = """You are SARA, an autonomous desktop agent running inside the SARA AI Nexus Hub.
You operate the user's Windows PC through real tools. Today is {today}.

OPERATING RULES
1. You are an agent, not a chatbot. When the user asks for anything that can be
   done with your tools, CALL THE TOOLS — do not describe what you could do,
   do not give manual instructions, do not ask permission unless the UI gate
   asks for you.
2. Live or current facts — weather, time, news, prices, versions, scores,
   file contents, system state — MUST come from a tool. Your memory is
   outdated; answering such questions from memory is FORBIDDEN and counts as
   fabrication. Search first, answer after.
3. Choose the best tool for each need; chain several when the task needs it.
   Wait for each result before deciding the next step.
3b. Media requests have a dedicated tool: to play a song or video on
   YouTube, ALWAYS call youtube_video (action=play, query=<song>) — never
   browser_control/web_search for that. If its result says it could not
   resolve a specific video, report that honestly instead of browsing to a
   search page and stopping there.
4. Report facts only. If a tool returns an error or a partial result, say so.
   NEVER invent outcomes. NEVER claim success for something you did not
   actually execute through a tool.
5. You are already in full agent mode: never say you are a language model,
   never mention modes, never refuse a feasible task.
6. When everything needed is done (or impossible), reply with a short final
   report in plain text: what you did, key results, and anything that failed.
   Address the user politely ("sir" is fine). No markdown headers.

TOOLS
{tools}"""

# Tools that may change or destroy real state — always gated behind approval.
DESTRUCTIVE_TOOLS = {"computer_settings", "system_command"}
DESTRUCTIVE_ACTIONS = {
    "file_controller": {"delete", "rename", "move", "write", "organize_desktop"},
    "desktop_control": {"shutdown", "restart", "lock", "signout", "logoff"},
}


def _is_destructive(name: str, args: dict) -> bool:
    if name in DESTRUCTIVE_TOOLS:
        return True
    act = str((args or {}).get("action", "")).lower().strip()
    return act in DESTRUCTIVE_ACTIONS.get(name, set())


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
class _Session:
    def __init__(self, task_id: str, goal: str, mode: str = "autonomous"):
        self.task_id = task_id
        self.goal = goal
        self.mode = mode
        self.status = "queued"
        self.created = time.time()
        self.event_q: queue.Queue[dict] = queue.Queue()
        self.approve_q: queue.Queue[tuple[str, bool]] = queue.Queue()
        self.stop = threading.Event()
        self.trace: list[dict] = []
        self.steps: list[dict] = []

    def emit(self, kind: str, **kw):
        self.event_q.put({"type": kind, **kw})

    def log(self, msg: str):
        self.trace.append({"t": time.time(), "kind": "log", "message": msg})
        self.emit("log", message=msg)


_sessions: Dict[str, _Session] = {}
_lock = threading.Lock()
_registry_cache = None           # ActionRegistry, built once per process


def _new(goal: str, mode: str = "autonomous") -> _Session:
    tid = str(uuid.uuid4())
    s = _Session(tid, goal, mode)
    with _lock:
        _sessions[tid] = s
        # keep memory bounded — drop sessions older than 2h
        now = time.time()
        for k in [k for k, v in _sessions.items() if now - v.created > 7200]:
            _sessions.pop(k, None)
    return s


def _get(task_id: str) -> _Session:
    s = _sessions.get(task_id)
    if not s:
        raise HTTPException(status_code=404, detail="Task not found")
    return s


# ---------------------------------------------------------------------------
# Tool registry (shared, built once)
# ---------------------------------------------------------------------------
def _get_registry():
    global _registry_cache
    if _registry_cache is None:
        from core.action_loader import discover_actions

        def _quiet(_msg: str):
            pass

        _registry_cache = discover_actions(DESKTOP_DIR / "actions",
                                           reserved_names=set(), logger=_quiet)
    return _registry_cache


def _run_tool(name: str, args: dict) -> str:
    reg = _get_registry()
    return str(reg.run(name, args or {}))


_TYPE_MAP = {"object": "object", "string": "string", "number": "number",
             "integer": "integer", "boolean": "boolean", "array": "array"}


def _conv_schema(p: Any) -> Any:
    """Gemini-style schema (OBJECT/STRING ...) -> OpenAI-style (object/string ...)."""
    if not isinstance(p, dict):
        return p
    out = dict(p)
    t = str(p.get("type", "")).lower()
    out["type"] = _TYPE_MAP.get(t, t or "string")
    if isinstance(p.get("properties"), dict):
        out["properties"] = {k: _conv_schema(v) for k, v in p["properties"].items()}
    if isinstance(p.get("items"), dict):
        out["items"] = _conv_schema(p["items"])
    return out


def _openai_tool_declarations(decls: list[dict]) -> list[dict]:
    """Action-registry declarations (Gemini format) -> OpenAI tools format."""
    return [
        {"type": "function", "function": {
            "name": d["name"],
            "description": d["description"],
            "parameters": _conv_schema(d.get("parameters") or {"type": "object", "properties": {}}),
        }}
        for d in decls
    ]


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
def _run(session: _Session) -> None:
    session.status = "running"
    try:
        from core.llm_client import call_llm, get_llm_settings, get_llm_provider

        _, model = get_llm_settings()
        provider = get_llm_provider()
        registry = _get_registry()
        tool_names = sorted(registry.names())

        session.emit("meta", model=model, provider=provider, tools=tool_names)
        session.log(f"[SARA] Goal: {session.goal}")
        session.log(f"[SARA] Engine online: {model} ({provider}) · {len(tool_names)} tools armed")

        from datetime import datetime
        system = SYSTEM_PROMPT.format(
            today=datetime.now().strftime("%A, %d %B %Y"),
            tools="\n".join(f"- {n}: {registry._actions[n].description}"
                            for n in tool_names) or "(none)",
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                session.goal + "\n\n(Use your tools to gather live facts before answering.)"},
        ]
        tools = _openai_tool_declarations(registry.get_tool_declarations())

        final = ""
        round_no = 0
        while round_no < MAX_TOOL_ROUNDS and not session.stop.is_set():
            round_no += 1
            try:
                result = call_llm(messages, tools=tools, timeout=180)
            except Exception as e:
                raise RuntimeError(f"LLM call failed: {e}")

            content = (result.get("content") or "").strip()
            tool_calls = result.get("tool_calls") or []

            if not tool_calls:
                final = content
                break

            # Execute every requested tool, sequentially.
            assistant_msg: dict = {"role": "assistant", "content": content or "",
                                   "tool_calls": [
                                       {"id": tc.get("id") or f"call_{i}",
                                        "type": "function",
                                        "function": {"name": tc["function"]["name"],
                                                     "arguments": json.dumps(
                                                         tc["function"].get("arguments") or {},
                                                         ensure_ascii=False)}}
                                       for i, tc in enumerate(tool_calls)
                                   ]}
            messages.append(assistant_msg)

            for tc in tool_calls:
                if session.stop.is_set():
                    break
                call_id = tc.get("id") or f"call_{round_no}"
                name = tc["function"]["name"]
                args = tc["function"].get("arguments") or {}

                if not registry.has(name):
                    out = f"Tool '{name}' does not exist. Available: {', '.join(tool_names)}"
                    ok = False
                    session.log(f"[SARA] ✗ Unknown tool '{name}' requested")
                else:
                    gated = session.mode == "assisted" or _is_destructive(name, args)
                    granted = True
                    if gated and not session.stop.is_set():
                        prompt = f"Approve {name}?" + (
                            f" — {json.dumps(args, ensure_ascii=False)[:200]}"
                            if args else "")
                        session.emit("approval_request", call_id=call_id,
                                     name=name, prompt=prompt)
                        session.log(f"[SARA] ⏸ Approval required: {name} {args}")
                        try:
                            _cid, granted = session.approve_q.get(timeout=APPROVAL_TIMEOUT_S)
                        except queue.Empty:
                            granted = False
                    if session.stop.is_set():
                        break
                    if not granted:
                        out = "Denied by the user. Do not retry this action; explain and continue."
                        ok = False
                        session.log(f"[SARA] ✗ Denied by user: {name}")
                    else:
                        if gated:
                            session.log(f"[SARA] ✓ Approved: {name}")
                        out, ok = _exec(session, call_id, name, args)

                session.steps.append({"name": name, "args": args, "ok": ok})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": (out or "(no output)")[:TOOL_OUTPUT_LIMIT],
                })

        if session.stop.is_set():
            session.status = "stopped"
            session.log("[SARA] Stopped by user.")
            session.emit("done", status="stopped",
                         summary="Stopped by user.", steps=session.steps)
            return

        if not final:
            # Round budget exhausted — get a plain-language report without tools.
            messages.append({"role": "user", "content":
                "Tool budget reached. Give your final report now based on what actually happened."})
            try:
                final = (call_llm(messages, tools=None, timeout=120).get("content") or "").strip()
            except Exception as e:
                final = f"Task ended before completion (report call failed: {e})."

        # ── HONEST COMPLETION ────────────────────────────────────────────
        # A task may only be reported 'completed' when at least one tool ran
        # AND every executed tool succeeded. Failures downgrade the status to
        # 'completed_with_issues' so the UI never shows a green "MISSION
        # ACCOMPLISHED" badge for work that did not actually happen.
        executed   = [s for s in session.steps]
        ok_steps   = [s for s in executed if s.get("ok")]
        bad_steps  = [s for s in executed if not s.get("ok")]
        no_tools   = not executed

        if no_tools or bad_steps:
            failed_names = ", ".join(s["name"] for s in bad_steps[:5]) or "(none)"
            warn = (
                "TASK INCOMPLETE — one or more tool actions failed or were "
                "denied/unknown (" + failed_names + "). Do NOT claim the goal "
                "was achieved. In your final report, state clearly what "
                "succeeded, what failed and why, and what remains undone."
            )
            messages.append({"role": "user", "content": warn})
            try:
                final = (call_llm(messages, tools=None, timeout=120).get("content") or "").strip() or final
            except Exception:
                pass
            if no_tools:
                status = "failed"
                final = final or "No tool actions were executed — nothing was done."
                session.log("[SARA] Task ended with NO executed actions.")
            else:
                status = "completed_with_issues"
                final = ("⚠ Completed with issues — "
                         f"{len(bad_steps)} of {len(executed)} tool actions failed.\n\n" + final)
                session.log(f"[SARA] Task finished with issues: "
                            f"{len(ok_steps)} OK / {len(bad_steps)} failed.")
        else:
            status = "completed"
            session.log("[SARA] Task complete.")

        session.status = status
        session.emit("done", status=status,
                     summary=final or "Done.", steps=session.steps)

    except Exception as exc:
        session.status = "failed"
        session.log(f"[SARA] ERROR: {exc}")
        session.emit("done", status="failed", summary=str(exc), steps=session.steps)


# Substrings that mark a tool's textual output as a failure even when the
# action ran without throwing. Tools return human-readable strings, so failure
# detection must be content-based; unknown-tool and crash outputs are caught
# separately below.
_FAIL_MARKERS = (
    "could not", "cannot create", "failed to", "failed:", "error:",
    "not found", "no such", "unknown action", "unknown pptx action",
    "unknown file type", "does not exist", "not installed",
    "access denied", "permission denied", "likely did not open",
    "generation failed", "execution halted", "timed out",
)


def _looks_failed(output: str) -> bool:
    # Failure messages from these tools always appear at the START of the
    # output ("Could not X", "File not found: ..."), so only the head is
    # scanned — a successful web_search's article text can legitimately
    # contain words like "could not" deeper inside.
    low = (output or "")[:300].lower()
    return any(m in low for m in _FAIL_MARKERS)


def _exec(session: _Session, call_id: str, name: str, args: dict) -> tuple[str, bool]:
    """Dispatch one tool call for real, emitting its events. Returns (output, ok)."""
    pretty = json.dumps(args, ensure_ascii=False) if args else "{}"
    session.emit("tool_call", call_id=call_id, name=name, args=args)
    session.log(f"[SARA] ▶ {name}({pretty})")
    t0 = time.time()
    try:
        out = _run_tool(name, args)
        low = (out or "").lower()
        ok = not (
            low.startswith("tool '")
            or low.startswith(f"action '{name}' is not available")
            or low.startswith("browser error")
            or _looks_failed(out)
        )
    except Exception as e:
        out, ok = f"{name} crashed: {e}", False
    dt = time.time() - t0
    short = (out or "")[:EVENT_OUTPUT_LIMIT]
    session.emit("tool_result", call_id=call_id, name=name, ok=ok,
                 output=short, seconds=round(dt, 1))
    session.log(f"[SARA] ◀ {name} → {'OK' if ok else 'FAILED'} ({dt:.1f}s): "
                f"{(out or '')[:160]}")
    return out, ok


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    goal: str
    mode: str = "autonomous"


class ApproveRequest(BaseModel):
    task_id: str
    call_id: Optional[str] = None
    action: str = "APPROVE"


class StopRequest(BaseModel):
    task_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/run")
async def run_task(body: RunRequest):
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="Goal cannot be empty")
    session = _new(body.goal.strip(), mode="assisted" if body.mode == "assisted" else "autonomous")
    threading.Thread(target=_run, args=(session,), daemon=True,
                     name=f"sara-agent-{session.task_id[:8]}").start()
    return {"task_id": session.task_id, "status": "started",
            "goal": session.goal, "mode": session.mode}


@router.get("/events")
async def stream_events(task_id: str):
    session = _get(task_id)

    async def _gen():
        last_beat = time.time()
        while True:
            try:
                ev = session.event_q.get_nowait()
            except queue.Empty:
                if time.time() - last_beat > 15:
                    last_beat = time.time()
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.08)
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                break

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
               "Connection": "keep-alive"}
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


@router.post("/approve")
async def approve_step(body: ApproveRequest):
    session = _get(body.task_id)
    granted = str(body.action).upper() != "DENY"
    session.approve_q.put((body.call_id or "", granted))
    return {"status": "ok", "action": "APPROVE" if granted else "DENY"}


@router.post("/stop")
async def stop_task(body: StopRequest):
    session = _get(body.task_id)
    session.stop.set()
    # Unblock any pending approval wait so the thread can exit promptly.
    session.approve_q.put(("", False))
    session.log("[SARA] Stop requested by user.")
    return {"status": "stopping"}


@router.get("/status")
async def get_status(task_id: str):
    s = _get(task_id)
    return {"task_id": task_id, "status": s.status, "goal": s.goal,
            "mode": s.mode, "steps": s.steps, "trace": s.trace[-200:]}


@router.get("/capabilities")
async def capabilities():
    try:
        names = sorted(_get_registry().names())
    except Exception as e:
        names = []
        err = str(e)
    else:
        err = ""
    return {
        "agent": "SARA Autonomous Agent (JARVIS core)",
        "version": "3.0.0",
        "model": "LLM-driven tool calling over the sara_desktop action registry",
        "tools": names,
        "capabilities": [
            "LLM-planned multi-step execution (no keyword routing)",
            "Real tool dispatch with real outputs fed back to the model",
            "Assisted mode: approve every action",
            "Autonomous mode: act freely, approve destructive actions only",
            "SSE event stream with live tool calls and results",
            "Emergency stop",
        ],
        "error": err,
        "status": "online" if names else "degraded",
    }


@router.get("/health")
async def health():
    return {"status": "online", "agent": "SARA Autonomous Agent",
            "core": "jarvis-tool-loop", "version": "3.0.0"}
