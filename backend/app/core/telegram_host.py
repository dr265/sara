"""Hosts the multi-tenant Telegram booking agent inside the unified SARA backend."""
import json
import os
import secrets
from pathlib import Path

import requests
from dotenv import load_dotenv

# Ensure backend/.env is loaded no matter how this module is imported
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi.middleware.wsgi import WSGIMiddleware  # noqa: E402
from telegram_agent import SaraApp, TelegramAdapter, WsgiApp, new_id  # noqa: E402
from app.core.telegram_bot_token import get_bot_token  # noqa: E402


def load_or_create_master_key() -> str:
    """Return a persistent SARA_MASTER_KEY so stored encrypted tokens survive restarts.

    Priority: environment variable > key file (backend/.sara_master_key) > newly generated.
    """
    key = os.environ.get("SARA_MASTER_KEY")
    if key:
        return key
    key_file = Path(__file__).resolve().parent.parent.parent / ".sara_master_key"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
    else:
        key = secrets.token_urlsafe(32)
        key_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    os.environ["SARA_MASTER_KEY"] = key
    return key


os.environ.setdefault("SARA_MASTER_KEY", load_or_create_master_key())
# Telegram agent LLM: driven by the shared xKiro/LLM config (provider=openai-compatible)
if os.environ.get("LLM_API_KEY"):
    os.environ.setdefault("SARA_LLM_API_KEY", os.environ["LLM_API_KEY"])
    os.environ.setdefault("SARA_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "https://api.xkiro.com/v1"))
    os.environ.setdefault("SARA_LLM_MODEL", os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash"))
os.environ.setdefault("SARA_LLM_PROVIDER", "openai")

telegram_backend = SaraApp()


class DirectTelegramAdapter(TelegramAdapter):
    """Talks directly to the Telegram Bot API (no polling/webhook server required)."""

    name = "direct"

    def _api(self, method: str, **params):
        url = f"https://api.telegram.org/bot{get_bot_token()}/{method}"
        response = requests.post(url, json=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def send_message(self, tenant_id, chat_id, text, connection_id=None):
        self._api("sendMessage", chat_id=chat_id, text=text)
        return {"external_id": new_id("tgmsg"), "status": "sent"}

    def send_media(self, tenant_id, chat_id, path, caption="", connection_id=None):
        url = f"https://api.telegram.org/bot{get_bot_token()}/sendDocument"
        with open(path, "rb") as fh:
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption or None},
                files={"document": (os.path.basename(path), fh)},
                timeout=60,
            )
        response.raise_for_status()
        return {"external_id": new_id("tgmed"), "status": "sent"}

    def download_file(self, tenant_id, file_id, connection_id=None):
        info = self._api("getFile", file_id=file_id)
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return b""
        url = f"https://api.telegram.org/file/bot{get_bot_token()}/{file_path}"
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return response.content


telegram_backend.telegram.adapters["direct"] = DirectTelegramAdapter()
telegram_backend.telegram.default_adapter = "direct"

telegram_wsgi = WsgiApp(telegram_backend)


class _TelegramGate:
    """ASGI wrapper that pauses guest-facing Telegram inbound when the switch is off.

    POSTs to /agent/webhooks/telegram/* are the guest intake for the bot. When
    the agent is paused we answer immediately with {"status": "paused"} and do
    no work. Everything else (owner console, knowledge, sheets, health) passes
    through untouched.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            method = scope.get("method", "")
            if method == "POST" and path.startswith("/agent/webhooks/telegram"):
                from app.core.agent_switch import is_running

                if not is_running("telegram"):
                    body = json.dumps({"status": "paused"}).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.inner(scope, receive, send)


def mount_telegram_agent(app) -> None:
    """Mount the WSGI agent under /agent (e.g. /agent/health, /agent/bookings, ...)."""
    app.mount("/agent", _TelegramGate(WSGIMiddleware(telegram_wsgi)))