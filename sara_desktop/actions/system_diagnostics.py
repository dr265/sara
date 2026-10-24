# actions/system_diagnostics.py
"""
Deep system diagnostics for SARA.
Ported from Brahma-Echo (actions/system_diagnostics_mcp.py), compacted for
SARA's action contract. Adds what system_status/computer_settings lack:
battery, per-monitor brightness get/set, top processes, and kill process.
"""

import platform
import subprocess
import time
from datetime import timedelta

import psutil


def _battery() -> str:
    batt = psutil.sensors_battery()
    if batt is None:
        return "No battery detected (desktop PC)."
    plugged = "charging" if batt.power_plugged else "on battery"
    mins = int(batt.secsleft / 60) if batt.secsleft and batt.secsleft > 0 else None
    left = f", about {mins//60}h{mins%60:02d}m left" if mins else ""
    return f"Battery {batt.percent:.0f}% ({plugged}{left})."


def _brightness(action: str, level=None) -> str:
    try:
        import screen_brightness_control as sbc
    except ImportError:
        raise ValueError("screen_brightness_control is not installed.")
    try:
        names = sbc.list_monitors()
        if action == "get":
            levels = sbc.get_brightness()
            names = list(names) if names else []
            if not isinstance(levels, (list, tuple)):
                levels = [levels]
            rows = []
            for i, lvl in enumerate(levels):
                label = str(names[i]) if i < len(names) and names[i] else ""
                if not label or label.lower().startswith("none"):
                    label = f"Monitor {i + 1}"
                rows.append(f"{label}: {lvl}%")
            return "; ".join(rows) or "No monitors found."
        # set
        lvl = max(0, min(100, int(level)))
        sbc.set_brightness(lvl)
        return f"Set brightness to {lvl}% on {len(names)} monitor(s)."
    except Exception as e:
        raise ValueError(f"Brightness control failed: {e}")


def _top_processes(limit: int = 8) -> str:
    procs = list(psutil.process_iter(["pid", "name"]))
    # psutil's first cpu_percent read is always 0.0 — prime every process,
    # sample over a short window, then take a real per-process reading.
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.5)
    rows = []
    for p in procs:
        try:
            name = p.info["name"] or "?"
            if p.pid == 0 or name.lower() == "system idle process":
                continue          # the idle process is machine idle, not a task
            rows.append((p.pid, name, p.cpu_percent(None) or 0.0))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda t: t[2], reverse=True)
    lines = [f"{name} (pid {pid}): {cpu:.1f}% CPU" for pid, name, cpu in rows[:limit]]
    return "Top processes:\n" + "\n".join(lines)


def _kill(pid: int, name: str) -> str:
    try:
        p = psutil.Process(pid)
        pname = p.name()
        p.kill()
        p.wait(timeout=3)
        return f"Killed {pname} (pid {pid})."
    except psutil.NoSuchProcess:
        raise ValueError(f"No process with pid {pid}.")
    except psutil.AccessDenied:
        raise ValueError(f"Access denied killing pid {pid} (try running SARA as administrator).")
    except Exception as e:
        raise ValueError(f"Kill failed: {e}")


def _disk_usage() -> str:
    parts = []
    for part in psutil.disk_partitions():
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            parts.append(f"{part.mountpoint} {usage.percent:.0f}% used "
                         f"({usage.free / 2**30:.0f} GB free)")
        except (PermissionError, OSError):
            continue
    return "; ".join(parts) or "No disks readable."


def system_diagnostics(parameters: dict, player=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "full").lower().strip()

    if action in ("battery",):
        return _battery()

    elif action in ("brightness", "get_brightness"):
        return _brightness("get")

    elif action in ("set_brightness", "brightness_set"):
        try:
            lvl = int(p.get("level", 50))
        except (TypeError, ValueError):
            raise ValueError("Provide 'level' (0-100).")
        return _brightness("set", lvl)

    elif action in ("top_processes", "processes", "top"):
        return _top_processes()

    elif action in ("kill", "kill_process", "end_task"):
        pid = p.get("pid")
        name = (p.get("name") or "").strip()
        if not pid and not name:
            raise ValueError("Provide 'pid' or 'name' of the process to kill.")
        if not pid:
            # resolve first match by name
            for pr in psutil.process_iter(["pid", "name"]):
                if (pr.info["name"] or "").lower().startswith(name.lower()):
                    pid = pr.info["pid"]
                    break
            if not pid:
                raise ValueError(f"No process found matching '{name}'.")
        return _kill(int(pid), name)

    elif action in ("disk", "disks", "storage"):
        return _disk_usage()

    elif action in ("full", "all", "report"):
        import time as _time
        vm = psutil.virtual_memory()
        uptime = timedelta(seconds=int(_time.time() - psutil.boot_time()))
        try:
            brightness = _brightness("get")
        except ValueError as exc:
            # a full report must still deliver the parts that work
            brightness = f"unavailable ({exc})"
        lines = [
            f"OS: {platform.system()} {platform.release()} ({platform.machine()})",
            f"Uptime: {str(uptime).split('.')[0]}",
            f"RAM: {vm.percent:.0f}% used of {vm.total / 2**30:.0f} GB",
            f"Battery: {_battery()}",
            f"Brightness: {brightness}",
            f"Disks: {_disk_usage()}",
            _top_processes(5),
        ]
        return "\n".join(lines)

    raise ValueError(
        f"Unknown diagnostics action: '{action}'. Valid actions: battery, "
        "brightness, set_brightness(level), top_processes, kill(pid|name), "
        "disk, full.")


TOOL = {
    "name": "system_diagnostics",
    "description": (
        "Deeper system diagnostics than system_status: battery state, "
        "per-monitor brightness (get/set), top processes by CPU, kill a "
        "process by pid or name, disk usage, or a full report. "
        "Actions: battery, brightness, set_brightness(level), top_processes, "
        "kill(pid|name), disk, full."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "battery | brightness | set_brightness | top_processes | kill | disk | full",
            },
            "level": {
                "type": "NUMBER",
                "description": "Brightness level 0-100 (for set_brightness).",
            },
            "pid": {
                "type": "NUMBER",
                "description": "Process ID to kill.",
            },
            "name": {
                "type": "STRING",
                "description": "Process name to kill (e.g. 'chrome').",
            },
        },
        "required": ["action"],
    },
    "handler": system_diagnostics,
}
