"""Sara Bots skills tests â€” store, validation, owner isolation."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.services.sara_bots.skills import (
    create_skill, delete_skill, get_skill, list_skills,
    skill_instructions, update_skill, validate_skill,
)


FAKE_TOOL_IDS = {"action.hello_world", "action.write_file", "llm.reason"}


@pytest.fixture()
def tmp_skills(tmp_path, monkeypatch):
    import app.services.sara_bots.skills as mod
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "SKILLS_FILE", tmp_path / "skills.json")
    return tmp_path


def test_create_and_list(tmp_skills):
    sk = create_skill("local", {
        "name": "Researcher", "description": "researches",
        "instructions": "Use web_search then summarize.",
        "required_tools": ["action.hello_world"],
        "version": "1.0.0",
    }, FAKE_TOOL_IDS)
    assert sk["id"].startswith("skl_")
    assert sk["enabled"] is True
    assert list_skills("local") == [sk] or list_skills("local")[0]["id"] == sk["id"]
    # owner isolation
    assert list_skills("alice") == []


def test_unknown_tool_rejected(tmp_skills):
    with pytest.raises(ValueError):
        validate_skill({
            "name": "X", "description": "d", "instructions": "i",
            "required_tools": ["does.not.exist"],
        }, FAKE_TOOL_IDS)


def test_update_and_delete(tmp_skills):
    sk = create_skill("local", {
        "name": "AA", "description": "d", "instructions": "i",
    }, FAKE_TOOL_IDS)
    updated = update_skill(sk["id"], "local",
                          {"name": "A2", "instructions": "i2"}, FAKE_TOOL_IDS)
    assert updated["name"] == "A2"
    assert updated["instructions"] == "i2"
    assert delete_skill(sk["id"], "local") is True
    assert get_skill(sk["id"], "local") is None


def test_owner_isolation(tmp_skills):
    sk = create_skill("owner1", {
        "name": "O1", "description": "d", "instructions": "i",
    }, FAKE_TOOL_IDS)
    # other owner cannot see/update/delete
    assert get_skill(sk["id"], "owner2") is None
    with pytest.raises(PermissionError):
        update_skill(sk["id"], "owner2", {"name": "x"}, FAKE_TOOL_IDS)
    with pytest.raises(PermissionError):
        delete_skill(sk["id"], "owner2")
    # original intact
    assert get_skill(sk["id"], "owner1") is not None


def test_skill_instructions_filters_disabled_and_unknown(tmp_skills):
    sk = create_skill("local", {
        "name": "SS", "description": "d", "instructions": "do X",
        "enabled": True,
    }, FAKE_TOOL_IDS)
    # enabled skill contributes
    out = skill_instructions([sk["id"], "skl_nope"], "local")
    assert "do X" in out
    # disable it
    update_skill(sk["id"], "local", {"enabled": False}, FAKE_TOOL_IDS)
    out2 = skill_instructions([sk["id"]], "local")
    assert out2 == ""


def test_validate_requires_instructions(tmp_skills):
    with pytest.raises(ValueError):
        validate_skill({"name": "X", "description": "d", "instructions": ""},
                      FAKE_TOOL_IDS)
    with pytest.raises(ValueError):
        validate_skill({"name": "X", "description": "d"}, FAKE_TOOL_IDS)


def test_create_skill_validation_error(tmp_skills):
    with pytest.raises(ValueError):
        create_skill("local", {"name": "X", "description": "d",
                              "instructions": "i",
                              "required_tools": ["nope"]}, FAKE_TOOL_IDS)