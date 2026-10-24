"""
e2e_live.py — live end-to-end check of Sara Agentic Mode (run with the
project venv while the backend is up):

    & sara_desktop\\venv\\Scripts\\python.exe e2e_live.py "scrape example.com and extract the headings"

Routes a spoken-style request through the REAL pipeline (intent → agent
selection → execution → verification) and prints the verified outcome.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.agent_registry import get_global_registry
from core.agentic_runtime import AgenticRuntime
from core.agent_router import TaskRouter


def main() -> int:
    request = sys.argv[1] if len(sys.argv) > 1 else \
        "scrape example.com and extract the headings"
    reg = get_global_registry()
    reg.discover_all(HERE / "actions", HERE / "plugins")
    # live readiness pass — without it the backend fleet is unprobed and
    # correctly NOT selectable (a registered agent is not a working agent).
    reg.refresh(timeout=30.0)
    print("inventory:", reg.state_counts())
    rt = AgenticRuntime(reg)
    rt.router = TaskRouter(reg)
    rt.set_mode("agentic")
    events: list = []
    rt.set_emit(lambda kind, payload: events.append((kind, payload)))

    t0 = time.time()
    decision = rt.submit(request, source="voice")
    print(f"DECISION ({time.time() - t0:.1f}s): {decision}")
    task_id = decision.get("task_id")
    if not task_id:
        return 1
    while time.time() - t0 < 180:
        task = rt.tasks.get(task_id)
        if task and not task.active:
            break
        time.sleep(2)
    task = rt.tasks.get(task_id)
    print(f"\nSTATUS: {task.status.value}  elapsed: {task.elapsed}s")
    print(f"AGENTS: {task.agents}")
    print(f"RESULT:\n{(task.result_text or '')[:1500]}")
    if task.error:
        print(f"ERROR: {task.error[:500]}")
    print("\nLIFECYCLE EVENTS:")
    for e in task.events:
        print(f"  [{e['kind']}] {e['text'][:120]}")
    return 0 if task.status.value == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
