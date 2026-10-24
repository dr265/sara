"""
Active Window & Foreground Application Context for SARA.
Ported from Brahma-Echo (core/window_context.py).

Uses Windows Win32 APIs via ctypes to inspect the user's current focused
application, window title, and capture targeted screenshots of the active
workspace. No third-party dependency required.
"""

import sys
import ctypes
from ctypes import wintypes
from typing import Optional, Dict, Any

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def get_foreground_window_info() -> Dict[str, Any]:
    """
    Returns metadata about the currently active foreground window:
    - title: Window title string
    - class_name: Window class name
    - hwnd: Window handle
    - rect: (x, y, w, h)
    """
    if not user32:
        return {"title": "", "class_name": "unknown", "hwnd": 0, "rect": (0, 0, 0, 0)}

    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "", "class_name": "", "hwnd": 0, "rect": (0, 0, 0, 0)}

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

        class_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buff, 256)
        class_name = class_buff.value.strip()

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(0, rect.right - rect.left)
        h = max(0, rect.bottom - rect.top)

        return {
            "title": title,
            "class_name": class_name,
            "pid": pid.value,
            "hwnd": hwnd,
            "rect": (rect.left, rect.top, w, h),
            "bbox": (rect.left, rect.top, rect.right, rect.bottom),
        }
    except Exception as exc:
        return {"title": "", "class_name": "error", "error": str(exc), "rect": (0, 0, 0, 0)}


def get_foreground_prompt_line() -> str:
    """One-line foreground-window summary for the system prompt.
    Empty string when the desktop is focused or on non-Windows, so idle
    moments don't clutter the prompt."""
    info = get_foreground_window_info()
    title = (info.get("title") or "").strip()
    if not title or title.lower() in ("program manager", "workerw", "desktop"):
        return ""
    return f"[ACTIVE WINDOW] The user is currently looking at: {title[:120]}"
