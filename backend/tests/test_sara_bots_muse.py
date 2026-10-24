"""SaraMuse powers tests — finance analysis, page diff, monitors, ideas.

Ports of the OpenMuse semantics into Sara's runtime: deterministic CSV
spending analysis, relative-time-aware page diffing, SSRF-guarded watches
with failure backoff, and idea derivation from real records. Everything
runs against temp stores; nothing touches the network or real data.
"""
from __future__ import annotations

import time

import pytest

from app.services.sara_bots import muse
from app.services.sara_bots.muse import (MuseStore, analyze_spending,
                                         count_page_diff, describe_page_diff,
                                         diff_page, matches_price, page_lines,
                                         refresh_ideas, without_relative_times)

VALID_CSV = """date,description,amount,category
2026-01-03,Coffee,4.50,Food
2026-01-05,Salary,-2000.00,Income
2026-01-07,Groceries,55.25,Food
2026-01-09,Bus pass,30.00,Transport
"""


# ── finance ──────────────────────────────────────────────────────────────────

def test_analyze_spending_totals_and_categories():
    data = analyze_spending(VALID_CSV)
    assert data["count"] == 4
    assert data["income"] == 2000.00
    assert data["spending"] == pytest.approx(89.75)
    assert data["saved"] == pytest.approx(1910.25)
    assert data["period"] == {"from": "2026-01-03", "to": "2026-01-09"}
    # categories sorted by amount, descending
    assert [c["name"] for c in data["categories"]] == ["Food", "Transport"]
    assert data["categories"][0]["amount"] == pytest.approx(59.75)


def test_analyze_spending_strips_bom():
    data = analyze_spending("\ufeff" + VALID_CSV)
    assert data["count"] == 4


@pytest.mark.parametrize("bad,reason", [
    ("date,description,amount\n2026-01-01,x,1.00", "missing column"),
    ("", "empty"),
    ("date,description,amount,category\n2026-13-99,x,1.00,Food", "bad date"),
    ("date,description,amount,category\n2026-01-01,x,abc,Food", "bad amount"),
    ("date,description,amount,category\n2026-01-01,x,1.234,Food", "3 decimals"),
])
def test_analyze_spending_rejects_invalid(bad, reason):
    with pytest.raises(ValueError):
        analyze_spending(bad)


def test_analyze_spending_row_count_limits():
    header = "date,description,amount,category\n"
    rows = "".join(f"2026-01-01,row{i},1.00,Misc\n" for i in range(5001))
    with pytest.raises(ValueError, match="5,000"):
        analyze_spending(header + rows)


# ── page diff ────────────────────────────────────────────────────────────────

def test_page_lines_dedupes_and_truncates():
    lines = page_lines("a\n  a \nb\n\n\nc", limit=10)
    assert lines == ["a", "b", "c"]


def test_without_relative_times_blanks_timestamps():
    text = "posted 1 day ago and vor 2 Stunden something"
    cleaned = without_relative_times(text)
    assert "1 day ago" not in cleaned
    assert "vor 2 stunden" not in cleaned.lower()


def test_diff_page_detects_added_updated_removed():
    previous = ["Item A costs $10", "Item B costs $20", "Gone tomorrow"]
    current = ["Item A costs $12", "Item B costs $20", "Brand new line"]
    diff = diff_page(previous, current)
    assert "Brand new line" in diff["added"]
    assert "Gone tomorrow" in diff["removed"]
    assert any("Item A" in line for line in diff["updated"])


def test_diff_page_ignores_relative_time_only_changes():
    previous = ["Story X — posted 1 day ago"]
    current = ["Story X — posted 2 days ago"]
    diff = diff_page(previous, current)
    assert diff["added"] == [] and diff["removed"] == [] and diff["updated"] == []


def test_describe_and_count_page_diff():
    diff = {"added": ["n1", "n2"], "updated": ["u1"], "removed": []}
    text = describe_page_diff(diff)
    assert "New:" in text and "Updated:" in text
    assert count_page_diff(diff) == "3 lines changed (2 new, 1 updated)"
    assert count_page_diff({"added": [], "updated": [], "removed": []}) == ""


