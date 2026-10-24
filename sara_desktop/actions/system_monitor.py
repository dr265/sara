
"""
System Monitor — background metric checks with voice alert support.
Zero subprocess calls on all platforms — uses ctypes/pynvml/psutil/wmi only.
"""
import ctypes
import platform
import threading
import time

import psutil

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

DEFAULT_THRESHOLDS = {
    "cpu":  90.0,
    "ram":  90.0,
    "temp": 85.0,
    "gpu":  95.0,
}

_COOLDOWN   = 300
_CPU_STREAK = 3

# ── GPU (NVML) — ONE process-wide, thread-safe session ───────────────────────
# NVML is NOT thread-safe, and this process queries it from several threads at
# once: the HUD poller (ui.py), the system_status tool (asyncio executor) and
# the background SystemMonitor loop. Two independent sessions re-initialising
# NVML concurrently corrupted the native heap and killed the whole app with
# 0xC0000374 (STATUS_HEAP_CORRUPTION). Every GPU read in the process must go
# through gpu_percent() — lock-serialised, initialised exactly once.
_gpu_lock   = threading.Lock()
_pynvml_mod = None      # cached pynvml module
_pynvml_h   = None      # cached device handle
_nvml_lib   = None      # cached ctypes NVML library
_nvml_dev   = None      # cached ctypes device handle
_gpu_mode   = None      # None=untested | "pynvml" | "ctypes" | "off"


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _init_ctypes_nvml() -> bool:
    """Load nvml.dll / libnvidia-ml once and cache lib + device handle.
    Caller must hold _gpu_lock."""
    global _nvml_lib, _nvml_dev
    if _nvml_lib is not None and _nvml_dev is not None:
        return True
    if _OS == "Windows":
        candidates = ("nvml", r"C:\Windows\System32\nvml.dll")
        load = ctypes.WinDLL
    else:
        candidates = ("libnvidia-ml.so.1", "libnvidia-ml.so",
                      "libnvidia-ml.dylib")
        load = ctypes.CDLL
    for name in candidates:
        try:
            lib = load(name)
            lib.nvmlInit_v2()
            dev = ctypes.c_void_p()
            lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            if not dev:
                continue
            _nvml_lib = lib
            _nvml_dev = dev
            return True
        except Exception:
            continue
    return False


def gpu_percent() -> float:
    """GPU utilisation — the ONLY NVML entry point in this process.

    Lock-serialised across every caller (HUD, tools, monitor loop) and
    initialised once. When NVML proves unavailable — or a query fails after
    a successful init — it is disabled permanently and reads return -1.0:
    a missing GPU number beats a heap-corrupted assistant.
    """
    global _pynvml_mod, _pynvml_h, _gpu_mode
    with _gpu_lock:
        if _gpu_mode == "off":
            return -1.0
        if _gpu_mode == "pynvml":
            try:
                return float(_pynvml_mod.nvmlDeviceGetUtilizationRates(
                    _pynvml_h).gpu)
            except Exception:
                _gpu_mode = "off"
                return -1.0
        if _gpu_mode == "ctypes":
            try:
                u = _Util()
                _nvml_lib.nvmlDeviceGetUtilizationRates(_nvml_dev,
                                                        ctypes.byref(u))
                return float(u.gpu)
            except Exception:
                _gpu_mode = "off"
                return -1.0

        # First call in this process: prefer pynvml, fall back to raw ctypes.
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            _pynvml_h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _pynvml_mod = pynvml
            _gpu_mode = "pynvml"
            return float(pynvml.nvmlDeviceGetUtilizationRates(_pynvml_h).gpu)
        except Exception:
            pass
        try:
            if _init_ctypes_nvml():
                _gpu_mode = "ctypes"
                u = _Util()
                _nvml_lib.nvmlDeviceGetUtilizationRates(_nvml_dev,
                                                        ctypes.byref(u))
                return float(u.gpu)
        except Exception:
            pass
        _gpu_mode = "off"
        return -1.0


def _get_gpu_usage() -> float:
    """Backward-compatible alias — everything funnels through gpu_percent()."""
    return gpu_percent()


def _get_cpu_temp() -> float:
    # psutil — works on Linux; occasionally Windows with proper drivers
    try:
        temps = psutil.sensors_temperatures()
        for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"]:
            if name in temps and temps[name]:
                return temps[name][0].current
        for entries in temps.values():
            if entries:
                return entries[0].current
    except Exception:
        pass

    # Windows: wmi module (pure Python COM, zero subprocess)
    if _OS == "Windows":
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="root/wmi")
            tz = w.MSAcpi_ThermalZoneTemperature()
            if tz:
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            pass

    return -1.0


def get_system_status() -> dict:
    """Snapshot of current system metrics for the system_status tool."""
    cpu  = psutil.cpu_percent(interval=0.2)
    ram  = psutil.virtual_memory()
    temp = _get_cpu_temp()
    gpu  = _get_gpu_usage()

    boot_time   = psutil.boot_time()
    uptime_secs = time.time() - boot_time
    uptime_h    = int(uptime_secs // 3600)
    uptime_m    = int((uptime_secs % 3600) // 60)

    return {
        "cpu_percent":   round(cpu, 1),
        "ram_percent":   round(ram.percent, 1),
        "ram_used_gb":   round(ram.used   / 1024 ** 3, 1),
        "ram_total_gb":  round(ram.total  / 1024 ** 3, 1),
        "cpu_temp_c":    round(temp, 1) if temp > 0 else None,
        "gpu_percent":   round(gpu,  1) if gpu  >= 0 else None,
        "uptime":        f"{uptime_h}h {uptime_m}m",
        "process_count": len(psutil.pids()),
    }


class SystemMonitor:
    """
    Stateful monitor — cooldown state persists across session reconnections.
    Call check() periodically; returns a [SYSTEM_ALERT] string or None.
    """

    def __init__(self, thresholds: dict | None = None):
        self.thresholds   = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}
        self._cpu_streak  = 0

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, 0)) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self) -> str | None:
        try:
            cpu  = psutil.cpu_percent(interval=None)
            ram  = psutil.virtual_memory().percent
            temp = _get_cpu_temp()
            gpu  = _get_gpu_usage()
        except Exception:
            return None

        alerts: list[str] = []

        if cpu >= self.thresholds["cpu"]:
            self._cpu_streak += 1
            if self._cpu_streak >= _CPU_STREAK and self._can_alert("cpu"):
                alerts.append(
                    f"[SYSTEM_ALERT] CPU usage has been critically high ({cpu:.0f}%) "
                    "for several seconds. Warn the user in their language and suggest "
                    "closing heavy applications."
                )
                self._record("cpu")
                self._cpu_streak = 0
        else:
            self._cpu_streak = 0

        if ram >= self.thresholds["ram"] and self._can_alert("ram"):
            alerts.append(
                f"[SYSTEM_ALERT] RAM is at {ram:.0f}% — nearly exhausted. "
                "Warn the user in their language and suggest freeing memory."
            )
            self._record("ram")

        if temp > 0 and temp >= self.thresholds["temp"] and self._can_alert("temp"):
            alerts.append(
                f"[SYSTEM_ALERT] CPU temperature is {temp:.0f}°C — above the safe limit. "
                "Warn the user in their language and advise reducing system load "
                "or checking cooling."
            )
            self._record("temp")

        if gpu >= 0 and gpu >= self.thresholds["gpu"] and self._can_alert("gpu"):
            alerts.append(
                f"[SYSTEM_ALERT] GPU load is at {gpu:.0f}%. "
                "Briefly inform the user in their language."
            )
            self._record("gpu")

        return " ".join(alerts) if alerts else None


