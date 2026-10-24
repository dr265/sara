"""Persistent memory for the WhatsApp hotel agent.

Shares the same SQLite database (backend/sara.db) and the same table schema
used by the Telegram agent, so every agent in the ecosystem keeps one store:

  conversations  — one row per WhatsApp chat (channel='whatsapp'), JSON state
                   (guest name, booking draft, language, intent) survives restarts
  messages       — full inbound/outbound history per conversation
  facts          — extracted guest memory (name, phone, room preference,
                   check-in/out dates, party size, notes) with entities
                   'guest', 'booking', 'preference'

The store is thread-safe (RLock + WAL) and lazily creates the schema, so it
works even when the Telegram agent has never booted on this database.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent.parent / "sara.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY, tenant_id TEXT, channel TEXT, external_chat_id TEXT,
  language TEXT, state TEXT, paused_until TEXT, summary TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, tenant_id TEXT, conversation_id TEXT,
  direction TEXT, text TEXT, media TEXT, intent TEXT, confidence REAL,
  sources TEXT, external_id TEXT, delivery_status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT PRIMARY KEY, tenant_id TEXT, entity TEXT, key TEXT, value TEXT,
  numeric_value REAL, currency TEXT, valid_from TEXT, valid_to TEXT,
  confidence REAL, doc_id TEXT, doc_version INTEGER, locator TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_wa_msgs_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wa_facts_tenant ON facts(tenant_id, entity, key);
"""

_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_GUEST_RE = re.compile(
    r"\b(?:my name is|i am|i'm|name is|it's)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", re.IGNORECASE
)
_ROOM_RE = re.compile(r"\b(standard|deluxe|suite|villa|penthouse)\b", re.IGNORECASE)
_GUESTS_RE = re.compile(r"\b(\d+)\s*(?:guest|people|person|adults?|pax)\b", re.IGNORECASE)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _norm_phone(value: Optional[str]) -> str:
    return re.sub(r"[^\d+]", "", value or "")


