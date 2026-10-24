"""SaraMuse powers — goals, watches, ideas, finance and notifications.

Adapted from OpenMuse (MIT license, github.com/CopilotKit/OpenMuse): the
finance CSV analysis, the page-change diff and the goal/monitor/idea domain
model are ports of that engine's ``finance.ts``, ``page-diff.ts`` and
``agent.ts``. The runtime here is Sara's own: durable JSON stores (same
atomic pattern as the rest of sara_bots), an asyncio monitor loop, and real
task submission through the Sara Bots engine — no simulated work.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("sara.bots.muse")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MUSE_FILE = DATA_DIR / "sara_bots_muse.json"

_MAX_GOALS = 100
_MAX_MONITORS = 50
_MAX_IDEAS = 200
_MAX_NOTIFICATIONS = 200
_MAX_MILESTONES = 20
MONITOR_TICK_S = 15.0
_MAX_PAGE_BYTES = 2 * 1024 * 1024


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).digest().hex()


# ─────────────────────────────────────────────────────────────────────────────
# Finance — port of OpenMuse engine/finance.ts (same conventions, same limits)
# ─────────────────────────────────────────────────────────────────────────────

_AMOUNT_RE = re.compile(r"^[-+]?\d+(?:\.\d{1,2})?$")
_REQUIRED_COLUMNS = ("date", "description", "amount", "category")


def analyze_spending(csv_text: str) -> Dict[str, Any]:
    """Parse a transaction CSV and summarize spending.

    Amount convention (kept from OpenMuse): positive values are expenses,
    negative values are income. Values use the source currency; no currency
    conversion is inferred.
    """
    if len(csv_text) > 500_000:
        raise ValueError("Import at most 500 KB of transaction CSV")
    # Excel and many bank exports start a UTF-8 CSV with a byte order mark.
    if csv_text.startswith("\ufeff"):
        csv_text = csv_text[1:]
    try:
        rows = [row for row in csv.reader(io.StringIO(csv_text))]
    except csv.Error as exc:
        raise ValueError(f"Invalid CSV: {exc}") from exc
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("CSV has no rows")
    header = [cell.strip().lower() for cell in rows[0]]
    for col in _REQUIRED_COLUMNS:
        if col not in header:
            raise ValueError("CSV needs date,description,amount,category columns")
        if header.count(col) > 1:
            raise ValueError("CSV has a duplicate required column")
    body = rows[1:]
    if not body or len(body) > 5000:
        raise ValueError("Import between 1 and 5,000 transactions")
    idx = {col: header.index(col) for col in _REQUIRED_COLUMNS}

    transactions: List[Dict[str, Any]] = []
    for n, row in enumerate(body, start=2):
        if len(row) != len(header):
            raise ValueError(f"Row {n} has the wrong number of columns")
        date = row[idx["date"]].strip()
        amount = row[idx["amount"]].strip()
        try:
            _ = time.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Row {n} needs an ISO date (YYYY-MM-DD) and a plain amount "
                "with at most two decimal places") from None
        if not _AMOUNT_RE.match(amount):
            raise ValueError(
                f"Row {n} needs an ISO date (YYYY-MM-DD) and a plain amount "
                "with at most two decimal places")
        cents = round(float(amount) * 100)
        if abs(cents) > 10**12:
            raise ValueError("Transaction amount is out of range")
        transactions.append({
            "id": f"row-{n}",
            "date": date,
            "description": row[idx["description"]].strip(),
            "amount": cents / 100,
            "category": row[idx["category"]].strip() or "Uncategorized",
            "_cents": cents,
        })

    spending_cents = sum(t["_cents"] for t in transactions if t["_cents"] > 0)
    income_cents = -sum(t["_cents"] for t in transactions if t["_cents"] < 0)
    grouped: Dict[str, int] = {}
    for t in transactions:
        if t["_cents"] > 0:
            grouped[t["category"]] = grouped.get(t["category"], 0) + t["_cents"]
    categories = sorted(
        ({"name": name, "amount": cents / 100} for name, cents in grouped.items()),
        key=lambda c: -c["amount"])
    dates = sorted(t["date"] for t in transactions)
    for t in transactions:
        t.pop("_cents", None)
    return {
        "income": income_cents / 100,
        "spending": spending_cents / 100,
        "saved": (income_cents - spending_cents) / 100,
        "count": len(transactions),
        "categories": categories,
        "transactions": transactions,
        "period": {"from": dates[0], "to": dates[-1]},
        "amountConvention": ("Positive expenses; negative income. Values use the "
                             "source currency; no currency conversion."),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Page diff — port of OpenMuse engine/page-diff.ts
# ─────────────────────────────────────────────────────────────────────────────

# Relative times ("posted 1 day ago" → "posted 2 days ago") tick on their own,
# so a change in them alone is not news. English + German forms, as upstream.
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:\d+|an?|one)\s+(?:sec(?:ond)?|min(?:ute)?|hour|hr|day|week|month|year)s?\s+ago\b"
    r"|\bvor\s+(?:\d+|einer?|einem)\s+(?:sekunde|minute|stunde|tag|woche|monat|jahr)(?:e|en|n)?\b",
    re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PLURAL_S_RE = re.compile(r"(\w)s\b")


def page_lines(text: str, limit: int = 2000) -> List[str]:
    lines: List[str] = []
    seen: set = set()
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()[:300]
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def without_relative_times(text: str) -> str:
    return _RELATIVE_TIME_RE.sub("<time>", text)


def _numberless(line: str) -> str:
    base = _NUMBER_RE.sub("#", without_relative_times(line))
    return _PLURAL_S_RE.sub(r"\1", base).lower()


def _pair(previous: List[str], current: List[str], key):
    open_pool: Dict[str, List[str]] = {}
    for line in previous:
        open_pool.setdefault(key(line), []).append(line)
    used: set = set()
    paired: List[str] = []
    unpaired: List[str] = []
    for line in current:
        pool = open_pool.get(key(line))
        if pool:
            match = pool.pop(0)
            used.add(match)
            paired.append(line)
        else:
            unpaired.append(line)
    return paired, unpaired, [l for l in previous if l not in used]


def diff_page(previous: List[str], current: List[str]) -> Dict[str, List[str]]:
    paired, unpaired, left = _pair(previous, current, lambda l: l)
    # A line where only a relative time changed is left out.
    paired2, unpaired2, left2 = _pair(left, unpaired, without_relative_times)
    paired3, unpaired3, left3 = _pair(left2, unpaired2, _numberless)
    return {"added": unpaired3, "updated": paired3, "removed": left3}


def describe_page_diff(diff: Dict[str, List[str]]) -> str:
    parts: List[str] = []

    def section(title: str, lines: List[str], limit: int) -> None:
        if not lines:
            return
        parts.append(f"{title}:")
        parts.extend(f"• {line[:160]}" for line in lines[:limit])
        if len(lines) > limit:
            parts.append(f"+{len(lines) - limit} more")

    section("New", diff["added"], 8)
    section("Updated", diff["updated"], 4)
    section("Removed", diff["removed"], 4)
    return "\n".join(parts)


def count_page_diff(diff: Dict[str, List[str]]) -> str:
    total = len(diff["added"]) + len(diff["updated"]) + len(diff["removed"])
    if not total:
        return ""
    bits = []
    if diff["added"]:
        bits.append(f"{len(diff['added'])} new")
    if diff["updated"]:
        bits.append(f"{len(diff['updated'])} updated")
    if diff["removed"]:
        bits.append(f"{len(diff['removed'])} removed")
    return f"{total} line{'s' if total != 1 else ''} changed ({', '.join(bits)})"


# ─────────────────────────────────────────────────────────────────────────────
# Store — one durable JSON file for goals, monitors, ideas, notifications
# ─────────────────────────────────────────────────────────────────────────────

class MuseStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else MUSE_FILE
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
        except FileNotFoundError:
            data = {}
        except Exception:
            log.exception("sara-muse store unreadable — starting empty")
            data = {}
        for key in ("goals", "monitors", "ideas", "notifications", "artifacts",
                    "monitor_pages"):
            data.setdefault(key, {})
        data.setdefault("identity", {"name": "SaraMuse", "tone": "warm"})
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    # -- identity -----------------------------------------------------------
    def identity(self) -> Dict[str, Any]:
        return dict(self._load()["identity"])

    def set_identity(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        data = self._load()
        identity = data["identity"]
        for key in ("name", "tone", "avatar", "showChatUpdates"):
            if key in patch:
                identity[key] = patch[key]
        self._save(data)
        return dict(identity)

    # -- goals --------------------------------------------------------------
    def create_goal(self, title: str, description: str, category: str,
                    milestones: List[str]) -> Dict[str, Any]:
        data = self._load()
        if len(data["goals"]) >= _MAX_GOALS:
            raise ValueError("goal limit reached")
        goal = {
            "id": new_id("goal"),
            "title": title[:160],
            "description": (description or "")[:4000],
            "category": (category or "Personal")[:80],
            "status": "active",
            "milestones": [{"id": new_id("ms"), "title": m[:200], "done": False}
                           for m in milestones[:_MAX_MILESTONES]],
            "createdAt": time.time(),
        }
        data["goals"][goal["id"]] = goal
        self._save(data)
        return dict(goal)

    def update_goal(self, goal_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        data = self._load()
        goal = data["goals"].get(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        if "status" in patch:
            if patch["status"] not in ("active", "paused", "completed"):
                raise ValueError("invalid goal status")
            goal["status"] = patch["status"]
        self._save(data)
        return dict(goal)

    def set_milestone_done(self, goal_id: str, milestone_id: str,
                           done: bool) -> Dict[str, Any]:
        data = self._load()
        goal = data["goals"].get(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        for m in goal["milestones"]:
            if m["id"] == milestone_id:
                m["done"] = bool(done)
                self._save(data)
                return dict(goal)
        raise KeyError(milestone_id)

    def goals(self) -> List[Dict[str, Any]]:
        return sorted(self._load()["goals"].values(),
                      key=lambda g: g.get("createdAt", 0), reverse=True)

    # -- monitors -----------------------------------------------------------
    def create_monitor(self, title: str, url: str, condition: str, value: str,
                       interval_minutes: int) -> Dict[str, Any]:
        self._validate_monitor(url, condition, value, interval_minutes)
        data = self._load()
        if len(data["monitors"]) >= _MAX_MONITORS:
            raise ValueError("monitor limit reached")
        monitor = {
            "id": new_id("mon"),
            "title": title[:160],
            "url": url,
            "condition": condition,
            "value": value[:300],
            "intervalMinutes": interval_minutes,
            "status": "active",
            "nextCheckAt": time.time(),
            "lastCheckedAt": None,
            "lastValue": None,
            "lastHash": None,
            "error": None,
            "checks": 0,
            "failures": 0,
        }
        data["monitors"][monitor["id"]] = monitor
        self._save(data)
        return dict(monitor)

    @staticmethod
    def _validate_monitor(url: str, condition: str, value: str,
                          interval_minutes: int) -> None:
        if condition not in ("change", "contains", "price_below"):
            raise ValueError("condition must be change | contains | price_below")
        if not 1 <= int(interval_minutes) <= 10080:
            raise ValueError("interval_minutes must be between 1 and 10080")
        if condition != "change" and not (value or "").strip():
            raise ValueError("Enter a condition value")
        if condition == "price_below":
            try:
                price = float(value)
            except (TypeError, ValueError):
                raise ValueError("Enter a positive price") from None
            if price <= 0:
                raise ValueError("Enter a positive price")
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            raise ValueError("Use a public HTTP(S) page")

    def update_monitor(self, monitor_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        data = self._load()
        monitor = data["monitors"].get(monitor_id)
        if monitor is None:
            raise KeyError(monitor_id)
        monitor.update(patch)
        self._save(data)
        return dict(monitor)

    def monitors(self) -> List[Dict[str, Any]]:
        return list(self._load()["monitors"].values())

    def monitor_pages(self) -> Dict[str, Any]:
        return dict(self._load()["monitor_pages"])

    def save_monitor_page(self, monitor_id: str, page: Dict[str, Any]) -> None:
        data = self._load()
        data["monitor_pages"][monitor_id] = page
        self._save(data)

    # -- ideas --------------------------------------------------------------
    def put_idea(self, idea: Dict[str, Any]) -> None:
        data = self._load()
        if idea["id"] not in data["ideas"] and len(data["ideas"]) >= _MAX_IDEAS:
            return
        data["ideas"][idea["id"]] = idea
        self._save(data)

    def update_idea(self, idea_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = self._load()
        idea = data["ideas"].get(idea_id)
        if idea is None:
            return None
        idea.update(patch)
        self._save(data)
        return dict(idea)

    def ideas(self) -> List[Dict[str, Any]]:
        return sorted(self._load()["ideas"].values(),
                      key=lambda i: i.get("createdAt", 0), reverse=True)

    # -- notifications ------------------------------------------------------
    def notify(self, title: str, body: str, task_id: Optional[str] = None,
               key: Optional[str] = None) -> Dict[str, Any]:
        data = self._load()
        notif_id = sha(key) if key else new_id("ntf")
        existing = data["notifications"].get(notif_id)
        if existing:
            return dict(existing)
        if len(data["notifications"]) >= _MAX_NOTIFICATIONS:
            oldest = sorted(data["notifications"].values(),
                            key=lambda n: n.get("createdAt", 0))
            for stale in oldest[:len(data["notifications"]) - _MAX_NOTIFICATIONS + 1]:
                data["notifications"].pop(stale["id"], None)
        notification = {
            "id": notif_id,
            "taskId": task_id,
            "title": title,
            "body": body,
            "createdAt": time.time(),
            "read": False,
        }
        data["notifications"][notif_id] = notification
        self._save(data)
        return dict(notification)

    def mark_read(self, notif_id: str) -> Dict[str, Any]:
        data = self._load()
        notification = data["notifications"].get(notif_id)
        if notification is None:
            raise KeyError(notif_id)
        notification["read"] = True
        self._save(data)
        return dict(notification)

    def notifications(self) -> List[Dict[str, Any]]:
        return sorted(self._load()["notifications"].values(),
                      key=lambda n: n.get("createdAt", 0), reverse=True)

    # -- artifacts (finance trackers etc.) -----------------------------------
    def put_artifact(self, task_key: str, kind: str, title: str, summary: str,
                     data_payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self._load()
        artifact = {
            "id": sha(f"{task_key}:{kind}"),
            "taskId": task_key,
            "kind": kind,
            "title": title,
            "summary": summary,
            "data": data_payload,
            "createdAt": time.time(),
        }
        data["artifacts"][artifact["id"]] = artifact
        self._save(data)
        return dict(artifact)

    def artifacts(self) -> List[Dict[str, Any]]:
        return sorted(self._load()["artifacts"].values(),
                      key=lambda a: a.get("createdAt", 0), reverse=True)


_store: Optional[MuseStore] = None
_store_lock = threading.Lock()


def get_muse_store() -> MuseStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = MuseStore()
    return _store


# ─────────────────────────────────────────────────────────────────────────────
# Page fetching (SSRF-guarded) + observation
# ─────────────────────────────────────────────────────────────────────────────

def _assert_public_host(host: str) -> None:
    """Fail closed on private/loopback targets — a watch must never probe
    the local network or cloud metadata endpoints."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"cannot resolve host '{host}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"host '{host}' resolves to a non-public address")


