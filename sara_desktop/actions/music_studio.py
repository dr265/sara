# actions/music_studio.py
"""Music Studio action — create REAL music from Jarvis voice mode.

This is the Normal-mode (voice) counterpart of the Agentic music path:
it calls the same backend endpoint the Hub UI uses
(POST /api/music/generate). The render is real (the built-in synthesizer,
or ModelsLab when a key is configured), the finished track is saved under
the user's Music folder and played back.

No mocks: when the backend cannot render, the action says exactly why.
"""
from __future__ import annotations

import os
import webbrowser
from pathlib import Path

import requests

try:
    from core.backend_url import resolve_backend_url
    BACKEND_URL = resolve_backend_url()
except Exception:  # pragma: no cover - env fallback mirrors agency_opener
    BACKEND_URL = os.environ.get("SARA_BACKEND_URL", "http://127.0.0.1:8000")

MIN_DURATION = 15
MAX_DURATION = 600
DEFAULT_DURATION = 30
MAX_PROMPT = 4000
GENERATE_TIMEOUT = 300   # modelslab can be slow; the built-in synth is quick
DOWNLOAD_TIMEOUT = 120


def _build_payload(parameters: dict) -> dict:
    """Validate voice parameters and build the /api/music/generate body."""
    p = parameters or {}
    prompt = str(
        p.get("prompt") or p.get("query") or p.get("description") or ""
    ).strip()
    if len(prompt) < 3:
        raise ValueError(
            "I need a musical brief - describe the track "
            "(style, mood, instruments), e.g. 'calm ambient piano with rain'."
        )
    payload = {"prompt": prompt[:MAX_PROMPT]}
    genre = str(p.get("genre") or "").strip()
    if genre:
        payload["genre"] = genre[:80]
    mood = str(p.get("mood") or "").strip()
    if mood:
        payload["mood"] = mood[:80]
    try:
        duration = int(p.get("duration") or DEFAULT_DURATION)
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION
    payload["duration"] = max(MIN_DURATION, min(MAX_DURATION, duration))
    return payload


def _save_track(url: str, filename: str) -> Path:
    """Download the rendered track into the user's Music folder."""
    target_dir = Path.home() / "Music" / "SARA Music"
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / filename
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
    return out


def _play(path: Path | None, url: str) -> str:
    """Play the finished track; returns how it was played (honest)."""
    if path is not None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 - user-requested playback
                return "playing in your default player"
        except Exception:
            pass
    try:
        webbrowser.open(url)
        return "playing in your browser"
    except Exception:
        if path is not None:
            return "saved - open the file to listen"
        return "ready to play in your browser"


def generate_music(parameters: dict, player=None) -> str:
    try:
        payload = _build_payload(parameters)
    except ValueError as exc:
        return str(exc)

    endpoint = f"{BACKEND_URL.rstrip('/')}/api/music/generate"
    try:
        resp = requests.post(endpoint, json=payload, timeout=GENERATE_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        return (
            f"Music Studio is unreachable ({exc.__class__.__name__}). "
            "Start the backend, then try again - or open /hub/music-studio."
        )

    if resp.status_code != 200:
        detail = ""
        try:
            detail = str(resp.json().get("detail") or "")
        except Exception:
            detail = (resp.text or "")[:200]
        return (
            f"Music Studio could not render that "
            f"(HTTP {resp.status_code}{': ' + detail if detail else ''})."
        )

    try:
        data = resp.json()
    except Exception:
        return "Music Studio returned an unreadable response - nothing was generated."

    tracks = data.get("tracks") or []
    if not tracks:
        return "Music Studio returned no track - nothing was generated."

    track = tracks[0]
    url = str(track.get("url") or "")
    if not url:
        return "Music Studio returned a track without an audio file - nothing to play."
    full_url = url if url.startswith("http") else f"{BACKEND_URL.rstrip('/')}{url}"
    engine = data.get("engine") or track.get("engine") or "unknown"

    saved: Path | None = None
    try:
        saved = _save_track(full_url, Path(url).name or "sara_track.wav")
    except Exception:
        saved = None

    playback = _play(saved, full_url)
    brief = payload["prompt"]
    brief_short = brief if len(brief) <= 70 else brief[:67] + "..."
    parts = [
        f'Generated "{brief_short}" - {engine} engine, '
        f"{payload['duration']}s - {playback}."
    ]
    if saved is not None:
        parts.append(f"Saved to {saved}.")
    warning = str(data.get("warning") or "").strip()
    if warning:
        parts.append(f"Note: {warning}")
    return " ".join(parts)


TOOL = {
    "name": "music_studio",
    "description": (
        "Creates ORIGINAL music with SARA's Music Studio - a real render, saved as an audio file. "
        "Use when the user asks to make, generate, compose or create music, a song, a track, a beat, "
        "an instrumental or background music (optionally with genre, mood, duration in seconds). "
        "Do NOT use for playing existing songs (use the spotify/youtube tools) and do NOT confuse "
        "with Sara Muse (a goals/notifications workspace). "
        "To open the Music Studio page itself (not create music), use open_agency_workspace instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {
                "type": "STRING",
                "description": "Musical brief: style, mood, instruments, e.g. 'calm ambient piano with soft rain'.",
            },
            "genre": {
                "type": "STRING",
                "description": "Optional genre hint, e.g. Lo-fi, Cinematic, Synthwave.",
            },
            "mood": {
                "type": "STRING",
                "description": "Optional mood hint, e.g. Calm, Epic, Happy.",
            },
            "duration": {
                "type": "NUMBER",
                "description": "Optional length in seconds (15-600). Default 30.",
            },
        },
        "required": ["prompt"],
    },
    "handler": generate_music,
}
