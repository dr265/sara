"""
GameForge — Autonomous AI Game Creation Agent (SARA Nexus Hub backend).

One prompt -> one real, tested, packaged 2D game. Pipeline:
    plan (LLM JSON spec) -> generate (LLM main.py) -> validate
    -> auto-repair loop (max MAX_REPAIRS) -> pygbag browser build -> zip archive.

Live wiring (this module is mounted in app/main.py):
    GET  /api/gameforge/health        engine status (llm, pygame, pygbag, output dir)
    GET  /api/gameforge/games         game library (persisted across restarts)
    POST /api/gameforge/games         { prompt, resolution, quality } -> starts a job
    GET  /api/gameforge/jobs/{id}     job status (stage, progress, message, error)
    GET  /api/gameforge/games/{id}/download    -> zip (Stored/Dist importable build)
    GET  /api/gameforge/games/{id}/preview     -> serves the pygbag browser build
                                                    (iframe the /preview/ URL)
    POST /api/gameforge/games/{id}/restart     -> re-run build for an existing game

Design notes:
    * The LLM brain is the SAME shared config every other SARA agent uses:
      LLM_API_KEY / LLM_BASE_URL / LLM_MODEL (OpenAI-compatible), with
      DEEPSEEK_API_KEY / OPENAI_API_KEY as automatic fallbacks. Nothing hardcoded.
    * Games live in backend/gameforge_projects/<id>/ — gitignored, restart-safe.
    * Validation runs the generated game in a SDL dummy video/audio driver with
      GAME_AGENT_TEST=1, so no window pops up on the server. Timeout is enforced.
    * The repair loop feeds the real compile/run traceback back to the model,
      which is what makes the "autonomous" part real rather than cosmetic.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import pygame  # noqa: F401  (validation environment check)
    PYGAME_OK = True
    PYGAME_VERSION = pygame.version.ver
except Exception:  # pragma: no cover
    PYGAME_OK = False
    PYGAME_VERSION = ""

# pygbag availability is probed WITHOUT importing it.
#
# `import pygbag` appends its own `support/` directory to sys.path, and that
# directory contains a vendored `typing_extensions.py`. From that point on
# every import of typing_extensions resolves to pygbag's copy instead of the
# real one, and fastapi / sqlalchemy / openai all fail with:
#   ImportError: cannot import name 'sentinel' from 'typing_extensions'
#   AttributeError: attribute '__default__' of 'typing.ParamSpec' is not writable
#
# It only stayed hidden because app/main.py imports those three before this
# module, so they were already in sys.modules. Any LATER lazy import of openai
# — which is how most agents reach the provider client — would have broken.
#
# pygbag is only ever run as a subprocess (`python -m pygbag --build`), so
# finding its spec on disk is a strictly better probe: same answer, no
# interpreter-wide side effect.
def _module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


PYGBAG_OK = _module_available("pygbag")

ROOT = Path(__file__).resolve().parents[2]
GAMES_DIR = ROOT / "gameforge_projects"
GAMES_DIR.mkdir(parents=True, exist_ok=True)

MAX_REPAIRS = 3
JOB_TIMEOUT = 900  # hard wall for one full generation job (seconds)
MAX_LLM_TOKENS = 16000

router = APIRouter(prefix="/api/gameforge", tags=["gameforge"])


# --------------------------------------------------------------------------- #
# LLM brain — shared SARA config first, then documented fallbacks
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


def _llm_backends() -> List[Dict[str, str]]:
    """Available LLM backends in priority order (shared SARA brain first).

    Every primary backend also gets the provider's verified free-tier fallback
    models so a premium-model 402/403 no longer kills generation.
    """
    backends: List[Dict[str, str]] = []
    if _env("LLM_API_KEY") and _env("LLM_BASE_URL"):
        backends.append({
            "provider": "sara-shared",
            "api_key": _env("LLM_API_KEY"),
            "base_url": _env("LLM_BASE_URL"),
            "model": _env("LLM_MODEL", "cc/claude-sonnet-5"),
        })
        # Verified free-tier fallbacks on the same provider (live-tested).
        for fb in ("qwen/qwen3.5-397b-a17b:free", "qwen/qwen3.6-35b-a3b:free", "cohere/tiny-aya-global"):
            backends.append({
                "provider": f"sara-shared:{fb}",
                "api_key": _env("LLM_API_KEY"),
                "base_url": _env("LLM_BASE_URL"),
                "model": fb,
            })
    if _env("ANYMODEL_API_KEY") and _env("ANYMODEL_BASE_URL"):
        backends.append({
            "provider": "anymodel",
            "api_key": _env("ANYMODEL_API_KEY"),
            "base_url": _env("ANYMODEL_BASE_URL"),
            "model": _env("ANYMODEL_MODEL", "ag/claude-sonnet-4-6"),
        })
    if _env("DEEPSEEK_API_KEY"):
        backends.append({
            "provider": "deepseek",
            "api_key": _env("DEEPSEEK_API_KEY"),
            "base_url": "https://api.deepseek.com",
            "model": _env("DEEPSEEK_MODEL", "deepseek-chat"),
        })
    if _env("OPENAI_API_KEY"):
        backends.append({
            "provider": "openai",
            "api_key": _env("OPENAI_API_KEY"),
            "base_url": "https://api.openai.com/v1",
            "model": _env("OPENAI_MODEL", "gpt-4o"),
        })
    return backends


_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def ai_call(messages: List[Dict[str, str]], max_tokens: int = MAX_LLM_TOKENS) -> str:
    """Call the first available LLM backend with retry/backoff; fall through to the next."""
    if httpx is None:
        raise RuntimeError("httpx is required for the LLM brain")

    errors: List[str] = []
    for backend in _llm_backends():
        last_error = "no attempt"
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{backend['base_url'].rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {backend['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": backend["model"],
                        "messages": messages,
                        "temperature": 0.25,
                        "max_tokens": max_tokens,
                    },
                    timeout=300,
                )
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    if response.status_code in _TRANSIENT_STATUS and attempt < 2:
                        time.sleep(6 * (attempt + 1))
                        continue
                    break
                data = response.json()
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                if not content:
                    last_error = "empty content"
                    if attempt < 2:
                        time.sleep(4)
                        continue
                    break
                return str(content).strip()
            except (getattr(httpx, "TransportError", Exception),
                    getattr(httpx, "TimeoutException", Exception), OSError) as exc:
                # Transient network failure (connection reset, read timeout) — retry.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                break
        errors.append(f"{backend['provider']}: {last_error}")

    raise RuntimeError(
        "All configured LLM backends failed: " + " | ".join(errors or ["none configured"])
    )


def parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```json", "", text, flags=re.I).strip()
    text = re.sub(r"^```", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise RuntimeError("The planning model did not return valid JSON")  # noqa: TRY003
        return json.loads(match.group(0))


def parse_python(text: str) -> str:
    match = re.search(r"```python\s*(.*?)```", text or "", flags=re.S | re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*(.*?)```", text or "", flags=re.S)
    if match:
        return match.group(1).strip()
    return (text or "").strip()


# --------------------------------------------------------------------------- #
# Pipeline stages (LLM)
# --------------------------------------------------------------------------- #
def plan_game(prompt: str, resolution: str, quality: str) -> Dict[str, Any]:
    instructions = """You are a principal game designer and Pygame architect.

