"""Custom Sara Bots â€” user-created, goal-driven AI workers.

This is the "create your own" layer of Sara Bots: a saved worker definition
(purpose, instructions, tool allowlist, autonomy, memory scope, plan mode)
that accepts goals and is executed by the durable task engine (engine.py).

One JSON file holds every custom bot (atomic writes, capped revisions).
Memory lives in one JSON file PER BOT under sara_bots_memory/ so isolation is
structural â€” a bot can never read another bot's memory through this store.

Honesty rules:
  * a custom bot can only be assigned tools that exist in the LIVE tool
    registry (validated against real ids at save time);
  * a natural-language instruction can never grant permissions â€” the tool
    allowlist + autonomy + the runner's deterministic gates decide.

Tenancy: every record carries an ``owner`` â€” the storage key of the tenant
that created it (the signed-in user's id, or ``"local"`` for the machine
operator). The API filters reads/writes by owner; the engine and scheduler
deliberately do not, so one backend can drive every user's bots. Records
written before tenancy (owner missing or the legacy placeholder ``"owner"``)
belong to the local operator.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("sara.bots.custom")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CUSTOM_FILE = DATA_DIR / "sara_bots_custom.json"
MEMORY_DIR = DATA_DIR / "sara_bots_memory"
ARTIFACTS_DIR = DATA_DIR / "sara_bots_artifacts"

BOT_CATEGORIES = ("research", "monitor", "ops", "coder", "business",
                  "creative", "data", "custom")
AUTONOMY_MODES = ("assist", "supervised", "autonomous")
PLAN_MODES = ("auto", "direct")
BOT_STATUSES = ("active", "paused", "disabled")
MEMORY_KINDS = ("preference", "fact", "project", "task", "feedback")

_MAX_REVISIONS = 20
_MAX_MEMORY_ENTRIES = 200
_MAX_NAME = 60
_MAX_DESC = 400
_MAX_GOAL = 2000
_MAX_INSTRUCTIONS = 8000
_MAX_OUTPUT_FORMAT = 200
_MAX_PARAMS_CHARS = 4000
_MAX_TOOLS = 40


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


#: Owner recorded for the machine's operator. Matches ``LOCAL_TENANT_ID`` in
#: the Ultron tenancy module; signed-in users get their own id instead.
LOCAL_OWNER = "local"


def normalize_owner(value: Any) -> str:
    """Owner of a stored record; legacy placeholders map to the operator."""
    owner = str(value or "").strip()
    return LOCAL_OWNER if owner in ("", "owner") else owner


def _require_str(value: Any, field: str, max_len: int,
                 min_len: int = 0) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{field}' must be a string")
    text = value.strip()
    if len(text) < min_len:
        raise ValueError(f"'{field}' must be at least {min_len} characters")
    if len(text) > max_len:
        raise ValueError(f"'{field}' exceeds {max_len} characters")
    return text


def validate_config(payload: Dict[str, Any], tool_ids: set,
                    skill_ids: Optional[set] = None,
                    existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate + normalize a custom-bot configuration.

    ``tool_ids`` is the set of REAL tool ids from the live registry â€” a bot
    can never be configured with a tool that does not exist. When ``existing``
    is given this is a partial update merged over the current config.
    """
    if not isinstance(payload, dict):
        raise ValueError("configuration must be an object")
    merged: Dict[str, Any] = dict(existing or {})
    merged.update(payload)

    out: Dict[str, Any] = {}
    out["name"] = _require_str(merged.get("name", ""), "name", _MAX_NAME, 2)
    out["description"] = _require_str(merged.get("description", ""),
                                      "description", _MAX_DESC)
    out["goal"] = _require_str(merged.get("goal", ""), "goal", _MAX_GOAL)
    out["instructions"] = _require_str(merged.get("instructions", ""),
                                       "instructions", _MAX_INSTRUCTIONS)
    category = str(merged.get("category", "custom")).strip().lower()
    if category not in BOT_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(BOT_CATEGORIES)}")
    out["category"] = category

    out["language"] = _require_str(str(merged.get("language", "en")),
                                   "language", 20) or "en"
    out["model"] = _require_str(str(merged.get("model", "")), "model", 100)
    out["output_format"] = _require_str(str(merged.get("output_format", "")),
                                        "output_format", _MAX_OUTPUT_FORMAT)

    tools = merged.get("tools", [])
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise ValueError(f"'tools' must be a list of at most {_MAX_TOOLS} ids")
    clean_tools: List[str] = []
    for tool in tools:
        if not isinstance(tool, str) or len(tool) > 120:
            raise ValueError("every tool entry must be a tool-id string")
        tool = tool.strip()
        if tool not in tool_ids:
            raise ValueError(f"tool '{tool}' does not exist in the live tool "
                             "registry â€” it cannot be assigned")
        if tool not in clean_tools:
            clean_tools.append(tool)
    out["tools"] = clean_tools
    if not clean_tools:
        raise ValueError("a bot needs at least one real tool")

    skills = merged.get("skills", [])
    if not isinstance(skills, list) or len(skills) > _MAX_TOOLS:
        raise ValueError("'skills' must be a list of at most "
                         f"{_MAX_TOOLS} ids")
    clean_skills: List[str] = []
    _allowed_skills = skill_ids or set()
    for sk in skills:
        if not isinstance(sk, str) or len(sk) > 120:
            raise ValueError("every skill entry must be a skill-id string")
        sk = sk.strip()
        if sk not in _allowed_skills:
            raise ValueError(f"skill '{sk}' does not exist for this "
                              "owner — it cannot be attached")
        if sk not in clean_skills:
            clean_skills.append(sk)
    out["skills"] = clean_skills

    autonomy = str(merged.get("autonomy", "supervised")).strip().lower()
    if autonomy not in AUTONOMY_MODES:
        raise ValueError(f"autonomy must be one of {', '.join(AUTONOMY_MODES)}")
    out["autonomy"] = autonomy

    plan_mode = str(merged.get("plan_mode", "auto")).strip().lower()
    if plan_mode not in PLAN_MODES:
        raise ValueError(f"plan_mode must be one of {', '.join(PLAN_MODES)}")
    out["plan_mode"] = plan_mode

    direct_tool = str(merged.get("direct_tool", "")).strip()
    if plan_mode == "direct":
        if direct_tool not in clean_tools:
            raise ValueError("direct mode requires 'direct_tool' to be one of "
                             "the assigned tools")
    out["direct_tool"] = direct_tool if plan_mode == "direct" else ""

    direct_params = merged.get("direct_params", {})
    if plan_mode == "direct":
        if not isinstance(direct_params, dict):
            raise ValueError("direct_params must be an object")
        try:
            encoded = json.dumps(direct_params, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"direct_params is not JSON-serializable: {exc}")
        if len(encoded) > _MAX_PARAMS_CHARS:
            raise ValueError("direct_params payload too large")
        out["direct_params"] = direct_params
    else:
        out["direct_params"] = {}

    out["memory_enabled"] = bool(merged.get("memory_enabled", True))

    try:
        hue = int(merged.get("hue", 200))
    except (TypeError, ValueError):
        raise ValueError("'hue' must be an integer")
    if not 0 <= hue <= 360:
        raise ValueError("'hue' must be between 0 and 360")
    out["hue"] = hue

    status = str(merged.get("status", "active")).strip().lower()
    if status not in BOT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(BOT_STATUSES)}")
    out["status"] = status
    return out


