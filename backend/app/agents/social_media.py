"""
Social Media Agency — SARA AI Platform
═══════════════════════════════════════════════════════════════════════════════
Fully autonomous, dynamic multi-platform social media management engine.
Single self-contained module: adapters, services, schemas, orchestration,
FastAPI router — everything in ONE file.

Integrates with the existing Sara AI backend (FastAPI on :8000).
All state lives in the shared SQLite/PostgreSQL DB (sara.db).
Credentials are Fernet-encrypted at rest.
LLM calls go through the existing full_ai_v3 brain.

Nothing is hardcoded for the user — configuration-driven, dynamic discovery.
"""
from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import uuid as uuid_mod
from base64 import b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from pydantic import BaseModel
from sqlalchemy.exc import OperationalError

log = logging.getLogger("sara.social")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Resolution order: SARA_PUBLIC_BASE_URL → PUBLIC_BASE_URL → localhost.
# PUBLIC_BASE_URL is the ngrok/public tunnel URL set in backend/.env and is what
# platform OAuth apps (Google, Meta, …) have registered as their redirect origin.
PUBLIC_BASE_URL = (
    os.getenv("SARA_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or "http://localhost:8000"
).rstrip("/")
DEMO_WORKSPACE_ID = "demo_user"

PLATFORMS = ("instagram", "facebook", "linkedin", "tiktok", "threads", "pinterest", "x", "youtube")

PLATFORM_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "tiktok": "TikTok",
    "threads": "Threads",
    "pinterest": "Pinterest",
    "x": "X (Twitter)",
    "youtube": "YouTube",
}

CONTENT_KINDS = ("text", "image", "video")
JOB_STATUSES = ("pending", "processing", "success", "failed", "dry_run", "uncertain")
RECURRENCES = ("none", "daily", "weekdays", "weekly", "biweekly", "monthly")

POLL_INTERVAL_S = float(os.getenv("SOCIAL_POLL_SECONDS", "10"))
PILOT_POLL_S = float(os.getenv("SOCIAL_PILOT_POLL_SECONDS", "60"))

DRY_RUN_DEFAULT = os.getenv("SOCIAL_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "on"}
SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "agent_social_settings.json"
PILOT_FILE = Path(__file__).resolve().parent.parent.parent / "agent_social_pilot.json"

# Real OAuth by default. A "connected" account that cannot publish is worse
# than an honest error. Set SOCIAL_{PLATFORM}_CLIENT_ID + _CLIENT_SECRET in
# backend/.env to enable a platform's OAuth, or connect an account manually
# with its real access token via POST /social/accounts (verified live).
SIMULATE_OAUTH = os.getenv("SOCIAL_SIMULATE_OAUTH", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

META_VERSIONS = {
    "facebook": os.getenv("SOCIAL_FACEBOOK_API_VERSION", "v21.0"),
    "instagram": os.getenv("SOCIAL_INSTAGRAM_API_VERSION", "v21.0"),
    "threads": os.getenv("SOCIAL_THREADS_API_VERSION", "v1.0"),
}


def env_int(key: str, default: int, low: int, high: int) -> int:
    try:
        v = int(os.getenv(key, str(default)))
    except Exception:
        v = default
    if not (low <= v <= high):
        v = default
    return v


MAX_ATTEMPTS = env_int("SOCIAL_MAX_ATTEMPTS", 4, 1, 20)
LEASE_SECONDS = env_int("SOCIAL_LEASE_SECONDS", 300, 30, 3600)

T = TypeVar("T")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — ERROR CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class PermanentError(Exception):
    """Non-retryable platform error (invalid content, permissions, banned)."""


class RetryableError(Exception):
    """Transient platform error (rate-limit, timeout, 5xx)."""
    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class UncertainError(Exception):
    """Write may or may not have landed — auto-retry risks duplication."""


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ENCRYPTION
# ═══════════════════════════════════════════════════════════════════════════════

def _encrypt_text(value: str) -> str:
    """Encrypt using the shared Fernet key from WhatsApp channels manager."""
    try:
        from app.services.whatsapp_channels import encrypt_json
        return encrypt_json({"v": value or ""})
    except Exception:
        # Fallback: use SARA_MASTER_KEY directly
        try:
            from cryptography.fernet import Fernet
            key = os.getenv("SARA_MASTER_KEY", "default_key_change_me")
            # Fernet requires url-safe base64 32-byte key
            if len(key) < 32:
                key = (key * 4)[:32]
            f = Fernet(urlsafe_b64encode(key.encode("utf-8")[:32]))
            return f.encrypt(value.encode("utf-8")).decode("utf-8")
        except Exception:
            return value  # last resort — unencrypted


def _decrypt_text(token: Optional[str]) -> str:
    """Decrypt a Fernet-encrypted token back to plaintext."""
    if not token:
        return ""
    try:
        from app.services.whatsapp_channels import decrypt_json
        return str(decrypt_json(token).get("v", ""))
    except Exception:
        try:
            from cryptography.fernet import Fernet
            key = os.getenv("SARA_MASTER_KEY", "default_key_change_me")
            if len(key) < 32:
                key = (key * 4)[:32]
            f = Fernet(urlsafe_b64encode(key.encode("utf-8")[:32]))
            return f.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return token


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_qs_safe(qs: str) -> dict:
    """URL-decode a query string safely; returns dict of lists."""
    try:
        return urllib.parse.parse_qs(qs or "")
    except Exception:
        return {}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def valid_timezone(name: str) -> bool:
    """Check whether *name* is a recognised IANA timezone or UTC offset."""
    if not name:
        return False
    if name.upper() == "UTC":
        return True
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        pass
    # Accept UTC±HH:MM offsets
    return bool(re.match(r"^UTC[+-]\d{1,2}(:\d{2})?$", name, re.I))


def following_future(
    after: datetime,
    recurrence: str,
    timezone_name: str,
    anchor: datetime,
) -> Optional[datetime]:
    """Compute the next occurrence of a recurring schedule after *after*.
    Skips missed occurrences (no flood after downtime)."""
    if recurrence == "none" or not recurrence:
        return None

    now = after or _utcnow()
    day = timedelta(days=1)

    if recurrence == "daily":
        candidate = now + day
    elif recurrence == "weekdays":
        candidate = now + day
        while candidate.weekday() >= 5:
            candidate += day
    elif recurrence == "weekly":
        candidate = now + timedelta(days=7)
    elif recurrence == "biweekly":
        candidate = now + timedelta(days=14)
    elif recurrence == "monthly":
        month = now.month % 12 + 1
        year = now.year + (1 if now.month == 12 else 0)
        candidate = now.replace(year=year, month=month, day=min(now.day, 28))
    else:
        return None

    # Preserve the anchor's time-of-day
    if anchor:
        candidate = candidate.replace(
            hour=anchor.hour, minute=anchor.minute, second=anchor.second
        )

    return candidate


def campaign_id_suffix() -> str:
    return uuid_mod.uuid4().hex[:6]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — PLATFORM ADAPTER INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

# Required credentials for LIVE publishing (beyond just access_token)
REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "instagram": ("access_token", "ig_user_id"),
    "facebook": ("access_token", "page_id"),
    "linkedin": ("access_token",),
    "tiktok": ("access_token",),
    "threads": ("access_token", "threads_user_id"),
    "pinterest": ("access_token",),
    "x": ("access_token",),
    "youtube": ("access_token",),
}

# Character limits per platform (for content validation)
PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "instagram": {"caption": 2200, "hashtags": 30},
    "facebook": {"caption": 63206},
    "linkedin": {"caption": 3000},
    "tiktok": {"caption": 2200},
    "threads": {"caption": 500},
    "pinterest": {"caption": 500, "title": 100},
    "x": {"caption": 280},
    "youtube": {"title": 100, "description": 5000},
}


def validate_credentials(platform: str, credentials: dict) -> None:
    """Raise ValueError if required credential fields are missing."""
    required = REQUIRED_CREDENTIALS.get(platform)
    if not required:
        return
    missing = [f for f in required if not str(credentials.get(f, "")).strip()]
    if missing:
        raise ValueError(f"Missing required fields for {platform}: {', '.join(missing)}")


