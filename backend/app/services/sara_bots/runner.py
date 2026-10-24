"""Sara Bots runner — real execution, bounded and audited.

Two execution paths, both genuine:

``local_action``  the approved action module is run by the headless worker
                  (worker.py) in a SEPARATE process: fixed interpreter, argv
                  array (never a shell string), scrubbed environment, hard
                  timeout with Windows process-tree kill, capped output.

``hub adapters`` an HTTP call to the agent's REAL route on this backend
                  (the same pattern the Ultron fleet probes use), so the
                  request exercises the actual agent code, its auth and its
                  validation. Only agents whose routes accept a simple JSON /
                  form contract have adapters; the rest deep-link to their
                  workspaces.

Every run produces an immutable record (id, bot, redacted params, real
status, output/error, timings, process facts) kept in memory (capped) and
appended to a JSONL audit log. No status is invented: ``running`` means a
process/task exists, ``completed`` means the handler/agent really returned.

Risk gating: a local run whose parameters name a destructive action (kill,
shutdown, delete, send, ...) or carry command/pid-style keys requires an
explicit ``confirm=true`` on the very same payload; anything else fails with
409 and a description of what would run. Gating is deterministic backend
logic — the UI cannot bypass it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.agents.sara_ultron_agent.core.fleet import redact

from .registry import (RISKY_ACTION_VALUES, RISKY_PARAM_KEYS, REPO_ROOT,
                       SaraBotsRegistry, get_registry)

log = logging.getLogger("sara.bots.runner")

WORKER_PATH = Path(__file__).resolve().parent / "worker.py"
AUDIT_FILE = REPO_ROOT / "backend" / "logs" / "sara_bots_audit.jsonl"
AGENCY_INVENTORY = REPO_ROOT / "backend" / "agency_inventory.json"

DESKTOP_PYTHON = REPO_ROOT / "sara_desktop" / "venv" / "Scripts" / "python.exe"

DEFAULT_TIMEOUT_S = 45.0
MAX_TIMEOUT_S = 180.0
MAX_OUTPUT_CHARS = 64_000
MAX_RUNS_KEPT = 400
MAX_CONCURRENT_LOCAL = 4
RATE_LIMIT_PER_MIN = 20

#: Environment allowlist for the worker process. Nothing else is inherited —
#: in particular no API keys, no DATABASE_URL, no ambient PATH.
_WORKER_ENV_KEYS = ("SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PUBLIC")


class RateLimited(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _LocalSemaphore:
    def __init__(self, slots: int):
        self._sem = threading.Semaphore(slots)

    def __enter__(self):
        if not self._sem.acquire(timeout=0.5):
            raise RateLimited(
                f"too many concurrent local runs (max {MAX_CONCURRENT_LOCAL}); retry shortly")
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False


class RunStore:
    """In-memory run records (capped) + JSONL audit trail."""

    def __init__(self, audit_path: Path = AUDIT_FILE):
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._order: deque = deque()
        self._lock = threading.RLock()
        self._audit_path = audit_path
        self._local_slots = _LocalSemaphore(MAX_CONCURRENT_LOCAL)
        self._rate_lock = threading.Lock()
        self._rate: deque = deque()

    def _audit(self, record: Dict[str, Any], event: str) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "event": event,
                "run_id": record.get("run_id"),
                "bot_id": record.get("bot_id"),
                "status": record.get("status"),
                "started_at": record.get("started_at"),
                "finished_at": record.get("finished_at"),
                "duration_ms": record.get("duration_ms"),
                "params_preview": redact(str(record.get("params_preview", "")))[:500],
                "error": redact(str(record.get("error") or ""))[:500],
                "pid": record.get("pid"),
            }, ensure_ascii=False, default=str)
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — audit must never break execution
            log.exception("sara-bots audit write failed")

    def create(self, bot_id: str, bot_name: str, kind: str,
               params_preview: str) -> Dict[str, Any]:
        record = {
            "run_id": uuid.uuid4().hex[:12],
            "bot_id": bot_id,
            "bot_name": bot_name,
            "kind": kind,
            "status": "queued",
            "params_preview": params_preview[:500],
            "output": "",
            "error": "",
            "events": [{"t": time.time(), "event": "queued"}],
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "exit_code": None,
            "pid": None,
            "cancellable": kind == "local",
        }
        with self._lock:
            self._runs[record["run_id"]] = record
            self._order.append(record["run_id"])
            while len(self._order) > MAX_RUNS_KEPT:
                self._runs.pop(self._order.popleft(), None)
        self._audit(record, "created")
        return record

    def mark(self, run_id: str, status: Optional[str] = None,
             event: Optional[str] = None, **fields: Any) -> None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return
            if status:
                record["status"] = status
            for key, value in fields.items():
                if value is not None:
                    record[key] = value
            if event:
                record["events"].append({"t": time.time(), "event": event})
        self._audit(record, event or status or "update")

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._runs.get(run_id)
            return dict(record) if record else None

    def list(self, bot_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            records = [dict(self._runs[rid]) for rid in reversed(self._order)
                       if not bot_id or self._runs[rid]["bot_id"] == bot_id]
        for record in records:
            record["output"] = record.get("output", "")[:2000]
        return records[:limit]

    def check_rate(self) -> None:
        now = time.time()
        with self._rate_lock:
            while self._rate and now - self._rate[0] > 60:
                self._rate.popleft()
            if len(self._rate) >= RATE_LIMIT_PER_MIN:
                raise RateLimited(
                    f"rate limit reached ({RATE_LIMIT_PER_MIN} runs/min); retry in under a minute")
            self._rate.append(now)

    @property
    def local_slots(self) -> _LocalSemaphore:
        return self._local_slots


def needs_confirmation(params: Dict[str, Any]) -> Optional[str]:
    """Deterministic risk check on a local-action payload. None = safe."""
    action = str(params.get("action") or params.get("mode") or "").lower().strip()
    if action in RISKY_ACTION_VALUES:
        return (f"this run's action '{action}' can change your machine or reach "
                "the outside world")
    for key in params:
        if str(key).lower() in RISKY_PARAM_KEYS:
            return f"this run passes a sensitive parameter ('{key}')"
    return None


def _worker_interpreter() -> str:
    if DESKTOP_PYTHON.exists():
        return str(DESKTOP_PYTHON)
    return sys.executable


def _worker_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in _WORKER_ENV_KEYS if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _kill_tree(pid: int) -> None:
    """Windows-safe process-tree termination (children die with the parent)."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=15, check=False,
        )
    except Exception:  # noqa: BLE001 — best effort; the timeout still applies
        log.warning("taskkill failed for pid %s", pid)


