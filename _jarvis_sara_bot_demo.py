"""Call a Sara Bot through SARA Desktop's own code path (Jarvis).

Uses ``core.sara_bots_client`` — the exact module ``main.py`` delegates to
for ADAPTIVE Stage 3 and SARA BOTS mode — to run a real approved local bot
via the platform's Sara Bots service.

Manual demo harness, not part of the test suite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent_registry import BackendConnection  # noqa: E402
from core.sara_bots_client import (format_result, list_bots,  # noqa: E402
                                   match_and_run)


def main() -> int:
    conn = BackendConnection()
    print(f"desktop backend connection: {conn.base_url}  "
          f"api_key_set={bool(conn.api_key)}", flush=True)

    bots = list_bots(conn)
    local = [b for b in bots if b.get("kind") == "local"
             and b.get("approved") and b.get("invocable")]
    print(f"Sara Bots discovered: {len(bots)} total, {len(local)} approved "
          f"+ invocable local bots", flush=True)

    request = "run a full system diagnostics report"
    print("request:", request, flush=True)
    result = match_and_run(request)
    print(json.dumps(result, indent=2, default=str)[:2500], flush=True)
    if result.get("ran"):
        print("-" * 60)
        print(format_result(result), flush=True)
    return 0 if result.get("ran") else 2


if __name__ == "__main__":
    raise SystemExit(main())
