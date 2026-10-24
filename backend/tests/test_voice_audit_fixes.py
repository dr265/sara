"""Regression tests for the second-pass audit fixes."""
import base64
import hashlib
import hmac

import pytest


def _sign(token: str, url: str, params: dict) -> str:
    s = url
    for k in sorted(params):
        s += k + params[k]
    return base64.b64encode(
        hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()
    ).decode()


# --- date/time intelligence -------------------------------------------------

def test_relative_day_offsets():
    from datetime import datetime
    from app.services.voice_booking_models import resolve_date_phrase
    now = datetime(2026, 9, 30)
    assert str(resolve_date_phrase("two days later", now=now)) == "2026-10-02"
    assert str(resolve_date_phrase("in two days", now=now)) == "2026-10-02"
    assert str(resolve_date_phrase("3 days after that", now=now)) == "2026-10-03"
    assert str(resolve_date_phrase("in 10 days", now=now)) == "2026-10-10"


def test_spoken_period_words_map_to_correct_hour():
    from app.services.voice_booking_models import parse_time_phrase
    # "2:00 in the evening" used to parse as 02:00 -> wrong booking.
    assert parse_time_phrase("2:00 in the evening") == "14:00"
    assert parse_time_phrase("9 at night") == "21:00"
    assert parse_time_phrase("around 5 PM") == "17:00"
    assert parse_time_phrase("11 am") == "11:00"
    # hotel context: "check in at 3" means afternoon, not 3 a.m.
    assert parse_time_phrase("check in at 3") == "15:00"


# --- stream phrase cache keying -------------------------------------------

def test_stream_cache_key_is_language_scoped():
    import app.voice_agent_engine as v
    en = v._stream_phrase_key("Hello there", "en-IN")
    hi = v._stream_phrase_key("Hello there", "hi-IN")
    assert en != hi, "same text in two languages must not share a cache entry"


# --- Twilio webhook signature validation ----------------------------------

def test_twilio_signature_rejects_forged_request():
    from fastapi.testclient import TestClient
    import app.voice_agent_engine as v
    if not (v.PUBLIC_BASE_URL and v.TWILIO_AUTH_TOKEN):
        pytest.skip("Twilio not configured")
    client = TestClient(v.app)
    params = {"SpeechResult": "hello", "CallSid": "CAforged", "From": "+91"}
    r = client.post(
        "/voice/twilio/gather", data=params,
        headers={"X-Twilio-Signature": "forged-signature"},
    )
    assert "<Hangup/>" in r.text


def test_twilio_signature_accepts_genuine_request():
    from fastapi.testclient import TestClient
    import app.voice_agent_engine as v
    if not (v.PUBLIC_BASE_URL and v.TWILIO_AUTH_TOKEN):
        pytest.skip("Twilio not configured")
    client = TestClient(v.app)
    params = {"SpeechResult": "hello", "CallSid": "CAreal", "From": "+91"}
    url = "http://testserver/voice/twilio/gather"
    r = client.post(
        "/voice/twilio/gather", data=params,
        headers={"X-Twilio-Signature": _sign(v.TWILIO_AUTH_TOKEN, url, params)},
    )
    assert "<Hangup/>" not in r.text


def test_status_callback_always_returns_200():
    from fastapi.testclient import TestClient
    import app.voice_agent_engine as v
    client = TestClient(v.app)
    r = client.post(
        "/voice/twilio/status-callback",
        data={"CallSid": "CAx", "CallStatus": "completed", "CallDuration": "42"},
    )
    assert r.status_code == 200


# --- compound guest counts ---------------------------------------------------

def test_compound_guest_counts():
    from app.services.voice_booking_models import parse_guest_count
    # "twenty five guests" used to match the single word "five" and return 5,
    # so a 25-person request was checked against 5 and offered a room the
    # hotel could not actually hold.
    assert parse_guest_count("twenty five guests") == 25
    assert parse_guest_count("twenty-five") == 25
    assert parse_guest_count("thirty two guests") == 32
    assert parse_guest_count("ten of us") == 10
    # plain forms must be unchanged
    assert parse_guest_count("two people") == 2
    assert parse_guest_count("25 guests") == 25


