"""SARA Ultron — integration test suite.

Run from backend/:  python -m tests.test_sara_ultron_agent

Style matches tests/test_edulearner.py (self-contained runner, no pytest
dependency in the venv). Exits non-zero if any check fails.

Covers, per the integration spec:
  * registration      - router shape, mount point, all 8 routes on the real app
  * identity          - canonical id / metadata / catalogue
  * tools             - native + desktop merge, collision guard, OpenAI schemas
  * validation        - planner output parsing, unknown-tool rejection, fallback
  * execution         - error policy (retry/skip/abort), destructive gating,
                        approval gate, stop handling
  * memory            - store/recall/rules on top of SARA's PersistentMemory
  * auth              - require_api_key on the mutating routes
  * failure handling  - honest status (never claims unexecuted success)
  * regression        - pre-existing SARA routes are all still mounted
"""

from __future__ import annotations

import json
import os
import sys
import types
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.agents.sara_ultron_agent as ultron  # noqa: E402
from app.agents.sara_ultron_agent import orchestrator as orch_mod  # noqa: E402
from app.agents.sara_ultron_agent.core import planner as planner_mod  # noqa: E402
from app.agents.sara_ultron_agent.core import tools as tools_mod  # noqa: E402
from app.agents.sara_ultron_agent.core.catalog import is_delegatable  # noqa: E402
from app.agents.sara_ultron_agent.core.executor import (  # noqa: E402
    ErrorDecision,
    UltronExecutor,
    classify_error,
    has_unresolved_placeholder,
    is_destructive,
    looks_failed,
    resolve_placeholders,
)
from app.agents.sara_ultron_agent.core.memory import UltronMemory  # noqa: E402
from app.agents.sara_ultron_agent.core.planner import (  # noqa: E402
    UltronPlanner,
    extract_json,
)
from app.agents.sara_ultron_agent.core.session import UltronSession  # noqa: E402
from app.agents.sara_ultron_agent.core.tools import (  # noqa: E402
    NATIVE_TOOL_NAMES,
    UltronToolRegistry,
    conv_schema,
)

# ---------------------------------------------------------------------------
# Tiny harness
# ---------------------------------------------------------------------------
_FAILURES: List[str] = []
_CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _CHECKS
    _CHECKS += 1
    if condition:
        print(f"  [OK  ] {label}")
    else:
        _FAILURES.append(label)
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 66)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeDesktop:
    """Stand-in for SARA's ActionRegistry, so tests never touch the real PC."""

    def __init__(self, descriptions: Dict[str, str], handlers: Dict[str, Any]):
        self._actions = {
            name: types.SimpleNamespace(
                name=name,
                description=desc,
                parameters={"type": "object", "properties": {}},
            )
            for name, desc in descriptions.items()
        }
        self._handlers = handlers

    def names(self):
        return set(self._actions)

    def has(self, name: str) -> bool:
        return name in self._actions

    def run(self, name: str, args: Dict[str, Any], ctx: Dict[str, Any] | None = None) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            return f"Action '{name}' is not available."
        return handler(args or {})


class FakeMemory(UltronMemory):
    """UltronMemory with its disk writes redirected, so tests stay hermetic."""

    def __init__(self) -> None:
        self._lock = __import__("threading").Lock()
        self.experiences_written: List[Dict[str, Any]] = []
        self.rules_written: List[str] = []
        self._exps: List[Dict[str, Any]] = []
        self._rules: List[str] = []

    def experiences(self):
        # Same contract as the real store: non-dict records are filtered out.
        return [e for e in self._exps if isinstance(e, dict)]

    def rules(self):
        return [r for r in self._rules if isinstance(r, str)]

    def remember(self, goal, plan, success, error_logs=""):
        self.experiences_written.append(
            {"goal": goal, "plan": plan, "success": success, "errors": error_logs}
        )

    def learn_rule(self, rule: str) -> bool:
        rule = (rule or "").strip()
        if not rule or rule in self._rules:
            return False
        self._rules.append(rule)
        self.rules_written.append(rule)
        return True

    def snapshot(self):
        return {"available": True, "experiences": len(self.experiences_written),
                "rules": len(self.rules_written), "success_rate": None}


def make_registry(handlers: Dict[str, Any] | None = None,
                  descriptions: Dict[str, str] | None = None) -> UltronToolRegistry:
    """A real UltronToolRegistry wired to a fake desktop action set."""
    handlers = handlers or {}
    descriptions = descriptions or {n: f"fake action {n}" for n in handlers}
    desktop = FakeDesktop(descriptions, handlers)
    return UltronToolRegistry(desktop, tools_mod._native_tools(FakeMemory()))


def make_session(goal: str = "test goal", mode: str = "autonomous") -> UltronSession:
    return UltronSession(goal, mode=mode)


