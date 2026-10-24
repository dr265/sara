# actions/game_forge.py
"""Game Forge action - build a real playable game from voice mode.

Calls the real backend pipeline (POST /api/gameforge/games, GET /jobs/{id},
GET /games) - the same routes the Hub workspace uses. The engine generates
real game code with the LLM, repairs it, builds a browser preview and stores
it in the Game Forge library. This action submits the job, polls honestly
and reports where to play the finished game.
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core import hub_agents_client as hub

NAME = "Game Forge"
DEFAULT_WAIT = 240
POLL_INTERVAL = 5.0
TERMINAL_STATUSES = {"done", "failed"}


def _find_game(game_id: str) -> Dict[str, Any]:
    try:
        resp = hub.get("/api/gameforge/games", timeout=15)
        if resp.status_code == 200:
            for game in resp.json():
                if str(game.get("id")) == game_id:
                    return game
    except Exception:
        pass
    return {}


def _snapshot_status(snap: Dict[str, Any]) -> str:
    return str(snap.get("status") or "").strip().lower()


def forge_game(parameters: dict) -> str:
    p = parameters or {}
    prompt = str(p.get("prompt") or p.get("idea") or p.get("description") or "").strip()
    if len(prompt) < 5:
        return ("Describe the game you want - for example: 'a space shooter where you "
                "collect coins and dodge asteroids'.")
    prompt = prompt[:1500]

    body: Dict[str, Any] = {"prompt": prompt}
    resolution = str(p.get("resolution") or "").strip()
    if resolution:
        body["resolution"] = resolution[:16]
    quality = str(p.get("quality") or "").strip()
    if quality:
        body["quality"] = quality[:32]

    try:
        resp = hub.post("/api/gameforge/games", body, timeout=60)
    except requests.exceptions.RequestException as exc:
        return hub.unreachable(NAME, exc)
    if resp.status_code >= 400:
        return hub.http_failure(NAME, resp)
    try:
        started = resp.json()
    except Exception:
        return "Game Forge returned an unreadable response - the job was not confirmed."

    job_id = str(started.get("jobId") or "").strip()
    game_id = str(started.get("gameId") or (started.get("game") or {}).get("id") or "").strip()
    if not job_id:
        return f"Game Forge accepted the request but returned no job id ({hub.brief(started, 200)})."

    try:
        wait = int(p.get("wait_seconds") or DEFAULT_WAIT)
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT
    wait = max(30, min(600, wait))

    def fetch():
        try:
            r = hub.get(f"/api/gameforge/jobs/{job_id}", timeout=15)
        except requests.exceptions.RequestException as exc:
            return False, {}, hub.unreachable(NAME, exc)
        if r.status_code >= 400:
            return False, {}, hub.http_failure(NAME, r)
        try:
            return True, r.json(), ""
        except Exception:
            return False, {}, "Game Forge returned an unreadable job status."

    snap, finished, note = hub.poll(
        fetch, lambda s: _snapshot_status(s) in TERMINAL_STATUSES, wait, POLL_INTERVAL)

    status = _snapshot_status(snap)
    message = str(snap.get("message") or "").strip()
    if finished and status == "done":
        game = _find_game(game_id or str(snap.get("gameId") or ""))
        title = str(game.get("title") or prompt[:60])
        playable = game.get("playable")
        parts = [f'Game built: "{title}"']
        if playable is False:
            parts.append("build finished but the browser preview is not playable yet")
        parts.append("open /hub/game-forge to play it")
        if game_id:
            parts.append(f"download: {hub.BASE_URL.rstrip('/')}/api/gameforge/games/{game_id}/download")
        return ". ".join(parts) + "."
    if finished and status == "failed":
        err = str(snap.get("error") or message or "unknown error")
        return f"Game Forge failed to build that game: {hub.brief(err, 300)}"

    progress = snap.get("progress")
    stage = str(snap.get("stage") or "working")
    if note == "still running":
        bits = f"stage: {stage}" + (f", {progress}%" if isinstance(progress, (int, float)) else "")
        return (f"Game Forge is still building after {wait} seconds ({bits}). "
                f"Job id {job_id[:8]} - it keeps running on the backend; "
                f"watch it on /hub/game-forge.")
    return f"Game Forge stopped with a problem: {note} Job id {job_id[:8]}."


TOOL = {
    "name": "game_forge",
    "description": (
        "Builds a real playable browser game from a text prompt using SARA's Game Forge "
        "(LLM codegen + repair + browser build). Use when the user asks to create/make/build a game. "
        "The build takes minutes; I submit it, wait as long as reasonable and report honestly. "
        "To OPEN the Game Forge page (not build), use open_agency_workspace."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {
                "type": "STRING",
                "description": "What the game should be, e.g. 'a snake game with neon graphics and power-ups'.",
            },
            "resolution": {
                "type": "STRING",
                "description": "Optional resolution, e.g. 1280x720 (default).",
            },
            "quality": {
                "type": "STRING",
                "description": "Optional quality preset, e.g. 'Polished' (default) or 'Fast'.",
            },
            "wait_seconds": {
                "type": "NUMBER",
                "description": "How long to wait for the build before reporting progress (30-600, default 240).",
            },
        },
        "required": ["prompt"],
    },
    "handler": forge_game,
}