Turn the user's idea into a detailed, implementable game specification.
Return JSON only with these keys:

title
genre
core_loop
player
enemies
world
levels
combat
powerups
bosses
particles
audio
menus
controls
ui
win_condition
lose_condition
polish
acceptance_tests

The game must be a complete and fun 2D game, not a toy demonstration.
It must be implementable in one Python file with pygame-ce and the standard
library. All artwork and sound must be generated procedurally. Do not require
internet access or external assets."""
    return parse_json(ai_call([
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(
            {"idea": prompt, "resolution": resolution, "quality": quality}, indent=2)},
    ], max_tokens=7000))


def generate_code(specification: Dict[str, Any]) -> str:
    instructions = """You are a senior Python and pygame-ce game developer.

Generate the complete source code for a polished, playable game.
Return only raw Python source code. Never use Markdown fences.

Strict requirements:
- The result must be a single self-contained main.py file.
- Use pygame-ce and Python standard-library modules only.
- Do not use external images, sounds, fonts, data files, URLs, or packages.
- Draw all graphics procedurally using pygame primitives.
- Generate simple sound effects procedurally or make audio optional.
- Include a professional title menu.
- Include gameplay, enemies, collisions, scoring, health, progression,
  pause, restart, game-over, and victory states where appropriate.
- Include clear keyboard controls.
- Use a stable delta-time game loop.
- Avoid global mutable design where practical.
- Do not use eval, exec, subprocess, requests, os.system, or file deletion.
- When GAME_AGENT_TEST=1, run headlessly and exit automatically after three
  seconds so automated validation can finish.
