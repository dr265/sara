"""
SARA v6 Cognitive Integration — wires the cognitive layer into the v5 engine.

Imported AT THE END of full_ai_v3 so every engine symbol already exists.
Additive only: nothing in the v5 surface is removed or renamed.
  - Agent.handle is wrapped: cognitive intents (undo/continuation/correction/
    memory queries/preferences) are handled by the CognitiveController; the
    first turn of every other goal ALSO flows through cognition (graph +
    episode tracking) before the stock planner/executor path runs.
  - Executor._dup gains undo-capture via cognition when available.
  - shutdown() persists graphs (already durable — SQLite WAL).
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time

import full_ai_v3 as _eng          # engine module (already fully loaded)
from full_ai_v3 import (           # names used below
    Agent, Executor, MEM, BUS, SPEAK, REG, Level, ActionFailed,
    tool, _norm, _redact, shutdown,
)
import sara_cognition as cog

log = logging.getLogger("sara.cognition")

# ---------------------------------------------------------------- controller
COGNITION = None  # set in _init_cognition() (after ENGINE globals exist)


def _init_cognition():
    global COGNITION
    if COGNITION is None:
        COGNITION = cog.get_cognition(_eng)
    return COGNITION


# Lazy: never build the controller at import time (the module is imported by
# the unified backend for ALL agents; keep first-touch cheap).
def cognition():
    return COGNITION if COGNITION is not None else _init_cognition()


# Idempotency: this module may be imported twice (first attempt can fail
# partway — e.g. a missing symbol — after tools/wraps are already applied;
# failed imports don't stay in sys.modules, so a retry re-executes everything).
# Drop our own previous registrations/wraps so a retry is a clean re-apply.
if getattr(Agent.handle, "__sara_wrapped__", False):
    Agent.handle = Agent.handle.__sara_orig__
if getattr(Executor.run, "__sara_wrapped__", False):
    Executor.run = Executor.run.__sara_orig__
REG.pop("cognition", None)
REG.pop("task_graph", None)


# ============================================================================
# 1. Agent.handle — cognitive routing (wraps the stock implementation)
# ============================================================================
_agent_handle_orig = Agent.handle


def _cognitive_pre(text: str) -> str | None:
    """Return a reply if cognition fully handled this utterance, else None."""
    try:
        c = cognition()
    except Exception as e:
        log.warning("cognition unavailable: %s", e)
        return None
    intent = c.interpreter.classify(text)
    if intent.kind in ("undo", "cancel", "memory_query", "preference", "memory_store",
                       "correction", "continuation"):
        try:
            return c.handle(text, "user")
        except Exception as e:
            log.exception("cognitive handle failed")
            return None
    return None


def _agent_handle_with_cognition(self, text, trigger="user"):
    text = (text or "").strip()
    if not text:
        return ""
    # quick commands keep priority (quit / pause watching / task status …)
    pre = None
    try:
        pre = _cognitive_pre(text)
    except Exception:
        pre = None
    if pre is not None:
        MEM.turn("user", text)
        MEM.turn("sara", pre)
        BUS.emit("user_input", text=_redact(text))
        SPEAK.say(pre)
        return pre
    # stock path for everything else
    reply = _agent_handle_orig(self, text, trigger)
    # feed the outcome into cognition so episodes/entities/projects stay complete
    try:
        c = cognition()
        c.conversation.append({"role": "sara", "text": reply or "", "ts": time.time()})
        c.note_external_outcome(MEM.last_goal or text, reply or "")
    except Exception:
        pass
    return reply


Agent.handle.__sara_wrapped__ = True          # type: ignore[attr-defined]
Agent.handle.__sara_orig__ = _agent_handle_orig  # type: ignore[attr-defined]
Agent.handle = _agent_handle_with_cognition  # type: ignore[assignment]


# ============================================================================
# 1b. Executor.run — undo journaling for EVERY mutating step (stock + cognitive)
# ============================================================================
_exec_run_orig = Executor.run


def _exec_run_with_undo(self, steps, trigger="user", ctx=None, on_step=None):
    c = None
    try:
        c = cognition()
    except Exception:
        c = None
    if c is None:
        return _exec_run_orig(self, steps, trigger, ctx, on_step)
    pre = []
    for s in steps:
        t = REG.get(s.tool)
        if t is not None and t.category in ("files", "window"):
            pre.append({"tool": s.tool, "args": dict(s.args),
                        "before": c.undo.capture_before(s.tool, s.args)})
    results = _exec_run_orig(self, steps, trigger, ctx, on_step)
    try:
        for i, rec in enumerate(results):
            if i >= len(pre):
                break
            meta = pre[i]
            if rec.get("ok") and not rec.get("skipped") and rec.get("tool") == meta["tool"]:
                after = c.undo.capture_after(meta["tool"], meta["args"], rec.get("output") or {})
                inverse = c.undo.compute_inverse(meta["tool"], meta["args"], meta["before"], after)
                if inverse:
                    c.store.log_undo("-", meta["tool"], meta["args"], meta["before"], after, inverse)
    except Exception:
        log.debug("undo journal capture failed", exc_info=True)
    return results


Executor.run.__sara_wrapped__ = True          # type: ignore[attr-defined]
Executor.run.__sara_orig__ = _exec_run_orig   # type: ignore[attr-defined]
Executor.run = _exec_run_with_undo  # type: ignore[assignment]


# ============================================================================
# 2. Cognitive tools — exposed through the standard REG so /tools/call,
#    the planner and voice can all use them.
# ============================================================================
@tool("cognition", "Persistent cognitive memory: status, recall, remember, resume the last task, explain a memory, undo history.", Level.LOW,
      category="memory", action="str!", content="str", query="str", memory_id="int", kind="str")
def t_cognition(action, content=None, query=None, memory_id=None, kind=None):
    c = cognition()
    a = _norm(action)
    if a == "status":
        return {**cog.cognitive_status(), "verified": True}
    if a == "recall":
        items = c.retriever.recall(query or "", limit=10)
        return {"memories": items, "verified": True}
    if a == "remember":
        kind_ = _norm(kind) or "fact"
        mid = c.store.remember(kind_, str(content or ""), tags=("tool",))
        return {"remembered_id": mid, "verified": bool(mid)}
    if a == "forget":
        # deactivate semantically matching memories
        items = c.retriever.recall(query or "", limit=5)
        for it in items:
            if it["similarity"] >= 0.45:
                c.store.deactivate(it["id"])
        return {"forgotten": len(items), "verified": True}
    if a == "why":
        return {"explanation": c.why_remembered(int(memory_id or 0)), "verified": True}
    if a == "resume_last":
        g = c.store.latest_open_graph() or c.store.latest_completed_graph()
        if not g:
            return {"note": "no previous task found", "verified": True}
        return {"task": g["goal"][:120], "state": g["state"],
                "summary": g.get("summary", ""), "verified": True}
    if a == "undo_history":
        return {"history": c.store.undo_history(), "verified": True}
    if a == "projects":
        return {"projects": c.store.all_projects(), "verified": True}
    raise ActionFailed(f"cognition: unknown action '{action}'. "
                       "Use status|recall|remember|forget|why|resume_last|undo_history|projects")


@tool("task_graph", "Inspect or advance the persistent task graph: current state, pending steps, completion.", Level.LOW,
      category="memory", action="str!")
def t_task_graph(action):
    c = cognition()
    g = c.graphs.current
    a = _norm(action)
    if a == "current":
        return {"graph": g.dump() if g else None, "verified": True}
    if a == "open":
        return {"open_tasks": c.store.open_graphs(), "verified": True}
    if a == "complete":
        if not g:
            raise ActionFailed("No active task graph.")
        g.state = "completed"
        for n in g.nodes.values():
            if n.status in ("pending", "ready", "running", "waiting", "blocked"):
                n.status = "completed"
        g.save(c.store)
        return {"status": "completed", "verified": True}
    raise ActionFailed("task_graph: unknown action. Use current|open|complete")


# ============================================================================
# 3. shutdown — flush cognition
# ============================================================================
_shutdown_orig = shutdown
_shutdown_installed = False


def _shutdown_with_cognition():
    global _shutdown_installed
    if _shutdown_installed:
        return _shutdown_orig()
    _shutdown_installed = True
    try:
        if COGNITION is not None:
            g = COGNITION.graphs.current
            if g and g.state == "active":
                g.state = "waiting"          # resumable after restart
                for n in g.nodes.values():
                    if n.status == "running":
                        n.status = "waiting"
                g.save(COGNITION.store)
            COGNITION.store.close()
    except Exception:
        pass
    _shutdown_orig()


# Rebind the engine's shutdown so `python full_ai_v3.py` persists graphs too.
# (atexit runs ours LIFO-first, then the original — both are idempotent.)
import atexit as _atexit
_eng.shutdown = _shutdown_with_cognition
_atexit.register(_shutdown_with_cognition)
