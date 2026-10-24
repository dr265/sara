"""SARA v5 - Persistent, context-following desktop AI agent.  Run: python full_ai_v3.py [--text]
Backend (app/api/desktop_agent.py) imports the engine from this file (all v4 names/APIs are preserved).

Deps : python-dotenv psutil pyautogui mss pillow pygetwindow pyperclip
Win  : uiautomation pycaw comtypes screen-brightness-control send2trash
Opt  : speechrecognition pyaudio pyttsx3 requests pygame bleak plyer pytesseract(+tesseract.exe for OCR fallback)

Setup:  pip install python-dotenv psutil pyautogui mss pillow pygetwindow pyperclip uiautomation pycaw comtypes screen-brightness-control send2trash
        pip install speechrecognition pyaudio pyttsx3 requests pygame bleak plyer pytesseract     (optional)
        .env: LLM_API_KEY=...  LLM_MODEL=...  [SARA_VISION_MODEL=... SARA_SEND_SCREENSHOTS=1]  (see CONFIG below)
Start:  python full_ai_v3.py --text      (keyboard)   |   python full_ai_v3.py   (voice)

What v5 adds (this single file is the engine):
  1. CONFIG      - env-configurable capture/privacy/session/memory settings (all default-safe).
  2. AWARENESS   - ScreenAwareness: active window/process/monitor/browser-URL detection, whole/active/window capture,
                   perceptual-hash change detection, OCR fallback, opt-in vision description; ContextWatcher thread that
                   detects app/tab/page switches (event-driven notifications, cheap 0.5s Win32 poll, no frame storage).
  3. MEMORY      - LongTermMemory (SQLite, WAL): preferences/rules/facts/workflows/lessons/context, relevance recall,
                   dedupe/contradiction handling, secret filter + arg redaction, stale cleanup, task history, tool stats.
  4. LEARNING    - Learner: outcome evaluation, corrections -> lessons/rules, durable instructions -> preferences,
                   successful plans -> reusable workflows (offline fallback), unreliable-tool reporting to the planner.
  5. SESSIONS    - SessionManager/TaskSession: OBSERVE -> CAPTURE -> UNDERSTAND -> RECALL -> DECIDE -> EXECUTE -> VERIFY
                   -> LEARN -> WAIT-FOR-CONTEXT-CHANGE loop with debounce, dedupe, timeout, retry limits, cancel/resume.
  6. EXECUTOR    - serialized execution lock, per-step timeout, timeout retry, duplicate-action guard, stale-context guard,
                   redacted event/log payloads, graceful shutdown (atexit).
  7. TOOLS       - analyze_screen, memory, privacy, task.  Voice/text commands: "pause watching", "resume watching",
                   "task status", "done", "continue", "remember that ...", "forget ...", "what do you remember".
"""
from __future__ import annotations
import argparse, asyncio, atexit, base64, ctypes, difflib, fnmatch, html, io, json, logging, math, os, queue, platform, random, re, shutil, sqlite3
import subprocess, sys, tempfile, threading, time, uuid, webbrowser, urllib.parse, urllib.request, urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
try: import psutil
except ImportError: psutil = None
try: from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): return False

APP_NAME, OS_NAME = "SARA AI", platform.system()
IS_WINDOWS = OS_NAME == "Windows"
NO_WIN = 0x08000000 if IS_WINDOWS else 0
DETACHED = subprocess.DETACHED_PROCESS if IS_WINDOWS else 0
load_dotenv(Path(__file__).resolve().parent / ".env")
ROOT = Path(os.environ.get("FULLAI_HOME", Path.home() / "FullAI_Workspace")).resolve()
STATE = ROOT / ".fullai"; STATE.mkdir(parents=True, exist_ok=True)
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/auto")
# Fallbacks on the SAME provider as LLM_BASE_URL. Defaults are the free-tier models that provably
# answer on xkiro with a non-paid key (verified live); override with LLM_FALLBACK_MODELS in .env.
LLM_FALLBACK_MODELS = [m.strip() for m in os.environ.get("LLM_FALLBACK_MODELS", "qwen/qwen3.5-397b-a17b:free,qwen/qwen3.6-35b-a3b:free,cohere/tiny-aya-global").split(",") if m.strip()]

# ---- ALTERNATE PROVIDERS (cross-provider failover) ----
# A single upstream can be fully paywalled/down; when every model on LLM_BASE_URL fails the engine
# moves to the next provider. AnyModel is auto-registered from ANYMODEL_* (already in .env), and
# extra providers can be added with LLM_ALT_PROVIDERS as
#   "base_url|api_key|model1,model2;base_url2|api_key2|model3"
ANYMODEL_API_KEY = os.environ.get("ANYMODEL_API_KEY", "").strip()
ANYMODEL_BASE_URL = os.environ.get("ANYMODEL_BASE_URL", "").strip()
ANYMODEL_MODELS = [m.strip() for m in os.environ.get(
    "ANYMODEL_FALLBACK_MODELS", "cc/claude-sonnet-4-6,cc/claude-haiku-4-5-20251001,ag/gemini-3.6-flash-medium").split(",") if m.strip()]
if os.environ.get("ANYMODEL_MODEL", "").strip():
    ANYMODEL_MODELS = [os.environ["ANYMODEL_MODEL"].strip()] + [m for m in ANYMODEL_MODELS if m != os.environ["ANYMODEL_MODEL"].strip()]


def _alt_providers():
    """Parse LLM_ALT_PROVIDERS into [{"base", "key", "models", "label"}] (order preserved)."""
    out = []
    for spec in os.environ.get("LLM_ALT_PROVIDERS", "").split(";"):
        parts = [p.strip() for p in spec.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1]:
            continue
        models = [m.strip() for m in parts[2].split(",") if m.strip()]
        if models:
            out.append({"base": parts[0], "key": parts[1], "models": models, "label": "alt"})
    return out


def llm_providers():
    """Ordered failover chain: every (base_url, api_key, model) the engine may call."""
    chain = [{"base": LLM_BASE_URL, "key": LLM_API_KEY, "model": m, "label": "primary"}
             for m in ([LLM_MODEL] + LLM_FALLBACK_MODELS) if m]
    chain += [{"base": p["base"], "key": p["key"], "model": m, "label": p["label"]}
              for p in _alt_providers() for m in p["models"]]
    if ANYMODEL_API_KEY and ANYMODEL_BASE_URL:
        chain += [{"base": ANYMODEL_BASE_URL, "key": ANYMODEL_API_KEY, "model": m, "label": "anymodel"}
                  for m in ANYMODEL_MODELS if m]
    seen, uniq = set(), []
    for c in chain:
        k = (c["base"].rstrip("/"), c["model"])
        if k in seen:
            continue
        seen.add(k); uniq.append(c)
    return uniq

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
WAKE_WORD = os.environ.get("SARA_WAKE_WORD", "").strip().lower()
MIC_INDEX = int(os.environ["SARA_MIC_INDEX"]) if os.environ.get("SARA_MIC_INDEX", "").isdigit() else None
TRUSTED = {t.strip() for t in os.environ.get("SARA_TRUSTED_TOOLS", "").split(",") if t.strip()}
# ---- CONFIG (v5) ----
def _env_flag(n, d):
    v = os.environ.get(n); return d if v is None else v.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