- CRITICAL test-mode behaviour: immediately after the imports, check
  os.environ.get("GAME_AGENT_TEST") == "1". In that case initialize pygame
  with the dummy drivers the environment already provides, create a small
  display surface, run the main loop for roughly three seconds (a handful of
  frames), then call pygame.quit() and sys.exit(0). Test mode must never open
  the menu or wait for any input.
- In test mode use SDL_VIDEODRIVER=dummy and SDL_AUDIODRIVER=dummy safely.
- The program must start with `import pygame` or `from pygame`.
- Make it visually coherent and substantially more complete than a tutorial."""
    return parse_python(ai_call([
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(specification, indent=2)},
    ], max_tokens=18000))


def repair_code(specification: Dict[str, Any], source: str, error: str, attempt: int) -> str:
    instructions = """You are repairing a generated pygame-ce game.

Return the complete corrected main.py source only.
Do not return explanations or Markdown.

Fix the reported error without removing major gameplay features.
Keep the game self-contained, procedural, dependency-light, and compatible
with both desktop pygame-ce and Pygbag.
The program must exit automatically when GAME_AGENT_TEST=1.
Reminder: the GAME_AGENT_TEST=1 smoke test must be checked immediately after
imports and must exit (pygame.quit + sys.exit(0)) after ~3 seconds, before any
menu or gameplay loop starts."""
    prompt = f"""Repair attempt: {attempt}

Specification:
{json.dumps(specification, indent=2)}

Current source:
{source}

