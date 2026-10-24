
import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen

# ─────────────────────────────────────────────────────────────────────────────

# ── Console encoding ─────────────────────────────────────────────────────────
# Status lines in this app carry emoji and arrows ("📤 file_controller → Moved:
# a.txt → Documents/"). On a non-UTF-8 console — cp1254 on a Turkish Windows,
# cp1251 on a Russian one, cp932 on a Japanese one — printing one of those
# raises UnicodeEncodeError, and because the print sits after the tool's own
# try/except, the exception escapes into the receive loop and takes the session
# down. The assistant dies on a log line.
#
# Reconfiguring costs nothing and makes the app start the same way in every
# locale. `errors="replace"` is the belt and braces — a console that genuinely
# cannot render a glyph shows a box instead of killing the process.
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import SaraUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_sara,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_voice, get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
    get_screen_watch_enabled, save_screen_watch_enabled,
    get_screen_watch_interval, save_screen_watch_interval,
)
from core.plugin_loader        import discover_plugins
from core.learned_rules        import LearnedRulesEngine
from core.window_context       import get_foreground_prompt_line
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.agent_registry       import (
    AgentInfo as _AgenticInfo, AgentState as _AgenticState,
    UnifiedAgentRegistry as AgenticRegistry,
    get_global_registry as _get_agentic_registry,
)
from core.agentic_runtime      import (
    AgenticRuntime, RuntimeMode, TaskStatus as AgenticTaskStatus,
)
from core.agent_router         import TaskRouter as _AgenticRouter