def drain(session: UltronSession) -> List[Dict[str, Any]]:
    events = []
    while True:
        try:
            events.append(session.event_q.get_nowait())
        except Exception:
            return events


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------
def test_registration() -> None:
    section("1. Registration")
    from app.main import app

    check("router is an APIRouter", ultron.router.__class__.__name__ == "APIRouter")
    check("prefix is /ultron", ultron.router.prefix == ultron.API_PREFIX == "/ultron")
    check("tagged 'SARA Ultron'", "SARA Ultron" in (ultron.router.tags or []))

    paths = set(app.openapi()["paths"])
    expected = {
        "/ultron/", "/ultron/run", "/ultron/events", "/ultron/approve",
        "/ultron/stop", "/ultron/status", "/ultron/capabilities", "/ultron/health",
    }
    missing = expected - paths
    check("all 8 routes mounted on the real app", not missing, f"missing {sorted(missing)}")

    # The agent must be reachable by its canonical identifier too.
    check("canonical id is sara_ultron_agent", ultron.AGENT_ID == "sara_ultron_agent")
    check("slug matches the hub route", ultron.AGENT_SLUG == "sara-ultron")
    check("appears in the agent catalogue",
          any(a["id"] == "sara-ultron" for a in ultron.AGENT_CATALOG))


# ---------------------------------------------------------------------------
# 2. Regression - pre-existing SARA surface untouched
# ---------------------------------------------------------------------------
def test_regression() -> None:
    section("2. Regression (existing SARA routes preserved)")
    from app.main import app

    paths = set(app.openapi()["paths"])
    for probe in (
        "/api/health", "/api/llm/health", "/api/v1/chat", "/api/v1/orchestrate",
        "/sara-auto/run", "/sara-auto/capabilities", "/email-agent/campaigns",
    ):
        check(f"still mounted: {probe}", probe in paths)
    check("route count did not collapse", len(paths) > 150, f"only {len(paths)} paths")


# ---------------------------------------------------------------------------
# 3. Tool registry
# ---------------------------------------------------------------------------
def test_tool_registry() -> None:
    section("3. Tool registry")
    reg = make_registry({"web_search": lambda a: "results"},
                        {"web_search": "Search the web."})

    names = reg.names()
    check("native tools merged in", NATIVE_TOOL_NAMES.issubset(set(names)))
    check("desktop actions merged in", "web_search" in names)
    check("source() distinguishes origin",
          reg.source("ultron_summarize") == "ultron" and reg.source("web_search") == "desktop")

    decls = reg.declarations()
    check("one declaration per tool", len(decls) == len(names))
    check("declarations are OpenAI-shaped",
          all(d.get("type") == "function"
              and "name" in d["function"]
              and "parameters" in d["function"]
              for d in decls))

    desc = reg.descriptions()
    check("descriptions() groups both sources",
          "SARA DESKTOP ACTIONS" in desc and "ULTRON NATIVE TOOLS" in desc)
    # The planner must see required arg names, or it invents "task" for "message".
    check("descriptions() lists required args",
          "ultron_delegate (requires: agent_id, message)" in desc, desc[:400])
    check("required_args() reads the schema",
          reg.required_args("ultron_delegate") == ["agent_id", "message"])
    check("required_args() empty when nothing required",
          reg.required_args("web_search") == [])

    # Pre-dispatch argument validation.
    check("missing required arg detected",
          reg.missing_args("ultron_delegate", {"agent_id": "x"}) == ["message"])
    check("blank required arg detected",
          reg.missing_args("ultron_delegate", {"agent_id": "x", "message": "  "})
          == ["message"])
    check("renamed arg counts as missing (task != message)",
          reg.missing_args("ultron_delegate",
                           {"agent_id": "x", "task": "y"}) == ["message"])
    check("fully supplied call has no missing args",
          reg.missing_args("ultron_delegate",
                           {"agent_id": "x", "message": "y"}) == [])

    # Names are reserved so a desktop action can never shadow a native tool.
    check("native names reserved for collision guard",
          {"ultron_delegate", "ultron_recall"} <= NATIVE_TOOL_NAMES)

    # Real registry (loads SARA's actual desktop actions).
    try:
        real = tools_mod.UltronToolRegistry.load(FakeMemory())
        check("real registry loads SARA desktop actions",
              len([n for n in real.names() if real.source(n) == "desktop"]) > 0)
        check("no native/desktop name collision",
              not (NATIVE_TOOL_NAMES & {n for n in real.names() if real.source(n) == "desktop"}))
    except Exception as exc:  # noqa: BLE001
        check("real registry loads SARA desktop actions", False, f"{type(exc).__name__}: {exc}")


def test_schema_conversion() -> None:
    section("4. Schema conversion (Gemini -> OpenAI)")
    converted = conv_schema({
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING"},
            "limit": {"type": "INTEGER"},
            "opts": {"type": "OBJECT", "properties": {"deep": {"type": "BOOLEAN"}}},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    })
    props = converted["properties"]
    check("top-level type lowered", converted["type"] == "object")
    check("primitive types lowered",
          props["query"]["type"] == "string" and props["limit"]["type"] == "integer")
    check("nested object converted", props["opts"]["properties"]["deep"]["type"] == "boolean")
    check("array items converted", props["tags"]["items"]["type"] == "string")
    check("non-dict passthrough", conv_schema("x") == "x")


