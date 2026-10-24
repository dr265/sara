"""Tests for the Dynamic Election Campaign Voice Agent.

The campaign agent reuses the existing voice stack (Twilio provider, call
state machine, webhooks, Sarvam stack, failover LLM router). These tests
cover what the campaign agent adds: the editable profile, the placeholder
rendering, the conversation flow, the tenant isolation and the API surface.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def voice_app(tmp_path, monkeypatch):
    """The voice engine wired to a throwaway database."""
    import app.voice_agent_engine as v

    engine = create_engine(
        f"sqlite:///{tmp_path / 'voice.db'}", connect_args={"check_same_thread": False}
    )
    v.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(v, "engine", engine)
    monkeypatch.setattr(v, "SessionLocal", session)
    return v


@pytest.fixture()
def client(voice_app):
    from app.api.deps import current_tenant
    from app.agents.sara_ultron_agent.core.tenancy import Tenant

    v = voice_app
    v.app.dependency_overrides[current_tenant] = lambda: Tenant(
        id="user_alice", kind="clerk", label="alice"
    )
    with TestClient(v.app) as test_client:
        yield test_client
    v.app.dependency_overrides.pop(current_tenant, None)


def _tenant_override(client, tenant_id: str):
    from app.api.deps import current_tenant
    from app.agents.sara_ultron_agent.core.tenancy import Tenant

    client.app.dependency_overrides[current_tenant] = lambda: Tenant(
        id=tenant_id, kind="clerk", label=tenant_id
    )


# ---------------------------------------------------------------------------
# Profile validation + placeholder rendering
# ---------------------------------------------------------------------------

def test_profile_rejects_blank_identity_fields():
    from app.services.voice_campaign import CampaignProfile

    with pytest.raises(Exception):
        CampaignProfile(representative_name="  ")
    with pytest.raises(Exception):
        CampaignProfile(organization_name="")


def test_profile_rejects_unsupported_language():
    from app.services.voice_campaign import CampaignProfile

    with pytest.raises(Exception):
        CampaignProfile(language="xx-XX")


def test_profile_accepts_every_supported_language():
    from app.services.voice_campaign import CampaignProfile
    from app.services.voice_languages import supported_language_list

    for entry in supported_language_list():
        profile = CampaignProfile(language=str(entry["code"]))
        assert profile.language == entry["code"]


def test_telugu_greeting_placeholders_are_resolved():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        representative_name="Demo Representative",
        organization_name="Demo Organization",
        location="Demo City",
        position="Representative",
    )
    spoken = profile.render(profile.greeting)
    assert "Demo Representative" in spoken
    assert "Demo City" in spoken
    assert "Representative" in spoken
    assert "[" not in spoken and "]" not in spoken


def test_unknown_placeholder_is_not_spoken():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(greeting="Hello [not_a_field] there")
    assert profile.render(profile.greeting) == "Hello there"


def test_english_placeholders_also_resolve():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        representative_name="Asha",
        location="Machilipatnam",
        ward="Ward 37",
        position="Corporator",
    )
    spoken = profile.render("I am [representative_name] from [location] [ward], a [position].")
    assert spoken == "I am Asha from Machilipatnam Ward 37, a Corporator."


def test_changing_the_profile_changes_the_rendered_text():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(representative_name="First")
    assert "First" in profile.render(profile.greeting)
    profile.representative_name = "Second"
    assert "Second" in profile.render(profile.greeting)
    assert "First" not in profile.render(profile.greeting)


# ---------------------------------------------------------------------------
# Calling schedule
# ---------------------------------------------------------------------------

def test_calling_schedule_blocks_outside_the_window():
    from app.services.voice_campaign import CampaignProfile, schedule_allows

    profile = CampaignProfile(calling_schedule="09:00-19:00", calling_days="all")
    allowed, _ = schedule_allows(profile, datetime(2026, 5, 4, 10, 0))
    assert allowed is True
    allowed, reason = schedule_allows(profile, datetime(2026, 5, 4, 22, 0))
    assert allowed is False
    assert "calling window" in reason


def test_calling_schedule_compares_at_minute_precision():
    from app.services.voice_campaign import CampaignProfile, schedule_allows

    profile = CampaignProfile(calling_schedule="00:00-23:59", calling_days="all")
    allowed, _ = schedule_allows(profile, datetime(2026, 5, 4, 23, 59, 42))
    assert allowed is True


def test_calling_schedule_blocks_disallowed_days():
    from app.services.voice_campaign import CampaignProfile, schedule_allows

    profile = CampaignProfile(calling_schedule="09:00-19:00", calling_days="mon,tue")
    # 2026-05-04 is a Monday, 2026-05-06 a Wednesday.
    assert schedule_allows(profile, datetime(2026, 5, 4, 10, 0))[0] is True
    assert schedule_allows(profile, datetime(2026, 5, 6, 10, 0))[0] is False


# ---------------------------------------------------------------------------
# Conversation flow
# ---------------------------------------------------------------------------

def _engine(profile):
    from app.services.voice_campaign import CampaignConversationEngine

    return CampaignConversationEngine(profile_fn=lambda _key: profile)


def test_scripted_flow_speaks_the_configured_sections():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        representative_name="Asha Rao",
        organization_name="People Party",
        campaign_name="Bright City",
        location="Machilipatnam",
        ward="Ward 37",
        position="Corporator",
    )
    engine = _engine(profile)
    replies = []
    for _ in range(5):
        result = asyncio.run(engine.handle_turn("sim:1", "ok"))
        replies.append(result.reply)

    joined = " ".join(replies)
    assert "Asha Rao" in joined
    assert "People Party" in joined
    assert "Bright City" in joined
    assert "Machilipatnam" in joined


def test_updating_the_profile_changes_the_next_turn():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(representative_name="First")
    engine = _engine(profile)
    first = asyncio.run(engine.handle_turn("sim:2", "hello"))
    assert "First" in first.reply

    profile.representative_name = "Second"
    second = asyncio.run(engine.handle_turn("sim:3", "hello"))
    assert "Second" in second.reply


def test_do_not_call_request_uses_the_configured_message():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        do_not_call_message="We will never call you again. Goodbye."
    )
    engine = _engine(profile)
    result = asyncio.run(engine.handle_turn("sim:4", "please do not call me again"))
    assert "never call you again" in result.reply
    assert any(e.get("type") == "DO_NOT_CALL_REQUESTED" for e in result.events)


def test_decline_and_busy_use_their_own_messages():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        decline_message="We respect your choice, goodbye.",
        busy_message="You are busy, we will call later.",
    )
    engine = _engine(profile)
    declined = asyncio.run(engine.handle_turn("sim:5", "I am not interested"))
    assert "respect your choice" in declined.reply
    assert any(e.get("type") == "CALLER_DECLINED" for e in declined.events)

    busy = asyncio.run(engine.handle_turn("sim:6", "I am busy right now"))
    assert "we will call later" in busy.reply
    assert any(e.get("type") == "CALLER_BUSY" for e in busy.events)


def test_human_handoff_uses_the_configured_destination():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        handoff_destination="+919000000123",
        human_handoff_message="Connecting you now.",
    )
    calls = []

    async def transfer(request):
        calls.append(request)
        return {"success": True}

    from app.services.voice_campaign import CampaignConversationEngine

    engine = CampaignConversationEngine(
        profile_fn=lambda _key: profile, transfer_fn=transfer
    )
    result = asyncio.run(engine.handle_turn("sim:7", "let me speak to a person"))
    assert "Connecting you now" in result.reply
    assert result.action["type"] == "transfer_success"
    assert result.action["destination"] == "+919000000123"
    assert calls and calls[0]["destination"] == "+919000000123"


def test_faq_is_answered_from_the_configured_answers():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        faqs=[
            {
                "question": "Which ward are you contesting?",
                "answer": "I am contesting Ward 37 of Machilipatnam.",
            }
        ]
    )
    engine = _engine(profile)
    result = asyncio.run(engine.handle_turn("sim:8", "Which ward are you contesting?"))
    assert "Ward 37" in result.reply
    assert any(e.get("type") == "FAQ_ANSWERED" for e in result.events)


def test_unknown_question_falls_back_to_the_safe_answer():
    from app.services.voice_campaign import CampaignProfile

    profile = CampaignProfile(
        unknown_answer="I do not have that information.",
        faqs=[{"question": "Which ward?", "answer": "Ward 37."}],
    )
    engine = _engine(profile)
    result = asyncio.run(engine.handle_turn("sim:9", "What is your bank balance?"))
    assert "do not have that information" in result.reply


# ---------------------------------------------------------------------------
# API â€” configuration lifecycle
# ---------------------------------------------------------------------------

def test_campaign_api_requires_an_identity(client):
    from app.api.deps import current_tenant

    client.app.dependency_overrides.pop(current_tenant, None)
    response = client.get("/voice/campaign/profiles")
    assert response.status_code == 401


def test_create_and_read_back_a_campaign_agent(client):
    created = client.post(
        "/voice/campaign/profiles",
        json={"name": "My Agent", "profile": {"representative_name": "Asha"}},
    )
    assert created.status_code == 200, created.text
    profile = created.json()["profile"]
    assert profile["name"] == "My Agent"
    assert profile["draft"]["representative_name"] == "Asha"
    assert profile["status"] == "PUBLISHED"

    fetched = client.get(f"/voice/campaign/profiles/{profile['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["profile"]["draft"]["representative_name"] == "Asha"


def test_invalid_configuration_is_rejected_with_a_useful_error(client):
    created = client.post(
        "/voice/campaign/profiles", json={"name": "Broken", "profile": {}}
    )
    profile_id = created.json()["profile"]["id"]
    response = client.put(
        f"/voice/campaign/profiles/{profile_id}",
        json={"profile": {"representative_name": ""}, "publish": True},
    )
    assert response.status_code == 422
    assert "representative_name" in response.json()["detail"]


def test_draft_edit_does_not_change_the_published_version(client):
    created = client.post(
        "/voice/campaign/profiles",
        json={"name": "Draft agent", "profile": {"representative_name": "Published Name"}},
    )
    profile_id = created.json()["profile"]["id"]

    saved = client.put(
        f"/voice/campaign/profiles/{profile_id}",
        json={"profile": {"representative_name": "Draft Name"}, "publish": False},
    )
    assert saved.status_code == 200
    body = saved.json()["profile"]
    assert body["draft"]["representative_name"] == "Draft Name"
    assert body["profile"]["representative_name"] == "Published Name"
    assert body["version"] == 1

    published = client.post(f"/voice/campaign/profiles/{profile_id}/publish")
    assert published.status_code == 200
    body = published.json()["profile"]
    assert body["profile"]["representative_name"] == "Draft Name"
    assert body["version"] == 2


def test_preview_resolves_the_placeholders(client):
    created = client.post(
        "/voice/campaign/profiles",
        json={
            "name": "Preview agent",
            "profile": {
                "representative_name": "Asha",
                "location": "Machilipatnam",
                "ward": "Ward 37",
            },
        },
    )
    profile_id = created.json()["profile"]["id"]
    preview = client.get(f"/voice/campaign/profiles/{profile_id}/preview")
    assert preview.status_code == 200
    published = preview.json()["published"]
    assert "Asha" in published["greeting"]
    assert "Machilipatnam" in published["greeting"]
    assert "[" not in published["greeting"]


def test_simulate_uses_the_saved_configuration(client):
    created = client.post(
        "/voice/campaign/profiles",
        json={"name": "Sim agent", "profile": {"representative_name": "Simulated Name"}},
    )
    profile_id = created.json()["profile"]["id"]
    response = client.post(
        f"/voice/campaign/profiles/{profile_id}/simulate",
        json={"message": "hello"},
    )
    assert response.status_code == 200
    assert "Simulated Name" in response.json()["reply"]


# ---------------------------------------------------------------------------
# API â€” multi-user isolation
# ---------------------------------------------------------------------------

def test_one_user_cannot_see_or_edit_another_users_agent(client):
    created = client.post(
        "/voice/campaign/profiles", json={"name": "Alice agent", "profile": {}}
    )
    profile_id = created.json()["profile"]["id"]

    _tenant_override(client, "user_bob")
    assert client.get("/voice/campaign/profiles").json()["profiles"] == []
    assert client.get(f"/voice/campaign/profiles/{profile_id}").status_code == 404
    assert (
        client.put(
            f"/voice/campaign/profiles/{profile_id}", json={"profile": {}}
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/voice/campaign/profiles/{profile_id}").status_code == 404
    )


# ---------------------------------------------------------------------------
# API â€” contacts and calling safeguards
# ---------------------------------------------------------------------------

def test_saving_a_contact_never_starts_a_call(client):
    created = client.post(
        "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000001111"}
    )
    assert created.status_code == 200
    contact = created.json()["contact"]
    assert contact["eligible"] is True
    assert client.get("/voice/campaign/calls").json()["calls"] == []


def test_opting_out_marks_the_contact_ineligible(client):
    contact = client.post(
        "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000002222"}
    ).json()["contact"]

    import app.voice_agent_engine as v

    with v.SessionLocal() as db:
        row = db.get(v.Customer, contact["id"])
        row.do_not_contact = True
        row.consent_status = "OPTED_OUT"
        db.commit()

    listed = client.get("/voice/campaign/contacts").json()["contacts"]
    assert listed[0]["eligible"] is False


def test_call_is_refused_for_an_opted_out_contact(client, voice_app):
    v = voice_app
    contact = client.post(
        "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000003333"}
    ).json()["contact"]
    with v.SessionLocal() as db:
        row = db.get(v.Customer, contact["id"])
        row.do_not_contact = True
        db.commit()

    profile = client.post("/voice/campaign/profiles", json={"name": "Caller"}).json()[
        "profile"
    ]
    response = client.post(
        "/voice/campaign/calls",
        json={"profile_id": profile["id"], "customer_id": contact["id"]},
    )
    assert response.status_code == 403
    assert "not to be called" in response.json()["detail"]


def test_call_is_refused_outside_the_calling_window(client, voice_app):
    v = voice_app
    # A window in the NEXT hour can never contain "now".
    next_hour = (datetime.now().hour + 1) % 24
    profile = client.post(
        "/voice/campaign/profiles",
        json={
            "name": "Night agent",
            "profile": {"calling_schedule": f"{next_hour:02d}:00-{next_hour:02d}:30"},
        },
    ).json()["profile"]
    contact = client.post(
        "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000004444"}
    ).json()["contact"]

    response = client.post(
        "/voice/campaign/calls",
        json={"profile_id": profile["id"], "customer_id": contact["id"]},
    )
    assert response.status_code == 409
    assert "calling window" in response.json()["detail"]


def test_campaign_call_uses_the_existing_telephony_provider(client, voice_app):
    v = voice_app
    placed = {}

    class FakeProvider:
        name = "fake"

        def capabilities(self):
            return {"outbound_calls": True}

        def place_call(self, *, to, business_id, call_id, objective):
            placed.update(
                {
                    "to": to,
                    "business_id": business_id,
                    "call_id": call_id,
                    "objective": objective,
                }
            )
            return {
                "success": True,
                "configured": True,
                "provider": "fake",
                "provider_call_id": "CAfake",
            }

        def answer_call(self, **kwargs):
            return {"success": False, "reason": "not used"}

        def hangup(self, **kwargs):
            return {"success": True}

        def transfer(self, **kwargs):
            return {"success": True}

    original = v.telephony_provider
    v.telephony_provider = FakeProvider()
    try:
        profile = client.post(
            "/voice/campaign/profiles",
            json={"name": "Caller", "profile": {"calling_schedule": "00:00-23:59"}},
        ).json()["profile"]
        contact = client.post(
            "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000005555"}
        ).json()["contact"]

        response = client.post(
            "/voice/campaign/calls",
            json={"profile_id": profile["id"], "customer_id": contact["id"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider_accepted"] is True
        assert body["provider_call_id"] == "CAfake"
        assert placed["to"]
        assert placed["call_id"] == body["call_id"]
    finally:
        v.telephony_provider = original


def test_campaign_call_is_bound_to_the_published_snapshot(client, voice_app):
    v = voice_app

    class FakeProvider:
        name = "fake"

        def capabilities(self):
            return {"outbound_calls": True}

        def place_call(self, **kwargs):
            return {
                "success": True,
                "configured": True,
                "provider": "fake",
                "provider_call_id": "CAfake",
            }

        def answer_call(self, **kwargs):
            return {"success": False, "reason": "not used"}

        def hangup(self, **kwargs):
            return {"success": True}

        def transfer(self, **kwargs):
            return {"success": True}

    original = v.telephony_provider
    v.telephony_provider = FakeProvider()
    try:
        profile = client.post(
            "/voice/campaign/profiles",
            json={
                "name": "Snapshot agent",
                "profile": {
                    "calling_schedule": "00:00-23:59",
                    "representative_name": "Snapshot Name",
                },
            },
        ).json()["profile"]
        contact = client.post(
            "/voice/campaign/contacts", json={"name": "Ravi", "phone": "+919000006666"}
        ).json()["contact"]
        response = client.post(
            "/voice/campaign/calls",
            json={"profile_id": profile["id"], "customer_id": contact["id"]},
        )
        call_id = response.json()["call_id"]

        # The live call keeps the snapshot it started with, even after a publish.
        client.put(
            f"/voice/campaign/profiles/{profile['id']}",
            json={"profile": {"representative_name": "New Name"}, "publish": True},
        )
        with v.SessionLocal() as db:
            call = db.get(v.Call, call_id)
            assert call.agent_profile_snapshot["representative_name"] == "Snapshot Name"
            assert call.agent_profile_id == profile["id"]
    finally:
        v.telephony_provider = original


# ---------------------------------------------------------------------------
# Webhook routing â€” the original agent must keep working
# ---------------------------------------------------------------------------

def test_calls_without_a_campaign_profile_use_the_booking_agent(voice_app):
    v = voice_app
    assert v._campaign_profile_for_call(call_sid="CAunknown") is None
    assert v._campaign_profile_for_call(call_id="call_nope") is None


def test_campaign_calls_resolve_their_profile(voice_app):
    v = voice_app
    profile = v.CampaignProfile(representative_name="Resolved Name")
    with v.SessionLocal() as db:
        call = v.Call(
            id="call_campaign_1",
            business_id="user_alice",
            customer_id="cus_1",
            state=v.CallState.CREATED.value,
            trace_id="trace",
            conversation_id="conv",
            agent_profile_id="camp_1",
            agent_profile_snapshot=profile.model_dump(),
        )
        db.add(call)
        db.commit()

    resolved = v._campaign_profile_for_call(call_id="call_campaign_1")
    assert resolved is not None
    assert resolved.representative_name == "Resolved Name"
    assert v._campaign_profile_for_session("call:call_campaign_1") is not None


# ---------------------------------------------------------------------------
# Webhook routing â€” the greeting and the turns come from the saved profile
# ---------------------------------------------------------------------------

def test_outbound_answer_speaks_the_saved_campaign_greeting(voice_app, monkeypatch):
    v = voice_app
    profile = v.CampaignProfile(
        representative_name="Asha Rao",
        location="Machilipatnam",
        greeting="Hello, I am [representative_name] calling from [location].",
        voice="anushka",
    )
    with v.SessionLocal() as db:
        call = v.Call(
            id="call_greeting_1",
            business_id="user_alice",
            customer_id="cus_1",
            state=v.CallState.CREATED.value,
            trace_id="trace",
            conversation_id="conv",
            provider_call_id="CAgreeting",
            agent_profile_id="camp_1",
            agent_profile_snapshot=profile.model_dump(),
        )
        db.add(call)
        db.commit()

    spoken = {}

    async def _fake_speak(text, language, budget=0.0, voice=None):
        spoken["text"] = text
        spoken["language"] = language
        spoken["voice"] = voice
        return {}

    monkeypatch.setattr(v, "_twilio_speak_or_say", _fake_speak)
    monkeypatch.setattr(v, "_schedule_phrase_warmup", lambda: None)

    from fastapi.testclient import TestClient

    with TestClient(v.app) as client:
        response = client.post(
            "/voice/twilio/outbound-answer?call_id=call_greeting_1",
            data={"CallSid": "CAgreeting", "From": "+919000000000"},
        )
    assert response.status_code == 200
    assert "Hello, I am Asha Rao calling from Machilipatnam." in response.text
    assert "Demo Representative" not in response.text
    assert spoken["text"] == "Hello, I am Asha Rao calling from Machilipatnam."
    assert spoken["language"] == profile.language
    assert spoken["voice"] == "anushka"


def test_campaign_turn_uses_the_saved_profile(voice_app, monkeypatch):
    v = voice_app
    profile = v.CampaignProfile(
        representative_name="Turn Name",
        identity_disclosure="This is [representative_name] of [organization_name].",
    )
    with v.SessionLocal() as db:
        call = v.Call(
            id="call_turn_1",
            business_id="user_alice",
            customer_id="cus_1",
            state=v.CallState.CREATED.value,
            trace_id="trace",
            conversation_id="conv",
            provider_call_id="CATurn",
            agent_profile_id="camp_1",
            agent_profile_snapshot=profile.model_dump(),
        )
        db.add(call)
        db.commit()

    async def _fake_speak(text, language, budget=0.0, voice=None):
        return {}

    monkeypatch.setattr(v, "_twilio_speak_or_say", _fake_speak)
    monkeypatch.setattr(v, "_schedule_phrase_warmup", lambda: None)
    monkeypatch.setattr(v, "_twilio_signature_valid", lambda request, form: True)

    from fastapi.testclient import TestClient

    with TestClient(v.app) as client:
        response = client.post(
            "/voice/twilio/gather",
            data={"SpeechResult": "hello", "CallSid": "CATurn", "From": "+919000000000"},
        )
    assert response.status_code == 200
    assert "This is Turn Name of Demo Organization." in response.text


def test_booking_turns_are_unaffected_by_the_campaign_agent(voice_app, monkeypatch):
    """A call without a campaign profile still reaches the booking engine."""
    v = voice_app
    seen = {}

    async def _fake_booking_turn(session_key, speech, call_id=None, caller_phone=None):
        seen["session_key"] = session_key
        seen["speech"] = speech
        return v.TurnResult(
            reply="The receptionist speaks.",
            phase="GREETING",
            context=v.booking_engine.get_context(session_key),
        )

    async def _fake_speak(text, language, budget=0.0, voice=None):
        return {}

    monkeypatch.setattr(v.booking_engine, "handle_turn", _fake_booking_turn)
    monkeypatch.setattr(v, "_twilio_speak_or_say", _fake_speak)
    monkeypatch.setattr(v, "_schedule_phrase_warmup", lambda: None)
    monkeypatch.setattr(v, "_twilio_signature_valid", lambda request, form: True)

    from fastapi.testclient import TestClient

    with TestClient(v.app) as client:
        response = client.post(
            "/voice/twilio/gather",
            data={"SpeechResult": "I want a room", "CallSid": "CABooking", "From": "+91"},
        )
    assert response.status_code == 200
    assert seen["speech"] == "I want a room"
    assert "The receptionist speaks." in response.text

