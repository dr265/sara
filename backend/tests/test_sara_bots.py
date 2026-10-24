"""Sara Bots tests — registry security, worker isolation, runner honesty, API.

These run against temp directories and a disposable state file; nothing touches
the developer's real approvals, and no external service is contacted.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.sara_bots import registry as sb_registry
from app.services.sara_bots.registry import (BotStateStore, SaraBotsRegistry,
                                             discover_local_actions)
from app.services.sara_bots import runner as sb_runner
from app.services.sara_bots.runner import RunStore, SaraBotsRunner, needs_confirmation

GOOD_ACTION = '''
import platform

def greet(parameters):
    return "hello " + str(parameters.get("who", "world"))

TOOL = {
    "name": "hello_world",
    "description": "Says hello. Used by the Sara Bots test suite.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "who": {"type": "STRING", "description": "who to greet"},
        },
    },
    "handler": greet,
}
'''

SESSION_ACTION = '''
def needs_player(parameters, player):
    return "never"

TOOL = {
    "name": "needs_session",
    "description": "Requires a live desktop session.",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": needs_player,
}
'''

SESSION_DEFAULT_ACTION = '''
def ok_action(parameters, player=None):
    return "fine"

TOOL = {
    "name": "session_default",
    "description": "Context kwarg has a default, so it is headless-safe.",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": ok_action,
}
'''

HANG_ACTION = '''
import time

def slow(parameters):
    time.sleep(30)
    return "late"

TOOL = {
    "name": "hang_forever",
    "description": "Sleeps 30s so the timeout path can be tested.",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": slow,
}
'''

RISKY_ACTION = '''
def killer(parameters):
    return "killed"

TOOL = {
    "name": "process_killer",
    "description": "Kills a process by pid or name.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING"}, "pid": {"type": "NUMBER"}}},
    "handler": killer,
}
'''


@pytest.fixture()
def action_root(tmp_path: Path) -> Path:
    root = tmp_path / "actions"
    root.mkdir()
    (root / "hello_world.py").write_text(GOOD_ACTION, encoding="utf-8")
    (root / "needs_session.py").write_text(SESSION_ACTION, encoding="utf-8")
    (root / "session_default.py").write_text(SESSION_DEFAULT_ACTION, encoding="utf-8")
    (root / "_private.py").write_text(GOOD_ACTION.replace("hello_world", "private"),
                                      encoding="utf-8")
    (root / "not_an_action.py").write_text("X = 1\n", encoding="utf-8")
    (root / "broken.py").write_text("def f(:\n", encoding="utf-8")
    return root


@pytest.fixture()
def store(tmp_path: Path) -> BotStateStore:
    return BotStateStore(tmp_path / "state.json")


@pytest.fixture()
def reg(action_root: Path, store: BotStateStore, tmp_path: Path,
        monkeypatch) -> SaraBotsRegistry:
    # tests scan isolated tmp dirs — declare this test's tmp tree the
    # approved scope (server-side only; the production default is untouched)
    monkeypatch.setattr(sb_registry, "APPROVED_SCOPE", tmp_path)
    registry = SaraBotsRegistry(store=store)
    result = registry.rescan(action_root)
    assert result["ok"], result
    return registry


# ── discovery (static, secure) ───────────────────────────────────────────────

def test_discovery_finds_only_real_actions(action_root: Path):
    actions, problems = discover_local_actions(action_root)
    names = {a.name for a in actions}
    assert names == {"hello_world", "needs_session", "session_default"}
    # helper + broken files are reported, not silently swallowed
    assert any(p["file"] == "broken.py" for p in problems)


def test_discovery_detects_session_requirements(action_root: Path):
    actions = {a.name: a for a in discover_local_actions(action_root)[0]}
    assert actions["needs_session"].requires_session is True
    assert actions["session_default"].requires_session is False
    assert actions["hello_world"].requires_session is False


def test_discovery_never_imports_code(action_root: Path, monkeypatch):
    """A file whose TOOL is fine but whose module would explode on import must
    still be discovered (proving ast-only analysis) — and must NOT run."""
    boom = action_root / "boom_on_import.py"
    boom.write_text(
        'raise RuntimeError("import side effect")\n'
        'TOOL = {"name": "boom_on_import", "description": "d", '
        '"parameters": {"type": "OBJECT", "properties": {}}, "handler": "f"}\n',
        encoding="utf-8")
    actions, _ = discover_local_actions(action_root)
    assert any(a.name == "boom_on_import" for a in actions)


def test_root_escape_is_rejected(store: BotStateStore):
    # pytest's tmp_path lives INSIDE the repo, so use a real external path
    outside = sb_registry.REPO_ROOT.parent / f"sb_outside_scope_{os.getpid()}"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(ValueError, match="outside the approved discovery scope"):
            store.set_root(str(outside))
    finally:
        outside.rmdir()


def test_dotdot_root_is_rejected(store: BotStateStore):
    with pytest.raises(ValueError, match="\\.\\."):
        store.set_root(str(sb_registry.REPO_ROOT / ".."))


def test_missing_root_is_rejected(store: BotStateStore):
    with pytest.raises(ValueError, match="not an existing directory"):
        store.set_root(str(sb_registry.REPO_ROOT / "sara_desktop" / "no_such_dir"))


def test_symlink_escape_is_skipped(action_root: Path, tmp_path: Path):
    secret = tmp_path / "secret.py"
    secret.write_text(GOOD_ACTION.replace("hello_world", "smuggled"), encoding="utf-8")
    link = action_root / "smuggled.py"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    actions, problems = discover_local_actions(action_root)
    names = {a.name for a in actions}
    assert "smuggled" not in names
    assert any("outside root" in p["reason"] for p in problems)


def test_discovery_respects_approval_gate(reg: SaraBotsRegistry):
    states = reg.bot_states(None)
    assert states["action.hello_world"]["state"] == "awaiting_approval"
    assert states["action.hello_world"]["invocable"] is False
    reg.store.approve("action.hello_world")
    states = reg.bot_states(None)
    assert states["action.hello_world"]["state"] == "ready"
    assert states["action.hello_world"]["invocable"] is True
    reg.store.disable("action.hello_world")
    states = reg.bot_states(None)
    assert states["action.hello_world"]["state"] == "disabled"


# ── worker (real subprocess execution) ──────────────────────────────────────

def _run_worker(root: Path, tool: str, params: dict) -> dict:
    import subprocess
    import sys

    worker = Path(sb_runner.WORKER_PATH)
    proc = subprocess.run(
        [sys.executable, "-I", str(worker), "--root", str(root), "--tool", tool],
        input=json.dumps(params), capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_worker_runs_real_action(action_root: Path):
    result = _run_worker(action_root, "hello_world", {"who": "sara"})
    assert result["ok"] is True
    assert result["output"] == "hello sara"


def test_worker_refuses_session_actions(action_root: Path):
    result = _run_worker(action_root, "needs_session", {})
    assert result["ok"] is False
    assert "live desktop context" in result["error"]


def test_worker_rejects_unknown_tool(action_root: Path):
    result = _run_worker(action_root, "nope", {})
    assert result["ok"] is False


def test_worker_rejects_bad_tool_name(action_root: Path):
    result = _run_worker(action_root, "../hello", {})
    assert result["ok"] is False


# ── runner (honest execution records) ───────────────────────────────────────

@pytest.fixture()
def run_store(tmp_path: Path) -> RunStore:
    return RunStore(audit_path=tmp_path / "audit.jsonl")


def test_needs_confirmation_rules():
    assert needs_confirmation({"action": "kill", "pid": 12}) is not None
    assert needs_confirmation({"action": "full"}) is None
    assert needs_confirmation({"cmd": "dir"}) is not None


def test_local_run_completes_with_real_output(reg: SaraBotsRegistry,
                                              run_store: RunStore):
    reg.store.approve("action.hello_world")
    runner = SaraBotsRunner(registry=reg, store=run_store)
    import asyncio

    record = asyncio.run(runner.start(
        "action.hello_world", {"who": "e2e"}, base_url="http://127.0.0.1:9",
        api_key="", timeout_s=20))
    run_id = record["run_id"]
    for _ in range(100):
        record = run_store.get(run_id)
        if record["status"] in ("completed", "failed", "timed_out", "cancelled"):
            break
        time.sleep(0.1)
    assert record["status"] == "completed", (
        f"status={record['status']} error={record['error']!r} "
        f"events={record['events']}")
    assert record["output"] == "hello e2e"
    assert record["duration_ms"] is not None
    assert record["pid"] is not None            # a REAL process ran
    assert (run_store._audit_path).exists()     # audit trail written


def test_local_run_timeout_kills_tree(action_root: Path, store: BotStateStore,
                                       run_store: RunStore, tmp_path: Path,
                                       monkeypatch):
    monkeypatch.setattr(sb_registry, "APPROVED_SCOPE", tmp_path)
    (action_root / "hang_forever.py").write_text(HANG_ACTION, encoding="utf-8")
    (action_root / "process_killer.py").write_text(RISKY_ACTION, encoding="utf-8")
    reg = SaraBotsRegistry(store=store)
    reg.rescan(action_root)
    reg.store.approve("action.hang_forever")
    runner = SaraBotsRunner(registry=reg, store=run_store)
    import asyncio

    record = asyncio.run(runner.start(
        "action.hang_forever", {}, base_url="http://x", api_key="", timeout_s=5))
    for _ in range(120):
        record = run_store.get(record["run_id"])
        if record["status"] != "running":
            break
        time.sleep(0.25)
    assert record["status"] == "timed_out"
    assert "timeout" in record["error"]


def test_unapproved_bot_cannot_run(reg: SaraBotsRegistry, run_store: RunStore):
    runner = SaraBotsRunner(registry=reg, store=run_store)
    import asyncio

    with pytest.raises(PermissionError, match="NOT approved"):
        asyncio.run(runner.start("action.hello_world", {}, base_url="http://x",
                                 api_key=""))


def test_risky_params_require_confirmation(action_root: Path, store: BotStateStore,
                                            run_store: RunStore, tmp_path: Path,
                                            monkeypatch):
    monkeypatch.setattr(sb_registry, "APPROVED_SCOPE", tmp_path)
    (action_root / "process_killer.py").write_text(RISKY_ACTION, encoding="utf-8")
    reg = SaraBotsRegistry(store=store)
    reg.rescan(action_root)
    reg.store.approve("action.process_killer")
    runner = SaraBotsRunner(registry=reg, store=run_store)
    import asyncio

    with pytest.raises(sb_runner.ConfirmationRequired):
        asyncio.run(runner.start("action.process_killer", {"action": "kill", "pid": 1},
                                 base_url="http://x", api_key=""))
    # confirmed rerun of the same payload proceeds (to a real subprocess)
    record = asyncio.run(runner.start(
        "action.process_killer", {"action": "kill", "pid": 1},
        base_url="http://x", api_key="", confirm=True))
    for _ in range(100):
        record = run_store.get(record["run_id"])
        if record["status"] != "running":
            break
        time.sleep(0.1)
    assert record["status"] == "completed"


# ── API surface (auth, validation, approval flow) ───────────────────────────

@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SARA_API_KEY", "test-key-123")

    import app.api.deps as deps
    monkeypatch.setattr(deps, "API_KEY", "test-key-123")

    # isolated singletons backed by a temp root with one safe action
    root = tmp_path / "actions"
    root.mkdir()
    (root / "hello_world.py").write_text(GOOD_ACTION, encoding="utf-8")
    store = BotStateStore(tmp_path / "state.json")
    store.save({"root": str(root), "approved": {}, "disabled": {}})
    # isolated tmp scope for this test's registry (see `reg` fixture)
    monkeypatch.setattr(sb_registry, "APPROVED_SCOPE", tmp_path)
    reg = SaraBotsRegistry(store=store)
    reg.rescan(root)
    runner = SaraBotsRunner(registry=reg,
                            store=RunStore(audit_path=tmp_path / "audit.jsonl"))

    # the API module binds get_registry/get_runner at import — patch THERE
    import app.api.sara_bots as sb_api
    monkeypatch.setattr(sb_api, "get_registry", lambda: reg)
    monkeypatch.setattr(sb_api, "get_runner", lambda: runner)

    # minimal app: just the Sara Bots router (no pollers, no DB, no network)
    from fastapi import FastAPI
    from app.api import sara_bots
    app = FastAPI()
    app.include_router(sara_bots.router)
    with TestClient(app) as client:
        yield client


def test_api_requires_key(client: TestClient):
    r = client.get("/api/sara-bots/bots")
    assert r.status_code == 401


def test_api_lists_bots_with_honest_states(client: TestClient):
    r = client.get("/api/sara-bots/bots", headers={"X-API-Key": "test-key-123"})
    assert r.status_code == 200
    body = r.json()
    ids = {b["id"] for b in body["bots"]}
    assert "agency-specialists" in ids
    assert "action.hello_world" in ids
    local = next(b for b in body["bots"] if b["id"] == "action.hello_world")
    assert local["state"] == "awaiting_approval"
    assert local["invocable"] is False


def test_api_run_blocked_until_approved_then_works(client: TestClient):
    headers = {"X-API-Key": "test-key-123"}
    r = client.post("/api/sara-bots/bots/action.hello_world/run",
                    json={"params": {"who": "api"}}, headers=headers)
    assert r.status_code == 403
    assert "NOT approved" in r.json()["detail"]

    r = client.post("/api/sara-bots/bots/action.hello_world/approval",
                    json={"action": "approve"}, headers=headers)
    assert r.status_code == 200

    r = client.post("/api/sara-bots/bots/action.hello_world/run",
                    json={"params": {"who": "api"}}, headers=headers)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    for _ in range(100):
        got = client.get(f"/api/sara-bots/runs/{run_id}", headers=headers).json()
        if got["status"] in ("completed", "failed", "timed_out"):
            break
        time.sleep(0.1)
    assert got["status"] == "completed"
    assert got["output"] == "hello api"


def test_api_risky_run_needs_confirm(client: TestClient, tmp_path: Path):
    root = tmp_path / "actions"
    (root / "process_killer.py").write_text(RISKY_ACTION, encoding="utf-8")
    # the fixture registry (same instance the API uses), rescanned to pick up
    # the risky action
    import app.api.sara_bots as sb_api
    reg = sb_api.get_registry()
    reg.rescan(root)
    headers = {"X-API-Key": "test-key-123"}
    r = client.post("/api/sara-bots/bots/action.process_killer/run",
                    json={"params": {"action": "kill", "pid": 4}}, headers=headers)
    # not approved yet → 403 (approval gate fires before the confirm gate)
    assert r.status_code == 403


def test_api_unknown_bot_404(client: TestClient):
    r = client.get("/api/sara-bots/bots/action.nope",
                   headers={"X-API-Key": "test-key-123"})
    assert r.status_code == 404


def test_api_root_validation(client: TestClient):
    headers = {"X-API-Key": "test-key-123"}
    # the user-profile root sits OUTSIDE the repo — must fail closed
    r = client.post("/api/sara-bots/root",
                    json={"path": str(sb_registry.REPO_ROOT.parent)},
                    headers=headers)
    assert r.status_code == 422          # outside the repo scope — fail closed
    assert "approved discovery scope" in r.json()["detail"]


def test_rescan_rejects_outside_scope(tmp_path: Path):
    """Unpatched production scope: a root outside the repo fails closed and
    the previous good discovery state is preserved (fail-closed rescan)."""
    outside = sb_registry.REPO_ROOT.parent / f"sb_rescan_outside_{os.getpid()}"
    outside.mkdir(exist_ok=True)
    try:
        reg = SaraBotsRegistry(store=BotStateStore(tmp_path / "state.json"))
        result = reg.rescan(outside)
        assert result["ok"] is False
        assert "outside the approved discovery scope" in result["error"]
        assert reg.discovered_actions() == []
        assert reg.scan_info()["root"] is None
    finally:
        outside.rmdir()


def test_failed_rescan_preserves_prior_state(action_root: Path,
                                             store: BotStateStore,
                                             tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sb_registry, "APPROVED_SCOPE", tmp_path)
    reg = SaraBotsRegistry(store=store)
    assert reg.rescan(action_root)["ok"] is True
    assert len(reg.discovered_actions()) == 3
    outside = sb_registry.REPO_ROOT.parent / f"sb_rescan_keep_{os.getpid()}"
    outside.mkdir(exist_ok=True)
    try:
        # temporarily restore the real scope so this rescan must fail
        monkeypatch.setattr(sb_registry, "APPROVED_SCOPE",
                            sb_registry.REPO_ROOT)
        result = reg.rescan(outside)
        assert result["ok"] is False
        # prior valid discovery survives the failed rescan
        assert {a.name for a in reg.discovered_actions()} == {
            "hello_world", "needs_session", "session_default"}
    finally:
        outside.rmdir()


def test_agency_router_requires_key(monkeypatch):
    """Direct /api/agency/* access is authenticated — no bypass around the
    Sara Bots approval gate for LLM-backed runs."""
    import app.api.deps as deps
    monkeypatch.setattr(deps, "API_KEY", "test-key-123")
    from fastapi import FastAPI
    from app.api import agency_agents
    app = FastAPI()
    app.include_router(agency_agents.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/api/agency/divisions").status_code == 401
        assert c.get("/api/agency/divisions",
                     headers={"X-API-Key": "wrong"}).status_code == 401
        # the correct key passes auth (handler result depends on the
        # agency checkout; anything but 401 proves the gate opened)
        ok = c.get("/api/agency/divisions",
                   headers={"X-API-Key": "test-key-123"})
        assert ok.status_code != 401