class MemoryStore:
    """SQLite-backed memory shared by every WhatsApp chat (thread-safe)."""

    def __init__(self, db_path: Optional[Path] = None, tenant_id: str = "whatsapp") -> None:
        db_path = db_path or DB_PATH  # callers pass None to mean "the shared sara.db"
        self.path = db_path
        self.tenant_id = tenant_id
        self._lock = threading.RLock()
        # autocommit + busy timeout: errors can never leave an open write
        # transaction (which would lock the whole shared DB permanently).
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # conversations (state survives restarts)
    # ------------------------------------------------------------------ #
    def get_or_create_conversation(self, chat_id: str, language: str = "en") -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM conversations WHERE tenant_id=? AND external_chat_id=? AND channel=?",
                (self.tenant_id, chat_id, "whatsapp"),
            ).fetchone()
            if row is not None:
                return dict(row)
            conv = {
                "id": _new_id("cnv"),
                "tenant_id": self.tenant_id,
                "channel": "whatsapp",
                "external_chat_id": chat_id,
                "language": language,
                "state": "{}",
                "paused_until": None,
                "summary": None,
                "created_at": _iso(),
                "updated_at": _iso(),
            }
            self.conn.execute(
                "INSERT INTO conversations(id, tenant_id, channel, external_chat_id, language,"
                " state, paused_until, summary, created_at, updated_at)"
                " VALUES(:id, :tenant_id, :channel, :external_chat_id, :language,"
                " :state, :paused_until, :summary, :created_at, :updated_at)",
                conv,
            )
            self.conn.commit()
            return conv

    def load_state(self, chat_id: str) -> Dict[str, Any]:
        conv = self.get_or_create_conversation(chat_id)
        try:
            return json.loads(conv.get("state") or "{}")
        except Exception:
            return {}

    def save_state(self, chat_id: str, state: Dict[str, Any], summary: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE conversations SET state=?, summary=?, updated_at=? WHERE tenant_id=? AND"
                " external_chat_id=? AND channel=?",
                (
                    json.dumps(state, ensure_ascii=False, default=str),
                    summary,
                    _iso(),
                    self.tenant_id,
                    chat_id,
                    "whatsapp",
                ),
            )
            self.conn.commit()

    def set_paused(self, chat_id: str, paused: bool = True) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE conversations SET paused_until=?, updated_at=? WHERE tenant_id=? AND"
                " external_chat_id=? AND channel=?",
                (_iso() if paused else None, _iso(), self.tenant_id, chat_id, "whatsapp"),
            )
            self.conn.commit()

    def is_paused(self, chat_id: str) -> bool:
        conv = self.get_or_create_conversation(chat_id)
        return bool(conv.get("paused_until"))

    def paused_at(self, chat_id: str) -> Optional[str]:
        """ISO timestamp of the last handoff-to-staff, or None when not paused."""
        conv = self.get_or_create_conversation(chat_id)
        value = conv.get("paused_until")
        return str(value) if value else None

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
    def add_message(
        self,
        chat_id: str,
        direction: str,
        text: str,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
        external_id: Optional[str] = None,
        delivery_status: str = "received",
    ) -> None:
        conv = self.get_or_create_conversation(chat_id)
        with self._lock:
            self.conn.execute(
                "INSERT INTO messages(id, tenant_id, conversation_id, direction, text, media,"
                " intent, confidence, sources, external_id, delivery_status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _new_id("msg"),
                    self.tenant_id,
                    conv["id"],
                    direction,
                    (text or "")[:4000],
                    None,
                    intent,
                    confidence,
                    None,
                    external_id,
                    delivery_status,
                    _iso(),
                ),
            )
            self.conn.commit()

    def history(self, chat_id: str, limit: int = 12) -> List[Dict[str, Any]]:
        conv = self.get_or_create_conversation(chat_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT direction, text, intent, created_at FROM messages WHERE conversation_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (conv["id"], limit),
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def all_chats(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT external_chat_id, language, state, summary, updated_at FROM conversations"
                " WHERE tenant_id=? AND channel=? ORDER BY updated_at DESC LIMIT ?",
                (self.tenant_id, "whatsapp", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # facts (guest memory)
    # ------------------------------------------------------------------ #
    def upsert_fact(self, entity: str, key: str, value: str, confidence: float = 1.0) -> None:
        if not value:
            return
        key = (key or "").lower().strip()[:120]
        if not key:
            return
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM facts WHERE tenant_id=? AND entity=? AND key=?",
                (self.tenant_id, entity, key),
            ).fetchone()
            if row is not None:
                self.conn.execute(
                    "UPDATE facts SET value=?, confidence=?, updated_at=? WHERE id=?",
                    (value[:1000], confidence, _iso(), row["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO facts(id, tenant_id, entity, key, value, numeric_value, currency,"
                    " valid_from, valid_to, confidence, doc_id, doc_version, locator, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _new_id("fact"),
                        self.tenant_id,
                        entity,
                        key,
                        value[:1000],
                        None,
                        None,
                        None,
                        None,
                        confidence,
                        None,
                        None,
                        "whatsapp-memory",
                        _iso(),
                    ),
                )
            self.conn.commit()

    def facts(self, entity: Optional[str] = None, limit: int = 40) -> List[Dict[str, Any]]:
        with self._lock:
            if entity:
                rows = self.conn.execute(
                    "SELECT entity, key, value, confidence, updated_at FROM facts WHERE tenant_id=?"
                    " AND entity=? ORDER BY updated_at DESC LIMIT ?",
                    (self.tenant_id, entity, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT entity, key, value, confidence, updated_at FROM facts WHERE tenant_id=?"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (self.tenant_id, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def memory_context(self, chat_id: str, max_facts: int = 12) -> str:
        """Human-readable guest memory for the LLM prompt."""
        conv = self.get_or_create_conversation(chat_id)
        try:
            state = json.loads(conv.get("state") or "{}")
        except Exception:
            state = {}

        lines: List[str] = []
        name = state.get("guest_name")
        if name:
            lines.append(f"- Guest name: {name}")
        phone = _norm_phone(state.get("phone_number"))
        if phone:
            lines.append(f"- Phone: {phone}")

        facts = self.facts(limit=max_facts)
        for f in facts:
            lines.append(f"- {f['entity']}: {f['key']} = {f['value']}")

        if not lines:
            return "No prior conversations with this guest."
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # fact extraction (runs after every inbound message)
    # ------------------------------------------------------------------ #
    def extract_facts(self, chat_id: str, text: str) -> List[str]:
        """Pull durable guest details out of a message and store them."""
        stored: List[str] = []
        text = (text or "").strip()
        if not text:
            return stored

        m = _GUEST_RE.search(text)
        if m:
            name = m.group(1).strip()
            self.upsert_fact("guest", "name", name)
            stored.append("name")

        m = _ROOM_RE.search(text)
        if m:
            self.upsert_fact("preference", "room_type", m.group(1).capitalize(), 0.8)
            stored.append("room_type")

        m = _GUESTS_RE.search(text)
        if m:
            self.upsert_fact("booking", "party_size", m.group(1), 0.8)
            stored.append("party_size")

        dates = _DATE_RE.findall(text)
        if len(dates) >= 2:
            self.upsert_fact("booking", "check_in", dates[0], 0.9)
            self.upsert_fact("booking", "check_out", dates[1], 0.9)
            stored.append("dates")
        elif dates:
            self.upsert_fact("booking", "date", dates[0], 0.9)
            stored.append("date")

        if "breakfast" in text.lower():
            self.upsert_fact("preference", "breakfast", "yes", 0.7)
        if "quiet" in text.lower() or "away from" in text.lower():
            self.upsert_fact("preference", "quiet_room", "yes", 0.7)
        if "late checkout" in text.lower():
            self.upsert_fact("preference", "late_checkout", "yes", 0.7)

        return stored

    def forget(self, chat_id: str) -> bool:
        """Delete one conversation and all of its messages (guest facts stay — they
        are tenant-wide knowledge shared across chats, matching the Telegram agent)."""
        with self._lock:
            conv = self.conn.execute(
                "SELECT id FROM conversations WHERE tenant_id=? AND external_chat_id=? AND channel=?",
                (self.tenant_id, chat_id, "whatsapp"),
            ).fetchone()
            if conv is None:
                return False
            self.conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv["id"],))
            self.conn.execute(
                "DELETE FROM conversations WHERE id=? AND tenant_id=?", (conv["id"], self.tenant_id)
            )
            self.conn.commit()
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass