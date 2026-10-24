"""Dynamic Election Campaign Voice Agent — editable profile + campaign call flow.

The SARA Voice Calling Agent already owns everything a campaign call needs:
the Twilio provider, the call state machine, the Twilio webhooks, the Sarvam
STT/TTS/translate stack, the 13-language detector and the failover LLM router.

What is NEW here is deliberately small:

* :class:`CampaignProfile` — the identity + message set the campaign owner
  configures from the UI. No candidate, ward or party is hardcoded in the
  engine: the saved profile is the single source of truth and every spoken
  line is rendered from it.
* :class:`CampaignConversationEngine` — a campaign-shaped conversation flow
  (greeting -> identity -> purpose -> introduction -> approved message -> FAQ
  -> closing) with branches for a busy recipient, a refusal, a do-not-call
  request and a human hand-off.

Like ``voice_booking`` this module is free of imports from
``app.voice_agent_engine`` so it can be unit tested standalone; the engine
wires the session factory and the provider adapters in at startup.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.services.voice_booking import TurnResult
from app.services.voice_booking_models import BookingContext
from app.services.voice_languages import (
    SUPPORTED_LANGUAGES,
    detect_language,
)

log = logging.getLogger("sara.voice.campaign")

CAMPAIGN_AGENT_KIND = "election_campaign"
CAMPAIGN_PROFILE_VERSION = 1


# ---------------------------------------------------------------------------
# Editable campaign profile
# ---------------------------------------------------------------------------

#: Bracketed labels that resolve to a profile field. The English names are the
#: canonical ones; the Telugu labels are the ones the demo greeting uses, so a
#: copy-pasted Telugu greeting works without the owner rewriting it.
PLACEHOLDER_ALIASES: Dict[str, str] = {
    "ప్రతినిధి పేరు": "representative_name",
    "ప్రతినిధి": "representative_name",
    "ప్రాంతం": "location",
    "పదవి": "position",
    "వార్డు": "ward",
    "సంస్థ": "organization_name",
    "ప్రచారణ": "campaign_name",
    "పంపరచు వివరాలు": "contact_details",
    "representative_name": "representative_name",
    "representative": "representative_name",
    "candidate": "representative_name",
    "organization_name": "organization_name",
    "organization": "organization_name",
    "party": "organization_name",
    "campaign_name": "campaign_name",
    "campaign": "campaign_name",
    "location": "location",
    "city": "location",
    "ward": "ward",
    "position": "position",
    "role": "position",
    "agent_name": "agent_name",
    "contact_details": "contact_details",
    "language": "language",
}

_PLACEHOLDER_RE = re.compile(r"\[([^\[\]]{1,80})\]")


def normalize_campaign_language(value: str) -> str:
    """Resolve a language hint to a supported code, or raise ValueError.

    ``resolve_language`` falls back to English for anything it does not
    recognise; a campaign configuration must never silently become English,
    so an unknown value is rejected instead.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("language cannot be empty")
    lowered = raw.lower()
    for info in SUPPORTED_LANGUAGES.values():
        if (
            lowered == info.code.lower()
            or lowered == info.short
            or lowered == info.name.lower()
            or lowered == info.native_name.lower()
            or lowered in info.aliases
        ):
            return info.code
    raise ValueError(f"unsupported language: {value!r}")


class CampaignProfile(BaseModel):
    """Everything the campaign owner can edit for the election voice agent.

    The values below are the shipped demo profile: generic placeholders, never
    a real candidate. Every field is replaceable from the frontend and the
    saved profile is what the agent speaks on the next call.
    """

    agent_name: str = "Dynamic Election Campaign Voice Agent"
    representative_name: str = "Demo Representative"
    organization_name: str = "Demo Organization"
    campaign_name: str = "Demo Campaign"
    location: str = "Demo City"
    ward: str = "Demo Ward"
    position: str = "Representative"
    language: str = "te-IN"
    voice: str = ""
    timezone: str = "Asia/Kolkata"

    greeting: str = (
        "నమస్కారం అండి! నా పేరు [ప్రతినిధి పేరు]. నేను [ప్రాంతం] నుంచి "
        "[పదవి] అభ్యర్థిగా పోటీ చేస్తున్నాను. మీకు ఇప్పుడు కొద్దిసేపు "
        "మాట్లాడటానికి సమయం ఉందా అండి?"
    )
    identity_disclosure: str = (
        "నేను [ప్రతినిధి పేరు], [సంస్థ] ప్రతినిధి. ఈ కాల్ [సంస్థ] నుంచి "
        "అధికారికంగా వస్తున్నాను."
    )
    purpose: str = (
        "[ప్రచారణ] గురించి మీకు కొద్దిగా వివరించి, మీ అభిప్రాయాన్ని తెలుసుకోవాలనుకుంటున్నాను."
    )
    introduction: str = (
        "నేను [ప్రాంతం], [వార్డు] నుంచి [పదవి]గా పోటీ చేస్తున్నాను. "
        "[ప్రచారణ] గురించి కొద్దిగా చెప్పగలనా?"
    )
    approved_message: str = (
        "ఇది మా అధికారికమైన ప్రచార సమాచారం: [ప్రచారణ] ప్రత్యేకంగా [ప్రాంతం], "
        "[వార్డు] ప్రజల కోసం. ధృవీకరించబడిన సమాచారం ఇతర చోట లేదు."
    )
    faqs: List[Dict[str, str]] = Field(
        default_factory=lambda: [
            {
                "question": "మీరు ఎక్కడి నుంచి పోటీ చేస్తున్నారు?",
                "answer": "నేను [ప్రాంతం], [వార్డు] నుంచి [పదవి]గా పోటీ చేస్తున్నాను.",
            },
            {
                "question": "మీరు ఎప్పుడు గుర్తులకు వెళ్తారు?",
                "answer": (
                    "[ప్రచారణ] గురించి మీరు అడిగినందుకు ధన్యవాదాలు. "
                    "దయచేసి [ప్రతినిధి పేరు] కి కాల్ చేయండి."
                ),
            },
        ]
    )
    busy_message: str = (
        "క్షమించండి, మీరు ఇప్పుడు బిజీగా ఉన్నట్లు కనిపిస్తోంది. మీకు సమయం "
        "వచ్చినప్పుడు మేము మళ్లీ కాల్ చేస్తాము. ధన్యవాదాలు."
    )
    decline_message: str = (
        "సరే, మీ నిర్ణయాన్ని మేము గౌరవిస్తున్నాము. మీకు వేరే సమాచారం కావాలంటే "
        "[ప్రతినిధి పేరు] ను కాల్ చేయండి. ధన్యవాదాలు."
    )
    human_handoff_message: str = (
        "దయచేసి కాసేపు, మీరు [ప్రతినిధి పేరు] కి మాట్లాడేందుకు మీ సంబంధితుడిని "
        "కనెక్ట్ చేస్తున్నాను."
    )
    do_not_call_message: str = (
        "మీ ఆద్మరణను గౌరవిస్తున్నాము. భవిష్యత్తులో మీకు కాల్ చేయబడదు. ధన్యవాదాలు."
    )
    closing_message: str = (
        "మీ సమయానికి ధన్యవాదాలు. [ప్రచారణ]కు మద్దతు ఇచ్చేందుకు దయచేసి "
        "మా ఆఫీస్‌ను సంప్రదించండి: [పంపరచు వివరాలు]."
    )
    unknown_answer: str = (
        "ఈ విషయంపై నాకు ఖచ్చితమైన సమాచారం లేదు. దయచేసి [ప్రతినిధి పేరు] కి "
        "మళ్లీ కాల్ చేయండి."
    )
    contact_details: str = "ఫోన్: +91-90000-00000 | ఇమెయిల్: contact@example.org"
    handoff_destination: str = ""
    calling_schedule: str = "09:00-19:00"
    calling_days: str = "mon,tue,wed,thu,fri,sat,sun"
    follow_up_enabled: bool = True

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        return normalize_campaign_language(value)

    @field_validator(
        "agent_name",
        "representative_name",
        "organization_name",
        "campaign_name",
        "location",
        "position",
    )
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("this field cannot be empty")
        return value

    @field_validator("faqs")
    @classmethod
    def _clean_faqs(cls, value: List[Dict[str, str]]) -> List[Dict[str, str]]:
        cleaned: List[Dict[str, str]] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question and not answer:
                continue
            cleaned.append({"question": question, "answer": answer})
        return cleaned

    def placeholder_values(self) -> Dict[str, str]:
        return {
            "agent_name": self.agent_name,
            "representative_name": self.representative_name,
            "organization_name": self.organization_name,
            "campaign_name": self.campaign_name,
            "location": self.location,
            "ward": self.ward,
            "position": self.position,
            "language": self.language,
            "contact_details": self.contact_details,
        }

    def render(self, text: str) -> str:
        """Replace ``[field]`` placeholders with the configured values.

        An unknown placeholder is dropped rather than spoken literally, so the
        caller never hears a bracket.
        """
        values = self.placeholder_values()

        def _replace(match: "re.Match[str]") -> str:
            key = match.group(1).strip().lower()
            field = PLACEHOLDER_ALIASES.get(key)
            if not field:
                return ""
            return str(values.get(field, "") or "")

        return re.sub(r"\s{2,}", " ", _PLACEHOLDER_RE.sub(_replace, text or "")).strip()

    def rendered_sections(self) -> Dict[str, str]:
        return {
            "greeting": self.render(self.greeting),
            "identity_disclosure": self.render(self.identity_disclosure),
            "purpose": self.render(self.purpose),
            "introduction": self.render(self.introduction),
            "approved_message": self.render(self.approved_message),
            "busy_message": self.render(self.busy_message),
            "decline_message": self.render(self.decline_message),
            "human_handoff_message": self.render(self.human_handoff_message),
            "do_not_call_message": self.render(self.do_not_call_message),
            "closing_message": self.render(self.closing_message),
            "unknown_answer": self.render(self.unknown_answer),
        }

    def rendered_faqs(self) -> List[Dict[str, str]]:
        return [
            {
                "question": self.render(item.get("question", "")),
                "answer": self.render(item.get("answer", "")),
            }
            for item in self.faqs
        ]


