"""Tests for core.sara_bots_client — offline, no service and no network.

The matching/parameter logic decides whether Jarvis is allowed to auto-run a
Sara Bot, so it is tested hard: it must pick the right bot, fill parameters
only from words the user actually said, and refuse every destructive option.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.sara_bots_client import (_words, pick_bot,  # noqa: E402
                                   resolve_params, score_bot)

DIAGNOSTICS = {
    "id": "action.system_diagnostics", "name": "System Diagnostics",
    "kind": "local", "state": "ready", "approved": True, "invocable": True,
    "capabilities": ["system_diagnostics"],
    "description": "Battery, brightness, top processes, disk usage report.",
    "accepts": {"action": {
        "type": "string", "required": True,
        "description": ("battery | brightness | set_brightness | "
                        "top_processes | kill | disk | full")}},
}
WEATHER = {
    "id": "action.weather_report", "name": "Weather Report",
    "kind": "local", "state": "ready", "approved": True, "invocable": True,
    "capabilities": ["weather"], "description": "Fetches live weather.",
    "accepts": {"city": {"type": "string", "required": True,
                         "description": "City name"}},
}
HUB_MUSIC = {"id": "music-studio", "name": "Music Studio", "kind": "hub",
             "state": "available", "approved": True, "invocable": False,
             "capabilities": ["music"], "accepts": {}}
UNAPPROVED = {"id": "action.rogue", "name": "Rogue Rogue", "kind": "local",
              "state": "ready", "approved": False, "invocable": True,
              "capabilities": ["rogue"], "accepts": {}}
BOTS = [DIAGNOSTICS, WEATHER, HUB_MUSIC, UNAPPROVED]


class TestMatching(unittest.TestCase):

    def test_words_drop_stopwords_and_short_words(self):
        words = _words("please run a FULL system diagnostics report for me")
        self.assertIn("full", words)
        self.assertIn("diagnostics", words)
        self.assertNotIn("run", words)      # stopword
        self.assertNotIn("a", words)        # short

    def test_picks_system_diagnostics_for_diagnostics_request(self):
        words = _words("run a full system diagnostics report")
        bot = pick_bot(words, BOTS)
        self.assertIsNotNone(bot)
        self.assertEqual(bot["id"], "action.system_diagnostics")

    def test_hub_and_unapproved_bots_are_never_picked(self):
        words = _words("use music studio to make a rogue track")
        self.assertIsNone(pick_bot(words, BOTS))

    def test_no_match_below_threshold(self):
        words = _words("write me a poem about the sea")
        self.assertIsNone(pick_bot(words, BOTS))

    def test_hub_bots_do_not_leak_into_local_pick(self):
        self.assertEqual(score_bot(_words("music studio"), HUB_MUSIC), 2)
        self.assertIsNone(pick_bot(_words("music studio"), BOTS))


class TestParamResolution(unittest.TestCase):

    def test_full_report_from_report_word(self):
        request = "run a full system diagnostics report"
        params, why = resolve_params(DIAGNOSTICS, request, _words(request))
        self.assertEqual(params, {"action": "full"})
        self.assertEqual(why, "")

    def test_disk_action_from_disk_word(self):
        request = "check my disk usage"
        params, why = resolve_params(DIAGNOSTICS, request, _words(request))
        self.assertEqual(params, {"action": "disk"})

    def test_kill_is_never_auto_selected(self):
        request = "kill the chrome app"
        params, why = resolve_params(DIAGNOSTICS, request, _words(request))
        self.assertIsNone(params)
        self.assertIn("kill", why)

    def test_missing_required_param_refuses(self):
        request = "get the weather report"
        params, why = resolve_params(WEATHER, request, _words(request))
        self.assertIsNone(params)
        self.assertIn("city", why)

    def test_city_extracted_from_request(self):
        request = "get the weather report in London"
        params, why = resolve_params(WEATHER, request, _words(request))
        self.assertEqual(params, {"city": "London"})


if __name__ == "__main__":
    unittest.main()