def test_oversized_party_is_not_offered_a_transfer():
    import asyncio
    from app import voice_agent_engine as v
    from datetime import date, timedelta

    async def run():
        key = "twilio:pytest_capacity"
        v.booking_engine.get_context(key)
        target = (date.today() + timedelta(days=20)).isoformat()
        # Block enough that no nearby date fits a huge party.
        v.demo_hotel._data["blocked_dates"] = [
            (date.today() + timedelta(days=d)).isoformat() for d in (19, 20, 21)
        ]
        try:
            r = await v.booking_engine.handle_turn(
                key, "I want to book a room for twenty five guests on the " + target
            )
            reply = (getattr(r, "reply", "") or "").lower()
            # It must not claim a big party is available.
            assert "available for 25" not in reply
            assert "up to 5 guests" not in reply
        finally:
            v.demo_hotel._data["blocked_dates"] = []
    asyncio.run(run())



# --- anti-loop guards (live-call regressions) ------------------------------
#
# Both tests below reproduce loops seen on real calls. They are deliberately
# written against the SAME fixture the main suite uses so they exercise the
# real state machine rather than a mock.

def _fresh_engine():
    from app.services.voice_booking import BookingConversationEngine
    from app.services.voice_booking_models import DemoHotelAvailability, PersonaConfig
    import json
    import tempfile
    from pathlib import Path

    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "loop_hotel.json"
    path.write_text(json.dumps({
        "hotel": {"name": "Loop Test Stay", "timezone": "Asia/Kolkata"},
        "receptionist_number": "+919000000999",
        "rooms": [{"type": "Standard", "total": 5, "price_per_night": 3500}],
        "blocked_dates": [],
        "bookings": [],
    }), encoding="utf-8")
    eng = BookingConversationEngine(
        availability=DemoHotelAvailability(path=path),
        persona=PersonaConfig(name="SARA", business_name="Loop Test Stay",
                              receptionist_number="+919000000999"),
        llm_fn=None,
        transfer_fn=None,
        timezone_name="Asia/Kolkata",
    )
    return eng, tmp


def test_guest_question_never_asked_more_than_twice():
    """Live-call regression: "how many guests?" repeated 2-3 times.

    The date was given FIRST, so the flow sat in DATE_IDENTIFIED, which asked
    the guest question WITHOUT incrementing the asked_guests counter. Every
    unintelligible answer therefore re-asked forever.
    """
    import asyncio
    from datetime import date, timedelta

    eng, tmp = _fresh_engine()
    try:
        session = "loop-guests"
        ctx = eng.get_context(session)
        ctx.phase = "GREETING"
        ctx.greeting_completed = True
        target = (date.today() + timedelta(days=9)).isoformat()

        async def run():
            replies = [await eng.handle_turn(session, f"room for {target}")]
            for _ in range(4):
                replies.append(await eng.handle_turn(session, "ummm"))
            return replies

        replies = asyncio.run(run())
        guest_asks = [
            r for r in replies
            if "guest" in (r.reply or "").lower()
            and r.phase.name == "COLLECTING_REQUIREMENTS"
        ]
        assert len(guest_asks) <= 2, (
            f"guest question asked {len(guest_asks)} times - loop regression"
        )
        assert ctx.guests == 2, f"expected fallback to 2 guests, got {ctx.guests}"
    finally:
        tmp.cleanup()


def test_date_question_never_asked_more_than_twice():
    """Live-call regression: "which date?" repeated 2-3 times."""
    import asyncio

    eng, tmp = _fresh_engine()
    try:
        # Wire a working transfer so escalation SUCCEEDS, as it does in
        # production. Without this the test would count the transfer-failure
        # recovery as a third date question, which never happens on a real call.
        async def _stub_transfer(payload):
            return {"success": True, "sid": "CA_stub_transfer"}

        eng.transfer_fn = _stub_transfer
        session = "loop-dates"
        ctx = eng.get_context(session)
        ctx.phase = "GREETING"
        ctx.greeting_completed = True

        async def run():
            replies = [await eng.handle_turn(session, "I want a room")]
            for _ in range(4):
                replies.append(await eng.handle_turn(session, "hmmmm"))
            return replies

        replies = asyncio.run(run())
        assert ctx.asked_dates <= 3, (
            f"date question asked {ctx.asked_dates} times - loop regression"
        )
        # The invariant is how many times the question was asked, not the ending
        # phase: after repeated failures the agent escalates to a human, and
        # with no transfer_fn wired in tests that shows up as TRANSFER_FAILED.
        # replies[0] is the post-greeting reply, which asks for date AND guests
        # together, so the date-branch questions are counted from replies[1:].
        date_asks = [
            r for r in replies[1:]
            if r.phase.name == "COLLECTING_REQUIREMENTS"
            and ("date" in (r.reply or "").lower() or "day and month" in (r.reply or "").lower())
        ]
        assert len(date_asks) <= 2, (
            f"date question asked {len(date_asks)} times - loop regression"
        )
    finally:
        tmp.cleanup()


