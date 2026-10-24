"""
core/agentic_runtime.py — the Agentic Mode runtime for Jarvis.

Owns two things:

  1. THE MODE SWITCH. Exactly one of SARA NORMAL / SARA AGENTIC is active.
     NORMAL behaves exactly as before — this runtime never touches the mic,
     the Gemini session, or any normal-mode command path. AGENTIC arms the
     task router: voice/text requests may be planned and executed by real
     agents. Switching back stops NEW requests from being routed; already
     running tasks keep running unless cancellation is requested (spec §3).

  2. TASK LIFECYCLE. Every task gets a unique id, timestamps, the recognized
     request, selected agents, ordered execution events, outputs and a final
     status — pending → planning → (waiting_confirmation →) running →
     completed | failed | cancelled. Cancellation sets a real stop flag the
     executor honours between steps (and asks the backend to stop when the
     work runs there); a task is only reported cancelled after the underlying
     operation actually stopped (spec §9).

Events flow through one sink callable (kind, payload) that main.py wires to
the desktop panel and the dashboard WebSocket — the UI only ever renders what
this runtime reports; nothing is fabricated.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from core.agent_registry import AgentState, UnifiedAgentRegistry


class RuntimeMode(str, Enum):
    NORMAL                 = "normal"                 # existing Jarvis behaviour, untouched
    AGENTIC                = "agentic"                # agent discovery + task routing active
    SARA_BOTS_AUTONOMOUS   = "sara_bots_autonomous"   # Sara Bots framework + autonomous workflows
    ADAPTIVE               = "adaptive"               # intelligent fallback: direct → agentic → bots → create bot → report blocker


class TaskStatus(str, Enum):
    PENDING              = "pending"
    PLANNING             = "planning"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING              = "running"
    COMPLETED            = "completed"
    FAILED               = "failed"
    CANCELLED            = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                        TaskStatus.CANCELLED)


MAX_EVENTS_PER_TASK = 200     # bounded trace; oldest dropped first
MAX_RETAINED_TASKS  = 50
DEDUPE_WINDOW_S     = 12.0    # same utterance must not start two tasks


@dataclass
class AgentTask:
    """One executed (or executing) request — the unit the UI displays."""
    task_id: str
    request: str                       # the recognized speech/text
    source: str = "voice"              # voice | text | tool
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: TaskStatus = TaskStatus.PLANNING
    #: Router decision: {"kind", "agents": [...], "steps": [...], ...}
    decision: Dict[str, Any] = field(default_factory=dict)
    agents: List[str] = field(default_factory=list)
    #: Per-step records: {agent, state, output, error, t_start, t_end}
    steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Real outputs the steps produced (audio/files/links) — collected by
    #: the router from actual payloads and output text, shown in the viewer.
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    result_text: str = ""
    error: str = ""
    #: Backend task id (Ultron / sara-auto) when the work runs remotely.
    external_task_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    #: Set once the underlying operation really stopped.
    cancel_confirmed: bool = False

    # -- mutations (called under TaskManager lock or from the worker) -------

    def add_event(self, kind: str, text: str, **extra: Any) -> None:
        self.events.append({"t": time.time(), "kind": kind,
                            "text": text[:500], **extra})
        if len(self.events) > MAX_EVENTS_PER_TASK:
            del self.events[: len(self.events) - MAX_EVENTS_PER_TASK]

    @property
    def active(self) -> bool:
        return not self.status.terminal

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(end - (self.started_at or self.created_at), 1)

    def to_dict(self, include_events: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "task_id": self.task_id,
            "request": self.request,
            "source": self.source,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "agents": list(self.agents),
            "steps": list(self.steps),
            "artifacts": list(self.artifacts),
            "result": self.result_text,
            "error": self.error,
            "external_task_id": self.external_task_id,
            "cancellable": (not self.status.terminal) and bool(
                self.external_task_id or not self.cancel.is_set()),
            "cancel_requested": self.cancel.is_set(),
            "elapsed_s": self.elapsed,
            "decision_kind": self.decision.get("kind", ""),
        }
        if include_events:
            d["events"] = list(self.events[-40:])
        return d


class TaskManager:
    """Thread-safe store of AgentTask objects with bounded retention."""

    def __init__(self) -> None:
        self._tasks: Dict[str, AgentTask] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()
        self._last_request: str = ""
        self._last_request_at: float = 0.0

    def create(self, request: str, source: str) -> AgentTask:
        task = AgentTask(task_id=str(uuid.uuid4()),
                         request=request[:2000], source=source)
        with self._lock:
            self._tasks[task.task_id] = task
            self._order.append(task.task_id)
            while len(self._order) > MAX_RETAINED_TASKS:
                drop = self._order.pop(0)
                old = self._tasks.pop(drop, None)
                if old and old.active:
                    # never silently forget a running task — put it back
                    self._order.append(drop)
                    self._tasks[drop] = old
                    break
            self._last_request = request.strip().lower()
            self._last_request_at = time.time()
        return task

    def is_duplicate(self, request: str) -> Optional[AgentTask]:
        """Same utterance inside the dedupe window → the task already made."""
        text = request.strip().lower()
        with self._lock:
            if (text and text == self._last_request
                    and time.time() - self._last_request_at < DEDUPE_WINDOW_S):
                for tid in reversed(self._order):
                    t = self._tasks.get(tid)
                    if t and t.request.strip().lower() == text:
                        return t
        return None

    def get(self, task_id: str) -> Optional[AgentTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def active(self) -> Optional[AgentTask]:
        """Newest non-terminal task, if any."""
        with self._lock:
            for tid in reversed(self._order):
                t = self._tasks.get(tid)
                if t and t.active:
                    return t
        return None

    def recent(self, limit: int = 10) -> List[AgentTask]:
        with self._lock:
            rows = [self._tasks[t] for t in reversed(self._order)
                    if t in self._tasks]
        return rows[:limit]

    def all_terminal_or_active(self) -> List[AgentTask]:
        with self._lock:
            return [self._tasks[t] for t in self._order if t in self._tasks]

    def set_status(self, task_id: str, status: TaskStatus) -> None:
        """Update the lifecycle of one task (used by the runtime's workers)."""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = status

    def all_terminal_or_active(self) -> List[AgentTask]:
        with self._lock:
            return [self._tasks[t] for t in self._order if t in self._tasks]
            return [self._tasks[t] for t in self._order if t in self._tasks]


class AgenticRuntime:
    """Mode manager + task router host for Jarvis.

    Wiring (done by main.py):
        runtime = AgenticRuntime(registry, emit=self._on_agentic_event)
        runtime.router = TaskRouter(...)          # built after actions exist
        runtime.on_mode_change = callback         # to inject [MODE_CHANGE]
        runtime.initialize()                      # background discovery+probe

    Nothing here touches audio, the microphone or the Gemini session, so
    mode switches can never create duplicate listeners (spec §9).
    """

    def __init__(self, registry: UnifiedAgentRegistry,
                 emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 confirm: Optional[Callable[[str, str, Callable[[], str]], str]] = None):
        self.registry = registry
        self.tasks = TaskManager()
        #: kind -> payload sink wired to UI + dashboard by main.py
        self._emit = emit
        #: (title, detail, runnable) -> instruction text; core.confirm.request
        self._confirm = confirm
        #: set by main.py — core.agent_router.TaskRouter
        self.router: Any = None
        #: called(mode_str) after every real mode change
        self.on_mode_change: Optional[Callable[[str], None]] = None
        self._mode = RuntimeMode.NORMAL
        self._mode_lock = threading.Lock()
        self._initialized = False
        self.registry.set_emit(self._registry_event)

    # -- events ------------------------------------------------------------

    def set_emit(self, emit: Callable[[str, Dict[str, Any]], None]) -> None:
        self._emit = emit

    def _fire(self, kind: str, payload: Dict[str, Any]) -> None:
        if self._emit:
            try:
                self._emit(kind, payload)
            except Exception:
                pass

    def _registry_event(self, kind: str, payload: Dict[str, Any]) -> None:
        # Registry state changes become UI events; agent_state maps 1:1.
        if kind == "agent_state":
            self._fire("agent_state", payload)
        elif kind == "agents_changed":
            self._fire("agents", {"counts": self.registry.state_counts(),
                                  "reason": payload.get("reason", "")})

    # -- mode --------------------------------------------------------------

    @property
    def mode(self) -> RuntimeMode:
        return self._mode

    @property
    def agentic_enabled(self) -> bool:
        """True for modes that route through the agent registry."""
        return self._mode in (RuntimeMode.AGENTIC, RuntimeMode.SARA_BOTS_AUTONOMOUS, RuntimeMode.ADAPTIVE)

    @property
    def uses_sara_bots(self) -> bool:
        """True for modes that use the Sara Bots framework."""
        return self._mode in (RuntimeMode.SARA_BOTS_AUTONOMOUS, RuntimeMode.ADAPTIVE)

    @property
    def uses_adaptive_fallback(self) -> bool:
        """True for ADAPTIVE mode with intelligent fallback."""
        return self._mode is RuntimeMode.ADAPTIVE

    def set_mode(self, mode: RuntimeMode | str) -> bool:
        """Switch between 4 modes. Returns True when it actually changed.

        Leaving agentic modes only stops NEW requests from being routed; running
        tasks are left alone (spec §3: no abrupt termination).
        """
        target = RuntimeMode(mode)
        with self._mode_lock:
            if target is self._mode:
                return False
            self._mode = target
        self._fire("mode", {"mode": target.value,
                            "initialized": self._initialized})
        if target in (RuntimeMode.AGENTIC, RuntimeMode.SARA_BOTS_AUTONOMOUS, RuntimeMode.ADAPTIVE):
            self.initialize()      # idempotent: first call only starts work
        if self.on_mode_change:
            try:
                self.on_mode_change(target.value)
            except Exception:
                pass
        return True

    # -- discovery / readiness --------------------------------------------

    def initialize(self, actions_dir: Any = None, plugins_dir: Any = None,
                   reserved: Any = None, action_registry: Any = None) -> None:
        """Discover agents and probe readiness on a worker thread. Idempotent."""
        if self._initialized:
            return
        self._initialized = True
        base = getattr(self.registry, "_base_dirs", None) or {}
        actions = actions_dir or base.get("actions")
        plugins = plugins_dir or base.get("plugins")

        def _worker() -> None:
            last_exc = None
            for attempt in range(4):
                try:
                    from pathlib import Path
                    app_base = Path(__file__).resolve().parent.parent
                    actions_dir = actions or (app_base / "actions")
                    plugins_dir = plugins or (app_base / "plugins")
                    self.registry.discover_all(Path(actions_dir),
                                               Path(plugins_dir),
                                               reserved or set(),
                                               action_registry=action_registry)
                    # The backend may still be warming up (its lifespan runs
                    # seeding + warmups); retry the fleet probe briefly.
                    import time as _t
                    fleet_ok = self.registry.fetch_fleet(timeout=6.0)
                    if not fleet_ok and attempt < 3:
                        _t.sleep(4.0 * (attempt + 1))
                        continue
                    counts = self.registry.refresh()
                    self._fire("agents", {"counts": counts,
                                          "reason": "initialized"})
                    return
                except Exception as exc:   # discovery must never kill the app
                    last_exc = exc
                    self._fire("agents",
                               {"counts": self.registry.state_counts(),
                                "reason": f"error: {exc}"})
                    return
            self._fire("agents",
                       {"counts": self.registry.state_counts(),
                        "reason": f"error: {last_exc}"})
        threading.Thread(target=_worker, daemon=True,
                         name="agentic-discovery").start()

    def refresh_agents(self) -> None:
        """Re-probe all agents on a worker thread (button / periodic)."""
        def _worker() -> None:
            counts = self.registry.refresh()
            self._fire("agents", {"counts": counts, "reason": "manual_refresh"})
        threading.Thread(target=_worker, daemon=True,
                         name="agentic-refresh").start()

    # -- task submission ---------------------------------------------------

    def submit(self, request: str, source: str = "voice",
               forced: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Plan one request. Returns the decision dict quickly (seconds).

        Decision kinds:
          {"kind": "execute", "task_id", "agents", "steps"} → worker started
          {"kind": "clarify", "question"}                    → ask the user
          {"kind": "duplicate", "task_id", "status"}         → dedupe hit
          {"kind": "error", "error"}                         → planning failed

        Only ever called when mode is an agentic mode (callers check), but this
        method double-guards so a race can never route a NORMAL request.
        """
        with self._mode_lock:
            if not self.agentic_enabled:
                return {"kind": "error",
                        "error": "Agentic Mode is off — request not routed."}
        text = (request or "").strip()
        if not text:
            return {"kind": "error", "error": "Empty request."}
        dup = self.tasks.is_duplicate(text)
        if dup is not None:
            return {"kind": "duplicate", "task_id": dup.task_id,
                    "status": dup.status.value}
        if self.router is None:
            return {"kind": "error", "error": "Task router not initialized."}

        task = self.tasks.create(text, source)
        task.status = TaskStatus.PLANNING
        task.add_event("planning", "Intent extraction and agent selection…")
        self._fire("task", {"phase": "planning", "task": task.to_dict()})

        try:
            if forced is not None and str(forced.get("kind") or "") == "execute":
                # ADAPTIVE Stage 1: an unambiguous local action was matched
                # without the LLM. Execution still goes through this exact
                # verified machinery (events, steps, cancel, persistence).
                decision = dict(forced)
                task.add_event("planning", "Direct local match.")
            else:
                decision = self.router.plan_task(text, task)
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = f"planning failed: {exc}"
            task.finished_at = time.time()
            task.add_event("error", task.error)
            self._fire("task", {"phase": "failed", "task": task.to_dict()})
            self._persist(task)
            return {"kind": "error", "error": task.error}

        task.decision = decision
        kind = decision.get("kind", "error")

        if kind == "clarify":
            task.status = TaskStatus.PENDING
            task.add_event("clarify", decision.get("question", ""))
            self._fire("task", {"phase": "clarify", "task": task.to_dict()})
            self._persist(task)
            return decision

        if kind != "execute":
            task.status = TaskStatus.FAILED
            task.error = str(decision.get("error", "no plan produced"))
            task.finished_at = time.time()
            self._fire("task", {"phase": "failed", "task": task.to_dict()})
            self._persist(task)
            return decision

        task.agents = list(decision.get("agents", []))
        task.steps = [{"agent": a, "state": "pending", "output": ""}
                      for a in task.agents]
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.add_event("started", f"Selected: {', '.join(task.agents)}")
        self._fire("task", {"phase": "started", "task": task.to_dict()})
        threading.Thread(target=self._run_task, args=(task,),
                         daemon=True, name=f"task-{task.task_id[:8]}").start()
        return {**decision, "kind": "execute", "task_id": task.task_id}

    # -- execution ---------------------------------------------------------

    def _run_task(self, task: AgentTask) -> None:
        """Worker thread: run the decision, honour cancellation, verify."""
        if self.router is None:
            task.status = TaskStatus.FAILED
            task.error = "router gone"
            self._persist(task)
            return

        # Consequential steps go through the human confirmation gate BEFORE
        # any external side effect (spec §11) — unless a step-level policy
        # already approved it (router returns approved=True).
        if self._confirm and self.router.requires_confirmation(task.decision):
            title = f"Agentic task: {', '.join(task.agents)}"
            detail = task.request[:400]
            task.status = TaskStatus.WAITING_CONFIRMATION
            task.add_event("confirm", "Waiting for on-screen confirmation…")
            self._fire("task", {"phase": "waiting_confirmation",
                                "task": task.to_dict()})

            def _after_confirm() -> str:
                task.status = TaskStatus.RUNNING
                task.add_event("confirmed", "User confirmed — executing.")
                self._fire("task", {"phase": "running", "task": task.to_dict()})
                return self._execute_and_finish(task)

            instruction = self._confirm(title, detail, _after_confirm)
            # _confirm returns immediately; instruction goes back to the model
            task.add_event("confirm_pending", instruction[:300])
            return   # worker ends here — execution continues from the gate

        self._execute_and_finish(task)

    def _execute_and_finish(self, task: AgentTask) -> str:
        try:
            result = self.router.execute_task(task, emit=self._task_event)
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            result = ""
            task.add_event("error", f"execution crashed: {exc}")
        if task.cancel.is_set() and task.status is not TaskStatus.FAILED:
            task.status = TaskStatus.CANCELLED
            task.cancel_confirmed = True
        task.finished_at = time.time()
        if task.status is not TaskStatus.CANCELLED:
            if result and not task.error:
                task.status = TaskStatus.COMPLETED
                task.result_text = result
            else:
                task.status = TaskStatus.FAILED
                task.error = task.error or "no result returned"
        self._fire("task", {"phase": task.status.value, "task": task.to_dict()})
        self._persist(task)
        # let a completed backend agent return to READY for the panel
        for agent_id in task.agents:
            if task.status is TaskStatus.COMPLETED:
                self.registry.restore_ready(agent_id)
        return task.result_text

    def _task_event(self, task: AgentTask, phase: str, text: str,
                    **extra: Any) -> None:
        task.add_event(phase, text, **extra)
        self._fire("task", {"phase": phase, "task_id": task.task_id,
                            "text": text[:300], **extra})

    def _persist(self, task: AgentTask) -> None:
        """Mirror this task into the durable TaskStore (best-effort)."""
        try:
            from core.task_store import get_task_store
            get_task_store().record(task.to_dict(include_events=False),
                                    mode=self._mode.value)
        except Exception:
            pass

    # -- status / cancellation --------------------------------------------

    def status_summary(self) -> Dict[str, Any]:
        active = self.tasks.active()
        return {
            "mode": self._mode.value,
            "initialized": self._initialized,
            "agent_counts": self.registry.state_counts(),
            "active_task": active.to_dict(include_events=False) if active else None,
            "recent": [t.to_dict(include_events=False)
                       for t in self.tasks.recent(5)],
        }

    def cancel(self, task_id: str = "") -> Dict[str, Any]:
        """Request cancellation and verify the underlying operation stopped.

        Sets the local stop flag AND asks the backend to stop its run when an
        external task id exists. Returns {"ok": bool, "status": ...}; the
        task is only marked cancelled when the backend confirms (or when the
        work is purely local and the executor observed the flag).
        """
        task = self.tasks.get(task_id) if task_id else self.tasks.active()
        if task is None:
            return {"ok": False, "error": "No active task to stop."}
        if task.status.terminal:
            return {"ok": False, "status": task.status.value,
                    "error": "Task already finished."}
        task.cancel.set()
        task.add_event("cancel", "Cancellation requested.")
        self._fire("task", {"phase": "cancelling", "task_id": task.task_id,
                            "text": "Cancellation requested"})
        if task.external_task_id:
            stopped = self.router.stop_external(task.external_task_id) \
                if self.router else False
            task.cancel_confirmed = bool(stopped)
            task.add_event("cancel", "backend stop "
                          + ("confirmed" if stopped else "FAILED"))
        else:
            # purely local: the executor checks task.cancel between steps; the
            # confirmation comes from execute_task returning for a cancelled run
            task.cancel_confirmed = True
        if task.cancel_confirmed and not task.status.terminal:
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.time()
            self._fire("task", {"phase": "cancelled", "task": task.to_dict()})
            self._persist(task)
        return {"ok": bool(task.cancel_confirmed),
                "status": task.status.value,
                "task_id": task.task_id}




