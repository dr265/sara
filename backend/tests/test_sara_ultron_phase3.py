"""Phase 3 regression suite - the Agent Control Center's data surfaces.

What this locks down
--------------------
* Every probe path in ``core.fleet.AGENT_PROBES`` resolves against the real
  ASGI app. The control center claims to know whether an agent is up; if a
  probe path rots, that claim silently becomes "unreachable", which reads as a
  dead agent. Here that fails the suite instead.
* ``online`` is only ever produced by a real 2xx. An unreachable fleet reports
  ``unreachable``, never a green light.
* Health payloads are redacted before they reach the browser.
* Lifecycle phases cannot label a partial run as a clean completion.
* The memory surface reads and writes the same on-disk store, and the write
  survives a fresh store instance (the restart test).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.sara_ultron_agent.core import fleet  # noqa: E402
from app.agents.sara_ultron_agent.core.memory import UltronMemory  # noqa: E402
from app.agents.sara_ultron_agent.core.session import (  # noqa: E402
    PHASES,
    TERMINAL_PHASES,
    UltronSession,
)


# ---------------------------------------------------------------------------
# 1. Probe table integrity
# ---------------------------------------------------------------------------
def test_every_probe_path_resolves():
    """Each probe path must exist in the served app; 404 is never a status."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    unresolvable = []
    for entry in fleet.AGENT_PROBES:
        probe = entry.get("probe")
        if not probe:
            continue
        response = client.get(probe)
        if response.status_code == 404:
            unresolvable.append((entry["id"], probe))
    assert not unresolvable, f"probe paths do not resolve: {unresolvable}"


def test_probe_table_ids_are_unique_and_named():
    ids = [e["id"] for e in fleet.AGENT_PROBES]
    assert len(ids) == len(set(ids)), "duplicate agent id in AGENT_PROBES"
    for entry in fleet.AGENT_PROBES:
        assert entry.get("name"), f"{entry['id']} has no display name"
        assert entry.get("probe") or entry.get("status") is None


def test_probe_map_is_the_probed_subset():
    mapping = fleet.probe_map()
    assert mapping["sara-ultron"] == "/ultron/health"
    assert all(p.startswith("/") for p in mapping.values())
    assert len(mapping) == sum(1 for e in fleet.AGENT_PROBES if e.get("probe"))


def test_agent_without_probe_is_declared_no_probe():
    """No probe -> no_probe. It must never be reported as online."""
    entry = {"id": "ghost", "name": "Ghost", "probe": None}
    result = asyncio.run(fleet._probe_one(client=None, semaphore=asyncio.Semaphore(1),
                                          base_url="http://x", entry=entry))
    assert result["status"] == fleet.NO_PROBE
    assert "cannot confirm" in result["detail"].lower()


# ---------------------------------------------------------------------------
# 2. Honesty: only a 2xx is online
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected", [
    (200, fleet.ONLINE),
    (204, fleet.ONLINE),
    (301, fleet.DEGRADED),
    (404, fleet.DEGRADED),
    (500, fleet.DEGRADED),
])
def test_http_status_classification(status, expected):
    assert fleet._classify(status, None, {}) == expected


def test_transport_error_is_unreachable_not_online():
    assert fleet._classify(None, "ConnectError: refused", None) == fleet.UNREACHABLE


def test_fleet_reports_unreachable_when_nothing_answers(monkeypatch):
    """The whole fleet down must read as down. No optimistic default."""
    class _Boom:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            raise ConnectionError("no route to host")

    monkeypatch.setattr(fleet.httpx, "AsyncClient", _Boom)
    result = asyncio.run(fleet.probe_fleet("http://testserver", []))
    assert result["summary"][fleet.ONLINE] == 0
    assert result["summary"][fleet.UNREACHABLE] == result["summary"]["probed"]
    assert all(a["status"] == fleet.UNREACHABLE
               for a in result["agents"] if a["probe"])


