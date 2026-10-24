"""SARA Voice Agent — Twilio Media Streams audio layer.

Covers the parts that decide whether a live call actually works: the G.711
mu-law codec, WAV packing/unpacking, resampling to the 8 kHz telephony rate,
Twilio frame sizing, and the energy VAD that decides when the caller has
finished speaking.
"""

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.voice_media_stream import (  # noqa: E402
    TWILIO_FRAME_BYTES,
    TWILIO_FRAME_SAMPLES,
    VoiceActivityDetector,
    parse_wav,
    pcm_rms,
    pcm_to_ulaw,
    resample_pcm,
    to_twilio_frames,
    ulaw_to_pcm,
    wav_from_pcm,
)


def _tone(seconds, freq=170.0, amp=9000, rate=8000):
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n)
    )


def test_ulaw_roundtrip_within_quantisation():
    """mu-law is logarithmic: the top segment steps by 512 and saturates."""
    worst = 0
    for pcm in range(-31000, 31001, 37):
        back = struct.unpack("<h", ulaw_to_pcm(pcm_to_ulaw(struct.pack("<h", pcm))))[0]
        worst = max(worst, abs(back - pcm))
    assert worst <= 512, worst


def test_ulaw_clips_instead_of_overflowing():
    top = struct.unpack("<h", ulaw_to_pcm(pcm_to_ulaw(struct.pack("<h", 32767))))[0]
    assert abs(top) <= 32124


def test_silence_decodes_to_near_zero():
    assert pcm_rms(ulaw_to_pcm(b"\xff" * 160)) < 40


def test_wav_roundtrip():
    pcm = _tone(0.25)
    parsed, rate = parse_wav(wav_from_pcm(pcm, 8000))
    assert parsed == pcm
    assert rate == 8000


def test_stereo_is_downmixed_to_mono():
    stereo = b"".join(struct.pack("<hh", 3000, -1000) for _ in range(100))
    blob = (
        b"RIFF" + struct.pack("<I", 36 + len(stereo)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 8000, 32000, 4, 16)
        + b"data" + struct.pack("<I", len(stereo)) + stereo
    )
    mixed, rate = parse_wav(blob)
    assert rate == 8000
    assert all(v == 1000 for v in struct.unpack("<100h", mixed))


def test_resample_22050_to_8000():
    hi = _tone(1.0, rate=22050)
    out = resample_pcm(hi, 22050, 8000)
    assert 7800 <= len(out) // 2 <= 8200


def test_frames_are_exactly_160_bytes():
    """20 ms at 8 kHz = 160 samples = 320 B PCM = 160 B mu-law."""
    frames = to_twilio_frames(_tone(0.5))
    assert frames
    assert all(len(f) == TWILIO_FRAME_BYTES for f in frames)
    assert TWILIO_FRAME_SAMPLES == 160


def test_vad_emits_an_utterance_after_speech_then_silence():
    vad = VoiceActivityDetector()
    tone, quiet = _tone(0.02), struct.pack("<160h", *([40] * 160))
    result = None
    for _ in range(50):
        result = vad.push(tone)
    for _ in range(50):
        result = vad.push(quiet)
        if result is not None:
            break
    assert result, "VAD never closed the turn"
    assert len(result) > 0


def test_vad_discards_a_sub_minimum_click():
    vad = VoiceActivityDetector()
    tone, quiet = _tone(0.02), struct.pack("<160h", *([40] * 160))
    out = None
    for i in range(80):
        out = vad.push(tone if i < 1 else quiet)
        if out is not None:
            break
    assert out is None


def test_vad_noise_floor_tracks_a_hissy_line():
    vad = VoiceActivityDetector()
    hiss = struct.pack("<160h", *([200] * 160))
    for _ in range(200):
        vad.push(hiss)
    assert abs(vad.noise_floor - 200) < 15, vad.noise_floor
    assert vad.onset_threshold() > 400
