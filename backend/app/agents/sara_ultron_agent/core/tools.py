"""Ultron's unified tool surface.

Two sources of tools, one interface:

1. **SARA's desktop actions.** ``sara_desktop/core/action_loader.discover_actions``
   already auto-discovers every ``actions/*.py`` exposing a module-level
   ``TOOL`` dict. SARA's autonomous agent (``app/api/sara_auto_agent.py``)
   consumes that registry directly. Ultron's executor consumes the *same*
   registry - one action set, two agents, zero duplication.

2. **Ultron-native tools** declared below. These cover the things the
   desktop action set cannot do: introspecting the SARA agent ecosystem,
   delegating a reasoning subtask to another SARA agent, reading/writing
   long-term memory, and summarising. They use the same ``TOOL`` dict shape
   as desktop actions so the two sources merge without special-casing.

Native tool names are passed to ``discover_actions`` as *reserved* names, so
a desktop action can never shadow (or be shadowed by) an Ultron tool.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .catalog import AGENT_CATALOG, DELEGATABLE_AGENTS, is_delegatable
from .memory import UltronMemory
from .voice import get_voice

log = logging.getLogger("sara.ultron.tools")

# sara_desktop lives at the repo root, sibling to backend/.
# .../sara-ai-platform/backend/app/agents/sara_ultron_agent/core/tools.py
#
# This used to be a hard-coded ``parents[5]``, which is correct in a checkout and
# wrong in the Docker image: there the contents of backend/ are copied to /app,
# so the same walk lands one level too high at "/" and resolved to
# "/sara_desktop" - which does not exist and cannot be created by a non-root
# user, so every Ultron task thread died with PermissionError while building
# the tool registry. Search instead of counting, and let SARA_DESKTOP_DIR win.
def _find_desktop_dir() -> Path:
    override = os.environ.get("SARA_DESKTOP_DIR", "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "sara_desktop"
        if (candidate / "actions").is_dir():
            return candidate
    # Nothing found. Return the historical location so the error message names
    # a path the operator can recognise, rather than an empty string.
    return here.parents[5] / "sara_desktop"


REPO_ROOT = Path(__file__).resolve().parents[5]
DESKTOP_DIR = _find_desktop_dir()

MAX_TOOL_OUTPUT = 3500          # chars of tool output fed back into the model
EVENT_OUTPUT_LIMIT = 1200       # chars of tool output shown on the SSE stream

_TYPE_MAP = {"object": "object", "string": "string", "number": "number",
             "integer": "integer", "boolean": "boolean", "array": "array"}


def conv_schema(p: Any) -> Any:
    """Gemini-style schema (OBJECT/STRING ...) -> OpenAI-style (object/string ...)."""
    if not isinstance(p, dict):
        return p
    out = dict(p)
    t = str(p.get("type", "")).lower()
    out["type"] = _TYPE_MAP.get(t, t or "string")
    if isinstance(p.get("properties"), dict):
        out["properties"] = {k: conv_schema(v) for k, v in p["properties"].items()}
    if isinstance(p.get("items"), dict):
        out["items"] = conv_schema(p["items"])
    return out


# ---------------------------------------------------------------------------
# Native tool handlers
# ---------------------------------------------------------------------------
def _tool_agent_catalog(args: Dict[str, Any]) -> str:
    """List the SARA agents Ultron knows about."""
    return json.dumps({"agents": AGENT_CATALOG}, ensure_ascii=False, indent=2)


def _tool_delegate(args: Dict[str, Any]) -> str:
    """Delegate a reasoning subtask to another SARA agent.

    Uses the inter-agent channel SARA already exposes at ``POST /api/v1/chat``
    (``app/api/legacy.py``), called in-process so no port/URL is guessed.
    """
    agent_id = str((args or {}).get("agent_id", "")).strip().lower()
    message = str((args or {}).get("message", "")).strip()

    if not agent_id or not message:
        return "ultron_delegate requires both 'agent_id' and 'message'."
    if not is_delegatable(agent_id):
        allowed = ", ".join(a["agent_id"] for a in DELEGATABLE_AGENTS)
        return f"Cannot delegate to '{agent_id}'. Delegatable agents: {allowed}."

    try:
        from app.api.legacy import ChatRequest, chat_with_agent

        result = chat_with_agent(ChatRequest(agent_id=agent_id, message=message))
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a failed step
        return f"Delegation to '{agent_id}' failed: {type(exc).__name__}: {exc}"

    reply = (result or {}).get("reply") or ""
    return f"[{agent_id}] {str(reply).strip()}" if reply else f"'{agent_id}' returned no reply."


def _tool_recall(memory: UltronMemory) -> Callable[[Dict[str, Any]], str]:
    def handler(args: Dict[str, Any]) -> str:
        query = str((args or {}).get("query", "")).strip()
        if not query:
            return "ultron_recall requires a non-empty 'query'."
        hits = memory.recall(query)
        if not hits:
            return "No prior Ultron memory matched this query."
        return json.dumps(hits[:3], ensure_ascii=False, indent=2)[:MAX_TOOL_OUTPUT]

    return handler


def _tool_answer(args: Dict[str, Any]) -> str:
    """Answer a question in natural language via SARA's LLM router.

    This is the tool that makes Ultron usable as an assistant rather than only
    as an automator. Without it the planner correctly returns ``{"steps": []}``
    for a plain question ("who are you", "what can you do"), and the runner
    used to treat that empty plan as a planning failure -- so every ordinary
    question ended as "degraded, goal unverified". Now a conversational goal
    routes here and gets a real answer.
    """
    question = str((args or {}).get("question", "")).strip()
    if not question:
        return "ultron_answer requires a non-empty 'question'."
    from app.core.llm_router import llm_chat_sync

    reply = llm_chat_sync(
        [
            {"role": "system", "content":
                "You are SARA Ultron, the supervisory agent of the SARA AI Nexus Hub.\n"
                "Answer directly and concisely (2-5 sentences) in plain text.\n"
                "You can plan goals, execute tools across the user's desktop and "
                "files, browse the web, delegate to specialist agents, and "
                "remember past runs. Never use markdown headers.\n"
                "Never reply with a single word, a placeholder, or a restatement "
                "of this instruction."},
            {"role": "user", "content": question[:MAX_TOOL_OUTPUT]},
        ],
        max_tokens=500,
        temperature=0.3,
        timeout=90.0,
    )
    # Cheap "auto" routers sometimes answer with a one-word stub. A question the
    # user actually asked deserves a real answer, so an empty or stub reply is
    # retried once at zero temperature before giving up and saying so plainly.
    text = (reply or "").strip()
    if len(text) < 20:
        retry = llm_chat_sync(
            [
                {"role": "system", "content":
                    "Answer the user's question in plain English. Be specific and "
                    "helpful. Do not use markdown headers."},
                {"role": "user", "content": question[:MAX_TOOL_OUTPUT]},
            ],
            max_tokens=500,
            temperature=0.0,
            timeout=90.0,
        )
        if len((retry or "").strip()) > len(text):
            text = retry.strip()
    if len(text) < 20:
        return ("I could not get a usable answer from any configured AI provider "
                "just now. The question was: " + question[:300])
    return text


def _tool_summarize(args: Dict[str, Any]) -> str:
    """Condense text using SARA's centralized LLM router (with failover)."""
    text = str((args or {}).get("text", "")).strip()
    if not text:
        return "ultron_summarize requires non-empty 'text'."
    from app.core.llm_router import llm_chat_sync

    return llm_chat_sync(
        [
            {"role": "system", "content":
                "Summarise the user's content in at most three sentences. "
                "State only what the content says; add nothing."},
            {"role": "user", "content": text[:MAX_TOOL_OUTPUT]},
        ],
        max_tokens=300,
        temperature=0.2,
        timeout=60.0,
    )


