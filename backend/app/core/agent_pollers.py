"""Background intake pollers for the Telegram and WhatsApp agents.

Why: a webhook requires a public URL. On this deployment the WhatsApp webhook
arrives through a throttled tunnel and the Telegram agent has no webhook at
all, so real guest messages can sit unread. These pollers pull inbound
messages directly and feed them through the same handlers the webhooks use.

Rules:
  * Each loop checks the persisted run/pause switch (app.core.agent_switch)
    every cycle — paused agents are simply not polled.
  * Telegram uses Bot API getUpdates (long-poll). Updates the agent has already
    seen are ignored internally (update-id dedupe), so restarts are safe.
  * WhatsApp (Zernio transport) lists inbox conversations and polls each
    conversation's newest messages. A per-conversation cursor file
    (<backend>/agent_whatsapp_cursor.json) means history is never replayed:
    the first time a conversation is seen its cursor is set to the newest
    message and only newer inbound messages are handled afterwards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("sara.pollers")

# Fast-poll defaults keep perceived latency near the LLM round-trip itself.
# Override via env (e.g. WHATSAPP_POLL_SECONDS=0.3) if your API plan allows.
POLL_INTERVAL_WHATSAPP = float(os.getenv("WHATSAPP_POLL_SECONDS", "1"))
POLL_INTERVAL_TELEGRAM = float(os.getenv("TELEGRAM_POLL_SECONDS", "1"))
CONVERSATION_REFRESH_SECONDS = float(
    os.getenv("WHATSAPP_CONVERSATION_REFRESH_SECONDS", "5")
)
TELEGRAM_LONG_POLL = 25              # getUpdates timeout (seconds)
CURSOR_FILE = Path(__file__).resolve().parent.parent.parent / "agent_whatsapp_cursor.json"

# Cached set of this agent's own conversations (id -> account_id). Re-fetched
# every CONVERSATION_REFRESH_SECONDS so brand-new chats are discovered, while
# the hot loop polls only known conversations' messages (1 fast API call).
_whatsapp_convs: dict[str, str] = {}
_last_conv_refresh = 0.0


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _load_cursor() -> dict:
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cursor(cursor: dict) -> None:
    tmp = CURSOR_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursor, indent=2), encoding="utf-8")
    tmp.replace(CURSOR_FILE)


# --------------------------------------------------------------------------- #
# Telegram: Bot API getUpdates long-poll
# --------------------------------------------------------------------------- #
_telegram_offset: int | None = None
_telegram_tenant: str | None = None


def _resolve_telegram_tenant() -> str | None:
    global _telegram_tenant
    if _telegram_tenant:
        return _telegram_tenant
    cand = (os.getenv("TELEGRAM_TENANT") or "").strip()
    if cand:
        _telegram_tenant = cand
        return cand
    try:
        from app.core.telegram_host import telegram_backend

        row = telegram_backend.db.one(
            "SELECT id FROM tenants ORDER BY created_at DESC LIMIT 1"
        )
        if row and row.get("id"):
            _telegram_tenant = str(row["id"])
            log.info("Telegram poller resolved tenant: %s", _telegram_tenant)
            return _telegram_tenant
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Telegram tenant resolution failed: %r", exc)
    return None


def reset_telegram_poller() -> None:
    """Drop the getUpdates offset so a newly authorized bot starts clean."""
    global _telegram_offset
    _telegram_offset = None


async def _telegram_poll_cycle(client: httpx.AsyncClient) -> None:
    global _telegram_offset
    from app.core.telegram_bot_token import get_bot_token

    token = get_bot_token()
    if not token:
        return
    from app.core.agent_switch import is_running

    if not is_running("telegram"):
        return
    tenant = _resolve_telegram_tenant()
    if not tenant:
        return

    params = {"timeout": TELEGRAM_LONG_POLL, "allowed_updates": '["message","edited_message"]'}
    if _telegram_offset is not None:
        params["offset"] = _telegram_offset
    resp = await client.get(f"/bot{token}/getUpdates", params=params)
    if resp.status_code == 401:
        log.error("Telegram getUpdates 401 — TELEGRAM_BOT_TOKEN invalid")
        await asyncio.sleep(30)
        return
    resp.raise_for_status()
    data = resp.json()
    updates = (data or {}).get("result") or []
    if not updates:
        return

    from app.core.telegram_host import telegram_backend

    max_id = _telegram_offset or 0
    failed_min: int | None = None
    for upd in updates:
        try:
            uid = int(upd.get("update_id") or 0)
            max_id = max(max_id, uid)
            # Let the agent's own duplicate guard decide; only pass chat updates.
            if any(k in upd for k in ("message", "edited_message", "channel_post", "callback_query")):
                await asyncio.to_thread(
                    telegram_backend.handle_telegram_update,
                    tenant,
                    upd,
                    "direct",
                )
        except Exception as exc:
            log.warning("telegram update %s failed: %r", upd.get("update_id"), exc)
            # Do NOT advance past a failed update — Telegram will redeliver it.
            failed_min = uid if failed_min is None else min(failed_min, uid)
    if failed_min is not None:
        _telegram_offset = failed_min  # retry from the first failure
        await asyncio.sleep(5)
    else:
        _telegram_offset = max_id + 1


async def telegram_poll_loop() -> None:
    while True:
        try:
            async with httpx.AsyncClient(
                base_url="https://api.telegram.org",
                timeout=httpx.Timeout(TELEGRAM_LONG_POLL + 15),
            ) as client:
                while True:
                    await _telegram_poll_cycle(client)
                    await asyncio.sleep(POLL_INTERVAL_TELEGRAM)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Telegram poller error: %r (retrying in 10s)", exc)
            await asyncio.sleep(10)


# --------------------------------------------------------------------------- #
# WhatsApp (Zernio transport): inbox poll fallback
# --------------------------------------------------------------------------- #
async def _refresh_whatsapp_convs(
    client: httpx.AsyncClient, base: str, headers: dict, account_id: str = ""
) -> bool:
    """(Re)discover this agent's conversations, throttled by CONVERSATION_REFRESH_SECONDS.

    Returns False when the caller should skip this cycle (rate limit / plan
    missing); the hot loop otherwise polls only known conversations.
    """
    global _whatsapp_convs, _last_conv_refresh
    from app.api.whatsapp_agent import agent

    if _whatsapp_convs and (time.monotonic() - _last_conv_refresh) < CONVERSATION_REFRESH_SECONDS:
        return True
    # NOTE: the Zernio conversations endpoint ignores an accountId filter
    # (returns 0 rows), so fetch all WhatsApp conversations and filter below.
    q = {"platform": "whatsapp", "status": "active", "limit": "100"}
    resp = await client.get(f"{base}/inbox/conversations", params=q, headers=headers)
    if resp.status_code == 429:
        log.warning("Zernio conversations 429 — backing off 10s")
        await asyncio.sleep(10)
        return False
    if resp.status_code == 403:  # Inbox add-on missing
        log.error("Zernio inbox poll 403 — Inbox access not enabled on this plan")
        await asyncio.sleep(120)
        return False
    resp.raise_for_status()
    conversations = (resp.json() or {}).get("data") or []
    fresh: dict[str, str] = {}
    for conv in conversations:
        cid, aid = conv.get("id"), conv.get("accountId")
        if cid and aid:
            fresh[str(cid)] = str(aid)
    # Remember which account owns the conversations (needed to reply).
    # The per-channel account_id (from the DB) wins over env/learned state.
    target = account_id or agent.zernio.account_id or ""
    if not target and fresh:
        target = next(iter(fresh.values()))
        agent.zernio.account_id = target
        log.info("Zernio poller learned account_id=%s", target)
    if target:
        fresh = {c: a for c, a in fresh.items() if a == target}
        if account_id:
            agent.zernio.account_id = target  # keep reply path consistent
    _whatsapp_convs = fresh
    _last_conv_refresh = time.monotonic()
    log.debug("Zernio poller watching %d conversation(s)", len(fresh))
    return True


def _resolve_zernio_config() -> tuple[str, str, str, str] | None:
    """Zernio credentials for polling.

    Priority: the website-managed Primary Command Center (provider=zernio)
    > the env transport (WA_MESSENGER=zernio + ZERNIO_API_KEY). Returns
    (channel_id, api_key, base_url, account_id) or None when nothing applies.
    """
    try:
        from app.services import whatsapp_channels

        for row in whatsapp_channels.store.list():
            if row["is_primary"] and row["enabled"] and row["provider"] == "zernio":
                _, creds = whatsapp_channels.store.get_creds(row["id"])
                if creds.get("api_key"):
                    return (
                        row["id"],
                        creds["api_key"],
                        (creds.get("base_url") or "https://zernio.com/api/v1").rstrip("/"),
                        str(creds.get("account_id") or ""),
                    )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("zernio channel lookup failed: %r", exc)
    if os.getenv("WA_MESSENGER", "unipile").strip().lower() == "zernio":
        key = os.getenv("ZERNIO_API_KEY", "")
        if key:
            base = (os.getenv("ZERNIO_BASE_URL", "https://zernio.com/api/v1")).rstrip("/")
            return ("env", key, base, os.getenv("ZERNIO_ACCOUNT_ID", "") or "")
    return None


async def _whatsapp_poll_cycle(client: httpx.AsyncClient) -> None:
    from app.core.agent_switch import is_running

    if not is_running("whatsapp"):
        return
    resolved = _resolve_zernio_config()
    if resolved is None:
        return  # webhook-only transport / nothing configured; nothing to poll
    channel_id, key, base, account_id = resolved
    from app.api.whatsapp_agent import agent

    headers = {"Authorization": f"Bearer {key}", "accept": "application/json"}
    # Per-channel namespace so switching primary channels never replays history.
    cursor_ns = f"{channel_id}::"
    if not await _refresh_whatsapp_convs(client, base, headers, account_id):
        return
    if not _whatsapp_convs:
        return

    cursor = _load_cursor()
    changed = False
    for conv_id, account_id in list(_whatsapp_convs.items()):
        ckey = f"{cursor_ns}{conv_id}"
        msgs_resp = await client.get(
            f"{base}/inbox/conversations/{conv_id}/messages",
            params={"accountId": account_id, "limit": "20", "sortOrder": "desc"},
            headers=headers,
        )
        if msgs_resp.status_code == 429:
            log.warning("Zernio messages 429 — backing off 10s")
            await asyncio.sleep(10)
            continue
        if msgs_resp.status_code != 200:
            continue
        # Zernio list-messages returns {"messages": [...]} (not "data")
        msgs = (msgs_resp.json() or {}).get("messages") or []
        if not msgs:
            continue
        newest_id = str(msgs[0].get("id") or "")
        if not newest_id:
            continue
        last_seen = cursor.get(ckey)
        if last_seen:
            # msgs are newest-first; walk oldest-first above the cursor.
            ordered = list(reversed(msgs))
            cut = next(
                (i for i, m in enumerate(ordered) if str(m.get("id") or "") == last_seen),
                None,
            )
            if cut is None:
                if str(ordered[0].get("id") or "") != last_seen:
                    continue  # cursor not in this window — skip until it surfaces
                pending = ordered
            else:
                pending = ordered[cut + 1 :]
        else:
            # First sighting: settle history up to the last self-sent message and
            # treat only inbound messages above it as pending. A brand-new
            # conversation (no outbound in the window) answers its first message.
            pending = []
            for m in reversed(msgs):
                if str(m.get("direction", "")).lower() in ("inbound", "incoming"):
                    pending.append(m)
                else:
                    cursor[ckey] = str(m.get("id") or "")
                    changed = True
                    break
            else:
                pending = list(reversed(msgs))
        for m in pending:
            mid = str(m.get("id") or "")
            # Zernio list-messages reports "incoming" (webhook says "inbound")
            if not mid or str(m.get("direction", "")).lower() not in ("inbound", "incoming"):
                continue
            payload = {
                "event": "message.received",
                "id": mid,
                "message": m,
                "account": {"id": account_id, "platform": "whatsapp"},
                "conversation": {"id": conv_id},
                "timestamp": m.get("sentAt") or m.get("timestamp") or "",
            }
            from app.agents.sara_whatsapp import parse_zernio_event

            try:
                ev = parse_zernio_event(payload)
            except Exception:
                ev = None
            if not ev:
                continue
            if agent.seen_messages.seen(ev["message_id"]):
                continue
            await agent.handle_inbound(
                ev["chat_id"], ev["text"], ev["sender_name"], ev["sender_number"]
            )
            cursor[ckey] = mid
            changed = True
    if changed:
        _save_cursor(cursor)


async def whatsapp_poll_loop() -> None:
    # One persistent connection: re-opening an httpx client per cycle costs a
    # full TLS round-trip (~1s) that is the difference between 1s and 3s replies.
    async with httpx.AsyncClient(timeout=httpx.Timeout(25)) as client:
        while True:
            try:
                await _whatsapp_poll_cycle(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("WhatsApp poller error: %r", exc)
            await asyncio.sleep(POLL_INTERVAL_WHATSAPP)
