import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before importing any module that reads environment variables
# (agents, LLM clients, providers). Without this, API keys resolve to empty at boot.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import (
    auth,
    business_data,
    chat,
    desktop_agent,
    documents,
    gameforge,
    legacy,
    music_studio,
    sara_auto_agent,
    scraping_agent,
    telegram_control,
    trading_agent,
    social_media,
    users,
    voice_agent,
    whatsapp_agent,
    whatsapp_channels,
)
from app.core.config import settings as _settings
from app.core.telegram_host import mount_telegram_agent
from app.database import models
from app.database.database import engine
from app import voice_agent_engine

# Import the social media engine directly (the ONE file)
from app.agents import social_media as social_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize database objects once, after the application is created."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    models.Base.metadata.create_all(bind=engine)

    # Point the WhatsApp agent's outbound path at the Primary Command Center
    # (if one is configured in the DB) — instant, no env edits.
    from app.services import whatsapp_channels

    whatsapp_channels.ensure_seeded()
    whatsapp_channels.activate_primary()

    # Seed the demo social workspace, then start the social job worker.
    social_engine.ensure_demo_workspace()

    # Voice agent warm-up. The voice engine is a MOUNTED sub-app, and Starlette
    # does not run a mounted app's own startup events — so its
    # @app.on_event("startup") hook (phrase-cache warm-up + Twilio voice-URL
    # sync) never fired, leaving every call with a cold cache and a Twilio
    # number still pointing at the demo greeting. Drive it from the parent
    # lifespan, which is the one that actually runs.
    with contextlib.suppress(Exception):
        voice_agent_engine._schedule_phrase_warmup()
    # Heal Twilio voice channels whose stored credentials stopped decrypting
    # (rotated SARA_MASTER_KEY). Without this the primary channel reports
    # "account_sid and auth_token are required" and inbound calls never route,
    # even though backend/.env holds valid keys.
    with contextlib.suppress(Exception):
        voice_agent_engine.repair_undecryptable_channels()
    # Re-publish the number's voice URL unless explicitly disabled, so inbound
    # calls reach SARA instead of Twilio's demo greeting.
    if str(getattr(_settings, "TWILIO_AUTO_SYNC_VOICE_URL", "1")).lower() in (
        "1", "true", "yes",
    ):
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(
                asyncio.to_thread(voice_agent_engine.sync_twilio_voice_url)
            )

    # Background intake pollers (Telegram getUpdates + Zernio inbox). Both
    # respect the persisted run/pause switch, so a paused agent is not polled.
    import app.core.agent_pollers as agent_pollers

    poll_tasks = [
        asyncio.create_task(agent_pollers.telegram_poll_loop()),
        asyncio.create_task(agent_pollers.whatsapp_poll_loop()),
        asyncio.create_task(social_engine.social_poll_loop()),
        asyncio.create_task(social_engine.social_pilot_loop()),
    ]

    # Sara Bots custom-worker engine + schedule ticker. The engine is a real
    # in-process asyncio worker; task state lives in durable JSON stores, so
    # tasks survive frontend disconnects and restarts (with honest recovery).
    from app.api.deps import API_KEY as _sara_api_key
    from app.services.sara_bots.engine import get_engine
    from app.services.sara_bots.scheduler import get_scheduler

    _bots_engine = get_engine()
    _bots_engine.configure(
        os.environ.get("SARA_INTERNAL_BASE_URL", "http://127.0.0.1:8000"),
        _sara_api_key)
    poll_tasks.append(asyncio.create_task(_bots_engine.run_forever()))
    poll_tasks.append(
        asyncio.create_task(get_scheduler(_bots_engine).run_forever()))
    # SaraMuse page watches — real scheduled checks of public pages with
    # failure backoff (OpenMuse monitor semantics).
    from app.services.sara_bots.muse import get_monitor_worker
    poll_tasks.append(asyncio.create_task(get_monitor_worker().run_forever()))
    # SARA Sales Agent worker — drains queued sales tasks and, in continuous
    # mode, re-qualifies stored leads on schedule (never sends by itself).
    from app.services.sales_agent import get_sales_worker
    poll_tasks.append(asyncio.create_task(get_sales_worker().run_forever()))
    yield
    # Stop pollers, then release external clients (Unipile httpx) on shutdown
    for task in poll_tasks:
        task.cancel()

    # Graceful shutdown of the Email Automation Agent (cancel running campaign workers)
    from app.agents import email_automation

    await email_automation.orchestrator.shutdown()
    for task in poll_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    await whatsapp_agent.agent.aclose()


app = FastAPI(title="SARA AI", version="1.0.0", lifespan=lifespan)

