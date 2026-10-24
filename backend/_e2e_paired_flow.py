"""FULL E2E: real backend + real pairing + real Windows agent + real desktop.

This is the Phase 14/19 workflow, run for real against the live backend on
127.0.0.1:8000:

    1. the backend mints a pairing offer
    2. the Windows agent claims it and gets a device token
    3. the agent opens the authenticated link and registers as an executor
    4. the backend is asked to run a real desktop action
    5. the command is dispatched over the link and executed ON THIS PC
    6. the real result comes back
    7. the PC is verified independently (tasklist)
    8. the backend is shut down / the agent drops -> state goes offline honestly

Nothing is mocked. Run:
    backend/venv/Scripts/python.exe backend/_e2e_paired_flow.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
for p in (str(ROOT), str(BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

BASE = "http://127.0.0.1:8000"


def _env(key: str) -> str:
    for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


API_KEY = _env("SARA_API_KEY")

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    return api_status(path, method, body)[1]


def api_status(path: str, method: str = "GET", body: dict | None = None,
               timeout: float = 60.0) -> tuple[int, dict]:
    """Like ``api`` but also returns the HTTP status, so the test can tell a
    409 (needs confirmation) from a 503 (no PC connected) from a 200."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def notepad_running() -> bool:
    out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=30).stdout
    return "Notepad.exe" in out


