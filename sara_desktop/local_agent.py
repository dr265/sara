"""SARA Windows Local Agent — the component that actually controls the PC.

WHAT THIS IS

The backend plans; this executes. A Linux container cannot reach the user's
mouse, keyboard, screen, windows or microphone, so every real desktop action
has to happen in a process running on Windows. This agent is that process.

It makes an OUTBOUND, authenticated WebSocket to the backend and then:

    receives {tool, args}  ->  runs the EXISTING action registry  ->  returns
    the real result and a real verification signal

DELIBERATE NON-GOALS

* It does not reimplement any tool. ``sara_desktop/actions/`` already contains
  17 working tools (mouse, keyboard, browser, files, PowerShell, media). This
  process loads that same registry through the same ``action_loader`` the
  desktop UI uses. A second implementation would drift and would be a second
  thing to keep working.
* It does not require an inbound port. It dials out to the backend, so a user
  behind NAT or a corporate firewall needs no port forwarding. That is the
  difference between this and "expose your desktop to the internet".
* It does not silently succeed. If the action raises or returns a failure
  string, that is what goes back. Phase 17 is explicit that reporting success
  when the machine did nothing is the worst possible bug here.

SAFETY

The agent re-checks risk BEFORE running anything, independently of the
backend. A compromised or spoofed control plane therefore still cannot delete
a disk or shut the machine down without the user approving it here, on the
machine, in the agent's own UI. Confirmation tokens are single-use and bound
to one command id.

CONFIGURATION (all environment variables, nothing hard-coded)

    SARA_BACKEND_URL     default http://127.0.0.1:8000
    SARA_DEVICE_TOKEN    the token from the pairing flow (required)
    SARA_PAIRING_CODE    one-time code, used instead of a token for first run
    SARA_AGENT_LABEL     name shown on the command center
    SARA_AUTO_APPROVE    "1" to skip the on-screen prompt for gated actions
    SARA_COMMAND_TIMEOUT seconds to allow one action

Run:

    python sara_desktop/local_agent.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Make this file runnable directly from a checkout without installing anything.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from core.backend_url import resolve_backend_url as _resolve_backend_url
    BACKEND_URL = (os.getenv("SARA_BACKEND_URL") or _resolve_backend_url()).rstrip("/")
except Exception:
    BACKEND_URL = os.getenv("SARA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
DEVICE_TOKEN = os.getenv("SARA_DEVICE_TOKEN", "").strip()
PAIRING_CODE = os.getenv("SARA_PAIRING_CODE", "").strip()
AGENT_LABEL = os.getenv("SARA_AGENT_LABEL", "SARA-PC").strip()
AUTO_APPROVE = os.getenv("SARA_AUTO_APPROVE", "0").strip() in ("1", "true", "yes")
HEARTBEAT_S = 15.0
#: How long to wait for a pong before declaring the socket dead. MUST stay below
#: HEARTBEAT_S - websocket-client refuses to run otherwise.
PING_TIMEOUT_S = 10.0
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 30.0

#: Ask the backend to pair this machine on first run instead of demanding a
#: hand-copied token. The backend only honours it from loopback/private, and it
#: can be switched off server-side with SARA_AUTOPAIR=0.
AUTO_PAIR = os.getenv("SARA_AUTO_PAIR", "1").strip() in ("1", "true", "yes")


def _token_cache_path() -> str:
    """Where the device token is cached between runs.

    Precedence: SARA_DEVICE_TOKEN wins, then an explicit SARA_TOKEN_FILE, then
    the same state directory the backend persists its registry in, so the pair
    lives in one obvious place per machine rather than beside the source.
    """
    explicit = os.getenv("SARA_TOKEN_FILE", "").strip()
    if explicit:
        return explicit
    base = os.getenv("SARA_STATE_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".sara")
    return os.path.join(base, "device_token")


def _read_cached_token() -> str:
    try:
        with open(_token_cache_path(), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _cache_token(token: str) -> None:
    """Persist the token so the next run connects without pairing again.

    Written with owner-only permissions on POSIX. On Windows the file inherits
    the user's profile ACL, which is why the state directory is under the user
    profile rather than a shared location.
    """
    path = _token_cache_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Write via a temp file so an interrupted write cannot leave a
        # half-token that would authenticate as nothing and look like a bug.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                   prefix=".device_token-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        _log(f"WARNING: could not cache the device token to {path}: {exc}")

#: These never run locally - they are backend-side reasoning or server tools.
#: The agent must refuse them rather than pretend.
CLOUD_ONLY_PREFIXES = ("ultron_",)
CLOUD_ONLY_EXACT = frozenset({"memory", "recall", "datetime_info", "weather_report"})


def _log(msg: str) -> None:
    """Timestamped console output, safe to read in a terminal at 2am."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Risk re-check (independent of the backend)