_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>",
                              re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _extract_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&nbsp;", " "))
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def observe_page(url: str) -> Dict[str, Any]:
    """Fetch a public page and reduce it to text. Raises on failure."""
    from urllib.parse import urlparse
    import httpx

    parsed = urlparse(url)
    _assert_public_host(parsed.hostname or "")
    async with httpx.AsyncClient(timeout=15.0, trust_env=False,
                                 follow_redirects=True,
                                 max_redirects=5) as client:
        resp = await client.get(
            url, headers={"User-Agent": "SaraMuse-watch/1.0 (+sara-ai-platform)"})
        resp.raise_for_status()
        content = resp.content[:_MAX_PAGE_BYTES]
        text = _extract_text(content.decode(resp.encoding or "utf-8",
                                            errors="replace"))
        return {"url": str(resp.url), "title": parsed.hostname or url,
                "text": text}


def matches_price(text: str, threshold: float) -> bool:
    for m in re.finditer(r"(?:\$|USD)\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)", text):
        if float(m.group(1).replace(",", "")) < threshold:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Ideas — suggestions with source evidence, derived from real records
# ─────────────────────────────────────────────────────────────────────────────

def refresh_ideas(store: Optional[MuseStore] = None) -> List[Dict[str, Any]]:
    """Derive suggestions from actual goal/task state. Mail-based ideas from
    OpenMuse are skipped honestly — Sara has no connected mailbox here yet."""
    store = store or get_muse_store()
    ideas = {i["id"]: i for i in store.ideas()}

    for goal in store.goals():
        if goal.get("status") != "active" or goal.get("milestones"):
            continue
        idea_id = sha(f"goal:{goal['id']}:{goal.get('description', '')}")
        existing = ideas.get(idea_id)
        if existing and existing.get("status") != "new":
            continue
        store.put_idea({
            "id": idea_id,
            "title": f"Let's make a plan for {goal['title']}",
            "reason": "This goal has no milestones yet. A concrete plan will give it a next step.",
            "evidence": [{"id": goal["id"], "kind": "user", "title": goal["title"],
                          "excerpt": goal.get("description", "")}],
            "prompt": f"Create an actionable plan for {goal['title']}. {goal.get('description', '')}".strip(),
            "kind": "plan",
            "input": {"goalId": goal["id"]},
            "status": existing.get("status", "new") if existing else "new",
            "createdAt": existing.get("createdAt") if existing else time.time(),
        })
    return store.ideas()


