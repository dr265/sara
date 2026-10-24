"""Sara Bots security gate tests — ALLOW/ASK/DENY boundary."""
from __future__ import annotations

import pytest

from app.services.sara_bots.security_gate import Decision, evaluate


def _tool(state="ready", risk="write"):
    return {"id": "action.write_file", "state": state, "risk": risk,
            "detail": "ok"}


def _bot(tools=None, autonomy="supervised"):
    return {"tools": tools or ["action.write_file"], "autonomy": autonomy}


def test_deny_when_not_granted():
    d, _ = evaluate("action.write_file", _tool(), {"tools": [], "autonomy": "autonomous"})
    assert d == Decision.DENY


def test_deny_when_tool_missing():
    d, _ = evaluate("action.write_file", None, _bot())
    assert d == Decision.DENY


def test_deny_when_not_ready():
    d, _ = evaluate("action.write_file", _tool(state="needs_approval"), _bot())
    assert d == Decision.DENY


def test_ask_for_write_under_supervised():
    d, _ = evaluate("action.write_file", _tool(risk="write"), _bot(autonomy="supervised"))
    assert d == Decision.ASK


def test_allow_for_write_under_autonomous():
    d, _ = evaluate("action.write_file", _tool(risk="write"), _bot(autonomy="autonomous"))
    assert d == Decision.ALLOW


def test_allow_read_under_any_mode():
    for mode in ("supervised", "assist", "autonomous"):
        d, _ = evaluate("action.hello_world" if False else "action.write_file",
                        _tool(risk="read"), _bot(autonomy=mode))
        assert d == Decision.ALLOW


def test_ask_under_assist():
    d, _ = evaluate("action.write_file", _tool(risk="external"), _bot(autonomy="assist"))
    assert d == Decision.ASK


def test_deny_unknown_autonomy():
    d, _ = evaluate("action.write_file", _tool(), _bot(autonomy="bogus"))
    assert d == Decision.DENY


def test_llm_tool_allowed():
    d, _ = evaluate("llm.reason", {"id": "llm.reason", "state": "ready", "risk": "llm"},
                    {"tools": ["llm.reason"], "autonomy": "supervised"})
    assert d == Decision.ALLOW