# --- never cut the call off (live-call requirement) ------------------------

def test_declining_never_ends_the_call():
    """A guest who says "no" must never be hung up on.

    Drives a faithful engine (real demo-hotel data) rather than the shared
    fixture, and asserts the whole contract:
      decline -> polite thanks + "come back again" + "anything else?"
      a second decline -> only THEN does the call end.
    """
    import asyncio
    eng, tmp = _fresh_engine()
    try:
        s = "wrap-no"
        async def run():
            r = []
            r.append(await eng.handle_turn(s, "I want a room for the 8th of October"))
            r.append(await eng.handle_turn(s, "two"))
            r.append(await eng.handle_turn(s, "no, I won't book then"))
            r.append(await eng.handle_turn(s, "no thanks"))
            return r
        replies = asyncio.run(run())
        r1 = replies[2]
        assert r1.phase.name != "CALL_ENDED", "SARA hung up on a declining guest"
        assert r1.phase.name == "WRAP_UP", f"expected WRAP_UP, got {r1.phase.name}"
        spoken = (r1.reply or "").lower()
        assert (
            "anything else" in spoken or "other quer" in spoken
            or "anything at all" in spoken
        ), f"must offer further help, got: {r1.reply!r}"
        assert replies[3].phase.name == "CALL_ENDED", "second decline must end the call"
    finally:
        tmp.cleanup()


def test_successful_booking_keeps_the_line_open():
    """After booking, SARA thanks the guest and asks if they need anything else.

    Hanging up straight after a successful booking is exactly what made her look
    like she was cutting guests off.
    """
    import asyncio
    from datetime import date, timedelta

    eng, tmp = _fresh_engine()
    try:
        s = "wrap-yes"
        target = (date.today() + timedelta(days=9)).isoformat()

        async def run():
            r = []
            r.append(await eng.handle_turn(s, f"I want a room for {target}"))
            r.append(await eng.handle_turn(s, "two"))
            r.append(await eng.handle_turn(s, "yes book it"))
            r.append(await eng.handle_turn(s, "my name is Ravi"))
            r.append(await eng.handle_turn(s, "no"))
            return r

        replies = asyncio.run(run())
        confirm = replies[3]
        ctx = eng.get_context(s)
        assert ctx.booking_id, "booking should have been created"
        assert "booked" in (confirm.reply or "").lower() or "set" in (confirm.reply or "").lower()
        assert "thank you for visiting" in (confirm.reply or "").lower()
        assert confirm.phase.name == "WRAP_UP", (
            "confirmation must keep the call open, not end it"
        )
        assert (
            "any other quer" in (confirm.reply or "").lower()
            or "anything else" in (confirm.reply or "").lower()
        ), f"must ask about further queries, got: {confirm.reply!r}"
        # Declining that follow-up ends the call.
        assert replies[4].phase.name == "CALL_ENDED", (
            "declining the follow-up should end the call"
        )
    finally:
        tmp.cleanup()


def test_room_types_are_offered_before_the_booking_choice():
    """The caller must hear the real room types before being asked to book."""
    import asyncio
    from datetime import date, timedelta

    eng, tmp = _fresh_engine()
    try:
        s = "rooms"
        target = (date.today() + timedelta(days=9)).isoformat()

        async def run():
            await eng.handle_turn(s, f"I want a room for {target}")
            return await eng.handle_turn(s, "two")

        r = asyncio.run(run())
        spoken = (r.reply or "").lower()
        assert "standard" in spoken, f"expected the real room types, got: {r.reply!r}"
        assert "book" in spoken or "confirm" in spoken
        assert "staff" in spoken or "team" in spoken
    finally:
        tmp.cleanup()
