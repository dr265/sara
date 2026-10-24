"""
core/backend_url.py — one canonical resolver for the platform URLs Jarvis uses.

Why this exists
---------------
The launchers (jarvis_standalone.bat / start_all.bat) used to boot the backend
on port 8001 while the desktop code, the frontend (.env.local) and the README
all assume 8000. The backend was healthy the whole time, but every probe from
Jarvis and the website hit the empty port 8000 — which surfaced as
"backend down" / "backend offline at http://127.0.0.1:8000" / agents showing
unavailable. Nothing was wrong except the two sides disagreeing about a port.

Resolution order (highest precedence first):

  1. ``SARA_BACKEND_URL`` / ``SARA_FRONTEND_URL`` environment variables
     (the launchers export these, so an override can never drift again)
  2. ``SARA_PORT`` / ``FRONTEND_PORT`` environment variables
  3. ``SARA_PORT`` / ``FRONTEND_PORT`` in backend/.env or the root .env
  4. the documented defaults — backend 8000, frontend 3000

Pure stdlib, no imports from the rest of the app, safe to import anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path

#: sara_desktop/  ->  C:\...\sara-ai-platform
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent

DEFAULT_BACKEND_PORT = "8000"
DEFAULT_FRONTEND_PORT = "3000"


def read_env_files() -> dict:
    """Read backend/.env then the root .env (first value wins per key)."""
    merged: dict = {}
    for env_file in (PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"):
        if not env_file.exists():
            continue
        try:
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in merged:
                    merged[key] = value
        except Exception:
            pass
    return merged


def backend_port() -> str:
    """The port the unified SARA backend is (or will be) listening on."""
    env = read_env_files()
    return (os.environ.get("SARA_PORT")
            or env.get("SARA_PORT")
            or DEFAULT_BACKEND_PORT).strip()


def frontend_port() -> str:
    """The port the SARA website (Next.js) is (or will be) listening on."""
    env = read_env_files()
    return (os.environ.get("FRONTEND_PORT")
            or env.get("FRONTEND_PORT")
            or DEFAULT_FRONTEND_PORT).strip()


def resolve_backend_url() -> str:
    """Full base URL of the backend, no trailing slash."""
    explicit = (os.environ.get("SARA_BACKEND_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{backend_port()}"


def resolve_frontend_url() -> str:
    """Full base URL of the website, no trailing slash."""
    explicit = (os.environ.get("SARA_FRONTEND_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{frontend_port()}"


def resolve_lan_ip() -> str:
    """Best-effort LAN IPv4 of this machine, or "" when there is none.

    Phone pairing needs a URL the phone can actually dial: 127.0.0.1 from the
    phone is the phone itself. The UDP "connect" below sends nothing — the
    kernel only picks the interface it would route through, which is exactly
    the address another device on the same network must use.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(
                socket.gethostname(), None):
            if family == socket.AF_INET and sockaddr:
                ip = str(sockaddr[0])
                if not ip.startswith("127."):
                    return ip
    except Exception:
        pass
    return ""
