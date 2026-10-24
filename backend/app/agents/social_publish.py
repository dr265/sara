"""
Minimal OAuth + account helpers, ported from the organized backend paste.

Appears as `app.agents.social_publish` so `social_media.py` can keep using
`sp.oauth_authorize_url`, `sp.exchange_oauth_code`, `sp.add_account`, and
`sp.list_accounts` without importing the full pasted agency file.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from typing import Any

from app.database.database import SessionLocal
from app.database.models import SocialAccount

META_VERSIONS = {
    "facebook": os.getenv("SOCIAL_FACEBOOK_API_VERSION", "v21.0"),
    "instagram": os.getenv("SOCIAL_INSTAGRAM_API_VERSION", "v21.0"),
    "threads": os.getenv("SOCIAL_THREADS_API_VERSION", "v1.0"),
}

REDIRECT_BASE = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Platforms supported by the social media agency (matches organized backend)
PLATFORMS = ("instagram", "facebook", "linkedin", "tiktok", "threads", "pinterest")

PLATFORM_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "tiktok": "TikTok",
    "threads": "Threads",
    "pinterest": "Pinterest",
}

# ---------------------------------------------------------------------------
# OAuth flow helpers
# ---------------------------------------------------------------------------


def oauth_authorize_url(platform: str, workspace_id: str) -> str:
    """Build the provider authorize URL (or a simulated immediate redirect).

    The callback is always PUBLIC_BASE_URL/social/oauth/callback so the flow
    works through the unified server whether OAuth is real or simulated.
    """
    if platform not in PLATFORMS:
        raise ValueError(
            "unknown platform '%s' (choose from %s)" % (platform, ", ".join(PLATFORMS))
        )
    state = urllib.parse.urlencode({"workspace_id": workspace_id, "platform": platform})
    redirect_uri = f"{REDIRECT_BASE}/social/oauth/callback"
    if SIMULATE_OAUTH or not _client_id(platform):
        # No real credentials → go straight to the callback with a mock code.
        # state must be URL-encoded or its '&' splits into extra query params.
        return (
            f"{REDIRECT_BASE}/social/oauth/callback"
            f"?code=mock_auth_code_123&state={urllib.parse.quote(state)}"
        )
    auth_urls = {
        "instagram": "https://api.instagram.com/oauth/authorize",
        "facebook": "https://www.facebook.com/v21.0/dialog/oauth",
        "linkedin": "https://www.linkedin.com/oauth/v2/authorization",
        "tiktok": "https://www.tiktok.com/v2/auth/authorize/",
        "threads": "https://threads.net/oauth/authorize",
        "pinterest": "https://www.pinterest.com/oauth/",
    }
    params = {
        "client_id": _client_id(platform),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": "public_profile",
    }
    if platform == "tiktok":
        params["client_key"] = params.pop("client_id")
    return f"{auth_urls[platform]}?{urllib.parse.urlencode(params)}"


def exchange_oauth_code(platform: str, code: str) -> dict:
    """Exchange an authorization code for a token bundle.

    Simulated unless the platform has a client secret configured; the returned
    dict is always JSON-serializable so it can be stored encrypted.
    """
    if SIMULATE_OAUTH or not _client_secret(platform):
        return {
            "access_token": f"live_token_{platform}_{code[:12]}",
            "expires_in": 3600,
            "simulated": True,
        }
    import httpx

    token_urls = {
        "instagram": "https://api.instagram.com/oauth/access_token",
        "facebook": "https://graph.facebook.com/v21.0/oauth/access_token",
        "linkedin": "https://www.linkedin.com/oauth/v2/accessToken",
        "tiktok": "https://www.tiktok.com/v2/oauth/token/",
    }
    payload = {
        "client_id": _client_id(platform),
        "client_secret": _client_secret(platform),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{REDIRECT_BASE}/social/oauth/callback",
    }
    resp = httpx.post(token_urls[platform], data=payload, timeout=25.0)
    if resp.status_code >= 400:
        raise RuntimeError(
            "OAuth token exchange failed (%d): %s" % (resp.status_code, resp.text[:200])
        )
    data = resp.json()
    return {
        "access_token": data.get("access_token", ""),
        "expires_in": data.get("expires_in", 3600),
    }


# ---------------------------------------------------------------------------
# Config + small helpers
# ---------------------------------------------------------------------------


def _client_id(platform: str) -> str:
    return os.getenv(f"SOCIAL_{platform.upper()}_CLIENT_ID", "").strip()


def _client_secret(platform: str) -> str:
    return os.getenv(f"SOCIAL_{platform.upper()}_CLIENT_SECRET", "").strip()


def _make_fernet():
    from cryptography.fernet import Fernet

    key = (ENCRYPTION_KEY * 4)[:32].encode("utf-8")
    return Fernet(key)


FERNET = None


def _fernet():
    global FERNET
    if FERNET is None:
        FERNET = _make_fernet()
    return FERNET


def _encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_text(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode("utf-8")).decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Workspaces / accounts / video DB
# ---------------------------------------------------------------------------


def list_workspaces() -> list[dict]:
    db = SessionLocal()
    try:
        from app.database.models import SocialWorkspace

        return [
            {
                "id": w.id,
                "name": w.name,
                "video_db_set": bool(_decrypt_text(w.encrypted_video_db_url)),
                "created_at": w.created_at.isoformat() if w.created_at else None,
            }
            for w in db.query(SocialWorkspace).order_by(SocialWorkspace.created_at).all()
        ]
    finally:
        db.close()


# shim: compatibility alias for calls in social_media.py
def env_int(key: str, default: int, low: int, high: int) -> int:
    import os as _os
    try:
        v = int(_os.getenv(key, str(default)))
    except Exception:
        v = default
    if not (low <= v <= high):
        v = default
    return v


def _env_int_regression() -> None:
    """Smoke-test the compatibility shim so env mis-configuration is caught early."""
    assert env_int("NONEXISTENT", 5, 1, 10) == 5
    assert env_int("NONEXISTENT", 5, 1, 4) == 5
    _os.environ["TMPKEY"] = "12"
    assert env_int("TMPKEY", 5, 1, 20) == 12


def list_accounts(workspace_id: str = "demo_workspace") -> list[dict]:
    db = SessionLocal()
    try:
        from app.database.models import SocialAccount

        rows = (
            db.query(SocialAccount)
            .filter(SocialAccount.workspace_id == workspace_id)
            .order_by(SocialAccount.created_at)
            .all()
        )
        return [
            {
                "id": a.id,
                "platform": a.platform,
                "platform_label": PLATFORM_LABELS.get(a.platform, a.platform),
                "account_name": a.account_name,
                "status": a.status or "connected",
                "last_error": a.last_error or "",
                "token_set": bool(a.encrypted_token),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]
    finally:
        db.close()


def add_account(workspace_id: str, platform: str, token_data: dict) -> dict:
    """Persist a freshly-exchanged token as a connected account (lock-safe)."""
    return _retry_locked(lambda: _add_account_impl(workspace_id, platform, token_data))


def _add_account_impl(workspace_id: str, platform: str, token_data: dict) -> dict:
    db = SessionLocal()
    try:
        from app.database.models import SocialWorkspace

        if platform not in PLATFORMS:
            raise ValueError("unknown platform '%s'" % platform)
        ws = db.query(SocialWorkspace).filter(SocialWorkspace.id == workspace_id).first()
        if ws is None:
            raise KeyError("workspace %s not found" % workspace_id)
        acc = SocialAccount(
            workspace_id=workspace_id,
            platform=platform,
            account_name=f"@{platform}_user",
            encrypted_token=_encrypt_text(json.dumps(token_data)),
            status="connected",
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)
        return {
            "id": acc.id,
            "platform": acc.platform,
            "account_name": acc.account_name,
            "status": acc.status,
        }
    finally:
        db.close()


def remove_account(account_id: str) -> bool:
    return _retry_locked(lambda: _remove_account_impl(account_id))


def _remove_account_impl(account_id: str) -> bool:
    db = SessionLocal()
    try:
        acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
        if acc is None:
            return False
        db.delete(acc)
        db.commit()
        return True
    finally:
        db.close()


def set_video_db(workspace_id: str, custom_db_url: str) -> bool:
    return _retry_locked(lambda: _set_video_db_impl(workspace_id, custom_db_url))


def _set_video_db_impl(workspace_id: str, custom_db_url: str) -> bool:
    db = SessionLocal()
    try:
        from app.database.models import SocialWorkspace

        ws = db.query(SocialWorkspace).filter(SocialWorkspace.id == workspace_id).first()
        if ws is None:
            raise KeyError("workspace %s not found" % workspace_id)
        ws.encrypted_video_db_url = _encrypt_text(custom_db_url) if custom_db_url else None
        db.commit()
        return True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Retry-on-locked helper (SQLite WAL writer collisions)
# ---------------------------------------------------------------------------


def _retry_locked(fn, tries: int = 5):
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            if "locked" not in str(exc).lower() or attempt == tries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError("unreachable")  # pragma: no cover
