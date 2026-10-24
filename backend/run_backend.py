"""Single entry point for the unified SARA backend.

Serves EVERYTHING on one port (default 8000):
  - SARA AI RAG/voice/users/documents API   (was port 5000)
  - legacy chat + orchestrate + WhatsApp    (was server.py on port 8000)
  - Telegram booking agent under /agent/*   (mounted WSGI app)
  - Desktop agent API + WebSocket feed      (was port 8756)

Run from the backend directory:
    python run_backend.py                # http://0.0.0.0:8000
    python run_backend.py --port 9000    # custom port
or
    uvicorn run_backend:app --port 8000
"""
import argparse
import logging
import os
import sys

# Make backend/ importable regardless of the launch directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app.main import app  # noqa: E402


class _RoutineProbeFilter(logging.Filter):
    """Keep routine status polling out of the access log.

    The console was drowning in 2xx lines: every Hub page poll triggers the
    fleet health sweep (all hub ``/health`` routes at once), browsers send CORS
    preflights, and the status boards re-read ``/agents``-style endpoints on a
    timer. Those lines carry no information and bury real failures. This filter
    silences ONLY successful reads of status paths; any response >= 400 and
    every request to every other route still logs exactly as before.
    """

    _QUIET_SUFFIXES = ("/health", "/healthz", "/status", "/agents",
                       "/agents/history")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client, method, path, _version, status = record.args
        except Exception:  # unexpected record shape - never hide anything
            return True
        try:
            status_code = int(status)
        except Exception:
            return True
        if status_code >= 400:  # real problems stay visible
            # EXCEPTION, deliberately narrow: local tooling that scans for
            # OpenAI-compatible model servers (opencode-cli does /health then
            # /v1/models on this port every minute or so) keeps hitting
            # paths under /v1/. SARA exposes no /v1/* API by design, so the
            # 404 recurs forever and calls for no operator action. Everything
            # else >= 400 still logs at full volume.
            if (status_code == 404 and str(method).upper() == "GET"
                    and str(path).startswith("/v1/")):
                return False
            return True
        if str(method).upper() == "OPTIONS":  # CORS preflight chatter
            return False
        if str(method).upper() != "GET":
            return True
        clean = str(path).split("?", 1)[0].rstrip("/")
        return not clean.endswith(self._QUIET_SUFFIXES)


# Attached here (not under __main__) so it also covers the documented
# `uvicorn run_backend:app` launch path, not just `python run_backend.py`.
logging.getLogger("uvicorn.access").addFilter(_RoutineProbeFilter())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified SARA backend")
    # Deliberately NOT falling back to the ambient `PORT` variable.
    #
    # PaaS platforms (Render, Heroku) inject `PORT`, and honouring it is the
    # obvious fix for "the service deployed but every request was refused".
    # It was tried here and reverted: plenty of ordinary developer tooling
    # exports its own `PORT`, and silently binding that instead of 8000 breaks
    # local development in a way that looks like a bug in the app.
    #
    # Render passes the port explicitly instead — `python run_backend.py
    # --port $PORT` in render.yaml — which is explicit, visible in the service
    # config, and cannot be hijacked by an unrelated environment variable.
    parser.add_argument("--port", type=int, default=int(os.environ.get("SARA_PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    import uvicorn

    # Single worker on purpose: the background pollers started in the lifespan
    # (Telegram getUpdates, WhatsApp inbox, social scheduling, social pilot) are
    # singletons. Two workers would each poll Telegram and the same update would
    # be processed twice. Scale by adding a worker service, not by adding
    # uvicorn workers here.
    uvicorn.run(app, host=args.host, port=args.port)