# ---------------------------------------------------------------------------
# 5. Delegation allowlist
# ---------------------------------------------------------------------------
def test_delegation_allowlist() -> None:
    section("5. Inter-agent delegation allowlist")
    check("known agent allowed", is_delegatable("backend-architect"))
    check("case-insensitive", is_delegatable("Backend-Architect"))
    check("unknown agent rejected", not is_delegatable("system32"))
    check("empty rejected", not is_delegatable(""))
    check("can't delegate to itself via allowlist bypass",
          not is_delegatable("../../etc/passwd"))

    out = tools_mod._tool_delegate({"agent_id": "definitely-not-an-agent", "message": "hi"})
    check("unknown agent_id refused at the tool", "Cannot delegate" in out, out)
    out = tools_mod._tool_delegate({"agent_id": "backend-architect", "message": ""})
    check("missing message refused at the tool", "requires both" in out, out)


# ---------------------------------------------------------------------------
# 6. Memory
# ---------------------------------------------------------------------------
def test_memory() -> None:
    section("6. Long-term memory (over SARA's PersistentMemory)")
    mem = FakeMemory()
    mem._exps = [
        {"goal": "search the web for python release notes", "plan": [], "success": True},
        {"goal": "delete old downloads folder", "plan": [], "success": False,
         "errors": "access denied"},
    ]
    hits = mem.recall("web search python")
    check("recall ranks the relevant experience first",
          hits and "web" in hits[0]["goal"], str(hits))
    check("recall is bounded", len(mem.recall("the and of")) <= 5)

    check("learn_rule is idempotent", mem.learn_rule("always confirm deletes"))
    check("learn_rule dedupes", not mem.learn_rule("always confirm deletes"))
    check("auto_learn ignores trivial errors", not mem.auto_learn("goal", "err"))
    check("auto_learn stores substantive errors",
          mem.auto_learn("delete downloads", "access denied on C:/Downloads"))

    # A bad rule is injected into every future prompt, so auto-learn must stay
    # conservative about what counts as a lesson.
    for not_a_lesson in (
        "system_command: Denied by the user. Do not retry this action.",
        "Stopped by user.",
        "Request timed out, please try again",
        "rate limit exceeded",
    ):
        check(f"auto_learn ignores: {not_a_lesson[:34]!r}",
              not mem.auto_learn("a goal", not_a_lesson))

    ctx = mem.context("search the web for python release notes")
    check("context includes rules", "always confirm deletes" in ctx)
    check("context includes experiences", "python release notes" in ctx)
    check("empty memory says so", "No prior Ultron memory" in FakeMemory().context("x"))

    # Round-trip: the shared store keeps plans as dicts, so recall must survive
    # reading back a record written by a real run.
    mem._exps = [{
        "goal": "organize the desktop by file type",
        "plan": [{"tool": "file_controller", "args": {"action": "organize_desktop"}}],
        "success": True,
    }]
    try:
        hits = mem.recall("organize desktop file type")
        check("recall survives a dict-shaped plan", bool(hits), str(hits))
        check("recall finds the goal through the plan", "desktop" in mem.context("organize desktop"))
    except Exception as exc:  # noqa: BLE001
        check("recall survives a dict-shaped plan", False, f"{type(exc).__name__}: {exc}")

    # Malformed records must degrade, not crash the run.
    mem._exps = [{"goal": "ok"}, "not-a-dict", {"plan": "scalar-plan", "goal": "b"}]
    try:
        mem.recall("anything")
        mem.context("anything")
        check("malformed memory records are tolerated", True)
    except Exception as exc:  # noqa: BLE001
        check("malformed memory records are tolerated", False, f"{type(exc).__name__}: {exc}")

    # Real store on disk (SARA's PersistentMemory) — verify the adapter works.
    real = ultron.get_memory()
    check("real memory store is available", real.memory_dir.is_dir())
    snap = real.snapshot()
    check("snapshot hides memory contents",
          set(snap) == {"available", "memory_dir", "experiences", "rules", "success_rate"})


# ---------------------------------------------------------------------------
# 7. Planner
# ---------------------------------------------------------------------------
def test_planner_json_extraction() -> None:
    section("7. Planner output parsing")
    check("bare JSON", extract_json('{"steps": []}') == {"steps": []})
    check("fenced JSON", extract_json('```json\n{"steps": []}\n```') == {"steps": []})
    check("fenced without lang", extract_json('```\n{"steps": []}\n```') == {"steps": []})
    check("JSON embedded in prose",
          extract_json('Sure! Here you go:\n{"steps": [{"tool": "x"}]}\nHope that helps.')
          == {"steps": [{"tool": "x"}]})
    check("top-level array", extract_json('[{"tool": "x"}]') == [{"tool": "x"}])
    check("garbage returns None", extract_json("no json here at all") is None)
    check("empty returns None", extract_json("") is None)


