import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Get absolute path to the backend directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = BASE_DIR / ".env"

class Settings(BaseSettings):
    # DATABASE_URL takes precedence; SARA_DB_URL is kept for existing installs.
    DATABASE_URL: str | None = None
    SARA_DB_URL: str | None = None
    OPENROUTER_API_KEY: str | None = None

    # Google Gemini (native generateContent API). When GEMINI_API_KEY is set the
    # shared LLM router tries it first and fails over to the other providers.
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-flash-latest"

    # LLM brain (shared by chat/voice/email agents) — driven by backend/.env
    LLM_API_KEY: str | None = None
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_MODEL: str = "openai/gpt-4o-mini"
    
    # Specific agent models
    VOICE_LLM_MODEL: str | None = None
    CHAT_LLM_MODEL: str | None = None
    FRONTEND_LLM_MODEL: str | None = None
    ARCHITECT_LLM_MODEL: str | None = None
    WHATSAPP_LLM_MODEL: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_PHONE_NUMBER: str | None = None
    # Public HTTPS tunnel Twilio can reach. Falls back to PUBLIC_BASE_URL when
    # unset, so existing deployments keep working unchanged.
    TWILIO_WEBHOOK_BASE_URL: str | None = None
    # Twilio OAuth (client-credentials grant). Used INSTEAD of the auth token
    # when both are present; the account SID still identifies the account.
    TWILIO_CLIENT_ID: str | None = None
    TWILIO_CLIENT_SECRET: str | None = None
    # Twilio API key pair (standard basic-auth alternative to the account auth
    # token). Kept optional; the auth token above remains the default path.
    TWILIO_API_KEY_SID: str | None = None
    TWILIO_API_KEY_SECRET: str | None = None
    # Re-publish the number's voice URL at boot so inbound calls reach SARA
    # instead of Twilio's demo greeting.
    TWILIO_AUTO_SYNC_VOICE_URL: str = "1"
    PUBLIC_BASE_URL: str | None = None
    ELEVENLABS_API_KEY: str | None = None
    ELEVENLABS_VOICE_ID: str = "21m00Tcm4TlvDq8ikWAM"
    VOICE_TTS_PROVIDER: str | None = None
    VOICE_STT_PROVIDER: str | None = None
    SARVAM_API_KEY: str | None = None
    # Sarvam model IDs — the older saaras:v2.5 / bulbul:v2 + anushka voice are
    # deprecated and now return HTTP 400.
    SARVAM_STT_MODEL: str = "saaras:v3"
    SARVAM_TTS_MODEL: str = "bulbul:v3"
    SARVAM_TTS_SPEAKER: str = "priya"
    SARVAM_TTS_LANGUAGE: str = "en-IN"
    
    # SMTP Email Configuration
    GMAIL_SENDER: str | None = None
    GMAIL_APP_PASSWORD: str | None = None

    # Generic SMTP override (falls back to Gmail SMTP when unset)
    SMTP_HOST: str | None = None
    SMTP_PORT: int | None = None
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_SSL: str | None = None

    # Email Automation Agent
    EMAIL_AGENT_FROM: str | None = None

    # Google OAuth (used by /api/auth/google)
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None

    # Google Sheets service account (Email Automation Agent) — paste the full JSON
    # as one line, or point GOOGLE_CREDENTIALS_FILE at a downloaded key file.
    GOOGLE_CREDENTIALS_JSON: str | None = None
    GOOGLE_CREDENTIALS_FILE: str | None = None

    # Kapso WhatsApp gateway (https://app.kapso.ai) — project API key +
    # phone number id of the connected number + the webhook secret you enter
    # in the Kapso dashboard when creating the webhook.
    KAPSO_API_KEY: str | None = None
    KAPSO_PHONE_NUMBER_ID: str | None = None
    KAPSO_WEBHOOK_SECRET: str | None = None

    # Shared API key protecting backend routes (expected in backend/.env)
    SARA_API_KEY: str | None = None

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