CAPTURE_ENABLED = _env_flag("SARA_CAPTURE", True)             # master switch for screen capture/analysis
SEND_SCREENSHOTS = _env_flag("SARA_SEND_SCREENSHOTS", False)  # allow sending screen IMAGES to the configured model (off by default)
VISION_MODEL = os.environ.get("SARA_VISION_MODEL", "").strip()
AUTO_CONTINUE = _env_flag("SARA_AUTO_CONTINUE", True)         # continue ongoing task when the user switches context
SESSION_TIMEOUT_MIN = float(os.environ.get("SARA_SESSION_TIMEOUT_MIN", "45"))
MAX_CONTINUATIONS = int(os.environ.get("SARA_MAX_CONTINUATIONS", "40"))
CONTEXT_POLL = float(os.environ.get("SARA_CONTEXT_POLL", "0.5"))
SCREEN_POLL = float(os.environ.get("SARA_SCREEN_POLL", "2.0"))
STALE_DAYS = int(os.environ.get("SARA_STALE_DAYS", "90"))
STEP_TIMEOUT = float(os.environ.get("SARA_STEP_TIMEOUT", "180"))
MEMORY_DB = Path(os.environ.get("SARA_MEMORY_DB", STATE / "memory.db"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                    handlers=[logging.FileHandler(STATE / "sara.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("sara")


def _engine_logging():
    """Guarantee the engine writes diagnostics into <state>/sara.log.

    basicConfig above is a no-op when the host app (uvicorn/FastAPI) configured logging first,
    and uvicorn's dictConfig also disables pre-existing loggers — which used to hide every planner,
    failover and tool message when the engine was served by the unified backend. Harmless to call
    repeatedly; errors are ignored because logging must never take the engine down.
    """
    try:
        log.disabled = False
        log.setLevel(logging.INFO)
        if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
            fh = logging.FileHandler(STATE / "sara.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            log.addHandler(fh)
    except Exception:
        pass


_engine_logging()


class Level(IntEnum): INFO = 0; LOW = 1; AUTHORIZED = 2; CONFIRM = 3
class PolicyViolation(Exception): pass
class ActionFailed(Exception):
    def __init__(self, msg: str, **info): super().__init__(msg); self.info = info

# ============================ 1. CAPABILITIES / BUS ============================
CAPS: Dict[str, str] = {}
def probe(name, fn):
    try: v = fn(); CAPS[name] = "available"; return v
    except Exception as e: CAPS[name] = f"missing: {type(e).__name__}: {e}"; return None
def cap_ok(n): return CAPS.get(n) == "available"
def _pg():
    import pyautogui; pyautogui.FAILSAFE = True; pyautogui.PAUSE = 0.05; return pyautogui
def _mss():
    import mss; mss.mss().close(); return mss
def _uia():
    if not IS_WINDOWS: raise RuntimeError("not Windows")
    import uiautomation; return uiautomation
PG, MSS, SR, UIA, GW, CLIP = (probe("desktop_input", _pg), probe("screen_capture", _mss), probe("speech_to_text", lambda: __import__("speech_recognition")),
                              probe("ui_automation", _uia), probe("window_control", lambda: __import__("pygetwindow")), probe("clipboard", lambda: __import__("pyperclip")))
PYCAW = probe("audio_endpoint", lambda: __import__("pycaw.pycaw", fromlist=["AudioUtilities"]))
SBC = probe("brightness_lib", lambda: __import__("screen_brightness_control"))
S2T = probe("recycle_bin", lambda: __import__("send2trash"))
BLEAK = probe("ble", lambda: __import__("bleak"))
PLYER = probe("notify_lib", lambda: __import__("plyer", fromlist=["notification"]))
OCR = probe("ocr", lambda: __import__("pytesseract"))
WINREG = probe("registry", lambda: __import__("winreg")) if IS_WINDOWS else None
PS_EXE = shutil.which("powershell") if IS_WINDOWS else None
CAPS.update({"windows": "available" if IS_WINDOWS else f"missing: {OS_NAME}", "powershell": "available" if PS_EXE else "missing: powershell.exe",
             "system_metrics": "available" if psutil else "missing: psutil",
             "llm": "available (%d models: %s)" % (len(llm_providers()), ", ".join(c["model"] for c in llm_providers()[:3])) if llm_providers() else "missing: LLM_API_KEY",
             "vision_share": "available" if (LLM_API_KEY or ANYMODEL_API_KEY) else "missing: LLM_API_KEY",
             "vision": "available" if (LLM_API_KEY and SEND_SCREENSHOTS) else "missing: SARA_SEND_SCREENSHOTS=1 (+SARA_VISION_MODEL) required"})
for c, e in (("netsh", "netsh"), ("winget", "winget"), ("tzutil", "tzutil"), ("shutdown_exe", "shutdown"), ("nvidia_smi", "nvidia-smi")):
    CAPS[c] = "available" if shutil.which(e) else f"missing: {e}"
_LAZY: Dict[str, Tuple[float, bool]] = {}
def lazy_cap(name, check, ttl=600):
    hit = _LAZY.get(name)
    if hit and time.time() - hit[0] < ttl: return hit[1]
    try: ok = bool(check())
    except Exception: ok = False
    _LAZY[name] = (time.time(), ok); CAPS[name] = "available" if ok else "missing: not installed"; return ok

class Bus:
    """Fan-out of engine events to every WebSocket viewer.

    Emits arrive from worker threads (executor, planner) so they are handed to the serving asyncio
    loop with call_soon_threadsafe(). `loop` used to be left at None forever, which silently dropped
    every event and left the UI showing "System idle" even while a plan was running: it is now bound
    explicitly by the API on each request, and self-binds the first time emit() runs inside a loop.
    """
    def __init__(self): self.subs: set = set(); self.loop = None
    def sub(self): q = asyncio.Queue(maxsize=2000); self.subs.add(q); return q
    def unsub(self, q): self.subs.discard(q)
    def bind(self, loop=None):
        """Bind (or re-bind) the loop that pushes queued events into the sockets."""
        if loop is None:
            try: loop = asyncio.get_running_loop()
            except RuntimeError: return False
        self.loop = loop; return True
    def emit(self, kind, **p):
        if not self.subs: return
        loop = self.loop
        if loop is None:                       # first emit from an async context binds the loop lazily
            try: loop = asyncio.get_running_loop()
            except RuntimeError: return
            self.loop = loop
        e = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": kind, **p}
        def push():
            for q in list(self.subs):
                try: q.put_nowait(e)
                except Exception: pass
        try: loop.call_soon_threadsafe(push)
        except Exception: pass
BUS = Bus()

def _sleep(s): time.sleep(max(0.0, float(s)))
def _norm(s): return " ".join(str(s).lower().split()).strip(" .,!?")
def _q(s): return "'" + str(s).replace("'", "''") + "'"
def _res(method, requested=None, actual=None, verified=False, fallback=None, **x):
    return {"method": method, "requested": requested, "actual": actual, "verified": bool(verified), "fallback": fallback, **x}
def _poll(fn, timeout, interval=0.5):
    end = time.time() + timeout
    while True:
        try:
            v = fn()
            if v: return v
        except Exception: pass
        if time.time() >= end: return None
        _sleep(interval)
def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin()) if IS_WINDOWS else os.geteuid() == 0
    except Exception: return False
def need_admin(what):
    if not is_admin(): raise ActionFailed(f"{what} requires administrator rights and SARA is not elevated. Use the Settings UI or run SARA as administrator.", requires_admin=True)
def need_win(what):
    if not IS_WINDOWS: raise ActionFailed(f"{what} is only supported on Windows.", unsupported=True)
_ADMIN_RE = re.compile(r"access is denied|requires elevation|administrator|UnauthorizedAccess|PermissionDenied", re.I)
# ---- secret hygiene (v5): nothing matching these is ever persisted or emitted ----
_SECRET_KEYS = re.compile(r"(pass(word|wd|phrase)?|pwd|secret|token|api[_ -]?key|auth|bearer|otp|pin\b|credential|cookie|session[_ -]?id)", re.I)
_SECRET_VALS = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|\b[A-Fa-f0-9]{32,}\b|[A-Za-z0-9+/]{48,}={0,2})")
def _redact(obj):
    if isinstance(obj, dict): return {k: ("***" if _SECRET_KEYS.search(str(k)) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [_redact(v) for v in obj]
    if isinstance(obj, str): return _SECRET_VALS.sub("***", obj)
    return obj
def _has_secret(text):
    t = str(text); return bool(_SECRET_VALS.search(t)) or bool(re.search(r"(password|passwd|passphrase|api[_ -]?key|token|secret|otp|pin)\s*(is|:|=)", t, re.I))
def _timed(fn, timeout):
    box: Dict[str, Any] = {}
    def w():
        try: box["v"] = fn()
        except BaseException as e: box["e"] = e
    th = threading.Thread(target=w, daemon=True, name="step"); th.start(); th.join(timeout)
    if th.is_alive(): raise ActionFailed(f"Step exceeded {timeout:.0f}s.", timeout=True)
    if "e" in box: raise box["e"]
    return box.get("v")

def run_cmd(args, timeout=30.0, check=True):
    try: r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, creationflags=NO_WIN)
    except FileNotFoundError: raise ActionFailed(f"'{args[0]}' is not available.", unsupported=True)
    except subprocess.TimeoutExpired: raise ActionFailed(f"'{args[0]}' timed out after {timeout:.0f}s.", timeout=True)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:300]
        raise ActionFailed(f"'{' '.join(args[:2])}' failed ({r.returncode}): {msg or 'no output'}", requires_admin=bool(_ADMIN_RE.search(msg)))
    return r

def run_ps(script, timeout=30.0, json_out=False):
    if not PS_EXE: raise ActionFailed("PowerShell is not available.", unsupported=True)
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try: r = subprocess.run([PS_EXE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                            capture_output=True, text=True, errors="replace", timeout=timeout, creationflags=NO_WIN)
    except subprocess.TimeoutExpired: raise ActionFailed(f"PowerShell timed out after {timeout:.0f}s.", timeout=True)
    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if not out and (r.returncode != 0 or err): raise ActionFailed(f"PowerShell failed: {err[:300] or 'no output'}", requires_admin=bool(_ADMIN_RE.search(err)))
    if not json_out: return out
    if not out: return None
    try: return json.loads(out)
    except json.JSONDecodeError: raise ActionFailed(f"PowerShell returned non-JSON: {out[:200]}")

def _coro(c, timeout):
    box = {}
    def t():
        try: box["v"] = asyncio.run(c)
        except Exception as e: box["e"] = e
    th = threading.Thread(target=t, daemon=True); th.start(); th.join(timeout)
    if th.is_alive(): raise ActionFailed("Async operation timed out.", timeout=True)
    if "e" in box: raise ActionFailed(str(box["e"]))
    return box.get("v")

# ============================ 2. VOICE I/O ============================
class _Mode: voice = False
MODE = _Mode()

class Speaker:
    def __init__(self):
        self.q: "queue.Queue[Optional[str]]" = queue.Queue(); self.muted = False; self._busy = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="tts").start()
    @staticmethod
    def is_hindi(t): return bool(re.search(r"[\u0900-\u097F]", t))
    def _minimax(self, text):
        try: import requests, pygame
        except ImportError: return False
        try:
            payload = {"model": "speech-01-turbo", "text": text, "stream": False,
                       "voice_setting": {"voice_id": "hindi_female_2_v1" if self.is_hindi(text) else "English_radiant_girl", "speed": 1.0, "vol": 1.0, "pitch": 0},
                       "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1}}
            res = requests.post("https://api.minimax.chat/v1/t2a_v2", json=payload, timeout=15,
                                headers={"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}).json()
            hx = (res.get("data") or {}).get("audio")
            if not hx: log.error("MiniMax error: %s", str(res)[:200]); return False
            if not pygame.mixer.get_init(): pygame.mixer.init(frequency=32000)
            pygame.mixer.music.load(io.BytesIO(bytes.fromhex(hx))); pygame.mixer.music.play()
            while pygame.mixer.music.get_busy(): time.sleep(0.05)
            return True
        except Exception as e: log.error("MiniMax failed: %s", e); return False
    def _loop(self):
        if IS_WINDOWS:
            try: ctypes.windll.ole32.CoInitialize(None)
            except Exception: pass
        engine = None
        while True:
            text = self.q.get()
            if text is None: break
            self._busy.set()
            try:
                if not (MINIMAX_API_KEY and self._minimax(text)):
                    try:
                        import pyttsx3
                        if engine is None: engine = pyttsx3.init(); engine.setProperty("rate", 185)
                        engine.say(text); engine.runAndWait()
                    except Exception as e: log.error("Local TTS failed: %s", e)
            finally: self._busy.clear()
    def say(self, text, wait=False):
        text = str(text).strip()
        if not text: return
        print(f"\n  [SARA] {text}\n"); BUS.emit("speech", text=text)
        if self.muted: return
        self.q.put(text)
        if wait:
            end = time.time() + 30
            while (not self.q.empty() or self._busy.is_set()) and time.time() < end: time.sleep(0.05)
            time.sleep(0.2)
    def drop(self):
        try:
            while True: self.q.get_nowait()
        except queue.Empty: pass
SPEAK = Speaker()

class Ears:
    def __init__(self):
        self.ok = bool(SR); self._cal = False; self._lock = threading.Lock()
        if self.ok: self.r = SR.Recognizer(); self.r.dynamic_energy_threshold = True; self.r.energy_threshold = 300; self.r.pause_threshold = 0.8
    def calibrate(self):
        try:
            with SR.Microphone(device_index=MIC_INDEX) as src: self.r.adjust_for_ambient_noise(src, duration=1.2)
        except Exception as e: log.error("Microphone unavailable: %s", e); self.ok = False
    def listen(self, timeout=8.0, phrase_limit=15.0):
        if not self.ok: return None
        with self._lock:
            if not self._cal: self.calibrate(); self._cal = True
            if not self.ok: return None
            try:
                with SR.Microphone(device_index=MIC_INDEX) as src: audio = self.r.listen(src, timeout=timeout, phrase_time_limit=phrase_limit)
            except Exception: return None
        try: text = (self.r.recognize_google(audio) or "").strip()
        except Exception: return None
        if text: print(f"  [you] {text}"); BUS.emit("heard", text=text)
        return text or None
EARS = Ears()
YES = {"yes", "yeah", "yep", "yup", "proceed", "confirm", "go ahead", "do it", "sure", "affirmative", "ok", "okay"}
NO = {"no", "nope", "cancel", "abort", "stop", "don't", "never mind", "negative"}
def ask_yes_no(question):
    for _ in range(2):
        SPEAK.say(question, wait=True)
        if MODE.voice and EARS.ok: heard = EARS.listen(timeout=9, phrase_limit=5)
        else:
            try: heard = input(f"  {question} (yes/no) > ")
            except (EOFError, KeyboardInterrupt): return False
        if not heard: continue
        low = _norm(heard)
        if any(low == w or low.startswith(w + " ") for w in YES): return True
        if any(low == w or low.startswith(w + " ") for w in NO): return False
    SPEAK.say("No clear confirmation. Cancelling for safety."); return False

# ============================ 3. APP RESOLVER ============================
class AppResolver:
    PROTOCOLS = {"whatsapp": "whatsapp://", "settings": "ms-settings:", "calculator": "calculator:", "mail": "mailto:", "store": "ms-windows-store:",
                 "camera": "microsoft.windows.camera:", "maps": "bingmaps:", "photos": "ms-photos:", "spotify": "spotify:"}
    EXE_HINTS = {"chrome": ["chrome.exe"], "edge": ["msedge.exe"], "firefox": ["firefox.exe"], "vscode": ["code.cmd", "code.exe"], "notepad": ["notepad.exe"],
                 "explorer": ["explorer.exe"], "terminal": ["wt.exe"], "powershell": ["powershell.exe"], "cmd": ["cmd.exe"]}
    ALIASES = {"google chrome": "chrome", "browser": "chrome", "vs code": "vscode", "visual studio code": "vscode", "code": "vscode", "file explorer": "explorer",
               "files": "explorer", "command prompt": "cmd", "windows terminal": "terminal", "whats app": "whatsapp", "whatsapp desktop": "whatsapp",
               "control panel": "settings", "docker": "docker desktop", "msedge": "edge", "windowsterminal": "terminal"}
    WINDOW_HINTS = {"chrome": "Chrome", "edge": "Edge", "firefox": "Firefox", "vscode": "Visual Studio Code", "whatsapp": "WhatsApp", "explorer": "File Explorer",
                    "settings": "Settings", "terminal": "Terminal", "notepad": "Notepad", "spotify": "Spotify"}
    def __init__(self): self._start_apps: Dict[str, str] = {}; self._loaded = False; self._lock = threading.Lock()
    def load_start_apps(self, force=False):
        with self._lock:
            if self._loaded and not force: return
            self._loaded = True
            if not PS_EXE: return
            cache = STATE / "startapps.json"
            if cache.exists() and not force and time.time() - cache.stat().st_mtime < 86400:
                try: self._start_apps = json.loads(cache.read_text("utf-8")); return
                except Exception: pass
            try:
                data = run_ps("Get-StartApps | ConvertTo-Json -Compress", 60, True) or []
                data = [data] if isinstance(data, dict) else data
                self._start_apps = {d["Name"]: d["AppID"] for d in data if d.get("Name") and d.get("AppID")}
                cache.write_text(json.dumps(self._start_apps), encoding="utf-8")
            except Exception as e: log.error("Get-StartApps failed: %s", e)
    def find_appid(self, name):
        self.load_start_apps(); n = _norm(name); items = list(self._start_apps.items())
        for pred in (lambda d: _norm(d) == n, lambda d: _norm(d).startswith(n), lambda d: n in _norm(d)):
            for disp, aid in items:
                if pred(disp): return disp, aid
        m = difflib.get_close_matches(n, [_norm(d) for d, _ in items], n=1, cutoff=0.72)
        return next(((d, a) for d, a in items if _norm(d) == m[0]), None) if m else None
    def canonical(self, name): n = _norm(name); return self.ALIASES.get(n, n)
    def window_hint(self, key):
        if key in self.WINDOW_HINTS: return self.WINDOW_HINTS[key]
        f = self.find_appid(key); return f[0] if f else key
    def launch(self, name):
        key, tried = self.canonical(name), []
        for exe in self.EXE_HINTS.get(key, []) + [f"{key}.exe", key]:
            full = shutil.which(exe)
            if full:
                subprocess.Popen([full], close_fds=True, creationflags=DETACHED, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"method": "exe", "target": full}
            tried.append(exe)
        if IS_WINDOWS:
            f = self.find_appid(key)
            if f: subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{f[1]}"], close_fds=True); return {"method": "appsfolder", "target": f[1], "display": f[0]}
            if key in self.PROTOCOLS:
                try: os.startfile(self.PROTOCOLS[key]); return {"method": "protocol", "target": self.PROTOCOLS[key]}
                except Exception: pass
            subprocess.Popen(f'start "" "{key}"', shell=True, creationflags=NO_WIN)
            return {"method": "shell", "target": key, "unverified": True}
        raise ActionFailed(f"'{name}' is not installed (tried {tried}).")
APPS = AppResolver()

# ============================ 4. PERCEPTION ============================
if IS_WINDOWS:
    import ctypes.wintypes as wt
    U32 = ctypes.windll.user32
    class MONITORINFO(ctypes.Structure): _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
    U32.MonitorFromWindow.restype = ctypes.c_void_p; U32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    U32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]

def foreground_title():
    try:
        if IS_WINDOWS:
            u = ctypes.windll.user32; h = u.GetForegroundWindow(); n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1); u.GetWindowTextW(h, buf, n + 1); return buf.value
        w = GW.getActiveWindow(); return w.title if w else ""
    except Exception: return ""
def find_window(fragment, timeout=12.0):
    if not GW: return None
    needle, end = _norm(fragment), time.time() + float(timeout)
    while True:
        try:
            for w in GW.getAllWindows():
                if w.title and needle in w.title.lower() and w.width > 0: return w
        except Exception: pass
        if time.time() >= end: return None
        _sleep(0.35)
def focus_window(w):
    if w is None: return False
    for _ in range(3):
        try:
            if w.isMinimized: w.restore()
            w.activate(); _sleep(0.35); return True
        except Exception:
            h = getattr(w, "_hWnd", None)
            if h and IS_WINDOWS:
                try: ctypes.windll.user32.ShowWindow(h, 9); ctypes.windll.user32.SetForegroundWindow(h); _sleep(0.35); return True
                except Exception: pass
            _sleep(0.3)
    return False
def ui_elements(depth=7, limit=250):
    if not UIA: return []
    try: ctypes.windll.ole32.CoInitialize(None)
    except Exception: pass
    out, seen = [], [0]
    try: root = UIA.GetForegroundControl()
    except Exception: return []
    def walk(c, d):
        if d > depth or seen[0] > 700 or len(out) >= limit: return
        try: kids = c.GetChildren()
        except Exception: return
        for k in kids:
            seen[0] += 1
            try:
                name = (k.Name or "").strip()
                if name:
                    r = k.BoundingRectangle
                    out.append({"name": name[:120], "type": k.ControlTypeName, "center": [(r.left + r.right) // 2, (r.top + r.bottom) // 2], "enabled": bool(k.IsEnabled)})
            except Exception: pass
            walk(k, d + 1)
    walk(root, 0); return out
def screen_state():
    wins = []
    if GW:
        try: wins = [w.title for w in GW.getAllWindows() if w.title and w.width > 0]
        except Exception: pass
    return {"foreground": foreground_title(), "windows": wins[:25], "elements": [e["name"] for e in ui_elements(5, 60)][:50]}

# ---- 4b. SCREEN / CONTEXT AWARENESS (v5) ----
class ScreenAwareness:
    """Knows WHERE the user is (process, window, monitor, browser URL) and can capture/inspect the whole screen on demand.
    Privacy: capture is user-controlled (enabled/paused), frames are never persisted, images leave the machine only if SARA_SEND_SCREENSHOTS=1."""
    BROWSERS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave", "opera.exe": "Opera", "vivaldi.exe": "Vivaldi"}
    TEXTY = {"TextControl", "DocumentControl", "EditControl", "HyperlinkControl", "HeaderItemControl"}
    def __init__(self): self.enabled = CAPTURE_ENABLED; self.paused = False; self._last_hash: Optional[int] = None; self._url_cache: Tuple[str, float, Any] = ("", 0.0, None)
    def allowed(self): return bool(MSS) and self.enabled and not self.paused
    def status(self): return {"capture_enabled": self.enabled, "paused": self.paused, "allowed": self.allowed(), "send_screenshots_to_model": SEND_SCREENSHOTS, "vision_model": (VISION_MODEL or LLM_MODEL) if SEND_SCREENSHOTS else None, "ocr": bool(OCR)}
    def set(self, enabled=None, paused=None):
        if enabled is not None: self.enabled = bool(enabled)
        if paused is not None: self.paused = bool(paused)
        BUS.emit("privacy_changed", **self.status()); return {**self.status(), "verified": True}
    def foreground(self):
        h = U32.GetForegroundWindow() if IS_WINDOWS else 0; title = foreground_title(); pid, proc, rect, mon = 0, "", None, None
        if IS_WINDOWS and h:
            try:
                p = wt.DWORD(); U32.GetWindowThreadProcessId(h, ctypes.byref(p)); pid = p.value
                if psutil and pid: proc = psutil.Process(pid).name()
            except Exception: pass
            try:
                r = wt.RECT(); U32.GetWindowRect(h, ctypes.byref(r)); rect = [r.left, r.top, r.right, r.bottom]
                mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
                if U32.GetMonitorInfoW(U32.MonitorFromWindow(h, 2), ctypes.byref(mi)):
                    rc = mi.rcMonitor; mon = {"x": rc.left, "y": rc.top, "w": rc.right - rc.left, "h": rc.bottom - rc.top, "primary": bool(mi.dwFlags & 1)}
            except Exception: pass
        return {"hwnd": int(h or 0), "pid": pid, "process": proc, "title": title, "rect": rect, "monitor": mon}
    def browser_url(self, fg):
        b = self.BROWSERS.get(_norm(fg.get("process") or ""))
        if not b or not UIA or not self.allowed(): return None
        key = f"{fg.get('hwnd')}|{fg.get('title')}"
        if self._url_cache[0] == key and time.time() - self._url_cache[1] < 3.0: return self._url_cache[2]
        url = None
        try:
            ctypes.windll.ole32.CoInitialize(None)
            root = UIA.ControlFromHandle(fg["hwnd"]) if fg.get("hwnd") else UIA.GetForegroundControl()
            e = root.EditControl(searchDepth=20, Compare=lambda c, d: "address" in (c.Name or "").lower())
            if e.Exists(0.3, 0.1):
                v = (e.GetValuePattern().Value or "").strip()
                if v and " " not in v: url = v if re.match(r"^[a-z]+://", v) else "https://" + v
        except Exception as ex: log.debug("url detect: %s", ex)
        out = {"browser": b, "url": url}; self._url_cache = (key, time.time(), out); return out
    def context(self, fg=None):
        fg = fg or self.foreground(); proc = _norm(fg.get("process") or ""); title = fg.get("title") or ""
        app = APPS.canonical(proc[:-4] if proc.endswith(".exe") else proc) if proc else _norm(title.split(" - ")[-1] if " - " in title else title)
        info = self.browser_url(fg); url = info["url"] if info else None
        page = re.sub(r"\s*[-–—]\s*(Google Chrome|Microsoft.*Edge|Mozilla Firefox|Brave|Opera|Vivaldi).*$", "", title) if info else None
        host = urllib.parse.urlparse(url).netloc if url else None
        sig = "|".join([proc or app, _norm(title)[:80], (url or "").split("#")[0][:160]])
        return {"sig": sig, "app": app, "process": proc, "title": title, "pid": fg.get("pid"), "hwnd": fg.get("hwnd"), "monitor": fg.get("monitor"), "rect": fg.get("rect"),
                "browser": info["browser"] if info else None, "url": url, "host": host, "page_title": page, "ts": time.time()}
    def capture(self, scope="active", ctx=None):
        if not MSS: raise ActionFailed("Screen capture unavailable (mss missing).", unsupported=True)
        if not self.allowed(): raise ActionFailed("Screen capture is paused or disabled by the user (privacy). Say 'resume watching' to allow it.", privacy=True)
        from PIL import Image
        ctx = ctx or WATCHER.current or self.context(); scope = _norm(scope)
        with MSS.mss() as s:
            mons = s.monitors; region = mons[0]
            if scope == "window" and ctx.get("rect"):
                l, t, r, b = ctx["rect"]; region = {"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)}
            elif scope == "active" and ctx.get("monitor"):
                m = ctx["monitor"]; region = next((x for x in mons[1:] if x["left"] == m["x"] and x["top"] == m["y"]), mons[1] if len(mons) > 1 else mons[0])
            elif scope in ("primary", "active"): region = mons[1] if len(mons) > 1 else mons[0]
            shot = s.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), dict(region)
    @staticmethod
    def _hash(img):
        px = list(img.convert("L").resize((16, 16)).getdata()); avg = sum(px) / len(px); return sum(1 << i for i, p in enumerate(px) if p > avg)
    def screen_changed(self, threshold=40):
        try: img, _ = self.capture("active")
        except Exception: return False
        h = self._hash(img); prev, self._last_hash = self._last_hash, h
        return prev is not None and bin(prev ^ h).count("1") >= threshold
    @staticmethod
    def _data_url(img, quality=60):
        im = img.copy(); im.thumbnail((1280, 1280)); buf = io.BytesIO(); im.save(buf, "JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    def describe(self, data_url, ctx):
        if not SEND_SCREENSHOTS: return "vision disabled: set SARA_SEND_SCREENSHOTS=1 (and optionally SARA_VISION_MODEL) to allow sending screen images to the model"
        return llm_call("Describe this desktop screen factually: application, page, key visible content, controls, dialogs or errors. Max 6 short lines.",
                        json.dumps({"context": {k: ctx.get(k) for k in ("app", "title", "url")}}), 0.1, 60, model=VISION_MODEL or LLM_MODEL, image=data_url)
    def analyze(self, scope="active", include_image=False, describe=False):
        ctx = WATCHER.current or self.context(); img, region = self.capture(scope, ctx); els = ui_elements(6, 160)
        text = [e["name"] for e in els if e["type"] in self.TEXTY][:60]; inter = [{"name": e["name"], "type": e["type"], "center": e["center"]} for e in els if e["type"] not in self.TEXTY][:60]
        out = {"context": {k: v for k, v in ctx.items() if k not in ("rect", "sig")}, "region": region, "size": list(img.size), "text": text, "interactive": inter, "verified": True}
        if len(text) + len(inter) < 8 and OCR:
            try: out["ocr"] = " ".join(OCR.image_to_string(img).split())[:1500]
            except Exception as e: out["ocr_error"] = str(e)[:120]
        if include_image or describe:
            url = self._data_url(img)
            if include_image: out["image"] = url
            if describe:
                try: out["description"] = self.describe(url, ctx)
                except Exception as e: out["description"] = f"vision failed: {e}"
        MEM.set("window", ctx.get("title") or "", quiet=True); return out
AWARE = ScreenAwareness()

class ContextWatcher(threading.Thread):
    """Detects when the user moves to another app/window/tab/page (cheap Win32 poll) and, only while a task session is active,
    checks for significant on-screen changes at a controlled interval. Emits 'context_changed' and notifies SESSIONS."""
    def __init__(self):
        super().__init__(daemon=True, name="context-watcher"); self.stop_ev = threading.Event(); self.current: Dict[str, Any] = {}; self.sig = ""
        self._quick = None; self._screen_ts = 0.0; self._clean_ts = time.time(); self.changes = 0
    def refresh(self, force=False):
        fg = AWARE.foreground(); quick = f"{fg.get('pid')}|{fg.get('title')}"
        if quick == self._quick and not force: return None
        self._quick = quick; ctx = AWARE.context(fg)
        if ctx["sig"] == self.sig and not force: return None
        self.sig, self.current, self.changes = ctx["sig"], ctx, self.changes + 1
        MEM.set("window", ctx["title"], quiet=True); MEM.set("app", ctx["app"], quiet=True)
        if ctx.get("url"): MEM.set("url", ctx["url"], quiet=True)
        BUS.emit("context_changed", **{k: v for k, v in ctx.items() if k != "rect"}); return ctx
    def run(self):
        if IS_WINDOWS:
            try: ctypes.windll.ole32.CoInitialize(None)
            except Exception: pass
        while not self.stop_ev.is_set():
            try:
                ctx = self.refresh()
                if ctx:
                    if not EXEC.busy.is_set(): SESSIONS.on_context(ctx, "window")
                elif SESSIONS.active() and AWARE.allowed() and not EXEC.busy.is_set() and time.time() - self._screen_ts >= SCREEN_POLL:
                    self._screen_ts = time.time()
                    if AWARE.screen_changed(): SESSIONS.on_context(self.current, "screen")
                if time.time() - self._clean_ts > 3600: self._clean_ts = time.time(); LTM.cleanup()
            except Exception as e: log.debug("watcher: %s", e)
            self.stop_ev.wait(CONTEXT_POLL)
WATCHER = ContextWatcher()

# ============================ 5. MEMORY ============================
class Memory:
    """Working (per-process) memory: slots, recent turns/actions, current goal. Long-term memory lives in LongTermMemory."""
    def __init__(self): self.lock = threading.Lock(); self.slots: Dict[str, Any] = {}; self.turns: List[dict] = []; self.actions: List[dict] = []; self.last_plan: List["Step"] = []; self.last_goal = ""
    def set(self, k, v, quiet=False):
        with self.lock: self.slots[k] = v
        if not quiet: BUS.emit("memory_update", key=k, value=str(v)[:200])
    def get(self, k, default=None):
        with self.lock: return self.slots.get(k, default)
    def turn(self, role, text):
        with self.lock: self.turns = (self.turns + [{"role": role, "text": _redact(text)[:500]}])[-16:]
    def action(self, tool_name, args, ok):
        with self.lock: self.actions = (self.actions + [{"tool": tool_name, "args": str(_redact(args))[:200], "ok": ok}])[-12:]
    def brief(self):
        with self.lock: return {"slots": {k: v for k, v in self.slots.items() if k != "planned_on"}, "recent_turns": self.turns[-8:], "recent_actions": self.actions[-6:], "last_goal": self.last_goal}
    def resolve(self, text):
        t = text
        for slot, pat, fmt in (("contact", r"\b(him|her|them)\b", "{}"), ("file", r"\b(that|this|the) (file|document|pdf|one)\b", '"{}"'), ("folder", r"\b(that|this|same) (folder|place|location)\b", '"{}"')):
            v = self.get(slot)
            if v and re.search(pat, t, re.I): t = re.sub(pat, fmt.format(v), t, flags=re.I)
        return t
MEM = Memory()

class LongTermMemory:
    """SQLite-backed persistent memory. kinds: preference | rule | fact | workflow | lesson | context. Never stores secrets."""
    KINDS = ("preference", "rule", "fact", "workflow", "lesson", "context")
    _POL = (("always", "never"), ("on", "off"), ("enable", "disable"), ("enabled", "disabled"), ("dark", "light"), ("mute", "unmute"), ("open", "close"), ("do", "dont"))
    def __init__(self, path):
        self.lock = threading.RLock(); self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10, isolation_level=None)
        with self.lock:
            try: self.db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error: pass
            self.db.executescript("""CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY, kind TEXT, key TEXT, content TEXT, tags TEXT, weight REAL, hits INTEGER,
                created REAL, updated REAL, last_used REAL, source TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ts REAL, session TEXT, trigger TEXT, goal TEXT, plan TEXT, results TEXT, status TEXT, context TEXT);
            CREATE TABLE IF NOT EXISTS tool_stats(tool TEXT, action TEXT, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, last_error TEXT, updated REAL, PRIMARY KEY(tool, action));
            CREATE INDEX IF NOT EXISTS ix_mem_kind ON memories(kind, active);""")
    @staticmethod
    def _tokens(t): return set(re.findall(r"[a-z0-9]{3,}", _norm(t)))
    def _conflicts(self, kind, content, rows):
        if kind not in ("rule", "preference"): return []
        polar = {w for p in self._POL for w in p}; a = set(_norm(content).replace("'", "").split()); ca = a - polar; out = []
        for mid, c in rows:
            b = set(_norm(c).replace("'", "").split()); cb = b - polar
            if not ca or not cb or len(ca & cb) / len(ca | cb) < 0.6: continue
            if any((x in a and y in b) or (y in a and x in b) for x, y in self._POL): out.append(mid)
        return out
    def remember(self, kind, content, key=None, tags=(), weight=1.0, source="user"):
        content = " ".join(str(content).split())[:1500]
        if not content or kind not in self.KINDS: return None
        if _has_secret(content) or (key and _SECRET_KEYS.search(key)): log.warning("memory: refused to store secret-like content"); return None
        now = time.time(); tags_s = ",".join(sorted({_norm(t) for t in tags if t}))
        with self.lock:
            if key:
                row = self.db.execute("SELECT id, content FROM memories WHERE kind=? AND key=? AND active=1", (kind, key)).fetchone()
                if row:
                    if row[1] == content: self.db.execute("UPDATE memories SET hits=hits+1, updated=?, weight=MIN(3.0, weight+0.2) WHERE id=?", (now, row[0])); return row[0]
                    self.db.execute("UPDATE memories SET active=0, updated=? WHERE id=?", (now, row[0]))   # superseded by newer value
            else:
                rows = self.db.execute("SELECT id, content FROM memories WHERE kind=? AND active=1", (kind,)).fetchall(); n = _norm(content)
                for mid, c in rows:
                    if _norm(c) == n or difflib.SequenceMatcher(None, _norm(c), n).ratio() >= 0.9:
                        self.db.execute("UPDATE memories SET hits=hits+1, updated=?, weight=MIN(3.0, weight+0.2) WHERE id=?", (now, mid)); return mid
                for mid in self._conflicts(kind, content, rows): self.db.execute("UPDATE memories SET active=0, updated=? WHERE id=?", (now, mid)); log.info("memory: retired contradicting #%s", mid)
            cur = self.db.execute("INSERT INTO memories(kind,key,content,tags,weight,hits,created,updated,last_used,source,active) VALUES(?,?,?,?,?,0,?,?,?,?,1)",
                                  (kind, key, content, tags_s, float(weight), now, now, now, source)); mid = cur.lastrowid
        BUS.emit("memory_update", key=f"ltm:{kind}", value=content[:200]); log.info("memory: learned %s #%s: %s", kind, mid, content[:80]); return mid
    def recall(self, query, limit=12, kinds=None):
        q = self._tokens(query); now = time.time(); scored = []
        with self.lock: rows = self.db.execute("SELECT id, kind, key, content, tags, weight, hits, updated FROM memories WHERE active=1" + (f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""), tuple(kinds or ())).fetchall()
        for mid, kind, key, content, tags, weight, hits, updated in rows:
            t = self._tokens(f"{content} {tags or ''} {key or ''}"); overlap = len(q & t) / (len(q) + 1e-9) if q else 0.0
            if overlap == 0 and kind in ("fact", "lesson", "workflow", "context"): continue
            s = overlap * 3 + weight * 0.5 + 0.3 * math.log1p(hits) + 1.0 / (1 + (now - updated) / 86400 / 30) + (1.0 if kind in ("rule", "preference") else 0.0)
            scored.append((s, mid, kind, key, content))
        scored.sort(reverse=True); top = scored[:limit]
        if top:
            with self.lock: self.db.executemany("UPDATE memories SET last_used=? WHERE id=?", [(now, x[1]) for x in top])
        return [{"id": mid, "kind": kind, "key": key, "content": content[:400]} for _, mid, kind, key, content in top]
    def list(self, kinds=None, limit=20):
        with self.lock: rows = self.db.execute("SELECT id, kind, key, content, weight, hits, updated FROM memories WHERE active=1" + (f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else "") + " ORDER BY updated DESC LIMIT ?", tuple(kinds or ()) + (int(limit),)).fetchall()
        return [{"id": r[0], "kind": r[1], "key": r[2], "content": r[3][:300], "weight": r[4], "hits": r[5], "updated": datetime.fromtimestamp(r[6]).isoformat(timespec="minutes")} for r in rows]
    def forget(self, query=None, memory_id=None):
        now = time.time()
        with self.lock:
            if memory_id: return self.db.execute("UPDATE memories SET active=0, updated=? WHERE id=?", (now, int(memory_id))).rowcount
            n, ids = _norm(query or ""), []
            for mid, c in self.db.execute("SELECT id, content FROM memories WHERE active=1"):
                if n and (n in _norm(c) or difflib.SequenceMatcher(None, _norm(c), n).ratio() >= 0.6): ids.append(mid)
            if ids: self.db.executemany("UPDATE memories SET active=0, updated=? WHERE id=?", [(now, i) for i in ids])
        return len(ids)
    def demote_workflow(self, goal):
        with self.lock: self.db.execute("UPDATE memories SET weight=weight-0.7, active=CASE WHEN weight-0.7<0.3 THEN 0 ELSE 1 END, updated=? WHERE kind='workflow' AND key=?", (time.time(), "wf:" + _norm(goal)[:120]))
    def find_workflow(self, goal):
        key = "wf:" + _norm(goal)[:120]
        with self.lock: rows = self.db.execute("SELECT key, content, hits, weight FROM memories WHERE kind='workflow' AND active=1 AND weight>=0.8").fetchall()
        best = None
        for k, c, hits, w in rows:
            r = 1.0 if k == key else difflib.SequenceMatcher(None, k, key).ratio()
            if r >= 0.88 and (best is None or r > best[0]): best = (r, k, c, hits, w)
        if not best: return None
        try: return {"goal_key": best[1][3:], "plan": json.loads(best[2]), "uses": best[3], "weight": best[4], "match": round(best[0], 2)}
        except Exception: return None
    def tool_stat(self, tool_name, action, ok, error=None):
        with self.lock: self.db.execute("INSERT INTO tool_stats(tool,action,ok,fail,last_error,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(tool,action) DO UPDATE SET ok=ok+excluded.ok, fail=fail+excluded.fail, last_error=COALESCE(excluded.last_error,last_error), updated=excluded.updated",
                                        (tool_name, action or "", 1 if ok else 0, 0 if ok else 1, (str(error)[:200] if error else None), time.time()))
    def unreliable(self):
        with self.lock: rows = self.db.execute("SELECT tool, action, ok, fail, last_error FROM tool_stats WHERE fail>=3 AND fail*1.0/(ok+fail) > 0.6").fetchall()
        return [{"tool": r[0], "action": r[1], "ok": r[2], "fail": r[3], "last_error": r[4]} for r in rows]
    def log_history(self, session, trigger, goal, plan, results, status, context):
        with self.lock: self.db.execute("INSERT INTO history(ts,session,trigger,goal,plan,results,status,context) VALUES(?,?,?,?,?,?,?,?)",
                                        (time.time(), session, trigger, _redact(goal)[:500], json.dumps(plan)[:4000], json.dumps(results)[:4000], status, json.dumps(context)[:800]))
    def recent_history(self, limit=6):
        with self.lock: rows = self.db.execute("SELECT ts, goal, status, context FROM history ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for ts, goal, status, ctx in rows:
            try: app = (json.loads(ctx or "{}") or {}).get("app")
            except Exception: app = None
            out.append({"when": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), "goal": goal[:120], "status": status, "app": app})
        return out
    def cleanup(self):
        now, stale = time.time(), STALE_DAYS * 86400
        with self.lock:
            a = self.db.execute("UPDATE memories SET active=0, updated=? WHERE active=1 AND kind IN ('fact','lesson','workflow','context') AND hits=0 AND ?-created > ?", (now, now, stale)).rowcount
            b = self.db.execute("UPDATE memories SET active=0, updated=? WHERE active=1 AND weight < 0.3", (now,)).rowcount
            c = self.db.execute("DELETE FROM memories WHERE active=0 AND ?-updated > ?", (now, 30 * 86400)).rowcount
            for kind in self.KINDS:
                ids = [r[0] for r in self.db.execute("SELECT id FROM memories WHERE kind=? AND active=1 ORDER BY (weight + 0.3*hits) DESC, updated DESC", (kind,)).fetchall()][500:]
                if ids: self.db.executemany("UPDATE memories SET active=0, updated=? WHERE id=?", [(now, i) for i in ids])
            self.db.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 5000)")
        if a or b or c: log.info("memory cleanup: retired %d stale, %d low-weight, purged %d", a, b, c)
        return {"retired_stale": a, "retired_low_weight": b, "purged": c}
    def stats(self):
        with self.lock:
            kinds = dict(self.db.execute("SELECT kind, COUNT(*) FROM memories WHERE active=1 GROUP BY kind").fetchall()); hist = self.db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        return {"active_by_kind": kinds, "history_rows": hist, "db": str(self.path), "size_kb": round(self.path.stat().st_size / 1024, 1) if self.path.exists() else 0}
    def close(self):
        with self.lock:
            try: self.db.close()
            except Exception: pass
LTM = LongTermMemory(MEMORY_DB)

class Learner:
    """Turns outcomes and user feedback into durable, non-code improvements (memories, rules, workflow reuse, tool reliability)."""
    CORRECT = re.compile(r"^(no[, ]|nope|wrong|not that|that's wrong|thats wrong|don't do that|dont do that|undo that|that was wrong)", re.I)
    DURABLE = re.compile(r"\b(from now on|always|never|remember (that|to|this|my)|i prefer|by default|my (name|favorite|favourite|default|usual)|call me|do not ever|don't ever)\b", re.I)
    ONGOING = re.compile(r"\b(help me|guide me|walk me|workflow|keep|as i go|follow (me|along)|until|whenever|while i|step by step|assist me|throughout)\b", re.I)
    def __init__(self, ltm: LongTermMemory): self.ltm = ltm; self.last: Optional[dict] = None
    def capture_instruction(self, text):
        if _has_secret(text) or not self.DURABLE.search(text): return None
        kind = "preference" if re.search(r"prefer|favorite|favourite|call me|my (name|default|usual)", text, re.I) else "rule"
        return self.ltm.remember(kind, text.strip(), tags=("user-stated",))
    def is_correction(self, text): return bool(self.CORRECT.search(text.strip()))
    def apply_correction(self, text):
        if not self.last: return None
        goal = self.last["goal"]; self.ltm.demote_workflow(goal)
        return self.ltm.remember("lesson", f"When asked '{goal[:120]}', the previous approach was wrong. User said: {text[:200]}", tags=("correction",), weight=1.5)
    @staticmethod
    def _trim(results): return [{"tool": r.get("tool"), "ok": r.get("ok"), "error": (r.get("error") or "")[:200] or None,
                                 "output": {k: str(v)[:160] for k, v in (r.get("output") or {}).items() if k in ("method", "requested", "actual", "verified", "note")}} for r in results]
    def after_task(self, goal, steps, results, trigger, session_id, context, meta):
        ok = bool(results) and all(r.get("ok") for r in results); plan = _redact([s.dump() for s in steps]); ctx = {k: (context or {}).get(k) for k in ("app", "host", "title")}
        status = "completed" if ok else ("cancelled" if any(r.get("cancelled") for r in results) else "completed_with_errors")
        try:
            self.ltm.log_history(session_id, trigger, goal, plan, self._trim(results), status, ctx)
            for r in results:
                if r.get("skipped"): continue
                args = next((s.args for s in steps if s.tool == r.get("tool")), {}); self.ltm.tool_stat(r.get("tool"), _norm(args.get("action", "")), r.get("ok"), r.get("error"))
            if ok and steps and trigger == "user" and all(REG[s.tool].level < Level.CONFIRM for s in steps if s.tool in REG):
                self.ltm.remember("workflow", json.dumps(plan)[:1500], key="wf:" + _norm(goal)[:120], tags=("workflow",), source="learned")
            if ok and ctx.get("app"): self.ltm.remember("context", f"Goal '{goal[:80]}' was done in {ctx['app']}" + (f" at {ctx['host']}" if ctx.get("host") else ""), key="ctx:" + _norm(goal)[:100], weight=0.6, source="learned")
            for item in (meta or {}).get("learn") or []:
                if isinstance(item, dict) and item.get("kind") in ("preference", "rule", "fact") and item.get("content"): self.ltm.remember(item["kind"], str(item["content"]), tags=("llm",), source="llm")
        except Exception as e: log.error("learner: %s", e)
        self.last = {"goal": goal, "ok": ok}; return status
LEARN = Learner(LTM)

# ============================ 6. TOOL REGISTRY ============================
@dataclass
class Tool:
    name: str; description: str; level: Level; schema: Dict[str, str]; fn: Callable
    confirm_prompt: str = ""; critical: bool = True; requires: Tuple[str, ...] = (); category: str = "general"
    confirm_actions: Tuple[str, ...] = (); verify: Optional[Callable[[dict, dict], bool]] = None
    def available(self):
        for c in self.requires:
            if not cap_ok(c): return False, f"{c}: {CAPS.get(c)}"
        return True, ""
    def effective_level(self, args): return Level.CONFIRM if _norm(args.get("action", "")) in self.confirm_actions else self.level
    def coerce(self, args):
        out = {}
        for k, spec in self.schema.items():
            if args.get(k) is None: continue
            v, typ = args[k], spec.rstrip("!")
            try:
                if typ == "int": v = int(float(v))
                elif typ == "float": v = float(v)
                elif typ == "bool": v = v if isinstance(v, bool) else _norm(v) in {"true", "yes", "1", "on", "enable", "enabled"}
                elif typ == "list" and not isinstance(v, list): s = str(v).strip(); v = json.loads(s) if s.startswith("[") else [x.strip() for x in s.split(",") if x.strip()]
                elif typ == "str": v = str(v)
            except Exception: raise PolicyViolation(f"{self.name}: '{k}' must be {typ}")
            out[k] = v
        return out
    def run(self, args):
        ok, why = self.available()
        if not ok: raise ActionFailed(f"{self.name} unavailable ({why}).", unsupported=True)
        clean = self.coerce(args or {})
        missing = [k for k, v in self.schema.items() if v.endswith("!") and k not in clean]
        if missing: raise PolicyViolation(f"{self.name}: missing args {missing}")
        out = self.fn(**clean)
        out = out if isinstance(out, dict) else {"result": out}
        if self.verify:
            try: out["verified"] = bool(self.verify(clean, out))
            except Exception as e: out["verified"] = False; out["verify_error"] = str(e)
        out.setdefault("verified", False); return out
REG: Dict[str, Tool] = {}
def tool(tool_name, desc, lvl, confirm_prompt="", critical=True, requires=(), category="general", confirm_actions=(), verify=None, **schema):
    def deco(fn):
        if tool_name in REG: raise RuntimeError(f"duplicate tool {tool_name}")
        REG[tool_name] = Tool(tool_name, desc, lvl, schema, fn, confirm_prompt, critical, tuple(requires), category, tuple(confirm_actions), verify); return fn
    return deco

# ============================ 7. CAPABILITY LAYER ============================
if IS_WINDOWS:
    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD), ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD),
                    ("dmFields", wt.DWORD), ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long), ("dmDisplayOrientation", wt.DWORD), ("dmDisplayFixedOutput", wt.DWORD),
                    ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                    ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD), ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD),
                    ("dmDisplayFlags", wt.DWORD), ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD), ("dmMediaType", wt.DWORD),
                    ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD), ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]
    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32), ("DeviceString", wt.WCHAR * 128), ("StateFlags", wt.DWORD), ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]
    U32.EnumDisplayDevicesW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wt.DWORD]
    U32.EnumDisplaySettingsW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(DEVMODEW)]
    U32.ChangeDisplaySettingsExW.argtypes = [wt.LPCWSTR, ctypes.POINTER(DEVMODEW), wt.HWND, wt.DWORD, wt.LPVOID]; U32.ChangeDisplaySettingsExW.restype = ctypes.c_long