def test_planner_validation() -> None:
    section("8. Planner validation")
    p = UltronPlanner(max_steps=3)
    plan = p._validate([
        {"tool": "ultron_summarize", "args": {"text": "a"}},
        {"tool": "hallucinated_tool", "args": {}},
        {"tool": "ultron_summarize", "args": {"text": "a"}},   # exact duplicate
        {"tool": "ultron_summarize", "args": {"text": "b"}},   # different args
        {"args": {}},                                           # missing tool name
        {"tool": "ultron_recall", "arguments": {"query": "q"}},  # 'arguments' alias
    ], has_tool=lambda n: n in NATIVE_TOOL_NAMES)

    tools = [s.tool for s in plan.steps]
    check("unknown tool dropped", "hallucinated_tool" not in tools)
    check("unknown tool reported", "hallucinated_tool" in plan.dropped)
    check("exact duplicate collapsed", tools.count("ultron_summarize") == 2, str(tools))
    check("missing name reported", "(missing tool name)" in plan.dropped)
    check("'arguments' alias accepted",
          any(s.tool == "ultron_recall" and s.args == {"query": "q"} for s in plan.steps))
    check("max_steps enforced", len(plan.steps) <= 3, str(len(plan.steps)))
    check("source marked llm", plan.source == "llm")

    capped = UltronPlanner(max_steps=1)
    check("max_steps=1 keeps one step",
          len(capped._validate([{"tool": "ultron_recall"}] * 5,
                               lambda n: True).steps) == 1)


def test_planner_fallback() -> None:
    section("9. Planner failure handling")
    original = planner_mod.llm_chat_sync

    try:
        # LLM raises.
        planner_mod.llm_chat_sync = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no provider"))
        plan = UltronPlanner().create_plan("do a thing", "tools", lambda n: True)
        check("LLM failure -> fallback plan", plan.source == "fallback" and len(plan.steps) == 1)

        # LLM returns non-JSON.
        planner_mod.llm_chat_sync = lambda *a, **k: "I am not going to comply."
        plan = UltronPlanner().create_plan("do a thing", "tools", lambda n: True)
        check("unparseable reply -> fallback", plan.source == "fallback")

        # LLM returns a plan that is entirely hallucinated.
        # has_tool only accepts the real native tools, so 'nope' is rejected.
        planner_mod.llm_chat_sync = lambda *a, **k: json.dumps(
            {"steps": [{"tool": "nope", "args": {}}]})
        plan = UltronPlanner().create_plan(
            "do a thing", "tools", lambda n: n in NATIVE_TOOL_NAMES)
        check("all-hallucinated plan -> fallback", plan.source == "fallback", plan.source)

        # Happy path.
        planner_mod.llm_chat_sync = lambda *a, **k: json.dumps(
            {"steps": [{"tool": "ultron_summarize", "args": {"text": "x"},
                        "rationale": "condense it"}]})
        plan = UltronPlanner().create_plan("summarize", "tools", lambda n: True)
        check("valid plan accepted", plan.source == "llm" and plan.steps[0].tool == "ultron_summarize")

        # Replan.
        replan = UltronPlanner().replan("g", [{"tool": "t", "output": "o"}], "it broke",
                                       "tools", lambda n: True)
        check("replan produces steps", len(replan.steps) >= 1)
    finally:
        planner_mod.llm_chat_sync = original

    fb = UltronPlanner.fallback("original goal")
    check("fallback is safe and grounded",
          fb.steps[0].tool == "ultron_summarize" and fb.steps[0].args["text"] == "original goal")


# ---------------------------------------------------------------------------
# 10. Executor: classification
# ---------------------------------------------------------------------------
def test_error_classification() -> None:
    section("10. Executor error policy")
    check("success not classified as failure", not looks_failed("Found 3 results"))
    check("crash string detected", looks_failed("Tool 'x' failed: boom", "x"))
    check("unavailable action detected", looks_failed("Action 'x' is not available.", "x"))
    # Handlers prefix their errors, so a success whose body merely discusses
    # failure must not be misread — the marker has to appear in the head.
    long_ok = ("Results for your query. " * 30) + "It could not be more complete."
    check("head-scanned only (article text safe)", not looks_failed(long_ok))
    check("empty output is not a failure", not looks_failed(""))

    check("retry on transient", classify_error("Request timed out", "t", 2) is ErrorDecision.RETRY)
    check("skip on missing tool", classify_error("File not found: x", "t", 2) is ErrorDecision.SKIP)
    check("skip on not installed", classify_error("ffmpeg not installed", "t", 2) is ErrorDecision.SKIP)
    check("abort on user denial", classify_error("Denied by the user.", "t", 2) is ErrorDecision.ABORT)
    check("no retries left -> skip", classify_error("failed: x", "t", 0) is ErrorDecision.SKIP)

    # Permanent upstream failures must not be retried - retrying only burns the
    # step budget and delays the honest report.
    for permanent in ("This model requires an active paid plan or deposited balance.",
                      "Delegation failed: HTTPException: 401 invalid api key",
                      "Error: insufficient_quota for this account"):
        check(f"permanent failure skips retries: {permanent[:34]!r}",
              classify_error(permanent, "t", 2) is ErrorDecision.SKIP,
              str(classify_error(permanent, "t", 2)))

    check("destructive tool flagged", is_destructive("system_command", {}))
    check("destructive action flagged",
          is_destructive("file_controller", {"action": "delete"}))
    check("safe action not flagged",
          not is_destructive("file_controller", {"action": "read"}))
    check("non-destructive tool not flagged", not is_destructive("web_search", {}))