# Single unified backend — allow the Next.js frontend (and its local origins)
#
# Production origins: the frontend is deployed separately (Vercel) and browsers
# enforce CORS per-origin. The previous regex matched loopback only, so a
# deployed frontend was silently refused every request while the backend itself
# looked perfectly healthy.
#
# SARA_ALLOWED_ORIGINS is a comma-separated list of exact origins. It is
# deliberately NOT defaulted to "*": the API is key-authenticated with
# credentials, and a wildcard plus credentials is both rejected by browsers and
# wrong for an API that can act on the user's desktop.
_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5000",
]
for _origin in (os.environ.get("SARA_ALLOWED_ORIGINS", "") or "").split(","):
    _origin = _origin.strip().rstrip("/")
    if _origin and _origin not in _ALLOWED_ORIGINS:
        _ALLOWED_ORIGINS.append(_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    # Dev servers may bind to any local port (e.g. 10483); allow all loopback origins.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# SARA AI routers (chat RAG, users, documents, voice agent, business profiles, Gmail auth)
app.include_router(users.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(voice_agent.router)
app.include_router(auth.router)
app.include_router(business_data.router)
app.include_router(gameforge.router)
app.include_router(music_studio.router)
app.include_router(scraping_agent.router)

# Legacy endpoints (orchestrate, /api/v1/chat, WhatsApp webhook)
app.include_router(legacy.router)

# Desktop agent (tasks, tools, confirmations, pause/resume, live WebSocket feed)
app.include_router(desktop_agent.router)

# Multi-tenant Telegram booking agent under /agent/*
mount_telegram_agent(app)

# Full voice command center under /voice-agent/*.
app.mount("/voice-agent", voice_agent_engine.app)

# WhatsApp hotel agent (Grand Stay) under /whatsapp/*
app.include_router(whatsapp_agent.router)

# WhatsApp Numbers / Channels management under /whatsapp/channels/*
app.include_router(whatsapp_channels.router)

# Telegram agent run/pause control under /telegram/*
app.include_router(telegram_control.router)

# Trading signal agent under /trading/* (backtest engine, no LLM dependency)
app.include_router(trading_agent.router)

# Social Media Agency under /social/* (OAuth, scheduling, job worker)
# Router is defined inside the consolidated single-file engine module.
app.include_router(social_engine.router)

# Frontend Developer Agent under /frontend-developer/*
from app.agents import frontend_developer
app.mount("/frontend-developer", frontend_developer.app)

# Backend Architect Agent under /backend-architect/*
from app.agents import backend_architect
app.mount("/backend-architect", backend_architect.app)

# Email Automation Agent under /email-agent/* (PDF/CSV/Sheets ingestion,
# LLM-personalized emails, SMTP/Gmail providers, campaign orchestrator)
from app.agents import email_automation
app.include_router(email_automation.router)

# EduLearner AI Agent under /api/edulearner/* (adaptive learning engine:
# profile -> gap analysis -> roadmap -> plan -> practice -> adaptive loop)
from app.agents.edulearner import api as edulearner_api
app.include_router(edulearner_api.router)

# SARA Ultron Agent under /ultron/* (supervisory loop: plan -> execute ->
# replan -> report -> remember, over the shared sara_desktop action registry
# and the centralized LLM router)
from app.agents import sara_ultron_agent
app.include_router(sara_ultron_agent.router)

@app.get("/api/health")
def health_check():
    return {"status": "online", "message": "SARA AI Backend Engine is fully operational."}


# Bare-root alias for the same payload. Local tooling (dev-server scanners,
# IDE integrations, other agents) commonly probes ``http://<host>:8000/health``
# without the ``/api`` prefix; answering those with 200 keeps the access log
# free of 404 noise instead of logging an error for a healthy server.
# Additive: ``/api/health`` is unchanged and remains the canonical route.
@app.get("/health")
def health_check_root():
    return health_check()


# Kubernetes-style sibling of the same probe, for the same reason.
@app.get("/healthz")
def health_check_z():
    return health_check()


@app.get("/api/llm/health")
def llm_health():
    """LLM failover chain status — provider health, active provider, last errors.
    Never exposes API keys."""
    from app.core.llm_router import health_snapshot

    return health_snapshot()

# Agency Agents (267 markdown prompt agents) under /api/agency/*
from app.api import agency_agents
app.include_router(agency_agents.router)

# Sara Bots (real-agent workspace: fleet-probed hub bots, agency specialists,
# approved local sara_desktop actions) under /api/sara-bots/*
from app.api import sara_bots, sara_bots_custom
app.include_router(sara_bots.router)
# Sara Bots custom workers (My Bots: wizard, durable tasks, approvals,
# schedules, per-bot memory, factory) — same prefix, same auth.
app.include_router(sara_bots_custom.router)
# SaraMuse powers (OpenMuse-inspired: goals, page watches, ideas, finance,
# notifications) — same prefix, same auth.
from app.api import sara_bots_muse
app.include_router(sara_bots_muse.router)
# SARA Autonomous AI Sales Agent (lead discovery, campaigns, command center
# chat) — same shared API key as the rest of the platform.
from app.api import sales_agent as sales_agent_api
app.include_router(sales_agent_api.router)

# SARA Fully Automated Agent under /sara-auto/*
app.include_router(sara_auto_agent.router)