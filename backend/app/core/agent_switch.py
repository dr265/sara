"""Run/pause switch shared by the agent frontends.

Each agent (whatsapp, telegram) keeps a persisted "running" flag in
<backend>/agent_runtime.json. The flag survives backend restarts, so an agent
that was switched ON stays ON until the user pauses it — and vice versa.

Defaults: agents run unless explicitly paused (matches pre-switch behaviour).
"""
from __future__ import annotations

import json
from pathlib import Path

_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "agent_runtime.json"
_DEFAULTS = {"whatsapp": True, "telegram": True, "social": True, "edulearner": True}


def _load() -> dict:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def is_running(agent: str) -> bool:
    state = _load()
    return bool(state.get(agent, _DEFAULTS.get(agent, True)))


def set_running(agent: str, running: bool) -> dict:
    state = _load()
    state[agent] = bool(running)
    _save(state)
    return {"agent": agent, "running": bool(running), "persisted": True}


def snapshot() -> dict:
    state = _load()
    return {name: bool(state.get(name, default)) for name, default in _DEFAULTS.items()}
