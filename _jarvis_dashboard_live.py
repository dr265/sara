"""_jarvis_dashboard_live.py — run the phone Remote Dashboard on a scratch
port for manual / browser testing (plain HTTP, no firewall prompts).

Mints two one-time keys and writes them to _dash_live_info.txt, then appends
every command that reaches the server's command queue to _dash_live_cmds.txt —
so an automated session can prove the full phone → server path, including
client-side AES encryption.

    sara_desktop\\venv\\Scripts\\python.exe _jarvis_dashboard_live.py [port]
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DSK = ROOT / "sara_desktop"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else \
    int(os.environ.get("SARA_DASH_LIVE_PORT", "8013"))
os.environ["SARA_DESKTOP_DASH_PORT"] = str(PORT)
sys.path.insert(0, str(DSK))
os.chdir(DSK)

import uvicorn  # noqa: E402
from dashboard import server as S  # noqa: E402

S.DashboardServer._ssl_enabled = staticmethod(lambda: False)
S._ensure_network_access = lambda port: None

srv = S.DashboardServer()
key1 = srv.new_key()
key2 = srv.new_key()

info = {
    "port": PORT,
    "key1": key1,
    "key2": key2,
    "auto_login": f"http://127.0.0.1:{PORT}/auto-login?key={key1}",
    "login_url": f"http://127.0.0.1:{PORT}/login",
}
(ROOT / "_dash_live_info.txt").write_text(json.dumps(info, indent=2),
                                          encoding="utf-8")
(ROOT / "_dash_live_cmds.txt").write_text("", encoding="utf-8")


def poller() -> None:
    seen = 0
    while True:
        q = list(srv._command_queue._queue)
        if len(q) > seen:
            with (ROOT / "_dash_live_cmds.txt").open("a", encoding="utf-8") as f:
                for item in q[seen:]:
                    f.write(json.dumps({"text": item, "ts": time.time()}) + "\n")
            seen = len(q)
        time.sleep(0.25)


threading.Thread(target=poller, daemon=True).start()

print(f"[live] dashboard on http://127.0.0.1:{PORT}")
print(f"[live] AUTO_LOGIN {info['auto_login']}")
print(f"[live] keys: {key1} (auto-login) · {key2} (manual login)")
uvicorn.Server(uvicorn.Config(srv.app, host="127.0.0.1", port=PORT,
                              log_level="error")).run()
