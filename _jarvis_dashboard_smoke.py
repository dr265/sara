"""_jarvis_dashboard_smoke.py — E2E smoke for the phone Remote Dashboard.

Runs the REAL sara_desktop/dashboard server in-process on a scratch port
(default 8013, plain HTTP, firewall prompts disabled) and exercises every
endpoint the phone UI uses, exactly like the phone does:

  pages      GET /  · GET /login · GET /static/crypto.js
  auth       POST /login (bad + good pin) · GET /auto-login (expired + ok)
  devices    POST /api/device-login (bad + good) · POST /api/revoke-devices
  commands   POST /api/command (plain + AES-256-CBC, replicated phone crypto)
  sockets    WS /ws (history replay + encrypted command) · WS /ws/phone-audio
  upload     POST /api/upload · GET /api/files · GET /uploads/<name>?token=

Run with the desktop venv (needs requests):
    sara_desktop\\venv\\Scripts\\python.exe _jarvis_dashboard_smoke.py
Exits 0 when everything passes.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DSK = ROOT / "sara_desktop"
PORT = int(os.environ.get("SARA_DASH_SMOKE_PORT", "8013"))
os.environ["SARA_DESKTOP_DASH_PORT"] = str(PORT)
sys.path.insert(0, str(DSK))
os.chdir(DSK)

try:  # cp1252 consoles choke on '→' etc. — never let a print kill the smoke
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests  # noqa: E402
import uvicorn  # noqa: E402
from dashboard import server as S  # noqa: E402

# Plain HTTP + no firewall/UAC side effects for the smoke run.
_ORIG_SSL = S.DashboardServer._ssl_enabled
S.DashboardServer._ssl_enabled = staticmethod(lambda: False)
S._ensure_network_access = lambda port: None

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" +
          (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def client_encrypt(session_key: str, plain: str) -> str:
    """Replicate the phone crypto: b64( IV || AES-256-CBC(
    key = SHA256(session_key + salt) ) ) — mirror of app.html encryptText()."""
    from cryptography.hazmat.primitives import padding as sym_pad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = hashlib.sha256(session_key.encode() + b"SARA-DASHBOARD-v1").digest()
    iv = os.urandom(16)
    padder = sym_pad.PKCS7(128).padder()
    data = padder.update(plain.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(iv + ct).decode("ascii")


def main() -> int:  # noqa: C901 — linear test script
    srv = S.DashboardServer()
    base = f"http://127.0.0.1:{PORT}"
    loop_box: dict = {}

    def run_server() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_box["loop"] = loop
        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                             log_level="error")
        loop.run_until_complete(uvicorn.Server(cfg).serve())

    threading.Thread(target=run_server, daemon=True).start()
    for _ in range(600):
        try:
            requests.get(base + "/login", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("FAIL  server did not start")
        return 1
    print(f"[smoke] dashboard test server on {base}\n")

    # ── pages ─────────────────────────────────────────────────────────────
    r = requests.get(base + "/", timeout=10)
    check("GET / serves app.html",
          r.status_code == 200 and "◈ SARA" in r.text
          and "downsampleTo16k" in r.text and "__PORT__" not in r.text)
    r = requests.get(base + "/login", timeout=10)
    check("GET /login serves login.html",
          r.status_code == 200 and "REMOTE ACCESS" in r.text)
    r = requests.get(base + "/static/crypto.js", timeout=30)
    check("GET /static/crypto.js serves CryptoJS",
          r.status_code == 200 and b"CryptoJS" in r.content,
          f"status={r.status_code}")

    # ── login / auto-login / device sessions ─────────────────────────────
    r = requests.post(base + "/login", json={"pin": "ZZZZZZ"}, timeout=10)
    check("bad pin rejected (401)", r.status_code == 401)

    key = srv.new_key()
    r = requests.post(base + "/login", json={"pin": key}, timeout=10)
    j = r.json()
    check("good pin mints bearer token",
          r.status_code == 200 and j.get("ok") and j.get("token"))
    tok = j.get("token", "")

    r = requests.get(base + "/auto-login", params={"key": "NOPE99"}, timeout=10)
    check("expired/unknown auto-login key shows Link Expired",
          r.status_code == 200 and "Link Expired" in r.text)

    key2 = srv.new_key()
    r = requests.get(base + "/auto-login", params={"key": key2}, timeout=10)
    m = re.search(r"sara_device_token','([^']+)'", r.text or "")
    m2 = re.search(r"sara_token','([^']+)'", r.text or "")
    check("good auto-login redirects with tokens",
          r.status_code == 200 and bool(m) and bool(m2))
    dev_tok = m.group(1) if m else ""
    tok2 = m2.group(1) if m2 else ""

    r = requests.post(base + "/api/device-login",
                      json={"device_token": "bogus"}, timeout=10)
    check("device-login rejects unknown device (401)", r.status_code == 401)

    r = requests.post(base + "/api/device-login",
                      json={"device_token": dev_tok}, timeout=10)
    j = r.json() if r.status_code == 200 else {}
    check("device-login re-issues token for paired device",
          r.status_code == 200 and j.get("ok") and j.get("token")
          and j.get("key") == key2)
    tok2 = j.get("token", tok2)

    # ── commands (plain + AES) ────────────────────────────────────────────
    h2 = {"Authorization": f"Bearer {tok2}"}
    r = requests.post(base + "/api/command", headers=h2,
                      json={"text": "plain-smoke-cmd"}, timeout=10)
    ok = r.status_code == 200 and list(srv._command_queue._queue)[-1] == \
        "plain-smoke-cmd"
    check("plain HTTP command reaches the command queue", ok)

    secret = "aes-smoke-a1b2c3"
    enc = client_encrypt(key2, secret)
    r = requests.post(base + "/api/command", headers=h2,
                      json={"enc": enc}, timeout=10)
    ok = r.status_code == 200 and list(srv._command_queue._queue)[-1] == secret
    check("AES-encrypted command decrypts server-side to plaintext", ok)

    r = requests.post(base + "/api/command",
                      json={"text": "no-auth"}, timeout=10)
    check("command without bearer rejected (401)", r.status_code == 401)

    # ── sockets ───────────────────────────────────────────────────────────
    async def ws_checks() -> None:
        import websockets
        url = f"ws://127.0.0.1:{PORT}/ws?token={tok2}"

        async def drain_until(wsx, wanted=None, timeout=1.5):
            """Collect messages until `wanted` type arrives (or timeout)."""
            got = []
            end = time.time() + timeout
            while time.time() < end:
                try:
                    raw = await asyncio.wait_for(
                        wsx.recv(), timeout=max(0.05, end - time.time()))
                except Exception:
                    break
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                got.append(data)
                if wanted and data.get("type") == wanted:
                    break
            return got

        async with websockets.connect(url, close_timeout=3) as ws:
            hist = await drain_until(ws, timeout=1.5)
            check("WS /ws replays broadcast history (sys)",
                  any(m.get("type") == "sys" for m in hist))
            check("history replay entries carry replay=true",
                  bool(hist) and all(m.get("replay") is True for m in hist))

            cmd = "ws-cmd-e2e"
            await ws.send(json.dumps(
                {"type": "command", "enc": client_encrypt(key2, cmd)}))
            await asyncio.sleep(0.4)
            check("WS encrypted command reaches the command queue",
                  list(srv._command_queue._queue)[-1] == cmd)

            # Heartbeat: phone ping → server pong (stuck-socket detection).
            await ws.send(json.dumps({"type": "ping"}))
            msgs = await drain_until(ws, wanted="pong", timeout=2)
            check("WS ping -> pong heartbeat",
                  any(m.get("type") == "pong" for m in msgs))

            # Activity mirror: what SARA did reaches the phone channel.
            fut_act = asyncio.run_coroutine_threadsafe(
                srv.broadcast({"type": "action", "text": "Opened Notepad"}),
                loop_box["loop"])
            await asyncio.wrap_future(fut_act)
            msgs = await drain_until(ws, wanted="action", timeout=3)
            act = next((m for m in msgs if m.get("type") == "action"), None)
            check("action feed broadcast reaches the phone channel",
                  bool(act) and act.get("text") == "Opened Notepad")

            # SARA's spoken audio mirrors to phones as base64 24 kHz PCM.
            tone = bytes(range(0, 120)) * 4
            b64 = base64.b64encode(tone).decode("ascii")
            fut = asyncio.run_coroutine_threadsafe(
                srv.broadcast({"type": "audio", "rate": 24000, "data": b64}),
                loop_box["loop"])
            await asyncio.wrap_future(fut)
            msgs = await drain_until(ws, wanted="audio", timeout=3)
            aud = next((m for m in msgs if m.get("type") == "audio"), None)
            check("audio broadcast reaches the phone channel (base64 PCM)",
                  bool(aud) and base64.b64decode(aud.get("data", "")) == tone
                  and aud.get("rate") == 24000)

            fut2 = asyncio.run_coroutine_threadsafe(
                srv.broadcast({"type": "audio_stop"}), loop_box["loop"])
            await asyncio.wrap_future(fut2)
            msgs = await drain_until(ws, wanted="audio_stop", timeout=3)
            check("audio_stop broadcast reaches the phone channel",
                  any(m.get("type") == "audio_stop" for m in msgs))

        # Reconnect: history replay must NOT include stale audio frames.
        async with websockets.connect(url, close_timeout=3) as ws2:
            saw_audio = False
            try:
                while True:
                    raw = await asyncio.wait_for(ws2.recv(), timeout=1.5)
                    if json.loads(raw).get("type") == "audio":
                        saw_audio = True
            except Exception:
                pass
            check("audio frames excluded from reconnect history", not saw_audio)

        # bad token must be refused
        refused = False
        try:
            async with websockets.connect(
                    f"ws://127.0.0.1:{PORT}/ws?token=bad", close_timeout=3):
                pass
        except Exception:
            refused = True
        check("WS /ws rejects bad token", refused)

        # phone mic channel: bad token refused
        refused = False
        try:
            async with websockets.connect(
                    f"ws://127.0.0.1:{PORT}/ws/phone-audio?token=bad",
                    close_timeout=3):
                pass
        except Exception:
            refused = True
        check("WS /ws/phone-audio rejects bad token", refused)

        # phone mic channel: good token accepts 16 kHz PCM frames
        async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/ws/phone-audio?token={tok2}",
                close_timeout=3) as ws:
            await ws.send(bytes(3200))          # 100 ms of 16 kHz mono int16
            await asyncio.sleep(0.4)
        check("phone PCM frame lands in the relay queue",
              srv._phone_audio_queue.qsize() >= 1)

    asyncio.run(ws_checks())

    # ── upload / files / download ─────────────────────────────────────────
    name_in = "smoke_upload.txt"
    payload = b"hello sara from the phone smoke"
    r = requests.post(base + "/api/upload", headers=h2,
                      files={"file": (name_in, payload)}, timeout=15)
    j = r.json() if r.status_code == 200 else {}
    check("POST /api/upload stores the file",
          r.status_code == 200 and j.get("ok") and j.get("size") == len(payload),
          f"status={r.status_code} body={r.text[:80]}")
    saved = j.get("name", "")

    r = requests.get(base + "/api/files", headers=h2, timeout=10)
    names = [f.get("name") for f in r.json().get("files", [])] \
        if r.status_code == 200 else []
    check("GET /api/files lists the upload", saved in names)

    r = requests.get(base + f"/uploads/{saved}", params={"token": tok2},
                     timeout=10)
    check("GET /uploads/<name>?token= downloads it",
          r.status_code == 200 and r.content == payload)

    r = requests.get(base + f"/uploads/{saved}", params={"token": "bad"},
                     timeout=10)
    check("upload download rejects bad token (401)", r.status_code == 401)

    try:
        (S.UPLOADS_DIR / saved).unlink()
        print(f"  cleanup: removed {S.UPLOADS_DIR / saved}")
    except Exception as e:
        print(f"  cleanup failed: {e}")

    # ── device revoke (last — kills the device session) ───────────────────
    r = requests.post(base + "/api/revoke-devices", headers=h2, timeout=10)
    check("admin can revoke device sessions",
          r.status_code == 200 and r.json().get("revoked", 0) >= 1)
    r = requests.post(base + "/api/device-login",
                      json={"device_token": dev_tok}, timeout=10)
    check("revoked device can no longer auto-login (401)",
          r.status_code == 401)

    # ── informational: HTTPS readiness ────────────────────────────────────
    S.DashboardServer._ssl_enabled = staticmethod(_ORIG_SSL)
    certs = (DSK / "config" / "certs" / "sara.key").exists() and \
            (DSK / "config" / "certs" / "sara.crt").exists()
    url = srv.get_url()
    print(f"\n  INFO  TLS certs present: {certs} · get_url() -> {url}")
    if certs:
        check("certs exist -> get_url() switches to https",
              url.startswith("https://"), url)

    print()
    if FAILED:
        print(f"SMOKE FAIL — {len(FAILED)} check(s) failed: {FAILED}")
        return 1
    print("SMOKE PASS — phone Remote Dashboard endpoints all green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
