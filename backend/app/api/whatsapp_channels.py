"""WhatsApp Numbers / Channels API.

Website-driven management of connected WhatsApp numbers:

  GET    /whatsapp/channels                list channels (credentials masked)
  GET    /whatsapp/channels/providers      wizard metadata per provider
  POST   /whatsapp/channels                create a channel (setup wizard)
  PATCH  /whatsapp/channels/{id}           rename / update credentials / disconnect
  DELETE /whatsapp/channels/{id}           remove a connection
  POST   /whatsapp/channels/{id}/test      validate the provider credentials
  POST   /whatsapp/channels/{id}/primary   make this number the command center
  GET/POST /whatsapp/channels/{id}/webhook provider webhook (verify + receive)

Webhook signatures are verified per provider (Meta X-Hub-Signature-256,
Twilio X-Twilio-Signature, Zernio X-Zernio-Signature, Unipile/generic
header secret). Only the Primary Command Center's messages reach the agent.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.services import whatsapp_channels as wc

log = logging.getLogger("sara.whatsapp.channels.routes")

router = APIRouter(prefix="/whatsapp/channels", tags=["WhatsApp Channels"])

WEBHOOK_HELP = {
    "meta": (
        "1. In the Meta Developer App → WhatsApp → Configuration, set Callback URL to the webhook URL below "
        "and Verify token to the verify_token you entered.\n"
        "2. Subscribe to the `messages` field.\n"
        "3. The GET verification below will complete automatically once Meta calls it."
    ),
    "zernio": "Zernio delivers webhooks to the URL below signed with X-Zernio-Signature. The background poller also works without it.",
    "unipile": "In Unipile dashboard, point your webhook at the URL below (X-Webhook-Secret header = the webhook_secret you entered).",
    "twilio": "In Twilio Console → Messaging → WhatsApp sender, set 'When a message comes in' to the webhook URL below (HTTP POST).",
    "kapso": "In app.kapso.ai open your number → Webhooks → Add Webhook (type 'Kapso (events)'), set Endpoint URL to the webhook URL below, "
    "paste the same webhook_secret you stored here, and subscribe to 'Message received' (optionally 'Message sent'). "
    "Kapso signs deliveries with X-Webhook-Signature (HMAC-SHA256 of the raw body using your secret).",
    "generic": "Point your provider's webhook at the URL below. It may send JSON with {message_id, text, from, name}; "
    "if it uses a different secret header, set secret_header.",
}


class KapsoWebhookRequest(BaseModel):
    url: str
    secret_key: str = ""
    events: list[str] = ["whatsapp.message.received"]
    active: bool = True


@router.post("/{channel_id}/kapso/register-webhook")
async def register_kapso_webhook(channel_id: str, payload: KapsoWebhookRequest) -> dict:
    """Register this backend's webhook URL with Kapso via their platform API
    (POST /platform/v1/whatsapp/phone_numbers/{phone_number_id}/webhooks).
    One click instead of manual dashboard configuration."""
    row, creds = wc.store.get_creds(channel_id)
    if not row or row.get("provider") != "kapso":
        raise HTTPException(status_code=404, detail="kapso channel not found")
    api_key = creds.get("api_key", "")
    phone_number_id = creds.get("phone_number_id", "")
    if not api_key or not phone_number_id:
        raise HTTPException(status_code=422, detail="channel missing api_key / phone_number_id")
    # Secret: request value wins, else the one stored on the channel (env seed).
    secret_key = payload.secret_key or creds.get("webhook_secret", "")
    if not secret_key:
        raise HTTPException(status_code=422, detail="no webhook secret: pass secret_key or store KAPSO_WEBHOOK_SECRET on the channel")
    import httpx

    body = {
        "whatsapp_webhook": {
            "kind": "kapso",
            "url": payload.url,
            "events": payload.events,
            "secret_key": secret_key,
            "active": payload.active,
        }
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.kapso.ai/platform/v1/whatsapp/phone_numbers/{phone_number_id}/webhooks",
                json=body,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Kapso API unreachable: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Kapso webhook registration failed: {resp.text[:300]}")
    return {"status": "registered", "kapso_response": resp.json()} 


class ChannelCreate(BaseModel):
    name: str = ""
    provider: str
    phone_number: str = ""
    credentials: dict = {}
    primary: bool = False


class ChannelUpdate(BaseModel):
    name: str | None = None
    phone_number: str | None = None
    credentials: dict | None = None
    enabled: bool | None = None


class PrimaryRequest(BaseModel):
    primary: bool = True


@router.get("/providers")
async def providers_info() -> dict:
    return {
        "providers": [
            {
                "id": p,
                "label": wc.PROVIDER_LABELS[p],
                "fields": wc.PROVIDER_FIELDS[p],
                "help": WEBHOOK_HELP.get(p, ""),
            }
            for p in wc.PROVIDERS
        ]
    }


@router.get("")
async def list_channels() -> dict:
    channels = wc.store.list()
    primary = next((c for c in channels if c["is_primary"]), None)
    return {
        "channels": channels,
        "primary": primary["id"] if primary else None,
        "public_base_url": wc.PUBLIC_BASE_URL,
        "agents_running": _agents_running(),
    }


def _agents_running() -> dict:
    from app.core.agent_switch import snapshot

    return snapshot()


@router.post("")
async def create_channel(payload: ChannelCreate) -> dict:
    if payload.provider not in wc.PROVIDERS:
        raise HTTPException(status_code=422, detail=f"unknown provider '{payload.provider}'")
    try:
        row = wc.store.create(
            name=payload.name,
            provider=payload.provider,
            phone_number=payload.phone_number,
            credentials=payload.credentials,
            primary=payload.primary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    wc.activate_primary()
    return {"status": "created", "channel": row}


@router.patch("/{channel_id}")
async def update_channel(channel_id: str, payload: ChannelUpdate) -> dict:
    try:
        row = wc.store.update(
            channel_id,
            name=payload.name,
            phone_number=payload.phone_number,
            credentials=payload.credentials,
            enabled=payload.enabled,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    if payload.enabled is not None:
        wc.activate_primary()
    return {"status": "updated", "channel": row}


@router.delete("/{channel_id}")
async def delete_channel(channel_id: str) -> dict:
    if not wc.store.delete(channel_id):
        raise HTTPException(status_code=404, detail="channel not found")
    wc.activate_primary()
    return {"status": "deleted", "id": channel_id}


@router.post("/{channel_id}/primary")
async def set_primary(channel_id: str) -> dict:
    try:
        row = wc.store.set_primary(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    wc.activate_primary()  # instant switch — no restart, no env edit
    return {"status": "primary", "channel": row}


@router.post("/{channel_id}/test")
async def test_channel(channel_id: str) -> dict:
    try:
        _, provider = wc.get_provider(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    result = await provider.test()
    status = "connected" if result.get("ok") else "error"
    wc.store.set_status(channel_id, status, "" if result.get("ok") else result.get("error", ""))
    return {"channel_id": channel_id, **result, "status": status}


@router.get("/{channel_id}/webhook")
async def webhook_verify(channel_id: str, request: Request) -> dict:
    """Provider verification handshake (Meta hub.challenge etc.)."""
    try:
        _, provider = wc.get_provider(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe":
        ok = await provider.verify_webhook(request)
        if not ok:
            raise HTTPException(status_code=403, detail="verify token mismatch")
        return {"hub.challenge": params.get("hub.challenge", "")}
    return {"status": "ok", "channel": channel_id, "note": "webhook endpoint live"}


@router.post("/{channel_id}/webhook")
async def webhook_receive(channel_id: str, request: Request, background: BackgroundTasks) -> dict:
    """Inbound message delivery from any provider (signature-verified)."""
    try:
        _, provider = wc.get_provider(channel_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="channel not found")
    if not await provider.verify_webhook(request):
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    event = await provider.parse_webhook(request)
    if event is None:
        return {"status": "ignored", "reason": "no routable message in payload"}

    async def dispatch() -> None:
        try:
            await wc.route_inbound(channel_id, event)
        except Exception as exc:  # never let webhook delivery crash the server
            log.exception("inbound dispatch failed: %r", exc)

    background.add_task(dispatch)
    return {"status": "accepted"}