import json, os, urllib.parse
from pathlib import Path
from app.database.database import SessionLocal
from app.database.models import SocialPlatformJob, SocialAccount

_META_VERSIONS = {
    "facebook":  os.getenv("SARA_FB_API_VERSION",   os.getenv("SOCIAL_FACEBOOK_API_VERSION",  "v21.0")),
    "instagram": os.getenv("SARA_IG_API_VERSION",   os.getenv("SOCIAL_INSTAGRAM_API_VERSION", "v21.0")),
    "threads":   os.getenv("SARA_TH_API_VERSION",   os.getenv("SOCIAL_THREADS_API_VERSION",   "v1.0")),
}

def oauth_redirect_uri(platform, workspace_id):
    # Exact prefix must match the route registration in app.main.
    base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    path = "/social" + "/oauth/" + platform + "/callback"
    return base + path


def exchange_oauth_code(platform, code):
    if platform == "facebook":
        return _exchange_facebook(platform, code)
    if platform == "threads":
        return _exchange_threads(platform, code)
    if platform == "pinterest":
        return _exchange_pinterest(platform, code)
    if platform == "tiktok":
        return _exchange_tiktok(platform, code)
    if platform == "linkedin":
        return _exchange_linkedin(platform, code)
    if platform == "x":
        return _exchange_x(platform, code)
    if platform == "youtube":
        return _exchange_youtube(platform, code)
    if platform in {"instagram"}:
        return _exchange_meta_via_facebook(platform, code)
    raise ValueError("unknown platform '%s'" % platform)


def _exchange_meta_via_facebook(platform, code):
    token = _exchange_platform(
        platform, code,
        "https://graph.facebook.com/" + _META_VERSIONS[platform] + "/oauth/access_token",
        {"grant_type": "authorization_code"},
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": "bearer",
        "expires_in": int(token.get("expires_in", 0) or 0),
        "platform": "facebook",
        "provider": "facebook",
        "raw_token_body": dict(token),
    }


def _exchange_facebook(platform, code):
    cfg = _oauth_config("facebook")
    token = _exchange_platform(
        platform, code,
        "https://graph.facebook.com/" + _META_VERSIONS["facebook"] + "/oauth/access_token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
        },
    )
    long_lived = _exchange_platform(
        platform, "",
        "https://graph.facebook.com/" + _META_VERSIONS["facebook"] + "/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "fb_exchange_token": token.get("access_token", ""),
        },
        tolerate_missing_code=True,
    )
    access_token = long_lived.get("access_token") or token.get("access_token", "")
    connections = []
    try:
        data = _http_get(
            "https://graph.facebook.com/" + _META_VERSIONS["facebook"] + "/me/accounts?access_token=" + urllib.parse.quote(access_token, safe=""),
            {"fields": "id,name,instagram_business_account"},
        )
        connections = data.get("data", [])
    except Exception as exc:
        pass
    if not connections:
        connections = [{"id": "0", "name": "connected-page"}]
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": int(long_lived.get("expires_in", token.get("expires_in", 0) or 0) or 0),
        "refresh_token": "",
        "provider": "facebook",
        "platform": "facebook",
        "connections": connections,
        "raw_token_body": dict(token),
    }


def _exchange_threads(platform, code):
    cfg = _oauth_config("threads")
    short = _exchange_platform(
        platform, code,
        "https://graph.threads.net/oauth/access_token",
        {
            "grant_type": "authorization_code",
            "client_key": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
            "state": "",
        },
    )
    long = _http_get(
        "https://graph.threads.net/access_token?" + urllib.parse.urlencode({
            "grant_type": "th_exchange_token",
            "access_token": short.get("access_token", ""),
            "client_secret": cfg["client_secret"],
        }),
        {},
    )
    return {
        "access_token": long.get("access_token", short.get("access_token", "")),
        "token_type": "bearer",
        "expires_in": int(long.get("expires_in", short.get("expires_in", 0) or 0) or 0),
        "provider": "threads",
        "platform": "threads",
        "raw_token_body": dict(long),
    }


def _exchange_pinterest(platform, code):
    cfg = _oauth_config("pinterest")
    token = _exchange_platform(
        platform, code,
        "https://api.pinterest.com/v5/oauth/token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
        },
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": "bearer",
        "expires_in": int(token.get("expires_in", 0) or 0),
        "refresh_token": token.get("refresh_token", ""),
        "provider": "pinterest",
        "platform": "pinterest",
        "raw_token_body": dict(token),
    }