# ---------------------------------------------------------------------------
# 11. Executor: execution
# ---------------------------------------------------------------------------
def test_executor_execution() -> None:
    section("11. Executor execution + retry")
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        return "transient error, please try again" if calls["n"] < 3 else "all good"

    reg = make_registry({"flaky": flaky}, {"flaky": "flaky tool"})
    ex = UltronExecutor(reg)
    ex.max_attempts = 3

    session = make_session()
    res = ex.execute(session, "flaky", {})
    check("retries until success", res.ok and res.attempts == 3, f"attempts={res.attempts}")
    check("real output preserved", res.output == "all good")
    check("tool_call / tool_result events emitted",
          {e["type"] for e in drain(session)} >= {"tool_call", "tool_result"})

    # Exhausted retries.
    ex2 = UltronExecutor(make_registry({"always": lambda a: "timed out"}))
    ex2.max_attempts = 2
    res2 = ex2.execute(make_session(), "always", {})
    check("gives up after max_attempts", not res2.ok and res2.attempts == 2)

    # A permanent upstream failure is not retried.
    billing = make_registry({"premium": lambda a: "This model requires an active paid plan."})
    exb = UltronExecutor(billing)
    resb = exb.execute(make_session(), "premium", {})
    check("billing failure not retried", resb.attempts == 1, f"attempts={resb.attempts}")
    check("billing failure not ok", not resb.ok)

    # Unknown tool -> skip, no execution.
    res3 = ex2.execute(make_session(), "does_not_exist", {})
    check("unknown tool not executed", res3.attempts == 0 and res3.decision is ErrorDecision.SKIP)
    check("unknown tool explains itself", "does not exist" in res3.output)

    # Wrongly-named arg -> rejected before dispatch, never counted as success.
    ran = []
    reg3 = make_registry({"ultron_delegate": lambda a: ran.append(1) or "ok"})
    ex3 = UltronExecutor(reg3)
    res4 = ex3.execute(make_session(), "ultron_delegate", {"agent_id": "x", "task": "y"})
    check("renamed arg rejected before dispatch", res4.attempts == 0)
    check("renamed arg is a failure, not a success", not res4.ok)
    check("renamed arg is skippable", res4.decision is ErrorDecision.SKIP)
    check("renamed arg not dispatched", len(ran) == 0)
    check("rejection names the correct arg", "message" in res4.output, res4.output)

    # Stop mid-run.
    s = make_session()
    s.stop.set()
    res4 = ex2.execute(s, "always", {})
    check("stopped session runs nothing", res4.attempts == 0)


def test_executor_approval_gate() -> None:
    section("12. Executor approval gate")
    ran = []
    reg = make_registry({"risky": lambda a: ran.append(1) or "done"},
                        {"risky": "risky tool"})

    # Destructive tool, autonomous mode -> still gated.
    class RiskyDesktop(FakeDesktop):
        pass

    reg2 = make_registry({"system_command": lambda a: ran.append(1) or "ran it"},
                         {"system_command": "run a shell command"})
    ex = UltronExecutor(reg2)
    s = make_session(mode="autonomous")
    s.approve_q.put(("", True))                     # pre-approve
    res = ex.execute(s, "system_command", {})
    check("destructive tool requires approval", res.ok, res.output)

    # Denied -> abort, tool must not run.
    before = len(ran)
    s2 = make_session(mode="autonomous")
    s2.approve_q.put(("", False))
    res2 = ex.execute(s2, "system_command", {})
    check("denied step aborts", res2.decision is ErrorDecision.ABORT and not res2.ok)
    check("denied tool did not execute", len(ran) == before)
    check("denial surfaces to the model", "Denied" in res2.output)

    # Assisted mode gates every step.
    s3 = make_session(mode="assisted")
    s3.approve_q.put(("", True))
    res3 = ex.execute(s3, "ultron_recall", {"query": "x"})
    check("assisted mode gates non-destructive tools too",
          any(e["type"] == "approval_request" for e in drain(s3)) and res3.ok)