def validate_content(config: Any, platform: str, content: dict) -> None:
    """Raise ValueError if content exceeds platform limits."""
    limits = PLATFORM_LIMITS.get(platform, {})
    caption = content.get("caption", "")
    max_caption = limits.get("caption", 99999)
    if len(caption) > max_caption:
        raise ValueError(
            f"{platform} caption exceeds {max_caption} characters (got {len(caption)})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — OAUTH MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

def _client_id(platform: str) -> str:
    return os.getenv(f"SOCIAL_{platform.upper()}_CLIENT_ID", "").strip()


def _client_secret(platform: str) -> str:
    return os.getenv(f"SOCIAL_{platform.upper()}_CLIENT_SECRET", "").strip()


# ── BYO OAuth apps (user supplies their own keys in the UI) ─────────────────

def set_platform_app(workspace_id: str, platform: str, client_id: str, client_secret: str) -> dict:
    """Store (or replace) the user's own OAuth app for a platform — encrypted."""
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformApp
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform '{platform}'")
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("both client_id and client_secret are required")

    def _impl() -> dict:
        db = SessionLocal()
        try:
            row = (
                db.query(SocialPlatformApp)
                .filter(SocialPlatformApp.workspace_id == workspace_id,
                        SocialPlatformApp.platform == platform)
                .first()
            )
            if row is None:
                row = SocialPlatformApp(workspace_id=workspace_id, platform=platform)
                db.add(row)
            row.encrypted_client_id = _encrypt_text(client_id)
            row.encrypted_client_secret = _encrypt_text(client_secret)
            db.commit()
            return {"platform": platform, "workspace_id": workspace_id, "stored": True}
        finally:
            db.close()

    return _retry_locked(_impl)


def get_platform_app(workspace_id: str, platform: str) -> tuple[str, str]:
    """Return (client_id, client_secret) from the user's stored app, or ('', '')."""
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformApp
    db = SessionLocal()
    try:
        row = (
            db.query(SocialPlatformApp)
            .filter(SocialPlatformApp.workspace_id == workspace_id,
                    SocialPlatformApp.platform == platform)
            .first()
        )
        if row is None:
            return "", ""
        return _decrypt_text(row.encrypted_client_id), _decrypt_text(row.encrypted_client_secret)
    finally:
        db.close()


def delete_platform_app(workspace_id: str, platform: str) -> bool:
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformApp
    db = SessionLocal()
    try:
        n = (
            db.query(SocialPlatformApp)
            .filter(SocialPlatformApp.workspace_id == workspace_id,
                    SocialPlatformApp.platform == platform)
            .delete()
        )
        db.commit()
        return n > 0
    finally:
        db.close()


def _resolve_app(platform: str, workspace_id: str = DEMO_WORKSPACE_ID) -> tuple[str, str]:
    """Client credentials resolution order: user's stored app → .env."""
    cid, sec = get_platform_app(workspace_id, platform)
    if cid and sec:
        return cid, sec
    return _client_id(platform), _client_secret(platform)


def platform_connection_modes(platform: str, workspace_id: str = DEMO_WORKSPACE_ID) -> dict:
    """What the UI needs to offer for this platform: oauth and/or manual keys."""
    required = {
        "instagram": ["access_token", "ig_user_id"],
        "facebook": ["access_token", "page_id"],
        "threads": ["access_token", "threads_user_id"],
        "linkedin": ["access_token", "author_urn"],
        "x": ["access_token"],
        "pinterest": ["access_token", "board_id"],
        "tiktok": ["access_token"],
        "youtube": ["access_token"],
    }.get(platform, ["access_token"])
    byo = get_platform_app(workspace_id, platform)
    return {
        "oauth_ready": bool(_client_id(platform) and _client_secret(platform)),
        "byo_oauth_ready": bool(byo[0] and byo[1]),
        "manual_fields": required,
    }


def oauth_ready_platforms(workspace_id: str = DEMO_WORKSPACE_ID) -> list[str]:
    ready = []
    for p in PLATFORMS:
        cid, sec = _resolve_app(p, workspace_id)
        if cid and sec:
            ready.append(p)
    return ready


def oauth_authorize_url(platform: str, workspace_id: str) -> str:
    """Build the provider authorize URL (or a simulated immediate redirect)."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform '{platform}' (choose from {', '.join(PLATFORMS)})")
    state = urllib.parse.urlencode({"workspace_id": workspace_id, "platform": platform})
    redirect_uri = f"{PUBLIC_BASE_URL}/social/oauth/callback"
    cid, csec = _resolve_app(platform, workspace_id)
    if not cid:
        raise ValueError(
            f"{platform} OAuth is not set up yet. Either add SOCIAL_{platform.upper()}_CLIENT_ID "
            f"+ SOCIAL_{platform.upper()}_CLIENT_SECRET to backend/.env, or save your own app keys "
            f"in the Connect dialog (they are stored encrypted). Register this redirect URI in "
            f"your {platform} developer app: {redirect_uri}"
        )
    auth_urls = {
        "instagram": "https://api.instagram.com/oauth/authorize",
        "facebook": f"https://www.facebook.com/{META_VERSIONS['facebook']}/dialog/oauth",
        "linkedin": "https://www.linkedin.com/oauth/v2/authorization",
        "tiktok": "https://www.tiktok.com/v2/auth/authorize/",
        "threads": "https://threads.net/oauth/authorize",
        "pinterest": "https://www.pinterest.com/oauth/",
        "x": "https://x.com/i/oauth2/authorize",
        "youtube": "https://accounts.google.com/o/oauth2/v2/auth",
    }
    scopes = {
        "instagram": "instagram_basic,instagram_content_publish",
        "facebook": "pages_manage_posts,pages_read_engagement",
        "linkedin": "w_member_social",
        "tiktok": "user.info.basic,video.publish",
        "threads": "threads_basic,threads_content_publish",
        "pinterest": "boards:read,pins:read,pins:write",
        "x": "tweet.read tweet.write users.read offline.access",
        "youtube": "https://www.googleapis.com/auth/youtube.upload",
    }
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": scopes.get(platform, "public_profile"),
    }
    if platform == "tiktok":
        params["client_key"] = params.pop("client_id")
    if platform == "x":
        params["code_challenge"] = "challenge"
        params["code_challenge_method"] = "plain"
    if platform == "youtube":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    return f"{auth_urls[platform]}?{urllib.parse.urlencode(params)}"


def _fetch_identity(platform: str, token_data: dict) -> tuple[str, dict]:
    """Best-effort live identity lookup with the freshly obtained token.

    Returns (account_name, extra_credentials). Raises RuntimeError when the
    token is unusable — a connection that cannot read its own identity will
    not be able to publish either.
    """
    import httpx

    token = token_data.get("access_token", "")
    if not token:
        raise RuntimeError("provider returned no access_token")

    try:
        if platform in ("facebook", "instagram"):
            r = httpx.get(
                f"https://graph.facebook.com/{META_VERSIONS['facebook']}/me",
                params={"fields": "id,name", "access_token": token},
                timeout=20,
            )
        elif platform == "threads":
            r = httpx.get(
                f"https://graph.threads.net/{META_VERSIONS['threads']}/me",
                params={"fields": "id,username", "access_token": token},
                timeout=20,
            )
        elif platform == "linkedin":
            r = httpx.get(
                "https://api.linkedin.com/v2/userinfo",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        elif platform == "x":
            r = httpx.get(
                "https://api.x.com/2/users/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        elif platform == "pinterest":
            r = httpx.get(
                "https://api.pinterest.com/v5/user_account",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        elif platform == "tiktok":
            r = httpx.post(
                "https://open.tiktokapis.com/v2/user/info/",
                json={"fields": ["open_id", "display_name"]},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        elif platform == "youtube":
            r = httpx.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        else:
            return f"@{platform}_user", {}
    except Exception as exc:
        raise RuntimeError(f"could not verify the {platform} token live: {exc}") from exc

    if r.status_code >= 400:
        # YouTube special case: the app only requests `youtube.upload` (a write
        # scope), but this probe hits the read-scoped channels.list endpoint,
        # which Google answers with 403 "insufficient permissions". A token
        # that cannot read its own channel can still UPLOAD fine, so accept it
        # and fall back to a generic account name.
        if platform == "youtube":
            return "@youtube_channel", {"channel_id": ""}
        raise RuntimeError(
            f"{platform} rejected the token ({r.status_code}): {r.text[:160]}"
        )
    try:
        data = r.json()
    except Exception:
        data = {}

    extra: dict = {}
    name = f"@{platform}_user"

    if platform == "facebook":
        name = "@" + (data.get("name") or data.get("id", "facebook")).replace(" ", "_").lower()
        extra["provider_account_id"] = data.get("id", "")
    elif platform == "instagram":
        # Instagram Graph requires the IG Professional ID. Derive it live via
        # the Facebook Page linked to the professional account.
        ig_id = _derive_ig_user_id(token)
        if not ig_id:
            raise RuntimeError(
                "token is valid but no Instagram Professional account is linked to it. "
                "Convert to a Business/Creator account and link a Facebook Page, then retry."
            )
        extra["ig_user_id"] = ig_id
        name = "@ig_" + ig_id[-6:]
        try:
            r2 = httpx.get(
                f"https://graph.facebook.com/{META_VERSIONS['instagram']}/{ig_id}",
                params={"fields": "username", "access_token": token},
                timeout=15,
            )
            if r2.status_code < 400:
                u = r2.json().get("username")
                if u:
                    name = "@" + u
        except Exception:
            pass
    elif platform == "threads":
        name = "@" + (data.get("username") or data.get("id", "threads"))
        extra["threads_user_id"] = data.get("id", "")
    elif platform == "linkedin":
        sub = data.get("sub") or data.get("id", "")
        name = "@" + (data.get("name") or sub or "linkedin").replace(" ", "_").lower()
        extra["author_urn"] = f"urn:li:person:{sub}"
    elif platform == "x":
        d = data.get("data", {}) or {}
        name = "@" + (d.get("username") or d.get("id", "x"))
        extra["provider_account_id"] = d.get("id", "")
    elif platform == "pinterest":
        name = "@" + (data.get("username") or data.get("id", "pinterest"))
    elif platform == "tiktok":
        d = (data.get("data", {}) or {}).get("user", {}) or {}
        name = "@" + (d.get("display_name") or d.get("open_id", "tiktok"))
        extra["provider_account_id"] = d.get("open_id", "")
    elif platform == "youtube":
        items = data.get("items") or []
        if items:
            name = "@" + (items[0].get("snippet", {}).get("customUrl") or items[0].get("id", "youtube"))
            extra["channel_id"] = items[0].get("id", "")

    return name, extra


def _derive_ig_user_id(access_token: str) -> str:
    """Find the IG Professional account ID behind a Facebook login token."""
    import httpx

    v = META_VERSIONS["facebook"]
    try:
        pages = httpx.get(
            f"https://graph.facebook.com/{v}/me/accounts",
            params={"fields": "id,name,instagram_business_account", "access_token": access_token},
            timeout=20,
        )
        if pages.status_code >= 400:
            return ""
        for page in (pages.json().get("data") or []):
            ig = (page.get("instagram_business_account") or {}).get("id", "")
            if ig:
                return ig
    except Exception:
        pass
    return ""


def exchange_oauth_code(platform: str, code: str, workspace_id: str = DEMO_WORKSPACE_ID) -> dict:
    """Exchange an authorization code for a token bundle (real provider call)."""
    cid, csec = _resolve_app(platform, workspace_id)
    if not csec:
        raise RuntimeError(
            f"{platform} OAuth is not set up: save your app keys in the Connect dialog "
            "or set SOCIAL_" + platform.upper() + "_CLIENT_SECRET in backend/.env"
        )
    token_urls = {
        "instagram": "https://api.instagram.com/oauth/access_token",
        "facebook": f"https://graph.facebook.com/{META_VERSIONS['facebook']}/oauth/access_token",
        "linkedin": "https://www.linkedin.com/oauth/v2/accessToken",
        "tiktok": "https://open.tiktokapis.com/v2/oauth/token/",
        "threads": "https://graph.threads.net/oauth/access_token",
        "pinterest": "https://api.pinterest.com/v5/oauth/token",
        "x": "https://api.x.com/2/oauth2/token",
        "youtube": "https://oauth2.googleapis.com/token",
    }
    redirect_uri = f"{PUBLIC_BASE_URL}/social/oauth/callback"
    payload = {
        "client_id": cid,
        "client_secret": csec,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if platform == "tiktok":
        payload["client_key"] = payload.pop("client_id")
    if platform == "threads":
        payload["client_key"] = payload.get("client_id", "")

    try:
        import httpx
        resp = httpx.post(token_urls.get(platform, ""), data=payload, timeout=25.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"OAuth token exchange failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        if not data.get("access_token"):
            raise RuntimeError(f"{platform} token response contained no access_token")
        result = {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "token_type": data.get("token_type", "bearer"),
            "scope": data.get("scope", ""),
            "issued_at": time.time(),
            "platform": platform,
            "provider": platform,
        }
        # Prove the token works before storing anything.
        name, extra = _fetch_identity(platform, result)
        result["account_name"] = name
        result.update(extra)
        return result
    except Exception as exc:
        raise RuntimeError(f"OAuth exchange failed for {platform}: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — PUBLISHING ADAPTERS
# ═══════════════════════════════════════════════════════════════════════════════

def _refresh_google_access_token(credentials: dict) -> str:
    """Refresh a Google access token when it has expired or is about to.

    Returns a working access_token. Raises PermanentError if no refresh_token
    is stored or Google refuses the refresh. Mutates `credentials` in place so
    callers keep the fresh token for the rest of the flow.
    """
    import time as _time
    import httpx

    issued_at = float(credentials.get("issued_at") or 0)
    expires_in = float(credentials.get("expires_in") or 3600)
    # Refresh when older than ~50 minutes (tokens last 60) or when Google
    # already told us the token is dead.
    fresh_enough = issued_at and (_time.time() - issued_at) < max(60.0, expires_in - 600.0)
    if credentials.get("_token_refreshed") or fresh_enough:
        return credentials["access_token"]

    refresh_token = credentials.get("refresh_token", "")
    if not refresh_token:
        raise PermanentError(
            "Google access token expired and no refresh_token was stored — "
            "re-connect the YouTube account (OAuth must be re-consented to grant offline access)"
        )
    cid, csec = _resolve_app("youtube", credentials.get("workspace_id") or DEMO_WORKSPACE_ID)
    if not csec:
        raise PermanentError("cannot refresh Google token: YouTube OAuth app credentials are missing")
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20.0,
    )
    if resp.status_code >= 400:
        raise PermanentError(f"Google token refresh failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    new_token = data.get("access_token", "")
    if not new_token:
        raise PermanentError("Google token refresh returned no access_token")
    credentials["access_token"] = new_token
    credentials["expires_in"] = float(data.get("expires_in") or 3600)
    credentials["issued_at"] = _time.time()
    credentials["_token_refreshed"] = True
    log.info("Refreshed Google access token for YouTube publishing")
    return new_token


def _upload_youtube_video(access_token: str, content: dict) -> dict:
    """Upload a video to YouTube via the Data API v3 resumable upload.

    `media_urls[0]` must be a publicly downloadable video file URL. Returns
    {remote_id, url}. Raises PermanentError/RetryableError/UncertainError.
    """
    import httpx

    media_urls = content.get("media_urls") or []
    if not media_urls:
        raise PermanentError("YouTube upload needs a video file URL")
    video_url = media_urls[0]

    title = (content.get("title") or content.get("caption") or "Untitled").strip()[:100]
    description = (content.get("caption") or title).strip()[:4900]

    # 1) Download the video bytes (up to ~200MB).
    try:
        dl = httpx.get(video_url, timeout=httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0), follow_redirects=True)
    except Exception as exc:
        raise RetryableError(f"could not download video for upload: {exc}")
    if dl.status_code >= 400:
        raise PermanentError(f"video source returned {dl.status_code} — cannot upload to YouTube")
    video_bytes = dl.content
    if not video_bytes:
        raise PermanentError("video source returned an empty file")
    content_type = dl.headers.get("content-type", "video/mp4")
    if "text/html" in content_type:
        raise PermanentError("video URL points to a web page, not a video file — the video provider returned no direct file")

    # 2) Initiate the resumable session.
    init = httpx.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(len(video_bytes)),
            "X-Upload-Content-Type": content_type,
        },
        json={
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": "22",  # People & Blogs
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        },
        timeout=30.0,
    )
    if init.status_code == 401:
        raise RetryableError("youtube upload unauthorized (token expired)")
    if init.status_code == 403:
        raise PermanentError(f"youtube upload forbidden — check API quota / youtube.upload scope: {init.text[:200]}")
    if init.status_code >= 400:
        raise PermanentError(f"youtube upload init failed ({init.status_code}): {init.text[:200]}")
    upload_url = init.headers.get("location")
    if not upload_url:
        raise PermanentError("youtube did not return a resumable upload URL")

    # 3) PUT the bytes.
    put = httpx.put(
        upload_url,
        content=video_bytes,
        headers={"Content-Type": content_type, "Content-Length": str(len(video_bytes))},
        timeout=httpx.Timeout(connect=15.0, read=600.0, write=600.0, pool=15.0),
    )
    if put.status_code >= 500:
        raise UncertainError(f"youtube upload interrupted ({put.status_code}) — outcome unknown")
    if put.status_code >= 400:
        raise PermanentError(f"youtube upload failed ({put.status_code}): {put.text[:200]}")
    try:
        data = put.json()
    except Exception:
        raise UncertainError("youtube upload ended without a JSON verdict — outcome unknown")
    remote_id = data.get("id", "")
    if not remote_id:
        raise UncertainError("youtube upload finished but returned no video id")

    # 4) Wait briefly for processing so the video page is usable right away.
    for _ in range(6):
        check = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "status", "id": remote_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        if check.status_code < 400:
            st = (((check.json().get("items") or [{}])[0]).get("status") or {})
            if st.get("uploadStatus") in ("processed", "uploaded"):
                break
            if st.get("uploadStatus") == "rejected":
                raise PermanentError(f"youtube rejected the upload: {str(st.get('rejectionReason') or st)[:200]}")
        time.sleep(5.0)

    return {"remote_id": remote_id, "url": f"https://www.youtube.com/watch?v={remote_id}"}


def publish_sync(
    platform: str,
    credentials: dict,
    content: dict,
    config: Any = None,
) -> Tuple[str, dict]:
    """Publish content to a platform. Returns (status, result_dict).
    Raises PermanentError, RetryableError, or UncertainError."""
    caption = content.get("caption", "")
    title = content.get("title", "")
    media_urls = content.get("media_urls", [])
    kind = content.get("kind", "text")

    access_token = credentials.get("access_token", "")
    unipile_account_id = credentials.get("unipile_account_id", "")
    if not access_token and not unipile_account_id:
        raise PermanentError("no access_token or unipile_account_id in credentials")

    try:
        import httpx
    except ImportError:
        log.warning("httpx not available — simulating publish")
        return "simulated", {"method": "simulated", "platform": platform}

    timeout = getattr(config, "timeout_seconds", 30) if config else 30

    try:
        if access_token.startswith("mock_token_"):
            log.info("Mock token detected. Simulating successful publish for %s", platform)
            return "success", {"method": "mock_publish", "platform": platform, "remote_id": f"mock_{uuid.uuid4().hex[:8]}"}

        if unipile_account_id:
            unipile_dsn = os.getenv("UNIPILE_DSN", "").rstrip("/")
            unipile_token = os.getenv("UNIPILE_ACCESS_TOKEN", "")
            url = f"{unipile_dsn}/api/v1/posts"
            body = {
                "account_id": unipile_account_id,
                "text": caption
            }
            if media_urls:
                body["attachments"] = [{"url": media_urls[0]}]
            resp = httpx.post(url, json=body, headers={"X-API-KEY": unipile_token}, timeout=timeout)
        elif platform == "facebook":
            page_id = credentials.get("page_id", "me")
            url = f"https://graph.facebook.com/{META_VERSIONS['facebook']}/{page_id}/feed"
            resp = httpx.post(url, data={"message": caption, "access_token": access_token}, timeout=timeout)
        elif platform == "instagram":
            ig_user_id = credentials.get("ig_user_id", "")
            url = f"https://graph.facebook.com/{META_VERSIONS['instagram']}/{ig_user_id}/media"
            media_data = {"caption": caption, "access_token": access_token}
            if media_urls:
                media_data["image_url"] = media_urls[0]
            resp = httpx.post(url, data=media_data, timeout=timeout)
        elif platform == "linkedin":
            author_urn = credentials.get("author_urn", "urn:li:person:unknown")
            url = "https://api.linkedin.com/v2/ugcPosts"
            body = {
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": caption},
                        "shareMediaCategory": "NONE",
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            }
            resp = httpx.post(url, json=body, headers={
                "Authorization": f"Bearer {access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
            }, timeout=timeout)
        elif platform == "x":
            url = "https://api.x.com/2/tweets"
            resp = httpx.post(url, json={"text": caption}, headers={
                "Authorization": f"Bearer {access_token}",
            }, timeout=timeout)
        elif platform == "threads":
            threads_user_id = credentials.get("threads_user_id", "")
            url = f"https://graph.threads.net/{META_VERSIONS['threads']}/{threads_user_id}/threads"
            resp = httpx.post(url, data={
                "media_type": "TEXT",
                "text": caption,
                "access_token": access_token,
            }, timeout=timeout)
        elif platform == "tiktok":
            # TikTok Content Posting API requires video upload — text-only not supported
            raise PermanentError("TikTok requires video content for live posting")
        elif platform == "youtube":
            if kind != "video" or not media_urls:
                raise PermanentError("YouTube posting requires a generated video (kind=video with a media URL)")
            token = _refresh_google_access_token({**credentials, "workspace_id": credentials.get("workspace_id", "")})
            up = _upload_youtube_video(token, content)
            return "published", {
                "method": "live",
                "platform": "youtube",
                "remote_id": up["remote_id"],
                "url": up["url"],
            }
        elif platform == "pinterest":
            url = "https://api.pinterest.com/v5/pins"
            body = {"title": title or caption[:100], "description": caption}
            if media_urls:
                body["media_source"] = {"source_type": "image_url", "url": media_urls[0]}
            board_id = credentials.get("board_id", "")
            if board_id:
                body["board_id"] = board_id
            resp = httpx.post(url, json=body, headers={
                "Authorization": f"Bearer {access_token}",
            }, timeout=timeout)
        else:
            raise PermanentError(f"no live adapter for platform '{platform}'")

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise RetryableError(f"{platform} rate limited", retry_after=retry_after)
        if resp.status_code >= 500:
            raise RetryableError(f"{platform} server error ({resp.status_code})")
        if resp.status_code >= 400:
            raise PermanentError(f"{platform} rejected ({resp.status_code}): {resp.text[:200]}")

        try:
            result = resp.json()
        except Exception:
            result = {"raw": resp.text[:200]}

        remote_id = result.get("id") or result.get("data", {}).get("id", "")
        return "published", {"method": "live", "platform": platform, "remote_id": str(remote_id), **result}

    except (PermanentError, RetryableError, UncertainError):
        raise
    except httpx.TimeoutException:
        raise UncertainError(f"{platform} timed out — post may have been published")
    except httpx.ConnectError:
        raise RetryableError(f"{platform} connection failed")
    except Exception as exc:
        raise RetryableError(f"unexpected publish error for {platform}: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — DATA ENGINE (Google Sheets + PDF Knowledge)
# ═══════════════════════════════════════════════════════════════════════════════

# Reuse the existing WhatsApp infrastructure (same OAuth, same DB)
try:
    from app.agents.whatsapp_sheets import WhatsAppSheets
    from app.agents.whatsapp_knowledge import KnowledgeRepository
except ImportError:
    WhatsAppSheets = None  # type: ignore
    KnowledgeRepository = None  # type: ignore
    log.warning("WhatsApp Sheets / Knowledge modules not available — data engine disabled")


class SocialSheets:
    """Google Sheets connector for the social agent (own tenant, own callback)."""

    REDIRECT_PATH = "/social/integrations/sheets/callback"

    def __init__(self) -> None:
        if WhatsAppSheets is not None:
            self._inner = WhatsAppSheets(tenant_id="social")
        else:
            self._inner = None

    def start_authorization(self, label: str = "Social data sheet", redirect_path: str | None = None) -> dict:
        if self._inner is None:
            raise RuntimeError("Google Sheets integration not available")
        return self._inner.start_authorization(label, redirect_path or self.REDIRECT_PATH)

    def exchange_code(self, connection_id: str, code: str, redirect_uri: str) -> dict:
        if self._inner is None:
            raise RuntimeError("Google Sheets integration not available")
        return self._inner.exchange_code(connection_id, code, redirect_uri)

    def status(self) -> dict:
        if self._inner is None:
            return {"connected": False}
        return self._inner.status()

    def sync(self, rows: list, target: str = "social-data") -> dict:
        if self._inner is None:
            raise RuntimeError("Google Sheets integration not available")
        return self._inner.sync(rows, target=target)

    def record_sync(self, target: str, rows: int, ok: bool, error: str = "") -> None:
        if self._inner is not None:
            self._inner.record_sync(target, rows, ok, error)

    def history(self) -> list:
        if self._inner is None:
            return []
        return self._inner.history()


class SocialKnowledge:
    """PDF / CSV / TXT knowledge store for the social agent."""

    def __init__(self) -> None:
        if KnowledgeRepository is not None:
            self._inner = KnowledgeRepository(tenant_id="social")
        else:
            self._inner = None

    def ingest(self, filename: str, data: bytes, mime: str = "") -> dict:
        if self._inner is None:
            raise RuntimeError("Knowledge engine not available")
        return self._inner.ingest(filename, data, mime)

    def list_documents(self) -> list:
        if self._inner is None:
            return []
        return self._inner.list_documents()

    def search(self, query: str = "") -> list:
        if self._inner is None:
            return []
        return self._inner.search(query)

    def delete_document(self, doc_id: str) -> bool:
        if self._inner is None:
            return False
        return self._inner.delete_document(doc_id)

    def knowledge_context(self, query: str, max_chars: int = 1500) -> str:
        if self._inner is None:
            return ""
        return self._inner.knowledge_context(query, max_chars=max_chars)


SHEETS = SocialSheets()
KNOWLEDGE = SocialKnowledge()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — SETTINGS & MIGRATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_settings(data: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def get_dry_run() -> bool:
    s = _read_settings()
    return bool(s.get("dry_run", DRY_RUN_DEFAULT))


def set_dry_run(enabled: bool) -> bool:
    s = _read_settings()
    s["dry_run"] = bool(enabled)
    _write_settings(s)
    log.info("Social dry-run mode set to %s", enabled)
    return get_dry_run()


_MIGRATION_COLUMNS = {
    "social_campaigns": [
        ("enabled", "INTEGER DEFAULT 1"),
        ("recurrence", "VARCHAR DEFAULT 'none'"),
        ("timezone_name", "VARCHAR DEFAULT 'UTC'"),
        ("account_ids", "TEXT"),
        ("anchor_at", "DATETIME"),
        ("next_run_at", "DATETIME"),
    ],
    "social_platform_jobs": [
        ("attempts", "INTEGER DEFAULT 0"),
        ("available_at", "DATETIME"),
        ("lease_until", "DATETIME"),
        ("claim_token", "VARCHAR"),
        ("result", "TEXT"),
    ],
}


def _migrate_schema() -> None:
    """Add columns introduced by claim/lease/recurrence to existing DBs."""
    from app.database.database import DATABASE_URL
    if not DATABASE_URL.startswith("sqlite"):
        return
    raw = DATABASE_URL.split("sqlite:///", 1)[-1]
    db_path = Path(raw)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent.parent.parent / db_path
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        for table, columns in _MIGRATION_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    log.info("Migration: added %s.%s", table, name)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_social_jobs_next_run ON social_campaigns (next_run_at)")
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — WORKSPACES & ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════════

def _retry_locked(fn: Callable[[], T], tries: int = 5) -> T:
    """Retry a DB write when SQLite is momentarily busy (WAL writer collision)."""
    for attempt in range(tries):
        try:
            return fn()
        except OperationalError as exc:
            if "locked" not in str(exc) or attempt == tries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError("unreachable")


def ensure_demo_workspace() -> None:
    """Idempotent — migrate schema + seed the demo agency workspace."""
    from app.database.database import SessionLocal
    from app.database.models import SocialWorkspace
    _migrate_schema()
    db = SessionLocal()
    try:
        if db.query(SocialWorkspace).count() == 0:
            db.add(SocialWorkspace(id=DEMO_WORKSPACE_ID, name="My Agency"))
            db.commit()
            log.info("Seeded demo social workspace '%s'", DEMO_WORKSPACE_ID)
    finally:
        db.close()


def list_workspaces() -> list[dict]:
    from app.database.database import SessionLocal
    from app.database.models import SocialWorkspace
    db = SessionLocal()
    try:
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


def list_accounts(workspace_id: str = DEMO_WORKSPACE_ID) -> list[dict]:
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount
    db = SessionLocal()
    try:
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


def add_account(
    workspace_id: str,
    platform: str,
    token_data: dict,
    account_name: str = "",
    verify: bool = True,
) -> dict:
    return _retry_locked(
        lambda: _add_account_impl(workspace_id, platform, token_data, account_name, verify)
    )


def _add_account_impl(
    workspace_id: str,
    platform: str,
    token_data: dict,
    account_name: str = "",
    verify: bool = True,
) -> dict:
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount, SocialWorkspace
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform '{platform}'")
    if not (token_data or {}).get("access_token", "").strip() and not (token_data or {}).get("password", "").strip():
        raise ValueError("access_token or password is required — the engine no longer accepts dummy connections")

    # Verify the token against the live provider unless the caller (OAuth
    # callback) already did. Manual connections land here.
    derived_name = ""
    if verify and not token_data.get("verified"):
        if token_data.get("password") and token_data.get("username"):
            unipile_dsn = os.getenv("UNIPILE_DSN", "").rstrip("/")
            unipile_token = os.getenv("UNIPILE_ACCESS_TOKEN", "")
            if not unipile_dsn or not unipile_token:
                raise ValueError("Unipile is not configured in .env for ID/Password login.")
            try:
                import httpx
                r = httpx.post(
                    f"{unipile_dsn}/api/v1/accounts",
                    headers={"X-API-KEY": unipile_token},
                    json={
                        "provider": platform.upper(),
                        "username": token_data["username"],
                        "password": token_data["password"]
                    },
                    timeout=30
                )
                r.raise_for_status()
                data = r.json()
                token_data = {
                    "unipile_account_id": data.get("account_id") or data.get("id"),
                    "username": token_data["username"],
                    "verified": True
                }
                derived_name = f"@{token_data['username']}"
            except Exception as exc:
                raise ValueError(f"Unipile proxy connection failed: {exc}")
        else:
            try:
                derived_name, extra = _fetch_identity(platform, token_data)
                token_data = {**token_data, **extra, "verified": True}
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc

    db = SessionLocal()
    try:
        ws = db.query(SocialWorkspace).filter(SocialWorkspace.id == workspace_id).first()
        if ws is None:
            raise KeyError(f"workspace {workspace_id} not found")
        # Upsert: one connection per platform per workspace. Re-connecting
        # refreshes the existing account instead of stacking duplicates
        # (a duplicate would cause every post to be published twice).
        acc = (
            db.query(SocialAccount)
            .filter(SocialAccount.workspace_id == workspace_id,
                    SocialAccount.platform == platform)
            .first()
        )
        if acc is not None:
            acc.account_name = (account_name or derived_name or acc.account_name or f"@{platform}_user")
            acc.encrypted_token = _encrypt_text(json.dumps(token_data))
            acc.status = "connected"
            acc.last_error = None
            created = False
        else:
            acc = SocialAccount(
                workspace_id=workspace_id,
                platform=platform,
                account_name=(account_name or derived_name or f"@{platform}_user"),
                encrypted_token=_encrypt_text(json.dumps(token_data)),
                status="connected",
            )
            db.add(acc)
            created = True
        db.commit()
        db.refresh(acc)
        log.info(
            "%s %s account '%s' in workspace %s (verified live)",
            "Connected" if created else "Re-connected",
            platform, acc.account_name, workspace_id,
        )
        return {
            "id": acc.id,
            "platform": acc.platform,
            "account_name": acc.account_name,
            "status": acc.status,
            "verified": True,
        }
    finally:
        db.close()


def remove_account(account_id: str) -> bool:
    return _retry_locked(lambda: _remove_account_impl(account_id))


def _remove_account_impl(account_id: str) -> bool:
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount
    db = SessionLocal()
    try:
        acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
        if acc is None:
            return False
        db.delete(acc)
        db.commit()
        log.info("Removed social account %s", account_id)
        return True
    finally:
        db.close()


def set_video_db(workspace_id: str, custom_db_url: str) -> bool:
    return _retry_locked(lambda: _set_video_db_impl(workspace_id, custom_db_url))


def _set_video_db_impl(workspace_id: str, custom_db_url: str) -> bool:
    from app.database.database import SessionLocal
    from app.database.models import SocialWorkspace
    db = SessionLocal()
    try:
        ws = db.query(SocialWorkspace).filter(SocialWorkspace.id == workspace_id).first()
        if ws is None:
            raise KeyError(f"workspace {workspace_id} not found")
        ws.encrypted_video_db_url = _encrypt_text(custom_db_url) if custom_db_url else None
        db.commit()
        log.info("Custom video DB configured for workspace %s", workspace_id)
        return True
    finally:
        db.close()


def set_account_credentials(account_id: str, credentials: dict) -> dict:
    """Store validated real platform credentials — switches account to live publishing."""
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount

    def _impl() -> dict:
        db = SessionLocal()
        try:
            acc = db.query(SocialAccount).filter(SocialAccount.id == account_id).first()
            if acc is None:
                raise KeyError(f"account {account_id} not found")
            if acc.platform not in REQUIRED_CREDENTIALS:
                raise ValueError(f"credentials for '{acc.platform}' are not supported")
            validate_credentials(acc.platform, {str(k): str(v) for k, v in (credentials or {}).items()})
            merged = {"access_token": "", **credentials}
            acc.encrypted_token = _encrypt_text(json.dumps(merged))
            acc.status = "connected"
            acc.last_error = None
            db.commit()
            return {"id": acc.id, "platform": acc.platform, "mode": "live"}
        finally:
            db.close()

    out = _retry_locked(_impl)
    log.info("Live credentials stored for account %s (%s)", account_id, out.get("platform"))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — SCHEDULING & CAMPAIGNS
# ═══════════════════════════════════════════════════════════════════════════════

def schedule_post(
    workspace_id: str,
    account_ids: list[str],
    content: str,
    content_kind: str = "text",
    scheduled_for: Optional[str] = None,
    recurrence: str = "none",
    timezone_name: str = "UTC",
) -> dict:
    """Create a campaign + content item + one job per selected account/platform."""
    return _retry_locked(
        lambda: _schedule_post_impl(
            workspace_id, account_ids, content, content_kind, scheduled_for, recurrence, timezone_name
        )
    )


def _schedule_post_impl(
    workspace_id: str,
    account_ids: list[str],
    content: str,
    content_kind: str = "text",
    scheduled_for: Optional[str] = None,
    recurrence: str = "none",
    timezone_name: str = "UTC",
) -> dict:
    from app.database.database import SessionLocal
    from app.database.models import (
        SocialAccount, SocialCampaign, SocialContentItem,
        SocialPlatformJob, SocialWorkspace,
    )

    if content_kind not in CONTENT_KINDS:
        raise ValueError(f"content_kind must be one of {', '.join(CONTENT_KINDS)}")
    content = (content or "").strip()
    if not content:
        raise ValueError("content is required")
    if not account_ids:
        raise ValueError("select at least one account")
    if recurrence not in RECURRENCES:
        raise ValueError(f"recurrence must be one of {', '.join(RECURRENCES)}")
    if not valid_timezone(timezone_name):
        raise ValueError(f"unknown IANA timezone '{timezone_name}'")

    db = SessionLocal()
    try:
        ws = db.query(SocialWorkspace).filter(SocialWorkspace.id == workspace_id).first()
        if ws is None:
            raise KeyError(f"workspace {workspace_id} not found")

        when = _utcnow()
        if scheduled_for:
            try:
                parsed = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                when = parsed
            except ValueError:
                raise ValueError("scheduled_for must be an ISO-8601 datetime")

        campaign = SocialCampaign(
            workspace_id=workspace_id,
            name=f"Post_{campaign_id_suffix()}",
            recurrence=recurrence,
            timezone_name=timezone_name,
            account_ids=json.dumps(list(account_ids)),
            anchor_at=when,
            next_run_at=(following_future(when, recurrence, timezone_name, when) if recurrence != "none" else None),
        )
        db.add(campaign)
        db.flush()

        item = SocialContentItem(campaign_id=campaign.id, kind=content_kind, raw_text=content)
        db.add(item)

        created = 0
        for acc_id in account_ids:
            acc = db.query(SocialAccount).filter(SocialAccount.id == acc_id).first()
            if acc is None or acc.workspace_id != workspace_id:
                continue
            db.add(
                SocialPlatformJob(
                    campaign_id=campaign.id,
                    account_id=acc.id,
                    platform=acc.platform,
                    scheduled_for=when,
                    status="pending",
                )
            )
            created += 1
        if created == 0:
            raise ValueError("none of the selected accounts exist in this workspace")
        db.commit()
        log.info("Scheduled campaign %s across %d platform(s)", campaign.name, created)
        return {"campaign_id": campaign.id, "campaign_name": campaign.name, "jobs_created": created}
    finally:
        db.close()


def list_jobs(workspace_id: str = DEMO_WORKSPACE_ID, limit: int = 50, status: Optional[str] = None) -> list[dict]:
    from app.database.database import SessionLocal
    from app.database.models import (
        SocialAccount,
        SocialCampaign,
        SocialContentItem,
        SocialPlatformJob,
        SocialWorkspace,
    )

    if status and status not in JOB_STATUSES:
        raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}")

    db = SessionLocal()
    try:
        q = (
            db.query(SocialPlatformJob, SocialCampaign.name, SocialAccount.account_name, SocialContentItem.raw_text)
            .join(SocialCampaign, SocialPlatformJob.campaign_id == SocialCampaign.id)
            .join(SocialWorkspace, SocialCampaign.workspace_id == SocialWorkspace.id)
            .join(SocialAccount, SocialPlatformJob.account_id == SocialAccount.id)
            .outerjoin(SocialContentItem, SocialContentItem.campaign_id == SocialCampaign.id)
            .filter(SocialWorkspace.id == workspace_id)
        )
        if status:
            q = q.filter(SocialPlatformJob.status == status)
        rows = q.order_by(SocialPlatformJob.created_at.desc()).limit(limit).all()

        def _method(j: Any) -> str:
            try:
                return str((json.loads(j.result or "{}") or {}).get("method", ""))
            except Exception:
                return ""

        return [
            {
                "id": j.id,
                "campaign": campaign_name,
                "platform": j.platform,
                "platform_label": PLATFORM_LABELS.get(j.platform, j.platform),
                "account": account_name,
                "content_preview": raw_text or "",
                "status": j.status,
                "error": j.error or "",
                "method": _method(j),
                "attempts": int(j.attempts or 0),
                "scheduled_for": j.scheduled_for.isoformat() if j.scheduled_for else None,
                "published_at": j.published_at.isoformat() if j.published_at else None,
            }
            for j, campaign_name, account_name, raw_text in rows
        ]
    finally:
        db.close()


def list_campaigns(workspace_id: str = DEMO_WORKSPACE_ID, limit: int = 50) -> list[dict]:
    from app.database.database import SessionLocal
    from app.database.models import SocialCampaign, SocialContentItem

    db = SessionLocal()
    try:
        rows = (
            db.query(SocialCampaign, SocialContentItem)
            .outerjoin(SocialContentItem, SocialContentItem.campaign_id == SocialCampaign.id)
            .filter(SocialCampaign.workspace_id == workspace_id)
            .order_by(SocialCampaign.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": c.id,
                "name": c.name,
                "enabled": bool(c.enabled),
                "recurrence": c.recurrence or "none",
                "timezone": c.timezone_name or "UTC",
                "next_run_at": c.next_run_at.isoformat() if c.next_run_at else None,
                "kind": (item.kind if item else "text"),
                "preview": ((item.raw_text[:110] + "…") if item and len(item.raw_text) > 110 else (item.raw_text if item else "")),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c, item in rows
        ]
    finally:
        db.close()


def set_campaign_enabled(campaign_id: str, enabled: bool) -> dict:
    from app.database.database import SessionLocal
    from app.database.models import SocialCampaign

    def _impl() -> dict:
        db = SessionLocal()
        try:
            camp = db.query(SocialCampaign).filter(SocialCampaign.id == campaign_id).first()
            if camp is None:
                raise KeyError(f"campaign {campaign_id} not found")
            camp.enabled = 1 if enabled else 0
            if not enabled:
                camp.next_run_at = None
            elif camp.recurrence and camp.recurrence != "none":
                camp.next_run_at = following_future(
                    _utcnow(), camp.recurrence, camp.timezone_name or "UTC", camp.anchor_at or _utcnow()
                )
            db.commit()
            return {"id": camp.id, "enabled": bool(camp.enabled), "next_run_at": camp.next_run_at.isoformat() if camp.next_run_at else None}
        finally:
            db.close()

    return _retry_locked(_impl)


def retry_job(job_id: str, acknowledge_duplicate_risk: bool = False) -> dict:
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformJob

    def _impl() -> dict:
        db = SessionLocal()
        try:
            job = db.query(SocialPlatformJob).filter(SocialPlatformJob.id == job_id).first()
            if job is None:
                raise KeyError(f"job {job_id} not found")
            if job.status not in ("failed", "uncertain"):
                raise ValueError(f"job is {job.status}; only failed/uncertain jobs can be retried")
            if job.status == "uncertain" and not acknowledge_duplicate_risk:
                raise ValueError(
                    "this job's outcome was unknown — confirm acknowledge_duplicate_risk=true to risk posting it twice"
                )
            job.status = "pending"
            job.attempts = 0
            job.available_at = None
            job.error = None
            job.result = None
            job.claim_token = None
            job.lease_until = None
            db.commit()
            return {"id": job.id, "status": job.status}
        finally:
            db.close()

    out = _retry_locked(_impl)
    log.info("Job %s re-queued for retry", job_id)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — JOB QUEUE & WORKER
# ═══════════════════════════════════════════════════════════════════════════════

class ContentCreationAgent:
    """Generates video content grounded in the workspace's data engine."""
    @staticmethod
    def generate_video(workspace_id: str, item: Any) -> dict:
        ctx = KNOWLEDGE.knowledge_context(item.raw_text, max_chars=1500)
        sheets = SHEETS.status()
        log.info(
            "Generating video for workspace %s (knowledge_chars=%d, sheets_connected=%s)",
            workspace_id, len(ctx), sheets.get("connected", False),
        )
        
        try:
            from app.services.higgsfield import HiggsfieldClient
            client = HiggsfieldClient()
            video_url = client.generate_video_sync(prompt=item.raw_text)
        except Exception as exc:
            log.error("Failed to generate video via Higgsfield: %s", exc)
            video_url = None

        return {
            "ok": True,
            "method": "ai_video_data_engine",
            "knowledge_chars": len(ctx),
            "sheets_connected": bool(sheets.get("connected", False)),
            "video_url": video_url,
        }


PLATFORM_PUBLISHERS: dict[str, Any] = {}


class _PublishConfig:
    def __init__(self) -> None:
        self.timeout_seconds = int(os.getenv("SOCIAL_HTTP_TIMEOUT", "30"))
        self.max_attempts = MAX_ATTEMPTS
        self.dry_run = DRY_RUN_DEFAULT


_PUBLISH_CONFIG = _PublishConfig()


def _account_credentials(encrypted_token: Optional[str], platform: str) -> tuple[Optional[dict], dict]:
    """Decrypt the token blob and decide real vs simulated credentials."""
    try:
        raw = _decrypt_text(encrypted_token)
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if data.get("simulated"):
        return None, data
    required = REQUIRED_CREDENTIALS.get(platform)
    if not required:
        return None, data
    if all(str(data.get(key, "")).strip() for key in required):
        return data, data
    return None, data


def _job_content(kind: str, raw_text: str, media_urls: list[str] = None) -> dict:
    return {"kind": kind, "caption": raw_text, "title": "", "media_urls": media_urls or []}


def _publish_snapshot(
    platform: str,
    credentials: Optional[dict],
    content: dict,
    account_name: str,
    campaign_name: str,
) -> dict:
    """One publish attempt. Never raises — returns an allowlisted result dict."""
    if credentials is None:
        log.info(
            "Published campaign %s to %s (account %s, kind=%s) [simulated]",
            campaign_name, platform, account_name, content["kind"],
        )
        return {"ok": True, "method": "simulated", "platform": platform}
    if content["kind"] != "text" and not content["media_urls"]:
        # NEVER report success without delivering — the UI would show a green
        # SUCCESS for a post that exists nowhere. Media platforms cannot post
        # without media, so fail honestly and let the queue surface the error.
        if platform in ("youtube", "tiktok", "instagram"):
            return {
                "ok": False, "permanent": True,
                "error": (
                    f"no video/image was produced for this {platform} post — "
                    "AI video generation returned nothing (check the video provider "
                    "API keys in backend/.env) so nothing was uploaded"
                ),
            }
        log.info("Published campaign %s to %s [simulated: no public media URL]", campaign_name, platform)
        return {"ok": True, "method": "simulated", "platform": platform, "reason": "no public media URL"}
    try:
        validate_credentials(platform, credentials)
        validate_content(_PUBLISH_CONFIG, platform, content)
    except ValueError as exc:
        return {"ok": False, "permanent": True, "error": f"invalid post for {platform}: {exc}"}
    try:
        status, result = publish_sync(platform, credentials, content, _PUBLISH_CONFIG)
        log.info("Published campaign %s to %s [live, status=%s]", campaign_name, platform, status)
        return {"ok": True, "method": "live", "platform": platform, "platform_status": status, **result}
    except PermanentError as exc:
        return {"ok": False, "permanent": True, "error": str(exc)}
    except RetryableError as exc:
        return {"ok": False, "retryable": True, "retry_after": float(exc.retry_after or 0.0), "error": str(exc)}
    except UncertainError as exc:
        return {"ok": False, "uncertain": True, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "retryable": True, "error": f"unexpected publish error: {exc}"}


def _reclaim_expired(db: Any) -> int:
    from app.database.models import SocialPlatformJob
    from sqlalchemy import or_
    now = _utcnow()
    reclaimed = (
        db.query(SocialPlatformJob)
        .filter(
            SocialPlatformJob.status == "processing",
            or_(SocialPlatformJob.lease_until.is_(None), SocialPlatformJob.lease_until < now),
        )
        .update(
            {
                SocialPlatformJob.status: "pending",
                SocialPlatformJob.claim_token: None,
                SocialPlatformJob.lease_until: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return reclaimed


def _enqueue_recurring(db: Any) -> int:
    from app.database.models import SocialAccount, SocialCampaign, SocialPlatformJob
    now = _utcnow()
    camps = (
        db.query(SocialCampaign)
        .filter(
            SocialCampaign.enabled == 1,
            SocialCampaign.recurrence.isnot(None),
            SocialCampaign.recurrence != "none",
            SocialCampaign.next_run_at.isnot(None),
            SocialCampaign.next_run_at <= now,
        )
        .all()
    )
    enqueued = 0
    for camp in camps:
        occurrence = camp.next_run_at
        try:
            target_ids = json.loads(camp.account_ids or "[]")
        except Exception:
            target_ids = []
        accounts = []
        if target_ids:
            accounts = (
                db.query(SocialAccount)
                .filter(SocialAccount.workspace_id == camp.workspace_id, SocialAccount.id.in_(target_ids))
                .all()
            )
        for acc in accounts:
            exists = (
                db.query(SocialPlatformJob)
                .filter(
                    SocialPlatformJob.campaign_id == camp.id,
                    SocialPlatformJob.account_id == acc.id,
                    SocialPlatformJob.scheduled_for == occurrence,
                )
                .first()
            )
            if exists is None:
                db.add(
                    SocialPlatformJob(
                        campaign_id=camp.id,
                        account_id=acc.id,
                        platform=acc.platform,
                        scheduled_for=occurrence,
                        status="pending",
                    )
                )
                enqueued += 1
        try:
            camp.next_run_at = following_future(
                occurrence, camp.recurrence, camp.timezone_name or "UTC", camp.anchor_at or occurrence
            )
        except Exception as exc:
            log.warning("recurrence roll failed for campaign %s — pausing it: %s", camp.id, exc)
            camp.enabled = 0
    db.commit()
    return enqueued


def _claim_jobs(db: Any, limit: int) -> list:
    from app.database.models import SocialPlatformJob
    from sqlalchemy import or_
    now = _utcnow()
    rows = (
        db.query(SocialPlatformJob)
        .filter(
            SocialPlatformJob.status == "pending",
            SocialPlatformJob.scheduled_for <= now,
            or_(SocialPlatformJob.available_at.is_(None), SocialPlatformJob.available_at <= now),
        )
        .order_by(SocialPlatformJob.scheduled_for)
        .limit(limit)
        .all()
    )
    claimed = []
    for job in rows:
        token = uuid_mod.uuid4().hex
        job.status = "processing"
        job.claim_token = token
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        job.attempts = int(job.attempts or 0) + 1
        claimed.append((job.id, token))
    db.commit()
    return claimed


def _load_snapshot(job_id: str) -> Optional[dict]:
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount, SocialCampaign, SocialContentItem, SocialPlatformJob
    db = SessionLocal()
    try:
        job = db.query(SocialPlatformJob).filter(SocialPlatformJob.id == job_id).first()
        if job is None:
            return None
        campaign = db.query(SocialCampaign).filter(SocialCampaign.id == job.campaign_id).first()
        account = db.query(SocialAccount).filter(SocialAccount.id == job.account_id).first()
        item = (
            db.query(SocialContentItem)
            .filter(SocialContentItem.campaign_id == job.campaign_id)
            .order_by(SocialContentItem.created_at)
            .first()
        )
        if campaign is None or account is None or item is None:
            return {"missing": True}
        creds, _raw = _account_credentials(account.encrypted_token, job.platform)
        return {
            "missing": False,
            "job_id": job.id,
            "platform": job.platform,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "workspace_id": campaign.workspace_id,
            "account_id": account.id,
            "account_name": account.account_name,
            "kind": item.kind,
            "raw_text": item.raw_text,
            "credentials": creds,
            "attempts": int(job.attempts or 0),
        }
    finally:
        db.close()


def _finalize_claim(
    job_id: str, token: str, status: str,
    result: Optional[dict], error: Optional[str],
    backoff_s: float = 0.0,
) -> None:
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformJob
    db = SessionLocal()
    try:
        job = db.query(SocialPlatformJob).filter(SocialPlatformJob.id == job_id).first()
        if job is None or job.claim_token != token:
            return
        now = _utcnow()
        job.status = status
        job.error = error
        job.result = json.dumps(result, ensure_ascii=False)[:2000] if result else None
        job.claim_token = None
        job.lease_until = None
        if status in ("success", "dry_run"):
            job.published_at = now
            job.available_at = None
        elif status == "pending":
            job.available_at = now + timedelta(seconds=backoff_s)
        db.commit()
    finally:
        db.close()


def _execute_claim(job_id: str, token: str) -> str:
    from types import SimpleNamespace
    snap = _load_snapshot(job_id)
    if snap is None:
        return "gone"
    if snap.get("missing"):
        _finalize_claim(job_id, token, "failed", None, "campaign/account/content missing")
        return "failed"

    hook = PLATFORM_PUBLISHERS.get(snap["platform"])
    if hook is not None:
        try:
            result = hook(
                SimpleNamespace(id=snap["job_id"], platform=snap["platform"]),
                SimpleNamespace(id=snap["campaign_id"], name=snap["campaign_name"]),
                SimpleNamespace(kind=snap["kind"], raw_text=snap["raw_text"]),
                SimpleNamespace(id=snap["account_id"], platform=snap["platform"], account_name=snap["account_name"]),
            )
            if not isinstance(result, dict) or not result.get("ok"):
                result = {"ok": False, "permanent": True, "error": str((result or {}).get("error") if isinstance(result, dict) else result)}
        except Exception as exc:
            result = {"ok": False, "retryable": True, "error": f"custom publisher failed: {exc}"}
    elif get_dry_run():
        result = {"ok": True, "method": "dry_run", "platform": snap["platform"]}
    else:
        if snap["kind"] == "video":
            try:
                video_res = ContentCreationAgent.generate_video(snap["workspace_id"], SimpleNamespace(raw_text=snap["raw_text"]))
                if video_res and video_res.get("video_url"):
                    snap["media_urls"] = [video_res["video_url"]]
            except Exception as exc:
                log.warning("video generation failed for job %s: %s", job_id, exc)
        result = _publish_snapshot(
            snap["platform"], snap["credentials"],
            _job_content(snap["kind"], snap["raw_text"], snap.get("media_urls")),
            snap["account_name"], snap["campaign_name"],
        )

    if result.get("ok"):
        status = "dry_run" if result.get("method") == "dry_run" else "success"
        _finalize_claim(job_id, token, status, result, None)
        return status

    error = str(result.get("error") or "publish failed")[:500]
    if result.get("uncertain"):
        _finalize_claim(job_id, token, "uncertain", result, error)
        return "uncertain"
    attempts = int(snap.get("attempts") or 1)
    if result.get("permanent") or attempts >= MAX_ATTEMPTS:
        _finalize_claim(job_id, token, "failed", result, error)
        return "failed"
    delay = float(result.get("retry_after") or 0.0) or min(30.0 * (2 ** max(0, attempts - 1)), 600.0)
    _finalize_claim(job_id, token, "pending", result, error, backoff_s=delay)
    return "retry_scheduled"


def poll_pending_jobs(limit: int = 50) -> int:
    claimed = _retry_locked(lambda: _poll_claims(limit), tries=3)
    done = 0
    for job_id, token in claimed:
        try:
            _execute_claim(job_id, token)
        except Exception:
            log.exception("claim execution crashed for job %s", job_id)
            _finalize_claim(job_id, token, "pending", None, "worker error — will retry", backoff_s=60)
        done += 1
    return done


def _poll_claims(limit: int) -> list:
    from app.database.database import SessionLocal
    db = SessionLocal()
    try:
        _reclaim_expired(db)
        _enqueue_recurring(db)
        return _claim_jobs(db, limit)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — AI AUTO-PILOT
# ═══════════════════════════════════════════════════════════════════════════════

_PILOT_DEFAULTS = {
    "enabled": False,
    "niche": "",
    "schedule_text": "",
    "cadence": {"days": None, "hour": 9, "minute": 0, "posts_per_run": 3, "interval_hours": None},
    "next_run": None,
    "last_run": None,
    "posts_generated": 0,
    "drafts": [],
}

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def pilot_state() -> dict:
    try:
        data = json.loads(PILOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    state = {**_PILOT_DEFAULTS, **(data if isinstance(data, dict) else {})}
    state.setdefault("drafts", [])
    return state


def _save_pilot(state: dict) -> None:
    tmp = PILOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PILOT_FILE)


def parse_schedule(text: str) -> dict:
    """Parse free-form schedules like '3 posts per week, mon/wed/fri 10:00'."""
    low = (text or "").lower()
    cadence: dict[str, Any] = {"days": None, "hour": 9, "minute": 0, "posts_per_run": 3, "interval_hours": None}

    m = re.search(r"every (\d+) hours?", low)
    if m:
        cadence["interval_hours"] = int(m.group(1))
        cadence["posts_per_run"] = 1
        return cadence

    days: list[int] = []
    for name, idx in _DAY_NAMES.items():
        if re.search(rf"\b{name}(?:day)?\b", low):
            days.append(idx)
    if days:
        cadence["days"] = sorted(set(days))

    hour, minute, suffix = 9, 0, None
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        hour, minute, suffix = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
        if m:
            hour, minute, suffix = int(m.group(1)), 0, m.group(2)
        else:
            m = re.search(r"\bat\s+(\d{1,2})\b", low)
            if m:
                hour, minute, suffix = int(m.group(1)), 0, None
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    cadence["hour"], cadence["minute"] = hour, minute

    m = re.search(r"(\d+) posts? per (?:day|week)", low)
    if m:
        cadence["posts_per_run"] = int(m.group(1))
    elif "daily" in low or "every day" in low:
        cadence["days"] = None
    return cadence


def _next_run(cadence: dict, after: Optional[datetime] = None) -> Optional[str]:
    now = (after or datetime.now().astimezone()).replace(microsecond=0)
    if cadence.get("interval_hours"):
        return (now + timedelta(hours=int(cadence["interval_hours"]))).isoformat()
    days = cadence.get("days")
    hour, minute = int(cadence.get("hour", 9)), int(cadence.get("minute", 0))
    for offset in range(8):
        candidate = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0)
        if days is not None and candidate.weekday() not in days:
            continue
        if candidate > now:
            return candidate.isoformat()
    return None


def _extract_json_list(text: str) -> list[dict]:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M)
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        try:
            data = json.loads(t[i : j + 1])
            return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def _llm_posts(niche: str, count: int) -> list[dict]:
    """Generate post ideas with the LLM, grounded in sheets/PDF knowledge."""
    try:
        from full_ai_v3 import llm_call
    except ImportError:
        log.warning("full_ai_v3 not available — using template posts")
        return [
            {"content": f"{niche} insight #{i + 1}: here's a quick, valuable tip for anyone in the {niche} space — save this for later! 🚀", "kind": "text"}
            for i in range(count)
        ]

    ctx = KNOWLEDGE.knowledge_context(niche, max_chars=1500)
    sheets = SHEETS.status()
    sources = []
    if ctx:
        sources.append(f"Knowledge base (PDFs/docs) relevant to this niche:\n{ctx}")
    if sheets.get("connected"):
        sources.append(f"Google Sheets connected: {sheets.get('label', 'linked')}")
    system = (
        "You are SARA's social media content engine. Write scroll-stopping posts "
        "for the given niche. Reply with ONLY a JSON array of objects, each "
        '{"content": "<full post copy>", "kind": "text"}. '
        "No markdown fences, no commentary."
    )
    user = f"Niche: {niche}\nNumber of posts: {count}\n" + ("\n".join(sources) if sources else "")
    try:
        raw = llm_call(system, user, temperature=0.9, timeout=90)
    except Exception as exc:
        log.warning("pilot LLM call failed (%s) — using template posts", exc)
        raw = ""
    posts = _extract_json_list(raw)
    posts = [
        {"content": str(p.get("content", "")).strip(), "kind": "text"}
        for p in posts
        if str(p.get("content", "")).strip()
    ][:count]
    if not posts:
        posts = [
            {"content": f"{niche} insight #{i + 1}: here's a quick, valuable tip for anyone in the {niche} space — save this for later! 🚀", "kind": "text"}
            for i in range(count)
        ]
    return posts


def _queue_posts(posts: list[dict], when_iso: Optional[str] = None) -> dict:
    accounts = list_accounts(DEMO_WORKSPACE_ID)
    if not accounts:
        return {"queued": 0, "drafts": len(posts)}
    ids = [a["id"] for a in accounts]
    queued = 0
    for p in posts:
        try:
            schedule_post(DEMO_WORKSPACE_ID, ids, p["content"], p["kind"], scheduled_for=when_iso)
            queued += 1
        except Exception as exc:
            log.warning("pilot queue failed: %s", exc)
    return {"queued": queued, "drafts": 0}


def run_pilot_once() -> dict:
    state = pilot_state()
    niche = (state.get("niche") or "").strip()
    if not niche:
        return {"generated": 0, "error": "no niche configured"}
    count = max(1, int(state.get("cadence", {}).get("posts_per_run", 3)))
    posts = _llm_posts(niche, count)
    result = _queue_posts(posts)
    state["posts_generated"] = int(state.get("posts_generated", 0)) + len(posts)
    state["last_run"] = datetime.now().astimezone().isoformat()
    state["next_run"] = _next_run(state.get("cadence", {}))
    if result.get("drafts"):
        drafts = list(state.get("drafts", []))
        drafts.extend([{"content": p["content"], "kind": p["kind"], "created": state["last_run"]} for p in posts])
        state["drafts"] = drafts[-50:]
    else:
        state["drafts"] = []
    _save_pilot(state)
    log.info("Pilot run: %d posts generated (queued=%d, drafts=%d)", len(posts), result.get("queued", 0), result.get("drafts", 0))
    return {"generated": len(posts), **result, "posts": posts}


def set_pilot(niche: str, schedule: str = "", enabled: bool = True) -> dict:
    state = pilot_state()
    state["niche"] = (niche or "").strip()
    state["schedule_text"] = (schedule or "").strip()
    state["cadence"] = parse_schedule(state["schedule_text"]) if state["schedule_text"] else _PILOT_DEFAULTS["cadence"]
    state["enabled"] = bool(enabled) and bool(state["niche"])
    state["next_run"] = _next_run(state["cadence"])
    _save_pilot(state)
    log.info("Pilot configured: niche=%r enabled=%s cadence=%s", state["niche"], state["enabled"], state["cadence"])
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — BACKGROUND TASKS (poll loops)
# ═══════════════════════════════════════════════════════════════════════════════

async def social_poll_loop() -> None:
    """Background scheduler task — wired into the unified server's lifespan."""
    log.info("Social media job worker started (interval %.1fs)", POLL_INTERVAL_S)
    while True:
        try:
            n = await asyncio.to_thread(poll_pending_jobs)
            if n:
                log.info("Social worker processed %d job(s)", n)
        except Exception:
            log.exception("social poll pass failed")
        await asyncio.sleep(POLL_INTERVAL_S)


async def social_pilot_loop() -> None:
    """Background scheduler for the autopilot."""
    log.info("Social autopilot loop started (poll %.1fs)", PILOT_POLL_S)
    while True:
        try:
            state = pilot_state()
            if state.get("enabled") and state.get("niche"):
                next_run = state.get("next_run")
                due = not next_run or datetime.now().astimezone().isoformat() >= str(next_run)
                if due:
                    await asyncio.to_thread(run_pilot_once)
        except Exception:
            log.exception("autopilot pass failed")
        await asyncio.sleep(PILOT_POLL_S)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — FASTAPI ROUTER (all /social/* endpoints)
# ═══════════════════════════════════════════════════════════════════════════════

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.agent_switch import is_running as _is_running, set_running as _set_running

router = APIRouter(prefix="/social", tags=["social"])


def _page_ok(title: str, detail: str) -> HTMLResponse:
    return HTMLResponse(
        "<html><body style='font-family:system-ui,-apple-system,sans-serif;text-align:center;padding-top:100px;background:#050505;color:#ffffff'>"
        "<div style='display:inline-block;border:1px solid rgba(255,255,255,.1);border-radius:24px;padding:48px 64px;background:rgba(255,255,255,0.03);box-shadow:0 0 40px rgba(42,157,248,0.1);'>"
        "<svg width='64' height='64' viewBox='0 0 24 24' fill='none' stroke='#2A9DF8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' style='margin-bottom:20px'><path d='M22 11.08V12a10 10 0 1 1-5.93-9.14'></path><polyline points='22 4 12 14.01 9 11.01'></polyline></svg>"
        f"<h2 style='margin:0 0 12px 0;font-size:28px;font-weight:700'>{title}</h2>"
        f"<p style='color:#a1a1aa;margin:0;font-size:15px;max-width:300px;line-height:1.5'>{detail}</p>"
        "</div>"
        "<script>setTimeout(function(){ if(window.opener) { window.opener.location.reload(); window.close(); } }, 1500);</script>"
        "</body></html>"
    )

def _page_err(title: str, detail: str) -> HTMLResponse:
    return HTMLResponse(
        "<html><body style='font-family:system-ui,-apple-system,sans-serif;padding:60px 20px;background:#050505;color:#ffffff;display:flex;justify-content:center;'>"
        "<div style='width:100%;max-width:600px;border:1px solid rgba(255,255,255,.1);border-radius:24px;padding:40px;background:rgba(255,255,255,0.03);text-align:left;box-shadow:0 0 40px rgba(239,68,68,0.1);'>"
        "<div style='display:flex;align-items:center;gap:16px;margin-bottom:24px;'>"
        "<svg width='32' height='32' viewBox='0 0 24 24' fill='none' stroke='#ef4444' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='10'></circle><line x1='12' y1='8' x2='12' y2='12'></line><line x1='12' y1='16' x2='12.01' y2='16'></line></svg>"
        f"<h2 style='margin:0;font-size:24px;font-weight:700'>{title}</h2>"
        "</div>"
        f"<p style='color:#a1a1aa;font-size:15px;line-height:1.6;margin-bottom:30px'>{detail}</p>"
        "<div style='background:rgba(0,0,0,0.5);border:1px solid rgba(255,255,255,0.05);border-radius:12px;padding:20px;'>"
        "<h3 style='margin:0 0 12px 0;font-size:14px;color:#2A9DF8;text-transform:uppercase;letter-spacing:1px;'>How to Fix This:</h3>"
        "<p style='margin:0 0 16px 0;color:#d4d4d8;font-size:14px;line-height:1.5'>To enable real, authentic social media login windows, you must configure your <code style='background:rgba(255,255,255,0.1);padding:2px 6px;border-radius:4px'>backend/.env</code> file with one of the following:</p>"
        "<div style='margin-bottom:16px'>"
        "<strong style='display:block;margin-bottom:4px;font-size:14px'>Option 1: Fix Unipile (Recommended)</strong>"
        "<code style='display:block;background:#000;padding:12px;border-radius:8px;font-size:12px;color:#a1a1aa;font-family:monospace'>UNIPILE_DSN=https://api.unipile.com<br>UNIPILE_ACCESS_TOKEN=your_token_here</code>"
        "</div>"
        "<div>"
        "<strong style='display:block;margin-bottom:4px;font-size:14px'>Option 2: Official Meta API Keys</strong>"
        "<code style='display:block;background:#000;padding:12px;border-radius:8px;font-size:12px;color:#a1a1aa;font-family:monospace'>SOCIAL_INSTAGRAM_CLIENT_ID=your_app_id<br>SOCIAL_INSTAGRAM_CLIENT_SECRET=your_app_secret</code>"
        "</div>"
        "</div>"
        "</div></body></html>"
    )


# --- Health + workspaces ---

@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "social media agency (multi-platform scheduling + OAuth)",
        "platforms": list(PLATFORMS),
        "simulate_oauth": SIMULATE_OAUTH,
        "oauth_ready": oauth_ready_platforms(),
        "connection_modes": {
            p: platform_connection_modes(p) for p in PLATFORMS
        },
        "redirect_uris": {
            p: f"{PUBLIC_BASE_URL}/social/oauth/callback" for p in PLATFORMS
        },
        "poll_interval_s": POLL_INTERVAL_S,
        "running": _is_running("social"),
        "dry_run": get_dry_run(),
        "public_base_url": PUBLIC_BASE_URL,
    }


@router.get("/workspaces")
async def api_workspaces() -> dict[str, Any]:
    return {"workspaces": list_workspaces()}


# --- Accounts ---

@router.get("/accounts")
async def api_accounts(workspace_id: str = DEMO_WORKSPACE_ID) -> dict[str, Any]:
    return {"workspace_id": workspace_id, "accounts": list_accounts(workspace_id)}


@router.delete("/accounts/{account_id}")
async def api_remove_account(account_id: str) -> dict[str, Any]:
    removed = remove_account(account_id)
    if not removed:
        raise HTTPException(status_code=404, detail="account not found")
    return {"removed": True}


# --- OAuth ---

@router.get("/oauth/{platform}/login")
async def api_oauth_login(platform: str, workspace_id: str = DEMO_WORKSPACE_ID) -> Any:
    try:
        url = oauth_authorize_url(platform, workspace_id)
        return RedirectResponse(url=url)
    except ValueError as exc:
        # Fallback to Unipile Hosted Link
        unipile_dsn = os.getenv("UNIPILE_DSN", "").rstrip("/")
        unipile_token = os.getenv("UNIPILE_ACCESS_TOKEN", "")
        if unipile_dsn and unipile_token:
            try:
                import httpx
                success_url = f"{PUBLIC_BASE_URL}/social/oauth/callback?workspace_id={workspace_id}&platform={platform}&unipile_hosted=1"
                r = httpx.post(
                    f"{unipile_dsn}/api/v1/hosted/accounts/link",
                    headers={"X-API-KEY": unipile_token},
                    json={
                        "type": "create",
                        "providers": [platform.upper()],
                        "api_url": unipile_dsn,
                        "expiresOn": "2030-01-01T00:00:00.000Z",
                        "name": f"Sara AI {workspace_id}",
                        "success_redirect_url": success_url,
                        "failure_redirect_url": f"{PUBLIC_BASE_URL}/social/oauth/callback?error=unipile_failed"
                    },
                    timeout=30
                )
                r.raise_for_status()
                return RedirectResponse(url=r.json().get("url"))
            except Exception as e:
                log.error("Unipile Hosted Link generation failed: %s", e)
        # If Unipile is missing or fails, show the ultra-premium error screen
        return _page_err("Action Required: Enable Real Authentication", "The system has strictly blocked this connection because no valid official API keys or proxy configurations were found in your environment.")


class ManualConnectRequest(BaseModel):
    workspace_id: str = DEMO_WORKSPACE_ID
    platform: str
    credentials: dict[str, str]
    account_name: str = ""


@router.post("/accounts/connect")
async def api_manual_connect(payload: ManualConnectRequest) -> dict[str, Any]:
    """Way 3 — connect an account with real credentials (verified live)."""
    try:
        acc = add_account(
            payload.workspace_id,
            payload.platform,
            dict(payload.credentials),
            account_name=payload.account_name.strip(),
            verify=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "connected", "account": acc}


class AppKeysRequest(BaseModel):
    workspace_id: str = DEMO_WORKSPACE_ID
    platform: str
    client_id: str
    client_secret: str


@router.post("/apps")
async def api_save_app_keys(payload: AppKeysRequest) -> dict[str, Any]:
    """Way 2 — store the user's OWN OAuth app keys (encrypted) for one-click
    OAuth from their own developer app. Never hardcoded, per workspace."""
    try:
        result = set_platform_app(
            payload.workspace_id, payload.platform,
            payload.client_id, payload.client_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "app": result}


@router.delete("/apps/{platform}")
async def api_delete_app_keys(platform: str, workspace_id: str = DEMO_WORKSPACE_ID) -> dict[str, Any]:
    removed = delete_platform_app(workspace_id, platform)
    return {"removed": removed}


class AuthCodeRequest(BaseModel):
    workspace_id: str = DEMO_WORKSPACE_ID
    platform: str
    code: str
    redirect_uri: str = ""


@router.post("/accounts/exchange-code")
async def api_exchange_code(payload: AuthCodeRequest) -> dict[str, Any]:
    """Way 4 — exchange an authorization code the user copied from a redirect
    (for desktop/loopback redirects where the provider cannot reach us)."""
    if not payload.code.strip():
        raise HTTPException(status_code=422, detail="authorization code is required")
    try:
        token_data = exchange_oauth_code(payload.platform, payload.code.strip(), payload.workspace_id)
        acc = add_account(
            payload.workspace_id, payload.platform, token_data,
            account_name=token_data.get("account_name", ""), verify=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "connected", "account": acc}


@router.get("/oauth/callback")
async def api_oauth_callback(
    code: str = "", state: str = "", platform: str = "", account_id: str = "", unipile_hosted: str = "",
) -> Any:
    params = parse_qs_safe(state)
    ws_id = (params.get("workspace_id") or [DEMO_WORKSPACE_ID])[0]
    plat = platform or (params.get("platform") or [""])[0]

    # Handle Unipile Hosted Link Callback
    if unipile_hosted and account_id:
        try:
            unipile_dsn = os.getenv("UNIPILE_DSN", "").rstrip("/")
            unipile_token = os.getenv("UNIPILE_ACCESS_TOKEN", "")
            import httpx
            r = httpx.get(f"{unipile_dsn}/api/v1/accounts/{account_id}", headers={"X-API-KEY": unipile_token}, timeout=10)
            r.raise_for_status()
            data = r.json()
            
            token_data = {
                "unipile_account_id": account_id,
                "username": data.get("name") or data.get("username") or "user",
                "verified": True
            }
            account = add_account(ws_id, plat, token_data, account_name=f"@{token_data['username']}", verify=False)
            return _page_ok(
                PLATFORM_LABELS.get(plat, plat) + " connected",
                "Account " + account.get("account_name", "") + " is linked via Unipile Proxy. You can close this window."
            )
        except Exception as exc:
            log.warning("Unipile callback failed for %s: %s", plat, exc)
            return _page_err("Unipile Connection failed", str(exc)[:200])

    if not code or not plat:
        return _page_err("Missing code or platform", "The OAuth callback was incomplete.")
    try:
        token_data = exchange_oauth_code(plat, code)
        account = add_account(ws_id, plat, token_data, account_name=token_data.get("account_name", ""), verify=False)
    except Exception as exc:
        log.warning("OAuth callback failed for %s: %s", plat, exc)
        detail = str(exc)[:300]
        if "redirect_uri_mismatch" in detail or "redirect_uri" in detail.lower():
            detail += (
                f" — Register this EXACT redirect URI in your {PLATFORM_LABELS.get(plat, plat)} "
                f"developer app, then try again: {PUBLIC_BASE_URL}/social/oauth/callback"
            )
        return _page_err("Connection failed", detail)
    return _page_ok(
        PLATFORM_LABELS.get(plat, plat) + " connected",
        "Account " + account.get("account_name", "") + " is linked to the agency. You can close this window.",
    )





# --- Video DB ---

class VideoDBRequest(BaseModel):
    custom_db_url: str = ""


@router.post("/workspaces/{workspace_id}/video-db")
async def api_set_video_db(workspace_id: str, payload: VideoDBRequest) -> dict[str, Any]:
    try:
        set_video_db(workspace_id, payload.custom_db_url.strip())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok", "encrypted": True, "message": "Custom video database securely linked!"}


# --- Scheduling ---

class ScheduleRequest(BaseModel):
    workspace_id: str = DEMO_WORKSPACE_ID
    account_ids: list[str] = []
    content: str = ""
    content_kind: str = "text"
    scheduled_for: str | None = None
    recurrence: str = "none"
    timezone: str = "UTC"


@router.post("/schedule")
async def api_schedule_post(payload: ScheduleRequest) -> dict[str, Any]:
    try:
        result = schedule_post(
            payload.workspace_id, payload.account_ids,
            payload.content, payload.content_kind,
            payload.scheduled_for, recurrence=payload.recurrence,
            timezone_name=payload.timezone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "scheduled", **result}


@router.get("/jobs")
async def api_jobs(
    workspace_id: str = DEMO_WORKSPACE_ID, status: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    try:
        rows = list_jobs(workspace_id, limit=max(1, min(limit, 200)), status=status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"workspace_id": workspace_id, "jobs": rows}


@router.get("/campaigns")
async def api_campaigns(workspace_id: str = DEMO_WORKSPACE_ID) -> dict[str, Any]:
    return {"workspace_id": workspace_id, "campaigns": list_campaigns(workspace_id)}


class CampaignEnableRequest(BaseModel):
    enabled: bool


@router.post("/campaigns/{campaign_id}/enable")
async def api_campaign_enable(campaign_id: str, payload: CampaignEnableRequest) -> dict[str, Any]:
    try:
        return set_campaign_enabled(campaign_id, payload.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class RetryRequest(BaseModel):
    acknowledge_duplicate_risk: bool = False


@router.post("/jobs/{job_id}/retry")
async def api_job_retry(job_id: str, payload: RetryRequest) -> dict[str, Any]:
    try:
        return retry_job(job_id, payload.acknowledge_duplicate_risk)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class CredentialsRequest(BaseModel):
    credentials: dict[str, str]


@router.put("/accounts/{account_id}/credentials")
async def api_account_credentials(account_id: str, payload: CredentialsRequest) -> dict[str, Any]:
    try:
        return set_account_credentials(account_id, payload.credentials)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --- Dry-run toggle ---

class DryRunRequest(BaseModel):
    dry_run: bool


@router.post("/dry-run")
async def api_dry_run(payload: DryRunRequest) -> dict[str, Any]:
    return {"dry_run": set_dry_run(payload.dry_run)}


@router.get("/dry-run")
async def api_dry_run_status() -> dict[str, Any]:
    return {"dry_run": get_dry_run()}


# --- AI Auto-Pilot ---

class PilotRequest(BaseModel):
    niche: str = ""
    schedule: str = ""
    enabled: bool = True


@router.get("/pilot")
async def api_pilot_status() -> dict[str, Any]:
    state = pilot_state()
    return {"status": "ok", "pilot": state}


@router.post("/pilot")
async def api_pilot_configure(payload: PilotRequest) -> dict[str, Any]:
    state = set_pilot(payload.niche, payload.schedule, payload.enabled)
    return {"status": "ok", "pilot": state}


class PilotGenerateRequest(BaseModel):
    count: int = 3


@router.post("/pilot/generate")
async def api_pilot_generate(payload: PilotGenerateRequest) -> dict[str, Any]:
    count = max(1, min(int(payload.count or 3), 20))
    result = run_pilot_once()
    result["generated"] = min(result.get("generated", 0), count)
    result["posts"] = result.get("posts", [])[:count]
    if not result.get("generated"):
        raise HTTPException(status_code=422, detail=result.get("error", "no niche configured — set one first"))
    return {"status": "ok", **result}


# --- Google Sheets ---

class SheetsConnectRequest(BaseModel):
    label: str = "Social data sheet"


@router.post("/integrations/sheets/connect")
async def api_sheets_connect(payload: SheetsConnectRequest) -> dict[str, Any]:
    try:
        return SHEETS.start_authorization(payload.label)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/integrations/sheets/callback")
async def api_sheets_callback(
    code: str = "", state: str = "", error: str = "",
) -> Any:
    if error:
        return _page_err("Authorization was cancelled", error)
    if not code or not state:
        return _page_err("Missing code or state", "The Google callback was incomplete.")
    try:
        state_data = json.loads(b64decode(state + "==").decode())
        connection_id = state_data.get("c", "")
    except Exception:
        connection_id = ""
    if not code or not connection_id:
        return _page_err("Missing code or state", "The Google callback was incomplete.")
    redirect_uri = PUBLIC_BASE_URL + "/social/integrations/sheets/callback"
    try:
        result = SHEETS.exchange_code(connection_id, code, redirect_uri)
        return _page_ok("Google Sheets connected", "Account: " + result.get("account", "connected"))
    except RuntimeError as exc:
        return _page_err("Connection failed", str(exc)[:200])


@router.get("/integrations/sheets/status")
async def api_sheets_status() -> dict[str, Any]:
    return SHEETS.status()


@router.post("/integrations/sheets/sync")
async def api_sheets_sync() -> dict[str, Any]:
    rows: list[list[Any]] = [
        ["accounts", a["id"], a.get("platform_label", a.get("platform", "")),
         a.get("account_name", a.get("name", "")), a.get("status", "")]
        for a in list_accounts()
    ]
    for j in list_jobs(limit=100):
        rows.append([
            "job", j["id"], j.get("platform_label", j.get("platform", "")),
            j.get("account", ""), j.get("status", ""), j.get("campaign", ""),
        ])
    rows.append([
        "generated", "", "", "",
        pilot_state().get("posts_generated", 0),
    ])
    if not rows:
        raise HTTPException(status_code=422, detail="nothing to sync yet")
    try:
        result = SHEETS.sync(rows, target="social-data")
        SHEETS.record_sync("social-data", len(rows), True)
        return {"status": "ok", **result}
    except RuntimeError as exc:
        SHEETS.record_sync("social-data", 0, False, str(exc))
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/integrations/sheets/history")
async def api_sheets_history() -> dict[str, Any]:
    return {"history": SHEETS.history()}


# --- Knowledge / PDF ---

class KnowledgeUpload(BaseModel):
    filename: str
    content_base64: str
    mime: str = ""


@router.post("/knowledge/upload")
async def api_knowledge_upload(payload: KnowledgeUpload) -> dict[str, Any]:
    try:
        data = b64decode(payload.content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 content")
    try:
        result = KNOWLEDGE.ingest(payload.filename, data, payload.mime)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", **result}


@router.get("/knowledge")
async def api_knowledge_list() -> dict[str, Any]:
    return {"documents": KNOWLEDGE.list_documents()}


@router.get("/knowledge/search")
async def api_knowledge_search(q: str = "") -> dict[str, Any]:
    return {"results": KNOWLEDGE.search(q)}


@router.delete("/knowledge/{doc_id}")
async def api_knowledge_delete(doc_id: str) -> dict[str, Any]:
    return {"deleted": KNOWLEDGE.delete_document(doc_id)}


# --- Backend control ---

@router.post("/run")
async def api_run_agent() -> dict[str, Any]:
    return _set_running("social", True)


@router.post("/pause")
async def api_pause_agent() -> dict[str, Any]:
    return _set_running("social", False)


@router.get("/running")
async def api_running() -> dict[str, Any]:
    return {"running": _is_running("social"), "dry_run": get_dry_run()}


# --- Reconciliation ---

class ReconciliationRequest(BaseModel):
    status: str
    external_id: str = ""
    message: str = ""


@router.post("/jobs/{job_id}/reconcile")
async def api_job_reconcile(job_id: str, payload: ReconciliationRequest) -> dict[str, Any]:
    from app.database.database import SessionLocal
    from app.database.models import SocialPlatformJob

    db = SessionLocal()
    try:
        job = db.query(SocialPlatformJob).filter(SocialPlatformJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail=f"job {job_id} not found")
        if job.status != "uncertain":
            raise HTTPException(status_code=422, detail=f"job is {job.status}; only uncertain jobs can be reconciled")
        outcome = payload.status.strip().lower()
        if outcome not in {"published", "failed", "not_published"}:
            raise HTTPException(status_code=422, detail="status must be one of published / failed / not_published")
        job.status = "success" if outcome == "published" else "failed"
        job.result = json.dumps({
            "reconciled": True,
            "status": outcome,
            "external_id": payload.external_id.strip() or None,
            "message": payload.message.strip() or None,
        })
        job.error = (
            None if outcome == "published"
            else payload.message.strip() or job.error or "reconciled as failed"
        )
        db.commit()
        log.info("Reconciled job %s -> %s (external_id=%r)", job_id, outcome, payload.external_id or None)
        return {
            "id": job.id,
            "status": job.status,
            "external_id": payload.external_id.strip() or None,
            "message": payload.message.strip() or None,
        }
    finally:
        db.close()
