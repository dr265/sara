"""Live voice for Ultron: listen, understand, speak.

SARA's existing voice surface is Twilio telephony (``app/api/voice_agent.py``)
- a phone call, not a person at their desk - so there was nothing local to
reuse. The pieces needed are all installed and working: ``speech_recognition``
for capture, ``pyaudio`` for the device, ``whisper`` for offline
transcription and ``pyttsx3`` for SAPI speech on Windows.

Echo suppression
----------------
The failure this guards against is the assistant talking to itself. Writing
text to a sound device returns as soon as the buffer accepts the audio, not
when the room has finished playing it, so a listener started straight after a
reply hears its own last sentence. Brahma-Echo solves this in ``core/echo.py``
with spectral subtraction, tuned by hand per machine.

This takes the simpler route that needs no tuning: a reply records how long it
will take to *speak* (at a normal speaking rate, plus a margin) and the
listener refuses to start until that has elapsed. It cannot be fooled by
speaker volume or microphone placement, and it costs one timestamp.

Nothing here blocks at import time. ``whisper`` model loading takes seconds and
``pyttsx3.init()`` grabs the speech engine, so both are created on first use
and reported honestly by :meth:`UltronVoice.status` before anything is asked
of them.
"""
from __future__ import annotations

import importlib
import io
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("sara.ultron.voice")

#: Default Whisper size. "base" is ~140 MB and transcribes a short utterance in
#: a couple of seconds on CPU; "tiny" is faster and noticeably worse at names.
DEFAULT_MODEL = "base"

#: Words per minute at a normal speaking pace, used to estimate how long a
#: reply will still be playing.
WORDS_PER_MINUTE = 165.0

#: Extra time allowed for the audio tail and driver buffering.
ECHO_MARGIN_S = 1.2

#: Longest single utterance the listener will accept before cutting out.
MAX_UTTERANCE_S = 30.0

#: How long a microphone probe result is trusted before the device is checked
#: again. Long enough that a UI polling every second does not spawn a process
#: every second, short enough that plugging a microphone in is noticed.
MIC_PROBE_TTL_S = 60.0

#: Seconds allowed for the probe child, including interpreter start and the
#: ``speech_recognition`` import.
MIC_PROBE_TIMEOUT_S = 20.0

#: The probe child performs the *whole* capture setup the server would
#: otherwise do in process - same library, same DLL, same machine, so its
#: answer is the server's answer.
#:
#: It deliberately repeats ``Microphone.__enter__`` instead of using it.
#: ``__enter__`` opens the stream inside ``except Exception: audio.terminate()``,
#: so a device that refuses to open leaves ``stream = None`` and the user sees
#: ``AttributeError: 'NoneType' object has no attribute 'close'`` from
#: ``__exit__`` - which says nothing about the real problem. On this machine
#: that hid ``OSError: [Errno -9999] Unanticipated host error`` from PortAudio
#: while three working microphones were plugged in. Here the real exception is
#: allowed to escape and becomes the reported reason.
#: The probe tries the DEFAULT device first and then walks every other input
#: device, because on Windows the default capture endpoint is routinely stale
#: or exclusive-locked while other microphones work fine. Probing only the
#: default reports "no microphone" on a machine that plainly has three.
#:
#: It also decodes PortAudio's opaque error. ``-9999`` means nothing to a user;
#: what it almost always is on Windows is a capture endpoint that exists, is
#: enabled, and cannot be opened - typically because another application holds
#: it exclusively, or a virtual-audio driver is wedged. That is a diagnosable
#: condition with a known fix, and saying so beats printing a raw errno.
_MIC_PROBE_SOURCE = (
    "import speech_recognition as sr\n"
    "def _open(index):\n"
    "    mic = sr.Microphone(device_index=index)\n"
    "    audio = mic.pyaudio_module.PyAudio()\n"
    "    try:\n"
    "        stream = sr.Microphone.MicrophoneStream(audio.open(\n"
    "            input_device_index=index, channels=1, format=mic.format,\n"
    "            rate=mic.SAMPLE_RATE, frames_per_buffer=mic.CHUNK, input=True))\n"
    "        stream.close()\n"
    "        return mic.device_index\n"
    "    finally:\n"
    "        audio.terminate()\n"
    "candidates = [None]\n"
    "try:\n"
    "    pa = sr.Microphone().pyaudio_module.PyAudio()\n"
    "    candidates += [d['index'] for d in (\n"
    "        pa.get_device_info_by_index(i) for i in range(pa.get_device_count()))\n"
    "        if d['maxInputChannels'] > 0]\n"
    "    pa.terminate()\n"
    "except Exception:\n"
    "    pass\n"
    "last = ''\n"
    "for idx in candidates:\n"
    "    try:\n"
    "        print('OK input device index=%s' % _open(idx))\n"
    "        raise SystemExit(0)\n"
    "    except SystemExit:\n"
    "        raise\n"
    "    except Exception as e:\n"
    "        last = '%s: %s' % (type(e).__name__, e)\n"
    "names = []\n"
    "try:\n"
    "    pa = sr.Microphone().pyaudio_module.PyAudio()\n"
    "    names = [d['name'] for d in (pa.get_device_info_by_index(i)\n"
    "        for i in range(pa.get_device_count())) if d['maxInputChannels'] > 0]\n"
    "    pa.terminate()\n"
    "except Exception:\n"
    "    pass\n"
    "if '-9999' in last or 'Unanticipated host error' in last:\n"
    "    print('PortAudio could not open any of the %d input endpoints Windows '\n"
    "          'reports. The capture endpoint is present and enabled but is '\n"
    "          'being held or is wedged. Another app using the microphone '\n"
    "          'exclusively, or a virtual-audio driver (Voicemod/VoiceMeeter/'\n"
    "          'NVIDIA Broadcast) is the usual cause. Try: close apps using '\n"
    "          'the mic, then restart the Windows Audio service. Last error: '\n"
    "          '%s' % (len(names), last))\n"
    "elif names:\n"
    "    print('Windows lists %d input endpoints but none could be opened. '\n"
    "          'Last error: %s' % (len(names), last))\n"
    "else:\n"
    "    print('No input endpoint at all. Last error: %s' % last)\n"
    "raise SystemExit(1)\n"
)

