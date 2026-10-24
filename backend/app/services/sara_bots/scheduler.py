"""Sara Bots schedules — persistent intervals that submit real tasks.

A schedule is just {bot, goal, interval}: every tick the scheduler looks for
enabled schedules whose ``next_run_at`` has passed and submits a NORMAL task
through the engine (same planning, same approvals, same audit trail). Dedupe:
while a task from the same schedule is still active, the tick is skipped — a
slow bot can never pile up overlapping runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .custom import (DATA_DIR, LOCAL_OWNER, CustomBotStore, get_custom_store,
                     new_id, normalize_owner)

log = logging.getLogger("sara.bots.scheduler")

SCHEDULES_FILE = DATA_DIR / "sara_bots_schedules.json"

MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 10080            # one week
TICK_S = 20.0
_MAX_SCHEDULES = 100


class ScheduleStore:
    """Thread-safe persistent schedule store (atomic JSON)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else SCHEDULES_FILE
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            data = __import__("json").loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
            data.setdefault("schedules", {})
            for schedule in data["schedules"].values():
                if isinstance(schedule, dict):
                    schedule["owner"] = normalize_owner(schedule.get("owner"))
            return data
        except FileNotFoundError:
            return {"schedules": {}}
        except Exception:
            log.exception("sara-bots: schedules.json unreadable — starting empty")
            return {"schedules": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(__import__("json").dumps(
            data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _clean_interval(value: Any) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise ValueError("interval_minutes must be an integer")
        if not MIN_INTERVAL_MIN <= minutes <= MAX_INTERVAL_MIN:
            raise ValueError(f"interval_minutes must be between "
                             f"{MIN_INTERVAL_MIN} and {MAX_INTERVAL_MIN}")
        return minutes

    def create(self, dot_id: str, goal: str, interval_minutes: Any,
               enabled: bool = True, owner: str = LOCAL_OWNER) -> Dict[str, Any]:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a goal is required for a schedule")
        if len(goal) > 2000:
            raise ValueError("schedule goal exceeds 2000 characters")
        minutes = self._clean_interval(interval_minutes)
        now = time.time()
        schedule = {
            "id": new_id("sch"),
            "dot_id": dot_id,
            "goal": goal,
            "interval_minutes": minutes,
            "enabled": bool(enabled),
            "owner": normalize_owner(owner),
            "created_at": now,
            "last_run_at": None,
            "next_run_at": now + minutes * 60,
            "runs": 0,
            "skipped": 0,
        }
        with self._lock:
            data = self._load()
            if len(data["schedules"]) >= _MAX_SCHEDULES:
                raise ValueError(f"schedule limit reached ({_MAX_SCHEDULES})")
            data["schedules"][schedule["id"]] = schedule
            self._save(data)
        return schedule

    def get(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._load()["schedules"].get(schedule_id)

    def list(self, dot_id: Optional[str] = None,
             owner: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            schedules = list(self._load()["schedules"].values())
        if owner is not None:
            schedules = [s for s in schedules if s.get("owner") == owner]
        if dot_id:
            schedules = [s for s in schedules if s.get("dot_id") == dot_id]
        schedules.sort(key=lambda s: s.get("created_at", 0))
        return schedules

    def update(self, schedule_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            schedule = data["schedules"].get(schedule_id)
            if schedule is None:
                raise KeyError(f"schedule '{schedule_id}' not found")
            if "interval_minutes" in fields:
                fields["interval_minutes"] = \
                    self._clean_interval(fields["interval_minutes"])
            if "goal" in fields:
                goal = str(fields["goal"]).strip()
                if not goal:
                    raise ValueError("goal cannot be empty")
                fields["goal"] = goal[:2000]
            schedule.update(fields)
            if "interval_minutes" in fields and \
                    not (schedule.get("next_run_at") or 0) > time.time():
                schedule["next_run_at"] = \
                    time.time() + schedule["interval_minutes"] * 60
            data["schedules"][schedule_id] = schedule
            self._save(data)
            return schedule

    def toggle(self, schedule_id: str) -> Dict[str, Any]:
        schedule = self.get(schedule_id)
        if schedule is None:
            raise KeyError(f"schedule '{schedule_id}' not found")
        enabled = not schedule.get("enabled", True)
        next_run = (time.time() + schedule["interval_minutes"] * 60
                    if enabled else schedule.get("next_run_at"))
        return self.update(schedule_id, enabled=enabled, next_run_at=next_run)

    def delete(self, schedule_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            schedule = data["schedules"].pop(schedule_id, None)
            if schedule is None:
                raise KeyError(f"schedule '{schedule_id}' not found")
            self._save(data)
        return schedule

    def delete_for_bot(self, dot_id: str) -> int:
        with self._lock:
            data = self._load()
            ids = [sid for sid, s in data["schedules"].items()
                   if s.get("dot_id") == dot_id]
            for sid in ids:
                data["schedules"].pop(sid, None)
            if ids:
                self._save(data)
        return len(ids)

    def due(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = now or time.time()
        with self._lock:
            schedules = list(self._load()["schedules"].values())
        return sorted(
            [s for s in schedules
             if s.get("enabled") and (s.get("next_run_at") or 0) <= now],
            key=lambda s: s.get("next_run_at") or 0)

    def mark_run(self, schedule_id: str, when: Optional[float] = None) -> None:
        when = when or time.time()
        self.update(schedule_id, last_run_at=when,
                    next_run_at=when,
                    runs=int((self.get(schedule_id) or {}).get("runs", 0)) + 1)
        # recompute next from the fresh interval
        schedule = self.get(schedule_id) or {}
        self.update(schedule_id,
                    next_run_at=when + schedule.get("interval_minutes", 60) * 60)

    def advance(self, schedule_id: str, when: Optional[float] = None) -> None:
        """Skip this tick without counting a run (paused bot / active task)."""
        when = when or time.time()
        schedule = self.get(schedule_id) or {}
        self.update(schedule_id,
                    next_run_at=when + schedule.get("interval_minutes", 60) * 60,
                    skipped=int(schedule.get("skipped", 0)) + 1)


class SaraBotsScheduler:
    """The background tick: due schedule -> real task via the engine."""

    def __init__(self, engine, store: Optional[ScheduleStore] = None,
                 custom: Optional[CustomBotStore] = None):
        self.engine = engine
        self.store = store or get_schedule_store()
        self.custom = custom or get_custom_store()
        self._running = False

    def tick(self) -> int:
        """Process due schedules once. Returns how many tasks were submitted.
        Public + synchronous so tests can drive it deterministically."""
        submitted = 0
        now = time.time()
        for schedule in self.store.due(now):
            bot = self.custom.get(schedule["dot_id"])
            if bot is None:
                # bot deleted -> schedule is dead weight; remove it
                try:
                    self.store.delete(schedule["id"])
                except KeyError:
                    pass
                continue
            if bot.get("status") != "active":
                self.store.advance(schedule["id"], now)
                continue
            try:
                active = self.engine.tasks.list(dot_id=bot["id"],
                                                active_only=True, limit=50)
            except Exception:
                active = []
            if any(t.get("schedule_id") == schedule["id"] for t in active):
                # a previous run from this schedule is still in flight
                self.store.advance(schedule["id"], now)
                continue
            try:
                self.engine.submit(bot, schedule["goal"], kind="schedule",
                                   schedule_id=schedule["id"])
                self.store.mark_run(schedule["id"], now)
                submitted += 1
            except ValueError as exc:
                # bot became non-active or goal invalid — advance, keep honest
                log.warning("sara-bots scheduler: %s skipped: %s",
                            schedule["id"], exc)
                self.store.advance(schedule["id"], now)
        return submitted

    async def run_forever(self) -> None:
        self._running = True
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sara-bots scheduler: tick failed")
            await asyncio.sleep(TICK_S)


_schedule_store: Optional[ScheduleStore] = None
_schedule_lock = threading.Lock()


def get_schedule_store() -> ScheduleStore:
    global _schedule_store
    with _schedule_lock:
        if _schedule_store is None:
            _schedule_store = ScheduleStore()
    return _schedule_store


_scheduler: Optional[SaraBotsScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler(engine=None) -> SaraBotsScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            if engine is None:
                from .engine import get_engine
                engine = get_engine()
            _scheduler = SaraBotsScheduler(engine)
    return _scheduler
