"""Voice Calling Agent — booking domain models, date intelligence and demo data.

This module is the data layer for the SARA Voice Calling Agent's appointment /
hotel-booking flow. It is deliberately free of imports from
``app.voice_agent_engine`` so the conversation engine (``voice_booking``) and
tests can use it standalone; the engine wires its SQLAlchemy session factory
in at startup.

Contents
    * conversation state machine states (BookingPhase)
    * BookingContext — what has already been collected from the caller
    * date intelligence: "8th", "8 October", "tomorrow", "next Friday",
      "this Sunday", "day after tomorrow", "the 8th next month" — resolved
      against the configured timezone with month/year boundary handling
    * AvailabilityEngine protocol + DemoHotelAvailability implementation:
      real room inventory, per-date availability, booking records, and a
      receptionist number — replaceable with a real hotel/booking API later
    * persona configuration (PersonaConfig) with a complete built-in demo
      hotel-receptionist persona that needs no manual setup
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("sara.voice.booking")

# ---------------------------------------------------------------------------
# Conversation state machine
# ---------------------------------------------------------------------------


class BookingPhase(str, Enum):
    """States of a live booking conversation.

    Persisted per call in voice_calls.extracted_data["booking_context"] so the
    engine survives restarts and the frontend can render the live state.
    """

    IDLE = "IDLE"
    CALL_CONNECTED = "CALL_CONNECTED"
    GREETING = "GREETING"
    COLLECTING_REQUIREMENTS = "COLLECTING_REQUIREMENTS"
    DATE_IDENTIFIED = "DATE_IDENTIFIED"
    CHECKING_AVAILABILITY = "CHECKING_AVAILABILITY"
    DATE_AVAILABLE = "DATE_AVAILABLE"
    DATE_UNAVAILABLE = "DATE_UNAVAILABLE"
    SUGGESTING_ALTERNATIVES = "SUGGESTING_ALTERNATIVES"
    CONFIRMING_SELECTION = "CONFIRMING_SELECTION"
    READY_FOR_TRANSFER = "READY_FOR_TRANSFER"
    TRANSFERRING = "TRANSFERRING"
    TRANSFER_SUCCESS = "TRANSFER_SUCCESS"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    # The call is done but the line is deliberately KEPT OPEN. The agent asks
    # whether the guest needs anything else instead of hanging up, which is
    # what made SARA appear to cut callers off after a successful booking.
    WRAP_UP = "WRAP_UP"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    CALL_ENDING = "CALL_ENDING"
    CALL_ENDED = "CALL_ENDED"


# Legal transitions — mirrored after voice_agent_engine.VALID_TRANSITIONS but
# scoped to the booking flow. Guards live in voice_booking.BookingEngine.
BOOKING_TRANSITIONS: Dict[BookingPhase, set] = {
    BookingPhase.IDLE: {BookingPhase.CALL_CONNECTED},
    BookingPhase.CALL_CONNECTED: {BookingPhase.GREETING, BookingPhase.CALL_ENDING},
    BookingPhase.GREETING: {BookingPhase.COLLECTING_REQUIREMENTS, BookingPhase.CALL_ENDING},
    BookingPhase.COLLECTING_REQUIREMENTS: {
        BookingPhase.DATE_IDENTIFIED,
        BookingPhase.WRAP_UP,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.DATE_IDENTIFIED: {
        BookingPhase.CHECKING_AVAILABILITY,
        BookingPhase.COLLECTING_REQUIREMENTS,  # ambiguous date -> clarify
    },
    BookingPhase.CHECKING_AVAILABILITY: {
        BookingPhase.DATE_AVAILABLE,
        BookingPhase.DATE_UNAVAILABLE,
        BookingPhase.ERROR_RECOVERY,
    },
    BookingPhase.DATE_AVAILABLE: {
        BookingPhase.READY_FOR_TRANSFER,
        BookingPhase.WRAP_UP,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.DATE_UNAVAILABLE: {BookingPhase.SUGGESTING_ALTERNATIVES, BookingPhase.CALL_ENDING},
    # Booking is done but the call is deliberately kept open.
    BookingPhase.BOOKING_CONFIRMED: {
        BookingPhase.WRAP_UP,
        BookingPhase.CALL_ENDING,
    },
    # The guest is asked "anything else?". They may keep asking questions, or
    # decline and end the call. WRAP_UP -> WRAP_UP is the normal loop.
    BookingPhase.WRAP_UP: {
        BookingPhase.WRAP_UP,
        BookingPhase.CALL_ENDING,
        BookingPhase.READY_FOR_TRANSFER,
    },
    BookingPhase.SUGGESTING_ALTERNATIVES: {
        BookingPhase.CONFIRMING_SELECTION,
        BookingPhase.READY_FOR_TRANSFER,
        BookingPhase.WRAP_UP,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.CONFIRMING_SELECTION: {
        BookingPhase.READY_FOR_TRANSFER,
        BookingPhase.SUGGESTING_ALTERNATIVES,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.READY_FOR_TRANSFER: {
        BookingPhase.WRAP_UP,
        BookingPhase.TRANSFERRING,
        BookingPhase.BOOKING_CONFIRMED,  # caller declines transfer; booking stands
        BookingPhase.COLLECTING_REQUIREMENTS,  # caller changes the date
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.TRANSFERRING: {BookingPhase.TRANSFER_SUCCESS, BookingPhase.TRANSFER_FAILED},
    BookingPhase.TRANSFER_SUCCESS: {BookingPhase.CALL_ENDED},
    BookingPhase.TRANSFER_FAILED: {
        BookingPhase.READY_FOR_TRANSFER,  # retry once
        BookingPhase.ERROR_RECOVERY,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.ERROR_RECOVERY: {
        BookingPhase.COLLECTING_REQUIREMENTS,
        BookingPhase.CHECKING_AVAILABILITY,
        BookingPhase.CALL_ENDING,
    },
    BookingPhase.CALL_ENDING: {BookingPhase.CALL_ENDED},
    BookingPhase.CALL_ENDED: set(),
}


# ---------------------------------------------------------------------------
# Collected booking requirements
# ---------------------------------------------------------------------------


@dataclass
class BookingContext:
    """Everything collected so far in the conversation.

    Serialized into the call record; the engine never relies on keyword
    matching over the transcript to remember what has been collected.
    """

    session_key: str = ""
    call_id: Optional[str] = None
    caller_phone: Optional[str] = None
    caller_name: Optional[str] = None
    language: str = "en-IN"

    requested_date: Optional[str] = None       # ISO yyyy-mm-dd (or check-in date)
    requested_date_raw: Optional[str] = None   # what the caller actually said
    requested_time: Optional[str] = None       # HH:MM when provided
    nights: int = 1
    guests: Optional[int] = None
    service: Optional[str] = None              # room type / service requested
    guest_name: Optional[str] = None

    alternatives_offered: List[str] = field(default_factory=list)  # ISO dates
    selected_date: Optional[str] = None
    transfer_requested: bool = False
    direct_booking: bool = False  # caller declined transfer; agent books directly
    booking_id: Optional[str] = None
    # True once SARA has offered the caller a choice of booking directly or
    # being connected to staff. Lets the next turn tell "book it" apart from
    # "forward me" without re-asking.
    staff_transfer_offered: bool = False
    # Counts how many times the agent has asked for the guest count without a
    # usable answer. Guards against an unbreakable repeat-the-question loop.
    asked_guests: int = 0
    # Counts how many times the agent has asked which date without a usable
    # answer. Same loop protection as asked_guests.
    asked_dates: int = 0
    # Spoken summary of the room types actually free on the requested date
    # (with prices), built from the real availability result so the agent
    # offers the caller a genuine choice instead of a bare "it is available".
    room_summary: str = ""
    # True once the caller has been offered the booking / staff hand-off choice.
    choice_offered: bool = False
    # Set when the caller has asked whether they need anything else, so the
    # agent can keep the conversation open instead of hanging up.
    wrap_up_asked: bool = False
    open_queries: int = 0
    # Set once the opening greeting has been delivered, so it is never repeated.
    greeting_completed: bool = False
    phase: str = BookingPhase.IDLE.value
    turn_count: int = 0
    language_detected: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_key": self.session_key,
            "call_id": self.call_id,
            "caller_phone": self.caller_phone,
            "caller_name": self.caller_name,
            "language": self.language,
            "requested_date": self.requested_date,
            "requested_date_raw": self.requested_date_raw,
            "requested_time": self.requested_time,
            "nights": self.nights,
            "guests": self.guests,
            "service": self.service,
            "guest_name": self.guest_name,
            "alternatives_offered": list(self.alternatives_offered),
            "selected_date": self.selected_date,
            "transfer_requested": self.transfer_requested,
            "direct_booking": self.direct_booking,
            "staff_transfer_offered": self.staff_transfer_offered,
            "greeting_completed": self.greeting_completed,
            "booking_id": self.booking_id,
            "phase": self.phase,
            "turn_count": self.turn_count,
            "language_detected": self.language_detected,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "BookingContext":
        data = data or {}
        ctx = cls()
        for key, value in data.items():
            if hasattr(ctx, key) and key != "phase":
                setattr(ctx, key, value)
        try:
            ctx.phase = BookingPhase(data.get("phase", BookingPhase.IDLE.value)).value
        except ValueError:
            ctx.phase = BookingPhase.IDLE.value
        return ctx

    def missing(self) -> List[str]:
        """Required fields still missing, in the order the agent should ask."""
        missing = []
        if not self.requested_date:
            missing.append("date")
        if self.guests is None:
            missing.append("guests")
        if not self.guest_name:
            missing.append("name")
        return missing

    def phase_value(self) -> BookingPhase:
        """Current phase as an enum (safe against stale serialized values)."""
        try:
            return BookingPhase(self.phase)
        except ValueError:
            return BookingPhase.IDLE


# ---------------------------------------------------------------------------
# Date intelligence
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_ORDINAL_SUFFIX_RE = re.compile(r"^(\d{1,2})(st|nd|rd|th)$", re.IGNORECASE)


def parse_day_number(token: str) -> Optional[int]:
    """'8th' / '8' / 'eighth' -> 8."""
    token = (token or "").strip().lower()
    if not token:
        return None
    m = _ORDINAL_SUFFIX_RE.match(token)
    if m:
        day = int(m.group(1))
        return day if 1 <= day <= 31 else None
    if token.isdigit():
        day = int(token)
        return day if 1 <= day <= 31 else None
    ordinals = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
        "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
        "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
        "nineteenth": 19, "twentieth": 20, "twenty first": 21, "twenty-first": 21,
        "twenty second": 22, "twenty-second": 22, "twenty third": 23,
        "twenty-third": 23, "twenty fourth": 24, "twenty-fourth": 24,
        "twenty fifth": 25, "twenty-fifth": 25, "twenty sixth": 26,
        "twenty-sixth": 26, "twenty seventh": 27, "twenty-seventh": 27,
        "twenty eighth": 28, "twenty-eighth": 28, "twenty ninth": 29,
        "twenty-ninth": 29, "thirtieth": 30, "thirty first": 31, "thirty-first": 31,
    }
    # Tolerate trailing punctuation/whitespace so "eighth." or " eighth " resolve.
    return ordinals.get(token.strip(" .!?,:;-"))


def local_now(tz_name: str = "Asia/Kolkata") -> datetime:
    """Current time in the configured business timezone (falls back to UTC)."""
    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone %r — falling back to UTC", tz_name)
        return datetime.now(dt_timezone.utc)


def _day_in_month(day: int, year: int, month: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_weekday(base: date, weekday: int) -> date:
    days_ahead = (weekday - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # "next Friday" always moves forward a full week
    return base + timedelta(days=days_ahead)


def _this_weekday(base: date, weekday: int) -> date:
    days_ahead = (weekday - base.weekday()) % 7
    return base + timedelta(days=days_ahead)


class AmbiguousDate(Exception):
    """Raised when a date phrase could mean more than one date."""

    def __init__(self, candidates: List[date], question: str):
        self.candidates = candidates
        self.question = question
        super().__init__(question)


def resolve_date_phrase(text: str, *, now: Optional[datetime] = None,
                        tz_name: str = "Asia/Kolkata") -> Optional[date]:
    """Resolve a natural-language date phrase to a concrete date.

    Handles: "8th", "the 8th", "8 October", "October 8th", "tomorrow",
    "day after tomorrow", "next Friday", "this Sunday", "the 8th next month",
    ISO "2026-10-08", "8/10" (day-first for Indian/UK style).

    Raises AmbiguousDate when the phrase cannot be resolved without asking
    ("8th" with no month when it is already past this month → could be this
    month or next), so the agent asks instead of guessing.
    """
    now = now or local_now(tz_name)
    today = now.date()
    text = " ".join((text or "").lower().split())
    if not text:
        return None

    # ISO date
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    if re.search(r"\btoday\b|\bajj\b|\baaj\b", text):
        return today
    if re.search(r"day after tomorrow|parso|day after", text):
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b|\bkal\b|\btmrw\b", text):
        return today + timedelta(days=1)

    # Relative offsets: "in two days", "two days later", "2 days after that".
    # Callers use these constantly once a date is agreed, and returning None
    # made SARA re-ask for the date instead of nudging it forward.
    # Spoken numbers are handled too — STT hands us words, not digits.
    _NUM_WORDS = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
    }
    rel = re.search(
        r"\b(?:in|after|within)?\s*(\d{1,2}|one|two|three|four|five|six|seven|"
        r"eight|nine|ten|couple|few)\s*days?\s*"
        r"(later|after|from now|hence|baad)?\b",
        text,
    )
    if rel:
        token = rel.group(1)
        n = int(token) if token.isdigit() else _NUM_WORDS.get(token, 0)
        if 0 < n <= 365:
            return today + timedelta(days=n)

    # "next <weekday>" / "this <weekday>" / bare "<weekday>"
    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            if re.search(rf"next\s+{name}", text):
                return _next_weekday(today, weekday)
            if re.search(rf"(this|coming|upcoming)\s+{name}", text):
                candidate = _this_weekday(today, weekday)
                return candidate if candidate != today else candidate + timedelta(days=7)
            # bare weekday — nearest future occurrence
            return _this_weekday(today, weekday) or today

    # "the 8th of next month" / "8th next month"
    day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)
    day_word_match = None
    for token in text.replace(",", " ").split():
        parsed = parse_day_number(token)
        if parsed and not token.isdigit():
            day_word_match = parsed
            break

    explicit_month: Optional[int] = None
    for name, num in _MONTHS.items():
        if re.search(rf"\b{name}\b", text):
            explicit_month = num
            break

    next_month = bool(re.search(r"next month|agla mahina", text))
    this_month = bool(re.search(r"this month|is mahine", text))

    day = day_word_match or (parse_day_number(day_match.group(1)) if day_match else None)

    if explicit_month is not None:
        year = now.year
        candidate = _day_in_month(day or 1, year, explicit_month) if day else None
        if candidate is None:
            return None
        # "8 October" said in November means next year's October
        if candidate < today and not next_month and (now.month - explicit_month) >= 6:
            candidate = _day_in_month(day, year + 1, explicit_month) or candidate
        return candidate

    if next_month and day:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        candidate = _day_in_month(day, year, month)
        if candidate:
            return candidate
        raise AmbiguousDate([], f"I couldn't find a {day}th in next month — which date works for you?")

    if day:
        # Bare day number: require date context (an ordinal "8th", "the 8th",
        # or booking words) so a bare "2" answering "how many guests?" is not
        # mistaken for the 2nd of the month.
        has_context = bool(
            re.search(
                r"\b(book|booking|reserve|reservation|appointment|schedule|check|"
                r"date|stay|visit|room|night|nights|table|slot|on the|for the)\b",
                text,
            )
        )
        # NOTE: day_match is None when the day came from a WORD ordinal
        # ("eighth"), so it must be guarded — otherwise the caller saying
        # "eighth" raised AttributeError and the whole turn blew up.
        has_ordinal = (
            day_match is not None and _ORDINAL_SUFFIX_RE.match(day_match.group(1)) is not None
        ) or (day_word_match is not None)
        has_the = bool(re.search(r"\bthe\s+", text))
        if not (has_context or has_ordinal or has_the):
            day = None
    if day:
        # Prefer this month; if already past, use next month.
        candidate = _day_in_month(day, now.year, now.month)
        if candidate and candidate >= today:
            return candidate
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        candidate_next = _day_in_month(day, year, month)
        if this_month and candidate is not None:
            return candidate
        if candidate_next:
            return candidate_next
        return candidate

    # numeric d/m
    dm = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if dm:
        d_num, m_num = int(dm.group(1)), int(dm.group(2))
        year = dm.group(3)
        if year:
            year = int(year)
            if year < 100:
                year += 2000
        else:
            year = now.year
        if m_num < 1 or m_num > 12 or d_num < 1 or d_num > 31:
            return None
        candidate = _day_in_month(d_num, year, m_num)
        if candidate and not year and candidate < today:
            candidate = _day_in_month(d_num, year + 1, m_num)
        return candidate

    return None


_TIME_RE = re.compile(
    r"\b(?:(\d{1,2})[:.](\d{2})|(\d{1,2}))\s*(a\.?m\.?|p\.?m\.?|o'?clock|hours?|baje)?\b",
    re.IGNORECASE,
)


def parse_time_phrase(text: str) -> Optional[str]:
    """Extract a time as HH:MM (24h) from '3 pm', '15:30', '11 o'clock'...

    Bare 12-hour numbers spoken without a marker are genuinely ambiguous
    ('check in at 3' could be 03:00 or 15:00), so a hotel context resolves a
    small number to the afternoon. Explicit words ("evening", "night",
    "afternoon") are honoured because callers say them constantly — reading
    "2:00 in the evening" as 02:00 would silently book the wrong time.
    """
    text = (text or "").lower().replace("o'clock", "oclock")
    m = _TIME_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1) or m.group(3) or 0)
    minute = int(m.group(2) or 0)
    suffix = (m.group(4) or "").replace(".", "").replace(" ", "")
    if hour < 1 or hour > 23:
        if hour == 24:
            hour = 0
        else:
            return None
    if suffix.startswith("pm") and hour < 12:
        hour += 12
    if suffix.startswith("am") and hour == 12:
        hour = 0
    if suffix in ("oclock", "hour", "hours", "baje") and hour < 8 and "pm" in text:
        hour += 12

    # Spoken period words, with or without a meridiem suffix.
    if not suffix.startswith(("am", "pm")):
        if re.search(r"\b(evening|night|afternoon|sham|dopahar)\b", text):
            if hour < 12:
                hour += 12
        elif re.search(r"\b(morning|subah|beforenoon)\b", text) and hour == 12:
            hour = 0
        # "check in at 3" for a hotel means afternoon, not 3 a.m.
        elif (
            hour < 8
            and re.search(r"\b(check\s*-?\s*in|arriv\w*|book\w*)\b", text)
        ):
            hour += 12

    return f"{hour:02d}:{minute:02d}"


def parse_guest_count(text: str) -> Optional[int]:
    """'2 people', 'for two', '4 guests', 'party of 3' -> int.

    A bare number reply ("2") also counts — the engine asks for the guest
    count before reading this value, so a whole-message number is the answer.
    """
    text_l = " ".join((text or "").lower().split())
    if re.fullmatch(r"\d{1,2}", text_l or ""):
        n = int(text_l)
        return n if 1 <= n <= 30 else None
    # A bare spoken answer ("two", "three", "we are two") is the MOST common
    # reply once the agent has asked how many guests. It must be accepted: the
    # old parser only matched "for two"/"two people", so `turn.guests` stayed
    # None, the agent never recorded the answer and asked the same question
    # again and again — an unbreakable loop on a live call.
    bare = re.fullmatch(r"(?:yes\s+|we(?:'re| are)\s+|just\s+)*([a-z]+)", text_l or "")
    if bare:
        word = bare.group(1)
        if word in _NUMBER_WORDS:
            return _NUMBER_WORDS[word]
    # "just me", "myself", "alone" -> one guest.
    if re.fullmatch(r"(?:just\s+)?(?:me|myself|alone|solo|one)", text_l or ""):
        return 1
    text = text_l
    m = re.search(r"\b(\d{1,2})\s*(?:people|persons|person|guests?|adults?|pax|members?)\b", text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 30 else None
    m = re.search(r"\bparty of\s+(\d{1,2})\b", text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 30 else None
    # Compound spoken numbers FIRST: "twenty five guests" otherwise matches the
    # single word "five" below and returns 5, so a 25-person request was
    # checked against 5 and offered a room the hotel could not actually hold.
    compound = _parse_compound_number(text)
    if compound:
        return compound
    for word, n in _NUMBER_WORDS.items():
        if re.search(rf"\bfor\s+{word}\b|\b{word}\s+(?:people|guests?)\b", text):
            return n
    m = re.search(r"\bsingle\b|\bjust me\b|\balone\b", text)
    if m:
        return 1
    return None


def _parse_compound_number(text: str) -> Optional[int]:
    """'twenty five' / 'thirty two' / 'ten of us' -> int."""
    m = re.search(
        r"\b(twenty|thirty|forty|fifty)[\s-](one|two|three|four|five|six|seven|eight|nine)\b",
        text,
    )
    if m:
        tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}[m.group(1)]
        units = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9,
        }
        return tens + units[m.group(2)]
    # "ten of us", "a dozen of us"
    m = re.search(r"\b(ten|eleven|twelve|dozen)\s+of\s+us\b", text)
    if m:
        return {"ten": 10, "eleven": 11, "twelve": 12, "dozen": 12}[m.group(1)]
    return None


def parse_nights(text: str) -> Optional[int]:
    """'for 2 nights', 'two nights', 'weekend (2 nights)'."""
    text = (text or "").lower()
    m = re.search(r"\b(\d{1,2})\s*nights?\b", text)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 30 else None
    for word, n in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\s+nights?\b", text):
            return n
    return None


_SERVICE_WORDS = [
    "standard room", "deluxe room", "deluxe", "suite", "presidential suite",
    "single room", "double room", "twin room", "family room", "executive room",
    "ac room", "non-ac room", "ac", "non ac", "table", "appointment",
    "consultation", "checkup", "haircut", "demo",
]


def parse_service(text: str) -> Optional[str]:
    """Extract a room type / service phrase if present."""
    text = " ".join((text or "").lower().split())
    for service in _SERVICE_WORDS:
        if re.search(rf"\b{re.escape(service)}\b", text):
            return service
    m = re.search(r"\b(?:a|the)\s+(?:room|table)\s+for\b", text)
    if m:
        return "room"
    if re.search(r"\broom\b", text):
        return "room"
    return None


def parse_name(text: str) -> Optional[str]:
    """'my name is X', 'I'm X', 'this is X' -> X (best-effort)."""
    text = " ".join((text or "").strip().split())
    m = re.search(
        r"\b(?:my name is|my name's|this is|i am|i'm|im|it's|its|name is)\s+([A-Za-z][A-Za-z .'-]{1,40})$",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip().title()
    return None


# ---------------------------------------------------------------------------
# Availability engine
# ---------------------------------------------------------------------------


@dataclass
class AvailabilityResult:
    """Outcome of one availability check — never fabricated by the agent."""

    requested_date: str
    available: bool
    remaining: int = 0
    room_types: List[str] = field(default_factory=list)
    message: str = ""
    source: str = "demo"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_date": self.requested_date,
            "available": self.available,
            "remaining": self.remaining,
            "room_types": list(self.room_types),
            "message": self.message,
            "source": self.source,
        }


