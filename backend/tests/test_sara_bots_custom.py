"""Sara Bots custom-worker tests — stores, engine, scheduler, planner, API.

Hermetic: temp dirs, fake tool registry, fake executor seam, no network, no
real LLM. The production code paths under test are the exact ones the backend
wires into the lifespan.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.sara_bots.custom import CustomBotStore, validate_config
from app.services.sara_bots.scheduler import SaraBotsScheduler, ScheduleStore
from app.services.sara_bots.tasks import TaskStore

FAKE_TOOLS = [
    {"id": "action.hello_world", "name": "Hello World", "kind": "action",
     "description": "says hello", "params": {}, "risk": "read",
     "state": "ready", "detail": "approved local action", "requires": [],
     "runnable": True},
    {"id": "action.write_file", "name": "Write File", "kind": "action",
     "description": "writes a file", "params": {}, "risk": "write",
     "state": "ready", "detail": "approved local action", "requires": [],
     "runnable": True},
    {"id": "hub.trading", "name": "Trading", "kind": "hub",
     "description": "trades something", "params": {}, "risk": "external",
     "state": "ready", "detail": "online", "requires": [], "runnable": True},
    {"id": "llm.reason", "name": "Reason", "kind": "llm",
     "description": "reasons", "params": {}, "risk": "llm",
     "state": "ready", "detail": "chain online", "requires": [],
     "runnable": True},
    {"id": "memory.note", "name": "Remember", "kind": "memory",
     "description": "remembers", "params": {}, "risk": "write",
     "state": "ready", "detail": "scoped", "requires": [], "runnable": True},
    {"id": "agent.ask_owner", "name": "Ask Owner", "kind": "chat",
     "description": "asks the owner", "params": {}, "risk": "llm",
     "state": "ready", "detail": "waits for the owner", "requires": [],
     "runnable": True},
    {"id": "browser.open", "name": "Browser Open", "kind": "browser",
     "description": "opens a page", "params": {}, "risk": "read",
     "state": "ready", "detail": "chromium ready", "requires": [],
     "runnable": True},
    {"id": "browser.read", "name": "Browser Read", "kind": "browser",
     "description": "reads the page", "params": {}, "risk": "read",
     "state": "ready", "detail": "chromium ready", "requires": [],
     "runnable": True},
    {"id": "browser.click", "name": "Browser Click", "kind": "browser",
     "description": "clicks", "params": {}, "risk": "external",
     "state": "ready", "detail": "chromium ready", "requires": [],
     "runnable": True},
]
FAKE_IDS = {t["id"] for t in FAKE_TOOLS}


def _get_tool(tool_id: str):
    for tool in FAKE_TOOLS:
        if tool["id"] == tool_id:
            return tool
    return None


@pytest.fixture()
def stores(tmp_path: Path):
    return {
        "custom": CustomBotStore(tmp_path / "custom.json",
                                 tmp_path / "memory"),
        "tasks": TaskStore(tmp_path / "tasks.json"),
        "schedules": ScheduleStore(tmp_path / "schedules.json"),
    }


def make_bot(store: CustomBotStore, **overrides):
    config = {
        "name": "Test Bot", "description": "does tests",
        "goal": "do the thing", "instructions": "be exact",
        "category": "ops", "tools": ["action.hello_world"],
        "autonomy": "autonomous", "plan_mode": "direct",
        "direct_tool": "action.hello_world", "direct_params": {},
        "memory_enabled": False, "hue": 200, "status": "active",
    }
    config.update(overrides)
    return store.create(validate_config(config, FAKE_IDS))


@pytest.fixture()
def engine(stores, monkeypatch):
    from app.services.sara_bots import engine as engine_mod
    import app.services.sara_bots.planner as planner_mod

    monkeypatch.setattr(engine_mod, "live_tools",
                        lambda *a, **k: list(FAKE_TOOLS))
    monkeypatch.setattr(engine_mod, "get_tool", _get_tool)
    monkeypatch.setattr(engine_mod, "planner_catalog",
                        lambda tools: "test catalog")

    async def fake_summarize(bot, goal, pieces):
        return {"ok": True,
                "text": "SUMMARY: " + " | ".join(p[2][:40] for p in pieces)}

    monkeypatch.setattr(planner_mod, "summarize", fake_summarize)
    return engine_mod.SaraBotsEngine(custom=stores["custom"],
                                     tasks=stores["tasks"])


def fake_exec(record=None, ok=True, output="step output", error="",
              status=None):
    calls = []

    async def _exec(task_id, bot, step_index, step, params):
        calls.append({"step": step, "params": params,
                      "force_confirm": bool(step.get("force_confirm"))})
        if record is not None:
            record.append((task_id, step_index))
        return {"ok": ok,
                "status": status or ("completed" if ok else "failed"),
                "output": output if ok else "", "error": error}

    return _exec, calls


# ── config validation + memory isolation ─────────────────────────────────────

def test_validate_config_rejects_unknown_tool():
    with pytest.raises(ValueError, match="live tool"):
        validate_config({"name": "xx", "description": "d", "goal": "g",
                         "instructions": "i", "tools": ["action.not_real"]},
                        FAKE_IDS)


def test_validate_config_direct_mode_requires_assigned_tool():
    with pytest.raises(ValueError, match="direct_tool"):
        validate_config({"name": "xx", "description": "d", "goal": "g",
                         "instructions": "i", "tools": ["action.hello_world"],
                         "plan_mode": "direct",
                         "direct_tool": "action.write_file"}, FAKE_IDS)


def test_memory_isolation_between_bots(stores):
    bot_a = make_bot(stores["custom"], name="Bot A")
    bot_b = make_bot(stores["custom"], name="Bot B")
    stores["custom"].memory_add(bot_a["id"], "fact", "A secret")
    stores["custom"].memory_add(bot_b["id"], "fact", "B secret")
    assert [e["text"] for e in stores["custom"].memory(bot_a["id"])] == \
        ["A secret"]
    file_a = (stores["custom"].memory_dir / f"{bot_a['id']}.json").read_text(
        encoding="utf-8")
    assert "B secret" not in file_a


def test_memory_path_traversal_rejected(stores):
    with pytest.raises(ValueError):
        stores["custom"].memory_add("../../evil", "fact", "nope")


# ── engine: direct execution, approvals, retries, cancel ─────────────────────

def test_direct_task_runs_to_completion(engine, stores):
    bot = make_bot(stores["custom"])
    task = engine.submit(bot, "say hi")
    fake, calls = fake_exec(output="hello there")
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "completed"
    assert fresh["outputs"]["0"] == "hello there"
    assert fresh["result_text"].startswith("SUMMARY:")
    events = {e["event"] for e in fresh["events"]}
    assert {"step_started", "step_completed", "completed"} <= events
    assert len(calls) == 1


def test_assist_mode_gates_write_step_then_approval_runs_it(engine, stores):
    bot = make_bot(stores["custom"], autonomy="assist",
                   tools=["action.write_file"],
                   direct_tool="action.write_file")
    task = engine.submit(bot, "write it")
    fake, calls = fake_exec(output="written")
    engine._execute_tool = fake

    asyncio.run(engine._process(task["id"]))
    waiting = stores["tasks"].get(task["id"])
    assert waiting["status"] == "waiting_approval"
    assert calls == []                                # nothing ran yet
    approvals = stores["tasks"].list_approvals(status="pending")
    assert len(approvals) == 1
    assert "assist mode" in approvals[0]["reason"]

    stores["tasks"].decide_approval(approvals[0]["id"], "approve")
    asyncio.run(engine._process(task["id"]))
    done = stores["tasks"].get(task["id"])
    assert done["status"] == "completed"
    assert calls[0]["force_confirm"] is True          # replayed WITH confirm


def test_autonomous_read_step_never_asks(engine, stores):
    bot = make_bot(stores["custom"])                   # autonomous, read tool
    task = engine.submit(bot, "say hi")
    fake, calls = fake_exec()
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    assert stores["tasks"].get(task["id"])["status"] == "completed"
    assert stores["tasks"].list_approvals(status="pending") == []
    assert len(calls) == 1


def test_destructive_params_force_approval_even_when_autonomous(engine,
                                                                stores):
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   direct_params={"action": "kill"})
    task = engine.submit(bot, "do something")
    fake, calls = fake_exec()
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "waiting_approval"
    approvals = stores["tasks"].list_approvals(status="pending")
    assert "destructive parameters" in approvals[0]["reason"]
    assert calls == []


def test_read_step_retries_once_then_fails(engine, stores):
    bot = make_bot(stores["custom"])                   # read tool
    task = engine.submit(bot, "say hi")
    fake, calls = fake_exec(ok=False, error="boom")
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "failed"
    assert len(calls) == 2                             # one bounded retry
    assert "boom" in fresh["error"]


def test_write_step_is_never_blindly_retried(engine, stores):
    bot = make_bot(stores["custom"], tools=["action.write_file"],
                   direct_tool="action.write_file")   # autonomous write
    task = engine.submit(bot, "write it")
    fake, calls = fake_exec(ok=False, error="disk full")
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "failed"
    assert len(calls) == 1


def test_memory_note_normalizes_invented_kind_and_bounds_text(engine, stores):
    """Planner LLMs may invent a memory label or exceed 500 chars — the step
    must still store the finding verbatim instead of crashing the task."""
    bot = make_bot(stores["custom"])

    async def _run():
        return await engine._execute_tool(
            "task_x", bot, 0, {"tool": "memory.note"},
            {"kind": "disk_finding", "text": "x" * 700})

    result = asyncio.run(_run())
    assert result["ok"] is True
    entries = stores["custom"].memory(bot["id"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "fact"          # invented label normalized
    assert len(entries[0]["text"]) == 500        # bounded, not crashed
    assert "Remembered (fact)" in result["output"]


def test_memory_note_empty_text_fails_honestly(engine):
    async def _run():
        return await engine._execute_tool(
            "task_x", {"id": "cbot_x"}, 0, {"tool": "memory.note"},
            {"kind": "fact", "text": "   "})

    result = asyncio.run(_run())
    assert result["ok"] is False
    assert "requires 'text'" in result["error"]


def test_planner_catalog_carries_param_descriptions():
    """Allowed values live in the param descriptions; the planner must see
    them or it invents values like 'disk_usage' for actions that only accept
    'disk'."""
    from app.services.sara_bots.tools import planner_catalog

    tools = [{"id": "action.system_diagnostics", "state": "ready",
              "risk": "read", "description": "diagnostics",
              "params": {"action": {"type": "string", "required": True,
                                    "description": "battery | disk | full"}}}]
    text = planner_catalog(tools)
    assert "battery | disk | full" in text
    assert "action:string*" in text


def test_system_diagnostics_contract_fails_honestly():
    """The desktop action must RAISE on invalid input / failed outcomes so the
    headless worker reports a failed run — never 'completed' with an error
    string as its output."""
    import importlib.util

    path = (Path(__file__).resolve().parents[2] / "sara_desktop" / "actions"
            / "system_diagnostics.py")
    spec = importlib.util.spec_from_file_location("sd_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = module.TOOL["handler"]

    with pytest.raises(ValueError, match="Unknown diagnostics action"):
        handler(parameters={"action": "disk_usage"})
    with pytest.raises(ValueError, match="Provide 'pid' or 'name'"):
        handler(parameters={"action": "kill"})
    with pytest.raises(ValueError, match="No process found"):
        handler(parameters={"action": "kill", "name": "zzz_no_such_proc_42"})
    assert isinstance(handler(parameters={"action": "battery"}), str)


def test_blocked_execution_result_parks_task(engine, stores):
    bot = make_bot(stores["custom"])
    task = engine.submit(bot, "say hi")
    fake, calls = fake_exec(ok=False, error="not approved yet",
                            status="blocked")
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "blocked"               # resumable, not failed
    assert "step_blocked" in {e["event"] for e in fresh["events"]}


def test_cancel_waiting_task_is_not_resurrected_by_decision(engine, stores):
    bot = make_bot(stores["custom"], autonomy="assist",
                   tools=["action.write_file"],
                   direct_tool="action.write_file")
    task = engine.submit(bot, "write it")
    fake, _ = fake_exec()
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    approval = stores["tasks"].list_approvals(status="pending")[0]
    engine.cancel(task["id"])
    assert stores["tasks"].get(task["id"])["status"] == "cancelled"
    stores["tasks"].decide_approval(approval["id"], "approve")
    assert stores["tasks"].get(task["id"])["status"] == "cancelled"


def test_approval_expiry_blocks_then_resume_requests_fresh(engine, stores):
    bot = make_bot(stores["custom"], autonomy="assist",
                   tools=["action.write_file"],
                   direct_tool="action.write_file")
    task = engine.submit(bot, "write it")
    fake, calls = fake_exec()
    engine._execute_tool = fake
    asyncio.run(engine._process(task["id"]))
    first = stores["tasks"].list_approvals(status="pending")[0]

    # force the TTL sweep far into the future
    stores["tasks"].expire_stale(now=time.time() + 100_000)
    blocked = stores["tasks"].get(task["id"])
    assert blocked["status"] == "blocked"
    assert stores["tasks"].get_approval(first["id"])["status"] == "expired"
    with pytest.raises(ValueError, match="expired"):
        stores["tasks"].decide_approval(first["id"], "approve")

    engine.resume(task["id"])
    assert stores["tasks"].get(task["id"])["status"] == "queued"
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "waiting_approval"      # fresh request raised
    pending = stores["tasks"].list_approvals(status="pending")
    assert len(pending) == 1 and pending[0]["id"] != first["id"]
    assert calls == []


def test_idempotent_submit_returns_same_task(engine, stores):
    bot = make_bot(stores["custom"])
    first = engine.submit(bot, "say hi", idempotency_key="k-1")
    second = engine.submit(bot, "say hi", idempotency_key="k-1")
    third = engine.submit(bot, "say hi", idempotency_key="k-2")
    assert second["id"] == first["id"]
    assert second.get("deduplicated") is True
    assert third["id"] != first["id"]


def test_recovery_marks_interrupted_steps_honestly(engine, stores):
    from app.services.sara_bots import engine as engine_mod

    class _Dummy:
        store = type("S", (), {"get": staticmethod(lambda _rid: None)})()

    engine_mod_runner = engine_mod.get_runner()
    assert engine_mod_runner is not None             # real runner is safe here

    task = stores["tasks"].create("cbot_abcdef1234", "goal")
    plan = [
        {"id": 0, "title": "write step", "tool": "action.write_file",
         "params": {}, "depends_on": [], "status": "running", "output": "",
         "error": "", "run_id": None, "approval_id": None,
         "force_confirm": False, "started_at": None, "finished_at": None},
        {"id": 1, "title": "read step", "tool": "action.hello_world",
         "params": {}, "depends_on": [], "status": "running", "output": "",
         "error": "", "run_id": None, "approval_id": None,
         "force_confirm": False, "started_at": None, "finished_at": None},
    ]
    stores["tasks"].update(task["id"], plan=plan)
    stores["tasks"].set_status(task["id"], "running")
    engine.recovery()
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "blocked"               # side effect interrupted
    assert fresh["plan"][0]["status"] == "unknown"    # never blindly re-run
    assert fresh["plan"][1]["status"] == "pending"    # read-only is safe again


# ── scheduler ────────────────────────────────────────────────────────────────

class StubEngine:
    def __init__(self, tasks):
        self.tasks = tasks
        self.submitted = []

    def submit(self, bot, goal, kind="user", schedule_id=None,
               idempotency_key=None):
        self.submitted.append((bot["id"], goal, schedule_id))
        return self.tasks.create(bot["id"], goal, kind=kind,
                                 schedule_id=schedule_id)


def test_scheduler_submits_then_dedupes_active_run(stores):
    bot = make_bot(stores["custom"])
    schedule = stores["schedules"].create(bot["id"], "go go", 5)
    stores["schedules"].update(schedule["id"],
                               next_run_at=time.time() - 1)
    stub = StubEngine(stores["tasks"])
    scheduler = SaraBotsScheduler(stub, store=stores["schedules"],
                                  custom=stores["custom"])

    assert scheduler.tick() == 1
    assert len(stub.submitted) == 1
    stores["schedules"].update(schedule["id"], next_run_at=time.time() - 1)
    assert scheduler.tick() == 0                      # task still queued
    assert len(stub.submitted) == 1
    assert stores["schedules"].get(schedule["id"])["skipped"] == 1
    assert stores["schedules"].get(schedule["id"])["runs"] == 1


def test_scheduler_skips_paused_bot_without_piling_up(stores):
    bot = make_bot(stores["custom"], status="paused")
    schedule = stores["schedules"].create(bot["id"], "go go", 5)
    stores["schedules"].update(schedule["id"], next_run_at=time.time() - 1)
    stub = StubEngine(stores["tasks"])
    scheduler = SaraBotsScheduler(stub, store=stores["schedules"],
                                  custom=stores["custom"])
    assert scheduler.tick() == 0
    assert stub.submitted == []
    assert stores["schedules"].get(schedule["id"])["next_run_at"] > time.time()


def test_scheduler_removes_schedule_for_deleted_bot(stores):
    schedule = stores["schedules"].create("cbot_dead000000", "go", 5)
    stores["schedules"].update(schedule["id"], next_run_at=time.time() - 1)
    stub = StubEngine(stores["tasks"])
    scheduler = SaraBotsScheduler(stub, store=stores["schedules"],
                                  custom=stores["custom"])
    scheduler.tick()
    assert stores["schedules"].get(schedule["id"]) is None


# ── planner + factory (no real LLM) ─────────────────────────────────────────

def test_planner_rejects_hallucinated_tool(monkeypatch):
    import app.core.llm_router as llm_router
    from app.services.sara_bots import planner as planner_mod

    async def fake_llm(messages, **kwargs):
        return json.dumps({"steps": [{"title": "x", "tool": "action.nope",
                                      "params": {}}]})

    monkeypatch.setattr(llm_router, "llm_chat", fake_llm)
    bot = {"name": "B", "description": "d", "instructions": "i",
           "output_format": "", "model": ""}
    result = asyncio.run(planner_mod.plan(bot, "goal", "catalog",
                                          {"action.hello_world"}))
    assert result["ok"] is False
    assert "allowlist" in result["error"]

    async def fake_llm_ok(messages, **kwargs):
        return json.dumps({"steps": [{"title": "hi",
                                      "tool": "action.hello_world",
                                      "params": {"who": "{{goal}}"}}]})

    monkeypatch.setattr(llm_router, "llm_chat", fake_llm_ok)
    ok = asyncio.run(planner_mod.plan(bot, "goal", "catalog",
                                      {"action.hello_world"}))
    assert ok["ok"] is True
    assert ok["steps"][0]["params"] == {"who": "{{goal}}"}


def test_factory_cleans_model_proposal():
    from app.services.sara_bots import factory

    ready = {t["id"]: t for t in FAKE_TOOLS}
    proposal = factory._clean_proposal({
        "name": "Crawler", "description": "d", "goal": "g",
        "instructions": "i", "category": "nonsense",
        "autonomy": "yolo", "plan_mode": "direct",
        "tools": ["action.hello_world", "action.does_not_exist",
                  "hub.trading"],
        "direct_tool": "action.does_not_exist",
        "schedule": {"interval_minutes": 1, "goal": "too fast"},
    }, ready)
    assert proposal["category"] == "custom"           # unknown -> safe default
    assert proposal["autonomy"] == "supervised"       # unknown -> safest gate
    assert proposal["plan_mode"] == "auto"            # misdesigned direct mode
    assert proposal["tools"] == ["action.hello_world", "hub.trading"]
    assert any("action.does_not_exist" == d["tool"]
               for d in proposal["dropped"])
    assert proposal["schedule"] is None               # 1 min < minimum 5


# ── API surface ──────────────────────────────────────────────────────────────

@pytest.fixture()
def client(stores, engine, monkeypatch):
    monkeypatch.setenv("SARA_API_KEY", "test-key-123")
    import app.api.deps as deps
    monkeypatch.setattr(deps, "API_KEY", "test-key-123")

    import app.api.sara_bots_custom as api_mod
    monkeypatch.setattr(api_mod, "get_custom_store", lambda: stores["custom"])
    monkeypatch.setattr(api_mod, "get_task_store", lambda: stores["tasks"])
    monkeypatch.setattr(api_mod, "get_schedule_store",
                        lambda: stores["schedules"])
    monkeypatch.setattr(api_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(api_mod, "live_tools",
                        lambda *a, **k: list(FAKE_TOOLS))
    monkeypatch.setattr(api_mod, "tool_ids",
                        lambda *a, **k: set(FAKE_IDS))

    from fastapi import FastAPI
    from app.api import sara_bots_custom
    app = FastAPI()
    app.include_router(sara_bots_custom.router)
    with TestClient(app) as test_client:
        yield test_client


HEADERS = {"X-API-Key": "test-key-123"}


def test_api_requires_key(client):
    assert client.get("/api/sara-bots/tools").status_code == 401
    assert client.get("/api/sara-bots/custom").status_code == 401


def test_api_tool_registry_lists_live_states(client):
    body = client.get("/api/sara-bots/tools", headers=HEADERS).json()
    assert body["summary"]["total"] == len(FAKE_TOOLS)
    assert body["summary"]["by_state"]["ready"] == len(FAKE_TOOLS)


def test_api_create_validate_and_readiness(client):
    bad = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "X Bot", "description": "d", "goal": "g", "instructions": "i",
        "tools": ["action.fake"]})
    assert bad.status_code == 422
    assert "live tool" in bad.json()["detail"]

    created = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "X Bot", "description": "d", "goal": "g", "instructions": "i",
        "tools": ["action.hello_world"], "autonomy": "supervised",
        "plan_mode": "direct", "direct_tool": "action.hello_world"})
    assert created.status_code == 201
    bot = created.json()
    assert bot["ready_tools"] == 1

    readiness = client.get(
        f"/api/sara-bots/custom/{bot['id']}/readiness",
        headers=HEADERS).json()
    assert readiness["runnable"] is True
    assert readiness["ready_tools"] == ["action.hello_world"]

    listing = client.get("/api/sara-bots/custom", headers=HEADERS).json()
    assert any(b["id"] == bot["id"] for b in listing["bots"])


def test_api_task_lifecycle_and_approvals(client, engine, stores):
    created = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "Worker", "description": "d", "goal": "g",
        "instructions": "i", "tools": ["action.write_file"],
        "autonomy": "assist", "plan_mode": "direct",
        "direct_tool": "action.write_file"}).json()

    fake, _ = fake_exec(output="done it")
    engine._execute_tool = fake

    submitted = client.post(
        f"/api/sara-bots/custom/{created['id']}/tasks",
        headers=HEADERS, json={"goal": "write things",
                               "idempotency_key": "api-k1"})
    assert submitted.status_code == 202
    task_id = submitted.json()["id"]

    duplicate = client.post(
        f"/api/sara-bots/custom/{created['id']}/tasks",
        headers=HEADERS, json={"goal": "write things",
                               "idempotency_key": "api-k1"})
    assert duplicate.json()["id"] == task_id          # idempotent submit

    asyncio.run(engine._process(task_id))
    approval = client.get("/api/sara-bots/approvals",
                          headers=HEADERS).json()["approvals"][0]
    assert approval["bot_name"] == "Worker"
    assert approval["goal"] == "write things"

    decided = client.post(
        f"/api/sara-bots/approvals/{approval['id']}/decide",
        headers=HEADERS, json={"decision": "approve"})
    assert decided.status_code == 200

    asyncio.run(engine._process(task_id))
    done = client.get(f"/api/sara-bots/tasks/{task_id}",
                      headers=HEADERS).json()
    assert done["status"] == "completed"
    assert done["outputs"]["0"] == "done it"


def test_api_memory_scoped_to_own_bot(client):
    bot_a = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "Bot A", "description": "d", "goal": "g", "instructions": "i",
        "tools": ["action.hello_world"],
        "plan_mode": "direct", "direct_tool": "action.hello_world"}).json()
    bot_b = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "Bot B", "description": "d", "goal": "g", "instructions": "i",
        "tools": ["action.hello_world"],
        "plan_mode": "direct", "direct_tool": "action.hello_world"}).json()

    added = client.post(f"/api/sara-bots/custom/{bot_a['id']}/memory",
                        headers=HEADERS,
                        json={"kind": "fact", "text": "only for A"})
    assert added.status_code == 201
    mem_a = client.get(f"/api/sara-bots/custom/{bot_a['id']}/memory",
                       headers=HEADERS).json()["memory"]
    mem_b = client.get(f"/api/sara-bots/custom/{bot_b['id']}/memory",
                       headers=HEADERS).json()["memory"]
    assert [e["text"] for e in mem_a] == ["only for A"]
    assert mem_b == []


def test_api_unknown_ids_are_404_and_invalid_are_422(client):
    assert client.get("/api/sara-bots/custom/cbot_zzzzzzzzzz",
                      headers=HEADERS).status_code == 404
    assert client.get("/api/sara-bots/custom/not..valid",
                      headers=HEADERS).status_code == 422
    bad_decision = client.post(
        "/api/sara-bots/approvals/apr_abcdef1234/decide",
        headers=HEADERS, json={"decision": "maybe"})
    assert bad_decision.status_code == 422


def test_api_delete_cascades_schedules_and_cancels_tasks(client, engine,
                                                         stores):
    created = client.post("/api/sara-bots/custom", headers=HEADERS, json={
        "name": "Doomed", "description": "d", "goal": "g",
        "instructions": "i", "tools": ["action.hello_world"],
        "plan_mode": "direct", "direct_tool": "action.hello_world"}).json()
    client.post("/api/sara-bots/schedules", headers=HEADERS, json={
        "dot_id": created["id"], "goal": "tick", "interval_minutes": 30})
    task = engine.submit(stores["custom"].get(created["id"]), "goal")

    deleted = client.delete(f"/api/sara-bots/custom/{created['id']}",
                            headers=HEADERS).json()
    assert deleted["deleted_schedules"] == 1
    assert deleted["cancelled_tasks"] == 1
    assert deleted["closed_browser"] is False        # no session was open
    assert stores["tasks"].get(task["id"])["status"] == "cancelled"
    assert stores["schedules"].list(dot_id=created["id"]) == []


# ── the chat pause (agent.ask_owner) + owner replies ─────────────────────────

def _step(idx, tool, params, depends_on=None):
    return {"id": idx, "title": f"step {idx}", "tool": tool, "params": params,
            "depends_on": depends_on or [], "status": "pending", "output": "",
            "error": "", "run_id": None, "approval_id": None,
            "force_confirm": False, "started_at": None, "finished_at": None}


def _ask_plan(question="Who should I reply to?", options=None):
    ask_params = {"question": question}
    if options:
        ask_params["options"] = options
    return [
        _step(0, "agent.ask_owner", ask_params),
        _step(1, "llm.reason",
              {"prompt": "Draft replies", "context": "{{steps.0.output}}"},
              depends_on=[0]),
    ]


def test_ask_owner_parks_the_task_and_reply_resumes_it(engine, stores):
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   tools=["agent.ask_owner", "llm.reason"],
                   direct_tool="agent.ask_owner")
    task = stores["tasks"].create(bot["id"], "answer my mail",
                                  plan=_ask_plan(options=["Alice", "Bob"]))
    fake, calls = fake_exec(output="drafted")
    engine._execute_tool = fake

    asyncio.run(engine._process(task["id"]))
    parked = stores["tasks"].get(task["id"])
    assert parked["status"] == "waiting_input"
    assert parked["plan"][0]["status"] == "waiting_input"
    assert parked["plan"][0]["question"] == "Who should I reply to?"
    assert parked["plan"][0]["options"] == ["Alice", "Bob"]
    assert calls == []                       # nothing ran while parked
    assert {e["event"] for e in parked["events"]} >= {"running",
                                                      "waiting_for_owner"}

    requeued = stores["tasks"].reply(task["id"], "Reply to 1 and 3, skip 2")
    assert requeued["status"] == "queued"

    asyncio.run(engine._process(task["id"]))
    done = stores["tasks"].get(task["id"])
    assert done["status"] == "completed"
    # the recorded output is the RAW answer (usable as data in later steps)…
    assert done["outputs"]["0"] == "Reply to 1 and 3, skip 2"
    # …while the step display keeps the human framing
    assert done["plan"][0]["output"] == \
        "Owner replied: Reply to 1 and 3, skip 2"
    # the raw reply was expanded into the next step's params
    assert calls[0]["params"]["context"] == "Reply to 1 and 3, skip 2"
    assert {e["event"] for e in done["events"]} >= {"owner_replied",
                                                    "completed"}


def test_ask_owner_options_string_is_split(engine, stores):
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   tools=["agent.ask_owner"],
                   direct_tool="agent.ask_owner")
    plan = [_step(0, "agent.ask_owner",
                  {"question": "Pick one", "options": "first | second |"})]
    task = stores["tasks"].create(bot["id"], "pick", plan=plan)
    asyncio.run(engine._process(task["id"]))
    parked = stores["tasks"].get(task["id"])
    assert parked["status"] == "waiting_input"
    assert parked["plan"][0]["options"] == ["first", "second"]


def test_ask_owner_without_a_question_fails_honestly(engine, stores):
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   tools=["agent.ask_owner"],
                   direct_tool="agent.ask_owner")
    task = stores["tasks"].create(bot["id"], "ask nothing",
                                  plan=[_step(0, "agent.ask_owner", {})])
    asyncio.run(engine._process(task["id"]))
    failed = stores["tasks"].get(task["id"])
    assert failed["status"] == "failed"
    assert "question" in failed["error"]


def test_reply_refuses_tasks_that_are_not_waiting(stores):
    tasks = stores["tasks"]
    queued = tasks.create("cbot_test00001", "goal")
    with pytest.raises(ValueError, match="not waiting"):
        tasks.reply(queued["id"], "hello")
    with pytest.raises(ValueError, match="required"):
        tasks.reply(queued["id"], "   ")
    assert tasks.reply("task_notthere01", "hello") is None


def test_api_reply_round_trip(client, stores, engine):
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   tools=["agent.ask_owner"],
                   direct_tool="agent.ask_owner")
    task = stores["tasks"].create(bot["id"], "ask me",
                                  plan=[_step(0, "agent.ask_owner",
                                              {"question": "Which ones?"})])
    asyncio.run(engine._process(task["id"]))
    parked = client.get(f"/api/sara-bots/tasks/{task['id']}",
                        headers=HEADERS).json()
    assert parked["status"] == "waiting_input"

    missing = client.post("/api/sara-bots/tasks/task_notthere01/reply",
                          headers=HEADERS, json={"text": "hi"})
    assert missing.status_code == 404
    empty = client.post(f"/api/sara-bots/tasks/{task['id']}/reply",
                        headers=HEADERS, json={"text": ""})
    assert empty.status_code == 422

    ok = client.post(f"/api/sara-bots/tasks/{task['id']}/reply",
                     headers=HEADERS, json={"text": "Reply to 2"})
    assert ok.status_code == 202
    assert ok.json()["status"] == "queued"

    again = client.post(f"/api/sara-bots/tasks/{task['id']}/reply",
                        headers=HEADERS, json={"text": "again"})
    assert again.status_code == 409


# ── the bot's own browser (registry, validation, engine seam) ────────────────

def test_browser_tools_follow_the_real_probe(monkeypatch):
    from app.services.sara_bots import browser as browser_mod
    from app.services.sara_bots import tools as tools_mod
    monkeypatch.setattr(browser_mod, "probe",
                        lambda: (True, "Chromium ready (test)"))
    tools = tools_mod._browser_tools()
    ids = {t["id"] for t in tools}
    assert ids == {"browser.open", "browser.read", "browser.click",
                   "browser.type", "browser.press", "browser.scroll",
                   "browser.screenshot", "browser.close"}
    assert all(t["state"] == "ready" for t in tools)
    by_id = {t["id"]: t for t in tools}
    assert by_id["browser.click"]["risk"] == "external"
    assert by_id["browser.type"]["risk"] == "external"
    assert by_id["browser.open"]["risk"] == "read"
    assert by_id["browser.close"]["risk"] == "read"

    monkeypatch.setattr(browser_mod, "probe",
                        lambda: (False, "no chromium yet"))
    degraded = tools_mod._browser_tools()
    assert all(t["state"] == "needs_configuration" for t in degraded)
    assert "no chromium yet" in degraded[0]["detail"]


def test_browser_validation_never_touches_a_browser():
    from app.services.sara_bots import browser as browser_mod
    assert "http(s)" in browser_mod._validate("open", {"url": "file:///x"})
    assert browser_mod._validate("open", {"url": "https://x.test"}) is None
    assert "click" in (browser_mod._validate("click", {}) or "")
    assert browser_mod._validate("click", {"text": "Send"}) is None
    assert browser_mod._validate("type", {"text": "hi"})        # target missing
    assert browser_mod._validate(
        "type", {"text": "hi", "target": "Search"}) is None
    assert browser_mod._validate("read", {}) is None


def test_browser_open_rejects_non_http_urls_without_launching():
    from app.services.sara_bots import browser as browser_mod
    res = asyncio.run(browser_mod.run_op("cbot_test00001", "open",
                                         {"url": "javascript:alert(1)"}))
    assert res["ok"] is False and "http(s)" in res["error"]


def test_browser_remembers_and_restores_the_last_page(tmp_path, monkeypatch):
    from app.services.sara_bots import browser as browser_mod

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.goto_calls = []

        def goto(self, url, **kw):
            self.goto_calls.append(url)
            self.url = url

        def wait_for_timeout(self, ms):
            return None

    profile = tmp_path / "prof"
    profile.mkdir()
    monkeypatch.setattr(browser_mod, "_profile_dir", lambda bot_id: profile)
    last = profile / "_last_url.txt"

    page = FakePage()
    sess = {"bot_id": "cbot_x", "page": page}
    assert browser_mod._restore_last_url("cbot_x", sess) == ""   # nothing yet

    browser_mod._remember_url("cbot_x", sess)                    # blank: no-op
    assert not last.exists()

    page.url = "https://mail.google.com/mail/u/0/#inbox"
    browser_mod._remember_url("cbot_x", sess)
    assert last.read_text(encoding="utf-8").startswith(
        "https://mail.google.com")

    fresh_page = FakePage()
    restored = browser_mod._restore_last_url(
        "cbot_x", {"bot_id": "cbot_x", "page": fresh_page})
    assert restored == "https://mail.google.com/mail/u/0/#inbox"
    assert fresh_page.goto_calls == [restored]

    # a page that is already open is never re-navigated
    assert browser_mod._restore_last_url(
        "cbot_x", {"bot_id": "cbot_x", "page": fresh_page}) == restored
    assert len(fresh_page.goto_calls) == 1


def test_cold_session_restores_the_last_page_and_says_so(tmp_path,
                                                         monkeypatch):
    from app.services.sara_bots import browser as browser_mod

    class FakePage:
        def __init__(self):
            self.url = "about:blank"

        def goto(self, url, **kw):
            self.url = url

        def wait_for_timeout(self, ms):
            return None

        def is_closed(self):
            return False

        def title(self):
            return "Inbox"

        def evaluate(self, *args, **kw):
            return ""

    class FakeContext:
        pages = []

        def close(self):
            return None

    def fake_launch(bot_id):
        return {"bot_id": bot_id, "page": FakePage(), "context": FakeContext(),
                "channel": "test", "created_at": 0.0}

    profile = tmp_path / "prof2"
    profile.mkdir()
    monkeypatch.setattr(browser_mod, "_profile_dir", lambda bot_id: profile)
    monkeypatch.setattr(browser_mod, "_launch", fake_launch)
    monkeypatch.setattr(browser_mod, "_sessions", {})
    (profile / "_last_url.txt").write_text("https://mail.google.com/",
                                           encoding="utf-8")

    res = browser_mod._run_with_session("cbot_x", browser_mod._OPS["read"],
                                        {}, "read")
    assert res["ok"] is True
    assert "https://mail.google.com/" in res["output"]
    assert "re-opened automatically" in res["output"]


def test_engine_runs_browser_steps_through_the_real_seam(engine, stores,
                                                         monkeypatch):
    from app.services.sara_bots import browser as browser_mod
    calls = []

    async def fake_run_op(bot_id, op, params):
        calls.append((bot_id, op, dict(params)))
        return {"ok": True, "output": f"browser {op} done"}

    monkeypatch.setattr(browser_mod, "run_op", fake_run_op)
    bot = make_bot(stores["custom"], autonomy="autonomous",
                   tools=["browser.open"], direct_tool="browser.open",
                   direct_params={"url": "https://mail.google.com/"})
    task = engine.submit(bot, "open my mail")
    asyncio.run(engine._process(task["id"]))
    done = stores["tasks"].get(task["id"])
    assert done["status"] == "completed"
    assert calls == [(bot["id"], "open",
                      {"url": "https://mail.google.com/"})]
    assert done["outputs"]["0"] == "browser open done"


def test_supervised_bot_gates_external_browser_clicks(engine, stores,
                                                      monkeypatch):
    from app.services.sara_bots import browser as browser_mod
    ran = []

    async def fake_run_op(bot_id, op, params):
        ran.append(op)
        return {"ok": True, "output": f"browser {op} done"}

    monkeypatch.setattr(browser_mod, "run_op", fake_run_op)
    bot = make_bot(stores["custom"], autonomy="supervised",
                   tools=["browser.click"], direct_tool="browser.click",
                   direct_params={"text": "Send"})
    task = engine.submit(bot, "press send")
    asyncio.run(engine._process(task["id"]))
    waiting = stores["tasks"].get(task["id"])
    assert waiting["status"] == "waiting_approval"
    assert ran == []                          # the click did NOT happen
    approval = stores["tasks"].list_approvals(status="pending")[0]
    assert "external" in approval["reason"]

    stores["tasks"].decide_approval(approval["id"], "approve")
    asyncio.run(engine._process(task["id"]))
    assert stores["tasks"].get(task["id"])["status"] == "completed"
    assert ran == ["click"]


def test_api_browser_status_route(client, stores, monkeypatch):
    from app.services.sara_bots import browser as browser_mod

    async def fake_info(bot_id, with_shot=False):
        return {"alive": True, "url": "https://mail.google.com/",
                "title": "Inbox (3)", "profile": "C:/x", "created_at": 1.0,
                "mode": "windowed",
                "png_b64": "aGk=" if with_shot else ""}

    monkeypatch.setattr(browser_mod, "session_info", fake_info)
    bot = make_bot(stores["custom"])
    res = client.get(f"/api/sara-bots/custom/{bot['id']}/browser?shot=1",
                     headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["alive"] is True
    assert body["url"] == "https://mail.google.com/"
    assert body["png_b64"] == "aGk="
    assert client.get("/api/sara-bots/custom/not..valid/browser",
                      headers=HEADERS).status_code == 422


# ── regressions: fresh-process tool states + honest planning errors ───────────

def test_bot_states_scans_before_reading_actions(tmp_path, monkeypatch):
    """Regression: the FIRST state build of a fresh process used to run before
    the action scan, so every approved local action was misreported as
    needs_approval — and a task planned in that window blocked with a
    misleading "not in the ready allowlist" error even though the tool was
    approved and assigned. bot_states() must scan first."""
    from app.services.sara_bots import registry as registry_mod

    actions_dir = tmp_path / "actions"
    actions_dir.mkdir()
    (actions_dir / "battery_check.py").write_text(
        "TOOL = {\n"
        "    'name': 'battery_check',\n"
        "    'description': 'reads the battery level',\n"
        "    'parameters': {},\n"
        "    'handler': battery_check,\n"
        "}\n\n"
        "def battery_check(parameters, player=None):\n"
        "    return 'battery 80%'\n",
        encoding="utf-8")

    reg = registry_mod.SaraBotsRegistry()
    monkeypatch.setattr(registry_mod, "APPROVED_SCOPE", tmp_path)
    monkeypatch.setattr(reg.store, "root", lambda: actions_dir)
    monkeypatch.setattr(reg.store, "approved_ids",
                        lambda: {"action.battery_check"})
    monkeypatch.setattr(reg.store, "disabled_ids", lambda: set())

    # simulate a fresh process: nothing scanned, nothing discovered, no cache
    assert reg._scanned_at == 0.0
    assert reg._discovered == {}

    states = reg.bot_states(None)
    assert states["action.battery_check"]["state"] == "ready"
    assert states["action.battery_check"]["approved"] is True


def test_planning_allowlist_error_names_assigned_tool_state(engine, stores,
                                                            monkeypatch):
    """When the planner emits an ASSIGNED but not-ready tool, the blocked
    reason must name the real state + fix path — not tell the owner to add a
    tool the bot already has."""
    import app.services.sara_bots.planner as planner_mod
    from app.services.sara_bots import engine as engine_mod

    async def fake_plan(bot, goal, catalog, allowed_ids):
        return {"ok": False,
                "error": "step 0 uses 'action.system_diagnostics' which is "
                         "not in this bot's ready allowlist"}

    monkeypatch.setattr(planner_mod, "plan", fake_plan)
    monkeypatch.setattr(engine_mod, "get_tool", lambda tid: {
        "id": tid, "state": "needs_approval",
        "detail": "discovered but not approved — approve it in the Fleet view",
    })

    base = {
        "name": "Battery Bot", "description": "d", "goal": "g",
        "instructions": "i", "category": "ops",
        "autonomy": "autonomous", "plan_mode": "auto",
        "direct_tool": "", "direct_params": {},
        "memory_enabled": False, "hue": 200, "status": "active",
    }
    ids = FAKE_IDS | {"action.system_diagnostics"}
    bot = stores["custom"].create(validate_config(
        {**base, "tools": ["action.hello_world", "action.system_diagnostics"]},
        ids))
    task = engine.submit(bot, "check battery status")
    asyncio.run(engine._process(task["id"]))
    fresh = stores["tasks"].get(task["id"])
    assert fresh["status"] == "blocked"
    assert "assigned to this bot but not ready right now" in fresh["error"]
    assert "needs_approval" in fresh["error"]
    assert "Fleet view" in fresh["error"]


def test_planning_allowlist_error_for_unassigned_tool_says_add_it(
        engine, stores, monkeypatch):
    """A tool the bot does NOT have must keep pointing at Edit configuration."""
    import app.services.sara_bots.planner as planner_mod

    async def fake_plan(bot, goal, catalog, allowed_ids):
        return {"ok": False,
                "error": "step 0 uses 'action.write_file' which is not in "
                         "this bot's ready allowlist"}

    monkeypatch.setattr(planner_mod, "plan", fake_plan)
    bot = make_bot(stores["custom"], plan_mode="auto",
                   tools=["action.hello_world"])
    task = engine.submit(bot, "write a file")
    asyncio.run(engine._process(task["id"]))
    err = stores["tasks"].get(task["id"])["error"]
    assert "add it in Edit configuration" in err


# ── planner: coverage rules + one bounded self-repair retry ──────────────────

def test_planner_rejects_plan_ending_on_the_owner_question():
    """An ask-only plan does no work and would let the final summary fabricate
    the result — it must be rejected deterministically."""
    from app.services.sara_bots.planner import _validate_reply

    reply = json.dumps({"steps": [
        {"title": "ask", "tool": "agent.ask_owner",
         "params": {"question": "which topic?"}}]})
    res = _validate_reply(reply, {"agent.ask_owner", "browser.open"})
    assert res["ok"] is False
    assert "reply must be USED" in res["error"]


def test_planner_keeps_work_steps_after_the_owner_question():
    from app.services.sara_bots.planner import _validate_reply

    reply = json.dumps({"steps": [
        {"title": "ask", "tool": "agent.ask_owner",
         "params": {"question": "which topic?"}},
        {"title": "search", "tool": "browser.type",
         "params": {"target": "Search", "text": "{{steps.0.output}}",
                    "submit": True}}]})
    res = _validate_reply(reply, {"agent.ask_owner", "browser.type"})
    assert res["ok"] is True
    assert [s["tool"] for s in res["steps"]] == ["agent.ask_owner",
                                                 "browser.type"]


def test_planner_self_repairs_after_a_rejected_reply(monkeypatch):
    """One bounded retry hands the rejection reason back to the model — the
    corrected reply is used instead of failing the whole task."""
    import app.core.llm_router as router_mod
    from app.services.sara_bots import planner as planner_mod

    replies = iter([
        json.dumps({"steps": [
            {"title": "ask", "tool": "agent.ask_owner",
             "params": {"question": "which topic?"}}]}),
        json.dumps({"steps": [
            {"title": "ask", "tool": "agent.ask_owner",
             "params": {"question": "which topic?"}},
            {"title": "open", "tool": "browser.open",
             "params": {"url": "https://en.wikipedia.org"}}]}),
    ])
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr(router_mod, "llm_chat", fake_chat)

    result = asyncio.run(planner_mod.plan(
        {"name": "B", "description": "d", "instructions": "i",
         "output_format": ""},
        "research a topic", "catalog",
        {"agent.ask_owner", "browser.open"}))
    assert result["ok"] is True
    assert [s["tool"] for s in result["steps"]] == ["agent.ask_owner",
                                                    "browser.open"]
    assert len(calls) == 2                        # exactly one retry
    assert "INVALID" in calls[1][-1]["content"]   # rejection handed back


def test_planner_fails_cleanly_when_both_attempts_are_invalid(monkeypatch):
    import app.core.llm_router as router_mod
    from app.services.sara_bots import planner as planner_mod

    ask_only = json.dumps({"steps": [
        {"title": "ask", "tool": "agent.ask_owner",
         "params": {"question": "which topic?"}}]})
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(messages)
        return ask_only

    monkeypatch.setattr(router_mod, "llm_chat", fake_chat)
    result = asyncio.run(planner_mod.plan(
        {"name": "B", "description": "d", "instructions": "i",
         "output_format": ""},
        "research a topic", "catalog", {"agent.ask_owner"}))
    assert result["ok"] is False
    assert "reply must be USED" in result["error"]
    assert len(calls) == 2


# ── tenancy: every user sees only their own bots ─────────────────────────────

def test_local_owner_matches_tenancy_and_legacy_values_normalize():
    from app.agents.sara_ultron_agent.core.tenancy import LOCAL_TENANT_ID
    from app.services.sara_bots.custom import LOCAL_OWNER, normalize_owner

    assert LOCAL_OWNER == LOCAL_TENANT_ID
    assert normalize_owner(None) == LOCAL_TENANT_ID
    assert normalize_owner("") == LOCAL_TENANT_ID
    assert normalize_owner("owner") == LOCAL_TENANT_ID    # pre-tenancy rows
    assert normalize_owner("user_abc") == "user_abc"


def test_legacy_records_belong_to_the_local_operator(tmp_path):
    bots_file = tmp_path / "bots.json"
    bots_file.write_text(json.dumps({"bots": {
        "cbot_legacy0001": {"id": "cbot_legacy0001", "name": "Old",
                            "owner": "owner"},
        "cbot_legacy0002": {"id": "cbot_legacy0002", "name": "Older"}}}),
        encoding="utf-8")
    store = CustomBotStore(bots_file, tmp_path / "mem")
    assert {b["id"] for b in store.list(owner="local")} == \
        {"cbot_legacy0001", "cbot_legacy0002"}
    assert store.list(owner="user_xyz") == []


def test_engine_tasks_approvals_and_schedules_inherit_the_bot_owner(
        engine, stores):
    bot = make_bot(stores["custom"])
    stores["custom"].update(bot["id"], {"owner": "user_zzz"})
    bot = stores["custom"].get(bot["id"])
    task = engine.submit(bot, "run once")
    assert task["owner"] == "user_zzz"
    approval = stores["tasks"].create_approval(task["id"], bot["id"], 0,
                                               "hub.trading", "external", "{}")
    assert approval["owner"] == "user_zzz"
    schedule = stores["schedules"].create(bot["id"], "tick", 30,
                                          owner=bot["owner"])
    assert schedule["owner"] == "user_zzz"
    assert [t["id"] for t in stores["tasks"].list(owner="user_zzz")] == \
        [task["id"]]
    assert stores["tasks"].list(owner="local") == []
    assert [s["id"] for s in stores["schedules"].list(owner="user_zzz")] == \
        [schedule["id"]]
    assert stores["schedules"].list(owner="local") == []


@pytest.fixture()
def as_user(client):
    from app.api import deps
    from app.agents.sara_ultron_agent.core.tenancy import Tenant

    def _as(user_id: str):
        client.app.dependency_overrides[deps.current_tenant] = (
            lambda: Tenant(id=user_id, kind="clerk", label=user_id))
        return client

    yield _as
    client.app.dependency_overrides.pop(deps.current_tenant, None)


def test_bots_tasks_and_approvals_are_scoped_to_the_signed_in_user(
        client, as_user, stores):
    # user A creates a bot, submits a task, and a pending approval appears
    as_user("user_aaa")
    created = client.post("/api/sara-bots/custom", json={
        "name": "Scoped Bot", "description": "d", "goal": "g",
        "instructions": "i", "tools": ["action.hello_world"],
        "autonomy": "autonomous", "plan_mode": "direct",
        "direct_tool": "action.hello_world"})
    assert created.status_code == 201
    bot = created.json()
    assert bot["owner"] == "user_aaa"

    submitted = client.post(f"/api/sara-bots/custom/{bot['id']}/tasks",
                            json={"goal": "run once"})
    assert submitted.status_code == 202
    task = submitted.json()
    assert task["owner"] == "user_aaa"
    approval = stores["tasks"].create_approval(task["id"], bot["id"], 0,
                                               "hub.trading", "external", "{}")
    assert approval["owner"] == "user_aaa"

    assert [b["id"] for b in
            client.get("/api/sara-bots/custom").json()["bots"]] == [bot["id"]]
    assert [t["id"] for t in
            client.get("/api/sara-bots/tasks").json()["tasks"]] == [task["id"]]
    assert [a["id"] for a in
            client.get("/api/sara-bots/approvals").json()["approvals"]] == \
        [approval["id"]]

    # user B: every read is empty, every per-id operation is a 404
    as_user("user_bbb")
    assert client.get("/api/sara-bots/custom").json()["bots"] == []
    assert client.get(
        f"/api/sara-bots/custom/{bot['id']}").status_code == 404
    assert client.patch(f"/api/sara-bots/custom/{bot['id']}",
                        json={"name": "Stolen"}).status_code == 404
    assert client.delete(
        f"/api/sara-bots/custom/{bot['id']}").status_code == 404
    assert client.post(
        f"/api/sara-bots/custom/{bot['id']}/pause").status_code == 404
    assert client.get(
        f"/api/sara-bots/custom/{bot['id']}/readiness").status_code == 404
    assert client.get(
        f"/api/sara-bots/custom/{bot['id']}/browser").status_code == 404
    assert client.get(
        f"/api/sara-bots/custom/{bot['id']}/memory").status_code == 404
    assert client.post(f"/api/sara-bots/custom/{bot['id']}/memory",
                       json={"kind": "fact", "text": "x"}).status_code == 404
    assert client.get("/api/sara-bots/tasks").json()["tasks"] == []
    assert client.get(
        f"/api/sara-bots/tasks/{task['id']}").status_code == 404
    assert client.post(
        f"/api/sara-bots/tasks/{task['id']}/cancel").status_code == 404
    assert client.post(f"/api/sara-bots/tasks/{task['id']}/resume"
                       ).status_code == 404
    assert client.post(f"/api/sara-bots/tasks/{task['id']}/reply",
                       json={"text": "hi"}).status_code == 404
    assert client.get("/api/sara-bots/approvals").json()["approvals"] == []
    assert client.post(
        f"/api/sara-bots/approvals/{approval['id']}/decide",
        json={"decision": "approve"}).status_code == 404
    assert client.get("/api/sara-bots/schedules").json()["schedules"] == []
    assert client.patch("/api/sara-bots/schedules/sch_zzzzzzzzzz",
                        json={"goal": "steal"}).status_code == 404

    # back as user A: everything is intact and the approval is decidable
    as_user("user_aaa")
    decided = client.post(
        f"/api/sara-bots/approvals/{approval['id']}/decide",
        json={"decision": "approve"})
    assert decided.status_code == 200
    assert decided.json()["approval"]["status"] == "approved"


def test_api_key_caller_keeps_the_local_tenants_bots(client, stores):
    # a legacy-style local bot (created through the store with defaults)
    bot = make_bot(stores["custom"], name="Local Bot")
    assert bot["owner"] == "local"
    listing = client.get("/api/sara-bots/custom", headers=HEADERS)
    assert bot["id"] in {b["id"] for b in listing.json()["bots"]}
