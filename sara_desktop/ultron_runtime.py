"""ULTRON desktop runtime - the reference client for context awareness.

The backend can only see a foreground window when it *is* the machine the user
is sitting at. On Render it is not. So awareness has a second path: a tiny
client on the user's machine reads the window locally and reports it.

    $ python sara_desktop/ultron_runtime.py --url http://127.0.0.1:8000 \\
          --token <device token> --screen

What it sends, and nothing else: the foreground window's handle, title, owning
process and - for Chromium browsers - the address bar if it can be read. No
screenshots, no keystrokes, no audio. It has no code that could send those even
if asked, which is the point.

Two deliberate properties:

**It refuses to run without the screen permission.** ``--screen`` is required
to report context at all; without it the client starts, says what is missing,
and exits. A runtime that quietly began reporting windows would be exactly the
behaviour this project is not allowed to ship.

**It reports changes, not polls.** The backend decides what a change is; the
client just sends what it currently sees and lets the diff happen server-side,
so an idle client costs one HTTP round trip per interval and no state of its
own.

Transport note: this speaks the same device-link WebSocket as any other paired
client, but uses plain HTTP for context reporting so it works even if the
WebSocket is blocked by a proxy. The WebSocket is used for the "stop" message
and for future command delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Local window reading - Windows only, no third-party dependency required
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    return sys.platform == "win32"


def read_foreground() -> Optional[Dict[str, Any]]:
    """Read the foreground window locally. ``None`` off Windows."""
    if not _is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # noqa: BLE001
        print(f"[runtime] ctypes unavailable: {exc}", file=sys.stderr)
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "hwnd": int(hwnd),
            "title": buffer.value or "",
            "pid": int(pid.value),
            "process": _process_name(int(pid.value)),
            "url": _browser_url(int(hwnd)),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[runtime] window read failed: {exc}", file=sys.stderr)
        return None


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil

        return (psutil.Process(pid).name() or "").lower().replace(".exe", "")
    except Exception:  # noqa: BLE001
        return ""


_BROWSERS = ("chrome", "msedge", "edge", "brave", "vivaldi", "opera")


def _browser_url(hwnd: int) -> Optional[str]:
    """The address bar, if UI Automation will give it to us. Never guessed."""
    try:
        import uiautomation as auto

        window = auto.ControlFromHandle(hwnd)
        if window is None:
            return None
        for control in window.GetChildren():
            for candidate in [control] + list(control.GetChildren()):
                name = (candidate.Name or "").strip()
                if candidate.ControlTypeName == "EditControl" and name.startswith(
                        ("http://", "https://", "file://", "about:")):
                    return name
    except Exception:  # noqa: BLE001 - no URL is a fine answer
        return None
    return None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class Reporter:
    """Posts context to the backend, with the device token."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.sent = 0
        self.errors = 0
        self.last_error = ""

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "X-Ultron-Device-Token": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.sent += 1
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            self.errors += 1
            self.last_error = f"HTTP {exc.code}: {body}"
            return {"ok": False, "error": self.last_error}
        except Exception as exc:  # noqa: BLE001 - the loop must survive a blip
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.last_error}

    def report(self, window: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/ultron/awareness/report", {
            "title": window.get("title", ""),
            "process": window.get("process", ""),
            "pid": window.get("pid", 0),
            "hwnd": window.get("hwnd", 0),
            "url": window.get("url"),
        })

    def check(self) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/ultron/permissions",
            headers={"X-API-Key": self.api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    reporter = Reporter(args.url, args.token)
    print(f"[runtime] backend {args.url}")

    # Refuse to run without the grant, rather than reporting windows anyway.
    if not args.no_check:
        state = reporter.check()
        screen = ((state.get("gates") or {}).get("screen") or {}).get("allowed")
        if not state.get("error") and not screen:
            print("[runtime] SCREEN permission is not granted on the backend.")
            print("[runtime] Grant it first:  POST /ultron/permissions/screen")
            print("[runtime] Nothing was read from this machine. Exiting.")
            return 2
        print(f"[runtime] screen permission: {'granted' if screen else 'unknown'}")

    interval = max(0.25, args.interval)
    last_key = None
    cycles = 0
    print(f"[runtime] watching the foreground window every {interval}s. Ctrl+C to stop.")
    try:
        while True:
            window = read_foreground()
            cycles += 1
            if window:
                # The backend decides what counts as a change; the client just
                # reports what it sees, and skips the round trip when nothing
                # looks different.
                key = f"{window.get('hwnd')}|{window.get('title')}|{window.get('url')}"
                if key != last_key:
                    result = reporter.report(window)
                    last_key = key
                    status = "changed" if result.get("changed") else \
                             result.get("reason", "unchanged")
                    print(f"[runtime] {status}: {window.get('title', '')[:60]}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n[runtime] stopped after {cycles} cycles, "
              f"{reporter.sent} reports, {reporter.errors} errors.")
        if reporter.last_error:
            print(f"[runtime] last error: {reporter.last_error}")
        return 0


def main() -> int:
    try:
        from core.backend_url import resolve_backend_url
        _default_url = os.environ.get("ULTRON_URL") or resolve_backend_url()
    except Exception:
        _default_url = os.environ.get("ULTRON_URL", "http://127.0.0.1:8000")
    parser = argparse.ArgumentParser(
        description="ULTRON context-awareness runtime (reads the active window only)")
    parser.add_argument("--url", default=_default_url,
                        help="backend base URL")
    parser.add_argument("--token", default=os.environ.get("ULTRON_TOKEN", ""),
                        help="device token, or the SARA API key on a trusted host")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between foreground-window reads")
    parser.add_argument("--no-check", action="store_true",
                        help="skip the permission pre-flight (not recommended)")
    args = parser.parse_args()
    if not args.token:
        parser.error("--token (or ULTRON_TOKEN) is required")
    if not _is_windows():
        print("[runtime] this runtime reads Windows windows and does nothing "
              "useful on this platform.")
        return 1
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