Actual error:
{error}"""
    return parse_python(ai_call([
        {"role": "system", "content": instructions},
        {"role": "user", "content": prompt},
    ], max_tokens=18000))


# --------------------------------------------------------------------------- #
# Validation & packaging (real subprocesses, real timeouts)
# --------------------------------------------------------------------------- #
def run_checked(command: List[str], cwd: Path, timeout: int, env: Optional[Dict[str, str]] = None) -> str:
    result = subprocess.run(
        command, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"Command failed with code {result.returncode}")
    return output


def validate_game(project: Path) -> None:
    source = project / "main.py"
    run_checked([sys.executable, "-m", "py_compile", str(source)], project, 20)

    env = os.environ.copy()
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    env["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
    env["GAME_AGENT_TEST"] = "1"
    try:
        run_checked([sys.executable, str(source)], project, 15, env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Validation timed out: the game did not exit within 15s. With "
            "GAME_AGENT_TEST=1 the program must run a ~3 second headless smoke test "
            "(init pygame, create the display, tick a few frames) and then call "
            "pygame.quit() and sys.exit(0) BEFORE opening the menu or main game loop."
        ) from exc


def build_browser_game(project: Path) -> Path:
    """Build the pygbag browser bundle. Raises on failure; caller decides fallback."""
    run_checked([sys.executable, "-m", "pygbag", "--build", str(project)], project, 180)
    for directory in (project / "build" / "web", project / "build"):
        if (directory / "index.html").exists():
            return directory
    matches = list(project.rglob("index.html"))
    if matches:
        return matches[0].parent
    raise RuntimeError("Pygbag did not produce a browser index.html")


_UX_PATCH_MARKER = "GameForge UX patch"


def patch_browser_build(browser_dir: Path) -> None:
    """
    Patch a pygbag build's index.html for real playability:
      * focus the canvas automatically (and on any click/touch/key) so SPACE
        and other keys reach the game immediately — pygbag canvases otherwise
        never receive keyboard input until clicked;
      * full-bleed dark canvas (kills the template's gray letterbox gutters).
    Injected content is idempotent (marker-checked), so restarts don't double-patch.
    """
    index = browser_dir / "index.html"
    if not index.exists():
        return
    html = index.read_text(encoding="utf-8", errors="replace")
    if _UX_PATCH_MARKER in html:
        return

    patch = f"""<style>/* {_UX_PATCH_MARKER}: full-bleed canvas, no gray gutters */
  html, body {{ margin: 0 !important; padding: 0 !important; height: 100% !important;
    background: #0b0d12 !important; overflow: hidden !important; }}
  #canvas {{ position: fixed !important; inset: 0 !important; width: 100vw !important;
    height: 100vh !important; margin: 0 !important; top: 0 !important; left: 0 !important;
    background: #0b0d12 !important; }}
  canvas {{ outline: none !important; }}
</style>
<script>/* {_UX_PATCH_MARKER}: keep keyboard focus on the game canvas */
(function () {{
  var focusCanvas = function () {{
    var c = document.getElementById("canvas");
    if (!c) return false;
    c.setAttribute("tabindex", "0");
    try {{ c.focus({{ preventScroll: true }}); }} catch (e) {{ c.focus(); }}
    return true;
  }};
  var tries = 0;
  var iv = setInterval(function () {{ if (focusCanvas() || ++tries > 60) clearInterval(iv); }}, 500);
  ["mousedown", "touchstart", "keydown"].forEach(function (ev) {{
    window.addEventListener(ev, function () {{ focusCanvas(); }}, {{ passive: true }});
  }});
}})();
</script>"""

    if "</body>" in html:
        html = html.replace("</body>", patch + "\n</body>", 1)
    else:
        html += patch
    index.write_text(html, encoding="utf-8")


def patch_all_existing_builds() -> int:
    """Patch every library game that already has a browser build (restart-safe)."""
    patched = 0
    for entry in _library():
        browser_dir = entry.get("browserDir")
        if browser_dir and Path(browser_dir, "index.html").exists():
            try:
                before = Path(browser_dir, "index.html").read_text(encoding="utf-8", errors="replace")
                patch_browser_build(Path(browser_dir))
                if _UX_PATCH_MARKER not in before:
                    patched += 1
            except Exception:  # noqa: BLE001
                pass
    return patched


def make_archive(project: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in project.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(project))
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Job registry + library persistence
# --------------------------------------------------------------------------- #
class GameJob:
    def __init__(self, job_id: str, game_id: str, prompt: str, resolution: str, quality: str):
        self.job_id = job_id
        self.game_id = game_id
        self.stage = "planning"
        self.status = "queued"
        self.progress = 0
        self.message = "Queued"
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.updated_at = time.time()
        self.prompt = prompt
        self.resolution = resolution
        self.quality = quality
        self.log: List[str] = []
        self.result: Dict[str, Any] = {}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "jobId": self.job_id,
            "gameId": self.game_id,
            "stage": self.stage,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "log": self.log[-12:],
        }


JOBS: Dict[str, GameJob] = {}


def _library() -> List[Dict[str, Any]]:
    index = GAMES_DIR / "library.json"
    if not index.exists():
        return []
    try:
        return json.loads(index.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_library(games: List[Dict[str, Any]]) -> None:
    (GAMES_DIR / "library.json").write_text(
        json.dumps(games, indent=2, ensure_ascii=False), encoding="utf-8")


def _upsert_game(entry: Dict[str, Any]) -> None:
    games = _library()
    games = [g for g in games if g.get("id") != entry["id"]]
    games.insert(0, entry)
    _save_library(games)


def _get_game(game_id: str) -> Dict[str, Any]:
    for game in _library():
        if game.get("id") == game_id:
            return game
    raise HTTPException(status_code=404, detail=f"Game '{game_id}' not found")


def _resolve_project(game_id: str) -> Path:
    project = GAMES_DIR / game_id
    if not project.exists():
        raise HTTPException(status_code=404, detail=f"Project folder for '{game_id}' is gone")
    return project


def run_pipeline(job: GameJob) -> Dict[str, Any]:
    """Full autonomous generation. Runs on a worker thread; updates `job` live."""
    def stage(name: str, progress: int, message: str = "") -> None:
        job.stage = name
        job.progress = progress
        job.message = message or name.capitalize()
        job.updated_at = time.time()
        job.log.append(f"[{time.strftime('%H:%M:%S')}] {name}: {job.message}")

    game_id = job.game_id
    project = GAMES_DIR / game_id
    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)

    stage("planning", 5)
    specification = plan_game(job.prompt, job.resolution, job.quality)
    (project / "specification.json").write_text(
        json.dumps(specification, indent=2, ensure_ascii=False), encoding="utf-8")

    stage("generating", 25)
    source = generate_code(specification)

    last_error = ""
    for attempt in range(MAX_REPAIRS + 1):
        (project / "main.py").write_text(source, encoding="utf-8")
        try:
            stage("testing" if attempt == 0 else "repairing",
                  45 + attempt * 8,
                  f"Validation pass {attempt + 1}/{MAX_REPAIRS + 1}")
            validate_game(project)
            last_error = ""
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            job.log.append(f"validation pass {attempt + 1} failed: {last_error[:300]}")
            if attempt == MAX_REPAIRS:
                raise RuntimeError(
                    "The game failed validation after all repair attempts:\n" + last_error
                ) from exc
            stage("repairing", 45 + attempt * 8, f"Repairing (attempt {attempt + 1}/{MAX_REPAIRS})")
            source = repair_code(specification, source, last_error, attempt + 1)

    browser_dir: Optional[Path] = None
    packaging_error = ""
    stage("packaging", 82, "Building browser bundle")
    try:
        browser_dir = build_browser_game(project)
        patch_browser_build(browser_dir)
    except Exception as exc:  # noqa: BLE001
        packaging_error = str(exc)
        job.message = "Browser build failed — shipping desktop zip"

    stage("packaging", 92, "Creating archive")
    archive_data = make_archive(project)
    (project / "game.zip").write_bytes(archive_data)

    entry = {
        "id": game_id,
        "title": str(specification.get("title") or "Generated Game"),
        "genre": specification.get("genre"),
        "engine": "pygame-ce",
        "runtime": "web" if browser_dir else "desktop",
        "resolution": job.resolution,
        "quality": job.quality,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "buildStatus": "ready",
        "playable": True,
        "published": False,
        "previewUrl": f"/api/gameforge/games/{game_id}/preview/" if browser_dir else None,
        "browserDir": str(browser_dir) if browser_dir else None,
        "browserBuildOk": browser_dir is not None,
        "packagingError": packaging_error or None,
        "prompt": job.prompt,
    }
    _upsert_game(entry)

    stage("ready", 100, "Ready to play")
    return {
        "id": game_id,
        "title": entry["title"],
        "specification": specification,
        "browser_dir": str(browser_dir) if browser_dir else "",
        "packaging_error": packaging_error,
    }


def _worker(job: GameJob) -> None:
    try:
        job.status = "running"
        run_pipeline(job)
        job.status = "done"
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.stage = "failed"
        job.error = str(exc)[-4000:]
        job.message = "Generation failed"
        # Record the failure in the library so the UI can show it honestly.
        try:
            _upsert_game({
                "id": job.game_id,
                "title": (job.prompt or "Generated Game")[:60],
                "engine": "pygame-ce",
                "runtime": "unknown",
                "resolution": job.resolution,
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "buildStatus": "failed",
                "playable": False,
                "published": False,
                "prompt": job.prompt,
            })
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("/maintenance/patch-builds")
def maintenance_patch_builds() -> Dict[str, Any]:
    """Re-apply the UX patch (canvas focus + full-bleed CSS) to all existing builds."""
    return {"patched": patch_all_existing_builds()}


@router.get("/health")
def health() -> Dict[str, Any]:
    backends = _llm_backends()
    return {
        "ok": bool(backends) and PYGAME_OK,
        "llm": {
            "configured": bool(backends),
            "provider": backends[0]["provider"] if backends else None,
            "model": backends[0]["model"] if backends else None,
        },
        "pygame": {"installed": PYGAME_OK, "version": PYGAME_VERSION},
        "pygbag": {"installed": PYGBAG_OK},
        "outputDir": str(GAMES_DIR),
        "maxRepairs": MAX_REPAIRS,
    }


@router.get("/games")
def list_games() -> List[Dict[str, Any]]:
    return _library()


@router.post("/games", status_code=202)
def create_game(payload: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="A game prompt is required")
    if not _llm_backends():
        raise HTTPException(
            status_code=503,
            detail="No LLM backend configured. Set LLM_API_KEY/LLM_BASE_URL/LLM_MODEL "
                   "(or DEEPSEEK_API_KEY / OPENAI_API_KEY) in backend/.env",
        )
    if not PYGAME_OK:
        raise HTTPException(status_code=503, detail="pygame is not installed in the backend venv")

    resolution = str(payload.get("resolution") or "1280x720")
    quality = str(payload.get("quality") or "Polished")

    game_id = f"game_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    job_id = uuid.uuid4().hex
    job = GameJob(job_id, game_id, prompt, resolution, quality)
    JOBS[job_id] = job

    thread = threading.Thread(target=_worker, args=(job,), daemon=True, name=f"gameforge-{job_id[:8]}")
    thread.start()

    snapshot = job.snapshot()
    snapshot["game"] = {
        "id": game_id,
        "title": prompt[:60],
        "buildStatus": "building",
        "playable": False,
        "resolution": resolution,
    }
    return snapshot


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job.snapshot()


@router.get("/games/{game_id}/download")
def download_game(game_id: str) -> FileResponse:
    _get_game(game_id)
    project = _resolve_project(game_id)
    archive = project / "game.zip"
    if not archive.exists():
        raise HTTPException(status_code=404, detail="Archive not built yet")
    return FileResponse(
        archive, media_type="application/zip", filename=f"{game_id}.zip")


@router.post("/games/{game_id}/restart", status_code=202)
def restart_game(game_id: str) -> Dict[str, Any]:
    entry = _get_game(game_id)
    project = _resolve_project(game_id)
    if not (project / "main.py").exists():
        raise HTTPException(status_code=400, detail="No generated source to rebuild")

    job_id = uuid.uuid4().hex
    job = GameJob(job_id, game_id, str(entry.get("prompt") or game_id),
                  str(entry.get("resolution") or "1280x720"),
                  str(entry.get("quality") or "Polished"))
    JOBS[job_id] = job
    thread = threading.Thread(target=_worker, args=(job,), daemon=True, name=f"gameforge-re-{job_id[:8]}")
    thread.start()
    return job.snapshot()


@router.get("/games/{game_id}/preview", response_model=None)
def preview_index(game_id: str) -> Any:
    """Redirect /preview -> /preview/ so relative asset URLs resolve correctly."""
    entry = _get_game(game_id)
    if not entry.get("browserDir"):
        return JSONResponse({"detail": "No browser build for this game"}, status_code=404)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/api/gameforge/games/{game_id}/preview/")


@router.get("/games/{game_id}/preview/{rest_of_path:path}")
def preview_file(game_id: str, rest_of_path: str) -> Any:
    """Serve the pygbag build (index.html + .wasm/.js/.dat assets) for the iframe."""
    entry = _get_game(game_id)
    if not rest_of_path:
        rest_of_path = "index.html"
    browser_dir = entry.get("browserDir")
    if not browser_dir:
        raise HTTPException(status_code=404, detail="No browser build for this game")
    base = Path(browser_dir)
    target = (base / rest_of_path).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found in browser build")
    media = {
        ".html": "text/html", ".js": "application/javascript", ".wasm": "application/wasm",
        ".dat": "application/octet-stream", ".json": "application/json",
        ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
        ".apk": "application/vnd.android.package-archive", ".zip": "application/zip",
    }
    return FileResponse(target, media_type=media.get(target.suffix.lower(), "application/octet-stream"))