_mic_probe_lock = threading.Lock()
_mic_probe: Dict[str, Any] = {"at": 0.0, "ok": False, "detail": "not probed yet"}

#: Every PortAudio touch in this process goes through this lock.
#:
#: ``pyaudio.PyAudio()`` is not re-entrant: its C initialisation is not
#: thread-safe, and two threads constructing a microphone at the same instant
#: took the whole API server down with a native access violation. Captured
#: from the running process with ``-X faulthandler``:
#:
#:     Windows fatal exception: access violation
#:       pyaudio/__init__.py:591 in __init__      <- thread A
#:       pyaudio/__init__.py:591 in __init__      <- thread B, same moment
#:       speech_recognition/__init__.py:78 in __init__
#:       core/voice.py:205 in status
#:
#: Two threads really did overlap: the browser polls the voice state more than
#: once a second, and every poll opens the device. Serialising capture costs a
#: queue and nothing else; a native crash costs the server. One microphone, one
#: listener at a time - which is also what an always-on loop needs anyway.
_microphone_lock = threading.RLock()


def probe_microphone(ttl: float = MIC_PROBE_TTL_S) -> Dict[str, Any]:
    """Can this machine actually open an input device?

    Run in a **child process**, and that is the entire point.

    ``pyaudio.PyAudio()`` calls into PortAudio, and on a machine with no usable
    input device this build of PortAudio does not raise - it dereferences a
    null host-API pointer and takes the process down with a native access
    violation. Observed and reproduced here:

        Windows fatal exception: access violation
          pyaudio/__init__.py:591 in __init__
          speech_recognition/__init__.py:78 in __init__
          core/voice.py:205 in status

    A segfault cannot be caught by ``except``, so the "graceful" handling that
    wrapped the call was worth nothing: the whole API server died mid-request,
    with no traceback in its own log and nothing for the user to see but a
    stopped backend. Doing the identical call in a child process costs one
    interpreter start and turns an uncatchable crash into an ordinary answer -
    "no input device" - which is the truth, and which the rest of this module
    already knows how to report.

    Cached because it is a subprocess: one child per :data:`MIC_PROBE_TTL_S`,
    for both the positive and the negative result, so a machine with no
    microphone does not pay for one every time the UI polls.
    """
    now = time.time()
    with _mic_probe_lock:
        cached = _mic_probe
        if cached["detail"] != "not probed yet" and (now - cached["at"]) < ttl:
            return dict(cached)

    detail = "microphone probe did not finish"
    ok = False
    try:
        import subprocess

        completed = subprocess.run(
            [sys.executable, "-c", _MIC_PROBE_SOURCE],
            capture_output=True, text=True, timeout=MIC_PROBE_TIMEOUT_S,
        )
        if completed.returncode == 0:
            ok = True
            said = (completed.stdout or "").strip().splitlines()
            detail = (said[-1] if said else
                      "input device opened (probed in a child process)")
        else:
            # The probe prints its human-readable diagnosis to stdout and any
            # raw traceback to stderr. Prefer stdout: that is where the
            # actionable explanation lives, and it is what the user needs to
            # see. Fall back to stderr for a genuine crash.
            said = [ln.strip() for ln in (completed.stdout or "").splitlines()
                    if ln.strip()]
            reason = said[-1] if said else ""
            if not reason:
                tail = (completed.stderr or "").strip().splitlines()
                reason = next((ln for ln in reversed(tail)
                               if ln.strip() and not ln.startswith("  ")), "")
            detail = (f"no usable input device: {reason[:400]}" if reason
                      else f"no usable input device: probe exited "
                           f"{completed.returncode}")
    except subprocess.TimeoutExpired:
        detail = "microphone probe timed out"
    except Exception as exc:  # noqa: BLE001 - a failed probe is not a crash
        detail = f"microphone probe failed: {type(exc).__name__}: {exc}"

    result = {"at": time.time(), "ok": ok, "detail": detail}
    with _mic_probe_lock:
        _mic_probe.update(result)
    return dict(result)


