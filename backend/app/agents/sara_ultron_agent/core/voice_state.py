"""Ultron voice state - the six-state machine the hologram is bound to.

The master prompt names the states:

    IDLE -> LISTENING -> THINKING -> EXECUTING -> SPEAKING -> ERROR

The temptation with a state machine is to *set* the state when something is
supposed to be happening. This module never does that. Every state below is
*derived* from two real inputs on every read:

  1. the live voice stack - is the recogniser actually inside a microphone
     read, is the synthesiser actually playing (``core/voice.py``), is there an
     input device at all;
  2. the live run sessions - which lifecycle phases are actually in progress
     (``core/session.py``).

So ``THINKING`` means at least one run is genuinely in ``understanding`` or
``planning`` right now. It cannot be reached by a timer, it cannot be reached by
a speech synthesiser finishing, and it cannot be reached when no run exists.
Precedence is fixed and documented below so the UI never has to guess.

Three states exist beyond the six named in the prompt because the prompt's own
requirements demand them:

  ``PERMISSION_REQUIRED``
      the browser or the OS denied microphone access. Rendering this as
      ``IDLE`` would be a lie that looks like a working feature.
  ``VERIFYING``
      a run is assembling its report. Folding this into ``THINKING`` would hide
      the one phase where Ultron is deciding whether it actually succeeded.
  ``UNAVAILABLE``
      no speech stack at all. The UI disables the mic and says why.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

#: The states, in the order the prompt names them plus the three the prompt's
#: own requirements force. The UI renders this tuple as the voice rail.
VOICE_STATES = (
    "IDLE",
    "LISTENING",
    "THINKING",
    "EXECUTING",
    "VERIFYING",
    "SPEAKING",
    "ERROR",
    "PERMISSION_REQUIRED",
    "UNAVAILABLE",
)

#: A voice failure keeps the core in ERROR this long, so the user sees why
#: before the rail quietly returns to IDLE.
ERROR_LINGER_S = 8.0

#: Phases that mean "Ultron is deciding".
_THINKING_PHASES = frozenset({"understanding", "planning", "verifying"})
_EXECUTING_PHASES = frozenset({"executing"})


def _busiest(phases: List[str]) -> Optional[str]:
    """Pick the phase that should win the display.

    Executing outranks thinking: it is the phase where real actions are
    happening on the user's machine, and a stale ``understanding`` from another
    concurrent run must not mask it.
    """
    for phase in ("executing", "verifying", "planning", "understanding"):
        if phase in phases:
            return phase
    return None


def derive_voice_state(
    voice_status: Optional[Dict[str, Any]],
    sessions: Optional[List[Dict[str, Any]]] = None,
    now: Optional[float] = None,
    last_error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the voice state from real inputs. Pure and side-effect free.

    ``voice_status`` is the dict from ``UltronVoice.status().as_dict()``;
    ``sessions`` is the list from ``SessionStore.snapshots()``. Either may be
    ``None``, which is exactly what happens when the backend is mid-restart -
    the state then reports ``UNAVAILABLE`` instead of guessing ``IDLE``.
    """
    now = now if now is not None else time.time()
    sessions = sessions or []

    active = [
        s for s in sessions if s.get("status") in ("queued", "running")
    ]
    phases = sorted({str(s.get("phase")) for s in active if s.get("phase")})
    dominant = _busiest(phases)

    # A fresh error outranks everything, so a failed run cannot hide behind an
    # idle-looking rail.
    error_active = False
    if last_error:
        age = now - float(last_error.get("t") or 0)
        error_active = 0 <= age <= ERROR_LINGER_S

    listening = bool(voice_status and voice_status.get("listening"))
    speaking = bool(voice_status and voice_status.get("speaking"))
    has_mic = bool(voice_status and voice_status.get("microphone"))
    stack_present = voice_status is not None

    reason: str
    if not stack_present:
        state = "UNAVAILABLE"
        reason = "The voice stack did not answer its capability probe."
    elif error_active:
        state = "ERROR"
        reason = str(last_error.get("message") or "The last voice operation failed.")
    elif not has_mic:
        # "No input device" and "the user denied permission" are the same
        # failure from the operator's side: the mic is not usable right now.
        state = "PERMISSION_REQUIRED"
        reason = str(
            (voice_status or {}).get("microphone_detail") or "No usable microphone."
        )
    elif speaking:
        state = "SPEAKING"
        reason = "Ultron is speaking through the local synthesiser."
    elif listening:
        state = "LISTENING"
        reason = "The recogniser is inside a live microphone read."
    elif dominant == "executing":
        state = "EXECUTING"
        reason = f"{len(active)} run(s) executing real tools."
    elif dominant == "verifying":
        state = "VERIFYING"
        reason = f"{len(active)} run(s) assembling the result report."
    elif dominant in ("understanding", "planning"):
        state = "THINKING"
        reason = f"{len(active)} run(s) reasoning or planning."
    else:
        state = "IDLE"
        reason = "No run in progress and the microphone is free."

    # Echo guard is a real, observable fact about the mic: it is why a listen
    # request is refused right after a reply. The UI shows it so "press the mic
    # and nothing happens" is explainable.
    echo_guard_s = float((voice_status or {}).get("echo_guard_s") or 0.0)

    return {
        "state": state,
        "reason": reason,
        "active_runs": len(active),
        "phases": phases,
        "dominant_phase": dominant,
        "listening": listening,
        "speaking": speaking,
        "microphone": has_mic,
        "transcriber": bool((voice_status or {}).get("transcriber")),
        "speech_synth": bool((voice_status or {}).get("speech_synth")),
        "model": (voice_status or {}).get("model"),
        "echo_guard_s": round(echo_guard_s, 2),
        "problems": list((voice_status or {}).get("problems") or []),
        # Continuous (wake-word) capture is NOT implemented. Reported here so
        # the rail can show it, rather than implying hands-free works.
        "continuous": False,
        "continuous_detail": (
            "Single-utterance capture only. The wake-word / barge-in loop is "
            "not implemented in this build - see the deployment notes."
        ),
        "checked_at": now,
    }


class VoiceStateTracker:
    """Holds the last voice failure so :func:`derive_voice_state` can see it."""

    def __init__(self) -> None:
        self._last_error: Optional[Dict[str, Any]] = None

    def note_error(self, message: str) -> None:
        self._last_error = {"t": time.time(), "message": str(message)[:200]}

    def note_success(self) -> None:
        self._last_error = None

    @property
    def last_error(self) -> Optional[Dict[str, Any]]:
        return self._last_error

    def snapshot(self, voice_status: Optional[Dict[str, Any]],
                 sessions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return derive_voice_state(voice_status, sessions, last_error=self._last_error)


#: Process-wide tracker. One failure at a time is enough; the UI only needs to
#: explain the most recent one.
tracker = VoiceStateTracker()
