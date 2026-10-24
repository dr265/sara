"""Sara Bots skills — reusable, SKILL.md-compatible instruction packages.

A skill is a *reusable instruction bundle* (the Agent Skills `SKILL.md`
format adapted to Sara): a stable id, a name/description, a body of
instructions, the tool ids it depends on, and a version. Skills are the
OpenMuse-inspired "powers" primitive — they are *not* tools themselves, but
they tell a bot HOW to use its assigned tools for a class of task.

Tenancy: skills are owner-scoped (same as custom bots). A skill created by
user A is invisible to user B. The machine operator (``local``) keeps the
shared library. A bot can only attach skills that exist for its owner.

Honesty: a skill's ``required_tools`` are validated against the LIVE tool
registry at save time — a skill can never claim a power the platform does
not actually have. Attaching a skill to a bot does NOT grant the bot any
tool; the bot's ``tools`` allowlist remains the sole authority (a skill
only provides instructions + declares dependencies).
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

from app.services.sara_bots.custom import (LOCAL_OWNER, normalize_owner,
                                           _require_str)

log = logging.getLogger("sara.bots.skills")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SKILLS_FILE = DATA_DIR / "sara_bots_skills.json"

_MAX_NAME = 60
_MAX_DESC = 400
_MAX_INSTRUCTIONS = 8000
_MAX_TOOLS = 40
_MAX_VERSION = 40

_lock = threading.RLock()


def new_id() -> str:
    return f"skl_{uuid.uuid4().hex[:10]}"


def _load() -> Dict[str, Any]:
    try:
        if SKILLS_FILE.exists():
            return json.loads(SKILLS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — corrupt file -> empty library
        log.warning("skills store unreadable: %s", exc)
    return {"skills": []}


def _save(data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SKILLS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, SKILLS_FILE)


def _skill_ids_for(owner: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (data.get("skills") or [])
            if normalize_owner(s.get("owner")) == normalize_owner(owner)]


def list_skills(owner: str) -> List[Dict[str, Any]]:
    with _lock:
        data = _load()
    return [dict(s) for s in _skill_ids_for(owner, data)]


def get_skill(skill_id: str, owner: str) -> Optional[Dict[str, Any]]:
    with _lock:
        data = _load()
    for s in _skill_ids_for(owner, data):
        if s.get("id") == skill_id:
            return dict(s)
    return None


def validate_skill(payload: Dict[str, Any], tool_ids: set,
                   existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("skill must be an object")
    merged: Dict[str, Any] = dict(existing or {})
    merged.update(payload)

    out: Dict[str, Any] = {}
    out["name"] = _require_str(merged.get("name", ""), "name", _MAX_NAME, 2)
    out["description"] = _require_str(merged.get("description", ""),
                                      "description", _MAX_DESC)
    out["instructions"] = _require_str(merged.get("instructions", ""),
                                       "instructions", _MAX_INSTRUCTIONS, 1)

    tools = merged.get("required_tools", [])
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise ValueError(f"'required_tools' must be a list of at most {_MAX_TOOLS} ids")
    cleaned: List[str] = []
    seen = set()
    for t in tools:
        if not isinstance(t, str):
            raise ValueError("'required_tools' entries must be strings")
        t = t.strip()
        if not t or t in seen:
            continue
        if t not in tool_ids:
            raise ValueError(f"unknown tool id '{t}' — skill cannot claim a power "
                            "the platform does not have")
        seen.add(t)
        cleaned.append(t)
    out["required_tools"] = cleaned

    version = str(merged.get("version", "1.0.0")).strip() or "1.0.0"
    if len(version) > _MAX_VERSION:
        raise ValueError("'version' too long")
    out["version"] = version

    out["enabled"] = bool(merged.get("enabled", True))

    return out


def create_skill(owner: str, payload: Dict[str, Any], tool_ids: set) -> Dict[str, Any]:
    validated = validate_skill(payload, tool_ids)
    now = time.time()
    skill = {
        "id": new_id(),
        "owner": normalize_owner(owner),
        "name": validated["name"],
        "description": validated["description"],
        "instructions": validated["instructions"],
        "required_tools": validated["required_tools"],
        "version": validated["version"],
        "enabled": validated["enabled"],
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        data = _load()
        data.setdefault("skills", []).append(skill)
        _save(data)
    return dict(skill)


def update_skill(skill_id: str, owner: str, payload: Dict[str, Any],
                 tool_ids: set) -> Dict[str, Any]:
    with _lock:
        data = _load()
        skills = data.setdefault("skills", [])
        for idx, s in enumerate(skills):
            if s.get("id") != skill_id:
                continue
            if normalize_owner(s.get("owner")) != normalize_owner(owner):
                raise PermissionError("skill belongs to another owner")
            validated = validate_skill(payload, tool_ids, existing=s)
            s.update({
                "name": validated["name"],
                "description": validated["description"],
                "instructions": validated["instructions"],
                "required_tools": validated["required_tools"],
                "version": validated["version"],
                "enabled": validated["enabled"],
                "updated_at": time.time(),
            })
            _save(data)
            return dict(s)
    raise KeyError(skill_id)


def delete_skill(skill_id: str, owner: str) -> bool:
    with _lock:
        data = _load()
        skills = data.setdefault("skills", [])
        for idx, s in enumerate(skills):
            if s.get("id") != skill_id:
                continue
            if normalize_owner(s.get("owner")) != normalize_owner(owner):
                raise PermissionError("skill belongs to another owner")
            del skills[idx]
            _save(data)
            return True
    return False


def skill_instructions(skill_ids: List[str], owner: str) -> str:
    """Return the concatenated instruction body of the named (enabled) skills.

    Unknown or disabled skills are silently skipped — a bot's skill list may
    reference skills that were later deleted/disabled.
    """
    if not skill_ids:
        return ""
    out: List[str] = []
    with _lock:
        data = _load()
    by_id = {s.get("id"): s for s in _skill_ids_for(owner, data)}
    for sid in skill_ids:
        s = by_id.get(sid)
        if not s or not s.get("enabled", True):
            continue
        out.append(f"## Skill: {s.get('name')} (v{s.get('version')})\n"
                   f"{s.get('instructions', '')}".strip())
    return "\n\n".join(out)


def skill_ids_for_owner(owner: str) -> set:
    with _lock:
        data = _load()
    return {s.get("id") for s in _skill_ids_for(owner, data)}