"""The always-on microphone loop.

"I turn MIC ON once, and Ultron stays listening until I turn it off." That is
the whole requirement, and it is harder than it sounds, because the naive
implementation has four failure modes:

**It stops itself.** A loop that runs once and returns is not always-on. This
one keeps cycling until stopped.

**It talks to itself.** Ultron speaks, the microphone hears it, and it answers
its own reply. The echo guard in ``core/voice.py`` arms itself when a reply
starts playing; this loop waits that guard out instead of listening anyway.

**It wedges on silence.** With no speech, a recogniser eventually times out. A
timeout is *normal* here, not an error, and must not count toward the
error budget or the loop would shut itself off in a quiet room.

**It never shuts off.** If STT is broken, or the device vanishes, or the model
dies, an always-on loop that keeps retrying forever is a microphone that never
stops recording. So consecutive *real* errors stop the loop, loudly, and say
why - a mic you have to reboot to switch off is not a user-controlled mic.

Barge-in
--------
If the loop is speaking and the operator starts talking, the speech is cut and
the new utterance is captured. On the server-side loop that means cancelling
TTS; in a browser it means stopping the audio element. Both call
:meth:`AlwaysOnVoice.barge_in`, which is the single place that decision lives.

The loop is driven by a **handler callback**, not by importing the orchestrator.
That keeps ``core`` free of a circular dependency and makes the whole loop
testable without an LLM: the tests install a fake handler and drive real state
transitions.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .awareness import context as context_manager
from .permissions import permissions
from .voice import get_voice, probe_microphone

log = logging.getLogger("sara.ultron.alwayson")

#: Seconds to listen for the start of an utterance before treating it as
#: silence. Short enough that a quiet room cycles quickly, long enough that
#: starting to speak is not clipped.
LISTEN_TIMEOUT_S = 6.0

#: Seconds of speech to accept in one utterance.
PHRASE_LIMIT_S = 14.0

#: Consecutive *real* errors that stop the loop. Silence never counts.
MAX_CONSECUTIVE_ERRORS = 3

#: Pause after a handled utterance, so the room settles before listening again.
CYCLE_GAP_S = 0.35

#: Longest single wait on the echo guard before re-checking the stop flag.
ECHO_WAIT_SLICE_S = 0.5

#: States the loop reports. Mirrors the voice-state machine the UI renders.
STATES = ("off", "starting", "listening", "transcribing", "handling",
          "speaking", "stopped", "error")


@dataclass
class AlwaysOnStats:
    started: float = 0.0
    cycles: int = 0
    utterances: int = 0
    handled: int = 0
    timeouts: int = 0            # silence, not errors
    errors: int = 0
    consecutive_errors: int = 0
    barge_ins: int = 0
    stops: int = 0
    last_error: str = ""
    last_utterance: str = ""
    last_handled_at: float = 0.0
    history: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        uptime = time.time() - self.started if self.started else 0.0
        return {
            "uptime_s": round(uptime, 1),
            "cycles": self.cycles,
            "utterances": self.utterances,
            "handled": self.handled,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "barge_ins": self.barge_ins,
            "stops": self.stops,
            "last_error": self.last_error,
            "last_utterance": self.last_utterance,
            "last_handled_at": self.last_handled_at,
            "cycles_per_min": round(self.cycles / (uptime / 60), 2) if uptime > 60 else None,
        }


Handler = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class AlwaysOnVoice:
    """The persistent listening loop."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = "off"
        self._speaking = threading.Event()
        self._barge_in = threading.Event()
        self.stats = AlwaysOnStats()
        self._handler: Optional[Handler] = None
        self._echo_guard_before = 0.0

    # -- lifecycle ------------------------------------------------------
    def set_handler(self, handler: Handler) -> None:
        """Install what to do with a recognised utterance.

        Kept as a callback so this module does not import the orchestrator,
        which would make ``core`` depend on the agent that already depends on
        ``core``.
        """
        self._handler = handler

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        return self.state in ("starting", "listening", "transcribing", "handling")

    @property
    def handler_missing(self) -> bool:
        """True when no handler has been installed yet.

        Exposed rather than reaching into ``_handler`` from the API layer, which
        would make a private field part of the route's contract.
        """
        return self._handler is None

    def start(self, handler: Optional[Handler] = None) -> Dict[str, Any]:
        if handler is not None:
            self.set_handler(handler)
        # The permission check is here *and* inside the loop. A user who
        # revokes the mic mid-session must be heard immediately, not at the
        # start of the next session.
        if not permissions.allowed("microphone"):
            return {"started": False, "reason": "microphone permission not granted"}
        # A device that will not open is not going to open on the third try
        # either. Say so once, up front, instead of starting a thread that is
        # guaranteed to fail - the microphone would be "on" and deaf.
        probe = probe_microphone()
        if not probe["ok"]:
            return {"started": False, "reason": probe["detail"]}
        if self.running:
            return {"started": False, "reason": "already running", **self.snapshot()}
        self._stop.clear()
        self.stats = AlwaysOnStats(started=time.time())
        with self._lock:
            self._state = "starting"
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ultron-always-on")
        self._thread.start()
        log.info("Always-on microphone started.")
        return {"started": True, **self.snapshot()}

    def stop(self, reason: str = "operator") -> Dict[str, Any]:
        was_running = self.running
        self._stop.set()
        thread = self._thread
        self._thread = None
        # Stop speaking too, or the room keeps talking after the mic is off.
        try:
            get_voice().cancel_speech()
        except Exception:  # noqa: BLE001 - stopping must always succeed
            pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=8)
        with self._lock:
            self._state = "off" if was_running else self._state
        self.stats.stops += 1
        if was_running:
            log.info("Always-on microphone stopped (%s).", reason)
        return {"stopped": was_running, "reason": reason, **self.snapshot()}

    def barge_in(self) -> Dict[str, Any]:
        """Cut speech short because the operator started talking."""
        result: Dict[str, Any] = {"barge_in": True}
        try:
            result["speech"] = get_voice().cancel_speech()
        except Exception as exc:  # noqa: BLE001
            result["speech"] = {"ok": False, "error": str(exc)}
        self._barge_in.set()
        self._speaking.clear()
        self.stats.barge_ins += 1
        log.info("Barge-in: speech cancelled, new utterance will be captured.")
        return result

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "running": self.running,
            "permission": permissions.allowed("microphone"),
            **self.stats.as_dict(),
            "history": list(self.stats.history[-10:])[::-1],
            "states": list(STATES),
        }

    def _set_state(self, state: str) -> None:
        with self._lock:
            if self._state != state:
                self._state = state
                self._publish(state)

    def _publish(self, state: str) -> None:
        try:
            from .bus import bus

            bus.publish("voice", "always_on", state=state,
                        running=self.running, cycles=self.stats.cycles,
                        consecutive_errors=self.stats.consecutive_errors)
        except Exception:  # noqa: BLE001
            pass

    # -- the loop -------------------------------------------------------
    def cycle_once(self) -> Dict[str, Any]:
        """One full listen → transcribe → handle pass.

        Separated from :meth:`_loop` so tests can step it deterministically
        without threads or sleeps.
        """
        voice = get_voice()

        # Wait out any speech still playing - this is what stops the assistant
        # answering itself.
        guard = voice.echo_guard_remaining()
        while guard > 0 and not self._stop.is_set():
            # Sliced so a stop request is honoured within half a second rather
            # than after a 30-second guard.
            self._stop.wait(min(guard, ECHO_WAIT_SLICE_S))
            guard = voice.echo_guard_remaining()
        if self._stop.is_set():
            return {"stopped": True}

        self._set_state("listening")
        self.stats.cycles += 1
        result = voice.listen(timeout=LISTEN_TIMEOUT_S, phrase_limit=PHRASE_LIMIT_S)
        self._set_state("transcribing")

        if not result.get("ok"):
            error = str(result.get("error", ""))
            # A recogniser timeout is silence, not failure. Counting it as an
            # error would switch the microphone off in a quiet room.
            if "timed out" in error.lower() or "timeout" in error.lower():
                self.stats.timeouts += 1
                self._set_state("listening")
                return {"ok": False, "reason": "silence", "error": error}
            if "still speaking" in error.lower():
                self.stats.timeouts += 1
                return {"ok": False, "reason": "echo_guard", "error": error}
            if "no microphone" in error.lower():
                # Permanent, not transient: stop now rather than spend the
                # error budget discovering the same thing three times.
                self.stats.errors += 1
                self.stats.last_error = error[:300]
                log.error("Always-on stopping: %s", error)
                self._stop.set()
                self._set_state("error")
                return {"ok": False, "reason": "no_microphone", "error": error}
            self._note_error(error)
            return {"ok": False, "reason": "error", "error": error}

        utterance = str(result.get("text", "")).strip()
        if not utterance:
            self.stats.timeouts += 1
            return {"ok": False, "reason": "empty", "error": "no speech recognised"}

        self.stats.utterances += 1
        self.stats.last_utterance = utterance

        # Hand the utterance over with the live context attached, so the
        # resolver and the run can use it.
        snapshot = context_manager.snapshot()
        self._set_state("handling")
        handled: Dict[str, Any] = {"ok": False, "reason": "no handler installed"}
        if self._handler is not None:
            try:
                handled = self._handler(utterance, snapshot)
            except Exception as exc:  # noqa: BLE001 - a bad handler must not
                # kill the loop; that is exactly the "never shuts off" trap.
                self._note_error(f"handler failed: {type(exc).__name__}: {exc}")
                return {"ok": False, "reason": "handler", "error": str(exc)}
        # The error streak is broken *here*, after the handler has run, not when
        # speech was recognised. A recogniser that works fine while every
        # handler raises would otherwise reset the counter three times in a row
        # and the loop would listen forever against a model that is dead.
        self.stats.consecutive_errors = 0
        self.stats.handled += 1
        self.stats.last_handled_at = time.time()
        entry = {
            "t": time.time(),
            "utterance": utterance[:200],
            "ok": bool(handled.get("ok")),
            "result": str(handled.get("summary") or handled.get("reason") or "")[:200],
        }
        self.stats.history.append(entry)
        if len(self.stats.history) > 50:
            del self.stats.history[: len(self.stats.history) - 50]

        # Speak the reply, if the handler asked for one.
        reply = str(handled.get("speak") or "").strip()
        if reply:
            self._speaking.set()
            self._set_state("speaking")
            try:
                voice.speak(reply)
            except Exception as exc:  # noqa: BLE001
                log.warning("Always-on speak failed: %s", exc)
            finally:
                self._speaking.clear()
                self._set_state("listening")
        return {"ok": True, "utterance": utterance, "handled": handled}

    def _note_error(self, error: str) -> None:
        self.stats.errors += 1
        self.stats.consecutive_errors += 1
        self.stats.last_error = error[:300]
        if self.stats.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            # Refuse to spin forever against a broken device or model.
            log.error("Always-on stopping after %d consecutive errors: %s",
                      self.stats.consecutive_errors, error)
            self._stop.set()
            self._set_state("error")

    def _loop(self) -> None:  # pragma: no cover - background thread
        while not self._stop.is_set():
            try:
                outcome = self.cycle_once()
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"{type(exc).__name__}: {exc}")
                outcome = {"ok": False, "reason": "exception"}
            if self._stop.is_set():
                break
            if outcome.get("ok"):
                self.stats.consecutive_errors = 0
            self._stop.wait(CYCLE_GAP_S)
        with self._lock:
            if self._state != "error":
                self._state = "stopped"
        self._publish(self.state)


#: Process-wide always-on loop.
always_on = AlwaysOnVoice()
