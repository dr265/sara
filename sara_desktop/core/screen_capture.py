"""
core/screen_capture.py — real screen capture for the desktop.

Two consumers, one honest source:

  * the Show Screen panel streams throttled frames of the actual desktop
    from a background thread — the Qt main thread never blocks on a grab;
  * task completion stores one full PNG of the screen as it looked when the
    work finished (spec §9: the completion screenshot shows the real
    result — an opened document, a rendered track, a finished page).

Everything fails soft: a missing monitor / driver makes the functions
return None, and callers report "screenshot unavailable" instead of
pretending something was captured.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parents[1] / "memory"
SHOTS_DIR = DATA_DIR / "screenshots"


def _ensure_shots_dir() -> Path:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return SHOTS_DIR


def available() -> bool:
    try:
        import mss  # noqa: F401
        return True
    except Exception:
        return False


def capture_rgb(max_width: int = 0) -> Optional[Tuple[int, int, bytes]]:
    """One frame of the primary monitor as (width, height, RGB bytes).

    ``max_width`` > 0 downscales (PIL) so streams stay cheap.
    """
    try:
        import mss
    except Exception:
        return None
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            width, height = shot.size
            data = shot.rgb
            if max_width and width > max_width:
                try:
                    from PIL import Image
                    img = Image.frombytes("RGB", (width, height), data)
                    ratio = max_width / float(width)
                    img = img.resize(
                        (max_width, max(1, int(height * ratio))),
                        Image.LANCZOS)
                    width, height = img.size
                    data = img.tobytes()
                except Exception:
                    pass
            return int(width), int(height), bytes(data)
    except Exception:
        return None


def capture_png(path: Optional[str] = None, prefix: str = "shot",
                max_width: int = 1280) -> Optional[str]:
    """Save one screenshot as PNG; returns the real path or None."""
    frame = capture_rgb(max_width)
    if frame is None:
        return None
    width, height, data = frame
    try:
        from PIL import Image
        img = Image.frombytes("RGB", (width, height), data)
        if path:
            dest = Path(path)
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            millis = int(time.time() * 1000) % 1000
            dest = _ensure_shots_dir() / \
                f"{prefix}_{stamp}_{millis:03d}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG")
        return str(dest)
    except Exception:
        return None


class ScreenStreamer:
    """Throttled background screen frames with clean start/stop semantics."""

    def __init__(self, callback: Callable[[int, int, bytes], None],
                 interval_s: float = 0.9, max_width: int = 1024) -> None:
        self._callback = callback
        self._interval = max(0.25, float(interval_s))
        self._max_width = int(max_width or 1024)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sara-screen-stream")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            frame = capture_rgb(self._max_width)
            if frame is not None:
                try:
                    self._callback(*frame)
                except Exception:
                    pass
            elapsed = time.time() - started
            self._stop.wait(max(0.05, self._interval - elapsed))
