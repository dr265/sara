# tests/test_agency_opener_hub.py
"""Hub-workspace opening + music action payloads.

Regression for the live bug where "open the music studio agent" answered
"Could not resolve ..." because agency_opener knew only the 267 agency
specialists and nothing about the Hub service workspaces (frontend
/hub/* pages: Music Studio, Sara Muse, Sara Ultron, Game Forge, ...).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from actions import agency_opener as ao  # noqa: E402
from actions import music_studio as ms  # noqa: E402


class HubRouteResolutionTest(unittest.TestCase):
    def test_exact_hub_names(self):
        self.assertEqual(ao._resolve_hub_route("music studio"), "music-studio")
        self.assertEqual(ao._resolve_hub_route("sara muse"), "sara-muse")
        self.assertEqual(ao._resolve_hub_route("ultron"), "sara-ultron")
        self.assertEqual(ao._resolve_hub_route("game forge"), "game-forge")
        self.assertEqual(ao._resolve_hub_route("music-studio"), "music-studio")

    def test_spoken_inside_a_sentence(self):
        self.assertEqual(
            ao._resolve_hub_route_fuzzy("open the music studio workspace"),
            "music-studio",
        )
        self.assertEqual(
            ao._resolve_hub_route_fuzzy("please open sara muse"), "sara-muse"
        )

    def test_short_keys_do_not_fire_inside_words(self):
        # "muse" (4 chars) must not match inside "amuse".
        self.assertIsNone(ao._resolve_hub_route_fuzzy("amuse me with a joke"))

    def test_common_words_are_not_hijacked(self):
        self.assertIsNone(ao._resolve_hub_route_fuzzy("make music"))
        self.assertIsNone(ao._resolve_hub_route_fuzzy("what is the weather"))


class OpenerBehaviorTest(unittest.TestCase):
    def test_music_studio_opens_hub_page(self):
        with mock.patch.object(ao.webbrowser, "open") as opened:
            reply = ao.open_agency_workspace({"agent": "music studio"})
        opened.assert_called_once()
        self.assertIn("/hub/music-studio", opened.call_args[0][0])
        self.assertIn("Opened", reply)

    def test_full_sentence_opens_hub_page(self):
        with mock.patch.object(ao.webbrowser, "open") as opened:
            ao.open_agency_workspace({"agent": "open the music studio agent"})
        self.assertIn("/hub/music-studio", opened.call_args[0][0])

    def test_explicit_hub_slug_as_agent_id(self):
        with mock.patch.object(ao.webbrowser, "open") as opened:
            ao.open_agency_workspace({"agent_id": "music-studio"})
        self.assertIn("/hub/music-studio", opened.call_args[0][0])

    def test_agency_names_still_win(self):
        # "backend architect" is an agency specialist - must not be hijacked.
        with mock.patch.object(ao.webbrowser, "open") as opened:
            ao.open_agency_workspace({"agent": "backend architect"})
        self.assertIn("/agency/engineering-backend-architect",
                      opened.call_args[0][0])

    def test_unknown_name_reports_both_catalogs(self):
        with mock.patch.object(ao.webbrowser, "open") as opened:
            reply = ao.open_agency_workspace({"agent": "zzqx flurble"})
        opened.assert_not_called()
        self.assertIn("Could not resolve", reply)
        self.assertIn("Hub workspace", reply)


class MusicPayloadTest(unittest.TestCase):
    def test_defaults(self):
        payload = ms._build_payload({"prompt": "calm piano"})
        self.assertEqual(payload["prompt"], "calm piano")
        self.assertEqual(payload["duration"], ms.DEFAULT_DURATION)
        self.assertNotIn("genre", payload)

    def test_duration_clamped(self):
        self.assertEqual(
            ms._build_payload({"prompt": "x y", "duration": 5})["duration"],
            ms.MIN_DURATION,
        )
        self.assertEqual(
            ms._build_payload({"prompt": "x y", "duration": 9999})["duration"],
            ms.MAX_DURATION,
        )

    def test_genre_and_mood_passthrough(self):
        payload = ms._build_payload(
            {"prompt": "epic drums", "genre": "Cinematic", "mood": "Epic"}
        )
        self.assertEqual(payload["genre"], "Cinematic")
        self.assertEqual(payload["mood"], "Epic")

    def test_prompt_required(self):
        with self.assertRaises(ValueError):
            ms._build_payload({"prompt": ""})
        with self.assertRaises(ValueError):
            ms._build_payload({})

    def test_tool_contract(self):
        self.assertEqual(ms.TOOL["name"], "music_studio")
        self.assertEqual(ms.TOOL["parameters"]["required"], ["prompt"])
        self.assertIs(ms.TOOL["handler"], ms.generate_music)


if __name__ == "__main__":
    unittest.main()
