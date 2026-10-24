"""UI-level check (offscreen): mode toggle button + AGENTIC RUNTIME panel
render real runtime events. Run with QT_QPA_PLATFORM=offscreen."""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PyQt6.QtWidgets import QApplication
from ui import SaraUI, MainWindow


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    # MainWindow needs a face image; generate a tiny placeholder if absent.
    face = HERE / "_test_face.png"
    if not face.exists():
        from PyQt6.QtGui import QPixmap, QColor
        pm = QPixmap(64, 64)
        pm.fill(QColor("#00d4ff"))
        pm.save(str(face), "PNG")
    win = MainWindow(str(face))
    ok = []

    def pump():
        for _ in range(20):
            app.processEvents()
            time.sleep(0.02)

    # 1. mode button exists, starts NORMAL, and flips on toggle
    assert win._mode_btn is not None
    assert win._mode_btn.isChecked() is False
    toggled: list = []
    win.on_mode_toggle = lambda agentic: toggled.append(agentic)
    win._mode_btn.setChecked(True)          # fires _on_mode_toggled
    pump()
    assert win._mode_btn.isChecked() is True
    assert toggled == [True], f"callback got {toggled}"
    ok.append("mode toggle fires callback")

    # 2. panel widgets exist and show real event data
    assert win._ag_mode_lbl is not None
    win.push_agentic_event({"type": "mode", "mode": "agentic"})
    pump()
    assert "AGENTIC" in win._ag_mode_lbl.text()
    ok.append("panel mode label")

    # 3. agent counts render from a real registry snapshot
    from core.agent_registry import get_global_registry
    reg = get_global_registry()
    win.push_agentic_event({
        "kind": "agents", "data": {"counts": reg.state_counts(),
                                   "reason": "test"}})
    pump()
    assert "agents:" in win._ag_agents_lbl.text()
    ok.append("panel agent counts")

    # 4. task lifecycle events render
    win.push_agentic_event({
        "kind": "task", "data": {"phase": "started",
                                 "task": {"request": "scrape example.com",
                                          "agents": ["ai-web-scraper"]},
                                 "text": "started"}})
    pump()
    assert "started" in win._ag_task_lbl.text().lower()
    ok.append("panel task line")

    # 5. switching back to NORMAL leaves the button consistent
    win._mode_btn.setChecked(False)
    pump()
    assert toggled[-1] is False
    win.push_agentic_event({"type": "mode", "mode": "normal"})
    pump()
    assert "NORMAL" in win._ag_mode_lbl.text()
    ok.append("mode back to normal")

    print("UI OK:", " | ".join(ok))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
