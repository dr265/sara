"""Audit every registered hub agent's backend route and report which are live.

Probes each agent's health/metadata endpoint against the running unified backend
and cross-references it with frontend/src/lib/agents.config.ts, so the answer to
"which agents should I show a client" is based on real responses, not on the
status field someone typed into a config file.

Run (with the backend up on :8000): python _audit_agents.py
"""
import json
import re
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "frontend" / "src" / "lib" / "agents.config.ts"

# agent id -> primary probe path (verified live on the unified backend)
PROBES = {
    "sara-ultron": "/ultron/health",
    "sara-auto": "/sara-auto/health",
    "voice-agent": "/voice-agent/health",
    "whatsapp-agent": "/whatsapp/health",
    "email-automation": "/email-agent/health",
    "trading-agent": "/trading/health",
    "social-media": "/social/health",
    "frontend-developer": "/frontend-developer/",
    "edulearner-ai": "/api/edulearner/health",
    "music-studio": None,          # no /health route; verified via UI/API separately
    "backend-architect": None,     # mounted app exposes no health route
    "telegram-assistant": None,    # mounted under /agent/* by telegram_host
    "ai-web-scraper": None,
    "game-forge": None,
}

# Note kept alongside so the final report can state WHY an agent is unverified.


def registered_agents():
    """Parse id / route / status out of the frontend agent registry."""
    if not CONFIG.exists():
        return {}
    text = CONFIG.read_text(encoding="utf-8")
    out = {}
    # Split on each object literal so a nested brace in one entry cannot make the
    # next entry's fields invisible to a non-greedy global match.
    for chunk in text.split("id:")[1:]:
        mid = re.search(r'id:\s*"([^"]+)"', chunk) or re.match(r'\s*"([^"]+)"', chunk)
        route = re.search(r'route:\s*"([^"]+)"', chunk)
        status = re.search(r'status:\s*"([^"]+)"', chunk)
        if mid and route and status:
            out[mid.group(1)] = {
                "route": route.group(1),
                "declared_status": status.group(1),
            }
    return out


def probe(path):
    """GET a probe path. Returns (status_or_None, short_body)."""
    try:
        r = httpx.get(BASE + path, timeout=25)
        return r.status_code, r.text[:200].replace("\n", " ")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:80]}"


print("=" * 78)
print("SARA HUB - LIVE AGENT AUDIT")
print("=" * 78)

reg = registered_agents()
print(f"\nFrontend registry lists {len(reg)} agents.\n")

rows = []
for agent_id, meta in reg.items():
    path = PROBES.get(agent_id)
    if path:
        status, body = probe(path)
        live = "LIVE" if status in (200, 201) else "DOWN"
    else:
        status, body, live = "n/a", "no health route exposed", "UNVERIFIED"
    rows.append((agent_id, meta["declared_status"], live, status, body[:80]))
    print(f"[{live:10}] {agent_id:22} declared={meta['declared_status']:11} http={status}")
    print(f"{'':15}{body[:80]}")

print("\n" + "=" * 78)
live_agents = [r[0] for r in rows if r[2] == "LIVE"]
unverified = [r[0] for r in rows if r[2] == "UNVERIFIED"]
declared_online = [r[0] for r in rows if r[1] == "online"]
print(f"VERIFIED LIVE ({len(live_agents)}): {live_agents}")
print(f"NOT PROBED    ({len(unverified)}): {unverified}")
print(f"BROKEN        ({len([r for r in rows if r[2] == 'DOWN'])}): "
      f"{[r[0] for r in rows if r[2] == 'DOWN']}")
print(f"Declared 'online' but NOT verified: "
      f"{sorted(set(declared_online) - set(live_agents))}")