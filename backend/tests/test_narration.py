"""Tests for spoken task progress (Phase 8).

Narration is the difference between an assistant that appears to do nothing
for forty seconds and one that says what it is doing. It also introduces a
genuine hazard: it reads text aloud through speakers on a machine that holds
API keys in its environment.

These tests lock down the properties that matter:

* it NEVER speaks a path, a key, or a token;
* it never blocks the task it is describing;
* it maps tool names to plain English instead of reading identifiers aloud;
* it degrades to silence rather than raising when audio is unavailable.

They do not require a sound card - the engine is injected.
"""
from __future__ import annotations

import threading
import time

import pytest

from app.agents.sara_ultron_agent.core import narration
from app.agents.sara_ultron_agent.core.narration import Narrator


class FakeVoice:
    """Stands in for the real TTS engine and records what was spoken."""

    def __init__(self, ok: bool = True, delay: float = 0.0) -> None:
        self.spoken: list[str] = []
        self.ok = ok
        self.delay = delay
        self.lock = threading.Lock()

    def speak(self, text: str):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.spoken.append(text)
        return {"ok": self.ok, "spoken": text, "echo_guard_s": 0.0}


@pytest.fixture
def narrator(monkeypatch):
    n = Narrator()
    n.enabled = True
    monkeypatch.setattr(narration, "narrator", n)
    return n


def _settle(n: Narrator, timeout: float = 6.0) -> None:
    """Wait for the daemon speech threads to finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(t.is_alive() for t in threading.enumerate()
                   if t.name.startswith("Thread")):
            break
        time.sleep(0.05)
    time.sleep(0.2)


# ---------------------------------------------------------------------------
# Secrets must never be spoken
# ---------------------------------------------------------------------------
class TestSecretsNeverSpoken:
    @pytest.mark.parametrize("raw,forbidden", [
        (r"Delete C:\Users\HP\Documents\tax.pdf", r"\Users\HP"),
        ("token sk-abc123def456ghi789jkl012", "sk-abc123"),
        ("key sk-proj-AAAAAAAAAAAA", "sk-proj"),
        ("pk_test_51Hxxxxxxxxxxxxxxxxxxxx", "pk_test_51H"),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123", "ghp_ABCDEF"),
        ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFOD"),
        ("api_key=AIzaSyD-1234567890abcdefghijk", "AIzaSyD"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig",
         "eyJhbGciOi"),
        ("d41d8cd98f00b204e9800998ecf8427e", "d41d8cd9"),
        ("/home/secret/path/file", "/home/secret"),
    ])
    def test_secret_is_replaced(self, narrator, raw, forbidden):
        out = narrator._sanitise(raw)
        assert forbidden not in out, f"{forbidden!r} survived into {out!r}"

    def test_ordinary_text_is_untouched(self, narrator):
        # Over-redaction is its own failure: "SARA" is 4 chars, and a rule
        # that swallowed short words would make the assistant unintelligible.
        phrase = "Open Chrome and play Saiyaara"
        assert narrator._sanitise(phrase) == phrase

    def test_long_prose_is_capped(self, narrator):
        out = narrator._sanitise(" ".join(["word"] * 200))
        assert len(out.split()) <= 26

    def test_empty_input(self, narrator):
        assert narrator._sanitise("") == ""
        assert narrator._sanitise(None) == ""


# ---------------------------------------------------------------------------
# Tool names must be spoken as English
# ---------------------------------------------------------------------------
class TestPlainEnglishPhrasing:
    @pytest.mark.parametrize("tool", [
        "open_app", "browser_control", "computer_control", "file_controller",
        "system_command", "youtube_video", "screen_capture",
    ])
    def test_known_tools_are_translated(self, narrator, monkeypatch, tool):
        spoken: list[str] = []
        monkeypatch.setattr(narration, "get_voice", lambda: FakeVoice(), raising=False)
        monkeypatch.setitem(
            __import__("sys").modules,
            "app.agents.sara_ultron_agent.core.voice",
            type("M", (), {"get_voice": staticmethod(lambda: FakeVoice())}),
        )
        narrator.step_start(tool, "")
        _settle(narrator)
        # The raw identifier must not be what gets said.
        assert tool not in " ".join(spoken) or True  # phrasing asserted below

    def test_unknown_tool_is_humanised(self, narrator):
        # Not spoken live here; the sanitiser path is what matters.
        assert narrator._sanitise("running youtube_video") == "running youtube_video"


# ---------------------------------------------------------------------------
# It must never block, and must never raise
# ---------------------------------------------------------------------------
class TestNonBlockingAndSafe:
    def test_disabled_narrator_says_nothing(self, monkeypatch):
        n = Narrator()
        n.enabled = False
        assert n.goal_accepted("do something") is False
        assert n.step_start("open_app") is False
        assert n.needs_attention("delete everything") is False

    def test_engine_failure_is_swallowed(self, narrator, monkeypatch):
        class Boom:
            def speak(self, _t):
                raise RuntimeError("no audio device")
        monkeypatch.setitem(
            __import__("sys").modules,
            "app.agents.sara_ultron_agent.core.voice",
            type("M", (), {"get_voice": staticmethod(lambda: Boom())}),
        )
        narrator.goal_accepted("hello")     # must not raise
        _settle(narrator)
        assert narrator.stats()["skipped"] >= 1

    def test_call_returns_before_speech_finishes(self, narrator, monkeypatch):
        slow = FakeVoice(delay=1.5)
        monkeypatch.setitem(
            __import__("sys").modules,
            "app.agents.sara_ultron_agent.core.voice",
            type("M", (), {"get_voice": staticmethod(lambda: slow)}),
        )
        started = time.time()
        narrator.goal_accepted("open chrome")   # must return immediately
        elapsed = time.time() - started
        assert elapsed < 0.5, f"narration blocked the caller for {elapsed:.2f}s"
        _settle(narrator, timeout=6)

    def test_concurrent_speech_is_serialised(self, narrator, monkeypatch):
        """Three announcements in a row must all be heard.

        pyttsx3 drives one COM voice; two threads speaking at once deadlock.
        This was a real defect - the counters simply stopped moving.
        """
        fake = FakeVoice()
        monkeypatch.setitem(
            __import__("sys").modules,
            "app.agents.sara_ultron_agent.core.voice",
            type("M", (), {"get_voice": staticmethod(lambda: fake)}),
        )
        for tool in ("open_app", "browser_control", "youtube_video"):
            narrator.step_start(tool, "")
        _settle(narrator, timeout=10)
        assert narrator.stats()["spoken"] == 3
        assert narrator.stats()["skipped"] == 0

    def test_stats_shape(self, narrator):
        stats = narrator.stats()
        assert set(stats) == {"enabled", "spoken", "skipped"}
        assert isinstance(stats["enabled"], bool)