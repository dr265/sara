"""SARA Autonomous AI Sales Agent — the real sales operation runtime.

Architecture (adapted, not copied, from the audited sources):
  * Harvey (MIT)          — discovery-provider adapter pattern, deterministic
                            website profiling signals, outbox double-send
                            guards, paced sending with daily caps.
  * SalesGPT (MIT/Apache) — conversation-stage vocabulary and the idea that a
                            sales controller is a stage machine driven by
                            validated state, plus prompt content for pitches.
  * SARA (this platform)  — the actual execution: llm_router failover chain,
                            the Email Automation agent's real SMTP/Gmail send
                            path, Sarvam STT for voice notes, durable JSON
                            stores in the sara_bots atomic-file style.

Nothing here fabricates work: leads come from real OpenStreetMap/Overpass
queries plus real HTTP fetches of the prospect's own website; emails go out
only through ``email_automation.send_email`` after an explicit approval and
never while the emergency stop is on; every count in a report is read back
from persisted records.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

log = logging.getLogger("sara.sales")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SALES_FILE = DATA_DIR / "sales_agent.json"
ATTACH_DIR = Path(__file__).resolve().parents[2] / "sales_attachments"

TICK_S = 20.0
_MAX_LEADS = 2000
_MAX_CONVERSATIONS = 100
_MAX_MESSAGES = 400
_MAX_AUDIT = 500
_DISCOVERY_CAP = 40
_PROFILE_TIMEOUT = 10.0

LEAD_STAGES = ("new", "researching", "qualified", "ready_for_outreach",
               "contacted", "replied", "interested", "meeting_requested",
               "proposal_sent", "converted", "not_interested", "suppressed")

INTENTS = ("product.list", "product.create_listing", "lead.discover",
           "lead.qualify", "campaign.create", "campaign.preview",
           "campaign.start", "campaign.approve", "automation.start",
           "automation.pause", "automation.resume", "automation.stop",
           "sales.status", "sales.report", "integration.health", "chat")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now() -> float:
    return time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Product registry — verified against the real codebase, never invented
# ─────────────────────────────────────────────────────────────────────────────

def _seed_products() -> Dict[str, Any]:
    def p(pid: str, name: str, desc: str, audience: List[str],
          industries: List[str], caps: List[str], problems: List[str],
          requires: str, status: str, route: str) -> Dict[str, Any]:
        return {
            "id": pid, "name": name, "description": desc,
            "target_audience": audience, "industries": industries,
            "verified_capabilities": caps, "problems_solved": problems,
            "integration_requirements": requires,
            "operational_status": status, "route": route,
            "pricing": "", "demo": "",
        }
    return {
        "ai-automation-agency": p(
            "ai-automation-agency", "AI Automation Agency",
            "Custom business automation: AI agents, workflow integration, "
            "multi-agent systems and reporting built on the SARA platform.",
            ["SMB owners", "Operations managers", "Agencies"],
            ["Any industry with repetitive manual processes"],
            ["Desktop + browser automation (29 approved local actions)",
             "Durable background tasks with approvals",
             "Custom worker bots with per-bot tool grants"],
            ["Manual repetitive work", "No audit trail for automations"],
            "None — runs on this platform", "operational", "/hub/sara-bots"),
        "interview-assistance": p(
            "interview-assistance", "AI Interview Assistance Platform",
            "Adaptive learning engine: gap analysis, roadmap, practice and "
            "contextual interview preparation assistance.",
            ["Students", "Job seekers", "Training providers", "Career coaches"],
            ["Education", "Recruiting", "Training"],
            ["Adaptive profile → gap analysis → roadmap → practice loop",
             "Contextual question assistance where supported"],
            ["Unstructured interview preparation"],
            "LLM key for the adaptive engine", "needs_configuration",
            "/hub/edulearner-ai"),
        "whatsapp-ai-agent": p(
            "whatsapp-ai-agent", "WhatsApp AI Agent",
            "Customer conversations, lead qualification, booking workflows "
            "and follow-ups over the connected WhatsApp Business provider.",
            ["Hotels", "Clinics", "Real-estate offices", "Retail"],
            ["Hospitality", "Healthcare", "Real estate"],
            ["Knowledge-base grounded replies", "Sheets sync for bookings",
             "Business context + persona configuration"],
            ["Missed customer messages", "Manual booking back-and-forth"],
            "WhatsApp provider credentials (Unipile) configured",
            "needs_configuration", "/hub/whatsapp-agent"),
        "ai-voice-calling": p(
            "ai-voice-calling", "AI Voice Calling Agent",
            "AI receptionist: inbound call handling, booking inquiries, "
            "lead qualification and call routing; multilingual STT/TTS.",
            ["Front desks", "Call centers", "Clinics", "Restaurants"],
            ["Hospitality", "Healthcare", "Food service"],
            ["Live voice engine with Sarvam STT/TTS", "Call routing rules"],
            ["Unanswered calls", "After-hours inquiries"],
            "Sarvam/voice credentials configured", "needs_configuration",
            "/hub/voice-agent"),
        "ai-email-automation": p(
            "ai-email-automation", "AI Email Automation",
            "Personalized email workflows: LLM-drafted per-recipient emails, "
            "campaign management, CSV/PDF/Sheets ingestion, attachments.",
            ["Sales teams", "Recruiters", "Marketing"],
            ["Any industry doing outreach"],
            ["Real SMTP/Gmail send path with retries",
             "Per-client personalized generation", "Dry-run previews"],
            ["Generic bulk mail", "Manual personalization"],
            "Sender mailbox connected (SMTP or Gmail)",
            "operational", "/hub/email-automation"),
        "web-research-scraper": p(
            "web-research-scraper", "Web Research & Scraping Agent",
            "Fetches real URLs and extracts structured data with the "
            "configured LLM brain; crawl mode with page caps.",
            ["Research teams", "Lead-gen operators", "Analysts"],
            ["Any industry"],
            ["Structured extraction from live pages", "Bounded crawls"],
            ["Manual copy-paste research"],
            "LLM key configured", "needs_configuration", "/hub/ai-web-scraper"),
        "social-media-automation": p(
            "social-media-automation", "Social Media Automation",
            "Content scheduling and publishing workflows with OAuth-connected "
            "accounts and a job worker.",
            ["Brands", "Creators", "Agencies"],
            ["Retail", "Media", "Hospitality"],
            ["OAuth account connect", "Scheduled job worker"],
            ["Inconsistent publishing"],
            "Social OAuth credentials", "needs_configuration",
            "/hub/social-media"),
        "document-workflows": p(
            "document-workflows", "Document & Report Workflows",
            "Real PDF/Word/Excel generation and processing from natural "
            "language, including charts and styled reports.",
            ["Consultants", "Finance", "Operations"],
            ["Any industry producing documents"],
            ["Real .pdf/.docx/.xlsx generation", "Merge/split/extract"],
            ["Manual document formatting"],
            "None — runs on this platform", "operational", "/hub/sara-bots"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────

class SalesStore:
    """Atomic-JSON durable store (same pattern as the sara_bots stores)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else SALES_FILE
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
        except FileNotFoundError:
            data = {}
        except Exception:  # noqa: BLE001 — corrupt file must not kill the agent
            log.exception("sales store unreadable — starting empty")
            data = {}
        for key in ("leads", "campaigns", "conversations", "tasks",
                    "approvals", "listings"):
            data.setdefault(key, {})
        data.setdefault("products", _seed_products())
        data.setdefault("audit", [])
        data.setdefault("settings", {
            "automation": {"mode": "off", "interval_minutes": 60,
                           "next_tick_at": 0.0},
            "emergency_stop": False,
            "max_sends_per_day": 50,
            "sent_today": {},
        })
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    # -- generic helpers -----------------------------------------------------
    def read(self) -> Dict[str, Any]:
        with self._lock:
            return self._load()

    def mutate(self, fn) -> Any:
        with self._lock:
            data = self._load()
            out = fn(data)
            self._save(data)
            return out

    def audit(self, data: Dict[str, Any], event: str, detail: str = "") -> None:
        data["audit"].append({"ts": _now(), "event": event, "detail": detail[:400]})
        if len(data["audit"]) > _MAX_AUDIT:
            data["audit"] = data["audit"][-_MAX_AUDIT:]

    # -- leads ---------------------------------------------------------------
    def upsert_lead(self, data: Dict[str, Any], lead: Dict[str, Any]) -> Dict[str, Any]:
        key = (lead.get("domain") or lead.get("name", "").lower()).strip()
        for existing in data["leads"].values():
            if key and (existing.get("domain") or "") == key:
                existing.update({k: v for k, v in lead.items() if v not in ("", None)})
                existing["updated_at"] = _now()
                return existing
        if len(data["leads"]) >= _MAX_LEADS:
            raise ValueError("lead limit reached")
        data["leads"][lead["id"]] = lead
        return lead

    # -- conversations -------------------------------------------------------
    def new_conversation(self, data: Dict[str, Any], title: str) -> Dict[str, Any]:
        if len(data["conversations"]) >= _MAX_CONVERSATIONS:
            oldest = sorted(data["conversations"].values(),
                            key=lambda c: c.get("updated_at", 0))
            for stale in oldest[:len(data["conversations"]) - _MAX_CONVERSATIONS + 1]:
                data["conversations"].pop(stale["id"], None)
        conv = {"id": new_id("conv"), "title": title[:80] or "New conversation",
                "created_at": _now(), "updated_at": _now(),
                "messages": [], "context": {}}
        data["conversations"][conv["id"]] = conv
        return conv

    def add_message(self, data: Dict[str, Any], conv_id: str, role: str,
                    text: str, **extra: Any) -> Dict[str, Any]:
        conv = data["conversations"].get(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        msg = {"id": new_id("msg"), "role": role, "text": text[:8000],
               "ts": _now(), **extra}
        conv["messages"].append(msg)
        if len(conv["messages"]) > _MAX_MESSAGES:
            conv["messages"] = conv["messages"][-_MAX_MESSAGES:]
        conv["updated_at"] = _now()
        return msg


_store: Optional[SalesStore] = None
_store_lock = threading.Lock()


def get_sales_store() -> SalesStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SalesStore()
    return _store


# ─────────────────────────────────────────────────────────────────────────────
# Lead discovery — real OpenStreetMap/Overpass queries + real website profiling
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_TAG_RE = re.compile(r"<[^>]+>")

_TECH_FINGERPRINTS = {
    "wordpress": ("wp-content", "WordPress site"),
    "shopify": ("cdn.shopify.com", "Shopify store"),
    "wix": ("wixsite.com", "Wix site"),
    "squarespace": ("squarespace.com", "Squarespace site"),
    "woocommerce": ("woocommerce", "WooCommerce store"),
}


def _public_host_or_none(url: str) -> Optional[str]:
    """SSRF guard: only public http(s) hosts, no credentials in the URL."""
    from app.services.sara_bots.muse import _assert_public_host
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.username:
            return None
        _assert_public_host(parsed.hostname or "")
        return parsed.hostname or None
    except Exception:  # noqa: BLE001 — private/unresolvable host: skip, honest
        return None


async def _geocode_bbox(region: str) -> Optional[List[float]]:
    """One Nominatim lookup (public, keyless, 1 req/s policy respected)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False,
                                     follow_redirects=True) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": region, "format": "json", "limit": 1},
                headers={"User-Agent": "SARA-SalesAgent/1.0 (lead discovery)"})
            resp.raise_for_status()
            rows = resp.json()
        if not rows:
            return None
        return [float(x) for x in rows[0]["boundingbox"]]  # S,N,W,E
    except Exception as exc:  # noqa: BLE001 — discovery failure is data
        log.warning("sales: geocode failed for %r: %s", region, exc)
        return None


async def _overpass_businesses(bbox: List[float], categories: List[str],
                               limit: int) -> List[Dict[str, Any]]:
    """Real Overpass query for named businesses with contact tags."""
    import httpx
    south, north, west, east = bbox
    name_filter = f"'name'{_cat_regex(categories)}"
    query = (
        f"[out:json][timeout:25];("
        f"node[{name_filter}]({south},{west},{north},{east});"
        f"way[{name_filter}]({south},{west},{north},{east});"
        f");out center {limit};"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                headers={"User-Agent": "SARA-SalesAgent/1.0 (lead discovery)"})
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("sales: overpass query failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        out.append({
            "name": name,
            "website": (tags.get("website") or tags.get("contact:website") or "").strip(),
            "phone": (tags.get("phone") or tags.get("contact:phone") or "").strip(),
            "email": (tags.get("email") or tags.get("contact:email") or "").strip(),
            "category": next((k for k in ("tourism", "amenity", "shop",
                                           "office", "craft", "building")
                              if tags.get(k)), ""),
            "category_value": next((tags[k] for k in ("tourism", "amenity",
                                     "shop", "office", "craft", "building")
                                    if tags.get(k)), ""),
            "lat": lat, "lon": lon,
            "source_url": f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
        })
        if len(out) >= limit:
            break
    return out


def _cat_regex(categories: List[str]) -> str:
    if not categories:
        return ""
    inner = "|".join(re.escape(c) for c in categories)
    return f'~"^({inner})$"i'


async def profile_website(url: str) -> Dict[str, Any]:
    """Deterministic profiling of a prospect's own public website.

    Ported concept from Harvey's profile.py: zero-LLM signal extraction
    (tech fingerprints, contact points, schema/booking gaps). Every signal
    is a fact read from the fetched page, never an inference.
    """
    import httpx
    signals: Dict[str, Any] = {"url": url, "reachable": False, "tech": [],
                               "emails": [], "phones": [], "has_schema": False,
                               "has_booking_hint": False, "title": ""}
    host = _public_host_or_none(url)
    if not host:
        signals["error"] = "not a public http(s) host"
        return signals
    try:
        async with httpx.AsyncClient(timeout=_PROFILE_TIMEOUT, trust_env=False,
                                     follow_redirects=True, max_redirects=4) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; SARA-SalesAgent/1.0)"})
            resp.raise_for_status()
            html = resp.text[:400_000]
    except Exception as exc:  # noqa: BLE001 — unreachable site is a real fact
        signals["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return signals
    signals["reachable"] = True
    low = html.lower()
    for key, (needle, label) in _TECH_FINGERPRINTS.items():
        if needle in low:
            signals["tech"].append(label)
    signals["has_schema"] = "schema.org" in low
    signals["has_booking_hint"] = any(
        k in low for k in ("book now", "bookings", "calendly", "opentable",
                           "reserve", "appointment"))
    text = _TAG_RE.sub(" ", html)
    signals["emails"] = sorted(set(
        e for e in _EMAIL_RE.findall(text)
        if not e.lower().endswith((".png", ".jpg", ".gif", ".webp"))))[:5]
    signals["phones"] = sorted(set(_PHONE_RE.findall(text)))[:3]
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    signals["title"] = (m.group(1).strip() if m else "")[:160]
    return signals


def qualify_lead(lead: Dict[str, Any], product_id: str) -> Dict[str, Any]:
    """Transparent deterministic scoring — every point has a written reason."""
    reasons: List[str] = []
    score = 20
    reasons.append("base: real business record from OpenStreetMap")
    prof = lead.get("profile") or {}
    if lead.get("website"):
        score += 15
        reasons.append("+15 has a website")
    if prof.get("reachable"):
        score += 10
        reasons.append("+10 website reachable")
    contact_email = lead.get("email") or (prof.get("emails") or [None])[0]
    if contact_email:
        score += 25
        reasons.append("+25 public contact email found")
    if lead.get("phone") or prof.get("phones"):
        score += 10
        reasons.append("+10 public phone number found")
    if prof.get("tech"):
        score += 5
        reasons.append(f"+5 tech stack visible ({', '.join(prof['tech'][:2])})")
    if product_id in ("whatsapp-ai-agent", "ai-voice-calling") \
            and prof.get("has_booking_hint"):
        score += 10
        reasons.append("+10 booking flow present (automation upsell signal)")
    if product_id in ("ai-email-automation", "ai-automation-agency") \
            and prof.get("reachable") and not prof.get("has_schema"):
        score += 5
        reasons.append("+5 no structured-data schema (SEO/automation gap)")
    score = min(score, 100)
    stage = "qualified" if score >= 60 else ("researching" if score >= 40 else "new")
    return {"score": score, "reasons": reasons, "stage": stage,
            "contact_email": contact_email or ""}


# ─────────────────────────────────────────────────────────────────────────────
# Intent engine — LLM interpretation with a deterministic keyword fallback
# ─────────────────────────────────────────────────────────────────────────────

_INTENT_SYSTEM = """You are the command parser of SARA Sales Agent.
Convert the user's request (English, Hindi or Hinglish) into ONE JSON object:
{{"intent": "<one of {intents}>",
  "params": {{"product": "<product name or id or ''>",
             "industry": "<industry/category keyword or ''>",
             "region": "<city/region or ''>",
             "limit": <int or 0>,
             "channel": "email|whatsapp|''",
             "text": "<free text for listings/reports or ''>"}}}}
Rules: pick the single best intent; never invent params the user did not imply;
lead discovery requests map to lead.discover; status/questions about counts map
to sales.status; anything conversational maps to chat.
Respond ONLY with the JSON object."""

_KEYWORD_INTENTS: List[tuple] = [
    (("emergency", "stop all", "rok do", "रोक"), "automation.stop"),
    (("status", "report", "kitne", "कितने", "आज"), "sales.status"),
    (("integration", "health", "connected"), "integration.health"),
    (("listing", "product page", "listing banao", "लिस्टिंग"), "product.create_listing"),
    (("product", "catalog", "products"), "product.list"),
    (("campaign", "email bhej", "emails भेज", "preview"), "campaign.create"),
    (("approve", "approval", "स्वीकृत"), "campaign.approve"),
    (("continuous", "automation start", "chalu karo", "चालू"), "automation.start"),
    (("pause", "resume"), "automation.pause"),
    (("lead", "client", "customer", "ढूँढ", "find", "khojo"), "lead.discover"),
    (("qualify", "score"), "lead.qualify"),
]


def parse_intent(text: str, products: Dict[str, Any]) -> Dict[str, Any]:
    """LLM-first parsing with a deterministic multilingual keyword fallback."""
    names = ", ".join(f"{v['name']} ({k})" for k, v in products.items())
    prompt = _INTENT_SYSTEM.format(intents=", ".join(INTENTS)) + \
        f"\nKnown products: {names}\nUser request: {text}"
    raw = ""
    try:
        from app.core.llm_router import llm_chat_sync
        raw = llm_chat_sync(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": text}],
            max_tokens=300, temperature=0.1, timeout=60.0, strict=True)
    except Exception as exc:  # noqa: BLE001 — no LLM: keyword router still works
        log.info("sales: intent LLM unavailable (%s) — keyword fallback", exc)
    parsed = _extract_json(raw)
    if parsed and parsed.get("intent") in INTENTS:
        params = parsed.get("params") or {}
        return {"intent": parsed["intent"],
                "params": {k: params.get(k, "") for k in
                           ("product", "industry", "region", "limit",
                            "channel", "text")},
                "parser": "llm"}
    low = text.lower()
    for keys, intent in _KEYWORD_INTENTS:
        if any(k in low for k in keys):
            return {"intent": intent, "params": _keyword_params(text, products),
                    "parser": "keyword"}
    return {"intent": "chat", "params": {}, "parser": "keyword"}


def _keyword_params(text: str, products: Dict[str, Any]) -> Dict[str, Any]:
    low = text.lower()
    product = ""
    for pid, prod in products.items():
        if prod["name"].lower() in low or pid in low:
            product = pid
            break
    limit = 0
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        limit = int(m.group(1))
    return {"product": product, "industry": "", "region": "",
            "limit": limit, "channel": "", "text": text}


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?", "", raw)
    raw = re.sub(r"```$", "", raw.strip()).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def resolve_product(params: Dict[str, Any], products: Dict[str, Any],
                    context: Dict[str, Any]) -> Optional[str]:
    wanted = (params.get("product") or "").strip().lower()
    if wanted:
        if wanted in products:
            return wanted
        for pid, prod in products.items():
            if wanted in prod["name"].lower() or pid in wanted:
                return pid
    ctx = context.get("product")
    if ctx in products:
        return ctx
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Task execution — every step writes real state
# ─────────────────────────────────────────────────────────────────────────────

def create_task(data: Dict[str, Any], kind: str, title: str,
                params: Dict[str, Any], conv_id: str = "") -> Dict[str, Any]:
    plans = {
        "discover": ["Resolve the region", "Query OpenStreetMap",
                     "Profile prospect websites", "Score and save leads"],
        "qualify": ["Re-score stored leads", "Update pipeline stages"],
        "listing": ["Read verified capabilities", "Draft the listing",
                    "Save the draft"],
        "campaign": ["Create the email campaign", "Stage recipients",
                     "Await approval", "Send through the real sender"],
        "report": ["Read persisted records", "Compose the report"],
    }
    task = {
        "id": new_id("task"), "kind": kind, "title": title[:160],
        "params": params, "conversation_id": conv_id,
        "status": "queued",
        "plan": [{"title": t, "status": "pending", "detail": ""}
                 for t in plans.get(kind, ["Execute"])],
        "result": "", "error": "", "created_at": _now(),
        "updated_at": _now(), "refs": {},
    }
    data["tasks"][task["id"]] = task
    return task


def _set_step(task: Dict[str, Any], idx: int, status: str, detail: str = "") -> None:
    if 0 <= idx < len(task["plan"]):
        task["plan"][idx]["status"] = status
        if detail:
            task["plan"][idx]["detail"] = detail[:300]
    task["updated_at"] = _now()


async def run_task(task_id: str) -> Dict[str, Any]:
    store = get_sales_store()
    data = store.read()
    task = data["tasks"].get(task_id)
    if task is None:
        raise KeyError(task_id)
    if task["status"] != "queued":
        return task
    task["status"] = "running"
    store.mutate(lambda d: d["tasks"].__setitem__(task_id, task))
    try:
        if task["kind"] == "discover":
            result = await _task_discover(task)
        elif task["kind"] == "qualify":
            result = await _task_qualify(task)
        elif task["kind"] == "listing":
            result = await _task_listing(task)
        elif task["kind"] == "campaign":
            result = await _task_campaign_create(task)
        elif task["kind"] == "report":
            result = await _task_report(task)
        else:
            result = f"Unsupported task kind '{task['kind']}'"
        task["status"] = "completed"
        task["result"] = result[:4000]
    except Exception as exc:  # noqa: BLE001 — failures are recorded, not hidden
        log.exception("sales: task %s failed", task_id)
        task["status"] = "failed"
        task["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    task["updated_at"] = _now()
    store.mutate(lambda d: (d["tasks"].__setitem__(task_id, task),
                            _audit_task(d, task)))
    return task


def _audit_task(data: Dict[str, Any], task: Dict[str, Any]) -> None:
    get_sales_store().audit(
        data, f"task.{task['status']}",
        f"{task['id']} {task['kind']}: {task['result'] or task['error']}")


async def _task_discover(task: Dict[str, Any]) -> str:
    params = task["params"]
    region = (params.get("region") or "").strip()
    if not region:
        raise ValueError("a region is required for lead discovery "
                         "(e.g. 'Jaipur' or 'Bengaluru')")
    categories = [c for c in re.split(r"[,;]", params.get("industry") or "")
                  if c.strip()] or ["hotel", "restaurant", "company"]
    limit = min(int(params.get("limit") or 15), _DISCOVERY_CAP)
    product_id = params.get("product") or ""
    store = get_sales_store()

    _set_step(task, 0, "running")
    bbox = await _geocode_bbox(region)
    if bbox is None:
        _set_step(task, 0, "failed", f"region '{region}' not resolvable")
        raise ValueError(f"region '{region}' could not be resolved — "
                         "try a city or district name")
    _set_step(task, 0, "succeeded", f"bbox {bbox}")

    _set_step(task, 1, "running")
    found = await _overpass_businesses(bbox, categories, limit)
    if not found:
        _set_step(task, 1, "failed", "no businesses matched in OpenStreetMap")
        return (f"No businesses matched '{', '.join(categories)}' around "
                f"{region} in OpenStreetMap. Try different category keywords.")
    _set_step(task, 1, "succeeded", f"{len(found)} candidates")

    _set_step(task, 2, "running")
    saved = 0
    for item in found:
        domain = _public_host_or_none(item["website"]) if item["website"] else None
        lead = {
            "id": new_id("lead"), "name": item["name"],
            "domain": domain or "", "website": item["website"],
            "phone": item["phone"], "email": item["email"],
            "industry": item.get("category_value") or "",
            "region": region, "source_url": item["source_url"],
            "discovered_at": _now(), "profile": {},
            "stage": "new", "score": 0, "score_reasons": [],
            "product_id": product_id,
        }
        if item["website"] and domain:
            lead["profile"] = await profile_website(item["website"])
            q = qualify_lead(lead, product_id)
            lead.update({"score": q["score"], "score_reasons": q["reasons"],
                         "stage": q["stage"],
                         "email": lead["email"] or q["contact_email"]})
        def _up(d: Dict[str, Any], lead=lead) -> None:
            get_sales_store().upsert_lead(d, lead)
        store.mutate(_up)
        saved += 1
    _set_step(task, 2, "succeeded", f"{saved} profiles")

    _set_step(task, 3, "succeeded")
    qualified = sum(1 for l in store.read()["leads"].values()
                    if l.get("region") == region and l.get("stage") == "qualified")
    return (f"Discovered {saved} real business(es) around {region} from "
            f"OpenStreetMap; {qualified} currently score as qualified. "
            f"Sources recorded per lead.")


async def _task_qualify(task: Dict[str, Any]) -> str:
    store = get_sales_store()
    product_id = task["params"].get("product") or ""
    _set_step(task, 0, "running")
    updated = 0
    def _run(d: Dict[str, Any]) -> None:
        nonlocal updated
        for lead in d["leads"].values():
            if product_id and lead.get("product_id") not in ("", product_id):
                continue
            q = qualify_lead(lead, lead.get("product_id") or product_id)
            lead.update({"score": q["score"], "score_reasons": q["reasons"],
                         "stage": q["stage"], "updated_at": _now()})
            updated += 1
    store.mutate(_run)
    _set_step(task, 0, "succeeded", f"{updated} leads re-scored")
    _set_step(task, 1, "succeeded")
    return f"Re-qualified {updated} lead(s) with transparent scoring."


async def _task_listing(task: Dict[str, Any]) -> str:
    store = get_sales_store()
    products = store.read()["products"]
    pid = task["params"].get("product") or ""
    prod = products.get(pid)
    if prod is None:
        raise ValueError("unknown product for listing")
    _set_step(task, 0, "succeeded", prod["name"])
    _set_step(task, 1, "running")
    caps = "\n".join(f"- {c}" for c in prod["verified_capabilities"])
    prompt = (
        "Write an honest product listing (title, one-paragraph description, "
        "5 business benefits, ideal customer segments, call to action) using "
        "ONLY these verified capabilities — never promise more:\n"
        f"{caps}\nProduct: {prod['name']}. {prod['description']}\n"
        f"Extra direction from the owner: {task['params'].get('text') or ''}\n"
        "Return plain markdown, no preamble.")
    try:
        from app.core.llm_router import llm_chat
        body = await llm_chat(
            [{"role": "user", "content": prompt}],
            max_tokens=900, temperature=0.5, strict=True)
    except Exception:  # noqa: BLE001 — deterministic template, clearly labeled
        body = (f"# {prod['name']}\n\n{prod['description']}\n\n"
                "**Verified capabilities**\n" + caps +
                "\n\n_Ideal for_: " + ", ".join(prod["target_audience"]) +
                "\n\n_(template draft — LLM unavailable)_")
    _set_step(task, 1, "succeeded")
    listing = {"id": new_id("list"), "product_id": pid, "body": body,
               "status": "draft", "created_at": _now()}
    store.mutate(lambda d: (d["listings"].__setitem__(listing["id"], listing),
                            d["tasks"][task["id"]]["refs"].__setitem__(
                                "listing_id", listing["id"])))
    _set_step(task, 2, "succeeded", listing["id"])
    return f"Listing draft saved ({listing['id']}). Preview:\n\n{body}"


async def _task_campaign_create(task: Dict[str, Any]) -> str:
    from app.agents import email_automation as ea
    store = get_sales_store()
    params = task["params"]
    pid = params.get("product") or ""
    data = store.read()
    prod = data["products"].get(pid)
    if prod is None:
        raise ValueError("unknown product for campaign")
    leads = [l for l in data["leads"].values()
             if l.get("stage") in ("qualified", "ready_for_outreach")
             and l.get("email")
             and (not pid or l.get("product_id") in ("", pid))]
    if not leads:
        raise ValueError("no qualified leads with a contact email yet — "
                         "run lead discovery first")
    _set_step(task, 0, "running")
    db = ea._db()
    try:
        camp = ea.Campaign(
            name=f"SARA Sales — {prod['name']}",
            status="draft", dry_run=True,
            context=(f"Product: {prod['name']}. {prod['description']}\n"
                     f"Verified capabilities: "
                     f"{'; '.join(prod['verified_capabilities'])}"),
            instructions=("Personalize per recipient using their business name "
                          "and visible signals. Honest claims only."),
            objective=f"Introduce {prod['name']} and request a short call",
            tone="professional",
            max_emails=min(len(leads), 100),
        )
        db.add(camp)
        db.commit()
        for lead in leads:
            db.add(ea.Client(campaign_id=camp.id, name=lead["name"],
                             email=lead["email"],
                             company=lead["name"],
                             fields_json=json.dumps(
                                 {"website": lead.get("website", ""),
                                  "industry": lead.get("industry", ""),
                                  "region": lead.get("region", "")}),
                             source="sales-agent", status="Pending"))
        db.commit()
        ea_campaign_id = camp.id
    finally:
        db.close()
    _set_step(task, 0, "succeeded", ea_campaign_id)
    _set_step(task, 1, "succeeded", f"{len(leads)} recipients staged")
    meta = {"id": new_id("camp"), "product_id": pid,
            "ea_campaign_id": ea_campaign_id, "status": "preview",
            "recipients": len(leads), "approval_id": "",
            "created_at": _now(), "sent": 0, "failed": 0}
    store.mutate(lambda d: (d["campaigns"].__setitem__(meta["id"], meta),
                            d["tasks"][task["id"]]["refs"].__setitem__(
                                "campaign_id", meta["id"])))
    _set_step(task, 2, "succeeded", "awaiting approval")
    _set_step(task, 3, "pending")
    return (f"Campaign staged in dry-run: {len(leads)} qualified recipient(s) "
            f"for {prod['name']} (email campaign {ea_campaign_id}). "
            "Approve it to enable real sending.")


async def _task_report(task: Dict[str, Any]) -> str:
    _set_step(task, 0, "running")
    text = status_report()
    _set_step(task, 0, "succeeded")
    _set_step(task, 1, "succeeded")
    return text


def status_report() -> str:
    data = get_sales_store().read()
    leads = list(data["leads"].values())
    by_stage: Dict[str, int] = {}
    for l in leads:
        by_stage[l.get("stage", "new")] = by_stage.get(l.get("stage", "new"), 0) + 1
    camps = list(data["campaigns"].values())
    sent = sum(int(c.get("sent") or 0) for c in camps)
    tasks = list(data["tasks"].values())
    running = sum(1 for t in tasks if t["status"] == "running")
    queued = sum(1 for t in tasks if t["status"] == "queued")
    mode = data["settings"]["automation"]["mode"]
    stop = data["settings"]["emergency_stop"]
    lines = [
        f"Leads: {len(leads)} total — " +
        (", ".join(f"{k}: {v}" for k, v in sorted(by_stage.items())) or "none yet"),
        f"Campaigns: {len(camps)} ({sum(1 for c in camps if c['status'] == 'running')} running) · emails actually sent: {sent}",
        f"Tasks: {running} running, {queued} queued, "
        f"{sum(1 for t in tasks if t['status'] == 'failed')} failed",
        f"Automation mode: {mode} · emergency stop: {'ON' if stop else 'off'}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Campaign sending — the real Email Automation send path, gated and paced
# ─────────────────────────────────────────────────────────────────────────────

async def send_approved_campaign(meta_id: str) -> Dict[str, Any]:
    from app.agents import email_automation as ea
    store = get_sales_store()
    data = store.read()
    meta = data["campaigns"].get(meta_id)
    if meta is None:
        raise KeyError(meta_id)
    if meta["status"] != "approved":
        raise ValueError(f"campaign is '{meta['status']}' — approve it first")
    if store.read()["settings"]["emergency_stop"]:
        raise ValueError("emergency stop is ON — no outbound sends allowed")
    db = ea._db()
    sent = failed = 0
    try:
        camp = db.get(ea.Campaign, meta["ea_campaign_id"])
        if camp is None:
            raise KeyError(meta["ea_campaign_id"])
        clients = [c for c in db.query(ea.Client)
                   .filter(ea.Client.campaign_id == camp.id,
                           ea.Client.status == "Pending")
                   .limit(camp.max_emails or 100).all()]
        for client in clients:
            settings = store.read()["settings"]
            if settings["emergency_stop"]:
                break
            today = time.strftime("%Y-%m-%d")
            sent_today = settings["sent_today"].get(today, 0)
            if sent_today >= int(settings.get("max_sends_per_day", 50)):
                log.warning("sales: daily send cap reached (%s)", sent_today)
                break
            try:
                try:
                    subject, body = await ea.generate_email(camp, client)
                except Exception:  # noqa: BLE001 — honest template fallback
                    subject = (camp.subject or
                               f"Quick idea for {client.company or client.name}")
                    body = (f"Hello {client.name},\n\n{camp.objective}\n\n"
                            f"{camp.context}\n\n— SARA Sales Agent (template "
                            "fallback: personalization LLM unavailable)")
                client.last_email = body
                client.status = "Sending"
                client.attempts += 1
                db.commit()
                message_id = await ea.send_email(camp, client.email, subject, body)
                client.status = "Sent"
                client.message_id = message_id or ""
                client.sent_at = ea.datetime.now(ea.timezone.utc)
                sent += 1
                store.mutate(lambda d: _bump_sent(d, today))
            except Exception as exc:  # noqa: BLE001 — per-recipient honesty
                client.status = "Failed"
                client.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                failed += 1
            db.commit()
            await asyncio.sleep(max(1.0, float(camp.delay_seconds or 5)))
        camp.sent_count = (camp.sent_count or 0) + sent
        camp.failed_count = (camp.failed_count or 0) + failed
        camp.status = "completed" if not failed else "running"
        db.commit()
    finally:
        db.close()
    store.mutate(lambda d: (d["campaigns"][meta_id].update(
        {"sent": d["campaigns"][meta_id].get("sent", 0) + sent,
         "failed": d["campaigns"][meta_id].get("failed", 0) + failed,
         "status": "running" if failed else "completed"}),
        get_sales_store().audit(d, "campaign.send",
                                f"{meta_id}: sent={sent} failed={failed}")))
    return {"sent": sent, "failed": failed, "campaign_id": meta_id}


def _bump_sent(data: Dict[str, Any], today: str) -> None:
    counts = data["settings"].setdefault("sent_today", {})
    counts[today] = int(counts.get(today, 0)) + 1


# ─────────────────────────────────────────────────────────────────────────────
# Integrations — real configuration/status checks, never assumed
# ─────────────────────────────────────────────────────────────────────────────

def integration_health() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        from app.agents import email_automation as ea
        health = ea.health()
        sender = ea.sender_status()
        out["email"] = {"connected": bool(sender.get("configured")),
                        "detail": sender.get("provider") or health.get("status", ""),
                        "provider": sender.get("provider", "")}
    except Exception as exc:  # noqa: BLE001
        out["email"] = {"connected": False, "detail": f"{type(exc).__name__}"}
    try:
        from app.core.llm_router import health_snapshot
        snap = health_snapshot()
        out["llm"] = {"connected": bool(snap.get("any_available")),
                      "detail": snap.get("active") or "no provider available"}
    except Exception as exc:  # noqa: BLE001
        out["llm"] = {"connected": False, "detail": f"{type(exc).__name__}"}
    wa_key = bool(os.getenv("UNIPILE_API_KEY") or os.getenv("WHATSAPP_API_KEY"))
    out["whatsapp"] = {"connected": wa_key,
                       "detail": ("provider credentials present" if wa_key else
                                  "no WhatsApp provider credentials configured")}
    out["voice"] = {"connected": bool(os.getenv("SARVAM_API_KEY")),
                    "detail": ("Sarvam STT configured" if os.getenv("SARVAM_API_KEY")
                               else "SARVAM_API_KEY not set — microphone input "
                                    "will report 'not configured'")}
    out["discovery"] = {"connected": True,
                        "detail": "OpenStreetMap Nominatim + Overpass (free, keyless)"}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Continuous worker
# ─────────────────────────────────────────────────────────────────────────────

class SalesWorker:
    """Background loop: runs queued tasks and, in continuous mode, keeps the
    pipeline moving (qualify new leads on schedule). Outreach never starts by
    itself — campaigns require explicit approval."""

    def __init__(self, store: Optional[SalesStore] = None):
        self.store = store or get_sales_store()
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        try:
            while True:
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 — a bad tick must not kill it
                    log.exception("sales worker tick failed")
                await asyncio.sleep(TICK_S)
        finally:
            self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def tick(self) -> None:
        data = self.store.read()
        for task in list(data["tasks"].values()):
            if task["status"] == "queued":
                await run_task(task["id"])
        auto = self.store.read()["settings"]["automation"]
        if auto.get("mode") == "continuous" and \
                _now() >= float(auto.get("next_tick_at") or 0):
            self.store.mutate(lambda d: d["settings"]["automation"].update(
                {"next_tick_at": _now() +
                    max(5, int(auto.get("interval_minutes") or 60)) * 60}))
            task = self.store.mutate(
                lambda d: create_task(d, "qualify", "Continuous qualification tick",
                                      {"product": ""}))
            await run_task(task["id"])

    def status(self) -> Dict[str, Any]:
        data = self.store.read()
        tasks = list(data["tasks"].values())
        return {
            "running": self._running,
            "mode": data["settings"]["automation"]["mode"],
            "emergency_stop": data["settings"]["emergency_stop"],
            "queued": sum(1 for t in tasks if t["status"] == "queued"),
            "active": sum(1 for t in tasks if t["status"] == "running"),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
        }


_worker: Optional[SalesWorker] = None
_worker_lock = threading.Lock()


def get_sales_worker() -> SalesWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = SalesWorker()
    return _worker


# ─────────────────────────────────────────────────────────────────────────────
# Voice notes — real Sarvam STT when configured, honest error otherwise
# ─────────────────────────────────────────────────────────────────────────────

async def transcribe_audio(audio: bytes, mime: str = "") -> Dict[str, Any]:
    if not audio:
        raise ValueError("empty audio payload")
    if len(audio) > 15 * 1024 * 1024:
        raise ValueError("audio exceeds 15 MB")
    if not os.getenv("SARVAM_API_KEY"):
        raise ValueError("voice transcription is not configured — set "
                         "SARVAM_API_KEY in backend/.env (the microphone UI "
                         "reports this honestly instead of guessing)")
    from app.services.sarvam_client import SarvamClient
    client = SarvamClient.from_env()
    transcript = await client.transcribe(audio)
    return {"text": transcript.text,
            "language_code": transcript.language_code or ""}
