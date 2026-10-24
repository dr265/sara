import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


# ---------------------------------------------------------------------------
# Core app models (from the project's existing DB schema)
# ---------------------------------------------------------------------------



class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Document(Base):
    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    filename = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(String, nullable=True)  # pgvector replaced with JSON str in SQLite
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    chat_id = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False) # 'user' or 'ai'
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Integration(Base):
    __tablename__ = "integrations"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    provider = Column(String, default="gmail")
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)

class BusinessProfile(Base):
    __tablename__ = "business_profiles"
    
    id = Column(String, primary_key=True, index=True) # e.g., "biz_user123"
    owner_id = Column(String, ForeignKey("users.id"))
    business_name = Column(String)
    business_type = Column(String) # e.g., "clinic", "hotel", "salon", "gym"
    rules_and_policies = Column(Text, nullable=True)

class BusinessDocument(Base):
    __tablename__ = "business_documents"
    
    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String, ForeignKey("business_profiles.id"))
    filename = Column(String)
    extracted_text = Column(Text) # The raw knowledge SARA will memorize
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

class Booking(Base):
    __tablename__ = "bookings"
    
    id = Column(String, primary_key=True, index=True) # e.g., "BKG-1024"
    business_id = Column(String, ForeignKey("business_profiles.id"))
    customer_name = Column(String)
    customer_phone = Column(String)
    service_requested = Column(String)
    booking_time = Column(DateTime)
    status = Column(String, default="CONFIRMED") # CONFIRMED or CANCELLED

class SocialWorkspace(Base):
    """An agency / company workspace for the Social Media Agency agent."""

    __tablename__ = "social_workspaces"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    # User-defined database for video generation (Fernet-encrypted at rest)
    encrypted_video_db_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialAccount(Base):
    """A connected social media account (OAuth token stored encrypted)."""

    __tablename__ = "social_accounts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String, ForeignKey("social_workspaces.id"), nullable=False, index=True)
    platform = Column(String, nullable=False)  # instagram | facebook | linkedin | tiktok
    account_name = Column(String, nullable=False)  # e.g. "@my_brand"
    encrypted_token = Column(Text, nullable=False)  # Fernet-encrypted JSON blob
    status = Column(String, default="connected")  # connecting | connected | error
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialPlatformApp(Base):
    """User-supplied OAuth app credentials (BYO keys) per workspace + platform.

    Lets a user connect accounts with one-click OAuth using their OWN developer
    app, without the operator putting anything in .env. Stored encrypted.
    """

    __tablename__ = "social_platform_apps"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String, ForeignKey("social_workspaces.id"), nullable=False, index=True)
    platform = Column(String, nullable=False)
    encrypted_client_id = Column(Text, nullable=False)
    encrypted_client_secret = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialCampaign(Base):
    __tablename__ = "social_campaigns"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String, ForeignKey("social_workspaces.id"), nullable=False)
    name = Column(String, nullable=False)
    enabled = Column(Integer, default=1)  # recurring campaigns can be paused
    recurrence = Column(String, default="none")  # none|daily|weekdays|weekly|biweekly|monthly
    timezone_name = Column(String, default="UTC")
    account_ids = Column(Text, nullable=True)  # JSON list — recurrence fan-out targets
    anchor_at = Column(DateTime, nullable=True)  # original run_at (anchor for recurrence math)
    next_run_at = Column(DateTime, nullable=True, index=True)  # next pending occurrence
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialContentItem(Base):
    __tablename__ = "social_content_items"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("social_campaigns.id"), nullable=False)
    kind = Column(String, default="text")  # text | image | video
    raw_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SocialPlatformJob(Base):
    """Job queue row — one per account/platform per campaign."""

    __tablename__ = "social_platform_jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("social_campaigns.id"), nullable=False)
    account_id = Column(String, ForeignKey("social_accounts.id"), nullable=False)
    platform = Column(String, nullable=False)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    status = Column(String, default="pending")  # pending|processing|success|failed|dry_run|uncertain
    error = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, default=0)
    available_at = Column(DateTime, nullable=True)  # retry backoff: not claimable before this
    lease_until = Column(DateTime, nullable=True)  # claimed lease; expired lease = reclaimable
    claim_token = Column(String, nullable=True)
    result = Column(Text, nullable=True)  # JSON allowlist: {"method", "remote_id", ...}
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class WhatsAppChannel(Base):
    """A connected WhatsApp number/channel.

    One channel is the active Primary Command Center: its inbound messages
    route to the Sara AI agent and replies go back through that same channel.
    Credentials are stored encrypted (Fernet, key derived from SARA_MASTER_KEY
    / WEBHOOK_SECRET) — never plaintext in the database.
    """

    __tablename__ = "whatsapp_channels"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)                       # display name, e.g. "Reception"
    provider = Column(String, nullable=False)                   # meta | zernio | unipile | twilio | generic
    phone_number = Column(String, nullable=True)                # display E.164 number
    credentials_enc = Column(Text, nullable=True)               # Fernet-encrypted JSON blob
    status = Column(String, default="disconnected")            # connecting | connected | error | disconnected
    is_primary = Column(Integer, default=0)                     # 1 = Primary Command Center
    enabled = Column(Integer, default=1)                        # user can disconnect without deleting
    last_error = Column(Text, nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    webhook_secret_enc = Column(Text, nullable=True)            # per-channel inbound signature secret
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EduLearnerProfile(Base):
    """EduLearner AI learner profile — the real SARA database row per learner.

    Mirrors the agent engine's UserProfile schema so the Profile tab can add/
    edit/delete every field (skills, projects, certifications, goals, resume)
    while the adaptive engine keeps reading its own JSON store.
    """

    __tablename__ = "edulearner_profiles"

    user_id = Column(String, primary_key=True)                  # learner id (hub identity)
    name = Column(String, default="")
    education = Column(Text, default="")
    experience = Column(Text, default="")
    resume_text = Column(Text, default="")                      # extracted text of the uploaded resume
    skills = Column(Text, default="[]")                         # JSON list[str]
    projects = Column(Text, default="[]")                       # JSON list[dict]
    certifications = Column(Text, default="[]")                 # JSON list[str]
    goal = Column(Text, default="")
    target_role = Column(String, default="")
    learning_style = Column(String, default="mixed")
    available_hours = Column(Integer, default=7)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EduLearnerResume(Base):
    """An uploaded resume file for an EduLearner learner.

    The extracted text lands on the profile (resume_text) and the raw file is
    kept on disk under sara_storage/edulearner/resumes for re-parsing.
    """

    __tablename__ = "edulearner_resumes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)
    filename = Column(String, nullable=False)
    stored_path = Column(Text, nullable=True)                   # where the raw file is kept
    extracted_text = Column(Text, default="")
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())