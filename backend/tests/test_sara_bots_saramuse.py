"""SaraMuse fleet-card tests — the OpenMuse-powered primary agent's presence
in the fleet registry must be a live view of the real custom bot: honest
state, real deep-link, no card when the bot does not exist.
"""
from __future__ import annotations

import pytest

from app.services.sara_bots import registry as sb_registry
from app.services.sara_bots.registry import SARAMUSE_ID, SaraBotsRegistry


class _StubStore:
    def __init__(self, bots):
        self._bots = bots

    def list(self, owner=None):
        return self._bots


def _saramuse_record(status="active", tools=None):
    return {
        "id": "cbot_test0001",
        "name": "SaraMuse",
        "owner": "local",
        "status": status,
        "description": "OpenMuse-powered autonomous agent for tests.",
        "hue": 260,
        "tools": tools or ["browser.open", "agent.ask_owner"],
    }


@pytest.fixture
def reg(tmp_path):
    return SaraBotsRegistry(store=sb_registry.BotStateStore(tmp_path / "state.json"))


def _seed(monkeypatch, bots):
    monkeypatch.setattr("app.services.sara_bots.custom.get_custom_store",
                        lambda: _StubStore(bots))


def _engine_status(monkeypatch, payload):
    monkeypatch.setattr("app.services.sara_bots.engine.get_engine",
                        lambda: _StubEngine(payload))


class _StubEngine:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def test_no_saramuse_bot_means_no_card(reg, monkeypatch):
    _seed(monkeypatch, [])
    assert reg.saramuse_bot() is None
    assert SARAMUSE_ID not in reg.bot_states({})


def test_card_is_a_live_view_of_the_real_bot(reg, monkeypatch):
    _seed(monkeypatch, [_saramuse_record()])
    spec = reg.saramuse_bot()
    assert spec is not None
    assert spec.id == SARAMUSE_ID
    assert spec.kind == "hub"            # → visible under "All" + "Hub agents"
    assert spec.route == "/hub/sara-muse"   # the OpenMuse-style workspace
    assert spec.adapter is None          # runs from its real workspace
    # bot() resolves fresh from the live store on every call (by design):
    resolved = reg.bot(SARAMUSE_ID)
    assert resolved is not None
    assert resolved.id == spec.id
    assert resolved.route == spec.route


def test_state_active_engine_live(reg, monkeypatch):
    _seed(monkeypatch, [_saramuse_record(tools=["a", "b", "c"])])
    _engine_status(monkeypatch, {"engine_running": True, "llm_ready": True,
                                 "active": 2})
    states = reg.bot_states({})
    block = states[SARAMUSE_ID]
    assert block["state"] == "available"
    assert "engine live" in block["detail"]
    assert "3 granted powers" in block["detail"]


def test_state_paused_bot(reg, monkeypatch):
    _seed(monkeypatch, [_saramuse_record(status="paused")])
    _engine_status(monkeypatch, {"engine_running": True, "llm_ready": True})
    block = reg.bot_states({})[SARAMUSE_ID]
    assert block["state"] == "needs_configuration"
    assert "paused" in block["detail"]


def test_state_engine_down_or_no_llm(reg, monkeypatch):
    _seed(monkeypatch, [_saramuse_record()])
    _engine_status(monkeypatch, {"engine_running": False, "llm_ready": True})
    assert reg.bot_states({})[SARAMUSE_ID]["state"] == "unavailable"
    _engine_status(monkeypatch, {"engine_running": True, "llm_ready": False})
    block = reg.bot_states({})[SARAMUSE_ID]
    assert block["state"] == "needs_configuration"
    assert "LLM" in block["detail"]


def test_broken_store_never_breaks_the_fleet(reg, monkeypatch):
    def boom():
        raise RuntimeError("store unreadable")

    monkeypatch.setattr("app.services.sara_bots.custom.get_custom_store", boom)
    assert reg.saramuse_bot() is None
    assert SARAMUSE_ID not in reg.bot_states({})
