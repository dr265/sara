"""SARA Ultron - a first-class supervisor agent for the SARA AI Nexus Hub.

SARA Ultron takes a high-level goal, decomposes it with SARA's centralized LLM
router, executes the resulting tool steps for real against SARA's shared tool
registry, re-plans around genuine failures, reports honestly on what actually
happened, and remembers the outcome for next time.

It is registered the same way as every other SARA agent, in
``backend/app/main.py``:

    from app.agents import sara_ultron_agent
    app.include_router(sara_ultron_agent.router)

What is reused from SARA rather than duplicated
----------------------------------------------
* ``app.core.llm_router``   - planning, reporting and summarization go through
  the centralized provider chain with failover and model fallback.
* ``sara_desktop`` actions  - the same auto-discovered action registry
  ``app/api/sara_auto_agent.py`` drives. One action set, two agents.
* ``app.core.memory_manager`` - ``PersistentMemory`` is the on-disk store;
  ``UltronMemory`` only adds a lock and a lexical recall scorer over it.
* ``app.api.deps.require_api_key`` - the shared auth dependency.
* ``logging.getLogger("sara.ultron.*")`` - SARA's logging convention.

What is new
-----------
The supervisory loop itself: plan up front, validate every step against the
tools that actually exist, apply a retry/skip/abort error policy, and re-plan
around failures instead of retrying blindly. That logic is adapted from the
standalone Brahma-Echo agent; its provider-specific clients, plaintext key
files and direct ``google.generativeai`` calls were deliberately not carried
over.

Context awareness
-----------------
Phase 5 added a Context Awareness Engine: a foreground-window observer, one
adapter per application family, a tiered vision stack (accessibility tree
first, screenshot only if the user explicitly enables it), reference
resolution that asks rather than guesses, and an always-on microphone loop
with barge-in. Three independent gates - ``microphone``, ``screen`` and
``control`` - are all **default-off**, and revoking one takes effect on the
next request rather than on the next restart.
"""

from .api import router
from .core.adapters import (
    AppAdapter,
    AdapterContext,
    AdapterRegistry,
    RawWindow,
    registry,
)
from .core.alwayson import AlwaysOnVoice, always_on
from .core.awareness import (
    AwarenessState,
    ContextManager,
    ContextSnapshot,
    context,
    context_manager,
)
from .core.bus import EventBus, Subscription, bus
from .core.catalog import AGENT_CATALOG, AGENT_ID, AGENT_METADATA, AGENT_SLUG, API_PREFIX
from .core.devices import (
    DEVICE_KINDS,
    DEVICE_SCOPES,
    Device,
    DeviceRegistry,
    PairingOffer,
    registry as device_registry,
)
from .core.executor import ErrorDecision, StepResult, UltronExecutor
from .core.fleet import AGENT_PROBES, probe_fleet
from .core.memory import UltronMemory
from .core.observer import WindowObserver, observer
from .core.permissions import GATES, PermissionDenied, PermissionRegistry, permissions
from .core.planner import Plan, PlanStep, UltronPlanner
from .core.references import Resolution, resolve
from .core.session import PHASES, TERMINAL_PHASES, UltronSession, store
from .core.tools import (
    NATIVE_TOOL_NAMES,
    UltronToolRegistry,
    get_memory,
    get_registry,
    reset_registry,
)
from .core.vision import VisionEngine, vision
from .core.voice_state import VOICE_STATES, VoiceStateTracker, derive_voice_state
from .orchestrator import UltronOrchestrator, run_session

__all__ = [
    # registration surface
    "router",
    "AGENT_ID",
    "AGENT_METADATA",
    "AGENT_CATALOG",
    "AGENT_SLUG",
    "API_PREFIX",
    # core
    "UltronOrchestrator",
    "UltronPlanner",
    "UltronExecutor",
    "UltronMemory",
    "UltronToolRegistry",
    "Plan",
    "PlanStep",
    "StepResult",
    "ErrorDecision",
    "UltronSession",
    "store",
    "PHASES",
    "TERMINAL_PHASES",
    "AGENT_PROBES",
    "probe_fleet",
    # realtime + devices + voice state
    "bus",
    "EventBus",
    "Subscription",
    "device_registry",
    "DeviceRegistry",
    "Device",
    "PairingOffer",
    "DEVICE_KINDS",
    "DEVICE_SCOPES",
    "VOICE_STATES",
    "VoiceStateTracker",
    "derive_voice_state",
    # context awareness + permissions
    "GATES",
    "permissions",
    "PermissionRegistry",
    "PermissionDenied",
    "context",
    "context_manager",
    "ContextManager",
    "ContextSnapshot",
    "AwarenessState",
    "registry",
    "AdapterRegistry",
    "AppAdapter",
    "AdapterContext",
    "RawWindow",
    "observer",
    "WindowObserver",
    "vision",
    "VisionEngine",
    "resolve",
    "Resolution",
    "always_on",
    "AlwaysOnVoice",
    "NATIVE_TOOL_NAMES",
    "get_registry",
    "get_memory",
    "reset_registry",
    "run_session",
]

__version__ = "1.0.0"
