"""The observer: a real read of what the user is looking at, right now.

Windows exposes the foreground window to any process with no extra privilege at
all, which means "what is the user in" is cheap and exact - there is no need to
sample pixels to answer it. This module does exactly that and nothing more.

What it reads
-------------
* the foreground window handle and its title (``user32.GetForegroundWindow``)
* the owning process id, name and path (``GetWindowThreadProcessId`` +
  ``psutil``, with a ``ctypes``-only fallback so the module works with nothing
  installed)
* the **real URL** for Chromium-family browsers, via UI Automation, when the
  browser exposes it and only then

What it deliberately does not do
--------------------------------
* take a screenshot
* record audio
* read window contents, text buffers, or the clipboard
* run at all unless the ``screen`` permission is granted

That last point is enforced at the top of every poll, not once at startup. A
permission check at startup would keep running after the user revoked access,
which is the exact failure this project is required not to have.

Cost
----
One ``GetForegroundWindow`` call per tick. The window title is read only when
the handle changed; the process lookup only when the title changed. On an idle
screen that is a few microseconds a tick, so a one-second cadence is
indistinguishable from free and still catches an alt-tab immediately.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .adapters import RawWindow
from .awareness import context as context_manager
from .permissions import permissions

log = logging.getLogger("sara.ultron.observer")

#: How often the foreground window is polled. One second catches an alt-tab
#: effectively instantly while costing a syscall per second.
POLL_S = 1.0

#: A window must stay stable this long before it is adopted. Without the
#: debounce, a context switch flickers through the intermediate windows (the
#: taskbar, a splash, the switcher) and the task thread inherits a transient.
SETTLE_S = 0.6

#: Window titles shorter than this are chrome, not context ("Program Manager",
#: "" on an empty desktop). They are ignored rather than adopted.
MIN_TITLE_LENGTH = 2

#: Read the browser address bar with UI Automation at most this often, per
#: window. Reading it is milliseconds; doing it on every tick is waste.
URL_READ_INTERVAL_S = 5.0


def is_windows() -> bool:
    import sys

    return sys.platform == "win32"


# ---------------------------------------------------------------------------
# Raw OS reads
# ---------------------------------------------------------------------------
def _user32():  # pragma: no cover - Windows only
    import ctypes

    return ctypes.windll.user32


def read_foreground_window() -> Optional[RawWindow]:
    """One real read of the foreground window. ``None`` if it cannot be read."""
    if not is_windows():
        return None
    try:  # pragma: no cover - exercised on Windows
        import ctypes
        from ctypes import wintypes

        user32 = _user32()
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value or ""

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return RawWindow(title=title, pid=pid.value, hwnd=int(hwnd))
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not read the foreground window: %s", exc)
        return None


def process_name(pid: int) -> str:
    """Executable name for a pid, lower-cased, without ``.exe``.

    Tries ``psutil`` first (accurate, gives the full path) and falls back to
    opening the process with ``ctypes``. It is called only when a window
    actually changed, so a failure here is cheap.
    """
    if pid <= 0:
        return ""
    try:
        import psutil

        return (psutil.Process(pid).name() or "").lower().replace(".exe", "")
    except Exception:  # noqa: BLE001
        pass
    if not is_windows():
        return ""
    try:  # pragma: no cover - exercised on Windows
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                  ctypes.byref(size)):
                return buffer.value.rsplit("\\", 1)[-1].lower().replace(".exe", "")
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("process_name(%s) failed: %s", pid, exc)
        return ""


# ---------------------------------------------------------------------------
# Browser URL, via UI Automation when the browser exposes it
# ---------------------------------------------------------------------------
_URL_CACHE: Dict[int, tuple] = {}


def read_browser_url(hwnd: int, process: str) -> Optional[str]:
    """The browser's address bar, if we can read it honestly.

    Chromium exposes the address bar through UI Automation. When it cannot be
    read - a Firefox build that does not, a locked-down profile, an extra
    window type - this returns ``None`` and the adapter reports ``url``
    unavailable. It never assembles a URL from the page title, because a
    guessed URL that looks right is worse than no URL: it would be acted on.
    """
    now = time.time()
    cached = _URL_CACHE.get(hwnd)
    if cached and now - cached[1] < URL_READ_INTERVAL_S:
        return cached[0]

    url: Optional[str] = None
    try:
        import uiautomation as auto

        window = auto.ControlFromHandle(hwnd)
        if window is not None:
            for control in window.GetChildren():
                name = (control.Name or "").strip()
                # Chromium's address bar is an Edit control in the toolbar.
                if control.ControlTypeName == "EditControl" and name.startswith(
                        ("http://", "https://", "file://", "about:")):
                    url = name
                    break
                for child in control.GetChildren():
                    child_name = (child.Name or "").strip()
                    if child.ControlTypeName == "EditControl" and child_name.startswith(
                            ("http://", "https://", "file://", "about:")):
                        url = child_name
                        break
                if url:
                    break
    except Exception as exc:  # noqa: BLE001 - a failed read is just no URL
        log.debug("UI Automation URL read failed (%s): %s", process, exc)
        url = None

    _URL_CACHE[hwnd] = (url, now)
    return url


# ---------------------------------------------------------------------------
# The observer loop
# ---------------------------------------------------------------------------
@dataclass
class ObserverStatus:
    running: bool = False
    pid: Optional[int] = None
    started: float = 0.0
    polls: int = 0
    changes: int = 0
    url_reads: int = 0
    permission_denials: int = 0
    unnamed_windows: int = 0
    last_error: str = ""
    interval_s: float = POLL_S

    def as_dict(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "uptime_s": round(time.time() - self.started, 1) if self.started else 0,
            "polls": self.polls,
            "changes": self.changes,
            "url_reads": self.url_reads,
            "permission_denials": self.permission_denials,
            "unnamed_windows": self.unnamed_windows,
            "last_error": self.last_error,
            "interval_s": self.interval_s,
        }


class WindowObserver:
    """Polls the foreground window and feeds the context manager."""

    def __init__(self, interval_s: float = POLL_S) -> None:
        self.interval_s = interval_s
        self.status = ObserverStatus(interval_s=interval_s)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_hwnd = 0
        self._last_identity = ""
        self._candidate_hwnd = 0
        self._candidate_since = 0.0

    # -- lifecycle ------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.status.running:
                return {"started": False, "reason": "already running",
                        **self.status.as_dict()}
            self._stop.clear()
            self.status = ObserverStatus(running=True, pid=__import__("os").getpid(),
                                         started=time.time(),
                                         interval_s=self.interval_s)
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="ultron-observer")
            self._thread.start()
        log.info("Context observer started (interval=%.1fs)", self.interval_s)
        return {"started": True, **self.status.as_dict()}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            was = self.status.running
            self._stop.set()
            thread = self._thread
            self._thread = None
            self.status.running = False
        if thread is not None:
            thread.join(timeout=self.interval_s + 2)
        if was:
            log.info("Context observer stopped.")
        return {"stopped": was, **self.status.as_dict()}

    @property
    def running(self) -> bool:
        return self.status.running

    # -- one poll -------------------------------------------------------
    def poll_once(self) -> Dict[str, Any]:
        """Read the foreground window once. Safe to call directly, and used by
        the tests so the loop itself never has to be raced."""
        # The permission check is inside the loop, every single time. Checking it
        # once at startup would keep this thread alive after a revoke.
        if not permissions.allowed("screen"):
            self.status.permission_denials += 1
            context_manager.mark_blocked(
                "screen access is not granted - the observer is idle"
            )
            return {"changed": False, "reason": "permission_required"}

        window = read_foreground_window()
        if window is None:
            context_manager.mark_blocked(
                "the foreground window could not be read on this platform"
            )
            return {"changed": False, "reason": "unreadable"}

        title = (window.title or "").strip()
        if len(title) < MIN_TITLE_LENGTH:
            # Taskbar, desktop, an empty title: real, but not context.
            return {"changed": False, "reason": "chrome, not context"}

        # Process lookup only when the window handle changed, OR when the last
        # one came back empty. The retry matters: a process running at a higher
        # integrity level (an elevated app) cannot be opened by a normal one, and
        # that failure is often transient - the user switches away and back and
        # the read succeeds. Without the retry the context would stay anonymous
        # for the rest of the session.
        if window.hwnd != self._last_hwnd or not window.process:
            process = process_name(window.pid)
            if process:
                window.process = process
                self._last_hwnd = window.hwnd
                # A browser can tell us its real URL; ask, then.
                if any(b in process
                       for b in ("chrome", "msedge", "edge", "brave", "vivaldi")):
                    url = read_browser_url(window.hwnd, process)
                    if url:
                        window.url = url
                        self.status.url_reads += 1
            else:
                # Say so. An unnamed process is a real limitation (integrity
                # level, or a protected process), and the HUD should show that
                # rather than presenting "Unknown application" as a fact.
                self.status.unnamed_windows += 1

        identity = window.identity
        now = time.time()

        # Debounce: a context switch flickers through transient windows.
        if identity != self._last_identity:
            if self._candidate_hwnd != window.hwnd:
                self._candidate_hwnd = window.hwnd
                self._candidate_since = now
                return {"changed": False, "reason": "settling"}
            if now - self._candidate_since < SETTLE_S:
                return {"changed": False, "reason": "settling"}
            self._last_identity = identity

        result = context_manager.observe(window, source="observer")
        self.status.polls += 1
        if result.get("changed"):
            self.status.changes += 1
        return result

    def _loop(self) -> None:  # pragma: no cover - background thread
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Observer poll failed: %s", exc)
            self._stop.wait(self.interval_s)
        self.status.running = False


#: Process-wide observer.
observer = WindowObserver()