DEFAULT_CAMPAIGN_PROFILE = CampaignProfile()


# ---------------------------------------------------------------------------
# Calling schedule
# ---------------------------------------------------------------------------

_DAY_CODES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}


def _parse_clock(value: str) -> Optional[dt_time]:
    value = (value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt_time(hour=hour, minute=minute)


def parse_calling_days(value: str) -> set:
    """Weekday numbers (0=Mon) the agent is allowed to call on."""
    days: set = set()
    for token in re.split(r"[,\s/]+", (value or "").strip().lower()):
        if not token:
            continue
        if token in {"all", "every", "daily"} or token == "*":
            return set(range(7))
        if token in _DAY_CODES:
            days.add(_DAY_CODES[token])
    return days


def schedule_allows(profile: CampaignProfile, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Is `now` inside the configured calling window?"""
    if now is None:
        now = datetime.now()
    days = parse_calling_days(profile.calling_days)
    if days and now.weekday() not in days:
        return False, "Today is not an allowed calling day for this campaign."
    window = (profile.calling_schedule or "").strip()
    if not window:
        return True, ""
    parts = window.split("-")
    if len(parts) != 2:
        return True, ""
    start = _parse_clock(parts[0])
    end = _parse_clock(parts[1])
    if start is None or end is None:
        return True, ""
    # Compare at minute precision: 23:59:42 is still inside a 23:59 window.
    current = now.replace(second=0, microsecond=0).time()
    if start <= end:
        inside = start <= current <= end
    else:
        inside = current >= start or current <= end
    if not inside:
        return False, (
            f"Outside the configured calling window ({profile.calling_schedule})."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Caller intent detection
# ---------------------------------------------------------------------------

DO_NOT_CALL_PATTERNS = (
    r"do not call", r"don'?t call", r"stop calling", r"remove my number",
    r"never call", r"unsubscribe", r"do not contact",
    r"నన్ను కాల్ చేయనండి", r"నా నంబర్ తీసేయండి", r"కాల్ చేయకండి",
    r"ఇక్కడి నుంచి కాల్ చేయకండి",
)

DECLINE_PATTERNS = (
    r"not interested", r"no interest", r"don'?t want", r"do not want",
    r"no thanks", r"not now", r"not right now",
    r"అవసరం లేదు", r"ప్రత్యేకంగా ఇవ్వను", r"కాదు", r"వద్దు",
)

BUSY_PATTERNS = (
    r"i'?m busy", r"im busy", r"in a meeting", r"can'?t talk", r"cant talk",
    r"busy right now", r"call me later", r"driving",
    r"బిజీగా ఉన్నాను", r"ఇప్పుడు వీలు లేదు", r"వీలు లేదు", r"మీటింగ్",
)

HANDOFF_PATTERNS = (
    r"speak to (a )?(person|human|someone)", r"talk to (a )?(person|human)",
    r"human please", r"real person", r"talk to the candidate",
    r"మనిషితో మాట్లాడండి", r"ఆఫీసర్", r"నిజమైన వ్యక్తి",
)

QUESTION_WORDS = (
    "who", "what", "when", "where", "why", "how", "which", "do ", "does ",
    "did ", "can ", "are ", "is ", "was ", "will ", "would ", "could ",
    "ఎవరు", "ఏమి", "ఎప్పుడు", "ఎక్కడ", "ఎందుకు", "ఎలా", "ఎన్ని",
)


def _matches(text: str, patterns: Tuple[str, ...]) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in patterns)


def looks_like_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if "?" in stripped:
        return True
    lowered = stripped.lower()
    return any(lowered.startswith(word) or f" {word}" in lowered for word in QUESTION_WORDS)


# ---------------------------------------------------------------------------
# Conversation flow
# ---------------------------------------------------------------------------

class CampaignPhase(str, Enum):
    """States of a campaign conversation (one per editable flow section)."""

    GREETING = "GREETING"
    IDENTITY = "IDENTITY"
    PURPOSE = "PURPOSE"
    INTRODUCTION = "INTRODUCTION"
    INFORMATION = "INFORMATION"
    FAQ = "FAQ"
    CLOSING = "CLOSING"
    ENDED = "ENDED"


#: The linear happy path. The greeting is spoken by the answer webhook, so the
#: first caller turn delivers the identity disclosure. A question is answered
#: on demand and does not consume a step of this flow.
_SCRIPTED_SECTIONS = {
    CampaignPhase.GREETING: ("identity_disclosure", CampaignPhase.IDENTITY),
    CampaignPhase.IDENTITY: ("purpose", CampaignPhase.PURPOSE),
    CampaignPhase.PURPOSE: ("introduction", CampaignPhase.INTRODUCTION),
    CampaignPhase.INTRODUCTION: ("approved_message", CampaignPhase.INFORMATION),
    CampaignPhase.INFORMATION: ("closing_message", CampaignPhase.ENDED),
}


class CampaignConversationEngine:
    """Runs the campaign conversation for a call.

    Every scripted line comes from the saved :class:`CampaignProfile`, so the
    agent cannot drift away from the owner's approved wording. The LLM is used
    only to phrase an answer to a caller's question, and it is grounded on the
    configured FAQ answers plus the approved message; a deterministic fallback
    is returned whenever the model is unavailable, slow or off-script.
    """

    def __init__(
        self,
        *,
        profile_fn: Callable[[str], Optional[CampaignProfile]],
        llm_fn: Optional[Callable[[str, str, Dict[str, Any]], Awaitable[str]]] = None,
        transfer_fn: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        translator_fn: Optional[Callable[[str, str, str], Awaitable[str]]] = None,
    ) -> None:
        self.profile_fn = profile_fn
        self.llm_fn = llm_fn
        self.transfer_fn = transfer_fn
        self.translator_fn = translator_fn
        self._sessions: Dict[str, BookingContext] = {}

    # -- session management ----------------------------------------------------

    def get_context(self, session_key: str) -> BookingContext:
        ctx = self._sessions.get(session_key)
        if ctx is None:
            ctx = BookingContext(session_key=session_key)
            ctx.phase = CampaignPhase.GREETING.value
            self._sessions[session_key] = ctx
        return ctx

    def set_context(self, session_key: str, ctx: BookingContext) -> None:
        ctx.session_key = session_key
        self._sessions[session_key] = ctx

    def reset(self, session_key: str) -> None:
        self._sessions.pop(session_key, None)

    # -- helpers ---------------------------------------------------------------

    def _phase(self, ctx: BookingContext) -> CampaignPhase:
        try:
            return CampaignPhase(ctx.phase)
        except ValueError:
            return CampaignPhase.GREETING

    def _advance(self, ctx: BookingContext, phase: CampaignPhase) -> None:
        ctx.phase = phase.value

    async def _natural(
        self,
        ctx: BookingContext,
        *,
        instruction: str,
        fallback: str,
        context: str = "",
    ) -> Tuple[str, bool]:
        """Phrase a reply with the LLM; always fall back to the approved text."""
        if self.llm_fn is None:
            return fallback, False
        system = (
            "You are a campaign voice agent speaking on a live phone call. "
            "Answer in ONE or TWO short spoken sentences. Never use markdown, "
            "emoji, lists or stage directions. Use ONLY the approved information "
            "supplied below — never invent facts, numbers, dates, promises or "
            "offers. If the approved information does not cover the question, "
            "say you do not have that information. Reply in the caller's language. "
            + context
        )
        user = instruction
        try:
            reply = await self.llm_fn(system, user, {})
        except Exception as exc:  # noqa: BLE001 - a reply must never kill the call
            log.warning("campaign LLM failed: %s", exc)
            return fallback, False
        reply = (reply or "").strip()
        if len(reply) < 8 or len(reply) > 700:
            return fallback, False
        return reply, True

    def _matching_faq(self, profile: CampaignProfile, question: str) -> Optional[Dict[str, str]]:
        lowered = (question or "").strip().lower()
        if not lowered:
            return None
        tokens = {t for t in re.split(r"\W+", lowered) if len(t) > 2}
        best: Optional[Dict[str, str]] = None
        best_score = 0.0
        for item in profile.faqs:
            q = (item.get("question") or "").strip().lower()
            if not q:
                continue
            if q == lowered or q in lowered or lowered in q:
                return item
            q_tokens = {t for t in re.split(r"\W+", q) if len(t) > 2}
            if not q_tokens:
                continue
            score = len(tokens & q_tokens) / len(q_tokens)
            if score > best_score:
                best_score = score
                best = item
        if best is not None and best_score >= 0.5:
            return best
        return None

    # -- turn handling ---------------------------------------------------------

    async def handle_turn(
        self,
        session_key: str,
        text: str,
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

        profile = self.profile_fn(session_key)
        if profile is None:
            reply = (
                "This campaign configuration is no longer available. "
                "Please call back later."
            )
            self._advance(ctx, CampaignPhase.ENDED)
            return TurnResult(
                reply=reply,
                phase=ctx.phase,
                context=ctx,
                events=[{"type": "CAMPAIGN_PROFILE_MISSING"}],
            )

        if not ctx.language or ctx.language == "en-IN":
            ctx.language = profile.language or ctx.language

        decision = detect_language(ctx.last_text, current=ctx.language)
        if decision.changed:
            ctx.language = decision.language
        ctx.language_detected = decision.to_dict()

        spoken = ctx.last_text
        events: List[Dict[str, Any]] = []

        # --- refusals and opt-outs win over everything else ------------------
        if _matches(spoken, DO_NOT_CALL_PATTERNS):
            self._advance(ctx, CampaignPhase.ENDED)
            events.append({"type": "DO_NOT_CALL_REQUESTED"})
            return TurnResult(
                reply=profile.render(profile.do_not_call_message),
                phase=ctx.phase,
                context=ctx,
                events=events,
            )

        if _matches(spoken, DECLINE_PATTERNS):
            self._advance(ctx, CampaignPhase.ENDED)
            events.append({"type": "CALLER_DECLINED"})
            return TurnResult(
                reply=profile.render(profile.decline_message),
                phase=ctx.phase,
                context=ctx,
                events=events,
            )

        if _matches(spoken, BUSY_PATTERNS):
            self._advance(ctx, CampaignPhase.ENDED)
            events.append({"type": "CALLER_BUSY"})
            return TurnResult(
                reply=profile.render(profile.busy_message),
                phase=ctx.phase,
                context=ctx,
                events=events,
            )

        if _matches(spoken, HANDOFF_PATTERNS):
            events.append({"type": "HUMAN_HANDOFF_REQUESTED"})
            return await self._handoff(ctx, profile, events)

        # --- a question is answered from the approved FAQ ---------------------
        if looks_like_question(spoken):
            reply, llm_used = await self._answer_question(ctx, profile, spoken)
            events.append({"type": "FAQ_ANSWERED"})
            return TurnResult(
                reply=reply,
                phase=ctx.phase,
                context=ctx,
                events=events,
                llm_used=llm_used,
            )

        # --- the scripted flow ------------------------------------------------
        delivered = self._phase(ctx)
        reply, next_phase = self._scripted_reply(ctx, profile)
        self._advance(ctx, next_phase)
        events.append({"type": "CAMPAIGN_SECTION", "section": delivered.value})
        return TurnResult(
            reply=reply,
            phase=ctx.phase,
            context=ctx,
            events=events,
        )

    def _scripted_reply(
        self, ctx: BookingContext, profile: CampaignProfile
    ) -> Tuple[str, CampaignPhase]:
        """The next approved line of the campaign, and the phase after it."""
        phase = self._phase(ctx)
        sections = profile.rendered_sections()
        section, next_phase = _SCRIPTED_SECTIONS.get(
            phase, ("closing_message", CampaignPhase.ENDED)
        )
        return sections.get(section, ""), next_phase

    async def _answer_question(
        self, ctx: BookingContext, profile: CampaignProfile, question: str
    ) -> Tuple[str, bool]:
        """Answer from the configured FAQ; the LLM only phrases the answer."""
        match = self._matching_faq(profile, question)
        if match is not None:
            return profile.render(match.get("answer", "")), False

        approved = profile.rendered_sections()["approved_message"]
        faq_block = "\n".join(
            f"- Q: {item.get('question', '')}\n  A: {item.get('answer', '')}"
            for item in profile.rendered_faqs()
        )
        context = (
            f"\nApproved message:\n{approved}\n"
            f"Approved questions and answers:\n{faq_block or '(none)'}\n"
            f"Candidate: {profile.representative_name} "
            f"({profile.position}), {profile.organization_name}, "
            f"{profile.location} {profile.ward}. "
            f"Campaign: {profile.campaign_name}."
        )
        fallback = profile.render(profile.unknown_answer)
        reply, llm_used = await self._natural(
            ctx,
            instruction=(
                f'The caller asked: "{question}". Answer briefly using only the '
                "approved information. If it is not covered, say you do not have "
                "that information and offer the contact details."
            ),
            fallback=fallback,
            context=context,
        )
        return reply, llm_used

    async def _handoff(
        self, ctx: BookingContext, profile: CampaignProfile, events: List[Dict[str, Any]]
    ) -> TurnResult:
        """Offer the configured human hand-off destination."""
        destination = (profile.handoff_destination or "").strip()
        message = profile.render(profile.human_handoff_message)
        if not destination:
            events.append({"type": "HUMAN_HANDOFF_UNCONFIGURED"})
            return TurnResult(
                reply=message,
                phase=ctx.phase,
                context=ctx,
                events=events,
            )

        if self.transfer_fn is not None:
            try:
                result = await self.transfer_fn(
                    {
                        "session_key": ctx.session_key,
                        "destination": destination,
                        "call_id": ctx.call_id,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("campaign hand-off failed: %s", exc)
                result = {"success": False, "reason": str(exc)[:160]}
        else:
            result = {"success": False, "reason": "transfer_not_configured"}

        if result.get("success"):
            events.append({"type": "HUMAN_HANDOFF_CONNECTED"})
            return TurnResult(
                reply=message,
                phase=ctx.phase,
                context=ctx,
                events=events,
                action={"type": "transfer_success", "destination": destination},
            )

        events.append(
            {"type": "HUMAN_HANDOFF_FAILED", "reason": result.get("reason", "")}
        )
        return TurnResult(
            reply=message,
            phase=ctx.phase,
            context=ctx,
            events=events,
        )
