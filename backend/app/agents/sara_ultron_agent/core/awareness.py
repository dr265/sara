"""The Context Awareness Engine: what the user is looking at, right now.

This is the module the whole "persistent assistant" idea rests on. Its job is
narrow and specific:

* hold the **current focus** - the window the user is actually in,
* hold the **previous context** - what they were in a moment ago,
* hold the **active task** and the **last action**,
* notice when any of that changed, and
* never confuse old context with current context.

Three rules it follows, each of which exists because breaking it produces a
system that is confidently wrong:

**A context is a snapshot with a revision, not a mutable pile of fields.**
Every accepted update bumps a revision and is appended to a bounded timeline.
That is what lets the UI draw "current vs previous" correctly and what makes
"the previous window" a lookup rather than a guess.

**An update that changes nothing does not bump the revision.** The observer
polls every second; if a user just moved the mouse, the window is identical and
the context must stay identical. Without this, every poll would look like a
change and downstream consumers (vision, the always-on loop) would re-analyse
forever - which is the single easiest way to burn a machine's CPU.

**Nothing here watches anything by itself.** The observer that feeds it checks
the ``screen`` permission before every poll, and reports
``permission_required`` rather than quietly returning stale data. A context
engine that keeps its last known state after the user revoked screen access is
exactly the "secretly watching" failure this project is supposed to avoid.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .adapters import (
    CAT_UNKNOWN,
    AdapterContext,
    AdapterRegistry,
    RawWindow,
    registry as default_registry,
)
from .permissions import permissions

log = logging.getLogger("sara.ultron.awareness")

#: How many context snapshots to keep for "the previous one" and for the HUD
#: timeline. Bounded, because this is an awareness buffer, not a log file.
TIMELINE_LIMIT = 50

#: Fields that make up the identity of a context. A change in any of them is a
#: real change the user would notice; anything else (a latency figure, a
#: confidence score that wobbles) is not.
IDENTITY_FIELDS = ("category", "app_name", "item", "url", "site", "project", "folder")


@dataclass
class ContextSnapshot:
    """One accepted observation of what the user is looking at."""

    revision: int = 0
    t: float = field(default_factory=time.time)
    context: AdapterContext = field(default_factory=AdapterContext)
    adapter: str = "none"
    raw_title: str = ""
    raw_process: str = ""
    pid: int = 0
    hwnd: int = 0
    #: Set by the caller - a task set by a run, or a manual note.
    task: str = ""
    last_action: str = ""
    last_action_t: float = 0.0
    last_action_ok: Optional[bool] = None
    #: Where this observation came from. Never silently merged.
    source: str = "observer"

    def identity(self) -> str:
        """The tuple that decides whether anything actually changed."""
        parts = [str(getattr(self.context, f) or "") for f in IDENTITY_FIELDS]
        return "|".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "t": self.t,
            "age_s": round(max(0.0, time.time() - self.t), 1),
            "context": self.context.as_dict(),
            "adapter": self.adapter,
            "raw_title": self.raw_title,
            "raw_process": self.raw_process,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "task": self.task,
            "last_action": self.last_action,
            "last_action_t": self.last_action_t,
            "last_action_ok": self.last_action_ok,
            "source": self.source,
        }


@dataclass
class AwarenessState:
    """Everything the HUD and the voice loop read, in one place."""

    current: Optional[ContextSnapshot] = None
    previous: Optional[ContextSnapshot] = None
    last_change_t: float = 0.0
    changes: int = 0
    #: Set when the observer could not run - a permission problem, a missing
    #: runtime, an exception. Never replaced with a stale "fine".
    blocked: Optional[str] = None
    vision: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "current": self.current.as_dict() if self.current else None,
            "previous": self.previous.as_dict() if self.previous else None,
            "last_change_t": self.last_change_t,
            "last_change_age_s": (
                round(time.time() - self.last_change_t, 1) if self.last_change_t else None
            ),
            "changes": self.changes,
            "blocked": self.blocked,
            "vision": self.vision,
        }


class ContextManager:
    """Holds the current context, the previous one, and the task thread."""

    def __init__(self, adapters: Optional[AdapterRegistry] = None) -> None:
        self._adapters = adapters or default_registry
        self._state = AwarenessState()
        self._timeline: Deque[ContextSnapshot] = deque(maxlen=TIMELINE_LIMIT)
        # A task issued before any window has been observed. Holding it beats
        # dropping it, and it is attached to the first real context.
        self._pending_task = ""
        self._lock = threading.RLock()

    # -- reads ----------------------------------------------------------
    def state(self) -> AwarenessState:
        """The whole :class:`AwarenessState`, including ``blocked``.

        A method rather than a property so it matches ``current()`` and
        ``previous()`` - the three read the same state at three levels of
        detail, and mixing property and method across them is a trap for the
        next person.
        """
        with self._lock:
            return self._state

    def current(self) -> Optional[ContextSnapshot]:
        with self._lock:
            return self._state.current

    def previous(self) -> Optional[ContextSnapshot]:
        with self._lock:
            return self._state.previous

    def timeline(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._timeline)[-limit:]
        return [s.as_dict() for s in reversed(items)]

    def snapshot(self) -> Dict[str, Any]:
        """The one payload the HUD, the voice loop and the vision tier read."""
        with self._lock:
            payload = self._state.as_dict()
            payload["adapters"] = self._adapters.names()
        if payload["blocked"] is None and not permissions.allowed("screen"):
            # The last observed window is still worth showing - throwing it away
            # would be its own kind of lie - but it is frozen, and a panel that
            # renders it without saying so is showing the user a window they
            # left five minutes ago as if it were live. Say it here rather than
            # trusting every consumer to remember to check the gate itself.
            payload["blocked"] = (
                "screen permission is off, so this context is the last one seen "
                "and is not updating"
            )
        payload["live"] = payload["blocked"] is None
        return payload

    # -- the observation path -------------------------------------------
    def observe(self, window: RawWindow, source: str = "observer") -> Dict[str, Any]:
        """Interpret one window and adopt it if it is genuinely different.

        Returns ``{"changed": bool, "reason": str, ...}``. Callers use
        ``changed`` to decide whether to spend money on vision, so getting this
        wrong in the "always changed" direction is expensive and getting it
        wrong in the other direction is merely late.
        """
        try:
            context, adapter = self._adapters.interpret(window)
        except Exception as exc:  # noqa: BLE001
            log.warning("Context interpretation failed: %s", exc)
            return {"changed": False, "reason": f"interpretation failed: {exc}"}

        with self._lock:
            current = self._state.current
            if current is not None:
                candidate = ContextSnapshot(
                    revision=current.revision,
                    t=time.time(),
                    context=context,
                    adapter=adapter,
                    raw_title=window.title,
                    raw_process=window.process,
                    pid=window.pid,
                    hwnd=window.hwnd,
                    task=current.task,
                    last_action=current.last_action,
                    last_action_t=current.last_action_t,
                    last_action_ok=current.last_action_ok,
                    source=source,
                )
                if candidate.identity() == current.identity():
                    # Same window, same tab, same file. Refresh the timestamp so
                    # "how stale is my context" stays honest, but do not churn.
                    current.t = candidate.t
                    current.raw_title = window.title
                    return {"changed": False, "reason": "identical context",
                            "revision": current.revision}

            self._adopt(window, context, adapter, source)
            snapshot = self._state.current
            return {
                "changed": True,
                "reason": "focus changed",
                "revision": snapshot.revision if snapshot else 0,
                "context": context.as_dict(),
            }

    def _adopt(self, window: RawWindow, context: AdapterContext,
               adapter: str, source: str) -> None:
        with self._lock:
            previous = self._state.current
            revision = (previous.revision + 1) if previous else 1
            snapshot = ContextSnapshot(
                revision=revision,
                t=time.time(),
                context=context,
                adapter=adapter,
                raw_title=window.title,
                raw_process=window.process,
                pid=window.pid,
                hwnd=window.hwnd,
                source=source,
            )
            # The task and last action follow the *user*, not the window: if
            # they alt-tab mid-task the task is still the task.
            if previous is not None:
                snapshot.task = previous.task
                snapshot.last_action = previous.last_action
                snapshot.last_action_t = previous.last_action_t
                snapshot.last_action_ok = previous.last_action_ok
                self._state.previous = previous
            self._state.current = snapshot
            self._state.last_change_t = snapshot.t
            self._state.changes += 1
            self._state.blocked = None
            if self._pending_task:
                snapshot.task = self._pending_task
                self._pending_task = ""
            self._timeline.append(snapshot)
            log.info("Context -> %s / %s (rev %d, %s)",
                     context.app_name, context.item or "-", revision, adapter)

    # -- the task thread -------------------------------------------------
    def set_task(self, task: str) -> Dict[str, Any]:
        """Set the active task. Passing "" clears it."""
        with self._lock:
            if self._state.current is None:
                # Record it anyway - a task can be issued before any window is
                # observed, and dropping it would be worse than holding it.
                self._state.blocked = None
            snapshot = self._state.current
            if snapshot is None:
                self._pending_task = task
                return {"ok": True, "task": task, "pending": True}
            snapshot.task = task
            return {"ok": True, "task": task, "revision": snapshot.revision}

    def record_action(self, action: str, ok: Optional[bool] = None) -> Dict[str, Any]:
        """Record what was just done. This is what "LAST ACTION" renders.

        ``ok=None`` means "attempted, outcome not yet verified". The UI renders
        those three states differently on purpose: an unverified action must
        never look like a success.
        """
        with self._lock:
            snapshot = self._state.current
            if snapshot is None:
                return {"ok": False, "reason": "no context to attach the action to"}
            snapshot.last_action = action
            snapshot.last_action_t = time.time()
            snapshot.last_action_ok = ok
            return {"ok": True, "action": action, "verified": ok,
                    "revision": snapshot.revision}

    def clear_task(self) -> Dict[str, Any]:
        return self.set_task("")

    # -- blocking ---------------------------------------------------------
    def mark_blocked(self, reason: str) -> None:
        """Record that awareness cannot run, and why.

        Deliberately does *not* clear ``current``: the last known context is
        still the best thing the HUD can show, but it is returned alongside
        ``blocked`` and ``age_s`` so the UI can grey it out rather than present
        stale data as live.
        """
        with self._lock:
            self._state.blocked = reason

    def set_vision(self, payload: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._state.vision = payload

    # -- history navigation ----------------------------------------------
    def find(self, revision: int) -> Optional[ContextSnapshot]:
        with self._lock:
            for snapshot in self._timeline:
                if snapshot.revision == revision:
                    return snapshot
            return None

    def switch_back(self) -> Optional[ContextSnapshot]:
        """Make the previous context current again - "go back to that window".

        Implemented as a new observation of the previous window, not by rewinding
        the revision counter: history is append-only, so a "back" navigation is
        itself an event the timeline records.
        """
        with self._lock:
            previous = self._state.previous
        if previous is None:
            return None
        window = RawWindow(title=previous.raw_title, process=previous.raw_process,
                           pid=previous.pid, hwnd=previous.hwnd,
                           url=previous.context.url)
        self.observe(window, source="switch_back")
        return self.current()


#: Process-wide context manager, shared by the observer, the voice loop, the
#: vision tier, the API and the HUD.
context = ContextManager()

#: Same object under the name the rest of ``core`` imports it by. Two names
#: for one object is a hazard only if they can drift apart, so this is an alias
#: and never a second instance - ``context_manager is context`` is asserted in
#: the test suite.
context_manager = context
