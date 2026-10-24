# tests/test_hub_actions.py
"""Every main Hub agent has a voice-callable desktop action (music_studio pattern).

Verifies, fully offline:
  * action_loader discovers all 14 hub actions with real handlers bound;
  * every TOOL schema is well-formed (name, description, required params);
  * handlers fail HONESTLY (no fake success) when the backend is down or
    credentials are missing;
  * representative payload builders / report formatters behave.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests  # noqa: E402

from core import hub_agents_client as hub  # noqa: E402
from core.action_loader import discover_actions  # noqa: E402

# The 14 main Hub agents (frontend/src/lib/agents.config.ts) as action names.
HUB_ACTION_NAMES = [
    "sara_ultron",
    "sara_auto",
    "ai_web_scraper",
    "music_studio",
    "email_automation",
    "social_media",
    "whatsapp_agent",
    "trading_agent",
    "telegram_assistant",
    "voice_agent",
    "edulearner_ai",
    "game_forge",
    "backend_architect",
    "frontend_developer",
]


def _conn_error(*_a, **_k):
    raise requests.exceptions.ConnectionError("backend down (test)")


class DiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs: list[str] = []
        cls.registry = discover_actions(DESKTOP / "actions", set(), logs.append)

    def test_all_14_hub_actions_registered_with_real_handlers(self):
        for name in HUB_ACTION_NAMES:
            self.assertIn(name, set(self.registry.names()),
                          f"hub action '{name}' was not discovered")
            rec = self.registry._actions.get(name)
            self.assertIsNotNone(rec)
            self.assertTrue(callable(rec.handler),
                            f"hub action '{name}' has no bound handler")

    def test_tool_schemas_are_well_formed(self):
        for name in HUB_ACTION_NAMES:
            rec = self.registry._actions[name]
            self.assertTrue(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", rec.name),
                            f"bad tool name: {rec.name}")
            self.assertGreater(len(rec.description or ""), 40,
                               f"{name}: description too short for routing")
            params = rec.parameters or {}
            self.assertEqual(params.get("type"), "OBJECT", f"{name}: params not OBJECT")
            props = params.get("properties") or {}
            for required in params.get("required", []):
                self.assertIn(required, props,
                              f"{name}: required '{required}' missing from properties")

    def test_sara_ultron_requires_a_goal(self):
        rec = self.registry._actions["sara_ultron"]
        self.assertEqual(rec.parameters.get("required"), ["goal"])

    def test_scraper_requires_url_and_prompt(self):
        rec = self.registry._actions["ai_web_scraper"]
        self.assertEqual(sorted(rec.parameters.get("required") or []), ["prompt", "url"])


class HonestFailureTest(unittest.TestCase):
    """Backend down -> honest 'unreachable'/'not reachable' text, never success."""

    def _assert_unreachable(self, text: str, action: str):
        lowered = text.lower()
        self.assertTrue(
            ("unreachable" in lowered) or ("not reachable" in lowered)
            or ("could not" in lowered) or ("rejected" in lowered),
            f"{action} did not report failure honestly: {text!r}")

    def test_run_actions_report_unreachable(self):
        from actions import sara_ultron, sara_auto, voice_agent, game_forge, backend_architect
        from actions import whatsapp_agent, edulearner_ai, trading_agent

        cases = [
            (sara_ultron.run_ultron, {"goal": "test goal"}),
            (sara_auto.run_sara_auto, {"goal": "test goal"}),
            (voice_agent.run_voice_agent, {"action": "command", "command": "email a@b.com"}),
            (game_forge.forge_game, {"prompt": "a small test game"}),
            (backend_architect.analyse_repo, {"task": "review the repo"}),
            (whatsapp_agent.run_whatsapp_agent, {"text": "hello"}),
            (edulearner_ai.run_edulearner, {"message": "what is 2+2?"}),
            (trading_agent.run_trading_agent, {"action": "demo", "candles": 300}),
        ]
        with mock.patch.object(hub, "post", _conn_error), \
             mock.patch.object(hub, "post_form", _conn_error), \
             mock.patch.object(hub, "get", _conn_error):
            for handler, params in cases:
                text = handler(params)
                self._assert_unreachable(text, handler.__name__)

    def test_scraper_reports_unreachable(self):
        from actions import ai_web_scraper
        with mock.patch("requests.post", _conn_error):
            text = ai_web_scraper.scrape_web(
                {"url": "example.com", "prompt": "title"})
        self._assert_unreachable(text, "scrape_web")

    def test_status_actions_report_not_reachable(self):
        from actions import whatsapp_agent, telegram_assistant, voice_agent
        from actions import edulearner_ai, frontend_developer, social_media, trading_agent
        cases = [
            (whatsapp_agent.run_whatsapp_agent, {"action": "status"}),
            (telegram_assistant.run_telegram_assistant, {"action": "status"}),
            (voice_agent.run_voice_agent, {"action": "status"}),
            (edulearner_ai.run_edulearner, {"action": "status"}),
            (frontend_developer.run_frontend_developer, {"action": "status"}),
            (social_media.run_social_media, {"action": "status"}),
            (trading_agent.run_trading_agent, {"action": "status"}),
        ]
        with mock.patch.object(hub, "health", lambda *a, **k: None), \
             mock.patch.object(hub, "get", _conn_error):
            for handler, params in cases:
                text = handler(params)
                self._assert_unreachable(text, handler.__name__)

    def test_email_create_needs_a_brief(self):
        from actions import email_automation
        text = email_automation.run_email_automation({"action": "create"})
        self.assertIn("Tell me what the campaign is about", text)

    def test_frontend_developer_without_credentials_creates_nothing(self):
        from actions import frontend_developer
        with mock.patch.object(hub, "env_value", lambda _k: ""):
            text = frontend_developer.run_frontend_developer(
                {"action": "task", "title": "Fix the header"})
        self.assertIn("needs a login", text)
        self.assertIn("Nothing was created", text)

    def test_social_schedule_without_accounts_creates_nothing(self):
        from actions import social_media
        with mock.patch.object(hub, "get", lambda *a, **k: mock.Mock(
                status_code=200, json=lambda: {"accounts": []})):
            text = social_media.run_social_media(
                {"action": "schedule", "content": "Hello world"})
        self.assertIn("No social account is connected", text)


class PayloadAndFormatTest(unittest.TestCase):
    def test_scraper_build_payload_normalises_url_and_fields(self):
        from actions import ai_web_scraper
        payload = ai_web_scraper._build_payload({
            "url": "example.com",
            "prompt": "names and prices",
            "mode": "crawl",
            "max_pages": 999,
            "schema_fields": "name, price ,rating",
        })
        self.assertEqual(payload["url"], "https://example.com")
        self.assertEqual(payload["mode"], "crawl")
        self.assertEqual(payload["maxPages"], 50)  # clamped
        self.assertEqual(payload["schemaFields"], ["name", "price", "rating"])

    def test_scraper_build_payload_rejects_missing_pieces(self):
        from actions import ai_web_scraper
        with self.assertRaises(ValueError):
            ai_web_scraper._build_payload({"prompt": "title"})
        with self.assertRaises(ValueError):
            ai_web_scraper._build_payload({"url": "example.com"})

    def test_trading_report_mentions_real_metrics(self):
        from actions import trading_agent
        text = trading_agent._report_run({
            "candles": 2500,
            "signals": 312,
            "final": {"accuracy": 0.6234},
            "trading": {"count": 45, "win_rate": 0.58, "pnl": 12.4},
        }, "demo candles")
        self.assertIn("312 signals", text)
        self.assertIn("45 paper trades", text)
        self.assertIn("12.4", text)

    def test_email_create_draft_message(self):
        from actions import email_automation
        with mock.patch.object(hub, "post", lambda *a, **k: mock.Mock(
                status_code=200, json=lambda: {"id": "abcd1234-xyz", "status": "draft"})):
            text = email_automation.run_email_automation(
                {"action": "create", "context": "promote the new espresso blend"})
        self.assertIn("draft", text.lower())
        self.assertIn("abcd1234", text)
        self.assertIn("dry-run", text)

    def test_whatsapp_status_reports_missing_transport_honestly(self):
        from actions import whatsapp_agent
        with mock.patch.object(hub, "health", lambda *a, **k: {
                "status": "ok", "configured": False, "business_context_loaded": True}):
            text = whatsapp_agent.run_whatsapp_agent({"action": "status"})
        self.assertIn("no live transport", text)

    def test_hub_brief_truncates_and_flattens(self):
        text = hub.brief({"a": "x" * 100}, limit=30)
        self.assertLessEqual(len(text), 30)
        self.assertNotIn("\n", text)

    def test_hub_detail_of_extracts_detail_field(self):
        resp = mock.Mock(status_code=422)
        resp.json = lambda: {"detail": "A game prompt is required"}
        self.assertEqual(hub.detail_of(resp), "A game prompt is required")
        self.assertIn("A game prompt is required", hub.http_failure("X", resp))


if __name__ == "__main__":
    unittest.main()