def _displays():
    out, i = [], 0
    if not IS_WINDOWS: return out
    while True:
        dd = DISPLAY_DEVICEW(); dd.cb = ctypes.sizeof(dd)
        if not U32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0): break
        i += 1
        if not dd.StateFlags & 1: continue
        dm = DEVMODEW(); dm.dmSize = ctypes.sizeof(dm); cur = None
        if U32.EnumDisplaySettingsW(dd.DeviceName, 0xFFFFFFFF, ctypes.byref(dm)):
            cur = {"width": dm.dmPelsWidth, "height": dm.dmPelsHeight, "refresh_hz": dm.dmDisplayFrequency, "x": dm.dmPositionX, "y": dm.dmPositionY}
        out.append({"device": dd.DeviceName, "name": dd.DeviceString, "primary": bool(dd.StateFlags & 4), "current": cur})
    return out
def _modes(device):
    modes, seen, i, dm = [], set(), 0, DEVMODEW(); dm.dmSize = ctypes.sizeof(dm)
    while U32.EnumDisplaySettingsW(device, i, ctypes.byref(dm)):
        i += 1; k = (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency)
        if k not in seen and dm.dmBitsPerPel >= 32: seen.add(k); modes.append({"width": k[0], "height": k[1], "refresh_hz": k[2]})
    return modes

SETTINGS = {"home": "", "bluetooth": "bluetooth", "wifi": "network-wifi", "network": "network", "airplane": "network-airplanemode", "display": "display", "nightlight": "nightlight",
            "sound": "sound", "apps": "appsfeatures", "startup": "startupapps", "battery": "batterysaver", "power": "powersleep", "datetime": "dateandtime",
            "notifications": "notifications", "microphone": "privacy-microphone", "camera": "privacy-webcam", "update": "windowsupdate", "storage": "storagesense",
            "about": "about", "devices": "connecteddevices", "printers": "printers", "accounts": "yourinfo", "personalization": "personalization"}
_SET_ALIAS = {"bt": "bluetooth", "wireless": "wifi", "brightness": "display", "monitor": "display", "audio": "sound", "volume": "sound", "time": "datetime", "date": "datetime",
              "timezone": "datetime", "mic": "microphone", "startupapps": "startup", "airplanemode": "airplane", "flightmode": "airplane"}
def open_settings_page(page):
    need_win("Windows Settings"); key = _norm(page).replace(" ", "").replace("-", ""); key = _SET_ALIAS.get(key, key)
    if key not in SETTINGS: raise ActionFailed(f"Unknown settings page '{page}'. Known: {sorted(SETTINGS)}")
    uri = "ms-settings:" + SETTINGS[key]; os.startfile(uri); w = find_window("Settings", 8)
    if w: focus_window(w)
    MEM.set("app", "settings"); return _res("ms-settings", uri, getattr(w, "title", None), bool(w))
def click_text(text):
    if not UIA: raise ActionFailed("UI Automation unavailable.", unsupported=True)
    n, best, score = _norm(text), None, 0.0
    for e in ui_elements(8, 400):
        en = _norm(e["name"]); s = difflib.SequenceMatcher(None, en, n).ratio()
        s = 1.0 if en == n else max(s, 0.92) if n in en else s
        if s > score: best, score = e, s
    if not best or score < 0.6: raise ActionFailed(f"No element matching '{text}' on screen.")
    x, y = best["center"]; PG.click(x, y); _sleep(0.6)
    return _res("uia_click", text, best["name"], True, score=round(score, 2), at=[x, y])

