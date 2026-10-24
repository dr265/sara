"""
core/task_store.py — durable task history for SARA Desktop.

Every task the desktop really executed (Agentic Mode runs, ADAPTIVE
fallbacks, Sara Bots task runs) leaves a record here: status, agents,
per-step outputs, the verified result text, artifacts (audio/files/links
the work actually produced), the completion screenshot and the phone
notification delivery result.

The agentic runtime keeps its tasks in RAM (bounded, gone on restart); the
Result Viewer, the "show me the result" tool and the completion-report
pipeline answer "what did the last task produce, and where is it?" from
this store instead. A record exists only for work that really ran, really
failed, or is really waiting for a human — nothing is ever fabricated.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DATA_DIR = Path(__file__).resolve().parents[1] / "memory"
HISTORY_FILE = DATA_DIR / "task_history.json"

MAX_RECORDS = 200          # oldest records are dropped first
MAX_EVENTS_KEPT = 30       # per record
MAX_ARTIFACTS = 20         # per record

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _tokens(text: Any) -> set:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


def _artifact_key(artifact: Dict[str, Any]) -> tuple:
    return (str(artifact.get("kind") or ""),
            str(artifact.get("url") or artifact.get("path") or ""))


class TaskStore:
    """Thread-safe, capped task history with atomic JSON persistence."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else HISTORY_FILE
        self._lock = threading.RLock()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []          # oldest → newest
        self._load()

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            records = raw.get("records") if isinstance(raw, dict) else None
            if not isinstance(records, list):
                return
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                tid = str(rec.get("task_id") or "")
                if not tid or tid in self._records:
                    continue
                self._records[tid] = rec
                self._order.append(tid)
        except Exception:
            # A corrupt/missing history must never stop the desktop.
            self._records = {}
            self._order = []

    def _persist_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1,
                       "records": [self._records[t] for t in self._order]}
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False,
                                      default=str), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            pass   # history is best-effort; never break a running task

    def _trim_locked(self) -> None:
        while len(self._order) > MAX_RECORDS:
            oldest = self._order.pop(0)
            self._records.pop(oldest, None)

    # ── writes ───────────────────────────────────────────────────────────

    def record(self, task: Dict[str, Any], mode: str = "",
               source: str = "") -> Dict[str, Any]:
        """Upsert one task record (called at every terminal/waiting point)."""
        tid = str((task or {}).get("task_id") or "")
        if not tid:
            return {}
        with self._lock:
            existing = self._records.get(tid) or {}
            rec: Dict[str, Any] = {
                "task_id": tid,
                "request": str(task.get("request")
                               or existing.get("request") or ""),
                "status": str(task.get("status")
                              or existing.get("status") or ""),
                "agents": list(task.get("agents")
                               or existing.get("agents") or []),
                "steps": list(task.get("steps")
                              or existing.get("steps") or []),
                "result": str(task.get("result") or task.get("result_text")
                              or existing.get("result") or ""),
                "error": str(task.get("error") or existing.get("error") or ""),
                "created_at": task.get("created_at")
                              or existing.get("created_at"),
                "started_at": task.get("started_at")
                              or existing.get("started_at"),
                "finished_at": task.get("finished_at")
                               or existing.get("finished_at"),
                "elapsed_s": task.get("elapsed_s")
                             or existing.get("elapsed_s"),
                "external_task_id": str(task.get("external_task_id")
                                        or existing.get("external_task_id")
                                        or ""),
                "source": str(task.get("source") or source
                              or existing.get("source") or ""),
                "mode": str(mode or existing.get("mode") or ""),
                "artifacts": list(existing.get("artifacts") or []),
                "screenshot": str(existing.get("screenshot") or ""),
                "report": str(existing.get("report") or ""),
                "notify": list(existing.get("notify") or []),
                "saved_at": time.time(),
            }
            # carry artifacts the runtime attached to the live task
            for art in (task.get("artifacts") or []):
                if isinstance(art, dict):
                    self._attach_locked(rec, art)
            events = task.get("events")
            if isinstance(events, list) and events:
                rec["events"] = list(events[-MAX_EVENTS_KEPT:])
            elif existing.get("events"):
                rec["events"] = existing["events"]
            if tid not in self._records:
                self._order.append(tid)
            self._records[tid] = rec
            self._trim_locked()
            self._persist_locked()
            return dict(rec)

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        tid = str(task_id or "")
        with self._lock:
            rec = self._records.get(tid)
            if rec is None:
                return None
            rec.update(fields)
            self._persist_locked()
            return dict(rec)

    def _attach_locked(self, rec: Dict[str, Any],
                       artifact: Dict[str, Any]) -> None:
        arts = rec.setdefault("artifacts", [])
        key = _artifact_key(artifact)
        if not key[1]:
            return
        if any(_artifact_key(a) == key for a in arts):
            return
        arts.append({k: v for k, v in artifact.items() if v not in (None, "")})
        if len(arts) > MAX_ARTIFACTS:
            del arts[: len(arts) - MAX_ARTIFACTS]

    def attach_artifact(self, task_id: str,
                        artifact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        tid = str(task_id or "")
        with self._lock:
            rec = self._records.get(tid)
            if rec is None or not isinstance(artifact, dict):
                return None
            self._attach_locked(rec, artifact)
            self._persist_locked()
            return dict(rec)

    def set_screenshot(self, task_id: str, path: str) -> None:
        self.update(task_id, screenshot=str(path or ""))

    def set_report(self, task_id: str, report: str) -> None:
        self.update(task_id, report=str(report or "")[:2000])

    def mark_notify(self, task_id: str, channel: str, ok: bool,
                    detail: str = "") -> None:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            if rec is None:
                return
            entries = rec.setdefault("notify", [])
            entries.append({"channel": str(channel), "ok": bool(ok),
                            "detail": str(detail)[:240], "t": time.time()})
            del entries[:-8]
            self._persist_locked()

    # ── reads ────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._records.get(str(task_id or ""))
            return dict(rec) if rec else None

    def latest(self, n: int = 10,
               statuses: Optional[Iterable[str]] = None
               ) -> List[Dict[str, Any]]:
        """Newest first; optionally filtered to a set of statuses."""
        want = set(statuses) if statuses else None
        with self._lock:
            out: List[Dict[str, Any]] = []
            for tid in reversed(self._order):
                rec = self._records.get(tid)
                if rec is None:
                    continue
                if want and str(rec.get("status") or "") not in want:
                    continue
                out.append(dict(rec))
                if len(out) >= max(1, int(n)):
                    break
            return out

    def find(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Keyword search over request/result/agents/artifact titles."""
        words = _tokens(query)
        if not words:
            return []
        with self._lock:
            scored: List[tuple] = []
            for tid in reversed(self._order):
                rec = self._records.get(tid)
                if rec is None:
                    continue
                score = 3 * len(words & _tokens(rec.get("request")))
                score += len(words & _tokens(rec.get("result")))
                score += len(words & _tokens(rec.get("error")))
                score += 2 * len(words & _tokens(
                    " ".join(str(a) for a in (rec.get("agents") or []))))
                for art in rec.get("artifacts") or []:
                    score += len(words & _tokens(art.get("title")))
                if score > 0:
                    scored.append((score, rec))
            # stable sort → equal scores keep newest-first order
            scored.sort(key=lambda t: t[0], reverse=True)
            return [dict(r) for _, r in scored[: max(1, int(limit))]]

    def clear(self) -> None:
        """Drop every record (used by tests)."""
        with self._lock:
            self._records = {}
            self._order = []
            self._persist_locked()


_STORE_LOCK = threading.Lock()
_STORE: Optional[TaskStore] = None


def get_task_store() -> TaskStore:
    """Process-wide store singleton.

    ``SARA_TASK_STORE_PATH`` redirects the history file — used by the test
    suite so runs never mix synthetic records into the real task history.
    """
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            override = os.environ.get("SARA_TASK_STORE_PATH", "").strip()
            _STORE = (TaskStore(path=Path(override)) if override
                      else TaskStore())
        return _STORE
