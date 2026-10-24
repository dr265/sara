# core/hub_agents_client.py
"""Shared HTTP client for the SARA Hub desktop actions.

Each Hub agent has a voice-callable desktop action built on this module (the
same pattern as actions/music_studio.py): the action talks to the real
backend service over the exact routes the Hub web UI uses, authenticated
with the same X-API-Key the desktop already reads from backend/.env.

Nothing here is mocked. When a service is unreachable or rejects a request,
the helpers return honest, speakable messages - the actions never fake
success and never invent completion.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

DEFAULT_TIMEOUT = 15

try:  # one canonical source for base URL + shared API key
    from core.agent_registry import BackendConnection
    _conn = BackendConnection()
    BASE_URL = _conn.base_url
    API_KEY = _conn.api_key
except Exception:  # pragma: no cover - env fallback mirrors actions/music_studio.py
    import os
    BASE_URL = os.environ.get("SARA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    API_KEY = os.environ.get("SARA_API_KEY", "").strip()

BACKEND_HINT = ("Make sure the SARA backend is running "
                "(jarvis_standalone.bat), then try again.")


def env_value(key: str) -> str:
    """os.environ first, then backend/.env and .env (same files main.py loads)."""
    import os
    from pathlib import Path
    val = (os.environ.get(key) or "").strip()
    if val:
        return val
    try:
        root = Path(__file__).resolve().parents[2]  # sara_desktop/core -> repo root
        for env_file in (root / "backend" / ".env", root / ".env"):
            if not env_file.exists():
                continue
            for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def url(path: str) -> str:
    return f"{BASE_URL.rstrip('/')}{path}"


def headers(content_type: bool = True) -> Dict[str, str]:
    h: Dict[str, str] = {}
    if content_type:
        h["Content-Type"] = "application/json"
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def get(path: str, timeout: float = DEFAULT_TIMEOUT, **kwargs) -> requests.Response:
    return requests.get(url(path), headers=headers(), timeout=timeout, **kwargs)


def post(path: str, payload: Any = None, timeout: float = 30, **kwargs) -> requests.Response:
    body = payload if payload is not None else {}
    return requests.post(url(path), json=body, headers=headers(), timeout=timeout, **kwargs)


def post_form(path: str, data: Dict[str, Any], timeout: float = 60) -> requests.Response:
    # No JSON content-type here - requests sets the form encoding itself.
    return requests.post(url(path), data=data,
                         headers=headers(content_type=False), timeout=timeout)


def detail_of(resp: requests.Response, limit: int = 300) -> str:
    """Best-effort human-readable reason from a backend response."""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "").strip()[:limit]
    if isinstance(data, dict):
        for key in ("detail", "error", "message", "reason"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:limit]
        return json.dumps(data, ensure_ascii=False, default=str)[:limit]
    return str(data)[:limit]


def http_failure(name: str, resp: requests.Response) -> str:
    """Honest one-liner for a non-2xx backend response."""
    detail = detail_of(resp)
    suffix = f": {detail}" if detail else "."
    if resp.status_code == 401:
        return (f"{name} rejected the request (HTTP 401 - missing or invalid "
                "API key). Check SARA_API_KEY in backend/.env.")
    if resp.status_code == 503:
        return f"{name} is not fully configured (HTTP 503{suffix if detail else ''})"
    return f"{name} rejected the request (HTTP {resp.status_code}{suffix})"


def unreachable(name: str, exc: Exception) -> str:
    return f"{name} is unreachable ({exc.__class__.__name__}). {BACKEND_HINT}"


def brief(value: Any, limit: int = 500) -> str:
    """One speakable line from any JSON-able value."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def health(path: str, timeout: float = 8) -> Optional[Dict[str, Any]]:
    """GET a health payload; None when down or unreadable."""
    try:
        resp = get(path, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else {"raw": data}
    except Exception:
        return None
    return None


def poll(fetch: Callable[[], Tuple[bool, Dict[str, Any], str]],
         is_terminal: Callable[[Dict[str, Any]], bool],
         timeout_s: float, interval_s: float = 3.0) -> Tuple[Dict[str, Any], bool, str]:
    """Poll a status endpoint until the snapshot is terminal.

    fetch() -> (ok, snapshot, error_text).
    Returns (last_snapshot, finished, note): note is "" when finished, else
    the honest reason polling stopped ("still running" or the real error).
    """
    deadline = time.time() + max(5.0, float(timeout_s))
    last: Dict[str, Any] = {}
    while True:
        ok, snap, err = fetch()
        if not ok:
            return last, False, err
        last = snap
        if is_terminal(snap):
            return snap, True, ""
        if time.time() >= deadline:
            return last, False, "still running"
        time.sleep(interval_s)