# ---------------------------------------------------------------------------
# Native tool declarations (same shape as a desktop action's TOOL dict)
# ---------------------------------------------------------------------------
def _native_tools(memory: UltronMemory) -> List[Dict[str, Any]]:
    return [
        {
            "name": "ultron_agent_catalog",
            "description": (
                "List every agent in the SARA ecosystem with its id, name and what it does. "
                "Use this before delegating work so you pick a real agent_id."
            ),
            "parameters": {"type": "object", "properties": {}},
            "handler": _tool_agent_catalog,
        },
        {
            "name": "ultron_delegate",
            "description": (
                "Delegate a self-contained subtask to another SARA agent and get its reply. "
                "Use for work outside your own tools (code, learning plans, campaigns, market analysis). "
                "Only use agent_id values returned by ultron_agent_catalog."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target SARA agent id."},
                    "message": {"type": "string", "description": "The complete, standalone subtask."},
                },
                "required": ["agent_id", "message"],
            },
            "handler": _tool_delegate,
        },
        {
            "name": "ultron_recall",
            "description": "Search Ultron's long-term memory for past runs and learned rules relevant to a query.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to look up."}},
                "required": ["query"],
            },
            "handler": _tool_recall(memory),
        },
        {
            "name": "ultron_remember",
            "description": (
                "Persist a durable lesson or manage learned rules. Default action 'remember'. "
                "Use 'rule' for a behavioural directive, 'goal' alone for a standing "
                "preference or fact to carry forward, and 'goal'+'success'+'errors' only "
                "when reporting the outcome of a run that actually happened. "
                "action 'list_rules' shows stored rules with their ids; "
                "action 'toggle_rule'/'delete_rule' needs 'rule_id'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["remember", "list_rules", "toggle_rule", "delete_rule"],
                        "description": "What to do. Defaults to remember.",
                    },
                    "goal": {"type": "string", "description": "What the task was."},
                    "rule": {"type": "string", "description": "A behavioural rule to always apply."},
                    "success": {"type": "boolean", "description": "Did the run succeed?"},
                    "errors": {"type": "string", "description": "Failure detail, if any."},
                    "rule_id": {"type": "string", "description": "Rule id for toggle/delete."},
                },
                "required": [],
            },
            "handler": memory.as_tool,
        },
        {
            "name": "ultron_answer",
            "description": (
                "Answer a question in natural language. Use this whenever the user "
                "asks something conversational or informational (who are you, what "
                "can you do, explain X) rather than asking for a tool to be run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to answer."},
                },
                "required": ["question"],
            },
            "handler": _tool_answer,
        },
        {
            "name": "ultron_summarize",
            "description": "Condense long text into a few sentences using SARA's LLM router.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to condense."}},
                "required": ["text"],
            },
            "handler": _tool_summarize,
        },
        *get_voice().as_tools(),
    ]


