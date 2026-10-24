"""
Email Automation Agent — SARA AI Nexus Hub
==========================================

Production email campaign engine mounted under /email-agent/* on the unified
SARA backend (app.main). One self-contained file, like the other hub engines
(voice_agent_engine, social_media, frontend_developer).

Pipeline
--------
1. Ingest clients from PDF / CSV uploads and Google Sheets.
2. Normalize every source into one Client schema (dedupe + email validation).
3. Store a campaign memory/context (text + files), kept separate from client data.
4. Generate a personalized, human-sounding email per client with the shared
   SARA LLM brain (same LLM_* config as the voice agent) — template fallback.
5. Send sequentially through a provider abstraction:
     * smtp   — Gmail app-password SMTP (identical system to voice_agent.send_gmail_smtp)
                or any generic SMTP host
     * gmail  — Gmail API OAuth2 (reuses the token saved by /api/auth/google)
6. Record every attempt (status, error, message_id, sent_at) before moving on,
   and sync results back into the connected Google Sheet row.
7. Resume safely: Sent clients are never re-sent; Failed can be retried.

Safety: dry-run mode, test-email redirect, retries with exponential backoff,
rate limiting, timeouts, structured logging, idempotency, graceful shutdown,
configurable max emails per run. Critical config errors never fail silently.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email import encoders
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.api.deps import require_api_key
from app.core.config import settings

logger = logging.getLogger("sara.email_agent")

# ---------------------------------------------------------------------------
# Configuration (env-driven; see backend/.env.example)
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]

EMAIL_AGENT_FROM = settings.EMAIL_AGENT_FROM or settings.GMAIL_SENDER or ""
EMAIL_AGENT_RATE_DELAY = float(os.getenv("EMAIL_AGENT_RATE_DELAY", "2.0"))
EMAIL_AGENT_MAX_RETRIES = int(os.getenv("EMAIL_AGENT_MAX_RETRIES", "3"))
EMAIL_AGENT_SEND_TIMEOUT = float(os.getenv("EMAIL_AGENT_SEND_TIMEOUT", "30"))
EMAIL_AGENT_USE_LLM = os.getenv("EMAIL_AGENT_USE_LLM", "true").lower() == "true"
UPLOAD_DIR = Path(os.getenv("EMAIL_AGENT_UPLOAD_DIR", str(BACKEND_DIR / "email_uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_APP_PASSWORDS_URL = "https://myaccount.google.com/apppasswords"

# Attachments (PDF / resume) that ride along with every email of a campaign.
ATTACHMENT_DIR = UPLOAD_DIR / "attachments"
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024          # per file
MAX_ATTACHMENTS_TOTAL_BYTES = 22 * 1024 * 1024   # per campaign (Gmail caps a message near 25 MB)
ALLOWED_ATTACHMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".png", ".jpg", ".jpeg"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHEET_STATUS_HEADER = "Status"

# Sender identity appended to every generated email (campaign.signature overrides it).
DEFAULT_SIGNATURE = (
    "Your sincerely,\n"
    "Sara AI Platform\n"
    "Email: support@example.com\n"
    "WhatsApp: +91 90000 00000"
)

router = APIRouter(prefix="/email-agent", tags=["Email Automation Agent"])


# ---------------------------------------------------------------------------
# Database (dedicated SQLite state — keeps campaign data isolated)
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "ea_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="Campaign")
    status: Mapped[str] = mapped_column(String(30), default="draft")  # draft|running|paused|completed|failed

    # Memory / context (campaign instructions — never mixed with client data)
    context: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")

    # Email composition
    subject: Mapped[str] = mapped_column(String(500), default="")
    tone: Mapped[str] = mapped_column(String(100), default="professional")
    objective: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(Text, default="")

    # Sending configuration
    provider: Mapped[str] = mapped_column(String(30), default="smtp")  # smtp|gmail
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    test_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    max_emails: Mapped[int] = mapped_column(Integer, default=500)
    delay_seconds: Mapped[float] = mapped_column(Float, default=EMAIL_AGENT_RATE_DELAY)
    max_retries: Mapped[int] = mapped_column(Integer, default=EMAIL_AGENT_MAX_RETRIES)

    # Google Sheets linkage
    sheet_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    sheet_range: Mapped[str] = mapped_column(String(100), default="Sheet1!A:Z")
    sheet_column_map: Mapped[str] = mapped_column(Text, default="{}")  # logical->letter header name

    # Run bookkeeping
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class Client(Base):
    __tablename__ = "ea_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    email: Mapped[str] = mapped_column(String(320), index=True)
    company: Mapped[str] = mapped_column(String(300), default="")
    fields_json: Mapped[str] = mapped_column(Text, default="{}")  # extra personalized fields
    source: Mapped[str] = mapped_column(String(100), default="")  # csv|pdf|google_sheet
    row_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sheet_row: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-based sheet row for sync

    status: Mapped[str] = mapped_column(String(30), default="Pending")  # Pending|Sending|Sent|Failed|Skipped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_email: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str] = mapped_column(String(500), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class ContextFile(Base):
    __tablename__ = "ea_context_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Attachment(Base):
    """A real file (PDF / resume / …) attached to every email of a campaign."""

    __tablename__ = "ea_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(300))          # original name shown to the user
    stored_name: Mapped[str] = mapped_column(String(300))       # safe name on disk (uuid + suffix)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class SenderSettings(Base):
    """The mailbox the user connected for sending (single row, id=1).

    Stored in the agent's own SQLite file, exactly like the .env-based
    credentials it overrides. The password is an app password (Gmail) or an
    SMTP password the user explicitly pasted here for this machine.
    """

    __tablename__ = "ea_sender_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    from_email: Mapped[str] = mapped_column(String(320), default="")
    from_name: Mapped[str] = mapped_column(String(120), default="")
    username: Mapped[str] = mapped_column(String(320), default="")
    password: Mapped[str] = mapped_column(Text, default="")
    host: Mapped[str] = mapped_column(String(200), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


_engine = create_engine(
    f"sqlite:///{BACKEND_DIR / 'email_agent.db'}",
    connect_args={"check_same_thread": False},
)
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(_engine)


def _db() -> Session:
    return _SessionLocal()


# ---------------------------------------------------------------------------
# Schemas (API layer)
# ---------------------------------------------------------------------------


class CampaignCreate(BaseModel):
    name: str = "Campaign"
    context: str = ""
    instructions: str = ""
    subject: str = ""
    tone: str = "professional"
    objective: str = ""
    cta: str = ""
    signature: str = ""
    provider: str = Field(default="smtp", pattern="^(smtp|gmail)$")
    dry_run: bool = True
    test_email: Optional[str] = None
    max_emails: int = Field(default=500, ge=1, le=100000)
    delay_seconds: float = Field(default=EMAIL_AGENT_RATE_DELAY, ge=0, le=3600)
    max_retries: int = Field(default=EMAIL_AGENT_MAX_RETRIES, ge=0, le=10)


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    context: Optional[str] = None
    instructions: Optional[str] = None
    subject: Optional[str] = None
    tone: Optional[str] = None
    objective: Optional[str] = None
    cta: Optional[str] = None
    signature: Optional[str] = None
    provider: Optional[str] = Field(default=None, pattern="^(smtp|gmail)$")
    dry_run: Optional[bool] = None
    test_email: Optional[str] = None
    max_emails: Optional[int] = Field(default=None, ge=1, le=100000)
    delay_seconds: Optional[float] = Field(default=None, ge=0, le=3600)
    max_retries: Optional[int] = Field(default=None, ge=0, le=10)


class TextRequest(BaseModel):
    text: str


class SheetConfig(BaseModel):
    spreadsheet_id: str
    range_name: str = "Sheet1!A:Z"
    import_clients: bool = True
    column_map: dict[str, str] = Field(default_factory=dict)  # logical -> header name


class PreviewRequest(BaseModel):
    client_id: str


class TestSendRequest(BaseModel):
    email: Optional[str] = None  # defaults to campaign.test_email
    client_id: Optional[str] = None  # defaults to first Pending client


class SenderSMTPRequest(BaseModel):
    from_email: str
    password: str
    from_name: str = ""
    username: str = ""       # defaults to from_email
    host: str = ""           # defaults to smtp.gmail.com for @gmail.com addresses
    port: int = Field(default=0, ge=0, le=65535)
    ssl: Optional[bool] = None


# ---------------------------------------------------------------------------
# Client normalization (one schema for every source)
# ---------------------------------------------------------------------------


def _clean_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _valid_email(value: str) -> bool:
    return bool(EMAIL_RE.fullmatch(value.strip()))


class NormalizedClient:
    __slots__ = ("name", "email", "company", "fields", "source")

    def __init__(self, name: str, email: str, company: str, fields: dict[str, Any], source: str) -> None:
        self.name = name
        self.email = email
        self.company = company
        self.fields = fields
        self.source = source

    def data(self) -> dict[str, Any]:
        return {"name": self.name, "email": self.email, "company": self.company, **(self.fields or {})}


def normalize_record(raw: dict[str, Any], source: str) -> Optional[NormalizedClient]:
    """Map arbitrary header names onto the internal Client schema."""
    normalized = {_clean_key(str(k)): str(v or "").strip() for k, v in raw.items()}

    def find(*names: str) -> str:
        for name in names:
            key = _clean_key(name)
            if normalized.get(key):
                return normalized[key]
        return ""

    email = find("email", "email address", "e-mail", "mail")
    if not _valid_email(email):
        return None
    name = find("name", "full name", "client name", "contact", "contact name", "first name")
    company = find("company", "organization", "organisation", "business", "firm")
    excluded = {
        "email", "email_address", "e_mail", "mail", "name", "full_name", "client_name",
        "contact", "contact_name", "first_name", "company", "organization", "organisation",
        "business", "firm", "status", "last_email", "sent_at", "error", "message_id", "notes",
    }
    fields = {k: v for k, v in normalized.items() if k not in excluded and v}
    return NormalizedClient(name, email.lower(), company, fields, source)


# ---------------------------------------------------------------------------
# Parsers: CSV / PDF
# ---------------------------------------------------------------------------


def parse_csv(data: bytes, source: str) -> list[NormalizedClient]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    rows = list(reader)
    clients = [c for row in rows if (c := normalize_record(row, source))]
    if clients or not rows:
        return clients
    # Fallback: no header name matched (e.g. "Email ID", "Contact Mail").
    # Find the email column by CONTENT — the one whose values contain '@'.
    cols = list(rows[0].keys())
    email_col = next(
        (col for col in cols if any("@" in str(r.get(col) or "") for r in rows[:25])),
        None,
    )
    if not email_col:
        return []
    name_col = next(
        (
            col
            for col in cols
            if col != email_col
            and all(len(str(r.get(col) or "").strip()) < 60 for r in rows[:5])
        ),
        "",
    )
    out: list[NormalizedClient] = []
    seen: set[str] = set()
    for row in rows:
        email = str(row.get(email_col) or "").strip().lower()
        if _valid_email(email) and email not in seen:
            seen.add(email)
            name = str(row.get(name_col) or "").strip() if name_col else ""
            fields = {
                _clean_key(col): str(row.get(col) or "").strip()
                for col in cols
                if col not in (email_col, name_col) and str(row.get(col) or "").strip()
            }
            out.append(NormalizedClient(name, email, "", fields, source))
    return out


def parse_pdf(data: bytes, source: str) -> list[NormalizedClient]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if not text.strip():
        raise ValueError(
            "no extractable text — the PDF is probably a scanned image. "
            "Use a text-based PDF or a CSV."
        )
    clients: list[NormalizedClient] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", line):
            email_l = email.lower()
            if email_l in seen or not _valid_email(email_l):
                continue
            seen.add(email_l)
            before = line[: line.lower().find(email_l)].strip(" ,;:-|")
            name = before if 2 <= len(before) <= 80 else ""
            clients.append(NormalizedClient(name, email_l, "", {}, source))
    if not clients:
        # Structured fallback: look for "Name: X ... email@y" blocks across the text
        for m in re.finditer(r"([\w .,'-]{2,60})[\s:|]*([\w.+-]+@[\w-]+\.[\w.-]+)", text):
            email_l = m.group(2).lower()
            if email_l not in seen and _valid_email(email_l):
                seen.add(email_l)
                clients.append(NormalizedClient(m.group(1).strip(), email_l, "", {}, source))
    return clients


# ---------------------------------------------------------------------------
# Google Sheets integration (service account, or the OAuth token from /api/auth/google)
# ---------------------------------------------------------------------------


def _sheets_credentials():
    """Return google.auth credentials with the Sheets scope, or raise RuntimeError."""
    try:
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials as UserCredentials
        import google.auth.transport.requests as _requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Google API client libraries are not installed") from exc

    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    svc_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "")
    if svc_json or svc_file:
        try:
            info = json.loads(svc_json) if svc_json else json.loads(Path(svc_file).read_text())
            return service_account.Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
        except Exception as exc:
            raise RuntimeError(f"Invalid Google service-account credentials: {exc}") from exc

    # Fall back to the OAuth token saved by the SARA platform (/api/auth/google)
    try:
        from app.database.database import SessionLocal as MainSession, engine as main_engine
        from app.database.models import Integration as MainIntegration

        with MainSession() as mdb:
            row = mdb.scalars(
                select(MainIntegration).where(MainIntegration.provider == "gmail")
            ).first()
    except Exception as exc:
        raise RuntimeError(f"Could not read the stored Google OAuth integration: {exc}") from exc

    if not row or not row.refresh_token:
        raise RuntimeError(
            "No Google Sheets credentials available. Set GOOGLE_CREDENTIALS_JSON (service "
            "account) in backend/.env, or re-connect Google with the Sheets scope enabled."
        )
    creds = UserCredentials(
        token=row.access_token,
        refresh_token=row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=SHEETS_SCOPES,
    )
    try:
        creds.refresh(_requests.Request())
    except Exception as exc:
        raise RuntimeError(f"Google OAuth refresh failed (re-connect the account): {exc}") from exc
    return creds


def _sheets_service():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_sheets_credentials(), cache_discovery=False)


DEFAULT_COLUMN_MAP = {
    "name": "Name",
    "email": "Email",
    "company": "Company",
    "status": SHEET_STATUS_HEADER,
    "last_email": "Last Email",
    "sent_at": "Sent At",
    "error": "Error",
    "message_id": "Message ID",
    "notes": "Notes",
}


def sheet_read_grid(spreadsheet_id: str, range_name: str) -> list[list[str]]:
    service = _sheets_service()
    result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    return [[str(c) if c is not None else "" for c in row] for row in result.get("values", [])]


def sheet_import(spreadsheet_id: str, range_name: str, campaign_id: str) -> dict[str, int]:
    """Import clients from the sheet (column detection + duplicate protection)."""
    grid = sheet_read_grid(spreadsheet_id, range_name)
    if not grid:
        raise ValueError("The sheet range is empty")
    header = grid[0]
    lookup = {_clean_key(h): idx for idx, h in enumerate(header)}
    col = {logical: lookup.get(_clean_key(name)) for logical, name in DEFAULT_COLUMN_MAP.items()}

    if col.get("email") is None:
        # Fall back: first column that looks like emails
        for idx, name in enumerate(header):
            sample = next((r[idx] for r in grid[1:] if idx < len(r) and "@" in r[idx]), "")
            if "@" in sample:
                col["email"] = idx
                break
    if col.get("email") is None:
        raise ValueError("No email column detected in the sheet")

    added = duplicates = skipped = 0
    with _db() as db:
        existing = {
            c.email
            for c in db.scalars(select(Client).where(Client.campaign_id == campaign_id))
        }
        for i, row in enumerate(grid[1:], start=2):
            def cell(logical: str) -> str:
                idx = col.get(logical)
                return row[idx].strip() if idx is not None and idx < len(row) else ""

            email = cell("email").lower()
            if not _valid_email(email):
                skipped += 1
                continue
            if email in existing:
                duplicates += 1
                continue
            fields = {
                _clean_key(header[j]): row[j].strip()
                for j in range(min(len(header), len(row)))
                if j not in {v for v in col.values() if v is not None} and row[j].strip()
            }
            db.add(
                Client(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    name=cell("name"),
                    email=email,
                    company=cell("company"),
                    fields_json=json.dumps(fields),
                    source="google_sheet",
                    row_number=i,
                    sheet_row=i,
                    status="Pending" if cell("status").lower() not in ("sent", "skipped") else "Sent",
                )
            )
            existing.add(email)
            added += 1
        db.commit()
    return {"added": added, "duplicates": duplicates, "skipped": skipped}


def sheet_sync_client(campaign: Campaign, client: Client) -> None:
    """Write status/result columns back to the connected sheet row (best effort)."""
    if not campaign.sheet_id or not client.sheet_row:
        return
    try:
        grid = sheet_read_grid(campaign.sheet_id, campaign.sheet_range)
        if not grid:
            return
        header = grid[0]
        lookup = {_clean_key(h): idx for idx, h in enumerate(header)}
        cmap = {**DEFAULT_COLUMN_MAP, **json.loads(campaign.sheet_column_map or "{}")}
        targets: dict[int, str] = {}
        for logical, header_name in cmap.items():
            idx = lookup.get(_clean_key(header_name))
            if idx is not None:
                targets[idx] = logical

        def col_letter(n: int) -> str:
            s = ""
            n += 1
            while n:
                n, r = divmod(n - 1, 26)
                s = chr(65 + r) + s
            return s

        indices = sorted(targets)
        if not indices:
            return
        start, end = col_letter(indices[0]), col_letter(indices[-1])
        row_vals: list[str] = [""] * (indices[-1] - indices[0] + 1)
        for idx in indices:
            logical = targets[idx]
            if logical == "status":
                row_vals[idx - indices[0]] = client.status
            elif logical == "last_email":
                row_vals[idx - indices[0]] = client.last_email[:40000]
            elif logical == "sent_at":
                row_vals[idx - indices[0]] = client.sent_at.isoformat() if client.sent_at else ""
            elif logical == "error":
                row_vals[idx - indices[0]] = client.error[:900]
            elif logical == "message_id":
                row_vals[idx - indices[0]] = client.message_id
            elif logical == "notes":
                row_vals[idx - indices[0]] = client.notes[:900]
        rng = f"{campaign.sheet_range.split('!')[0]}!{start}{client.sheet_row}:{end}{client.sheet_row}"
        _sheets_service().spreadsheets().values().update(
            spreadsheetId=campaign.sheet_id,
            range=rng,
            valueInputOption="USER_ENTERED",
            body={"values": [row_vals]},
        ).execute()
    except Exception as exc:
        client.notes = f"sheet sync failed: {exc}"[:900]
        logger.warning("Sheet sync failed for %s: %s", client.email, exc)


# ---------------------------------------------------------------------------
# Memory / context management
# ---------------------------------------------------------------------------


def _campaign_memory(campaign: Campaign) -> str:
    """Combined memory: instructions + context text + uploaded context files."""
    parts = [campaign.instructions or "", campaign.context or ""]
    with _db() as db:
        rows = db.scalars(
            select(ContextFile).where(ContextFile.campaign_id == campaign.id)
        ).all()
        for row in rows:
            parts.append(row.content)
    return "\n\n".join(p for p in parts if p.strip())


def _select_relevant_memory(memory: str, client: NormalizedClient | Client) -> str:
    """Keep only memory chunks relevant to THIS client (never leak other clients)."""
    if not memory:
        return ""
    tokens = {
        t
        for t in re.split(r"[^a-z0-9]+", f"{client.name} {client.company}".lower())
        if len(t) > 2
    }
    chunks = [c.strip() for c in re.split(r"\n\s*\n", memory) if c.strip()]
    if len(chunks) <= 1:
        return memory
    relevant = [
        c
        for c in chunks
        if tokens & set(re.split(r"[^a-z0-9]+", c.lower()))
    ]
    # Always keep short "global" instruction chunks (<= 240 chars) — they are campaign rules.
    relevant += [c for c in chunks if len(c) <= 240 and c not in relevant]
    return "\n\n".join(relevant) if relevant else memory[:4000]


# ---------------------------------------------------------------------------
# Email generation (shared SARA LLM brain — same config as the voice agent)
# ---------------------------------------------------------------------------

_LLM_SEMAPHORE = asyncio.Semaphore(2)


def _llm_client():
    if not settings.LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not configured (shared SARA brain).")
    from app.core.llm_router import get_llm_client as _failover_client

    return _failover_client()


def _template_email(campaign: Campaign, client: NormalizedClient | Client) -> str:
    greeting = f"Hi {client.name}," if client.name else "Hello,"
    parts = [greeting, "", campaign.objective or campaign.context or "I hope this message finds you well."]
    if campaign.instructions:
        parts.append(campaign.instructions)
    if campaign.cta:
        parts.append("")
        parts.append(campaign.cta)
    return "\n".join(parts).strip()


_SUBJECT_LINE_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.IGNORECASE)
_CLOSING_RE = re.compile(
    r"\n+\s*(?:best regards|kind regards|regards|sincerely|yours sincerely|warm regards"
    r"|thank you|thanks|cheers)[,.!]*\s*(?:\n[^\n]*)?$",
    re.IGNORECASE,
)


def _split_subject(raw: str) -> tuple[Optional[str], str]:
    """Split a leading 'SUBJECT: ...' line off the LLM output."""
    lines = raw.strip().splitlines()
    for i, line in enumerate(lines[:4]):
        m = _SUBJECT_LINE_RE.match(line)
        if m:
            subject = m.group(1).strip().strip('"').strip()
            rest = lines[:i] + lines[i + 1:]
            return (subject or None), "\n".join(rest).strip()
    return None, raw.strip()


def _append_signature(body: str, campaign_signature: str = "") -> str:
    """Strip any LLM sign-off, then append the sender signature block."""
    clean = _CLOSING_RE.sub("", body or "").rstrip()
    signature = (campaign_signature or "").strip() or DEFAULT_SIGNATURE
    return f"{clean}\n\n{signature}".strip()


def _fallback_subject(campaign: Campaign, client: NormalizedClient | Client) -> str:
    if campaign.subject:
        return campaign.subject
    if getattr(client, "company", ""):
        return f"Quick question about {client.company}"
    return "A message from SARA AI"


def _generation_prompt(campaign: Campaign, client: NormalizedClient | Client, memory: str) -> str:
    values = client.data()
    relevant = "\n".join(f"- {k}: {v}" for k, v in values.items() if v)
    return (
        f"Write one {campaign.tone} email that sounds naturally human.\n"
        f"Campaign objective: {campaign.objective}\n"
        f"Call to action: {campaign.cta}\n"
        f"Campaign memory (use ONLY what is relevant to this client):\n{memory}\n"
        f"This client's data (the only facts you may use):\n{relevant}\n\n"
        "Rules:\n"
        "- The FIRST line must be exactly 'SUBJECT:' followed by a short, specific,\n"
        "  personalized subject line (max 8 words) tailored to this client.\n"
        "  No clickbait, no emojis, no ALL CAPS, no surrounding quotes.\n"
        "- After the subject line leave one blank line, then write the email body.\n"
        "- Never invent facts, prices, promises, past interactions, or company details.\n"
        "- Use only the client data above; never mention other clients or this instruction block.\n"
        "- 120-220 words, plain text, no markdown, no placeholders like [Name].\n"
        "- Do NOT add any sign-off or signature (Best regards, Sincerely, your name, phone\n"
        "  number or email address) — the sender's signature is appended automatically."
    )


async def generate_email(campaign: Campaign, client: NormalizedClient | Client) -> tuple[str, str]:
    """Return (subject, body). The subject is written by the AI per client; the
    sender signature is always appended to the body."""
    memory = _select_relevant_memory(_campaign_memory(campaign), client)
    if EMAIL_AGENT_USE_LLM and settings.LLM_API_KEY:
        try:
            async with _LLM_SEMAPHORE:
                response = await _llm_client().chat.completions.create(
                    model=settings.CHAT_LLM_MODEL or settings.LLM_MODEL,
                    messages=[{"role": "system", "content": _generation_prompt(campaign, client, memory)}],
                    temperature=0.4,
                    max_tokens=600,
                    timeout=45,
                )
            raw = (response.choices[0].message.content or "").strip()
            if raw:
                subject, body = _split_subject(raw)
                return (subject or _fallback_subject(campaign, client)), _append_signature(
                    body, campaign.signature
                )
        except Exception as exc:
            logger.warning("LLM generation failed for %s (%s); using template.", client.email, exc)
    return _fallback_subject(campaign, client), _append_signature(
        _template_email(campaign, client), campaign.signature
    )


# ---------------------------------------------------------------------------
# Providers: SMTP (Gmail app-password — same system as the voice agent) + Gmail API
# ---------------------------------------------------------------------------


def _sender_settings_row() -> Optional[SenderSettings]:
    with _db() as db:
        return db.get(SenderSettings, 1)


def _saved_smtp_config() -> Optional[dict[str, str]]:
    """SMTP credentials the user connected through the UI (they win over .env)."""
    row = _sender_settings_row()
    if not row or not row.from_email or not row.password:
        return None
    return {
        "host": row.host,
        "port": str(row.port),
        "user": row.username or row.from_email,
        "password": row.password,
        "from": row.from_email,
        "name": row.from_name or "SARA AI",
        "ssl": "true" if row.ssl else "false",
    }


def _smtp_config(provider: str) -> dict[str, str]:
    if provider == "smtp":
        saved = _saved_smtp_config()
        if saved:
            return saved
        if settings.GMAIL_SENDER or settings.SMTP_HOST:
            host = settings.SMTP_HOST or GMAIL_SMTP_HOST
            return {
                "host": host,
                "port": str(settings.SMTP_PORT or GMAIL_SMTP_PORT),
                "user": settings.SMTP_USERNAME or settings.GMAIL_SENDER or "",
                "password": settings.SMTP_PASSWORD or settings.GMAIL_APP_PASSWORD or "",
                "from": settings.EMAIL_AGENT_FROM or settings.GMAIL_SENDER or settings.SMTP_USERNAME or "",
                "name": "SARA AI",
                "ssl": "true" if settings.SMTP_SSL else "false",
            }
    if not EMAIL_AGENT_FROM and not settings.GMAIL_SENDER:
        raise RuntimeError(
            "No email sender is connected. Open this page's 'Sender' panel and connect "
            "your mailbox (Google sign-in or app-password SMTP), or set GMAIL_SENDER + "
            "GMAIL_APP_PASSWORD (or SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD) in backend/.env."
        )
    return {
        "host": GMAIL_SMTP_HOST,
        "port": str(GMAIL_SMTP_PORT),
        "user": settings.GMAIL_SENDER or "",
        "password": settings.GMAIL_APP_PASSWORD or "",
        "from": EMAIL_AGENT_FROM or settings.GMAIL_SENDER,
        "name": "SARA AI",
        "ssl": "false",
    }


def _smtp_login(cfg: dict[str, str]) -> None:
    """Connect + authenticate only (no message) — the 'Save & verify' check."""
    host, port = cfg["host"], int(cfg["port"])
    ctx = ssl.create_default_context()
    if cfg["ssl"] == "true":
        with smtplib.SMTP_SSL(host, port, timeout=EMAIL_AGENT_SEND_TIMEOUT, context=ctx) as smtp:
            smtp.login(cfg["user"], cfg["password"])
    else:
        with smtplib.SMTP(host, port, timeout=EMAIL_AGENT_SEND_TIMEOUT) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(cfg["user"], cfg["password"])


def _mime_pair(filename: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    maintype, _, subtype = mime.partition("/")
    return maintype, subtype or "octet-stream"


def _mime_attachment_part(path: Path, filename: str) -> MIMEBase:
    maintype, subtype = _mime_pair(filename)
    part = MIMEBase(maintype, subtype)
    part.set_payload(path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    return part


def _build_smtp_message(
    cfg: dict[str, str], recipient: str, subject: str, body: str,
    attachments: list[dict[str, Any]] = (),
) -> MIMEMultipart:
    msg = MIMEMultipart()
    msg["From"] = formataddr((cfg.get("name") or "SARA AI", cfg["from"]))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4()}@sara.local>"
    msg.attach(MIMEText(body, "plain"))
    for att in attachments:
        msg.attach(_mime_attachment_part(Path(att["path"]), att["filename"]))
    return msg


def _send_via_smtp(
    provider_cfg: dict[str, str], recipient: str, subject: str, body: str,
    attachments: list[dict[str, Any]] = (),
) -> str:
    msg = _build_smtp_message(provider_cfg, recipient, subject, body, attachments)
    host, port = provider_cfg["host"], int(provider_cfg["port"])
    ctx = ssl.create_default_context()
    if provider_cfg["ssl"] == "true":
        with smtplib.SMTP_SSL(host, port, timeout=EMAIL_AGENT_SEND_TIMEOUT, context=ctx) as smtp:
            smtp.login(provider_cfg["user"], provider_cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=EMAIL_AGENT_SEND_TIMEOUT) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(provider_cfg["user"], provider_cfg["password"])
            smtp.send_message(msg)
    return str(msg["Message-ID"])


def _gmail_api_credentials():
    from google.oauth2.credentials import Credentials as UserCredentials
    import google.auth.transport.requests as _requests

    from app.database.database import SessionLocal as MainSession
    from app.database.models import Integration as MainIntegration

    with MainSession() as mdb:
        row = mdb.scalars(select(MainIntegration).where(MainIntegration.provider == "gmail")).first()
    if not row or not row.refresh_token:
        raise RuntimeError(
            "Gmail API is not connected. Open the 'Sender' panel on this page and "
            "click 'Connect with Google', then run the campaign again."
        )
    creds = UserCredentials(
        token=row.access_token,
        refresh_token=row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(_requests.Request())
    return creds


def _build_gmail_message(
    recipient: str, subject: str, body: str,
    attachments: list[dict[str, Any]] = (),
) -> EmailMessage:
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = EMAIL_AGENT_FROM or settings.GMAIL_SENDER or _gmail_status()["email"]
    message["Subject"] = subject
    message.set_content(body)
    for att in attachments:
        maintype, subtype = _mime_pair(att["filename"])
        message.add_attachment(
            Path(att["path"]).read_bytes(),
            maintype=maintype, subtype=subtype, filename=att["filename"],
        )
    return message


def _send_via_gmail_api(
    recipient: str, subject: str, body: str,
    attachments: list[dict[str, Any]] = (),
) -> str:
    from googleapiclient.discovery import build

    message = _build_gmail_message(recipient, subject, body, attachments)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service = build("gmail", "v1", credentials=_gmail_api_credentials(), cache_discovery=False)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


def _campaign_attachments(campaign_id: str) -> list[dict[str, Any]]:
    """Files that ride along with every email of the campaign (guard total size)."""
    with _db() as db:
        rows = db.scalars(
            select(Attachment).where(Attachment.campaign_id == campaign_id).order_by(Attachment.created_at)
        ).all()
    out: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        path = ATTACHMENT_DIR / campaign_id / row.stored_name
        if not path.exists():
            logger.warning("Attachment missing on disk, skipping: %s (%s)", row.filename, path)
            continue
        total += path.stat().st_size
        if total > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise RuntimeError(
                f"Attachments exceed the {MAX_ATTACHMENTS_TOTAL_BYTES // (1024 * 1024)} MB total limit "
                "for one email — remove or shrink files before sending"
            )
        out.append({"path": str(path), "filename": row.filename, "mime": row.mime, "size": row.size})
    return out


async def send_email(campaign: Campaign, recipient: str, subject: str, body: str) -> str:
    """Dispatch through the configured provider with the campaign's attachments."""
    attachments = _campaign_attachments(campaign.id)
    if campaign.provider == "gmail":
        return await asyncio.to_thread(_send_via_gmail_api, recipient, subject, body, attachments)
    cfg = _smtp_config("smtp")
    return await asyncio.to_thread(_send_via_smtp, cfg, recipient, subject, body, attachments)


