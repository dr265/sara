"""
Twilio Media Streams 2.0 bridge — real bidirectional audio for SARA.

WHY THIS EXISTS
---------------
The voice engine's TwiML path (<Say>/<Gather>/<Redirect>) is half-duplex: Twilio
records the caller, posts it to a webhook, and only then does SARA speak. That
can never feel like a conversation — every turn pays a full round-trip and the
caller can never interrupt SARA mid-sentence.

`<Connect><Stream>` opens a WebSocket and pushes 20 ms mu-law frames
*continuously* in both directions. This module turns that socket into a real
telephone conversation:

    caller audio -> VAD turn-taking -> Sarvam STT -> booking engine
                 -> Sarvam TTS     -> downsample  -> mu-law -> caller

Design notes
------------
* **No `audioop`.** It was removed in Python 3.13 (this project's runtime), so
  G.711 mu-law and the resampler are pure Python. At 8 kHz that is ~8k
  samples/sec — far below any hot path.
* **Energy VAD with an adaptive noise floor**, hysteresis and hangover rather
  than a fixed threshold, which either clips quiet speakers or transcribes a
  door slam depending on how it is tuned.
* **Pre-roll buffer** keeps ~120 ms from *before* onset so the first syllable
  is never clipped.
* **Barge-in.** When the caller speaks over SARA we drop playback and send a
  Twilio `clear` to flush what Twilio has already buffered.
* **Adaptive latency.** A fast turn is answered with the answer alone; only a
  slow turn gets a spoken filler. Always saying "one moment" is the single
  biggest tell of a machine.

The engine injects `VoiceStreamHooks`, so this module has no import-time
dependency on the engine and stays unit-testable.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("sara.voice.stream")

# --------------------------------------------------------------------------
# Telephony audio constants (Twilio Media Streams)
# --------------------------------------------------------------------------
TWILIO_SAMPLE_RATE = 8_000
TWILIO_FRAME_MS = 20
TWILIO_FRAME_SAMPLES = TWILIO_SAMPLE_RATE * TWILIO_FRAME_MS // 1000   # 160
TWILIO_FRAME_BYTES = TWILIO_FRAME_SAMPLES                               # 160 B

MAX_UTTERANCE_S = 15.0       # force a turn end on a long monologue
MIN_SPEECH_S = 0.28          # shorter than this is a cough / door click
ENDPOINT_SILENCE_S = 0.70    # pause that ends the caller's turn
FAST_TURN_S = 0.90           # answer without a filler below this
TURN_TIMEOUT_S = 12.0        # hard ceiling on one engine turn
STT_TIMEOUT_S = 6.0
# Sarvam TTS latency is measured between ~3 s and ~35 s depending on load.
# A 9 s ceiling meant most replies were cancelled mid-render and the caller
# heard nothing; phrases are cache hits, so this ceiling only applies to
# genuinely new text.
TTS_TIMEOUT_S = 45.0


# --------------------------------------------------------------------------
# G.711 mu-law codec (pure Python — `audioop` is gone in 3.13)
# --------------------------------------------------------------------------

def _build_ulaw_tables() -> tuple[list[int], bytes]:
    """Pre-compute the 256-entry decode table and the 16-bit encode table."""
    decode = [0] * 256
    for u in range(256):
        c = ~u & 0xFF
        sign = c & 0x80
        exponent = (c >> 4) & 0x07
        mantissa = c & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        decode[u] = -sample if sign else sample

    bias, clip = 0x84, 32635
    encode = bytearray(65536)
    for pcm in range(-clip, clip + 1):
        magnitude = -pcm if pcm < 0 else pcm
        sign = 0x80 if pcm < 0 else 0x00
        biased = magnitude + bias
        exponent = 7
        for mask in (0x4000, 0x2000, 0x1000, 0x0800, 0x0400, 0x0200, 0x0100):
            if biased & mask:
                break
            exponent -= 1
        mantissa = (biased >> (exponent + 3)) & 0x0F
        encode[pcm + 32768] = (~(sign | (exponent << 4) | mantissa)) & 0xFF
    return decode, bytes(encode)


_ULAW_TO_LINEAR, _LINEAR_TO_ULAW = _build_ulaw_tables()


def ulaw_to_pcm(payload: bytes) -> bytes:
    """Decode 8 kHz mono mu-law into signed 16-bit little-endian PCM."""
    table = _ULAW_TO_LINEAR
    return struct.pack(f"<{len(payload)}h", *(table[b] for b in payload))


def pcm_to_ulaw(pcm: bytes) -> bytes:
    """Encode signed 16-bit little-endian PCM into 8 kHz mono mu-law."""
    count = len(pcm) // 2
    if count == 0:
        return b""
    table = _LINEAR_TO_ULAW
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    return bytes(table[s + 32768] for s in samples)


def pcm_rms(pcm: bytes) -> float:
    """Root-mean-square level of 16-bit PCM — the VAD's energy measure."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    total = 0.0
    for s in struct.unpack(f"<{count}h", pcm[: count * 2]):
        total += s * s
    return math.sqrt(total / count)


# --------------------------------------------------------------------------
# WAV helpers — Sarvam speaks WAV, the phone speaks mu-law
# --------------------------------------------------------------------------

def wav_from_pcm(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM in a canonical 44-byte RIFF/WAVE header."""
    if not pcm:
        pcm = b"\x00\x00"
    if len(pcm) % 2:
        pcm += b"\x00"
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack(
            "<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16
        )
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def _to_mono16(body: bytes, channels: int, bits: int) -> bytes:
    """Downmix and widen arbitrary PCM to signed 16-bit mono."""
    if not body:
        return b""
    if bits == 8:
        return struct.pack(f"<{len(body)}h", *((b - 128) << 8 for b in body))
    if bits == 32:
        count = len(body) // 4
        values = struct.unpack(f"<{count}i", body[: count * 4])
        return struct.pack(
            f"<{count}h", *(max(-32768, min(32767, v >> 16)) for v in values)
        )
    if bits != 16:
        return body
    count = len(body) // 2
    if channels <= 1:
        return body[: count * 2]
    samples = struct.unpack(f"<{count}h", body[: count * 2])
    frames = count // channels
    mixed = [
        sum(samples[i * channels: i * channels + channels]) // channels
        for i in range(frames)
    ]
    return struct.pack(f"<{len(mixed)}h", *mixed)


def parse_wav(data: bytes) -> tuple[bytes, int]:
    """Extract (mono 16-bit PCM, sample_rate) from a WAV blob.

    Hand-rolled rather than using the `wave` module so streams carrying extra
    chunks (LIST, fact, WAVE_FORMAT_EXTENSIBLE) still parse — TTS providers
    vary, and a hard failure here would kill the whole call.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data, TWILIO_SAMPLE_RATE  # headerless PCM

    rate = channels = bits = None
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = data[pos + 8: pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            audio_format, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if audio_format not in (1, 0xFFFE):  # 0xFFFE = EXTENSIBLE
                return _to_mono16(body, 1, 16), TWILIO_SAMPLE_RATE
        elif cid == b"data":
            if not rate:
                return _to_mono16(body, 1, 16), TWILIO_SAMPLE_RATE
            return _to_mono16(body, channels or 1, bits or 16), rate
        pos += 8 + size + (size & 1)   # RIFF chunks are word-aligned
    return b"", TWILIO_SAMPLE_RATE


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler — TTS 22.05 kHz -> phone 8 kHz."""
    if src_rate == dst_rate or not pcm:
        return pcm
    src_count = len(pcm) // 2
    if src_count == 0:
        return pcm
    samples = struct.unpack(f"<{src_count}h", pcm[: src_count * 2])
    ratio = src_rate / dst_rate
    dst_count = max(1, int(src_count / ratio))
    out = bytearray()
    last = src_count - 1
    for i in range(dst_count):
        pos = i * ratio
        left = int(pos)
        if left >= last:
            out += struct.pack("<h", samples[last])
            continue
        frac = pos - left
        value = samples[left] + (samples[left + 1] - samples[left]) * frac
        out += struct.pack("<h", max(-32768, min(32767, int(round(value)))))
    return bytes(out)


def to_twilio_frames(pcm_8k: bytes) -> list[bytes]:
    """Split 8 kHz PCM into the 160-byte mu-law frames Twilio expects.

    One frame is 20 ms = 160 *samples* = 320 bytes of 16-bit PCM, which
    encodes to 160 bytes of mu-law. Stepping by samples (not bytes) is what
    keeps every outbound frame at the size Twilio requires.
    """
    if not pcm_8k:
        return []
    stride = TWILIO_FRAME_SAMPLES * 2          # bytes of PCM per 20 ms frame
    pad = (-len(pcm_8k)) % stride
    pcm_8k = pcm_8k + b"\x00" * pad
    return [
        pcm_to_ulaw(pcm_8k[i: i + stride])
        for i in range(0, len(pcm_8k), stride)
    ]


# --------------------------------------------------------------------------
# Voice activity detection
# --------------------------------------------------------------------------

class VoiceActivityDetector:
    """Energy VAD with an adaptive noise floor, hysteresis and hangover.

    Three things separate this from a naive "is it loud?" gate:

    1. **Adaptive floor** — line noise varies wildly between a landline, a
       handset and a laptop mic. The floor is a slow EMA that only updates
       while the caller is *not* speaking, so speech never inflates it.
    2. **Hysteresis + hangover** — onset needs several consecutive voiced frames
       (kills clicks and keyboard taps) and the endpoint needs a sustained pause
       (kills the gap mid-sentence, the classic "SARA talks over herself" bug).
    3. **Pre-roll** — audio from just before onset is retained so the first
       syllable is never clipped.
    """

    def __init__(
        self,
        *,
        on_threshold: float = 380.0,
        noise_floor: float = 120.0,
        hangover_frames: int = 3,
        preroll_frames: int = 6,
    ) -> None:
        self.on_threshold = on_threshold
        self.noise_floor = noise_floor
        self.hangover_frames = hangover_frames
        self.preroll_frames = preroll_frames

        self._preroll: list[bytes] = []
        self._voiced_run = 0
        self._speaking = False
        self._active_ms = 0
        self._silence_ms = 0
        self._utterance: list[bytes] = []
        self._started_at = 0.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def onset_threshold(self) -> float:
        """Current onset bar (also used for the barge-in check)."""
        return max(self.on_threshold, self.noise_floor * 2.6)

    def _release_threshold(self) -> float:
        # A lower bar keeps the turn open so soft trailing syllables survive.
        return max(self.on_threshold * 0.6, self.noise_floor * 1.7)

    def reset(self) -> None:
        self._preroll.clear()
        self._voiced_run = 0
        self._speaking = False
        self._active_ms = 0
        self._silence_ms = 0
        self._utterance = []
        self._started_at = 0.0

    def push(self, pcm: bytes) -> Optional[bytes]:
        """Feed one 20 ms PCM frame.

        Returns the caller's completed utterance when their turn ends, else
        None. A burst shorter than MIN_SPEECH_S is discarded internally and
        also returns None — a cough must never become a conversational turn.
        """
        level = pcm_rms(pcm)
        utterance: Optional[bytes] = None

        if not self._speaking:
            self._preroll.append(pcm)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)

            if level >= self.onset_threshold():
                self._voiced_run += 1
            else:
                self._voiced_run = 0
                self.noise_floor += (max(level, 1.0) - self.noise_floor) * 0.05

            if self._voiced_run >= self.hangover_frames:
                self._speaking = True
                self._started_at = time.monotonic()
                self._active_ms = 0
                self._silence_ms = 0
                self._utterance = list(self._preroll)   # pre-roll included
                self._preroll.clear()
            return None

        self._utterance.append(pcm)
        if level >= self._release_threshold():
            self._active_ms += TWILIO_FRAME_MS
            self._silence_ms = 0
        else:
            self._silence_ms += TWILIO_FRAME_MS

        elapsed_ms = (time.monotonic() - self._started_at) * 1000
        ended = self._silence_ms >= int(ENDPOINT_SILENCE_S * 1000)
        if ended or elapsed_ms >= MAX_UTTERANCE_S * 1000:
            blob = b"".join(self._utterance)
            long_enough = self._active_ms >= MIN_SPEECH_S * 1000
            self.reset()
            if long_enough and blob:
                utterance = blob
        return utterance


# --------------------------------------------------------------------------
# Engine bridge
# --------------------------------------------------------------------------

async def _noop_warm(session_key: str) -> None:
    """Default `warm_session`: nothing to prime."""


@dataclass(kw_only=True)
class VoiceStreamHooks:
    """Everything the stream needs from the voice engine.

    Injected rather than imported so this module stays free of engine-level
    imports (and of import cycles) and can be unit-tested in isolation.
    """

    # (call_sid) -> our Call.id, or None.
    resolve_call_id: Callable[[str], Optional[str]]
    # (session_key) -> caller phone, or None. Optional: deployments that do not
    # use number-based memory simply omit it.
    resolve_caller_phone: Callable[[str], Optional[str]] = lambda _sid: None
    # (wav_bytes, language_hint) -> {"text": str, "language_code": str | None}
    transcribe: Callable[[bytes, Optional[str]], Awaitable[dict]]
    # (text, language) -> wav bytes (b"" on failure)
    speak: Callable[[str, str], Awaitable[bytes]]
    # (session_key, text, call_id=..., caller_phone=...) -> TurnResult
    turn: Callable[..., Awaitable[Any]]
    # ("filler" | "greeting", language) -> spoken text
    phrase: Callable[[str, str], str]
    default_language: str = "en-IN"
    # Optional one-shot hook invoked right after `start` (session priming).
    # It is a FIELD, not a method, so the engine can inject it.
    warm_session: Callable[[str], Awaitable[None]] = _noop_warm


@dataclass
class PlaybackQueue:
    """Paced outbound audio with barge-in cancellation."""

    frames: list[bytes] = field(default_factory=list)
    cancelled: bool = False
    speaking: bool = False


class MediaStreamSession:
    """One live phone call over a Twilio Media Streams WebSocket.

    Event flow, against Twilio's documented protocol:

        connected -> start -> media* -> (mark | dtmf | stop)

    Inbound `media` frames are mu-law decoded, fed to the VAD and turned into
    turns. Each turn runs STT -> booking engine -> TTS -> outbound `media`
    frames, with a Twilio `clear` on barge-in.
    """

    def __init__(self, websocket: Any, hooks: VoiceStreamHooks) -> None:
        self.ws = websocket
        self.hooks = hooks

        self.stream_sid: Optional[str] = None
        self.call_sid: str = ""
        self.caller_phone: Optional[str] = None
        self.session_key: str = "twilio:unknown"
        self.call_id: Optional[str] = None
        self.language: str = hooks.default_language

        self.vad = VoiceActivityDetector()
        self.playback = PlaybackQueue()
        self._playback_task: Optional[asyncio.Task] = None
        self._turn_task: Optional[asyncio.Task] = None
        self._closing = False
        self._speech_idle = asyncio.Event()
        self._speech_idle.set()

    # -- socket helpers ----------------------------------------------------

    async def _send(self, payload: dict) -> None:
        if self._closing or self.ws is None:
            return
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception as exc:   # a dead socket must not raise into the loop
            log.debug("stream send failed: %s", exc)

    async def _send_frame(self, frame: bytes) -> None:
        await self._send({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {
                "track": "outbound",
                "payload": base64.b64encode(frame).decode("ascii"),
            },
        })

    # -- playback ----------------------------------------------------------

    async def _enqueue_speech(self, wav: bytes) -> None:
        """Convert TTS output to Twilio frames and play it at real time."""
        if not wav:
            return
        pcm, rate = parse_wav(wav)
        if not pcm:
            return
        frames = to_twilio_frames(resample_pcm(pcm, rate, TWILIO_SAMPLE_RATE))
        if not frames:
            return
        self.playback.frames.extend(frames)
        self.playback.speaking = True
        self._speech_idle.clear()
        if self._playback_task is None or self._playback_task.done():
            self._playback_task = asyncio.get_running_loop().create_task(
                self._drain_playback()
            )

    async def _drain_playback(self) -> None:
        """Feed queued frames to Twilio, one every 20 ms, until empty."""
        try:
            while self.playback.frames and not self._closing:
                if self.playback.cancelled:
                    self.playback.frames.clear()
                    break
                await self._send_frame(self.playback.frames.pop(0))
                await asyncio.sleep(TWILIO_FRAME_MS / 1000.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("playback drain stopped: %s", exc)
        finally:
            self.playback.frames.clear()
            self.playback.speaking = False
            self.playback.cancelled = False
            if not self._closing:
                self._speech_idle.set()

    async def _speak(self, text: str, budget: float = TTS_TIMEOUT_S) -> None:
        """Render and play one phrase; failures are swallowed, never fatal.

        A cache hit returns synchronously. A cache miss awaits Sarvam, which can
        be slow, so one bounded retry follows before we give up — better a
        slightly late reply than a caller left listening to nothing.
        """
        if not text or not text.strip():
            return
        for attempt in range(2):
            try:
                wav = await asyncio.wait_for(
                    self.hooks.speak(text, self.language),
                    timeout=budget if attempt == 0 else budget * 2,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "TTS budget exceeded call=%s attempt=%d text=%r",
                    self.call_sid, attempt + 1, text[:40],
                )
                continue
            except Exception as exc:
                log.warning("TTS failed call=%s: %s", self.call_sid, exc)
                return
            if wav:
                await self._enqueue_speech(wav)
                return
            # Empty audio means the provider gave us nothing; one retry.
        log.warning("no audio produced call=%s text=%r", self.call_sid, text[:40])


    # -- barge-in ----------------------------------------------------------

    async def _interrupt_playback(self) -> None:
        """The caller spoke over SARA: stop now and tell Twilio to forget it.

        The `clear` is what actually makes this feel human — Twilio holds
        roughly 2 s of our audio, so without it SARA keeps talking for a couple
        of seconds after being cut off.
        """
        if not self.playback.frames and not self.playback.speaking:
            return
        self.playback.cancelled = True
        self.playback.frames.clear()
        await self._send({"event": "clear", "streamSid": self.stream_sid})
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()

    # -- one conversational turn ------------------------------------------

    async def _run_turn(self, utterance_pcm: bytes) -> None:
        """STT -> booking engine -> TTS -> speaker, for one caller utterance."""
        try:
            heard = await asyncio.wait_for(
                self.hooks.transcribe(
                    wav_from_pcm(utterance_pcm, TWILIO_SAMPLE_RATE), self.language
                ),
                timeout=STT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning("STT timed out call=%s", self.call_sid)
            return
        except Exception as exc:
            log.warning("STT failed call=%s: %s", self.call_sid, exc)
            return

        text = ((heard or {}).get("text") or "").strip()
        if not text:
            return
        detected = (heard or {}).get("language_code")
        if detected:
            # Trust the STT's own detection for the TTS voice.
            with contextlib.suppress(Exception):
                self.language = detected
        log.info("stream turn call=%s heard=%r", self.call_sid, text[:80])

        turn_task = asyncio.get_running_loop().create_task(
            self.hooks.turn(
                self.session_key, text,
                call_id=self.call_id,
                caller_phone=self.caller_phone,
            )
        )
        self._turn_task = turn_task

        # Only stall with "one moment" when the answer is genuinely slow. A
        # fast turn is answered straight away, like a real receptionist.
        filler_sent = False
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(turn_task), timeout=FAST_TURN_S)
        if not turn_task.done():
            await self._speak(self.hooks.phrase("filler", self.language), budget=6.0)
            filler_sent = True

        try:
            result = await asyncio.wait_for(turn_task, timeout=TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("booking turn timed out call=%s", self.call_sid)
            return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("booking turn failed call=%s: %s", self.call_sid, exc)
            return
        finally:
            self._turn_task = None

        reply = (getattr(result, "reply", "") or "").strip()
        if not reply:
            return
        if filler_sent:
            # Never talk over our own filler.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._speech_idle.wait(), timeout=30.0)
        await self._speak(reply)


    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        """Consume Twilio events until the socket closes or the call stops."""
        log.info("media stream connected")
        try:
            while not self._closing:
                raw = await self.ws.receive_text()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("media stream ended call=%s: %s", self.call_sid, exc)
        finally:
            await self._teardown()

    async def _dispatch(self, message: dict) -> None:
        event = str(message.get("event") or "")
        if event == "start":
            await self._on_start(message)
        elif event == "media":
            await self._on_media(message)
        elif event == "dtmf":
            await self._on_dtmf(message)
        elif event in ("stop", "disconnected"):
            self._closing = True
            log.info("media stream stopped call=%s", self.call_sid)

    async def _on_start(self, message: dict) -> None:
        start = message.get("start") or {}
        self.stream_sid = start.get("streamSid")
        self.call_sid = str(start.get("callSid") or "")
        self.session_key = (
            f"twilio:{self.call_sid}" if self.call_sid else "twilio:unknown"
        )
        with contextlib.suppress(Exception):
            self.call_id = self.hooks.resolve_call_id(self.call_sid) or None
            self.caller_phone = self.hooks.resolve_caller_phone(self.call_sid)
        log.info(
            "media stream started call=%s stream=%s call_id=%s",
            self.call_sid, self.stream_sid, self.call_id,
        )
        with contextlib.suppress(Exception):
            await self.hooks.warm_session(self.session_key)
        # Greeting must never block the event loop. The budget is generous
        # because the phrase is served from the boot warm-up cache (instant);
        # it only matters on a cold cache, where Sarvam has been measured at
        # up to 35 s — a 5 s ceiling simply cut the caller off and produced
        # dead air on every call.
        asyncio.get_running_loop().create_task(
            self._speak(self.hooks.phrase("greeting", self.language), budget=45.0)
        )

    async def _on_media(self, message: dict) -> None:
        media = message.get("media") or {}
        if media.get("track") not in (None, "inbound"):
            return
        payload = media.get("payload")
        if not payload:
            return
        try:
            pcm = ulaw_to_pcm(base64.b64decode(payload))
        except Exception:
            return

        # Barge-in is checked BEFORE the frame enters the VAD so a frame is
        # never fed in twice (which would corrupt the endpointing state).
        if self.playback.speaking and pcm_rms(pcm) >= self.vad.onset_threshold():
            await self._interrupt_playback()

        utterance = self.vad.push(pcm)
        if utterance is not None:
            asyncio.get_running_loop().create_task(self._run_turn(utterance))

    async def _on_dtmf(self, message: dict) -> None:
        digit = str((message.get("dtmf") or {}).get("digit") or "").strip()
        if not digit:
            return
        # On a trial account the keypress is the caller's "I'm still here" —
        # reply with the greeting rather than leaving the call in silence.
        log.info("dtmf keypress call=%s digit=%s", self.call_sid, digit)
        if not self.vad.speaking:
            # Same generous budget as the opening greeting: this is the
            # caller's only feedback that we are alive, so a 5 s ceiling here
            # left trial-account callers hearing nothing after their keypress.
            await self._speak(self.hooks.phrase("greeting", self.language), budget=45.0)

    async def _teardown(self) -> None:
        self._closing = True
        self.playback.cancelled = True
        self.playback.frames.clear()
        for task in (self._playback_task, self._turn_task):
            if task is not None and not task.done():
                task.cancel()
        log.info("media stream closed call=%s", self.call_sid)