@dataclass
class BookingResult:
    created: bool
    booking_id: Optional[str] = None
    message: str = ""
    source: str = "demo"


class AvailabilityEngine(Protocol):
    """Contract for real booking APIs to implement (hotel PMS, Calendly...)."""

    name: str

    def check_date(
        self,
        target: date,
        *,
        guests: Optional[int] = None,
        service: Optional[str] = None,
    ) -> AvailabilityResult: ...

    def nearby_availability(
        self,
        target: date,
        *,
        guests: Optional[int] = None,
        service: Optional[str] = None,
        window: int = 2,
    ) -> List[date]: ...

    def create_booking(
        self,
        *,
        booking_date: str,
        guests: int,
        guest_name: str,
        service: Optional[str] = None,
        time_str: Optional[str] = None,
        phone: Optional[str] = None,
        notes: str = "",
    ) -> BookingResult: ...


class DemoHotelAvailability:
    """File-backed demo hotel with REAL inventory the flow executes against.

    Room inventory, per-date availability and booking records live in
    backend/voice_demo_hotel.json (created on first use). Availability answers
    are computed from the stored inventory minus real bookings — nothing is
    hardcoded in conversational replies. Swap this class for an adapter to a
    real PMS/booking API to go live; the conversation engine only talks to the
    AvailabilityEngine protocol.
    """

    name = "demo-hotel"

    DEFAULT_DATA: Dict[str, Any] = {
        "hotel": {
            "name": "Grand Stay Hotel",
            "timezone": "Asia/Kolkata",
            "check_in_time": "14:00",
            "check_out_time": "11:00",
            "currency": "INR",
        },
        "receptionist_number": os.getenv("VOICE_DEMO_RECEPTIONIST_NUMBER", "+919000000001"),
        "rooms": [
            {"type": "Standard", "total": 8, "price_per_night": 3500},
            {"type": "Deluxe", "total": 4, "price_per_night": 5500},
            {"type": "Suite", "total": 2, "price_per_night": 9000},
        ],
        # Closed / fully-booked patterns are expressed as blocked dates so the
        # demo genuinely returns "unavailable" for some days.
        "blocked_dates": [],
        "bookings": [],
    }

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(
            path
            or os.getenv(
                "VOICE_DEMO_HOTEL_FILE",
                str(Path(__file__).resolve().parent.parent.parent / "voice_demo_hotel.json"),
            )
        )
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(self._data, dict) or "rooms" not in self._data:
                    raise ValueError("invalid demo hotel file")
            except FileNotFoundError:
                self._data = json.loads(json.dumps(self.DEFAULT_DATA))
                self._save()
            except Exception as exc:
                log.error("Demo hotel data unreadable (%s) — recreating", exc)
                self._data = json.loads(json.dumps(self.DEFAULT_DATA))
                self._save()

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self._path)

    @property
    def hotel(self) -> Dict[str, Any]:
        return dict(self._data.get("hotel", {}))

    @property
    def receptionist_number(self) -> str:
        return str(self._data.get("receptionist_number") or "")

    # -- availability ----------------------------------------------------------

    def _room_total(self, service: Optional[str]) -> int:
        rooms = self._data.get("rooms") or []
        if service:
            for room in rooms:
                if str(room.get("type", "")).lower() in (service or "").lower():
                    return int(room.get("total", 0))
        return sum(int(room.get("total", 0)) for room in rooms)

    def _booked_count(self, target: date, service: Optional[str]) -> int:
        iso = target.isoformat()
        count = 0
        for booking in self._data.get("bookings") or []:
            if booking.get("date") != iso:
                continue
            if booking.get("status") in ("cancelled",):
                continue
            if service:
                room_type = str(booking.get("service") or "").lower()
                if room_type and room_type in (service or "").lower():
                    count += 1
            else:
                count += 1
        return count

    def check_date(
        self,
        target: date,
        *,
        guests: Optional[int] = None,
        service: Optional[str] = None,
    ) -> AvailabilityResult:
        with self._lock:
            iso = target.isoformat()
            blocked = iso in set(self._data.get("blocked_dates") or [])
            total = self._room_total(service)
            booked = self._booked_count(target, service)
            remaining = max(0, total - booked)
            available = (not blocked) and remaining > 0 and (
                guests is None or remaining >= guests
            )
            room_types = [
                str(r.get("type")) for r in (self._data.get("rooms") or []) if r.get("type")
            ]
            return AvailabilityResult(
                requested_date=iso,
                available=available,
                remaining=remaining,
                room_types=room_types,
                message=(
                    "available" if available else
                    ("closed" if blocked else "fully booked")
                ),
                source=self.name,
            )

    def nearby_availability(
        self,
        target: date,
        *,
        guests: Optional[int] = None,
        service: Optional[str] = None,
        window: int = 2,
    ) -> List[date]:
        """Actual available dates near `target`: -1, +1 first, then ±2... ±window."""
        with self._lock:
            found: List[date] = []
            for delta in range(1, max(1, window) + 1):
                for candidate in (target - timedelta(days=delta), target + timedelta(days=delta)):
                    if candidate in found or candidate < date.today():
                        continue
                    if self.check_date(candidate, guests=guests, service=service).available:
                        found.append(candidate)
            found.sort()
            return found

    def create_booking(
        self,
        *,
        booking_date: str,
        guests: int,
        guest_name: str,
        service: Optional[str] = None,
        time_str: Optional[str] = None,
        phone: Optional[str] = None,
        notes: str = "",
    ) -> BookingResult:
        with self._lock:
            try:
                target = date.fromisoformat(booking_date)
            except ValueError:
                return BookingResult(False, message="invalid booking date")
            result = self.check_date(target, guests=guests, service=service)
            if not result.available:
                return BookingResult(
                    False,
                    message=f"{booking_date} is not available ({result.message})",
                )
            booking_id = f"vb_{uuid.uuid4().hex[:10]}"
            self._data.setdefault("bookings", []).append(
                {
                    "id": booking_id,
                    "date": booking_date,
                    "time": time_str or "",
                    "guests": guests,
                    "guest_name": guest_name,
                    "service": service or "",
                    "phone": phone or "",
                    "notes": notes,
                    "status": "confirmed",
                    "created_at": datetime.now(dt_timezone.utc).isoformat(),
                    "source": "voice-agent",
                }
            )
            self._save()
            log.info("demo booking created id=%s date=%s guests=%s", booking_id, booking_date, guests)
            return BookingResult(True, booking_id=booking_id, message="booking confirmed")

    def list_bookings(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list((self._data.get("bookings") or [])[-limit:])


# ---------------------------------------------------------------------------
# Persona configuration
# ---------------------------------------------------------------------------


@dataclass
class PersonaConfig:
    """Configurable agent persona — everything a business can customize.

    The DEFAULT is a fully working Hotel Reservation Receptionist so the voice
    agent runs out of the box with zero configuration, exactly like the
    WhatsApp agent's demo hotel.
    """

    name: str = "SARA Hotel Booking Receptionist"
    role: str = (
        "Professional hotel reservation and front-desk voice assistant."
    )
    business_name: str = "Grand Stay Hotel"
    tone: str = "Natural, polite, confident, concise, professional, human-like."
    language: str = "en-IN"
    greeting: str = (
        "Hello! This is SARA, the receptionist at {business_name}. "
        "How may I help you today?"
    )
    # Hindi-first welcome (the caller hears this first), then the conversation
    # mirrors whichever language the caller uses — Hindi, English or Hinglish.
    booking_rules: List[str] = field(default_factory=lambda: [
        "Ask one relevant question at a time.",
        "Do not repeat information unnecessarily.",
        "Confirm important booking details before proceeding.",
        "Never invent room availability — always report the real check result.",
        "Never claim a reservation is completed unless the booking system confirms it.",
        "When the requested date is unavailable, offer the real nearby available dates.",
        "Offer to connect to the receptionist when a date is confirmed available.",
    ])
    cancellation_rules: List[str] = field(default_factory=lambda: [
        "Cancellations are handled by the receptionist — offer to transfer the call.",
    ])
    transfer_rules: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "require_caller_confirmation": True,
        "auto_transfer_on_unavailable": False,
        "message_before_transfer": (
            "Sure, connecting you to the receptionist now. Please hold while I forward the call."
        ),
    })
    business_hours: Dict[str, str] = field(default_factory=lambda: {
        "open": "08:00", "close": "22:00", "timezone": "Asia/Kolkata",
    })
    escalation_rules: List[str] = field(default_factory=lambda: [
        "If the caller is frustrated or asks for a human, offer the receptionist transfer.",
        "If a technical problem repeats, apologise and offer the receptionist transfer.",
    ])
    knowledge_base: str = ""
    receptionist_number: str = ""
    booking_system: str = "demo-hotel"
    fallback_behavior: str = (
        "On any technical trouble, apologise briefly, keep the call alive, and "
        "offer to connect the caller to the receptionist."
    )
    max_call_seconds: int = int(os.getenv("VOICE_MAX_CALL_SECONDS", "900"))
    is_default: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "business_name": self.business_name,
            "tone": self.tone,
            "language": self.language,
            "greeting": self.greeting,
            "booking_rules": list(self.booking_rules),
            "cancellation_rules": list(self.cancellation_rules),
            "transfer_rules": dict(self.transfer_rules),
            "business_hours": dict(self.business_hours),
            "escalation_rules": list(self.escalation_rules),
            "knowledge_base": self.knowledge_base,
            "receptionist_number": self.receptionist_number,
            "booking_system": self.booking_system,
            "fallback_behavior": self.fallback_behavior,
            "max_call_seconds": self.max_call_seconds,
            "is_default": self.is_default,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]], *, default: "PersonaConfig") -> "PersonaConfig":
        """Merge a (possibly partial) user persona over the built-in default.

        Unset fields keep the working defaults, so a caller can set just a
        receptionist number or business name and still get a complete persona.
        """
        data = data or {}
        merged = default.to_dict()
        for key, value in data.items():
            if key in merged and value not in (None, "", [], {}):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        persona = cls()
        for key, value in merged.items():
            if hasattr(persona, key):
                setattr(persona, key, value)
        persona.is_default = bool(merged.get("is_default", False))
        return persona

    def greeting_text(self) -> str:
        return self.greeting.format(business_name=self.business_name, name=self.name)


def load_persona(path: Path) -> PersonaConfig:
    """Load the persona file if present; otherwise return the built-in default."""
    default = PersonaConfig(receptionist_number=_env_receptionist_default())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning("Persona file unreadable (%s) — using default hotel persona", exc)
        return default
    if not isinstance(data, dict):
        return default
    # Accept both structured persona objects and a flat "persona" string.
    if isinstance(data.get("persona"), str) and "name" not in data:
        text = data["persona"].strip()
        if not text:
            return default
        default.name = text[:80]
        default.knowledge_base = text
        default.is_default = False
        return default
    return PersonaConfig.from_dict(data, default=default)


def _env_receptionist_default() -> str:
    return os.getenv("VOICE_RECEPTIONIST_NUMBER", "")


def save_persona(path: Path, data: Dict[str, Any]) -> PersonaConfig:
    """Persist a custom persona; `is_default` becomes False."""
    default = PersonaConfig()
    persona = PersonaConfig.from_dict(data, default=default)
    persona.is_default = False
    path.write_text(
        json.dumps(persona.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return persona
