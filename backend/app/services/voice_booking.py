"""Voice Calling Agent — booking conversation engine (state machine).

Turn-based brain for the live booking call flow. The engine:

  * keeps a BookingContext per session (what has been collected so far) — no
    fragile keyword-only matching over the raw transcript;
  * resolves natural-language dates ("8th", "tomorrow", "next Friday") with
    timezone-aware date intelligence and asks instead of guessing when ambiguous;
  * checks the REAL availability engine — never assumes, never fabricates;
  * on unavailability checks ±1 day first, then ±2 days, and speaks only the
    genuinely available alternatives;
  * offers a receptionist transfer and performs it ONLY after explicit caller
    confirmation (unless the persona's business rule allows auto-transfer);
  * treats provider informational/trial messages ("This call is powered by…",
    "This is a trial account…") as non-events — the call is NEVER terminated
    because of them;
  * degrades gracefully: every external failure lands in ERROR_RECOVERY with a
    caller-safe spoken reply while the technical cause goes to the logs.

The engine composes spoken replies deterministically for state-critical turns
(availability results, alternatives, transfer confirmation) and uses the LLM
(through the shared failover router) for open conversation — so availability
statements can never be invented by the model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.services.voice_booking_models import (
    AmbiguousDate,
    AvailabilityEngine,
    BookingContext,
    BookingPhase,
    BOOKING_TRANSITIONS,
    PersonaConfig,
    local_now,
    parse_day_number,
    parse_guest_count,
    parse_name,
    parse_nights,
    parse_service,
    parse_time_phrase,
    resolve_date_phrase,
)

log = logging.getLogger("sara.voice.conversation")

# ---------------------------------------------------------------------------
# Provider informational messages — NEVER terminate a call on these
# ---------------------------------------------------------------------------

_PROVIDER_TRIAL_PATTERNS = [
    r"\bthis (?:call|phone) is (?:being )?powered by\b",
    r"\bthis is a trial\b",
    r"\btrial account\b",
    r"\bpowered by twilio\b",
    r"\btrial (?:message|recording|notification|version)\b",
    r"\byou are on a (?:trial|demo) account\b",
    r"\btwilio (?:trial|free)\b",
    r"\bthis is a (?:test|demo) call\b",
    # Twilio transcribes its trial notice differently on every attempt
    # ("You have a trial account...", "you have reached a trial version...",
    # "your trial has expired, upgrade now"). A bare word like "trial" must
    # NOT match caller speech such as "my trial of the room was great", so
    # require a provider-context keyword in the same sentence.
    r"\btrial\b[^.?!]{0,60}?\b(?:account|version|message|period|notification|restriction|expired?|expiry|limit|twilio|upgrad\w*)\b",
    r"\b(?:account|version|message|period|notification)\b[^.?!]{0,60}?\btrial\b",
    r"\bupgrad\w*\b[^.?!]{0,60}?\baccount\b",
    r"\b(?:free|demo) trial\b",
    r"\bpress any key\b",
]

_PROVIDER_TRIAL_RE = re.compile("|".join(_PROVIDER_TRIAL_PATTERNS), re.IGNORECASE)


def is_provider_informational(text: str) -> bool:
    """True when the transcript is a provider/trial informational message.

    Such messages MUST NOT change conversation state or terminate the call —
    the provider plays them on trial accounts, but the caller is still on the
    line and the conversation continues normally.
    """
    return bool(_PROVIDER_TRIAL_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Caller intents (structured understanding, not just keywords)
# ---------------------------------------------------------------------------


@dataclass
class CallerTurn:
    text: str
    intent: str = "other"
    date_value: Optional[date] = None
    date_raw: Optional[str] = None
    date_ambiguous: bool = False
    date_candidates: List[date] = field(default_factory=list)
    time_value: Optional[str] = None
    guests: Optional[int] = None
    nights: Optional[int] = None
    service: Optional[str] = None
    name: Optional[str] = None
    is_provider_message: bool = False


# A follow-up question is the only turn still worth an LLM call, but the
# provider needs ~3.2s. The cap sits under the 6.5s fast-turn budget so the
# guest still hears a real answer rather than the "hiccup" recovery line, while
# a hang or retry can never blow the turn.
_FOLLOW_UP_BUDGET_S = float(os.getenv("VOICE_FOLLOW_UP_BUDGET_S", "4.0"))

_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|please do|go ahead|forward|connect|"
    r"transfer me?|book it|sounds good|correct|right|haan|ha|theek hai)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"\b(no|nope|nah|not now|don'?t|do not|skip|leave it|forget it|nahi|no thanks|no thank you)\b",
    re.IGNORECASE,
)
# The agent offers two explicit choices once availability is confirmed:
# "book the room now" or "connect you with our hotel staff". An affirmative
# that NAMES booking must complete the booking in-system; only an unqualified
# "yes" or an explicit transfer request should trigger the live transfer.
# Without this, "yes, book it" was silently routed to a staff transfer.
_BOOK_DIRECTLY_RE = re.compile(
    r"\b(?:book|reserve|confirm|proceed|go\s*ahead)\b(?:\s+(?:it|this|the|that|my|room|up))*\b"
    r"|\bdirect(?:ly)?\b|\bconfirm\s+(?:the\s+)?(?:booking|reservation)\b"
    r"|\bcomplete\s+the\s+booking\b",
    re.IGNORECASE,
)
# A negated booking word is a DECLINE, not a request: "no, I won't book then"
# and "not booking today" must close the conversation politely rather than
# firing the booking path. (A regex look-behind cannot express this because
# "don't"/"won't" are variable width.)
# Negation only counts when it sits in the SAME clause as the booking word.
# "no, I won't book then" is a decline, but "no, book it directly" is the
# caller CORRECTING an earlier offer and must still book. A comma therefore
# separates the two, which is why plain "no" is not enough to veto.
_NEGATED_BOOK_RE = re.compile(
    r"\b(?:won't|will not|do not|don't|not|no|never|cannot|can't|neither)\s+"
    r"(?:\w+\s+){0,2}book(?:ing|s)?\b",
    re.IGNORECASE,
)


def _wants_booking(text: Optional[str]) -> bool:
    """True only when the guest is genuinely asking to book, not declining.

    "yes book it" -> True      "book it directly" -> True
    "no, I won't book" -> False   "not booking then" -> False
    """
    t = (text or "").strip()
    if not t:
        return False
    if not _BOOK_DIRECTLY_RE.search(t):
        return False
    if _NEGATED_BOOK_RE.search(t):
        return False
    return True


# "forward me", "connect me to staff", "let me talk to someone" must route to a
# live hand-off, not to the booking path.
_TRANSFER_WORDS_RE = re.compile(
    r"\b(?:forward|transfer|connect|staff|receptionist|reception|someone|"
    r"human|agent|manager|supervisor)\b",
    re.IGNORECASE,
)
_BOOK_RE = re.compile(
    r"\b(book|booking|reserve|reservation|appointment|slot|table|room for|"
    r"schedule|availab\w*)\b",
    re.IGNORECASE,
)
_TRANSFER_RE = re.compile(
    r"\b(receptionist|human|real person|agent|manager|front desk|operator|"
    r"staff|speak to (?:someone|somebody|a person)|transfer)\b",
    re.IGNORECASE,
)
_BYE_RE = re.compile(
    r"\b(bye|goodbye|good bye|that'?s all|nothing else|see you|alvida)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"\b(price|prices|cost|rate|rates|tariff|how much|charge|charges)\b", re.IGNORECASE)


def _reply_matches_language(text: str, language: Optional[str]) -> bool:
    """Guard against the LLM answering an English call in another script.

    The system prompt asks the model to mirror the caller's language, but that is
    a suggestion, not a guarantee. In a live trial the model began replying in
    Tamil to a clearly English-speaking guest, which is unusable for the caller.
    Any non-Latin script in a reply that should be English is rejected outright;
    the caller then hears the deterministic English fallback instead.
    """
    want = (language or "en-IN").split("-")[0].lower()
    if want == "en":
        # Latin (English/Hinglish) is expected. Devanagari, Tamil, Telugu,
        # Kannada, Malayalam, Gujarati, Bengali, Arabic or CJK all mean the
        # model switched language mid-call.
        return not any(ord(ch) > 0x024F for ch in (text or ""))
    return True


def parse_caller_turn(text: str, *, now: Optional[Any] = None,
                      tz_name: str = "Asia/Kolkata") -> CallerTurn:
    """Structure one caller utterance: intent + every extractable field."""
    turn = CallerTurn(text=text or "")
    turn.is_provider_message = is_provider_informational(text)

    if turn.is_provider_message:
        turn.intent = "provider_message"
        return turn

    lower = (text or "").lower()

    # Date resolution (may raise AmbiguousDate)
    try:
        resolved = resolve_date_phrase(text, now=now, tz_name=tz_name)
        if resolved:
            turn.date_value = resolved
            turn.date_raw = text.strip()[:80]
    except AmbiguousDate as exc:
        turn.date_ambiguous = True
        turn.date_candidates = list(exc.candidates)

    turn.time_value = parse_time_phrase(text)
    turn.guests = parse_guest_count(text)
    turn.nights = parse_nights(text)
    turn.service = parse_service(text)
    turn.name = parse_name(text)

    if _TRANSFER_RE.search(lower):
        turn.intent = "transfer_request"
    elif _NO_RE.search(lower) and not _YES_RE.search(lower):
        # Denial always wins over goodbye-phrases like "no thanks".
        turn.intent = "deny"
    elif _BYE_RE.search(lower) and not turn.date_value:
        turn.intent = "goodbye"
    elif _PRICE_RE.search(lower) and not turn.date_value:
        turn.intent = "pricing_question"
    elif _BOOK_RE.search(lower) or turn.date_value or turn.date_ambiguous:
        turn.intent = "booking_request"
    elif _YES_RE.search(lower) and not _NO_RE.search(lower):
        turn.intent = "affirm"
    elif _NO_RE.search(lower):
        turn.intent = "deny"
    elif turn.name:
        turn.intent = "provide_name"
    else:
        turn.intent = "other"
    return turn


# ---------------------------------------------------------------------------
# Turn result
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    reply: str
    phase: BookingPhase
    context: BookingContext
    events: List[Dict[str, Any]] = field(default_factory=list)
    action: Optional[Dict[str, Any]] = None  # e.g. {"type": "transfer", ...}
    llm_used: bool = False


def _date_label(d: date) -> str:
    return f"{d.strftime('%A').capitalize()}, {d.day} {d.strftime('%B')} {d.year}"


class BookingConversationEngine:
    """Owns the booking conversation for every active call session."""

    def __init__(
        self,
        *,
        availability: AvailabilityEngine,
        persona: PersonaConfig,
        llm_fn: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[str]]] = None,
        transfer_fn: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        translator_fn: Optional[Callable[[str, str, str], Awaitable[str]]] = None,
        timezone_name: str = "Asia/Kolkata",
        context_ttl_seconds: int = 6 * 3600,
    ) -> None:
        self.availability = availability
        self.persona = persona
        self.llm_fn = llm_fn
        self.transfer_fn = transfer_fn
        # translator_fn(text, source_code, target_code) -> translated text.
        # Backed by the real Sarvam translate API so non-English callers are
        # genuinely understood (no fake translations); failures fall back to
        # parsing the original text.
        self.translator_fn = translator_fn
        self.timezone_name = timezone_name or persona.business_hours.get("timezone", "Asia/Kolkata")
        self.context_ttl_seconds = context_ttl_seconds
        self._sessions: Dict[str, BookingContext] = {}

    # -- session management ----------------------------------------------------

    def get_context(self, session_key: str) -> BookingContext:
        ctx = self._sessions.get(session_key)
        if ctx is None:
            ctx = BookingContext(session_key=session_key, language=self.persona.language)
            self._sessions[session_key] = ctx
        return ctx

    def set_context(self, session_key: str, ctx: BookingContext) -> None:
        ctx.session_key = session_key
        self._sessions[session_key] = ctx

    def reset(self, session_key: str) -> None:
        self._sessions.pop(session_key, None)

    def _transition(self, ctx: BookingContext, new_phase: BookingPhase) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if new_phase == ctx.phase_value():
            return events
        try:
            current = BookingPhase(ctx.phase)
        except ValueError:
            current = BookingPhase.IDLE
        allowed = BOOKING_TRANSITIONS.get(current, set())
        if new_phase != current and new_phase not in allowed:
            # ERROR_RECOVERY / CALL_ENDING are reachable from anywhere in
            # practice; force them through explicitly rather than crashing a
            # live call on a state-machine technicality.
            if new_phase in (BookingPhase.ERROR_RECOVERY, BookingPhase.CALL_ENDING):
                log.warning(
                    "booking phase jump %s -> %s (forced)",
                    current.value, new_phase.value,
                )
            else:
                log.warning(
                    "booking phase jump %s -> %s (allowed set: %s)",
                    current.value, new_phase.value,
                    sorted(p.value for p in allowed),
                )
        events.append({
            "type": "PHASE_CHANGE",
            "previous": current.value,
            "new": new_phase.value,
        })
        ctx.phase = new_phase.value
        return events

    # -- LLM helper --------------------------------------------------------------

    def _instant(self, ctx: BookingContext, *variants: str) -> tuple[str, bool]:
        """Return a natural phrasing INSTANTLY, with no LLM call at all.

        Deterministic questions ("which date?", "how many guests?") do not need
        a 3-second model round-trip — the wording is fixed anyway. Rotating a
        few variants keeps the voice from sounding like a broken record while
        cutting the turn from ~3s to a few milliseconds.
        """
        pool = [v for v in variants if v]
        if not pool:
            return "", False
        return pool[ctx.turn_count % len(pool)], False

    async def _natural(
        self,
        ctx: BookingContext,
        *,
        instruction: str,
        fallback: str,
        extra: str = "",
    ) -> tuple[str, bool]:
        """Ask the LLM to phrase a reply; return (text, llm_used).

        The deterministic `fallback` is ALWAYS returned when the LLM is not
        configured, fails, or produces something unusable — availability and
        transfer wording must never depend on model success.
        """
        if self.llm_fn is None:
            return fallback, False
        system = (
            f"You are {self.persona.name}, the live voice receptionist at "
            f"{self.persona.business_name}. {self.persona.role} "
            f"Tone: {self.persona.tone} "
            f"This is a REAL-TIME phone conversation with a guest on the line. "
            f"BE A HUMAN FRONT-DESK RECEPTIONIST, NOT A ROBOT: "
            f"(1) Reply in ONE to TWO short spoken sentences — like talking, never "
            f"an essay. "
            f"(2) Keep the booking moving: whatever the guest just told you, use it, "
            f"acknowledge it naturally (\"Sure, ma'am\", \"बिल्कुल जी\"), and ask for the "
            f"NEXT missing detail — date first, then number of guests, then how long "
            f"they'll stay. One question per turn. "
            f"(3) REACT like a person: if the guest gives two things at once, confirm "
            f"both in passing and ask the next thing. Never repeat a question they "
            f"already answered, never read back everything each turn. "
            f"(4) No markdown, no emojis, no lists, no \'As an AI\' disclaimers. "
            f"(5) LANGUAGE RULE: DEFAULT TO ENGLISH. The guest is usually speaking "
            f"English — answer in plain English unless their last line is written in "
            f"another script. Only then mirror that language (Hindi in Devanagari, "
            f"Tamil in Tamil, and so on). NEVER switch to a language the guest did "
            f"not use; NEVER reply in Tamil, Telugu, Kannada, Malayalam, Gujarati, "
            f"Bengali or Devanagari to an English-speaking guest. "
            f"(6) Never invent availability, prices or confirmation status — use the "
            f"real check results you are given. "
            f"Knowledge: {self.persona.knowledge_base or 'none beyond the business basics.'} "
            f"{extra}"
        )
        user = f"Caller said: \"{ctx.last_text or ''}\"\nTask: {instruction}"
        # The provider intermittently returns a truncated first token ("We")
        # instead of a full sentence. One immediate retry recovers a genuine
        # answer far more often than falling back to robotic boilerplate, which
        # matters most for follow-up questions the guest actually asked.
        text = ""
        for attempt in range(2):
            try:
                text = (await self.llm_fn(system, user, ctx.to_dict())).strip()
            except Exception as exc:
                log.warning("booking LLM phrasing failed: %s", exc)
                text = ""
            if text and len(text) <= 600 and len(text.split()) >= 3:
                break
            if attempt == 0:
                log.info("booking LLM reply unusable (%r) - retrying once", text[:40])
        if not text or len(text) > 600:
            return fallback, False
        # DEGENERATE-REPLY GUARD. The model occasionally returns a truncated
        # fragment ("Of") instead of a usable turn. Speaking that aloud is
        # meaningless to the caller, so anything too short to be a real spoken
        # sentence is discarded in favour of the deterministic fallback.
        if len(text.split()) < 3 or len(text.strip()) < 10:
            log.warning("booking LLM reply too short (%r) - using fallback", text)
            return fallback, False
        # HARD LANGUAGE GUARD. The "reply in the caller's language" instruction is
        # only a soft nudge — on a live call the model drifted into Tamil/Devanagari
        # while the guest was speaking plain English, which is unusable for the
        # caller. When the call is English, reject any reply carrying another
        # script and fall back to the deterministic English line.
        if not _reply_matches_language(text, ctx.language):
            log.warning(
                "booking LLM reply in wrong language (want=%s) - using fallback",
                ctx.language,
            )
            return fallback, False
        return text, True

    # -- main turn handler --------------------------------------------------------

    async def handle_turn(
        self,
        session_key: str,
        text: str,
        *,
        call_id: Optional[str] = None,
        caller_phone: Optional[str] = None,
    ) -> TurnResult:
        ctx = self.get_context(session_key)
        ctx.turn_count += 1
        ctx.last_text = (text or "").strip()
        if call_id and not ctx.call_id:
            ctx.call_id = call_id
        if caller_phone and not ctx.caller_phone:
            ctx.caller_phone = caller_phone

        # Multilingual understanding: when the caller is speaking a non-English
        # language, translate their words to English (real Sarvam mayura call)
        # before parsing dates/guest counts. Parsing stays exact; translation
        # failure degrades to parsing the original text.
        parse_text = text
        if (
            self.translator_fn is not None
            and (text or "").strip()
            and (ctx.language or "en-IN").split("-")[0] != "en"
        ):
            try:
                translated = (
                    await self.translator_fn(text, ctx.language, "en-IN")
                ).strip()
                if translated:
                    parse_text = translated
            except Exception as exc:
                log.warning("caller-text translation failed; using original: %s", exc)
        turn = parse_caller_turn(parse_text, tz_name=self.timezone_name)

        # Provider trial/informational message: a non-event. The call STAYS
        # alive and the state machine does not move.
        if turn.is_provider_message:
            log.info(
                "provider informational message ignored (call continues) session=%s call=%s",
                session_key, call_id,
            )
            return TurnResult(
                reply="",
                phase=BookingPhase(ctx.phase),
                context=ctx,
                events=[{
                    "type": "PROVIDER_INFO_MESSAGE",
                    "note": "ignored — call remains active",
                    "text": (text or "")[:120],
                }],
            )

        try:
            phase = BookingPhase(ctx.phase)
        except ValueError:
            phase = BookingPhase.IDLE

        if phase == BookingPhase.IDLE:
            phase = BookingPhase.CALL_CONNECTED
            ctx.phase = phase.value

        # GLOBAL direct-booking guard. We explicitly offered "book the room now,
        # or connect you with our staff". Whichever phase we happen to be in,
        # an affirmative that NAMES booking means "book it", never "transfer
        # me". Previously this only worked in READY_FOR_TRANSFER, so from
        # DATE_AVAILABLE the same words triggered a staff transfer instead.
        if (
            ctx.staff_transfer_offered
            and not ctx.direct_booking
            and turn.intent in ("affirm", "other", "booking_request")
            and _wants_booking(turn.text)
            and (ctx.selected_date or ctx.requested_date)
        ):
            ctx.direct_booking = True
            result = await self._complete_booking_without_transfer(ctx, turn, [])
            result.events.insert(0, {"type": "DIRECT_BOOKING_REQUESTED"})
            return result

        handler = {
            BookingPhase.CALL_CONNECTED: self._h_connected,
            BookingPhase.GREETING: self._h_connected,
            BookingPhase.COLLECTING_REQUIREMENTS: self._h_collecting,
            BookingPhase.DATE_IDENTIFIED: self._h_date_identified,
            BookingPhase.CHECKING_AVAILABILITY: self._h_date_identified,
            BookingPhase.DATE_AVAILABLE: self._h_date_available,
            BookingPhase.DATE_UNAVAILABLE: self._h_alternatives,
            BookingPhase.SUGGESTING_ALTERNATIVES: self._h_alternatives,
            BookingPhase.CONFIRMING_SELECTION: self._h_confirming,
            BookingPhase.READY_FOR_TRANSFER: self._h_ready_transfer,
            BookingPhase.TRANSFERRING: self._h_transferring,
            BookingPhase.TRANSFER_SUCCESS: self._h_transfer_done,
            BookingPhase.TRANSFER_FAILED: self._h_transfer_failed,
            BookingPhase.BOOKING_CONFIRMED: self._h_booking_confirmed,
            BookingPhase.WRAP_UP: self._h_wrap_up,
            BookingPhase.ERROR_RECOVERY: self._h_error_recovery,
            BookingPhase.CALL_ENDING: self._h_ending,
        }.get(phase, self._h_collecting)

        result = await handler(ctx, turn)

        # Spoken replies follow the caller's language. The LLM already mirrors
        # it when it phrases the reply; deterministic fallbacks are translated
        # here (real Sarvam call) so a Hindi/Tamil caller never hears English
        # boilerplate. Translation failure keeps the English fallback — the
        # call continues either way.
        if (
            result.reply
            and not result.llm_used
            and self.translator_fn is not None
            and (ctx.language or "en-IN").split("-")[0] != "en"
        ):
            try:
                translated = await self.translator_fn(result.reply, "en-IN", ctx.language)
                if translated:
                    result.reply = translated
            except Exception as exc:
                log.warning("reply translation failed; keeping fallback: %s", exc)
        return result

    # -- phase handlers ------------------------------------------------------------

    async def _h_connected(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events = self._transition(ctx, BookingPhase.COLLECTING_REQUIREMENTS)
        if turn.intent == "booking_request" and (turn.date_value or turn.date_ambiguous):
            return await self._h_collecting(ctx, turn)
        # GREETING ONLY ONCE. The TwiML answer already greeted the caller
        # ("Hello! Thank you for calling... This is SARA"), so when the engine
        # then produced its own introduction the caller heard her introduce
        # herself twice. Only introduce when the greeting genuinely has not
        # been delivered yet (e.g. a caller who spoke before we answered).
        if ctx.greeting_completed:
            reply, llm_used = self._instant(
                ctx,
                "Certainly. How many guests will be staying, and for which date?",
                "Of course. Could you tell me the date and how many guests?",
                "Sure. What date would you like, and how many guests?",
            )
            return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                              context=ctx, events=events, llm_used=llm_used)
        ctx.greeting_completed = True
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                "Greet the caller warmly as the hotel's receptionist, introduce "
                "yourself by name, and ask how you can help. If they already stated "
                "a booking need, acknowledge it and ask for the missing date or "
                "details naturally. Reply in the caller's language."
            ),
            fallback=self.persona.greeting_text(),
        )
        return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                          context=ctx, events=events, llm_used=llm_used)

    async def _h_collecting(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []

        if turn.intent == "goodbye":
            return await self._end(ctx, turn, reason="caller_ended")

        if turn.intent == "transfer_request":
            # Transfer without an identified booking: still allow it (persona rule).
            return await self._offer_transfer(ctx, events, after_unavailable=False)

        if turn.intent == "deny" and (ctx.requested_date or ctx.guests):
            # The guest is already part-way through a booking and has said no.
            # Do NOT restart collection and ask the date again - close politely
            # and keep the line open so SARA never appears to cut the caller off.
            return self._open_wrap_up(
                ctx, events,
                closing=(
                    f"No problem at all. Thank you for visiting "
                    f"{self.persona.business_name}, and do come back again."
                ),
            )

        if turn.name and not ctx.guest_name:
            ctx.guest_name = turn.name

        if turn.date_ambiguous and not turn.date_value:
            return await self._ask_date_clarification(ctx, turn, events)

        if turn.date_value:
            ctx.requested_date = turn.date_value.isoformat()
            ctx.requested_date_raw = turn.date_raw
            if turn.time_value:
                ctx.requested_time = turn.time_value
            if turn.guests:
                ctx.guests = turn.guests
            if turn.service:
                ctx.service = turn.service
            if turn.nights:
                ctx.nights = turn.nights
            events += self._transition(ctx, BookingPhase.DATE_IDENTIFIED)
            return await self._h_date_identified(ctx, turn)

        # No date yet — collect the next missing piece, one question at a time.
        if not ctx.requested_date:
            # A caller answering "which date?" with a bare day ("8", "on 8",
            # "8 please") was being ignored, so the agent asked the same
            # question again and again — the exact loop reported on the live
            # call. We are explicitly waiting for a date, so a bare day number
            # IS the date. This must run BEFORE any guest capture, because the
            # same number also parses as a guest count ("8" -> 8 guests) and
            # would otherwise announce "available for 8 guests".
            if turn.date_value is None and not turn.date_ambiguous:
                forced = self._bare_day_as_date(turn.text)
                if forced is not None:
                    turn.date_value = forced
                    turn.date_raw = turn.text
                    turn.guests = None
                    ctx.guests = None
                    ctx.requested_date = forced.isoformat()
                    ctx.requested_date_raw = turn.text
                    events.append({"type": "DATE_FROM_BARE_DAY", "date": forced.isoformat()})
                    events += self._transition(ctx, BookingPhase.DATE_IDENTIFIED)
                    return await self._h_date_identified(ctx, turn)
            if turn.intent in ("affirm", "other", "pricing_question") and turn.guests:
                ctx.guests = turn.guests
            ctx.asked_dates = (getattr(ctx, "asked_dates", 0) or 0) + 1
            if ctx.asked_dates >= 2:
                # Never repeat an unanswered question: re-ask ONCE with a
                # concrete example, then hand the caller to a human rather than
                # trapping them in a loop.
                events.append({"type": "DATE_REASK", "count": ctx.asked_dates})
                if ctx.asked_dates >= 3:
                    # Three failed attempts: stop guessing and hand over to a
                    # human. Being transferred beats looping on the phone.
                    return await self._offer_transfer(ctx, events, after_unavailable=False)
                reply, llm_used = self._instant(
                    ctx,
                    "Sorry, I didn't catch that clearly. Could you say the day and month, "
                    "for example the eighth of October?",
                    "Sorry, I missed that one. Which day and month would you like to book?",
                )
                return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                                  context=ctx, events=events, llm_used=llm_used)
            reply, llm_used = self._instant(
                ctx,
                "Certainly. Which date would you like to book for?",
                "Of course. What date would you like to stay with us?",
                "Sure. Which date suits you for your stay?",
            )
            return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                              context=ctx, events=events, llm_used=llm_used)

        if ctx.guests is None and turn.guests:
            ctx.guests = turn.guests
        if ctx.guests is None:
            return self._ask_guests(ctx, events)

        # Caller already declined the transfer and is completing the booking
        # directly with the agent — route name/details to the booking path.
        if ctx.direct_booking:
            return await self._complete_booking_without_transfer(ctx, turn, events)

        # date + guests known but we landed here without checking — go check
        events += self._transition(ctx, BookingPhase.DATE_IDENTIFIED)
        return await self._h_date_identified(ctx, turn)

    def _room_summary(self, result) -> str:
        """Speak the room types genuinely free on this date, with prices.

        Built from the REAL availability result and the hotel's own room list,
        so the caller hears an actual choice. Never invents a room type or a
        price. Spoken form is short - long lists are hard to follow on a phone.
        """
        types = [t for t in (getattr(result, "room_types", None) or []) if t]
        if not types:
            return ""
        prices: Dict[str, Any] = {}
        try:
            for room in (getattr(self.availability, "_data", {}) or {}).get("rooms", []):
                if isinstance(room, dict) and room.get("type"):
                    prices[str(room["type"])] = room.get("price_per_night")
        except Exception:
            prices = {}

        def _price(t: str) -> str:
            p = prices.get(t)
            try:
                return f" at {int(p):,} rupees" if p else ""
            except Exception:
                return ""

        if len(types) == 1:
            return f"I have the {types[0]} room{_price(types[0])} per night available."
        if len(types) == 2:
            return (
                f"I have the {types[0]} room{_price(types[0])} and the "
                f"{types[1]} room{_price(types[1])} per night available."
            )
        # Three or more: attaching a single price here would be misleading
        # (it sounded like one price covered every room type), so name them
        # and let the guest ask for details.
        head = ", ".join(types[:-1])
        return f"I have the {head} and the {types[-1]} rooms available."

    def _ask_guests(
        self, ctx: BookingContext, events: List[Dict[str, Any]]
    ) -> TurnResult:
        """Ask for the party size, and NEVER ask more than twice.

        This is a single shared helper because the bug it fixes lived in having
        the question duplicated: the counter was incremented only in
        _h_collecting, while _h_date_identified (the phase you are in once the
        date is already known) asked for guests WITHOUT counting. A caller who
        gave the date first and then something unintelligible therefore looped
        on "how many guests?" forever, with asked_guests stuck at 0.

        After two unanswered attempts the agent stops asking, assumes the
        common case of two guests and moves on, so the call always progresses.
        """
        asked = getattr(ctx, "asked_guests", 0) or 0
        label = _human_date(ctx.requested_date) if ctx.requested_date else ""

        if asked >= 2:
            ctx.guests = 2
            events = list(events or [])
            events.append({"type": "GUEST_COUNT_DEFAULTED", "guests": 2, "asked": asked})
            reply, llm_used = self._instant(
                ctx,
                f"No problem, I'll go ahead for two guests"
                + (f" on {label}." if label else "."),
                f"That's fine, I'll set it as two guests"
                + (f" for {label}." if label else "."),
            )
            return TurnResult(reply=reply, phase=BookingPhase.DATE_IDENTIFIED,
                              context=ctx, events=events, llm_used=llm_used)

        ctx.asked_guests = asked + 1
        events = list(events or [])
        if asked == 1:
            # Second attempt: make it trivially easy to answer in one word.
            reply, llm_used = self._instant(
                ctx,
                "Sorry, I didn't catch that. Just the number of guests, for example two.",
            )
        elif label:
            reply, llm_used = self._instant(
                ctx,
                f"Sure, I'll check {label}. How many guests will be staying?",
                f"I've got {label} noted down. And how many guests will be staying?",
                f"Perfect, {label}. May I know how many guests will be staying?",
            )
        else:
            reply, llm_used = self._instant(
                ctx,
                "How many guests will be staying?",
                "And how many guests will be joining us?",
            )
        return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                          context=ctx, events=events, llm_used=llm_used)

    def _bare_day_as_date(self, text: str) -> Optional[date]:
        """Read a bare day answer ("8", "on 8", "8 please") as a date.

        Only used while the agent is explicitly waiting for a date, where a bare
        day number cannot mean anything else. Delegates to the existing date
        resolver with an "on the" prefix so all month/ordering rules still apply.
        """
        raw = (text or "").lower().strip()
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", raw)
        if not m:
            return None
        day = parse_day_number(m.group(1))
        if not day or not (1 <= day <= 31):
            return None
        try:
            return resolve_date_phrase(f"on the {m.group(1)}", tz_name=self.timezone_name)
        except Exception:
            return None

    async def _ask_date_clarification(
        self, ctx: BookingContext, turn: CallerTurn, events: List[Dict[str, Any]]
    ) -> TurnResult:
        candidates = turn.date_candidates or []
        if len(candidates) >= 2:
            options = " or ".join(_date_label(d) for d in candidates[:2])
            fallback = f"Did you mean {options}?"
        else:
            fallback = "Could you tell me the full date you have in mind — the day and the month?"
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                "The caller's date phrase was ambiguous. Ask ONE clarifying question "
                "to pin down the exact date. Do not guess."
            ),
            fallback=fallback,
        )
        return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                          context=ctx, events=events, llm_used=llm_used)

    async def _h_date_identified(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        """Date known: confirm the remaining requirement, then check availability."""
        events: List[Dict[str, Any]] = []
        if turn.guests and ctx.guests is None:
            ctx.guests = turn.guests
        if turn.time_value and not ctx.requested_time:
            ctx.requested_time = turn.time_value

        if ctx.guests is None and turn.intent != "affirm":
            # Must use the shared helper so the unanswered-question counter is
            # actually incremented on THIS path too. Asking here without the
            # counter is what caused the repeat "how many guests?" loop.
            return self._ask_guests(ctx, events)

        events += self._transition(ctx, BookingPhase.CHECKING_AVAILABILITY)
        return await self._check_and_respond(ctx, events)

    async def _check_and_respond(self, ctx: BookingContext, events: List[Dict[str, Any]]) -> TurnResult:
        target = date.fromisoformat(ctx.requested_date)
        try:
            result = self.availability.check_date(
                target, guests=ctx.guests, service=ctx.service
            )
        except Exception as exc:
            log.exception("availability check failed session=%s", ctx.session_key)
            return await self._error_recovery(
                ctx, events, technical=str(exc), operation="availability_check"
            )

        events.append({
            "type": "AVAILABILITY_CHECK",
            "date": ctx.requested_date,
            "available": result.available,
            "remaining": result.remaining,
            "source": result.source,
        })

        if result.available:
            events += self._transition(ctx, BookingPhase.DATE_AVAILABLE)
            # Remember the genuine room options so the very next spoken turn
            # can offer a real choice rather than a bare "it is available".
            ctx.room_summary = self._room_summary(result)
            return await self._offer_transfer(ctx, events, after_unavailable=False)

        events += self._transition(ctx, BookingPhase.DATE_UNAVAILABLE)
        return await self._h_alternatives(ctx, CallerTurn(text=""), extra_events=events)

    async def _h_date_available(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []
        if turn.intent == "affirm":
            return await self._offer_transfer(ctx, events, after_unavailable=False)
        if turn.intent == "deny":
            # Caller declined both booking and staff. Close politely but KEEP
            # THE CALL OPEN and ask whether they need anything else, instead of
            # hanging up on them mid-conversation.
            return self._open_wrap_up(
                ctx, events,
                closing=(
                    f"No problem at all. Thank you for visiting "
                    f"{self.persona.business_name}, and do come back again."
                ),
            )
        if turn.date_value and turn.date_value.isoformat() != ctx.requested_date:
            ctx.requested_date = turn.date_value.isoformat()
            events += self._transition(ctx, BookingPhase.DATE_IDENTIFIED)
            return await self._h_date_identified(ctx, turn)
        return await self._offer_transfer(ctx, events, after_unavailable=False)

    async def _h_alternatives(
        self,
        ctx: BookingContext,
        turn: CallerTurn,
        extra_events: Optional[List[Dict[str, Any]]] = None,
    ) -> TurnResult:
        events: List[Dict[str, Any]] = list(extra_events or [])

        if turn.intent == "goodbye":
            return await self._end(ctx, turn, reason="caller_ended")

        # Caller picked one of the offered alternatives (or named any new date)
        picked = self._match_alternative(ctx, turn)
        if picked:
            ctx.selected_date = picked.isoformat()
            events.append({"type": "ALTERNATIVE_SELECTED", "date": ctx.selected_date})
            events += self._transition(ctx, BookingPhase.CONFIRMING_SELECTION)
            reply, llm_used = await self._natural(
                ctx,
                instruction=(
                    f"Confirm the selected date ({_date_label(picked)}) in one short "
                    "sentence, then ask whether they would like to be connected to "
                    "the receptionist to complete the booking."
                ),
                fallback=(
                    f"Certainly. {_date_label(picked)} is available. Would you like "
                    "me to forward your call to the receptionist to complete the booking?"
                ),
            )
            return TurnResult(reply=reply, phase=BookingPhase.CONFIRMING_SELECTION,
                              context=ctx, events=events, llm_used=llm_used)

        if turn.intent == "deny" and ctx.alternatives_offered:
            return self._open_wrap_up(
                ctx, events,
                closing=(
                    f"No problem at all. Thank you for visiting "
                    f"{self.persona.business_name}, and kindly visit us again."
                ),
            )

        if turn.intent == "transfer_request":
            return await self._offer_transfer(ctx, events, after_unavailable=True)

        # First entry (or caller asked to check a different date): compute REAL
        # nearby availability: ±1 day first, then ±2 days.
        target = date.fromisoformat(ctx.requested_date) if ctx.requested_date else local_now(
            self.timezone_name
        ).date()
        try:
            nearby = self.availability.nearby_availability(
                target, guests=ctx.guests, service=ctx.service, window=2
            )
        except Exception as exc:
            log.exception("nearby availability failed session=%s", ctx.session_key)
            return await self._error_recovery(
                ctx, events, technical=str(exc), operation="nearby_availability"
            )

        ctx.alternatives_offered = [d.isoformat() for d in nearby]
        events.append({
            "type": "ALTERNATIVES_COMPUTED",
            "requested": target.isoformat(),
            "alternatives": ctx.alternatives_offered,
        })

        requested_label = _human_date(target.isoformat())
        if nearby:
            options = " and ".join(_date_label(d) for d in nearby[:2])
            events += self._transition(ctx, BookingPhase.SUGGESTING_ALTERNATIVES)
            reply, llm_used = await self._natural(
                ctx,
                instruction=(
                    f"Tell the caller that {requested_label} is currently unavailable, "
                    f"and that you can see availability on {options}. Ask if they "
                    "would like to book one of those. Two sentences max."
                ),
                fallback=(
                    f"I'm sorry, {requested_label} is currently unavailable. I checked "
                    f"nearby dates and I have availability on {options}. "
                    "Would you like to book either of those?"
                ),
            )
            return TurnResult(reply=reply, phase=BookingPhase.SUGGESTING_ALTERNATIVES,
                              context=ctx, events=events, llm_used=llm_used)

        events += self._transition(ctx, BookingPhase.READY_FOR_TRANSFER)
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                f"Tell the caller {requested_label} and the nearby days are fully "
                "booked. Offer to connect them to the receptionist who can help "
                "with other options. Two sentences max."
            ),
            fallback=(
                f"I'm sorry, {requested_label} and the nearby days are fully booked "
                "at the moment. Would you like me to connect you to the receptionist "
                "who can help with other options?"
            ),
        )
        return TurnResult(reply=reply, phase=BookingPhase.READY_FOR_TRANSFER,
                          context=ctx, events=events, llm_used=llm_used)

    def _match_alternative(self, ctx: BookingContext, turn: CallerTurn) -> Optional[date]:
        """Did the caller pick one of the dates we offered (or state a new one)?

        A negated mention ("no, not the 9th") never selects a date — the caller
        is explicitly declining it.
        """
        if _NO_RE.search(turn.text or "") and not _YES_RE.search(turn.text or ""):
            return None
        if turn.date_value:
            offered = {d.isoformat() for d in map(date.fromisoformat, ctx.alternatives_offered or [])}
            if turn.date_value.isoformat() in offered or turn.date_value >= local_now(
                self.timezone_name
            ).date():
                return turn.date_value
        # "the 9th" spoken after we offered days: match on bare day number
        m = re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)?\b", turn.text, re.IGNORECASE)
        if m:
            day = parse_day_number(m.group(1))
            if day:
                for iso in ctx.alternatives_offered or []:
                    if date.fromisoformat(iso).day == day:
                        return date.fromisoformat(iso)
        return None

    async def _h_confirming(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []
        if turn.intent == "affirm":
            events += self._transition(ctx, BookingPhase.READY_FOR_TRANSFER)
            return await self._h_ready_transfer(ctx, turn)
        if turn.intent == "deny":
            # Declined this date — ask whether they'd like the other option.
            remaining = [
                iso for iso in ctx.alternatives_offered or []
                if iso != ctx.selected_date
            ]
            if remaining:
                ctx.selected_date = None
                ctx.alternatives_offered = remaining
                other = _date_label(date.fromisoformat(remaining[0]))
                reply, llm_used = await self._natural(
                    ctx,
                    instruction=f"Offer the other available date ({other}) in one short sentence.",
                    fallback=f"Would you like {other} instead?",
                )
                return TurnResult(reply=reply, phase=BookingPhase.SUGGESTING_ALTERNATIVES,
                                  context=ctx, events=events, llm_used=llm_used)
            return await self._end(
                ctx, turn, reason="caller_declined_alternatives",
                farewell=(
                    "No problem. Thank you for calling "
                    f"{self.persona.business_name}. Have a good day!"
                ),
            )
        if turn.date_value:
            picked = self._match_alternative(ctx, turn)
            if picked:
                ctx.selected_date = picked.isoformat()
                reply, llm_used = await self._natural(
                    ctx,
                    instruction=(
                        f"Confirm {_date_label(picked)} and ask if they would like to "
                        "be connected to the receptionist."
                    ),
                    fallback=(
                        f"Certainly. {_date_label(picked)} is available. Would you like "
                        "me to forward your call to the receptionist?"
                    ),
                )
                return TurnResult(reply=reply, phase=BookingPhase.CONFIRMING_SELECTION,
                                  context=ctx, events=events, llm_used=llm_used)
        reply, llm_used = await self._natural(
            ctx,
            instruction="Ask whether they would like to book the selected date. One short question.",
            fallback="Shall I go ahead and book that date for you?",
        )
        return TurnResult(reply=reply, phase=BookingPhase.CONFIRMING_SELECTION,
                          context=ctx, events=events, llm_used=llm_used)

    def _fits_party(self, ctx: BookingContext) -> bool:
        """True when the requested party fits in the rooms actually free.

        Guards against announcing availability the hotel cannot honour. Any
        doubt (no guest count, no date, no engine) returns True so this can
        never block a legitimate booking — it only catches a known overflow.
        """
        if not ctx.guests or not ctx.selected_date:
            return True
        engine = getattr(self, "availability", None)
        if engine is None:
            return True
        try:
            target = date.fromisoformat(ctx.selected_date)
        except (TypeError, ValueError):
            return True
        try:
            result = engine.check_date(
                target, guests=ctx.guests, service=ctx.service or None
            )
        except Exception as exc:
            log.warning("capacity check failed (allowing): %s", exc)
            return True
        return bool(getattr(result, "available", True))

    async def _offer_transfer(
        self,
        ctx: BookingContext,
        events: List[Dict[str, Any]],
        *,
        after_unavailable: bool,
    ) -> TurnResult:
        """Date is available (or caller insists): ask before transferring."""
        ctx.requested_date = ctx.requested_date or ctx.selected_date
        events += self._transition(ctx, BookingPhase.READY_FOR_TRANSFER)

        # SAFETY NET: never announce availability we cannot honour. If the
        # party is larger than the rooms actually free on that date, fall back
        # to the alternatives path so the caller is offered real dates instead
        # of a reservation the hotel cannot take.
        if not after_unavailable and not self._fits_party(ctx):
            events.append({"type": "CAPACITY_EXCEEDED", "guests": ctx.guests})
            return await self._h_alternatives(
                ctx, CallerTurn(text=""), extra_events=events
            )

        if after_unavailable:
            reply, llm_used = self._instant(
                ctx,
                "Of course. Please hold while I connect you to the receptionist.",
            )
            return await self._do_transfer(ctx, reply, events, llm_used)

        label = _human_date(ctx.selected_date or ctx.requested_date or "")
        label_cap = label[:1].upper() + label[1:] if label else label
        guests_part = f" for {ctx.guests} guests" if ctx.guests else ""

        # No date captured (e.g. the caller could not be understood and we are
        # bailing out to a human). Announcing " is available" with an empty date
        # sounds broken, so use a plain hand-off line instead.
        if not label:
            reply, llm_used = self._instant(
                ctx,
                "I'd rather not guess at your dates. Let me connect you with our "
                "reservations team, who can confirm the exact dates available.",
            )
            return await self._do_transfer(ctx, reply, events, False)
        # Offer BOTH choices. Offering only the receptionist made SARA sound
        # like a gatekeeper and left the caller unable to simply book. Record
        # that the offer was made so the next turn can tell "book it" (direct
        # booking) apart from "forward me" (transfer).
        ctx.staff_transfer_offered = True
        ctx.choice_offered = True
        # Availability answer: real room options (from the real result) followed
        # by the explicit booking-vs-staff choice. Phrased instantly - this is
        # deterministic information, and a 3s wait here is very noticeable.
        rooms = (ctx.room_summary or "").strip()
        lead = f"{rooms} " if rooms else f"Yes, {label} is available{guests_part}. "
        reply, llm_used = self._instant(
            ctx,
            f"{lead}Would you like me to confirm the booking now, "
            "or connect you with our hotel staff?",
            f"{lead}Shall I go ahead and book the room for you, "
            "or would you prefer to speak with our staff?",
            f"{lead}Do you want to confirm the booking, or shall I put you "
            "through to our team?",
        )
        return TurnResult(reply=reply, phase=BookingPhase.READY_FOR_TRANSFER,
                          context=ctx, events=events, llm_used=llm_used)

    async def _h_ready_transfer(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []

        if turn.intent == "deny":
            # "no, book directly" still means book it. A plain "no" to the
            # booking-vs-staff choice means "neither" and must close politely
            # with the line still open, NOT force a booking.
            if _wants_booking(turn.text):
                ctx.direct_booking = True
                return await self._complete_booking_without_transfer(ctx, turn, events)
            return self._open_wrap_up(
                ctx, events,
                closing=(
                    f"No problem at all. Thank you for visiting "
                    f"{self.persona.business_name}, and do come back again."
                ),
            )

        if turn.intent == "affirm" and _wants_booking(turn.text):
            # Caller chose the "book it for me" option we offered explicitly.
            ctx.direct_booking = True
            events.append({"type": "DIRECT_BOOKING_REQUESTED"})
            return await self._complete_booking_without_transfer(ctx, turn, events)

        # Plain "yes" to "shall I confirm the booking, or connect you with
        # staff?" means confirm - booking is the first option offered. Only
        # fall back to a transfer when no explicit choice was on the table.
        if turn.intent == "affirm" and ctx.choice_offered and not (
            _TRANSFER_WORDS_RE.search(turn.text or "")
        ):
            ctx.direct_booking = True
            events.append({"type": "DIRECT_BOOKING_REQUESTED"})
            return await self._complete_booking_without_transfer(ctx, turn, events)

        if turn.intent == "affirm" or turn.intent == "transfer_request":
            auto_rule = bool((self.persona.transfer_rules or {}).get("require_caller_confirmation", True))
            if not auto_rule:
                # Business rule explicitly permits automatic transfer.
                return await self._do_transfer(
                    ctx, (self.persona.transfer_rules or {}).get(
                        "message_before_transfer",
                        "Please hold while I connect you to the receptionist.",
                    ),
                    events, False,
                )
            reply, llm_used = await self._natural(
                ctx,
                instruction=(
                    "The caller agreed to the transfer. Say you are connecting them "
                    "to the receptionist now and to please hold for a moment."
                ),
                fallback="Absolutely. Please hold for a moment while I connect you.",
            )
            return await self._do_transfer(ctx, reply, events, llm_used)

        if turn.date_value and turn.date_value.isoformat() != (ctx.selected_date or ctx.requested_date):
            # Caller changed the date — restart the availability check.
            ctx.requested_date = turn.date_value.isoformat()
            ctx.selected_date = None
            events += self._transition(ctx, BookingPhase.DATE_IDENTIFIED)
            return await self._h_date_identified(ctx, turn)

        if turn.intent == "booking_request" and not turn.date_value:
            # "no ... just book it directly" — a booking push without a new date
            # means the caller wants the agent to complete it in the system.
            if _NO_RE.search(turn.text or ""):
                ctx.direct_booking = True
                return await self._complete_booking_without_transfer(ctx, turn, events)

        if turn.guests and ctx.guests is None:
            ctx.guests = turn.guests

        # Still deciding — re-offer the transfer once, politely.
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                "Gently ask again whether they would like to be connected to the "
                "receptionist to complete the booking, or if they'd like a different "
                "date. Two short sentences max."
            ),
            fallback=(
                "Would you like me to connect you to the receptionist to complete "
                "the booking, or would you prefer a different date?"
            ),
        )
        return TurnResult(reply=reply, phase=BookingPhase.READY_FOR_TRANSFER,
                          context=ctx, events=events, llm_used=llm_used)

    async def _do_transfer(
        self,
        ctx: BookingContext,
        reply: str,
        events: List[Dict[str, Any]],
        llm_used: bool,
    ) -> TurnResult:
        events += self._transition(ctx, BookingPhase.TRANSFERRING)
        ctx.transfer_requested = True

        destination = (
            self.persona.receptionist_number
            or getattr(self.availability, "receptionist_number", "")
            or ""
        )
        if not destination:
            log.error("transfer requested but no receptionist number configured")
            events += self._transition(ctx, BookingPhase.TRANSFER_FAILED)
            return TurnResult(
                reply=(
                    "I'm sorry, I couldn't reach the receptionist line right now. "
                    "Would you like me to take your booking details instead?"
                ),
                phase=BookingPhase.TRANSFER_FAILED,
                context=ctx,
                events=events,
                action={"type": "transfer_failed", "reason": "no_destination_configured"},
            )

        if self.transfer_fn is None:
            log.error("transfer_fn not wired — telephony transfer unavailable")
            events += self._transition(ctx, BookingPhase.TRANSFER_FAILED)
            return TurnResult(
                reply=(
                    "I'm sorry, I'm unable to transfer the call at the moment. "
                    "May I take your booking details instead?"
                ),
                phase=BookingPhase.TRANSFER_FAILED,
                context=ctx,
                events=events,
                action={"type": "transfer_failed", "reason": "transfer_not_configured"},
            )

        try:
            result = await self.transfer_fn({
                "session_key": ctx.session_key,
                "call_id": ctx.call_id,
                "destination": destination,
                "context": ctx.to_dict(),
            })
        except Exception as exc:
            log.exception("transfer raised session=%s", ctx.session_key)
            result = {"success": False, "reason": str(exc)[:200]}

        success = bool(result.get("success"))
        events.append({
            "type": "TRANSFER_RESULT",
            "success": success,
            "reason": result.get("reason", ""),
            "destination": destination,
        })

        if success:
            events += self._transition(ctx, BookingPhase.TRANSFER_SUCCESS)
            return TurnResult(
                reply=reply,
                phase=BookingPhase.TRANSFER_SUCCESS,
                context=ctx,
                events=events,
                action={
                    "type": "transfer_success",
                    "destination": destination,
                    "provider": result.get("provider", ""),
                },
                llm_used=llm_used,
            )

        events += self._transition(ctx, BookingPhase.TRANSFER_FAILED)
        reason = result.get("reason") or "unknown"
        log.warning("transfer failed session=%s reason=%s", ctx.session_key, reason)
        return TurnResult(
            reply=(
                "I'm sorry, I couldn't connect you to the receptionist just now. "
                "Would you like me to take your booking details instead?"
            ),
            phase=BookingPhase.TRANSFER_FAILED,
            context=ctx,
            events=events,
            action={"type": "transfer_failed", "reason": reason[:120]},
            llm_used=llm_used,
        )

    async def _h_transferring(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        # Transfer already attempted; caller audio should be bridged. If we get
        # another turn here the transfer did not complete — recover.
        return await self._do_transfer(
            ctx, "Please hold while I connect you to the receptionist.", [], False
        )

    async def _h_transfer_done(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        return await self._end(ctx, turn, reason="transfer_completed")

    async def _h_transfer_failed(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []
        if turn.intent == "affirm" and not ctx.booking_id:
            ctx.direct_booking = True
            return await self._complete_booking_without_transfer(ctx, turn, events)
        if turn.intent == "transfer_request":
            return await self._do_transfer(
                ctx, "Let me try connecting you again. Please hold.", events, False
            )
        if not ctx.booking_id:
            ctx.direct_booking = True
            return await self._complete_booking_without_transfer(ctx, turn, events)
        return await self._end(ctx, turn, reason="transfer_failed_call_ended")

    async def _complete_booking_without_transfer(
        self, ctx: BookingContext, turn: CallerTurn, events: List[Dict[str, Any]]
    ) -> TurnResult:
        """Caller declined transfer — finish the booking in the system directly."""
        if turn.name and not ctx.guest_name:
            ctx.guest_name = turn.name
        if ctx.guests is None and turn.guests:
            ctx.guests = turn.guests

        booking_date = ctx.selected_date or ctx.requested_date
        if not booking_date:
            events += self._transition(ctx, BookingPhase.COLLECTING_REQUIREMENTS)
            reply, llm_used = self._instant(
                ctx,
                "Certainly. Which date shall I book for you?",
                "Of course. What date would you like me to book?",
            )
            return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                              context=ctx, events=events, llm_used=llm_used)

        events += self._transition(ctx, BookingPhase.COLLECTING_REQUIREMENTS)
        if ctx.guests is None:
            reply, llm_used = self._instant(
                ctx,
                "May I know how many guests the booking is for?",
                "How many guests will be staying with us?",
            )
            return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                              context=ctx, events=events, llm_used=llm_used)

        if not ctx.guest_name:
            reply, llm_used = self._instant(
                ctx,
                "May I have your name for the reservation, please?",
                "Could I take the name for the booking?",
            )
            return TurnResult(reply=reply, phase=BookingPhase.COLLECTING_REQUIREMENTS,
                              context=ctx, events=events, llm_used=llm_used)

        try:
            result = self.availability.create_booking(
                booking_date=booking_date,
                guests=ctx.guests,
                guest_name=ctx.guest_name,
                service=ctx.service,
                time_str=ctx.requested_time,
                phone=ctx.caller_phone,
                notes=f"voice agent booking (session {ctx.session_key})",
            )
        except Exception as exc:
            log.exception("booking creation failed session=%s", ctx.session_key)
            return await self._error_recovery(
                ctx, events, technical=str(exc), operation="create_booking"
            )

        if not result.created:
            log.warning("booking rejected: %s", result.message)
            return await self._error_recovery(
                ctx, events,
                technical=result.message,
                operation="create_booking",
                caller_reply=(
                    "I'm sorry, that date has just become unavailable. "
                    "May I check another date for you?"
                ),
            )

        ctx.booking_id = result.booking_id
        events.append({"type": "BOOKING_CREATED", "booking_id": result.booking_id,
                       "date": booking_date})
        events += self._transition(ctx, BookingPhase.BOOKING_CONFIRMED)
        label = _human_date(booking_date)
        # The confirmation states hard facts (real date, guest count, real
        # booking reference) so it is phrased instantly — a 3s pause before
        # "you're booked in" is the worst possible moment to make a guest wait.
        # Confirm, thank the guest for visiting, AND ask whether they need
        # anything else - all in one turn, so the line stays open rather than
        # ending the moment the booking succeeds.
        reply, llm_used = self._instant(
            ctx,
            f"You're all set — {label} is booked for {ctx.guests} under "
            f"{ctx.guest_name}, reference {result.booking_id}. Thank you for "
            f"visiting us, and enjoy your stay! Do you have any other queries?",
            f"That's confirmed — {label} is booked for {ctx.guests} guest(s) "
            f"under {ctx.guest_name}. Your reference is {result.booking_id}. "
            f"Thank you for visiting us! Is there anything else I can help you with?",
        )
        # Transition the STORED context too, not just the returned phase -
        # handle_turn dispatches on ctx.phase, so without this the next turn
        # would be handled by BOOKING_CONFIRMED and SARA would repeat herself.
        events += self._transition(ctx, BookingPhase.WRAP_UP)
        events.append({"type": "WRAP_UP_STARTED"})
        ctx.wrap_up_asked = True
        return TurnResult(reply=reply, phase=BookingPhase.WRAP_UP,
                          context=ctx, events=events, llm_used=llm_used)

    async def _h_booking_confirmed(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        # DO NOT end the call here. Hanging up straight after a successful
        # booking is what made SARA look like she was cutting the guest off.
        # She thanks them, confirms the booking and asks whether they need
        # anything else, keeping the line open like a real receptionist.
        return self._open_wrap_up(
            ctx, events=[],
            closing="Your room is booked. Thank you for visiting us.",
        )

    def _open_wrap_up(
        self,
        ctx: BookingContext,
        events: List[Dict[str, Any]],
        closing: str = "",
    ) -> TurnResult:
        """Ask 'anything else?' and KEEP THE CALL ALIVE."""
        events = list(events or [])
        # MUST actually move the stored context into WRAP_UP. Returning the phase
        # on the TurnResult is not enough: handle_turn dispatches on ctx.phase,
        # so without this transition the NEXT turn was still handled by the old
        # phase handler and SARA repeated the same closing line forever.
        events += self._transition(ctx, BookingPhase.WRAP_UP)
        events.append({"type": "WRAP_UP_STARTED"})
        ctx.wrap_up_asked = True
        reply, llm_used = self._instant(
            ctx,
            f"{closing} Do you have any other queries?",
            f"{closing} Is there anything else I can help you with today?",
            f"{closing} Can I help you with anything else before you go?",
        )
        return TurnResult(reply=reply, phase=BookingPhase.WRAP_UP,
                          context=ctx, events=events, llm_used=llm_used)

    async def _h_wrap_up(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        """Handle the guest's answer to 'do you have any other queries?'.

        A "no" closes the call politely. Anything else is answered naturally and
        the question is re-asked, so the guest is never cut off mid-sentence and
        never left on hold without a reply.
        """
        events: List[Dict[str, Any]] = []
        ctx.open_queries = (getattr(ctx, "open_queries", 0) or 0) + 1

        if turn.intent == "goodbye" or _NO_RE.search(turn.text or ""):
            return await self._end(
                ctx, turn, reason="caller_declined_further_help",
                farewell=(
                    f"Thank you for visiting {self.persona.business_name}. "
                    f"We do hope to see you again. Have a lovely day!"
                ),
            )

        # Guest has a real question. Answer it, then check again rather than
        # ending the call on them.
        events.append({"type": "FOLLOW_UP_QUERY", "text": (turn.text or "")[:120]})
        # A follow-up is the one place the LLM is still worth calling, but the
        # provider takes ~3s and a retry can exceed the 6.5s turn budget, which
        # made the caller hear "sorry about that hiccup" instead of an answer.
        # Cap the whole thing so the guest ALWAYS gets a real spoken reply.
        try:
            reply, llm_used = await asyncio.wait_for(
                self._answer_follow_up(ctx, turn), timeout=_FOLLOW_UP_BUDGET_S
            )
        except (asyncio.TimeoutError, Exception) as exc:
            log.info("follow-up answer over budget (%s) - using instant reply", type(exc).__name__)
            reply, llm_used = self._instant(
                ctx,
                "Let me check that for you. Is there anything else I can help with?",
                "I'll get that information for you. Anything else I can help with?",
            )
        return TurnResult(reply=reply, phase=BookingPhase.WRAP_UP,
                          context=ctx, events=events, llm_used=llm_used)

    async def _answer_follow_up(self, ctx: BookingContext, turn: CallerTurn) -> tuple[str, bool]:
        """LLM answer for a guest's follow-up question.

        Always time-boxed by the caller (_FOLLOW_UP_BUDGET_S) so a slow provider
        can never push the turn past the fast-turn budget and trigger the
        "sorry about that hiccup" recovery line.
        """
        instruction = (
            "The guest has a follow-up question. Answer it in one or two short "
            "spoken sentences, using only the hotel knowledge you have. Never "
            "invent prices or availability. Then ask if there is anything else "
            "they need."
        )
        fallback = (
            "Certainly, I've noted that for you. Is there anything else I can "
            "help you with?"
        )
        if ctx.open_queries >= 4:
            # Still answering, but gently steer toward closing so the call can
            # never run forever.
            instruction = (
                "Answer the guest briefly, then warmly ask whether there is "
                "anything else at all. One or two short sentences."
            )
        reply, llm_used = await self._natural(ctx, instruction=instruction, fallback=fallback)
        return reply, llm_used

    async def _h_error_recovery(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        events: List[Dict[str, Any]] = []
        if turn.date_value:
            ctx.requested_date = turn.date_value.isoformat()
            events += self._transition(ctx, BookingPhase.CHECKING_AVAILABILITY)
            return await self._check_and_respond(ctx, events)
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                "There was a small technical problem earlier. Ask the caller what "
                "they would like to do next, politely. One or two sentences."
            ),
            fallback=(
                "I'm having a little trouble checking that right now. "
                "Please give me a moment — which date would you like me to check?"
            ),
        )
        return TurnResult(reply=reply, phase=BookingPhase.ERROR_RECOVERY,
                          context=ctx, events=events, llm_used=llm_used)

    async def _error_recovery(
        self,
        ctx: BookingContext,
        events: List[Dict[str, Any]],
        *,
        technical: str,
        operation: str,
        caller_reply: Optional[str] = None,
    ) -> TurnResult:
        """Technical failure: caller-safe reply, technical detail to logs."""
        log.error(
            "booking operation failed op=%s session=%s technical=%s",
            operation, ctx.session_key, technical[:300],
        )
        events += self._transition(ctx, BookingPhase.ERROR_RECOVERY)
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                "There was a small technical hiccup. Apologise briefly, keep the "
                "call alive, and ask them to give you a moment. One or two sentences."
            ),
            fallback=(
                "I'm having a little trouble checking that right now. "
                "Please give me a moment."
            ),
        )
        return TurnResult(
            reply=caller_reply or reply,
            phase=BookingPhase.ERROR_RECOVERY,
            context=ctx,
            events=events + [{"type": "TECHNICAL_ERROR", "operation": operation}],
            llm_used=llm_used,
        )

    async def _h_ending(self, ctx: BookingContext, turn: CallerTurn) -> TurnResult:
        return await self._end(ctx, turn, reason="call_ending")

    async def _end(
        self,
        ctx: BookingContext,
        turn: CallerTurn,
        *,
        reason: str,
        farewell: Optional[str] = None,
    ) -> TurnResult:
        events = self._transition(ctx, BookingPhase.CALL_ENDING)
        events += self._transition(ctx, BookingPhase.CALL_ENDED)
        reply = farewell or (
            f"Thank you for calling {self.persona.business_name}. Have a great day!"
        )
        if turn.intent == "goodbye":
            reply, _ = await self._natural(
                ctx,
                instruction="Say a warm, brief goodbye.",
                fallback=reply,
            )
        return TurnResult(
            reply=reply,
            phase=BookingPhase.CALL_ENDED,
            context=ctx,
            events=events + [{"type": "CALL_ENDED", "reason": reason}],
            action={"type": "end_call", "reason": reason},
        )


def _human_date(iso: str) -> str:
    """'2026-10-08' -> 'the 8th of October' (or 'today'/'tomorrow')."""
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or ""
    today = local_now().date()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    day = d.day
    suffix = "th"
    if day % 10 == 1 and day != 11:
        suffix = "st"
    elif day % 10 == 2 and day != 12:
        suffix = "nd"
    elif day % 10 == 3 and day != 13:
        suffix = "rd"
    return f"the {day}{suffix} of {d.strftime('%B')}"