@dataclass
class VoiceStatus:
    """What the voice stack can actually do right now, and why not if it can't."""

    available: bool = False
    listening: bool = False
    speaking: bool = False
    microphone: bool = False
    microphone_detail: str = "not probed"
    transcriber: bool = False
    transcriber_detail: str = "not loaded"
    speech_synth: bool = False
    speech_synth_detail: str = "not loaded"
    model: str = DEFAULT_MODEL
    voices: List[str] = field(default_factory=list)
    echo_guard_s: float = 0.0
    problems: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        data = {
            "available": self.available,
            "listening": self.listening,
            "speaking": self.speaking,
            "microphone": self.microphone,
            "microphone_detail": self.microphone_detail,
            "transcriber": self.transcriber,
            "transcriber_detail": self.transcriber_detail,
            "speech_synth": self.speech_synth,
            "speech_synth_detail": self.speech_synth_detail,
            "model": self.model,
            "voices": self.voices,
            "echo_guard_s": round(self.echo_guard_s, 2),
            "problems": list(self.problems),
        }
        return data


class UltronVoice:
    """Process-wide voice I/O. Every entry point is safe to call off-thread."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model_name = model
        self._model: Any = None
        self._engine: Any = None
        self._recogniser: Any = None
        self._lock = threading.RLock()
        self._mic_detail = "not probed"
        self._tts_detail = "not loaded"
        self._asr_detail = "not loaded"
        self._last_spoken: str = ""
        self._echo_until: float = 0.0
        self._listening = False
        self._speaking = False
        self._recogniser_ready = False

    # -- capability probing ---------------------------------------------
    def _ensure_recogniser(self) -> bool:
        if self._recogniser_ready:
            return self._recogniser is not None
        with self._lock:
            if self._recogniser_ready:
                return self._recogniser is not None
            try:
                import speech_recognition as sr

                self._recogniser = sr.Recognizer()
                self._recogniser_ready = True
                self._mic_detail = "speech_recognition + pyaudio ready"
            except Exception as exc:  # noqa: BLE001
                self._recogniser = None
                self._mic_detail = f"unavailable: {type(exc).__name__}: {exc}"
            return self._recogniser is not None

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            try:
                import whisper

                log.info("Loading whisper model '%s' (first run downloads it)",
                         self._model_name)
                self._model = whisper.load_model(self._model_name)
                self._asr_detail = f"whisper '{self._model_name}' loaded"
                return True
            except Exception as exc:  # noqa: BLE001
                self._asr_detail = f"unavailable: {type(exc).__name__}: {exc}"
                log.warning("Whisper unavailable: %s", exc)
                return False

    def _ensure_engine(self) -> bool:
        if self._engine is not None:
            return True
        with self._lock:
            if self._engine is not None:
                return True
            try:
                import pyttsx3

                engine = pyttsx3.init()
                try:
                    engine.setProperty("rate", 175)
                except Exception:  # noqa: BLE001 - rate is cosmetic
                    pass
                self._engine = engine
                self._tts_detail = "pyttsx3 ready"
                return True
            except Exception as exc:  # noqa: BLE001
                self._tts_detail = f"unavailable: {type(exc).__name__}: {exc}"
                log.warning("pyttsx3 unavailable: %s", exc)
                return False

    def _voice_names(self, ensure: bool = True) -> List[str]:
        """Installed TTS voices, loading the engine only if asked to.

        ``status()`` passes ``ensure=False``: ``pyttsx3.init()`` spawns a COM
        speech-synthesiser process, which is exactly the kind of heavyweight
        side effect a read-only capability probe must not have.
        """
        if ensure and not self._ensure_engine():
            return []
        if self._engine is None:
            return []
        try:
            voices = self._engine.getProperty("voices") or []
            return [str(getattr(v, "name", v)) for v in voices]
        except Exception:  # noqa: BLE001
            return []

    def _tts_installed(self) -> bool:
        """Is pyttsx3 importable, without initialising it?"""
        if self._engine is not None:
            return True
        try:
            importlib.import_module("pyttsx3")
            return True
        except Exception:  # noqa: BLE001
            return False

    def status(self) -> VoiceStatus:
        """Probe the stack. Never raises, never loads the Whisper model."""
        out = VoiceStatus(model=self._model_name)

        mic_ready = self._ensure_recogniser()
        if mic_ready:
            # Probed out-of-process on purpose - see probe_microphone(). Opening
            # the device here used to be able to take the whole server down.
            probe = probe_microphone()
            out.microphone = bool(probe["ok"])
            out.microphone_detail = probe["detail"]
        else:
            out.microphone = False
            out.microphone_detail = self._mic_detail

        out.transcriber = self._model is not None
        out.transcriber_detail = self._asr_detail
        out.speech_synth = self._tts_installed()
        out.speech_synth_detail = (
            "pyttsx3 ready" if self._engine is not None
            else ("pyttsx3 importable; loads on first speak"
                  if out.speech_synth else self._tts_detail)
        )
        out.voices = self._voice_names(ensure=False)
        out.listening = self._listening
        out.speaking = self._speaking
        out.echo_guard_s = max(0.0, self._echo_until - time.time())
        out.available = out.microphone
        if not out.microphone:
            out.problems.append(
                "No microphone available - voice input is unavailable. "
                "Text goals and spoken replies are unaffected."
            )
        if not out.speech_synth:
            out.problems.append(
                "No speech synthesiser - Ultron can listen but cannot speak."
            )
        return out

    # -- echo guard ------------------------------------------------------
    def echo_guard_remaining(self) -> float:
        return max(0.0, self._echo_until - time.time())

    def _mark_speaking(self, text: str) -> float:
        """Record how long the speaker will keep playing after we return."""
        words = len(re.findall(r"\S+", text or ""))
        seconds = (words / WORDS_PER_MINUTE) * 60.0 + ECHO_MARGIN_S
        self._last_spoken = text
        self._echo_until = time.time() + seconds
        return seconds

    def _is_echo(self, heard: str) -> bool:
        """True when a transcript is close enough to be our own last reply.

        Guards the case the timestamp cannot: the user says something during the
        tail and both voices land in the same clip.
        """
        if not heard or not self._last_spoken:
            return False
        normalise = lambda s: set(re.findall(r"[a-z0-9']+", s.lower()))  # noqa: E731
        heard_words = normalise(heard)
        if not heard_words:
            return False
        spoken_words = normalise(self._last_spoken)
        if not spoken_words:
            return False
        overlap = len(heard_words & spoken_words) / len(heard_words)
        return overlap >= 0.8

    # -- transcription ---------------------------------------------------
    def transcribe(self, audio: bytes) -> Dict[str, Any]:
        """Transcribe raw WAV bytes locally with Whisper."""
        if not audio:
            return {"ok": False, "text": "", "error": "no audio captured"}

        # Decode the container *before* touching Whisper. ``_ensure_model`` may
        # pull a ~140 MB model off disk, and a malformed clip should fail in
        # microseconds without paying for it.
        try:
            import numpy as np
            import wave

            with wave.open(io.BytesIO(audio), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                width = wav.getsampwidth()
                rate = wav.getframerate()
                channels = wav.getnchannels()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "",
                    "error": f"not a readable WAV clip: {type(exc).__name__}"}

        dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
        if dtype is None:
            return {"ok": False, "text": "",
                    "error": f"unsupported sample width {width}"}
        if rate <= 0:
            return {"ok": False, "text": "", "error": "clip reports no sample rate"}

        if not self._ensure_model():
            return {"ok": False, "text": "", "error": self._asr_detail}
        try:
            audio_np = np.frombuffer(frames, dtype=dtype).astype(np.float32) / 32768.0
            if channels > 1:
                audio_np = audio_np.reshape(-1, channels).mean(axis=1)
            result = self._model.transcribe(audio_np, fp16=False)
            text = str(result.get("text", "")).strip()
            return {
                "ok": True,
                "text": text,
                "language": result.get("language", ""),
                "seconds": round(len(audio_np) / float(rate or 1), 2),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("Transcription failed: %s", exc)
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {exc}"}

    # -- public: listen / speak -----------------------------------------
    def listen(self, timeout: float = 8.0, phrase_limit: float = 12.0) -> Dict[str, Any]:
        """Record one utterance and transcribe it.

        Refuses while the speakers are still playing the last reply, which is
        what stops Ultron answering itself.
        """
        if not self._ensure_recogniser() or self._recogniser is None:
            return {"ok": False, "text": "",
                    "error": f"no microphone: {self._mic_detail}"}

        # Never reach `sr.Microphone()` in this process without a device
        # confirmed out-of-process first. See probe_microphone(): on a machine
        # with no input device this call is a native crash, not an exception.
        probe = probe_microphone()
        if not probe["ok"]:
            return {"ok": False, "text": "",
                    "error": f"no microphone: {probe['detail']}"}

        remaining = self.echo_guard_remaining()
        if remaining > 0:
            return {"ok": False, "text": "", "echo_guard_s": round(remaining, 2),
                    "error": "suppressed: Ultron is still speaking. "
                             "Try again when it has finished."}

        timeout = max(1.0, min(float(timeout), MAX_UTTERANCE_S))
        phrase_limit = max(1.0, min(float(phrase_limit), MAX_UTTERANCE_S))
        self._listening = True
        try:
            # `sr.Microphone()`, NOT `self._recogniser.Microphone()`.
            # `sr.Recognizer` is the *recognition* engine - it has no
            # Microphone attribute - so the previous spelling raised
            # AttributeError on every single call and voice input had never
            # worked. Caught in the UI as
            #   "Listening failed: AttributeError: 'Recognizer' object has no
            #    attribute 'Microphone'"
            # The microphone is a module-level context manager; the recogniser
            # consumes it. Regression-locked by
            # tests/test_sara_ultron_phase4.py::test_listen_uses_module_microphone.
            import speech_recognition as sr

            # Serialised: see _microphone_lock. The lock covers capture only -
            # Whisper runs outside it, so a slow transcription never blocks the
            # next listener from opening the device.
            with _microphone_lock:
                with sr.Microphone() as source:
                    # `Microphone.__enter__` swallows a stream-open failure and
                    # leaves `stream = None`, so the only symptom is a
                    # `NoneType.close` from `__exit__`. Saying that to a user is
                    # useless; the probe already knows the real reason, so report
                    # it instead of the plumbing failure.
                    if getattr(source, "stream", None) is None:
                        detail = probe_microphone()["detail"]
                        return {"ok": False, "text": "",
                                "error": f"no microphone: {detail}"}
                    # Ambient noise as the baseline for what "silence" means here.
                    try:
                        self._recogniser.adjust_for_ambient_noise(source, duration=0.5)
                    except Exception:  # noqa: BLE001 - calibration is best effort
                        pass
                    audio = self._recogniser.listen(source, timeout=timeout,
                                                    phrase_time_limit=phrase_limit)
                raw = audio.get_wav_data()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "",
                    "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._listening = False

        result = self.transcribe(raw)
        if result.get("ok") and self._is_echo(result.get("text", "")):
            return {"ok": False, "text": "", "echo": True,
                    "error": "discarded: that matched Ultron's own last reply."}
        return result

    def speak(self, text: str) -> Dict[str, Any]:
        """Say something aloud and arm the echo guard for its duration."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "spoken": "", "error": "nothing to say"}
        if not self._ensure_engine():
            return {"ok": False, "spoken": "", "error": self._tts_detail}

        # Arm the guard *before* the audio starts, not after. `runAndWait()`
        # blocks until the room has finished playing, so arming afterwards
        # would hold the guard open for a second full playback of a reply that
        # has already ended. Armed up front it also refuses a listen() that
        # arrives mid-sentence, and expires ~ECHO_MARGIN_S after the real end
        # of the audio.
        tail = self._mark_speaking(text)
        with self._lock:
            self._speaking = True
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception as exc:  # noqa: BLE001
                # A partial play still reached the room, so the guard stays
                # armed; only the caller is told it did not finish.
                return {"ok": False, "spoken": "", "echo_guard_s": round(tail, 2),
                        "error": f"{type(exc).__name__}: {exc}"}
            finally:
                self._speaking = False
        return {"ok": True, "spoken": text, "echo_guard_s": round(tail, 2)}

    def cancel_speech(self) -> Dict[str, Any]:
        """Stop mid-sentence and drop the echo guard."""
        with self._lock:
            if self._engine is None:
                return {"ok": False, "error": "speech engine not loaded"}
            try:
                self._engine.stop()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._speaking = False
        self._echo_until = 0.0
        return {"ok": True}

    # -- native tool surface ---------------------------------------------
    def as_tools(self) -> List[Dict[str, Any]]:
        """Expose listening and speaking to the planner.

        A goal that arrives by voice still runs through the normal plan/execute
        loop, so the approval gate and the trace apply exactly as they do for a
        typed goal.
        """
        return [
            {
                "name": "ultron_listen",
                "description": (
                    "Listen to one utterance from the user's microphone and "
                    "transcribe it locally. Use this when the goal explicitly "
                    "asks to be heard or listened to, or to confirm a spoken "
                    "instruction. Returns the transcript, or an error explaining "
                    "why nothing was heard."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "timeout": {
                            "type": "NUMBER",
                            "description": "Seconds to wait for the user to start speaking. Default 8.",
                        },
                        "phrase_limit": {
                            "type": "NUMBER",
                            "description": "Seconds of speech to accept before cutting out. Default 12.",
                        },
                    },
                },
                "handler": self._tool_listen,
            },
            {
                "name": "ultron_speak",
                "description": (
                    "Say a reply out loud using the system speech synthesiser. "
                    "Arms an echo guard so a later listen() does not transcribe "
                    "Ultron's own voice."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "text": {
                            "type": "STRING",
                            "description": "What to say. Keep it to one or two sentences.",
                        },
                    },
                },
                "handler": self._tool_speak,
            },
        ]

    def _tool_listen(self, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        try:
            timeout = float(args.get("timeout", 8))
            phrase_limit = float(args.get("phrase_limit", 12))
        except (TypeError, ValueError):
            timeout, phrase_limit = 8.0, 12.0
        result = self.listen(timeout=timeout, phrase_limit=phrase_limit)
        if not result.get("ok"):
            return f"Voice input failed: {result.get('error', 'unknown reason')}"
        text = str(result.get("text", "")).strip()
        if not text:
            return "Voice input failed: no speech was recognised in that window."
        return f"Heard: {text}"

    def _tool_speak(self, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        text = str(args.get("text", "") or "").strip()
        if not text:
            return "Voice output failed: no 'text' was given to speak."
        result = self.speak(text)
        if not result.get("ok"):
            return f"Voice output failed: {result.get('error', 'unknown reason')}"
        return f"Spoken: {text}"


#: Process-wide voice, created on first use.
_voice: Optional[UltronVoice] = None
_voice_lock = threading.Lock()


def get_voice(model: str = DEFAULT_MODEL) -> UltronVoice:
    global _voice
    with _voice_lock:
        if _voice is None:
            _voice = UltronVoice(model=model)
        return _voice
