"""Per-run session state and the bounded in-process session store.

Mirrors the session model SARA's autonomous agent already uses
(``app/api/sara_auto_agent.py``): a background thread drives a run while the
SSE endpoint drains a queue, and a second queue carries approval decisions
back to the thread. Ultron uses the same shape so the proven Hub UI pattern
(events -> approve/stop) transfers unchanged.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .tenancy import LOCAL_TENANT_ID

#: Sessions older than this are dropped when a new one is created.
SESSION_TTL_S = 7200

#: Cap on retained events per session so a long run cannot grow unbounded.
MAX_EVENTS = 5000

#: Task lifecycle, in order. The UI renders exactly this sequence from real
#: phase events - a phase is only ever set by the code that reached it.
PHASES = (
    "received",     # accepted by POST /run, worker not started yet
    "understanding",  # memory recall + tool surface resolution
    "planning",     # planner building the step list
    "executing",    # executor running steps
    "verifying",    # building the report from real step results
    # Terminal phases. These mirror the run statuses exactly (rather than a
    # single generic "done") so a partially successful run can never be shown
    # as a clean completion.
    "completed",
    "completed_with_issues",
    "failed",
    "stopped",
)

#: Phases that mean the run is over.
TERMINAL_PHASES = frozenset({"completed", "completed_with_issues", "failed", "stopped"})


class UltronSession:
    """Mutable state for one Ultron run."""

    def __init__(self, goal: str, mode: str = "autonomous",
                 owner_id: str = LOCAL_TENANT_ID) -> None:
        self.task_id = str(uuid.uuid4())
        #: Which tenant this run belongs to. Every read of a session is filtered
        #: on this; a session id alone is not sufficient authority to read a
        #: run, because ids are handed to clients and appear in logs.
        self.owner_id = str(owner_id or LOCAL_TENANT_ID)
        self.goal = goal
        self.mode = mode
        self.status = "queued"
        self.created = time.time()
        #: Current lifecycle phase (see ``PHASES``). Drives the UI's real
        #: progress indicator, so it is only advanced by the orchestrator and
        #: executor themselves.
        self.phase = "received"
        #: Tool the executor is inside right now ("" between steps).
        self.current_tool = ""
        self.event_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=MAX_EVENTS)
        # (call_id, granted) tuples, produced by POST /approve.
        self.approve_q: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self.stop = threading.Event()
        self.trace: List[Dict[str, Any]] = []
        self.steps: List[Dict[str, Any]] = []
        self.plan: List[Dict[str, Any]] = []
        self.replans = 0
        #: Memory context recalled before the run, injected into the report prompt.
        self.memory_snapshot: str = ""

    def emit(self, kind: str, **kw: Any) -> None:
        try:
            self.event_q.put_nowait({"type": kind, **kw})
        except queue.Full:  # pragma: no cover - only under extreme load
            pass

    def set_phase(self, phase: str, **kw: Any) -> None:
        """Advance the lifecycle phase and tell the stream about it."""
        if phase not in PHASES:  # pragma: no cover - guard against typos
            raise ValueError(f"Unknown Ultron phase {phase!r}")
        self.phase = phase
        self.emit("phase", phase=phase, **kw)

    def log(self, message: str) -> None:
        self.trace.append({"t": time.time(), "kind": "log", "message": message})
        # Bound the trace: the UI only ever renders the tail.
        if len(self.trace) > 2000:
            del self.trace[:1000]
        self.emit("log", message=message)

    @property
    def active(self) -> bool:
        return not self.stop.is_set()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "owner_id": self.owner_id,
            "status": self.status,
            "goal": self.goal,
            "mode": self.mode,
            "created": self.created,
            "phase": self.phase,
            "current_tool": self.current_tool,
            "replans": self.replans,
            "plan": self.plan,
            "steps": self.steps,
            "trace": self.trace[-200:],
        }


class SessionStore:
    """Thread-safe session registry with TTL eviction."""

    def __init__(self) -> None:
        self._sessions: Dict[str, UltronSession] = {}
        self._lock = threading.Lock()

    def create(self, goal: str, mode: str = "autonomous",
               owner_id: str = LOCAL_TENANT_ID) -> UltronSession:
        session = UltronSession(goal, mode, owner_id=owner_id)
        with self._lock:
            self._sessions[session.task_id] = session
            cutoff = time.time() - SESSION_TTL_S
            for key in [k for k, v in self._sessions.items() if v.created < cutoff]:
                self._sessions.pop(key, None)
        return session

    def get(self, task_id: str, owner_id: Optional[str] = None) -> Optional[UltronSession]:
        """Fetch one session.

        When ``owner_id`` is given the session must belong to that tenant, and a
        mismatch reads as "not found" rather than "forbidden" - a 404 here does
        not confirm the task exists, so it leaks nothing.
        """
        with self._lock:
            session = self._sessions.get(task_id)
            if session is None:
                return None
            if owner_id is not None and session.owner_id != owner_id:
                return None
            return session

    def count(self, owner_id: Optional[str] = None) -> int:
        with self._lock:
            if owner_id is None:
                return len(self._sessions)
            return sum(1 for s in self._sessions.values()
                       if s.owner_id == owner_id)

    def snapshots(self, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Snapshots of retained sessions, newest first.

        Used by the Agent Control Center to show what Ultron is doing right
        now. Snapshots carry the goal and the trace tail, so this is only ever
        exposed on the authenticated surface - see ``api.py``.

        Pass ``owner_id`` to return only that tenant's runs. With no argument the
        behaviour is unchanged, which is what the process-wide local install and
        the existing tests rely on.
        """
        with self._lock:
            pool = [s for s in self._sessions.values()
                    if owner_id is None or s.owner_id == owner_id]
            sessions = sorted(pool, key=lambda s: s.created, reverse=True)
            return [s.snapshot() for s in sessions]

    def clear(self, owner_id: Optional[str] = None) -> int:
        """Drop sessions. Scoped when ``owner_id`` is given; global otherwise.

        Emergency stop uses the global form deliberately: an operator hitting
        STOP ALL means every run, including other tenants'.
        """
        with self._lock:
            if owner_id is None:
                count = len(self._sessions)
                self._sessions.clear()
                return count
            doomed = [k for k, s in self._sessions.items() if s.owner_id == owner_id]
            for key in doomed:
                self._sessions.pop(key, None)
            return len(doomed)


#: Process-wide store shared by the API routes and the executor thread.
store = SessionStore()
