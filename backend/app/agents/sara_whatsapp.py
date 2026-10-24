"""SARA — Grand Stay Hotel WhatsApp agent (Unipile or Zernio transport).

Engine extracted from the original standalone `sara_whatsapp.py` design so it can
be mounted inside the unified SARA backend (backend/run_backend.py → port 8000).

Architecture
  Messenger webhook (Unipile or Zernio)  ->  LLM (strict JSON)  ->  messenger send

Transport is selected with WA_MESSENGER=unipile (default) | zernio.

Run / webhook URLs (served by app.api.whatsapp_agent under /whatsapp/*):
  GET  /whatsapp/health               engine status
  POST /whatsapp/webhooks/unipile     Unipile message webhook
  POST /whatsapp/admin/sessions/{chat_id}/resume
  DELETE /whatsapp/admin/sessions/{chat_id}

Configuration (backend/.env):
  UNIPILE_DSN, UNIPILE_ACCESS_TOKEN, UNIPILE_ACCOUNT_ID,
  WEBHOOK_SECRET, STAFF_CHAT_ID, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
  WA_MESSENGER, ZERNIO_API_KEY, ZERNIO_BASE_URL, ZERNIO_ACCOUNT_ID,
  ZERNIO_WEBHOOK_SECRET, BUSINESS_FILE (defaults to <backend>/business_context.json)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("sara.whatsapp")

# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #
UNIPILE_DSN = os.getenv("UNIPILE_DSN", "").rstrip("/")
UNIPILE_ACCESS_TOKEN = os.getenv("UNIPILE_ACCESS_TOKEN", "")
UNIPILE_ACCOUNT_ID = os.getenv("UNIPILE_ACCOUNT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
STAFF_CHAT_ID = os.getenv("STAFF_CHAT_ID")

# Transport selection: "unipile" (default) or "zernio" (https://zernio.com).
WA_MESSENGER = os.getenv("WA_MESSENGER", "unipile").strip().lower()
ZERNIO_API_KEY = os.getenv("ZERNIO_API_KEY", "")
ZERNIO_BASE_URL = os.getenv("ZERNIO_BASE_URL", "https://zernio.com/api/v1").rstrip("/")
ZERNIO_ACCOUNT_ID = os.getenv("ZERNIO_ACCOUNT_ID")
ZERNIO_WEBHOOK_SECRET = os.getenv("ZERNIO_WEBHOOK_SECRET")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL = os.getenv("WHATSAPP_LLM_MODEL", os.getenv("LLM_MODEL", "gpt-4o-mini"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))

BUSINESS_FILE = Path(os.getenv("BUSINESS_FILE", "business_context.json"))
if not BUSINESS_FILE.is_absolute():
    BUSINESS_FILE = Path(__file__).resolve().parent.parent.parent / BUSINESS_FILE

HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "12"))
SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", str(6 * 3600)))
# After an escalation to staff, SARA stays quiet while staff could be replying.
# If the guest messages again after this many seconds (or when no staff chat is
# configured at all), SARA auto-resumes the conversation so guests are never ghosted.
WA_AUTO_RESUME_SECONDS = int(os.getenv("WA_AUTO_RESUME_SECONDS", "300"))
MAX_SEEN_MESSAGES = 5000

# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """<system_role>
You are SARA, an elite backend AI operating over WhatsApp. You process incoming messages and output logic exclusively for a WhatsApp agent (Zernio / Unipile transport). You are a data processor; you do not output conversational text directly, but rather generate strictly validated JSON that the backend will parse.
</system_role>

<agent_persona>
{agent_persona}
</agent_persona>

