"""Sara Bots task store — durable task + approval records for custom bots.

One JSON file holds every task and approval request; writes are atomic
(tmp + os.replace) so a crash can never leave a half-written task. Terminal
tasks are capped (oldest dropped first) so the file cannot grow unbounded.

Task lifecycle (every state derives from persisted fields set by the engine
from real runtime facts — never invented):
  queued -> planning -> running -> (waiting_approval | waiting_input)
                                         |            |
                                         v            v
                              completed | failed | cancelled | blocked

``waiting_input`` = the bot asked the owner a question in the chat
(agent.ask_owner) and the task is parked until they reply.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("sara.bots.tasks")

from .custom import DATA_DIR, LOCAL_OWNER, new_id, normalize_owner

TASKS_FILE = DATA_DIR / "sara_bots_tasks.json"

TASK_STATES = ("queued", "planning", "running", "waiting_approval",
               "waiting_input", "completed", "failed", "cancelled",
               "blocked", "paused")
TERMINAL_STATES = ("completed", "failed", "cancelled")
ACTIVE_STATES = ("queued", "planning", "running", "waiting_approval",
                 "waiting_input", "paused")
STEP_STATES = ("pending", "running", "waiting_approval", "waiting_input",
               "completed", "failed", "skipped", "cancelled", "unknown")
APPROVAL_STATES = ("pending", "approved", "denied")

_MAX_TASKS = 500
_MAX_EVENTS = 200
MAX_STEP_OUTPUT = 8000
MAX_RESULT_TEXT = 20000


class TaskStore:
    """Thread-safe durable task + approval store."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else TASKS_FILE
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
            data.setdefault("tasks", {})
            data.setdefault("approvals", {})
            for task in data["tasks"].values():
                if isinstance(task, dict):
                    task["owner"] = normalize_owner(task.get("owner"))
            for approval in data["approvals"].values():
                if isinstance(approval, dict):
                    approval["owner"] = normalize_owner(approval.get("owner"))
            return data
        except FileNotFoundError:
            return {"tasks": {}, "approvals": {}}
        except Exception:
            log.exception("sara-bots: tasks.json unreadable — starting empty")
            return {"tasks": {}, "approvals": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        tasks: Dict[str, Any] = data["tasks"]
        if len(tasks) > _MAX_TASKS:
            ordered = sorted(tasks.values(), key=lambda t: t.get("created_at", 0))
            for stale in ordered:
                if len(tasks) <= _MAX_TASKS:
                    break
                if stale.get("status") in TERMINAL_STATES:
                    tasks.pop(stale["id"], None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- tasks ---------------------------------------------------------------
    def create(self, dot_id: str, goal: str, kind: str = "user",
               schedule_id: Optional[str] = None,
               plan: Optional[List[Dict[str, Any]]] = None,
               idempotency_key: Optional[str] = None,
               owner: str = LOCAL_OWNER) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            data = self._load()
            if idempotency_key:
                # AgentFactory-style idempotency: a retried submit with the
                # same key returns the SAME task instead of running twice.
                for existing in data["tasks"].values():
                    if (existing.get("dot_id") == dot_id
                            and existing.get("idempotency_key") == idempotency_key):
                        return {**existing, "deduplicated": True}
            task = {
                "id": new_id("task"),
                "dot_id": dot_id,
                "goal": goal,
                "kind": kind,                      # user | schedule | test
                "schedule_id": schedule_id,
                "idempotency_key": idempotency_key,
                "status": "queued",
                "plan": plan or [],
                "current_step": 0,
                "events": [{"t": now, "event": "queued"}],
                "outputs": {},
                "result_text": "",
                "error": "",
                "cancel_requested": False,
                "owner": normalize_owner(owner),
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            data["tasks"][task["id"]] = task
            self._save(data)
        return task

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._load()["tasks"].get(task_id)

    def list(self, dot_id: Optional[str] = None,
             status: Optional[str] = None,
             kind: Optional[str] = None, limit: int = 100,
             active_only: bool = False,
             owner: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            tasks = list(self._load()["tasks"].values())
        if owner is not None:
            tasks = [t for t in tasks if t.get("owner") == owner]
        if dot_id:
            tasks = [t for t in tasks if t.get("dot_id") == dot_id]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        if kind:
            tasks = [t for t in tasks if t.get("kind") == kind]
        if active_only:
            tasks = [t for t in tasks if t.get("status") in ACTIVE_STATES]
        tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
        return tasks[:max(1, min(limit, 200))]

    def next_queued(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            queued = [t for t in self._load()["tasks"].values()
                      if t.get("status") == "queued"]
        queued.sort(key=lambda t: t.get("created_at", 0))
        return queued[0] if queued else None

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            task.update(fields)
            data["tasks"][task_id] = task
            self._save(data)
            return task

    def set_status(self, task_id: str, status: str,
                   event: Optional[str] = None,
                   **fields: Any) -> Optional[Dict[str, Any]]:
        if status not in TASK_STATES:
            raise ValueError(f"invalid task state '{status}'")
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            task.update(fields)
            task["status"] = status
            if status in TERMINAL_STATES and not task.get("finished_at"):
                task["finished_at"] = time.time()
            self._append_event_locked(task, event or status)
            data["tasks"][task_id] = task
            self._save(data)
            return task

    def append_event(self, task_id: str, event: str,
                     detail: str = "") -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            self._append_event_locked(task, event, detail)
            data["tasks"][task_id] = task
            self._save(data)
            return task

    @staticmethod
    def _append_event_locked(task: Dict[str, Any], event: str,
                             detail: str = "") -> None:
        events = task.setdefault("events", [])
        entry: Dict[str, Any] = {"t": time.time(), "event": event}
        if detail:
            entry["detail"] = str(detail)[:400]
        events.append(entry)
        task["events"] = events[-_MAX_EVENTS:]

    # -- step helpers --------------------------------------------------------
    def set_step(self, task_id: str, step_index: int,
                 **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            plan = task.get("plan", [])
            if not 0 <= step_index < len(plan):
                return None
            plan[step_index].update(fields)
            data["tasks"][task_id] = task
            self._save(data)
            return task

    def record_output(self, task_id: str, step_index: int,
                      text: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            outputs = task.setdefault("outputs", {})
            outputs[str(step_index)] = str(text)[:MAX_STEP_OUTPUT]
            data["tasks"][task_id] = task
            self._save(data)
            return task

    def reply(self, task_id: str, text: str) -> Optional[Dict[str, Any]]:
        """Owner's chat reply to a ``waiting_input`` task.

        Stores the answer on the parked step and re-queues the task — the
        engine turns the answer into that step's output and continues. A
        reply for a task that is not waiting is refused, never guessed."""
        text = (text or "").strip()
        if not text:
            raise ValueError("a reply text is required")
        if len(text) > 2000:
            raise ValueError("reply exceeds 2000 characters")
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id)
            if task is None:
                return None
            if task.get("status") != "waiting_input":
                raise ValueError(f"task is {task.get('status')} — it is not "
                                 "waiting for a reply")
            plan = task.get("plan", [])
            idx = int(task.get("current_step", 0))
            if not 0 <= idx < len(plan):
                raise ValueError("no waiting step found in this task")
            step = plan[idx]
            if step.get("status") != "waiting_input":
                raise ValueError("the current step is not waiting for a reply")
            step["answer"] = text[:2000]
            step["answered_at"] = time.time()
            task["status"] = "queued"
            self._append_event_locked(task, "owner_replied",
                                      f"[{idx}] reply received")
            data["tasks"][task_id] = task
            self._save(data)
            return task

    # -- approvals -----------------------------------------------------------
    def create_approval(self, task_id: str, dot_id: str, step_index: int,
                        tool: str, reason: str, params_preview: str,
                        ttl_seconds: int = 3600) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            data = self._load()
            task = data["tasks"].get(task_id) or {}
            approval = {
                "id": new_id("apr"),
                "task_id": task_id,
                "dot_id": dot_id,
                "step_index": step_index,
                "tool": tool,
                "reason": reason,
                "params_preview": params_preview[:500],
                "status": "pending",
                "owner": normalize_owner(task.get("owner")),
                "created_at": now,
                "decided_at": None,
                "expires_at": now + max(60, int(ttl_seconds)),
            }
            data["approvals"][approval["id"]] = approval
            self._save(data)
        return approval

    def expire_stale(self, now: Optional[float] = None) -> int:
        """Sweep pending approvals past their TTL (AgentFactory pattern).

        An expired approval parks its task in ``blocked`` — it never silently
        disappears and never re-requests approval on its own; the owner resumes
        the task to get a fresh request with a fresh TTL.
        """
        now = now or time.time()
        with self._lock:
            data = self._load()
            changed = 0
            for approval in data["approvals"].values():
                if approval.get("status") != "pending":
                    continue
                if (approval.get("expires_at") or 0) > now:
                    continue
                approval["status"] = "expired"
                approval["decided_at"] = now
                changed += 1
                task = data["tasks"].get(approval["task_id"])
                if task is None or task.get("status") != "waiting_approval":
                    continue
                plan = task.get("plan", [])
                idx = approval["step_index"]
                if 0 <= idx < len(plan) and \
                        plan[idx].get("approval_id") == approval["id"]:
                    plan[idx]["error"] = "approval request expired"
                    self._append_event_locked(
                        task, "approval_expired",
                        f"step {idx}: approval expired after its TTL")
                    task["status"] = "blocked"
                    task["error"] = ("the approval request expired — review the "
                                     "step and resume the task to request a "
                                     "fresh decision")
            if changed:
                self._save(data)
            return changed

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._load()["approvals"].get(approval_id)

    def list_approvals(self, status: Optional[str] = None,
                       dot_id: Optional[str] = None,
                       owner: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            approvals = list(self._load()["approvals"].values())
        if owner is not None:
            approvals = [a for a in approvals if a.get("owner") == owner]
        if status:
            approvals = [a for a in approvals if a.get("status") == status]
        if dot_id:
            approvals = [a for a in approvals if a.get("dot_id") == dot_id]
        approvals.sort(key=lambda a: a.get("created_at", 0), reverse=True)
        return approvals[:200]

    def decide_approval(self, approval_id: str,
                        decision: str) -> Optional[Dict[str, Any]]:
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be 'approve' or 'deny'")
        with self._lock:
            data = self._load()
            approval = data["approvals"].get(approval_id)
            if approval is None:
                return None
            if approval["status"] == "expired":
                raise ValueError("approval expired — review the step and "
                                 "resume the task to request a fresh decision")
            if approval["status"] != "pending":
                return approval  # already decided — idempotent
            if (approval.get("expires_at") or 0) <= time.time():
                # an expired request is final: it cannot be approved late
                approval["status"] = "expired"
                approval["decided_at"] = time.time()
                data["approvals"][approval_id] = approval
                self._save(data)
                raise ValueError("approval expired — review the step and "
                                 "resume the task to request a fresh decision")
            approval["status"] = "approved" if decision == "approve" else "denied"
            approval["decided_at"] = time.time()
            data["approvals"][approval_id] = approval
            task = data["tasks"].get(approval["task_id"])
            if task is not None and task.get("status") in (
                    "waiting_approval", "blocked", "paused"):
                plan = task.get("plan", [])
                idx = approval["step_index"]
                if 0 <= idx < len(plan):
                    if approval["status"] == "approved":
                        # the owner approved THIS payload on THIS step; the
                        # execution is replayed with confirm=True and nothing
                        # else about the payload changes
                        plan[idx]["force_confirm"] = True
                        plan[idx]["approval_id"] = approval_id
                        plan[idx]["status"] = "pending"
                    else:
                        plan[idx]["status"] = "skipped"
                        plan[idx]["error"] = "denied by owner"
                task["status"] = "queued"   # engine resumes (or concludes)
                data["tasks"][task["id"]] = task
            self._save(data)
            return approval


_store: Optional[TaskStore] = None
_store_lock = threading.Lock()


def get_task_store() -> TaskStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = TaskStore()
    return _store
