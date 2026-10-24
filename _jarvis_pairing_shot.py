"""Render the REAL PhonePairingOverlay offscreen to a PNG for review.

Instantiates the actual desktop overlay, lets it mint a live offer on the
backend, and saves exactly what the user sees. The offer expires on its own
after 120 s — nothing is claimed here.

Run:  sara_desktop\\venv\\Scripts\\python.exe _jarvis_pairing_shot.py
"""
from __future__ import annotations

import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "sara_desktop"))

# Use the real platform plugin (real fonts) but never map a window:
# WA_DontShowOnScreen lays the widget out and paints it while it stays hidden.
os.environ.pop("QT_QPA_PLATFORM", None)

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui import PhonePairingOverlay  # noqa: E402

OUT = os.path.join(os.environ.get("TEMP", REPO), "sara_pairing_overlay.png")


def main() -> int:
    app = QApplication(sys.argv)
    overlay = PhonePairingOverlay(parent=None)
    overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    overlay.resize(overlay._OW, overlay._OH)
    overlay.show()

    deadline = time.time() + 30
    while time.time() < deadline and overlay._minting:
        app.processEvents()
        time.sleep(0.05)
    # let the QR pixmap and labels paint
    for _ in range(10):
        app.processEvents()
        time.sleep(0.05)

    ok = overlay.grab().save(OUT)
    print(f"saved: {ok} -> {OUT}")
    print(f"payload: {overlay._url_label.text()}")
    print(f"code   : {overlay._pairing_code}")
    print(f"status : {overlay._status_label.text()}")
    overlay._do_close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
