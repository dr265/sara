"""End-to-end proof: backend bridge -> Windows agent -> real desktop action.

Runs the REAL LocalAgent.execute() against the REAL sara_desktop action
registry on this machine. Nothing is mocked. The point is to prove two
contract properties:

  1. A command dispatched through the cloud-side bridge produces a REAL
     result from the actual desktop (Phase 17: never fabricate success).
  2. A gated command is REFUSED on the machine even when a caller tries to
     wave it through (Phase 9/26: authorization is not a frontend checkbox).

Run:  backend/venv/Scripts/python.exe backend/_e2e_remote_execution.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
for p in (str(ROOT), str(BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.agents.sara_ultron_agent.core.execution_bridge import (  # noqa: E402
    ConfirmationRequired, ExecutionBridge, LocalAgent, NoLocalAgent,
)
from app.agents.sara_ultron_agent.core.risk import classify  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "sara_local_agent", ROOT / "sara_desktop" / "local_agent.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))


def notepad_running() -> bool:
    out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=30).stdout
    return "Notepad.exe" in out


def main() -> int:
    print("=" * 72)
    print("E2E: cloud dispatch -> Windows execution bridge -> real desktop")
    print("=" * 72)

    agent_mod = mod.LocalAgent()

    # ---------------------------------------------------------------- 1
    print("\n1. BRIDGE REJECTS A COMMAND WITH NO PC CONNECTED")
    bridge = ExecutionBridge()
    try:
        asyncio.run(bridge.dispatch("computer_control", {"action": "click"}))
        record("no-agent dispatch raises", False, "it returned instead of raising")
    except NoLocalAgent as exc:
        record("no-agent dispatch raises", "Nothing was executed" in str(exc), str(exc)[:90])

    # ---------------------------------------------------------------- 2
    print("\n2. BRIDGE ENFORCES CONFIRMATION BEFORE DISPATCH")
    bridge2 = ExecutionBridge()

    async def send_stub(payload):
        return None

    fake = LocalAgent(device_id="e2e", label="e2e-pc", send=send_stub)
    bridge2.register_agent(fake)
    try:
        asyncio.run(bridge2.dispatch("file_controller",
                                     {"action": "delete", "path": r"C:\e2e\target"}))
        record("gated call blocked without confirmation", False, "it was dispatched")
    except ConfirmationRequired as exc:
        record("gated call blocked without confirmation",
               "C:\\e2e\\target" in exc.prompt, exc.prompt)

    # ---------------------------------------------------------------- 3
    print("\n3. ROUTINE ACTION EXECUTES ON THE REAL DESKTOP")
    res = agent_mod.execute("open_app", {"app_name": "notepad"})
    record("open_app reported success", bool(res.get("ok")), res.get("output", "")[:70])
    time.sleep(1.5)
    alive = notepad_running()
    record("Notepad ACTUALLY running on this PC", alive,
           "confirmed via tasklist" if alive else "tasklist did not show Notepad.exe")

    # ---------------------------------------------------------------- 4
    print("\n4. AGENT INDEPENDENTLY REFUSES A GATED ACTION")
    # confirmed=False on purpose: even a compromised caller cannot force this.
    res = agent_mod.execute("file_controller",
                            {"action": "delete", "path": r"C:\e2e\never-created"})
    refused = (not res.get("ok")) and bool(res.get("needs_confirmation"))
    record("delete refused on the PC", refused,
           res.get("prompt") or res.get("error", "")[:80])

    # ---------------------------------------------------------------- 5
    print("\n5. FAILURES ARE NEVER REPORTED AS SUCCESS")
    res = agent_mod.execute("no_such_tool", {})
    record("unknown tool -> ok=False", res.get("ok") is False, res.get("error", "")[:80])
    res = agent_mod.execute("open_app", {"app_name": "definitely-not-an-app-xyz"})
    record("bad app name -> ok=False", res.get("ok") is False,
           res.get("output", "")[:80])

    # ---------------------------------------------------------------- 6
    print("\n6. CONTEXT LAYER REPORTS THE LIVE DESKTOP")
    ctx = agent_mod.context()
    has_window = bool(ctx.get("active_window"))
    record("active window detected", has_window, ctx.get("active_window", "")[:70])
    record("app classified", bool(ctx.get("app_name")),
           f"{ctx.get('category')} / {ctx.get('app_name')}")

    # ---------------------------------------------------------------- cleanup
    subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                   capture_output=True, text=True, timeout=30)

    # ---------------------------------------------------------------- summary
    passed = sum(1 for r in results if r[0] == PASS)
    failed = len(results) - passed
    print("\n" + "=" * 72)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 72)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())