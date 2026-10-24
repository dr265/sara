"""Spoken progress — SARA narrates what she is doing while she does it.

THE BEHAVIOUR THIS ADDS

Today a run is silent until it finishes, so from the room it looks like
nothing happened and then everything did. A JARVIS-style agent talks while it
works: it acknowledges the goal, names what it is doing, and says the outcome.
That is what this module does.

    "open chrome and play saiyaara"
      > Got it. Opening Chrome.
      > Searching for Saiyaara.
      > Playing it now.

DESIGN CONSTRAINTS, AND WHY

**Never blocks the task.** Speech runs on a daemon thread. A hung audio driver
must not become a hung assistant, so every call into the voice stack is
wrapped and every failure is swallowed after being logged.

**Never speaks secrets.** Only a short, sanitised phrase is spoken. File
contents, tokens and API keys do not go near a speaker.

**Never talks over itself.** The voice stack's echo guard is respected, and a
per-thread queue means two concurrent runs cannot interleave sentences.

**Respects an off switch.** ``SARA_SPOKEN_PROGRESS=0`` disables it entirely.
Talking unasked is obnoxious, so this is opt-out rather than opt-in only
because the user explicitly asked for it - but it stays configurable.

**Degrades to nothing.** With no TTS engine (a Linux container has none, and
correctly so) every method is a no-op and the task runs exactly as before.

WHAT IT IS NOT

It is not the continuous-listening loop. That is ``core/alwayson.py``, and it
needs a working microphone. This module only speaks OUT, which works today.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, Optional

log = logging.getLogger("sara.ultron.narration")

#: Set SARA_SPOKEN_PROGRESS=0 to silence all narration.
def _enabled_default() -> bool:
    return os.getenv("SARA_SPOKEN_PROGRESS", "1").strip().lower() not in (
        "0", "false", "no", "off")


class Narrator:
    """Speaks task progress without ever blocking the task itself."""

    def __init__(self) -> None:
        self.enabled = _enabled_default()
        self._lock = threading.Lock()
        #: Serialises playback. pyttsx3 drives a single COM SAPI voice, and
        #: two threads calling speak() at once deadlock - observed as narration
        #: threads that never returned and counters stuck at zero. Announcing
        #: three steps in quick succession is the NORMAL case, so this is not
        #: an edge case; it is the main path.
        self._speech_lock = threading.Lock()
        self._spoke = 0
        self._skipped = 0

    # -- primitives -----------------------------------------------------
    def _say(self, text: str) -> bool:
        """Speak one short phrase. Never raises, never blocks the caller."""
        if not self.enabled or not text:
            return False
        phrase = self._sanitise(text)
        if not phrase:
            return False
        thread = threading.Thread(target=self._speak, args=(phrase,), daemon=True)
        thread.start()
        return True

    def _speak(self, phrase: str) -> None:
        try:
            from .voice import get_voice

            voice = get_voice()
            # One utterance at a time. The lock is only ever held for the
            # duration of the speech itself, and it is a plain Lock rather than
            # an RLock because nothing inside re-enters.
            acquired = self._speech_lock.acquire(timeout=90)
            if not acquired:
                with self._lock:
                    self._skipped += 1
                return
            try:
                result = voice.speak(phrase)
            finally:
                self._speech_lock.release()
            with self._lock:
                if result.get("ok"):
                    self._spoke += 1
                else:
                    self._skipped += 1
        except Exception as exc:  # noqa: BLE001 - speech is never fatal
            with self._lock:
                self._skipped += 1
            log.debug("Narration skipped: %s: %s", type(exc).__name__, exc)

    @staticmethod
    def _sanitise(text: str) -> str:
        """Reduce a phrase to something safe and short to say out loud.

        Strips anything path- or token-shaped, so a file name or a key can
        never be read aloud, and caps the length so a long tool output does not
        become a paragraph of speech.
        """
        text = str(text or "").replace("\n", " ").strip()
        if not text:
            return ""
        # Paths become a neutral word rather than the real location.
        text = re.sub(r"[A-Za-z]:\\[^\s]*", "the requested file", text)
        text = re.sub(r"/[^\s]{4,}", "the requested location", text)
        # Long opaque runs are almost always keys or hashes. The threshold is
        # deliberately LOW: a real `sk-...` OpenAI-style key can be ~24 chars,
        # so a 32-char rule sails straight past it. Caught by the prefix rule
        # below as well as by this one, so the two overlap rather than relying
        # on either alone.
        text = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "a secret value", text)
        # Known credential shapes, regardless of length. These are the ones
        # that actually appear in this project's env files and tool output.
        text = re.sub(
            r"\b(?:sk|pk|rk|api|key|token|bearer)[-_][A-Za-z0-9_\-]{8,}\b",
            "a secret value", text, flags=re.IGNORECASE)
        text = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b", "a secret value", text)
        text = re.sub(r"\bAKIA[0-9A-Z]{12,}\b", "a secret value", text)
        text = re.sub(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
                      "a secret value", text)  # full JWT
        # JWT segments individually. The 24-char rule above redacts the long
        # middle segment but leaves the ~20-char `eyJ...` header, so the two
        # rules overlap and must both exist.
        text = re.sub(r"\beyJ[A-Za-z0-9_\-]{8,}", "a secret value", text)
        text = re.sub(r"\b[0-9a-f]{32,}\b", "a secret value", text)  # hex digest
        text = re.sub(r"\s+", " ", text).strip()
        words = text.split(" ")
        if len(words) > 25:
            text = " ".join(words[:25]) + "."
        return text

    # -- task lifecycle --------------------------------------------------
    def goal_accepted(self, goal: str) -> bool:
        """Acknowledge the request before any work starts."""
        return self._say(f"Got it. {goal}")

    def step_start(self, tool: str, action: str = "") -> bool:
        """Announce what is being done, in plain words.

        Tool names are developer vocabulary ("computer_control"). The user
        should hear "Opening an application", not an identifier.
        """
        phrasing = {
            "open_app": "Opening an application",
            "browser_control": "Working in the browser",
            "computer_control": "Controlling the mouse and keyboard",
            "computer_settings": "Adjusting a setting",
            "desktop_control": "Managing windows",
            "file_controller": "Handling files",
            "file_processor": "Handling files",
            "system_command": "Running a command",
            "web_search": "Searching the web",
            "youtube_video": "Opening YouTube",
            "weather_report": "Checking the weather",
            "screen_capture": "Looking at the screen",
        }
        said = phrasing.get(tool)
        if not said:
            said = f"Running {tool.replace('_', ' ')}" if tool else ""
        if action:
            said = f"{said}, {action}"
        return self._say(said)

    def step_done(self, tool: str, ok: bool = True) -> bool:
        """Confirm a step finished, or report that it did not."""
        if ok:
            return self._say("Done.")
        return self._say("That did not work. Trying something else.")

    def run_complete(self, goal: str, ok: bool) -> bool:
        """Close the loop out loud, the way a person would."""
        return self._say("All done." if ok else "I could not finish that.")

    def needs_attention(self, prompt: str) -> bool:
        """Read out a confirmation request.

        This is the one place narration MUST happen even if the user is not
        looking at the screen: a destructive action is waiting on them.
        """
        return self._say(f"I need your permission. {prompt}")

    # -- diagnostics -----------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"enabled": self.enabled, "spoken": self._spoke,
                    "skipped": self._skipped}


#: Process-wide narrator. One per backend, like every other singleton here.
narrator = Narrator()


def set_enabled(value: bool) -> bool:
    """Turn narration on or off at runtime. Returns the new state."""
    narrator.enabled = bool(value)
    log.info("Spoken progress %s", "enabled" if value else "disabled")
    return narrator.enabled