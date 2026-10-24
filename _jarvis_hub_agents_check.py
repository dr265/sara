# _jarvis_hub_agents_check.py
"""Live end-to-end check for the Hub-agent desktop actions.

Runs REAL calls against the running backend through the actual action
handlers (the same functions Jarvis voice mode invokes). Usage:

    python _jarvis_hub_agents_check.py                 -> quick checks
    python _jarvis_hub_agents_check.py <check> [...]   -> selected checks
    python _jarvis_hub_agents_check.py all             -> quick + long checks

Long checks (game build, architect run, ultron/auto runs) submit real work.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT / "sara_desktop"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from actions import (  # noqa: E402
    ai_web_scraper, backend_architect, edulearner_ai, email_automation,
    frontend_developer, game_forge, sara_auto, sara_ultron, social_media,
    telegram_assistant, trading_agent, voice_agent, whatsapp_agent,
)

QUICK = {
    "trading_status": lambda: trading_agent.run_trading_agent({"action": "status"}),
    "trading_demo": lambda: trading_agent.run_trading_agent(
        {"action": "demo", "candles": 300}),
    "whatsapp_status": lambda: whatsapp_agent.run_whatsapp_agent({"action": "status"}),
    "whatsapp_chat": lambda: whatsapp_agent.run_whatsapp_agent(
        {"text": "Hello! In one short sentence, what can you help me with?"}),
    "telegram_status": lambda: telegram_assistant.run_telegram_assistant(
        {"action": "status"}),
    "social_status": lambda: social_media.run_social_media({"action": "status"}),
    "voice_status": lambda: voice_agent.run_voice_agent({"action": "status"}),
    "email_status": lambda: email_automation.run_email_automation({"action": "status"}),
    "email_create": lambda: email_automation.run_email_automation({
        "action": "create",
        "name": "Jarvis voice check",
        "context": "A friendly note introducing SARA's new AI photo editor to existing customers.",
        "objective": "get replies from curious customers",
    }),
    "edulearner_status": lambda: edulearner_ai.run_edulearner({"action": "status"}),
    "edulearner_ask": lambda: edulearner_ai.run_edulearner(
        {"message": "In two short sentences, what is spaced repetition?"}),
    "scraper_run": lambda: ai_web_scraper.scrape_web({
        "url": "https://example.com",
        "prompt": "Extract the page title and the main heading text",
    }),
    "frontend_status": lambda: frontend_developer.run_frontend_developer(
        {"action": "status"}),
}

LONG = {
    "ultron_run": lambda: sara_ultron.run_ultron({
        "goal": "In one short sentence, tell me which agent you are.",
        "wait_seconds": 120,
    }),
    "sara_auto_run": lambda: sara_auto.run_sara_auto({
        "goal": "In one short sentence, say hello and confirm you are ready.",
        "wait_seconds": 120,
    }),
    "architect_run": lambda: backend_architect.analyse_repo({
        "task": ("List the top-level modules of this repository and summarize "
                 "what the application does in three bullet points."),
        "repo": str(ROOT),
        "wait_seconds": 180,
    }),
    "game_forge_run": lambda: game_forge.forge_game({
        "prompt": ("A simple one-button arcade game: press SPACE to score points "
                   "and dodge falling blocks. Bright colors, small file."),
        "wait_seconds": 420,
    }),
}

ALL = {**QUICK, **LONG}

if __name__ == "__main__":
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not args:
        selected = list(QUICK)
    elif args == ["all"]:
        selected = list(ALL)
    else:
        selected = args

    failures = 0
    for name in selected:
        fn = ALL.get(name)
        if fn is None:
            print(f"== {name}: UNKNOWN CHECK")
            failures += 1
            continue
        print(f"== {name} ...")
        started = time.time()
        try:
            result = fn()
            ok_text = str(result)
        except Exception as exc:  # noqa: BLE001 - report, never hide
            ok_text = f"EXCEPTION {exc.__class__.__name__}: {exc}"
        print(f"   [{time.time() - started:.1f}s] {ok_text}")
        lowered = ok_text.lower()
        if ("unreachable" in lowered or "not reachable" in lowered
                or "exception" in lowered or "rejected the request" in lowered):
            failures += 1
            print("   ^^ flagged as failure")
    print(f"\nDone: {len(selected) - failures}/{len(selected)} checks returned a real result.")