<core_directives>
1. STRICT JSON OUTPUT: Your entire response must be a single, valid JSON object. NEVER use markdown code blocks. NEVER add conversational filler before or after the JSON.
2. WHATSAPP FORMATTING: The "reply" field will be sent directly to WhatsApp. Use standard WhatsApp formatting (*bold*, _italics_) and emojis natively. Keep responses short and punchy (1-3 brief sentences maximum).
3. PROGRESSIVE DISCLOSURE: Never overwhelm the user. Ask for ONE piece of missing information at a time.
4. TAPPABLE MENUS: Put guest-selectable actions in "ui_options" — short plain-text labels, NO emojis, max ~22 characters each, up to 5. The backend renders them as a native tappable WhatsApp list. RULES: (a) "ui_options" MUST be [] when you asked the guest to type free-form information (their name, a complaint, an address, ...); (b) every option must relate to the question you just asked — NEVER carry over or repeat earlier menu items ("View prices", "Speak to staff") while asking for a date or any other missing detail; (c) when you are actively asking for a date, guest count or number of nights, offer 2-4 tappable shortcuts for that ONE question (dates: "Today", "Tomorrow", "This weekend", "Next week"; counts: "2 guests", "3 nights") — never offer date shortcuts in a greeting or before the guest has chosen what they need; vague date taps are expanded into an exact-day picker automatically, so never guess a date from them; NEVER include a "Type my own date" row yourself — the backend adds it automatically whenever a date question is asked; (d) never ask the guest to type a rigid format like YYYY-MM-DD and never tell them to type a number.
5. DATA GROUNDING: Reference <business_context> for prices, rules, and availability. Never invent data. If information is missing from the context, say so and offer staff help.
6. ESCALATION: If the user is frustrated, asks for a human, or requests something outside hotel operations, set "escalate_to_human" to true.
7. ANY ENTRY POINT: The user may start the conversation with any message (a question, dates, a complaint). Respond to what they actually said; do not force a greeting flow.
8. STATE CONTINUITY: <conversation_state> holds what is already known. Carry those values forward and only update fields the user changed. Set "ready_to_book" to true only when check_in, check_out, guests, room_type and guest_name are all known.
9. ONE QUESTION ONLY: Ask exactly ONE thing per reply. If the check-in date is known, ask EITHER for the check-out date OR the number of nights — never both in one message.
10. TAP = ANSWER: A tapped option arrives as its plain label text (e.g. "Today" or "3 nights") — treat it as the guest's real answer. NEVER guess a single date from a vague answer ("Next week", "This weekend", "Next month", "in two weeks"): reply asking for the exact day and put the concrete dates in "ui_options" as tappable rows (compute the real weekdays from today's date, e.g. "Mon 7 Sep", "Tue 8 Sep"); set check_in/check_out only after the guest picks a specific day. "Today"/"Tomorrow" may set the date directly. "N nights" becomes check_out = check_in + N days and "2 guests" becomes guests: 2.
11. MAIN MENU FIRST: If the guest's first message is a greeting, or just "help"/"menu"/"options" with no clear request, DO NOT collect dates — open with a short welcome plus the main menu in "ui_options": ["Book a room", "Check availability", "View prices", "Amenities", "Speak to staff"]. Only start asking that path's questions once the guest picks one. If the guest states their need directly (dates, room, prices, a complaint), start there instead.
12. BOOKING STEP ORDER: For a new booking ask exactly ONE question per reply, in this order: 1) check-in date, 2) nights (or check-out date), 3) number of guests, 4) room type, 5) guest name, 6) confirm the booking details. Briefly confirm each answer the guest gives, then ask ONLY the next missing item. Never combine two questions in one message, never skip ahead, and never ask for dates unless dates are the current missing step.
</core_directives>

<business_context>
{business_context}
</business_context>

<current_state>
Today's Date: {today}
</current_state>

<guest_memory>
{guest_memory}
</guest_memory>

<knowledge_base>
{knowledge_context}
</knowledge_base>

<conversation_state>
{conversation_state}
</conversation_state>

