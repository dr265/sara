"""Tests for the router's artifact scan and the ADAPTIVE fast path.

Artifacts: only references that really exist in the payload become
artifacts — bounded depth, deduped, typed. Fast path: only unambiguous,
non-consequential, self-parsing local actions may auto-run; everything
else must fall through to the planner.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent_registry import AgentInfo  # noqa: E402
from core.agent_router import TaskRouter, _collect_artifacts  # noqa: E402


class FakeRegistry:
    backend_url = "http://127.0.0.1:8000"

    def __init__(self, agents=()):
        self._agents = list(agents)

    def get_all_agents(self):
        return list(self._agents)


def _agent(action, name, description, capabilities, accepts=None):
    return AgentInfo(id=f"desktop_action:{action}", name=name,
                     category="Desktop Automation", description=description,
                     capabilities=list(capabilities), source="desktop_action",
                     accepts=dict(accepts or {}))


class CollectArtifactsTests(unittest.TestCase):
    def test_music_payload(self):
        payload = {"status": "ok", "track": {
            "prompt": "lo-fi rain", "url": "/api/music/audio/abc.mp3"}}
        arts = _collect_artifacts(payload, "http://host")
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["kind"], "audio")
        self.assertEqual(arts[0]["url"], "http://host/api/music/audio/abc.mp3")
        self.assertEqual(arts[0]["title"], "lo-fi rain")

    def test_image_and_document_urls(self):
        payload = {"data": {"image_url": "https://x/y.png",
                            "report": {"file_path": r"C:\out\report.pdf"}}}
        arts = _collect_artifacts(payload, "http://host")
        kinds = {a["kind"] for a in arts}
        self.assertIn("image", kinds)
        self.assertIn("document", kinds)
        doc = [a for a in arts if a["kind"] == "document"][0]
        self.assertEqual(doc["path"], r"C:\out\report.pdf")

    def test_ignores_noise(self):
        arts = _collect_artifacts({"status": "done", "note": "ok",
                                   "count": 3}, "http://host")
        self.assertEqual(arts, [])

    def test_depth_bounded(self):
        payload = {"a": {"b": {"c": {"d": {"url": "http://x/deep.mp3"}}}}}
        self.assertEqual(_collect_artifacts(payload, "http://host"), [])


class FastPathTests(unittest.TestCase):
    def setUp(self):
        self.agents = [
            _agent("add_reminder", "Add Reminder",
                   "Create a reminder for something the user must do.",
                   ("remind", "reminder", "note", "remember"),
                   accepts={"text": {"type": "string"}}),
            _agent("open_app", "Open App",
                   "Open an application or program on this computer.",
                   ("open", "app", "application", "launch"),
                   accepts={"app_name": {"type": "string"}}),
            _agent("weather_report", "Weather Report",
                   "Fetches live weather for a city.",
                   ("weather", "forecast"),
                   accepts={"city": {"type": "string"}}),
        ]
        self.router = TaskRouter(FakeRegistry(self.agents))

    def test_clear_selfparsing_match(self):
        plan = self.router.local_fast_plan("remind me to call mom tomorrow")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["agents"], ["desktop_action:add_reminder"])
        self.assertTrue(plan.get("fast_path"))

    def test_structured_action_defers_to_planner(self):
        # "open_app" scores high but needs app_name filled in by the LLM —
        # the fast path must refuse rather than run it with no parameters.
        plan = self.router.local_fast_plan("open the calculator application")
        self.assertIsNone(plan)

    def test_no_match_for_empty_or_fuzzy(self):
        self.assertIsNone(self.router.local_fast_plan("hi"))
        self.assertIsNone(self.router.local_fast_plan("do something maybe"))

    def test_note_artifacts_on_task(self):
        task = SimpleNamespace(artifacts=[])
        payload = {"track": {"prompt": "x", "url": "/api/music/audio/z.mp3"}}
        self.router._note_artifacts(payload, task)
        self.assertEqual(len(task.artifacts), 1)
        self.assertEqual(task.artifacts[0]["url"],
                         "http://127.0.0.1:8000/api/music/audio/z.mp3")
        # duplicate payload → no duplicate artifact
        self.router._note_artifacts(payload, task)
        self.assertEqual(len(task.artifacts), 1)

    def test_note_text_paths(self):
        task = SimpleNamespace(artifacts=[])
        self.router._note_text_artifacts(
            r"Saved to C:\Users\HP\Documents\song.mp3 successfully.", task)
        self.assertEqual(len(task.artifacts), 1)
        self.assertEqual(task.artifacts[0]["kind"], "audio")
        self.assertEqual(task.artifacts[0]["path"],
                         r"C:\Users\HP\Documents\song.mp3")


if __name__ == "__main__":
    unittest.main()
