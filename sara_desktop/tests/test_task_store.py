"""Tests for core.task_store — the durable task history (offline).

The Result Viewer, the "show me the result" voice tool and the completion
report pipeline all answer from this store, so its upsert/dedupe/search
behaviour is tested hard: a record must never lose fields on re-record,
never duplicate artifacts, and survive a real JSON round-trip.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.task_store import TaskStore  # noqa: E402


def _task(tid, request, status="completed", **extra):
    task = {"task_id": tid, "request": request, "status": status,
            "agents": ["sara-ultron"], "steps": [], "result": "",
            "error": "", "created_at": 1000.0, "finished_at": 1001.0,
            "artifacts": []}
    task.update(extra)
    return task


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "history.json"
        self.store = TaskStore(path=self.path)

    def test_record_and_get(self):
        self.store.record(_task("t1", "make me a song"), mode="agentic")
        rec = self.store.get("t1")
        self.assertEqual(rec["request"], "make me a song")
        self.assertEqual(rec["mode"], "agentic")

    def test_latest_order_and_status_filter(self):
        self.store.record(_task("t1", "first"))
        self.store.record(_task("t2", "second"))
        self.store.record(_task("t3", "third", status="failed"))
        self.assertEqual([r["task_id"] for r in self.store.latest(3)],
                         ["t3", "t2", "t1"])
        self.assertEqual(
            [r["task_id"] for r in self.store.latest(3,
                                                     statuses=("completed",))],
            ["t2", "t1"])

    def test_record_upsert_keeps_screenshot_and_result(self):
        self.store.record(_task("t1", "research quantum computing"))
        self.store.set_screenshot("t1", r"C:\shots\t1.png")
        self.store.record(_task("t1", "research quantum computing",
                                result="done"))
        rec = self.store.get("t1")
        self.assertEqual(rec["screenshot"], r"C:\shots\t1.png")
        self.assertEqual(rec["result"], "done")

    def test_artifact_dedupe_and_cap(self):
        self.store.record(_task("t1", "song"))
        art = {"kind": "audio", "title": "A", "url": "http://x/a.mp3"}
        self.store.attach_artifact("t1", art)
        self.store.attach_artifact("t1", dict(art))
        self.assertEqual(len(self.store.get("t1")["artifacts"]), 1)
        for i in range(30):
            self.store.attach_artifact(
                "t1", {"kind": "file", "title": f"f{i}",
                       "path": f"C:\\x\\f{i}.txt"})
        self.assertLessEqual(len(self.store.get("t1")["artifacts"]), 20)

    def test_record_carries_task_artifacts(self):
        task = _task("t1", "generate a track",
                     artifacts=[{"kind": "audio", "title": "demo",
                                 "url": "http://x/demo.mp3"}])
        self.store.record(task)
        rec = self.store.get("t1")
        self.assertEqual(rec["artifacts"][0]["url"], "http://x/demo.mp3")

    def test_find_keyword(self):
        self.store.record(_task("t1", "generate a lo-fi song about rain"))
        self.store.record(_task("t2", "check disk usage report"))
        found = self.store.find("song rain")
        self.assertTrue(found)
        self.assertEqual(found[0]["task_id"], "t1")

    def test_persistence_roundtrip(self):
        self.store.record(_task("t1", "persist me"), mode="adaptive")
        self.store.mark_notify("t1", "phone", True, "delivered")
        again = TaskStore(path=self.path)
        rec = again.get("t1")
        self.assertEqual(rec["mode"], "adaptive")
        self.assertEqual(rec["notify"][0]["channel"], "phone")
        self.assertTrue(rec["notify"][0]["ok"])

    def test_missing_file_is_empty(self):
        store = TaskStore(path=self.path.parent / "nope.json")
        self.assertEqual(store.latest(5), [])

    def test_corrupt_file_is_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = TaskStore(path=self.path)
        self.assertEqual(store.latest(5), [])


if __name__ == "__main__":
    unittest.main()