def test_placeholder_resolution() -> None:
    section("13. Planner placeholder resolution")
    check("stepN placeholder substituted",
          resolve_placeholders({"text": "summarize ${step1_output}"}, 1, "REAL DATA")
          == {"text": "summarize REAL DATA"})
    check("previous_output alias substituted",
          resolve_placeholders({"q": "${previous_output}"}, 1, "R") == {"q": "R"})
    check("hyphenated alias substituted",
          resolve_placeholders({"q": "${step-1_output}"}, 1, "R") == {"q": "R"})
    check("out-of-scope step placeholder becomes empty",
          resolve_placeholders({"t": "${step1_output}"}, 5, "R") == {"t": ""})
    check("unknown placeholder becomes empty",
          resolve_placeholders({"t": "${mystery}"}, 1, "R") == {"t": ""})
    check("nested structures resolved",
          resolve_placeholders({"a": {"b": ["${previous_output}"]}}, 1, "R")
          == {"a": {"b": ["R"]}})
    check("non-strings untouched",
          resolve_placeholders({"n": 3, "b": True}, 1, "R") == {"n": 3, "b": True})
    check("placeholder detector", has_unresolved_placeholder({"t": "${step1_output}"}))
    check("placeholder detector ignores real data",
          not has_unresolved_placeholder({"t": "plain text"}))

    # End to end: step 2 must receive step 1's real output, not the literal.
    seen = {}
    reg = make_registry({
        "fetch": lambda a: "THE ANSWER IS 42",
        "use_it": lambda a: (seen.update(a), "done")[1],
    })
    ex = UltronExecutor(reg)
    ex.execute_plan(make_session(), [
        {"tool": "fetch", "args": {}},
        {"tool": "use_it", "args": {"text": "${step1_output}"}},
    ])
    check("second step got the real previous output",
          seen.get("text") == "THE ANSWER IS 42", str(seen))
    check("literal placeholder never reaches the tool",
          "step1_output" not in json.dumps(seen), str(seen))


def test_noop_output_is_not_success() -> None:
    section("14. No-op outputs are not counted as success")
    # Real wording observed from ultron_summarize when handed a placeholder.
    noop = ("No user content was provided to summarize. The input field contains "
            "only a placeholder variable.")
    check("no-op output detected as failure", looks_failed(noop))
    check("no-op output is skippable", classify_error(noop, "ultron_summarize", 2)
          is ErrorDecision.SKIP)

    reg = make_registry({"noop_tool": lambda a: noop})
    ex = UltronExecutor(reg)
    ex.max_attempts = 1
    res = ex.execute(make_session(), "noop_tool", {})
    check("no-op tool result is not ok", not res.ok)

    # And therefore a run built only on no-op tools cannot report success.
    orch = orch_mod.UltronOrchestrator(reg, FakeMemory())
    orig_p, orig_o = planner_mod.llm_chat_sync, orch_mod.llm_chat_sync
    planner_mod.llm_chat_sync = lambda *a, **k: json.dumps(
        {"steps": [{"tool": "noop_tool", "args": {}}]})
    orch_mod.llm_chat_sync = lambda *a, **k: "report"
    try:
        s = make_session("a goal")
        orch.run(s)
    finally:
        planner_mod.llm_chat_sync, orch_mod.llm_chat_sync = orig_p, orig_o
    check("no-op run never reports completed", s.status != "completed", s.status)


def test_plan_rendering_survives_any_arg_type() -> None:
    section("15. Plan rendering is type-safe")
    # A planner may pass nested objects/lists to a tool that accepts one; the
    # report renderer used to assume every arg was a string and crashed the run.
    steps = [
        {"tool": "t1", "args": {"a": "x", "b": 3, "c": True, "n": None}},
        {"tool": "t2", "args": {"obj": {"deep": [1, 2, {"k": "v"}]}}},
        {"tool": "t3", "args": {"long": "y" * 500}},
        {"tool": "t4", "args": {}},
        {"tool": "t5"},
    ]
    try:
        rendered = orch_mod.json_lines(steps)
        check("nested arg types render without raising", isinstance(rendered, str))
        check("all steps rendered", all(s["tool"] in rendered for s in steps))
        check("long arg truncated", "..." in rendered)
        check("no newlines in one step", "\n" not in rendered.split("\n")[0])
    except Exception as exc:  # noqa: BLE001
        check("nested arg types render without raising", False,
              f"{type(exc).__name__}: {exc}")

    try:
        check("empty plan renders as '(no plan)'",
              orch_mod.json_lines([]) == "(no plan)")
        check("unserialisable arg falls back to str",
              "object" in orch_mod._render_arg(object()))
    except Exception as exc:  # noqa: BLE001
        check("unserialisable arg falls back to str", False, f"{type(exc).__name__}")