<output_schema>
Respond ONLY with this exact JSON structure:
{{
  "reply": "Your message to the user, formatted for WhatsApp.",
  "intent": "booking | pricing | availability | support | general",
  "escalate_to_human": false,
  "state": {{
    "guest_name": null,
    "phone_number": null,
    "check_in": "YYYY-MM-DD or null",
    "check_out": "YYYY-MM-DD or null",
    "guests": null,
    "room_type": null,
    "ready_to_book": false
  }},
  "ui_options": ["Option 1", "Option 2"]
}}
</output_schema>"""

REPAIR_PROMPT = (
    "Your previous output was not valid according to the schema: {error}. "
    "Return ONLY the corrected JSON object, nothing else."
)

FALLBACK_REPLY = (
    "Sorry, I hit a small glitch on my side. 🙏 Could you please repeat that, "
    "or type *Speak to Staff* and a team member will help you."
)

# Specific, actionable message when the LLM provider refuses the request
# (e.g. OpenRouter error 402: out of credits / max_tokens too large).
LLM_PROVIDER_BLOCKED_REPLY = (
    "⚠️ My language engine is currently unavailable (provider rejected the request — "
    "usually out of credits or an invalid API key). Please check the LLM provider "
    "balance/key and try again, or type *Speak to Staff* for immediate help."
)
FALLBACK_OPTIONS = ["Check Availability", "Speak to Staff"]

# --------------------------------------------------------------------------- #
# Schema (strict validation of the LLM output)
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BookingState(BaseModel):
    guest_name: str | None = None
    phone_number: str | None = None
    check_in: str | None = None
    check_out: str | None = None
    guests: int | None = None
    room_type: str | None = None
    ready_to_book: bool = False

    @field_validator("check_in", "check_out", mode="before")
    @classmethod
    def _iso_date_or_none(cls, v: Any) -> str | None:
        if v in (None, "", "null"):
            return None
        v = str(v)
        if not DATE_RE.match(v):
            raise ValueError("date must be YYYY-MM-DD or null")
        date.fromisoformat(v)  # raises on impossible dates
        return v

    @field_validator("guests", mode="before")
    @classmethod
    def _guests_int(cls, v: Any) -> int | None:
        if v in (None, "", "null"):
            return None
        n = int(v)
        if n < 1:
            raise ValueError("guests must be >= 1")
        return n


class SaraResponse(BaseModel):
    reply: str = Field(min_length=1, max_length=1500)
    intent: Literal["booking", "pricing", "availability", "support", "general"]
    escalate_to_human: bool = False
    state: BookingState = Field(default_factory=BookingState)
    ui_options: list[str] = Field(default_factory=list)

    @field_validator("ui_options")
    @classmethod
    def _clean_options(cls, v: list[str]) -> list[str]:
        cleaned = [str(o).strip() for o in v if str(o).strip()]
        return cleaned[:10]  # exact-day pickers can hold a full week of dates


# --------------------------------------------------------------------------- #
# Date UX helpers: main menu, custom-date option, vague-date expansion.
# --------------------------------------------------------------------------- #
MAIN_MENU_OPTIONS = [
    "Book a room",
    "Check availability",
    "View prices",
    "Amenities",
    "Speak to staff",
]
CUSTOM_DATE_OPTION = "Type my own date"
DATE_QUICK_OPTIONS = ["Today", "Tomorrow", "This weekend", "Next week"]

_GREETING_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|yo|namaste|hola|hiya|hi there|hello there|good (morning|afternoon|evening)|what'?s up|can you help( me)?|help( me)?|menu|options|start|new chat|restart)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_DATE_ASK_RE = re.compile(
    r"\b(when|which day|which date|what day|what date|arriv|depart|leave|until when|check[- ]?in date|check[- ]?out date)\b",
    re.IGNORECASE,
)
_VAGUE_DATE_RE = re.compile(
    r"\b(next weekend|this weekend|next week|this week|weekend|tomorrow|tmrw|today)\b",
    re.IGNORECASE,
)


def _vague_dates(text: str) -> list[date] | None:
    """Concrete dates implied by a vague date phrase (e.g. "next week").

    Returns the specific dates the guest could mean, or None when the text has
    no vague anchor. Single-day anchors (today/tomorrow) return a one-item list
    so callers can choose whether an extra picker step is needed."""
    m = _VAGUE_DATE_RE.search(text or "")
    if not m:
        return None
    anchor = m.group(1).lower()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    if anchor == "today":
        return [today]
    if anchor in ("tomorrow", "tmrw"):
        return [today + timedelta(days=1)]
    if anchor in ("this weekend", "weekend"):
        if today.weekday() == 5:  # Saturday: this weekend is today + tomorrow
            return [today, today + timedelta(days=1)]
        if today.weekday() == 6:  # Sunday: only today remains
            return [today]
        sat = monday + timedelta(days=5)
        return [sat, sat + timedelta(days=1)]
    if anchor == "next weekend":
        sat = monday + timedelta(days=12)  # Saturday of next week
        return [sat, sat + timedelta(days=1)]
    if anchor == "next week":
        start = monday + timedelta(days=7)
        return [start + timedelta(days=i) for i in range(7)]
    if anchor == "this week":
        return [
            monday + timedelta(days=i)
            for i in range(7)
            if monday + timedelta(days=i) >= today
        ]
    return None


def _date_label(d: date) -> str:
    """Short tappable label like "Sat 6 Sep" (year added when it differs)."""
    label = f"{d:%a} {d.day} {d:%b}"
    return f"{label} {d.year}" if d.year != date.today().year else label


# --------------------------------------------------------------------------- #
# Session store (in-memory; swap for Redis in multi-instance deployments)
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    chat_id: str
    history: list[dict[str, str]] = field(default_factory=list)
    state: BookingState = field(default_factory=BookingState)
    last_options: list[str] = field(default_factory=list)
    pending_dates: list[date] | None = None  # exact-day picker shown to the guest
    handed_off: bool = False
    updated_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Sessions backed by persistent SQLite memory (state + history survive restarts)."""

    def __init__(self, memory: Any = None, session_ttl: int = SESSION_TTL) -> None:
        self._session_ttl = session_ttl
        self._sessions: dict[str, Session] = {}
        self.memory = memory

    def _hydrate(self, s: Session) -> None:
        """Load persisted state/history into a fresh session (first touch or after TTL)."""
        if self.memory is None:
            return
        try:
            persisted = self.memory.load_state(s.chat_id)
            if persisted:
                known = {
                    k: v
                    for k, v in persisted.items()
                    if k
                    in {
                        "guest_name",
                        "phone_number",
                        "check_in",
                        "check_out",
                        "guests",
                        "room_type",
                        "ready_to_book",
                        "intent",
                        "language",
                        "lead_saved",
                    }
                }
                s.state = BookingState(**{k: v for k, v in known.items() if v is not None})
            for m in self.memory.history(s.chat_id, limit=HISTORY_TURNS):
                role = "assistant" if m.get("direction") == "out" else "user"
                s.history.append({"role": role, "content": m.get("text") or ""})
            if self.memory.is_paused(s.chat_id):
                s.handed_off = True
        except Exception:
            pass  # memory must never break the conversation loop

    def get(self, chat_id: str) -> Session:
        s = self._sessions.get(chat_id)
        if s is None or time.time() - s.updated_at > self._session_ttl:
            fresh = Session(chat_id=chat_id)
            self._hydrate(fresh)
            self._sessions[chat_id] = fresh
            return fresh
        return s

    def reset(self, chat_id: str) -> None:
        self._sessions.pop(chat_id, None)
        if self.memory is not None:
            try:
                self.memory.save_state(chat_id, {})
            except Exception:
                pass

    def resume(self, chat_id: str) -> bool:
        s = self._sessions.get(chat_id)
        if s:
            s.handed_off = False
            s.updated_at = time.time()
        if self.memory is not None:
            try:
                self.memory.set_paused(chat_id, paused=False)
            except Exception:
                pass
        return s is not None