def test_fleet_reports_online_only_for_a_real_2xx(monkeypatch):
    class _Response:
        status_code = 200

        def json(self):
            return {"status": "online", "message": "ok"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return _Response()

    monkeypatch.setattr(fleet.httpx, "AsyncClient", _Client)
    result = asyncio.run(fleet.probe_fleet("http://testserver", []))
    assert result["summary"][fleet.ONLINE] == result["summary"]["probed"]
    assert result["summary"][fleet.UNREACHABLE] == 0


def test_ultron_entry_carries_live_run_state(monkeypatch):
    class _Response:
        status_code = 200

        def json(self):
            return {"status": "online"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return _Response()

    monkeypatch.setattr(fleet.httpx, "AsyncClient", _Client)
    session = UltronSession("summarise my desktop notes", mode="autonomous")
    session.status = "running"
    session.phase = "executing"
    session.current_tool = "file_controller"
    result = asyncio.run(fleet.probe_fleet("http://testserver", [session.snapshot()]))

    ultron = next(a for a in result["agents"] if a["id"] == "sara-ultron")
    assert ultron["live"]["active_runs"] == 1
    assert ultron["live"]["current"]["current_tool"] == "file_controller"
    assert ultron["live"]["current"]["phase"] == "executing"
    assert result["live_runs"], "an active run must appear in live_runs"


def test_health_payload_is_redacted_before_leaving_the_process():
    payload = {
        "status": "online",
        "message": "key sk-abcdef0123456789 and api_key=deadbeefdeadbeefdeadbeefdeadbeef",
        "trace": "Bearer abcdefghijklmnop",
    }
    detail = fleet._detail_from_payload(payload)
    assert "sk-abcdef0123456789" not in detail
    assert "deadbeefdeadbeefdeadbeefdeadbeef" not in detail
    assert "abcdefghijklmnop" not in detail
    assert "[redacted]" in detail
    # The route-defining keys still come through, or the panel is useless.
    assert "online" in detail


def test_detail_ignores_unknown_keys():
    assert fleet._detail_from_payload({"secret_config": "do-not-echo"}) == ""


def test_detail_is_length_bounded():
    detail = fleet._detail_from_payload({"message": "x" * 5000})
    assert len(detail) <= fleet.DETAIL_LIMIT


# ---------------------------------------------------------------------------
# 3. Lifecycle phases
# ---------------------------------------------------------------------------
def test_phases_are_ordered_and_terminal_set_is_complete():
    assert PHASES[0] == "received"
    assert PHASES.index("planning") < PHASES.index("executing") < PHASES.index("verifying")
    for phase in ("completed", "completed_with_issues", "failed", "stopped"):
        assert phase in PHASES
        assert phase in TERMINAL_PHASES


def test_set_phase_rejects_unknown_values():
    session = UltronSession("goal")
    with pytest.raises(ValueError):
        session.set_phase("pretending_to_work")


def test_partial_run_cannot_be_phased_as_completed():
    """``completed_with_issues`` maps to its own phase, not to ``completed``."""
    session = UltronSession("goal")
    session.status = "completed_with_issues"
    session.set_phase(session.status if session.status in TERMINAL_PHASES else "failed")
    assert session.phase == "completed_with_issues"


def test_phase_events_reach_the_stream():
    session = UltronSession("goal")
    session.set_phase("planning")
    event = session.event_q.get_nowait()
    assert event == {"type": "phase", "phase": "planning"}


def test_snapshot_exposes_phase_and_current_tool():
    session = UltronSession("goal")
    session.phase = "executing"
    session.current_tool = "browser_control"
    snap = session.snapshot()
    assert snap["phase"] == "executing"
    assert snap["current_tool"] == "browser_control"


# ---------------------------------------------------------------------------
# 4. Memory surface - real reads, real writes, real persistence
# ---------------------------------------------------------------------------
@pytest.fixture()
def temp_memory(monkeypatch, tmp_path):
    """An isolated UltronMemory rooted in a temp FULLAI_HOME."""
    from app.agents.sara_ultron_agent import api as ultron_api

    monkeypatch.setenv("FULLAI_HOME", str(tmp_path))
    memory = UltronMemory("sara_ultron_agent_test")
    monkeypatch.setattr(ultron_api, "get_memory", lambda: memory)
    return memory


def test_rule_roundtrip_through_the_api_is_persistent(temp_memory, tmp_path):
    from app.agents.sara_ultron_agent.api import MemoryRuleRequest, memory_rules, memory_state

    async def _call(req):
        return await memory_rules(req)

    added = asyncio.run(_call(MemoryRuleRequest(action="add", rule="Always reply in Hindi.")))
    assert added["changed"] is True
    rule_id = added["rules"][0]["id"]

    # Re-adding the identical rule is reported as a no-op, not a success.
    again = asyncio.run(_call(MemoryRuleRequest(action="add", rule="Always reply in Hindi.")))
    assert again["changed"] is False
    assert "already existed" in again["detail"]

    # A brand-new store instance over the same directory still sees it: this is
    # the restart test, done without restarting the process.
    reopened = UltronMemory("sara_ultron_agent_test")
    assert any(r["id"] == rule_id for r in reopened.list_rules_full())
    assert reopened.rules() == ["Always reply in Hindi."]

    state = asyncio.run(memory_state())
    assert state["rules"] == 1
    assert state["store"] == str(reopened.memory_dir)
    assert state["rule_records"][0]["origin"] == "user_correction"

    toggled = asyncio.run(_call(MemoryRuleRequest(action="toggle", rule_id=rule_id)))
    assert toggled["changed"] is True
    assert UltronMemory("sara_ultron_agent_test").list_rules_full()[0]["active"] is False

    deleted = asyncio.run(_call(MemoryRuleRequest(action="delete", rule_id=rule_id)))
    assert deleted["changed"] is True
    assert UltronMemory("sara_ultron_agent_test").rules() == []


def test_rule_actions_report_real_failures(temp_memory):
    from fastapi import HTTPException

    from app.agents.sara_ultron_agent.api import MemoryRuleRequest, memory_rules

    def _call(**kw):
        return asyncio.run(memory_rules(MemoryRuleRequest(**kw)))

    with pytest.raises(HTTPException) as missing_rule:
        _call(action="add", rule="   ")
    assert missing_rule.value.status_code == 400

    with pytest.raises(HTTPException) as unknown_id:
        _call(action="toggle", rule_id="nope")
    assert unknown_id.value.status_code == 404

    with pytest.raises(HTTPException) as unknown_action:
        _call(action="explode")
    assert unknown_action.value.status_code == 400


def test_experiences_are_read_back_from_disk(temp_memory):
    from app.agents.sara_ultron_agent.api import memory_state

    temp_memory.remember("open chrome and search for python tutorials",
                         [{"tool": "web_search", "args": {"query": "python"}}], True)
    temp_memory.remember("delete everything on my desktop", [], False, "Denied by the user.")

    state = asyncio.run(memory_state())
    assert state["experiences"] == 2
    assert state["success_rate"] == 0.5
    assert state["recent"][0]["goal"] == "delete everything on my desktop"
    assert state["recent"][0]["success"] is False
    assert state["recent"][0]["steps"] == 0
    # Bounded output: a management panel, not a dump.
    assert len(state["recent"]) <= 15


def test_corrupt_memory_file_is_survivable(temp_memory):
    """A hand-edited store must degrade to empty, never raise into the UI."""
    from app.agents.sara_ultron_agent.api import memory_state

    temp_memory.experience_file.write_text("{not json at all", encoding="utf-8")
    state = asyncio.run(memory_state())
    assert state["experiences"] == 0
    assert state["rules"] == 0


def test_snapshot_never_returns_memory_content(temp_memory):
    temp_memory.remember("a private goal the user typed", [], True)
    snapshot = temp_memory.snapshot()
    assert "private goal" not in json.dumps(snapshot)
    assert set(snapshot) == {"available", "memory_dir", "experiences", "rules", "success_rate"}


# ---------------------------------------------------------------------------
# 5. ultron_remember must not invent outcomes
# ---------------------------------------------------------------------------
def test_remember_without_an_outcome_stores_a_rule_not_a_run(temp_memory):
    """A goal with no reported outcome is a statement, not a finished run.

    This is the regression guard for a real defect: the handler used to default
    ``success`` to True, so "remember X" recorded a successful experience for a
    task that was never executed - inflating the success rate and injecting a
    fabricated memory into later plans.
    """
    reply = temp_memory.as_tool({"goal": "prefer replies in Hindi"})
    assert "standing rule" in reply.lower()
    assert temp_memory.experiences() == []
    assert temp_memory.rules() == ["prefer replies in Hindi"]


def test_remember_with_an_explicit_outcome_records_a_run(temp_memory):
    reply = temp_memory.as_tool({"goal": "tidy the desktop", "success": False,
                                 "errors": "permission denied on C:/Windows"})
    assert reply == "Experience recorded."
    stored = temp_memory.experiences()
    assert len(stored) == 1
    assert stored[0]["success"] is False
    assert temp_memory.rules() == []


def test_remember_rule_argument_wins(temp_memory):
    reply = temp_memory.as_tool({"goal": "anything", "rule": "always cite sources"})
    assert reply == "Rule learned."
    assert temp_memory.rules() == ["always cite sources"]
    assert temp_memory.experiences() == []


def test_remember_still_requires_a_goal(temp_memory):
    assert "non-empty 'goal'" in temp_memory.as_tool({})
    assert temp_memory.experiences() == []


def test_remember_accepts_the_aliases_a_planner_invents(temp_memory):
    """`action: "goal"` was observed from the live planner; it must not fail."""
    for alias in ("goal", "note", "preference", "remember_rule"):
        reply = temp_memory.as_tool({"action": alias,
                                     "goal": f"preference via {alias}"})
        assert reply == "Stored as a standing rule."
    assert len(temp_memory.rules()) == 4


def test_remember_reports_an_unknown_action_instead_of_guessing(temp_memory):
    reply = temp_memory.as_tool({"action": "delete_rules", "goal": "something"})
    assert "Unknown action" in reply
    assert temp_memory.rules() == []
    assert temp_memory.experiences() == []


def test_remember_rule_management_verbs_still_work(temp_memory):
    temp_memory.as_tool({"rule": "cite sources"})
    rule_id = temp_memory.list_rules_full()[0]["id"]
    assert temp_memory.as_tool({"action": "list_rules"}).startswith(rule_id)
    assert "toggled" in temp_memory.as_tool({"action": "toggle_rule",
                                              "rule_id": rule_id})
    assert temp_memory.rules() == []
    assert "deleted" in temp_memory.as_tool({"action": "delete_rule",
                                              "rule_id": rule_id})
    assert temp_memory.list_rules_full() == []


def test_stored_rules_drop_replacement_characters(temp_memory):
    """A mangled dash from an upstream gateway must not reach a prompt."""
    temp_memory.learn_rule("Prefer Hindi \ufffd but answer English questions")
    stored = temp_memory.rules()[0]
    assert "\ufffd" not in stored
    assert "Prefer Hindi" in stored and "English questions" in stored


# ---------------------------------------------------------------------------
# 6. Learned lessons must be readable, not raw payloads
# ---------------------------------------------------------------------------
def test_sanitize_lesson_strips_structural_noise():
    from app.agents.sara_ultron_agent.core.memory import sanitize_lesson

    raw = (
        'Delegation to "edulearner-ai" failed: HTTPException: 500: LLM Error: '
        '{"error":{"message":"This premium model requires an active paid plan '
        'or real deposited balance"}}'
    )
    clean = sanitize_lesson(raw)
    assert "{" not in clean and "}" not in clean and '"' not in clean
    assert "premium model requires" in clean
    assert "Delegation to edulearner-ai failed" in clean


def test_sanitize_lesson_masks_opaque_tokens_and_cuts_at_a_word():
    from app.agents.sara_ultron_agent.core.memory import sanitize_lesson

    masked = sanitize_lesson('key ' + 'a1b2c3d4' * 6 + ' rejected')
    assert 'a1b2c3d4a1b2c3d4' not in masked
    assert '[opaque]' in masked
    assert 'rejected' in masked

    long_text = sanitize_lesson("word " * 200)
    assert len(long_text) <= 201  # limit plus the ellipsis
    assert long_text.endswith("…")
    assert not long_text.endswith(" …")  # never a dangling separator


def test_auto_learn_rejects_unreadable_lessons(temp_memory):
    # A payload with nothing readable left after sanitising is not a lesson.
    assert temp_memory.auto_learn("a goal", '{"a":1}') is False
    assert temp_memory.auto_learn("a goal", "{" + "}" * 40) is False
    assert temp_memory.rules() == []


def test_auto_learn_stores_payloads_without_the_payload(temp_memory):
    """A JSON-enveloped failure is learned as prose, never as a raw payload.

    Observed for real before the fix: a learned rule contained half of an
    upstream error envelope, braces and all, and that rule was then injected
    into every subsequent planning prompt.
    """
    ok = temp_memory.auto_learn(
        "ask edulearner for a study plan",
        'ultron_delegate: HTTPException: 500: LLM Error: '
        '{"error":{"message":"This premium model requires an active paid plan"}}',
    )
    assert ok is True
    rule = temp_memory.rules()[0]
    assert "premium model requires an active paid plan" in rule
    for forbidden in '{', '}', '"', '\\':
        assert forbidden not in rule
    assert len(rule) < 300


def test_auto_learn_still_learns_a_real_failure(temp_memory):
    ok = temp_memory.auto_learn(
        "convert the spreadsheet",
        "file_controller: Could not open budget.xlsx because the workbook is locked",
    )
    assert ok is True
    rules = temp_memory.rules()
    assert len(rules) == 1
    assert "locked" in rules[0]
    assert "{ " not in rules[0]


def test_auto_learn_ignores_user_decisions_and_transients(temp_memory):
    assert temp_memory.auto_learn("a goal", "Denied by the user. Do not retry.") is False
    assert temp_memory.auto_learn("a goal", "the request timed out, try again") is False
    assert temp_memory.rules() == []


# ---------------------------------------------------------------------------
# 7. Memory cleanup
# ---------------------------------------------------------------------------
def test_forget_removes_exactly_one_experience(temp_memory):
    temp_memory.remember("first run", [], True)
    temp_memory.remember("second run", [], True)
    temp_memory.remember("third run", [], False, "failed")
    middle = temp_memory.experiences()[1]

    assert temp_memory.forget(middle["timestamp"], middle["goal"]) is True
    remaining = [e["goal"] for e in temp_memory.experiences()]
    assert remaining == ["first run", "third run"]
    # Re-reading with a fresh instance proves the write reached disk.
    assert [e["goal"] for e in UltronMemory("sara_ultron_agent_test").experiences()] \
        == ["first run", "third run"]


def test_forget_misses_are_reported_not_faked(temp_memory):
    temp_memory.remember("the only run", [], True)
    assert temp_memory.forget(123456.0) is False
    missing_goal = temp_memory.experiences()[0]
    assert temp_memory.forget(missing_goal["timestamp"], "a different goal") is False
    assert len(temp_memory.experiences()) == 1


def test_forget_route_reports_a_real_404(temp_memory):
    from fastapi import HTTPException

    from app.agents.sara_ultron_agent.api import MemoryForgetRequest, memory_forget

    with pytest.raises(HTTPException) as missing:
        asyncio.run(memory_forget(MemoryForgetRequest(timestamp=1.0)))
    assert missing.value.status_code == 404

    temp_memory.remember("forgettable", [], True)
    stamp = temp_memory.experiences()[0]["timestamp"]
    result = asyncio.run(memory_forget(MemoryForgetRequest(timestamp=stamp)))
    assert result["changed"] is True
    assert temp_memory.experiences() == []