class SystemControl:
    def metrics(self):
        if not psutil: raise ActionFailed("psutil missing.", unsupported=True)
        vm, disks = psutil.virtual_memory(), []
        for p in psutil.disk_partitions():
            try: u = psutil.disk_usage(p.mountpoint); disks.append({"mount": p.mountpoint, "total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1), "used_percent": u.percent})
            except Exception: pass
        return {"os": f"{OS_NAME} {platform.release()}", "host": platform.node(), "admin": is_admin(), "cpu_percent": psutil.cpu_percent(interval=0.4), "cpu_cores": psutil.cpu_count(),
                "ram_total_gb": round(vm.total / 2**30, 1), "ram_used_percent": vm.percent, "disks": disks, "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
                "battery": self.battery(), "gpu": self.gpu(), "verified": True}
    def gpu(self):
        try:
            if cap_ok("nvidia_smi"):
                r = run_cmd(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], 10)
                return [dict(zip(("name", "util_percent", "mem_used_mb", "mem_total_mb", "temp_c"), [p.strip() for p in l.split(",")])) for l in r.stdout.strip().splitlines()]
            if PS_EXE:
                data = run_ps("ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion)", 20, True) or []
                data = [data] if isinstance(data, dict) else data
                return [{"name": g.get("Name"), "vram_gb": round((g.get("AdapterRAM") or 0) / 2**30, 1), "driver": g.get("DriverVersion")} for g in data]
        except Exception as e: log.debug("gpu: %s", e)
        return []
    def battery(self):
        if not psutil: return None
        try: b = psutil.sensors_battery()
        except Exception: return None
        if not b: return None
        left = None if b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else max(0, int(b.secsleft // 60))
        return {"percent": round(b.percent), "plugged": bool(b.power_plugged), "minutes_left": left}
    # ---- volume ----
    def _endpoint(self):
        if not PYCAW: raise ActionFailed("pycaw/comtypes missing.", unsupported=True)
        from ctypes import cast, POINTER; from comtypes import CLSCTX_ALL; from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        try: ctypes.windll.ole32.CoInitialize(None)
        except Exception: pass
        dev = AudioUtilities.GetSpeakers(); return cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    def volume(self, action="get", level=None):
        a = _norm(action)
        if not PYCAW:
            if not PG: raise ActionFailed("No volume backend (pycaw or pyautogui).", unsupported=True)
            key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute", "toggle_mute": "volumemute"}.get(a)
            if not key: raise ActionFailed(f"Volume '{a}' needs pycaw.", unsupported=True)
            for _ in range(max(1, int((level or 10) / 2)) if a in ("up", "down") else 1): PG.press(key)
            return _res("media_keys", a, None, False, fallback="pycaw missing: sent media keys, level not verified")
        ep = self._endpoint(); cur = round(ep.GetMasterVolumeLevelScalar() * 100)
        if a == "get": return _res("pycaw", None, {"percent": cur, "muted": bool(ep.GetMute())}, True)
        if a in ("mute", "unmute", "toggle_mute"):
            ep.SetMute({"mute": 1, "unmute": 0}.get(a, int(not ep.GetMute())), None); return _res("pycaw", a, {"muted": bool(ep.GetMute())}, True)
        if a == "set" and level is None: raise PolicyViolation("volume set requires 'level'")
        target = {"set": level, "up": cur + (level or 10), "down": cur - (level or 10)}.get(a)
        if target is None: raise PolicyViolation(f"Unknown volume action '{action}' (get|set|up|down|mute|unmute|toggle_mute)")
        target = max(0, min(100, int(target))); ep.SetMasterVolumeLevelScalar(target / 100, None)
        if target > 0 and ep.GetMute(): ep.SetMute(0, None)
        now = round(ep.GetMasterVolumeLevelScalar() * 100); MEM.set("volume", now, quiet=True); return _res("pycaw", target, now, abs(now - target) <= 1)
    # ---- brightness ----
    def brightness(self, action="get", level=None):
        a = _norm(action)
        if SBC:
            try: cur = SBC.get_brightness(); cur = cur[0] if isinstance(cur, list) else cur
            except Exception as e: raise ActionFailed(f"Brightness read failed: {e}")
            if a == "get": return _res("sbc", None, cur, True)
            target = {"set": level, "up": cur + (level or 10), "down": cur - (level or 10)}.get(a)
            if target is None: raise PolicyViolation(f"Unknown brightness action '{action}'")
            target = max(0, min(100, int(target))); SBC.set_brightness(target); _sleep(0.4)
            now = SBC.get_brightness(); now = now[0] if isinstance(now, list) else now; return _res("sbc", target, now, abs(now - target) <= 2)
        if PS_EXE and a in ("set", "up", "down"):
            cur = int(run_ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness", 15) or 50)
            target = max(0, min(100, int({"set": level, "up": cur + (level or 10), "down": cur - (level or 10)}[a] or 0)))
            run_ps(f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1,{target})", 15); return _res("wmi", target, target, False)
        raise ActionFailed("No brightness backend (screen-brightness-control missing; external monitors often unsupported).", unsupported=True)
    # ---- wifi ----
    def wifi_status(self):
        if not cap_ok("netsh"): raise ActionFailed("netsh unavailable.", unsupported=True)
        r = run_cmd(["netsh", "wlan", "show", "interfaces"], 15, check=False); out = {"connected": False, "ssid": None, "signal": None}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(":"); k, v = k.strip().lower(), v.strip()
            if k == "state": out["connected"] = v.lower().startswith("connected")
            elif k == "ssid": out["ssid"] = v
            elif k == "signal": out["signal"] = v
        return out
    def wifi(self, action="status", ssid=None, password=None):
        a = _norm(action); need_win("Wi-Fi control")
        if a == "status": return _res("netsh", None, self.wifi_status(), True)
        if a in ("list", "scan"):
            r = run_cmd(["netsh", "wlan", "show", "networks", "mode=bssid"], 20, check=False); nets, cur = [], None
            for line in r.stdout.splitlines():
                m = re.match(r"\s*SSID \d+ : (.*)", line)
                if m: cur = {"ssid": m.group(1).strip(), "signal": None}; nets.append(cur)
                elif cur and "Signal" in line: cur["signal"] = line.split(":")[-1].strip()
            return _res("netsh", None, [n for n in nets if n["ssid"]], True)
        if a in ("on", "off", "enable", "disable"):
            state = "enable" if a in ("on", "enable") else "disable"
            try: run_cmd(["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={state}"], 20)
            except ActionFailed as e:
                if e.info.get("requires_admin"): need_admin("Enabling/disabling the Wi-Fi adapter")
                raise
            return _res("netsh", state, None, False)
        if a == "disconnect": run_cmd(["netsh", "wlan", "disconnect"], 15); ok = _poll(lambda: not self.wifi_status()["connected"], 8, 0.7); return _res("netsh", "disconnect", self.wifi_status(), bool(ok))
        if a == "connect":
            if not ssid: raise PolicyViolation("wifi connect requires 'ssid'")
            if password:
                xml = (f'<?xml version="1.0"?><WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1"><name>{html.escape(ssid)}</name><SSIDConfig><SSID><name>{html.escape(ssid)}</name></SSID></SSIDConfig>'
                       f'<connectionType>ESS</connectionType><connectionMode>auto</connectionMode><MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>'
                       f'<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{html.escape(password)}</keyMaterial></sharedKey></security></MSM></WLANProfile>')
                fd, p = tempfile.mkstemp(suffix=".xml"); os.close(fd)
                try: Path(p).write_text(xml, encoding="utf-8"); run_cmd(["netsh", "wlan", "add", "profile", f"filename={p}", "user=current"], 20)
                finally:
                    try: os.unlink(p)
                    except Exception: pass
            run_cmd(["netsh", "wlan", "connect", f"name={ssid}"], 20); ok = _poll(lambda: self.wifi_status().get("ssid") == ssid and self.wifi_status()["connected"], 20, 1.0)
            return _res("netsh", ssid, self.wifi_status(), bool(ok), note=None if ok else "Not connected yet: wrong password, out of range, or profile missing.")
        raise PolicyViolation(f"Unknown wifi action '{action}' (status|list|connect|disconnect|on|off)")
    # ---- bluetooth ----
    _RADIO_PS = """[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($t, $r) { $m = $asTaskGeneric.MakeGenericMethod($r); $n = $m.Invoke($null, @($t)); $n.Wait(-1) | Out-Null; $n.Result }
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bt = $radios | ? { $_.Kind -eq 'Bluetooth' }
if (-not $bt) { Write-Output 'NORADIO'; exit }
%s
Write-Output ($bt | Select-Object -First 1).State"""
    def bluetooth(self, action="status", device=None):
        a = _norm(action); need_win("Bluetooth control")
        if a in ("on", "off", "status"):
            body = "" if a == "status" else f"[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null; Await ($bt.SetStateAsync('{'On' if a == 'on' else 'Off'}')) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null; Start-Sleep -Milliseconds 800"
            out = run_ps(self._RADIO_PS % body, 40).strip().splitlines()[-1] if PS_EXE else "NORADIO"
            if out == "NORADIO": raise ActionFailed("No Bluetooth radio found.", unsupported=True)
            return _res("winrt_radio", a, out, a == "status" or _norm(out) == a)
        if a in ("scan", "list"):
            if not BLEAK: raise ActionFailed("bleak missing for BLE scan; use action='open' for the Settings page.", unsupported=True)
            devs = _coro(BLEAK.BleakScanner.discover(timeout=6.0), 15) or []
            return _res("bleak", None, [{"name": d.name or "?", "address": d.address} for d in devs][:40], True)
        if a in ("paired", "devices"):
            data = run_ps("ConvertTo-Json -Compress -InputObject @(Get-PnpDevice -Class Bluetooth -Status OK | Select-Object FriendlyName, InstanceId)", 30, True) or []
            data = [data] if isinstance(data, dict) else data; return _res("pnp", None, [{"name": d.get("FriendlyName"), "id": d.get("InstanceId")} for d in data], True)
        if a in ("open", "pair", "connect"): return {**open_settings_page("bluetooth"), "note": f"Pairing/connecting '{device or ''}' must be finished in Settings (Windows exposes no safe public API)."}
        raise PolicyViolation(f"Unknown bluetooth action '{action}' (status|on|off|scan|paired|open)")
    # ---- display ----
    def display(self, action="list", width=None, height=None, refresh_hz=None, device=None, orientation=None):
        a = _norm(action); need_win("Display control"); disp = _displays()
        if a == "list": return _res("win32", None, disp, True)
        tgt = next((d for d in disp if device and _norm(device) in (_norm(d["device"]), _norm(d["name"]))), None) or next((d for d in disp if d["primary"]), disp[0] if disp else None)
        if not tgt: raise ActionFailed("No active display found.")
        if a == "modes": return _res("win32", tgt["device"], _modes(tgt["device"]), True)
        if a in ("set", "resolution", "rotate"):
            dm = DEVMODEW(); dm.dmSize = ctypes.sizeof(dm); U32.EnumDisplaySettingsW(tgt["device"], 0xFFFFFFFF, ctypes.byref(dm)); fields = 0
            if a == "rotate" or orientation is not None:
                o = {"landscape": 0, "0": 0, "portrait": 1, "90": 1, "landscape_flipped": 2, "180": 2, "portrait_flipped": 3, "270": 3}.get(_norm(orientation or "portrait"))
                if o is None: raise PolicyViolation("orientation must be landscape|portrait|landscape_flipped|portrait_flipped")
                if (dm.dmDisplayOrientation in (0, 2)) != (o in (0, 2)): dm.dmPelsWidth, dm.dmPelsHeight = dm.dmPelsHeight, dm.dmPelsWidth
                dm.dmDisplayOrientation = o; fields |= 0x80 | 0x80000 | 0x100000
            if width and height:
                modes = _modes(tgt["device"]); m = [x for x in modes if x["width"] == int(width) and x["height"] == int(height)]
                if not m: raise ActionFailed(f"{width}x{height} unsupported. Supported: {sorted({(x['width'], x['height']) for x in modes}, reverse=True)[:12]}")
                dm.dmPelsWidth, dm.dmPelsHeight = int(width), int(height); dm.dmDisplayFrequency = int(refresh_hz or max(x["refresh_hz"] for x in m)); fields |= 0x80000 | 0x100000 | 0x400000
            elif refresh_hz: dm.dmDisplayFrequency = int(refresh_hz); fields |= 0x400000
            if not fields: raise PolicyViolation("display set requires width+height, refresh_hz or orientation")
            dm.dmFields = fields; rc = U32.ChangeDisplaySettingsExW(tgt["device"], ctypes.byref(dm), None, 0, None)
            if rc != 0: raise ActionFailed(f"ChangeDisplaySettingsEx failed (code {rc}).")
            _sleep(1.0); cur = next((d for d in _displays() if d["device"] == tgt["device"]), {}).get("current")
            ok = (not width or (cur and cur["width"] == int(width) and cur["height"] == int(height))) and (not refresh_hz or (cur and cur["refresh_hz"] == int(refresh_hz)))
            return _res("win32_changedisplaysettings", {"width": width, "height": height, "refresh_hz": refresh_hz, "orientation": orientation}, cur, bool(ok))
        if a == "nightlight": return open_settings_page("nightlight")
        raise PolicyViolation(f"Unknown display action '{action}' (list|modes|set|rotate|nightlight)")
    # ---- power / session ----
    def power(self, action, delay_sec=0):
        a = _norm(action); need_win("Power control"); d = str(max(0, int(delay_sec or 0)))
        if a == "lock": ctypes.windll.user32.LockWorkStation(); return _res("LockWorkStation", a, a, True)
        if a == "sleep": run_ps("Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)", 15); return _res("SetSuspendState", a, a, False)
        if a == "hibernate": run_cmd(["shutdown", "/h"], 15); return _res("shutdown.exe", a, a, False)
        if a in ("shutdown", "restart"): run_cmd(["shutdown", "/s" if a == "shutdown" else "/r", "/t", d], 15); return _res("shutdown.exe", a, f"{a} in {d}s", False)
        if a in ("cancel", "abort"): run_cmd(["shutdown", "/a"], 15, check=False); return _res("shutdown.exe", a, "cancelled", False)
        if a in ("signout", "logoff", "log off"): run_cmd(["shutdown", "/l"], 15); return _res("shutdown.exe", a, a, False)
        raise PolicyViolation(f"Unknown power action '{action}' (lock|sleep|hibernate|shutdown|restart|cancel|signout)")
    def power_plan(self, action="get", plan=None):
        a = _norm(action); need_win("Power plans"); r = run_cmd(["powercfg", "/list"], 15); plans = []
        for line in r.stdout.splitlines():
            m = re.search(r"GUID:\s*([0-9a-f-]{36})\s*\((.*?)\)\s*(\*)?", line, re.I)
            if m: plans.append({"guid": m.group(1), "name": m.group(2), "active": bool(m.group(3))})
        if a in ("get", "list"): return _res("powercfg", None, plans, True)
        if a == "set":
            n = _norm(plan or ""); tgt = next((p for p in plans if n and (n in _norm(p["name"]) or n == p["guid"])), None)
            if not tgt: raise ActionFailed(f"No power plan matching '{plan}'. Available: {[p['name'] for p in plans]}")
            run_cmd(["powercfg", "/setactive", tgt["guid"]], 15); now = self.power_plan("get")["actual"]; return _res("powercfg", tgt["name"], next((p["name"] for p in now if p["active"]), None), any(p["active"] and p["guid"] == tgt["guid"] for p in now))
        raise PolicyViolation("power_plan action must be get|set")
    def timezone(self, action="get", zone=None):
        a = _norm(action)
        if not cap_ok("tzutil"): raise ActionFailed("tzutil unavailable.", unsupported=True)
        cur = run_cmd(["tzutil", "/g"], 10).stdout.strip()
        if a == "get": return _res("tzutil", None, {"timezone": cur, "local_time": datetime.now().isoformat(timespec="seconds")}, True)
        if a == "list": return _res("tzutil", None, [l.strip() for l in run_cmd(["tzutil", "/l"], 15).stdout.splitlines() if "Time" in l and "(" not in l][:200], True)
        if a == "set":
            if not zone: raise PolicyViolation("timezone set requires 'zone'")
            zones = [l.strip() for l in run_cmd(["tzutil", "/l"], 15).stdout.splitlines() if l.strip() and "(" not in l]
            tgt = next((z for z in zones if _norm(z) == _norm(zone)), None) or next((z for z in zones if _norm(zone) in _norm(z)), None)
            if not tgt: raise ActionFailed(f"No timezone matching '{zone}'.")
            try: run_cmd(["tzutil", "/s", tgt], 10)
            except ActionFailed as e:
                if e.info.get("requires_admin"): need_admin("Changing the time zone")
                raise
            now = run_cmd(["tzutil", "/g"], 10).stdout.strip(); return _res("tzutil", tgt, now, now == tgt)
        raise PolicyViolation("timezone action must be get|list|set")
    # ---- processes ----
    def processes(self, action="list", name=None, pid=None, limit=25):
        if not psutil: raise ActionFailed("psutil missing.", unsupported=True)
        a = _norm(action)
        if a in ("list", "top"):
            procs = []
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try: procs.append({"pid": p.info["pid"], "name": p.info["name"], "mem_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 2**20, 1)})
                except Exception: pass
            procs.sort(key=lambda x: -x["mem_mb"]); return _res("psutil", None, procs[: int(limit or 25)], True)
        if a in ("kill", "end", "stop", "terminate"):
            n, killed = _norm(name or ""), []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if (pid and p.info["pid"] == int(pid)) or (n and _norm(p.info["name"]) in (n, n + ".exe")): p.terminate(); killed.append({"pid": p.info["pid"], "name": p.info["name"]})
                except Exception: pass
            if not killed: raise ActionFailed(f"No process matching {name or pid}.")
            psutil.wait_procs([psutil.Process(k["pid"]) for k in killed if psutil.pid_exists(k["pid"])], timeout=5)
            return _res("psutil", name or pid, killed, all(not psutil.pid_exists(k["pid"]) for k in killed))
        raise PolicyViolation("processes action must be list|kill")
    def startup_apps(self):
        need_win("Startup apps"); data = run_ps("ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location)", 30, True) or []
        return _res("wmi", None, [data] if isinstance(data, dict) else data, True)
    def installed_apps(self, query=None, limit=40):
        APPS.load_start_apps(); names = sorted(APPS._start_apps); q = _norm(query or "")
        return _res("startapps", query, [n for n in names if not q or q in _norm(n)][: int(limit or 40)], True)
SYS = SystemControl()

class FileOps:
    IMG = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    @staticmethod
    def path(p, must_exist=False):
        s = str(p or "").strip().strip('"'); low = _norm(s)
        known = {"desktop": Path.home() / "Desktop", "downloads": Path.home() / "Downloads", "documents": Path.home() / "Documents", "pictures": Path.home() / "Pictures",
                 "home": Path.home(), "workspace": ROOT, "music": Path.home() / "Music", "videos": Path.home() / "Videos"}
        pth = known.get(low) or (ROOT / s if s and not Path(s).expanduser().is_absolute() and not s.startswith(("~", ".")) else Path(s).expanduser())
        pth = pth.resolve()
        if must_exist and not pth.exists(): raise ActionFailed(f"Path not found: {pth}")
        return pth
    def list(self, path=".", pattern="*", limit=80):
        d = self.path(path, True)
        if d.is_file(): return {"path": str(d), "entries": [{"name": d.name, "size": d.stat().st_size, "dir": False}], "verified": True}
        ents = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), _norm(x.name)))
        out = [{"name": e.name, "dir": e.is_dir(), "size": (e.stat().st_size if e.is_file() else None)} for e in ents if fnmatch.fnmatch(e.name.lower(), pattern.lower())][: int(limit or 80)]
        MEM.set("folder", str(d)); return {"path": str(d), "entries": out, "verified": True}
    def search(self, name, root=None, limit=30, timeout=20):
        base = self.path(root or "home", True); pat, out, end = name.lower(), [], time.time() + float(timeout)
        skip = {"node_modules", ".git", "appdata", "$recycle.bin", "windows", "program files", "program files (x86)", ".cache", "__pycache__"}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if _norm(x) not in skip and not x.startswith(".")]
            for f in filenames:
                if fnmatch.fnmatch(f.lower(), pat) or pat in f.lower(): out.append(str(Path(dirpath) / f))
                if len(out) >= int(limit): break
            if len(out) >= int(limit) or time.time() > end: break
        if out: MEM.set("file", out[0])
        return {"query": name, "root": str(base), "matches": out, "truncated": time.time() > end, "verified": True}
    def read(self, path, max_chars=6000):
        p = self.path(path, True)
        if p.suffix.lower() == ".pdf":
            try: import pypdf; txt = "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(str(p)).pages[:20])
            except ImportError: raise ActionFailed("pypdf missing for PDF reading (pip install pypdf).", unsupported=True)
        elif p.suffix.lower() in self.IMG and OCR:
            from PIL import Image; txt = OCR.image_to_string(Image.open(p))
        else: txt = p.read_text("utf-8", errors="replace")
        MEM.set("file", str(p)); return {"path": str(p), "chars": len(txt), "content": txt[: int(max_chars)], "verified": True}
    def write(self, path, content, append=False):
        p = self.path(path); p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f: f.write(str(content))
        MEM.set("file", str(p)); return _res("write", str(p), p.stat().st_size, p.exists(), appended=bool(append))
    def mkdir(self, path): p = self.path(path); p.mkdir(parents=True, exist_ok=True); MEM.set("folder", str(p)); return _res("mkdir", str(p), str(p), p.is_dir())
    def move(self, src, dst, copy=False):
        s, d = self.path(src, True), self.path(dst)
        if d.is_dir() or str(dst).endswith(("/", "\\")): d.mkdir(parents=True, exist_ok=True); d = d / s.name
        d.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if s.is_dir() else shutil.copy2)(s, d) if copy else shutil.move(str(s), str(d))
        MEM.set("file", str(d)); return _res("copy" if copy else "move", str(s), str(d), d.exists() and (copy or not s.exists()))
    def delete(self, path):
        p = self.path(path, True)
        if p in (Path.home().resolve(), ROOT, Path(p.anchor)): raise PolicyViolation(f"Refusing to delete {p}")
        if S2T: S2T.send2trash(str(p)); method = "recycle_bin"
        else: (shutil.rmtree if p.is_dir() else os.remove)(p); method = "permanent"
        return _res(method, str(p), None, not p.exists())
    def rename(self, path, new_name):
        p = self.path(path, True); d = p.with_name(str(new_name)); p.rename(d); MEM.set("file", str(d)); return _res("rename", str(p), str(d), d.exists())
    def open(self, path):
        p = self.path(path, True); os.startfile(str(p)) if IS_WINDOWS else subprocess.Popen(["xdg-open", str(p)]); MEM.set("file" if p.is_file() else "folder", str(p)); return _res("startfile", str(p), str(p), False)
    def zip(self, path, dest=None):
        p = self.path(path, True); d = self.path(dest) if dest else p.with_suffix(".zip"); out = shutil.make_archive(str(d.with_suffix("")), "zip", root_dir=p if p.is_dir() else p.parent, base_dir=None if p.is_dir() else p.name)
        return _res("zip", str(p), out, Path(out).exists())
FILES = FileOps()

# ============================ 8. TOOLS ============================
def _v_app_open(args, out): return bool(find_window(APPS.window_hint(APPS.canonical(args.get("name", ""))), 10)) if _norm(args.get("action", "open")) in ("open", "launch", "focus", "switch") else out.get("verified", False)
@tool("app", "Open/launch, focus/switch to, or close a desktop application by name (e.g. 'chrome', 'vs code', 'whatsapp').", Level.LOW, category="apps",
      requires=(), confirm_actions=("close_all",), verify=_v_app_open, action="str!", name="str!")
def t_app(action, name):
    a, key = _norm(action), APPS.canonical(name); hint = APPS.window_hint(key)
    if a in ("open", "launch", "start", "run"):
        w = find_window(hint, 0.5)
        if w and focus_window(w): MEM.set("app", key); return _res("focus_existing", name, w.title, True)
        info = APPS.launch(name); w = find_window(hint, 15); MEM.set("app", key)
        if w: focus_window(w)
        return _res(info["method"], name, getattr(w, "title", None), bool(w), note=None if w else "Launched but no window detected yet.", target=info.get("target"))
    if a in ("focus", "switch", "activate", "show"):
        w = find_window(hint, 3)
        if not w: raise ActionFailed(f"No open window for '{name}'. Use action='open'.")
        focus_window(w); MEM.set("app", key); return _res("focus", name, w.title, _norm(hint) in _norm(foreground_title()))
    if a in ("close", "quit", "exit", "close_all"):
        closed = []
        if GW:
            for w in GW.getAllWindows():
                if w.title and _norm(hint) in w.title.lower():
                    try: w.close(); closed.append(w.title)
                    except Exception: pass
                    if a != "close_all": break
        if not closed and psutil:
            exe = (APPS.EXE_HINTS.get(key) or [f"{key}.exe"])[0]
            for p in psutil.process_iter(["name"]):
                try:
                    if _norm(p.info["name"]) == _norm(exe): p.terminate(); closed.append(p.info["name"])
                except Exception: pass
        if not closed: raise ActionFailed(f"'{name}' does not appear to be running.")
        _sleep(0.8); return _res("close", name, closed, not find_window(hint, 0.3))
    raise PolicyViolation("app action must be open|focus|close")

@tool("window", "List, minimize, maximize, restore, close, move or snap the active/named window. action: list|minimize|maximize|restore|close|snap_left|snap_right|minimize_all", Level.LOW, category="window", requires=("window_control",), action="str!", title="str", monitor="int")
def t_window(action, title=None, monitor=None):
    a = _norm(action)
    if a == "list": return {"windows": [{"title": w.title, "active": w.isActive, "minimized": w.isMinimized} for w in GW.getAllWindows() if w.title and w.width > 0][:40], "foreground": foreground_title(), "verified": True}
    if a in ("minimize_all", "show_desktop"): PG.hotkey("win", "d"); return _res("hotkey", a, None, False)
    w = find_window(title, 3) if title else (GW.getActiveWindow() if GW else None)
    if not w: raise ActionFailed(f"Window '{title or 'active'}' not found.")
    if a == "minimize": w.minimize()
    elif a == "maximize": focus_window(w); w.maximize()
    elif a == "restore": w.restore(); focus_window(w)
    elif a == "close": w.close()
    elif a in ("snap_left", "snap_right"): focus_window(w); PG.hotkey("win", "left" if a == "snap_left" else "right"); _sleep(0.4); PG.press("esc")
    elif a in ("focus", "activate"): focus_window(w)
    else: raise PolicyViolation(f"Unknown window action '{action}'")
    _sleep(0.5); state = {"minimize": w.isMinimized, "maximize": w.isMaximized, "close": not find_window(w.title, 0.3)}.get(a, True)
    return _res("pygetwindow", a, w.title, bool(state))

def _v_typed_into_window(args, out):
    """After an input step aimed at a target app, the typed text must have actually landed in that
    app's window — typing blind into the foreground app is how plans 'complete' with nothing done."""
    if _norm(args.get("action", "")) not in ("type", "write", "hotkey", "press"):
        return out.get("verified", False)
    if not args.get("target"):
        return out.get("verified", False)
    hint = APPS.window_hint(APPS.canonical(args["target"]))
    fg = _norm(foreground_title())
    return bool(hint) and _norm(hint) in fg or _norm(APPS.canonical(args["target"])) in fg


@tool("input", "Keyboard/mouse: type text, press keys/hotkeys (e.g. 'ctrl+s'), click a visible UI element by its label, click x,y, scroll, or press enter. action: type|hotkey|press|click_text|click|scroll|paste. When action='type' and a target app is given, the app is focused first — always pass target.", Level.LOW,
      category="input", requires=("desktop_input",), verify=_v_typed_into_window, action="str!", text="str", keys="str", x="int", y="int", amount="int", target="str")
def t_input(action, text=None, keys=None, x=None, y=None, amount=None, target=None):
    a = _norm(action)
    focused_app = None
    if target:
        w = find_window(APPS.window_hint(APPS.canonical(target)), 3)
        if not w or not focus_window(w): raise ActionFailed(f"Target window '{target}' not found for input.", stale=True)
        focused_app = target
    elif a in ("type", "write", "hotkey", "press"):
        raise PolicyViolation("Typing without a 'target' app is disabled: the keystrokes would land in whatever window the user has open. Pass target='<app>'.")
    if a in ("type", "write"):
        if text is None: raise PolicyViolation("type requires 'text'")
        if CLIP and (len(text) > 60 or not text.isascii()): CLIP.copy(text); PG.hotkey("ctrl", "v"); m = "paste"
        else: PG.write(text, interval=0.015); m = "type"
        _sleep(0.4)
        return _res(m, text[:80], None, False, chars=len(text), typed_into=focused_app or foreground_title())
    if a == "paste": PG.hotkey("ctrl", "v"); return _res("hotkey", "ctrl+v", None, False)
    if a in ("hotkey", "press", "key", "keys", "enter"):
        combo = [k.strip().lower() for k in re.split(r"[+\s]+", (keys or text or ("enter" if a == "enter" else "")).strip()) if k.strip()]
        if not combo: raise PolicyViolation("hotkey requires 'keys'")
        PG.hotkey(*combo) if len(combo) > 1 else PG.press(combo[0]); _sleep(0.3); return _res("hotkey", "+".join(combo), None, False)
    if a in ("click_text", "click_element", "click_button"): return click_text(text or keys or "")
    if a in ("click", "double_click", "right_click"):
        if x is None or y is None: raise PolicyViolation("click requires x and y")
        {"click": PG.click, "double_click": PG.doubleClick, "right_click": PG.rightClick}[a](int(x), int(y)); return _res("mouse", a, [x, y], False)
    if a == "scroll": PG.scroll(int(amount if amount is not None else -500)); return _res("scroll", amount, None, False)
    raise PolicyViolation(f"Unknown input action '{action}'")

def _v_code_file(args, out):
    return bool(out.get("actual")) and Path(out["actual"]).exists()

# ---------------------------------------------------------------------------
# Human-style typing for the code tool.
# Default: type the file char-by-char into the editor like a human (configurable
# speed).  "single go" / instant mode: write the file programmatically (old way).
# ---------------------------------------------------------------------------
SARA_TYPE_SPEED = float(os.environ.get("SARA_TYPE_SPEED", "65"))   # words per minute
SARA_TYPE_CHUNK = int(os.environ.get("SARA_TYPE_CHUNK", "4"))      # chars per keystroke burst
SARA_TYPE_CAL = float(os.environ.get("SARA_TYPE_CAL", "1.0"))      # speed multiplier (1.0 = wpm is honoured as-is)

def _type_human(text, wpm, filename, step_index=0):
    """Type `text` into the focused editor window like a human.
    Per newline: Escape (dismiss any suggestion popup) -> Enter -> Home
    (cancel auto-indent), then the line's own indentation is typed explicitly.
    Pacing is schedule-based: each burst sleeps until an absolute per-char
    schedule, so jitter, hesitations and Windows sleep granularity cannot
    accumulate drift — the effective wpm stays true to the configured speed.
    Runs on a worker thread; honours cancel (emergency stop) and pause."""
    text = text.replace("\t", "    ").replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    total = len(text)
    st = {"i": 0, "error": None}
    per_char = 60.0 / (max(20.0, float(wpm)) * 5.0) / max(0.2, SARA_TYPE_CAL)
    t_start = time.time()
    def worker():
        try:
            while st["i"] < total:
                if EXEC.cancel.is_set(): raise ActionFailed("Typing cancelled (emergency stop).", cancelled=True)
                while AGENT.paused:
                    if EXEC.cancel.is_set(): raise ActionFailed("Typing cancelled (emergency stop).", cancelled=True)
                    _sleep(0.4); t_start += 0.4   # pause shifts the whole schedule
                chunk = text[st["i"]:st["i"] + max(1, SARA_TYPE_CHUNK)]
                if PG is None: raise ActionFailed("desktop_input capability unavailable; cannot type.", offline=True)
                wait = t_start + st["i"] * per_char - time.time()   # time until this burst is due
                if wait > 0:
                    if chunk == "\n": wait += per_char * 1.5      # a newline reads longer, like a real typist
                    if random.random() < 0.03: wait += random.uniform(0.3, 1.1)   # brief hesitation; schedule catches up later
                    _sleep(wait)
                if chunk == "\n":
                    PG.press("escape"); _sleep(0.03)
                    PG.press("enter"); _sleep(0.05)
                    PG.press("home")            # neutralise VS Code auto-indent
                else:
                    PG.typewrite(chunk, interval=0.0)
                st["i"] += len(chunk)
                if st["i"] % 60 < max(1, SARA_TYPE_CHUNK):   # progress ~every 60 chars
                    BUS.emit("step_progress", tool="code", step=step_index, typed=st["i"], total=total,
                             path=filename, wpm=wpm, eta_s=round(max(0.0, (time.time() - t_start) / st["i"] * (total - st["i"])), 1))
        except BaseException as e:
            st["error"] = e
    th = threading.Thread(target=worker, daemon=True, name="sara-type")
    th.start()
    expected = total * per_char
    deadline = time.time() + expected * 2.5 + 180.0
    while th.is_alive() and time.time() < deadline: _sleep(0.25)
    if th.is_alive():
        EXEC.cancel.set()   # abort typing loop; reported as a clean failure (no destructive retry)
        raise ActionFailed(f"Human typing exceeded its {expected * 2.5 + 180.0:.0f}s safety deadline and was aborted.")
    if st["error"] is not None: raise st["error"]
    BUS.emit("step_progress", tool="code", step=step_index, typed=total, total=total, path=filename, done=True)
    return round(time.time() - t_start, 1)


@tool("code", "Create a source-code file from a description or literal content and open it in an editor (VS Code when present). By default the code is typed into the editor character-by-character like a human at a configurable speed (wpm). Pass mode='instant' (or the user says 'single go') to write the file programmatically instead. language: java|python|c|cpp|csharp|js|ts|html|css|json|text. Pass code to write exact content, or describe what the file should contain.", Level.LOW, category="apps",
      requires=(), verify=_v_code_file, language="str!", describe="str", code="str", filename="str", editor="str", wpm="num", mode="str")
def t_code(language, describe=None, code=None, filename=None, editor=None, wpm=None, mode=None):
    lang = _norm(language).replace("++", "pp").replace("#", "sharp")
    ext = {"java": "java", "python": "py", "py": "py", "c": "c", "cpp": "cpp", "csharp": "cs", "js": "js", "javascript": "js", "ts": "ts", "typescript": "ts",
           "html": "html", "css": "css", "json": "json", "text": "txt", "txt": "txt"}.get(lang)
    if not ext: raise PolicyViolation(f"Unknown language '{language}'.")
    if code:
        body = str(code)
    else:
        if not describe: raise PolicyViolation("code needs either literal 'code' or a 'describe' brief")
        try:
            body = llm_call(
                f"You write complete, compilable {language} programs. Output ONLY the raw source code — no markdown fences, no commentary.",
                json.dumps({"task": describe}), 0.15, 120)
            body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", body.strip(), flags=re.I | re.M)
        except ActionFailed as e:
            raise ActionFailed(f"Could not generate code (all models failed): {e}", offline=True)
    if not filename:
        words = re.findall(r"[A-Za-z0-9]+", (describe or f"{language}_program")[:60])[:3]
        stem = "Main" if lang == "java" else "".join(w.capitalize() for w in words) or "program"
        if lang == "java" and words: stem = "".join(w.capitalize() for w in words)
        filename = f"{stem}.{ext}"
    filename = Path(filename).name  # never let this escape the workspace via a path in the name
    p = ROOT / "code" / filename; p.parent.mkdir(parents=True, exist_ok=True)
    m = _norm(mode or "")
    instant = m in ("instant", "single", "single go", "one go", "once", "direct", "at once", "instantly", "without typing", "write it directly") or bool(os.environ.get("SARA_TYPE_INSTANT"))

    opened = None; typed_s = None
    # Pre-create the file so `code <path>` opens a real buffer and verification has something to check.
    # Human mode always starts from an EMPTY file: the buffer must not contain old code on a re-run.
    p.write_text("" if not instant else (body if body.endswith("\n") else body + "\n"), encoding="utf-8")
    if not instant:
        # Human-typing path: open the (empty) file in VS Code and type it in like a person.
        ed = _norm(editor or "vscode")
        code_exe = shutil.which("code") or shutil.which("code.cmd")
        if PG is None: raise ActionFailed("desktop_input capability unavailable; use mode='instant'.", offline=True)
        if not code_exe or ed not in ("vscode", "code", "visual studio code", "vs"):
            # no VS Code CLI / non-vscode editor: fall back to deterministic write + open
            p.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
            instant = True
        else:
            BUS.emit("step_progress", tool="code", step=0, typed=0, total=len(body), path=filename, wpm=wpm)
            subprocess.Popen([code_exe, "--new-window", str(p)], close_fds=True, creationflags=DETACHED,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened = "vscode"
            w = find_window("Visual Studio Code", 25) or find_window(Path(filename).stem, 10)
            if not w or not focus_window(w):
                _sleep(1.2)
                w = find_window("Visual Studio Code", 8)
                if w: focus_window(w)
            _sleep(2.5)   # let VS Code finish loading the empty buffer
            try:   # clear any boilerplate and dismiss stray popups before typing
                PG.press("escape"); PG.hotkey("ctrl", "home"); PG.hotkey("ctrl", "shift", "end"); PG.press("backspace")
            except Exception: pass
            _sleep(0.3)
            typed_s = _type_human(body, float(wpm or SARA_TYPE_SPEED), filename, step_index=0)
            p.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")  # authoritative file on disk
            _sleep(0.8)   # let VS Code notice the buffer changed
            try:
                PG.hotkey("ctrl", "s")   # save through the GUI so the on-screen buffer is what's saved
            except Exception: pass
            _sleep(0.6)
            if EXEC.cancel.is_set(): raise ActionFailed("Typing cancelled (emergency stop).", cancelled=True)
    MEM.set("file", str(p))

    if instant:
        ed = _norm(editor or "vscode")
        if ed in ("vscode", "code", "visual studio code", "vs"):
            code_exe = shutil.which("code") or shutil.which("code.cmd")
            if code_exe:
                subprocess.Popen([code_exe, str(p)], close_fds=True, creationflags=DETACHED,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = "vscode"
        if not opened and ed in ("", "notepad", "default"):
            try: os.startfile(str(p)); opened = "default"
            except Exception: pass
        if not opened and ed in ("vscode", "code", "visual studio code", "vs"):
            try: os.startfile(str(p)); opened = "default (VS Code CLI not found)"
            except Exception: pass
    w = find_window("Visual Studio Code", 12) or find_window(Path(filename).stem, 4)
    if w: focus_window(w)
    BUS.emit("step_completed", tool="code", step=0, output=_redact({"path": str(p), "editor": opened, "typed": typed_s is not None, "seconds": typed_s}))
    return _res("typed" if typed_s else "created", filename, str(p), p.exists(), editor=opened, lines=body.count("\n") + 1,
                typing_seconds=typed_s, mode="instant" if instant else "human",
                note=None if opened else "File created but no editor could be opened.")

REG["code"].long_running = 1500.0   # human-style typing may legitimately run for minutes


@tool("web", "Open a URL or run a web search in the default browser. action: open|search", Level.LOW, category="web", action="str!", url="str", query="str", engine="str")
def t_web(action, url=None, query=None, engine=None):
    a = _norm(action)
    if a in ("search", "google", "youtube"):
        if not query: raise PolicyViolation("search requires 'query'")
        e = _norm(engine or ("youtube" if a == "youtube" else "google"))
        base = {"google": "https://www.google.com/search?q=", "youtube": "https://www.youtube.com/results?search_query=", "bing": "https://www.bing.com/search?q=", "duckduckgo": "https://duckduckgo.com/?q="}.get(e, "https://www.google.com/search?q=")
        url = base + urllib.parse.quote_plus(query)
    if not url: raise PolicyViolation("open requires 'url'")
    if not re.match(r"^[a-z]+://", url): url = "https://" + url
    webbrowser.open(url); MEM.set("url", url); w = _poll(lambda: WATCHER.refresh(True) and WATCHER.current if AWARE.browser_url(AWARE.foreground()) else None, 6, 0.8)
    return _res("webbrowser", url, (WATCHER.current or {}).get("url"), bool(w))

@tool("clipboard", "Read or set the clipboard. action: get|set", Level.LOW, category="general", requires=("clipboard",), action="str!", text="str")
def t_clipboard(action, text=None):
    if _norm(action) == "get": v = CLIP.paste(); return {"text": (v or "")[:4000], "verified": True}
    if text is None: raise PolicyViolation("set requires 'text'")
    CLIP.copy(text); return _res("pyperclip", text[:80], None, CLIP.paste() == text)

@tool("files", "File system: list|search|read|write|append|mkdir|move|copy|rename|delete|open|zip. Paths may be absolute, relative to the workspace, or 'desktop'/'downloads'/'documents'.", Level.AUTHORIZED,
      category="files", confirm_actions=("delete",), action="str!", path="str", dest="str", content="str", name="str", pattern="str", limit="int")
def t_files(action, path=None, dest=None, content=None, name=None, pattern=None, limit=None):
    a = _norm(action); p = path or MEM.get("file") or MEM.get("folder") or "workspace"
    if a == "list": return FILES.list(path or MEM.get("folder") or "workspace", pattern or "*", limit or 80)
    if a in ("search", "find"): return FILES.search(name or pattern or "", path, limit or 30)
    if a == "read": return FILES.read(p, limit or 6000)
    if a in ("write", "create"): return FILES.write(path or name, content or "")
    if a == "append": return FILES.write(path or name, content or "", append=True)
    if a in ("mkdir", "new_folder"): return FILES.mkdir(path or name)
    if a in ("move", "copy"): return FILES.move(p, dest, copy=(a == "copy"))
    if a == "rename": return FILES.rename(p, name or dest)
    if a in ("delete", "remove", "trash"): return FILES.delete(p)
    if a in ("open", "show"): return FILES.open(p)
    if a in ("zip", "compress"): return FILES.zip(p, dest)
    raise PolicyViolation(f"Unknown files action '{action}'")
REG["files"].level = Level.LOW  # read-ish by default; mutations are gated below
_FILE_MUTATING = {"write", "create", "append", "mkdir", "new_folder", "move", "copy", "rename", "zip", "compress"}
def _files_level(args):
    a = _norm(args.get("action", ""))
    return Level.CONFIRM if a in ("delete", "remove", "trash") else Level.AUTHORIZED if a in _FILE_MUTATING else Level.LOW
REG["files"].effective_level = _files_level  # type: ignore[assignment]

@tool("system", "System info and metrics (CPU/RAM/disk/GPU/battery/uptime), processes (list|kill), startup apps, installed apps. action: metrics|processes|kill|startup|installed", Level.LOW, category="system",
      confirm_actions=("kill",), action="str!", name="str", pid="int", query="str", limit="int")
def t_system(action, name=None, pid=None, query=None, limit=None):
    a = _norm(action)
    if a in ("metrics", "info", "status"): return SYS.metrics()
    if a in ("processes", "top"): return SYS.processes("list", limit=limit or 25)
    if a in ("kill", "end_task"): return SYS.processes("kill", name=name, pid=pid)
    if a == "startup": return SYS.startup_apps()
    if a in ("installed", "apps"): return SYS.installed_apps(query, limit or 40)
    if a == "battery": return {"battery": SYS.battery(), "verified": True}
    raise PolicyViolation(f"Unknown system action '{action}'")

@tool("volume", "Master volume: get|set|up|down|mute|unmute|toggle_mute (level 0-100 or step).", Level.LOW, category="settings", action="str!", level="int")
def t_volume(action, level=None): return SYS.volume(action, level)
@tool("brightness", "Screen brightness: get|set|up|down (level 0-100 or step).", Level.LOW, category="settings", action="str!", level="int")
def t_brightness(action, level=None): return SYS.brightness(action, level)
@tool("wifi", "Wi-Fi: status|list|connect|disconnect|on|off. Never echo the password back to the user.", Level.AUTHORIZED, category="settings", requires=("netsh",), confirm_actions=("off", "disable"), action="str!", ssid="str", password="str")
def t_wifi(action, ssid=None, password=None): return SYS.wifi(action, ssid, password)
@tool("bluetooth", "Bluetooth: status|on|off|scan|paired|open (pairing finishes in Settings).", Level.AUTHORIZED, category="settings", requires=("windows",), action="str!", device="str")
def t_bluetooth(action, device=None): return SYS.bluetooth(action, device)
@tool("display", "Displays: list|modes|set (width,height,refresh_hz)|rotate (orientation)|nightlight.", Level.AUTHORIZED, category="settings", requires=("windows",), confirm_actions=("set", "rotate", "resolution"),
      action="str!", width="int", height="int", refresh_hz="int", device="str", orientation="str")
def t_display(action, **kw): return SYS.display(action, **kw)
@tool("settings", "Open a Windows Settings page: bluetooth|wifi|display|sound|apps|startup|battery|power|datetime|notifications|update|storage|about|privacy pages...", Level.LOW, category="settings", requires=("windows",), page="str!")
def t_settings(page): return open_settings_page(page)
@tool("power", "Power/session control: lock|sleep|hibernate|shutdown|restart|cancel|signout (delay_sec optional). Also power plans via action='plan' (plan name) and 'plans'.", Level.CONFIRM,
      confirm_prompt="This will {action} the computer. Continue?", category="system", requires=("windows",), action="str!", delay_sec="int", plan="str")
def t_power(action, delay_sec=None, plan=None):
    a = _norm(action)
    if a == "plans": return SYS.power_plan("get")
    if a == "plan": return SYS.power_plan("set", plan)
    return SYS.power(a, delay_sec or 0)
REG["power"].effective_level = lambda args: Level.LOW if _norm(args.get("action", "")) in ("plans", "lock", "cancel") else Level.AUTHORIZED if _norm(args.get("action", "")) == "plan" else Level.CONFIRM  # type: ignore[assignment]
@tool("timezone", "Time zone: get|list|set (zone name like 'India Standard Time').", Level.AUTHORIZED, category="settings", requires=("tzutil",), action="str!", zone="str")
def t_timezone(action, zone=None): return SYS.timezone(action, zone)

@tool("notify", "Show a desktop notification (title, message).", Level.INFO, category="general", message="str!", title="str")
def t_notify(message, title=None):
    if PLYER:
        try: PLYER.notification.notify(title=title or APP_NAME, message=str(message)[:250], app_name=APP_NAME, timeout=6); return _res("plyer", message, None, True)
        except Exception as e: log.debug("plyer: %s", e)
    if PS_EXE:
        run_ps(f"[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; $n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; $n.ShowBalloonTip(6000, {_q(title or APP_NAME)}, {_q(str(message)[:250])}, 'Info'); Start-Sleep -Seconds 1", 20)
        return _res("balloon", message, None, False)
    SPEAK.say(f"{title or 'Note'}: {message}"); return _res("speech", message, None, False)

@tool("say", "Speak/print a message to the user (use for answers, questions, or status). Does not perform any action.", Level.INFO, category="general", critical=False, text="str!")
def t_say(text): SPEAK.say(text); return {"spoken": text, "verified": True}
@tool("wait", "Pause for N seconds (max 30) to let the UI settle before the next step.", Level.INFO, category="general", critical=False, seconds="float!")
def t_wait(seconds):
    # The schema caps this at 30s, but planners still emit 30-60; clamp instead of failing the plan.
    s = max(0.0, min(30.0, float(seconds)))
    if float(seconds) > 30.0: log.warning("wait %.1fs clamped to 30s", float(seconds))
    _sleep(s); return {"waited": s, "verified": True}
@tool("ask_user", "Ask the user a yes/no question or a free-form question and return the answer. kind: yesno|text", Level.INFO, category="general", question="str!", kind="str")
def t_ask_user(question, kind="text"):
    if _norm(kind or "text") == "yesno": return {"answer": ask_yes_no(question), "verified": True}
    SPEAK.say(question, wait=True)
    ans = EARS.listen(timeout=12, phrase_limit=20) if MODE.voice and EARS.ok else input(f"  {question} > ")
    if ans: MEM.turn("user", ans)
    return {"answer": ans or "", "verified": bool(ans)}

@tool("screenshot", "Save a screenshot of the active monitor/window/all monitors to a file (scope: active|window|all). Returns the file path.", Level.LOW, category="perception", requires=("screen_capture",), scope="str", path="str")
def t_screenshot(scope="active", path=None):
    img, region = AWARE.capture(scope or "active"); p = FILES.path(path) if path else ROOT / "screenshots" / f"shot_{datetime.now():%Y%m%d_%H%M%S}.png"
    p.parent.mkdir(parents=True, exist_ok=True); img.save(p); MEM.set("file", str(p)); return _res("mss", scope, str(p), p.exists(), region=region)

# ---- v5 tools ----
@tool("analyze_screen", "Look at the screen: returns where the user is (app/window/browser URL), visible text, interactive elements, optional OCR and (if enabled) a vision description. scope: active|window|all. Use before acting on unknown UI.",
      Level.INFO, category="perception", requires=("screen_capture",), scope="str", describe="bool", include_image="bool")
def t_analyze_screen(scope="active", describe=False, include_image=False):
    out = AWARE.analyze(scope or "active", include_image=bool(include_image), describe=bool(describe))
    if include_image and not SEND_SCREENSHOTS: out.pop("image", None); out["note"] = "image withheld: SARA_SEND_SCREENSHOTS is off"
    return out

@tool("memory", "Long-term memory: remember (kind preference|rule|fact, content) | recall (query) | list (kind) | forget (query or id) | stats | history. Never store passwords or keys.", Level.LOW, category="memory",
      confirm_actions=("forget_all",), action="str!", content="str", kind="str", query="str", memory_id="int", limit="int")
def t_memory(action, content=None, kind=None, query=None, memory_id=None, limit=None):
    a = _norm(action)
    if a in ("remember", "store", "save"):
        if not content: raise PolicyViolation("remember requires 'content'")
        k = _norm(kind or "fact"); k = k if k in LTM.KINDS else "fact"; mid = LTM.remember(k, content, tags=("user-stated",))
        if mid is None: raise ActionFailed("Refused: content looks like a secret (passwords/keys are never stored).", privacy=True)
        return _res("sqlite", content[:80], {"id": mid, "kind": k}, True)
    if a in ("recall", "search"): return {"memories": LTM.recall(query or content or "", limit or 10), "verified": True}
    if a == "list": return {"memories": LTM.list([kind] if kind and kind in LTM.KINDS else None, limit or 20), "verified": True}
    if a in ("forget", "delete"):
        n = LTM.forget(query or content, memory_id)
        if not n: raise ActionFailed("Nothing matched to forget.")
        return _res("sqlite", query or memory_id, {"forgotten": n}, True)
    if a == "forget_all":
        with LTM.lock: n = LTM.db.execute("UPDATE memories SET active=0, updated=? WHERE active=1", (time.time(),)).rowcount
        return _res("sqlite", "all", {"forgotten": n}, True)
    if a == "stats": return {**LTM.stats(), "unreliable_tools": LTM.unreliable(), "verified": True}
    if a == "history": return {"history": LTM.recent_history(limit or 8), "verified": True}
    if a == "cleanup": return {**LTM.cleanup(), "verified": True}
    raise PolicyViolation(f"Unknown memory action '{action}'")

@tool("privacy", "Screen-capture privacy: status | pause (stop looking at the screen) | resume | disable | enable.", Level.INFO, category="privacy", action="str!")
def t_privacy(action):
    a = _norm(action)
    if a == "status": return {**AWARE.status(), "verified": True}
    if a in ("pause", "stop", "stop watching"): return AWARE.set(paused=True)
    if a in ("resume", "start", "resume watching"): return AWARE.set(paused=False)
    if a == "disable": return AWARE.set(enabled=False)
    if a == "enable": return AWARE.set(enabled=True, paused=False)
    raise PolicyViolation("privacy action must be status|pause|resume|enable|disable")

@tool("task", "Ongoing task sessions (SARA keeps following the user across apps/tabs until done): start (goal) | status | pause | resume | done | cancel | note (content).", Level.INFO, category="session",
      action="str!", goal="str", content="str")
def t_task(action, goal=None, content=None):
    a = _norm(action)
    if a == "start":
        if not goal: raise PolicyViolation("start requires 'goal'")
        return SESSIONS.start(goal, WATCHER.current or AWARE.context())
    if a == "status": return SESSIONS.status()
    if a == "pause": return SESSIONS.pause()
    if a in ("resume", "continue"): return SESSIONS.resume()
    if a in ("done", "complete", "finish"): return SESSIONS.finish("completed")
    if a in ("cancel", "stop", "abort"): return SESSIONS.finish("cancelled")
    if a == "note":
        s = SESSIONS.cur
        if not s: raise ActionFailed("No task session.")
        s.notes.append(_redact(content or "")[:300]); return {"notes": s.notes[-5:], "verified": True}
    raise PolicyViolation(f"Unknown task action '{action}'")

# ============================ 9. LLM / PLANNER ============================
@dataclass
class Step:
    tool: str; args: Dict[str, Any] = field(default_factory=dict); why: str = ""
    def dump(self): return {"tool": self.tool, "args": _redact(self.args), "why": self.why[:120]}
@dataclass
class Plan:
    say: str = ""; steps: List[Step] = field(default_factory=list); done: bool = False; learn: List[dict] = field(default_factory=list); source: str = "llm"; ongoing: bool = False

# Messages that mean "this model/key is paywalled or the upstream is out of capacity" rather than
# "your key is wrong" — those are worth failing over, a bare 401/403 is not.
_PAYWALL_HINTS = ("premium model", "paid plan", "deposited balance", "insufficient balance", "insufficient credit",
                  "quota", "credit", "billing", "at capacity", "capacity", "rate limit", "temporarily unavailable",
                  "unavailable", "does not exist", "model_not_found", "not supported")


# Models known to accept an image_url message; putting them first avoids burning a round-trip on a
# text-only model that just 400s when the caller sent a screenshot.
VISION_HINT_MODELS = [m.strip() for m in os.environ.get(
    "SARA_VISION_MODELS",
    "cc/claude-sonnet-4-6,cc/claude-haiku-4-5-20251001,ag/gemini-3.6-flash-medium,glm/glm-4.6v,"
    "am/llama-3.2-11b-vision-instruct,google/gemini-2.5-flash,x-ai/grok-4.6,"
    "deepseek/deepseek-v4-flash-vision-exp,z-ai/glm-4.6v").split(",") if m.strip()]


def _ascii(s) -> str:
    """Providers happily return Cyrillic/emoji in error bodies; Windows consoles are cp1252 and the
    logging module raises on them, so every upstream message is flattened before it is logged/shown."""
    try: return str(s).encode("ascii", "replace").decode("ascii")
    except Exception: return "<unprintable upstream error>"


# Sticky failover: a provider that just answered is tried first for the next few minutes, so an
# overloaded/paywalled primary (e.g. a 429-ing model) costs one probe instead of every call.
_LLM_WORKING: Optional[tuple] = None
_LLM_WORKING_AT = 0.0
LLM_STICKY_SECONDS = float(os.environ.get("SARA_LLM_STICKY_SECONDS", "600"))


def _sticky_first(candidates):
    if not _LLM_WORKING or (time.time() - _LLM_WORKING_AT) > LLM_STICKY_SECONDS:
        return candidates
    base, model = _LLM_WORKING
    hit = [c for c in candidates if c["base"].rstrip("/") == base and c["model"] == model]
    return hit + [c for c in candidates if c not in hit] if hit else candidates


def _remember_working(cand):
    global _LLM_WORKING, _LLM_WORKING_AT
    _LLM_WORKING, _LLM_WORKING_AT = ((cand["base"].rstrip("/"), cand["model"]), time.time())


def _http_retryable(code: int, detail: str) -> bool:
    """True when the failover chain should try the next candidate for this HTTP error."""
    if code == 401:
        return False                                  # bad/missing key: no fallback can help
    if code == 403:
        d = (detail or "").lower()
        return any(h in d for h in _PAYWALL_HINTS)    # "needs paid plan / no balance" 403 → try another provider
    return code in (400, 402, 404, 405, 408, 409, 410, 429) or code >= 500


def llm_call(system, user, temperature=0.2, timeout=60, model=None, image=None):
    # Walks the whole failover chain (primary → same-provider fallbacks → alternate providers such as
    # AnyModel) so one overloaded, paywalled or missing model can no longer degrade the agent to canned
    # responses. Only a genuinely bad key (401, or a 403 that isn't a plan/balance message) stops it.
    if not LLM_API_KEY and not ANYMODEL_API_KEY: raise ActionFailed("LLM not configured (LLM_API_KEY missing).", offline=True)
    candidates = llm_providers()
    if model:  # an explicit model is tried first, then the normal chain (minus that duplicate)
        primary_base = LLM_BASE_URL.rstrip("/")
        candidates = ([{"base": LLM_BASE_URL, "key": LLM_API_KEY, "model": model, "label": "explicit"}] +
                      [c for c in candidates if not (c["base"].rstrip("/") == primary_base and c["model"] == model)])
    if image:  # screenshots → try the vision-capable models first, keep the rest as backup
        candidates = sorted(candidates, key=lambda c: 0 if c["model"] in VISION_HINT_MODELS else 1)
    else:
        candidates = _sticky_first(candidates)
    content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": image}}] if image else user
    last_exc: Optional[ActionFailed] = None
    for idx, cand in enumerate(candidates):
        if not cand["key"]:
            continue
        body = {"model": cand["model"], "temperature": float(temperature), "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        req = urllib.request.Request(cand["base"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {cand['key']}", "Content-Type": "application/json", "HTTP-Referer": "https://sara.local", "X-Title": APP_NAME,
                                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})  # browser UA: providers behind Cloudflare (e.g. xkiro) block python-urllib with 403/1010
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r: data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = _ascii(e.read()[:200].decode("utf-8", "replace")) if hasattr(e, "read") else ""
            last_exc = ActionFailed(f"LLM HTTP {e.code} [{cand['label']}/{cand['model']}]: {detail}", offline=e.code >= 500 or e.code in (410, 429))
            if not _http_retryable(e.code, detail): raise last_exc
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = ActionFailed(f"LLM unreachable [{cand['label']}/{cand['model']}]: {e}", offline=True)
        else:
            if idx: log.info("LLM served by fallback %s/%s", cand["label"], cand["model"])
            _remember_working(cand)
            try: return data["choices"][0]["message"]["content"] or ""
            except Exception: raise ActionFailed(f"LLM returned an unexpected payload: {_ascii(str(data)[:200])}")
        if idx + 1 < len(candidates): log.warning("LLM %s/%s failed (%s) - next candidate: %s", cand["label"], cand["model"], _ascii(last_exc), candidates[idx + 1]["model"])
    raise last_exc if last_exc else ActionFailed("LLM call failed: no usable model in the failover chain.", offline=True)

def vision_analyze(image_data_url: str, question: str = "") -> str:
    """Look at an image the CALLER supplied (live screen share / shared frame) and answer about it.

    Distinct from AWARE.describe(), which is the agent's own periodic OS capture and is gated behind
    SARA_SEND_SCREENSHOTS: here the user explicitly handed us the frame, so it is analysed directly.
    Uses the same failover chain (vision-capable models first)."""
    if not str(image_data_url or "").startswith("data:image"):
        raise ActionFailed("vision_analyze needs a data:image/... URL (JPEG or PNG data URL).")
    q = (question or "").strip() or ("Describe this desktop screen factually: application, page, key visible content, "
                                      "controls, dialogs or errors. Max 6 short lines.")
    system = ("You are SARA, looking at a screen the user explicitly shared with you. Be concise, factual and "
              "specific about what is on screen (app, page, visible text, buttons, fields, errors). Never invent detail.")
    return llm_call(system, json.dumps({"question": q}), 0.1, 90, image=image_data_url).strip()


def _parse_json(text):
    t = text.strip(); t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M)
    try: return json.loads(t)
    except Exception: pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try: return json.loads(t[i:j + 1])
        except Exception: pass
    return {"say": t[:400]}

class Planner:
    SYSTEM = """You are SARA, a persistent Windows desktop assistant. You control the PC ONLY through the tools listed. Respond with ONE JSON object:
{"say": "<short spoken reply, may be empty>", "plan": [{"tool": "<name>", "args": {...}, "why": "<why>"}], "done": <bool>, "learn": [{"kind": "preference|rule|fact", "content": "..."}]}
Rules: never invent tools/args; prefer the fewest steps; use analyze_screen before clicking unknown UI; if the request is a question, answer in "say" with an empty plan;
if it needs info first (e.g. metrics), plan the info tool and set done=false so you can be called again with the result; put durable user preferences/rules in "learn" (never secrets);
respect user preferences/rules from memory; if a tool is marked unreliable, prefer an alternative; "done"=true when nothing more is needed after this plan.
Continuation mode: you are told the user moved to a new app/page while a task is ongoing. Return an EMPTY plan and empty "say" if nothing useful can be done there; act only when the new context is relevant to the goal. Set done=true only when the whole goal is finished.
Writing code (any language, any app named): use ONE code step, never the input tool:
  {"tool": "code", "args": {"language": "<java|python|c|cpp|csharp|js|ts|html|css|json>", "describe": "<what the program must do, precise enough to implement>", "filename": "<Optional exact filename>"}}
DEFAULT BEHAVIOUR: the code is typed into the editor character-by-character like a human at a configurable speed (args: wpm — words per minute; lower = slower, e.g. 45 = deliberate, 90 = fast). Add "mode": "instant" ONLY when the user says things like 'single go', 'in one go', 'instantly', 'all at once', 'without typing' or otherwise asks for it instantly.
It generates the full source, opens the editor, types it like a human, saves it, and the file also lands under the workspace. Use code with literal "code" only when the user dictated the exact text.
For keyboard 'input' steps, ALWAYS pass target="<app>" (e.g. target="vscode") so the keystrokes go to the right window; without target the step is refused.
Never plan input type/hotkey steps after opening an app to 'write a file' — that is what the code tool is for. Say so in the step's "why"."""
    def __init__(self): self._doc = None
    def tool_doc(self):
        if self._doc is None:
            lines = []
            for t in REG.values():
                ok, why = t.available(); sch = ", ".join(f"{k}:{v}" for k, v in t.schema.items())
                lines.append(f"- {t.name}({sch}) [{t.level.name}{'' if ok else ', UNAVAILABLE: ' + why}] {t.description}")
            self._doc = "\n".join(lines)
        return self._doc
    def _context_pack(self, goal, ctx, extra=None):
        ctx = ctx or WATCHER.current or {}
        pack = {"goal": goal, "now": datetime.now().strftime("%Y-%m-%d %H:%M %A"), "os": f"{OS_NAME} {platform.release()}", "admin": is_admin(),
                "user_context": {k: ctx.get(k) for k in ("app", "title", "browser", "url", "host", "page_title")}, "working_memory": MEM.brief(),
                "long_term_memory": LTM.recall(goal + " " + (ctx.get("app") or ""), 12), "recent_tasks": LTM.recent_history(5), "unreliable_tools": LTM.unreliable(),
                "capture": AWARE.status(), "screen": screen_state() if AWARE.allowed() else {"foreground": foreground_title()}}
        if extra: pack.update(extra)
        return _redact(pack)
    def plan(self, goal, ctx=None, extra=None, temperature=0.15):
        try: raw = llm_call(self.SYSTEM + "\n\nTOOLS:\n" + self.tool_doc(), json.dumps(self._context_pack(goal, ctx, extra), ensure_ascii=False), temperature, 90)
        except ActionFailed as e:
            if e.info.get("offline"): return self.offline(goal, ctx, str(e))
            raise
        return self._to_plan(_parse_json(raw), "llm")
    def continuation(self, session, ctx):
        extra = {"mode": "continuation", "ongoing_task": session.summary(), "trigger": f"user switched to {ctx.get('app')} ({ctx.get('title')})", "notes": session.notes[-5:], "steps_so_far": session.log[-6:]}
        try: raw = llm_call(self.SYSTEM + "\n\nTOOLS:\n" + self.tool_doc(), json.dumps(self._context_pack(session.goal, ctx, extra), ensure_ascii=False), 0.1, 90)
        except ActionFailed as e:
            if e.info.get("offline"): return Plan(source="offline")  # never act blindly without the model in continuation mode
            raise
        return self._to_plan(_parse_json(raw), "llm")
    def _to_plan(self, data, source):
        steps = []
        for s in data.get("plan") or []:
            if not isinstance(s, dict): continue
            name = str(s.get("tool") or s.get("name") or "").strip(); args = s.get("args") if isinstance(s.get("args"), dict) else {}
            if name in REG: steps.append(Step(name, args, str(s.get("why") or "")))
            else:
                m = difflib.get_close_matches(name, list(REG), n=1, cutoff=0.75)
                if m: steps.append(Step(m[0], args, str(s.get("why") or "")))
        learn = [x for x in (data.get("learn") or []) if isinstance(x, dict)]
        return Plan(str(data.get("say") or "").strip(), steps, bool(data.get("done", not steps)), learn, source)
    # ---- offline fallback: learned workflows + a small rule grammar ----
    _RULES: List[Tuple[re.Pattern, Callable[[re.Match], List[Step]]]] = [
        (re.compile(r"^(?:please )?(?:open|launch|start|run) (?:the )?(.+?)(?: app| application)?$", re.I), lambda m: [Step("app", {"action": "open", "name": m.group(1)})]),
        (re.compile(r"^(?:please )?(?:close|quit|exit|kill) (?:the )?(.+?)(?: app| application| window)?$", re.I), lambda m: [Step("app", {"action": "close", "name": m.group(1)})]),
        (re.compile(r"^(?:switch|go|focus) (?:to )?(.+)$", re.I), lambda m: [Step("app", {"action": "focus", "name": m.group(1)})]),
        (re.compile(r"^(?:set )?volume (?:to )?(\d+)", re.I), lambda m: [Step("volume", {"action": "set", "level": int(m.group(1))})]),
        (re.compile(r"^(?:turn )?volume (up|down)", re.I), lambda m: [Step("volume", {"action": m.group(1)})]),
        (re.compile(r"^(mute|unmute)(?: (?:the )?(?:volume|sound|audio))?$", re.I), lambda m: [Step("volume", {"action": m.group(1).lower()})]),
        (re.compile(r"^(?:set )?brightness (?:to )?(\d+)", re.I), lambda m: [Step("brightness", {"action": "set", "level": int(m.group(1))})]),
        (re.compile(r"^(?:turn )?brightness (up|down)", re.I), lambda m: [Step("brightness", {"action": m.group(1)})]),
        (re.compile(r"^(?:google|search(?: for)?|look up) (.+)$", re.I), lambda m: [Step("web", {"action": "search", "query": m.group(1)})]),
        (re.compile(r"^(?:open|go to) ((?:https?://|www\.)\S+|\S+\.(?:com|org|net|io|in|dev)\S*)$", re.I), lambda m: [Step("web", {"action": "open", "url": m.group(1)})]),
        (re.compile(r"^(?:what(?:'s| is) on (?:my )?screen|analy[sz]e (?:the |my )?screen|look at (?:the |my )?screen|describe (?:the |my )?screen)", re.I), lambda m: [Step("analyze_screen", {"scope": "active", "describe": True})]),
        (re.compile(r"^(?:take|capture) (?:a )?screenshot", re.I), lambda m: [Step("screenshot", {"scope": "active"})]),
        (re.compile(r"^(?:type|write) (.+)$", re.I), lambda m: [Step("input", {"action": "type", "text": m.group(1)})]),
        (re.compile(r"^press (.+)$", re.I), lambda m: [Step("input", {"action": "hotkey", "keys": m.group(1)})]),
        (re.compile(r"^click (?:on )?(.+)$", re.I), lambda m: [Step("input", {"action": "click_text", "text": m.group(1)})]),
        (re.compile(r"^(lock)(?: (?:the )?(?:pc|computer|screen))?$", re.I), lambda m: [Step("power", {"action": "lock"})]),
        (re.compile(r"^(shutdown|shut down|restart|reboot|sleep|hibernate)(?: (?:the )?(?:pc|computer))?$", re.I), lambda m: [Step("power", {"action": {"shut down": "shutdown", "reboot": "restart"}.get(m.group(1).lower(), m.group(1).lower())})]),
        (re.compile(r"^(?:open )?(?:windows )?settings(?: for| ->)? ?(.*)$", re.I), lambda m: [Step("settings", {"page": m.group(1) or "home"})]),
        (re.compile(r"^(?:wifi|wi-fi) (status|list|scan|on|off|disconnect)", re.I), lambda m: [Step("wifi", {"action": m.group(1).lower()})]),
        (re.compile(r"^(?:bluetooth|bt) (status|on|off|scan)", re.I), lambda m: [Step("bluetooth", {"action": m.group(1).lower()})]),
        (re.compile(r"^(?:system )?(?:metrics|status|how(?:'s| is) (?:my|the) (?:pc|computer|system)|battery)", re.I), lambda m: [Step("system", {"action": "metrics"})]),
        (re.compile(r"^(?:list|show) (?:files|folder) (?:in |of )?(.+)$", re.I), lambda m: [Step("files", {"action": "list", "path": m.group(1)})]),
        (re.compile(r"^(?:find|search) (?:file|files) (.+)$", re.I), lambda m: [Step("files", {"action": "search", "name": m.group(1)})]),
    ]
    # Code-writing grammar — checked BEFORE _RULES so "open vscode and write java code for ..."
    # becomes a deterministic code step instead of app-open + blind typing.
    _CODE_RULES: List[Tuple[re.Pattern, Callable[[re.Match], List[Step]]]] = [
        (re.compile(r"^(?:(?:open|launch|start|switch to)\s+(?:vs ?code|visual studio code|notepad|an? editor)\s*(?:and|then|,)?\s*)?(?:(?:there|in it|on it|in a new window|in new window|on new window|in the new window|a new window|new window)(?:\s*(?:and|then|,)?\s*))*(?:please\s+)?(?:write|create|make|code)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|new\s+)?(c\+\+|c#|c\b(?!\+|#)|javascript|typescript|python|java|html|css|json|c source|js|ts)\s*(?:code\s+|program\s+|file\s+|script\s+|class\s+|source\s+)?(?:for|that|which|to|on|about)?\s*(.*?)\s*(?:\bin\s+|\bon\s+|\bwith\s+|\busing\s+)?(?:vs ?code|visual studio code|notepad|editor)?[\s.!]*$", re.I),
         lambda m: [Step("code", {"language": m.group(1).lower().replace("c source", "c"), "describe": (m.group(2) or "a simple demonstration program").strip() or "a simple demonstration program", "editor": "vscode",
                                 **(({"mode": "instant"} if re.search(r"single\s*go|one\s*go|at\s*once|instantly|all\s*at\s*once|without\s*typing|one\s*shot|single\s*shot|directly", m.group(0), re.I) else {})),
                                 **(({"wpm": 38} if re.search(r"slow(ly)?|like\s*a\s*human|human(\s|-)?typ(e|ing)|real(\s|-)?typ(e|ing)|natural", m.group(0), re.I) else {})),
                                 **(({"wpm": 110} if re.search(r"fast|quickly|speed", m.group(0), re.I) else {}))},
                    "code tool: generate the file, then type it into the editor like a human (or instantly on 'single go')")]),
    ]
    def offline(self, goal, ctx, reason=""):
        wf = LTM.find_workflow(goal)
        if wf:
            steps = [Step(s["tool"], s.get("args") or {}, s.get("why", "")) for s in wf["plan"] if s.get("tool") in REG]
            if steps: log.info("planner: offline, reusing learned workflow %s (match %.2f)", wf["goal_key"], wf["match"]); return Plan("Model is offline; reusing what worked last time.", steps, True, [], "workflow")
        g = _norm(goal)
        for pat, mk in self._CODE_RULES + self._RULES:
            m = pat.match(g)
            if m: return Plan("", mk(m), True, [], "rules")
        return Plan(f"I can't reach the language model right now ({reason[:80]}), and I don't have a learned workflow for that. Try a simple command like 'open chrome' or 'volume 40'.", [], True, [], "offline")
PLANNER = Planner()

# ============================ 10. EXECUTOR ============================
class StaleContext(ActionFailed): pass
class Executor:
    UI_CATS = {"input", "window"}; MUTATING = {"input", "window", "apps", "files", "settings", "system", "web"}
    def __init__(self): self.lock = threading.RLock(); self.busy = threading.Event(); self.cancel = threading.Event(); self._recent: List[Tuple[str, float]] = []; self.step_timeout = STEP_TIMEOUT
    def _timeout_for(self, t: Tool):
        # Tools that stream progress (e.g. human-style typing) may legitimately run for minutes.
        return max(self.step_timeout, float(getattr(t, "long_running", 0) or 0))
    def _dup(self, step):
        key = f"{step.tool}:{json.dumps(step.args, sort_keys=True, default=str)}"; now = time.time(); self._recent = [(k, t) for k, t in self._recent if now - t < 4.0]
        if any(k == key for k, _ in self._recent) and REG[step.tool].category in self.MUTATING and step.tool not in ("wait", "say"): return True
        self._recent.append((key, now)); return False
    def _authorize(self, t: Tool, step: Step, trigger):
        lvl = t.effective_level(step.args)
        if step.tool in TRUSTED: return True
        if lvl == Level.CONFIRM or (trigger != "user" and lvl >= Level.AUTHORIZED):
            prompt = (t.confirm_prompt or "SARA wants to run {tool} {action}. Allow?").format(tool=t.name, action=_norm(step.args.get("action", "")) or "", **{k: v for k, v in step.args.items() if k != "password"})
            return ask_yes_no(prompt)
        return True
    def run(self, steps: List[Step], trigger="user", ctx=None, on_step: Optional[Callable[[dict], None]] = None) -> List[dict]:
        results: List[dict] = []; planned_sig = (ctx or WATCHER.current or {}).get("sig", "")
        with self.lock:
            self.busy.set(); self.cancel.clear()
            try:
                for i, step in enumerate(steps):
                    t = REG.get(step.tool)
                    if self.cancel.is_set(): results.append({"tool": step.tool, "ok": False, "cancelled": True, "error": "cancelled"}); break
                    if not t: results.append({"tool": step.tool, "ok": False, "error": "unknown tool"}); continue
                    rec = {"tool": step.tool, "args": _redact(step.args), "index": i, "ok": False}
                    BUS.emit("step_start", trigger=trigger, **rec)
                    try:
                        if not self._authorize(t, step, trigger): raise ActionFailed("Not authorized by user.", cancelled=True)
                        if self._dup(step): rec.update(ok=True, skipped=True, output={"note": "duplicate action within 4s skipped", "verified": False}); results.append(rec); continue
                        if t.category in self.UI_CATS and planned_sig and not step.args.get("target"):
                            cur = WATCHER.refresh(True) or WATCHER.current
                            if cur and cur.get("sig") != planned_sig and i > 0 and _norm(step.args.get("action", "")) not in ("hotkey", "press"):
                                raise StaleContext(f"Foreground changed to '{cur.get('title')}' since the plan was made; re-planning.", stale=True)
                        try: out = _timed(lambda: t.run(step.args), self._timeout_for(t))
                        except ActionFailed as e:
                            if not e.info.get("timeout"): raise
                            log.warning("step %s timed out; retrying once", step.tool); out = _timed(lambda: t.run(step.args), self._timeout_for(t))
                        rec.update(ok=True, output=_redact(out)); MEM.action(step.tool, step.args, True)
                    except StaleContext as e:
                        rec.update(ok=False, error=str(e), stale=True); results.append(rec); BUS.emit("step_end", trigger=trigger, **rec); MEM.action(step.tool, step.args, False); raise
                    except (ActionFailed, PolicyViolation) as e:
                        info = getattr(e, "info", {}); rec.update(ok=False, error=str(e), **{k: v for k, v in info.items() if isinstance(v, (bool, int, str))}); MEM.action(step.tool, step.args, False)
                        if info.get("cancelled"): results.append(rec); BUS.emit("step_end", trigger=trigger, **rec); break
                    except Exception as e:
                        log.exception("step %s crashed", step.tool); rec.update(ok=False, error=f"{type(e).__name__}: {e}"); MEM.action(step.tool, step.args, False)
                    results.append(rec); BUS.emit("step_end", trigger=trigger, **rec)
                    if on_step:
                        try: on_step(rec)
                        except Exception: pass
                    if not rec["ok"] and t.critical: break
            finally: self.busy.clear()
        return results
    def stop(self): self.cancel.set()
EXEC = Executor()

# ============================ 11. SESSIONS ============================
@dataclass
class TaskSession:
    id: str; goal: str; created: float; updated: float; status: str = "active"; continuations: int = 0; failures: int = 0; last_sig: str = ""
    start_app: str = ""; log: List[dict] = field(default_factory=list); notes: List[str] = field(default_factory=list); apps_seen: List[str] = field(default_factory=list)
    def summary(self): return {"id": self.id, "goal": self.goal, "status": self.status, "age_min": round((time.time() - self.created) / 60, 1), "continuations": self.continuations, "failures": self.failures, "started_in": self.start_app, "apps_seen": self.apps_seen[-8:], "notes": self.notes[-5:]}

class SessionManager:
    def __init__(self): self.lock = threading.Lock(); self.cur: Optional[TaskSession] = None; self._pending = None; self._timer: Optional[threading.Timer] = None; self._running = threading.Event(); self.history: List[dict] = []
    def active(self): s = self.cur; return bool(s and s.status == "active")
    def _emit(self, s, **kw): BUS.emit("session_update", **{**s.summary(), **kw})
    def start(self, goal, ctx):
        if self.cur and self.cur.status in ("active", "paused", "stalled"): self.finish("superseded", quiet=True)
        s = TaskSession(uuid.uuid4().hex[:8], goal.strip(), time.time(), time.time(), start_app=(ctx or {}).get("app") or "", last_sig=(ctx or {}).get("sig") or "")
        with self.lock: self.cur = s
        MEM.set("task", goal[:120]); self._emit(s, event="started"); log.info("session %s started: %s", s.id, goal[:80])
        return {**s.summary(), "verified": True, "note": "I'll keep following this task as you move between apps. Say 'task status', 'done' or 'continue' anytime."}
    def status(self):
        s = self.cur
        return {**(s.summary() if s else {"status": "none"}), "watching": AWARE.allowed(), "auto_continue": AUTO_CONTINUE, "verified": True}
    def pause(self):
        s = self.cur
        if not s or s.status not in ("active", "stalled"): raise ActionFailed("No active task to pause.")
        s.status = "paused"; s.updated = time.time(); self._emit(s, event="paused"); return {**s.summary(), "verified": True}
    def resume(self):
        s = self.cur
        if not s or s.status in ("done", "cancelled"): raise ActionFailed("No task to resume. Tell me what you're working on and I'll start a new one.")
        s.status = "active"; s.failures = 0; s.updated = time.time(); self._emit(s, event="resumed")
        threading.Thread(target=self._continue, args=(s, WATCHER.current or AWARE.context(), "resume"), daemon=True).start(); return {**s.summary(), "verified": True}
    def finish(self, how="completed", quiet=False):
        s = self.cur
        if not s: raise ActionFailed("No task session to finish.")
        if self._timer: self._timer.cancel()
        s.status = "done" if how == "completed" else how; s.updated = time.time(); self.history.append(s.summary()); self._emit(s, event="finished", how=how)
        try: LTM.log_history(s.id, "session", s.goal, [], s.log[-10:], how, {"app": s.start_app, "apps_seen": s.apps_seen[-8:]})
        except Exception: pass
        MEM.set("task", None, quiet=True)
        if not quiet: log.info("session %s finished: %s", s.id, how)
        with self.lock: self.cur = None
        return {**s.summary(), "verified": True}
    # ---- context events from the watcher ----
    def on_context(self, ctx, reason):
        s = self.cur
        if not s or s.status != "active" or not AUTO_CONTINUE: return
        if time.time() - s.updated > SESSION_TIMEOUT_MIN * 60: SPEAK.say(f"No progress on '{s.goal[:50]}' for {int(SESSION_TIMEOUT_MIN)} minutes; I'll stop following it. Say 'continue' to pick it up again."); s.status = "paused"; self._emit(s, event="timeout"); return
        if reason == "window" and ctx.get("sig") == s.last_sig: return  # dedupe: same place as last time we acted/evaluated
        if reason == "window" and ctx.get("app") and ctx["app"] not in s.apps_seen: s.apps_seen.append(ctx["app"])
        with self.lock:
            self._pending = (ctx, reason)
            if self._timer: self._timer.cancel()
            self._timer = threading.Timer(1.2, self._fire); self._timer.daemon = True; self._timer.start()   # debounce rapid alt-tabbing
    def _fire(self):
        with self.lock: p, self._pending = self._pending, None
        if not p or not self.active() or self._running.is_set() or EXEC.busy.is_set(): return
        ctx, reason = p; s = self.cur
        if s.continuations >= MAX_CONTINUATIONS: SPEAK.say("I've reached the continuation limit for this task; say 'continue' if you want me to keep going."); s.status = "paused"; self._emit(s, event="limit"); return
        threading.Thread(target=self._continue, args=(s, ctx, reason), daemon=True, name="session-continue").start()
    def _continue(self, s: TaskSession, ctx, reason):
        if self._running.is_set(): return
        self._running.set()
        try:
            s.last_sig = ctx.get("sig", ""); s.continuations += 1; self._emit(s, event="evaluating", app=ctx.get("app"), reason=reason)
            plan = PLANNER.continuation(s, ctx)
            if not plan.steps and not plan.say: self._emit(s, event="idle", app=ctx.get("app")); return   # nothing relevant here: stay silent
            if plan.say: SPEAK.say(plan.say)
            results = []
            if plan.steps:
                try: results = EXEC.run(plan.steps, trigger="session", ctx=ctx)
                except StaleContext: self._emit(s, event="stale", app=ctx.get("app")); return
                ok = bool(results) and all(r.get("ok") or r.get("skipped") for r in results); s.failures = 0 if ok else s.failures + 1
                s.log.extend({"app": ctx.get("app"), "tool": r.get("tool"), "ok": r.get("ok"), "error": (r.get("error") or "")[:120] or None} for r in results); s.log = s.log[-30:]
                LEARN.after_task(s.goal, plan.steps, results, "session", s.id, ctx, {"learn": plan.learn})
                if s.failures >= 3: s.status = "stalled"; SPEAK.say(f"I've hit three problems in a row on '{s.goal[:60]}'. I'll pause; say 'continue' when you want me to retry."); self._emit(s, event="stalled"); return
            if plan.done and (not plan.steps or all(r.get("ok") for r in results)): SPEAK.say("That task looks complete."); self.finish("completed")
        except ActionFailed as e: s.failures += 1; log.warning("session continue: %s", e); self._emit(s, event="error", error=str(e)[:160])
        except Exception as e: log.exception("session continue crashed"); s.failures += 1
        finally:
            s.updated = time.time(); self._running.clear()
SESSIONS = SessionManager()

# ============================ 12. AGENT / COMMANDS / MAIN ============================
class Agent:
    QUIT = {"quit", "exit", "goodbye", "bye sara", "shut yourself down"}
    def __init__(self): self.lock = threading.Lock(); self.paused = False
    # ---- direct commands (no model round-trip) ----
    def _command(self, text):
        t = _norm(text)
        if t in self.QUIT: return "__quit__"
        if re.match(r"^(pause|stop) (watching|looking|screen capture)", t): AWARE.set(paused=True); return "Okay, I've stopped looking at your screen. Say 'resume watching' when you want me back."
        if re.match(r"^(resume|start|continue) (watching|looking|screen capture)", t):
            if ask_yes_no("Allow me to look at your screen again?"): AWARE.set(paused=False); return "Watching resumed."
            return "Okay, staying paused."
        if t in ("task status", "status of task", "what are you working on", "current task"):
            st = SESSIONS.status()
            return f"Working on: {st['goal']} ({st['status']}, {st['continuations']} check-ins, seen {', '.join(st.get('apps_seen') or []) or 'no other apps'})." if st.get("goal") else "No ongoing task right now."
        if t in ("done", "i'm done", "im done", "task done", "finish task", "stop task", "cancel task", "that's all", "thats all"):
            if not SESSIONS.cur: return "There's no ongoing task."
            SESSIONS.finish("cancelled" if "cancel" in t else "completed"); return "Task closed."
        if t in ("continue", "resume", "resume task", "keep going", "go on"):
            try: SESSIONS.resume(); return "Resuming the task."
            except ActionFailed as e: return str(e)
        m = re.match(r"^(?:please )?remember (?:that )?(.+)$", text.strip(), re.I)
        if m:
            body = m.group(1).strip(); kind = "preference" if re.search(r"\b(prefer|like|favou?rite|usually|default)\b", body, re.I) else "rule" if re.search(r"\b(always|never|don't|do not|should)\b", body, re.I) else "fact"
            mid = LTM.remember(kind, body, tags=("user-stated",))
            return f"Got it, I'll remember that ({kind})." if mid else "I won't store that: it looks like a password or key."
        m = re.match(r"^(?:please )?forget (?:about |that )?(.+)$", text.strip(), re.I)
        if m: n = LTM.forget(m.group(1)); return f"Forgot {n} memor{'y' if n == 1 else 'ies'}." if n else "I couldn't find a matching memory."
        if re.match(r"^what do you (remember|know)( about me)?", t) or t in ("show memory", "list memories", "my memories"):
            items = LTM.list(None, 12)
            return "I don't have any long-term memories yet." if not items else "Here's what I remember: " + "; ".join(f"[{i['kind']}] {i['content'][:90]}" for i in items)
        if t in ("privacy status", "are you watching", "are you watching my screen"): s = AWARE.status(); return f"Screen capture is {'active' if s['allowed'] else 'paused' if s['paused'] else 'disabled'}; screenshots {'are' if s['send_screenshots_to_model'] else 'are not'} sent to the model."
        if t in ("stop", "cancel", "abort", "halt") and EXEC.busy.is_set(): EXEC.stop(); return "Stopping."
        return None
    # ---- main entry: text -> speech/actions ----
    def handle(self, text, trigger="user"):
        text = (text or "").strip()
        if not text: return ""
        MEM.turn("user", text); BUS.emit("user_input", text=_redact(text))
        cmd = self._command(text)
        if cmd == "__quit__": SPEAK.say("Goodbye."); raise SystemExit(0)
        if cmd: SPEAK.say(cmd); MEM.turn("sara", cmd); return cmd
        if self.paused: return "System is paused."
        with self.lock:
            if LEARN.is_correction(text): LEARN.apply_correction(text)
            LEARN.capture_instruction(text)
            goal = MEM.resolve(text); MEM.last_goal = goal; ctx = WATCHER.refresh(True) or WATCHER.current or AWARE.context(); ongoing = bool(LEARN.ONGOING.search(text))
            reply = self._run_goal(goal, ctx, trigger)
            if ongoing and not SESSIONS.active():
                info = SESSIONS.start(goal, ctx); SPEAK.say("I'll keep following along as you switch apps. Say 'done' when you're finished."); reply = f"{reply} {info['note']}".strip()
        MEM.turn("sara", reply or ""); return reply
    def _run_goal(self, goal, ctx, trigger, depth=0):
        try: plan = PLANNER.plan(goal, ctx)
        except ActionFailed as e: SPEAK.say(f"Planning failed: {e}"); return str(e)
        BUS.emit("plan", goal=_redact(goal), source=plan.source, steps=[s.dump() for s in plan.steps], say=plan.say)
        if plan.say and (not plan.steps or depth == 0): SPEAK.say(plan.say)
        if not plan.steps:
            for item in plan.learn: LTM.remember(item.get("kind", "fact"), str(item.get("content", "")), tags=("llm",), source="llm") if item.get("kind") in ("preference", "rule", "fact") else None
            return plan.say
        try: results = EXEC.run(plan.steps, trigger=trigger, ctx=ctx)
        except StaleContext as e:
            if depth >= 1: SPEAK.say("The screen keeps changing under me; I'll stop here."); return str(e)
            SPEAK.say("You switched windows, let me re-plan."); return self._run_goal(goal, WATCHER.refresh(True) or WATCHER.current, trigger, depth + 1)
        MEM.last_plan = plan.steps; status = LEARN.after_task(goal, plan.steps, results, trigger, (SESSIONS.cur.id if SESSIONS.cur else "-"), ctx, {"learn": plan.learn})
        reply = self._report(plan, results)
        if status == "completed" and not plan.done and depth < 3:   # model asked to see the results before finishing (e.g. answer a question with metrics)
            follow = self._run_goal(goal, ctx, trigger, depth + 1) if self._needs_followup(results) else ""
            reply = f"{reply} {follow}".strip()
        return reply
    @staticmethod
    def _needs_followup(results): return any(r.get("tool") in ("analyze_screen", "system", "files", "memory", "clipboard", "ask_user", "wifi", "bluetooth", "display", "window") and r.get("ok") for r in results)
    def _report(self, plan: Plan, results):
        fails = [r for r in results if not r.get("ok") and not r.get("skipped")]
        if not fails:
            if not plan.say: msg = "Done." if all(r.get("output", {}).get("verified") for r in results if not r.get("skipped")) else "Done, though I couldn't fully verify every step."; SPEAK.say(msg); return msg
            return plan.say
        f = fails[0]; hint = " I need administrator rights for that." if f.get("requires_admin") else " That's not supported on this machine." if f.get("unsupported") else " Screen capture is paused; say 'resume watching' first." if f.get("privacy") else ""
        msg = "Cancelled." if f.get("cancelled") else f"I couldn't finish: {f['tool']} failed - {str(f.get('error'))[:160]}.{hint}"
        SPEAK.say(msg); return msg
AGENT = Agent()
def handle_text(text): return AGENT.handle(text)   # backend convenience API

def shutdown():
    try: WATCHER.stop_ev.set(); EXEC.stop()
    except Exception: pass
    try:
        if SESSIONS.cur and SESSIONS.cur.status in ("active", "paused", "stalled"): SESSIONS.finish("shutdown", quiet=True)
    except Exception: pass
    try: SPEAK.q.put(None)
    except Exception: pass
    LTM.close(); log.info("SARA shut down cleanly")
atexit.register(shutdown)

def start_background():
    if not WATCHER.is_alive():
        try: WATCHER.start()
        except RuntimeError: pass
    threading.Thread(target=APPS.load_start_apps, daemon=True).start()

def banner():
    missing = sorted(k for k, v in CAPS.items() if v != "available")
    print(f"\n{APP_NAME} v5  |  model: {LLM_MODEL if LLM_API_KEY else 'OFFLINE (rules + learned workflows)'}  |  workspace: {ROOT}")
    print(f"tools: {len(REG)}  |  memory: {LTM.stats()['active_by_kind']}  |  capture: {'on' if AWARE.allowed() else 'off'}  |  screenshots->model: {'yes' if SEND_SCREENSHOTS else 'no'}")
    if missing: print(f"unavailable capabilities: {', '.join(missing)}")
    print("commands: 'pause watching' | 'resume watching' | 'task status' | 'done' | 'continue' | 'remember that ...' | 'forget ...' | 'what do you remember' | 'quit'\n")

def text_loop():
    while True:
        try: line = input("you > ").strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not line: continue
        try: AGENT.handle(line)
        except SystemExit: break
        except Exception as e: log.exception("handle failed"); SPEAK.say(f"Something went wrong: {e}")

def voice_loop():
    if not EARS.ok: SPEAK.say("No microphone or speech recognition available; falling back to keyboard."); return text_loop()
    MODE.voice = True; SPEAK.say(f"{APP_NAME} is listening." + (f" Say '{WAKE_WORD}' to get my attention." if WAKE_WORD else ""))
    while True:
        try:
            heard = EARS.listen(timeout=10, phrase_limit=15)
            if not heard: continue
            if WAKE_WORD:
                low = _norm(heard)
                if not low.startswith(WAKE_WORD) and WAKE_WORD not in low.split()[:2]: continue
                heard = re.sub(rf"^\W*{re.escape(WAKE_WORD)}\W*", "", heard, flags=re.I).strip()
                if not heard: SPEAK.say("Yes?", wait=True); heard = EARS.listen(timeout=8, phrase_limit=15)
                if not heard: continue
            SPEAK.drop(); AGENT.handle(heard)
        except SystemExit: break
        except KeyboardInterrupt: break
        except Exception as e: log.exception("voice loop"); SPEAK.say(f"Something went wrong: {e}")

def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v5 desktop agent"); ap.add_argument("--text", action="store_true", help="keyboard mode (default: voice)")
    ap.add_argument("--once", metavar="COMMAND", help="run a single command and exit"); ap.add_argument("--no-capture", action="store_true", help="start with screen capture disabled")
    a = ap.parse_args()
    if a.no_capture: AWARE.set(enabled=False)
    start_background(); banner()
    if a.once: AGENT.handle(a.once); return
    (text_loop if a.text else voice_loop)()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("\nbye")

# ============================================================================
# v4 COMPATIBILITY SURFACE (backend) — app/api/desktop_agent.py imports these:
#   AUTH (.locked/.auto_max/.authorize), llm_plan(goal)->(steps,say), EXEC.run_plan,
#   EXEC.summarize, AGENT.paused, BUS, CAPS, REG, MEM, stream_frame preview thread.
# Also re-registers native tools that v5 consolidated away so every tool stays
# reachable over HTTP (/tools/call) with identical behaviour.
# ============================================================================
class Authorizer:
    """API-facing policy gate used by desktop_agent.py and EXEC.run_plan (v4 API)."""
    def __init__(self): self.auto_max = Level.AUTHORIZED; self.locked = False; self.trusted = set(TRUSTED)
    def authorize(self, t: Tool, args: dict):
        if self.locked: raise PolicyViolation("Agent is locked. Say 'unlock agent'.")
        lvl = t.effective_level(args)
        if lvl <= self.auto_max: return
        if t.name in self.trusted: log.warning("trusted policy auto-approved %s %s", t.name, args); return
        prompt = t.confirm_prompt or f"Proceed with {t.name.replace('_', ' ')} {args.get('action', '')} {args.get('name') or args.get('path') or args.get('target') or ''}?"
        try: prompt = prompt.format(**{k: str(v)[:70] for k, v in args.items()})
        except Exception: pass
        if not ask_yes_no(prompt): raise PolicyViolation(f"'{t.name}' cancelled by the user.")
AUTH = Authorizer()


def _ctx_now():
    return WATCHER.current or AWARE.context()


def llm_plan(goal):
    """v4-compatible planner entry: returns (steps, say). Used by /tasks."""
    BUS.emit("planning", goal=goal)
    try:
        p = PLANNER.plan(goal, _ctx_now())
    except Exception as e:
        # The whole failover chain is down/paywalled. Don't go silent: tell the operator why, then
        # still try to serve the request from learned workflows + the offline rule grammar.
        log.error("Planner failed: %s", e)
        BUS.emit("planner_fallback", goal=goal, reason=str(e)[:300])
        p = None
        try:
            p = PLANNER.offline(goal, _ctx_now(), str(e))
        except Exception as e2:
            log.error("Offline planner failed: %s", e2)
        if p is None or not p.steps:
            reason = str(e)[:220]
            BUS.emit("plan_failed", goal=goal, error=reason)
            return [], ("I could not reach any language model for that. "
                        f"Reason: {reason}. Check the LLM keys/credits in backend/.env, then try again.")
    BUS.emit("plan_ready", steps=[s.dump() for s in p.steps], say=p.say, source=p.source)
    return p.steps, p.say


def replan(goal, failed: Step, error: str):
    try:
        extra = {"mode": "replan", "failed_step": failed.dump(), "error": error}
        return PLANNER.plan(goal, _ctx_now(), extra, temperature=0.2).steps[:6]
    except Exception as e:
        log.error("Replan failed: %s", e); return []


def chat_reply(goal):
    try: return llm_call("You are SARA, a concise spoken desktop assistant. Reply in at most two short sentences.", json.dumps({"user": goal, "context": MEM.brief()}), 0.4, 30).strip()
    except Exception: return "I could not reach the model right now."


# -- Executor.run_plan / summarize (v4 semantics: BUS events + critical-tool replan) --
def _exec_run_plan(self, goal, steps, depth=0):
    task_id, results = uuid.uuid4().hex[:10], []
    if depth == 0: self.cancel.clear()
    BUS.emit("task_started", task_id=task_id, goal=goal, plan=[s.dump() for s in steps])
    for i, step in enumerate(steps, 1):
        if self.cancel.is_set(): results.append({"tool": step.tool, "ok": False, "error": "cancelled", "cancelled": True}); break
        t = REG.get(step.tool)
        if not t: continue
        BUS.emit("step_started", task_id=task_id, step=i, tool=t.name, args=_redact(step.args), why=step.why, level=int(t.level), agent=APP_NAME)
        try:
            AUTH.authorize(t, step.args); out = t.run(step.args)
            results.append({"tool": t.name, "ok": True, "output": out}); MEM.action(t.name, step.args, True)
            BUS.emit("step_completed", task_id=task_id, step=i, tool=t.name, output=_redact(out), verified=bool(out.get("verified")))
            if not out.get("verified"): log.info("%s completed without verification: %s", t.name, out.get("note") or out.get("actual"))
        except PolicyViolation as e:
            results.append({"tool": t.name, "ok": False, "error": str(e), "policy": True}); BUS.emit("step_failed", task_id=task_id, step=i, tool=t.name, error=str(e)); break
        except Exception as e:
            info = getattr(e, "info", {}); err = f"{type(e).__name__}: {e}"
            MEM.action(t.name, step.args, False); BUS.emit("step_failed", task_id=task_id, step=i, tool=t.name, error=err, **{k: v for k, v in info.items() if isinstance(v, (bool, int, str))})
            recovered = False
            if t.critical and depth < 2 and not info.get("protected"):
                SPEAK.say("That did not work. Let me try another way.")
                alt = replan(goal, step, err + (" [requires administrator]" if info.get("requires_admin") else "") + (" [unsupported]" if info.get("unsupported") else ""))
                if alt:
                    sub = self.run_plan(goal, alt, depth + 1); results.extend(sub); recovered = bool(sub) and all(r.get("ok") for r in sub)
            if not recovered:
                results.append({"tool": t.name, "ok": False, "error": err, **{k: v for k, v in info.items() if isinstance(v, (bool, int, str))}})
                if t.critical: break
    status = "cancelled" if self.cancel.is_set() else "completed" if all(r.get("ok") for r in results) else "completed_with_errors"
    BUS.emit("task_finished", task_id=task_id, status=status); return results


def _exec_summarize(self, results):
    if not results: return "I could not work out how to do that."
    if any(r.get("cancelled") for r in results): return "Stopped."
    bad = [r for r in results if not r.get("ok")]
    if bad:
        f = bad[0]
        if f.get("policy"): return "Okay, cancelled."
        if f.get("requires_admin"): return f"{f['tool'].replace('_', ' ')} needs administrator rights, which I don't have."
        if f.get("unsupported"): return f"{f['tool'].replace('_', ' ')} is not supported on this machine."
        return f"I could not finish. {f['tool'].replace('_', ' ')} failed."
    unverified = [r for r in results if not r["output"].get("verified") and REG[r["tool"]].level >= Level.AUTHORIZED]
    return "Done, but I could not confirm the result." if unverified and len(unverified) == len(results) else "Done."


Executor.run_plan = _exec_run_plan  # type: ignore[assignment]
Executor.summarize = _exec_summarize  # type: ignore[assignment]


# Agent pause/unpause words (voice/text) mirror the API /pause + /resume buttons.
_agent_command_orig = Agent._command
def _agent_command_pause(self, text):
    t = _norm(text)
    if t in ("unpause agent", "unpause", "wake up", "resume agent", "resume assistant", "start working"):
        self.paused = False; SPEAK.say("Resumed."); return "Resumed."
    if t in ("pause agent", "pause assistant", "freeze", "go idle", "take a break"):
        self.paused = True; SPEAK.say("Paused."); return "Paused. Say 'unpause agent' to resume."
    return _agent_command_orig(self, text)
Agent._command = _agent_command_pause  # type: ignore[assignment]


# ============================ 13. STREAMING (frame preview for the UI) ============================
def stream_desktop():
    while True:
        try:
            if MSS and BUS.subs:
                from PIL import Image
                with MSS.mss() as s: shot = s.grab(s.monitors[1])
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"); img.thumbnail((800, 600)); buf = io.BytesIO(); img.save(buf, "JPEG", quality=40)
                BUS.emit("stream_frame", image="data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
        except Exception: pass
        _sleep(0.5 if BUS.subs else 2.0)
threading.Thread(target=stream_desktop, daemon=True, name="streamer").start()


# ============================ 14. RE-ADDED NATIVE TOOLS (kept from v4) ============================
class _AudioCtl:
    def _ep(self, kind):
        if not PYCAW: raise ActionFailed("pycaw missing.", unsupported=True)
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL, CoInitialize, cast, POINTER
        try: CoInitialize()
        except Exception: pass
        if kind == "output": dev = AudioUtilities.GetSpeakers()
        else:
            try: dev = AudioUtilities.GetMicrophone()
            except Exception: dev = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(1, 1)
        return cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    def get(self, kind="output"):
        ep = self._ep(kind); return {"percent": round(ep.GetMasterVolumeLevelScalar() * 100), "muted": bool(ep.GetMute()), "verified": True}
    def set(self, kind="output", value=None, delta=None, mute=None):
        before = self.get(kind); ep, target = self._ep(kind), before["percent"]
        if value is not None: target = int(value)
        elif delta is not None: target += int(delta)
        target = max(0, min(100, target))
        if value is not None or delta is not None:
            ep.SetMasterVolumeLevelScalar(target / 100.0, None)
            if target > 0 and before["muted"] and mute is None: ep.SetMute(0, None)
        if mute is not None: ep.SetMute(1 if mute else 0, None)
        _sleep(0.15); after = self.get(kind)
        return _res("pycaw", {"percent": target, "muted": mute}, after, abs(after["percent"] - target) <= 1 and (mute is None or after["muted"] == bool(mute)), previous=before)
    def _cmdlets(self): return bool(PS_EXE) and bool(run_ps("if (Get-Command Set-AudioDevice -ErrorAction SilentlyContinue) { 'yes' }", 25))
    def devices(self):
        if IS_WINDOWS and lazy_cap("audio_cmdlets", self._cmdlets):
            return [{"name": d.get("Name"), "type": d.get("Type"), "default": bool(d.get("Default")), "id": d.get("ID")}
                    for d in run_ps("ConvertTo-Json -Compress -InputObject @(Get-AudioDevice -List | Select Index,Default,Type,Name,ID)", 25, True) or []]
        if PYCAW:
            from pycaw.pycaw import AudioUtilities; out = []
            for d in AudioUtilities.GetAllDevices():
                try:
                    if getattr(d.state, "name", "Active") == "Active": out.append({"name": d.FriendlyName, "id": d.id, "type": None, "default": None})
                except Exception: pass
            return out
        raise ActionFailed("No audio device backend.", unsupported=True)
    def set_default(self, name, kind="output"):
        need_win("Audio device switch")
        if not lazy_cap("audio_cmdlets", self._cmdlets): raise ActionFailed("Needs PowerShell module AudioDeviceCmdlets (Install-Module AudioDeviceCmdlets). Use settings sound instead.", unsupported=True)
        want = "Playback" if kind == "output" else "Recording"; devs = [d for d in self.devices() if d["type"] == want]; n = _norm(name)
        m = next((d for d in devs if _norm(d["name"]) == n), None) or next((d for d in devs if n in _norm(d["name"])), None)
        if not m:
            cl = difflib.get_close_matches(n, [_norm(d["name"]) for d in devs], 1, 0.6); m = next((d for d in devs if cl and _norm(d["name"]) == cl[0]), None)
        if not m: raise ActionFailed(f"No {want} device matches '{name}'. Available: {[d['name'] for d in devs]}")
        run_ps(f"Set-AudioDevice -ID {_q(m['id'])} | Out-Null", 25); actual = run_ps(f"(Get-AudioDevice -{want}).Name", 25) or ""
        return _res("AudioDeviceCmdlets", m["name"], actual, _norm(actual) == _norm(m["name"]))
_AUD = _AudioCtl()


_RADIO_ALL = r'''[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) { $n = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $n.Wait(-1) | Out-Null; $n.Result }
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
'''


class _RadioCtl:
    KINDS = {"bluetooth": "Bluetooth", "wifi": "WiFi", "wi fi": "WiFi", "cellular": "MobileBroadband"}
    def status(self):
        need_win("Radios"); return run_ps(_RADIO_ALL + "ConvertTo-Json -Compress -InputObject @($radios | % { [pscustomobject]@{ kind=$_.Kind.ToString(); state=$_.State.ToString(); name=$_.Name } })", 40, True) or []
    def set(self, kind, on):
        need_win("Radios"); k, st = self.KINDS.get(_norm(kind), kind), "On" if on else "Off"
        data = run_ps(_RADIO_ALL + f"$out = foreach ($r in @($radios | ? {{ $_.Kind.ToString() -eq '{k}' }})) {{ $x = Await ($r.SetStateAsync([Windows.Devices.Radios.RadioState]::{st})) ([Windows.Devices.Radios.RadioAccessStatus]); [pscustomobject]@{{ result=$x.ToString(); state=$r.State.ToString() }} }}\n"
                      f"ConvertTo-Json -Compress -InputObject @{{ access=$access.ToString(); radios=@($out) }}", 40, True) or {}
        radios = data.get("radios") or []
        if not radios: raise ActionFailed(f"No {k} radio found.", unsupported=True)
        if data.get("access") not in (None, "Allowed"): raise ActionFailed(f"Radio access denied ({data.get('access')}). Use Settings.", unsupported=True)
        states = [r.get("state") for r in radios]; return _res("winrt_radio", st, states, all(s == st for s in states), results=[r.get("result") for r in radios])
_RADIO = _RadioCtl()


class _SvcCtl:
    def _get(self, name):
        need_win("Services")
        if not psutil: raise ActionFailed("psutil missing.", unsupported=True)
        try: return psutil.win_service_get(name)
        except psutil.NoSuchProcess: pass
        n = _norm(name)
        for s in psutil.win_service_iter():
            try:
                if _norm(s.name()) == n or _norm(s.display_name()) == n or n in _norm(s.display_name()): return s
            except psutil.Error: pass
        raise ActionFailed(f"No service matches '{name}'.")
    def status(self, name):
        d = self._get(name).as_dict(); return {k: d[k] for k in ("name", "display_name", "status", "start_type", "pid")} | {"verified": True}
    def control(self, name, action):
        s, a = self._get(name), _norm(action)
        if a not in ("start", "stop", "restart"): raise ActionFailed("action must be start|stop|restart")
        before = s.status()
        if (a, before) in (("start", "running"), ("stop", "stopped")): return _res("sc", a, before, True, note="already set")
        cmd = {"start": "Start-Service", "stop": "Stop-Service -Force", "restart": "Restart-Service -Force"}[a]
        try: run_ps(f"{cmd} -Name {_q(s.name())} -ErrorAction Stop", 60)
        except ActionFailed as e: raise ActionFailed(f"{cmd} '{s.name()}' failed: {e}. Service control usually needs administrator rights.", requires_admin=True)
        want = "stopped" if a == "stop" else "running"; ok = _poll(lambda: s.status() == want or None, 20.0, 1.0)
        return _res(cmd, want, s.status(), bool(ok), previous=before)
_SVC = _SvcCtl()


class _PkgCtl:
    def _need(self):
        if not cap_ok("winget"): raise ActionFailed("winget unavailable; install 'App Installer' from the Store.", unsupported=True)
    def _parse(self, text):
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]; h = next((i for i, l in enumerate(lines) if l.startswith("Name") and " Id" in l), None)
        if h is None: return []
        col = lines[h].index(" Id") + 1; out = []
        for l in lines[h + 2:]:
            name, rest = l[:col].strip(), l[col:].split()
            if name and rest: out.append({"name": name, "id": rest[0], "version": rest[1] if len(rest) > 1 else None})
        return out
    def installed(self, f=""):
        self._need(); return self._parse(run_cmd(["winget", "list", "--accept-source-agreements", "--disable-interactivity"] + (["--query", f] if f else []), 120, False).stdout)[:100]
    def _match(self, q): n = _norm(q); return [a for a in self.installed(q) if n in _norm(a["name"]) or n in _norm(a["id"])]
    def install(self, q):
        self._need()
        if self._match(q): return _res("winget", q, self._match(q), True, note="already installed")
        r = run_cmd(["winget", "install", q, "--silent", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"], 900, False)
        out = (r.stdout + r.stderr).strip()
        if r.returncode and re.search(r"administrator|elevat", out, re.I): raise ActionFailed(f"winget: '{q}' needs elevation. {out[-200:]}", requires_admin=True)
        found = self._match(q); return _res("winget install", q, found, bool(found), exit_code=r.returncode, output=out[-300:])
    def uninstall(self, q):
        self._need(); before = self._match(q)
        if not before: raise ActionFailed(f"No installed package matches '{q}'.")
        t = before[0]; r = run_cmd(["winget", "uninstall", "--id", t["id"], "--silent", "--accept-source-agreements", "--disable-interactivity"], 900, False)
        out = (r.stdout + r.stderr).strip()
        if r.returncode and re.search(r"administrator|elevat", out, re.I): raise ActionFailed(f"winget: uninstalling '{t['name']}' needs elevation.", requires_admin=True)
        still = [a for a in self._match(q) if a["id"] == t["id"]]
        return _res("winget uninstall", t, "removed" if not still else "still installed", not still, exit_code=r.returncode, output=out[-300:])
_PKG = _PkgCtl()


def _adapters_list():
    need_win("Adapters")
    return [{"name": a.get("Name"), "description": a.get("InterfaceDescription"), "status": a.get("Status"), "mac": a.get("MacAddress"), "speed": a.get("LinkSpeed")}
            for a in run_ps("ConvertTo-Json -Compress -InputObject @(Get-NetAdapter | Select Name,InterfaceDescription,Status,MacAddress,LinkSpeed)", 30, True) or []]


def _adapter_power(name, on):
    ads, n = _adapters_list(), _norm(name)
    ad = next((a for a in ads if _norm(a["name"]) == n), None) or next((a for a in ads if n in _norm(a["name"]) or n in _norm(a["description"] or "")), None)
    if not ad: raise ActionFailed(f"No adapter matches '{name}'. Adapters: {[a['name'] for a in ads]}")
    need_admin(f"{'Enabling' if on else 'Disabling'} adapter '{ad['name']}'")
    run_ps(f"{'Enable' if on else 'Disable'}-NetAdapter -Name {_q(ad['name'])} -Confirm:$false", 40); want = ("Up", "Disconnected") if on else ("Disabled",)
    after = _poll(lambda: next((a for a in _adapters_list() if a["name"] == ad["name"] and a["status"] in want), None), 12.0, 1.0)
    return _res("NetAdapter cmdlet", "enabled" if on else "disabled", (after or {}).get("status", "unknown"), bool(after), previous=ad["status"])


def _airplane(on=None):
    radios = _RADIO.status()
    if on is None: return {"approximation": True, "all_radios_off": bool(radios) and all(r["state"] != "On" for r in radios), "radios": radios, "verified": True}
    det = []
    for kind in sorted({r["kind"] for r in radios}):
        try: det.append(_RADIO.set(kind, not on))
        except ActionFailed as e: det.append({"kind": kind, "error": str(e)})
    after = _RADIO.status()
    return _res("winrt_radio(all)", "airplane on" if on else "airplane off", after, bool(after) and all((r["state"] != "On") == on for r in after), details=det, note="no public airplane API; radios toggled. Use settings airplane for the real switch.")


def _dt_info():
    now = datetime.now().astimezone()
    try: tz = run_cmd(["tzutil", "/g"], 5).stdout.strip() if cap_ok("tzutil") else time.tzname[0]
    except ActionFailed: tz = time.tzname[0]
    return {"local": now.isoformat(timespec="seconds"), "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "timezone_id": tz, "utc_offset": now.strftime("%z"), "weekday": now.strftime("%A"), "verified": True}


@tool("screen_capture", "Return a screenshot of the active monitor as an image data-URL (scale 0.05-1).", Level.LOW, critical=False, requires=("screen_capture",), category="perception", scale="float")
def t_screen_capture(scale=0.4):
    from PIL import Image
    img, _ = AWARE.capture("active")
    max_w = max(160, int(1920 * max(0.05, min(1.0, float(scale or 0.4)))))
    img.thumbnail((max_w, max_w * 3 // 4)); buf = io.BytesIO(); img.save(buf, "JPEG", quality=55)
    return {"image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), "verified": True}


@tool("capabilities", "Report available capabilities/dependencies and admin status.", Level.INFO, critical=False, category="system")
def t_caps(): return {"caps": dict(CAPS), "admin": is_admin(), "os": f"{OS_NAME} {platform.release()}", "verified": True}


@tool("datetime_info", "Current local/UTC time, weekday, time zone.", Level.INFO, critical=False, category="system")
def t_dt(): return _dt_info()


@tool("battery_status", "Battery percent, charging state, minutes left.", Level.INFO, critical=False, requires=("system_metrics",), category="system")
def t_battery():
    b = SYS.battery()
    if b is None: raise ActionFailed("No battery detected.", unsupported=True)
    return {**b, "verified": True}


@tool("microphone", "Default microphone: action get|mute|unmute|level (value 0-100)|set_default (name).", Level.AUTHORIZED, requires=("audio_endpoint",), category="audio", action="str!", value="int", name="str")
def t_mic(action, value=None, name=None):
    a = _norm(action)
    if a == "get": return _AUD.get("input")
    if a in ("mute", "unmute"): return _AUD.set("input", mute=a == "mute")
    if a == "level":
        if value is None: raise PolicyViolation("level needs value")
        return _AUD.set("input", value=value)
    if a in ("set default", "set_default"):
        if not name: raise PolicyViolation("set_default needs name")
        return _AUD.set_default(name, "input")
    raise PolicyViolation("action must be get|mute|unmute|level|set_default")


@tool("audio_device", "action list | set_default (name) for playback devices.", Level.AUTHORIZED, category="audio", action="str!", name="str")
def t_audio_dev(action, name=None):
    a = _norm(action)
    if a == "list": return {"devices": _AUD.devices(), "verified": True}
    if a in ("set default", "set_default"):
        if not name: raise PolicyViolation("set_default needs name")
        return _AUD.set_default(name, "output")
    raise PolicyViolation("action must be list|set_default")


@tool("network_adapter", "action list|enable|disable (name). enable/disable need administrator.", Level.AUTHORIZED, confirm_actions=("disable",), requires=("windows", "powershell"), category="connectivity", action="str!", name="str")
def t_adapter(action, name=None):
    a = _norm(action)
    if a == "list": return {"adapters": _adapters_list(), "verified": True}
    if a in ("enable", "disable"):
        if not name: raise PolicyViolation(f"{a} needs name")
        return _adapter_power(name, a == "enable")
    raise PolicyViolation("action must be list|enable|disable")


@tool("airplane_mode", "action get|on|off (approximation: toggles all radios).", Level.AUTHORIZED, confirm_actions=("on",), requires=("windows", "powershell"), category="connectivity", action="str!")
def t_airplane(action):
    a = _norm(action)
    if a == "get": return _airplane(None)
    if a in ("on", "off"): return _airplane(a == "on")
    raise PolicyViolation("action must be get|on|off")


@tool("service", "Windows service: action status|start|stop|restart (name).", Level.AUTHORIZED, confirm_actions=("stop", "restart"), requires=("windows", "powershell", "system_metrics"), category="services",
      action="str!", name="str!")
def t_service(action, name):
    a = _norm(action); return _SVC.status(name) if a == "status" else _SVC.control(name, a)


@tool("app_package", "winget: action list|install|uninstall (query = name or id).", Level.AUTHORIZED, confirm_actions=("install", "uninstall"), requires=("windows", "winget"), category="apps",
      action="str!", query="str")
def t_pkg(action, query=""):
    a = _norm(action)
    if a == "list": return {"installed": _PKG.installed(query or ""), "verified": True}
    if not query: raise PolicyViolation(f"{a} needs query")
    if a == "install": return _PKG.install(query)
    if a == "uninstall": return _PKG.uninstall(query)
    raise PolicyViolation("action must be list|install|uninstall")


@tool("youtube_play", "Play a specific song or video on YouTube by searching and opening the top result.", Level.LOW, category="browser", query="str!")
def t_yt(query):
    SPEAK.say(f"Playing {query} on YouTube."); search = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    try:
        page = urllib.request.urlopen(urllib.request.Request(search, headers={"User-Agent": "Mozilla/5.0"}), timeout=6).read().decode("utf-8", "replace")
        ids = re.findall(r"watch\?v=([A-Za-z0-9_-]{11})", page)
        if ids:
            url = "https://www.youtube.com/watch?v=" + ids[0]; webbrowser.open(url); MEM.set("media", query); MEM.set("url", url)
            ok = bool(_poll(lambda: "youtube" in _norm(foreground_title()) or None, 6.0)); return _res("youtube_direct", query, url, ok, url=url)
    except Exception as e: log.error("YT fetch failed: %s", e)
    webbrowser.open(search); return _res("youtube_search", query, search, False, note="search page")


@tool("media_control", "Global media keys: play/pause, next, previous, volume up/down, mute.", Level.AUTHORIZED, requires=("desktop_input",), category="media", command="str!")
def t_media(command):
    c = _norm(command)
    key = ("nexttrack" if any(k in c for k in ("next", "skip")) else "prevtrack" if any(k in c for k in ("prev", "back", "last")) else "volumeup" if any(k in c for k in ("up", "increase", "louder"))
           else "volumedown" if any(k in c for k in ("down", "decrease", "quieter")) else "volumemute" if "mute" in c else "playpause")
    before = None
    if key.startswith("volume"):
        try: before = _AUD.get()
        except ActionFailed: pass
    PG.press(key, presses=5 if key in ("volumeup", "volumedown") else 1); _sleep(0.3)
    if before is not None:
        try: after = _AUD.get(); return _res("media_key", key, after, after != before, previous=before)
        except ActionFailed: pass
    return _res("media_key", key, "key sent", False)


# Keep screen-context tracking alive when running under the unified backend too
# (CLI main() calls this again; it is guarded/idempotent).
start_background()