class SeenMessages:
    """Bounded LRU set used to de-duplicate webhook retries."""

    def __init__(self, limit: int = MAX_SEEN_MESSAGES) -> None:
        self._items: OrderedDict[str, None] = OrderedDict()
        self._limit = limit

    def seen(self, message_id: str) -> bool:
        if message_id in self._items:
            return True
        self._items[message_id] = None
        if len(self._items) > self._limit:
            self._items.popitem(last=False)
        return False


# --------------------------------------------------------------------------- #
# Business context (hot-reloads the JSON file on change)
# --------------------------------------------------------------------------- #
class BusinessContext:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime = 0.0
        self._text = "{}"

    def load(self) -> str:
        """Hot-reloads the file when it changes, so prices/inventory can be updated live."""
        try:
            mtime = self._path.stat().st_mtime
            if mtime != self._mtime:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._text = json.dumps(data, indent=2, ensure_ascii=False)
                self._mtime = mtime
                log.info("Business context reloaded from %s", self._path)
        except FileNotFoundError:
            log.warning(
                "Business context file %s not found; SARA will refuse to quote prices.", self._path
            )
            self._text = json.dumps({"note": "No business data loaded. Do not quote prices or availability."})
        return self._text


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> dict[str, Any]:
    """Tolerates stray code fences or leading text, then parses the first JSON object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1])


class LlmClient:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = LLM_MODEL,
        timeout: float = LLM_TIMEOUT,
    ) -> None:
        self._model = model
        self._client_timeout = timeout
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def _build_system_prompt(
        self,
        business_text: str,
        state_text: str,
        guest_memory: str,
        agent_persona: str,
        knowledge: str = "",
    ) -> str:
        return SYSTEM_PROMPT.format(
            agent_persona=agent_persona,
            business_context=business_text,
            today=date.today().isoformat(),
            guest_memory=guest_memory,
            knowledge_context=knowledge,
            conversation_state=state_text,
        )

    async def ask(
        self,
        business_text: str,
        session: Session,
        user_text: str,
        guest_memory: str = "",
        agent_persona: str = "",
        knowledge: str = "",
    ) -> SaraResponse:
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": self._build_system_prompt(
                    business_text,
                    session.state.model_dump_json(indent=2),
                    guest_memory,
                    agent_persona,
                    knowledge,
                ),
            }
        ]
        messages += session.history[-HISTORY_TURNS:]
        messages.append({"role": "user", "content": user_text})

        raw = ""
        last_error = "unknown"
        for attempt in range(2):
            try:
                # Centralized failover chain — walks every provider + verified
                # free-tier fallback models when the configured model is
                # premium-blocked (403) or the balance is exhausted (402).
                from app.core.llm_router import llm_chat as _failover_chat

                raw = await _failover_chat(
                    messages,
                    model=self._model,
                    max_tokens=1600,
                    temperature=0.3,
                    timeout=self._client_timeout,
                )
                return SaraResponse.model_validate(_extract_json(raw))
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)[:500]
                log.warning("Invalid SARA output (attempt %d): %s", attempt + 1, last_error)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": REPAIR_PROMPT.format(error=last_error)})
            except Exception as exc:  # network / provider errors
                last_error = repr(exc)
                log.error("LLM call failed: %s", last_error)
                break

        blocked = any(
            marker in last_error.lower()
            for marker in ("402", "credits", "insufficient", "max_tokens", "401", "invalid api key", "authentication")
        )
        return SaraResponse(
            reply=LLM_PROVIDER_BLOCKED_REPLY if blocked else FALLBACK_REPLY,
            intent="support",
            escalate_to_human=False,
            state=session.state,
            ui_options=FALLBACK_OPTIONS,
        )


# --------------------------------------------------------------------------- #
# Unipile client (client created lazily on first use)
# --------------------------------------------------------------------------- #
class UnipileClient:
    def __init__(self, dsn: str, token: str) -> None:
        self._dsn = dsn
        self._token = token
        self._client: httpx.AsyncClient | None = None

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._dsn,
                headers={"X-API-KEY": self._token, "accept": "application/json"},
                timeout=httpx.Timeout(20.0),
            )
        return self._client

    async def send_text(self, chat_id: str, text: str) -> None:
        if not self._dsn or not self._token:
            raise RuntimeError("UNIPILE_DSN / UNIPILE_ACCESS_TOKEN not configured")
        client = self._ensure()
        # Unipile expects multipart/form-data with a "text" field.
        resp = await client.post(
            f"/api/v1/chats/{chat_id}/messages",
            files={"text": (None, text)},
        )
        if resp.status_code >= 400:
            log.error("Unipile send failed %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- #
# Zernio client (alternative transport; https://zernio.com)
# --------------------------------------------------------------------------- #
class ZernioClient:
    """Sends inbox messages through the Zernio unified-messaging API.

    POST {base}/inbox/conversations/{conversationId}/messages
      {"accountId": "...", "message": "..."}
    """

    def __init__(self, api_key: str, base_url: str = ZERNIO_BASE_URL, account_id: str = "") -> None:
        self._api_key = api_key
        self._base_url = base_url
        self.account_id = account_id  # may be filled at runtime from the first webhook
        self._client: httpx.AsyncClient | None = None

    def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._api_key}", "accept": "application/json"},
                timeout=httpx.Timeout(20.0),
            )
        return self._client

    async def send_text(self, chat_id: str, text: str) -> None:
        if not self._api_key:
            raise RuntimeError("ZERNIO_API_KEY not configured")
        if not self.account_id:
            raise RuntimeError("ZERNIO_ACCOUNT_ID not configured (or no Zernio webhook seen yet)")
        client = self._ensure()
        resp = await client.post(
            f"{self._base_url}/inbox/conversations/{chat_id}/messages",
            json={"accountId": self.account_id, "message": text},
        )
        if resp.status_code >= 400:
            log.error("Zernio send failed %s: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

    async def send_interactive(self, chat_id: str, payload: dict[str, Any]) -> None:
        """Send a Meta-style interactive message (native buttons or a list).

        `payload` carries the extra top-level fields (message / buttons /
        interactive); accountId is added here. Fields are Meta Cloud API
        verbatim, which is what Zernio forwards for WhatsApp.
        """
        if not self._api_key:
            raise RuntimeError("ZERNIO_API_KEY not configured")
        if not self.account_id:
            raise RuntimeError("ZERNIO_ACCOUNT_ID not configured (or no Zernio webhook seen yet)")
        body: dict[str, Any] = {"accountId": self.account_id}
        body.update(payload)
        client = self._ensure()
        resp = await client.post(
            f"{self._base_url}/inbox/conversations/{chat_id}/messages",
            json=body,
        )
        if resp.status_code >= 400:
            log.error(
                "Zernio interactive send failed %s: %s", resp.status_code, resp.text[:300]
            )
            resp.raise_for_status()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- #
# Message handling
# --------------------------------------------------------------------------- #
def render_reply(resp: SaraResponse) -> str:
    if not resp.ui_options:
        return resp.reply
    menu = "\n".join(f"{i}. {opt}" for i, opt in enumerate(resp.ui_options, start=1))
    return f"{resp.reply}\n\n{menu}"


def resolve_menu_choice(text: str, options: list[str]) -> str:
    """If the guest answered with a number, translate it to the option label."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[.)]?\s*", text)
    if m and options:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return options[idx]
    return text


# --------------------------------------------------------------------------- #
# Native WhatsApp interactive messages (tappable buttons / list)
# --------------------------------------------------------------------------- #
# Meta forbids emojis & markdown in button titles; strip them plus markdown.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, supplemental symbols
    "\U00002600-\U000027BF"  # misc symbols / dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002B00-\U00002BFF"  # arrows / misc symbols
    "\U0000FE0F\U0000200D"  # variation selectors + ZWJ
    "]+",
    flags=re.UNICODE,
)

LIST_ROW_TITLE_LIMIT = 24  # Meta hard cap for list-row titles


def _clean_wa_label(raw: Any, limit: int) -> str:
    """Make an option safe for a WhatsApp button/list title: no markdown, no
    emojis, single-spaced, and truncated at a word boundary within `limit`."""
    label = re.sub(r"[*_~`|]", "", str(raw or ""))
    label = _EMOJI_RE.sub("", label)
    label = " ".join(label.split())
    if len(label) <= limit:
        return label
    cut = label.rfind(" ", 0, limit)
    return label[: cut if cut > 0 else limit].strip()


def build_interactive_payload(text: str, options: list[str]) -> dict[str, Any] | None:
    """Build the extra send fields for a native WhatsApp list picker.

    Every option set (1-5) renders as a "Choose an option" interactive list
    under "interactive" (Meta Cloud API verbatim, as Zernio requires). The
    top-level `buttons` quick-reply form is deliberately NOT used: Zernio
    returns 500 "Failed to send message" for it (verified 2026-09-04), while
    the list form is stable. Returns None when there is nothing tappable.
    """
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if not opts:
        return None

    rows = []
    for o in opts[:10]:
        label = _clean_wa_label(o, LIST_ROW_TITLE_LIMIT)
        if label:
            rows.append({"id": label, "title": label})
    if not rows:
        return None
    return {
        "interactive": {
            "type": "list",
            "body": {"text": text},
            "action": {
                "button": "Choose an option",
                "sections": [{"title": "Options", "rows": rows}],
            },
        }
    }