class CustomBotStore:
    """Thread-safe JSON store for custom bots and their scoped memory."""

    def __init__(self, path: Optional[Path] = None,
                 memory_dir: Optional[Path] = None):
        self.path = Path(path) if path else CUSTOM_FILE
        self.memory_dir = Path(memory_dir) if memory_dir else MEMORY_DIR
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
            data.setdefault("bots", {})
            for bot in data["bots"].values():
                if isinstance(bot, dict):
                    bot["owner"] = normalize_owner(bot.get("owner"))
            return data
        except FileNotFoundError:
            return {"bots": {}}
        except Exception:
            log.exception("sara-bots: custom store unreadable â€” starting empty")
            return {"bots": {}}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    # -- CRUD ----------------------------------------------------------------
    def list(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            bots = list(self._load()["bots"].values())
        if owner is not None:
            bots = [b for b in bots if b.get("owner") == owner]
        bots.sort(key=lambda b: b.get("created_at", 0))
        return bots

    def get(self, bot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._load()["bots"].get(bot_id)

    def create(self, config: Dict[str, Any],
               owner: str = LOCAL_OWNER) -> Dict[str, Any]:
        now = time.time()
        bot = dict(config)
        bot["id"] = new_id("cbot")
        bot["owner"] = normalize_owner(owner)
        bot["created_at"] = now
        bot["updated_at"] = now
        bot["revisions"] = [{"at": now, "note": "created",
                             "snapshot": {k: bot[k] for k in
                                          ("name", "tools", "autonomy",
                                           "plan_mode", "status")}}]
        with self._lock:
            data = self._load()
            data["bots"][bot["id"]] = bot
            self._save(data)
        return bot

    def update(self, bot_id: str, config: Dict[str, Any],
               note: str = "updated") -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            bot = data["bots"].get(bot_id)
            if bot is None:
                raise KeyError(f"bot '{bot_id}' not found")
            bot = dict(bot)
            bot.update(config)
            bot["updated_at"] = time.time()
            revisions = list(bot.get("revisions", []))
            revisions.append({"at": bot["updated_at"], "note": note,
                              "snapshot": {k: bot.get(k) for k in
                                           ("name", "tools", "autonomy",
                                            "plan_mode", "status")}})
            bot["revisions"] = revisions[-_MAX_REVISIONS:]
            data["bots"][bot_id] = bot
            self._save(data)
        return bot

    def delete(self, bot_id: str) -> Dict[str, Any]:
        with self._lock:
            data = self._load()
            bot = data["bots"].pop(bot_id, None)
            if bot is None:
                raise KeyError(f"bot '{bot_id}' not found")
            self._save(data)
        mem = self.memory_dir / f"{bot_id}.json"
        try:
            if mem.exists():
                mem.unlink()
        except OSError:
            log.warning("sara-bots: could not remove memory for %s", bot_id)
        return bot

    def duplicate(self, bot_id: str, new_name: str) -> Dict[str, Any]:
        with self._lock:
            original = self._load()["bots"].get(bot_id)
        if original is None:
            raise KeyError(f"bot '{bot_id}' not found")
        config = {k: v for k, v in original.items()
                  if k not in ("id", "created_at", "updated_at", "revisions",
                               "owner")}
        config["name"] = _require_str(new_name or f"{original['name']} copy",
                                      "name", _MAX_NAME, 2)
        return self.create(config,
                           owner=normalize_owner(original.get("owner")))

    def set_status(self, bot_id: str, status: str) -> Dict[str, Any]:
        if status not in BOT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(BOT_STATUSES)}")
        return self.update(bot_id, {"status": status},
                           note=f"status -> {status}")

    # -- memory (per-bot file; structural isolation) -------------------------
    def _memory_path(self, bot_id: str) -> Path:
        if not bot_id or not all(c.isalnum() or c in "_-" for c in bot_id):
            raise ValueError("invalid bot id")
        return self.memory_dir / f"{bot_id}.json"

    def memory(self, bot_id: str) -> List[Dict[str, Any]]:
        path = self._memory_path(bot_id)
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            return entries if isinstance(entries, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            log.exception("sara-bots: memory unreadable for %s", bot_id)
            return []

    def _save_memory(self, bot_id: str, entries: List[Dict[str, Any]]) -> None:
        path = self._memory_path(bot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries[:_MAX_MEMORY_ENTRIES],
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    def memory_add(self, bot_id: str, kind: str, text: str) -> Dict[str, Any]:
        kind = (kind or "fact").strip().lower()
        if kind not in MEMORY_KINDS:
            raise ValueError(f"memory kind must be one of "
                             f"{', '.join(MEMORY_KINDS)}")
        text = _require_str(text, "text", 500, 1)
        with self._lock:
            entries = self.memory(bot_id)
            entry = {"id": new_id("mem"), "kind": kind, "text": text,
                     "ts": time.time()}
            entries.append(entry)
            self._save_memory(bot_id, entries)
        return entry

    def memory_replace(self, bot_id: str,
                       entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(entries, list) or len(entries) > _MAX_MEMORY_ENTRIES:
            raise ValueError(f"memory must be a list of at most "
                             f"{_MAX_MEMORY_ENTRIES} entries")
        clean: List[Dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("every memory entry must be an object")
            kind = str(item.get("kind", "fact")).strip().lower()
            if kind not in MEMORY_KINDS:
                raise ValueError(f"invalid memory kind '{kind}'")
            text = _require_str(item.get("text", ""), "text", 500, 1)
            clean.append({"id": str(item.get("id") or new_id("mem")),
                          "kind": kind, "text": text,
                          "ts": float(item.get("ts") or time.time())})
        with self._lock:
            self._save_memory(bot_id, clean)
        return clean

    def memory_clear(self, bot_id: str) -> None:
        with self._lock:
            self._save_memory(bot_id, [])


_store: Optional[CustomBotStore] = None
_store_lock = threading.Lock()


def get_custom_store() -> CustomBotStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = CustomBotStore()
    return _store
