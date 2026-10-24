"""Drive the SARA Hub music-studio agent through the real Jarvis runtime.

This uses the same code path the desktop app uses when you ask Jarvis (in
Agentic mode) to generate music:

    AgenticRuntime.submit(request)
      -> TaskRouter.plan_task          (LLM selects the agent + params)
      -> TaskRouter.execute_task       (invokes the agent for real)
      -> POST {backend}/api/music/generate
      -> the Hub's AI Music Studio renders a real audio file

It is a manual demo harness, not part of the test suite.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent_registry import BackendConnection, UnifiedAgentRegistry  # noqa: E402
from core.agentic_runtime import AgenticRuntime  # noqa: E402
from core.agent_router import TaskRouter  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled"}


def main() -> int:
    conn = BackendConnection()
    reg = UnifiedAgentRegistry(connection=conn)
    reg.discover_all(DESKTOP / "actions", DESKTOP / "plugins", set(),
                     action_registry=None)
    reg.refresh()

    def emit(kind: str, payload: dict) -> None:
        if kind == "task":
            phase = payload.get("phase", "?")
            task = payload.get("task") or {}
            print(f"[jarvis-event] task phase={phase} status={task.get('status')}",
                  flush=True)

    rt = AgenticRuntime(reg, emit=emit)
    rt.router = TaskRouter(reg)
    rt.set_mode("agentic")
    print("mode:", rt.mode.value, flush=True)

    info = reg.get_agent("music-studio")
    if info is None:
        print("music-studio agent NOT discovered"); return 1
    print(f"music-studio: state={info.state} invocable={info.invocable} "
          f"path={info.invoke_path}", flush=True)

    request = ("Use the music studio to generate a 20 second calm ambient "
               "piano track. Genre: Ambient. Mood: Calm. Duration: 20 seconds.")
    print("request:", request, flush=True)
    decision = rt.submit(request, source="text")
    print("decision:", json.dumps(decision, default=str)[:500], flush=True)

    task_id = decision.get("task_id")
    if not task_id:
        return 1

    t0 = time.time()
    task = rt.tasks.get(task_id)
    while task is not None and task.status.value not in TERMINAL \
            and time.time() - t0 < 420:
        time.sleep(2)
        task = rt.tasks.get(task_id)

    task = rt.tasks.get(task_id)
    print("-" * 60)
    print("status :", task.status.value)
    print("agents :", task.agents)
    print("error  :", task.error or "")
    print("result :", (task.result_text or "")[:900])
    for step in (task.steps or []):
        print("step   :", json.dumps(step, default=str)[:300])
    return 0 if task.status.value == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
