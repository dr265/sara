"""Sara Bots client for Jarvis (SARA Desktop).

The platform's Sara Bots service (backend ``/api/sara-bots``) discovers the
approved local action bots, gates destructive runs and executes every local
bot in a sandboxed worker process. Jarvis reaches that service through the
same :class:`BackendConnection` (base URL + API key) it uses for every other
backend call.

ADAPTIVE Stage 3 ("try an existing Sara Bot") was discovery-only. This module
adds the missing caller: given a spoken/text request it

  1. lists the discovered bots through the live service,
  2. scores only READY + approved + invocable ``local`` bots against the
     content words of the request,
  3. resolves the bot's parameters from words the user actually said — never
     invented ones, and
  4. starts a real run and waits for its terminal record.

Everything here is honest by construction: :func:`match_and_run` returns
``ran=False`` plus a reason whenever nothing was executed, and otherwise the
completed run's real output verbatim. Destructive options (kill, shutdown,
send, …) are never auto-selected; the service's own confirmation gate stays
the final authority and Jarvis never auto-confirms.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

from core.agent_registry import BackendConnection

#: Run statuses that mean the service is finished with this run.
TERMINAL_STATUSES = {"completed", "failed", "timed_out", "cancelled"}

#: Option values that must never be auto-selected from a request: they change
#: the machine or reach the outside world.
BLOCKED_OPTIONS = {
    "kill", "kill_process", "end_task", "shutdown", "restart", "sleep",
    "delete", "remove", "format", "send", "publish", "post", "tweet",
    "broadcast", "logout", "sign_out", "lock", "unlock", "clean",
    "organize", "wallpaper", "wallpaper_url", "set_brightness", "task",
}

#: Request words that stand in for an option value (e.g. "diagnostics report"
#: → ``full``). The option itself must still be safe to auto-run.
OPTION_SYNONYMS: Dict[str, Set[str]] = {
    "full": {"full", "everything", "complete", "report", "overview",
             "diagnostics", "health", "summary"},
    "disk": {"disk", "disks", "storage", "space", "drives"},
    "battery": {"battery", "power"},
    "brightness": {"brightness", "screen"},
    "top_processes": {"processes", "process", "cpu", "ram", "memory",
                      "tasks"},
    "stats": {"stats", "statistics", "usage"},
    "list": {"list", "items"},
}

#: Words too generic to prove a bot match on their own.
_STOPWORDS = {
    "the", "a", "an", "my", "me", "you", "your", "please", "jarvis", "run",
    "use", "call", "check", "show", "give", "get", "and", "or", "of", "for",
    "with", "on", "in", "at", "to", "is", "it", "do", "what", "how", "now",
    "some", "this", "that",
}

#: Longest output Jarvis keeps in a spoken/typed reply (the full record stays
#: in the service's run history).
MAX_OUTPUT_CHARS = 4000


def _words(text: Any) -> Set[str]:
    """Lowercase content words (length > 2, no stopwords)."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _bot_words(bot: Dict[str, Any]) -> Set[str]:
    blob = " ".join([
        str(bot.get("name") or ""),
        str(bot.get("id") or "").replace(".", " ").replace("_", " "),
        " ".join(str(c) for c in (bot.get("capabilities") or [])),
        str(bot.get("description") or ""),
    ])
    return _words(blob)