#: Declared up front (not derived from _native_tools, which needs a memory
#: instance) so discover_actions can reserve them before any file is imported.
NATIVE_TOOL_NAMES = frozenset({
    "ultron_agent_catalog",
    "ultron_answer",
    "ultron_delegate",
    "ultron_listen",
    "ultron_recall",
    "ultron_remember",
    "ultron_speak",
    "ultron_summarize",
})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class UltronToolRegistry:
    """Desktop actions + Brahma-Echo actions + Ultron-native tools, one interface."""

    def __init__(self, desktop_registry: Any, native: List[Dict[str, Any]],
                 brahma: Any = None) -> None:
        self._desktop = desktop_registry
        self._brahma = brahma
        self._native = {t["name"]: t for t in native}
        self._descriptions: Dict[str, str] = {
            name: t["description"] for name, t in self._native.items()
        }
        for name in self._desktop.names():
            rec = self._desktop._actions.get(name)
            self._descriptions[name] = (rec.description if rec else name)
        for name in self._brahma_names():
            rec = self._brahma.record(name)
            self._descriptions[name] = (rec.description if rec else name)

    # -- discovery ------------------------------------------------------
    @classmethod
    def load(cls, memory: Optional[UltronMemory] = None,
             with_brahma: bool = True) -> "UltronToolRegistry":
        """Build the registry once per process (thread-safe, lazy).

        The Brahma-Echo set is loaded into *this* registry only. It is
        deliberately never written into ``sara_desktop/actions``, so the
        autonomous agent's tool surface stays exactly as it was.
        """
        if str(DESKTOP_DIR) not in sys.path:
            sys.path.insert(0, str(DESKTOP_DIR))
        from core.action_loader import discover_actions

        def _quiet(_msg: str) -> None:  # action_loader logs via print by default
            pass

        desktop = discover_actions(
            DESKTOP_DIR / "actions",
            reserved_names=set(NATIVE_TOOL_NAMES),
            logger=_quiet,
        )

        brahma = None
        if with_brahma:
            from .brahma_bridge import get_bridge

            try:
                brahma = get_bridge()
            except Exception as exc:  # noqa: BLE001 - never fatal to the agent
                log.warning("Brahma-Echo bridge unavailable: %s", exc)
                brahma = None

        return cls(desktop, _native_tools(memory or UltronMemory()), brahma)

    # -- introspection --------------------------------------------------
    def _brahma_names(self) -> List[str]:
        return sorted(self._brahma.names()) if self._brahma is not None else []

    def names(self) -> List[str]:
        return sorted(
            set(self._desktop.names()) | set(self._native) | set(self._brahma_names())
        )

    def has(self, name: str) -> bool:
        return (
            name in self._native
            or self._desktop.has(name)
            or (self._brahma is not None and self._brahma.has(name))
        )

    def _schema(self, name: str) -> Dict[str, Any]:
        """The parameter schema for one tool, from whichever source owns it."""
        if name in self._native:
            return self._native[name]["parameters"] or {}
        if self._brahma is not None and self._brahma.has(name):
            rec = self._brahma.record(name)
            return (rec.parameters if rec else None) or {
                "type": "object", "properties": {}
            }
        rec = self._desktop._actions.get(name)
        if rec is None:
            return {}
        return rec.parameters or {"type": "object", "properties": {}}

    def required_args(self, name: str) -> List[str]:
        """Parameter names a tool cannot be called without."""
        required = self._schema(name).get("required")
        if isinstance(required, (list, tuple)):
            return [str(r) for r in required]
        return []

    def missing_args(self, name: str, args: Dict[str, Any]) -> List[str]:
        """Required parameters that are absent or blank in this call.

        Models routinely invent a differently-named argument (``task`` instead
        of ``message``). Rejecting the call before dispatch saves a wasted
        tool call and gives the re-planner an unambiguous reason.
        """
        args = args or {}
        missing = []
        for key in self.required_args(name):
            value = args.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(key)
        return missing

    def descriptions(self) -> str:
        """Tool catalogue for the planner prompt, grouped by source.

        Each line carries the tool's required parameters: without them the
        planner has to guess argument names from the description alone, which
        is exactly how an invented ``task`` argument gets in front of a tool
        that wants ``message``.
        """
        def _line(name: str) -> str:
            required = self.required_args(name)
            suffix = f" (requires: {', '.join(required)})" if required else ""
            return f"- {name}{suffix}: {self._descriptions.get(name, name)}"

        brahma = self._brahma_names()
        lines: List[str] = []
        if brahma:
            gated = [n for n in brahma if self.is_gated(n)]
            lines += [
                "BRAHMA-ECHO ACTIONS (its full action set, Ultron only).",
                "Most of these take an 'action' field; the accepted values are "
                "listed in each tool's description:",
            ]
            lines += [_line(n) for n in brahma]
            if gated:
                lines += [
                    "",
                    "These change the machine, other people's data, or an "
                    "account, so they always stop for the user's approval "
                    "first: " + ", ".join(gated),
                ]
            lines.append("")

        desktop = sorted(self._desktop.names())
        native = sorted(self._native)
        if desktop or native or not brahma:
            lines += ["SARA DESKTOP ACTIONS (operate the user's PC):"]
            lines += [_line(n) for n in desktop] or ["- (none)"]
            lines.append("")
        lines.append("ULTRON NATIVE TOOLS (reasoning, memory, delegation, voice):")
        lines += [_line(n) for n in native] or ["- (none)"]
        return "\n".join(lines)

    def declarations(self) -> List[Dict[str, Any]]:
        """OpenAI-format tool list the planner/executor can send to the LLM."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": self._descriptions.get(name, name),
                    "parameters": conv_schema(self._schema(name)),
                },
            }
            for name in self.names()
        ]

    # -- execution ------------------------------------------------------
    def run(self, name: str, args: Optional[Dict[str, Any]] = None) -> str:
        args = args or {}
        if name in self._native:
            try:
                return str(self._native[name]["handler"](args)) or "Done."
            except Exception as exc:  # noqa: BLE001 - reported to the planner as a step failure
                log.warning("Ultron native tool '%s' failed: %s", name, type(exc).__name__)
                return f"Tool '{name}' failed: {exc}"
        if self._brahma is not None and self._brahma.has(name):
            return str(self._brahma.run(name, args) or "Done.")
        return str(self._desktop.run(name, args) or "Done.")

    def source(self, name: str) -> str:
        if name in self._native:
            return "ultron"
        if self._brahma is not None and self._brahma.has(name):
            return "brahma"
        return "desktop"

    def is_gated(self, name: str) -> bool:
        """True when a tool must stop for human approval before it runs."""
        return bool(self._brahma is not None and self._brahma.is_gated(name))

    def brahma_stats(self) -> Dict[str, Any]:
        if self._brahma is None:
            return {"available": 0, "unavailable": 0, "source_dir": None}
        return self._brahma.stats()


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
# Separate locks: get_registry() calls get_memory() while building, so a single
# non-reentrant lock would deadlock.
_registry: Optional[UltronToolRegistry] = None
_memory: Optional[UltronMemory] = None
_registry_lock = threading.Lock()
_memory_lock = threading.Lock()


def get_memory() -> UltronMemory:
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:
                _memory = UltronMemory()
    return _memory


def get_registry() -> UltronToolRegistry:
    """Return the shared registry, building it on first use."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                log.info("Building Ultron tool registry (desktop actions + native tools)")
                _registry = UltronToolRegistry.load(get_memory())
                log.info("Ultron tool registry ready: %d tools", len(_registry.names()))
    return _registry


def reset_registry() -> None:
    """Drop the cached registry - used by tests and after .env changes."""
    global _registry
    with _registry_lock:
        _registry = None
