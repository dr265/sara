"""WhatsApp Numbers / Channels manager.

Multi-channel WhatsApp command-center support for the Sara AI platform.

  * Channels live in the DB (whatsapp_channels) with Fernet-encrypted
    credentials — no plaintext API keys at rest. The Fernet key is derived
    from SARA_MASTER_KEY / WEBHOOK_SECRET in backend/.env.
  * Providers: meta (WhatsApp Cloud API), zernio, unipile, twilio, generic
    webhook. Each implements: test(), send_text(), send_interactive(),
    verify_webhook(), parse_webhook() — adding a new provider later means
    registering one class here, nothing else.
  * Exactly one channel is the Primary Command Center. Its inbound messages
    route to the Sara agent and replies go out through that same channel.
  * The poller (app.core.agent_pollers) and the webhook routes
    (app.api.whatsapp_channels) both read the store through this module.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger("sara.whatsapp.channels")

#: Process-level flag so a persistent key mismatch cannot flood the log
#: (once produced ~35k warning lines in a single run).
_DECRYPT_FAIL_LOGGED = False

PUBLIC_BASE_URL = (os.getenv("SARA_PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")

PROVIDERS = ("meta", "zernio", "unipile", "twilio", "kapso", "generic")
PROVIDER_LABELS = {
    "meta": "Meta WhatsApp Cloud API",
    "zernio": "Zernio",
    "unipile": "Unipile",
    "twilio": "Twilio",
    "kapso": "Kapso",
    "generic": "Generic webhook / provider",
}

# Per-provider credential fields (used by the setup wizard and validation).
PROVIDER_FIELDS: dict[str, list[str]] = {
    "meta": ["access_token", "phone_number_id", "app_id", "app_secret", "business_account_id", "verify_token"],
    "zernio": ["api_key", "account_id", "base_url", "webhook_secret"],
    "unipile": ["dsn", "access_token", "account_id", "webhook_secret"],
    "twilio": ["account_sid", "auth_token", "phone_number"],
    "kapso": ["api_key", "phone_number_id", "webhook_secret"],
    "generic": ["webhook_secret", "send_url", "send_bearer_token", "secret_header"],
}

GRAPH_VERSION = "v21.0"


# --------------------------------------------------------------------------- #
# Credential encryption (Fernet, key derived from env master secret)
# --------------------------------------------------------------------------- #
def _fernet():
    from cryptography.fernet import Fernet

    master = (os.getenv("SARA_MASTER_KEY") or os.getenv("WEBHOOK_SECRET") or "sara-channel-master").encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(master).digest())
    return Fernet(key)


def encrypt_json(obj: Any) -> str:
    return _fernet().encrypt(json.dumps(obj).encode()).decode()


def decrypt_json(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except Exception:
        # Fires on every stored-credential access; a key mismatch once
        # produced ~35k warnings in a single run, so log the remediation
        # hint once per process and stay quiet afterwards.
        global _DECRYPT_FAIL_LOGGED
        if not _DECRYPT_FAIL_LOGGED:
            _DECRYPT_FAIL_LOGGED = True
            log.warning(
                "Failed to decrypt channel credentials (key changed?) - "
                "re-save the channel credentials or restore "
                "SARA_MASTER_KEY/WEBHOOK_SECRET; further failures in this "
                "process are logged at debug level only"
            )
        else:
            log.debug("Failed to decrypt channel credentials (key changed?)")
        return {}


# --------------------------------------------------------------------------- #
# Inbound message shape
# --------------------------------------------------------------------------- #
@dataclass
class InboundMessage:
    message_id: str
    chat_id: str
    text: str
    sender_name: str = ""
    sender_number: str = ""


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class ChannelStore:
    """CRUD over whatsapp_channels via the unified backend DB."""

    def __init__(self) -> None:
        self._seed_env()

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _row_to_dict(row: Any) -> dict:
        return {
            "id": row.id,
            "name": row.name,
            "provider": row.provider,
            "phone_number": row.phone_number or "",
            "status": row.status or "disconnected",
            "enabled": bool(row.enabled),
            "is_primary": bool(row.is_primary),
            "last_error": row.last_error or "",
            "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
            "webhook_url": f"{PUBLIC_BASE_URL}/whatsapp/channels/{row.id}/webhook",
            "credentials_set": list(decrypt_json(row.credentials_enc).keys()),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _seed_env(self) -> None:
        """First run: mirror the env-configured transport as a channel so the
        existing setup shows up in the UI without any manual steps."""
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        try:
            db = SessionLocal()
            try:
                if db.query(WhatsAppChannel).count() > 0:
                    return
            finally:
                db.close()
        except Exception:
            return
        creds: dict[str, Any] = {}
        provider, phone = "", ""
        if os.getenv("KAPSO_API_KEY"):
            # Kapso first (explicit env): project key + connected phone number id.
            provider = "kapso"
            creds = {
                "api_key": os.getenv("KAPSO_API_KEY", ""),
                "phone_number_id": os.getenv("KAPSO_PHONE_NUMBER_ID", ""),
                "webhook_secret": os.getenv("KAPSO_WEBHOOK_SECRET", ""),
            }
            phone = ""
        elif os.getenv("WA_MESSENGER", "unipile").strip().lower() == "zernio" and os.getenv("ZERNIO_API_KEY"):
            provider = "zernio"
            creds = {
                "api_key": os.getenv("ZERNIO_API_KEY", ""),
                "account_id": os.getenv("ZERNIO_ACCOUNT_ID", ""),
                "base_url": os.getenv("ZERNIO_BASE_URL", "https://zernio.com/api/v1"),
                "webhook_secret": os.getenv("ZERNIO_WEBHOOK_SECRET", ""),
            }
        elif os.getenv("UNIPILE_DSN") and os.getenv("UNIPILE_ACCESS_TOKEN"):
            provider = "unipile"
            creds = {
                "dsn": os.getenv("UNIPILE_DSN", ""),
                "access_token": os.getenv("UNIPILE_ACCESS_TOKEN", ""),
                "account_id": os.getenv("UNIPILE_ACCOUNT_ID", ""),
                "webhook_secret": os.getenv("WEBHOOK_SECRET", ""),
            }
        if provider:
            try:
                self.create(name=("Kapso (env)" if provider == "kapso" else "Env transport"), provider=provider, phone_number=phone, credentials=creds, primary=True)
                log.info("Seeded WhatsApp channel from env (%s)", provider)
            except Exception as exc:
                log.warning("Env channel seed failed: %r", exc)

    # -- CRUD -------------------------------------------------------------- #
    def list(self) -> list[dict]:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            rows = db.query(WhatsAppChannel).order_by(WhatsAppChannel.created_at).all()
            return [self._row_to_dict(r) for r in rows]
        finally:
            db.close()

    def get(self, channel_id: str) -> Optional[dict]:
        for row in self.list():
            if row["id"] == channel_id:
                return row
        return None

    def get_creds(self, channel_id: str) -> tuple[dict, dict]:
        """Return (row_dict, decrypted_credentials) for a channel."""
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            row = db.query(WhatsAppChannel).filter(WhatsAppChannel.id == channel_id).first()
            if row is None:
                return {}, {}
            return self._row_to_dict(row), decrypt_json(row.credentials_enc)
        finally:
            db.close()

    def create(
        self,
        name: str,
        provider: str,
        phone_number: str = "",
        credentials: Optional[dict] = None,
        primary: bool = False,
    ) -> dict:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider '{provider}' (choose from {', '.join(PROVIDERS)})")
        db = SessionLocal()
        try:
            if primary:
                db.query(WhatsAppChannel).update({WhatsAppChannel.is_primary: 0})
            row = WhatsAppChannel(
                name=name or f"{PROVIDER_LABELS.get(provider, provider)} channel",
                provider=provider,
                phone_number=phone_number or "",
                credentials_enc=encrypt_json(credentials or {}),
                status="connecting",
                is_primary=1 if primary else 0,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._row_to_dict(row)
        finally:
            db.close()

    def update(
        self,
        channel_id: str,
        *,
        name: str | None = None,
        phone_number: str | None = None,
        credentials: dict | None = None,
        enabled: bool | None = None,
    ) -> dict:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            row = db.query(WhatsAppChannel).filter(WhatsAppChannel.id == channel_id).first()
            if row is None:
                raise KeyError(f"channel {channel_id} not found")
            if name is not None:
                row.name = name
            if phone_number is not None:
                row.phone_number = phone_number
            if credentials:
                merged = decrypt_json(row.credentials_enc)
                merged.update({k: v for k, v in credentials.items() if v not in (None, "")})
                row.credentials_enc = encrypt_json(merged)
            if enabled is not None:
                row.enabled = 1 if enabled else 0
            db.commit()
            db.refresh(row)
            return self._row_to_dict(row)
        finally:
            db.close()

    def set_status(self, channel_id: str, status: str, error: str = "") -> None:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            row = db.query(WhatsAppChannel).filter(WhatsAppChannel.id == channel_id).first()
            if row is None:
                return
            row.status = status
            row.last_error = error[:500] if error else None
            row.last_checked_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

    def set_primary(self, channel_id: str) -> dict:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            row = db.query(WhatsAppChannel).filter(WhatsAppChannel.id == channel_id).first()
            if row is None:
                raise KeyError(f"channel {channel_id} not found")
            db.query(WhatsAppChannel).update({WhatsAppChannel.is_primary: 0})
            row.is_primary = 1
            row.enabled = 1
            db.commit()
            db.refresh(row)
            return self._row_to_dict(row)
        finally:
            db.close()

    def delete(self, channel_id: str) -> bool:
        from app.database.database import SessionLocal
        from app.database.models import WhatsAppChannel

        db = SessionLocal()
        try:
            row = db.query(WhatsAppChannel).filter(WhatsAppChannel.id == channel_id).first()
            if row is None:
                return False
            db.delete(row)
            db.commit()
            return True
        finally:
            db.close()


store = ChannelStore()


def ensure_seeded() -> None:
    """Idempotent — call after tables exist (startup) so the env transport is
    mirrored as a channel on first run even though the module imported earlier."""
    store._seed_env()


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
class Provider:
    """Base provider interface. Subclasses implement the five operations."""

    name = "base"

    def __init__(self, creds: dict, phone_number: str = "") -> None:
        self.creds = creds
        self.phone_number = phone_number

    async def test(self) -> dict:
        raise NotImplementedError

    async def send_text(self, chat_id: str, text: str) -> None:
        raise NotImplementedError

    async def send_interactive(self, chat_id: str, text: str, options: list[str]) -> bool:
        """Send tappable options. Return True when delivered natively."""
        return False

    async def verify_webhook(self, request: Any) -> bool:
        raise NotImplementedError

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0), **kw)


class MetaProvider(Provider):
    """WhatsApp Cloud API (graph.facebook.com)."""

    name = "meta"
    GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

    @property
    def phone_number_id(self) -> str:
        return str(self.creds.get("phone_number_id", ""))

    async def test(self) -> dict:
        token = self.creds.get("access_token", "")
        if not token or not self.phone_number_id:
            return {"ok": False, "error": "access_token and phone_number_id are required"}
        try:
            async with _client() as c:
                resp = await c.get(
                    f"{self.GRAPH}/{self.phone_number_id}",
                    params={"fields": "display_phone_number,verified_name,quality_rating", "access_token": token},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "ok": True,
                    "detail": f"{data.get('verified_name', '')} · {data.get('display_phone_number', '')} (quality {data.get('quality_rating', 'n/a')})",
                }
            return {"ok": False, "error": f"Graph API {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def _post_message(self, to: str, body: dict) -> None:
        token = self.creds.get("access_token", "")
        if not token or not self.phone_number_id:
            raise RuntimeError("Meta channel missing access_token / phone_number_id")
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **body}
        async with _client() as c:
            resp = await c.post(f"{self.GRAPH}/{self.phone_number_id}/messages", json=payload, params={"access_token": token})
        if resp.status_code >= 400:
            raise RuntimeError(f"Meta send {resp.status_code}: {resp.text[:300]}")

    async def send_text(self, chat_id: str, text: str) -> None:
        await self._post_message(chat_id, {"type": "text", "text": {"body": text, "preview_url": False}})

    async def send_interactive(self, chat_id: str, text: str, options: list[str]) -> bool:
        if not options or len(options) > 5:
            return False
        rows = []
        for opt in options:
            label = " ".join(str(opt).split())[:24]
            rows.append({"id": f"row{len(rows)}", "title": label})
        try:
            await self._post_message(
                chat_id,
                {
                    "type": "interactive",
                    "interactive": {
                        "type": "list",
                        "header": {"type": "text", "text": "Choose an option"},
                        "body": {"text": (text or "Please choose:")[:1024]},
                        "action": {"button": "Choose", "sections": [{"title": "Options", "rows": rows}]},
                    },
                },
            )
            return True
        except Exception as exc:
            log.warning("Meta interactive send failed (%r) — text fallback", exc)
            return False

    async def verify_webhook(self, request: Any) -> bool:
        if request.method == "GET":
            # Meta subscription handshake: hub.challenge echoed back on success.
            params = dict(request.query_params)
            if params.get("hub.mode") != "subscribe":
                return False
            expected = self.creds.get("verify_token", "")
            return bool(expected) and hmac.compare_digest(str(params.get("hub.verify_token", "")), expected)
        # POST: X-Hub-Signature-256 = "sha256=" + hex(HMAC-SHA256(raw body, app_secret))
        secret = self.creds.get("app_secret", "")
        if not secret:
            return True  # no app_secret stored → accept (handshake already gates the endpoint)
        raw = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        computed = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return bool(sig) and hmac.compare_digest(computed, sig.strip().lower())

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        try:
            entry = (payload.get("entry") or [{}])[0]
            change = (entry.get("changes") or [{}])[0]
            value = change.get("value") or {}
            msg = (value.get("messages") or [None])[0]
            if not msg:
                return None
            msg_type = msg.get("type", "text")
            text = ""
            if msg_type == "text":
                text = (msg.get("text") or {}).get("body", "")
            elif msg_type == "interactive":
                inter = msg.get("interactive") or {}
                reply = inter.get("button_reply") or inter.get("list_reply") or {}
                text = reply.get("title") or reply.get("description") or ""
            elif msg_type == "button":
                text = (msg.get("button") or {}).get("text", "")
            contact = (value.get("contacts") or [{}])[0].get("profile") or {}
            return InboundMessage(
                message_id=str(msg.get("id") or ""),
                chat_id=str(msg.get("from") or ""),
                text=str(text or "").strip(),
                sender_name=str(contact.get("name") or ""),
                sender_number=str(msg.get("from") or ""),
            )
        except Exception:
            return None


class ZernioProvider(Provider):
    name = "zernio"

    def _base(self) -> str:
        return (self.creds.get("base_url") or "https://zernio.com/api/v1").rstrip("/")

    async def test(self) -> dict:
        key = self.creds.get("api_key", "")
        if not key:
            return {"ok": False, "error": "api_key is required"}
        try:
            async with _client() as c:
                resp = await c.get(f"{self._base()}/accounts", headers={"Authorization": f"Bearer {key}", "accept": "application/json"})
            if resp.status_code in (200, 201):
                return {"ok": True, "detail": "Zernio API key accepted"}
            return {"ok": False, "error": f"Zernio {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def send_text(self, chat_id: str, text: str) -> None:
        key, aid = self.creds.get("api_key", ""), self.creds.get("account_id", "")
        if not key or not aid:
            raise RuntimeError("Zernio channel missing api_key / account_id")
        async with _client() as c:
            resp = await c.post(
                f"{self._base()}/inbox/conversations/{chat_id}/messages",
                json={"accountId": aid, "message": text},
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Zernio send {resp.status_code}: {resp.text[:300]}")

    async def send_interactive(self, chat_id: str, text: str, options: list[str]) -> bool:
        key, aid = self.creds.get("api_key", ""), self.creds.get("account_id", "")
        if not key or not aid:
            return False
        from app.agents.sara_whatsapp import build_interactive_payload

        payload = build_interactive_payload(text, options)
        if not payload:
            return False
        body = {"accountId": aid, **payload}
        try:
            async with _client() as c:
                resp = await c.post(
                    f"{self._base()}/inbox/conversations/{chat_id}/messages",
                    json=body,
                    headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Zernio interactive {resp.status_code}: {resp.text[:200]}")
            return True
        except Exception as exc:
            log.warning("Zernio interactive send failed (%r) — text fallback", exc)
            return False

    async def verify_webhook(self, request: Any) -> bool:
        secret = self.creds.get("webhook_secret", "")
        if not secret:
            return True  # no secret configured → accept (polling is the primary intake anyway)
        raw = await request.body()
        sig = request.headers.get("x-zernio-signature", "")
        computed = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return bool(sig) and hmac.compare_digest(computed, sig.strip().lower())

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        from app.agents.sara_whatsapp import parse_zernio_event

        event = parse_zernio_event(payload if isinstance(payload, dict) else {})
        if not event:
            return None
        return InboundMessage(
            message_id=str(event.get("message_id") or ""),
            chat_id=str(event.get("chat_id") or ""),
            text=str(event.get("text") or ""),
            sender_name=str(event.get("sender_name") or ""),
            sender_number=str(event.get("sender_number") or ""),
        )


class UnipileProvider(Provider):
    name = "unipile"

    def _dsn(self) -> str:
        return (self.creds.get("dsn") or "").rstrip("/")

    async def test(self) -> dict:
        dsn, token = self._dsn(), self.creds.get("access_token", "")
        if not dsn or not token:
            return {"ok": False, "error": "dsn and access_token are required"}
        try:
            async with _client() as c:
                resp = await c.get(f"{dsn}/api/v1/accounts", headers={"X-API-KEY": token, "accept": "application/json"})
            if resp.status_code == 200:
                return {"ok": True, "detail": "Unipile API key accepted"}
            return {"ok": False, "error": f"Unipile {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def send_text(self, chat_id: str, text: str) -> None:
        dsn, token = self._dsn(), self.creds.get("access_token", "")
        if not dsn or not token:
            raise RuntimeError("Unipile channel missing dsn / access_token")
        async with _client() as c:
            resp = await c.post(
                f"{dsn}/api/v1/chats/{chat_id}/messages",
                files={"text": (None, text)},
                headers={"X-API-KEY": token, "accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Unipile send {resp.status_code}: {resp.text[:300]}")

    async def verify_webhook(self, request: Any) -> bool:
        secret = self.creds.get("webhook_secret", "")
        if not secret:
            return True
        sent = request.headers.get("x-webhook-secret", "")
        return bool(sent) and hmac.compare_digest(sent, secret)

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        from app.agents.sara_whatsapp import parse_unipile_event

        event = parse_unipile_event(payload if isinstance(payload, dict) else {})
        if not event:
            return None
        return InboundMessage(
            message_id=str(event.get("message_id") or ""),
            chat_id=str(event.get("chat_id") or ""),
            text=str(event.get("text") or ""),
            sender_name=str(event.get("sender_name") or ""),
            sender_number=str(event.get("sender_number") or ""),
        )


class TwilioProvider(Provider):
    name = "twilio"
    API = "https://api.twilio.com/2010-04-01"

    async def test(self) -> dict:
        sid, token = self.creds.get("account_sid", ""), self.creds.get("auth_token", "")
        if not sid or not token:
            return {"ok": False, "error": "account_sid and auth_token are required"}
        try:
            async with _client() as c:
                resp = await c.get(f"{self.API}/Accounts/{sid}.json", auth=(sid, token))
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "detail": f"Twilio account {data.get('friendly_name', sid)}"}
            return {"ok": False, "error": f"Twilio {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def send_text(self, chat_id: str, text: str) -> None:
        sid, token = self.creds.get("account_sid", ""), self.creds.get("auth_token", "")
        if not sid or not token:
            raise RuntimeError("Twilio channel missing account_sid / auth_token")
        fr = self.creds.get("phone_number") or self.phone_number
        if not fr:
            raise RuntimeError("Twilio channel missing phone_number (the connected number)")
        async with _client() as c:
            resp = await c.post(
                f"{self.API}/Accounts/{sid}/Messages.json",
                data={"From": fr, "To": chat_id, "Body": text},
                auth=(sid, token),
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Twilio send {resp.status_code}: {resp.text[:300]}")

    async def verify_webhook(self, request: Any) -> bool:
        token = self.creds.get("auth_token", "")
        if not token:
            return True
        sig = request.headers.get("x-twilio-signature", "")
        if not sig:
            return False
        form = await request.form()
        url = str(request.url)
        params = {k: v for k, v in form.items()}
        # Twilio signs the URL + sorted params (list values joined) with HMAC-SHA1.
        flat = {k: v for k, v in params.items()}
        joined = url + "".join(f"{k}{flat[k]}" for k in sorted(flat))
        expected = base64.b64encode(hmac.new(token.encode(), joined.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(expected, sig)

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        form = await request.form()
        body = str(form.get("Body") or "").strip()
        if not body:
            return None
        return InboundMessage(
            message_id=str(form.get("MessageSid") or ""),
            chat_id=str(form.get("From") or ""),
            text=body,
            sender_name=str(form.get("ProfileName") or ""),
            sender_number=str(form.get("From") or ""),
        )


class GenericProvider(Provider):
    """Bring-your-own webhook: provider POSTs JSON to our webhook URL.

    Inbound JSON: {"message_id"/"id", "text"/"body"/"message",
                   "from"/"sender"/"phone", "name"/"sender_name"}.
    Outbound (if send_url set): POST {"to": ..., "text": ...} with
    Authorization: Bearer <send_bearer_token>.
    """

    name = "generic"

    async def test(self) -> dict:
        if self.creds.get("send_url"):
            try:
                async with _client() as c:
                    resp = await c.get(
                        self.creds["send_url"],
                        headers=self._headers(),
                        follow_redirects=True,
                    )
                return {"ok": True, "detail": f"send_url reachable (HTTP {resp.status_code})"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
        return {"ok": True, "detail": "Webhook-only channel (no send_url); inbound will work"}

    def _headers(self) -> dict:
        headers = {"accept": "application/json"}
        if self.creds.get("send_bearer_token"):
            headers["Authorization"] = f"Bearer {self.creds['send_bearer_token']}"
        return headers

    async def send_text(self, chat_id: str, text: str) -> None:
        url = self.creds.get("send_url", "")
        if not url:
            raise RuntimeError("Generic channel has no send_url configured — outbound not possible")
        async with _client() as c:
            resp = await c.post(url, json={"to": chat_id, "text": text}, headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"Generic send {resp.status_code}: {resp.text[:300]}")

    async def verify_webhook(self, request: Any) -> bool:
        secret = self.creds.get("webhook_secret", "")
        if not secret:
            return True
        header_name = self.creds.get("secret_header") or "x-webhook-secret"
        sent = request.headers.get(header_name, "") or request.headers.get("x-webhook-secret", "")
        return bool(sent) and hmac.compare_digest(sent, secret)

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        try:
            raw = await request.body()
            payload = json.loads(raw) if raw else {}
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text") or payload.get("body") or payload.get("message") or "").strip()
        if not text:
            return None
        mid = str(payload.get("message_id") or payload.get("id") or payload.get("messageId") or "")
        if not mid:
            return None
        sender = str(payload.get("from") or payload.get("sender") or payload.get("phone") or payload.get("wa_id") or "")
        return InboundMessage(
            message_id=mid,
            chat_id=sender,
            text=text,
            sender_name=str(payload.get("name") or payload.get("sender_name") or ""),
            sender_number=sender,
        )


class KapsoProvider(Provider):
    """Kapso (https://app.kapso.ai) — WhatsApp Cloud API-compatible gateway.

    Kapso exposes Meta's WhatsApp Cloud API surface at
    https://api.kapso.ai/meta/whatsapp/v24.0/{phone_number_id}/messages with
    project API keys passed in the X-API-Key header (no Bearer token).
    Inbound events arrive as Kapso webhooks:
        X-Webhook-Event: whatsapp.message.received
        X-Webhook-Signature: hmac-sha256(raw_body, webhook_secret)
    payload: {message:{id, type, from, text:{body}, kapso:{direction, content}},
              conversation:{id, contact_name, phone_number}, phone_number_id}
    Docs: https://docs.kapso.ai
    """

    name = "kapso"
    API = "https://api.kapso.ai/meta/whatsapp/v24.0"

    @property
    def phone_number_id(self) -> str:
        return str(self.creds.get("phone_number_id", ""))

    async def test(self) -> dict:
        key = self.creds.get("api_key", "")
        if not key:
            return {"ok": False, "error": "api_key is required"}
        try:
            async with _client() as c:
                if self.phone_number_id:
                    resp = await c.get(
                        f"https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/{self.phone_number_id}",
                        headers={"X-API-Key": key, "accept": "application/json"},
                    )
                else:
                    resp = await c.get(
                        "https://api.kapso.ai/platform/v1/whatsapp/phone_numbers",
                        headers={"X-API-Key": key, "accept": "application/json"},
                    )
            if resp.status_code == 200:
                data = resp.json()
                if self.phone_number_id:
                    pn = data.get("data") or data or {}
                    return {
                        "ok": True,
                        "detail": (
                            f"Kapso number {pn.get('display_phone_number', '')} "
                            f"({pn.get('display_name', '')}) — key valid"
                        ),
                    }
                items = (data.get("data") or [])
                names = ", ".join(
                    str(p.get("display_phone_number") or p.get("name") or "?") for p in items[:5]
                )
                return {
                    "ok": True,
                    "detail": f"Kapso key valid — {len(items)} number(s): {names}",
                }
            return {"ok": False, "error": f"Kapso {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def send_text(self, chat_id: str, text: str) -> None:
        key, pid = self.creds.get("api_key", ""), self.phone_number_id
        if not key or not pid:
            raise RuntimeError("Kapso channel missing api_key / phone_number_id")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": "text",
            "text": {"body": text, "preview_url": False},
        }
        async with _client() as c:
            resp = await c.post(
                f"{self.API}/{pid}/messages",
                json=payload,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Kapso send {resp.status_code}: {resp.text[:300]}")

    async def verify_webhook(self, request: Any) -> bool:
        # Kapso signs deliveries with X-Webhook-Signature (HMAC-SHA256 of the raw
        # body using the webhook secret). No secret stored -> accept (the UI is
        # expected to store one for production traffic).
        secret = self.creds.get("webhook_secret", "")
        if not secret:
            return True
        raw = await request.body()
        sig = request.headers.get("x-webhook-signature", "")
        if not sig:
            return False
        computed = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(computed, sig.strip().lower())
        except Exception:
            return False

    async def parse_webhook(self, request: Any) -> InboundMessage | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        # Batched (buffered) deliveries carry {batch: true, data: [...]}
        if payload.get("batch") and isinstance(payload.get("data"), list):
            for item in payload["data"]:
                parsed = self._parse_single(item if isinstance(item, dict) else {})
                if parsed:
                    return parsed
            return None
        return self._parse_single(payload)

    def _parse_single(self, payload: dict) -> InboundMessage | None:
        msg = payload.get("message") or {}
        kapso = msg.get("kapso") or {}
        if kapso.get("direction") and kapso["direction"] != "inbound":
            return None  # ignore our own outbound echoes
        text = ""
        if isinstance(msg.get("text"), dict):
            text = str(msg["text"].get("body") or "")
        text = text or str(kapso.get("content") or "")
        if not text.strip():
            return None
        sender = str(
            msg.get("from")
            or (payload.get("conversation") or {}).get("phone_number")
            or ""
        )
        if not sender:
            return None
        return InboundMessage(
            message_id=str(msg.get("id") or payload.get("phone_number_id") or ""),
            chat_id=sender,
            text=text.strip(),
            sender_name=str((payload.get("conversation") or {}).get("contact_name") or ""),
            sender_number=sender,
        )


PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "meta": MetaProvider,
    "zernio": ZernioProvider,
    "unipile": UnipileProvider,
    "twilio": TwilioProvider,
    "kapso": KapsoProvider,
    "generic": GenericProvider,
}


def get_provider(channel_id: str) -> tuple[dict, Provider]:
    """Load a channel row + a ready provider instance (credentials decrypted)."""
    row, creds = store.get_creds(channel_id)
    if not row:
        raise KeyError(f"channel {channel_id} not found")
    cls = PROVIDER_CLASSES.get(row["provider"])
    if cls is None:
        raise ValueError(f"unknown provider {row['provider']}")
    return row, cls(creds, phone_number=row.get("phone_number", ""))


def missing_fields(channel_id: str) -> list[str]:
    """Credential fields required but not yet filled for this channel."""
    row, creds = store.get_creds(channel_id)
    required = set(PROVIDER_FIELDS.get(row.get("provider", ""), []))
    required.discard("webhook_secret")
    required.discard("app_id")
    required.discard("app_secret")
    required.discard("business_account_id")
    if row.get("provider") == "generic":
        required.discard("send_url")
        required.discard("send_bearer_token")
    return [f for f in required if not creds.get(f)]


# --------------------------------------------------------------------------- #
# Channel messenger (what the Sara agent uses to reply through the active channel)
# --------------------------------------------------------------------------- #
class ChannelMessenger:
    """Duck-typed drop-in for the agent's send path.

    The agent keeps `agent.channel_messenger`; when set, its send_text /
    _deliver calls route here and replies leave through the primary channel.
    """

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self._provider: Provider | None = None
        self._refresh()

    def _refresh(self) -> None:
        try:
            _, self._provider = get_provider(self.channel_id)
        except Exception as exc:
            log.warning("ChannelMessenger(%s) init failed: %r", self.channel_id, exc)
            self._provider = None

    async def send_text(self, chat_id: str, text: str) -> None:
        if self._provider is None:
            raise RuntimeError(f"channel {self.channel_id} unavailable")
        await self._provider.send_text(chat_id, text)

    async def send_interactive(self, chat_id: str, text: str, options: list[str]) -> bool:
        if self._provider is None:
            return False
        try:
            return await self._provider.send_interactive(chat_id, text, options)
        except Exception:
            return False


def activate_primary() -> ChannelMessenger | None:
    """Point the WhatsApp agent's outbound path at the primary channel.

    Returns the messenger (or None when no primary channel exists — the env
    transport keeps working then). Called at startup and after any primary
    switch so the change applies instantly, no restart needed.
    """
    from app.api.whatsapp_agent import agent

    messenger: ChannelMessenger | None = None
    for row in store.list():
        if row["is_primary"] and row["enabled"] and row["provider"] in PROVIDER_CLASSES:
            messenger = ChannelMessenger(row["id"])
            break
    agent.channel_messenger = messenger
    log.info("WhatsApp primary channel active: %s", messenger.channel_id if messenger else "(env transport)")
    return messenger


async def route_inbound(channel_id: str, event: InboundMessage) -> dict:
    """Dedupe + dispatch an inbound message to the Sara agent.

    Only the Primary Command Center routes to the agent; other channels are
    acknowledged but ignored so a switched number stops answering instantly.
    """
    from app.api.whatsapp_agent import agent
    from app.core.agent_switch import is_running

    row = store.get(channel_id)
    if not row:
        log.info("inbound ignored: unknown channel %s", channel_id)
        return {"status": "ignored", "reason": "unknown channel"}
    if not row["enabled"]:
        log.info("inbound ignored: channel %s disconnected", channel_id)
        return {"status": "ignored", "reason": "channel disconnected"}
    if not row["is_primary"]:
        log.info("inbound ignored: channel %s not primary", channel_id)
        return {"status": "ignored", "reason": "not the primary command center"}
    if not is_running("whatsapp"):
        return {"status": "paused"}
    if not event.message_id or agent.seen_messages.seen(event.message_id):
        log.info("inbound duplicate: %s", event.message_id)
        return {"status": "duplicate"}
    log.info("inbound accepted → agent (channel=%s, from=%s)", channel_id, event.sender_number or event.chat_id)
    await agent.handle_inbound(
        event.chat_id, event.text, event.sender_name, event.sender_number
    )
    return {"status": "accepted"}