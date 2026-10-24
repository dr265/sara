"""SARA Sales Agent tests — scoring, intent routing, gates and stores.

Nothing here touches the network: discovery/sending are exercised only
through their deterministic guards (qualification math, keyword intent
fallback, approval + emergency-stop gates, SSRF host filter).
"""
from __future__ import annotations

import pytest

from app.services import sales_agent as sales
from app.services.sales_agent import SalesStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = SalesStore(tmp_path / "sales.json")
    monkeypatch.setattr(sales, "_store", st)
    return st


def _lead(**over):
    base = {
        "id": "lead_x", "name": "Hotel Amber", "domain": "amber.example",
        "website": "https://amber.example", "phone": "+91 1234567890",
        "email": "", "industry": "hotel", "region": "Jaipur",
        "source_url": "https://www.openstreetmap.org/node/1",
        "discovered_at": 0.0, "profile": {}, "stage": "new", "score": 0,
        "score_reasons": [], "product_id": "whatsapp-ai-agent",
    }
    base.update(over)
    return base


def test_qualify_scoring_is_transient_and_transparent():
    lead = _lead(profile={"reachable": True, "tech": ["WordPress site"],
                          "emails": ["info@amber.example"], "phones": [],
                          "has_booking_hint": True, "has_schema": False})
    q = sales.qualify_lead(lead, "whatsapp-ai-agent")
    # 20 base +15 website +10 reachable +25 email +10 phone +5 tech +10 booking
    assert q["score"] == 95
    assert q["stage"] == "qualified"
    assert q["contact_email"] == "info@amber.example"
    assert any("+25" in r for r in q["reasons"])


def test_qualify_low_score_stays_new():
    # No website, no reachable flag, no email AND no phone: the only points
    # left are the 20 base ones, below the 40 "researching" bar, so the lead
    # must stay "new". (The phone rule itself is covered by the scoring test
    # above, which counts the fixture's number as +10.)
    q = sales.qualify_lead(_lead(website="", phone="", profile={}),
                           "ai-email-automation")
    assert q["score"] == 20
    assert q["stage"] == "new"


def test_intent_keyword_fallback_when_llm_missing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no provider")
    monkeypatch.setattr("app.core.llm_router.llm_chat_sync", boom)
    products = sales._seed_products()
    out = sales.parse_intent(
        "मेरे WhatsApp AI Agent के लिए Jaipur में hotels के clients ढूँढो",
        products)
    assert out["intent"] == "lead.discover"
    assert out["parser"] == "keyword"
    assert out["params"]["product"] == "whatsapp-ai-agent"

    out2 = sales.parse_intent("आज कितने leads मिले? status बताओ", products)
    assert out2["intent"] == "sales.status"

    out3 = sales.parse_intent("सभी नए outbound messages रोको", products)
    assert out3["intent"] == "automation.stop"


def test_resolve_product_prefers_params_then_context():
    products = sales._seed_products()
    assert sales.resolve_product({"product": "voice"}, products, {}) == \
        "ai-voice-calling"
    assert sales.resolve_product({}, products, {"product": "ai-email-automation"}) == \
        "ai-email-automation"
    assert sales.resolve_product({}, products, {}) is None


def test_conversation_store_caps_messages(store):
    data = store.read()
    conv = store.new_conversation(data, "t")
    store._save(data)
    for i in range(sales._MAX_MESSAGES + 5):
        store.mutate(lambda d: store.add_message(d, conv["id"], "user", f"m{i}"))
    assert len(store.read()["conversations"][conv["id"]]["messages"]) == \
        sales._MAX_MESSAGES


def test_send_blocked_without_approval(store):
    store.mutate(lambda d: d["campaigns"].__setitem__("camp_1", {
        "id": "camp_1", "product_id": "x", "ea_campaign_id": "ea",
        "status": "preview", "recipients": 1, "created_at": 0.0,
        "sent": 0, "failed": 0}))
    with pytest.raises(ValueError):
        import asyncio
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            sales.send_approved_campaign("camp_1"))


def test_emergency_stop_blocks_approved_campaign(store):
    store.mutate(lambda d: (d["campaigns"].__setitem__("camp_2", {
        "id": "camp_2", "product_id": "x", "ea_campaign_id": "ea",
        "status": "approved", "recipients": 1, "created_at": 0.0,
        "sent": 0, "failed": 0}),
        d["settings"].update({"emergency_stop": True})))
    import asyncio
    with pytest.raises(ValueError):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            sales.send_approved_campaign("camp_2"))


def test_public_host_guard():
    assert sales._public_host_or_none("http://127.0.0.1:8000/x") is None
    assert sales._public_host_or_none("ftp://example.com") is None
    assert sales._public_host_or_none("https://user:pw@example.com") is None


def test_transcribe_reports_missing_config_honestly(monkeypatch, store):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    import asyncio
    with pytest.raises(ValueError):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            sales.transcribe_audio(b"riff...."))


def test_status_report_reads_real_records(store):
    store.mutate(lambda d: sales.SalesStore.upsert_lead(
        store, d, _lead()))
    text = sales.status_report()
    assert "Leads: 1 total" in text
    assert "emergency stop: off" in text


def test_integration_health_shape(store):
    health = sales.integration_health()
    for key in ("email", "llm", "whatsapp", "voice", "discovery"):
        assert key in health
        assert isinstance(health[key]["connected"], bool)
    assert health["discovery"]["connected"] is True