def merge_state(previous: BookingState, new: BookingState) -> BookingState:
    """Never let the model silently drop known values."""
    merged = previous.model_dump()
    for key, value in new.model_dump().items():
        if key == "ready_to_book" or (value is not None and value is not False):
            merged[key] = value
    return BookingState(**merged)


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_unipile_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalises a Unipile messaging webhook. Returns None when the event should be ignored."""
    event = _get(payload, "event", default="")
    if event not in ("message_received", "message"):
        return None
    if str(_get(payload, "account_type", default="")).upper() != "WHATSAPP":
        return None
    if UNIPILE_ACCOUNT_ID and _get(payload, "account_id") != UNIPILE_ACCOUNT_ID:
        return None
    if payload.get("is_group"):
        return None

    sender = payload.get("sender") or {}
    account_info = payload.get("account_info") or {}
    own_id = _get(account_info, "user_id", "provider_id")
    sender_provider_id = _get(sender, "attendee_provider_id", "provider_id")
    if payload.get("is_sender") or (own_id and sender_provider_id == own_id):
        return None  # message sent by the hotel account itself

    chat_id = _get(payload, "chat_id")
    message_id = _get(payload, "message_id", "id")
    if not chat_id or not message_id:
        return None

    text = _get(payload, "message", "text", default="")
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if not text:
        attachments = payload.get("attachments") or []
        text = "[Guest sent a non-text message"
        text += f" with {len(attachments)} attachment(s)]" if attachments else "]"

    number = str(sender_provider_id or "")
    number = number.split("@")[0] if number else ""

    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "sender_name": _get(sender, "attendee_name", default=""),
        "sender_number": number,
    }


def _interactive_reply_text(msg: dict[str, Any]) -> str:
    """Extract the label of a tapped WhatsApp button/list row.

    WhatsApp interactive replies carry no free text; Meta reports the tapped
    option inside an "interactive" object. Zernio passes that through, so scan
    both the message and its nested "message" container for button_reply /
    list_reply and return the title (or text/id as a fallback).
    """
    containers: list[dict[str, Any]] = []
    inter = msg.get("interactive")
    if isinstance(inter, dict):
        containers.append(inter)
    nested = msg.get("message")
    if isinstance(nested, dict):
        inter = nested.get("interactive")
        if isinstance(inter, dict):
            containers.append(inter)
        else:
            containers.append(nested)

    for container in containers:
        for key in ("button_reply", "list_reply", "reply"):
            part = container.get(key)
            if isinstance(part, dict):
                for field in ("title", "text", "description", "id"):
                    value = part.get(field)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        for field in ("title", "text"):
            value = container.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def parse_zernio_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalises a Zernio inbox webhook (event "message.received").

    Returns the same shape as parse_unipile_event() plus "account_id", so the
    rest of the engine (sessions, memory, LLM, reply) is transport-agnostic.
    """
    if payload.get("event") != "message.received":
        return None

    account = payload.get("account") or {}
    if str(account.get("platform", "")).lower() != "whatsapp":
        return None
    if ZERNIO_ACCOUNT_ID and str(account.get("id", "")) != ZERNIO_ACCOUNT_ID:
        return None

    msg = payload.get("message") or {}
    direction = str(msg.get("direction", "")).lower()
    if direction not in ("inbound", "incoming"):
        return None  # ignore messages the business account itself sent

    conversation = payload.get("conversation") or {}
    chat_id = msg.get("conversationId") or conversation.get("id")
    message_id = payload.get("id") or msg.get("platformMessageId") or msg.get("id")
    if not chat_id or not message_id:
        return None

    # Webhook payloads carry the text under "text"; the list-messages API
    # returns it under "message". A tapped button/list row arrives without
    # text and must be read from the interactive reply object. Accept all.
    text = msg.get("text")
    if not isinstance(text, str):
        nested = msg.get("message")
        text = nested if isinstance(nested, str) else ""
    text = text.strip()
    if not text:
        text = _interactive_reply_text(msg)
    if not text:
        attachments = msg.get("attachments") or []
        text = "[Guest sent a non-text message"
        text += f" with {len(attachments)} attachment(s)]" if attachments else "]"

    sender = msg.get("sender") or {}
    # Webhook shape nests the sender; list-messages flattens it on the message.
    name = (sender.get("name") or msg.get("senderName")
            or conversation.get("participantName") or "")
    number = str(
        sender.get("phoneNumber") or sender.get("id")
        or msg.get("senderPhoneNumber") or msg.get("senderId")
        or conversation.get("participantId") or ""
    )
    number = re.sub(r"\D", "", number)

    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "sender_name": str(name),
        "sender_number": number,
        "account_id": str(account.get("id") or msg.get("accountId") or ""),
    }