def test_executor_plan_loop() -> None:
    section("16. Executor plan loop")
    reg = make_registry({
        "ok_tool": lambda a: "fine",
        "bad_tool": lambda a: "File not found: nothing",
    })
    ex = UltronExecutor(reg)
    s = make_session()
    results = ex.execute_plan(s, [
        {"tool": "ok_tool", "args": {}},
        {"tool": "bad_tool", "args": {}},
        {"tool": "ok_tool", "args": {}},
    ])
    check("every step attempted", len(results) == 3)
    check("mixed results recorded", results[0].ok and not results[1].ok and results[2].ok)
    check("steps appended to the session trace", len(s.steps) == 3)
    check("step dict is serialisable", json.dumps(s.steps[0]) is not None)

# ---------------------------------------------------------------------------
# 14. Orchestrator: honest completion
# ---------------------------------------------------------------------------
def run_orchestrator(registry, goal, stop_after_first=False):
    """Drive the orchestrator with a stubbed LLM. Returns the session."""
    mem = FakeMemory()
    orch = orch_mod.UltronOrchestrator(registry, mem)

    def fake_llm(messages, **kw):
        # Planning call vs. reporting call.
        if any("planning stage" in str(m.get("content", "")) for m in messages):
            return json.dumps({"steps": orch.pending_plan})
        return "Final report: done."

    orch.pending_plan = [{"tool": "plan_tool", "args": {}}]
    orig_planner_llm, orig_orch_llm = planner_mod.llm_chat_sync, orch_mod.llm_chat_sync
    planner_mod.llm_chat_sync = fake_llm
    orch_mod.llm_chat_sync = fake_llm
    try:
        s = make_session(goal)
        if stop_after_first:
            s.stop.set()
        orch.run(s)
        return s
    finally:
        planner_mod.llm_chat_sync = orig_planner_llm
        orch_mod.llm_chat_sync = orig_orch_llm
    # The session is returned above with its event queue still populated, so
    # callers can assert on the exact event stream the SSE route would send.


def test_orchestrator_statuses() -> None:
    section("17. Orchestrator honest completion")

    # All steps succeed. Drain once — the queue is consumed by the first read,
    # and the event stream is what the SSE route would send.
    reg = make_registry({"plan_tool": lambda a: "everything worked"})
    s = run_orchestrator(reg, "do the thing")
    check("success -> completed", s.status == "completed", s.status)
    ev = drain(s)
    kinds = [e["type"] for e in ev]
    check("meta event emitted", "meta" in kinds)
    check("plan event emitted", "plan" in kinds)
    check("tool_call / tool_result emitted",
          "tool_call" in kinds and "tool_result" in kinds)
    check("done event closes the stream", kinds[-1] == "done", str(kinds))
    check("done carries the honest status",
          ev[-1].get("status") == "completed", str(ev[-1].get("status")))
    check("done carries a report", bool(ev[-1].get("summary")))

    # A step fails, replans are exhausted -> completed_with_issues, not "completed".
    reg2 = make_registry({"plan_tool": lambda a: "File not found: nothing here"})
    s2 = run_orchestrator(reg2, "do the failing thing")
    check("failure -> completed_with_issues", s2.status == "completed_with_issues", s2.status)
    check("failure never reported as completed", s2.status != "completed")

    # A tool the registry does not have: the planner drops it, falls back to
    # the degraded single step, and the goal must not be reported as done.
    reg3 = make_registry({})   # no 'plan_tool' anywhere
    s3 = run_orchestrator(reg3, "do the impossible thing")
    check("unplannable goal -> not reported complete",
          s3.status != "completed", s3.status)
    check("unplannable goal is reported as an issue",
          s3.status in ("failed", "completed_with_issues"), s3.status)

    # Stopped before starting.
    s4 = run_orchestrator(reg, "stop me", stop_after_first=True)
    check("stopped -> stopped", s4.status == "stopped", s4.status)
    check("stopped run still closes the stream",
          [e["type"] for e in drain(s4)][-1] == "done")


def test_orchestrator_records_memory() -> None:
    section("18. Orchestrator memory write-back")
    mem = FakeMemory()
    reg = make_registry({"plan_tool": lambda a: "fine"})
    orch = orch_mod.UltronOrchestrator(reg, mem)
    orig_p, orig_o = planner_mod.llm_chat_sync, orch_mod.llm_chat_sync
    planner_mod.llm_chat_sync = lambda *a, **k: json.dumps(
        {"steps": [{"tool": "plan_tool", "args": {}}]})
    orch_mod.llm_chat_sync = lambda *a, **k: "report"
    try:
        s = make_session("remember this goal")
        orch.run(s)
    finally:
        planner_mod.llm_chat_sync, orch_mod.llm_chat_sync = orig_p, orig_o
    check("experience written back", len(mem.experiences_written) == 1)
    check("goal recorded", mem.experiences_written[0]["goal"] == "remember this goal")
    check("success flag recorded", mem.experiences_written[0]["success"] is True)