# ---------------------------------------------------------------------------
# Campaign orchestrator (async task, stop flags, retry/backoff, resume)
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.stop_flags: set[str] = set()

    def running(self, campaign_id: str) -> bool:
        task = self.tasks.get(campaign_id)
        return bool(task and not task.done())

    def stop(self, campaign_id: str) -> None:
        self.stop_flags.add(campaign_id)
        task = self.tasks.get(campaign_id)
        if task and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel every running worker and await it."""
        for task in list(self.tasks.values()):
            task.cancel()
        for task in list(self.tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks.clear()
        self.stop_flags.clear()

    def launch(self, campaign_id: str) -> None:
        if not self.running(campaign_id):
            self.tasks[campaign_id] = asyncio.create_task(self._run(campaign_id))

    def _pick_next(self, db: Session, campaign: Campaign) -> Optional[Client]:
        return db.scalars(
            select(Client)
            .where(
                Client.campaign_id == campaign.id,
                Client.status.in_(["Pending", "Failed"]),
                Client.attempts < campaign.max_retries + 1,
            )
            .order_by(Client.row_number, Client.id)
        ).first()

    async def _run(self, campaign_id: str) -> None:
        try:
            with _db() as db:
                campaign = db.get(Campaign, campaign_id)
                if not campaign:
                    return
                campaign.status = "running"
                campaign.last_run_at = datetime.now(timezone.utc)
                db.commit()

            processed = 0
            while campaign_id not in self.stop_flags:
                with _db() as db:
                    campaign = db.get(Campaign, campaign_id)
                    if not campaign or campaign.status == "paused":
                        return
                    client = self._pick_next(db, campaign)
                    if not client or processed >= campaign.max_emails:
                        campaign.status = "completed"
                        db.commit()
                        logger.info("Campaign %s completed (%s sent, %s failed)",
                                    campaign_id, campaign.sent_count, campaign.failed_count)
                        return

                    client.status = "Sending"
                    client.attempts += 1
                    db.commit()
                    record_id = client.id
                    nc = NormalizedClient(
                        client.name,
                        client.email,
                        client.company,
                        json.loads(client.fields_json or "{}"),
                        client.source,
                    )

                    recipient = campaign.test_email or client.email
                    try:
                        subject, body = await generate_email(campaign, nc)
                        message_id = ""
                        if campaign.dry_run:
                            message_id = f"dry-run-{uuid.uuid4()}"
                            logger.info("[DRY RUN] Would send to %s (%s chars)", recipient, len(body))
                        else:
                            message_id = await self._send_with_retry(campaign, recipient, subject, body)
                        client.status = "Sent"
                        client.sent_at = datetime.now(timezone.utc)
                        client.last_email = body
                        client.message_id = message_id
                        client.error = ""
                        campaign.sent_count += 1
                    except Exception as exc:
                        logger.exception("Send failed for %s", client.email)
                        client.status = "Failed"
                        client.error = str(exc)[:4000]
                        campaign.failed_count += 1

                    sheet_sync_client(campaign, client)
                    db.commit()
                processed += 1
                await asyncio.sleep(max(0.0, campaign.delay_seconds))
        except asyncio.CancelledError:
            with _db() as db:
                campaign = db.get(Campaign, campaign_id)
                if campaign and campaign.status == "running":
                    campaign.status = "paused"
                    db.commit()
            raise
        finally:
            self.stop_flags.discard(campaign_id)
            self.tasks.pop(campaign_id, None)

    async def _send_with_retry(self, campaign: Campaign, recipient: str, subject: str, body: str) -> str:
        """Send with exponential backoff. Raises after max_retries attempts."""
        last_exc: Exception | None = None
        for attempt in range(max(1, campaign.max_retries + 1)):
            if campaign.id in self.stop_flags:
                raise RuntimeError("Campaign stopped before send")
            try:
                return await send_email(campaign, recipient, subject, body)
            except Exception as exc:  # noqa: PERF203 — retry loop by design
                last_exc = exc
                wait = min(30.0, 0.5 * (2 ** attempt))
                logger.warning("Send attempt %s/%s failed (%s); backing off %.1fs",
                               attempt + 1, campaign.max_retries + 1, exc, wait)
                await asyncio.sleep(wait)
        assert last_exc is not None
        raise last_exc


orchestrator = Orchestrator()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _campaign_or_404(db: Session, campaign_id: str) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return campaign


def _campaign_dict(campaign: Campaign) -> dict[str, Any]:
    return {k: v for k, v in vars(campaign).items() if not k.startswith("_")}


def _client_dict(client: Client) -> dict[str, Any]:
    return {k: v for k, v in vars(client).items() if not k.startswith("_")}


def _require_recipient_ready(campaign: Campaign) -> None:
    """Fail fast on critical configuration errors — never silently continue."""
    if campaign.dry_run:
        return
    if campaign.test_email and not _valid_email(campaign.test_email):
        raise HTTPException(400, "campaign.test_email is not a valid email address")
    try:
        if campaign.provider == "gmail":
            _gmail_api_credentials()
        else:
            _smtp_config("smtp")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# API — health & configuration report
# ---------------------------------------------------------------------------


def _gmail_status() -> dict[str, Any]:
    """Connection state of the Google account linked via /api/auth/google."""
    try:
        from sqlalchemy import select as _select

        from app.database.database import SessionLocal as MainSession
        from app.database.models import Integration as MainIntegration
        from app.database.models import User as MainUser

        with MainSession() as mdb:
            row = mdb.scalars(
                _select(MainIntegration).where(MainIntegration.provider == "gmail")
            ).first()
            email = ""
            if row and row.user_id:
                user = mdb.get(MainUser, row.user_id)
                if user and user.email:
                    email = user.email
        return {"connected": bool(row and row.refresh_token), "email": email}
    except Exception:
        return {"connected": False, "email": ""}


def _gmail_integration_connected() -> bool:
    """True when a Google account with a refresh token is linked via /api/auth/google."""
    return bool(_gmail_status()["connected"])


@router.get("/health")
def health() -> dict[str, Any]:
    saved = _sender_settings_row()
    sender_set = bool(settings.GMAIL_SENDER or settings.SMTP_USERNAME or (saved and saved.from_email))
    password_set = bool(settings.GMAIL_APP_PASSWORD or settings.SMTP_PASSWORD or (saved and saved.password))
    gmail = _gmail_status()
    return {
        "status": "ok",
        "agent": "Email Automation Agent",
        "llm": bool(settings.LLM_API_KEY),
        # smtp is only True when a real send could actually authenticate
        "smtp": sender_set and password_set,
        "smtp_sender": sender_set,
        "smtp_password": password_set,
        "smtp_from": (saved.from_email if saved and saved.from_email
                      else (settings.SMTP_USERNAME or settings.GMAIL_SENDER or "")),
        "gmail_api": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
        "gmail_connected": gmail["connected"],
        "gmail_email": gmail["email"],
        "sheets_service_account": bool(
            os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_CREDENTIALS_FILE")
        ),
        "running_campaigns": [cid for cid in orchestrator.tasks if orchestrator.running(cid)],
    }


# ---------------------------------------------------------------------------
# API — sender connection ("Connect your email")
# ---------------------------------------------------------------------------


@router.get("/sender")
def sender_status() -> dict[str, Any]:
    """Everything the Sender panel needs: SMTP mailbox + Google account state."""
    saved = _sender_settings_row()
    gmail = _gmail_status()
    smtp: dict[str, Any] = {
        "configured": False,
        "from_email": "",
        "from_name": "",
        "host": "",
        "port": 0,
        "ssl": False,
        "source": None,  # saved | env
    }
    if saved and saved.from_email and saved.password:
        smtp.update({
            "configured": True,
            "from_email": saved.from_email,
            "from_name": saved.from_name,
            "host": saved.host,
            "port": saved.port,
            "ssl": bool(saved.ssl),
            "source": "saved",
        })
    elif settings.GMAIL_APP_PASSWORD or settings.SMTP_PASSWORD:
        smtp.update({
            "configured": True,
            "from_email": (settings.SMTP_USERNAME or settings.GMAIL_SENDER or ""),
            "host": (settings.SMTP_HOST or GMAIL_SMTP_HOST),
            "port": int(settings.SMTP_PORT or GMAIL_SMTP_PORT),
            "ssl": bool(settings.SMTP_SSL),
            "source": "env",
        })
    return {
        "smtp": smtp,
        "gmail": {
            "available": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET),
            "connected": gmail["connected"],
            "email": gmail["email"],
        },
        "app_passwords_url": GOOGLE_APP_PASSWORDS_URL,
    }


def _smtp_resolve(payload: SenderSMTPRequest) -> dict[str, Any]:
    from_email = payload.from_email.strip()
    if not _valid_email(from_email):
        raise HTTPException(400, "Enter a valid sender email address")
    password = payload.password.strip()
    # Gmail app passwords are displayed as 4 groups of 4 letters ("abcd efgh ijkl mnop")
    # and are commonly pasted with spaces — normalize exactly that shape.
    if re.fullmatch(r"[A-Za-z]{16}", password.replace(" ", "")) and len(password) > 16:
        password = password.replace(" ", "")
    if not password:
        raise HTTPException(400, "Enter the app password / SMTP password for this mailbox")
    domain = from_email.rsplit("@", 1)[-1].lower()
    host = payload.host.strip()
    port = payload.port
    ssl_flag = payload.ssl
    if not host:
        if domain in {"gmail.com", "googlemail.com"}:
            host = GMAIL_SMTP_HOST
            port = port or GMAIL_SMTP_PORT
            if ssl_flag is None:
                ssl_flag = False
        else:
            raise HTTPException(
                400,
                "This address is not @gmail.com — fill in the SMTP host (and port) of your mail provider",
            )
    if not port:
        port = 465 if ssl_flag else 587
    if ssl_flag is None:
        ssl_flag = port == 465
    return {
        "host": host,
        "port": port,
        "user": (payload.username.strip() or from_email),
        "password": password,
        "from": from_email,
        "name": payload.from_name.strip() or "SARA AI",
        "ssl": "true" if ssl_flag else "false",
    }


@router.post("/sender/smtp", dependencies=[Depends(require_api_key)])
def connect_smtp(payload: SenderSMTPRequest) -> dict[str, Any]:
    """Verify the credentials with a real SMTP login, then save them for sending."""
    cfg = _smtp_resolve(payload)
    try:
        _smtp_login(cfg)
    except Exception as exc:
        raise HTTPException(
            400,
            f"Could not sign in to {cfg['host']}:{cfg['port']} — {exc}. "
            "For Gmail: enable 2-Step Verification, create an App password and paste it here; "
            "your password is used only to send from this machine.",
        ) from exc
    with _db() as db:
        row = db.get(SenderSettings, 1)
        if not row:
            row = SenderSettings(id=1)
            db.add(row)
        row.from_email = cfg["from"]
        row.from_name = cfg["name"]
        row.username = cfg["user"]
        row.password = cfg["password"]
        row.host = cfg["host"]
        row.port = cfg["port"]
        row.ssl = cfg["ssl"] == "true"
        db.commit()
    return {
        "status": "connected",
        "verified": True,
        "from_email": cfg["from"],
        "host": cfg["host"],
        "port": cfg["port"],
        "ssl": cfg["ssl"] == "true",
    }


@router.post("/sender/smtp/disconnect", dependencies=[Depends(require_api_key)])
def disconnect_smtp() -> dict[str, Any]:
    with _db() as db:
        row = db.get(SenderSettings, 1)
        if row:
            db.delete(row)
            db.commit()
    env_fallback = bool(settings.GMAIL_APP_PASSWORD or settings.SMTP_PASSWORD)
    return {"status": "disconnected", "env_fallback": env_fallback}


# ---------------------------------------------------------------------------
# API — campaigns
# ---------------------------------------------------------------------------


@router.post("/campaigns", dependencies=[Depends(require_api_key)])
def create_campaign(payload: CampaignCreate) -> dict[str, str]:
    campaign = Campaign(id=str(uuid.uuid4()), **payload.model_dump())
    with _db() as db:
        db.add(campaign)
        db.commit()
    return {"id": campaign.id, "status": campaign.status}


@router.get("/campaigns")
def list_campaigns() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
        out = []
        for c in rows:
            counts: dict[str, int] = {}
            for client in db.scalars(select(Client.status).where(Client.campaign_id == c.id)):
                counts[client] = counts.get(client, 0) + 1
            out.append({**_campaign_dict(c), "counts": counts, "live": orchestrator.running(c.id)})
        return out


@router.get("/campaigns/{campaign_id}")
def campaign_status(campaign_id: str) -> dict[str, Any]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        counts: dict[str, int] = {}
        for status in db.scalars(select(Client.status).where(Client.campaign_id == campaign_id)):
            counts[status] = counts.get(status, 0) + 1
        return {**_campaign_dict(campaign), "counts": counts, "live": orchestrator.running(campaign_id)}


@router.patch("/campaigns/{campaign_id}", dependencies=[Depends(require_api_key)])
def update_campaign(campaign_id: str, payload: CampaignUpdate) -> dict[str, str]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        if orchestrator.running(campaign_id):
            raise HTTPException(409, "Pause the campaign before editing it")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(campaign, field, value)
        db.commit()
    return {"status": "saved"}


@router.delete("/campaigns/{campaign_id}", dependencies=[Depends(require_api_key)])
def delete_campaign(campaign_id: str) -> dict[str, str]:
    orchestrator.stop(campaign_id)
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        for client in db.scalars(select(Client).where(Client.campaign_id == campaign_id)):
            db.delete(client)
        for row in db.scalars(select(ContextFile).where(ContextFile.campaign_id == campaign_id)):
            db.delete(row)
        for row in db.scalars(select(Attachment).where(Attachment.campaign_id == campaign_id)):
            db.delete(row)
        db.delete(campaign)
        db.commit()
    shutil.rmtree(ATTACHMENT_DIR / campaign_id, ignore_errors=True)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# API — memory / context
# ---------------------------------------------------------------------------


@router.post("/campaigns/{campaign_id}/context", dependencies=[Depends(require_api_key)])
def set_context(campaign_id: str, payload: TextRequest) -> dict[str, str]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        campaign.context = payload.text
        db.commit()
    return {"status": "saved"}


@router.post("/campaigns/{campaign_id}/instructions", dependencies=[Depends(require_api_key)])
def set_instructions(campaign_id: str, payload: TextRequest) -> dict[str, str]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        campaign.instructions = payload.text
        db.commit()
    return {"status": "saved"}


@router.post("/campaigns/{campaign_id}/context/upload", dependencies=[Depends(require_api_key)])
async def upload_context(campaign_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _campaign_or_404(_db(), campaign_id)
    data = await file.read()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        content = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    else:
        content = data.decode("utf-8", errors="replace")
    if not content.strip():
        raise HTTPException(400, "The uploaded file contains no readable text")
    with _db() as db:
        row = ContextFile(
            id=str(uuid.uuid4()), campaign_id=campaign_id,
            filename=file.filename or "context.txt", content=content[:200000],
        )
        db.add(row)
        db.commit()
        return {"id": row.id, "filename": row.filename, "chars": len(row.content)}


@router.get("/campaigns/{campaign_id}/context/files")
def list_context_files(campaign_id: str) -> list[dict[str, Any]]:
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        return [
            {"id": r.id, "filename": r.filename, "chars": len(r.content), "created_at": r.created_at}
            for r in db.scalars(select(ContextFile).where(ContextFile.campaign_id == campaign_id))
        ]


# ---------------------------------------------------------------------------
# API — attachments (real files attached to every email of the campaign)
# ---------------------------------------------------------------------------


def _attachment_dir(campaign_id: str) -> Path:
    path = ATTACHMENT_DIR / campaign_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _attachment_dict(row: Attachment) -> dict[str, Any]:
    return {
        "id": row.id,
        "filename": row.filename,
        "mime": row.mime,
        "size": row.size,
        "created_at": row.created_at,
    }


@router.post("/campaigns/{campaign_id}/attachments", dependencies=[Depends(require_api_key)])
async def upload_attachment(campaign_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a file that will be attached to every email the campaign sends."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            400, f"File is larger than the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB per-file limit"
        )
    original = Path(file.filename or "attachment").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix or original}'. Allowed: "
            + ", ".join(sorted(ALLOWED_ATTACHMENT_SUFFIXES)),
        )
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        existing = db.scalars(select(Attachment).where(Attachment.campaign_id == campaign_id)).all()
        total = sum(row.size for row in existing)
        # Uploading the same filename again replaces the old version (e.g. a
        # refreshed resume) instead of sending the file twice.
        for row in list(existing):
            if row.filename.lower() == original.lower():
                (ATTACHMENT_DIR / campaign_id / row.stored_name).unlink(missing_ok=True)
                total -= row.size
                db.delete(row)
        if total + len(data) > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise HTTPException(
                400,
                f"Attachments would exceed the {MAX_ATTACHMENTS_TOTAL_BYTES // (1024 * 1024)} MB "
                "total limit for one email — remove a file first",
            )
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        (_attachment_dir(campaign_id) / stored_name).write_bytes(data)
        row = Attachment(
            id=str(uuid.uuid4()),
            campaign_id=campaign_id,
            filename=original,
            stored_name=stored_name,
            mime=(mimetypes.guess_type(original)[0] or "application/octet-stream"),
            size=len(data),
        )
        db.add(row)
        db.commit()
        return {**_attachment_dict(row), "status": "stored"}


@router.get("/campaigns/{campaign_id}/attachments")
def list_attachments(campaign_id: str) -> list[dict[str, Any]]:
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        rows = db.scalars(
            select(Attachment).where(Attachment.campaign_id == campaign_id).order_by(Attachment.created_at)
        ).all()
        return [_attachment_dict(row) for row in rows]


@router.delete("/campaigns/{campaign_id}/attachments/{attachment_id}", dependencies=[Depends(require_api_key)])
def delete_attachment(campaign_id: str, attachment_id: str) -> dict[str, str]:
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        row = db.get(Attachment, attachment_id)
        if not row or row.campaign_id != campaign_id:
            raise HTTPException(404, "Attachment not found")
        (ATTACHMENT_DIR / campaign_id / row.stored_name).unlink(missing_ok=True)
        db.delete(row)
        db.commit()
    return {"status": "deleted"}


@router.get("/campaigns/{campaign_id}/attachments/{attachment_id}/download")
def download_attachment(campaign_id: str, attachment_id: str):
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        row = db.get(Attachment, attachment_id)
        if not row or row.campaign_id != campaign_id:
            raise HTTPException(404, "Attachment not found")
        path = ATTACHMENT_DIR / campaign_id / row.stored_name
        if not path.exists():
            raise HTTPException(404, "Attachment file is missing on disk")
        return FileResponse(path, media_type=row.mime, filename=row.filename)


# ---------------------------------------------------------------------------
# API — ingestion (PDF / CSV / Google Sheets)
# ---------------------------------------------------------------------------


@router.post("/campaigns/{campaign_id}/upload", dependencies=[Depends(require_api_key)])
async def upload_clients(campaign_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty")
    suffix = Path(file.filename or "").suffix.lower()
    source = file.filename or "upload"
    try:
        if suffix == ".csv":
            clients = parse_csv(data, source)
        elif suffix == ".pdf":
            clients = parse_pdf(data, source)
        else:
            raise HTTPException(400, "Only .csv and .pdf files are supported")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}") from exc

    diagnosis = ""
    if not clients:
        if suffix == ".csv":
            header = next(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))), [])
            diagnosis = (
                f"No valid email found in the file. Detected columns: {header[:8]}. "
                "The parser accepts any column named Email / Mail / E-mail (any spelling) "
                "or any column whose values contain '@'. If this is an Excel workbook, "
                "export it as CSV first (File → Save As → CSV UTF-8)."
            )
        else:
            diagnosis = (
                "No email addresses could be extracted from this PDF "
                "(scanned image, or the document lists no emails). "
                "Try a text-based PDF or a CSV with an Email column."
            )

    with _db() as db:
        _campaign_or_404(db, campaign_id)
        existing = {c.email for c in db.scalars(select(Client).where(Client.campaign_id == campaign_id))}
        added = duplicates = invalid = 0
        for nc in clients:
            if nc.email in existing:
                duplicates += 1
                continue
            db.add(
                Client(
                    id=str(uuid.uuid4()),
                    campaign_id=campaign_id,
                    name=nc.name,
                    email=nc.email,
                    company=nc.company,
                    fields_json=json.dumps(nc.fields),
                    source=nc.source,
                    row_number=added + 1,
                )
            )
            existing.add(nc.email)
            added += 1
        db.commit()
    invalid = max(0, len(clients) - added - duplicates)
    return {
        "parsed": len(clients),
        "added": added,
        "duplicates": duplicates,
        "invalid_or_empty": invalid,
        "diagnosis": diagnosis,
    }


@router.post("/campaigns/{campaign_id}/sheet", dependencies=[Depends(require_api_key)])
def configure_sheet(campaign_id: str, payload: SheetConfig) -> dict[str, Any]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        campaign.sheet_id = payload.spreadsheet_id
        campaign.sheet_range = payload.range_name
        campaign.sheet_column_map = json.dumps(payload.column_map)
        db.commit()
        result = (
            sheet_import(payload.spreadsheet_id, payload.range_name, campaign_id)
            if payload.import_clients
            else {"added": 0, "duplicates": 0, "skipped": 0}
        )
    return {"status": "connected", **result}


@router.post("/campaigns/{campaign_id}/sheet/sync", dependencies=[Depends(require_api_key)])
def sync_sheet_now(campaign_id: str) -> dict[str, Any]:
    """Push the current DB state of every client back to the sheet."""
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        if not campaign.sheet_id:
            raise HTTPException(400, "No Google Sheet connected for this campaign")
        clients = db.scalars(select(Client).where(Client.campaign_id == campaign_id)).all()
        for client in clients:
            sheet_sync_client(campaign, client)
        db.commit()
    return {"status": "synced", "clients": len(clients)}


# ---------------------------------------------------------------------------
# API — validation / preview / test
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/validate")
def validate_clients(campaign_id: str) -> dict[str, Any]:
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        clients = db.scalars(select(Client).where(Client.campaign_id == campaign_id)).all()
        seen: set[str] = set()
        valid = invalid = dupes = 0
        issues: list[dict[str, str]] = []
        for c in clients:
            if not _valid_email(c.email):
                invalid += 1
                issues.append({"client_id": c.id, "email": c.email, "issue": "invalid email"})
            elif c.email in seen:
                dupes += 1
                issues.append({"client_id": c.id, "email": c.email, "issue": "duplicate email"})
            else:
                valid += 1
                seen.add(c.email)
        return {"total": len(clients), "valid": valid, "invalid": invalid, "duplicates": dupes, "issues": issues}


@router.post("/campaigns/{campaign_id}/preview")
async def preview_email(campaign_id: str, payload: PreviewRequest) -> dict[str, Any]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        client = db.get(Client, payload.client_id)
        if not client or client.campaign_id != campaign_id:
            raise HTTPException(404, "Client not found in this campaign")
        nc = NormalizedClient(
            client.name, client.email, client.company, json.loads(client.fields_json or "{}"), client.source
        )
        subject, body = await generate_email(campaign, nc)
        return {
            "subject": subject,
            "body": body,
            "to": client.email,
            "attachments": [
                {"filename": a["filename"], "size": a["size"]}
                for a in _campaign_attachments(campaign_id)
            ],
        }


@router.post("/campaigns/{campaign_id}/test-send", dependencies=[Depends(require_api_key)])
async def test_send(campaign_id: str, payload: TestSendRequest) -> dict[str, Any]:
    """Dry test: generate for one client and send (or simulate) to the test inbox."""
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        recipient = payload.email or campaign.test_email
        if not recipient or not _valid_email(recipient):
            raise HTTPException(400, "Provide a valid test email address (or set test_email on the campaign)")
        client = None
        if payload.client_id:
            client = db.get(Client, payload.client_id)
        if not client:
            client = db.scalars(
                select(Client).where(Client.campaign_id == campaign_id).order_by(Client.row_number)
            ).first()
        if not client:
            raise HTTPException(400, "Import clients first — nothing to generate from")
        nc = NormalizedClient(
            client.name, client.email, client.company, json.loads(client.fields_json or "{}"), client.source
        )
        subject, body = await generate_email(campaign, nc)
        attachments = [
            {"filename": a["filename"], "size": a["size"]}
            for a in _campaign_attachments(campaign_id)
        ]
        if campaign.dry_run:
            return {"mode": "dry-run", "to": recipient, "subject": subject, "body": body,
                    "attachments": attachments}
        message_id = await send_email(campaign, recipient, subject, body)
        return {"mode": "sent", "to": recipient, "message_id": message_id, "body": body,
                "attachments": attachments}


# ---------------------------------------------------------------------------
# API — run control
# ---------------------------------------------------------------------------


def _reset_stale_sending(db: Session, campaign_id: str) -> int:
    """Requeue clients left in 'Sending' by a crashed/interrupted run."""
    stuck = db.scalars(
        select(Client).where(Client.campaign_id == campaign_id, Client.status == "Sending")
    ).all()
    for client in stuck:
        client.status = "Pending"
    if stuck:
        db.commit()
    return len(stuck)


@router.post("/campaigns/{campaign_id}/start", dependencies=[Depends(require_api_key)])
async def start_campaign(campaign_id: str) -> dict[str, str]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        total = len(db.scalars(select(Client.id).where(Client.campaign_id == campaign_id)).all())
        if not total:
            raise HTTPException(400, "No clients imported yet — upload a CSV/PDF or connect a Google Sheet")
        _reset_stale_sending(db, campaign_id)
        _require_recipient_ready(campaign)
        orchestrator.stop_flags.discard(campaign_id)
        campaign.status = "running"
        db.commit()
    orchestrator.launch(campaign_id)
    return {"status": "running"}


@router.post("/campaigns/{campaign_id}/pause", dependencies=[Depends(require_api_key)])
async def pause_campaign(campaign_id: str) -> dict[str, str]:
    orchestrator.stop(campaign_id)
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        campaign.status = "paused"
        db.commit()
    return {"status": "paused"}


@router.post("/campaigns/{campaign_id}/resume", dependencies=[Depends(require_api_key)])
async def resume_campaign(campaign_id: str) -> dict[str, str]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        if campaign.status != "paused":
            raise HTTPException(400, "Only paused campaigns can be resumed")
        _reset_stale_sending(db, campaign_id)
        _require_recipient_ready(campaign)
        campaign.status = "running"
        db.commit()
    orchestrator.stop_flags.discard(campaign_id)
    orchestrator.launch(campaign_id)
    return {"status": "running"}


@router.post("/campaigns/{campaign_id}/retry-failed", dependencies=[Depends(require_api_key)])
async def retry_failed(campaign_id: str) -> dict[str, Any]:
    with _db() as db:
        campaign = _campaign_or_404(db, campaign_id)
        failed = db.scalars(select(Client).where(Client.campaign_id == campaign_id, Client.status == "Failed")).all()
        for client in failed:
            client.status = "Pending"
            client.error = ""
            client.attempts = 0
        db.commit()
    if campaign.status == "completed" and failed:
        orchestrator.stop_flags.discard(campaign_id)
        orchestrator.launch(campaign_id)
    return {"status": "requeued", "clients": len(failed)}


# ---------------------------------------------------------------------------
# API — results
# ---------------------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/clients")
def campaign_clients(campaign_id: str, status: str = "") -> list[dict[str, Any]]:
    with _db() as db:
        _campaign_or_404(db, campaign_id)
        stmt = select(Client).where(Client.campaign_id == campaign_id)
        if status:
            stmt = stmt.where(Client.status == status.capitalize())
        rows = db.scalars(stmt.order_by(Client.row_number, Client.id)).all()
        return [_client_dict(r) for r in rows]