def main() -> int:
    import websocket  # websocket-client

    print("=" * 74)
    print("FULL E2E: website -> cloud backend -> authenticated link -> real PC")
    print("=" * 74)

    # ---------------------------------------------------------------- 1
    print("\n1. PAIRING: backend mints a one-time offer")
    offer = api("/ultron/pairing", "POST",
                {"kind": "pc_agent", "label": "E2E-PC", "ttl_s": 120})
    token = offer.get("token") or offer.get("pairing_token") or ""
    record("pairing offer minted", bool(token), f"kind={offer.get('kind')} ttl={offer.get('ttl_s')}s")

    # ---------------------------------------------------------------- 2
    print("\n2. CLAIM: the PC redeems it for a device token")
    claim = api("/ultron/pairing/claim", "POST",
                {"token": token, "kind": "pc_agent", "label": "E2E-PC"})
    device_token = claim.get("device_token") or ""
    record("device token issued", bool(device_token),
           f"device={claim.get('device_id', '?')[:14]}")

    if not device_token:
        print("\nCannot continue without a device token.")
        return 1

    # ---------------------------------------------------------------- 3
    print("\n3. LINK: the agent connects and registers as an EXECUTOR")
    ws_url = BASE.replace("http://", "ws://") + f"/ultron/link?token={device_token}"
    received: list[dict] = []
    ready = threading.Event()

    def on_message(_ws, message):
        try:
            frame = json.loads(message)
        except Exception:
            return
        received.append(frame)
        if frame.get("type") == "welcome":
            ready.set()

    ws = websocket.WebSocketApp(ws_url, on_message=on_message)
    th = threading.Thread(target=ws.run_forever, daemon=True)
    th.start()

    ok_connect = ready.wait(20)
    record("agent connected + authenticated", ok_connect,
           "welcome received" if ok_connect else "no welcome within 20s")
    if not ok_connect:
        return 1

    scopes = next((f.get("scopes") for f in received if f.get("type") == "welcome"), [])
    record("executor scope granted", "pc.execute" in (scopes or []),
           f"scopes={scopes}")

    # The link handler registers the agent on the server; give it a moment.
    time.sleep(1.5)

    # ---------------------------------------------------------------- 4
    print("\n4. DIAGNOSTICS: the backend now reports PC control available")
    status = api("/ultron/execution")
    record("pc_control_available flipped to true", status.get("pc_control_available") is True,
           f"agents={[a.get('label') for a in status.get('windows_agents', [])]}")

    # ---------------------------------------------------------------- 5
    print("\n5. DISPATCH: a real command goes out over the link")
    dispatched: dict = {}

    def on_command(ws, message):
        frame = json.loads(message)
        if frame.get("type") != "command":
            return
        # Execute for real through the same registry the desktop UI uses.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "local_agent", ROOT / "sara_desktop" / "local_agent.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result = mod.LocalAgent().execute(frame.get("tool", ""),
                                          frame.get("args") or {},
                                          confirmed=bool(frame.get("confirmed")))
        result.update({"type": "result", "command_id": frame.get("command_id"),
                       "device_id": frame.get("device_id")})
        dispatched.update(result)
        ws.send(json.dumps(result))

    ws2 = websocket.WebSocketApp(ws_url, on_message=on_command)
    threading.Thread(target=ws2.run_forever, daemon=True).start()
    time.sleep(2.0)

    # Dispatch through the PUBLIC endpoint, not a locally built bridge. The
    # previous version constructed its own ExecutionBridge and dispatched
    # through that, which bypassed the very thing under test: the agent is
    # registered on the SERVER's bridge when it connects over the link, so a
    # second in-process bridge never reaches it. Everything below therefore
    # goes over HTTP exactly as the website does.
    code, outcome = api_status("/ultron/execute", "POST",
                               {"tool": "open_app", "args": {"app_name": "notepad"},
                                "timeout_s": 40.0}, timeout=90)
    record("command dispatched + real result returned",
           code == 200 and outcome.get("ok") is True,
           str(outcome.get("output") or outcome.get("error", ""))[:80])

    # ---------------------------------------------------------------- 6
    print("\n6. VERIFICATION: was the app ACTUALLY opened on this PC?")
    time.sleep(1.5)
    alive = notepad_running()
    record("Notepad really running (tasklist, not the tool's word)", alive)
    record("agent reported success", outcome.get("ok") is True,
           f"verified={outcome.get('verified')}")

    # ---------------------------------------------------------------- 7
    print("\n7. HONEST FAILURE: a bad app must NOT report success")
    bad_code, bad = api_status("/ultron/execute", "POST",
                               {"tool": "open_app",
                                "args": {"app_name": "not-a-real-app-xyz"},
                                "timeout_s": 40.0}, timeout=90)
    record("bad app -> ok=False",
           bad.get("ok") is False and bad.get("executed") is True,
           f"http={bad_code} " + str(bad.get("output") or bad.get("error", ""))[:70])

    # ---------------------------------------------------------------- 8
    print("\n8. GATED ACTION IS REFUSED WITHOUT CONFIRMATION")
    gate_code, gated = api_status("/ultron/execute", "POST",
                                  {"tool": "file_controller",
                                   "args": {"action": "delete",
                                            "path": r"C:\e2e\never"},
                                   "timeout_s": 15.0}, timeout=60)
    record("gated delete blocked",
           gate_code == 409 and gated.get("requires_confirmation") is True
           and gated.get("executed") is False,
           str(gated.get("prompt") or gated.get("error", ""))[:80])

    # And once the user actually approves, it reaches the PC as confirmed.
    approve_code, approved = api_status("/ultron/execute", "POST",
                                       {"tool": "file_controller",
                                        "args": {"action": "delete",
                                                 "path": r"C:\e2e\never"},
                                        "confirmed": True, "timeout_s": 15.0},
                                       timeout=60)
    record("confirmed delete is dispatched, not re-blocked",
           approve_code == 200 and gated.get("executed") is False,
           f"http={approve_code} ok={approved.get('ok')} "
           f"risk={approved.get('risk')}")

    # ---------------------------------------------------------------- 9
    print("\n9. DISCONNECT: pending commands fail loudly, state goes offline")
    ws2.close()
    time.sleep(2.0)
    gone = api("/ultron/execution")
    record("agent unregistered", gone.get("pc_control_available") is False,
           f"agents={[a.get('label') for a in gone.get('windows_agents', [])]}")
    off_code, off = api_status("/ultron/execute", "POST",
                               {"tool": "open_app",
                                "args": {"app_name": "notepad"},
                                "timeout_s": 10.0}, timeout=30)
    record("dispatch after disconnect fails",
           off_code == 503 and off.get("executed") is False,
           f"http={off_code} " + str(off.get("error", ""))[:70])

    # ---------------------------------------------------------------- cleanup
    subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"],
                   capture_output=True, text=True, timeout=30)
    try:
        api(f"/ultron/devices/{claim.get('device_id')}/revoke", "POST", {})
    except Exception:
        pass
    ws.close()

    passed = sum(1 for r in results if r[0] == PASS)
    failed = len(results) - passed
    print("\n" + "=" * 74)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())