# Agentic Mode tagged-message directives understood by prompt.txt's
# [TAGGED MESSAGES] convention (app-originated, never read aloud).
from core.action_loader        import discover_actions
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are SARA, SARA Administrator's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see SaraLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "screen_watch",
        "description": (
            "Controls CONTINUOUS screen vision. "
            "Use 'start' when the user says: watch my screen, keep an eye on my "
            "screen, screen vision on, look at my screen continuously — in ANY "
            "language. While active, you receive a fresh screenshot every ~20 "
            "seconds as silent context, so you always know what is displayed "
            "and can answer questions about it without capturing again. "
            "Use 'stop' when the user says: stop watching, screen vision off, "
            "stop looking at my screen. "
            "Use 'status' to check whether it is on or off. "
            "The setting persists across restarts until turned off."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "start | stop | status",
                },
                "interval": {
                    "type": "STRING",
                    "description": "Optional: seconds between frames, 5-120 (default 20). Only used with 'start'.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "SARA checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_sara",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Sara. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
    {
        "name": "show_result",
        "description": (
            "Open the Result Viewer showing the REAL outcome of a finished "
            "task — result text, produced files/audio, the completion "
            "screenshot. Call when the user asks to see a result, the "
            "report, the last output, or 'show me what you did'. Optional "
            "'task' narrows to a task by keyword; otherwise the latest "
            "finished task is shown. NEVER call for work still running."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {
                    "type": "STRING",
                    "description": "Optional keyword(s) of the task to show.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "show_screen",
        "description": (
            "Open or close the Show Screen panel: a live view of the real "
            "screen plus current task status, INSIDE the desktop app — it "
            "never opens an external browser. Use when the user says show "
            "screen, show me the screen, show what you see, or close the "
            "screen view."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "open (default) or close.",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class SaraLive:
    def __init__(self, ui: SaraUI):
        self.ui             = ui
        self._asst_name     = "SARA"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        # Continuous screen vision: while True, a background task feeds frames
        # of the screen into the session as silent context (see
        # _run_screen_watch). Persisted so it survives restarts.
        self._screen_watch_active  = get_screen_watch_enabled()
        self._screen_watch_interval = get_screen_watch_interval()
        self._interrupted          = False   # True while draining audio after user interrupt
        # ── Echo tail + push-to-talk state (Mark-LV carry-over) ───────────────
        self._echo                 = None      # core.echo.EchoGuard — built lazily
        self._out_latency          = 0.24      # overwritten by _play_audio
        self._tail_until           = 0.0       # monotonic expiry of the speaker tail
        self._out_level            = 0.0
        self._ptt: object | None   = None      # core.hotkey.PushToTalk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.on_mode_change    = self._on_mode_changed       # 4-mode selector

        # ── Agentic Mode runtime (Jarvis side; backend/website untouched) ───
        self.agentic = AgenticRuntime(
            registry=_get_agentic_registry(),
            emit=self._on_agentic_event,
            confirm=self._agentic_confirm_gate,
        )
        self.agentic.on_mode_change = self._announce_mode_change
        self.agentic.router = _AgenticRouter(self.agentic.registry)
        self._agentic_results: Dict[str, Any] = {}   # finished task results
        # utterance dedupe boundary: identical speech twice never starts 2 tasks
        self._agentic_seen: Dict[str, float] = {}
        # activity-log dedupe: the fleet refresh fires one "agents" event per
        # registered agent per probe, and blocked agents re-fire
        # "agent_state: blocked" every cycle. Logging those verbatim produced
        # the "[agentic:agents] fleet" flood; only real changes log now.
        self._agentic_counts_sig: tuple = ()
        self._agentic_state_sig: Dict[str, str] = {}
        # completion pipeline (spec §9): one report+screenshot+notify per task
        self._notified_tasks: set = set()
        self._phone_notifier_ref = None
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        # The three agentic_* tools are appended AFTER discovery so actions can
        # never shadow them; they are mode-guarded at execution time.
        agentic_decls = [
            {
                "name": "agentic_task",
                "description": (
                    "Execute a real-world task through the SARA agent registry — "
                    "ONLY in SARA AGENTIC mode (the system prompt says when it is "
                    "active). Web research/scraping, emails, messages, music, "
                    "reports, multi-agent workflows. Pass the user's full request "
                    "verbatim as 'request'. Task runs in the background; the result "
                    "is reported back to this conversation when it finishes. "
                    "Do NOT claim the work is done until that result arrives."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "request": {
                            "type": "STRING",
                            "description": "The user's task request, verbatim.",
                        },
                    },
                    "required": ["request"],
                },
            },
            {
                "name": "agentic_status",
                "description": (
                    "Report the real state of Agentic Mode: the active mode, the "
                    "registered/ready/blocked agents, and the current/last tasks. "
                    "Call when the user asks what you are doing, whether a task "
                    "finished, or what the result was."
                ),
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "agentic_cancel",
                "description": (
                    "Stop the currently running agentic task at the user's request. "
                    "Call when the user says stop, cancel, or never mind that task. "
                    "Reports whether the underlying operation really stopped."
                ),
                "parameters": {"type": "OBJECT", "properties": {}},
            },
        ]
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
            extra_decls=agentic_decls,
        )
        # The agentic registry sees the SAME validated actions Jarvis itself
        # uses — with runnable handlers, so Agentic Mode can execute local
        # steps through the exact same code path as the voice session.
        self.agentic.registry.discover_all(
            _base_dir / "actions", _base_dir / "plugins",
            _inline_names, action_registry=self._action_registry,
        )
        self.agentic.initialize(
            actions_dir=_base_dir / "actions",
            plugins_dir=_base_dir / "plugins",
            reserved=_inline_names,
            action_registry=self._action_registry,
        )
        # Plugins must not collide with either an inline tool or a discovered action.

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: (print(f"[Wake] {m}"), self.ui.write_log(f"SYS: {m}")),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Sara' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Sara' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        return wake_install(logger=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask SARA to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        # SARA AGENTIC: typed commands go through the router (plan/execute/
        # clarify); conversational text falls back into the session below.
        if self.agentic.agentic_enabled:
            decision = self.agentic.submit(text, source="text")
            self._handle_text_decision(decision, text)
            return
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Sara" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Sara' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _handle_text_decision(self, decision: dict, text: str):
        """Route the outcome of an AGENTIC-mode typed command.

        execute → worker already started; fall through into the session so the
        model can acknowledge naturally. clarify → ask the user on screen AND
        in the session. converse/duplicate/error → plain pass-through text.
        The session fallback never duplicates work: planning happens once,
        here.
        """
        kind = decision.get("kind", "error")
        if kind == "execute":
            agents = ", ".join(decision.get("agents", []))
            self.ui.write_log(f"SYS: agentic task started → {agents}")
            pass_text = (f"[AGENTIC TYPED] I typed: '{text}'. Your agentic "
                         f"runtime started the task with agents: {agents}. "
                         f"Say ONE short sentence that work has begun, then "
                         f"wait for the [TASK_RESULT] message. Do NOT invent "
                         f"any outcome.")
        elif kind == "clarify":
            q = decision.get("question", "")
            self.ui.write_log(f"SYS: agentic clarification needed — {q}")
            pass_text = (f"[AGENTIC TYPED] I typed: '{text}'. The router "
                         f"needs one clarification before it can act: {q} "
                         f"Ask it in one short sentence in my language.")
        elif kind == "duplicate":
            pass_text = (f"[AGENTIC TYPED] I typed: '{text}'. That request is "
                         f"already running — do not start it again, just say "
                         f"it is in progress.")
        elif kind == "error":
            self.ui.write_log(f"ERR: agentic text command failed — "
                              f"{decision.get('error', 'unknown')}")
            return
        else:   # converse
            pass_text = text
        if self._loop and self.session:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": pass_text}]},
                        turn_complete=True,
                    ),
                    self._loop,
                )
            except Exception:
                pass

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        # ── Echo tail ──────────────────────────────────────────────────────────
        # When speech stops, sound is still leaving the speakers for one
        # buffer. The mic is not muted during it — EchoGuard still passes a
        # genuine reply while dropping our own echo — so replying instantly
        # still works. Exactly the Mark-LV mechanism.
        if value:
            self._tail_until = 0.0  # type: ignore[attr-defined]
        else:
            try:
                self._tail_until = time.monotonic() + float(getattr(self, "_out_latency", 0.24)) + 0.18  # type: ignore[attr-defined]
            except Exception:
                pass
            # The echo history is NOT cleared at tail start: the tail still needs
            # it. It is dropped when the tail expires (see _listen_audio).
            try:
                self._out_level = 0.0  # type: ignore[attr-defined]
            except Exception:
                pass
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop SARA mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[SARA] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")
        if self._dashboard and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._dashboard.broadcast({"type": "audio_stop"}), self._loop
                )
            except Exception:
                pass

    # ── Push-to-talk — hold Ctrl+Space to talk (Mark-LV carry-over) ──────────

    def _tail_active(self) -> bool:
        try:
            return time.monotonic() < float(getattr(self, "_tail_until", 0.0))
        except Exception:
            return False

    def _on_ptt(self, held: bool) -> None:
        self._ptt_held = bool(held)  # type: ignore[attr-defined]
        if held and getattr(self, "_wake_enabled", False) and not getattr(self, "_awake", True):
            self._awake = True  # type: ignore[attr-defined]
            self._last_user_speech = time.monotonic()
        try:
            self.ui.set_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope achieved."""
        from core.hotkey import PushToTalk
        self._ptt_enabled = bool(enabled)  # type: ignore[attr-defined]
        self._ptt_held = False  # type: ignore[attr-defined]
        if not enabled:
            ptt = getattr(self, "_ptt", None)
            if ptt is not None:
                try:
                    ptt.stop()
                except Exception:
                    pass
                self._ptt = None  # type: ignore[attr-defined]
            return "off"
        if getattr(self, "_ptt", None) is None:
            self._ptt = PushToTalk(self._on_ptt)  # type: ignore[attr-defined]
        try:
            scope = self._ptt.start()  # type: ignore[attr-defined]
        except Exception as e:
            return f"error: {e}"
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"  # type: ignore[attr-defined]
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "SARA").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "SARA"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Address the user with the ordinary respectful form "
                      "for a superior in the language you are currently speaking — "
                      "\"sir\" in English, its everyday equivalent in any other "
                      "language. Never an archaic or aristocratic form, and never "
                      "the form from a different language than the one you are "
                      "speaking in this sentence.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        # Learned behavioral rules (user corrections that persist) and the
        # foreground-window hint keep the prompt current without code edits.
        try:
            rules_str = LearnedRulesEngine.get_prompt_injections()
        except Exception:
            rules_str = ""
        if rules_str:
            parts.append(rules_str)
        try:
            win_line = get_foreground_prompt_line()
        except Exception:
            win_line = ""
        if win_line:
            parts.append(win_line)
        parts.append(sys_prompt)

        # Agentic Mode section — unconditional by design: it declares which
        # mode is active and the model-side routing rules for it, while the
        # tool handlers enforce the same rules. In NORMAL mode this reads as
        # "disabled, never call", so it cannot change normal behaviour.
        for _line in self._agentic_prompt_lines():
            if _line and _line not in parts:
                parts.append(_line)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": (
                TOOL_DECLARATIONS
                + self._action_registry.get_tool_declarations()
                + self._plugin_registry.get_tool_declarations()
            )}],
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — SARA can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: SARA stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[SARA] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        # ── Agentic Mode tools: mode-guarded at the door. In SARA NORMAL they
        # refuse immediately, so the agentic runtime can never interfere with
        # the existing normal-mode command paths (spec §3).
        if name in ("agentic_task", "agentic_status", "agentic_cancel"):
            return await self._execute_agentic(name, args, fc)

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_running_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "screen_watch":
                action = args.get("action", "status").lower().strip()
                if action == "start":
                    _iv = args.get("interval")
                    if _iv:
                        try:
                            self._screen_watch_interval = min(120.0, max(5.0, float(_iv)))
                            save_screen_watch_interval(self._screen_watch_interval)
                        except (TypeError, ValueError):
                            pass
                    self._screen_watch_active = True
                    save_screen_watch_enabled(True)
                    # First frame immediately, so the model has context at once
                    try:
                        loop = asyncio.get_event_loop()
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        if self.session:
                            import base64 as _b64
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [
                                    {"inline_data": {"mime_type": mime_t,
                                                     "data": _b64.b64encode(img_b).decode("ascii")}},
                                    {"text": "[SCREEN VISION ON] You now see the user's screen continuously. "
                                             "Do NOT speak about this frame. Stay quiet unless spoken to."},
                                ]},
                                turn_complete=False,
                            )
                    except Exception as e:
                        print(f"[ScreenWatch] ⚠️ first frame: {e}")
                    self.ui.write_log(f"SYS: Continuous screen vision ON "
                                      f"(every {self._screen_watch_interval:.0f}s).")
                    result = (f"Continuous screen vision is now ON — a fresh screenshot is delivered "
                              f"every {self._screen_watch_interval:.0f} seconds. Tell the user briefly.")
                elif action == "stop":
                    self._screen_watch_active = False
                    save_screen_watch_enabled(False)
                    self.ui.write_log("SYS: Continuous screen vision OFF.")
                    result = "Continuous screen vision is now OFF. Tell the user briefly."
                else:
                    result = (f"Screen vision is {'ON' if self._screen_watch_active else 'OFF'}"
                              + (f", refreshing every {self._screen_watch_interval:.0f}s."
                                 if self._screen_watch_active else ". Say 'start' to enable."))

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_sara":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name == "show_result":
                result = self._tool_show_result(args)

            elif name == "show_screen":
                result = self._tool_show_screen(args)

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                elif self._action_registry.has(name):
                    # entry-point-only declarations (agentic_*) when the mode
                    # guard above was bypassed — keep the door closed here too.
                    result = (f"Tool '{name}' is a session-layer action and "
                              f"cannot run from the action registry.")
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[SARA] 📤 {name} → {str(result)[:80]}")
        self._mirror_action(name, args, result)
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    # ── Agentic Mode tool handlers (mode-guarded session-layer actions) ────

    async def _execute_agentic(self, name: str, args: dict, fc):
        """agentic_task / agentic_status / agentic_cancel with intelligent fallback."""
        # In ADAPTIVE mode, the fallback chain is handled by the LLM's tool calls
        # but we also provide a direct execution path for when the LLM decides to use it
        if not self.agentic.agentic_enabled:
            self.ui.set_state("LISTENING")
            msg = ("Agentic Mode is currently off — that request was not "
                   "routed to any agent. The user can switch it on with the "
                   "mode selector on the HUD.")
            return types.FunctionResponse(
                id=fc.id, name=name, response={"result": msg})

        if name == "agentic_status":
            summary = self.agentic.status_summary()
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": self._format_agentic_status(summary)})

        if name == "agentic_cancel":
            loop_c = asyncio.get_running_loop()
            outcome = await loop_c.run_in_executor(
                None, lambda: self.agentic.cancel())
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": self._format_agentic_cancel(outcome)})

        request = str(args.get("request") or "").strip()
        if not request:
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "Empty task request — nothing to do."})

        # In ADAPTIVE mode, run the intelligent fallback chain
        if self.agentic.mode == RuntimeMode.ADAPTIVE:
            return await self._execute_with_fallback(request, fc)

        loop_t = asyncio.get_running_loop()
        decision = await loop_t.run_in_executor(
            None, lambda: self.agentic.submit(request, source="voice"))
        # SARA BOTS mode: when the router finds no executable path at all,
        # give the Sara Bots framework (approved local bots) a real try
        # before answering. Conversation/clarify outcomes are left alone.
        if self.agentic.uses_sara_bots and decision.get("kind") == "error":
            bot_result = await self._try_sara_bots(request)
            if bot_result:
                self.ui.set_state("LISTENING")
                return types.FunctionResponse(
                    id=fc.id, name=name, response={"result": bot_result})
        return await self._decision_response(decision, fc)

    async def _execute_with_fallback(self, request: str, fc) -> types.FunctionResponse:
        """Intelligent execution fallback chain for ADAPTIVE mode.
        
        Stage 1: Direct execution (local tools)
        Stage 2: Sara Agentic Mode (agent registry)
        Stage 3: Existing Sara Bot
        Stage 4: Create specialized Sara Bot
        Stage 5: Report blocker
        """
        self.ui.write_log(f"SYS: ADAPTIVE fallback started for: {request[:80]}")
        
        loop_t = asyncio.get_running_loop()

        # Stage 1: direct LOCAL action fast path — an unambiguous desktop
        # action (strict score + margin, non-consequential, not in the
        # danger set) matched without the LLM. Execution still goes through
        # the SAME verified runtime machinery as Stage 2 (events, steps,
        # cancel, task store).
        forced = None
        try:
            forced = await loop_t.run_in_executor(
                None, lambda: self.agentic.router.local_fast_plan(request))
        except Exception:
            forced = None
        if forced:
            decision = await loop_t.run_in_executor(
                None, lambda: self.agentic.submit(request, source="voice",
                                                  forced=forced))
            if decision.get("kind") == "execute":
                self.ui.write_log(
                    f"SYS: ADAPTIVE Stage 1 — direct local action "
                    f"({', '.join(forced.get('agents') or [])})")
                return await self._decision_response(decision, fc)
            self.ui.write_log("SYS: ADAPTIVE Stage 1 — direct match fell "
                              "through to the planner")

        # Stage 2: Sara Agentic Mode (LLM-planned over the real registry)
        self.ui.write_log("SYS: ADAPTIVE Stage 2 — routing to agent registry")
        decision = await loop_t.run_in_executor(
            None, lambda: self.agentic.submit(request, source="voice"))

        kind = decision.get("kind")
        if kind == "execute":
            self.ui.write_log("SYS: ADAPTIVE Stage 2 succeeded (agentic)")
            return await self._decision_response(decision, fc)
        if kind in ("converse", "duplicate") or (
                kind == "clarify" and not decision.get("no_agent")):
            # the router answered directly (smalltalk / dedupe hit / a real
            # missing fact) — no fallback chain, just the real response
            self.ui.write_log("SYS: ADAPTIVE Stage 2 — direct response "
                              "(no execution needed)")
            return await self._decision_response(decision, fc)
        
        # Stage 3: Try existing Sara Bot
        self.ui.write_log("SYS: ADAPTIVE Stage 3 — checking Sara Bots registry")
        bot_result = await self._try_sara_bots(request)
        if bot_result:
            self.ui.write_log("SYS: ADAPTIVE Stage 3 succeeded (Sara Bot)")
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name="agentic_task",
                response={"result": f"[SARA BOT] {bot_result}"})

        # Stage 4: create a specialized Sara Bot (real) and run the task once
        self.ui.write_log("SYS: ADAPTIVE Stage 4 — creating a specialized "
                          "Sara Bot for this capability")
        bot_proposal = await self._propose_specialized_bot(request)
        if bot_proposal:
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name="agentic_task",
                response={"result": bot_proposal})

        # Stage 5: Report blocker
        self.ui.write_log("SYS: ADAPTIVE Stage 5 — all routes exhausted")
        blocker_msg = self._format_blocker_report(request)
        self.ui.set_state("LISTENING")
        return types.FunctionResponse(
            id=fc.id, name="agentic_task",
            response={"result": blocker_msg})

    async def _try_direct_execution(self, request: str) -> Optional[str]:
        """Stage 1 helper: the unambiguous-local-action fast path.

        Returns the matched desktop action's agent id, or None. The actual
        execution happens in ``_execute_with_fallback`` through
        ``AgenticRuntime.submit(..., forced=...)`` — one verified path.
        """
        try:
            loop_t = asyncio.get_running_loop()
            forced = await loop_t.run_in_executor(
                None, lambda: self.agentic.router.local_fast_plan(request))
        except Exception:
            return None
        if not forced:
            return None
        agents = forced.get("agents") or []
        return agents[0] if agents else None

    async def _try_sara_bots(self, request: str) -> Optional[str]:
        """Stage 3: find and RUN a matching approved Sara Bot (real result).

        Delegates to ``core.sara_bots_client``: the platform's Sara Bots
        service keeps discovery, approval and the sandboxed worker; this
        method only matches the request and reports the run's real output.
        """
        try:
            from core.sara_bots_client import (format_result, list_bots,
                                               match_and_run)
        except Exception as exc:      # honest skip — never a fake success
            self.ui.write_log(f"SYS: Sara Bots client unavailable: {exc}")
            return None

        loop_t = asyncio.get_running_loop()

        # 3a. Reuse a previously created custom bot when its purpose matches
        # (spec §7: never create duplicates — run the existing worker).
        try:
            from core.sara_bots_client import (format_custom_result,
                                               match_custom_bot)
            custom = await loop_t.run_in_executor(
                None,
                lambda: match_custom_bot(
                    request,
                    progress=lambda s: self.ui.write_log(
                        f"SYS: custom bot → {s}")))
            if custom.get("ran"):
                self.ui.write_log(f"SYS: custom Sara Bot "
                                  f"{custom.get('bot_id')} → "
                                  f"{custom.get('status')}")
                return format_custom_result(custom)
        except Exception as exc:
            self.ui.write_log(f"SYS: custom-bot reuse check failed: {exc}")

        # 3b. Platform-approved local bots (the discovered fleet)
        result = await loop_t.run_in_executor(None,
                                              lambda: match_and_run(request))
        if result.get("ran"):
            self.ui.write_log(f"SYS: Sara Bot {result.get('bot_id')} → "
                              f"{result.get('status')} "
                              f"({result.get('run_id')})")
            return format_result(result)
        self.ui.write_log(f"SYS: Sara Bots — {result.get('reason')}")

        # Nothing ran: keep the discovery pointer for SaraMuse (unchanged).
        try:
            for bot in await loop_t.run_in_executor(None, list_bots):
                if bot.get("id") == "saramuse":
                    return ("SaraMuse autonomous agent is available at its "
                            "workspace. Open /hub/sara-muse to use it.")
        except Exception as exc:
            self.ui.write_log(f"SYS: Sara Bots check failed: {exc}")
        return None

    async def _propose_specialized_bot(self, request: str) -> Optional[str]:
        """Stage 4: create a REAL specialized Sara Bot and run the task once.

        Reached only after the registry, existing bots and custom bots all
        failed. The worker is created through the platform's custom-bots API
        (validated against the live tool registry), the goal is submitted as
        a durable engine task, then bounded-polled. Honest blockers (no
        suitable tool, approval required, still running) are reported as
        such — never as success (spec §6).
        """
        try:
            from core.sara_bots_client import (create_and_run_custom_bot,
                                               format_custom_result)
        except Exception as exc:
            self.ui.write_log(f"SYS: Sara Bots client unavailable: {exc}")
            return None
        self.ui.write_log("SYS: ADAPTIVE Stage 4 — creating a "
                          "specialized Sara Bot…")
        loop_t = asyncio.get_running_loop()
        result = await loop_t.run_in_executor(
            None,
            lambda: create_and_run_custom_bot(
                request, wait_s=240.0,
                progress=lambda s: self.ui.write_log(f"SYS: Stage 4 → {s}")))
        if not result.get("ran"):
            self.ui.write_log(f"SYS: ADAPTIVE Stage 4 declined — "
                              f"{result.get('reason')}")
            return None
        self.ui.write_log(f"SYS: ADAPTIVE Stage 4 — bot "
                          f"{result.get('bot_id')} task "
                          f"{str(result.get('task_id'))[:8]} → "
                          f"{result.get('status')}")
        # record + screenshot + phone notify for a terminal outcome
        if str(result.get("status")) in ("completed", "failed", "timed_out",
                                         "cancelled"):
            synth = {
                "task_id": f"custom-{result.get('task_id')}",
                "request": request,
                "status": str(result.get("status")),
                "agents": [str(result.get("bot_id") or "")],
                "result": str(result.get("output") or ""),
                "error": str(result.get("error") or ""),
                "source": "adaptive-stage4",
                "artifacts": result.get("artifacts") or [],
            }
            phase = ("completed" if result.get("status") == "completed"
                     else "failed")
            self._on_task_completed(synth, phase)
        return format_custom_result(result)

    def _format_blocker_report(self, request: str) -> str:
        """Stage 5: Format a blocker report when all routes fail."""
        return (
            f"All permitted execution routes were attempted for:\n"
            f"  \"{request}\"\n\n"
            f"Routes tried:\n"
            f"  1. Direct local action match — no unambiguous local action\n"
            f"  2. Agentic Mode (agent registry) — no suitable agent\n"
            f"  3. Sara Bots (approved + custom) — no matching bot ran\n"
            f"  4. Specialized bot creation — declined (see reason above)\n\n"
            f"Next steps:\n"
            f"  • Approve a new specialized bot (see Stage 4 proposal)\n"
            f"  • Check if required credentials/integrations are configured\n"
            f"  • Verify the request is within SARA's supported scope\n"
            f"  • Try rephrasing the request with more specific details"
        )

    async def _decision_response(self, decision: dict, fc):
        """One router decision → the FunctionResponse the model sees."""
        kind = decision.get("kind", "error")
        if kind == "clarify":
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"result":
                    f"[NEEDS CLARIFICATION] {decision.get('question', '')} "
                    f"Ask the user this one question in their own language."})
        if kind == "duplicate":
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"result":
                    f"That request is already running (task "
                    f"{str(decision.get('task_id', ''))[:8]}). Do not repeat "
                    f"it; report progress normally."})
        if kind == "converse":
            self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"result":
                    "No agent execution needed. Respond to the user normally."})
        if kind == "execute":
            dropped = decision.get("dropped") or []
            agents = ", ".join(decision.get("agents", []))
            note = (f" (not available, skipped: {', '.join(dropped)})"
                    if dropped else "")
            self.ui.write_log(f"SYS: agentic task started → {agents}{note}")
            return types.FunctionResponse(
                id=fc.id, name=fc.name,
                response={"result":
                    f"[TASK STARTED] Agents: {agents}{note}. "
                    f"Say ONE short sentence that work has begun, then wait — "
                    f"the real result will arrive here as a tagged message. "
                    f"Do NOT invent or summarize any outcome yet."})
        self.ui.set_state("LISTENING")
        return types.FunctionResponse(
            id=fc.id, name=fc.name,
            response={"result":
                f"Could not plan the task: {decision.get('error', 'unknown')}"})

    # -- agentic status / cancel wording ------------------------------------

    @staticmethod
    def _format_agentic_status(summary: dict) -> str:
        counts = summary.get("agent_counts") or {}
        total = sum(counts.values()) if counts else 0
        ready = counts.get("ready", 0)
        running = counts.get("running", 0)
        blocked = counts.get("blocked", 0)
        unavailable = counts.get("unavailable", 0)
        active = summary.get("active_task")
        recent = summary.get("recent") or []
        lines = [
            f"[AGENTIC STATE] Mode: {summary.get('mode', '?')}. "
            f"Agents: {total} known, {ready} ready, {running} running, "
            f"{blocked} blocked, {unavailable} unavailable.",
        ]
        if active:
            lines.append(
                f"Active task {str(active.get('task_id', ''))[:8]}: "
                f"'{(active.get('request') or '')[:90]}' "
                f"[{active.get('status')}] "
                f"agents: {', '.join(active.get('agents') or [])}.")
        elif recent:
            last = recent[0]
            lines.append(
                f"Last task {str(last.get('task_id', ''))[:8]}: "
                f"[{last.get('status')}] "
                f"{(last.get('result') or last.get('error') or '')[:200]}")
        else:
            lines.append("No tasks yet.")
        lines.append("Speak this in 1-2 short sentences; do not read it aloud.")
        return " ".join(lines)

    @staticmethod
    def _format_agentic_cancel(outcome: dict) -> str:
        if outcome.get("ok"):
            return (f"The task is stopped (status: {outcome.get('status')}). "
                    f"Say you stopped it, in one short sentence.")
        return (f"I could not stop it: {outcome.get('error', 'unknown')}. "
                f"Tell the user that plainly.")

    # -- agentic event sink (UI panel + dashboard) ---------------------------

    def _agentic_confirm_gate(self, title: str, detail: str, run) -> str:
        """Agentic runtime → core.confirm gate (spec §11).

        Consequential agentic work (send/publish/delete) parks behind the same
        on-screen CONFIRM/CANCEL banner as local irreversible actions. Returns
        immediately with the sentence the model speaks; the task's execution
        continues from the gate only after a human presses CONFIRM.
        """
        key = f"agentic-{int(time.time() * 1000)}"
        return confirm_gate.request(key, title, detail, run)

    def _agentic_log_line(self, kind: str, payload: dict) -> str:
        """One meaningful activity-log line per real event, or "" to skip.

        The fleet refresh emits `agents` once per registered agent and blocked
        agents repeat `agent_state: blocked` on every probe cycle. Both are
        legitimate state refreshes — the HUD panel consumes all of them — but
        logging each one wrote the same "[agentic:agents] fleet" line dozens of
        times per minute. Only a change in the aggregate counts or in one
        agent's state is log-worthy now. Returns the line or "" to suppress.
        """
        if kind == "agents":
            counts = payload.get("counts") or {}
            sig = tuple(sorted((str(k), int(v)) for k, v in counts.items()))
            if sig == self._agentic_counts_sig:
                return ""
            self._agentic_counts_sig = sig
            reason = str(payload.get("reason") or "")
            detail = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
            if reason == "backend_down":
                return f"SYS: [agentic] backend unreachable — agents stay cached ({detail})"
            return f"SYS: [agentic] agents updated — {detail}"
        if kind == "agent_state":
            agent_id = str(payload.get("id") or "")
            state = str(payload.get("state") or "")
            if not agent_id or self._agentic_state_sig.get(agent_id) == state:
                return ""
            self._agentic_state_sig[agent_id] = state
            detail = str(payload.get("detail") or "")
            suffix = f" — {detail[:90]}" if detail and state in ("blocked", "failed") else ""
            return f"SYS: [agentic] {agent_id} → {state}{suffix}"
        return (f"SYS: [agentic:{kind}] "
                f"{(payload.get('reason') or payload.get('phase') or '')[:120]}".rstrip())

    def _on_agentic_event(self, kind: str, payload: dict):
        """Runtime → {HUD panel, log, dashboard}. Never touches audio."""
        try:
            self.ui.push_agentic_event({"kind": kind, "data": payload})
        except Exception:
            pass
        try:
            line = self._agentic_log_line(kind, payload)
            if line:
                self.ui.write_log(line)
        except Exception:
            pass
        try:
            kind_map = {
                "mode": "agentic_mode", "agents": "agentic_agents",
                "agent_state": "agentic_agent_state", "task": "agentic_task",
            }
            # The runtime fires from worker threads — hop onto the main loop and
            # send a JSON-safe copy (send_json must never choke on a task object,
            # or it would silently drop every dashboard client).
            if self._dashboard and self._loop:
                import json as _json
                msg = _json.loads(_json.dumps(
                    {"type": kind_map.get(kind, "agentic"),
                     "kind": kind, **payload},
                    default=str,
                ))
                asyncio.run_coroutine_threadsafe(
                    self._dashboard.broadcast(msg), self._loop
                )
        except Exception:
            pass
        try:
            if kind == "task" and payload.get("phase") in (
                    "completed", "failed", "cancelled"):
                task = payload.get("task") or {}
                self._inject_task_result(task, payload.get("phase", ""))
                self._on_task_completed(task, payload.get("phase", ""))
        except Exception:
            pass

    # -- completion pipeline (spec §9: report + screenshot + phone notify) ---

    def _phone_notifier(self):
        """Lazily build the notifier bound to the live remote dashboard."""
        notifier = self._phone_notifier_ref
        if notifier is not None:
            return notifier
        try:
            from core.notify_phone import PhoneNotifier
        except Exception:
            return None
        dash = self._dashboard
        push = self._dashboard_send if dash is not None else None

        def _clients() -> int:
            try:
                return len(getattr(dash, "_clients", ()) or ())
            except Exception:
                return 0

        notifier = PhoneNotifier(
            push=push,
            client_count=_clients if dash is not None else None,
            uploads_dir=str(getattr(dash, "_uploads_dir", "") or "") or None,
        )
        self._phone_notifier_ref = notifier
        return notifier

    def _on_task_completed(self, task: dict, phase: str) -> None:
        """One-shot post-task pipeline: screenshot, report, phone delivery.

        Runs for every terminal agentic task (completed/failed/cancelled):
        the real screen is captured on success, the task is persisted with
        its artifacts, and the report is pushed to the paired phone — the
        delivery result is recorded honestly per channel.
        """
        try:
            task_id = str(task.get("task_id") or "")
            if not task_id or task_id in self._notified_tasks:
                return
            self._notified_tasks.add(task_id)
            if len(self._notified_tasks) > 400:
                self._notified_tasks.clear()

            from core.task_store import get_task_store
            store = get_task_store()

            screenshot = None
            if phase == "completed":
                try:
                    from core import screen_capture
                    screenshot = screen_capture.capture_png(
                        prefix=f"task_{task_id[:8]}")
                except Exception:
                    screenshot = None
            if screenshot:
                store.set_screenshot(task_id, screenshot)

            record = store.get(task_id) or store.record(task)
            report_bits = []
            notifier = self._phone_notifier()
            if notifier is not None:
                try:
                    channels = notifier.send_report(record or task,
                                                    screenshot)
                    for channel, info in (channels or {}).items():
                        ok = bool((info or {}).get("ok"))
                        detail = str((info or {}).get("detail") or "")
                        store.mark_notify(task_id, channel, ok, detail)
                        report_bits.append(
                            f"{channel}: {'OK' if ok else 'NOT DELIVERED'}"
                            f" — {detail}")
                except Exception as exc:
                    report_bits.append(f"notify failed: {exc}")
            if report_bits:
                store.set_report(task_id, "\n".join(report_bits))

            shot_note = " — screenshot saved" if screenshot else ""
            first = report_bits[0][:120] if report_bits else "no channel"
            self.ui.write_log(f"SYS: task {task_id[:8]} {phase}{shot_note} | "
                              f"{first}")
        except Exception as exc:
            try:
                self.ui.write_log(f"SYS: completion pipeline error: {exc}")
            except Exception:
                pass

    def _tool_show_result(self, args: dict) -> str:
        """Voice: open the Result Viewer on a REAL finished task."""
        try:
            from core.task_store import get_task_store
        except Exception as exc:
            return f"Result viewer unavailable: {exc}"
        store = get_task_store()
        query = str(args.get("task") or args.get("query") or "").strip()
        record = None
        if query:
            found = store.find(query, limit=1)
            record = found[0] if found else None
        if record is None:
            finished = store.latest(1, statuses=("completed", "failed"))
            record = finished[0] if finished else None
        if record is None:
            return ("There is no finished task to show yet. Start a task "
                    "first, then ask me to show the result.")
        try:
            self.ui.show_result(record)
        except Exception as exc:
            return f"I found the result but could not open the viewer: {exc}"
        status = str(record.get("status") or "?")
        req = str(record.get("request") or "")[:90]
        summary = str(record.get("result") or record.get("error") or "")[:180]
        arts = len(record.get("artifacts") or [])
        extra = f" {arts} artifact(s) attached." if arts else ""
        return (f"[RESULT SHOWN] '{req}' — {status}.{extra} "
                f"Result begins: {summary}. The viewer is now open on "
                f"screen — say ONE short sentence referring the user to it; "
                f"do not read the whole result aloud.")

    def _tool_show_screen(self, args: dict) -> str:
        """Voice: open/close the live Show Screen panel (inside the app)."""
        action = str(args.get("action") or "open").strip().lower()
        if action in ("close", "stop", "hide", "off"):
            try:
                self.ui.show_screen("close")
            except Exception as exc:
                return f"Could not close the screen panel: {exc}"
            return "Live screen panel closed."
        try:
            self.ui.show_screen("open")
        except Exception as exc:
            return f"Could not open the screen panel: {exc}"
        return ("[SCREEN SHOWN] The live screen panel is open — real frames "
                "of this screen plus the current task status. Say ONE short "
                "sentence; do not describe the panel contents in detail.")

    # -- agentic result injection (verified outputs → spoken response) -------

    def _inject_task_result(self, task: dict, phase: str):
        """Deliver a finished task's real outcome back into the conversation.

        Only fires for Agentic Mode tasks on a live session, so NORMAL mode
        and headless operation never get stray injections. The tagged message
        carries the VERIFIED output — the model speaks it; if the task
        failed, it says what failed in plain words (spec §5 step 9).
        """
        try:
            if not self.agentic.agentic_enabled:
                return
            if not self._loop or not self.session:
                return
            outcome = task.get("result") or ""
            error = task.get("error") or ""
            if phase == "completed" and outcome:
                head = (f"[TASK_RESULT] The agent task finished successfully. "
                        f"Verified result: {outcome[:1200]}")
                tail = ("Report this to the user in one or two short spoken "
                        "sentences in their own language. Summarize the key "
                        "outcome; do not read raw output verbatim.")
            elif phase == "cancelled":
                head = ("[TASK_CANCELLED] The agentic task was stopped at the "
                        "user's request before finishing.")
                tail = ("Confirm in one short sentence that it is stopped. "
                        "Do not describe any partial work as finished.")
            else:
                head = (f"[TASK_FAILED] The agent task failed: "
                        f"{(error or 'no result returned')[:800]}")
                tail = ("State plainly, in one or two short sentences in the "
                        "user's language, that the task did not complete and "
                        "what the blocker was. Never claim success.")
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": f"{head} {tail}"}]},
                    turn_complete=True,
                ),
                self._loop,
            )
        except Exception:
            pass

    # -- mode toggle + text command routing -----------------------------------

    def _on_mode_changed(self, mode_index: int, mode_name: str):
        """Header combo → runtime. Never touches audio/mic/session settings."""
        try:
            mode_map = {
                0: RuntimeMode.NORMAL,
                1: RuntimeMode.AGENTIC,
                2: RuntimeMode.SARA_BOTS_AUTONOMOUS,
                3: RuntimeMode.ADAPTIVE,
            }
            target_mode = mode_map.get(mode_index, RuntimeMode.NORMAL)
            changed = self.agentic.set_mode(target_mode)
            self.ui.push_agentic_event({
                "type": "mode",
                "mode": self.agentic.mode.value,
            })
            if changed:
                self.ui.write_log(f"SYS: Mode switched to {mode_name}.")
                # Announce mode change to the live session for ADAPTIVE/AGENTIC/SARA_BOTS
                if target_mode != RuntimeMode.NORMAL:
                    self._announce_mode_change(target_mode.value)
            elif target_mode == RuntimeMode.NORMAL:
                self._announce_mode_change(RuntimeMode.NORMAL.value)
        except Exception as e:
            self.ui.write_log(f"ERR: mode switch failed: {e}")

    def _announce_mode_change(self, mode: str):
        """Runtime callback: teach the live model the new mode's routing rules.

        Injected as one tagged message (see prompt.txt [TAGGED MESSAGES]) so
        the switch changes BACKEND behaviour mid-session, not just the panel.
        """
        try:
            if not self._loop or not self.session:
                return
            if mode == RuntimeMode.AGENTIC.value:
                text = (
                    "[MODE_CHANGE] SARA AGENTIC MODE is now ACTIVE. From now on, "
                    "when the user asks you to PERFORM a real task — research or "
                    "scrape the web, analyse data, handle email/WhatsApp/social "
                    "messages, generate music, build or review code, run a "
                    "workflow across several tools — call the agentic_task tool "
                    "with their full request, exactly once. For 'what are you "
                    "doing / did it finish / tell me the result', call "
                    "agentic_status. For 'stop / cancel the task', call "
                    "agentic_cancel. Keep normal conversation for everything "
                    "else. Never set confirmed=yes yourself and never claim a "
                    "task succeeded until a [TASK_RESULT] message says it did.")
            elif mode == RuntimeMode.SARA_BOTS_AUTONOMOUS.value:
                text = (
                    "[MODE_CHANGE] SARA BOTS + AUTONOMOUS POWERS MODE is now ACTIVE. "
                    "From now on, when the user asks you to PERFORM a complex task — "
                    "goal decomposition, multi-step workflows, browser automation, "
                    "scheduling, persistent tasks, or creating specialized bots — "
                    "use the Sara Bots framework via agentic_task. This mode enables "
                    "goal planning, skill reuse, task scheduling, and custom bot creation. "
                    "For 'what are you doing / did it finish / tell me the result', call "
                    "agentic_status. For 'stop / cancel the task', call "
                    "agentic_cancel. Keep normal conversation for everything else.")
            elif mode == RuntimeMode.ADAPTIVE.value:
                text = (
                    "[MODE_CHANGE] ADAPTIVE / AUTO FALLBACK MODE is now ACTIVE. "
                    "From now on, the system intelligently selects the execution path: "
                    "1) Try direct execution (local tools). "
                    "2) If that fails, route to SARA AGENTIC MODE (agent registry). "
                    "3) If that fails, use SARA BOTS framework (custom bots, workflows). "
                    "4) If no suitable bot exists, propose creating a specialized bot. "
                    "5) If all permitted routes fail, report the blocker honestly. "
                    "This is the default mode — the system chooses the best path automatically. "
                    "For 'what are you doing / did it finish / tell me the result', call "
                    "agentic_status. For 'stop / cancel the task', call "
                    "agentic_cancel. Keep normal conversation for everything else.")
            else:
                text = (
                    "[MODE_CHANGE] SARA NORMAL MODE is now ACTIVE. Do NOT call "
                    "agentic_task, agentic_status or agentic_cancel anymore — "
                    "behave exactly as the normal assistant: conversation, "
                    "your local tools, nothing routed to the agent registry.")
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]},
                    turn_complete=False,
                ),
                self._loop,
            )
        except Exception:
            pass

    # -- prompt plumbing: declare the active mode to the Live session ---------

    def _agentic_prompt_lines(self) -> list:
        """Mode section appended to the system instruction at session build.

        In NORMAL mode the tools stay unmentioned (spec §3: no interference);
        in AGENTIC/SARA_BOTS/ADAPTIVE modes the model learns when to route (spec §5).
        Tool handlers enforce the mode independently — this is advisory only.
        """
        mode = self.agentic.mode
        if mode == RuntimeMode.NORMAL:
            return [
                "[SARA NORMAL MODE]",
                "Behave exactly as the normal assistant. The agentic_task, "
                "agentic_status and agentic_cancel tools are disabled in this "
                "mode — never call them.",
            ]
        elif mode == RuntimeMode.AGENTIC:
            counts = self.agentic.registry.state_counts()
            ready = counts.get("ready", 0)
            return [
                "[SARA AGENTIC MODE ACTIVE]",
                f"The agent registry holds {sum(counts.values())} agents "
                f"({ready} ready right now). When the user's request needs "
                f"one, call agentic_task with their full request. Never fabr"
                f"icate results — the real outcome returns as a [TASK_RESULT] "
                f"tagged message; only then do you speak it.",
            ]
        elif mode == RuntimeMode.SARA_BOTS_AUTONOMOUS:
            return [
                "[SARA BOTS + AUTONOMOUS POWERS MODE ACTIVE]",
                "The Sara Bots framework is available: custom bots, shared "
                "capabilities, task engine, autonomous workflows, goal "
                "decomposition, multistep execution, scheduling, and bot "
                "creation. When the user's request needs these capabilities, "
                "call agentic_task with their full request. The real outcome "
                "returns as a [TASK_RESULT] tagged message; only then do you "
                "speak it.",
            ]
        elif mode == RuntimeMode.ADAPTIVE:
            return [
                "[ADAPTIVE / AUTO FALLBACK MODE ACTIVE]",
                "Intelligent execution fallback is enabled. The system will "
                "automatically select the best execution path: 1) Direct local "
                "tools, 2) Agentic Mode (agent registry), 3) Sara Bots "
                "framework, 4) Create specialized bot, 5) Report blocker. "
                "When the user's request needs agentic capabilities, call "
                "agentic_task with their full request. The system handles the "
                "fallback chain automatically. Never fabricate results — the "
                "real outcome returns as a [TASK_RESULT] tagged message; only "
                "then do you speak it.",
            ]
        return [
            "[SARA NORMAL MODE]",
            "Behave exactly as the normal assistant. The agentic_task, "
            "agentic_status and agentic_cancel tools are disabled in this "
            "mode — never call them.",
        ]

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[SARA] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so SARA can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                sara_speaking = self._is_speaking
            # While SARA is speaking nothing is streamed — see the barge-in
            # note below for how little it takes to re-enable voice interrupt.
            if not sara_speaking:
                # ── Echo tail — mic stays open but filtering own echo ───────
                # For a moment after speech ends, room sound still carries the
                # speakers' buffer. EchoGuard drops blocks that are just echo
                # while still passing a genuine user who replies instantly.
                if self._tail_active():
                    try:
                        if getattr(self, "_echo", None) is None:
                            from core.echo import EchoGuard as _EG
                            self._echo = _EG()
                        if not self._echo.is_user_speech(  # type: ignore[union-attr]
                                indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                            return
                        self._tail_until = 0.0  # type: ignore[attr-defined]
                    except Exception:
                        return
                elif getattr(self, "_echo", None) and getattr(self._echo, "_hist", None):  # type: ignore[attr-defined]
                    try:
                        self._echo.reset()  # type: ignore[union-attr]
                    except Exception:
                        pass
                # ── Push-to-talk — when enabled mic is closed by default ────
                # Holding the chord opens it; wake word is bypassed while held
                # so push-to-talk works without saying a name first.
                if getattr(self, "_ptt_enabled", False) and not getattr(self, "_ptt_held", False):
                    return
            if not sara_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[SARA] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[SARA] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[SARA] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[SARA] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[SARA] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[SARA] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                _slice = _audio_data[_i : _i + _SLICE]
                                self.audio_in_queue.put_nowait(_slice)
                                self._mirror_audio_to_phone(_slice)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "sara",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"role": "user", "parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until SARA finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[SARA] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[SARA] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[SARA] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[SARA] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[SARA] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Size the echo tail from the device's real output latency, not a guess
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat  # type: ignore[attr-defined]
            print(f"[SARA] Output latency {float(getattr(self, '_out_latency', 0.0))*1000:.0f} ms "
                  f"+ echo tail {(float(getattr(self, '_out_latency', 0.24)) + 0.18)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform from SARA's own voice while speaking.
                try:
                    self.ui.set_audio_level(_pcm_level(
                        np.frombuffer(bytes(batch), dtype=np.int16)))
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[SARA] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Speak this greeting in {lang}, then follow the "
                       f"user's own language from their first reply onward."
                       if lang else "")
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = (f" Speak in {lang} unless the user has since "
                            f"spoken another language, in which case use theirs."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-flash-latest",
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or SARA is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Continuous screen vision ────────────────────────────────────────────────

    async def _run_screen_watch(self) -> None:
        """While screen vision is enabled, capture the screen at a fixed
        interval and inject each frame into the Live session as silent
        context (turn_complete=False), so the model always knows what is on
        display and can answer questions about it without a fresh capture.

        Skips a frame when SARA is mid-speech (never talks over herself) and
        quietly retries on capture errors instead of dying."""
        import base64 as _b64
        while True:
            await asyncio.sleep(self._screen_watch_interval)
            if not self._screen_watch_active or not self.session:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            try:
                loop = asyncio.get_event_loop()
                img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [
                        {"inline_data": {"mime_type": mime_t,
                                         "data": _b64.b64encode(img_b).decode("ascii")}},
                        {"text": "[SCREEN VISION - periodic frame] The user's current screen. "
                                 "Silently update your picture of what they are doing. "
                                 "Do NOT speak unless asked about the screen."},
                    ]},
                    turn_complete=False,
                )
            except Exception as e:
                print(f"[ScreenWatch] ⚠️ {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone activity mirror ─────────────────────────────────────────────

    def _dashboard_send(self, msg: dict) -> None:
        """Thread-safe dashboard broadcast that can never disturb SARA."""
        try:
            dash = self._dashboard
            if dash is None:
                return
            import json as _json
            safe = _json.loads(_json.dumps(msg, default=str))
            try:
                asyncio.get_running_loop().create_task(dash.broadcast(safe))
            except RuntimeError:
                if self._loop:
                    asyncio.run_coroutine_threadsafe(dash.broadcast(safe), self._loop)
        except Exception:
            pass

    _ACTION_SKIP = {"save_memory", "recall_memory", "system_status"}

    @staticmethod
    def _action_text(name: str, args: dict, result) -> str:
        """One short human line describing what SARA just did (phone feed)."""
        def g(*keys):
            for k in keys:
                v = args.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""
        r = str(result or "")
        failed = r.startswith("Tool '") and " failed" in r[:160]

        _MAP = {
            "open_app":            lambda: f"Opened {g('app_name', 'name') or 'an app'}",
            "file_controller":     lambda: f"File: {g('action', 'operation') or 'change'} {g('path', 'file_path', 'name')}".strip(),
            "file_processor":      lambda: f"Processed file {g('file_path', 'path', 'name') or ''}".strip(),
            "send_message":        lambda: f"Sent a message to {g('receiver', 'contact', 'to') or g('platform') or 'the contact'}" + (f": \"{g('message', 'text')[:60]}\"" if g('message', 'text') else ""),
            "web_search":          lambda: f"Searched the web: \"{(g('query') or ', '.join(args.get('items', []) if isinstance(args.get('items'), list) else []))[:80]}\"",
            "youtube_video":       lambda: f"YouTube: {g('action', 'query', 'title') or 'control'}",
            "video_player":        lambda: f"Video player: {g('action') or 'control'}",
            "spotify_controller":  lambda: f"Spotify: {g('action') or 'control'}",
            "weather_report":      lambda: f"Weather check: {g('city', 'location') or 'local'}",
            "reminder":            lambda: f"Reminder: {g('action', 'text', 'title') or 'updated'}",
            "calendar_scheduler":  lambda: f"Calendar: {g('action') or 'updated'} {g('title', 'event')}".strip(),
            "screen_process":      lambda: f"Looked at your {g('angle') or 'screen'}",
            "close_camera":        lambda: "Closed the camera",
            "screen_watch":        lambda: f"Screen vision: {g('action') or 'status'}",
            "browser_control":     lambda: f"Browser: {g('action') or 'control'}",
            "computer_control":    lambda: f"Computer control: {g('action') or 'command'}",
            "computer_settings":   lambda: f"Changed settings: {g('action') or 'update'}",
            "system_command":      lambda: f"Ran: {g('command') or 'a system command'}",
            "system_monitor":      lambda: "Checked system stats",
            "system_diagnostics":  lambda: "Ran system diagnostics",
            "word_document":       lambda: f"Word document: {g('action') or 'created'} {g('filename', 'title')}".strip(),
            "pdf_document":        lambda: f"PDF: {g('action') or 'created'} {g('filename', 'title')}".strip(),
            "spreadsheet_builder": lambda: f"Spreadsheet: {g('action') or 'created'} {g('filename', 'title')}".strip(),
            "clipboard_processor": lambda: f"Clipboard: {g('action') or 'processed'}",
            "code_helper":         lambda: f"Code helper: {g('action') or 'worked on code'}",
            "dev_agent":           lambda: f"Dev agent: {g('action', 'query') or 'task'}",
            "desktop":             lambda: f"Desktop: {g('action') or 'action'}",
            "learn_rule":          lambda: f"Learned a rule: {g('rule', 'text')[:60]}",
            "undo":                lambda: "Undid the last change",
            "manage_monitor":      lambda: f"Monitor {g('action') or 'update'}: {g('topic') or ''}".strip(),
            "shutdown_sara":       lambda: "Shutting SARA down…",
            "game_updater":        lambda: f"Game updater: {g('action') or 'checked'}",
            "flight_finder":       lambda: f"Flight search: {g('query', 'destination') or ''}".strip(),
            "meeting_assistant":   lambda: f"Meeting assistant: {g('action') or 'action'}",
            "background_monitor":  lambda: f"Background monitor: {g('action') or 'update'}",
            "auto_heal_engine":    lambda: f"Auto-heal: {g('action') or 'action'}",
            "agency_opener":       lambda: f"Opened {g('agency', 'name') or 'an agency'}",
        }
        fn = _MAP.get(name)
        try:
            base = (fn() if fn else "") or name.replace("_", " ").title()
        except Exception:
            base = name.replace("_", " ").title()
        return (f"Failed: {base}" if failed else base)[:140]

    def _mirror_action(self, name: str, args: dict, result) -> None:
        """Push a concise 'what SARA did' line to connected phones."""
        try:
            if name in self._ACTION_SKIP:
                return
            text = self._action_text(name, args, result)
            if text:
                self._dashboard_send({"type": "action", "text": text,
                                      "ts": datetime.now().isoformat()})
        except Exception:
            pass

    # ── Phone audio relay ────────────────────────────────────────────────────────

    def _mirror_audio_to_phone(self, chunk: bytes) -> None:
        """Forward one ~50 ms slice of SARA's voice to connected phones.

        Base64 inside the normal JSON broadcast; skipped entirely when no
        Remote Dashboard client is connected (zero cost for desktop-only use).
        Never allowed to disturb the audio path — all errors are swallowed.
        """
        try:
            dash = self._dashboard
            if dash is None or not dash._clients:
                return
            import base64 as _b64
            msg = {"type": "audio", "rate": RECEIVE_SAMPLE_RATE,
                   "data": _b64.b64encode(chunk).decode("ascii")}
            try:
                asyncio.get_running_loop().create_task(dash.broadcast(msg))
            except RuntimeError:
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        dash.broadcast(msg), self._loop)
        except Exception:
            pass

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            # Drop frames queued while no session was live (phone mic opened
            # before SARA connected) — never replay stale speech.
            _ts = chunk.get("ts") if isinstance(chunk, dict) else None
            if _ts and (time.time() - _ts) > 1.5:
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()
        # a phone just arrived — flush completion reports queued while none
        # were connected. The page's WebSocket registers a moment after this
        # callback, so wait (bounded) for a live client before pushing.
        try:
            notifier = self._phone_notifier_ref
            if notifier is not None and notifier.has_pending():
                def _flush_when_live():
                    for _ in range(40):          # up to ~20 s
                        try:
                            if len(getattr(self._dashboard, "_clients", ()) or ()):
                                break
                        except Exception:
                            break
                        time.sleep(0.5)
                    sent = notifier.flush_pending()
                    if sent:
                        self.ui.write_log(
                            f"SYS: delivered {sent} queued report(s) "
                            f"to the phone.")
                threading.Thread(target=_flush_when_live, daemon=True,
                                 name="sara-report-flush").start()
        except Exception:
            pass

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes SARA if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[SARA] Connecting...")
                self.ui.set_state("THINKING")
                self.ui.set_core_status("connecting")
                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[SARA] Connected.")
                    self.ui.set_core_status("online")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Sara" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: SARA online — sleeping. Say 'Hey Sara' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: SARA online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    # Continuous screen vision: restore the persisted state so
                    # it stays on across restarts until explicitly turned off.
                    self._screen_watch_active = get_screen_watch_enabled()
                    self._screen_watch_interval = get_screen_watch_interval()
                    if self._screen_watch_active:
                        self.ui.write_log(
                            f"SYS: Screen vision active (every {self._screen_watch_interval:.0f}s) "
                            "— say 'stop watching my screen' to turn it off."
                        )

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    tg.create_task(self._run_screen_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[SARA] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[SARA] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[SARA] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[SARA] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                self.ui.set_core_status("offline")
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[SARA] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = SaraUI("face.png")

    def runner():
        ui.wait_for_api_key()
        sara = SaraLive(ui)
        try:
            asyncio.run(sara.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()

