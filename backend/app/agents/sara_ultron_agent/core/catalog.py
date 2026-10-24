"""SARA agent catalogue - the backend mirror of the Nexus Hub registry.

The Hub's agent inventory lives in ``frontend/src/lib/agents.config.ts``
(``AGENT_REGISTRY``). SARA Ultron needs the same inventory on the backend so
it can *route* work to the right agent instead of trying to do everything
itself, so this module keeps a backend copy of that inventory.

This is deliberately read-only metadata: it is used for planning and for the
``ultron_agent_catalog`` tool. It is NOT a second source of truth for
registration - the live registration surface is still
``app.include_router(...)`` in ``app/main.py`` plus the mounted sub-apps.

Keep this in sync with ``AGENT_REGISTRY`` in agents.config.ts.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: Canonical identifier for this agent, as required by the integration spec.
AGENT_ID = "sara_ultron_agent"

#: Route slug used by the Hub frontend (``/hub/<slug>``).
AGENT_SLUG = "sara-ultron"

#: API prefix the router is mounted under.
API_PREFIX = "/ultron"

AGENT_METADATA: Dict[str, Any] = {
    "id": AGENT_ID,
    "name": "SARA Ultron",
    "category": "General",
    "short_description": (
        "Goal-directed supervisor that plans, executes, replans and remembers "
        "multi-step work across the SARA agent ecosystem."
    ),
    "version": "1.0.0",
    "api_prefix": API_PREFIX,
    "route": f"/hub/{AGENT_SLUG}",
    "sse": True,
    "websocket": False,
    "requires_auth": True,
    "capabilities": [
        "goal_decomposition",
        "tool_execution",
        "error_retry_skip_abort",
        "replanning",
        "long_term_memory",
        "agent_delegation",
    ],
}

#: Every agent Ultron may delegate a reasoning subtask to. Keys are the
#: ``agent_id`` values accepted by the existing SARA inter-agent endpoint
#: ``POST /api/v1/chat`` (see app/api/legacy.py).
DELEGATABLE_AGENTS: List[Dict[str, str]] = [
    {
        "agent_id": "backend-architect",
        "name": "Backend Architect",
        "description": "Backend infrastructure, API design and database schema work.",
    },
    {
        "agent_id": "frontend-developer",
        "name": "Frontend Developer",
        "description": "Responsive React/Next.js interface implementation.",
    },
    {
        "agent_id": "email-automation",
        "name": "Email Automation",
        "description": "Email campaigns, personalization and outreach.",
    },
    {
        "agent_id": "edulearner-ai",
        "name": "EduLearner AI",
        "description": "Adaptive learning, skill-gap analysis and study roadmaps.",
    },
    {
        "agent_id": "trading-agent",
        "name": "Trading Agent",
        "description": "Market signal analysis and trade logic.",
    },
    {
        "agent_id": "social-media",
        "name": "Social Media Manager",
        "description": "Content strategy, trends and publishing.",
    },
    {
        "agent_id": "telegram-assistant",
        "name": "Telegram Assistant",
        "description": "Community engagement and group moderation.",
    },
    {
        "agent_id": "whatsapp-agent",
        "name": "SARA WhatsApp Engine",
        "description": "WhatsApp conversational AI and document RAG.",
    },
    {
        "agent_id": "ai-web-scraper",
        "name": "AI Web Scraper",
        "description": "Automated web extraction and synthesis.",
    },
    {
        "agent_id": "voice-agent",
        "name": "Voice Agent",
        "description": "Speech-to-text, synthesis and realtime calls.",
    },
]

#: The full Hub inventory (mirror of AGENT_REGISTRY). Used for planning hints
#: and by the ``ultron_agent_catalog`` tool.
AGENT_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "sara-ultron",
        "name": "SARA Ultron",
        "category": "General",
        "short_description": AGENT_METADATA["short_description"],
        "route": f"/hub/{AGENT_SLUG}",
        "status": "online",
    },
    {
        "id": "backend-architect",
        "name": "Backend Architect",
        "category": "Engineering",
        "short_description": "Designs and scales backend infrastructure and API architecture.",
        "route": "/hub/backend-architect",
        "status": "online",
    },
    {
        "id": "ai-web-scraper",
        "name": "AI Web Scraper",
        "category": "Research",
        "short_description": "Automated extraction and synthesis of web data.",
        "route": "/hub/ai-web-scraper",
        "status": "beta",
    },
    {
        "id": "edulearner-ai",
        "name": "EduLearner AI",
        "category": "Education",
        "short_description": "Evidence-based adaptive learning and skill-gap analysis.",
        "route": "/hub/edulearner-ai",
        "status": "online",
    },
    {
        "id": "email-automation",
        "name": "Email Automation",
        "category": "Marketing",
        "short_description": "Automates email campaigns and personalized outreach.",
        "route": "/hub/email-automation",
        "status": "online",
    },
    {
        "id": "frontend-developer",
        "name": "Frontend Developer",
        "category": "Engineering",
        "short_description": "Builds responsive React/Next.js interfaces from design specs.",
        "route": "/hub/frontend-developer",
        "status": "online",
    },
    {
        "id": "game-forge",
        "name": "Game Forge",
        "category": "Engineering",
        "short_description": "Generates game assets and logic.",
        "route": "/hub/game-forge",
        "status": "beta",
    },
    {
        "id": "music-studio",
        "name": "Music Studio",
        "category": "Other",
        "short_description": "AI generated music and audio.",
        "route": "/hub/music-studio",
        "status": "beta",
    },
    {
        "id": "sara-auto",
        "name": "SARA Universal",
        "category": "General",
        "short_description": "General purpose orchestrator for complex multi-step workflows.",
        "route": "/hub/sara-auto",
        "status": "online",
    },
    {
        "id": "social-media",
        "name": "Social Media Manager",
        "category": "Marketing",
        "short_description": "Strategizes and schedules cross-platform social content (X/Twitter).",
        "route": "/hub/social-media",
        "status": "online",
    },
    {
        "id": "telegram-assistant",
        "name": "Telegram Assistant",
        "category": "Business",
        "short_description": "Automates community engagement on Telegram.",
        "route": "/hub/telegram-assistant",
        "status": "online",
    },
    {
        "id": "trading-agent",
        "name": "Trading Agent",
        "category": "Finance",
        "short_description": "Analyzes market signals and formulates trade logic.",
        "route": "/hub/trading-agent",
        "status": "online",
    },
    {
        "id": "voice-agent",
        "name": "Voice Agent",
        "category": "Other",
        "short_description": "Voice-driven conversational AI interface.",
        "route": "/hub/voice-agent",
        "status": "beta",
    },
    {
        "id": "whatsapp-agent",
        "name": "SARA WhatsApp Engine",
        "category": "Business",
        "short_description": "Core engine for WhatsApp conversational AI.",
        "route": "/hub/whatsapp-agent",
        "status": "online",
    },
]

_DELEGATABLE_IDS = frozenset(a["agent_id"] for a in DELEGATABLE_AGENTS)


def is_delegatable(agent_id: str) -> bool:
    """True when ``agent_id`` may be used with the ``ultron_delegate`` tool."""
    return str(agent_id or "").strip().lower() in _DELEGATABLE_IDS
