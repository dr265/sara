"""Tests for core.notify_phone — honest per-channel completion delivery.

The rule under test: a channel is claimed delivered ONLY when its real
transport confirmed it. Zero phones connected means "queued", never
"delivered"; Telegram without full configuration means "not configured".
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DESKTOP = PROJECT_ROOT / "sara_desktop"
for _p in (str(DESKTOP), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.notify_phone import PhoneNotifier  # noqa: E402

TELEGRAM_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                 "SARA_TELEGRAM_CHAT_ID")


def _record(**extra):
    rec = {"task_id": "abcd1234-0000", "status": "completed",
           "request": "research widget prices", "result": "Found 3 offers.",
           "error": "", "artifacts": [{"kind": "document",
                                       "title": "report.pdf",
                                       "path": "C:\\x\\report.pdf"}]}
    rec.update(extra)
    return rec


class NotifyTests(unittest.TestCase):
    def setUp(self):
        # the host may carry Telegram credentials — force the unconfigured
        # path so the tests assert the "not configured" contract instead
        patcher = mock.patch.dict(os.environ, {k: "" for k in TELEGRAM_KEYS})
        patcher.start()
        self.addCleanup(patcher.stop)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.uploads = Path(tmp.name)
        self.sent = []
        self.clients = 1

        def push(msg):
            self.sent.append(msg)

        self.notifier = PhoneNotifier(
            push=push,
            client_count=lambda: self.clients,
            uploads_dir=str(self.uploads),
            backend_root=tmp.name)   # no backend/.env → unconfigured

    def test_delivered_with_connected_phone(self):
        shot = self.uploads / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        channels = self.notifier.send_report(_record(), str(shot))
        self.assertTrue(channels["phone"]["ok"])
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["type"], "report")
        self.assertTrue(msg["image"].startswith("/uploads/"))
        copied = self.uploads / msg["image"].split("/")[-1]
        self.assertTrue(copied.exists())
        self.assertFalse(channels["telegram"]["ok"])
        self.assertIn("not configured", channels["telegram"]["detail"])

    def test_queued_when_no_phone(self):
        self.clients = 0
        channels = self.notifier.send_report(_record())
        self.assertFalse(channels["phone"]["ok"])
        self.assertTrue(channels["phone"].get("queued"))
        self.assertEqual(self.sent, [])
        self.assertTrue(self.notifier.has_pending())
        # a phone appears → flush delivers the queued report
        self.clients = 1
        self.assertEqual(self.notifier.flush_pending(), 1)
        self.assertFalse(self.notifier.has_pending())
        self.assertEqual(self.sent[0]["type"], "report")

    def test_report_text_bounded_and_honest(self):
        text = PhoneNotifier.build_report_text(_record())
        self.assertIn("COMPLETED", text)
        self.assertIn("research widget prices", text)
        self.assertIn("artifact", text)
        self.assertLessEqual(len(text), 900)

    def test_unknown_client_count_claims_broadcast_only(self):
        notifier = PhoneNotifier(push=self.sent.append, client_count=None,
                                 uploads_dir=str(self.uploads),
                                 backend_root=str(self.uploads))
        channels = notifier.send_report(_record())
        self.assertTrue(channels["phone"]["ok"])
        self.assertIn("unknown", channels["phone"]["detail"])

    def test_no_push_channel_is_reported_honestly(self):
        notifier = PhoneNotifier(push=None, uploads_dir=str(self.uploads),
                                 backend_root=str(self.uploads))
        channels = notifier.send_report(_record())
        self.assertFalse(channels["phone"]["ok"])
        self.assertIn("unavailable", channels["phone"]["detail"])


if __name__ == "__main__":
    unittest.main()
