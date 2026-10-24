"""Dry-run every bridged action name and report the ones that are dead ends.

A tool that advertises an action it does not implement is exactly how Ultron
ends up reporting a successful run while nothing happened. This executes each
declared action once with no arguments and classifies the reply, so the
inventory is checked rather than assumed.

Safety
------
Destructive actions are **listed, never invoked** - both the tools gated
wholesale (``RISKY_TOOLS``) and the individual actions gated by
``DESTRUCTIVE_BRAHMA_ACTIONS``. A check harness that ran ``shutdown``,
``file_controller/delete`` or ``desktop/clean`` unattended would be a bug worse
than the dead ends it is looking for, so it obeys the same approval gate the
executor does. The gate is applied here *and* re-checked in the test suite, so a
newly discovered action cannot slip through by being forgotten in one place.

Everything else really does run: these are the volume moves, screenshots and
clicks that make up the bulk of the surface. Run it deliberately.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agents.sara_ultron_agent.core.brahma_bridge import (  # noqa: E402
    LIVE_BROWSER_TOOLS, RISKY_TOOLS, BrahmaBridge,
)
from app.agents.sara_ultron_agent.core.executor import (  # noqa: E402
    DESTRUCTIVE_BRAHMA_ACTIONS,
)

#: Replies that prove the action name itself is wrong. Deliberately narrow: a
#: real error ("file not found", "connection failed") proves the action *ran*.
DEAD_END = re.compile(
    r"unknown action|unrecognized action|invalid action|could not match|"
    r"unsupported action|action not found|no such action|"
    r"unknown (?:command|option)|no handler for",
    re.I,
)


def classify(out: str) -> tuple[str, str]:
    text = " ".join(str(out).split())
    if DEAD_END.search(text):
        return "DEAD", text[:150]
    return "ok", text[:110]


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    bridge = BrahmaBridge().load()
    stats = bridge.stats()

    print("=" * 96)
    print(f"Brahma bridge: {stats['available']} tools, "
          f"{stats['unavailable']} unavailable, source={stats['source_dir']}")
    print("=" * 96)

    dead: list[tuple[str, str, str]] = []
    checked = 0
    skipped = 0
    rows: list[str] = []

    for name in stats["available_names"]:
        record = bridge.record(name)
        if record is None:
            continue
        schema = record.parameters or {}
        enum = (schema.get("properties", {}).get("action", {}) or {}).get("enum")

        if not enum:
            rows.append(f"          {name:32} (free-form args)")
            continue

        if name in RISKY_TOOLS:
            skipped += len(enum)
            rows.append(f"  GATED  {name:32} {len(enum):3} actions "
                        f"(not invoked: tool gated)")
            continue

        if name in LIVE_BROWSER_TOOLS:
            skipped += len(enum)
            rows.append(f"  LIVE   {name:32} {len(enum):3} actions "
                        f"(not invoked: drives a real MCP browser session; "
                        f"see tests/test_sara_bots_custom.py)")
            continue

        destructive = DESTRUCTIVE_BRAHMA_ACTIONS.get(name, set())
        results = []
        for action in enum:
            if action in destructive:
                skipped += 1
                results.append("~")
                continue
            checked += 1
            try:
                out = bridge.run(name, {"action": action})
            except BaseException as exc:  # noqa: BLE001
                out = f"EXCEPTION {type(exc).__name__}: {exc}"
            verdict, text = classify(out)
            if verdict == "DEAD":
                dead.append((name, action, text))
            results.append("." if verdict == "ok" else "X")
        rows.append(f"          {name:32} {len(enum):3} actions "
                    f"[{''.join(results)}]  (. ok  X dead  ~ gated)")

    print("\n-- per-tool action dry run --")
    for row in rows:
        print(row)

    print("\n" + "=" * 96)
    print(f"actions exercised : {checked}")
    print(f"actions skipped   : {skipped}  (destructive; listed, not invoked)")
    print(f"dead ends         : {len(dead)}")
    print("=" * 96)
    for tool, action, text in dead:
        print(f"  DEAD  {tool} / {action}: {text}")

    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
