"""
AI Music Studio — SARA Nexus Hub agent.

Natural-language music generation:
  1. The shared SARA LLM brain (LLM_API_KEY/LLM_BASE_URL/LLM_MODEL, OpenAI-compatible)
     enriches the user's prompt into a structured musical brief (genre, mood, tempo,
     instruments, structure, duration).
  2. Audio is produced by the first available engine:
       - ModelsLab text-to-music  (MODELSLAB_API_KEY in backend/.env)  — cloud
       - Built-in procedural synthesizer (numpy)                        — always available
  3. Every generation is stored in a persistent JSON library with the rendered audio
     served from /api/music/audio/{filename}.

Adapted from the AI Music Studio pipeline (prompt parsing for durations/variations,
audio URL discovery, safe downloads, size caps) into a hub-compatible FastAPI router.
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import os
import re
import secrets
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from openai import OpenAI
from pydantic import BaseModel, Field

LOGGER = logging.getLogger("music_studio")

router = APIRouter(prefix="/api/music", tags=["music-studio"])

ROOT = Path(__file__).resolve().parents[2]           # backend/
AUDIO_DIR = ROOT / "generated_music"
LIBRARY_FILE = ROOT / "data" / "music_library.json"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_MODELSLAB_ENDPOINT = "https://modelslab.com/api/v6/music/text-to-music"
REQUEST_TIMEOUT = (10, 300)
DOWNLOAD_TIMEOUT = (10, 180)
MAX_AUDIO_BYTES = 250 * 1024 * 1024
MAX_LIBRARY_ITEMS = 100

# ---------------------------------------------------------------------------
# Library persistence
# ---------------------------------------------------------------------------

def _load_library() -> List[Dict[str, Any]]:
    try:
        return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt/truncated library (e.g. hard kill mid-write): preserve the
        # damaged file for recovery instead of silently overwriting it.
        if LIBRARY_FILE.exists() and LIBRARY_FILE.stat().st_size > 2:
            try:
                LIBRARY_FILE.replace(LIBRARY_FILE.with_suffix(".json.bak"))
            except Exception:
                pass
        return []


def _save_library(items: List[Dict[str, Any]]) -> None:
    # Atomic write: a kill mid-write can never truncate the real file.
    tmp = LIBRARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(items[:MAX_LIBRARY_ITEMS], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, LIBRARY_FILE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._")
    return value[:80] or "generated_music"


# ---------------------------------------------------------------------------
# Prompt parsing (duration + variations), ported from the studio app
# ---------------------------------------------------------------------------

def get_duration_seconds(text: str) -> Optional[int]:
    for pattern, multiplier in (
        (r"\b(\d{1,4})\s*(?:seconds?|secs?|s)\b", 1),
        (r"\b(\d{1,4})\s*(?:minutes?|mins?|m)\b", 60),
    ):
        match = re.search(pattern, text.lower())
        if match:
            value = int(match.group(1)) * multiplier
            return max(5, min(value, 600))
    return None


def get_variation_count(text: str) -> int:
    match = re.search(
        r"\b(?:make|create|generate|give me)\s+(\d{1,2})\s+"
        r"(?:variations?|versions?|options?|tracks?)\b",
        text.lower(),
    )
    return max(1, min(int(match.group(1)), 8)) if match else 1


# ---------------------------------------------------------------------------
# LLM prompt enhancement — shared SARA brain
# ---------------------------------------------------------------------------

def _llm_config() -> Tuple[str, str, str]:
    key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    base = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    model = (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o").strip()
    return key, base, model


class ProviderError(Exception):
    pass


ENHANCE_SCHEMA = {
    "enhanced_prompt": "string",
    "genre": "string",
    "mood": "string",
    "energy": "string",
    "tempo_bpm": "integer or null",
    "key": "string or null",
    "instruments": "array of strings",
    "rhythm": "string",
    "harmony": "string",
    "melody": "string",
    "production_style": "string",
    "textures": "string",
    "vocals": "string",
    "structure": "string",
    "duration_seconds": "integer or null",
    "intended_use": "string",
}


def _salvage_truncated_json(text: str) -> Optional[Dict[str, Any]]:
    """Recover a usable object from a JSON response the model cut off mid-write.

    The 16-field brief is long enough that the provider regularly returns
    ``finish_reason="stop"`` on a *truncated* document - an unclosed string and
    an unclosed object, e.g. ``..."textures": "Layered string``. The old code
    called that "not valid JSON" and surfaced a hard error to the user even
    though the first dozen fields parsed perfectly.

    So: walk the text tracking string/escape state, remember the offset of the
    last *complete* top-level ``"key": value`` pair, and re-close from there.
    Everything up to that pair survives; only the cut-off tail is dropped.
    """
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]

    in_string = False
    escaped = False
    # Nesting stack of open brackets. Tracking *both* {} and [] matters: the
    # brief's `instruments` field is a string array, so a comma inside it sits
    # at the same nesting level as a real field separator. A brace-only depth
    # counter mistakes that comma for the end of a field and cuts mid-array.
    stack: List[str] = []
    # Offset (relative to `body`) just past the end of the last completed
    # top-level "key": <value> pair, or None if we never saw one.
    last_complete: Optional[int] = None
    # Offset just past the end of the last completed *value* of any kind at
    # depth 1, including a string - that is what lets a response truncated
    # inside its very first field still yield an object.
    last_value_end: Optional[int] = None
    expecting_value = False

    for i, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            if in_string:
                in_string = False
                if expecting_value and len(stack) == 1:
                    last_value_end = i + 1
                    expecting_value = False
            else:
                in_string = True
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                return None  # root object closed - already balanced
            if len(stack) == 1 and expecting_value:
                last_value_end = i + 1
                expecting_value = False
        elif ch == ":" and len(stack) == 1:
            expecting_value = True
        elif ch == "," and len(stack) == 1:
            expecting_value = False
            last_complete = i

    # Prefer the last fully-separated field; fall back to a single trailing
    # value when the model was cut off inside the very first field.
    if last_complete is not None:
        candidate = body[:last_complete].rstrip().rstrip(",")
    elif last_value_end is not None:
        candidate = body[:last_value_end].rstrip().rstrip(",")
    elif in_string and len(stack) == 1 and expecting_value:
        # Cut off inside the very first field's string: no value ever closed,
        # so close the quote and the root object ourselves.
        candidate = body.rstrip() + '"'
    else:
        return None
    if not candidate.endswith("}"):
        candidate += "}"
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value else None


def extract_json(text: str) -> Dict[str, Any]:
    """Parse the model's JSON brief, tolerating fences, prose and truncation."""
    text = (text or "").strip()
    if not text:
        raise ProviderError("The prompt-enhancement response was empty.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            value = _salvage_truncated_json(text)
    else:
        # No closing brace at all: the response was cut off, not malformed.
        value = _salvage_truncated_json(text)

    if not isinstance(value, dict):
        raise ProviderError("The prompt-enhancement response was not valid JSON.")
    return value


def enhance_prompt(user_prompt: str, genre_hint: str, mood_hint: str,
                   duration_override: Optional[int], vocal: bool) -> Dict[str, Any]:
    key, base, model = _llm_config()
    if not key:
        # Honest fallback: structured brief without an LLM.
        return {
            "enhanced_prompt": user_prompt,
            "genre": genre_hint,
            "mood": mood_hint,
            "energy": "medium",
            "tempo_bpm": None,
            "key": None,
            "instruments": [],
            "rhythm": "",
            "harmony": "",
            "melody": "",
            "production_style": "",
            "textures": "",
            "vocals": "none (instrumental)" if not vocal else "lead vocal",
            "structure": "",
            "duration_seconds": duration_override,
            "intended_use": "",
        }

    system = (
        "You are a professional music producer and prompt engineer. "
        "Preserve the user's intent. Fill in missing musical details intelligently. "
        "Return JSON only, matching this schema:\n"
        f"{json.dumps(ENHANCE_SCHEMA)}\n"
        "The enhanced_prompt must be one rich, concise prompt suitable for a "
        "text-to-music generation API. Do not include API parameters, credentials, "
        "URLs, or commentary. If duration is absent, return null for duration_seconds."
    )
    user = (
        f"User request:\n{user_prompt}\n\n"
        f"UI selections — genre: {genre_hint}; mood: {mood_hint}; "
        f"vocals: {'lead vocal' if vocal else 'instrumental only'}; "
        f"slider duration: {duration_override or 'unspecified'} seconds."
    )

    from app.core.llm_router import get_llm_client as _failover_client
    client = _failover_client()

    def _fallback(reason: str) -> Dict[str, Any]:
        LOGGER.warning("Prompt enhancement degraded (%s); using raw prompt.", reason)
        return {
            "enhanced_prompt": user_prompt,
            "genre": genre_hint,
            "mood": mood_hint,
            "energy": "medium",
            "tempo_bpm": None,
            "key": None,
            "instruments": [],
            "rhythm": "",
            "harmony": "",
            "melody": "",
            "production_style": "",
            "textures": "",
            "vocals": "none (instrumental)" if not vocal else "lead vocal",
            "structure": "",
            "duration_seconds": duration_override,
            "intended_use": "",
        }

    result: Optional[Dict[str, Any]] = None
    # Two attempts: the first at the configured temperature, the second at 0
    # with the schema restated. A truncated write usually means the model was
    # still mid-document; a colder, more explicit retry usually completes.
    for attempt, (temp, sys_prompt) in enumerate((
        (0.7, system),
        (0.0, system + "\nOutput ONLY a single JSON object. Start with { and end with }."),
    )):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=temp,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user},
                ],
                timeout=60,
            )
            content = response.choices[0].message.content or ""
            result = extract_json(content)
            break
        except ProviderError as exc:
            LOGGER.warning("Prompt enhancement attempt %d unparseable: %s", attempt + 1, exc)
            continue
        except Exception as exc:  # provider hiccup should not kill the run
            return _fallback(f"{type(exc).__name__}: {exc}")

    if not result:
        # Enhancement is a nicety, not the product. Never fail the generation
        # because the brain was terse - the synthesizer can run off the raw
        # prompt, and the user still gets real audio back.
        return _fallback("no usable JSON from the provider")

    result["enhanced_prompt"] = str(result.get("enhanced_prompt") or user_prompt).strip()
    # UI selections always win over LLM guesses.
    if genre_hint:
        result["genre"] = genre_hint
    if mood_hint:
        result["mood"] = mood_hint
    if duration_override is not None:
        result["duration_seconds"] = duration_override
    if vocal:
        result["vocals"] = "lead vocal"
    return result


