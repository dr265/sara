"""Structured logging configuration for the SARA voice booking flow.

Emits one clean line per event with call/session context so failures are
diagnosable without exposing secrets:

    2026-09-30 10:12:33 | voice.booking.turn | call_id=... session=... phase=...

Sensitive values (API keys, auth headers) are never passed to these loggers;
providers log only key names and safe message summaries.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_CONFIGURED = False

_SENSITIVE_KEY_MARKERS = ("key", "token", "secret", "password", "authorization")


def _scrub(value):
    """Best-effort scrub of secret-looking fields from log payloads."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(m in str(k).lower() for m in _SENSITIVE_KEY_MARKERS) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def configure_logging() -> None:
    """Idempotent structured-logging setup (console; JSON when VOICE_LOG_JSON=1)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    json_mode = os.getenv("VOICE_LOG_JSON", "").lower() in ("1", "true", "yes")
    level = os.getenv("VOICE_LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()

    class _VoiceFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            payload = {
                "ts": ts,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["exception"] = self.formatException(record.exc_info)[-800:]
            if json_mode:
                return json.dumps(_scrub(payload), ensure_ascii=False)
            base = f"{ts} | {record.levelname:<5} | {record.name} | {record.getMessage()}"
            if record.exc_info:
                base += "\n" + self.formatException(record.exc_info)
            return base

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_VoiceFormatter())
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "WARNING").upper())
    for name in (
        "sara.voice.engine",
        "sara.voice.booking",
        "sara.voice.conversation",
        "sara.voice.sarvam",
        "sara.voice.transfer",
    ):
        logging.getLogger(name).setLevel(level)