# --------------------------------------------------------------------------- #
# Agent engine (one instance lives in app/api/whatsapp_agent.py)
# --------------------------------------------------------------------------- #
class WhatsAppHotelAgent:
    def __init__(
        self,
        business_file: Path = BUSINESS_FILE,
        dsn: str = UNIPILE_DSN,
        access_token: str = UNIPILE_ACCESS_TOKEN,
        tenant_id: str = "whatsapp",
        db_path: Path | None = None,
    ) -> None:
        self.business = BusinessContext(business_file)
        from app.agents.whatsapp_knowledge import KnowledgeRepository
        from app.agents.whatsapp_memory import MemoryStore
        from app.agents.whatsapp_persona import PersonaStore
        from app.agents.whatsapp_sheets import WhatsAppSheets

        self.persona = PersonaStore()
        self.memory = MemoryStore(db_path=db_path, tenant_id=tenant_id)
        self.knowledge = KnowledgeRepository(db_path=db_path, tenant_id=tenant_id)
        self.sheets = WhatsAppSheets(db_path=db_path, tenant_id=tenant_id)
        self.sessions = SessionStore(memory=self.memory)
        self.seen_messages = SeenMessages()
        self.unipile = UnipileClient(dsn, access_token)
        self.zernio = ZernioClient(ZERNIO_API_KEY, ZERNIO_BASE_URL, account_id=ZERNIO_ACCOUNT_ID or "")
        self.llm = LlmClient(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, model=LLM_MODEL)
        # Set by app.services.whatsapp_channels.activate_primary() when a
        # website-managed channel is the Primary Command Center. When set,
        # all replies route through that exact number/provider instead of the
        # env-configured transport.
        self.channel_messenger = None

    @property
    def messenger_name(self) -> str:
        return "zernio" if WA_MESSENGER == "zernio" else "unipile"

    @property
    def configured(self) -> bool:
        if WA_MESSENGER == "zernio":
            return bool(ZERNIO_API_KEY and LLM_API_KEY)
        return bool(UNIPILE_DSN and UNIPILE_ACCESS_TOKEN and LLM_API_KEY)

    async def send_text(self, chat_id: str, text: str) -> None:
        """Deliver a plain-text reply through whichever messenger is active.

        Website-managed channels (Primary Command Center) win over the env
        transport; the env Zernio/Unipile path remains the fallback.
        """
        if self.channel_messenger is not None:
            await self.channel_messenger.send_text(chat_id, text)
            return
        if WA_MESSENGER == "zernio":
            await self.zernio.send_text(chat_id, text)
        else:
            await self.unipile.send_text(chat_id, text)

    async def _deliver(self, chat_id: str, resp: SaraResponse) -> str:
        """Send resp.reply with native WhatsApp buttons/list when possible.

        Zernio supports Meta interactive messages, so 1-3 ui_options become
        tappable buttons and 4-5 become a tappable list. Falls back to the
        numbered-text menu if the interactive payload can't be built or the
        send fails. Returns the text form stored to memory."""
        plain = render_reply(resp)
        if resp.ui_options and self.channel_messenger is not None:
            try:
                if await self.channel_messenger.send_interactive(
                    chat_id, resp.reply, list(resp.ui_options)
                ):
                    return plain
            except Exception as exc:
                log.warning(
                    "WhatsApp channel interactive send failed (%r) — numbered fallback", exc
                )
        elif resp.ui_options and WA_MESSENGER == "zernio":
            payload = build_interactive_payload(resp.reply, resp.ui_options)
            if payload:
                try:
                    await self.zernio.send_interactive(chat_id, payload)
                    return plain
                except Exception as exc:
                    log.warning(
                        "WhatsApp interactive send failed (%r) — numbered fallback", exc
                    )
        await self.send_text(chat_id, plain)
        return plain

    async def notify_staff(
        self, session: Session, sender_name: str, sender_number: str, last_text: str
    ) -> None:
        if not STAFF_CHAT_ID:
            return
        st = session.state
        summary = (
            "🚨 *SARA escalation*\n"
            f"Guest: {sender_name or st.guest_name or 'Unknown'} ({sender_number or st.phone_number or 'n/a'})\n"
            f"Chat ID: {session.chat_id}\n"
            f"Dates: {st.check_in or '?'} → {st.check_out or '?'} | Guests: {st.guests or '?'} | Room: {st.room_type or '?'}\n"
            f"Last message: _{last_text[:200]}_"
        )
        try:
            await self.send_text(STAFF_CHAT_ID, summary)
        except Exception as exc:
            log.error("Staff notification failed: %r", exc)

    async def _deliver_canned(
        self, chat_id: str, session: Session, user_text: str, resp: SaraResponse
    ) -> None:
        """Record a server-composed turn (menu / date picker / custom-date prompt)
        in history + memory and deliver it, mirroring the LLM turn flow."""
        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": resp.model_dump_json()})
        session.history = session.history[-HISTORY_TURNS:]
        session.last_options = resp.ui_options
        session.updated_at = time.time()
        try:
            outgoing = await self._deliver(chat_id, resp)
        except Exception as exc:
            log.error("Failed to deliver reply to %s: %r", chat_id, exc)
            return
        self.memory.add_message(chat_id, "out", outgoing, intent=resp.intent, delivery_status="sent")

    def _auto_resume(self, chat_id: str) -> bool:
        """Re-engage a chat that was handed to staff once the guest writes again.

        SARA only stays silent while a real human could still be taking over:
        if no staff channel is configured there is nobody to answer, and if the
        handoff is older than WA_AUTO_RESUME_SECONDS the guest has waited long
        enough. Returns True when SARA may answer this message."""
        try:
            paused_at = self.memory.paused_at(chat_id)
            if not paused_at:
                return True
            if not STAFF_CHAT_ID:
                log.info("Chat %s auto-resumed: no staff channel configured.", chat_id)
            else:
                when = datetime.fromisoformat(str(paused_at).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - when).total_seconds()
                if elapsed <= WA_AUTO_RESUME_SECONDS:
                    return False  # staff may still be handling this chat
                log.info("Chat %s auto-resumed after %.0fs with staff.", chat_id, elapsed)
            self.memory.set_paused(chat_id, paused=False)
            return True
        except Exception as exc:  # never trap a guest because of bookkeeping trouble
            log.warning("auto-resume check failed for %s: %r", chat_id, exc)
            self.memory.set_paused(chat_id, paused=False)
            return True

    async def handle_inbound(
        self, chat_id: str, text: str, sender_name: str = "", sender_number: str = ""
    ) -> None:
        session = self.sessions.get(chat_id)
        async with session.lock:
            if session.handed_off and not self._auto_resume(chat_id):
                log.info("Chat %s is with staff; SARA stays silent.", chat_id)
                return

            # Pre-fill what we already know from WhatsApp metadata.
            if sender_number and not session.state.phone_number:
                session.state.phone_number = sender_number

            self.memory.add_message(chat_id, "in", user_text := resolve_menu_choice(text, session.last_options))
            self.memory.extract_facts(chat_id, user_text)

            want_check_in = session.state.check_in is None
            want_check_out = session.state.check_out is None

            # 1) "Type my own date" — invite free-text date entry.
            if user_text.strip() == CUSTOM_DATE_OPTION and (want_check_in or want_check_out):
                await self._deliver_canned(
                    chat_id,
                    session,
                    user_text,
                    SaraResponse(
                        reply=(
                            "Of course — just type any date you like 🗓️ "
                            "(for example *next Friday* or *20 Oct*), and I'll take it from there."
                        ),
                        intent="availability" if want_check_in else "booking",
                        state=session.state,
                        ui_options=[],
                    ),
                )
                return

            # 2) A tap on one of the exact-date picker rows resolves deterministically:
            #    no model round-trip is needed to know that "Tue 8 Sep" means a date.
            picked: date | None = None
            if session.pending_dates:
                for cand in session.pending_dates:
                    if user_text.strip() == _date_label(cand):
                        picked = cand
                        break
            session.pending_dates = None
            if picked is not None:
                if session.state.check_in is None:
                    session.state.check_in = picked.isoformat()
                elif session.state.check_out is None:
                    session.state.check_out = picked.isoformat()
                log.info(
                    "chat=%s exact-date tap %r -> %s", chat_id, user_text.strip(), picked.isoformat()
                )

            # 3) Fresh conversation that opens with a greeting/"help"? Show the main
            #    menu first — never jump into date collection before intent is known.
            if len(session.history) == 0 and _GREETING_RE.match(user_text):
                await self._deliver_canned(
                    chat_id,
                    session,
                    user_text,
                    SaraResponse(
                        reply="Hi there! 👋 How can I help you today?",
                        intent="general",
                        state=session.state,
                        ui_options=list(MAIN_MENU_OPTIONS),
                    ),
                )
                return

            # 4) A vague date answer ("next week", "this weekend"...) must not let
            #    the model guess a day: offer the concrete dates as a picker first.
            concrete_dates = _vague_dates(user_text)
            if concrete_dates and len(concrete_dates) > 1 and (want_check_in or want_check_out):
                options = [_date_label(d) for d in concrete_dates]
                options.append(CUSTOM_DATE_OPTION)
                session.pending_dates = list(concrete_dates)  # maps the upcoming taps
                ask_in = want_check_in
                await self._deliver_canned(
                    chat_id,
                    session,
                    user_text,
                    SaraResponse(
                        reply=(
                            "Which exact day would you like to check in? 🗓️ Tap a date below, "
                            "or choose the last option to type any date."
                            if ask_in
                            else "Which exact day would you like to check out? 🗓️ Tap a date below, "
                            "or choose the last option to type any date."
                        ),
                        intent="availability" if ask_in else "booking",
                        state=session.state,
                        ui_options=options,
                    ),
                )
                log.info(
                    "chat=%s vague-date %r expanded to %d option(s)",
                    chat_id,
                    user_text,
                    len(options),
                )
                return

            guest_memory = self.memory.memory_context(chat_id)
            knowledge_text = self.knowledge.knowledge_context(user_text)
            resp = await self.llm.ask(
                self.business.load(),
                session,
                user_text,
                guest_memory=guest_memory,
                agent_persona=self.persona.load(),
                knowledge=knowledge_text,
            )

            # 5) Every genuine date QUESTION carries quick shortcuts plus a
            #    "Type my own date" row so the guest can pick or type freely.
            #    (Only an actual date ask qualifies — "how many nights?" does not.)
            if (
                resp.intent in ("booking", "availability")
                and resp.reply.rstrip().endswith("?")
                and _DATE_ASK_RE.search(resp.reply)
            ):
                if (want_check_in and resp.state.check_in is None) or (
                    want_check_out and resp.state.check_out is None
                ):
                    options = list(resp.ui_options)
                    if not options:
                        options = list(DATE_QUICK_OPTIONS)
                    if CUSTOM_DATE_OPTION not in options:
                        options.append(CUSTOM_DATE_OPTION)
                    resp.ui_options = options

            session.state = merge_state(session.state, resp.state)
            session.history.append({"role": "user", "content": user_text})
            session.history.append({"role": "assistant", "content": resp.model_dump_json()})
            session.history = session.history[-HISTORY_TURNS:]
            session.last_options = resp.ui_options
            session.updated_at = time.time()

            self.memory.save_state(
                chat_id,
                session.state.model_dump(),
                summary=f"{resp.intent}: {user_text[:200]}",
            )

            try:
                outgoing = await self._deliver(chat_id, resp)
            except Exception as exc:
                log.error("Failed to deliver reply to %s: %r", chat_id, exc)
                return
            self.memory.add_message(chat_id, "out", outgoing, intent=resp.intent, delivery_status="sent")

            log.info(
                "chat=%s intent=%s escalate=%s ready=%s",
                chat_id,
                resp.intent,
                resp.escalate_to_human,
                session.state.ready_to_book,
            )

            if resp.escalate_to_human:
                session.handed_off = True
                self.memory.set_paused(chat_id, paused=True)
                await self.notify_staff(session, sender_name, sender_number, user_text)

    async def aclose(self) -> None:
        await self.unipile.aclose()
        await self.zernio.aclose()
        for closer in (self.memory, self.knowledge, self.sheets):
            try:
                closer.close()
            except Exception:
                pass
