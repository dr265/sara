import sys
import os
import time
import subprocess
import platform
import shutil
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

# How long to wait for a launched app's process to show up, and the polling
# interval while waiting. Apps do not spawn instantly after `start` returns.
_PROC_WAIT_S = 6.0
_PROC_POLL_S = 0.5

# Direct executable locations that are commonly NOT on PATH (e.g. VS Code's
# Code.exe lives in its install folder; only code.cmd/`bin` is on PATH).
_DIRECT_EXE_PATHS = {
    "code": [
        "Microsoft VS Code/Code.exe",
        "VS Code/Code.exe",
    ],
}


def _proc_count(keywords: list[str]) -> int:
    """Count running processes whose image name matches any keyword.

    keywords are lowercase base names without extension, e.g. ['code', 'powerpnt'].
    Returns -1 when psutil is unavailable (verification disabled).
    """
    if not _PSUTIL:
        return -1
    n = 0
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        base = name.split(".")[0].strip()
        if base in keywords:
            n += 1
    return n


def _proc_keywords(normalized: str) -> list[str]:
    """Which process image names indicate that `normalized` is running."""
    full = (normalized or "").lower().strip().replace(".exe", "")
    # Multi-word product names whose process name is unrelated
    # ('Visual Studio Code' -> Code.exe, 'Google Chrome' -> chrome.exe, ...)
    aliases = {
        "visual studio code": ["code"],
        "google chrome": ["chrome"],
        "microsoft edge": ["msedge"],
        "file explorer": ["explorer"],
        "command prompt": ["cmd"],
    }
    if full in aliases:
        return aliases[full]
    base = full.split()[0] if full else ""
    return [base] if base else []

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "powershell terminal":{"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "command prompt":     {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw


def _launch_windows(app_name: str) -> bool:
    app_lower = app_name.lower().strip()

    # 1. Shells & Terminals: ALWAYS open interactive console window
    if "powershell" in app_lower:
        try:
            subprocess.Popen("start powershell.exe", shell=True)
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"[open_app] powershell start error: {e}")

    if "cmd" in app_lower or "command prompt" in app_lower:
        try:
            subprocess.Popen("start cmd.exe", shell=True)
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"[open_app] cmd start error: {e}")

    if "terminal" in app_lower:
        try:
            subprocess.Popen("start wt.exe", shell=True)
            time.sleep(0.8)
            return True
        except Exception:
            try:
                subprocess.Popen("start powershell.exe", shell=True)
                time.sleep(0.8)
                return True
            except Exception:
                pass

    # 2. Windows Protocol schemes
    if "whatsapp" in app_lower:
        try:
            subprocess.Popen("start whatsapp:", shell=True)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    if "spotify" in app_lower:
        try:
            subprocess.Popen("start spotify:", shell=True)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    if "calc" in app_lower:
        try:
            subprocess.Popen("start calc:", shell=True)
            time.sleep(0.8)
            return True
        except Exception:
            pass

    if ":" in app_name:
        try:
            subprocess.Popen(f"start {app_name}", shell=True)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    # 2.5. Known direct executable paths (apps whose launcher is not on PATH,
    #       e.g. VS Code: `start code` often fails silently even when installed)
    exe_rel = _DIRECT_EXE_PATHS.get(app_lower)
    if exe_rel:
        import os as _os
        roots = [_os.environ.get("LOCALAPPDATA", ""),
                 _os.environ.get("ProgramFiles", ""),
                 _os.environ.get("ProgramFiles(x86)", "")]
        for root in roots:
            if not root:
                continue
            for rel in exe_rel:
                candidate = Path(root) / rel
                if candidate.exists():
                    try:
                        os.startfile(str(candidate))
                        time.sleep(1.0)
                        return True
                    except Exception as e:
                        print(f"[open_app] direct exe failed: {e}")

    # 3. Native Windows ShellExecute via os.startfile
    try:
        os.startfile(app_name)
        time.sleep(1.0)
        return True
    except Exception:
        pass

    # 4. Windows `start "" "app_name"`
    try:
        res = subprocess.run(f'start "" "{app_name}"', shell=True, capture_output=True, timeout=3)
        if res.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    # 5. Which binary lookup
    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            subprocess.Popen(f'start "" "{app_name}"', shell=True)
            time.sleep(1.2)
            return True
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    # 6. Start Menu search via PyAutoGUI
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(2.0)
        return True
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    # 7. Web fallback for WhatsApp
    if "whatsapp" in app_lower:
        try:
            subprocess.Popen('start "" "https://web.whatsapp.com"', shell=True)
            return True
        except Exception:
            pass

    return False


def _launch_macos(app_name: str) -> bool:
    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]

def _launch_linux(app_name: str) -> bool:
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if shutil.which(term):
                try:
                    subprocess.Popen([term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    return True
                except Exception:
                    continue

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    app_name = (
        params.get("app_name") or
        params.get("app") or
        params.get("name") or
        params.get("application") or
        params.get("program") or
        params.get("target") or
        ""
    ).strip()

    if not app_name:
        return "No application name provided."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' -> '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    keywords = _proc_keywords(normalized) or _proc_keywords(app_name)
    baseline  = _proc_count(keywords) if keywords else -1   # -1 = unverifiable

    try:
        launched = launcher(normalized)
        if not launched and normalized.lower() != app_name.lower():
            launched = launcher(app_name)

        if not launched:
            return (
                f"Could not confirm that {app_name} launched. "
                f"It may still be loading, or it might not be installed."
            )

        # VERIFY: a NEW matching process should appear within a few seconds
        # (compared against the baseline, so an already-running instance does
        # not fake a success). `start` and os.startfile return 0 even when
        # nothing actually opens — the classic false 'VS Code opened'.
        if keywords and baseline >= 0:
            deadline = time.time() + _PROC_WAIT_S
            while time.time() < deadline:
                if _proc_count(keywords) > baseline:
                    return f"Opened {app_name} successfully."
                time.sleep(_PROC_POLL_S)
            if baseline > 0:
                return (f"{app_name} is already running ({baseline} instance(s)); "
                        f"a new instance was requested but did not appear. It may "
                        f"have focused the existing window instead.")
            return (
                f"Issued the launch command for {app_name}, but no matching "
                f"process appeared within {_PROC_WAIT_S:.0f}s — it likely did "
                "NOT open. It may not be installed or the command may have "
                "failed silently."
            )

        # No psutil / unknown app: report honestly that this is unverified.
        return (f"Launch command for {app_name} was issued "
                f"(process verification unavailable).")
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"


TOOL = {
    "name": "open_app",
    "description": "Opens any application on the computer. Use this whenever the user asks to open, launch, or start any app, terminal, website, or program. Always call this tool — never just say you opened it.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Exact name of the application (e.g. 'PowerShell', 'WhatsApp', 'Chrome', 'Spotify')"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}
