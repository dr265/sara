"""Central risk-policy engine — the ONE place authorization is decided.

    LOW_RISK      runs automatically. open an app, click, scroll, type.
    NORMAL        runs automatically. edit/move files, normal navigation.
    SENSITIVE     runs automatically, but is logged prominently. send email.
    HIGH_RISK     needs one specific confirmation. delete a file, restart.
    IRREVERSIBLE  needs confirmation with an explicit impact statement.

WHY THIS FILE EXISTS

The gating decision used to be duplicated: ``DESTRUCTIVE_TOOLS`` and
``DESTRUCTIVE_ACTIONS`` appear in BOTH ``core/executor.py`` and
``app/api/sara_auto_agent.py``, and ``core/brahma_bridge.py`` adds a third
copy. Two copies drift apart, and an action that is destructive in one agent
is unguarded in the other. Worse, the old rule was two-valued: either a call
needed approval or it did not.

That produced the behaviour users actually experience: either an assistant
that interrupts constantly, or one that deletes a folder without asking.

THE RULE HERE

Confirmation is required for genuinely destructive, irreversible,
security-critical or outward-facing-hazardous operations — and ONLY for those.
Opening an application, clicking, scrolling, typing, navigating, switching
windows and reading the screen are LOW_RISK and must never prompt. A trusted,
paired machine doing ordinary desktop work is the normal case, not an event.

When a confirmation IS required, the prompt is built from the operation's real
effect — the concrete path, the count, the name — so the user approves
something they can picture rather than a generic "Are you sure?".

CONFIGURABILITY

``RISK_OVERRIDES`` in backend/.env reclassifies any tool/action as
``tool=action:TIER``. That is a flat string format because it has to be
writable from a .env file without a parser, and it is applied last so an
operator can widen OR narrow the policy without editing code.

This module is pure decision logic: no I/O, no framework imports, no globals
mutated at runtime. That is what makes it testable, and Phase 10 requires the
policy to be enforced centrally — so it must be importable by the executor,
the remote bridge and the tests without dragging any of them in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple


class Risk(IntEnum):
    """Ordered severity. Higher is worse; ``CONFIRM_THRESHOLD`` is the line."""

    LOW_RISK = 0
    NORMAL = 1
    SENSITIVE = 2
    HIGH_RISK = 3
    IRREVERSIBLE = 4


#: Anything at or above this must be explicitly confirmed by the user.
#: Everything below runs unattended — that is the point of the design.
CONFIRM_THRESHOLD = Risk.HIGH_RISK

TIER_NAMES = {
    Risk.LOW_RISK: "LOW_RISK",
    Risk.NORMAL: "NORMAL",
    Risk.SENSITIVE: "SENSITIVE",
    Risk.HIGH_RISK: "HIGH_RISK",
    Risk.IRREVERSIBLE: "IRREVERSIBLE",
}


# ---------------------------------------------------------------------------
# Classification tables
# ---------------------------------------------------------------------------
# Keyed by tool name. A tool absent from every table is LOW_RISK by default,
# because the overwhelming majority of automation is ordinary and an unknown
# name must not become an interrogation.
#
# Note what is deliberately NOT here: open_app, computer_control, scroll,
# click, type, hotkey, browser_navigation, screenshot, clipboard, window
# switching. Those are the operations that used to require approval and made
# the assistant unusable.

_TOOL_RISK: Dict[str, Risk] = {
    # -- plain desktop interaction: never prompts --------------------------
    "open_app": Risk.LOW_RISK,
    "computer_control": Risk.LOW_RISK,
    "computer_settings": Risk.LOW_RISK,
    "desktop_control": Risk.LOW_RISK,
    "browser_control": Risk.LOW_RISK,
    "youtube_video": Risk.LOW_RISK,
    "web_search": Risk.LOW_RISK,
    "screen_processor": Risk.LOW_RISK,
    "screen_capture": Risk.LOW_RISK,
    "screenshot": Risk.LOW_RISK,
    "clipboard": Risk.LOW_RISK,
    "datetime_info": Risk.LOW_RISK,
    "media_control": Risk.LOW_RISK,
    "ask_user": Risk.LOW_RISK,
    "display": Risk.LOW_RISK,
    "input": Risk.LOW_RISK,
    "app": Risk.LOW_RISK,
    "window": Risk.LOW_RISK,
    "browser": Risk.LOW_RISK,
    "web": Risk.LOW_RISK,
    "capabilities": Risk.LOW_RISK,
    "memory": Risk.LOW_RISK,
    "youtube_play": Risk.LOW_RISK,
    "code": Risk.LOW_RISK,
    "task": Risk.LOW_RISK,
    "service": Risk.LOW_RISK,
    "timezone": Risk.LOW_RISK,
    "audio_device": Risk.LOW_RISK,
    "microphone": Risk.LOW_RISK,
    "battery_status": Risk.LOW_RISK,
    "network_adapter": Risk.LOW_RISK,
    "system_metrics": Risk.LOW_RISK,
    "notify": Risk.LOW_RISK,
    "privacy": Risk.LOW_RISK,
    "power": Risk.LOW_RISK,
    "brightness": Risk.LOW_RISK,
    "volume": Risk.LOW_RISK,
    "wait": Risk.LOW_RISK,
    "recall": Risk.LOW_RISK,
    "say": Risk.LOW_RISK,
    "weather_report": Risk.LOW_RISK,
    "flight_finder": Risk.LOW_RISK,
    "reminder": Risk.LOW_RISK,
    "file_processor": Risk.LOW_RISK,
    "background_monitor": Risk.LOW_RISK,
    "proactive": Risk.LOW_RISK,

    # -- native Ultron tools -----------------------------------------------
    "ultron_agent_catalog": Risk.LOW_RISK,
    "ultron_answer": Risk.LOW_RISK,
    "ultron_recall": Risk.LOW_RISK,
    "ultron_summarize": Risk.LOW_RISK,
    "ultron_listen": Risk.LOW_RISK,
    "ultron_speak": Risk.LOW_RISK,
    "ultron_delegate": Risk.NORMAL,
    "ultron_remember": Risk.NORMAL,

    # -- files: editing is normal, destruction is not -----------------------
    "file_controller": Risk.NORMAL,

    # -- outward-facing / costly --------------------------------------------
    "send_message": Risk.SENSITIVE,
    "system_command": Risk.SENSITIVE,
    "dev_agent": Risk.NORMAL,
    "code_helper": Risk.NORMAL,
    "game_updater": Risk.NORMAL,
}

# Per-action overrides. Checked before the tool-level default, because "delete
# this file" and "read this file" are the same tool and wildly different risks.
_ACTION_RISK: Dict[str, Dict[str, Risk]] = {
    "file_controller": {
        "delete": Risk.HIGH_RISK,
        "delete_permanently": Risk.IRREVERSIBLE,
        "permanent_delete": Risk.IRREVERSIBLE,
        "purge": Risk.IRREVERSIBLE,
        "wipe": Risk.IRREVERSIBLE,
        "move": Risk.NORMAL,
        "rename": Risk.NORMAL,
        "copy": Risk.NORMAL,
        "write": Risk.NORMAL,
        "create_file": Risk.NORMAL,
        "create_folder": Risk.NORMAL,
        "read": Risk.LOW_RISK,
        "list": Risk.LOW_RISK,
        "find": Risk.LOW_RISK,
        "search": Risk.LOW_RISK,
        "organize": Risk.NORMAL,
        "organize_desktop": Risk.NORMAL,
    },
    "computer_settings": {
        # Ordinary display/typing settings stay silent...
        "volume_set": Risk.LOW_RISK,
        "volume_get": Risk.LOW_RISK,
        "brightness_set": Risk.LOW_RISK,
        "dark_mode": Risk.LOW_RISK,
        "fullscreen": Risk.LOW_RISK,
        "type_text": Risk.LOW_RISK,
        "close_app": Risk.LOW_RISK,
        "minimize": Risk.LOW_RISK,
        "maximize": Risk.LOW_RISK,
        "restore": Risk.LOW_RISK,
        "switch_window": Risk.LOW_RISK,
        "scroll": Risk.LOW_RISK,
        "tab_new": Risk.LOW_RISK,
        "tab_close": Risk.LOW_RISK,
        "tab_switch": Risk.LOW_RISK,
        "zoom": Risk.LOW_RISK,
        "reload": Risk.LOW_RISK,
        "back": Risk.LOW_RISK,
        "forward": Risk.LOW_RISK,
        "screenshot": Risk.LOW_RISK,
        # ...but these end a session or change the network the session runs on.
        "shutdown": Risk.HIGH_RISK,
        "restart": Risk.HIGH_RISK,
        "reboot": Risk.HIGH_RISK,
        "restart_computer": Risk.HIGH_RISK,
        "shutdown_computer": Risk.HIGH_RISK,
        "lock_screen": Risk.NORMAL,
        "signout": Risk.HIGH_RISK,
        "logoff": Risk.HIGH_RISK,
        "sleep": Risk.NORMAL,
        "toggle_wifi": Risk.HIGH_RISK,
    },
    "desktop_control": {
        "shutdown": Risk.HIGH_RISK,
        "restart": Risk.HIGH_RISK,
        "reboot": Risk.HIGH_RISK,
        "lock": Risk.NORMAL,
        "signout": Risk.HIGH_RISK,
        "logoff": Risk.HIGH_RISK,
        "sleep": Risk.NORMAL,
        "task": Risk.NORMAL,
    },
    "computer_control": {
        "hotkey": Risk.LOW_RISK,
        "press": Risk.LOW_RISK,
        "type": Risk.LOW_RISK,
        "smart_type": Risk.LOW_RISK,
        "click": Risk.LOW_RISK,
        "double_click": Risk.LOW_RISK,
        "right_click": Risk.LOW_RISK,
        "scroll": Risk.LOW_RISK,
        "drag": Risk.LOW_RISK,
        "move_mouse": Risk.LOW_RISK,
        "paste": Risk.LOW_RISK,
        "screenshot": Risk.LOW_RISK,
        "find_element": Risk.LOW_RISK,
    },
    "send_message": {
        "send": Risk.SENSITIVE,
        "send_email": Risk.SENSITIVE,
        "send_whatsapp": Risk.SENSITIVE,
        "send_sms": Risk.SENSITIVE,
        "post": Risk.HIGH_RISK,
        "publish": Risk.HIGH_RISK,
        "buy": Risk.HIGH_RISK,
        "purchase": Risk.HIGH_RISK,
        "pay": Risk.IRREVERSIBLE,
        "transfer": Risk.IRREVERSIBLE,
    },
    "system_command": {
        # A generic shell is inherently powerful; individual commands are
        # classified further in classify() via the DESTRUCTIVE_COMMAND_RE.
        "execute": Risk.SENSITIVE,
        "run": Risk.SENSITIVE,
        "powershell": Risk.SENSITIVE,
        "shell": Risk.SENSITIVE,
    },
}

#: Anything above this is irreversible enough to warrant a hard warning even
#: when reached through a generic shell tool, where per-action names are not
#: available. Matched against the command text only.
DESTRUCTIVE_COMMAND_RE = re.compile(
    r"(?ix)\b("
    r"format\b.*\b[a-z]:|"          # format a drive
    r"rd\s+/s|/s/q|del\s+/[fsq]|remove-item\b.*-recurse|"  # recursive delete
    r"rm\s+-rf|rmdir\s+/s|"         # unix recursive delete
    r"diskpart|clear-disk|"
    r"reg\s+delete|"
    r"netsh\s+advfirewall|set-executionpolicy|disable-windowsfeature|"
    r"bcdedit|cipher\s+/w|shred\s|"
    r"vssadmin\s+delete|wbadmin\s+delete|"
    r"takeown|icacls\b.*(/grant|/remove)"
    r")"
)

#: Commands that mutate security posture specifically.
SECURITY_COMMAND_RE = re.compile(
    r"(?i)\b(netsh\s+advfirewall|set-executionpolicy|disable-windowsfeature|"
    r"add-windowsfeature|net\s+user\s+\S+\s+/\s*add|"
    r"secedit|sc\s+config\s+\S+\s+start=|"
    r"reg\s+add\s+.*(firewall|security)|defender|"
    r"set-mppreference|disable-realtimeMonitoring)\b"
)

#: Commands that spend money.
FINANCIAL_COMMAND_RE = re.compile(
    r"(?i)\b(invoke-webrequest.*(payment|charge|transfer)|"
    r"stripe\s+charge|paypal\s+payment|"
    r"bitcoin|btc-send|"
    r"netsh\s+interface\s+.*transfer)\b"
)


@dataclass(frozen=True)
class Decision:
    """The verdict for one call, plus everything needed to explain it."""

    tool: str
    action: str
    risk: Risk
    requires_confirmation: bool
    reason: str = ""
    #: Human-readable impact statement, e.g.
    #: "permanently delete C:\\Users\\HP\\Downloads"
    impact: str = ""
    #: Overridden via RISK_OVERRIDES, for display/audit.
    source: str = "policy"

    @property
    def tier(self) -> str:
        return TIER_NAMES[self.risk]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "action": self.action,
            "risk": self.tier,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "impact": self.impact,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Operator overrides (RISK_OVERRIDES in backend/.env)
# ---------------------------------------------------------------------------
def _load_overrides() -> Dict[Tuple[str, str], Risk]:
    """Parse ``RISK_OVERRIDES`` — ``tool=action:TIER`` separated by commas.

    Both ``tool=action:TIER`` and ``tool=:TIER`` (any action of that tool) are
    accepted, because reclassifying 40 delete actions one at a time is exactly
    the maintenance burden that produced the drift this file removes.
    """
    raw = os.getenv("RISK_OVERRIDES", "").strip()
    out: Dict[Tuple[str, str], Risk] = {}
    if not raw:
        return out
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk or "=" not in chunk:
            continue
        tool_part, _, tier = chunk.rpartition(":")
        tier = tier.strip().upper().replace("-", "_").replace(" ", "_")
        tool, _, action = tool_part.partition("=")
        tool = tool.strip()
        action = (action or "").strip()
        try:
            level = Risk[tier]
        except KeyError:
            continue  # an unparseable override is ignored, never guessed at
        out[(tool, action)] = level
    return out


_OVERRIDES = _load_overrides()


def overrides() -> Dict[Tuple[str, str], Risk]:
    """The active operator overrides, for the diagnostics endpoint."""
    return dict(_OVERRIDES)


# ---------------------------------------------------------------------------
# Impact descriptions
# ---------------------------------------------------------------------------
_PATH_KEYS = ("path", "file_path", "folder", "directory", "src", "source", "target", "dst")
_COUNT_KEYS = ("count", "total", "files")


def _first_str(args: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _describe(tool: str, action: str, args: Dict[str, Any]) -> str:
    """Build the sentence a human reads before approving.

    The requirement is specificity: "This will permanently delete
    C:\\Projects\\old_data. Continue?" is approvable; "Allow?" is not.
    """
    path = _first_str(args, _PATH_KEYS)
    verb = action or tool

    if verb in ("delete", "delete_permanently", "permanent_delete", "purge", "wipe"):
        count = args.get("count") or args.get("total")
        how = "permanently delete" if verb != "delete" else "delete (to Recycle Bin)"
        if isinstance(count, int) and count > 1:
            return f"{how} {count} items at {path or 'the current folder'}"
        return f"{how} {path or 'the selected item'}"
    if verb in ("move", "rename"):
        dest = _first_str(args, ("target", "dst", "destination", "to"))
        return f"move/rename {path or 'the selected item'} to {dest or 'a new location'}"
    if verb in ("format", "clear-disk"):
        return f"ERASE THE DISK at {path or 'a drive'} - all data will be unrecoverable"
    if verb in ("shutdown", "restart", "reboot", "restart_computer", "shutdown_computer"):
        return f"{verb} this computer - unsaved work may be lost"
    if verb in ("signout", "logoff"):
        return "sign out of the current Windows account"
    if verb == "toggle_wifi":
        return "toggle Wi-Fi - the network this session runs over may drop"
    if verb in ("publish", "post"):
        return f"publish this publicly and irreversibly: {str(args.get('content') or '')[:120]}"
    if verb in ("send", "send_email", "send_whatsapp", "send_sms"):
        target = _first_str(args, ("to", "recipient", "address", "number"))
        return f"send a message to {target or 'the given recipient'}"
    if verb in ("pay", "purchase", "transfer"):
        return f"spend money: {str(args.get('amount') or args.get('value') or 'an unspecified amount')}"
    if tool == "system_command":
        return f"run this system command: {str(args.get('command') or args.get('args') or '')[:200]}"
    if path:
        return f"{verb} {path}"
    return f"run {tool}/{action}" if action else f"run {tool}"


# ---------------------------------------------------------------------------
# The decision function
# ---------------------------------------------------------------------------
def _classify_command_text(tool: str, args: Dict[str, Any]) -> Optional[Decision]:
    """Classify by inspecting a shell command, used only for system_command."""
    text = " ".join(
        str(args.get(key) or "") for key in ("command", "cmd", "args", "script")
    ).strip()
    if not text:
        return None
    if FINANCIAL_COMMAND_RE.search(text):
        return Decision(tool, "", Risk.IRREVERSIBLE, True,
                        "command moves money", _describe(tool, "transfer", args), "command_re")
    if SECURITY_COMMAND_RE.search(text):
        return Decision(tool, "", Risk.IRREVERSIBLE, True,
                        "command changes security controls",
                        f"change system security configuration: {text[:180]}", "command_re")
    if DESTRUCTIVE_COMMAND_RE.search(text):
        return Decision(tool, "", Risk.HIGH_RISK, True,
                        "command is destructive",
                        f"run a destructive command: {text[:180]}", "command_re")
    return None


def classify(tool: str, args: Optional[Dict[str, Any]] = None) -> Decision:
    """Decide whether one tool call needs confirmation. Never raises.

    Precedence, highest first:
      1. operator override (RISK_OVERRIDES)
      2. destructive / security / financial command text
      3. per-action table
      4. per-tool table
      5. LOW_RISK default
    """
    tool = str(tool or "").strip()
    args = dict(args or {})
    action = str(args.get("action") or "").strip()

    # 1. operator override, most specific first
    for key in ((tool, action), (tool, "")):
        if key in _OVERRIDES:
            level = _OVERRIDES[key]
            return Decision(tool, action, level, level >= CONFIRM_THRESHOLD,
                            "set by RISK_OVERRIDES", _describe(tool, action, args),
                            "override")

    # 2. shell command inspection
    if tool == "system_command":
        found = _classify_command_text(tool, args)
        if found is not None:
            return found

    # 3. per-action
    per_action = _ACTION_RISK.get(tool, {}).get(action)
    if per_action is not None:
        return Decision(tool, action, per_action, per_action >= CONFIRM_THRESHOLD,
                        f"{tool}.{action}", _describe(tool, action, args))

    # 4. per-tool
    tool_risk = _TOOL_RISK.get(tool)
    if tool_risk is None:
        # Unknown tool: LOW_RISK, because an unrecognised name is far more
        # often a new safe action than a new dangerous one. Callers that know
        # better can pin it via RISK_OVERRIDES.
        return Decision(tool, action, Risk.LOW_RISK, False,
                        "not in policy table; treated as routine")
    return Decision(tool, action, tool_risk, tool_risk >= CONFIRM_THRESHOLD,
                    f"{tool} default tier", _describe(tool, action, args))


def requires_confirmation(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    return classify(tool, args).requires_confirmation


def confirmation_prompt(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    """The exact sentence shown to the user.

    Requirement: it must name the real target. "Are you sure?" is not
    approvable because the user cannot tell what is about to happen.
    """
    d = classify(tool, args)
    if not d.requires_confirmation:
        return ""
    if d.risk >= Risk.IRREVERSIBLE:
        return f"This is IRREVERSIBLE and will {d.impact}. Continue?"
    return f"This will {d.impact}. Continue?"


def policy_snapshot() -> Dict[str, Any]:
    """Machine-readable policy for the diagnostics screen."""
    return {
        "confirm_threshold": TIER_NAMES[CONFIRM_THRESHOLD],
        "tiers": [TIER_NAMES[t] for t in Risk],
        "overrides": {f"{t}={a or '*'}": TIER_NAMES[r] for (t, a), r in _OVERRIDES.items()},
        "high_risk_actions": sorted(
            f"{tool}.{action}"
            for tool, actions in _ACTION_RISK.items()
            for action, risk in actions.items()
            if risk >= CONFIRM_THRESHOLD
        ),
        "gated_tools": sorted(
            tool for tool, risk in _TOOL_RISK.items() if risk >= CONFIRM_THRESHOLD
        ),
        "ungated_by_default": "any tool not listed is LOW_RISK and runs unattended",
    }


def describe_table() -> List[Dict[str, Any]]:
    """Flat view of the whole policy, for auditing."""
    rows: List[Dict[str, Any]] = []
    for tool, risk in sorted(_TOOL_RISK.items()):
        rows.append({"tool": tool, "action": "*", "risk": TIER_NAMES[risk]})
    for tool, actions in sorted(_ACTION_RISK.items()):
        for action, risk in sorted(actions.items()):
            rows.append({"tool": tool, "action": action, "risk": TIER_NAMES[risk]})
    return rows