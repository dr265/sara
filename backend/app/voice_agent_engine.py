"""
SARA Voice Agent
Single-file autonomous voice-agent foundation.

This file provides real persistence and orchestration boundaries.
External providers must be configured through concrete provider adapters.

Environment variables:

    VOICE_AGENT_DATABASE_URL=sqlite:///./voiceagent.db
    VOICE_AGENT_DEV_MODE=true

The default providers are safe local adapters. They persist operations
and return explicit "not configured" results instead of claiming success.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import time
import io
import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import openpyxl
except ImportError:
    openpyxl = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Keep the engine's own database next to the other backend databases
# (backend/voiceagent.db) regardless of the launch directory.
_DB_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv(
    "VOICE_AGENT_DATABASE_URL",
    f"sqlite:///{_DB_DIR / 'voiceagent.db'}",
)

DEV_MODE = os.getenv("VOICE_AGENT_DEV_MODE", "true").lower() == "true"

# Voice-agent brain + speech stack. Every value comes from backend/.env via the
# shared settings object — nothing is hardcoded except the key NAMES.
from app.api.deps import require_api_key  # noqa: E402
from app.core.config import settings as _settings  # noqa: E402
from app.core.llm_router import llm_chat as _failover_chat  # noqa: E402
from app.services.voice_campaign import (  # noqa: E402
    CAMPAIGN_AGENT_KIND,
    CampaignConversationEngine,
    CampaignProfile,
    DEFAULT_CAMPAIGN_PROFILE,
    schedule_allows,
)

LLM_API_KEY = _settings.LLM_API_KEY
LLM_BASE_URL = _settings.LLM_BASE_URL
LLM_MODEL = _settings.LLM_MODEL
ELEVENLABS_API_KEY = _settings.ELEVENLABS_API_KEY
ELEVENLABS_VOICE_ID = _settings.ELEVENLABS_VOICE_ID
VOICE_TTS_PROVIDER = (_settings.VOICE_TTS_PROVIDER or "elevenlabs").lower()
VOICE_STT_PROVIDER = (_settings.VOICE_STT_PROVIDER or "sarvam").lower()
SARVAM_API_KEY = _settings.SARVAM_API_KEY
SARVAM_STT_MODEL = _settings.SARVAM_STT_MODEL
SARVAM_TTS_MODEL = _settings.SARVAM_TTS_MODEL
SARVAM_TTS_SPEAKER = _settings.SARVAM_TTS_SPEAKER
SARVAM_TTS_LANGUAGE = _settings.SARVAM_TTS_LANGUAGE
# Twilio's public callback tunnel. TWILIO_WEBHOOK_BASE_URL is the voice-specific
# override; PUBLIC_BASE_URL stays the fallback so existing deployments are
# unaffected. This single value drives the wss:// Media Streams URL and every
# TwiML action URL, so a stale value is what silently breaks inbound calls.
PUBLIC_BASE_URL = (
    (_settings.TWILIO_WEBHOOK_BASE_URL or _settings.PUBLIC_BASE_URL) or ""
).rstrip("/")
TWILIO_ACCOUNT_SID = _settings.TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN = _settings.TWILIO_AUTH_TOKEN
TWILIO_PHONE_NUMBER = _settings.TWILIO_PHONE_NUMBER

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

app = FastAPI(
    title="SARA Voice Agent",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def normalize_phone(phone: str, default_region: str | None = None) -> str:
    """Normalise any user-entered number to E.164.

    `default_region` used to be hardcoded to "US", which silently mangled
    Indian numbers: "9389748900" parsed as a valid US area code (938-974-8900)
    and became "+19389748900" — a real US number that Twilio happily accepted
    at the API layer and then never connected to the owner. The region now
    comes from SARA_PHONE_REGION (default "IN"), and a bare national number
    is validated before any fallback is attempted.
    """
    phone = (phone or "").strip()

    if not phone:
        return ""

    if default_region is None:
        default_region = (os.getenv("SARA_PHONE_REGION") or "IN").strip().upper() or "IN"

    digits = re.sub(r"\D", "", phone)

    # Fast path: an explicit "+…" is unambiguous, so trust the country code.
    if phone.startswith("+") and phonenumbers:
        try:
            parsed = phonenumbers.parse(phone, None)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except Exception:
            pass

    # A bare 10-digit number is ambiguous: phonenumbers happily validates
    # "4155552671" as BOTH +14155552671 (US) and +914155552671 (IN), and
    # "9389748900" as both +19389748900 (US) and +919389748900 (IN). We
    # disambiguate by shape instead of by parse order — Indian mobile numbers
    # are 10 digits starting 6-9, which is never a valid US area code.
    if digits and not phone.startswith("+") and len(digits) == 10:
        region = "IN" if digits[0] in "6789" else "US"
        if phonenumbers:
            try:
                parsed = phonenumbers.parse(digits, region)
                if phonenumbers.is_valid_number(parsed):
                    return phonenumbers.format_number(
                        parsed, phonenumbers.PhoneNumberFormat.E164
                    )
            except Exception:
                pass
        return f"+{region}{digits}" if region == "IN" else f"+1{digits}"

    if phonenumbers:
        try:
            parsed = phonenumbers.parse(phone, default_region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed,
                    phonenumbers.PhoneNumberFormat.E164,
                )
        except Exception:
            pass

    if digits:
        return f"+{digits}"

    return phone


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9_]+", text or "")
        if len(token) > 2
    }


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class CallState(str, Enum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    VALIDATING = "VALIDATING"
    DIALING = "DIALING"
    RINGING = "RINGING"
    CONNECTED = "CONNECTED"
    GREETING = "GREETING"
    DISCOVERY = "DISCOVERY"
    QUALIFICATION = "QUALIFICATION"
    INFORMATION = "INFORMATION"
    BOOKING = "BOOKING"
    CONFIRMATION = "CONFIRMATION"
    FOLLOWUP = "FOLLOWUP"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    COMPLETED = "COMPLETED"
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RETRY_PENDING = "RETRY_PENDING"


class CampaignState(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class MessageChannel(str, Enum):
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"


class MessageState(str, Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    FALLBACK_USED = "FALLBACK_USED"


class SyncState(str, Enum):
    SYNCED = "SYNCED"
    PENDING = "PENDING"
    RETRYING = "RETRYING"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "voice_customers"
    __table_args__ = (
        UniqueConstraint("business_id", "normalized_phone"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[str] = mapped_column(String(80))
    normalized_phone: Mapped[str] = mapped_column(String(80), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    timezone: Mapped[str] = mapped_column(String(80), default="UTC")
    preferred_channel: Mapped[str] = mapped_column(
        String(30),
        default="AUTOMATIC",
    )
    consent_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN")
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    service_communication_consent: Mapped[bool] = mapped_column(Boolean, default=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)

    primary_need: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secondary_needs: Mapped[dict] = mapped_column(JSON, default=dict)
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    pain_points: Mapped[dict] = mapped_column(JSON, default=dict)
    objections: Mapped[dict] = mapped_column(JSON, default=dict)

    sentiment: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    intent: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    urgency: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    qualification_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lifecycle_stage: Mapped[str] = mapped_column(String(80), default="NEW")

    next_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_followup_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_call_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_sms_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_whatsapp_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    call_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)

    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    memory: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Call(Base):
    __tablename__ = "voice_calls"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("voice_customers.id"),
        index=True,
    )
    campaign_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    provider_call_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The campaign agent this call speaks with, plus the exact profile
    # snapshot the call started with. The snapshot is what a live call keeps
    # using, so publishing a new configuration mid-call never changes what
    # the caller is already hearing; the NEXT call picks up the new version.
    agent_profile_id: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, index=True
    )
    agent_profile_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )

    direction: Mapped[str] = mapped_column(String(20), default="OUTBOUND")
    state: Mapped[str] = mapped_column(String(40), default=CallState.CREATED.value)
    objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    retry_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    transcript: Mapped[list] = mapped_column(JSON, default=list)
    extracted_data: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    trace_id: Mapped[str] = mapped_column(String(100), index=True)
    conversation_id: Mapped[str] = mapped_column(String(100), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "voice_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    call_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    event_type: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    previous_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    new_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "voice_idempotency"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    provider_event_id: Mapped[str] = mapped_column(String(255), index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "voice_knowledge_documents"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    file_hash: Mapped[str] = mapped_column(String(128), index=True)
    classification: Mapped[str] = mapped_column(String(80), default="UNKNOWN")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    processing_status: Mapped[str] = mapped_column(String(40), default="PROCESSED")
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "voice_messages"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    customer_id: Mapped[str] = mapped_column(String(80), index=True)
    call_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    appointment_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    channel: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default=MessageState.QUEUED.value)
    body: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Campaign(Base):
    __tablename__ = "voice_campaigns"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(255))
    objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(30), default=CampaignState.DRAFT.value)

    customer_ids: Mapped[list] = mapped_column(JSON, default=list)
    retry_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    communication_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    call_window: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class SyncTask(Base):
    __tablename__ = "voice_sync_tasks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    operation: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(30), default=SyncState.PENDING.value)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "voice_settings"
    __table_args__ = (
        UniqueConstraint("business_id", "key"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class VoiceChannel(Base):
    """Runtime-connected voice channel (Twilio phone / WhatsApp / SMS).

    Exactly like the WhatsApp agent's Numbers/Channels table: the owner adds
    their own provider credentials from the UI (no .env edits, no restart),
    credentials are Fernet-encrypted at rest, and the primary channel is what
    outbound calls + SMS/WhatsApp replies route through.
    """

    __tablename__ = "voice_channels"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(40))  # twilio | meta | generic
    phone_number: Mapped[str] = mapped_column(String(80), default="")
    credentials_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="disconnected")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignProfileRow(Base):
    """A saved, editable campaign agent profile.

    Scoped by ``business_id`` — the tenant scope key every other ``voice_*``
    table already uses — so one user's campaign configuration can never be
    read or written by another user.

    ``profile`` is the PUBLISHED snapshot the live agent speaks from;
    ``draft`` is the working copy the UI edits. Publishing copies the draft
    into the snapshot and bumps ``version``.
    """

    __tablename__ = "voice_campaign_profiles"
    __table_args__ = (
        UniqueConstraint("business_id", "name"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    business_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(255))
    agent_kind: Mapped[str] = mapped_column(
        String(60), default=CAMPAIGN_AGENT_KIND
    )
    status: Mapped[str] = mapped_column(String(30), default="PUBLISHED")
    version: Mapped[int] = mapped_column(Integer, default=1)

    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    draft: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CustomerInput(BaseModel):
    business_id: str
    name: Optional[str] = None
    phone: str
    email: Optional[str] = None
    company: Optional[str] = None
    language: Optional[str] = None
    timezone: str = "UTC"
    preferred_channel: str = "AUTOMATIC"
    custom_fields: Dict[str, Any] = Field(default_factory=dict)


class CallCreate(BaseModel):
    business_id: str
    customer_id: Optional[str] = None
    phone: Optional[str] = None
    objective: str = "general discovery"
    direction: str = "OUTBOUND"
    campaign_id: Optional[str] = None


class CallComplete(BaseModel):
    business_id: str
    outcome: str = "COMPLETED"
    summary: Optional[str] = None
    extracted_data: Dict[str, Any] = Field(default_factory=dict)
    transcript: List[Dict[str, Any]] = Field(default_factory=list)


class MessageRequest(BaseModel):
    business_id: str
    customer_id: str
    body: str
    preferred_channel: str = "AUTOMATIC"
    call_id: Optional[str] = None


class CampaignCreate(BaseModel):
    business_id: str
    name: str
    objective: str = "lead qualification"
    customer_ids: List[str] = Field(default_factory=list)
    retry_policy: Dict[str, Any] = Field(default_factory=dict)
    communication_policy: Dict[str, Any] = Field(default_factory=dict)
    call_window: Dict[str, Any] = Field(default_factory=dict)


class SheetSyncRequest(BaseModel):
    business_id: str
    customer_id: str
    operation: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class OwnerCommand(BaseModel):
    business_id: str
    command: str


class WebhookEvent(BaseModel):
    business_id: str
    provider_event_id: str
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class SimulationRequest(BaseModel):
    business_id: str
    customer: CustomerInput
    scenario: str = "discovery"
    language: str = "en"


# ---------------------------------------------------------------------------
# Provider interfaces
# ---------------------------------------------------------------------------

class TelephonyProvider(Protocol):
    name: str

    def capabilities(self) -> Dict[str, bool]:
        ...

    def place_call(
        self,
        *,
        to: str,
        business_id: str,
        call_id: str,
        objective: str,
    ) -> Dict[str, Any]:
        ...

    def answer_call(
        self,
        *,
        provider_call_id: str,
        business_id: str,
        call_id: str,
    ) -> Dict[str, Any]:
        ...

    def hangup(self, *, provider_call_id: str) -> Dict[str, Any]:
        ...

    def transfer(
        self,
        *,
        provider_call_id: str,
        destination: str,
    ) -> Dict[str, Any]:
        ...


class MessagingProvider(Protocol):
    name: str

    def can_send(self, channel: MessageChannel, phone: str) -> bool:
        ...

    def send(
        self,
        *,
        channel: MessageChannel,
        phone: str,
        body: str,
        business_id: str,
        customer_id: str,
    ) -> Dict[str, Any]:
        ...


class LocalTelephonyProvider:
    name = "local-unconfigured"

    def capabilities(self) -> Dict[str, bool]:
        return {
            "inbound_calls": False,
            "outbound_calls": False,
            "websocket_streaming": False,
            "bidirectional_audio": False,
            "recording": False,
            "transfer": False,
        }

    def place_call(self, **kwargs: Any) -> Dict[str, Any]:
        if not DEV_MODE:
            raise RuntimeError("No telephony provider is configured")

        return {
            "success": False,
            "configured": False,
            "provider": self.name,
            "reason": "No telephony provider configured",
        }

    def answer_call(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "provider": self.name,
            "reason": "No telephony provider configured",
        }

    def hangup(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "reason": "No telephony provider configured",
        }

    def transfer(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "reason": "Transfer is unavailable",
        }


class LocalMessagingProvider:
    name = "local-unconfigured"

    def can_send(self, channel: MessageChannel, phone: str) -> bool:
        return False

    def send(self, **kwargs: Any) -> Dict[str, Any]:
        return {
            "success": False,
            "configured": False,
            "reason": "No messaging provider configured",
        }


telephony_provider: TelephonyProvider = LocalTelephonyProvider()
messaging_provider: MessagingProvider = LocalMessagingProvider()


# ---------------------------------------------------------------------------
# Event ledger
# ---------------------------------------------------------------------------

def record_event(
    db: Session,
    *,
    business_id: str,
    event_type: str,
    source: str,
    actor: str,
    customer_id: Optional[str] = None,
    call_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    previous_state: Optional[str] = None,
    new_state: Optional[str] = None,
    payload: Optional[dict] = None,
    confidence: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Event:
    event = Event(
        id=new_id("evt"),
        business_id=business_id,
        customer_id=customer_id,
        call_id=call_id,
        conversation_id=conversation_id,
        event_type=event_type,
        source=source,
        actor=actor,
        previous_state=previous_state,
        new_state=new_state,
        payload=payload or {},
        confidence=confidence,
        metadata_json=metadata or {},
    )

    db.add(event)
    db.flush()
    return event


def audit(
    db: Session,
    *,
    business_id: str,
    operation: str,
    target: str,
    result: dict,
    actor: str = "system",
) -> None:
    record_event(
        db,
        business_id=business_id,
        event_type="AUDIT_ACTION",
        source="voice_agent",
        actor=actor,
        payload={
            "operation": operation,
            "target": target,
            "result": result,
        },
    )


# ---------------------------------------------------------------------------
# Customer identity and memory
# ---------------------------------------------------------------------------

def get_customer(
    db: Session,
    business_id: str,
    customer_id: Optional[str] = None,
    phone: Optional[str] = None,
) -> Optional[Customer]:
    if customer_id:
        customer = db.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.id == customer_id,
            )
        )
        if customer:
            return customer

    if phone:
        normalized = normalize_phone(phone)
        return db.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.normalized_phone == normalized,
            )
        )

    return None


def upsert_customer(
    db: Session,
    data: CustomerInput,
) -> Customer:
    normalized = normalize_phone(data.phone)

    customer = db.scalar(
        select(Customer).where(
            Customer.business_id == data.business_id,
            Customer.normalized_phone == normalized,
        )
    )

    is_new = customer is None

    if customer is None:
        customer = Customer(
            id=new_id("cus"),
            business_id=data.business_id,
            phone=data.phone,
            normalized_phone=normalized,
        )
        db.add(customer)

    customer.name = data.name or customer.name
    customer.email = data.email or customer.email
    customer.company = data.company or customer.company
    customer.language = data.language or customer.language
    customer.timezone = data.timezone or customer.timezone
    customer.preferred_channel = (
        data.preferred_channel or customer.preferred_channel
    )
    customer.custom_fields = {
        **(customer.custom_fields or {}),
        **data.custom_fields,
    }
    customer.updated_at = utcnow()

    db.flush()

    record_event(
        db,
        business_id=data.business_id,
        customer_id=customer.id,
        event_type="CUSTOMER_CREATED" if is_new else "CUSTOMER_UPDATED",
        source="voice_agent",
        actor="system",
        payload={
            "phone": customer.normalized_phone,
            "name": customer.name,
        },
    )

    return customer


def load_customer_context(db: Session, customer: Customer) -> Dict[str, Any]:
    events = db.scalars(
        select(Event)
        .where(
            Event.business_id == customer.business_id,
            Event.customer_id == customer.id,
        )
        .order_by(Event.timestamp.desc())
        .limit(50)
    ).all()

    return {
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone": customer.normalized_phone,
            "email": customer.email,
            "primary_need": customer.primary_need,
            "requirements": customer.requirements,
            "preferences": customer.preferences,
            "next_action": customer.next_action,
            "next_followup_at": json_safe(customer.next_followup_at),
            "intent": customer.intent,
            "sentiment": customer.sentiment,
        },
        "memory": customer.memory or {},
        "recent_events": [
            {
                "type": event.event_type,
                "timestamp": json_safe(event.timestamp),
                "payload": event.payload,
            }
            for event in events
        ],
    }


def save_customer_memory(
    db: Session,
    customer: Customer,
    memory_update: Dict[str, Any],
    source: str = "voice_agent",
) -> None:
    customer.memory = {
        **(customer.memory or {}),
        **memory_update,
    }

    customer.updated_at = utcnow()

    record_event(
        db,
        business_id=customer.business_id,
        customer_id=customer.id,
        event_type="MEMORY_UPDATED",
        source=source,
        actor="system",
        payload=memory_update,
    )


# ---------------------------------------------------------------------------
# Knowledge and file intelligence
# ---------------------------------------------------------------------------

def extract_file_content(filename: str, data: bytes) -> tuple[str, dict]:
    suffix = Path(filename).suffix.lower()
    metadata: Dict[str, Any] = {
        "filename": filename,
        "size": len(data),
        "extension": suffix,
    }

    if suffix == ".pdf":
        if not PdfReader:
            raise RuntimeError("PDF support requires pypdf")

        reader = PdfReader(io.BytesIO(data))
        pages = []

        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append(f"\n[Page {index}]\n{text}")

        metadata["page_count"] = len(reader.pages)
        return "\n".join(pages), metadata

    if suffix in {".csv"}:
        decoded = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(decoded)))
        metadata["row_count"] = len(rows)
        return "\n".join(" | ".join(row) for row in rows), metadata

    if suffix in {".xlsx", ".xlsm"}:
        if not openpyxl:
            raise RuntimeError("Excel support requires openpyxl")

        workbook = openpyxl.load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
        )

        chunks = []

        for worksheet in workbook.worksheets:
            chunks.append(f"\n[Sheet: {worksheet.title}]")

            for row in worksheet.iter_rows(values_only=True):
                chunks.append(" | ".join("" if v is None else str(v) for v in row))

        metadata["sheet_count"] = len(workbook.worksheets)
        return "\n".join(chunks), metadata

    if suffix in {".json"}:
        parsed = json.loads(data.decode("utf-8-sig", errors="replace"))
        return json.dumps(parsed, indent=2, ensure_ascii=False), metadata

    return data.decode("utf-8-sig", errors="replace"), metadata


def classify_document(filename: str, content: str) -> tuple[str, float]:
    text = f"{filename}\n{content}".lower()

    rules = [
        ("CUSTOMER_DATA", ["customer", "phone", "mobile", "email", "lead"]),
        ("APPOINTMENT_DATA", ["appointment", "booking", "scheduled", "reservation"]),
        ("PRICING", ["price", "pricing", "cost", "rate", "fee"]),
        ("POLICY", ["policy", "cancellation", "refund", "terms"]),
        ("FAQ", ["faq", "frequently asked", "question", "answer"]),
        ("PRODUCT_DATA", ["product", "sku", "inventory", "stock"]),
        ("SERVICE_DATA", ["service", "offering", "package"]),
        ("KNOWLEDGE", ["guide", "information", "overview", "details"]),
    ]

    best_type = "UNKNOWN"
    best_score = 0

    for document_type, keywords in rules:
        score = sum(1 for keyword in keywords if keyword in text)

        if score > best_score:
            best_type = document_type
            best_score = score

    confidence = min(0.99, 0.35 + best_score * 0.1) if best_score else 0.1
    return best_type, confidence


def list_knowledge_documents(
    db: Session,
    business_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Every processed document for a business, newest first.

    Backs the knowledge panel's "what have I taught SARA" list. An empty
    search term has no tokens to score, so search alone can never enumerate
    the corpus -- this is the honest way to show it.
    """
    documents = db.scalars(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.business_id == business_id,
            KnowledgeDocument.processing_status == "PROCESSED",
        )
        .order_by(KnowledgeDocument.created_at.desc())
    ).all()

    return [
        {
            "document_id": document.id,
            "filename": document.filename,
            "classification": document.classification,
            "score": 0,
            "content": document.content[:4000],
            "metadata": document.metadata_json,
        }
        for document in documents[:limit]
    ]