def _run_local_blocking(store: RunStore, run_id: str, root: Path,
                        tool_name: str, params: Dict[str, Any],
                        timeout_s: float) -> None:
    """Execute the worker subprocess. Runs on a worker thread."""
    started = time.time()
    store.mark(run_id, status="running", event="started",
               started_at=started, pid=None)
    cmd = [_worker_interpreter(), "-I", str(WORKER_PATH),
           "--root", str(root), "--tool", tool_name]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, cwd=str(root.parent),
            env=_worker_env(), text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        store.mark(run_id, status="failed", event="spawn_failed", finished_at=time.time(),
                   duration_ms=int((time.time() - started) * 1000),
                   error=f"could not start the worker process: {exc}")
        return

    store.mark(run_id, event="process_started", pid=proc.pid)
    # keep a handle for cancellation
    _LIVE_PROCS[run_id] = proc
    try:
        stdout, stderr = proc.communicate(
            input=json.dumps(params, ensure_ascii=False), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        store.mark(run_id, status="timed_out", event="timed_out",
                   finished_at=time.time(),
                   duration_ms=int((time.time() - started) * 1000),
                   exit_code=proc.returncode,
                   error=f"exceeded the {timeout_s:.0f}s timeout; the process tree "
                         "was terminated and the result is incomplete")
        return
    finally:
        _LIVE_PROCS.pop(run_id, None)

    duration_ms = int((time.time() - started) * 1000)
    store.mark(run_id, finished_at=time.time(), duration_ms=duration_ms,
               exit_code=proc.returncode)

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if proc.returncode != 0:
        store.mark(run_id, status="failed", event="crashed",
                   error=f"worker exited with code {proc.returncode}: "
                         f"{redact(stderr[-1500:] or stdout[-1500:])}")
        return
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (json.JSONDecodeError, IndexError):
        store.mark(run_id, status="failed", event="bad_protocol",
                   error="worker reply was not valid JSON",
                   output=stdout[:4000])
        return
    output = str(payload.get("output", ""))[:MAX_OUTPUT_CHARS]
    if payload.get("ok"):
        store.mark(run_id, status="completed", event="completed", output=output)
    else:
        store.mark(run_id, status="failed", event="action_failed",
                   error=redact(str(payload.get("error", "action failed"))),
                   output=output)


#: run_id -> Popen for cancellation (only local runs)
_LIVE_PROCS: Dict[str, subprocess.Popen] = {}


class SaraBotsRunner:
    """Façade the router calls. One instance per process."""

    def __init__(self, registry: Optional[SaraBotsRegistry] = None,
                 store: Optional[RunStore] = None):
        self.registry = registry or get_registry()
        self.store = store or RunStore()

    # ── public API ─────────────────────────────────────────────────────────
    async def start(self, bot_id: str, params: Dict[str, Any],
                    base_url: str, api_key: str,
                    timeout_s: float = DEFAULT_TIMEOUT_S,
                    confirm: bool = False) -> Dict[str, Any]:
        """Validate + launch one run. Returns the initial run record."""
        self.store.check_rate()
        spec = self.registry.bot(bot_id)
        if spec is None or spec.adapter is None:
            raise KeyError(f"bot '{bot_id}' is not runnable from Sara Bots")

        state = self.registry.bot_states(None).get(bot_id, {})
        if state.get("state") == "disabled":
            raise PermissionError(f"bot '{bot_id}' is disabled")
        if bot_id.startswith("action."):
            if not state.get("approved"):
                raise PermissionError(
                    f"bot '{bot_id}' is discovered but NOT approved yet — approve "
                    "it first (POST /api/sara-bots/bots/{id}/approval)")
            reason = needs_confirmation(params)
            if reason and not confirm:
                raise ConfirmationRequired(
                    f"Confirm this run: {reason}. Re-send with confirm=true to "
                    "execute exactly this payload.")

        timeout_s = min(max(5.0, float(timeout_s)), MAX_TIMEOUT_S)
        preview = json.dumps(params, ensure_ascii=False, default=str)
        record = self.store.create(bot_id, spec.name, spec.kind, preview)

        if spec.adapter == "local_action":
            # Run from the exact directory the file was discovered in — the
            # discovered source_path is the approved location for this bot,
            # independent of any later root reconfiguration.
            root = (Path(spec.source_path).parent if spec.source_path
                    else self.registry.store.root()
                    or REPO_ROOT / "sara_desktop" / "actions")
            tool_name = bot_id[len("action."):]
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None, self._guarded_local, record["run_id"], root, tool_name,
                params, timeout_s)
        else:
            task = asyncio.create_task(
                self._run_adapter(record["run_id"], spec.adapter, params,
                                  base_url, api_key, timeout_s))
            _LIVE_TASKS[record["run_id"]] = task
            task.add_done_callback(lambda t, rid=record["run_id"]: _LIVE_TASKS.pop(rid, None))
        return self.store.get(record["run_id"]) or record

    def _guarded_local(self, run_id: str, root: Path, tool_name: str,
                       params: Dict[str, Any], timeout_s: float) -> None:
        try:
            with self.store.local_slots:
                _run_local_blocking(self.store, run_id, root, tool_name,
                                    params, timeout_s)
        except RateLimited as exc:
            self.store.mark(run_id, status="failed", event="rejected",
                            error=str(exc))
        except Exception as exc:  # noqa: BLE001 — recorded, never crashes the loop
            self.store.mark(run_id, status="failed", event="internal_error",
                            error=f"{type(exc).__name__}: {exc}")

    def cancel(self, run_id: str) -> Dict[str, Any]:
        record = self.store.get(run_id)
        if record is None:
            raise KeyError(f"unknown run '{run_id}'")
        if record["status"] not in ("queued", "running"):
            raise PermissionError(f"run '{run_id}' already finished ({record['status']})")
        proc = _LIVE_PROCS.get(run_id)
        if proc is not None:
            _kill_tree(proc.pid)
            self.store.mark(run_id, status="cancelled", event="cancelled",
                            finished_at=time.time(),
                            error="cancelled by the owner; the process tree was terminated")
        else:
            # adapter runs: cancel the coroutine if we still hold it
            task = _LIVE_TASKS.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            self.store.mark(run_id, status="cancelled", event="cancelled",
                            finished_at=time.time(), error="cancelled by the owner")
        return self.store.get(run_id) or record

    # ── hub adapters (real HTTP to the real routes) ────────────────────────
    async def _run_adapter(self, run_id: str, adapter: str,
                           params: Dict[str, Any], base_url: str,
                           api_key: str, timeout_s: float) -> None:
        started = time.time()
        self.store.mark(run_id, status="running", event="started", started_at=started)
        headers = {"X-API-Key": api_key} if api_key else {}
        base = base_url.rstrip("/")
        try:
            if adapter == "trading_demo":
                await self._adapter_trading(run_id, base, headers, params, timeout_s)
            elif adapter == "scraper":
                await self._adapter_scraper(run_id, base, headers, params, timeout_s)
            elif adapter == "email_campaign":
                await self._adapter_email(run_id, base, headers, params, timeout_s)
            else:
                self.store.mark(run_id, status="failed", event="no_adapter",
                                error=f"unknown adapter '{adapter}'")
        except asyncio.CancelledError:
            self.store.mark(run_id, status="cancelled", event="cancelled",
                            finished_at=time.time(),
                            error="cancelled by the owner mid-flight")
        except httpx.TimeoutException:
            self.store.mark(run_id, status="timed_out", event="timed_out",
                            finished_at=time.time(),
                            duration_ms=int((time.time() - started) * 1000),
                            error=f"the agent did not answer within {timeout_s:.0f}s")
        except Exception as exc:  # noqa: BLE001 — real failure surfaced
            self.store.mark(run_id, status="failed", event="agent_error",
                            finished_at=time.time(),
                            duration_ms=int((time.time() - started) * 1000),
                            error=redact(f"{type(exc).__name__}: {exc}"))
        else:
            self.store.mark(run_id, finished_at=time.time(),
                            duration_ms=int((time.time() - started) * 1000))

    async def _adapter_trading(self, run_id: str, base: str, headers: Dict[str, str],
                               params: Dict[str, Any], timeout_s: float) -> None:
        data = {
            "auto_trade": "false",      # demo replay — never simulate live orders here
            "threshold": str(float(params.get("threshold", 0.85))),
            "eta": str(float(params.get("eta", 0.15))),
            "candles": str(int(params.get("candles", 2500))),
        }
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False,
                                     follow_redirects=False) as client:
            resp = await client.post(f"{base}/trading/demo", data=data, headers=headers)
        if resp.status_code != 200:
            self.store.mark(run_id, status="failed", event="agent_error",
                            error=f"trading agent returned HTTP {resp.status_code}: "
                                  f"{redact(resp.text[:400])}")
            return
        self.store.mark(run_id, status="completed", event="completed",
                        output=resp.text[:MAX_OUTPUT_CHARS])

    async def _adapter_scraper(self, run_id: str, base: str, headers: Dict[str, str],
                               params: Dict[str, Any], timeout_s: float) -> None:
        urls = params.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        body = {
            "urls": urls, "prompt": str(params.get("prompt", "")),
            "mode": "single",
            "maxPages": int(params.get("maxPages", 3)),
            "timeoutSec": min(timeout_s, 30),
        }
        events: List[Dict[str, Any]] = []
        final: Dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=timeout_s + 30, trust_env=False,
                                     follow_redirects=False) as client:
            async with client.stream("POST", f"{base}/api/agent",
                                     json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    self.store.mark(run_id, status="failed", event="agent_error",
                                    error=f"scraper returned HTTP {resp.status_code}: "
                                          f"{redact(text[:400])}")
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append({k: event.get(k) for k in ("type", "phase", "message")
                                   if k in event})
                    if isinstance(event, dict) and event.get("type") in (
                            "result", "done", "complete", "error"):
                        final = event
        if final.get("type") == "error":
            self.store.mark(run_id, status="failed", event="agent_error",
                            error=redact(str(final.get("error") or final)[:800]))
            return
        payload = {
            "final": final,
            "events": events[:80],
        }
        self.store.mark(run_id, status="completed", event="completed",
                        output=json.dumps(payload, ensure_ascii=False,
                                          default=str)[:MAX_OUTPUT_CHARS])

    async def _adapter_agency(self, run_id: str, base: str, headers: Dict[str, str],
                              params: Dict[str, Any], timeout_s: float) -> None:
        agent_id = str(params.get("agency_agent_id", "")).strip()
        message = str(params.get("message", "")).strip()
        if not _valid_agency_id(agent_id):
            self.store.mark(run_id, status="failed", event="invalid_input",
                            error="agency_agent_id must be an id from GET /api/agency/agents")
            return
        async with httpx.AsyncClient(timeout=max(timeout_s, 60.0), trust_env=False,
                                     follow_redirects=False) as client:
            resp = await client.post(f"{base}/api/agency/agents/{agent_id}/run",
                                     json={"message": message, "max_tokens": 2048},
                                     headers=headers)
        if resp.status_code != 200:
            self.store.mark(run_id, status="failed", event="agent_error",
                            error=f"agency agent returned HTTP {resp.status_code}: "
                                  f"{redact(resp.text[:400])}")
            return
        self.store.mark(run_id, status="completed", event="completed",
                        output=resp.text[:MAX_OUTPUT_CHARS])


    async def _adapter_email(self, run_id: str, base: str, headers: Dict[str, str],
                             params: Dict[str, Any], timeout_s: float) -> None:
        """Create a draft email campaign via the Email Automation agent and
        hand the owner a link to finish (clients / attachments / sending)."""
        name = str(params.get("name", "Sara bot campaign")).strip() or "Sara bot campaign"
        subject = str(params.get("subject", "")).strip()
        objective = str(params.get("objective", "")).strip()
        tone = str(params.get("tone", "professional")).strip() or "professional"
        context = str(params.get("context", "")).strip()
        instructions = str(params.get("instructions", "")).strip()
        provider = str(params.get("provider", "smtp")).strip().lower()
        if provider not in ("smtp", "gmail"):
            provider = "smtp"
        body: Dict[str, Any] = {
            "name": name, "subject": subject, "objective": objective,
            "tone": tone, "provider": provider, "dry_run": True,
        }
        if context:
            body["context"] = context
        if instructions:
            body["instructions"] = instructions
        try:
            async with httpx.AsyncClient(timeout=max(timeout_s, 30.0),
                                        trust_env=False, follow_redirects=False) as client:
                resp = await client.post(f"{base}/email-agent/campaigns",
                                         json=body, headers=headers)
        except Exception as exc:
            self.store.mark(run_id, status="failed", event="agent_error",
                            error=f"email agent unreachable: {redact(str(exc)[:400])}")
            return
        if resp.status_code not in (200, 201):
            self.store.mark(run_id, status="failed", event="agent_error",
                            error=f"email agent returned HTTP {resp.status_code}: "
                                  f"{redact(resp.text[:400])}")
            return
        try:
            data = resp.json()
        except Exception:
            data = {}
        campaign_id = data.get("id") or ""
        link = ("/hub/email-automation"
                + (f"?campaign={campaign_id}" if campaign_id else ""))
        result = {
            "campaign_id": campaign_id,
            "name": name,
            "status": data.get("status", "draft"),
            "link": link,
            "next_steps": [
                "Open the link to upload clients (CSV/PDF/Google Sheet).",
                "Connect your sender mailbox if you have not yet (Sender panel).",
                "Attach PDFs/resumes in the Attachments section.",
                "Switch to LIVE and start dispatch when ready.",
            ],
        }
        self.store.mark(run_id, status="completed", event="completed",
                        output=json.dumps(result, ensure_ascii=False)[:MAX_OUTPUT_CHARS])


#: run_id -> asyncio.Task for adapter cancellation
_LIVE_TASKS: Dict[str, asyncio.Task] = {}

_AGENCY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _valid_agency_id(agent_id: str) -> bool:
    if not _AGENCY_ID_RE.match(agent_id):
        return False
    try:
        inventory = json.loads(AGENCY_INVENTORY.read_text(encoding="utf-8"))
        return any(str(x.get("id")) == agent_id for x in inventory)
    except Exception:  # noqa: BLE001 — unreadable inventory = not validated
        return False


_runner: Optional[SaraBotsRunner] = None
_runner_lock = threading.Lock()


def get_runner() -> SaraBotsRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = SaraBotsRunner()
        return _runner