# ---------------------------------------------------------------------------
# 16. API surface: auth, validation, errors
# ---------------------------------------------------------------------------
def test_api() -> None:
    section("19. API auth, validation and error handling")
    from app.api.deps import API_KEY
    from app.main import app

    client = TestClient(app)
    auth = {"X-API-Key": API_KEY} if API_KEY else {}

    # Health / metadata are open.
    h = client.get("/ultron/health")
    check("health 200", h.status_code == 200)
    check("health reports identity", h.json().get("id") == "sara_ultron_agent")
    check("health leaks no secrets",
          "key" not in json.dumps(h.json()).lower() or "apikey" not in json.dumps(h.json()).lower())

    info = client.get("/ultron/")
    check("registry entry 200", info.status_code == 200)
    check("registry entry has canonical id", info.json().get("id") == "sara_ultron_agent")

    caps = client.get("/ultron/capabilities")
    check("capabilities 200", caps.status_code == 200)
    body = caps.json()
    check("capabilities lists native tools",
          sorted(body.get("native_tools", [])) == sorted(NATIVE_TOOL_NAMES),
          f"got {body.get('native_tools')}")
    check("capabilities exposes the voice tools",
          {"ultron_listen", "ultron_speak"} <= set(body.get("native_tools", [])))
    check("capabilities lists desktop actions", len(body.get("desktop_actions", [])) > 0)
    check("capabilities exposes delegation allowlist",
          "backend-architect" in body.get("delegation", {}).get("agents", []))
    check("capabilities leaks no keys", "sk-" not in json.dumps(body))
    check("capabilities memory reports counts only",
          "experiences" in body.get("memory", {})
          and "content" not in json.dumps(body.get("memory", {})))

    # Validation.
    r = client.post("/ultron/run", json={"goal": "   "}, headers=auth)
    check("empty goal -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/ultron/run", json={"goal": "x" * 5000}, headers=auth)
    check("overlong goal -> 400", r.status_code == 400, str(r.status_code))

    # Auth (only meaningful when SARA_API_KEY is configured).
    r = client.post("/ultron/run", json={"goal": "hello"})
    if API_KEY:
        check("missing API key -> 401", r.status_code == 401, str(r.status_code))
        r = client.post("/ultron/run", json={"goal": "hello"},
                        headers={"X-API-Key": "wrong-key"})
        check("wrong API key -> 401", r.status_code == 401, str(r.status_code))
        # Auth is enforced on /stop too, independently of task existence.
        r = client.post("/ultron/stop", json={"task_id": "nope"},
                        headers={"X-API-Key": "wrong-key"})
        check("wrong key on /stop -> 401", r.status_code == 401, str(r.status_code))
    else:
        check("unconfigured key -> 503", r.status_code == 503, str(r.status_code))

    # Unknown task id.
    check("unknown task /status -> 404", client.get("/ultron/status?task_id=nope").status_code == 404)
    check("unknown task /events -> 404", client.get("/ultron/events?task_id=nope").status_code == 404)
    r = client.post("/ultron/approve", json={"task_id": "nope"}, headers=auth)
    check("unknown task /approve -> 404", r.status_code == 404, str(r.status_code))
    r = client.post("/ultron/stop", json={"task_id": "nope"}, headers=auth)
    check("unknown task /stop -> 404", r.status_code == 404, str(r.status_code))


# ---------------------------------------------------------------------------
# 17. Session store
# ---------------------------------------------------------------------------
def test_session_store() -> None:
    section("20. Session store")
    from app.agents.sara_ultron_agent.core.session import SessionStore

    store = SessionStore()
    s = store.create("goal here")
    check("session created with a uuid", len(s.task_id) == 36)
    check("session retrievable", store.get(s.task_id) is s)
    check("unknown id -> None", store.get("nope") is None)
    check("count tracks sessions", store.count() == 1)
    s.log("a message")
    s.emit("custom", value=1)
    kinds = [e["type"] for e in drain(s)]
    check("log and custom events emitted", kinds == ["log", "custom"])
    snap = s.snapshot()
    check("snapshot is JSON-serialisable", json.dumps(snap) is not None)
    check("snapshot carries the plan", "plan" in snap and "replans" in snap)
    store.clear()
    check("clear empties the store", store.count() == 0)


# ---------------------------------------------------------------------------
def main() -> int:
    print("SARA Ultron integration tests")
    print("=" * 66)
    for fn in (
        test_registration, test_regression, test_tool_registry, test_schema_conversion,
        test_delegation_allowlist, test_memory, test_planner_json_extraction,
        test_planner_validation, test_planner_fallback, test_error_classification,
        test_executor_execution, test_executor_approval_gate, test_placeholder_resolution,
        test_noop_output_is_not_success, test_plan_rendering_survives_any_arg_type,
        test_executor_plan_loop,
        test_orchestrator_statuses, test_orchestrator_records_memory, test_api,
        test_session_store,
    ):
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            _FAILURES.append(f"{fn.__name__} raised")

    print("\n" + "=" * 66)
    print(f"{_CHECKS - len(_FAILURES)}/{_CHECKS} checks passed")
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILED:")
        for f in _FAILURES:
            print("  -", f)
        return 1
    print("All SARA Ultron integration tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