def _exchange_tiktok(platform, code):
    cfg = _oauth_config("tiktok")
    token = _exchange_platform(
        platform, code,
        "https://open.tiktokapis.com/v2/oauth/token/",
        {
            "grant_type": "authorization_code",
            "client_key": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
        },
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": "Bearer",
        "expires_in": int(token.get("expires_in", 0) or 0),
        "refresh_token": token.get("refresh_token", ""),
        "scope": token.get("scope", ""),
        "provider": "tiktok",
        "platform": "tiktok",
        "raw_token_body": dict(token),
    }


def _exchange_linkedin(platform, code):
    cfg = _oauth_config("linkedin")
    token = _exchange_platform(
        platform, code,
        "https://www.linkedin.com/oauth/v2/accessToken",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
            "state": "",
        },
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": "Bearer",
        "expires_in": int(token.get("expires_in", 0) or 0),
        "refresh_token": token.get("refresh_token", ""),
        "scope": token.get("scope", ""),
        "provider": "linkedin",
        "platform": "linkedin",
        "raw_token_body": dict(token),
    }


def _exchange_x(platform, code):
    cfg = _oauth_config("x")
    token = _exchange_platform(
        platform, code,
        "https://api.x.com/2/oauth2/token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
            "state": "",
        },
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": int(token.get("expires_in", 0) or 0),
        "refresh_token": token.get("refresh_token", ""),
        "scope": token.get("scope", ""),
        "provider": "x",
        "platform": "x",
        "raw_token_body": dict(token),
    }


def _exchange_youtube(platform, code):
    cfg = _oauth_config("youtube")
    token = _exchange_platform(
        platform, code,
        "https://oauth2.googleapis.com/token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": oauth_redirect_uri(platform, "demo"),
            "code": code,
        },
    )
    return {
        "access_token": token.get("access_token", ""),
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": int(token.get("expires_in", 0) or 0),
        "refresh_token": token.get("refresh_token", ""),
        "scope": token.get("scope", ""),
        "provider": "youtube",
        "platform": "youtube",
        "raw_token_body": dict(token),
    }


def _exchange_platform(platform, code, url, body, tolerate_missing_code=False):
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    data = urllib.parse.urlencode({k: v for k, v in body.items() if v != ""})
    if not data and tolerate_missing_code:
        post_body = {}
    else:
        post_body = dict(body)
        if code:
            post_body["code"] = code

    # Fail closed when the platform key/secret are missing.
    cfg = _oauth_config(platform)
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise RuntimeError("platform " + platform + " is not configured (missing client_id/client_secret)")

    try:
        resp = _http_post(url, post_body, headers=headers)
    except Exception as exc:
        raise RuntimeError("token exchange failed: " + str(exc)[:200]) from exc

    if not isinstance(resp, dict):
        raise RuntimeError("token endpoint returned an invalid response")

    error = resp.get("error")
    if error:
        desc = str(error.get("message") or error.get("error_description") or str(error))[:200]
        raise RuntimeError(desc or "platform token error")

    return resp


