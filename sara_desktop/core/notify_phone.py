"""
core/notify_phone.py — completion reports to the phone (spec §9).

Channels, in order, every one honest — a channel is only marked delivered
when its transport confirmed it:

  1. SARA Remote Dashboard (the paired-phone channel built into this
     desktop): the report text + screenshot are pushed over the dashboard
     WebSocket to the connected phones. With zero phones connected the
     message is reported as QUEUED and re-sent on the next connect —
     never claimed as delivered.
  2. Telegram (optional): active only when BOTH a bot token and an owner
     chat id are configured (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in the
     environment or backend/.env). Otherwise it is reported as "not
     configured" — never silently assumed.

No credentials are hardcoded and no message is ever claimed delivered
without a confirmed transport result.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

MAX_TEXT = 900


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except Exception:
        pass
    return out


def _telegram_config(backend_root: Optional[Path]) -> tuple:
    """(token, chat_id) from the environment, then backend/.env — or ("","")."""
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            or os.environ.get("SARA_TELEGRAM_CHAT_ID", "").strip())
    root = Path(backend_root) if backend_root else \
        Path(__file__).resolve().parents[2]
    env_file = root / "backend" / ".env"
    if env_file.exists():
        env = _parse_env_file(env_file)
        token = token or env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = chat or env.get("TELEGRAM_CHAT_ID", "").strip() \
            or env.get("SARA_TELEGRAM_CHAT_ID", "").strip()
    return token, chat


class PhoneNotifier:
    """Builds the completion report and delivers it through real channels."""

    def __init__(self, push: Optional[Callable[[dict], Any]] = None,
                 client_count: Optional[Callable[[], int]] = None,
                 uploads_dir: Optional[str] = None,
                 backend_root: Optional[str] = None) -> None:
        self._push = push
        self._client_count = client_count
        self._uploads_dir = Path(uploads_dir) if uploads_dir else None
        self._backend_root = Path(backend_root) if backend_root else None
        self._pending: List[dict] = []

    # ── report construction ─────────────────────────────────────────────

    @staticmethod
    def build_report_text(record: Dict[str, Any]) -> str:
        status = str(record.get("status") or "?").upper()
        request = str(record.get("request") or "").strip()[:160]
        body = str(record.get("result") or record.get("error") or "").strip()
        if len(body) > 600:
            body = body[:599] + "…"
        artifacts = record.get("artifacts") or []
        lines = [f"SARA — task {status}",
                 f"Request: {request}"] if request else [f"SARA — task {status}"]
        if body:
            lines.append(body)
        if artifacts:
            first = str((artifacts[0] or {}).get("title")
                        or (artifacts[0] or {}).get("url")
                        or (artifacts[0] or {}).get("path") or "")
            lines.append(f"{len(artifacts)} artifact(s)"
                         + (f" — {first[:80]}" if first else ""))
        tid = str(record.get("task_id") or "")[:8]
        if tid:
            lines.append(f"task {tid}")
        return "\n".join(lines)[:MAX_TEXT]

    # ── delivery ────────────────────────────────────────────────────────

    def _send_telegram(self, token: str, chat: str, text: str,
                       screenshot_path: Optional[str]) -> tuple:
        try:
            import requests
        except Exception as exc:
            return False, f"requests unavailable: {exc}"
        try:
            if screenshot_path and Path(screenshot_path).exists():
                with open(screenshot_path, "rb") as fh:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat, "caption": text[:1000]},
                        files={"photo": ("report.png", fh, "image/png")},
                        timeout=30)
                kind = "photo"
            else:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat, "text": text[:3900]},
                    timeout=30)
                kind = "message"
            ok = (resp.status_code == 200
                  and str((resp.json() or {}).get("ok")).lower() == "true")
            if ok:
                return True, f"{kind} delivered"
            return False, f"telegram HTTP {resp.status_code}: {resp.text[:150]}"
        except Exception as exc:
            return False, f"telegram send failed: {type(exc).__name__}: {exc}"

    def send_report(self, record: Dict[str, Any],
                    screenshot_path: Optional[str] = None
                    ) -> Dict[str, Any]:
        """Deliver one completion report; returns per-channel truth."""
        channels: Dict[str, Any] = {}
        text = self.build_report_text(record)
        task_id = str(record.get("task_id") or "")

        image_name = ""
        if screenshot_path and self._uploads_dir:
            try:
                src = Path(screenshot_path)
                if src.exists():
                    stamp = int(time.time())
                    image_name = f"sara_task_{task_id[:8]}_{stamp}.png"
                    self._uploads_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, self._uploads_dir / image_name)
            except Exception:
                image_name = ""

        msg = {"type": "report", "task_id": task_id,
               "status": str(record.get("status") or ""),
               "text": text,
               "image": f"/uploads/{image_name}" if image_name else ""}

        # 1) paired phones over the dashboard WebSocket
        if self._push is not None:
            clients: Optional[int] = None
            try:
                if callable(self._client_count):
                    clients = int(self._client_count() or 0)
            except Exception:
                clients = None
            try:
                if clients == 0:
                    # nothing is listening — queue and deliver the moment a
                    # phone registers (keeps the replay history clean)
                    self._pending.append(dict(msg))
                    del self._pending[:-5]
                    channels["phone"] = {
                        "ok": False, "queued": True,
                        "detail": "no phone connected — queued for next connect"}
                else:
                    self._push(dict(msg))
                    if clients is None:
                        channels["phone"] = {
                            "ok": True,
                            "detail": "broadcast to dashboard (client count unknown)"}
                    else:
                        channels["phone"] = {
                            "ok": True,
                            "detail": f"delivered to {clients} connected phone(s)"}
            except Exception as exc:
                channels["phone"] = {"ok": False,
                                     "detail": f"push failed: {exc}"}
        else:
            channels["phone"] = {"ok": False,
                                 "detail": "remote dashboard unavailable"}

        # 2) Telegram (only when fully configured)
        token, chat = _telegram_config(self._backend_root)
        if token and chat:
            ok, detail = self._send_telegram(token, chat, text, screenshot_path)
            channels["telegram"] = {"ok": ok, "detail": detail}
        else:
            missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token),
                                      ("TELEGRAM_CHAT_ID", chat)) if not v]
            channels["telegram"] = {
                "ok": False,
                "detail": f"not configured ({', '.join(missing)})"}
        return channels

    def has_pending(self) -> bool:
        """True when a report is waiting for the next phone connect."""
        return bool(self._pending)

    def flush_pending(self) -> int:
        """Re-send queued reports (call when a phone connects)."""
        if self._push is None or not self._pending:
            return 0
        sent = 0
        queued, self._pending = self._pending, []
        for msg in queued:
            try:
                self._push(dict(msg))
                sent += 1
            except Exception:
                self._pending.append(msg)
        return sent