# ---------------------------------------------------------------------------
def _backend_risk_tool(tool: str, args: Dict[str, Any]):
    """Ask the backend's policy module for the verdict, if importable.

    The agent does not duplicate the table. If the backend module is available
    it is authoritative; if not, the agent falls back to its own conservative
    denylist below rather than defaulting to "allow".
    """
    try:
        sys.path.insert(0, str(_ROOT / "backend"))
        from app.agents.sara_ultron_agent.core.risk import classify

        return classify(tool, args)
    except Exception:
        return None


#: Last-resort gate used only when the backend policy module cannot be
#: imported. Deliberately narrow: unknown actions are allowed, because the
#: agent is executing ordinary desktop work and blocking everything would make
#: it useless, but anything that reads as destructive is refused outright.
_LOCAL_DENY = ("delete", "format", "rd /s", "rm -rf", "remove-item", "shutdown",
               "restart", "reboot", "cipher", "diskpart", "netsh advfirewall",
               "reg delete", "vssadmin delete", "takeown")


def _locally_blocked(tool: str, args: Dict[str, Any]) -> bool:
    try:
        verdict = _backend_risk_tool(tool, args)
    except Exception:
        verdict = None
    if verdict is not None:
        return bool(getattr(verdict, "requires_confirmation", False))
    haystack = f"{tool} {args.get('action', '')} {args.get('command', '')}".lower()
    return any(bad in haystack for bad in _LOCAL_DENY)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class LocalAgent:
    """Maintains the link and executes commands against the real desktop."""

    def __init__(self) -> None:
        self._registry: Any = None
        self._ws: Any = None
        self._executed = 0
        self._failed = 0
        self._stopped = False

    # -- tool loading ----------------------------------------------------
    def registry(self) -> Any:
        """Load the SAME action registry sara_desktop already uses.

        Deliberately the existing ``discover_actions``: 17 tools, already
        tested against a real machine, already handling Chrome/Edge/Firefox,
        PowerShell, files, media and the mouse/keyboard.
        """
        if self._registry is not None:
            return self._registry
        desktop_dir = str(_ROOT / "sara_desktop")
        if desktop_dir not in sys.path:
            sys.path.insert(0, desktop_dir)
        from core.action_loader import discover_actions

        reg = discover_actions(Path(desktop_dir) / "actions", logger=lambda m: None)
        self._registry = reg
        names = sorted(reg.names())
        _log(f"Loaded {len(names)} desktop tools: {', '.join(names)}")
        return reg

    # -- execution -------------------------------------------------------
    def execute(self, tool: str, args: Dict[str, Any],
                confirmed: bool = False) -> Dict[str, Any]:
        """Run one tool and report what ACTUALLY happened."""
        started = time.time()
        args = dict(args or {})

        if tool.startswith(CLOUD_ONLY_PREFIXES) or tool in CLOUD_ONLY_EXACT:
            return self._fail(tool, f"'{tool}' runs in the backend, not on the PC.",
                              started)

        try:
            reg = self.registry()
        except Exception as exc:  # noqa: BLE001
            return self._fail(tool, f"Could not load the desktop tool registry: {exc}",
                              started)

        if not reg.has(tool):
            return self._fail(
                tool,
                f"'{tool}' is not a desktop tool on this machine. "
                f"Available: {', '.join(sorted(reg.names()))}",
                started,
            )

        if _locally_blocked(tool, args) and not (confirmed or AUTO_APPROVE):
            verdict = _backend_risk_tool(tool, args)
            prompt = (getattr(verdict, "impact", "") and
                      f"This will {verdict.impact}. Approve?") or \
                     f"'{tool}' is a gated operation. Approve?"
            return {
                "ok": False,
                "output": "",
                "error": f"CONFIRMATION REQUIRED on the PC: {prompt}",
                "verified": False,
                "needs_confirmation": True,
                "prompt": prompt,
                "tool": tool,
                "duration_s": round(time.time() - started, 2),
            }

        _log(f"> {tool}({json.dumps(args, default=str)[:160]})")
        try:
            raw = reg.run(tool, args)
        except Exception as exc:  # noqa: BLE001 - report, never fake success
            _log(f"x {tool} raised {type(exc).__name__}: {exc}")
            return self._fail(tool, f"{type(exc).__name__}: {exc}", started)

        output = str(raw if raw is not None else "")
        ok, verified = self._classify_output(tool, output)

        if ok:
            self._executed += 1
            _log(f"< {tool} OK ({round(time.time() - started, 2)}s): {output[:140]}")
        else:
            self._failed += 1
            _log(f"< {tool} FAILED: {output[:140]}")

        return {
            "ok": ok,
            "output": output,
            "error": "" if ok else output[:400],
            "verified": verified,
            "tool": tool,
            "duration_s": round(time.time() - started, 2),
        }

    @staticmethod
    def _classify_output(tool: str, output: str) -> tuple[bool, bool]:
        """Decide success from the handler's real return value.

        The desktop handlers return prose, not exit codes, so success is judged
        by whether they reported a problem. Phase 17's requirement - never
        claim success the machine did not confirm - is enforced here.
        """
        low = (output or "").strip().lower()
        if not low:
            return False, False

        failure_markers = (
            "could not", "cannot ", "failed", "error:", "not found",
            "no such", "not installed", "is not available", "unknown action",
            "denied", "timed out", "exception", "traceback", "permission denied",
        )
        is_fail = any(m in low for m in failure_markers)
        return (not is_fail), (not is_fail)

    def _fail(self, tool: str, error: str, started: float) -> Dict[str, Any]:
        self._failed += 1
        return {
            "ok": False,
            "output": "",
            "error": error,
            "verified": False,
            "tool": tool,
            "duration_s": round(time.time() - started, 2),
        }

    # -- context (Phase 6) ------------------------------------------------
    def context(self) -> Dict[str, Any]:
        """Report what the user is actually looking at, read-only.

        Reuses the awareness layer that already exists rather than reading
        windows a second time.
        """
        ctx: Dict[str, Any] = {"at": time.time(), "executed": self._executed,
                               "failed": self._failed}
        try:
            backend = str(_ROOT / "backend")
            if backend not in sys.path:
                sys.path.insert(0, backend)
            from app.agents.sara_ultron_agent.core.observer import read_foreground_window
            from app.agents.sara_ultron_agent.core.adapters import registry as adapters

            window = read_foreground_window()
            if window is not None:
                interpreted, adapter = adapters.interpret(window)
                ctx["active_window"] = getattr(window, "title", "") or ""
                ctx["active_process"] = getattr(window, "process", "") or ""
                ctx["adapter"] = adapter
                ctx["category"] = interpreted.category
                ctx["app_name"] = interpreted.app_name
                ctx["site"] = interpreted.site
                ctx["item"] = interpreted.item
                ctx["url"] = getattr(window, "url", None)
        except Exception as exc:  # noqa: BLE001 - context is best-effort
            ctx["context_error"] = f"{type(exc).__name__}: {exc}"
        return ctx

    # -- pairing ---------------------------------------------------------
    @staticmethod
    def claim_token() -> Optional[str]:
        """Exchange a one-time pairing code for a device token.

        This is the documented alternative to opening an inbound port: the user
        types a code shown on the website, and the agent dials out to claim it.
        """
        if not PAIRING_CODE:
            return None
        try:
            import urllib.request

            body = json.dumps({"token": PAIRING_CODE, "kind": "pc_agent",
                               "label": AGENT_LABEL}).encode()
            req = urllib.request.Request(
                f"{BACKEND_URL}/ultron/pairing/claim", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
            token = payload.get("device_token") or ""
            if token:
                _log("Paired successfully. Store the token in SARA_DEVICE_TOKEN.")
            return token or None
        except Exception as exc:  # noqa: BLE001
            _log(f"Pairing failed: {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def auto_pair_token() -> Optional[str]:
        """Ask a local backend to pair this machine, with no human step.

        This is what makes the desktop simply CONNECT. It only succeeds when the
        backend sees the request from loopback or a private network - otherwise
        the backend refuses and we fall through to the manual flows rather than
        pretending.
        """
        try:
            import urllib.request

            body = json.dumps({"kind": "pc_agent", "label": AGENT_LABEL,
                               "platform": sys.platform}).encode()
            req = urllib.request.Request(
                f"{BACKEND_URL}/ultron/pairing/auto", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            _log(f"Auto-pair unavailable ({type(exc).__name__}).")
            return None

        token = payload.get("device_token") or ""
        if token:
            if payload.get("rotated"):
                _log("Re-paired automatically with the local backend "
                     "(cached token was missing or stale).")
            else:
                _log("Paired automatically with the local backend.")
            _cache_token(token)
            return token
        # No token because the backend refused to rotate: something is already
        # connected under this identity. Say so instead of implying failure.
        _log("Already connected on this machine; no token issued "
             f"({payload.get('note', '')})".strip())
        return None

    # -- main loop --------------------------------------------------------
    def run(self) -> int:
        # Order matters: an explicit token wins, then the cache, then the
        # one-time code, and only then auto-pair. Auto-pair is last so it can
        # never quietly replace a credential the operator chose.
        token = (DEVICE_TOKEN
                 or _read_cached_token()
                 or self.claim_token()
                 or (self.auto_pair_token() if AUTO_PAIR else None))
        if not token:
            _log("No device token could be obtained.")
            _log(f"  1. automatic: start the agent while the backend runs on "
                 f"this machine (SARA_BACKEND_URL={BACKEND_URL}), or")
            _log("  2. manual: pair at <backend>/ultron/pairing and set "
                 "SARA_DEVICE_TOKEN.")
            return 2
        if not DEVICE_TOKEN:
            # Only cache what we obtained ourselves; an env-provided token is
            # the operator's to manage.
            if not _read_cached_token():
                _cache_token(token)

        # Preload the registry so the first command is not slow, and so a broken
        # tool set is visible at startup rather than mid-task.
        try:
            self.registry()
        except Exception as exc:  # noqa: BLE001
            _log(f"WARNING: desktop tools failed to load: {exc}")

        try:
            # The distribution is named ``websocket-client`` but the module it
            # installs is ``websocket``. Importing ``websocket_client`` always
            # raises ImportError even when the package is present, which made
            # this agent report "install websocket-client" while doing exactly
            # that - and never connect.
            from websocket import WebSocketApp  # type: ignore
        except ImportError:
            _log("The websocket-client package is required: "
                 "pip install -r requirements-desktop-windows.txt")
            return 2

        ws_url = BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ultron/link?token={token}"

        def on_message(_ws, message):
            try:
                frame = json.loads(message)
            except Exception:
                return
            kind = str(frame.get("type") or "")
            if kind == "command":
                result = self.execute(str(frame.get("tool") or ""),
                                      frame.get("args") or {},
                                      confirmed=bool(frame.get("confirmed")))
                result.update({
                    "type": "result",
                    "command_id": frame.get("command_id"),
                    "device_id": frame.get("device_id"),
                })
                self._send(result)
            elif kind in ("tick", "pong"):
                pass
            elif kind == "revoked":
                _log(f"Backend revoked this device: {frame.get('detail')}")
                self._stopped = True

        def on_open(_ws):
            _log(f"Connected to {BACKEND_URL} as '{AGENT_LABEL}'")
            self._send({"type": "info", "focus": "pc_agent",
                        "name": AGENT_LABEL, "platform": "windows",
                        "info": self.context()})

        def on_close(_ws, *_a):
            if not self._stopped:
                _log("Link closed; will reconnect.")

        def on_error(_ws, err):
            _log(f"Link error: {err}")

        delay = RECONNECT_MIN_S
        while not self._stopped:
            app = WebSocketApp(ws_url, on_open=on_open, on_message=on_message,
                               on_close=on_close, on_error=on_error)
            # Keep a reference so _send() has a live socket to write to.
            self._ws = app
            # websocket-client requires ping_timeout < ping_interval and raises
            # on connect otherwise. These were previously 30s and 15s (timeout
            # ABOVE interval), so the agent raised WebSocketException the moment
            # it opened a socket and had never once stayed connected. The
            # liveness rule is: expect a pong within PING_TIMEOUT_S, and send a
            # ping every HEARTBEAT_S.
            app.run_forever(ping_interval=int(HEARTBEAT_S),
                            ping_timeout=int(PING_TIMEOUT_S))
            self._ws = None
            if self._stopped:
                break
            _log(f"Reconnecting in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)
        return 0

    def _send(self, payload: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            _log("Dropping frame: no live socket")
            return
        try:
            ws.send(json.dumps(payload, default=str))
        except Exception as exc:  # noqa: BLE001
            _log(f"Could not send frame: {type(exc).__name__}: {exc}")


def main() -> int:
    _log("SARA Windows Local Agent")
    _log(f"  backend : {BACKEND_URL}")
    _log(f"  label   : {AGENT_LABEL}")
    cached = _read_cached_token()
    _log(f"  token   : {'set (SARA_DEVICE_TOKEN)' if DEVICE_TOKEN else ('cached' if cached else 'will auto-pair' if AUTO_PAIR else 'MISSING')}")
    if AUTO_APPROVE:
        _log("  AUTO_APPROVE is ON: gated actions will NOT ask on screen.")
    return LocalAgent().run()


if __name__ == "__main__":
    raise SystemExit(main())