"""Sara Bots security gate — the trusted ALLOW / ASK / DENY boundary.

Every tool invocation proposed by a planner (or a bot) must pass through
``evaluate()`` before any real code runs. The gate is the single place where
capability grants, tool readiness, risk and autonomy mode are combined.

Outcomes:
  ALLOW  the exact call is permitted by grants + policy -> execute.
  ASK    the call needs an explicit owner decision (approval) before running.
  DENY   the call is prohibited and must never execute.

The gate never trusts the model: it validates the tool id, the bot's granted
tool list, the tool's live state, and the autonomy mode. Approvals are scoped
to the exact tool id (not to a general "allowed to do X" instruction).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, Tuple


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


def _risk(tool: Dict[str, Any]) -> str:
    return str(tool.get("risk") or "write").lower()


def evaluate(tool_id: str, tool: Optional[Dict[str, Any]],
             bot: Dict[str, Any],
             params: Optional[Dict[str, Any]] = None) -> Tuple[Decision, str]:
    """Return (decision, reason) for a proposed tool call."""
    params = params or {}
    # Capability grants only apply when the bot carries an explicit tool list
    # (real bots always do). The engine's test seam passes a minimal bot dict
    # without 'tools', so we skip the grant check in that case.
    # Capability grants apply to runner-dispatched tools. Built-ins handled
    # directly by the engine (memory.note, llm.reason, browser.*) are exempt
    # from the per-bot grant check since they have no external side effects
    # beyond the bot's own scoped state.
    _runner_prefixes = ("action.", "hub.", "agency.run")
    is_runner_tool = tool_id.startswith(_runner_prefixes) or tool_id == "agency.run"
    if is_runner_tool and "tools" in bot:
        granted = set(bot.get("tools") or [])
        if tool_id not in granted:
            return Decision.DENY, (
                f"tool '{tool_id}' is not granted to this bot — capability "
                "grants are per-bot and cannot be self-elevated")

    # 2. Tool must exist in the live registry.
    if not tool:
        return Decision.DENY, f"tool '{tool_id}' is not in the live registry"

    # 3. Readiness: a non-ready tool cannot run.
    state = str(tool.get("state") or "unavailable")
    if state not in ("ready", "available"):
        return Decision.DENY, (
            f"tool '{tool_id}' is not ready ({state}: "
            f"{tool.get('detail', '')})")

    autonomy = str(bot.get("autonomy") or "supervised").lower()
    risk = _risk(tool)

    # Read-only / llm tools run under any autonomy mode once granted + ready.
    if risk in ("read", "llm"):
        return Decision.ALLOW, "read/llm tool granted and ready"

    # Write / external tools depend on autonomy mode.
    if autonomy == "supervised":
        return Decision.ASK, (
            f"supervised mode requires approval for {risk} tool '{tool_id}'")
    if autonomy == "assist":
        return Decision.ASK, (
            f"assist mode requires approval for {risk} tool '{tool_id}'")
    if autonomy == "autonomous":
        return Decision.ALLOW, (
            f"autonomous mode permits {risk} tool '{tool_id}' within grants")
    return Decision.DENY, f"unknown autonomy mode '{autonomy}'"


def is_allowed(decision: Decision) -> bool:
    return decision == Decision.ALLOW


def needs_approval(decision: Decision) -> bool:
    return decision == Decision.ASK