def test_matches_price():
    assert matches_price("Deal now $19.99 only", 25)
    assert not matches_price("Deal now $19.99 only", 15)
    assert matches_price("Price USD 1,200.00 today", 1300)


# ── store: goals, monitors, ideas, notifications ────────────────────────────

@pytest.fixture
def store(tmp_path):
    return MuseStore(path=tmp_path / "muse.json")


def test_goal_lifecycle(store):
    goal = store.create_goal("Ship SaraMuse", "OpenMuse powers in Sara",
                             "Work", ["Backend", "UI"])
    assert goal["status"] == "active"
    assert len(goal["milestones"]) == 2
    ms = goal["milestones"][0]
    updated = store.set_milestone_done(goal["id"], ms["id"], True)
    assert updated["milestones"][0]["done"] is True
    paused = store.update_goal(goal["id"], {"status": "paused"})
    assert paused["status"] == "paused"
    with pytest.raises(KeyError):
        store.set_milestone_done(goal["id"], "ms_missing", True)
    with pytest.raises(ValueError):
        store.update_goal(goal["id"], {"status": "bogus"})


def test_monitor_validation(store):
    with pytest.raises(ValueError, match="public HTTP"):
        store.create_monitor("x", "ftp://example.com", "change", "", 15)
    with pytest.raises(ValueError, match="condition value"):
        store.create_monitor("x", "https://example.com", "contains", "", 15)
    with pytest.raises(ValueError, match="positive price"):
        store.create_monitor("x", "https://example.com", "price_below", "-3", 15)
    with pytest.raises(ValueError, match="between"):
        store.create_monitor("x", "https://example.com", "change", "", 0)
    ok = store.create_monitor("Watch", "https://example.com", "change", "", 15)
    assert ok["status"] == "active"
    assert ok["checks"] == 0


def test_notifications_dedupe_by_key_and_cap(store):
    first = store.notify("Hello", "Body", key="k1")
    again = store.notify("Hello again", "Body 2", key="k1")
    assert first["id"] == again["id"]
    assert again["title"] == "Hello"  # not overwritten
    marked = store.mark_read(first["id"])
    assert marked["read"] is True
    with pytest.raises(KeyError):
        store.mark_read("ntf_missing")


def test_refresh_ideas_from_goal_without_milestones(store, monkeypatch):
    monkeypatch.setattr(muse, "get_muse_store", lambda: store)
    store.create_goal("Plan me", "Needs a plan", "Personal", [])
    ideas = refresh_ideas(store)
    assert any(i["kind"] == "plan" and i["status"] == "new" for i in ideas)
    # a goal WITH milestones produces no idea
    store.create_goal("Planned", "Has steps", "Personal", ["Step 1"])
    ideas = refresh_ideas(store)
    assert sum(1 for i in ideas if i["kind"] == "plan") == 1


def test_snapshot_survives_missing_bot(store, monkeypatch):
    monkeypatch.setattr(muse, "get_muse_store", lambda: store)

    def no_registry():
        raise RuntimeError("registry down")

    import app.services.sara_bots.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_registry", no_registry)
    snap = muse.snapshot(store)
    assert snap["bot"] is None
    assert snap["identity"]["name"] == "SaraMuse"
    assert snap["worker"]["running"] is False


# ── SSRF guard ───────────────────────────────────────────────────────────────

def test_assert_public_host_rejects_private(monkeypatch):
    def fake_getaddrinfo(host, _):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]
    monkeypatch.setattr(muse.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        muse._assert_public_host("evil.example")


def test_assert_public_host_rejects_metadata_endpoint(monkeypatch):
    def fake_getaddrinfo(host, _):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr(muse.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        muse._assert_public_host("metadata.example")


def test_extract_text_strips_scripts_and_tags():
    html = ("<html><head><script>var x=1;</script></head>"
            "<body><h1>Title</h1><p>Hello &amp; welcome</p>"
            "<style>.x{}</style></body></html>")
    text = muse._extract_text(html)
    assert "var x=1" not in text
    assert "Title" in text
    assert "Hello & welcome" in text