def list_bots(conn: Optional[BackendConnection] = None,
              timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Live registry snapshot from the platform's Sara Bots service."""
    conn = conn or BackendConnection()
    resp = requests.get(f"{conn.base_url}/api/sara-bots/bots",
                        headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    return list(payload.get("bots") or [])


def score_bot(request_words: Set[str], bot: Dict[str, Any]) -> int:
    """How many of the request's content words name this bot/capability."""
    return len(request_words & _bot_words(bot))


def pick_bot(request_words: Set[str], bots: List[Dict[str, Any]],
             min_score: int = 2) -> Optional[Dict[str, Any]]:
    """Best READY + approved + invocable local bot for these request words.

    ``min_score`` defaults to 2 so a single generic word can never trigger a
    run; ties keep the first (registry order) bot.
    """
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for bot in bots:
        if bot.get("kind") != "local":
            continue
        if not (bot.get("approved") and bot.get("invocable")):
            continue
        if str(bot.get("state") or "").lower() not in ("ready", "available"):
            continue
        score = score_bot(request_words, bot)
        if score > best_score:
            best, best_score = bot, score
    if best is None or best_score < min_score:
        return None
    return best


def _option_mentioned(option: str, request_words: Set[str]) -> bool:
    option = option.strip().lower()
    if option in request_words:
        return True
    return bool(OPTION_SYNONYMS.get(option, set()) & request_words)


def _coerce(value: Any, type_name: Any) -> Any:
    t = str(type_name or "").lower()
    if t in ("number", "integer", "int", "float"):
        try:
            return int(str(value))
        except ValueError:
            try:
                return float(str(value))
            except ValueError:
                return value
    if t in ("boolean", "bool"):
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return str(value)


def resolve_params(bot: Dict[str, Any], request: str,
                   request_words: Set[str]
                   ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fill the bot's parameters from words the user actually said.

    Returns ``(params, "")`` when every required parameter is satisfied, or
    ``(None, reason)`` — an honest refusal instead of guessing.
    """
    accepts = bot.get("accepts") or {}
    params: Dict[str, Any] = {}
    for name, spec in accepts.items():
        spec = spec if isinstance(spec, dict) else {"type": "string"}
        desc = str(spec.get("description") or "")
        options = [o.strip() for o in desc.split("|")] if "|" in desc else []
        chosen: Optional[str] = None
        for option in options:
            base = re.split(r"\s*\(", option, 1)[0].strip()
            if base and _option_mentioned(base, request_words):
                chosen = base
                break
        if (chosen is None and str(spec.get("type")) == "string"
                and str(name).lower() in ("city", "location")):
            m = re.search(r"\b(?:in|for|at)\s+"
                          r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)", request)
            if m:
                chosen = m.group(1)
        if chosen is not None:
            if chosen.lower() in BLOCKED_OPTIONS:
                return None, (f"option '{chosen}' can change the machine or "
                              "reach the outside world — never auto-run")
            params[name] = _coerce(chosen, spec.get("type"))
            continue
        if spec.get("required"):
            return None, f"required parameter '{name}' is not in the request"
    return params, ""


def run_bot(conn: BackendConnection, bot_id: str, params: Dict[str, Any],
            timeout_s: float = 90.0, http_timeout: float = 20.0,
            poll_s: float = 0.75) -> Dict[str, Any]:
    """Start a real run on the Sara Bots service and wait for its record.

    Raises ``PermissionError`` when the service's risk gate demands explicit
    confirmation for this payload; other HTTP errors propagate untouched.
    """
    started = requests.post(
        f"{conn.base_url}/api/sara-bots/bots/{bot_id}/run",
        headers=conn.headers(), timeout=http_timeout,
        json={"params": params, "timeout_s": float(timeout_s)})
    if started.status_code == 409:
        detail = ""
        try:
            detail = str((started.json() or {}).get("detail") or "")
        except Exception:  # noqa: BLE001 — keep the generic reason
            pass
        raise PermissionError(detail or
                              "the service requires explicit confirmation")
    started.raise_for_status()
    record = started.json() or {}
    run_id = str(record.get("run_id") or record.get("id") or "")
    deadline = time.time() + float(timeout_s) + 60.0
    while (run_id and record.get("status") not in TERMINAL_STATUSES
           and time.time() < deadline):
        time.sleep(poll_s)
        poll = requests.get(f"{conn.base_url}/api/sara-bots/runs/{run_id}",
                            headers=conn.headers(), timeout=http_timeout)
        poll.raise_for_status()
        record = poll.json() or {}
    return record


def match_and_run(request: str, timeout_s: float = 90.0) -> Dict[str, Any]:
    """Run the best matching approved local Sara Bot for one request.

    Result dict (always honest):

    * ``{"ran": True, "bot_id", "bot_name", "params", "run_id", "status",
      "output", "error", "duration_ms"}``
    * ``{"ran": False, "reason": "..."}`` — nothing was executed.
    """
    try:
        conn = BackendConnection()
        bots = list_bots(conn)
    except Exception as exc:  # noqa: BLE001 — honest skip, never a fake run
        return {"ran": False,
                "reason": f"Sara Bots service unreachable "
                          f"({type(exc).__name__}: {exc})"}

    request_words = _words(request)
    bot = pick_bot(request_words, bots)
    if bot is None:
        return {"ran": False,
                "reason": "no discovered local bot matches this request"}

    params, why = resolve_params(bot, request, request_words)
    if params is None:
        return {"ran": False, "reason": f"{bot.get('id')} matched, but {why}"}

    try:
        record = run_bot(conn, str(bot.get("id")), params,
                         timeout_s=timeout_s)
    except PermissionError as exc:
        return {"ran": False,
                "reason": f"{bot.get('id')} needs explicit confirmation: "
                          f"{exc}"}
    except Exception as exc:  # noqa: BLE001 — surface, don't invent
        return {"ran": False,
                "reason": f"could not start {bot.get('id')} "
                          f"({type(exc).__name__}: {exc})"}

    output = str(record.get("output") or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS - 1] + "…"
    return {
        "ran": True,
        "bot_id": str(bot.get("id")),
        "bot_name": str(bot.get("name") or bot.get("id")),
        "params": params,
        "run_id": str(record.get("run_id") or ""),
        "status": str(record.get("status") or ""),
        "output": output,
        "error": str(record.get("error") or ""),
        "duration_ms": record.get("duration_ms"),
    }


def format_result(result: Dict[str, Any]) -> str:
    """One honest paragraph for the assistant/UI (used by main.py)."""
    if not result.get("ran"):
        return ""
    seconds = (result.get("duration_ms") or 0) / 1000.0
    header = (f"[Sara Bot] {result.get('bot_name')} ({result.get('bot_id')})"
              f" — {result.get('status')} in {seconds:.1f}s")
    body = (result.get("output") or result.get("error") or "").strip()
    return header + (f"\n{body}" if body else "")


# ── custom bots ("My Bots") — reuse, create, run, wait ──────────────────────
# ADAPTIVE Stage 3 reuses a previously created custom bot when its purpose
# matches the request; Stage 4 creates a REAL specialized worker through the
# platform API (validated against the live tool registry) and runs the goal
# as a durable engine task. Every function here returns honest structures:
# {"ran": False, "reason": ...} when nothing was executed.

#: Engine task states that mean the durable engine is finished with a task.
CUSTOM_TERMINAL = {"completed", "failed", "timed_out", "cancelled"}
#: States that need a human decision before anything more happens.
CUSTOM_WAITING = {"waiting_input", "waiting_approval", "blocked"}
#: Custom-bot statuses that may be reused (paused/disabled ones may not).
CUSTOM_RUNABLE_STATUS = {"active"}

#: Absolute Windows paths inside task outputs become file artifacts.
_BOT_PATH_RE = re.compile(r"[A-Za-z]:\\[^\r\n\"'<>|*?]{3,180}\.[A-Za-z0-9]{1,5}")

#: Tool-id fragments that reach the outside world or mutate the machine —
#: only assigned when the request itself asks for that action.
_SENSITIVE_TOOL_WORDS = ("send", "publish", "post", "email", "message",
                         "delete", "remove", "purchase", "buy", "trade",
                         "pay", "shutdown", "kill", "install")


def list_custom_bots(conn: Optional[BackendConnection] = None,
                     timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Every custom bot the local owner created (live service)."""
    conn = conn or BackendConnection()
    resp = requests.get(f"{conn.base_url}/api/sara-bots/custom",
                        headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    return list(payload.get("bots") or [])


def list_live_tools(conn: Optional[BackendConnection] = None,
                    timeout: float = 15.0) -> List[Dict[str, Any]]:
    """The live tool registry (real readiness states per tool)."""
    conn = conn or BackendConnection()
    resp = requests.get(f"{conn.base_url}/api/sara-bots/tools",
                        headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    return list(payload.get("tools") or [])


def score_custom_bot(request_words: Set[str], bot: Dict[str, Any]) -> int:
    blob = " ".join(str(bot.get(k) or "") for k in
                    ("name", "description", "goal", "instructions"))
    return len(request_words & _words(blob))


def pick_custom_bot(request_words: Set[str], bots: List[Dict[str, Any]],
                    min_score: int = 2) -> Optional[Dict[str, Any]]:
    """Best runnable custom bot over the score threshold, or None."""
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for bot in bots:
        if str(bot.get("status") or "active").lower() not in CUSTOM_RUNABLE_STATUS:
            continue
        score = score_custom_bot(request_words, bot)
        if score > best_score:
            best, best_score = bot, score
    if best is None or best_score < min_score:
        return None
    return best


def run_custom_task(conn: Optional[BackendConnection], bot_id: str, goal: str,
                    timeout: float = 30.0) -> Dict[str, Any]:
    conn = conn or BackendConnection()
    resp = requests.post(f"{conn.base_url}/api/sara-bots/custom/{bot_id}/tasks",
                         headers=conn.headers(), timeout=timeout,
                         json={"goal": str(goal)[:2000]})
    resp.raise_for_status()
    return resp.json() or {}


def get_custom_task(conn: Optional[BackendConnection], task_id: str,
                    timeout: float = 15.0) -> Dict[str, Any]:
    conn = conn or BackendConnection()
    resp = requests.get(f"{conn.base_url}/api/sara-bots/tasks/{task_id}",
                        headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json() or {}


def cancel_custom_task(conn: Optional[BackendConnection], task_id: str,
                       timeout: float = 15.0) -> Dict[str, Any]:
    conn = conn or BackendConnection()
    resp = requests.post(f"{conn.base_url}/api/sara-bots/tasks/{task_id}/cancel",
                         headers=conn.headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json() or {}


def wait_custom_task(conn: Optional[BackendConnection], task_id: str,
                     timeout_s: float = 180.0, poll_s: float = 2.0,
                     progress: Optional[Callable[[str], None]] = None
                     ) -> Dict[str, Any]:
    """Poll to a terminal/waiting state (bounded); returns the last record."""
    conn = conn or BackendConnection()
    deadline = time.time() + max(1.0, float(timeout_s))
    record: Dict[str, Any] = {}
    last = ""
    while time.time() < deadline:
        try:
            record = get_custom_task(conn, task_id)
        except Exception:
            time.sleep(poll_s)
            continue
        status = str(record.get("status") or "")
        if status != last:
            last = status
            if callable(progress):
                try:
                    progress(status)
                except Exception:
                    pass
        if status in CUSTOM_TERMINAL or status in CUSTOM_WAITING:
            return record
        time.sleep(poll_s)
    return record


def _task_output(task: Dict[str, Any]) -> str:
    parts: List[str] = []
    outputs = task.get("outputs") or {}
    if isinstance(outputs, dict):
        for key in sorted(outputs.keys(), key=str):
            val = str(outputs.get(key) or "").strip()
            if val:
                parts.append(val)
    for key in ("output", "result", "result_text", "summary", "final_report"):
        val = task.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    text = "\n".join(parts)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS - 1] + "…"
    return text


def _task_artifacts(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    raw = task.get("artifacts")
    if isinstance(raw, list):
        for item in raw[:12]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("url") or item.get("path") or "").strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            key = "url" if ref.startswith(("http://", "https://")) else "path"
            arts.append({"kind": str(item.get("kind") or "file"),
                         "title": str(item.get("title") or ref[:80]),
                         key: ref})
    for match in _BOT_PATH_RE.findall(_task_output(task))[:6]:
        if match in seen:
            continue
        seen.add(match)
        arts.append({"kind": "file",
                     "title": match.rsplit("\\", 1)[-1][:80],
                     "path": match})
    return arts


def _run_summary(bot: Dict[str, Any], record: Dict[str, Any],
                 reused: bool = False) -> Dict[str, Any]:
    status = str(record.get("status") or "")
    return {
        "ran": True,
        "reused": reused,
        "bot_id": str(bot.get("id") or ""),
        "bot_name": str(bot.get("name") or bot.get("id") or ""),
        "task_id": str(record.get("id") or record.get("task_id") or ""),
        "status": status,
        "waiting": status in CUSTOM_WAITING,
        "still_running": status not in CUSTOM_TERMINAL
                          and status not in CUSTOM_WAITING,
        "output": _task_output(record),
        "error": str(record.get("error") or ""),
        "artifacts": _task_artifacts(record),
    }


def _start_and_wait(conn: BackendConnection, bot: Dict[str, Any], goal: str,
                    wait_s: float, reused: bool,
                    progress=None) -> Dict[str, Any]:
    try:
        started = run_custom_task(conn, str(bot.get("id")), goal)
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = str((exc.response.json() or {}).get("detail") or "")
        except Exception:
            detail = str(exc)
        return {"ran": False,
                "reason": f"bot {bot.get('id')} rejected the task: "
                          f"{detail[:300]}"}
    except Exception as exc:
        return {"ran": False,
                "reason": f"could not start bot {bot.get('id')}: "
                          f"{type(exc).__name__}: {exc}"}
    task_id = str(started.get("id") or started.get("task_id") or "")
    if not task_id:
        return {"ran": False,
                "reason": "bot task start returned no task id"}
    record = wait_custom_task(conn, task_id, timeout_s=wait_s,
                              progress=progress)
    return _run_summary(bot, record, reused=reused)


def match_custom_bot(request: str, wait_s: float = 150.0,
                     progress=None) -> Dict[str, Any]:
    """Reuse: run the best-matching CUSTOM bot's task for this request."""
    try:
        conn = BackendConnection()
        bots = list_custom_bots(conn)
    except Exception as exc:
        return {"ran": False,
                "reason": f"Sara Bots service unreachable "
                          f"({type(exc).__name__}: {exc})"}
    bot = pick_custom_bot(_words(request), bots)
    if bot is None:
        return {"ran": False, "reason": "no custom bot matches this request"}
    return _start_and_wait(conn, bot, request, wait_s, reused=True,
                           progress=progress)


def _short_bot_name(request: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", request or "") if w]
    name = "Auto: " + " ".join(words[:5])
    return name[:60] or "Auto Bot"


def _guess_category(request: str) -> str:
    low = (request or "").lower()
    for words, cat in (
            (("monitor", "watch", "price", "track"), "monitor"),
            (("research", "scrape", "collect", "compare", "find"), "research"),
            (("code", "script", "program", "bug", "build"), "coder"),
            (("write", "draft", "content", "article", "post"), "creative"),
            (("csv", "spreadsheet", "data", "report", "analys"), "data"),
            (("budget", "invoice", "spending", "finance", "account"), "business"),
    ):
        if any(w in low for w in words):
            return cat
    return "custom"


def _pick_tools(request: str, tools: List[Dict[str, Any]],
                limit: int = 3) -> List[str]:
    """Score READY live tools against the request — never guesses an id."""
    request_words = _words(request)
    scored: List[Tuple[int, str]] = []
    for tool in tools:
        if str(tool.get("state") or "").lower() != "ready":
            continue
        tool_id = str(tool.get("id") or "")
        if not tool_id:
            continue
        blob = " ".join(str(tool.get(k) or "")
                        for k in ("id", "name", "description"))
        score = len(request_words & _words(blob))
        low = tool_id.lower()
        # sensitive tools only when the request itself asks for that action
        if any(w in low for w in _SENSITIVE_TOOL_WORDS) and \
                not any(w in (request or "").lower()
                        for w in _SENSITIVE_TOOL_WORDS):
            score = 0
        if score > 0:
            scored.append((score, tool_id))
    scored.sort(key=lambda t: t[0], reverse=True)
    picked: List[str] = []
    for _score, tool_id in scored:
        if tool_id not in picked:
            picked.append(tool_id)
        if len(picked) >= limit:
            break
    return picked


def create_and_run_custom_bot(request: str, wait_s: float = 180.0,
                              progress=None) -> Dict[str, Any]:
    """ADAPTIVE Stage 4: create a REAL specialized bot (when tools fit) + run.

    Refuses honestly when no READY live tool matches the capability — a bot
    without real tools would be theatre. Never duplicates an existing bot:
    a close match is reused instead.
    """
    try:
        conn = BackendConnection()
        bots = list_custom_bots(conn)
    except Exception as exc:
        return {"ran": False,
                "reason": f"Sara Bots service unreachable "
                          f"({type(exc).__name__}: {exc})"}

    # never create a duplicate — a close match runs the existing worker
    request_words = _words(request)
    existing = pick_custom_bot(request_words, bots, min_score=2)
    if existing is not None:
        return _start_and_wait(conn, existing, request, wait_s, reused=True,
                               progress=progress)

    try:
        tools = list_live_tools(conn)
    except Exception as exc:
        return {"ran": False,
                "reason": f"live tool registry unreachable: "
                          f"{type(exc).__name__}: {exc}"}
    tool_ids = _pick_tools(request, tools)
    if not tool_ids:
        return {"ran": False,
                "reason": ("no READY live tool matches this capability — "
                           "a bot could not be given real abilities")}

    payload = {
        "name": _short_bot_name(request),
        "description": (request or "")[:400],
        "goal": (request or "")[:2000],
        "instructions": (
            "Execute this objective end-to-end using ONLY the assigned tools. "
            "Verify each step's real outcome; never report unverified success. "
            "Finish with a short factual summary of what was produced."),
        "category": _guess_category(request),
        "tools": tool_ids,
        "autonomy": "supervised",
        "plan_mode": "auto",
        "memory_enabled": True,
        "output_format": "markdown report",
    }
    try:
        created = requests.post(f"{conn.base_url}/api/sara-bots/custom",
                                headers=conn.headers(), timeout=25,
                                json=payload)
        created.raise_for_status()
        bot = created.json() or {}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = str((exc.response.json() or {}).get("detail") or "")
        except Exception:
            detail = str(exc)
        return {"ran": False,
                "reason": f"creation rejected: {detail[:300]}"}
    except Exception as exc:
        return {"ran": False,
                "reason": f"creation failed: {type(exc).__name__}: {exc}"}

    bot_id = str(bot.get("id") or "")
    if not bot_id:
        return {"ran": False, "reason": "creation returned no bot id"}
    if callable(progress):
        try:
            progress(f"created '{bot.get('name')}' ({bot_id}) "
                     f"with tools: {', '.join(tool_ids)}")
        except Exception:
            pass
    return _start_and_wait(conn, bot, request, wait_s, reused=False,
                           progress=progress)


def format_custom_result(result: Dict[str, Any]) -> str:
    """One honest paragraph for the assistant/UI (mirrors format_result)."""
    if not result.get("ran"):
        return ""
    name = result.get("bot_name") or result.get("bot_id") or "Sara Bot"
    status = str(result.get("status") or "")
    task8 = str(result.get("task_id") or "")[:8]
    if result.get("waiting"):
        return (f"[Sara Bot] {name} — task {task8} is WAITING for your "
                f"decision (status: {status}). Do not treat the work as "
                f"finished; approve or respond in the Sara Bots panel.")
    if result.get("still_running"):
        return (f"[Sara Bot] {name} — still working on task {task8} "
                f"(status: {status}). It runs in the background; do NOT "
                f"claim any result yet.")
    body = (result.get("output") or result.get("error") or "").strip()
    reused = " (reused existing bot)" if result.get("reused") else ""
    header = f"[Sara Bot] {name}{reused} — {status} (task {task8})"
    return header + (f"\n{body}" if body else "")