def decide_idea(idea_id: str, action: str, prompt: Optional[str] = None) -> Dict[str, Any]:
    store = get_muse_store()
    idea = next((i for i in store.ideas() if i["id"] == idea_id), None)
    if idea is None:
        raise KeyError(idea_id)
    if action == "dismiss":
        if idea.get("status") == "new":
            store.update_idea(idea_id, {"status": "dismissed"})
        return store.ideas() and next(i for i in store.ideas() if i["id"] == idea_id)
    if action != "accept":
        raise ValueError("action must be accept | dismiss")
    if idea.get("status") == "dismissed":
        return idea

    task_id = _submit_to_saramuse(prompt or idea.get("prompt", ""),
                                  idempotency_key=f"idea:{idea_id}")
    goal_id = idea.get("input", {}).get("goalId")
    if goal_id:
        try:
            store.update_goal(str(goal_id), {})  # existence check only
        except KeyError:
            goal_id = None
    store.update_idea(idea_id, {"status": "accepted", "taskId": task_id})
    return next(i for i in store.ideas() if i["id"] == idea_id)


def _submit_to_saramuse(goal: str, idempotency_key: str) -> str:
    """Submit a real engine task to the live SaraMuse bot. Raises when the
    bot or engine is unavailable — never fabricates a task."""
    from app.services.sara_bots.registry import get_registry
    from app.services.sara_bots.engine import get_engine

    pair = get_registry()._saramuse_pair()
    if pair is None:
        raise ValueError("SaraMuse bot not found — create it under My Bots first")
    _, live = pair
    task = get_engine().submit(live, goal, kind="user",
                               idempotency_key=idempotency_key)
    return task["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Monitor worker — real scheduled page checks with failure backoff
# ─────────────────────────────────────────────────────────────────────────────

class MonitorWorker:
    """Checks due monitors on a fixed tick. Faithful to OpenMuse's semantics:
    change watches ignore relative-time-only shifts, alerts are deduplicated,
    failures back off exponentially and pause the watch after 5 in a row."""

    def __init__(self, store: Optional[MuseStore] = None):
        self.store = store or get_muse_store()
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("sara-muse: monitor tick failed")
                await asyncio.sleep(MONITOR_TICK_S)
        finally:
            self._running = False

    async def tick(self) -> None:
        now = time.time()
        for monitor in self.store.monitors():
            if monitor.get("status") != "active":
                continue
            if float(monitor.get("nextCheckAt") or 0) > now:
                continue
            await self.check(monitor["id"])

    async def check(self, monitor_id: str) -> Dict[str, Any]:
        monitor = next((m for m in self.store.monitors()
                        if m["id"] == monitor_id), None)
        if monitor is None:
            raise KeyError(monitor_id)
        if monitor.get("status") == "stopped":
            raise ValueError("monitor is stopped")
        try:
            observation = await observe_page(monitor["url"])
        except Exception as exc:  # noqa: BLE001 — failure is data, with backoff
            return self._record_failure(monitor, f"{type(exc).__name__}: {exc}")

        text = re.sub(r"\s+", " ", observation["text"]).strip()
        current_hash = sha(text)
        previous_hash = monitor.get("lastHash")
        condition = monitor.get("condition", "change")
        if condition == "change":
            matched = bool(previous_hash and previous_hash != current_hash)
        elif condition == "contains":
            matched = str(monitor.get("value", "")).lower() in text.lower()
        else:
            matched = matches_price(text, float(monitor.get("value") or 0))

        lines = page_lines(observation["text"]) if condition == "change" else []
        timeless_hash = (sha(without_relative_times(text))
                        if condition == "change" else None)
        saved_page = self.store.monitor_pages().get(monitor_id)
        baseline = saved_page if (saved_page or {}).get("hash") == previous_hash else None
        # Only relative times changed anywhere on the page: watch quietly.
        quiet = bool(baseline and baseline.get("timelessHash") == timeless_hash)
        previously_matched = bool(monitor.get("matched"))
        should_notify = bool(
            matched and not quiet
            and (condition == "change" or not previously_matched))
        diff = diff_page(baseline["lines"], lines) if (baseline and should_notify) else None
        changes = describe_page_diff(diff) if diff else ""
        alert_sequence = int(monitor.get("alertSequence") or 0) + (1 if should_notify else 0)
        interval = max(1, int(monitor.get("intervalMinutes") or 15))

        self.store.update_monitor(monitor_id, {
            "status": "active",
            "checks": int(monitor.get("checks") or 0) + 1,
            "lastCheckedAt": time.time(),
            "lastHash": current_hash,
            "lastValue": text[:1000],
            "nextCheckAt": time.time() + interval * 60,
            "error": None,
            "failures": 0,
            "matched": matched,
            "alertSequence": alert_sequence,
        })
        if condition == "change":
            self.store.save_monitor_page(monitor_id, {
                "hash": current_hash, "timelessHash": timeless_hash, "lines": lines})

        result: str
        if should_notify:
            count = count_page_diff(diff) if diff else ""
            result = (f"Change found: {count}. A notification is ready." if count
                      else "Change found. A notification is ready.")
            body = (f"Changed at {observation['url']}\n{changes}" if changes
                    else f"Condition met at {observation['url']}: {text[:240]}")
            self.store.notify(
                monitor.get("title") or "Watch",
                body,
                key=f"monitor:{monitor_id}:{alert_sequence}:{current_hash}")
        else:
            result = "Watching. I'll check again on schedule."
        return {"monitor_id": monitor_id, "matched": matched,
                "notified": should_notify, "result": result, "changes": changes}

    def _record_failure(self, monitor: Dict[str, Any], detail: str) -> Dict[str, Any]:
        failures = int(monitor.get("failures") or 0) + 1
        backoff_minutes = min(60, 2 ** failures)
        paused = failures >= 5
        self.store.update_monitor(monitor["id"], {
            "status": "paused" if paused else "active",
            "error": detail,
            "failures": failures,
            "nextCheckAt": time.time() + backoff_minutes * 60,
        })
        if paused:
            self.store.notify(
                "Watch paused after repeated failures",
                f"{monitor.get('title') or monitor['id']}: {detail}",
                key=f"watch-paused:{monitor['id']}:{failures}")
        return {"monitor_id": monitor["id"], "matched": False, "notified": False,
                "result": "Check failed" + (" — watch paused" if paused else
                                            " — retry scheduled"),
                "error": detail}


_worker: Optional[MonitorWorker] = None
_worker_lock = threading.Lock()


def get_monitor_worker() -> MonitorWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = MonitorWorker()
    return _worker


# ─────────────────────────────────────────────────────────────────────────────
# Workspace snapshot — everything the SaraMuse UI needs in one payload
# ─────────────────────────────────────────────────────────────────────────────

def snapshot(store: Optional[MuseStore] = None) -> Dict[str, Any]:
    store = store or get_muse_store()
    try:
        from app.services.sara_bots.registry import get_registry
        pair = get_registry()._saramuse_pair()
        bot = pair[1] if pair else None
    except Exception:  # noqa: BLE001 — snapshot must survive registry errors
        bot = None
    memories: List[Dict[str, Any]] = []
    if bot is not None:
        try:
            from app.services.sara_bots.custom import get_custom_store
            memories = get_custom_store().memory(bot["id"])
        except Exception:  # noqa: BLE001
            memories = []
    try:
        from app.services.sara_bots.engine import get_engine
        engine = get_engine().status()
    except Exception:  # noqa: BLE001
        engine = {"engine_running": False}
    return {
        "goals": store.goals(),
        "monitors": store.monitors(),
        "ideas": store.ideas(),
        "notifications": store.notifications(),
        "artifacts": store.artifacts(),
        "memories": memories,
        "identity": store.identity(),
        "bot": ({"id": bot["id"], "name": bot.get("name"), "status": bot.get("status")}
                if bot else None),
        "worker": {"running": bool(engine.get("engine_running")),
                   "queued": engine.get("queued", 0),
                   "active": engine.get("active", 0)},
    }
