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

import csv
import hashlib
import io
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
from app.core.config import settings as _settings  # noqa: E402
from app.core.llm_router import llm_chat as _failover_chat  # noqa: E402

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
PUBLIC_BASE_URL = (_settings.PUBLIC_BASE_URL or "").rstrip("/")
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


def normalize_phone(phone: str, default_region: str = "US") -> str:
    phone = (phone or "").strip()

    if not phone:
        return ""

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

    digits = re.sub(r"\D", "", phone)

    if len(digits) == 10:
        return f"+1{digits}"

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
    CallState.DIALING: {CallState.RINGING, CallState.CONNECTED, CallState.FAILED},
    CallState.RINGING: {
        CallState.CONNECTED,
        CallState.DISCONNECTED,
        CallState.FAILED,
    },
    CallState.CONNECTED: {
        CallState.GREETING,
        CallState.DISCOVERY,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
        CallState.FAILED,
    },
    CallState.GREETING: {
        CallState.DISCOVERY,
        CallState.INFORMATION,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
    },
    CallState.DISCOVERY: {
        CallState.QUALIFICATION,
        CallState.INFORMATION,
        CallState.BOOKING,
        CallState.CONFIRMATION,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
    },
    CallState.QUALIFICATION: {
        CallState.INFORMATION,
        CallState.BOOKING,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
    },
    CallState.INFORMATION: {
        CallState.BOOKING,
        CallState.CONFIRMATION,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
    },
    CallState.BOOKING: {
        CallState.CONFIRMATION,
        CallState.FOLLOWUP,
        CallState.HUMAN_HANDOFF,
        CallState.DISCONNECTED,
    },
    CallState.CONFIRMATION: {
        CallState.FOLLOWUP,
        CallState.COMPLETED,
        CallState.DISCONNECTED,
    },
    CallState.FOLLOWUP: {
        CallState.COMPLETED,
        CallState.DISCONNECTED,
    },
    CallState.HUMAN_HANDOFF: {
        CallState.COMPLETED,
        CallState.DISCONNECTED,
        CallState.FAILED,
    },
    CallState.DISCONNECTED: {
        CallState.RETRY_PENDING,
        CallState.COMPLETED,
        CallState.FAILED,
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
    query: str,
) -> Dict[str, Any]:
    with SessionLocal() as db:
        return {
            "query": query,
            "results": search_knowledge(db, business_id, query),
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


def _llm_configured() -> bool:
    return bool(LLM_API_KEY)


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


async def tts_speak(text: str, *, language: str = "en-IN") -> dict:
    """Speak `text` with the configured provider; return {url} or {error}."""
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
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.sarvam.ai/text-to-speech",
                    headers={
                        "api-subscription-key": SARVAM_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "inputs": [text],
                        "target_language_code": language,
                        "speaker": SARVAM_TTS_SPEAKER,
                        "model": SARVAM_TTS_MODEL,
                        "speech_sample_rate": 22050,
                    },
                )
                r.raise_for_status()
                data = r.json()
                b64_audio = data.get("audios", [""])[0]
                if not b64_audio:
                    return {"error": f"Sarvam TTS returned no audio: {data}"}
                audio = _b64.b64decode(b64_audio)
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
        return {"url": f"/voice-agent/static/voice/{fname}", "bytes": len(audio)}
    except Exception as exc:
        log.warning("TTS failed: %s", exc)
        return {"error": str(exc)[:300]}


async def stt_transcribe(audio: bytes, *, language: str = "unknown") -> dict:
    """Transcribe call audio with the configured provider (Sarvam batch API)."""
    if not _stt_configured():
        return {
            "error": (
                f"STT not configured (VOICE_STT_PROVIDER={VOICE_STT_PROVIDER!r}). "
                "Set SARVAM_API_KEY in backend/.env."
            )
        }
    try:
        files = {"file": ("audio.wav", audio, "audio/wav")}
        data = {"model": SARVAM_STT_MODEL}
        if language and language != "unknown":
            data["language_code"] = language
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": SARVAM_API_KEY},
                files=files,
                data=data,
            )
            r.raise_for_status()
            out = r.json()
        return {"text": (out.get("transcript") or "").strip()}
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
    """(sid, token, from_number) — primary runtime channel beats env."""
    row, creds = _primary_voice_channel()
    if row and row.provider == "twilio":
        sid = creds.get("account_sid") or ""
        token = creds.get("auth_token") or ""
        number = creds.get("phone_number") or row.phone_number or ""
        if sid and token:
            return sid, token, number
    return TWILIO_ACCOUNT_SID or "", TWILIO_AUTH_TOKEN or "", TWILIO_PHONE_NUMBER or ""


def _twilio_client():
    """Lazily built client for whatever creds are active (env or runtime channel)."""
    sid, token, _ = _twilio_creds()
    if not sid or not token:
        return None
    try:
        from twilio.rest import Client as _TwilioClient

        return _TwilioClient(sid, token)
    except Exception as exc:
        log.warning("Twilio client init failed: %s", exc)
        return None


class TwilioTelephonyProvider:
    name = "twilio"

    def capabilities(self) -> Dict[str, bool]:
        _, _, number = _twilio_creds()
        return {
            "inbound_calls": bool(number),
            "outbound_calls": _twilio_client() is not None and bool(number),
            "websocket_streaming": False,
            "bidirectional_audio": False,
            "recording": False,
            "transfer": False,
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
        return {"success": False, "reason": "Transfer not implemented yet"}


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

# The engine owns its sqlite tables; ensure they exist before seeding channels.
Base.metadata.create_all(bind=engine)
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
# Live voice loop endpoints — the WhatsApp-style "phone" talks here.
# ---------------------------------------------------------------------------

from app.core.agent_switch import is_running, set_running  # noqa: E402


class TalkRequest(BaseModel):
    business_id: str = "biz_demo"
    text: str
    caller_phone: Optional[str] = None
    language: str = "en-IN"
    speak: bool = True


@app.get("/voice/status")
async def voice_status() -> Dict[str, Any]:
    """Live configuration snapshot for the Command Center health chips."""
    return {
        "status": "ok",
        "running": is_running("voice"),
        "llm": {"provider": LLM_MODEL, "configured": _llm_configured()},
        "tts": {"provider": VOICE_TTS_PROVIDER, "configured": _tts_configured()},
        "stt": {"provider": VOICE_STT_PROVIDER, "configured": _stt_configured()},
        "telephony": {
            "provider": telephony_provider.name,
            "capabilities": telephony_provider.capabilities(),
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
    """Live loop: text in -> SARA reply out (+ TTS audio url when configured)."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    if not is_running("voice"):
        raise HTTPException(status_code=409, detail="Voice agent is paused")

    turn = await llm_reply(req.business_id, text, channel="web-voice")
    audio: Dict[str, Any] = {}
    if req.speak and turn.get("reply"):
        audio = await tts_speak(turn["reply"], language=req.language)

    return {
        "reply": turn["reply"],
        "intent": turn.get("intent"),
        "audio_url": audio.get("url"),
        "audio_error": audio.get("error"),
        "llm_configured": turn.get("configured", True),
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


@app.post("/voice/twilio/outbound-answer")
async def twilio_outbound_answer(request: Request) -> Response:
    """Twilio hits this when the callee picks up an outbound call."""
    base = PUBLIC_BASE_URL or "http://localhost:8000"
    turn = await llm_reply("biz_demo", "(call answered — greet the caller)")
    greeting = turn.get("reply") or "Hello! This is SARA. How can I help you today?"

    audio = await tts_speak(greeting)
    if audio.get("url"):
        twiml = (
            f"<Response><Play>{base}{audio['url']}</Play>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    else:
        twiml = (
            "<Response><Say voice='Polly.Aditi' language='en-IN'>"
            f"{greeting}</Say>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/twilio/gather")
async def twilio_gather(request: Request) -> Response:
    """One spoken turn of a live Twilio call: LLM -> TTS -> TwiML."""
    form = await request.form()
    speech = (form.get("SpeechResult") or "").strip()
    base = PUBLIC_BASE_URL or "http://localhost:8000"

    if not speech:
        twiml = (
            "<Response><Say voice='Polly.Aditi' language='en-IN'>"
            "Sorry, I didn't catch that.</Say>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
        return Response(content=twiml, media_type="application/xml")

    turn = await llm_reply("biz_demo", speech, channel="twilio")
    reply = turn.get("reply", "")

    audio = await tts_speak(reply)
    if audio.get("url"):
        twiml = (
            f"<Response><Play>{base}{audio['url']}</Play>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    else:
        safe = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        twiml = (
            f"<Response><Say voice='Polly.Aditi' language='en-IN'>{safe}</Say>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    return Response(content=twiml, media_type="application/xml")
# Serve generated audio: GET /voice-agent/static/voice/<file>.mp3
app.mount("/static/voice", StaticFiles(directory=str(_static_dir())), name="voice_audio")


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
                return _fail("account_sid and auth_token are required")
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
    """Inbound Twilio CALL webhook — greet + run the live voice loop (TwiML)."""
    form = await request.form()
    from_number = str(form.get("From") or "")
    base = PUBLIC_BASE_URL or "http://localhost:8000"

    # Register the inbound call as a customer + call record (parity with webhooks/provider).
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
            call.provider_call_id = str(form.get("CallSid") or "")
            db.commit()

    turn = await llm_reply("biz_demo", "(inbound call answered — greet the caller)")
    greeting = turn.get("reply") or "Hello! This is SARA. How can I help you today?"
    audio = await tts_speak(greeting)
    if audio.get("url"):
        twiml = (
            f"<Response><Play>{base}{audio['url']}</Play>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    else:
        safe = greeting.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        twiml = (
            "<Response><Say voice='Polly.Aditi' language='en-IN'>"
            f"{safe}</Say>"
            f"<Gather input='speech' action='{base}/voice-agent/voice/twilio/gather'"
            f" method='POST' timeout='6' speechTimeout='auto' language='en-US'>"
            f"</Gather></Response>"
        )
    return Response(content=twiml, media_type="application/xml")
