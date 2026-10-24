"""Manual smoke test: phone pairing WITHOUT the Brahma Connect app.

Drives the real desktop overlay (``sara_desktop.ui.PhonePairingOverlay``)
offscreen and performs the exact HTTP calls a phone camera + browser would:

  1. the overlay mints a real offer on the backend   (POST /ultron/pairing)
  2. this script opens the QR payload URL            (GET  /ultron/pair/claim?t=)
  3. this script taps PAIR THIS DEVICE               (POST /ultron/pairing/claim)
  4. the overlay must notice by itself and show "Paired ✓"

Cleanup: the smoke-test device is revoked at the end so it does not linger
in ``/ultron/devices``.

Run from the repo root (backend must be running):

    sara_desktop\\venv\\Scripts\\python.exe _jarvis_pairing_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):        # ✓ and … on a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "sara_desktop"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.agent_registry import BackendConnection  # noqa: E402
from core.backend_url import resolve_lan_ip  # noqa: E402
from ui import PhonePairingOverlay  # noqa: E402


def http(method: str, url: str, *, body=None, headers=None, timeout=10):
    """One HTTP call, returning (status, body_text). 4xx/5xx are returned too."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_until(app: QApplication, predicate, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    conn = BackendConnection()
    base = conn.base_url
    lan = resolve_lan_ip()
    print(f"backend URL : {base}")
    print(f"LAN address : {lan or '(none — phone could not reach this PC)'}")

    app = QApplication(sys.argv)
    overlay = PhonePairingOverlay(parent=None)
    overlay.show()

    # 1) the overlay mints on its own (background thread -> signal -> UI)
    minted = wait_until(
        app,
        lambda: (not overlay._minting) and bool(overlay._url_label.text()),
        30.0)
    if not minted:
        print(f"FAIL: no offer minted — status={overlay._status_label.text()!r}")
        return 1

    payload = overlay._url_label.text().strip()
    code = overlay._pairing_code
    print(f"QR payload  : {payload}")
    print(f"pair code   : {code}")
    print(f"overlay says: {overlay._status_label.text()!r}")

    problems = []
    if not payload.startswith("http://"):
        problems.append("payload is not an http URL")
    if "/ultron/pair/claim?t=" not in payload:
        problems.append("payload is not the backend's claim link")
    if "http://127.0.0.1" in payload or "localhost" in payload:
        problems.append("payload points at loopback — a phone cannot reach it")
    if lan and f"http://{lan}:" not in payload:
        problems.append(f"payload does not use the LAN IP {lan}")
    if not (len(code) == 6 and code.isdigit()):
        problems.append(f"pair_code {code!r} is not a 6-digit code")

    token_match = re.search(r"[?&]t=([^&]+)", payload)
    if not token_match:
        problems.append("no token in the payload")
        token = ""
    else:
        token = token_match.group(1)

    # 2) the phone camera opens the link -> backend-served redeem page
    page_status, page = http("GET", payload)
    print(f"redeem page : HTTP {page_status}, {len(page)} bytes")
    if page_status != 200:
        problems.append(f"redeem page returned HTTP {page_status}")
    if "PAIR" not in page.upper():
        problems.append("redeem page has no PAIR control")

    # 3) the page's button -> keyless claim (exactly what the phone posts)
    claim_status, raw = http(
        "POST", f"{base}/ultron/pairing/claim",
        body={"token": token, "kind": "phone", "platform": "smoke-test"})
    try:
        claim = json.loads(raw)
    except Exception:
        claim = {}
    device = claim.get("device") or {}
    device_id = device.get("device_id") or ""
    print(f"claim       : HTTP {claim_status} device={device_id or '(none)'} "
          f"state={device.get('state') or '-'}")
    if claim_status != 200 or not device_id:
        problems.append(f"claim failed: HTTP {claim_status} {raw[:200]}")

    # 4) the overlay must detect the redemption on its own (poll thread)
    paired = wait_until(app, lambda: overlay._paired, 25.0)
    print(f"overlay     : paired={overlay._paired} "
          f"status={overlay._status_label.text()!r}")
    if not paired:
        problems.append("overlay never flipped to Paired ✓")

    # cleanup: revoke the smoke-test device so the registry stays honest
    if device_id:
        rev_status, raw = http(
            "POST", f"{base}/ultron/devices/{device_id}/revoke",
            body={"reason": "pairing smoke test cleanup"}, headers=conn.headers())
        try:
            rev = json.loads(raw).get("revoked")
        except Exception:
            rev = raw[:120]
        print(f"revoke      : HTTP {rev_status} revoked={rev}")
        if rev_status != 200:
            problems.append(f"could not revoke smoke-test device: HTTP {rev_status}")

    overlay._do_close()
    app.processEvents()

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPAIRING SMOKE TEST: PASS — camera -> browser page -> paired, no app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
