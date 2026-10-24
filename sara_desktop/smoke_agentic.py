"""Headless smoke: import ui + main wiring surfaces (no mic, no display)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ok = []

import ui
ok.append("ui module")
assert hasattr(ui, "SaraUI")
ok.append("SaraUI class")

from core.agent_registry import get_global_registry
reg = get_global_registry()
ok.append("registry singleton")

from core.agentic_runtime import AgenticRuntime, RuntimeMode
rt = AgenticRuntime(reg)
rt.set_mode("agentic")
assert rt.agentic_enabled
rt.set_mode("normal")
assert not rt.agentic_enabled
ok.append("runtime mode switch")

from core.agent_router import TaskRouter
router = TaskRouter(reg)
assert router is not None
ok.append("router")

# main.py imports (sounddevice/google.genai heavy but present in venv)
import main as main_mod
ok.append("main module")

# SaraLive tool declarations must include the agentic trio
src = (HERE / "main.py").read_text(encoding="utf-8")
for name in ("agentic_task", "agentic_status", "agentic_cancel"):
    assert name in src, f"{name} missing from main.py"
ok.append("agentic tools declared")

# ui.py must expose the mode button + panel update path
ui_src = (HERE / "ui.py").read_text(encoding="utf-8")
for token in ("_mode_btn", "_on_mode_toggled", "_agentic_sig",
              "_on_agentic_event", "AGENTIC RUNTIME", "on_mode_toggle"):
    assert token in ui_src, f"{token} missing from ui.py"
ok.append("ui mode + panel wiring")

print("HEADLESS OK:", " | ".join(ok))