# ---------------------------------------------------------------------------
# Engine 1 — ModelsLab text-to-music (cloud)
# ---------------------------------------------------------------------------

def _find_audio_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        if value.lower().startswith(("http://", "https://")):
            return value
    if isinstance(value, list):
        for item in value:
            found = _find_audio_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("audio_url", "audio", "url", "output", "output_url",
                    "music_url", "link"):
            if key in value:
                found = _find_audio_url(value[key])
                if found:
                    return found
        for item in value.values():
            found = _find_audio_url(item)
            if found:
                return found
    return None


def models_lab_generate(api_key: str, endpoint: str, prompt: str,
                        duration_seconds: Optional[int],
                        seed: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    payload: Dict[str, Any] = {"key": api_key, "prompt": prompt}
    if duration_seconds is not None:
        payload["duration"] = duration_seconds
    if seed is not None:
        payload["seed"] = seed

    try:
        response = requests.post(
            endpoint, json=payload, timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        raise ProviderError(f"ModelsLab request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"ModelsLab returned non-JSON data with HTTP {response.status_code}."
        ) from exc

    if response.status_code >= 400:
        message = body.get("message", "request failed") if isinstance(body, dict) else body
        raise ProviderError(f"ModelsLab returned HTTP {response.status_code}: {message}")

    if isinstance(body, dict):
        status = str(body.get("status", "")).lower()
        if status in {"error", "failed", "failure"}:
            raise ProviderError(str(body.get("message") or body))

    audio_url = _find_audio_url(body)
    if not audio_url:
        raise ProviderError("ModelsLab response did not contain an audio URL.")
    return audio_url, body if isinstance(body, dict) else {}


def download_audio(url: str, label: str) -> Path:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    extension = Path(url.split("?", 1)[0]).suffix.lower()
    if extension not in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        extension = ".mp3"
    destination = AUDIO_DIR / f"{safe_filename(label)}_{secrets.token_hex(8)}{extension}"

    try:
        with requests.get(
            url, stream=True, timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": "SARA-Music-Studio/1.0"},
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise ProviderError("The audio URL returned a non-audio response.")
            total = 0
            with destination.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise ProviderError("The downloaded audio exceeded the size limit.")
                    file.write(chunk)
    except requests.RequestException as exc:
        destination.unlink(missing_ok=True)
        raise ProviderError(f"Audio download failed: {exc}") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    if not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise ProviderError("The downloaded audio file was empty.")
    return destination


# ---------------------------------------------------------------------------
# Engine 2 — built-in procedural synthesizer (numpy, always available)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 44100

_SCALES: Dict[str, List[int]] = {
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "major": [0, 2, 4, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "harmonic minor": [0, 2, 3, 5, 7, 8, 11],
    "pentatonic": [0, 3, 5, 7, 10],
}

_NOTE_TO_SEMITONE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def _note_freq(root_semitone: int, scale_degree: int, octave: int = 0) -> float:
    semis = root_semitone + scale_degree + 12 * octave
    return 440.0 * (2.0 ** ((semis - 9) / 12.0))  # A4 = 440


def _env(length: int, attack: float = 0.01, release: float = 0.4) -> np.ndarray:
    n = max(length, 1)
    a = max(int(attack * n), 1)
    r = max(int(release * n), 1)
    env = np.ones(n)
    env[:a] = np.linspace(0, 1, a)
    env[-r:] *= np.linspace(1, 0, r)
    return env


def _synth_tone(freq: float, seconds: float, waveform: str = "saw",
                detune: float = 0.0) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    if waveform == "sine":
        wave_form = np.sin(2 * np.pi * freq * t)
    elif waveform == "square":
        wave_form = np.sign(np.sin(2 * np.pi * freq * t)) * 0.7
    elif waveform == "triangle":
        wave_form = 2 * np.abs(2 * ((freq * t) % 1) - 1) - 1
    else:  # saw
        wave_form = 2 * ((freq * t) % 1) - 1
    if detune > 0:
        wave_form = 0.6 * wave_form + 0.4 * np.sin(2 * np.pi * freq * (1 + detune) * t)
    return wave_form


def _drums(total_samples: int, bpm: float, energy: float) -> np.ndarray:
    beat = 60.0 / max(bpm, 40)
    kick_len = int(0.12 * SAMPLE_RATE)
    hat_len = int(0.04 * SAMPLE_RATE)
    kick_t = np.arange(kick_len) / SAMPLE_RATE
    kick = np.sin(2 * np.pi * (110 * np.exp(-kick_t * 28)) * kick_t) * np.exp(-kick_t * 24)
    hat_t = np.arange(hat_len) / SAMPLE_RATE
    rng = np.random.default_rng(7)
    hat = rng.uniform(-1, 1, hat_len) * np.exp(-hat_t * 90) * 0.3

    mix = np.zeros(total_samples)
    step = int(beat / 2 * SAMPLE_RATE)  # eighth notes
    for i in range(0, total_samples // step):
        at = i * step
        if i % 2 == 0:
            mix[at:at + len(kick)] += kick * (0.9 * energy)
        else:
            end = min(at + len(hat), total_samples)
            mix[at:end] += hat[: end - at] * (0.5 + 0.5 * energy)
    return mix


def procedural_render(brief: Dict[str, Any], duration: int, seed: int) -> Path:
    """Render a multi-layer arrangement (drums, bass, chords, lead) to a WAV file."""
    rng = np.random.default_rng(seed)

    genre = str(brief.get("genre") or "").lower()
    mood = str(brief.get("mood") or "").lower()
    instruments = " ".join(brief.get("instruments") or []).lower()
    structure = str(brief.get("structure") or "").lower()

    bpm = float(brief.get("tempo_bpm") or 0)
    if not (60 <= bpm <= 190):
        bpm = 128.0
        if any(w in genre + instruments for w in ("ambient", "cinematic", "orchestr")):
            bpm = 84.0
        if any(w in genre + instruments for w in ("hip hop", "trap", "lofi", "lo-fi")):
            bpm = 90.0
        if "drill" in genre or "dnb" in genre:
            bpm = 150.0

    if any(w in mood + genre for w in ("dark", "melancholic", "sad", "mysterious")):
        scale_name, root = "minor", 9      # A minor
    elif any(w in mood + genre for w in ("uplifting", "happy", "bright", "pop")):
        scale_name, root = "major", 0      # C major
    elif "jazz" in genre:
        scale_name, root = "dorian", 2     # D dorian
    else:
        scale_name, root = "minor", 0

    scale = _SCALES[scale_name]
    energy = 0.9 if any(w in mood + genre for w in ("epic", "aggressive", "energetic")) else 0.6

    beat = 60.0 / bpm
    total = int(duration * SAMPLE_RATE)
    mix = np.zeros(total)

    # --- drum layer ---
    if not any(w in genre for w in ("classical", "orchestral", "ambient")) or "drum" in instruments:
        mix += _drums(total, bpm, energy)[:total]

    # --- bass line (root-fifth pattern per bar) ---
    bass_wave = "square" if any(w in genre for w in ("electronic", "synth", "retro")) else "saw"
    bar_samples = int(beat * 4 * SAMPLE_RATE)
    pos = 0
    bar_index = 0
    while pos < total:
        degree = scale[0] if bar_index % 4 < 2 else scale[3 % len(scale)]
        freq = _note_freq(root - 24, degree)
        note_len = min(bar_samples, total - pos)
        tone = _synth_tone(freq, note_len / SAMPLE_RATE, bass_wave)
        tone *= _env(note_len, 0.02, 0.2) * 0.32
        mix[pos:pos + len(tone)] += tone
        pos += bar_samples
        bar_index += 1

    # --- chord pad (triads, one per bar) ---
    pad_pos = 0
    pad_bar = 0
    progression = [0, 5 % len(scale), 3 % len(scale), 4 % len(scale)]
    while pad_pos < total:
        chunk = min(bar_samples, total - pad_pos)
        chord = np.zeros(chunk)
        for offset in (0, 2, 4):
            degree = scale[(progression[pad_bar % len(progression)] + offset) % len(scale)]
            octave = 1 if (progression[pad_bar % len(progression)] + offset) >= len(scale) else 0
            freq = _note_freq(root - 12, degree, octave)
            chord += _synth_tone(freq, chunk / SAMPLE_RATE, "triangle", detune=0.004)
        chord *= _env(chunk, 0.15, 0.35) * 0.12
        mix[pad_pos:pad_pos + chunk] += chord
        pad_pos += bar_samples
        pad_bar += 1

    # --- lead / arp melody (eighth-note arpeggio with rests) ---
    lead_wave = "square" if "synth" in genre + instruments or "retro" in genre else "saw"
    step = int(beat / 2 * SAMPLE_RATE)
    degrees = list(range(len(scale)))
    for i in range(total // step):
        if rng.random() < 0.3:
            continue  # musical rest
        at = i * step
        chunk = min(step, total - at)
        degree = scale[rng.choice(degrees)]
        octave = 1 if rng.random() < 0.4 else 0
        freq = _note_freq(root, degree, octave)
        tone = _synth_tone(freq, chunk / SAMPLE_RATE, lead_wave)
        tone *= _env(chunk, 0.01, 0.5) * (0.14 + 0.1 * energy)
        mix[at:at + chunk] += tone

    # --- simple radio-style filter + mastering ---
    if len(mix) > SAMPLE_RATE // 8:
        kernel = np.ones(SAMPLE_RATE // 300) / (SAMPLE_RATE // 300)
        mix = np.convolve(mix, kernel, mode="same")
    peak = float(np.max(np.abs(mix)) or 1.0)
    mix = np.tanh(mix / peak * 1.15) * 0.92
    pcm = (mix * 32767).astype("<i2")

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out = AUDIO_DIR / f"music_{secrets.token_hex(8)}.wav"
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return out


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    genre: str = "Cinematic"
    mood: str = "Epic"
    duration: int = Field(default=30, ge=15, le=600)
    vocal: bool = False
    variations: int = Field(default=1, ge=1, le=4)


class TrackOut(BaseModel):
    id: str
    url: str
    prompt: str
    engine: str
    metadata: Dict[str, Any]
    createdAt: str


def _to_track(item: Dict[str, Any]) -> TrackOut:
    return TrackOut(
        id=item["id"],
        url=f"/api/music/audio/{item['filename']}",
        prompt=item.get("original_prompt", ""),
        engine=item.get("engine", "procedural"),
        metadata=item.get("metadata", {}),
        createdAt=item.get("timestamp", _utc_now()),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> Dict[str, Any]:
    key, base, model = _llm_config()
    return {
        "status": "ok",
        "llm": {"configured": bool(key), "model": model if key else None},
        "engines": {
            "modelslab": bool((os.getenv("MODELSLAB_API_KEY") or "").strip()),
            "procedural": True,
        },
        "library_items": len(_load_library()),
    }


@router.get("/history")
def history(limit: int = 50) -> List[TrackOut]:
    return [_to_track(i) for i in _load_library()[: max(1, min(limit, MAX_LIBRARY_ITEMS))]]


@router.post("/generate")
def generate(req: GenerateRequest) -> Dict[str, Any]:
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="Prompt is empty.")

    detected_duration = get_duration_seconds(prompt)
    variations = max(req.variations, get_variation_count(prompt))
    duration = min(detected_duration or req.duration, 600)

    try:
        brief = enhance_prompt(prompt, req.genre, req.mood, duration, req.vocal)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    created: List[Dict[str, Any]] = []
    engine_used = "procedural"
    engine_warning: Optional[str] = None

    modelslab_key = (os.getenv("MODELSLAB_API_KEY") or "").strip()
    endpoint = (os.getenv("MODELSLAB_MUSIC_ENDPOINT") or DEFAULT_MODELSLAB_ENDPOINT).strip()

    library = _load_library()
    for index in range(variations):
        generation_id = secrets.token_urlsafe(12)
        item: Dict[str, Any] = {
            "id": generation_id,
            "original_prompt": prompt,
            "enhanced_prompt": brief.get("enhanced_prompt", prompt),
            "timestamp": _utc_now(),
            "metadata": {
                "genre": brief.get("genre"),
                "mood": brief.get("mood"),
                "energy": brief.get("energy"),
                "tempo_bpm": brief.get("tempo_bpm"),
                "key": brief.get("key"),
                "instruments": brief.get("instruments"),
                "structure": brief.get("structure"),
                "duration_seconds": brief.get("duration_seconds") or duration,
                "vocals": brief.get("vocals"),
                "variation": index + 1,
            },
            "filename": "",
            "status": "generating",
            "engine": "modelslab" if modelslab_key else "procedural",
        }

        try:
            if modelslab_key:
                engine_used = "modelslab"
                audio_url, raw = models_lab_generate(
                    api_key=modelslab_key,
                    endpoint=endpoint,
                    prompt=item["enhanced_prompt"],
                    duration_seconds=duration,
                    seed=None if variations == 1 else secrets.randbelow(2**31),
                )
                audio_path = download_audio(audio_url, f"music_{generation_id}")
            else:
                engine_used = "procedural"
                audio_path = procedural_render(brief, duration, secrets.randbelow(2**31))
        except ProviderError as exc:
            if engine_used == "modelslab":
                # Cloud engine failed — fall back to the built-in synthesizer so
                # the user still gets real audio, with an honest warning.
                engine_warning = f"ModelsLab failed ({exc}); rendered with the built-in synthesizer instead."
                engine_used = "procedural"
                audio_path = procedural_render(brief, duration, secrets.randbelow(2**31))
            else:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        item["filename"] = audio_path.name
        item["engine"] = engine_used
        item["metadata"]["provider_endpoint"] = endpoint if engine_used == "modelslab" else None
        item["metadata"]["file_bytes"] = audio_path.stat().st_size
        item["status"] = "completed"
        library.insert(0, item)
        created.append(item)

    _save_library(library)
    return {
        "tracks": [_to_track(i).model_dump() for i in created],
        "brief": {k: v for k, v in brief.items() if k != "enhanced_prompt"},
        "enhanced_prompt": brief.get("enhanced_prompt", prompt),
        "engine": engine_used,
        "warning": engine_warning,
    }


@router.get("/audio/{filename}")
def audio(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = AUDIO_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found.")
    mime = mimetypes.guess_type(path.name)[0] or "audio/wav"
    return FileResponse(path, media_type=mime, filename=path.name)


@router.delete("/history/{track_id}")
def delete_track(track_id: str) -> Dict[str, Any]:
    library = _load_library()
    keep: List[Dict[str, Any]] = []
    removed: Optional[Dict[str, Any]] = None
    for item in library:
        if item.get("id") == track_id and removed is None:
            removed = item
            continue
        keep.append(item)
    if removed is None:
        raise HTTPException(status_code=404, detail="Track not found.")
    if removed.get("filename"):
        try:
            (AUDIO_DIR / removed["filename"]).unlink(missing_ok=True)
        except Exception:
            pass
    _save_library(keep)
    return {"deleted": track_id, "remaining": len(keep)}


@router.get("/status")
def status() -> Dict[str, Any]:
    """Alias used by hub dashboards — same payload as /health."""
    return health()