def _oauth_config(platform):
    return {
        "client_id": os.getenv("SOCIAL_" + platform.upper() + "_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("SOCIAL_" + platform.upper() + "_CLIENT_SECRET", "").strip(),
    }


def _http_post(url, data, headers=None):
    import httpx
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        r = client.post(url, content=data.encode("utf-8"), headers=headers or {})
        try:
            return r.json()
        except Exception:
            return {"_raw_status": r.status_code, "_raw_text": r.text[:400]}


def _http_get(url, params):
    import httpx
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        r = client.get(url, params=params)
        try:
            return r.json()
        except Exception:
            return {"_raw_status": r.status_code, "_raw_text": r.text[:400]}


def add_account(workspace_id, platform, token_data):
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount

    merged = _normalize_token(platform, token_data)
    if not merged.get("access_token"):
        raise ValueError("no access_token in token exchange result")

    db = SessionLocal()
    try:
        if token_data.get("connections"):
            rows = []
            for item in token_data.get("connections", []):
                eid = str(item.get("id") or "").strip()
                if not eid:
                    continue
                key, val = _connection_key(platform, eid)
                acc = db.query(SocialAccount).filter(
                    SocialAccount.workspace_id == workspace_id,
                    SocialAccount.encrypted_token.like("%\"%s\":\"%s\"%%" % (key, val)),
                ).first()
                if not acc:
                    acc = SocialAccount(
                        workspace_id=workspace_id,
                        platform=platform,
                        account_name=_account_label(item, platform),
                        status="connected",
                        encrypted_token=_encrypt_text(json.dumps(_account_token(platform, item))),
                    )
                    db.add(acc)
                    rows.append(acc)
                else:
                    acc.status = "connected"
                    acc.last_error = None
                    acc.encrypted_token = _encrypt_text(json.dumps(_account_token(platform, item)))
                    rows.append(acc)
            if rows:
                db.commit()
                sid = rows[0].id
            else:
                acc = SocialAccount(
                    workspace_id=workspace_id,
                    platform=platform,
                    account_name=_account_label(token_data.get("connections", [{}])[0] if token_data.get("connections") else {}, platform),
                    status="connected",
                    encrypted_token=_encrypt_text(json.dumps(merged)),
                )
                db.add(acc)
                db.commit()
                sid = acc.id
        else:
            acc = SocialAccount(
                workspace_id=workspace_id,
                platform=platform,
                account_name=_account_label(token_data, platform),
                status="connected",
                encrypted_token=_encrypt_text(json.dumps(merged)),
            )
            db.add(acc)
            db.commit()
            sid = acc.id
        acc = db.query(SocialAccount).filter(SocialAccount.id == sid).first()
        return _account_view(acc, platform, db)
    finally:
        db.close()


def _normalize_token(platform, token_data):
    # Accept both the old style dicts and the new style dicts from the consolidated
    # exchange helpers above.
    out = {}
    if not isinstance(token_data, dict):
        token_data = {}
    for key in ("access_token", "refresh_token", "token_type", "expires_in", "scope", "provider", "platform"):
        val = token_data.get(key, "")
        out[key] = str(val) if val not in (None, "", ) and key != "expires_in" else int(val or 0)
    out["access_token"] = str(token_data.get("access_token", out.get("access_token", "")) or "")
    out["provider"] = str(token_data.get("provider") or token_data.get("platform") or platform or "")
    out["platform"] = str(token_data.get("platform") or platform or "")
    return out


def _account_token(platform, item):
    # For Meta platforms, store account-scoped tokens where the provider returns them.
    if platform == "facebook" and isinstance(item, dict):
        return {
            "access_token": str(item.get("access_token") or item.get("access_token", "")),
            "provider": "facebook",
            "platform": "facebook",
            "page_id": str(item.get("id", "")),
            "page_name": str(item.get("name", "")),
        }
    if platform == "instagram" and isinstance(item, dict):
        return {
            "access_token": str(item.get("access_token") or item.get("access_token", "")),
            "provider": "facebook",
            "platform": "instagram",
            "ig_user_id": str(item.get("id", "")),
        }
    # Generic fallback for any other platform.
    return {
        "access_token": str(item.get("access_token", "")),
        "refresh_token": str(item.get("refresh_token", "")),
        "provider": str(item.get("provider") or platform or ""),
        "platform": str(item.get("platform") or platform or ""),
    }


def _connection_key(platform, external_id):
    prefix = {"facebook": "page", "instagram": "ig"}.get(platform, platform)
    return prefix + "_id", str(external_id)


def _account_label(item, platform):
    if platform in {"facebook", "instagram", "threads"} and isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        if not name and platform == "instagram":
            name = "@" + str(item.get("username") or "")
        if not name:
            name = "@account"
        return name
    if platform == "linkedin" and isinstance(item, dict):
        return str(item.get("name") or "member")
    if platform == "x" and isinstance(item, dict):
        return "@" + str(item.get("username") or item.get("name") or "account")
    if platform == "pinterest" and isinstance(item, dict):
        return str(item.get("username") or item.get("name") or "account")
    if platform == "youtube" and isinstance(item, dict):
        return str(item.get("title") or item.get("name") or "channel")
    return "@" + platform


def _account_view(acc, platform, db):
    if not acc or not acc.id:
        return {}
    return {
        "id": acc.id,
        "platform": acc.platform,
        "platform_label": PLATFORM_LABELS.get(acc.platform, acc.platform),
        "account_name": acc.account_name,
        "status": acc.status or "connected",
        "last_error": acc.last_error or "",
        "workspace_id": acc.workspace_id,
        "credentials": acc.encrypted_token and True,
    }


def _encrypt_text(value):
    from cryptography.fernet import Fernet
    from app.agents.social_media import ENCRYPTION_KEY
    key = (ENCRYPTION_KEY * 4)[:32].encode("utf-8")
    f = Fernet(key)
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def list_accounts(workspace_id="demo"):
    db = SessionLocal()
    try:
        rows = db.query(SocialAccount)\
            .filter(SocialAccount.workspace_id == workspace_id)\
            .order_by(SocialAccount.created_at.desc())\
            .all()
        out = []
        for acc in rows:
            view = _account_view(acc, acc.platform, db)
            if not view:
                continue
            merged = _decrypt_text(acc.encrypted_token) if acc.encrypted_token else None
            if merged:
                for k in ("access_token", "refresh_token", "ig_user_id", "page_id", "threads_user_id", "board_id", "author_urn", "privacy_level", "api_version"):
                    if k in merged:
                        view[k] = merged[k]
            view["provider"] = view.get("provider") or acc.platform
            out.append(view)
        return out
    finally:
        db.close()


def _decrypt_text(token):
    if not token:
        return None
    try:
        from cryptography.fernet import Fernet
        from app.agents.social_media import ENCRYPTION_KEY
        key = (ENCRYPTION_KEY * 4)[:32].encode("utf-8")
        return json.loads(Fernet(key).decrypt(token.encode("utf-8")).decode("utf-8"))
    except Exception:
        return None


def _account_fields_from_token(token_data, platform):
    fields = {}
    if not isinstance(token_data, dict):
        return fields
    if platform == "instagram" and token_data.get("connections"):
        first = token_data["connections"][0]
        fields["ig_user_id"] = str(first.get("instagram_business_account", {}).get("id") or first.get("id") or "")
        fields["ig_username"] = str(first.get("instagram_business_account", {}).get("username") or "")
        fields["access_token"] = str(token_data.get("access_token", ""))
        return fields
    if platform == "facebook" and token_data.get("connections"):
        first = token_data["connections"][0]
        fields["page_id"] = str(first.get("id") or "")
        fields["page_name"] = str(first.get("name") or "")
        fields["access_token"] = str(token_data.get("access_token", ""))
        return fields
    if platform == "threads" and token_data.get("connections"):
        first = token_data["connections"][0]
        fields["threads_user_id"] = str(first.get("id") or first.get("user_id") or "")
        fields["access_token"] = str(token_data.get("access_token", ""))
        return fields
    if platform == "linkedin":
        fields["author_urn"] = "urn:li:person:" + str(token_data.get("sub") or "000000000")
        fields["access_token"] = str(token_data.get("access_token", ""))
        return fields
    if platform == "x":
        fields["access_token"] = str(token_data.get("access_token", ""))
        fields["username"] = str(token_data.get("username") or "")
        fields["refresh_token"] = str(token_data.get("refresh_token", ""))
        return fields
    if platform == "pinterest":
        fields["access_token"] = str(token_data.get("access_token", ""))
        fields["refresh_token"] = str(token_data.get("refresh_token", ""))
        fields["board_id"] = "0"
        return fields
    if platform == "youtube":
        fields["access_token"] = str(token_data.get("access_token", ""))
        fields["refresh_token"] = str(token_data.get("refresh_token", ""))
        fields["youtube_channel_id"] = str(token_data.get("sub") or "000000000")
        fields["youtube_channel_title"] = str(token_data.get("name") or "")
        return fields
    fields["access_token"] = str(token_data.get("access_token", ""))
    return fields


def _token_body_digest(platform, token_data):
    import hashlib
    raw = json.dumps({
        "platform": platform,
        "access_token": token_data.get("access_token", ""),
        "scope": token_data.get("scope", ""),
        "expires_in": token_data.get("expires_in", 0),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _update_account_credentials_from_token(account, token_data):
    import json
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount
    db = SessionLocal()
    try:
        current = db.query(SocialAccount).filter(SocialAccount.id == account["id"]).first()
        if not current:
            return False
        new = _account_fields_from_token(token_data, account["platform"])
        # Meta connections are account-scoped; re-derive the token on each callback so
        # short-lived page tokens never go stale underneath the long-lived one.
        if account["platform"] in {"facebook", "instagram", "threads"} and token_data.get("connections"):
            first = token_data["connections"][0]
            if account["platform"] == "facebook":
                new = {
                    "access_token": str(token_data.get("access_token", "")),
                    "page_id": str(first.get("id") or ""),
                    "page_name": str(first.get("name") or ""),
                    "provider": "facebook",
                }
            elif account["platform"] == "instagram":
                new = {
                    "access_token": str(token_data.get("access_token", "")),
                    "ig_user_id": str(first.get("instagram_business_account", {}).get("id") or first.get("id", "")),
                    "provider": "facebook",
                }
            elif account["platform"] == "threads":
                new = {
                    "access_token": str(token_data.get("access_token", "")),
                    "threads_user_id": str(first.get("id") or first.get("user_id") or ""),
                    "provider": "threads",
                }
        new["provider"] = account["platform"]
        new["platform"] = account["platform"]
        current.encrypted_token = _encrypt_text(json.dumps(new))
        current.status = "connected"
        current.last_error = None
        db.commit()
        return True
    finally:
        db.close()


def _accounts_from_token(platform, token_data, workspace_id):
    if not isinstance(token_data, dict):
        return []
    connections = token_data.get("connections") or []
    if not connections and token_data.get("access_token"):
        # Single-account provider fallback.
        leaf = {"id": token_data.get("user_id") or token_data.get("sub") or "account", "name": token_data.get("name") or platform}
        if platform == "youtube":
            leaf["id"] = token_data.get("sub") or "channel"
            leaf["name"] = token_data.get("name") or "channel"
        connections = [leaf]
    seen = set()
    out = []
    for item in connections:
        key = str(item.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        token = _account_token(platform, item)
        acc = SocialAccount(
            workspace_id=workspace_id,
            platform=platform,
            account_name=_account_label(item, platform),
            status="connected",
            encrypted_token=_encrypt_text(json.dumps(token)),
        )
        db = SessionLocal()
        try:
            existing = db.query(SocialAccount).filter(
                SocialAccount.workspace_id == workspace_id,
                SocialAccount.platform == platform,
                SocialAccount.encrypted_token.like("%\"%s\":\"%s\"%%" % _connection_key(platform, key)),
            ).first()
            if existing:
                existing.encrypted_token = _encrypt_text(json.dumps(token))
                existing.status = "connected"
                existing.last_error = None
                db.commit()
                out.append(_account_view(existing, platform, db))
            else:
                db.add(acc)
                db.commit()
                out.append(_account_view(acc, platform, db))
        finally:
            db.close()
    return out


def _existing_account_by_token(workspace_id, platform, token_data):
    import json
    db = SessionLocal()
    try:
        token = _account_token(platform, token_data)
        encoded = json.dumps(token, sort_keys=True, separators=(",", ":"))
        rows = db.query(SocialAccount).filter(
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.platform == platform,
        ).all()
        for row in rows:
            try:
                stored = json.loads(_decrypt_text(row.encrypted_token) or "{}")
                if (stored.get("access_token") == token.get("access_token")
                        and stored.get("page_id") == token.get("page_id")
                        and stored.get("ig_user_id") == token.get("ig_user_id")
                        and stored.get("threads_user_id") == token.get("threads_user_id")):
                    return row
            except Exception:
                continue
        return None
    finally:
        db.close()


def _upsert_account_from_token(workspace_id, platform, token_data):
    import json
    from app.database.database import SessionLocal
    from app.database.models import SocialAccount
    db = SessionLocal()
    try:
        token = _account_token(platform, token_data)
        encoded = json.dumps(token, sort_keys=True, separators=(",", ":"))
        existing = db.query(SocialAccount).filter(
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.platform == platform,
        ).order_by(SocialAccount.created_at.desc()).first()
        if existing and existing.encrypted_token:
            try:
                stored = json.loads(_decrypt_text(existing.encrypted_token) or "{}")
                if stored.get("access_token") == token.get("access_token"):
                    existing.encrypted_token = _encrypt_text(encoded)
                    existing.status = "connected"
                    existing.last_error = None
                    db.commit()
                    return _account_view(existing, platform, db)
            except Exception:
                pass
        acc = SocialAccount(
            workspace_id=workspace_id,
            platform=platform,
            account_name=_account_label(token_data, platform),
            status="connected",
            encrypted_token=_encrypt_text(encoded),
        )
        db.add(acc)
        db.commit()
        return _account_view(acc, platform, db)
    finally:
        db.close()