def search_knowledge(
    db: Session,
    business_id: str,
    query: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    query_tokens = tokenize(query)
    documents = db.scalars(
        select(KnowledgeDocument).where(
            KnowledgeDocument.business_id == business_id,
            KnowledgeDocument.processing_status == "PROCESSED",
        )
    ).all()

    results = []

    for document in documents:
        document_tokens = tokenize(document.content)
        score = len(query_tokens.intersection(document_tokens))

        if score:
            results.append(
                {
                    "document_id": document.id,
                    "filename": document.filename,
                    "classification": document.classification,
                    "score": score,
                    "content": document.content[:4000],
                    "metadata": document.metadata_json,
                }
            )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# Communication routing
# ---------------------------------------------------------------------------

def resolve_best_channel(
    db: Session,
    customer: Customer,
    requested_channel: str = "AUTOMATIC",
) -> Dict[str, Any]:
    if customer.do_not_contact:
        return {
            "preferred_channel": None,
            "fallback_channel": None,
            "reason": "Customer has requested no contact",
            "whatsapp_available": False,
            "whatsapp_eligible": False,
            "sms_available": False,
            "sms_eligible": False,
        }

    whatsapp_available = messaging_provider.can_send(
        MessageChannel.WHATSAPP,
        customer.normalized_phone,
    )

    sms_available = messaging_provider.can_send(
        MessageChannel.SMS,
        customer.normalized_phone,
    )

    whatsapp_eligible = (
        whatsapp_available
        and customer.service_communication_consent
    )

    sms_eligible = (
        sms_available
        and customer.service_communication_consent
    )

    requested_channel = requested_channel.upper()

    if requested_channel == "WHATSAPP":
        preferred = "WHATSAPP" if whatsapp_eligible else None
        fallback = "SMS" if sms_eligible else None
    elif requested_channel == "SMS":
        preferred = "SMS" if sms_eligible else None
        fallback = None
    elif requested_channel == "WHATSAPP_ONLY":
        preferred = "WHATSAPP" if whatsapp_eligible else None
        fallback = None
    elif requested_channel == "SMS_ONLY":
        preferred = "SMS" if sms_eligible else None
        fallback = None
    else:
        preferred = "WHATSAPP" if whatsapp_eligible else (
            "SMS" if sms_eligible else None
        )
        fallback = "SMS" if preferred == "WHATSAPP" and sms_eligible else None

    return {
        "preferred_channel": preferred,
        "fallback_channel": fallback,
        "reason": (
            "WhatsApp eligible"
            if preferred == "WHATSAPP"
            else "SMS fallback"
            if preferred == "SMS"
            else "No eligible messaging channel"
        ),
        "whatsapp_available": whatsapp_available,
        "whatsapp_configured": whatsapp_available,
        "whatsapp_eligible": whatsapp_eligible,
        "sms_available": sms_available,
        "sms_eligible": sms_eligible,
    }


def send_message(
    db: Session,
    *,
    business_id: str,
    customer: Customer,
    body: str,
    preferred_channel: str = "AUTOMATIC",
    call_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolution = resolve_best_channel(
        db,
        customer,
        preferred_channel,
    )

    selected = resolution["preferred_channel"]

    if not selected:
        message = Message(
            id=new_id("msg"),
            business_id=business_id,
            customer_id=customer.id,
            call_id=call_id,
            channel="NONE",
            state=MessageState.FAILED.value,
            body=body,
            failure_reason=resolution["reason"],
        )

        db.add(message)

        record_event(
            db,
            business_id=business_id,
            customer_id=customer.id,
            call_id=call_id,
            event_type="MESSAGE_FAILED",
            source="communication_service",
            actor="system",
            payload=resolution,
        )

        db.commit()

        return {
            "success": False,
            "reason": resolution["reason"],
            "resolution": resolution,
        }

    channel = MessageChannel(selected)

    message = Message(
        id=new_id("msg"),
        business_id=business_id,
        customer_id=customer.id,
        call_id=call_id,
        channel=selected,
        state=MessageState.QUEUED.value,
        body=body,
    )

    db.add(message)
    db.flush()

    provider_result = messaging_provider.send(
        channel=channel,
        phone=customer.normalized_phone,
        body=body,
        business_id=business_id,
        customer_id=customer.id,
    )

    if provider_result.get("success"):
        message.state = MessageState.SENT.value
        message.provider_message_id = provider_result.get("provider_message_id")
        message.sent_at = utcnow()
        customer.message_count += 1

        if selected == "WHATSAPP":
            customer.last_whatsapp_at = utcnow()
        else:
            customer.last_sms_at = utcnow()

        record_event(
            db,
            business_id=business_id,
            customer_id=customer.id,
            call_id=call_id,
            event_type=f"{selected}_SENT",
            source="communication_service",
            actor="system",
            payload=provider_result,
        )

        db.commit()

        return {
            "success": True,
            "channel": selected,
            "message_id": message.id,
            "provider_result": provider_result,
        }

    if resolution["fallback_channel"]:
        fallback = MessageChannel(resolution["fallback_channel"])

        fallback_result = messaging_provider.send(
            channel=fallback,
            phone=customer.normalized_phone,
            body=body,
            business_id=business_id,
            customer_id=customer.id,
        )

        if fallback_result.get("success"):
            message.channel = fallback.value
            message.state = MessageState.FALLBACK_USED.value
            message.provider_message_id = fallback_result.get(
                "provider_message_id"
            )
            message.sent_at = utcnow()
            customer.message_count += 1

            record_event(
                db,
                business_id=business_id,
                customer_id=customer.id,
                call_id=call_id,
                event_type="MESSAGE_FALLBACK_USED",
                source="communication_service",
                actor="system",
                payload={
                    "original_channel": selected,
                    "fallback_channel": fallback.value,
                    "provider_result": fallback_result,
                },
            )

            db.commit()

            return {
                "success": True,
                "channel": fallback.value,
                "fallback_used": True,
                "message_id": message.id,
                "provider_result": fallback_result,
            }

    message.state = MessageState.FAILED.value
    message.failure_reason = provider_result.get("reason", "Provider failure")

    record_event(
        db,
        business_id=business_id,
        customer_id=customer.id,
        call_id=call_id,
        event_type="MESSAGE_FAILED",
        source="communication_service",
        actor="system",
        payload=provider_result,
    )

    db.commit()

    return {
        "success": False,
        "channel": selected,
        "message_id": message.id,
        "provider_result": provider_result,
    }


# ---------------------------------------------------------------------------
# Call orchestration
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[CallState, set[CallState]] = {
    CallState.CREATED: {CallState.PREPARING, CallState.CANCELLED},
    CallState.PREPARING: {CallState.VALIDATING, CallState.FAILED},
    CallState.VALIDATING: {CallState.DIALING, CallState.FAILED, CallState.CANCELLED},
    CallState.DIALING: {
        CallState.RINGING,
        CallState.CONNECTED,
        CallState.FAILED,
        CallState.CANCELLED,
    },
    CallState.RINGING: {
        CallState.CONNECTED,
        CallState.DISCONNECTED,
        CallState.FAILED,
        CallState.CANCELLED,
    },
    CallState.CONNECTED: {
        CallState.GREETING,
        CallState.DISCOVERY,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.FAILED,
        CallState.CANCELLED,
    },
    CallState.GREETING: {
        CallState.DISCOVERY,
        CallState.INFORMATION,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.DISCOVERY: {
        CallState.QUALIFICATION,
        CallState.INFORMATION,
        CallState.BOOKING,
        CallState.CONFIRMATION,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.QUALIFICATION: {
        CallState.INFORMATION,
        CallState.BOOKING,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.INFORMATION: {
        CallState.BOOKING,
        CallState.CONFIRMATION,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.BOOKING: {
        CallState.CONFIRMATION,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.CONFIRMATION: {
        CallState.FOLLOWUP,
        CallState.COMPLETED,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.FOLLOWUP: {
        CallState.COMPLETED,
        CallState.DISCONNECTED,
        CallState.CANCELLED,
    },
    CallState.HUMAN_HANDOFF: {
        CallState.COMPLETED,
        CallState.DISCONNECTED,
        CallState.FAILED,
        CallState.CANCELLED,
    },
    CallState.DISCONNECTED: {
        CallState.RETRY_PENDING,
        CallState.COMPLETED,
        CallState.FAILED,
        CallState.CANCELLED,
    },
    CallState.RETRY_PENDING: {
        CallState.DIALING,
        CallState.CANCELLED,
    },
    CallState.COMPLETED: set(),
    CallState.FAILED: {CallState.RETRY_PENDING},
    CallState.CANCELLED: set(),
}


def transition_call(
    db: Session,
    call: Call,
    new_state: CallState,
    *,
    actor: str = "system",
    payload: Optional[dict] = None,
) -> None:
    old_state = CallState(call.state)

    if new_state != old_state and new_state not in VALID_TRANSITIONS.get(
        old_state,
        set(),
    ):
        raise ValueError(
            f"Invalid call transition: {old_state.value} -> {new_state.value}"
        )

    call.state = new_state.value

    if new_state == CallState.CONNECTED:
        call.started_at = call.started_at or utcnow()

    if new_state in {
        CallState.COMPLETED,
        CallState.FAILED,
        CallState.CANCELLED,
        CallState.DISCONNECTED,
    }:
        call.ended_at = utcnow()

    record_event(
        db,
        business_id=call.business_id,
        customer_id=call.customer_id,
        call_id=call.id,
        conversation_id=call.conversation_id,
        event_type=f"CALL_{new_state.value}",
        source="call_state_machine",
        actor=actor,
        previous_state=old_state.value,
        new_state=new_state.value,
        payload=payload or {},
    )


def create_call(db: Session, request: CallCreate) -> Call:
    stop = get_setting(db, request.business_id, "emergency_stop", False)

    if stop and request.direction.upper() == "OUTBOUND":
        raise HTTPException(
            status_code=409,
            detail="Outbound calling is currently stopped",
        )

    customer = get_customer(
        db,
        request.business_id,
        request.customer_id,
        request.phone,
    )

    if not customer:
        if not request.phone:
            raise HTTPException(
                status_code=400,
                detail="customer_id or phone is required",
            )

        customer = upsert_customer(
            db,
            CustomerInput(
                business_id=request.business_id,
                phone=request.phone,
            ),
        )

    if customer.do_not_contact and request.direction.upper() == "OUTBOUND":
        raise HTTPException(
            status_code=403,
            detail="Customer has requested no contact",
        )

    existing_active = db.scalar(
        select(Call).where(
            Call.business_id == request.business_id,
            Call.customer_id == customer.id,
            Call.state.in_(
                [
                    CallState.CREATED.value,
                    CallState.PREPARING.value,
                    CallState.VALIDATING.value,
                    CallState.DIALING.value,
                    CallState.RINGING.value,
                    CallState.CONNECTED.value,
                ]
            ),
        )
    )

    if existing_active:
        raise HTTPException(
            status_code=409,
            detail="An active call already exists for this customer",
        )

    call = Call(
        id=new_id("call"),
        business_id=request.business_id,
        customer_id=customer.id,
        campaign_id=request.campaign_id,
        direction=request.direction.upper(),
        objective=request.objective,
        state=CallState.CREATED.value,
        trace_id=new_id("trace"),
        conversation_id=new_id("conv"),
    )

    db.add(call)
    db.flush()

    record_event(
        db,
        business_id=request.business_id,
        customer_id=customer.id,
        call_id=call.id,
        conversation_id=call.conversation_id,
        event_type="CALL_CREATED",
        source="voice_agent",
        actor="system",
        payload={
            "direction": call.direction,
            "objective": call.objective,
        },
    )

    db.commit()
    db.refresh(call)

    return call


def prepare_call(db: Session, call: Call) -> Dict[str, Any]:
    customer = db.get(Customer, call.customer_id)

    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    transition_call(db, call, CallState.PREPARING)

    context = load_customer_context(db, customer)
    knowledge = search_knowledge(
        db,
        call.business_id,
        call.objective or "",
    )

    call_brief = {
        "customer": context["customer"],
        "memory": context["memory"],
        "recent_events": context["recent_events"][:10],
        "knowledge": knowledge,
        "objective": call.objective,
        "known_information": customer.requirements or {},
        "preferred_language": customer.language,
        "escalation_conditions": [
            "human request",
            "angry customer",
            "legal concern",
            "payment dispute",
            "uncertain answer",
        ],
    }

    transition_call(db, call, CallState.VALIDATING)
    db.commit()

    return call_brief


def execute_call(db: Session, call: Call) -> Dict[str, Any]:
    customer = db.get(Customer, call.customer_id)

    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    call_brief = prepare_call(db, call)

    if call.direction == "INBOUND":
        result = telephony_provider.answer_call(
            provider_call_id=call.provider_call_id or "",
            business_id=call.business_id,
            call_id=call.id,
        )
    else:
        transition_call(db, call, CallState.DIALING)

        result = telephony_provider.place_call(
            to=customer.normalized_phone,
            business_id=call.business_id,
            call_id=call.id,
            objective=call.objective or "",
        )

    if result.get("provider_call_id"):
        call.provider_call_id = result["provider_call_id"]

    if result.get("success"):
        call.attempt_count += 1
        call.last_attempt_at = utcnow()

        transition_call(db, call, CallState.CONNECTED)

        customer.call_count += 1
        customer.last_call_at = utcnow()

        transition_call(db, call, CallState.GREETING)

        db.commit()

        return {
            "success": True,
            "call_id": call.id,
            "provider_result": result,
            "call_brief": call_brief,
        }

    call.failure_reason = result.get("reason", "Provider unavailable")
    transition_call(
        db,
        call,
        CallState.FAILED,
        payload=result,
    )

    db.commit()

    return {
        "success": False,
        "call_id": call.id,
        "provider_result": result,
        "call_brief": call_brief,
    }


def complete_call(
    db: Session,
    call: Call,
    request: CallComplete,
) -> Dict[str, Any]:
    customer = db.get(Customer, call.customer_id)

    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    call.transcript = request.transcript
    call.extracted_data = request.extracted_data
    call.summary = request.summary or generate_summary(request)
    call.outcome = request.outcome

    extracted = request.extracted_data

    if "primary_need" in extracted:
        customer.primary_need = extracted["primary_need"]

    if "requirements" in extracted:
        customer.requirements = {
            **(customer.requirements or {}),
            **extracted["requirements"],
        }

    for field in [
        "intent",
        "sentiment",
        "urgency",
        "qualification_score",
        "next_action",
    ]:
        if field in extracted:
            setattr(customer, field, extracted[field])

    if "next_followup_at" in extracted:
        try:
            customer.next_followup_at = datetime.fromisoformat(
                extracted["next_followup_at"]
            )
        except Exception:
            pass

    save_customer_memory(
        db,
        customer,
        {
            "last_call_summary": call.summary,
            "last_topic": call.current_topic,
            "last_extracted_data": extracted,
            "last_call_at": utcnow().isoformat(),
        },
    )

    if call.state not in {
        CallState.COMPLETED,
        CallState.CANCELLED,
        CallState.FAILED,
    }:
        if call.state == CallState.GREETING:
            transition_call(db, call, CallState.DISCOVERY)

        if call.state in {
            CallState.DISCOVERY,
            CallState.QUALIFICATION,
            CallState.INFORMATION,
            CallState.BOOKING,
            CallState.CONFIRMATION,
            CallState.FOLLOWUP,
        }:
            transition_call(db, call, CallState.COMPLETED)

    record_event(
        db,
        business_id=call.business_id,
        customer_id=customer.id,
        call_id=call.id,
        conversation_id=call.conversation_id,
        event_type="CALL_COMPLETED",
        source="post_call_intelligence",
        actor="system",
        payload={
            "summary": call.summary,
            "outcome": call.outcome,
            "extracted_data": extracted,
        },
    )

    db.commit()

    return {
        "call_id": call.id,
        "customer_id": customer.id,
        "summary": call.summary,
        "outcome": call.outcome,
        "extracted_data": extracted,
    }


def generate_summary(request: CallComplete) -> str:
    if request.extracted_data:
        facts = ", ".join(
            f"{key}: {value}"
            for key, value in request.extracted_data.items()
        )
        return f"Call outcome: {request.outcome}. Captured: {facts}"

    return f"Call outcome: {request.outcome}."


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

def create_campaign(db: Session, request: CampaignCreate) -> Campaign:
    campaign = Campaign(
        id=new_id("camp"),
        business_id=request.business_id,
        name=request.name,
        objective=request.objective,
        state=CampaignState.READY.value,
        customer_ids=request.customer_ids,
        retry_policy=request.retry_policy,
        communication_policy=request.communication_policy,
        call_window=request.call_window,
    )

    db.add(campaign)

    record_event(
        db,
        business_id=request.business_id,
        event_type="CAMPAIGN_CREATED",
        source="campaign_engine",
        actor="system",
        payload={
            "campaign_id": campaign.id,
            "name": campaign.name,
            "customer_count": len(request.customer_ids),
        },
    )

    db.commit()
    db.refresh(campaign)

    return campaign


def run_campaign(db: Session, campaign: Campaign) -> Dict[str, Any]:
    if get_setting(db, campaign.business_id, "emergency_stop", False):
        campaign.state = CampaignState.PAUSED.value
        db.commit()

        return {
            "started": False,
            "reason": "Emergency stop is active",
        }

    campaign.state = CampaignState.RUNNING.value
    db.commit()

    created_calls = []
    skipped = []

    for customer_id in campaign.customer_ids:
        if get_setting(db, campaign.business_id, "emergency_stop", False):
            campaign.state = CampaignState.PAUSED.value
            db.commit()
            break

        customer = get_customer(
            db,
            campaign.business_id,
            customer_id=customer_id,
        )

        if not customer:
            skipped.append({
                "customer_id": customer_id,
                "reason": "Customer not found",
            })
            continue

        if customer.do_not_contact:
            skipped.append({
                "customer_id": customer_id,
                "reason": "Do not contact",
            })
            continue

        try:
            call = create_call(
                db,
                CallCreate(
                    business_id=campaign.business_id,
                    customer_id=customer.id,
                    objective=campaign.objective or "campaign",
                    campaign_id=campaign.id,
                ),
            )
            created_calls.append(call.id)
        except Exception as exc:
            skipped.append({
                "customer_id": customer_id,
                "reason": str(exc),
            })

    if campaign.state == CampaignState.RUNNING.value:
        campaign.state = CampaignState.COMPLETED.value

    db.commit()

    return {
        "started": True,
        "campaign_id": campaign.id,
        "created_calls": created_calls,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Settings and owner console
# ---------------------------------------------------------------------------

def get_setting(
    db: Session,
    business_id: str,
    key: str,
    default: Any = None,
) -> Any:
    setting = db.scalar(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.key == key,
        )
    )

    return setting.value if setting else default


def set_setting(
    db: Session,
    business_id: str,
    key: str,
    value: Any,
) -> None:
    setting = db.scalar(
        select(AppSetting).where(
            AppSetting.business_id == business_id,
            AppSetting.key == key,
        )
    )

    if not setting:
        setting = AppSetting(
            id=new_id("setting"),
            business_id=business_id,
            key=key,
        )
        db.add(setting)

    setting.value = value
    setting.updated_at = utcnow()


def owner_command(db: Session, request: OwnerCommand) -> Dict[str, Any]:
    command = request.command.strip()
    normalized = command.lower()

    if normalized in {"stop all calling", "emergency stop", "stop calling"}:
        set_setting(db, request.business_id, "emergency_stop", True)

        record_event(
            db,
            business_id=request.business_id,
            event_type="GLOBAL_EMERGENCY_STOP",
            source="owner_console",
            actor="owner",
            payload={"command": command},
        )

        db.commit()

        return {
            "type": "ACTION",
            "success": True,
            "message": "All new outbound calling and retries are stopped.",
        }

    if normalized in {"resume calling", "resume all calling"}:
        set_setting(db, request.business_id, "emergency_stop", False)

        record_event(
            db,
            business_id=request.business_id,
            event_type="GLOBAL_EMERGENCY_STOP_CLEARED",
            source="owner_console",
            actor="owner",
            payload={"command": command},
        )

        db.commit()

        return {
            "type": "ACTION",
            "success": True,
            "message": "Outbound calling is enabled again.",
        }

    if "who" in normalized and "called" in normalized:
        calls = db.scalars(
            select(Call).where(
                Call.business_id == request.business_id,
                Call.created_at >= utcnow() - timedelta(days=1),
            )
        ).all()

        return {
            "type": "INFORMATION_REQUEST",
            "success": True,
            "calls": [
                {
                    "call_id": call.id,
                    "customer_id": call.customer_id,
                    "state": call.state,
                    "outcome": call.outcome,
                    "created_at": json_safe(call.created_at),
                }
                for call in calls
            ],
        }

    if "knowledge" in normalized or "files" in normalized:
        documents = db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.business_id == request.business_id,
            )
        ).all()

        return {
            "type": "INFORMATION_REQUEST",
            "success": True,
            "files": [
                {
                    "filename": document.filename,
                    "classification": document.classification,
                    "status": document.processing_status,
                }
                for document in documents
            ],
        }

    if normalized.startswith("call "):
        rest = command[5:].strip()

        # Extract a phone number if present: "call devendra at +919389748900"
        phone_match = re.search(r"(\+?\d[\d\s\-().]{7,}\d)", rest)
        target_phone = ""
        target_name = ""
        if phone_match:
            target_phone = normalize_phone(phone_match.group(1))
            target_name = rest[: phone_match.start()].strip()
            target_name = re.sub(r"\b(at|on|to|number|no)\s*$", "", target_name, flags=re.I).strip()
            target_name = re.sub(r"[,\-]\s*$", "", target_name).strip()
        else:
            target_name = rest

        customer = None
        if target_phone:
            customer = db.scalar(
                select(Customer).where(
                    Customer.business_id == request.business_id,
                    Customer.normalized_phone == target_phone,
                )
            )
            if customer is None:
                # Unknown number — create the customer on the fly, then dial.
                customer = upsert_customer(
                    db,
                    CustomerInput(
                        business_id=request.business_id,
                        name=target_name.title() if target_name else None,
                        phone=target_phone,
                    ),
                )
            elif target_name and not customer.name:
                customer.name = target_name.title()
        else:
            customers = db.scalars(
                select(Customer).where(
                    Customer.business_id == request.business_id,
                    Customer.name.ilike(f"%{target_name}%"),
                )
            ).all()

            if len(customers) > 1:
                return {
                    "type": "ACTION",
                    "success": False,
                    "message": (
                        f"Multiple customers match “{target_name}” — be more specific or give the number, "
                        f"e.g. “call {target_name} at +15551234567”."
                    ),
                    "matches": [
                        {"id": c.id, "name": c.name, "phone": c.normalized_phone}
                        for c in customers
                    ],
                }
            if customers:
                customer = customers[0]
            else:
                return {
                    "type": "ACTION",
                    "success": False,
                    "message": (
                        f"No customer named '{target_name}' yet. Give me their number and I'll create them "
                        f"and dial - e.g. 'call {target_name} at +15551234567'."
                    ),
                }

        call = create_call(
            db,
            CallCreate(
                business_id=request.business_id,
                customer_id=customer.id,
                objective="owner requested callback",
            ),
        )

        result = execute_call(db, call)

        audit(
            db,
            business_id=request.business_id,
            operation="owner_call_customer",
            target=customer.id,
            result=result,
            actor="owner",
        )

        db.commit()

        reason = ""
        if not result.get("success"):
            provider = result.get("provider_result") or {}
            reason = provider.get("reason") or call.failure_reason or "telephony provider unavailable"

        return {
            "type": "ACTION",
            "success": bool(result.get("success")),
            "message": (
                f"📞 Dialing {customer.name or customer.normalized_phone} from your connected line now."
                if result.get("success")
                else f"Call failed: {reason}"
            ),
            "call_id": call.id,
            "customer": {
                "id": customer.id,
                "name": customer.name,
                "phone": customer.normalized_phone,
            },
        }

    return {
        "type": "INFORMATION_REQUEST",
        "success": False,
        "message": "The command was not recognized.",
    }


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate_conversation(
    db: Session,
    request: SimulationRequest,
) -> Dict[str, Any]:
    customer = upsert_customer(db, request.customer)

    scenario_data = {
        "discovery": {
            "primary_need": "Customer needs information about the business service.",
            "intent": "MEDIUM_INTENT",
            "sentiment": "neutral",
            "next_action": "Send follow-up information",
        },
        "booking": {
            "primary_need": "Customer wants to schedule an appointment.",
            "intent": "APPOINTMENT",
            "sentiment": "positive",
            "next_action": "Create appointment",
        },
        "cancellation": {
            "primary_need": "Customer wants to cancel an existing request.",
            "intent": "CANCELLATION",
            "sentiment": "neutral",
            "next_action": "Cancel appointment or follow-up",
        },
        "escalation": {
            "primary_need": "Customer requires human assistance.",
            "intent": "HUMAN_REQUEST",
            "sentiment": "frustrated",
            "next_action": "Create human handoff task",
        },
    }.get(request.scenario, {})

    save_customer_memory(
        db,
        customer,
        {
            "simulation": True,
            "scenario": request.scenario,
            "simulated_extraction": scenario_data,
        },
        source="simulation",
    )

    record_event(
        db,
        business_id=request.business_id,
        customer_id=customer.id,
        event_type="SIMULATION_COMPLETED",
        source="simulation",
        actor="owner",
        payload={
            "scenario": request.scenario,
            "would_send_message": request.scenario in {"booking", "discovery"},
        },
    )

    db.commit()

    return {
        "simulation": True,
        "customer_id": customer.id,
        "profile": {
            "name": customer.name,
            "phone": customer.normalized_phone,
            "memory": customer.memory,
        },
        "events_would_be_created": [
            "CUSTOMER_UPDATED",
            "MEMORY_UPDATED",
            "SIMULATION_COMPLETED",
        ],
        "message_would_be_sent": (
            "A confirmation or follow-up message would be sent."
            if request.scenario in {"booking", "discovery"}
            else None
        ),
        "result": scenario_data,
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "sara-voice-agent",
        "timestamp": utcnow().isoformat(),
        "telephony_provider": telephony_provider.name,
        "telephony_capabilities": telephony_provider.capabilities(),
    }


@app.get("/voice/calls")
def api_list_calls(
    business_id: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """Recent calls for the Command Center live feed."""
    with SessionLocal() as db:
        calls = db.scalars(
            select(Call)
            .where(Call.business_id == business_id)
            .order_by(Call.created_at.desc())
            .limit(min(max(limit, 1), 200))
        ).all()

        return {
            "calls": [
                {
                    "id": call.id,
                    "customer_id": call.customer_id,
                    "customer_name": _customer_name(db, call.customer_id),
                    "phone": _customer_phone(db, call.customer_id),
                    "direction": call.direction,
                    "state": call.state,
                    "objective": call.objective,
                    "outcome": call.outcome,
                    "duration_seconds": _duration_seconds(call),
                    "provider_call_id": call.provider_call_id,
                    "started_at": json_safe(call.started_at),
                    "created_at": json_safe(call.created_at),
                }
                for call in calls
            ]
        }


def _customer_name(db: Session, customer_id: str) -> Optional[str]:
    customer = db.get(Customer, customer_id)
    return customer.name if customer else None


def _customer_phone(db: Session, customer_id: str) -> Optional[str]:
    customer = db.get(Customer, customer_id)
    return customer.normalized_phone if customer else None


def _duration_seconds(call: Call) -> Optional[float]:
    if call.started_at and call.ended_at:
        return round(
            (call.ended_at - call.started_at).total_seconds(),
            1,
        )
    return None


@app.get("/voice/analytics")
def api_analytics(business_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        calls = db.scalars(
            select(Call).where(Call.business_id == business_id)
        ).all()

        messages = db.scalars(
            select(Message).where(Message.business_id == business_id)
        ).all()

        completed = [
            call for call in calls
            if call.state == CallState.COMPLETED.value
        ]

        failed = [
            call for call in calls
            if call.state == CallState.FAILED.value
        ]

        callbacks = [
            call for call in calls
            if call.outcome == "CALLBACK"
            or call.extracted_data.get("intent") == "CALLBACK"
        ]

        return {
            "business_id": business_id,
            "total_calls": len(calls),
            "completed_calls": len(completed),
            "failed_calls": len(failed),
            "callback_calls": len(callbacks),
            "total_messages": len(messages),
            "whatsapp_messages": sum(
                1 for message in messages
                if message.channel == MessageChannel.WHATSAPP.value
            ),
            "sms_messages": sum(
                1 for message in messages
                if message.channel == MessageChannel.SMS.value
            ),
            "sms_fallbacks": sum(
                1 for message in messages
                if message.state == MessageState.FALLBACK_USED.value
            ),
            "emergency_stop": get_setting(
                db,
                business_id,
                "emergency_stop",
                False,
            ),
        }


@app.post("/voice/customers")
def api_create_customer(request: CustomerInput) -> Dict[str, Any]:
    with SessionLocal() as db:
        customer = upsert_customer(db, request)
        db.commit()

        return {
            "id": customer.id,
            "business_id": customer.business_id,
            "name": customer.name,
            "phone": customer.normalized_phone,
        }


@app.get("/voice/customers/{customer_id}")
def api_get_customer(
    customer_id: str,
    business_id: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        customer = get_customer(
            db,
            business_id,
            customer_id=customer_id,
        )

        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        return load_customer_context(db, customer)


@app.post("/voice/calls")
def api_create_call(request: CallCreate) -> Dict[str, Any]:
    with SessionLocal() as db:
        call = create_call(db, request)

        result = {
            "call_id": call.id,
            "customer_id": call.customer_id,
            "state": call.state,
            "trace_id": call.trace_id,
            "conversation_id": call.conversation_id,
        }

        return result


@app.post("/voice/calls/{call_id}/start")
def api_start_call(
    call_id: str,
    business_id: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        call = db.scalar(
            select(Call).where(
                Call.id == call_id,
                Call.business_id == business_id,
            )
        )

        if not call:
            raise HTTPException(status_code=404, detail="Call not found")

        return execute_call(db, call)


@app.post("/voice/calls/{call_id}/complete")
def api_complete_call(
    call_id: str,
    request: CallComplete,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        call = db.scalar(
            select(Call).where(
                Call.id == call_id,
                Call.business_id == request.business_id,
            )
        )

        if not call:
            raise HTTPException(status_code=404, detail="Call not found")

        return complete_call(db, call, request)


@app.post("/voice/calls/{call_id}/cancel")
def api_cancel_call(
    call_id: str,
    business_id: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        call = db.scalar(
            select(Call).where(
                Call.id == call_id,
                Call.business_id == business_id,
            )
        )

        if not call:
            raise HTTPException(status_code=404, detail="Call not found")

        if call.state in {
            CallState.COMPLETED.value,
            CallState.CANCELLED.value,
            CallState.FAILED.value,
        }:
            raise HTTPException(
                status_code=409,
                detail="Call cannot be cancelled in its current state",
            )

        customer = db.get(Customer, call.customer_id)

        if call.provider_call_id:
            telephony_provider.hangup(
                provider_call_id=call.provider_call_id,
            )

        transition_call(
            db,
            call,
            CallState.CANCELLED,
            actor="owner",
        )

        record_event(
            db,
            business_id=business_id,
            customer_id=customer.id if customer else None,
            call_id=call.id,
            event_type="CALL_CANCELLED",
            source="owner_console",
            actor="owner",
        )

        db.commit()

        return {
            "success": True,
            "call_id": call.id,
            "state": call.state,
        }


@app.post("/voice/messages/send")
def api_send_message(request: MessageRequest) -> Dict[str, Any]:
    with SessionLocal() as db:
        customer = get_customer(
            db,
            request.business_id,
            customer_id=request.customer_id,
        )

        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        return send_message(
            db,
            business_id=request.business_id,
            customer=customer,
            body=request.body,
            preferred_channel=request.preferred_channel,
            call_id=request.call_id,
        )


@app.post("/voice/campaigns")
def api_create_campaign(request: CampaignCreate) -> Dict[str, Any]:
    with SessionLocal() as db:
        campaign = create_campaign(db, request)

        return {
            "campaign_id": campaign.id,
            "state": campaign.state,
            "customer_count": len(campaign.customer_ids),
        }


@app.post("/voice/campaigns/{campaign_id}/run")
def api_run_campaign(
    campaign_id: str,
    business_id: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        campaign = db.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.business_id == business_id,
            )
        )

        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        return run_campaign(db, campaign)


@app.post("/voice/campaigns/{campaign_id}/pause")
def api_pause_campaign(
    campaign_id: str,
    business_id: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        campaign = db.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.business_id == business_id,
            )
        )

        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        campaign.state = CampaignState.PAUSED.value

        record_event(
            db,
            business_id=business_id,
            event_type="CAMPAIGN_PAUSED",
            source="campaign_engine",
            actor="owner",
            payload={"campaign_id": campaign.id},
        )

        db.commit()

        return {
            "success": True,
            "campaign_id": campaign.id,
            "state": campaign.state,
        }


@app.post("/voice/files/upload")
async def api_upload_file(
    business_id: str = Form(...),
    uploaded_file: UploadFile = File(...),
) -> Dict[str, Any]:
    data = await uploaded_file.read()

    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="File exceeds the 50 MB limit",
        )

    filename = uploaded_file.filename or "uploaded-file"
    content_type = uploaded_file.content_type or "application/octet-stream"

    try:
        content, metadata = extract_file_content(filename, data)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"File processing failed: {exc}",
        )

    classification, confidence = classify_document(filename, content)

    with SessionLocal() as db:
        document = KnowledgeDocument(
            id=new_id("file"),
            business_id=business_id,
            filename=filename,
            mime_type=content_type,
            file_hash=hash_bytes(data),
            classification=classification,
            confidence=confidence,
            processing_status="PROCESSED",
            content=content,
            metadata_json=metadata,
        )

        db.add(document)

        record_event(
            db,
            business_id=business_id,
            event_type="FILE_PROCESSED",
            source="file_center",
            actor="owner",
            payload={
                "document_id": document.id,
                "filename": filename,
                "classification": classification,
                "confidence": confidence,
            },
        )

        db.commit()

        return {
            "document_id": document.id,
            "filename": filename,
            "classification": classification,
            "confidence": confidence,
            "metadata": metadata,
            "processing_status": document.processing_status,
        }


@app.get("/voice/knowledge/search")
def api_search_knowledge(
    business_id: str,
    q: str = "",
    query: Optional[str] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """Lexical search over the business knowledge base.

    The hub UI sends ``q`` (matching the WhatsAgent knowledge routes); the
    engine originally only accepted ``query``, which made every call from the
    UI fail validation with a 422. Both names are honoured now. With no term
    the endpoint lists documents instead of returning nothing, so the panel
    has a real corpus to render.
    """
    term = (q or query or "").strip()
    with SessionLocal() as db:
        if term:
            return {
                "query": term,
                "results": search_knowledge(db, business_id, term, limit=limit),
            }
        return {
            "query": term,
            "results": list_knowledge_documents(db, business_id),
        }


@app.post("/voice/sheets/sync")
def api_sheet_sync(request: SheetSyncRequest) -> Dict[str, Any]:
    with SessionLocal() as db:
        task = SyncTask(
            id=new_id("sync"),
            business_id=request.business_id,
            customer_id=request.customer_id,
            operation=request.operation,
            payload=request.payload,
            state=SyncState.PENDING.value,
        )

        db.add(task)

        record_event(
            db,
            business_id=request.business_id,
            customer_id=request.customer_id,
            event_type="SHEET_SYNC_QUEUED",
            source="google_sheets",
            actor="system",
            payload={
                "sync_task_id": task.id,
                "operation": request.operation,
            },
        )

        db.commit()

        return {
            "sync_task_id": task.id,
            "state": task.state,
            "message": (
                "Synchronization queued. Configure a Google Sheets adapter "
                "to execute the pending task."
            ),
        }


@app.post("/voice/owner/command")
def api_owner_command(request: OwnerCommand) -> Dict[str, Any]:
    with SessionLocal() as db:
        return owner_command(db, request)


@app.post("/voice/simulation")
def api_simulation(request: SimulationRequest) -> Dict[str, Any]:
    with SessionLocal() as db:
        return simulate_conversation(db, request)


@app.post("/voice/webhooks/provider")
def api_provider_webhook(request: WebhookEvent) -> Dict[str, Any]:
    with SessionLocal() as db:
        existing = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.business_id == request.business_id,
                IdempotencyRecord.provider_event_id
                == request.provider_event_id,
            )
        )

        if existing:
            return {
                "duplicate": True,
                "result": existing.result,
            }

        result: Dict[str, Any] = {
            "accepted": True,
            "event_type": request.event_type,
        }

        if request.event_type == "inbound_call":
            phone = request.payload.get("phone")

            if not phone:
                raise HTTPException(
                    status_code=400,
                    detail="Inbound call webhook requires phone",
                )

            customer = get_customer(
                db,
                request.business_id,
                phone=phone,
            )

            if not customer:
                customer = upsert_customer(
                    db,
                    CustomerInput(
                        business_id=request.business_id,
                        phone=phone,
                        name=request.payload.get("name"),
                    ),
                )

            call = create_call(
                db,
                CallCreate(
                    business_id=request.business_id,
                    customer_id=customer.id,
                    objective="inbound customer call",
                    direction="INBOUND",
                ),
            )

            call.provider_call_id = request.payload.get("provider_call_id")
            db.commit()

            result.update({
                "call_id": call.id,
                "customer_id": customer.id,
                "auto_answer": True,
            })

        elif request.event_type == "sms_received":
            phone = request.payload.get("phone")
            body = request.payload.get("body", "")
            customer = get_customer(
                db,
                request.business_id,
                phone=phone,
            )

            if customer:
                intent = classify_message_intent(body)

                if intent == "DO_NOT_CONTACT":
                    customer.do_not_contact = True
                    customer.consent_status = "OPTED_OUT"

                elif intent == "CANCELLATION":
                    customer.next_action = "Cancellation requested"

                elif intent == "RESCHEDULE":
                    customer.next_action = "Reschedule requested"

                record_event(
                    db,
                    business_id=request.business_id,
                    customer_id=customer.id,
                    event_type="SMS_RECEIVED",
                    source="webhook",
                    actor="customer",
                    payload={
                        "body": body,
                        "intent": intent,
                    },
                )

                db.commit()

        idempotency = IdempotencyRecord(
            id=new_id("idem"),
            business_id=request.business_id,
            provider_event_id=request.provider_event_id,
            result=result,
        )

        db.add(idempotency)
        db.commit()

        return result


def classify_message_intent(body: str) -> str:
    text = body.lower().strip()

    if any(
        phrase in text
        for phrase in [
            "do not contact",
            "don't contact",
            "stop calling",
            "unsubscribe",
            "opt out",
        ]
    ):
        return "DO_NOT_CONTACT"

    if any(
        phrase in text
        for phrase in [
            "cancel",
            "remove my booking",
            "i don't need it",
        ]
    ):
        return "CANCELLATION"

    if any(
        phrase in text
        for phrase in [
            "reschedule",
            "move it",
            "change the time",
            "change it to",
        ]
    ):
        return "RESCHEDULE"

    if any(
        phrase in text
        for phrase in [
            "call me",
            "callback",
            "call later",
        ]
    ):
        return "CALLBACK"

    return "OTHER"


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

@app.get("/voice/cost-estimate")
def api_cost_estimate(
    contacts: int,
    expected_duration_minutes: float = 3.0,
    expected_answer_rate: float = 0.35,
    retry_count: int = 1,
) -> Dict[str, Any]:
    estimated_answered_calls = contacts * expected_answer_rate
    estimated_attempts = contacts + (
        max(0, contacts - estimated_answered_calls) * retry_count
    )

    pricing = {
        "telephony_per_minute": 0.03,
        "stt_per_minute": 0.01,
        "tts_per_minute": 0.015,
        "llm_per_call": 0.02,
        "sms_per_message": 0.01,
        "whatsapp_per_message": 0.02,
    }

    telephony = estimated_attempts * expected_duration_minutes * pricing[
        "telephony_per_minute"
    ]

    stt = estimated_answered_calls * expected_duration_minutes * pricing[
        "stt_per_minute"
    ]

    tts = estimated_answered_calls * expected_duration_minutes * pricing[
        "tts_per_minute"
    ]

    llm = estimated_answered_calls * pricing["llm_per_call"]

    messaging = estimated_answered_calls * pricing["whatsapp_per_message"]

    total = telephony + stt + tts + llm + messaging

    return {
        "estimate": True,
        "contacts": contacts,
        "expected_answer_rate": expected_answer_rate,
        "expected_duration_minutes": expected_duration_minutes,
        "retry_count": retry_count,
        "attempts": round(estimated_attempts, 2),
        "answered_calls": round(estimated_answered_calls, 2),
        "breakdown": {
            "telephony": round(telephony, 4),
            "stt": round(stt, 4),
            "tts": round(tts, 4),
            "llm": round(llm, 4),
            "messaging": round(messaging, 4),
        },
        "estimated_total": round(total, 4),
        "pricing_source": "configurable defaults",
    }


@app.post("/voice/admin/emergency-stop")
def api_emergency_stop(
    business_id: str,
    enabled: bool = True,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        set_setting(db, business_id, "emergency_stop", enabled)

        record_event(
            db,
            business_id=business_id,
            event_type=(
                "GLOBAL_EMERGENCY_STOP"
                if enabled
                else "GLOBAL_EMERGENCY_STOP_CLEARED"
            ),
            source="admin",
            actor="owner",
            payload={"enabled": enabled},
        )

        db.commit()

        return {
            "business_id": business_id,
            "emergency_stop": enabled,
        }


# ---------------------------------------------------------------------------
# Provider registration hooks
# ---------------------------------------------------------------------------

def register_telephony_provider(provider: TelephonyProvider) -> None:
    """
    Register a real telephony provider adapter during application startup.

    Example:

        register_telephony_provider(TwilioTelephonyProvider(...))
    """
    global telephony_provider
    telephony_provider = provider


def register_messaging_provider(provider: MessagingProvider) -> None:
    """
    Register a real WhatsApp/SMS provider adapter during application startup.
    """
    global messaging_provider
    messaging_provider = provider


# ---------------------------------------------------------------------------
# LIVE VOICE SERVICES — env-driven, nothing hardcoded
#   LLM brain  : shared LLM_API_KEY / LLM_BASE_URL / LLM_MODEL (backend/.env)
#   TTS        : ElevenLabs (ELEVENLABS_API_KEY) or Sarvam (SARVAM_API_KEY)
#   STT        : Sarvam (SARVAM_API_KEY) — for uploaded/webhook call audio
# ---------------------------------------------------------------------------

import base64 as _b64
import logging

import httpx

log = logging.getLogger("sara.voice.engine")

# Structured voice logging (call id, session, phase, latency) with secret scrub.
from app.services.voice_logging import configure_logging as _voice_configure_logging  # noqa: E402

_voice_configure_logging()

# Sarvam client — STT / STT-translate / TTS / translate with retries,
# rate-limit handling and structured errors. Key comes from backend/.env only.
from app.services.sarvam_client import (  # noqa: E402
    SarvamAuthError,
    SarvamBadResponse,
    SarvamError,
    SarvamRateLimitError,
    SarvamServerError,
    SarvamTimeout,
    sarvam_client,
)

# 13-language support: English default + 12 Indian languages with per-turn
# automatic detection, explicit switch commands and honest TTS fallbacks.
from app.services.voice_languages import (  # noqa: E402
    DEFAULT_LANGUAGE,
    bilingual_greeting,
    detect_language,
    resolve_language,
    sarvam_tts_language,
    supported_language_list,
    twilio_gather_language,
)


def _llm_configured() -> bool:
    # The shared router also accepts Gemini (and its other gateways), so an
    # install configured with only GEMINI_API_KEY is fully functional.
    return bool(LLM_API_KEY or _settings.GEMINI_API_KEY)


def _tts_configured() -> bool:
    if VOICE_TTS_PROVIDER == "elevenlabs":
        return bool(ELEVENLABS_API_KEY)
    if VOICE_TTS_PROVIDER == "sarvam":
        return bool(SARVAM_API_KEY)
    return False


def _stt_configured() -> bool:
    if VOICE_STT_PROVIDER == "sarvam":
        return bool(SARVAM_API_KEY)
    return False


async def llm_reply(
    business_id: str,
    user_text: str,
    *,
    channel: str = "voice",
) -> dict:
    """One autonomous agent turn: memory + knowledge in, grounded reply out.

    Persists the conversation turn as events so the Command Center feed and the
    owner console see the same context the caller experiences.
    """
    if not _llm_configured():
        return {
            "reply": "Voice brain is not configured. Add LLM_API_KEY to backend/.env.",
            "intent": "SYSTEM_UNCONFIGURED",
            "configured": False,
        }

    with SessionLocal() as db:
        # Persona: concise spoken reply, grounded in knowledge + customer memory.
        # The runtime persona + business context (editable from the UI, like the
        # WhatsApp agent) shape every voice turn.
        system_prompt = (
            "<persona>\n"
            + voice_persona_load()
            + "\n</persona>\n\n"
            "<business_context>\n"
            + (voice_business_load() or "{}")
            + "\n</business_context>\n\n"
            "Reply in 1-3 short sentences that sound natural when spoken aloud. "
            "Never use markdown, emoji, or lists. If you don't know something, "
            "say so honestly and offer a follow-up. Current date: "
            f"{utcnow().date().isoformat()}."
        )

        # Caller memory (if this caller is known)
        memory_lines: list[str] = []
        with SessionLocal() as db2:
            events = db2.scalars(
                select(Event)
                .where(
                    Event.business_id == business_id,
                    Event.source == "voice_call",
                )
                .order_by(Event.timestamp.desc())
                .limit(12)
            ).all()
            for e in reversed(events):
                who = "caller" if e.actor == "customer" else "sara"
                body = (e.payload or {}).get("text", "")
                if body:
                    memory_lines.append(f"{who}: {body[:220]}")

        knowledge_hits = search_knowledge(db, business_id, user_text, limit=3)
        knowledge_block = "\n".join(
            f"- {r['content'][:300]}" for r in knowledge_hits
        )

        if memory_lines:
            system_prompt += "\n\nRecent conversation:\n" + "\n".join(memory_lines)
        if knowledge_block:
            system_prompt += "\n\nBusiness knowledge:\n" + knowledge_block

        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.4,
            "max_tokens": 220,
        }

        # Persist the caller turn
        record_event(
            db,
            business_id=business_id,
            event_type="VOICE_UTTERANCE",
            source="voice_call",
            actor="customer",
            payload={"text": user_text, "channel": channel},
        )    # Centralized failover: primary -> secondary -> next healthy provider.
    # The configured premium model may be blocked/exhausted; the router walks
    # every provider + known-working fallback models automatically.
    try:
        reply = await _failover_chat(
            payload["messages"],
            model=LLM_MODEL,
            max_tokens=payload.get("max_tokens", 220),
            temperature=payload.get("temperature", 0.4),
            timeout=45,
        )
    except Exception as exc:
        log.warning("LLM call failed (full failover chain): %s", exc)
        return {
            "reply": "I'm having trouble reaching my reasoning service right now.",
            "intent": "LLM_ERROR",
            "configured": True,
            "error": str(exc)[:200],
        }

    # Persist SARA's turn
    with SessionLocal() as db:
        record_event(
            db,
            business_id=business_id,
            event_type="VOICE_REPLY",
            source="voice_call",
            actor="system",
            payload={"text": reply, "channel": channel},
        )
        db.commit()

    return {"reply": reply, "intent": "VOICE_TURN", "configured": True}


def _static_dir() -> Path:
    d = Path(__file__).resolve().parent / "static" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def tts_speak(text: str, *, language: str = "en-IN", voice: Optional[str] = None) -> dict:
    """Speak `text` with the configured provider; return {url} or {error}.

    Sarvam path uses the full SarvamClient (chunking, retries, rate-limit
    handling, structured errors) and any of the 13 supported languages; the
    ElevenLabs path is preserved unchanged. TTS failures degrade to Twilio
    <Say>/browser text — the call never drops.

    `voice` selects a provider speaker where the provider supports one
    (the campaign agent's editable voice setting).
    """
    if not text.strip():
        return {"error": "empty text"}
    try:
        if VOICE_TTS_PROVIDER == "elevenlabs" and ELEVENLABS_API_KEY:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
                    headers={
                        "xi-api-key": ELEVENLABS_API_KEY,
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text,
                        "model_id": "eleven_multilingual_v2",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        },
                    },
                )
                r.raise_for_status()
                audio = r.content
        elif VOICE_TTS_PROVIDER == "sarvam" and SARVAM_API_KEY:
            if not sarvam_client.configured:
                return {"error": "Sarvam client unavailable (SARVAM_API_KEY missing?)"}
            tts_code, native = sarvam_tts_language(language)
            if not native:
                log.info(
                    "TTS non-native language=%s -> speaking %s (honest fallback)",
                    language, tts_code,
                )
            try:
                speech = await sarvam_client.speak(
                    text,
                    target_language_code=_tts_sarvam_language(tts_code),
                    speaker=voice or None,
                )
            except SarvamError as exc:
                log.warning(
                    "TTS failed kind=%s status=%s detail=%s",
                    exc.kind, getattr(exc, "status", None), getattr(exc, "detail", None),
                )
                return {"error": exc.safe_message}
            audio = speech.audio
            if not audio:
                return {"error": "Sarvam TTS returned no audio"}
        else:
            return {
                "error": (
                    f"TTS not configured (VOICE_TTS_PROVIDER={VOICE_TTS_PROVIDER!r}). "
                    "Set ELEVENLABS_API_KEY or SARVAM_API_KEY in backend/.env."
                )
            }

        fname = f"tts_{uuid.uuid4().hex[:12]}.mp3"
        path = _static_dir() / fname
        path.write_bytes(audio)
        # 'bytes_data' carries the rendered audio to the Media Streams warm-up
        # cache, which needs the bytes themselves rather than the URL written
        # above. Absent when a provider returns an error dict.
        return {
            "url": f"/voice-agent/static/voice/{fname}",
            "bytes": len(audio),
            "bytes_data": audio,
        }
    except Exception as exc:
        log.warning("TTS failed: %s", exc)
        return {"error": str(exc)[:300]}


async def stt_transcribe(audio: bytes, *, language: str = "unknown") -> dict:
    """Transcribe call audio with the configured provider (Sarvam batch API).

    Uses the SarvamClient with retry/backoff and structured errors; the exact
    technical failure (auth, rate limit, timeout, malformed body) is logged.
    With language='unknown' Sarvam auto-detects among the supported languages.
    """
    if not _stt_configured():
        return {
            "error": (
                f"STT not configured (VOICE_STT_PROVIDER={VOICE_STT_PROVIDER!r}). "
                "Set SARVAM_API_KEY in backend/.env."
            )
        }
    if not sarvam_client.configured:
        return {"error": "Sarvam client unavailable (SARVAM_API_KEY missing?)"}
    try:
        lang_code = None if (not language or language == "unknown") else language
        try:
            transcript = await sarvam_client.transcribe(audio, language_code=lang_code)
        except SarvamError as exc:
            log.warning(
                "STT failed kind=%s status=%s detail=%s",
                exc.kind, getattr(exc, "status", None), getattr(exc, "detail", None),
            )
            return {"error": exc.safe_message}
        return {
            "text": transcript.text,
            "language_code": transcript.language_code,
            "language_probability": transcript.language_probability,
        }
    except Exception as exc:
        log.warning("STT failed: %s", exc)
        return {"error": str(exc)[:300]}


# ---------------------------------------------------------------------------
# Runtime voice channels — connect your own Twilio / Meta / webhook provider
# from the UI (encrypted credentials), exactly like the WhatsApp agent's
# Numbers/Channels panel. No .env edits, no restart.
# ---------------------------------------------------------------------------

VOICE_CHANNEL_PROVIDERS = {
    "twilio": {
        "label": "Twilio",
        "icon": "🟥",
        "fields": ["account_sid", "auth_token", "phone_number"],
        "help": (
            "Twilio powers phone calls AND SMS/WhatsApp messaging. Create a project at "
            "console.twilio.com, copy Account SID + Auth Token, and use a phone number "
            "(E.164, e.g. +15551234567) that has SMS enabled. For WhatsApp messaging, "
            "activate the WhatsApp sender on that same number in the Twilio console."
        ),
    },
    "meta": {
        "label": "Meta WhatsApp Cloud API",
        "icon": "🟦",
        "fields": ["access_token", "phone_number_id", "app_secret", "verify_token"],
        "help": (
            "Connect a WhatsApp Business number directly through Meta Cloud API: "
            "access token, phone number ID, app secret and a verify token of your "
            "choice (used for the webhook handshake)."
        ),
    },
    "generic": {
        "label": "Generic webhook / provider",
        "icon": "🔗",
        "fields": ["webhook_secret", "send_url", "send_bearer_token"],
        "help": (
            "Bring your own provider: inbound messages POST JSON "
            "{message_id, text/body, from/sender, name} to the webhook URL; "
            "outbound (optional) POSTs {to, text} to send_url with a bearer token."
        ),
    },
}


def _channel_encrypt(obj: dict) -> str:
    from app.services.whatsapp_channels import encrypt_json

    return encrypt_json(obj or {})


def _channel_decrypt(token: str) -> dict:
    from app.services.whatsapp_channels import decrypt_json

    return decrypt_json(token or "")


def _channel_row_to_dict(row: Any) -> Dict[str, Any]:
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "phone_number": row.phone_number or "",
        "status": row.status or "disconnected",
        "enabled": bool(row.enabled),
        "is_primary": bool(row.is_primary),
        "last_error": row.last_error or "",
        "credentials_set": list(_channel_decrypt(row.credentials_enc).keys()),
        "webhook_url": f"{base}/voice-agent/voice/channels/{row.id}/webhook",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _seed_voice_channels() -> None:
    """First run: mirror env Twilio (if set) as a channel so existing setup shows up."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return
    with SessionLocal() as db:
        if db.query(VoiceChannel).count() > 0:
            return
        db.add(
            VoiceChannel(
                id=new_id("vch"),
                name="Env Twilio (backend/.env)",
                provider="twilio",
                phone_number=TWILIO_PHONE_NUMBER or "",
                credentials_enc=_channel_encrypt(
                    {
                        "account_sid": TWILIO_ACCOUNT_SID,
                        "auth_token": TWILIO_AUTH_TOKEN,
                        "phone_number": TWILIO_PHONE_NUMBER or "",
                    }
                ),
                status="connected",
                enabled=True,
                is_primary=True,
            )
        )
        db.commit()


def repair_undecryptable_channels() -> int:
    """Re-key Twilio channels whose stored credentials no longer decrypt.

    Channel credentials are Fernet-encrypted with a key derived from
    SARA_MASTER_KEY. If that master key is ever changed or was previously
    absent, existing rows can NEVER be read again — they decrypt to {} and the
    channel silently loses its account_sid/auth_token even though backend/.env
    holds perfectly good ones. The result is a primary channel that fails
    every operation with "account_sid and auth_token are required" and no
    inbound calls.

    So on boot we detect Twilio channels with empty credentials and re-encrypt
    them from the current env keys. Nothing is lost: the env values are the
    same source of truth used to seed the channel in the first place, and only
    Twilio channels are touched (other providers have no env equivalent, so
    they are left for the owner to re-enter in the UI).
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return 0
    repaired = 0
    with SessionLocal() as db:
        rows = db.scalars(
            select(VoiceChannel).where(VoiceChannel.provider == "twilio")
        ).all()
        for row in rows:
            creds = _channel_decrypt(row.credentials_enc)
            has_creds = bool(creds.get("account_sid") and creds.get("auth_token"))
            # A channel can decrypt fine yet still belong to a PREVIOUS Twilio
            # account after the .env account is swapped. Its old number then
            # keeps being used for outbound calls, silently dialling the old,
            # exhausted account. Re-key whenever the stored SID differs from
            # the active one — not only when it is unreadable.
            stale_account = (
                has_creds
                and TWILIO_ACCOUNT_SID
                and creds.get("account_sid") != TWILIO_ACCOUNT_SID
            )
            if has_creds and not stale_account:
                continue  # healthy — leave it alone
            if not has_creds:
                log.warning(
                    "voice channel %s has unreadable credentials — re-keying from env",
                    row.id,
                )
            else:
                log.warning(
                    "voice channel %s belongs to account %s but env is %s — re-keying",
                    row.id,
                    str(creds.get("account_sid"))[:10],
                    str(TWILIO_ACCOUNT_SID)[:10],
                )
            row.credentials_enc = _channel_encrypt(
                {
                    "account_sid": TWILIO_ACCOUNT_SID,
                    "auth_token": TWILIO_AUTH_TOKEN,
                    "phone_number": TWILIO_PHONE_NUMBER or row.phone_number or "",
                }
            )
            if TWILIO_PHONE_NUMBER:
                row.phone_number = TWILIO_PHONE_NUMBER
            # A row flagged "error" purely because of the key rotation must
            # stop reporting that error once it can actually be used.
            if (row.last_error or "").startswith(
                ("account_sid and auth_token",)
            ):
                row.last_error = ""
            if row.status == "error":
                row.status = "connected"
            repaired += 1
        if repaired:
            db.commit()
            log.info("re-keyed %d Twilio voice channel(s) from env", repaired)
    return repaired


def _primary_voice_channel() -> tuple[Optional[Any], dict]:
    """(row, creds) of the enabled primary channel, or (None, {})."""
    with SessionLocal() as db:
        row = db.scalar(
            select(VoiceChannel)
            .where(VoiceChannel.is_primary == True, VoiceChannel.enabled == True)  # noqa: E712
            .order_by(VoiceChannel.created_at)
            .limit(1)
        )
        if row is None:
            return None, {}
        return row, _channel_decrypt(row.credentials_enc)


def _voice_channel_webhook_row(channel_id: str) -> Any:
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        return row


# ---------------------------------------------------------------------------
# Twilio telephony adapter — env keys first, runtime primary channel wins.
# ---------------------------------------------------------------------------

def _twilio_creds() -> tuple[str, str, str]:
    """(sid, token, from_number) — primary runtime channel beats env.

    A channel whose credentials cannot be decrypted (e.g. SARA_MASTER_KEY was
    rotated) yields no sid/token and MUST NOT shadow the working env keys, or
    the whole telephony path goes dark while .env looks perfectly correct.
    """
    row, creds = _primary_voice_channel()
    if row and row.provider == "twilio":
        sid = creds.get("account_sid") or ""
        token = creds.get("auth_token") or ""
        number = creds.get("phone_number") or row.phone_number or ""
        if sid and token:
            return sid, token, number
        # Unreadable/partial channel: fall back to env, keeping the channel's
        # own number so the identity stays consistent with the UI.
        return (
            TWILIO_ACCOUNT_SID or "",
            TWILIO_AUTH_TOKEN or "",
            number or TWILIO_PHONE_NUMBER or "",
        )
    return TWILIO_ACCOUNT_SID or "", TWILIO_AUTH_TOKEN or "", TWILIO_PHONE_NUMBER or ""


_OAUTH_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires": 0.0}


def _twilio_oauth_token() -> Optional[str]:
    """Fetch (and cache) a Twilio OAuth access token via client credentials.

    Preferred over the auth token when TWILIO_CLIENT_ID/SECRET are configured,
    because it can be scoped and revoked without exposing the account auth
    token. Returns None to fall back to the auth token.
    """
    client_id = _settings.TWILIO_CLIENT_ID
    client_secret = _settings.TWILIO_CLIENT_SECRET
    if not client_id or not client_secret:
        return None

    now = time.time()
    cached = _OAUTH_TOKEN_CACHE.get("token")
    if cached and now < float(_OAUTH_TOKEN_CACHE.get("expires") or 0) - 60:
        return cached  # refresh 60 s early

    try:
        import httpx

        resp = httpx.post(
            # Twilio's client-credentials grant lives on the v2 token endpoint.
            "https://oauth.twilio.com/v2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if token:
            _OAUTH_TOKEN_CACHE["token"] = token
            _OAUTH_TOKEN_CACHE["expires"] = now + float(payload.get("expires_in") or 3600)
            log.info("Twilio OAuth token acquired (client_id=%s...)", str(client_id)[:6])
            return token
        log.warning("Twilio OAuth returned no access_token")
    except Exception as exc:
        # Never log the secret itself.
        log.warning("Twilio OAuth token fetch failed: %s", str(exc)[:150])
    return None


def _twilio_client():
    """Lazily built client for whatever creds are active (env or runtime channel).

    Auth order: runtime channel -> Twilio OAuth -> account auth token.
    """
    row, creds = _primary_voice_channel()
    if row and row.provider == "twilio":
        sid = creds.get("account_sid") or ""
        token = creds.get("auth_token") or ""
        if sid and token:
            try:
                from twilio.rest import Client as _TwilioClient

                return _TwilioClient(sid, token)
            except Exception as exc:
                log.warning("Twilio client init failed: %s", exc)
                return None

    sid, token, _ = _twilio_creds()
    if not sid:
        return None
    try:
        from twilio.rest import Client as _TwilioClient

        # Prefer OAuth, but only when it is actually valid for THIS account.
        # An OAuth app can belong to a different subaccount than the account
        # SID it is paired with, and Twilio then rejects the call with a 401
        # ("auth token is not valid for account ..."). Probing once and falling
        # back to the account auth token keeps outbound calls working instead
        # of failing outright whenever the two are mismatched.
        oauth = _twilio_oauth_token()
        # Remember a rejected OAuth credential so we stop re-probing on every
        # call and simply use the auth token from then on.
        if oauth and not _OAUTH_TOKEN_CACHE.get("rejected"):
            try:
                _TwilioClient(sid, oauth).api.accounts(sid).fetch()
                return _TwilioClient(sid, oauth)
            except Exception as exc:
                _OAUTH_TOKEN_CACHE["rejected"] = True
                log.warning(
                    "Twilio OAuth not valid for this account (%s) — using auth token",
                    str(exc)[:120],
                )
        if not token:
            return None
        return _TwilioClient(sid, token)
    except Exception as exc:
        log.warning("Twilio client init failed: %s", exc)
        return None


class TwilioTelephonyProvider:
    name = "twilio"

    def capabilities(self) -> Dict[str, bool]:
        _, _, number = _twilio_creds()
        streaming = bool(PUBLIC_BASE_URL)   # Twilio must reach us over wss://
        return {
            "inbound_calls": bool(number),
            "outbound_calls": _twilio_client() is not None and bool(number),
            # The media-stream WebSocket now exists and is the preferred path;
            # it needs a public URL (ngrok/tunnel) to be reachable.
            "websocket_streaming": streaming,
            "bidirectional_audio": streaming,
            "recording": False,
            # TwilioTelephonyProvider.transfer() really re-targets the live leg.
            "transfer": _twilio_client() is not None and bool(number),
            "twiml_fallback": True,
        }

    def place_call(
        self,
        *,
        to: str,
        business_id: str,
        call_id: str,
        objective: str,
    ) -> Dict[str, Any]:
        client = _twilio_client()
        sid, _, number = _twilio_creds()
        if client is None or not number:
            return {
                "success": False,
                "configured": True,
                "reason": "Twilio unavailable — connect a Twilio channel in the UI or set TWILIO_* keys.",
            }
        base = PUBLIC_BASE_URL or "http://localhost:8000"
        try:
            call = client.calls.create(
                to=to,
                from_=number,
                url=f"{base}/voice-agent/voice/twilio/outbound-answer?call_id={call_id}",
                # Without a status callback Twilio never reports ringing /
                # completed / failed, so the Call row could never leave
                # "initiated" and the UI showed every outbound call as stuck.
                # Only attach it when we have a public URL Twilio can reach.
                **(
                    {
                        "status_callback": (
                            f"{base}/voice-agent/voice/twilio/status-callback"
                            "?call_id=" + str(call_id)
                        ),
                        "status_callback_method": "POST",
                    }
                    if PUBLIC_BASE_URL
                    else {}
                ),
            )
            return {
                "success": True,
                "configured": True,
                "provider": self.name,
                "provider_call_id": call.sid,
                "account_sid": sid,
            }
        except Exception as exc:
            return {"success": False, "configured": True, "reason": str(exc)[:250]}

    def answer_call(self, **kwargs: Any) -> Dict[str, Any]:
        return {"success": False, "configured": True, "reason": "Use inbound webhook"}

    def hangup(self, *, provider_call_id: str) -> Dict[str, Any]:
        client = _twilio_client()
        if client is None:
            return {"success": False, "reason": "Twilio client unavailable"}
        try:
            client.calls(provider_call_id).update(status="completed")
            return {"success": True}
        except Exception as exc:
            return {"success": False, "reason": str(exc)[:200]}

    def transfer(self, **kwargs: Any) -> Dict[str, Any]:
        """Hand the live call leg to the receptionist via Twilio's API.

        Previously this returned "not implemented", so the booking engine always
        saw transfer_failed and the <Dial> TwiML was never reached. Twilio's
        `update(url=...)` on an in-progress call re-targets the leg with
        TwiML, which is a live bridge, not a hang-up-and-redial.
        """
        provider_call_id = str(kwargs.get("provider_call_id") or "").strip()
        destination = str(kwargs.get("destination") or "").strip()
        if not provider_call_id:
            return {"success": False, "reason": "no_live_telephony_leg"}
        if not destination:
            return {"success": False, "reason": "no_destination_configured"}
        client = _twilio_client()
        if client is None:
            return {"success": False, "reason": "Twilio client unavailable"}
        try:
            client.calls(provider_call_id).update(
                twiml=(
                    "<Response><Dial timeout='30' answerOnBridge='true'>"
                    f"<Number>{destination}</Number></Dial></Response>"
                )
            )
            return {
                "success": True,
                "provider": self.name,
                "destination": destination,
            }
        except Exception as exc:
            # Twilio rejects a redirect once the leg is no longer in progress
            # ("Call is not in-progress. Cannot redirect."). That is a timing
            # condition, not a config error, so name it explicitly — otherwise
            # the reason string is an opaque 400 and nobody can tell a working
            # transfer from a dead one.
            detail = str(exc)[:200]
            reason = (
                "call_leg_not_in_progress"
                if "not in-progress" in detail.lower()
                or "not in progress" in detail.lower()
                else "provider_refused_transfer"
            )
            log.warning(
                "transfer failed leg=%s dest=%s reason=%s (%s)",
                provider_call_id, destination, reason, detail,
            )
            return {
                "success": False,
                "provider": self.name,
                "reason": reason,
                "detail": detail,
            }


# ---------------------------------------------------------------------------
# Messaging provider — SMS/WhatsApp through the primary runtime channel.
# ---------------------------------------------------------------------------

class ChannelMessagingProvider:
    """Sends SMS (Twilio) / WhatsApp (Twilio or Meta) via the primary channel.

    Registered as the engine's messaging_provider so /voice/messages/send and
    the voice loop's follow-ups route through whichever channel the owner
    connected — with a clear 'not configured' result when nothing is active.
    """

    name = "voice-channel"

    def can_send(self, channel: MessageChannel, phone: str) -> bool:
        row, creds = _primary_voice_channel()
        if row is None or not row.enabled:
            return False
        if row.provider == "twilio":
            return bool(creds.get("account_sid") and creds.get("auth_token") and (row.phone_number or creds.get("phone_number")))
        if row.provider == "meta":
            return channel == MessageChannel.WHATSAPP and bool(creds.get("access_token") and creds.get("phone_number_id"))
        return False

    def send(
        self,
        *,
        channel: MessageChannel,
        phone: str,
        body: str,
        business_id: str,
        customer_id: str,
    ) -> Dict[str, Any]:
        row, creds = _primary_voice_channel()
        if row is None:
            return {
                "success": False,
                "configured": False,
                "reason": "No voice channel connected — add one in the Connect tab.",
            }
        try:
            if row.provider == "twilio":
                sid = creds.get("account_sid") or ""
                token = creds.get("auth_token") or ""
                number = creds.get("phone_number") or row.phone_number or ""
                if not (sid and token and number):
                    return {"success": False, "configured": True, "reason": "Twilio channel missing credentials."}
                if channel == MessageChannel.WHATSAPP:
                    from_ = f"whatsapp:{number}" if not number.startswith("whatsapp:") else number
                    to = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
                else:
                    from_, to = number, phone
                with httpx.Client(timeout=30) as client:
                    r = client.post(
                        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                        data={"From": from_, "To": to, "Body": body},
                        auth=(sid, token),
                    )
                    r.raise_for_status()
                    data = r.json()
                return {
                    "success": True,
                    "configured": True,
                    "provider": "twilio",
                    "provider_message_id": data.get("sid"),
                    "channel": channel.value,
                }
            if row.provider == "meta" and channel == MessageChannel.WHATSAPP:
                token = creds.get("access_token") or ""
                phone_number_id = creds.get("phone_number_id") or ""
                if not (token and phone_number_id):
                    return {"success": False, "configured": True, "reason": "Meta channel missing credentials."}
                with httpx.Client(timeout=30) as client:
                    r = client.post(
                        f"https://graph.facebook.com/v21.0/{phone_number_id}/messages",
                        params={"access_token": token},
                        json={
                            "messaging_product": "whatsapp",
                            "recipient_type": "individual",
                            "to": phone,
                            "type": "text",
                            "text": {"body": body, "preview_url": False},
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                return {
                    "success": True,
                    "configured": True,
                    "provider": "meta",
                    "provider_message_id": (data.get("messages") or [{}])[0].get("id"),
                    "channel": channel.value,
                }
            return {
                "success": False,
                "configured": True,
                "reason": f"Provider '{row.provider}' cannot send {channel.value}.",
            }
        except Exception as exc:
            log.warning("Voice channel send failed: %s", exc)
            return {"success": False, "configured": True, "reason": str(exc)[:250]}


_channel_messaging = ChannelMessagingProvider()
register_messaging_provider(_channel_messaging)
register_telephony_provider(TwilioTelephonyProvider())


def _ensure_voice_schema() -> None:
    """Add columns a newer engine needs to an EXISTING voiceagent.db.

    ``create_all`` only creates missing TABLES — it never adds a column to a
    table that already exists. The campaign agent needs two extra columns on
    ``voice_calls`` (the agent profile and the snapshot the call started
    with), so they are added here, idempotently, and a failure is logged
    rather than allowed to stop the engine from booting.
    """
    from sqlalchemy import inspect, text

    wanted = (
        ("voice_calls", "agent_profile_id", "VARCHAR(80)"),
        ("voice_calls", "agent_profile_snapshot", "JSON"),
    )
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            for table, column, ddl in wanted:
                if table not in tables:
                    continue
                existing = {c["name"] for c in inspector.get_columns(table)}
                if column in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                log.info("voice schema: added %s.%s", table, column)
    except Exception as exc:  # noqa: BLE001 - never block engine startup
        log.warning("voice schema migration skipped: %s", exc)


# The engine owns its sqlite tables; ensure they exist before seeding channels.
Base.metadata.create_all(bind=engine)
_ensure_voice_schema()
_seed_voice_channels()


# ---------------------------------------------------------------------------
# Persona + business context (runtime-editable, injected into every voice turn)
# ---------------------------------------------------------------------------
VOICE_PERSONA_FILE = Path(__file__).resolve().parent.parent / "voice_persona.json"
VOICE_BUSINESS_FILE = Path(__file__).resolve().parent.parent / "voice_business_context.json"

DEFAULT_VOICE_PERSONA = os.getenv(
    "VOICE_AGENT_PERSONA",
    (
        "You are SARA, a warm and efficient voice agent. You help callers and "
        "message senders with the business's services, prices, availability and "
        "bookings. Read <business_context> for exact services, prices and "
        "policies — never invent data. Sound natural when spoken aloud."
    ),
)


def voice_persona_load() -> str:
    try:
        data = json.loads(VOICE_PERSONA_FILE.read_text(encoding="utf-8"))
        persona = (data or {}).get("persona") or ""
        if persona.strip():
            return persona.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return DEFAULT_VOICE_PERSONA


def voice_persona_save(persona: str) -> str:
    persona = (persona or "").strip()
    if not persona:
        raise HTTPException(status_code=422, detail="persona cannot be empty")
    VOICE_PERSONA_FILE.write_text(
        json.dumps({"persona": persona}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return persona


def voice_persona_reset() -> str:
    try:
        VOICE_PERSONA_FILE.unlink()
    except FileNotFoundError:
        pass
    return DEFAULT_VOICE_PERSONA


def voice_business_load() -> str:
    """Hot-reloads the business context JSON (services, prices, policies)."""
    try:
        data = json.loads(VOICE_BUSINESS_FILE.read_text(encoding="utf-8"))
        return json.dumps(data, indent=2, ensure_ascii=False)
    except FileNotFoundError:
        return "{}"
    except Exception:
        return "{}"


def voice_business_save(data: dict) -> str:
    VOICE_BUSINESS_FILE.write_text(
        json.dumps(data or {}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return voice_business_load()

# ---------------------------------------------------------------------------
# Booking engine — persona, demo-hotel availability, transfer (real Twilio)
# ---------------------------------------------------------------------------

from app.services.voice_booking_models import (  # noqa: E402
    AvailabilityResult,
    BookingContext,
    BookingPhase,
    DemoHotelAvailability,
    PersonaConfig,
    load_persona as _persona_load,
    save_persona as _persona_save,
)
from app.services.voice_booking import (  # noqa: E402
    BookingConversationEngine,
    TurnResult,
    is_provider_informational,
)

VOICE_PERSONA_CONFIG_FILE = Path(__file__).resolve().parent.parent / "voice_hotel_persona.json"
VOICE_BOOKING_MODE_KEY = "voice_booking_mode"


def booking_mode_enabled(db: Session, business_id: str) -> bool:
    """Booking state-machine mode on by default; owners can flip it per business."""
    return bool(get_setting(db, business_id, VOICE_BOOKING_MODE_KEY, True))


def set_booking_mode(db: Session, business_id: str, enabled: bool) -> None:
    set_setting(db, business_id, VOICE_BOOKING_MODE_KEY, bool(enabled))


async def _booking_llm(system: str, user: str, context: Dict[str, Any]) -> str:
    """Booking engine's LLM adapter — shared failover router, spoken brevity."""
    if not _llm_configured():
        raise RuntimeError("LLM not configured")
    t0 = time.monotonic()
    reply = await _failover_chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=LLM_MODEL,
        max_tokens=160,
        temperature=0.4,
        timeout=30,
    )
    dt = time.monotonic() - t0
    if dt > 3.0:
        log.warning("booking LLM slow: %.1fs (model=%s) — consider a faster model", dt, LLM_MODEL)
    else:
        log.info("booking LLM ok: %.1fs model=%s", dt, LLM_MODEL)
    return reply


def _call_id_for_session(session_key: str) -> Optional[str]:
    """Our Call id for a booking session key ('call:<id>' / 'twilio:<sid>')."""
    if session_key.startswith("call:"):
        return session_key[5:]
    if session_key.startswith("twilio:"):
        sid = session_key[7:]
        with SessionLocal() as db:
            call = db.scalar(select(Call).where(Call.provider_call_id == sid))
            return call.id if call else None
    return None


async def _voice_transfer(request: Dict[str, Any]) -> Dict[str, Any]:
    """Receptionist transfer through the REAL telephony provider.

    For live Twilio calls the provider redirects the call leg to a <Dial> of
    the receptionist number. There is deliberately NO fake success: a session
    without a live telephony leg (browser voice demo) returns success=False
    with an honest reason, and the engine falls back to taking the booking
    directly — which then really writes to the demo data source.
    """
    session_key = request.get("session_key") or ""
    destination = request.get("destination") or ""
    call_id = request.get("call_id") or _call_id_for_session(session_key)

    # The lookup, the state transition and BOTH event writes must share one
    # session. Committing inside the `with` and then reusing `db` afterwards
    # raised ResourceClosedError, which surfaced to the booking engine as a
    # failed transfer — i.e. the receptionist hand-off never worked.
    with SessionLocal() as db:
        db_call = None
        if call_id:
            db_call = db.get(Call, call_id)
        if db_call is None and session_key.startswith("twilio:"):
            db_call = db.scalar(
                select(Call).where(
                    Call.provider_call_id == session_key[7:]
                )
            )
        provider_call_id = db_call.provider_call_id if db_call is not None else None
        business_id = db_call.business_id if db_call is not None else "biz_demo"
        resolved_call_id = db_call.id if db_call is not None else call_id

        if db_call is not None:
            try:
                transition_call(db, db_call, CallState.HUMAN_HANDOFF)
            except ValueError:
                log.warning(
                    "call %s in state %s could not move to HUMAN_HANDOFF",
                    db_call.id, db_call.state,
                )
            record_event(
                db,
                business_id=business_id,
                call_id=resolved_call_id,
                event_type="VOICE_TRANSFER_REQUESTED",
                source="voice_booking",
                actor="system",
                payload={"session_key": session_key, "destination": destination},
            )
            db.commit()

    if not provider_call_id:
        log.info(
            "transfer skipped session=%s — no live telephony leg (web voice)",
            session_key,
        )
        return {
            "success": False,
            "reason": "no_live_telephony_leg",
            "detail": "This session has no live call leg to transfer.",
        }

    result = telephony_provider.transfer(
        provider_call_id=provider_call_id, destination=destination
    )
    with SessionLocal() as db:
        record_event(
            db,
            business_id=business_id,
            call_id=resolved_call_id,
            event_type="VOICE_TRANSFER_RESULT",
            source="voice_booking",
            actor="provider",
            payload={
                "success": bool(result.get("success")),
                "reason": result.get("reason", ""),
                "provider": result.get("provider", ""),
                "destination": destination,
            },
        )
        db.commit()
    if not result.get("success"):
        result.setdefault("reason", "provider refused the transfer")
    return result


async def _booking_translate(text: str, source: str, target: str) -> str:
    """Real Sarvam translate (mayura) for multilingual booking conversations."""
    if not text or not text.strip():
        return text
    if not sarvam_client.configured:
        raise RuntimeError("SARVAM_API_KEY not configured")
    return await sarvam_client.translate(
        text,
        source_language_code=source,
        target_language_code=target,
    )


def _build_booking_engine() -> BookingConversationEngine:
    persona = _persona_load(VOICE_PERSONA_CONFIG_FILE)
    if not persona.receptionist_number:
        persona.receptionist_number = demo_hotel.receptionist_number
    return BookingConversationEngine(
        availability=demo_hotel,
        persona=persona,
        llm_fn=_booking_llm,
        transfer_fn=_voice_transfer,
        translator_fn=_booking_translate,
        timezone_name=demo_hotel.hotel.get("timezone")
        or persona.business_hours.get("timezone", "Asia/Kolkata"),
    )


def booking_persona() -> PersonaConfig:
    """Reload the persona file each call so UI edits apply instantly."""
    persona = _persona_load(VOICE_PERSONA_CONFIG_FILE)
    if not persona.receptionist_number:
        persona.receptionist_number = demo_hotel.receptionist_number
    booking_engine.persona = persona
    booking_engine.timezone_name = (
        demo_hotel.hotel.get("timezone")
        or persona.business_hours.get("timezone", "Asia/Kolkata")
    )
    return persona


demo_hotel = DemoHotelAvailability()
booking_engine = _build_booking_engine()


# ---------------------------------------------------------------------------
# Dynamic Election Campaign Voice Agent
#
# The campaign agent reuses everything the booking agent already has: the
# Twilio provider, the call state machine, the webhooks, the Sarvam
# STT/TTS/translate stack, the language detector and the failover LLM
# router. What it adds is a saved, editable campaign profile per tenant and
# a campaign-shaped conversation flow.
#
# Routing is PER CALL: a call speaks with the campaign agent only when the
# Call row carries an ``agent_profile_id``. Every other call keeps using the
# original booking agent, untouched.
# ---------------------------------------------------------------------------

CAMPAIGN_PROFILE_STATUS_PUBLISHED = "PUBLISHED"
CAMPAIGN_PROFILE_STATUS_DRAFT = "DRAFT"


def _profile_payload_to_model(payload: Optional[dict]) -> CampaignProfile:
    """Build a validated profile, falling back to the shipped demo values."""
    data = dict(payload or {})
    try:
        return CampaignProfile(**data)
    except Exception as exc:  # noqa: BLE001 - a bad row must not break a call
        log.warning("campaign profile invalid, using defaults: %s", exc)
        return DEFAULT_CAMPAIGN_PROFILE.model_copy(deep=True)


def campaign_profile_to_dict(
    row: Optional[CampaignProfileRow],
    *,
    include_draft: bool = True,
) -> Dict[str, Any]:
    """Serialise a saved profile for the API."""
    if row is None:
        profile = DEFAULT_CAMPAIGN_PROFILE
        return {
            "id": None,
            "name": profile.agent_name,
            "status": CAMPAIGN_PROFILE_STATUS_DRAFT,
            "version": 0,
            "agent_kind": CAMPAIGN_AGENT_KIND,
            "profile": profile.model_dump(),
            "draft": profile.model_dump(),
            "published_profile": profile.model_dump(),
            "created_at": None,
            "updated_at": None,
            "published_at": None,
        }

    published = dict(row.profile or {})
    draft = dict(row.draft or published)
    payload: Dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "version": row.version,
        "agent_kind": row.agent_kind,
        "profile": published,
        "published_profile": published,
        "published_at": json_safe(row.published_at),
        "created_at": json_safe(row.created_at),
        "updated_at": json_safe(row.updated_at),
    }
    if include_draft:
        payload["draft"] = draft
    return payload


def list_campaign_profiles(db: Session, business_id: str) -> List[CampaignProfileRow]:
    return list(
        db.scalars(
            select(CampaignProfileRow)
            .where(CampaignProfileRow.business_id == business_id)
            .order_by(CampaignProfileRow.created_at.asc())
        ).all()
    )


def get_campaign_profile_row(
    db: Session, profile_id: str, business_id: str
) -> Optional[CampaignProfileRow]:
    """Fetch a profile, scoped to its owner. Returns None for another tenant."""
    return db.scalar(
        select(CampaignProfileRow).where(
            CampaignProfileRow.id == profile_id,
            CampaignProfileRow.business_id == business_id,
        )
    )


def published_campaign_profile(
    db: Session, profile_id: str, business_id: str
) -> Optional[CampaignProfileRow]:
    row = get_campaign_profile_row(db, profile_id, business_id)
    if row is None:
        return None
    if row.status != CAMPAIGN_PROFILE_STATUS_PUBLISHED:
        return None
    return row


def _campaign_profile_for_call(
    call_sid: str = "", call_id: str = ""
) -> Optional[CampaignProfile]:
    """The campaign profile a live call speaks with, if it is a campaign call.

    The snapshot stored on the Call row wins, so a configuration published
    while the call is running never changes what the caller is hearing.
    """
    try:
        with SessionLocal() as db:
            call = None
            if call_id:
                call = db.get(Call, call_id)
            if call is None and call_sid:
                call = db.scalar(
                    select(Call).where(Call.provider_call_id == call_sid)
                )
            if call is None or not call.agent_profile_id:
                return None
            snapshot = call.agent_profile_snapshot
            if snapshot:
                return _profile_payload_to_model(snapshot)
            row = get_campaign_profile_row(
                db, call.agent_profile_id, call.business_id
            )
            if row is None:
                return None
            return _profile_payload_to_model(row.profile)
    except Exception as exc:  # noqa: BLE001 - never break a live call
        log.warning("campaign profile lookup failed: %s", exc)
        return None


def _campaign_profile_for_session(session_key: str) -> Optional[CampaignProfile]:
    """Profile for a live session key ('twilio:<CallSid>' / 'call:<id>')."""
    if not session_key:
        return None
    if session_key.startswith("twilio:"):
        return _campaign_profile_for_call(call_sid=session_key[7:])
    if session_key.startswith("call:"):
        return _campaign_profile_for_call(call_id=session_key[5:])
    return None


def _build_campaign_engine() -> CampaignConversationEngine:
    return CampaignConversationEngine(
        profile_fn=_campaign_profile_for_session,
        llm_fn=_booking_llm,
        transfer_fn=_voice_transfer,
        translator_fn=_booking_translate,
    )


campaign_engine = _build_campaign_engine()


def _voice_engine_for_profile(
    profile: Optional[CampaignProfile],
):
    """The conversation engine that must serve this call."""
    return campaign_engine if profile is not None else booking_engine


def _voice_context_for_profile(
    session_key: str, profile: Optional[CampaignProfile]
):
    """The session context for this call, from the right engine."""
    engine = _voice_engine_for_profile(profile)
    ctx = engine.get_context(session_key)
    if profile is not None and ctx.language in (None, "", "en-IN"):
        ctx.language = profile.language or ctx.language
    return ctx


def _voice_context_for_call_sid(call_sid: str):
    """The session context for a live Twilio leg (campaign or booking)."""
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"
    campaign = _campaign_profile_for_call(call_sid=call_sid)
    return _voice_context_for_profile(session_key, campaign)


def _tts_sarvam_language(language: Optional[str]) -> str:
    """Map browser/UI language hints onto Sarvam TTS language codes."""
    if not language:
        return SARVAM_TTS_LANGUAGE or "en-IN"
    lang = language.lower()
    if lang.startswith("en"):
        return "en-IN"
    if "-" in language:
        return language
    return f"{language}-IN"


from app.core.agent_switch import is_running, set_running  # noqa: E402


class TalkRequest(BaseModel):
    business_id: str = "biz_demo"
    text: str
    caller_phone: Optional[str] = None
    language: str = DEFAULT_LANGUAGE
    speak: bool = True
    session_key: Optional[str] = None
    call_id: Optional[str] = None


@app.get("/voice/status")
async def voice_status() -> Dict[str, Any]:
    """Live configuration snapshot for the Command Center health chips."""
    persona = booking_persona()
    return {
        "status": "ok",
        "running": is_running("voice"),
        "booking_state_machine": {
            "is_default_persona": persona.is_default,
            "persona": persona.name,
            "business_name": persona.business_name,
            "receptionist_number": bool(persona.receptionist_number),
            "demo_hotel_rooms": len(demo_hotel._data.get("rooms", [])),
        },
        "llm": {
            "provider": _settings.GEMINI_MODEL if _settings.GEMINI_API_KEY else LLM_MODEL,
            "provider_chain": "gemini -> configured failover",
            "configured": _llm_configured(),
        },
        "tts": {"provider": VOICE_TTS_PROVIDER, "configured": _tts_configured()},
        "stt": {"provider": VOICE_STT_PROVIDER, "configured": _stt_configured()},
        "telephony": {
            "provider": telephony_provider.name,
            "capabilities": telephony_provider.capabilities(),
            "media_stream_ws_url": _media_stream_url(),
            "inbound_answer_url": (
                f"{PUBLIC_BASE_URL}/voice-agent/voice/twilio/inbound"
                if PUBLIC_BASE_URL else ""
            ),
        },
    }


@app.post("/voice/run")
async def voice_run() -> Dict[str, Any]:
    return set_running("voice", True)


@app.post("/voice/pause")
async def voice_pause() -> Dict[str, Any]:
    return set_running("voice", False)


@app.post("/voice/talk")
async def voice_talk(req: TalkRequest) -> Dict[str, Any]:
    """Live loop: text in -> SARA reply out (+ TTS audio url when configured).

    Booking state-machine mode is the default for the web phone; every turn is
    language-detected so mid-call switches follow the caller instantly.
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    if not is_running("voice"):
        raise HTTPException(status_code=409, detail="Voice agent is paused")

    session_key = req.session_key or f"web:{req.caller_phone or 'anon'}"
    persona = booking_persona()

    with SessionLocal() as db:
        use_booking = booking_mode_enabled(db, req.business_id)

    booking_turn = None
    ctx_lang = None
    if use_booking:
        # Per-turn language detection (explicit switch > script > keep) so
        # start-of-call detection and mid-call switches both land here.
        pre_ctx = booking_engine.get_context(session_key)
        decision = detect_language(text, current=pre_ctx.language)
        if decision.changed:
            pre_ctx.language = decision.language
            log.info(
                "language switch session=%s %s -> %s (%s, conf=%.2f)",
                session_key, decision.previous, decision.language,
                decision.source, decision.confidence,
            )
        pre_ctx.language_detected = decision.to_dict()
        ctx_lang = pre_ctx.language

    if use_booking:
        result = await booking_engine.handle_turn(
            session_key, text, call_id=req.call_id, caller_phone=req.caller_phone
        )
        booking_turn = {
            "reply": result.reply,
            "phase": result.phase.value,
            "handled": bool(result.reply),
            "action": result.action,
            "events": result.events,
        }
        if not result.reply:
            # Provider informational/trial message: stay up, stay silent, keep listening.
            return {
                "reply": "",
                "intent": "provider_informational",
                "phase": result.phase.value,
                "state": result.context.to_dict(),
                "audio_url": None,
                "audio_error": None,
                "llm_configured": _llm_configured(),
                "booking": True,
            }

    reply = booking_turn["reply"]
    phase = booking_turn["phase"]

    with SessionLocal() as db:
        record_event(
            db,
            business_id=req.business_id,
            call_id=req.call_id,
            event_type="VOICE_BOOKING_TURN",
            source="voice_call",
            actor="system",
            payload={
                "caller": text[:220],
                "reply": reply[:220],
                "phase": phase,
                "session_key": session_key,
            },
        )
        db.commit()

    audio: Dict[str, Any] = {}
    if req.speak and reply:
        audio = await tts_speak(reply, language=ctx_lang or req.language)

    ctx = booking_engine.get_context(session_key)
    return {
        "reply": reply,
        "intent": "booking_turn",
        "phase": phase,
        "state": ctx.to_dict(),
        "audio_url": audio.get("url"),
        "audio_error": audio.get("error"),
        "llm_configured": _llm_configured(),
        "booking": True,
        "action": booking_turn.get("action"),
    }


class TranscriptRequest(BaseModel):
    business_id: str = "biz_demo"
    audio_base64: str
    language: str = "unknown"


@app.post("/voice/transcribe")
async def voice_transcribe(req: TranscriptRequest) -> Dict[str, Any]:
    """STT for call recordings / uploaded audio clips."""
    try:
        audio = _b64.b64decode(req.audio_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 audio")
    result = await stt_transcribe(audio, language=req.language)
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result


@app.get("/voice/twilio/media-stream-url")
async def voice_media_stream_url() -> Dict[str, Any]:
    """Diagnostics: the exact public URLs the voice agent answers on.

    Use `media_stream_ws_url` for the Twilio number's **"A call comes in"**
    voice URL so the call lands on the full-duplex socket, and
    `inbound_answer_url` for the same thing as an HTTP webhook.
    """
    base = PUBLIC_BASE_URL or ""
    return {
        "status": "ok",
        "public_base_url": base,
        "media_stream_ws_url": _media_stream_url(),
        "inbound_answer_url": f"{base}/voice-agent/voice/twilio/inbound" if base else "",
        "outbound_answer_url": f"{base}/voice-agent/voice/twilio/outbound-answer" if base else "",
        "transfer_status_url": f"{base}/voice-agent/voice/twilio/transfer-status" if base else "",
        "streaming_enabled": bool(base),
        "note": (
            "Twilio needs a public wss:// URL (ngrok/tunnel). Without one the "
            "agent falls back to the TwiML <Gather> loop, which still works "
            "but is half-duplex."
        ),
    }


def _twilio_signature_valid(request: Request, form) -> bool:
    """Verify Twilio's X-Twilio-Signature on a webhook request.

    Returns True when validation cannot be performed (no public URL, no
    signature, or no auth token) so local and console testing still works,
    but a genuine, signed, publicly-reachable request IS verified. Enabling
    this stops anyone who learns the webhook URL from placing calls or
    injecting transcript text into a live conversation.
    """
    if not PUBLIC_BASE_URL or not TWILIO_AUTH_TOKEN:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return True
    try:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        # request.url is a yarl URL, NOT a str; the validator calls .decode()
        # on it, so passing it directly raises and used to fall through to
        # "allow" — which silently disabled this check entirely.
        url = str(request.url)
        params = {k: v for k, v in form.items()}
        if validator.validate(url, params, signature):
            return True
        log.warning("Twilio signature mismatch on %s", url)
        return False
    except Exception as exc:
        # A verification ERROR must not silently open the door. Fail closed.
        log.warning("Twilio signature check errored: %s", str(exc)[:120])
        return False


def _register_inbound_call(call_sid: str, from_number: str) -> Optional[str]:
    """Record an INBOUND Twilio call so a live telephony leg exists.

    Transfer redirects the leg via Twilio's `calls(sid).update(twiml=<Dial>)`,
    and that API rejects the call with "Call is not in-progress" unless it can
    resolve a real, in-progress call. Inbound calls previously created only a
    Customer row and no Call row at all, so `_voice_transfer` had no
    `provider_call_id` to work with and the receptionist hand-off never
    happened on inbound calls.

    Returns the Call id, or None if the row could not be written (the call
    still proceeds — a missing analytics row must never break a live call).
    """
    if not call_sid:
        return None
    try:
        with SessionLocal() as db:
            call = db.scalar(
                select(Call).where(Call.provider_call_id == call_sid)
            )
            if call is None:
                customer = None
                if from_number:
                    customer = get_customer(db, "biz_demo", phone=from_number)
                call = Call(
                    id=new_id("call"),
                    business_id="biz_demo",
                    customer_id=getattr(customer, "id", None),
                    provider_call_id=call_sid,
                    direction="INBOUND",
                    state=CallState.CONNECTED.value,
                    objective="inbound voice call",
                    trace_id=new_id("trc"),
                    conversation_id=new_id("cnv"),
                    started_at=utcnow(),
                )
                db.add(call)
            else:
                # The leg is live: make sure it reflects a connected call so a
                # transfer can act on it.
                if call.state in (
                    CallState.CREATED.value,
                    CallState.DIALING.value,
                    CallState.RINGING.value,
                ):
                    call.state = CallState.CONNECTED.value
                call.started_at = call.started_at or utcnow()
            db.commit()
            return call.id
    except Exception as exc:
        log.warning("could not register inbound call %s: %s", call_sid, str(exc)[:150])
        return None


@app.post("/voice/twilio/inbound")
async def twilio_inbound_call(request: Request) -> Response:
    """The default "A call comes in" voice URL for the SARA Twilio number.

    Answers with a cached greeting + <Gather>. The greeting is served from the
    warm phrase cache and NEVER waits on TTS, so this webhook returns in
    milliseconds. That is deliberate: Twilio abandons a webhook that takes
    longer than 15 s and plays "an application error has occurred" to the
    caller, so anything slow (warm-up, DB, TTS) is deliberately kept OFF this
    path and done in the background instead.
    """
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    form = await request.form()
    from_number = str(form.get("From") or "")
    call_sid = str(form.get("CallSid") or "")

    # Fire-and-forget: the background cache fill must never delay the answer.
    _schedule_phrase_warmup()

    # Register the live leg so a receptionist transfer can act on it later.
    _register_inbound_call(call_sid, from_number)

    if from_number:
        with contextlib.suppress(Exception):
            with SessionLocal() as db:
                customer = get_customer(db, "biz_demo", phone=from_number)
                if customer is None:
                    customer = upsert_customer(
                        db,
                        CustomerInput(business_id="biz_demo", phone=from_number),
                    )
                db.commit()

    connect = _twiml_connect_stream()
    if connect:
        return Response(
            content=f"<Response>{connect}</Response>", media_type="application/xml"
        )

    # No public URL: greet from the warm cache and run the TwiML loop.
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"
    ctx = booking_engine.get_context(session_key)
    ctx.phase = BookingPhase.GREETING.value
    # The greeting is spoken in the TwiML below — mark it delivered so the
    # conversation engine never introduces the receptionist a second time.
    ctx.greeting_completed = True
    greeting = _phrase_for("greeting", ctx.language or DEFAULT_LANGUAGE)
    audio = _instant_speak(greeting, ctx.language or DEFAULT_LANGUAGE)
    return Response(
        content=_twiml_speak_gather_lang(
            audio, greeting, base, ctx.language or DEFAULT_LANGUAGE
        ),
        media_type="application/xml",
    )


@app.post("/voice/twilio/outbound-answer")
async def twilio_outbound_answer(request: Request) -> Response:
    """Twilio hits this when the callee picks up an outbound call."""
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    form = await request.form()
    call_sid = str(form.get("CallSid") or "").strip()
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:outbound-unknown"

    # This engine app is MOUNTED inside the main SARA server, so FastAPI
    # startup events never fire for it — warm the phrase cache lazily on the
    # first webhook instead (idempotent, runs once in the background).
    _schedule_phrase_warmup()

    # Link this Twilio leg to our Call row. Without it `_voice_transfer` finds
    # no `provider_call_id`, returns "no_live_telephony_leg", and the
    # receptionist hand-off silently never happens on outbound calls.
    if call_sid:
        with contextlib.suppress(Exception):
            with SessionLocal() as db:
                call = db.scalar(
                    select(Call).where(Call.provider_call_id == call_sid)
                )
                # CallState has no QUEUED/IN_PROGRESS members — referencing them
                # raised AttributeError, and because this block is wrapped in
                # contextlib.suppress the failure was SILENT, so outbound calls
                # never linked to their Call row and the receptionist hand-off
                # always reported "no_live_telephony_leg".
                if call is not None and call.state in (
                    CallState.CREATED.value, CallState.DIALING.value,
                    CallState.RINGING.value,
                ):
                    transition_call(db, call, CallState.CONNECTED)
                db.commit()

    persona = booking_persona()
    ctx = booking_engine.get_context(session_key)
    ctx.phase = BookingPhase.GREETING.value
    # The greeting is spoken in the TwiML below — mark it delivered so the
    # conversation engine never introduces the receptionist a second time.
    ctx.greeting_completed = True

    # --- Dynamic Election Campaign Voice Agent -------------------------------
    # A campaign call speaks with the SAVED campaign profile: the greeting,
    # the language and the voice all come from the configuration the owner
    # published. Calls without a campaign profile keep the original
    # receptionist behaviour, untouched.
    call_id = str(request.query_params.get("call_id") or "").strip()
    campaign = _campaign_profile_for_call(call_sid=call_sid, call_id=call_id)
    if campaign is not None:
        campaign_ctx = campaign_engine.get_context(session_key)
        campaign_ctx.greeting_completed = True
        campaign_ctx.language = campaign.language or campaign_ctx.language
        greeting_text = campaign.render(campaign.greeting)
        language = campaign.language or "en-IN"
        # Render the greeting in the campaign language when the TTS provider
        # can; the <Say> fallback keeps the call alive if it cannot.
        audio = await _twilio_speak_or_say(
            greeting_text, language, budget=_CAMPAIGN_TTS_BUDGET_S,
            voice=campaign.voice or None,
        )
        log.info(
            "campaign greeting served call_sid=%s profile=%s language=%s",
            call_sid, call_id, language,
        )
        return Response(
            content=_twiml_speak_gather_lang(audio, greeting_text, base, language),
            media_type="application/xml",
        )

    # INSTANT greeting: served from the warm phrase cache (rendered at boot
    # with Sarvam in English + Hindi). No LLM, no TTS wait — the caller hears
    # the receptionist within ~1 second of answering. English is the default;
    # the conversation then mirrors whichever language the caller uses.
    greeting = _phrase_for("greeting", "en-IN")

    async def _warm_llm_greeting() -> None:
        # IMPORTANT: run against a THROWAWAY session key — touching the live
        # call's session would race the caller's first real turn.
        try:
            result = await asyncio.wait_for(
                booking_engine.handle_turn(
                    f"warmup:{uuid.uuid4().hex}",
                    "(caller just answered — greet and ask how you can help)",
                ),
                timeout=12.0,
            )
            if result and result.reply:
                _schedule_phrase_render(result.reply, "en-IN")
        except Exception as exc:
            log.warning("greeting warm-up skipped: %s", exc)

    try:
        asyncio.get_running_loop().create_task(_warm_llm_greeting())
    except RuntimeError:
        pass

    audio = _instant_speak(greeting, "en-IN")  # cache hit or instant <Say>
    # NOTE: _gather_attrs() supplies input/action/etc — never repeat any
    # attribute literally, or Twilio rejects the whole document (error 12100).
    if audio.get("url"):
        twiml = (
            f"<Response><Play>{base}{audio['url']}</Play>"
            f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
            f"{_gather_attrs('en-IN', base)}></Gather></Response>"
        )
    else:
        twiml = (
            f"<Response><Say{_say_attrs('en-IN')}>"
            f"{_xml_escape(greeting)}</Say>"
            f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
            f"{_gather_attrs('en-IN', base)}></Gather></Response>"
        )
    return Response(content=twiml, media_type="application/xml")


_TWILIO_GATHER_ATTRS = " method='POST' timeout='7' speechTimeout='auto' language='en-IN'"

# Twilio aborts a webhook call after 15 s and plays "an application error has
# occurred … goodbye" before hanging up — so EVERY TwiML response here must
# be produced well inside that budget. Sarvam TTS has been observed stalling
# for 20+ s; when it does, we fall back to Twilio's instant <Say> engine
# instead of letting the call die. The same deadline protects the booking
# engine/LLM path.
_TWILIO_WEBHOOK_BUDGET_S = 11.0
_TTS_DEADLINE_S = 9.0
_TTS_MIN_START_BUDGET_S = 3.0  # below this, <Say> instantly instead of starting TTS
# Campaign replies are short and scripted, so they can afford a real TTS
# render in the campaign language. Kept well inside Twilio's 15 s webhook
# limit; a slow provider still degrades to the <Say> fallback.
_CAMPAIGN_TTS_BUDGET_S = float(os.getenv("VOICE_CAMPAIGN_TTS_BUDGET_S", "5.0"))
# How long a booking turn may run before the caller hears "one moment please".
# Availability checks are instant and the LLM normally answers in 2-3 s, so
# this is generous enough that the happy path answers in a SINGLE round-trip
# and the guest is never put on hold for a result we could just speak.
# Raise only if your LLM provider is consistently slower than this.
_FAST_TURN_BUDGET_S = float(os.getenv("VOICE_FAST_TURN_BUDGET_S", "6.5"))
# Boot phrase warm-up is now OFF by default. Every reply is spoken with
# Twilio's own <Say> engine, which is instant and needs no pre-rendered audio,
# so the warm-up's only remaining value is the optional Sarvam voice layered
# on afterwards. Its cost, however, is real: rendering 22 phrases against
# Sarvam at boot starves live calls of provider capacity and inflated a normal
# turn from ~3 s to ~6.5 s. Set VOICE_PHRASE_WARMUP=1 if you want the natural
# voice pre-cached and accept the extra boot load.
_PHRASE_WARMUP_ENABLED = str(
    os.getenv("VOICE_PHRASE_WARMUP", "0")
).strip().lower() in ("1", "true", "yes", "on")


def _xml_escape(text: Optional[str]) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _twiml_speak_gather(audio: Dict[str, Any], text: str, base: str) -> str:
    """<Play> the TTS audio when we have it, else <Say> the text, then listen."""
    gather = (
        f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
        f"{_TWILIO_GATHER_ATTRS}></Gather>"
    )
    if audio.get("url"):
        return f"<Response><Play>{base}{audio['url']}</Play>{gather}</Response>"
    return (
        f"<Response><Say voice='Polly.Aditi' language='en-IN'>{_xml_escape(text)}</Say>"
        f"{gather}</Response>"
    )


async def _twilio_speak_or_say(
    text: str,
    language: Optional[str] = None,
    budget: float = _TTS_DEADLINE_S,
    voice: Optional[str] = None,
) -> Dict[str, Any]:
    """TTS with a hard deadline; degrade to Twilio <Say> instead of stalling.

    A slow Sarvam TTS (observed at 20+ s) must never push a TwiML webhook
    past Twilio's 15 s limit. If it doesn't answer within `budget` seconds
    the attempt is cancelled and the caller hears Twilio's instant <Say>
    voice — the call continues either way and NEVER drops.

    This is the caller-facing path, so it registers as LIVE TTS traffic; the
    boot phrase warm-up then stands aside rather than competing with it.
    """
    global _LIVE_TTS_PENDING
    _LIVE_TTS_PENDING += 1
    try:
        return await _speak_tracked(text, language, budget, voice=voice)
    finally:
        _LIVE_TTS_PENDING = max(0, _LIVE_TTS_PENDING - 1)


async def _speak_tracked(
    text: str, language: Optional[str], budget: float, voice: Optional[str] = None
) -> Dict[str, Any]:
    """The actual TTS attempt for a live caller; never raises."""
    try:
        audio = await asyncio.wait_for(
            tts_speak(text, language=language or "en-IN", voice=voice),
            timeout=budget,
        )
        if isinstance(audio, dict) and audio.get("url"):
            return audio
    except asyncio.TimeoutError:
        log.warning("TTS exceeded %.1fs — replying with Twilio <Say> (call continues)", budget)
    except Exception as exc:
        log.warning("TTS failed (%s) — replying with Twilio <Say> (call continues)", exc)
    return {}  # empty dict => _twiml_speak_gather uses <Say>


# ---------------------------------------------------------------------------
# Instant-response layer: phrase cache + warm-up + human fillers
#
# Twilio wants a TwiML answer fast, and callers hate dead air. So every FIXED
# phrase a hotel call needs (greeting, fillers, silence nudges, provider
# notice, recovery) is pre-rendered with Sarvam in Hindi + English at
# startup. Webhooks then answer from cache in well under 2 seconds, and a
# receptionist-style filler ("एक मिनट… मैं चेक करती हूँ") covers the LLM
# while the real answer is prepared on a second Twilio request.
# ---------------------------------------------------------------------------

_PHRASE_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    # ENGLISH FIRST: it is the default language, so its clips render first
    # during warm-up and early calls speak real Sarvam audio immediately.
    "en-IN": {
        "greeting": [
            "Hello! Thank you for calling Grand Stay Hotel. This is SARA — how may I help you today?"
        ],
        "filler": ["One moment please…", "Let me check that for you…"],
        "filler2": ["Thanks for holding…", "Just a moment more…"],
        "silence1": ["Hello? Are you there? How may I help you?"],
        "silence2": ["I'm listening — please go ahead."],
        "silence3": ["Would you like to book a room? Just say yes, or tell me your date."],
        "silence_end": [
            "I'm unable to hear you. Please call us again whenever you'd like to talk. Thank you!"
        ],
        "provider": ["Hello, I'm still here — how may I help you?"],
        "recover": ["Sorry about that small hiccup — I'm still here. How may I help you?"],
    },
    "hi-IN": {
        "greeting": [
            "नमस्ते! Grand Stay Hotel में आपका स्वागत है। मैं सारा बोल रही हूँ। "
            "बताइए, आज मैं आपकी क्या मदद कर सकती हूँ?",
        ],
        "filler": ["एक मिनट… मैं अभी चेक करती हूँ।", "जी, एक सेकंड…"],
        "filler2": ["मैंने देख लिया… बस एक मिनट।", "जी, बता रही हूँ…"],
        "silence1": ["जी, बताइए — मैं आपकी क्या मदद कर सकती हूँ?"],
        "silence2": ["मुझे अभी आपकी आवाज़ नहीं आई। बताइए, मैं आपके लिए क्या कर सकती हूँ?"],
        "silence3": ["क्या आप कमरा बुक करना चाहते हैं? जी हाँ कहिए, या अपनी तारीख़ बताइए।"],
        "silence_end": [
            "मुझे आपकी आवाज़ नहीं आ रही। जब आप बात करना चाहें, हमें फिर से कॉल करें। धन्यवाद!"
        ],
        "provider": ["जी, मैं सुन रही हूँ — बताइए, मैं आपकी क्या मदद कर सकती हूँ?"],
        "recover": [
            "क्षमा कीजिए, एक छोटी सी तकनीकी दिक्कत आई थी। बताइए, मैं आपकी क्या मदद कर सकती हूँ?"
        ],
    },
    "en-IN": {
        "greeting": [
            "Hello! Thank you for calling Grand Stay Hotel. This is SARA — how may I help you today?"
        ],
        "filler": ["One moment please…", "Let me check that for you…"],
        "filler2": ["Thanks for holding…", "Just a moment more…"],
        "silence1": ["Hello? Are you there? How may I help you?"],
        "silence2": ["I'm listening — please go ahead."],
        "silence3": ["Would you like to book a room? Just say yes, or tell me your date."],
        "silence_end": [
            "I'm unable to hear you. Please call us again whenever you'd like to talk. Thank you!"
        ],
        "provider": ["Hello, I'm still here — how may I help you?"],
        "recover": ["Sorry about that small hiccup — I'm still here. How may I help you?"],
    },
}

# Every fixed phrase has a guaranteed-spoken fallback: Sarvam may stall for
# minutes, but Twilio's Polly engine answers in milliseconds and keeps the
# conversation flowing. Callers ALWAYS hear a voice after the trial message.
_PHRASE_SAY: Dict[str, str] = {
    "hi-IN": "en-IN",   # Hindi text through Polly.Aditi is Hindi-voiced
    "en-IN": "en-IN",
}

_PHRASE_CACHE: Dict[str, str] = {}      # phrase text -> static audio url
# Phrase text -> raw WAV bytes. The media stream needs the BYTES (it writes
# mu-law frames straight to the socket), not a URL, so it cannot share
# _PHRASE_CACHE. Without this the stream re-renders every phrase live, and
# Sarvam TTS has been measured at 3-35 s per call — far beyond the greeting
# budget — so the caller heard nothing at all.
_STREAM_PHRASE_CACHE: Dict[str, bytes] = {}
_PHRASE_WARM_LOCK = threading.Lock()
# Number of LIVE, caller-facing TTS renders currently in flight. The boot
# warm-up stands down whenever this is non-zero so a real inbound call is
# never starved of TTS (which surfaced to callers as Twilio's "an application
# error has occurred" after the webhook blew its 15 s budget).
_LIVE_TTS_PENDING = 0
_ROTATION_STATE: Dict[str, int] = {}
_PENDING_ANSWERS: Dict[str, Dict[str, Any]] = {}  # session -> prepared turn task


async def _compute_booking_turn(session_key: str, speech: str) -> Optional[TurnResult]:
    """Run one booking turn with a timeout; failures return None (never raise).

    Runs as a background task while the caller hears the instant filler, so a
    slow LLM/TTS can never blow Twilio's 15 s webhook limit.
    """
    try:
        return await asyncio.wait_for(
            booking_engine.handle_turn(session_key, speech),
            timeout=_TWILIO_WEBHOOK_BUDGET_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "booking turn exceeded %.1fs session=%s (filler already sent)",
            _TWILIO_WEBHOOK_BUDGET_S, session_key,
        )
        return None
    except Exception as exc:
        # A failure must NEVER 500 a webhook — the filler TwiML already sent
        # keeps the call alive; this turn just has no fresh answer.
        log.exception("booking turn failed session=%s: %s", session_key, exc)
        return None


async def _compute_voice_turn(
    session_key: str, speech: str, campaign: Optional[CampaignProfile] = None
) -> Optional[TurnResult]:
    """One conversation turn for whichever agent owns this call."""
    if campaign is None:
        return await _compute_booking_turn(session_key, speech)
    try:
        result = await asyncio.wait_for(
            campaign_engine.handle_turn(session_key, speech),
            timeout=_TWILIO_WEBHOOK_BUDGET_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "campaign turn exceeded %.1fs session=%s (filler already sent)",
            _TWILIO_WEBHOOK_BUDGET_S, session_key,
        )
        return None
    except Exception as exc:
        log.exception("campaign turn failed session=%s: %s", session_key, exc)
        return None
    _record_campaign_turn(session_key, result)
    return result


def _record_campaign_turn(session_key: str, result: Optional[TurnResult]) -> None:
    """Persist campaign turn events and honour a do-not-call request.

    An opt-out asked for on the phone takes effect immediately: the customer
    is flagged so no later campaign can dial that number again.
    """
    if result is None:
        return
    try:
        with SessionLocal() as db:
            call = None
            call_id = _call_id_for_session(session_key)
            if call_id:
                call = db.get(Call, call_id)
            if call is None and session_key.startswith("twilio:"):
                call = db.scalar(
                    select(Call).where(Call.provider_call_id == session_key[7:])
                )
            business_id = call.business_id if call is not None else "biz_demo"
            customer = (
                db.get(Customer, call.customer_id)
                if call is not None and call.customer_id
                else None
            )
            spoken = getattr(getattr(result, "context", None), "last_text", "") or ""
            for event in getattr(result, "events", []) or []:
                event_type = str(event.get("type") or "TURN")
                record_event(
                    db,
                    business_id=business_id,
                    customer_id=call.customer_id if call is not None else None,
                    call_id=call.id if call is not None else None,
                    conversation_id=(
                        call.conversation_id if call is not None else None
                    ),
                    event_type=f"CAMPAIGN_{event_type}",
                    source="campaign_agent",
                    actor="caller",
                    payload={"text": spoken, **event},
                )
                if event_type == "DO_NOT_CALL_REQUESTED" and customer is not None:
                    customer.do_not_contact = True
                    customer.consent_status = "OPTED_OUT"
                    customer.updated_at = utcnow()
            db.commit()
    except Exception as exc:  # noqa: BLE001 - never break a live call
        log.warning("campaign event persistence failed: %s", exc)


def _phrase_for(kind: str, language: Optional[str]) -> str:
    """A fixed phrase for `kind` in the session language (rotated when several)."""
    lang = language or "en-IN"
    table = _PHRASE_TEMPLATES.get(lang) or _PHRASE_TEMPLATES["en-IN"]
    phrases = table.get(kind) or _PHRASE_TEMPLATES["en-IN"].get(kind)
    if not phrases:
        return "Hello! How may I help you?"
    if len(phrases) > 1:
        key = f"{lang}:{kind}"
        idx = _ROTATION_STATE.get(key, 0)
        _ROTATION_STATE[key] = idx + 1
        return phrases[idx % len(phrases)]
    return phrases[0]


def _say_attrs(language: Optional[str]) -> str:
    """<Say> attributes with a language Twilio always has (guaranteed voice)."""
    say_lang = _PHRASE_SAY.get(language or "en-IN", "en-IN")
    return f" voice='Polly.Aditi' language='{say_lang}'"


def _cache_set(phrase: str, url: str) -> None:
    _PHRASE_CACHE[(phrase or "").strip()] = url


def _cache_get(phrase: str) -> Optional[str]:
    return _PHRASE_CACHE.get((phrase or "").strip())


def _stream_phrase_key(phrase: str, language: Optional[str]) -> str:
    """Canonical key for the Media Streams audio cache.

    Includes the resolved Sarvam language so the same phrase text spoken in
    two languages never collides. Shared by the warm-up and _stream_speak so
    the two can never drift apart (a mismatch would silently make every
    greeting re-render live).
    """
    code, _native = sarvam_tts_language(language or DEFAULT_LANGUAGE)
    return f"{_tts_sarvam_language(code)}|{(phrase or '').strip()}"


async def _warm_phrase_cache() -> int:
    """Render every fixed phrase with Sarvam TTS (Hindi + English).

    Each phrase is stored twice: as a static URL for the TwiML <Gather> path
    and as raw WAV bytes for the Media Streams path, so the opening greeting is
    already rendered when the caller first hears SARA.
    """
    rendered = 0
    for lang, kinds in _PHRASE_TEMPLATES.items():
        for phrases in kinds.values():
            for phrase in phrases:
                # Check on EVERY phrase, not once at the start: a live call can
                # begin at any moment, and a 22-phrase warm-up that only checked
                # once kept hammering Sarvam for minutes (observed single
                # renders of 107 s), starving the caller and blowing Twilio's
                # webhook budget. Bail out the moment a caller needs TTS.
                if _LIVE_TTS_PENDING:
                    log.info(
                        "phrase warm-up paused after %d clip(s): a live call is "
                        "using TTS",
                        rendered,
                    )
                    return rendered
                # Must match the key _stream_speak builds, or the stream never
                # hits the cache and every greeting re-renders live.
                key = _stream_phrase_key(phrase, lang)
                if _cache_get(phrase) and _STREAM_PHRASE_CACHE.get(key):
                    continue
                try:
                    audio = await tts_speak(phrase, language=lang)
                except Exception as exc:
                    log.warning("warm-up TTS failed (%s): %.40s", lang, exc)
                    continue
                if audio.get("url"):
                    _cache_set(phrase, audio["url"])
                    rendered += 1
                    # Reuse the SAME render for the stream. tts_speak already
                    # produced the audio bytes, so the stream cache costs no
                    # extra Sarvam call — rendering twice doubled warm-up time,
                    # and at 3-35 s per call the cache missed its window.
                    raw = audio.get("bytes_data")
                    if raw and not _STREAM_PHRASE_CACHE.get(key):
                        _STREAM_PHRASE_CACHE[key] = raw
                if not _STREAM_PHRASE_CACHE.get(key) and sarvam_client.configured:
                    try:
                        code, _native = sarvam_tts_language(lang)
                        speech = await sarvam_client.speak(
                            phrase, target_language_code=_tts_sarvam_language(code)
                        )
                        if speech.audio:
                            _STREAM_PHRASE_CACHE[key] = speech.audio
                    except Exception as exc:
                        log.warning("stream warm-up failed: %.40s", exc)
    return rendered


def _schedule_phrase_warmup() -> None:
    """Warm the phrase cache once in the background (idempotent).

    Off by default — see _PHRASE_WARMUP_ENABLED. When enabled it still yields
    to any live caller so it can never delay a real conversation.
    """
    if not _PHRASE_WARMUP_ENABLED:
        return
    if not _PHRASE_WARM_LOCK.acquire(blocking=False):
        return

    async def _run() -> None:
        try:
            n = await _warm_phrase_cache()
            log.info("phrase cache warm: %d audio clips ready", n)
        except Exception as exc:
            log.warning("phrase warm-up failed (will retry on demand): %s", exc)
        finally:
            _PHRASE_WARM_LOCK.release()

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        _PHRASE_WARM_LOCK.release()


def _schedule_phrase_render(phrase: str, language: str) -> None:
    """Render one fixed phrase in the background so the NEXT call hits cache."""
    if _cache_get(phrase):
        return

    async def _run() -> None:
        try:
            audio = await tts_speak(phrase, language=language)
            if audio.get("url"):
                _cache_set(phrase, audio["url"])
        except Exception as exc:
            log.warning("phrase render failed: %s", exc)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


async def _cached_speak(phrase: str, language: str, *, budget: float = 4.0) -> Dict[str, Any]:
    """Audio for a FIXED phrase: cache hit = instant, miss = bounded TTS."""
    url = _cache_get(phrase)
    if url:
        return {"url": url}
    audio = await _twilio_speak_or_say(phrase, language, budget=budget)
    if audio.get("url"):
        _cache_set(phrase, audio["url"])
    return audio


def _instant_speak(phrase: str, language: str) -> Dict[str, Any]:
    """Cache hit or NOTHING — a webhook must never wait on TTS here.

    Sarvam TTS has been observed stalling for minutes; a cold phrase is
    rendered in the background (ready for the next turn/call) and the current
    response falls back to Twilio's instant <Say> voice instead.
    """
    url = _cache_get(phrase)
    if url:
        return {"url": url}
    _schedule_phrase_render(phrase, language)
    return {}


_TWILIO_LANG_MAP = {
    "hi": "hi-IN", "en": "en-IN", "ta": "ta-IN", "te": "te-IN",
    "bn": "bn-IN", "mr": "mr-IN", "gu": "gu-IN", "kn": "kn-IN",
    "ml": "ml-IN", "pa": "pa-IN", "od": "od-IN", "ur": "ur-PK",
    "as": "hi-IN",
}


# Consecutive silent <Gather> timeouts per CallSid. A caller who pauses for a
# few seconds is normal (thinking, writing, handing the phone over) and must
# never be disconnected; only persistent silence ends the call, and even then
# politely and with an offer to call back.
_GATHER_TIMEOUTS: Dict[str, int] = {}
_GATHER_TIMEOUT_MAX = 3


@app.post("/voice/twilio/keepalive")
async def twilio_keepalive(request: Request) -> Response:
    """Safety net that guarantees SARA never ends the call from our side.

    Every <Gather> is followed by a <Redirect> here. Twilio only reaches it
    when the Gather finished WITHOUT calling back - the timeout webhook was
    lost, the tunnel hiccuped, or the carrier dropped the leg. Previously that
    meant an empty rest-of-<Response> and Twilio hung up mid-conversation.
    Now the call is re-armed: a short, warm nudge plus a fresh <Gather>, so the
    guest hears a voice instead of dead air and the conversation continues.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "unknown")
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    language = None
    try:
        language = _voice_context_for_call_sid(call_sid).language
    except Exception:  # pragma: no cover - context is best effort
        pass

    count = _GATHER_TIMEOUTS.get(call_sid, 0) + 1
    _GATHER_TIMEOUTS[call_sid] = count
    log.info("keepalive reached call=%s (#%s) - re-arming the call", call_sid, count)

    if count >= 4:
        _GATHER_TIMEOUTS.pop(call_sid, None)
        say = _say_attrs(language)
        goodbye = (
            "It looks like the line has gone quiet. Sorry we couldn't reach you, "
            "and thank you for calling. Please call back any time."
        )
        return Response(
            content=f"<Response><Say{say}>{_xml_escape(goodbye)}</Say></Response>",
            media_type="application/xml",
        )

    say = _say_attrs(language)
    nudge = (
        "Sorry, I didn't hear you. Are you still there?"
        if count == 1
        else "I'm still here whenever you're ready."
    )
    gather = (
        f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
        f"{_gather_attrs(language, base)}></Gather>"
    )
    keepalive = (
        f"<Redirect method='POST'>{base}/voice-agent/voice/twilio/keepalive</Redirect>"
    )
    return Response(
        content=f"<Response><Say{say}>{_xml_escape(nudge)}</Say>{gather}{keepalive}</Response>",
        media_type="application/xml",
    )


@app.post("/voice/twilio/gather-timeout")
async def twilio_gather_timeout(request: Request) -> Response:
    """Handle an <Gather> that timed out with no caller speech.

    Without this the call was DISCONNECTED by our own TwiML: a <Gather> that
    times out hands control back to the rest of the <Response>, and ours had
    nothing after it, so Twilio hung up. SARA therefore cut callers off
    whenever they paused longer than the Gather timeout mid-conversation.
    Here we instead nudge them and re-open the Gather, so a short silence just
    costs a "are you still there?" rather than the call.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "unknown")
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    language = None
    try:
        ctx = _voice_context_for_call_sid(call_sid)
        language = ctx.language
    except Exception:  # pragma: no cover - context is best effort
        pass

    count = _GATHER_TIMEOUTS.get(call_sid, 0) + 1
    _GATHER_TIMEOUTS[call_sid] = count
    log.info("gather timeout #%s call=%s (caller silent)", count, call_sid)

    if count >= _GATHER_TIMEOUT_MAX:
        _GATHER_TIMEOUTS.pop(call_sid, None)
        say = _say_attrs(language)
        goodbye = (
            "It looks like you've stepped away. Sorry we couldn't reach you, "
            "and thank you for calling. Please call back any time."
        )
        return Response(
            content=f"<Response><Say{say}>{_xml_escape(goodbye)}</Say></Response>",
            media_type="application/xml",
        )

    # Still worth staying on the line: nudge, then listen again.
    nudge = (
        "Hello, are you still there?"
        if count == 1
        else "Hello? I can still hear the line. Is anyone there?"
    )
    say = _say_attrs(language)
    gather = (
        f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
        f"{_gather_attrs(language, base)}></Gather>"
    )
    return Response(
        content=f"<Response><Say{say}>{_xml_escape(nudge)}</Say>{gather}</Response>",
        media_type="application/xml",
    )
def _gather_attrs(language: Optional[str], base: str = "") -> str:
    """<Gather> attributes: caller's language + DTMF (trial 'press any key').

    Twilio TRIAL accounts tell the callee to 'press any key to continue'. A
    speech-only Gather would swallow that keypress and the call would go
    silent — so every Gather also accepts digits and any keypress is treated
    as 'the caller is there, keep the conversation going'.
    """
    two = (language or "en-IN").split("-")[0].lower()
    bcp47 = _TWILIO_LANG_MAP.get(two, "en-IN")
    # onTimeout is NOT optional. When a <Gather> times out with no input Twilio
    # simply continues executing whatever follows it in the <Response>. Ours was
    # <Say>...</Say><Gather>...</Gather> with NOTHING after it, so a caller who
    # paused longer than `timeout` got the call HUNG UP by the agent mid
    # conversation. onTimeout nudges them and re-opens the Gather instead.
    on_timeout = (
        f" onTimeout='{base}/voice-agent/voice/twilio/gather-timeout'" if base else ""
    )
    return (
        f" method='POST' timeout='6' speechTimeout='auto' input='speech dtmf'"
        f" numDigits='1' language='{bcp47}'{on_timeout}"
    )


def _twiml_speak_gather_lang(audio: Dict[str, Any], text: str, base: str, language: Optional[str]) -> str:
    """Speak the reply, then listen in the caller's language.

    <Say> ALWAYS comes first so the caller is guaranteed to hear a voice.
    Twilio renders <Say> on its own infrastructure, whereas <Play> requires
    Twilio to download an mp3 from OUR tunnel first — measured at 0.7-3.1 s
    through ngrok, and a slow or timed-out fetch means Twilio plays NOTHING
    and the caller hears dead air. So the natural Sarvam voice is layered on
    after the guaranteed <Say> rather than being the only voice source.
    """
    two = (language or "en-IN").split("-")[0].lower()
    bcp47 = _TWILIO_LANG_MAP.get(two, "en-IN")
    # _gather_attrs() already supplies input/action/method/timeout/language —
    # repeating any of them makes Twilio reject the document (error 12100).
    gather = (
        f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
        f"{_gather_attrs(language, base)}></Gather>"
    )
    say = (
        f"<Say{_say_attrs(language)}>{_xml_escape(text)}</Say>"
    )
    keepalive = f"<Redirect method='POST'>{base}/voice-agent/voice/twilio/keepalive</Redirect>"
    if audio.get("url"):
        # <Say> first so speech starts instantly, then the richer Sarvam clip.
        return (
            f"<Response>{say}<Play>{base}{audio['url']}</Play>{gather}"
            f"{keepalive}</Response>"
        )
    return f"<Response>{say}{gather}{keepalive}</Response>"


@app.post("/voice/twilio/gather")
async def twilio_gather(request: Request) -> Response:
    """One spoken turn of a live Twilio call: booking engine -> TTS -> TwiML.

    The CallSid keys the booking session so the caller's collected details
    survive across every Gather round-trip. Language is auto-detected on EVERY
    turn (start-of-call and mid-call switches both land here) and the TTS and
    Twilio recognizer follow the caller. If the engine asks us to transfer,
    the response TwiML <Dial>s the receptionist instead of continuing the loop.
    Provider trial/informational messages are answered with a short spoken nudge
    (never silence, never a hang-up) so the call keeps flowing naturally.
    """
    form = await request.form()

    # Reject an unsigned/forged callback. This endpoint injects SpeechResult
    # straight into a live conversation, so anyone who guessed the URL could
    # otherwise drive SARA on a live call.
    if not _twilio_signature_valid(request, form):
        log.warning("rejected unsigned Twilio gather callback (signature mismatch)")
        return Response(
            content="<Response><Hangup/></Response>", media_type="application/xml"
        )

    speech = (form.get("SpeechResult") or "").strip()
    digits = (form.get("Digits") or "").strip()
    call_sid = str(form.get("CallSid") or "").strip()
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"

    # A campaign call is served by the campaign agent; every other call keeps
    # using the original booking agent.
    campaign = _campaign_profile_for_call(call_sid=call_sid)
    ctx = _voice_context_for_profile(session_key, campaign)

    # The caller spoke: clear accumulated silence so a later pause restarts the
    # "are you still there?" ladder from the beginning.
    if speech or digits:
        _GATHER_TIMEOUTS.pop(call_sid, None)

    # Lazy phrase-cache warm-up (mounted sub-apps get no startup event).
    _schedule_phrase_warmup()

    # --- keypress (Twilio trial "press any key") -----------------------------
    # A DTMF press is the caller telling us they are there. Greet them and
    # keep the loop alive — never treat a keypress as silence.
    if digits and not speech:
        log.info("caller pressed a key (trial press-any-key) session=%s", session_key)
        ctx.silence_count = 0
        if campaign is not None:
            # A campaign call re-plays the CAMPAIGN greeting, never the
            # receptionist's hotel script.
            phrase = campaign.render(campaign.greeting)
            language = campaign.language or "en-IN"
        else:
            phrase = _phrase_for("greeting", "en-IN")
            language = "en-IN"
        audio = _instant_speak(phrase, language)
        return Response(
            content=_twiml_speak_gather_lang(audio, phrase, base, language),
            media_type="application/xml",
        )

    # --- no speech this round ------------------------------------------------
    if not speech:
        ctx.silence_count = getattr(ctx, "silence_count", 0) + 1
        n = ctx.silence_count
        # A real receptionist doesn't repeat the same nudge — she escalates:
        # invite -> reassure -> offer a booking hint, and only after a long
        # silence say goodbye (while STILL keeping the call open for the
        # caller to change their mind).
        kind = "silence_end" if n >= 4 else ("silence1", "silence2", "silence3")[min(n - 1, 2)]
        phrase = _phrase_for(kind, ctx.language)
        audio = _instant_speak(phrase, ctx.language)
        _schedule_phrase_render(phrase, ctx.language)  # re-warm if it was cold
        twiml = _twiml_speak_gather_lang(audio, phrase, base, ctx.language)
        return Response(content=twiml, media_type="application/xml")

    ctx.silence_count = 0

    # --- provider/trial notice: catch it here too ---------------------------
    # Twilio transcribes its trial message slightly differently on every call
    # ("you have a trial account...", "you have reached a trial version...").
    # It is Twilio speaking OVER our agent — treat it as a non-event and keep
    # the call alive; never feed it to the booking engine as caller speech.
    try:
        if is_provider_informational(speech):
            log.info(
                "provider trial message detected at webhook (call continues) session=%s", session_key
            )
            nudge = _phrase_for("provider", ctx.language)
            audio = _instant_speak(nudge, ctx.language)
            twiml = _twiml_speak_gather_lang(audio, nudge, base, ctx.language)
            return Response(content=twiml, media_type="application/xml")
    except Exception as exc:  # detection must never break the call
        log.warning("provider-message check skipped: %s", exc)

    # --- language: explicit switch > script detection > keep current --------
    decision = detect_language(speech, current=ctx.language)
    if decision.changed:
        ctx.language = decision.language
        log.info(
            "language switch session=%s %s -> %s (%s, conf=%.2f)",
            session_key, decision.previous, decision.language,
            decision.source, decision.confidence,
        )
    ctx.language_detected = decision.to_dict()

    # --- the human receptionist pattern: fill first, answer next ------------
    # The caller must hear a voice within ~1s, like a real front desk. Reply
    # IMMEDIATELY with a short cached filler ("One moment, let me check…"),
    # and prepare the real answer (booking engine + LLM + TTS) in the
    # background. The filler's <Gather> Redirect then fetches it via
    # /voice/twilio/answer — a SECOND Twilio request with a fresh 15 s
    # budget, so a slow LLM can never break the call again.
    turn_started = time.monotonic()
    persona = booking_persona()
    filler = _phrase_for("filler", ctx.language)
    filler_audio = _instant_speak(filler, ctx.language)  # never wait on TTS here

    # The task's RETURN VALUE is the prepared answer.
    prep_task = asyncio.get_running_loop().create_task(
        _compute_voice_turn(session_key, speech, campaign)
    )

    # FAST PATH. An availability check against the demo inventory is instant and
    # the LLM normally answers in 2-3 s, so the caller does not need to hear
    # "one moment please" at all. Awaiting the reply in THIS request keeps the
    # whole turn to a single Twilio round-trip.
    #
    # The previous design always played a filler and then <Redirect>ed to
    # /answer, adding a second webhook round-trip and making every turn feel
    # like the guest was being put on hold. The filler is now only a safety net
    # for a turn that genuinely overruns the fast budget.
    try:
        result = await asyncio.wait_for(
            asyncio.shield(prep_task), timeout=_FAST_TURN_BUDGET_S
        )
    except asyncio.TimeoutError:
        log.info(
            "turn exceeded fast budget %.1fs session=%s — using the slow path",
            _FAST_TURN_BUDGET_S, session_key,
        )
        result = None
    except Exception as exc:
        log.warning("booking turn failed session=%s: %s", session_key, exc)
        prep_task.cancel()
        return await _full_twiml_reply(
            None, ctx, persona, base, call_sid, budget=_TWILIO_WEBHOOK_BUDGET_S
        )

    if result is not None and getattr(result, "reply", ""):
        # Reply within budget. Speech is rendered by Twilio itself (<Say>), so
        # the caller hears the answer as soon as this response returns.
        log.info(
            "fast reply in %.2fs session=%s: %.60s",
            time.monotonic() - turn_started, session_key, result.reply,
        )
        reply_budget = 0.0
        if campaign is not None:
            # A campaign reply is spoken in the campaign language, so it needs
            # a real TTS render — Twilio's <Say> voice cannot read Telugu.
            remaining = (
                _TWILIO_WEBHOOK_BUDGET_S - (time.monotonic() - turn_started) - 1.0
            )
            reply_budget = max(
                _TTS_MIN_START_BUDGET_S,
                min(_CAMPAIGN_TTS_BUDGET_S, remaining),
            )
        return await _full_twiml_reply(
            result, ctx, persona, base, call_sid,
            budget=reply_budget,
            voice=(campaign.voice or None) if campaign is not None else None,
        )
    if result is not None:
        # Turn completed but produced nothing to say — recover out loud rather
        # than returning an empty response.
        log.warning("booking turn produced no reply session=%s", session_key)

    # Slow turn: play the short filler and let /answer speak the real reply with
    # a fresh 15 s budget. This is the fallback, not the normal path.
    if time.monotonic() - turn_started > 2.0:
        prep_task.cancel()
        _PENDING_ANSWERS.pop(session_key, None)
        return await _full_twiml_reply(
            None, ctx, persona, base, call_sid,
            budget=max(2.5, _TWILIO_WEBHOOK_BUDGET_S - (time.monotonic() - turn_started)),
        )

    _PENDING_ANSWERS[session_key] = {"task": prep_task}
    redirect = f"{base}/voice-agent/voice/twilio/answer"
    # <Say> first: this is the "one moment" the caller is waiting on, so it must
    # never depend on downloading an mp3 from our tunnel (which can stall and
    # leave the caller in silence).
    filler_part = f"<Say{_say_attrs(ctx.language)}>{_xml_escape(filler)}</Say>"
    if filler_audio.get("url"):
        filler_part += f"<Play>{base}{filler_audio['url']}</Play>"
    twiml = (
        f"<Response>{filler_part}"
        f"<Redirect method='POST'>{_xml_escape(redirect)}</Redirect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/twilio/answer")
async def twilio_answer(request: Request) -> Response:
    """Second phase of a turn: speak the reply the gather phase prepared.

    Entered via <Gather action='…/twilio/gather'><Redirect>…/twilio/answer
    </Redirect></Gather> (or as a plain <Redirect> target). This request has
    Twilio's full 15 s budget again, so the prepared reply — including fresh
    Sarvam TTS for the dynamic answer — always makes it to the caller.
    """
    form = await request.form()
    call_sid = str(form.get("CallSid") or "").strip()
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"
    ctx = _voice_context_for_call_sid(call_sid)
    persona = booking_persona()
    pending = _PENDING_ANSWERS.pop(session_key, None)

    if not pending:
        # Nothing pending (missed Redirect, double-hit, restart): rejoin the
        # normal loop gracefully — never a 500, never a hang-up.
        reply = _phrase_for("recover", ctx.language)
        audio = _instant_speak(reply, ctx.language)
        return Response(
            content=_twiml_speak_gather_lang(audio, reply, base, ctx.language),
            media_type="application/xml",
        )

    # Wait for the turn prepared during the filler phase. ONE elapsed-based
    # deadline: whatever is left of the 11 s budget goes to the model wait,
    # the rest to TTS — so the total never approaches Twilio's 15 s limit.
    phase_start = time.monotonic()
    model_wait = max(1.5, _TWILIO_WEBHOOK_BUDGET_S - 1.0)  # 10s for the model
    try:
        result = await asyncio.wait_for(pending["task"], timeout=model_wait)
    except asyncio.TimeoutError:
        result = None
    if result is None or not result.reply:
        recover = _phrase_for("recover", ctx.language)
        audio = _instant_speak(recover, ctx.language)
        return Response(
            content=_twiml_speak_gather_lang(audio, recover, base, ctx.language),
            media_type="application/xml",
        )

    reply = result.reply
    action = result.action or {}

    # Bridge the remaining wait like a human: a second short "thanks for
    # holding" line while the answer audio finishes rendering.
    bridge = _phrase_for("filler2", ctx.language)
    bridge_audio = _instant_speak(bridge, ctx.language)
    # <Say> (Twilio-rendered, always reliable) then the optional Sarvam clip.
    bridge_part = f"<Say{_say_attrs(ctx.language)}>{_xml_escape(bridge)}</Say>"
    if bridge_audio.get("url"):
        bridge_part += f"<Play>{base}{bridge_audio['url']}</Play>"

    # Dynamic answer text: fresh Sarvam TTS with whatever budget remains.
    tts_budget = max(2.0, _TWILIO_WEBHOOK_BUDGET_S - (time.monotonic() - phase_start))
    campaign = _campaign_profile_for_call(call_sid=call_sid)
    audio = await _twilio_speak_or_say(
        reply, ctx.language, budget=tts_budget,
        voice=(campaign.voice or None) if campaign is not None else None,
    )
    # <Say> is the GUARANTEED voice: Twilio renders it on its own servers, so
    # the caller always hears the answer. <Play> of our Sarvam clip is layered
    # on after it, purely to add the natural voice — if that mp3 fetch through
    # the tunnel is slow or fails, the caller has already heard the reply
    # instead of falling into dead air.
    answer_part = (
        f"<Say{_say_attrs(ctx.language)}>{_xml_escape(reply)}</Say>"
        f"<Play>{base}{audio['url']}</Play>"
        if audio.get("url")
        else f"<Say{_say_attrs(ctx.language)}>{_xml_escape(reply)}</Say>"
    )

    if action.get("type") == "transfer_success":
        destination = action.get("destination") or persona.receptionist_number
        log.info("transfer dialing destination=%s call_sid=%s", destination, call_sid)
        return Response(
            content=(
                f"<Response>{bridge_part}{answer_part}"
                f"<Dial timeout='30' action='{base}/voice-agent/voice/twilio/transfer-status'>"
                f"<Number>{_xml_escape(destination)}</Number></Dial></Response>"
            ),
            media_type="application/xml",
        )

    # Normal (and end_call safety-net) replies keep the conversation loop:
    # [bridge] + answer + listen again in the caller's language.
    listen = (
        f"<Gather action='{base}/voice-agent/voice/twilio/gather'"
        f"{_gather_attrs(ctx.language, base)}></Gather>"
    )
    return Response(
        content=f"<Response>{bridge_part}{answer_part}{listen}</Response>",
        media_type="application/xml",
    )


async def _full_twiml_reply(
    result: Optional[TurnResult],
    ctx: BookingContext,
    persona: Any,
    base: str,
    call_sid: str,
    *,
    budget: float,
    voice: Optional[str] = None,
) -> Response:
    """Legacy one-request reply path (used only when the filler was too slow)."""
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"
    if result is None or not result.reply:
        recover = _phrase_for("recover", ctx.language)
        audio = _instant_speak(recover, ctx.language)
        return Response(
            content=_twiml_speak_gather_lang(audio, recover, base, ctx.language),
            media_type="application/xml",
        )

    action = result.action or {}

    if action.get("type") == "transfer_success":
        destination = action.get("destination") or persona.receptionist_number
        log.info("transfer dialing destination=%s call_sid=%s", destination, call_sid)
        return Response(
            content=(
                f"<Response><Dial timeout='30' action='{base}/voice-agent/voice/twilio/transfer-status'>"
                f"<Number>{_xml_escape(destination)}</Number></Dial></Response>"
            ),
            media_type="application/xml",
        )

    reply = result.reply
    if budget >= _TTS_MIN_START_BUDGET_S:
        audio = await _twilio_speak_or_say(
            reply, ctx.language, budget=budget, voice=voice
        )
    else:
        log.info("tts budget %.1fs too small — using <Say> (call continues)", budget)
        audio = {}
    return Response(
        content=_twiml_speak_gather_lang(audio, reply, base, ctx.language),
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Live bidirectional audio — Twilio Media Streams 2.0
#
# The TwiML path above is half-duplex (record -> webhook -> speak). This
# WebSocket is the full-duplex path: Twilio streams the caller's audio to us
# continuously and we stream SARA's voice straight back, which is what makes
# the conversation feel human (and lets callers interrupt).
#
# Point the Twilio number's "A call comes in" webhook here, or let the answer
# endpoints below hand the call off with <Connect><Stream>.
# ---------------------------------------------------------------------------

_MEDIA_STREAM_WS_PATH = "voice/twilio/media-stream"


def _media_stream_url() -> str:
    """wss:// URL for <Connect><Stream>, derived from PUBLIC_BASE_URL."""
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    return base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + (
        f"/voice-agent/{_MEDIA_STREAM_WS_PATH}"
    )


def _stream_transcribe(audio: bytes, language: Optional[str]) -> Dict[str, Any]:
    """STT adapter for the media stream (same engine as the web voice loop)."""
    return stt_transcribe(audio, language=language or "unknown")


async def _stream_speak(text: str, language: str) -> bytes:
    """TTS adapter for the media stream — returns raw WAV bytes.

    Cached phrases (greeting, fillers, silence nudges — everything rendered by
    the boot warm-up) return instantly. Only genuinely new text pays Sarvam's
    latency, which runs in the background and is awaited with a generous
    budget; the caller is never left in dead air.
    """
    if not text or not text.strip():
        return b""
    # Keyed by (language, text) — see _stream_phrase_key. A text-only key
    # would hand a Hindi caller the cached English clip.
    key = _stream_phrase_key(text, language)

    cached = _STREAM_PHRASE_CACHE.get(key)
    if cached:
        return cached

    if not sarvam_client.configured:
        return b""
    code, _native = sarvam_tts_language(language or DEFAULT_LANGUAGE)
    try:
        speech = await sarvam_client.speak(
            text, target_language_code=_tts_sarvam_language(code)
        )
    except SarvamError as exc:
        log.warning("stream TTS failed kind=%s: %s", exc.kind, exc.safe_message())
        return b""
    except Exception as exc:
        log.warning("stream TTS failed: %s", exc)
        return b""
    audio = speech.audio or b""
    if audio:
        # Bound the cache so a long call cannot grow it without limit.
        if len(_STREAM_PHRASE_CACHE) > 200:
            _STREAM_PHRASE_CACHE.clear()
        _STREAM_PHRASE_CACHE[key] = audio
    return audio


def _stream_resolve_call_id(call_sid: str) -> Optional[str]:
    try:
        return _call_id_for_session(f"twilio:{call_sid}")
    except Exception:
        return None


def _stream_resolve_caller_phone(call_sid: str) -> Optional[str]:
    """Look up the caller number so the booking engine keeps number memory."""
    try:
        with SessionLocal() as db:
            call = db.scalar(select(Call).where(Call.provider_call_id == call_sid))
            if call is None:
                return None
            customer = db.get(Customer, call.customer_id) if call.customer_id else None
            return getattr(customer, "phone", None)
    except Exception:
        return None


def _build_stream_hooks() -> Any:
    from app.services.voice_media_stream import VoiceStreamHooks

    async def _warm(session_key: str) -> None:
        # Create the context up-front so the greeting and the first turn share
        # one session, and warm the phrase cache for the default language.
        campaign = _campaign_profile_for_session(session_key)
        _voice_context_for_profile(session_key, campaign)
        _schedule_phrase_warmup()

    async def _turn(session_key: str, text: str, *args: Any, **kwargs: Any) -> Any:
        """One turn of whichever agent owns this call."""
        campaign = _campaign_profile_for_session(session_key)
        engine = _voice_engine_for_profile(campaign)
        return await engine.handle_turn(session_key, text, *args, **kwargs)

    return VoiceStreamHooks(
        resolve_call_id=_stream_resolve_call_id,
        resolve_caller_phone=_stream_resolve_caller_phone,
        transcribe=_stream_transcribe,
        speak=_stream_speak,
        turn=_turn,
        phrase=_phrase_for,
        default_language=DEFAULT_LANGUAGE,
        warm_session=_warm,
    )


@app.websocket("/" + _MEDIA_STREAM_WS_PATH)
async def twilio_media_stream(websocket: WebSocket) -> None:
    """Twilio Media Streams 2.0 endpoint — the live, full-duplex voice loop.

    This is the endpoint whose absence made every call connect but never speak.
    Accept the socket immediately (Twilio retries otherwise), then hand the
    connection to MediaStreamSession, which owns the event loop for the call.
    """
    from app.services.voice_media_stream import MediaStreamSession

    await websocket.accept()
    session = MediaStreamSession(websocket, _build_stream_hooks())
    try:
        await session.run()
    except Exception as exc:  # never let one call take down the worker
        log.exception("media stream crashed call=%s: %s", session.call_sid, exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


def _media_streams_enabled() -> bool:
    """Whether inbound calls should be answered with <Connect><Stream>.

    DEFAULT IS OFF. The TwiML <Say>/<Gather> loop is the reliable path: it
    answers in well under a second, works through any tunnel, and — crucially
    — still speaks on a TRIAL account. Media Streams cannot satisfy Twilio's
    trial "press any key to execute the code" gate, so with <Connect><Stream>
    a trial caller hears only that prompt and then dead air.

    Set TWILIO_MEDIA_STREAMS=1 once the account is upgraded and the wss://
    tunnel is known to be stable to get the full-duplex experience back.
    """
    return str(os.getenv("TWILIO_MEDIA_STREAMS", "0")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _twiml_connect_stream() -> str:
    """<Connect><Stream> hand-off, or "" when live streaming is unavailable.

    A blank string means "fall back to the TwiML <Gather> loop", which is what
    happens when streaming is disabled, no public URL is configured, or the
    tunnel cannot be relied on.
    """
    if not _media_streams_enabled():
        return ""
    if not PUBLIC_BASE_URL:
        return ""
    ws_url = _media_stream_url()
    return (
        f"<Connect><Stream url='{_xml_escape(ws_url)}' "
        f"track='inbound'>{_xml_escape(ws_url)}</Stream></Connect>"
    )


def sync_twilio_voice_url() -> Dict[str, Any]:
    """Point the Twilio number's inbound voice URL at SARA.

    The number was still answering `https://demo.twilio.com/welcome/voice/`
    (Twilio's demo page), so inbound calls never reached this backend at all —
    exactly the "call connects but SARA says nothing" symptom.

    Idempotent and best-effort: exposed as an authenticated endpoint so a
    restarted ngrok tunnel can be re-published without editing the console.
    """
    if not PUBLIC_BASE_URL:
        return {"status": "skipped", "reason": "PUBLIC_BASE_URL is not set"}
    target = f"{PUBLIC_BASE_URL}/voice-agent/voice/twilio/inbound"
    try:
        client = _twilio_client()
        if client is None:
            return {"status": "skipped", "reason": "Twilio not configured"}
        for incoming in client.incoming_phone_numbers.list():
            if (incoming.voice_url or "") == target:
                return {"status": "ok", "number": incoming.phone_number,
                        "voice_url": incoming.voice_url, "changed": False}
            incoming.update(voice_url=target, voice_method="POST")
            log.info("Twilio voice URL updated %s -> %s",
                     incoming.phone_number, target)
            return {"status": "ok", "number": incoming.phone_number,
                    "voice_url": target, "changed": True}
        return {"status": "skipped", "reason": "no Twilio number owned"}
    except Exception as exc:
        log.warning("could not sync Twilio voice URL: %s", exc)
        return {"status": "error", "reason": str(exc)[:200]}


@app.post("/voice/twilio/voice-url")
async def voice_twilio_voice_url(_: None = Depends(require_api_key)) -> Dict[str, Any]:
    """Re-publish the inbound voice URL (call after restarting the ngrok tunnel)."""
    return sync_twilio_voice_url()


# Twilio call lifecycle -> CallState. Anything unrecognised is ignored rather
# than rejected: Twilio must always receive a 200 or it retries the callback
# and eventually flags the number as having delivery problems.
#
# These are DIALLING-phase states, not conversation phases — the booking state
# machine takes over once the caller is actually connected.
_TWILIO_STATUS_MAP: Dict[str, CallState] = {
    "queued": CallState.CREATED,
    "initiated": CallState.DIALING,
    "ringing": CallState.RINGING,
    "in-progress": CallState.CONNECTED,
    "answered": CallState.CONNECTED,
    "completed": CallState.COMPLETED,
    "busy": CallState.FAILED,
    "failed": CallState.FAILED,
    "no-answer": CallState.FAILED,
    "canceled": CallState.CANCELLED,
}


@app.post("/voice/twilio/status-callback")
async def twilio_status_callback(request: Request) -> Response:
    """Twilio's per-call status callback for OUTBOUND calls.

    Keeps the Call row in step with reality (ringing -> in-progress ->
    completed/failed) so the UI never shows a finished call as "initiated".
    Always answers 200: a non-2xx makes Twilio retry and eventually mark the
    number's delivery as failing, which would break every outbound call.
    """
    try:
        form = await request.form()
        call_sid = str(form.get("CallSid") or "").strip()
        status = str(form.get("CallStatus") or "").strip().lower()
        duration = str(form.get("CallDuration") or "").strip()
        call_id = request.query_params.get("call_id")

        target = _TWILIO_STATUS_MAP.get(status)
        if target is not None:
            with contextlib.suppress(Exception):
                with SessionLocal() as db:
                    call = None
                    if call_sid:
                        call = db.scalar(
                            select(Call).where(Call.provider_call_id == call_sid)
                        )
                    if call is None and call_id:
                        call = db.get(Call, call_id)
                    if call is not None:
                        # Twilio can deliver callbacks out of order (a late
                        # "ringing" after "completed"), and transition_call
                        # raises on an invalid edge. The state we already
                        # recorded is equally true, so skip it.
                        try:
                            transition_call(
                                db, call, target,
                                payload={
                                    "twilio_status": status,
                                    "call_duration": duration,
                                },
                            )
                            db.commit()
                            log.info(
                                "twilio status call_sid=%s status=%s -> %s",
                                call_sid, status, target.value,
                            )
                        except ValueError:
                            log.info(
                                "twilio status ignored call_sid=%s status=%s "
                                "(current=%s)",
                                call_sid, status, call.state,
                            )
    except Exception as exc:
        # Never let a status callback 500 back at Twilio.
        log.warning("status callback failed: %s", str(exc)[:150])

    return Response(content="", status_code=200, media_type="text/plain")


@app.post("/voice/twilio/transfer-status")
async def twilio_transfer_status(request: Request) -> Response:
    """<Dial action> callback after a receptionist transfer attempt.

    DialCallStatus tells us exactly what happened: completed (answered),
    no-answer, busy, failed or canceled. Only a completed dial is a success;
    anything else returns the caller to the agent loop with an honest apology.
    """
    form = await request.form()
    status = str(form.get("DialCallStatus") or form.get("DialStatus") or "").lower().strip()
    call_sid = str(form.get("CallSid") or "").strip()
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    session_key = f"twilio:{call_sid}" if call_sid else "twilio:unknown"

    log.info("twilio transfer dial status=%s call_sid=%s", status, call_sid)

    if status == "completed":
        ctx = booking_engine.get_context(session_key)
        ctx.phase = BookingPhase.TRANSFER_SUCCESS.value
        # The receptionist owns the call now; end our leg politely.
        return Response(content="<Response><Hangup/></Response>", media_type="application/xml")

    ctx = booking_engine.get_context(session_key)
    ctx.phase = BookingPhase.TRANSFER_FAILED.value
    reply = (
        "I'm sorry, the receptionist is unavailable right now. "
        "Would you like me to take your booking details instead?"
    )
    audio = await _twilio_speak_or_say(reply, ctx.language)
    gather = (
        f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
        f"{_TWILIO_GATHER_ATTRS}></Gather>"
    )
    if audio.get("url"):
        twiml = f"<Response><Play>{base}{audio['url']}</Play>{gather}</Response>"
    else:
        twiml = f"<Response><Say voice='Polly.Aditi' language='en-IN'>{_xml_escape(reply)}</Say>{gather}</Response>"
    return Response(content=twiml, media_type="application/xml")


# Serve generated audio: GET /voice-agent/static/voice/<file>.mp3
app.mount("/static/voice", StaticFiles(directory=str(_static_dir())), name="voice_audio")


@app.on_event("startup")
async def _voice_warmup_on_startup() -> None:
    """Pre-render every fixed call phrase (Hindi + English) right at boot."""
    _schedule_phrase_warmup()
    # Make sure the Twilio number actually points at us. This is a no-op once
    # it matches, and self-heals after a tunnel URL changes.
    asyncio.get_running_loop().create_task(
        asyncio.to_thread(sync_twilio_voice_url)
    )


# ---------------------------------------------------------------------------
# Booking + persona + demo-data endpoints (first-class SARA voice APIs)
# ---------------------------------------------------------------------------

class BusinessContextUpdate(BaseModel):
    business_context: dict



# ---------------------------------------------------------------------------
# Runtime config — persona / business context (WhatsApp-agent parity)
# ---------------------------------------------------------------------------

class PersonaUpdate(BaseModel):
    persona: str


class BusinessContextUpdate(BaseModel):
    business_context: dict


@app.get("/voice/config")
def voice_config() -> Dict[str, Any]:
    """Full agent configuration — persona, business context, model, channels."""
    row, _ = _primary_voice_channel()
    return {
        "status": "ok",
        "persona": voice_persona_load(),
        "persona_source": (
            "voice_persona.json"
            if VOICE_PERSONA_FILE.exists()
            else "env (VOICE_AGENT_PERSONA)"
        ),
        "business_context_loaded": VOICE_BUSINESS_FILE.exists(),
        "business_context": json.loads(voice_business_load() or "{}"),
        "model": LLM_MODEL,
        "primary_channel": _channel_row_to_dict(row) if row else None,
        "providers": {
            "telephony": {
                "provider": telephony_provider.name,
                "capabilities": telephony_provider.capabilities(),
            },
            "messaging": {
                "provider": _channel_messaging.name,
                "capabilities": {
                    "whatsapp": _channel_messaging.can_send(MessageChannel.WHATSAPP, "x"),
                    "sms": _channel_messaging.can_send(MessageChannel.SMS, "x"),
                },
            },
        },
    }


@app.put("/voice/config/persona")
def voice_config_persona(payload: PersonaUpdate) -> Dict[str, Any]:
    return {
        "status": "ok",
        "persona": voice_persona_save(payload.persona),
        "source": "voice_persona.json",
    }


@app.delete("/voice/config/persona")
def voice_config_persona_reset() -> Dict[str, Any]:
    return {
        "status": "ok",
        "persona": voice_persona_reset(),
        "source": "env (VOICE_AGENT_PERSONA)",
    }


@app.get("/voice/config/business-context")
def voice_config_business_get() -> Dict[str, Any]:
    return {
        "business_context": json.loads(voice_business_load() or "{}"),
        "file": str(VOICE_BUSINESS_FILE),
    }


@app.put("/voice/config/business-context")
def voice_config_business_put(payload: BusinessContextUpdate) -> Dict[str, Any]:
    voice_business_save(payload.business_context)
    return {"status": "ok", "business_context_loaded": True}


# ---------------------------------------------------------------------------
# Memory viewer (WhatsApp-agent parity)
# ---------------------------------------------------------------------------

@app.get("/voice/memory")
def voice_memory(limit: int = 50) -> Dict[str, Any]:
    """Persistent customer memory — customers, facts, latest conversation turns."""
    with SessionLocal() as db:
        customers = db.scalars(
            select(Customer).order_by(Customer.updated_at.desc()).limit(limit)
        ).all()
        rows = []
        for c in customers:
            rows.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "phone": c.phone,
                    "language": c.language,
                    "timezone": c.timezone,
                    "intent": c.intent,
                    "sentiment": c.sentiment,
                    "lifecycle_stage": c.lifecycle_stage,
                    "qualification_score": c.qualification_score,
                    "next_action": c.next_action,
                    "memory": c.memory or {},
                    "custom_fields": c.custom_fields or {},
                    "call_count": c.call_count,
                    "message_count": c.message_count,
                    "last_call_at": c.last_call_at.isoformat() if c.last_call_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                }
            )
        events = db.scalars(
            select(Event)
            .order_by(Event.timestamp.desc())
            .limit(30)
        ).all()
        return {
            "customers": rows,
            "recent_events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "actor": e.actor,
                    "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                    "payload": e.payload or {},
                }
                for e in events
            ],
        }


@app.get("/voice/knowledge/documents")
def voice_knowledge_documents() -> Dict[str, Any]:
    """All stored knowledge documents (uploaded PDF/CSV/TXT/JSON/Excel)."""
    with SessionLocal() as db:
        docs = db.scalars(
            select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())
        ).all()
        return {
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "classification": d.classification,
                    "confidence": d.confidence,
                    "processing_status": d.processing_status,
                    "size": (d.metadata_json or {}).get("size"),
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in docs
            ]
        }


# ---------------------------------------------------------------------------
# Voice channels — connect your own Twilio / Meta / webhook provider
# ---------------------------------------------------------------------------

class ChannelCreateRequest(BaseModel):
    name: str = ""
    provider: str
    phone_number: str = ""
    credentials: dict = Field(default_factory=dict)
    primary: bool = False


class ChannelUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    enabled: Optional[bool] = None
    credentials: Optional[dict] = None


@app.get("/voice/channels")
def voice_channels_list() -> Dict[str, Any]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(VoiceChannel).order_by(VoiceChannel.created_at)
        ).all()
        return {
            "channels": [_channel_row_to_dict(r) for r in rows],
            "providers": [
                {
                    "id": pid,
                    "label": info["label"],
                    "icon": info["icon"],
                    "fields": info["fields"],
                    "help": info["help"],
                }
                for pid, info in VOICE_CHANNEL_PROVIDERS.items()
            ],
        }


@app.post("/voice/channels")
def voice_channels_create(payload: ChannelCreateRequest) -> Dict[str, Any]:
    if payload.provider not in VOICE_CHANNEL_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider '{payload.provider}' (choose from {', '.join(VOICE_CHANNEL_PROVIDERS)})",
        )
    with SessionLocal() as db:
        if payload.primary:
            db.query(VoiceChannel).update({VoiceChannel.is_primary: False})
        row = VoiceChannel(
            id=new_id("vch"),
            name=payload.name or VOICE_CHANNEL_PROVIDERS[payload.provider]["label"],
            provider=payload.provider,
            phone_number=payload.phone_number or "",
            credentials_enc=_channel_encrypt(payload.credentials),
            status="connecting",
            enabled=True,
            is_primary=payload.primary,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "ok", "channel": _channel_row_to_dict(row)}


@app.patch("/voice/channels/{channel_id}")
def voice_channels_update(channel_id: str, payload: ChannelUpdateRequest) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        if payload.name is not None:
            row.name = payload.name
        if payload.phone_number is not None:
            row.phone_number = payload.phone_number
        if payload.credentials:
            merged = _channel_decrypt(row.credentials_enc)
            merged.update({k: v for k, v in payload.credentials.items() if v not in (None, "")})
            row.credentials_enc = _channel_encrypt(merged)
        if payload.enabled is not None:
            row.enabled = payload.enabled
            if not payload.enabled and row.is_primary:
                # never leave a disconnected channel as the primary
                row.is_primary = False
        db.commit()
        db.refresh(row)
        return {"status": "ok", "channel": _channel_row_to_dict(row)}


@app.delete("/voice/channels/{channel_id}")
def voice_channels_delete(channel_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        was_primary = bool(row.is_primary)
        db.delete(row)
        db.commit()
        if was_primary:
            # Never leave the agent without a primary — promote the oldest enabled channel.
            nxt = db.scalar(
                select(VoiceChannel)
                .where(VoiceChannel.enabled == True)  # noqa: E712
                .order_by(VoiceChannel.created_at)
                .limit(1)
            )
            if nxt is not None:
                nxt.is_primary = True
                db.commit()
        return {"deleted": True, "id": channel_id}


@app.post("/voice/channels/{channel_id}/test")
async def voice_channels_test(channel_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        creds = _channel_decrypt(row.credentials_enc)

    def _ok(msg: str) -> dict:
        with SessionLocal() as db:
            db.query(VoiceChannel).filter(VoiceChannel.id == channel_id).update(
                {"status": "connected", "last_error": None}
            )
            db.commit()
        return {"ok": True, "detail": msg}

    def _fail(err: str) -> dict:
        with SessionLocal() as db:
            db.query(VoiceChannel).filter(VoiceChannel.id == channel_id).update(
                {"status": "error", "last_error": str(err)[:400]}
            )
            db.commit()
        return {"ok": False, "error": str(err)[:300]}

    try:
        if row.provider == "twilio":
            sid, token = creds.get("account_sid", ""), creds.get("auth_token", "")
            if not (sid and token):
                # The channel's stored blob is unreadable (rotated
                # SARA_MASTER_KEY). Rather than failing a channel whose
                # backend/.env credentials are perfectly valid, heal it in
                # place and retry — this is what unblocks a primary channel
                # that has been stuck reporting "account_sid and auth_token
                # are required" and silently dropped every inbound call.
                sid, token, _num = _twilio_creds()
                if not (sid and token):
                    return _fail(
                        "account_sid and auth_token are required (set TWILIO_ACCOUNT_SID "
                        "and TWILIO_AUTH_TOKEN in backend/.env, or re-enter them in the UI)"
                    )
                with SessionLocal() as db:
                    db.query(VoiceChannel).filter(VoiceChannel.id == channel_id).update(
                        {
                            "credentials_enc": _channel_encrypt(
                                {
                                    "account_sid": sid,
                                    "auth_token": token,
                                    "phone_number": row.phone_number or _num or "",
                                }
                            )
                        }
                    )
                    db.commit()
                log.info("voice channel %s re-keyed from env during test", channel_id)
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                    auth=(sid, token),
                )
            if r.status_code == 200:
                data = r.json()
                return _ok(f"Twilio account {data.get('friendly_name', sid)} — credentials valid.")
            return _fail(f"Twilio {r.status_code}: {r.text[:200]}")
        if row.provider == "meta":
            token, pid = creds.get("access_token", ""), creds.get("phone_number_id", "")
            if not (token and pid):
                return _fail("access_token and phone_number_id are required")
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(
                    f"https://graph.facebook.com/v21.0/{pid}",
                    params={"fields": "display_phone_number,verified_name,quality_rating", "access_token": token},
                )
            if r.status_code == 200:
                data = r.json()
                return _ok(f"{data.get('verified_name', '')} · {data.get('display_phone_number', '')} (quality {data.get('quality_rating', 'n/a')})")
            return _fail(f"Graph API {r.status_code}: {r.text[:200]}")
        if row.provider == "generic":
            if creds.get("send_url"):
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    r = await client.get(creds["send_url"], headers={"accept": "application/json"})
                return _ok(f"send_url reachable (HTTP {r.status_code})")
            return _ok("Webhook-only channel — inbound will work, outbound needs send_url.")
        return _fail(f"unknown provider '{row.provider}'")
    except Exception as exc:
        return _fail(str(exc))


@app.post("/voice/channels/{channel_id}/primary")
def voice_channels_primary(channel_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        db.query(VoiceChannel).update({VoiceChannel.is_primary: False})
        row.is_primary = True
        row.enabled = True
        row.status = "connected"
        db.commit()
        db.refresh(row)
        return {"status": "ok", "channel": _channel_row_to_dict(row)}


# ---------------------------------------------------------------------------
# Voice channel webhooks — inbound SMS / WhatsApp / calls route to the agent
# ---------------------------------------------------------------------------

async def _handle_channel_inbound(
    channel_id: str, *, phone: str, body: str, message_id: str, sender_name: str = ""
) -> Dict[str, Any]:
    """Route an inbound SMS/WhatsApp message through the voice agent loop."""
    from app.core.agent_switch import is_running as _is_running

    if not _is_running("voice"):
        return {"status": "paused"}
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None or not row.enabled:
            return {"status": "ignored", "reason": "channel unknown or disabled"}
        if not phone:
            return {"status": "ignored", "reason": "missing sender phone"}

        business_id = "biz_demo"
        customer = get_customer(db, business_id, phone=phone)
        if customer is None:
            customer = upsert_customer(
                db,
                CustomerInput(
                    business_id=business_id,
                    phone=phone,
                    name=sender_name or None,
                ),
            )
        db.commit()

        intent = classify_message_intent(body)
        record_event(
            db,
            business_id=business_id,
            customer_id=customer.id,
            event_type="TEXT_RECEIVED",
            source="voice_channel",
            actor="customer",
            payload={"body": body[:500], "intent": intent, "channel_id": channel_id},
        )
        db.commit()

    turn = await llm_reply(business_id, body, channel="sms")
    reply = turn.get("reply", "")

    # Deliver the reply through the primary channel (SMS/WhatsApp).
    with SessionLocal() as db:
        customer = get_customer(db, business_id, customer_id=customer.id)
        if customer and reply:
            send_message(
                db,
                business_id=business_id,
                customer=customer,
                body=reply,
                preferred_channel="WHATSAPP" if "whatsapp" in (phone or "").lower() else "AUTOMATIC",
            )
    return {"status": "accepted", "intent": intent, "reply": reply[:200]}


@app.post("/voice/channels/{channel_id}/webhook")
async def voice_channel_webhook(channel_id: str, request: Request) -> Dict[str, Any]:
    """Inbound text webhook — Twilio form data, Meta JSON, or generic JSON."""
    with SessionLocal() as db:
        row = db.scalar(select(VoiceChannel).where(VoiceChannel.id == channel_id))
        if row is None:
            raise HTTPException(status_code=404, detail="voice channel not found")
        creds = _channel_decrypt(row.credentials_enc)

    # Generic JSON shape first (fast path): {message_id, text/body, from/sender, name}
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if isinstance(payload, dict) and (payload.get("text") or payload.get("body") or payload.get("message")):
        text = str(payload.get("text") or payload.get("body") or payload.get("message") or "")
        sender = str(payload.get("from") or payload.get("sender") or payload.get("phone") or "")
        mid = str(payload.get("message_id") or payload.get("id") or payload.get("messageId") or "")
        if text.strip() and sender:
            return await _handle_channel_inbound(
                channel_id, phone=sender, body=text.strip(), message_id=mid
            )

    # Meta WhatsApp Cloud API webhook (entry[].changes[].value.messages[])
    if row.provider == "meta" and isinstance(payload, dict) and payload.get("entry"):
        try:
            msg = (payload["entry"][0].get("changes") or [{}])[0].get("value", {}).get("messages") or [None]
            msg = msg[0]
            if msg:
                text = (msg.get("text") or {}).get("body", "")
                sender = str(msg.get("from") or "")
                mid = str(msg.get("id") or "")
                return await _handle_channel_inbound(
                    channel_id, phone=sender, body=text.strip(), message_id=mid
                )
        except Exception as exc:
            log.warning("Meta webhook parse failed: %s", exc)
        return {"status": "ignored"}

    # Twilio form data: From, Body, MessageSid, To
    form = await request.form()
    text = str(form.get("Body") or "").strip()
    sender = str(form.get("From") or "").strip()
    mid = str(form.get("MessageSid") or "").strip()
    if text and sender:
        return await _handle_channel_inbound(channel_id, phone=sender, body=text, message_id=mid)

    return {"status": "ignored", "reason": "no parseable message"}


@app.post("/voice/channels/{channel_id}/voice")
async def voice_channel_inbound_call(channel_id: str, request: Request) -> Response:
    """Inbound Twilio CALL webhook — answer the phone and start talking.

    Two problems this fixes:

    * It used to `await llm_reply(...)` *before* answering. That LLM call can
      take longer than Twilio's 15 s webhook limit, so the whole call died with
      "an application error has occurred". The greeting now comes from the warm
      Sarvam phrase cache — no LLM on the critical path.
    * The <Gather> was hardcoded to `language='en-US'` and ignored DTMF, so a
      Hindi caller was transcribed in English and a trial-account keypress was
      swallowed. It now uses the shared `_gather_attrs()`.
    """
    form = await request.form()
    from_number = str(form.get("From") or "")
    call_sid = str(form.get("CallSid") or "")
    base = PUBLIC_BASE_URL or "http://localhost:8000"

    # Mounted sub-apps get no startup event, so warm the phrase cache lazily.
    _schedule_phrase_warmup()

    # Register the inbound call as a customer + call record (parity with
    # webhooks/provider), and link the provider id so transfers can find it.
    if from_number:
        with SessionLocal() as db:
            customer = get_customer(db, "biz_demo", phone=from_number)
            if customer is None:
                customer = upsert_customer(
                    db,
                    CustomerInput(business_id="biz_demo", phone=from_number),
                )
            call = create_call(
                db,
                CallCreate(
                    business_id="biz_demo",
                    customer_id=customer.id,
                    objective="inbound channel call",
                    direction="INBOUND",
                ),
            )
            call.provider_call_id = call_sid
            db.commit()

    # Preferred path: hand the call to the full-duplex media stream. The
    # session greets the caller itself and then holds a real conversation.
    connect = _twiml_connect_stream()
    if connect:
        return Response(content=f"<Response>{connect}</Response>", media_type="application/xml")

    # Fallback (no public URL): TwiML greeting from cache, then the Gather loop.
    ctx = booking_engine.get_context(f"twilio:{call_sid}" if call_sid else "twilio:unknown")
    ctx.phase = BookingPhase.GREETING.value
    greeting = _phrase_for("greeting", ctx.language or DEFAULT_LANGUAGE)
    audio = _instant_speak(greeting, ctx.language or DEFAULT_LANGUAGE)
    return Response(
        content=_twiml_speak_gather_lang(
            audio, greeting, base, ctx.language or DEFAULT_LANGUAGE
        ),
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Booking / persona / demo-hotel / languages — first-class voice APIs
# ---------------------------------------------------------------------------

class HotelPersonaUpdate(BaseModel):
    """Partial persona update — unset fields keep the working defaults."""
    name: Optional[str] = None
    role: Optional[str] = None
    business_name: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    greeting: Optional[str] = None
    booking_rules: Optional[List[str]] = None
    cancellation_rules: Optional[List[str]] = None
    transfer_rules: Optional[Dict[str, Any]] = None
    business_hours: Optional[Dict[str, str]] = None
    escalation_rules: Optional[List[str]] = None
    knowledge_base: Optional[str] = None
    receptionist_number: Optional[str] = None
    booking_system: Optional[str] = None
    fallback_behavior: Optional[str] = None
    reset: bool = False


class DemoHotelUpdate(BaseModel):
    """Partial demo-hotel update (inventory, blocked dates, receptionist)."""
    receptionist_number: Optional[str] = None
    blocked_dates: Optional[List[str]] = None
    rooms: Optional[List[Dict[str, Any]]] = None
    hotel: Optional[Dict[str, Any]] = None


class AvailabilityCheckRequest(BaseModel):
    date: str
    guests: int = 0
    service: Optional[str] = None
    alternatives_window: int = 2


class TransferRequest(BaseModel):
    call_id: Optional[str] = None
    session_key: Optional[str] = None
    destination: Optional[str] = None


@app.get("/voice/booking/persona")
def voice_booking_persona_get() -> Dict[str, Any]:
    persona = booking_persona()
    return {
        "status": "ok",
        "persona": persona.to_dict(),
        "is_default": persona.is_default,
        "config_file": str(VOICE_PERSONA_CONFIG_FILE),
    }


@app.put("/voice/booking/persona")
def voice_booking_persona_put(payload: HotelPersonaUpdate) -> Dict[str, Any]:
    if payload.reset:
        try:
            VOICE_PERSONA_CONFIG_FILE.unlink()
        except FileNotFoundError:
            pass
        persona = booking_persona()
        return {"status": "ok", "persona": persona.to_dict(), "is_default": persona.is_default}

    data = payload.model_dump(exclude_none=True)
    data.pop("reset", None)
    if not data:
        raise HTTPException(status_code=422, detail="no persona fields to update")
    persona = _persona_save(VOICE_PERSONA_CONFIG_FILE, data)
    booking_engine.persona = persona
    return {"status": "ok", "persona": persona.to_dict(), "is_default": persona.is_default}


@app.get("/voice/booking/demo-hotel")
def voice_booking_demo_hotel() -> Dict[str, Any]:
    with demo_hotel._lock:
        data = json.loads(json.dumps(demo_hotel._data))
    data["bookings"] = data.get("bookings", [])[-10:]
    return {
        "status": "ok",
        "hotel": data.get("hotel", {}),
        "rooms": data.get("rooms", []),
        "blocked_dates": data.get("blocked_dates", []),
        "receptionist_number": data.get("receptionist_number", ""),
        "recent_bookings": data.get("bookings", []),
    }


@app.put("/voice/booking/demo-hotel")
def voice_booking_demo_hotel_put(payload: DemoHotelUpdate) -> Dict[str, Any]:
    with demo_hotel._lock:
        if payload.receptionist_number is not None:
            demo_hotel._data["receptionist_number"] = payload.receptionist_number.strip()
        if payload.blocked_dates is not None:
            demo_hotel._data["blocked_dates"] = payload.blocked_dates
        if payload.rooms is not None:
            demo_hotel._data["rooms"] = payload.rooms
        if payload.hotel is not None:
            demo_hotel._data["hotel"].update(payload.hotel)
        demo_hotel._save()
        data = json.loads(json.dumps(demo_hotel._data))
    return {"status": "ok", **{"hotel": data.get("hotel", {}), "rooms": data.get("rooms", [])}}


@app.get("/voice/booking/bookings")
def voice_booking_bookings(limit: int = 50) -> Dict[str, Any]:
    return {"status": "ok", "bookings": demo_hotel.list_bookings(limit=limit)}


@app.post("/voice/booking/check")
def voice_booking_check(payload: AvailabilityCheckRequest) -> Dict[str, Any]:
    """Direct availability probe (the exact call the voice flow makes)."""
    try:
        target = date.fromisoformat(payload.date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="date must be ISO yyyy-mm-dd")
    result = demo_hotel.check_date(
        target,
        guests=payload.guests or None,
        service=payload.service,
    )
    nearby = demo_hotel.nearby_availability(
        target,
        guests=payload.guests or None,
        service=payload.service,
        window=max(0, min(7, payload.alternatives_window)),
    )
    return {
        "status": "ok",
        "availability": result.to_dict(),
        "nearby_available": [d.isoformat() for d in nearby],
    }


@app.post("/voice/booking/transfer")
async def voice_booking_transfer(
    payload: TransferRequest, _: None = Depends(require_api_key)
) -> Dict[str, Any]:
    """Owner-triggered receptionist transfer for a live call (authenticated use).

    Requires the shared SARA API key: without it anyone who could reach the
    public tunnel could bridge a call to an arbitrary phone number.
    """
    if not payload.call_id and not payload.session_key:
        raise HTTPException(status_code=422, detail="call_id or session_key required")
    destination = payload.destination or booking_persona().receptionist_number
    if not destination:
        raise HTTPException(status_code=422, detail="no receptionist number configured")
    if not _looks_like_phone(destination):
        raise HTTPException(status_code=422, detail="destination must be a phone number")
    result = await _voice_transfer(
        {
            "session_key": payload.session_key or "",
            "call_id": payload.call_id,
            "destination": destination,
        }
    )
    return {"status": "ok", **result}


def _looks_like_phone(value: str) -> bool:
    """Loose E.164 check — guards the transfer against TwiML/URL injection."""
    digits = re.sub(r"[^0-9+]", "", value or "")
    return 8 <= len(re.sub(r"\D", "", digits)) <= 15 and digits[0] in "+0123456789"


@app.get("/voice/languages")
def voice_languages_list() -> Dict[str, Any]:
    """Languages the voice agent can genuinely hold a call in."""
    return {
        "status": "ok",
        "default_language": DEFAULT_LANGUAGE,
        "languages": supported_language_list(),
    }


@app.post("/voice/stt-translate")
async def voice_stt_translate(req: TranscriptRequest) -> Dict[str, Any]:
    """STT + translate (any Indic audio -> English text) for multilingual calls."""
    try:
        audio = _b64.b64decode(req.audio_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid base64 audio")
    if not sarvam_client.configured:
        raise HTTPException(status_code=503, detail="SARVAM_API_KEY not configured")
    try:
        transcript = await sarvam_client.transcribe_translate(audio)
    except SarvamError as exc:
        log.warning(
            "stt-translate failed kind=%s status=%s", exc.kind, getattr(exc, "status", None)
        )
        raise HTTPException(status_code=503, detail=exc.safe_message)
    return {"text": transcript.text, "language_code": transcript.language_code}


# ---------------------------------------------------------------------------
# Dynamic Election Campaign Voice Agent — tenant-scoped API
#
# Every route is scoped to the CALLER's tenant (``current_tenant`` -> the
# existing Clerk/API-key tenancy) and to that tenant's own rows, so one
# user's campaign configuration, contacts and calls are invisible to another
# user. The telephony, the call state machine and the webhooks are the shared
# ones the original voice agent already uses.
# ---------------------------------------------------------------------------

from app.api.deps import current_tenant  # noqa: E402
from app.agents.sara_ultron_agent.core.tenancy import scope_key  # noqa: E402


def _campaign_scope(tenant) -> str:
    """The tenant scope key every campaign row is stored under."""
    return scope_key(tenant)


class CampaignProfileCreate(BaseModel):
    name: str = "Election Campaign Agent"
    profile: Dict[str, Any] = Field(default_factory=dict)
    publish: bool = True


class CampaignProfileUpdate(BaseModel):
    name: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None
    publish: bool = False


class CampaignContactCreate(BaseModel):
    name: Optional[str] = None
    phone: str
    language: Optional[str] = None


class CampaignCallCreate(BaseModel):
    profile_id: str
    customer_id: Optional[str] = None
    phone: Optional[str] = None
    objective: Optional[str] = None


class CampaignSimulate(BaseModel):
    message: str
    session_key: Optional[str] = None


def _validate_profile_payload(payload: Dict[str, Any]) -> CampaignProfile:
    """Validate a submitted profile; raise 422 with a useful message."""
    try:
        return CampaignProfile(**(payload or {}))
    except Exception as exc:  # noqa: BLE001 - pydantic error -> 422
        raise HTTPException(
            status_code=422, detail=f"Invalid campaign configuration: {exc}"
        )


def _campaign_profile_or_404(db: Session, profile_id: str, business_id: str):
    row = get_campaign_profile_row(db, profile_id, business_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign agent not found")
    return row


@app.get("/voice/campaign/profiles")
def campaign_profiles_list(tenant=Depends(current_tenant)) -> Dict[str, Any]:
    """Every campaign agent owned by the caller."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        rows = list_campaign_profiles(db, business_id)
        return {
            "status": "ok",
            "business_id": business_id,
            "profiles": [campaign_profile_to_dict(row) for row in rows],
        }


@app.post("/voice/campaign/profiles")
def campaign_profiles_create(
    payload: CampaignProfileCreate, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """Create a campaign agent from the shipped demo profile."""
    business_id = _campaign_scope(tenant)
    profile = _validate_profile_payload(payload.profile or {})
    name = (payload.name or profile.agent_name or "Election Campaign Agent").strip()
    with SessionLocal() as db:
        existing = db.scalar(
            select(CampaignProfileRow).where(
                CampaignProfileRow.business_id == business_id,
                CampaignProfileRow.name == name,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"A campaign agent named {name!r} already exists",
            )
        data = profile.model_dump()
        row = CampaignProfileRow(
            id=new_id("campaign"),
            business_id=business_id,
            name=name,
            agent_kind=CAMPAIGN_AGENT_KIND,
            status=(
                CAMPAIGN_PROFILE_STATUS_PUBLISHED
                if payload.publish
                else CAMPAIGN_PROFILE_STATUS_DRAFT
            ),
            version=1,
            profile=data,
            draft=data,
            published_at=utcnow() if payload.publish else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "status": "ok",
            "profile": campaign_profile_to_dict(row),
        }


@app.get("/voice/campaign/profiles/{profile_id}")
def campaign_profiles_get(
    profile_id: str, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """The saved configuration of one campaign agent."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        return {"status": "ok", "profile": campaign_profile_to_dict(row)}


@app.put("/voice/campaign/profiles/{profile_id}")
def campaign_profiles_update(
    profile_id: str,
    payload: CampaignProfileUpdate,
    tenant=Depends(current_tenant),
) -> Dict[str, Any]:
    """Save the campaign configuration.

    ``publish=false`` keeps the edit as a DRAFT (the live agent keeps using
    the last published version). ``publish=true`` validates the values,
    stores them and publishes them for the next call.
    """
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(status_code=422, detail="name cannot be empty")
            clash = db.scalar(
                select(CampaignProfileRow).where(
                    CampaignProfileRow.business_id == business_id,
                    CampaignProfileRow.name == name,
                    CampaignProfileRow.id != profile_id,
                )
            )
            if clash is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"A campaign agent named {name!r} already exists",
                )
            row.name = name
        if payload.profile is not None:
            merged = dict(row.draft or row.profile or {})
            merged.update(payload.profile)
            profile = _validate_profile_payload(merged)
            row.draft = profile.model_dump()
            if payload.publish:
                row.profile = profile.model_dump()
                row.status = CAMPAIGN_PROFILE_STATUS_PUBLISHED
                row.version = (row.version or 0) + 1
                row.published_at = utcnow()
        elif payload.publish:
            profile = _validate_profile_payload(dict(row.draft or row.profile or {}))
            row.profile = profile.model_dump()
            row.status = CAMPAIGN_PROFILE_STATUS_PUBLISHED
            row.version = (row.version or 0) + 1
            row.published_at = utcnow()
        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
        return {
            "status": "ok",
            "published": row.status == CAMPAIGN_PROFILE_STATUS_PUBLISHED,
            "profile": campaign_profile_to_dict(row),
        }


@app.post("/voice/campaign/profiles/{profile_id}/publish")
def campaign_profiles_publish(
    profile_id: str, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """Publish the current draft so the next call uses it."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        profile = _validate_profile_payload(dict(row.draft or row.profile or {}))
        row.profile = profile.model_dump()
        row.status = CAMPAIGN_PROFILE_STATUS_PUBLISHED
        row.version = (row.version or 0) + 1
        row.published_at = utcnow()
        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
        return {"status": "ok", "profile": campaign_profile_to_dict(row)}


@app.delete("/voice/campaign/profiles/{profile_id}")
def campaign_profiles_delete(
    profile_id: str, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        db.delete(row)
        db.commit()
        return {"status": "ok", "deleted": profile_id}


@app.get("/voice/campaign/profiles/{profile_id}/preview")
def campaign_profiles_preview(
    profile_id: str, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """The exact lines the agent will speak, with placeholders resolved."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        published = _profile_payload_to_model(row.profile)
        draft = _profile_payload_to_model(row.draft or row.profile)
        return {
            "status": "ok",
            "published": published.rendered_sections(),
            "published_faqs": published.rendered_faqs(),
            "draft": draft.rendered_sections(),
            "draft_faqs": draft.rendered_faqs(),
            "language": published.language,
            "version": row.version,
        }


@app.post("/voice/campaign/profiles/{profile_id}/simulate")
async def campaign_profiles_simulate(
    profile_id: str,
    payload: CampaignSimulate,
    tenant=Depends(current_tenant),
) -> Dict[str, Any]:
    """Run one conversation turn against the saved agent — no call, no charge."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, profile_id, business_id)
        profile = _profile_payload_to_model(row.profile)
    session_key = payload.session_key or f"sim:{new_id('sim')}"
    engine = CampaignConversationEngine(
        profile_fn=lambda _key: profile,
        llm_fn=_booking_llm,
        transfer_fn=_voice_transfer,
        translator_fn=_booking_translate,
    )
    result = await engine.handle_turn(session_key, payload.message)
    return {
        "status": "ok",
        "reply": result.reply,
        "events": result.events,
        "llm_used": result.llm_used,
        "session_key": session_key,
    }


@app.get("/voice/campaign/contacts")
def campaign_contacts_list(
    limit: int = 50, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """The caller's own contacts, with their calling eligibility."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        customers = db.scalars(
            select(Customer)
            .where(Customer.business_id == business_id)
            .order_by(Customer.updated_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return {
            "status": "ok",
            "contacts": [_campaign_contact_dict(c) for c in customers],
        }


def _campaign_contact_dict(customer: Customer) -> Dict[str, Any]:
    blocked = bool(customer.do_not_contact)
    consent = (customer.consent_status or "").upper()
    eligible = not blocked and consent not in {"OPTED_OUT", "DECLINED", "REVOKED"}
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "normalized_phone": customer.normalized_phone,
        "language": customer.language,
        "do_not_contact": blocked,
        "consent_status": customer.consent_status,
        "eligible": eligible,
        "reason": (
            "opted out" if blocked
            else f"consent {consent.lower()}" if consent in {"OPTED_OUT", "DECLINED", "REVOKED"}
            else ""
        ),
        "call_count": customer.call_count,
        "last_call_at": json_safe(customer.last_call_at),
        "updated_at": json_safe(customer.updated_at),
    }


@app.post("/voice/campaign/contacts")
def campaign_contacts_create(
    payload: CampaignContactCreate, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """Add a contact for the caller. Nothing is dialled by saving a contact."""
    business_id = _campaign_scope(tenant)
    phone = normalize_phone(payload.phone)
    if not phone:
        raise HTTPException(status_code=422, detail="a valid phone number is required")
    with SessionLocal() as db:
        customer = db.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.normalized_phone == phone,
            )
        )
        if customer is None:
            customer = upsert_customer(
                db,
                CustomerInput(
                    business_id=business_id,
                    name=payload.name,
                    phone=phone,
                    language=payload.language,
                ),
            )
        elif payload.name and not customer.name:
            customer.name = payload.name
        db.commit()
        db.refresh(customer)
        return {"status": "ok", "contact": _campaign_contact_dict(customer)}


@app.get("/voice/campaign/calls")
def campaign_calls_list(
    limit: int = 25, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """The caller's campaign calls, with the live provider state."""
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        calls = db.scalars(
            select(Call)
            .where(
                Call.business_id == business_id,
                Call.agent_profile_id.isnot(None),
            )
            .order_by(Call.created_at.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
        rows = []
        for call in calls:
            customer = db.get(Customer, call.customer_id) if call.customer_id else None
            rows.append(
                {
                    "id": call.id,
                    "customer_name": customer.name if customer else None,
                    "phone": customer.phone if customer else None,
                    "state": call.state,
                    "direction": call.direction,
                    "objective": call.objective,
                    "provider_call_id": call.provider_call_id,
                    "created_at": json_safe(call.created_at),
                    "started_at": json_safe(call.started_at),
                    "ended_at": json_safe(call.ended_at),
                    "summary": call.summary,
                    "outcome": call.outcome,
                }
            )
        return {"status": "ok", "calls": rows}


@app.post("/voice/campaign/calls")
def campaign_calls_create(
    payload: CampaignCallCreate, tenant=Depends(current_tenant)
) -> Dict[str, Any]:
    """Start one campaign call through the existing telephony integration.

    The request is validated (ownership, published configuration, calling
    window, contact eligibility) before the provider is asked to dial, and
    the provider's own answer is returned verbatim — the call is never
    reported as connected before the provider confirms it.
    """
    business_id = _campaign_scope(tenant)
    with SessionLocal() as db:
        row = _campaign_profile_or_404(db, payload.profile_id, business_id)
        if row.status != CAMPAIGN_PROFILE_STATUS_PUBLISHED:
            raise HTTPException(
                status_code=409,
                detail="Publish this campaign agent before calling with it.",
            )
        profile = _profile_payload_to_model(row.profile)

        customer = None
        if payload.customer_id:
            customer = db.get(Customer, payload.customer_id)
            if customer is None or customer.business_id != business_id:
                raise HTTPException(status_code=404, detail="Contact not found")
        elif payload.phone:
            phone = normalize_phone(payload.phone)
            if not phone:
                raise HTTPException(
                    status_code=422, detail="a valid phone number is required"
                )
            customer = upsert_customer(
                db, CustomerInput(business_id=business_id, phone=phone)
            )
        else:
            raise HTTPException(
                status_code=422, detail="customer_id or phone is required"
            )

        if customer.do_not_contact:
            raise HTTPException(
                status_code=403,
                detail="This contact asked not to be called. The opt-out is respected.",
            )
        consent = (customer.consent_status or "").upper()
        if consent in {"OPTED_OUT", "DECLINED", "REVOKED"}:
            raise HTTPException(
                status_code=403,
                detail=f"This contact's consent status is {consent.lower()}.",
            )

        allowed, reason = schedule_allows(profile)
        if not allowed:
            raise HTTPException(status_code=409, detail=reason)

        objective = (payload.objective or "").strip() or (
            f"election campaign outreach: {profile.campaign_name}"
        )
        call = create_call(
            db,
            CallCreate(
                business_id=business_id,
                customer_id=customer.id,
                objective=objective,
                direction="OUTBOUND",
            ),
        )
        # The call speaks with THIS published snapshot, so a later publish
        # cannot change what this call is saying.
        call.agent_profile_id = row.id
        call.agent_profile_snapshot = row.profile
        db.commit()
        db.refresh(call)

        result = execute_call(db, call)
        provider = result.get("provider_result") or {}
        return {
            "status": "ok",
            "call_id": call.id,
            "state": call.state,
            "provider": provider.get("provider") or telephony_provider.name,
            "provider_call_id": provider.get("provider_call_id"),
            "provider_accepted": bool(provider.get("success")),
            "provider_reason": provider.get("reason", ""),
            "campaign": {
                "profile_id": row.id,
                "name": row.name,
                "version": row.version,
                "representative": profile.representative_name,
                "greeting": profile.render(profile.greeting),
            },
        }

