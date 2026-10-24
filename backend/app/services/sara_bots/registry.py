"""Sara Bots registry — discovery + approval state, with zero code execution.

Three sources, one honest registry:

1. ``hub`` bots   — the real backend agents the Ultron fleet already probes
                    (fleet.AGENT_PROBES). Liveness is merged at request time
                    from those real HTTP probes; a curated invoke contract is
                    attached ONLY to agents whose real route accepts a simple
                    JSON/form payload (verified against the live routers).
2. ``agency``     — the 267 markdown specialists served by /api/agency/*
                    (system prompt + llm_router). One bot card; runs a single
                    chosen specialist through its REAL route.
3. ``local`` bots — Python actions discovered from ONE approved root
                    (default: <repo>/sara_desktop/actions). Discovery is
                    strictly ``ast``-based: files are parsed, never imported.
                    A discovered bot is DISABLED until explicitly approved;
                    approval is recorded in a state file owned by this module.

Path policy (fail closed):
  * the root must resolve inside the platform repository — this is the
    approved-scope boundary for automatic discovery;
  * ``..`` segments, symlinked components, non-``.py`` files, ``_``-prefixed
    files, oversized files and huge directories are rejected/skipped;
  * nothing outside the root is ever read.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("sara.bots.registry")

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ROOT = REPO_ROOT / "sara_desktop" / "actions"
STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "sara_bots_state.json"

#: Hard containment boundary for automatic discovery. A configured root must
#: resolve inside this tree or configuration fails closed.
APPROVED_SCOPE = REPO_ROOT

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_SAFE_TOOL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

#: The OpenMuse-powered primary agent also surfaces as a first-class fleet
#: card (kind=hub → visible under "All" and "Hub agents"). The card is a live
#: view of the REAL custom bot in the engine store — never a copy or a mock;
#: its state is derived from the bot record + the running engine.
SARAMUSE_ID = "saramuse"
SARAMUSE_NAME = "SaraMuse"
_MAX_FILE_BYTES = 512 * 1024          # a bigger "action" file is not scanned
_MAX_FILES = 500
#: Action parameter values that are inherently destructive — a run carrying
#: one of these requires explicit confirmation even on an approved bot.
RISKY_ACTION_VALUES = {
    "kill", "kill_process", "end_task", "shutdown", "restart", "sleep",
    "delete", "remove", "format", "send", "publish", "post", "tweet",
    "broadcast", "logout", "sign_out", "lock", "unlock",
}
RISKY_PARAM_KEYS = {"pid", "password", "token", "api_key", "command", "cmd", "shell"}

_CTX_KEYS = ("player", "speak", "response", "session_memory")


# ─────────────────────────────────────────────────────────────────────────────
# Curated hub bots (real agents, real probes, real invoke contracts)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FieldSpec:
    type: str                    # string | number | boolean | array
    description: str = ""
    required: bool = False
    default: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }


@dataclass
class BotSpec:
    id: str
    name: str
    kind: str                                  # hub | agency | local
    category: str
    description: str
    capabilities: List[str] = field(default_factory=list)
    adapter: Optional[str] = None              # runner adapter id (None = status/deep-link only)
    fields: Dict[str, FieldSpec] = field(default_factory=dict)
    route: Optional[str] = None                # real workspace route (deep link)
    probe: Optional[str] = None                # real health probe path (fleet)
    requires: List[str] = field(default_factory=list)
    cancellable: bool = False
    #: deterministic orb hue so every bot keeps a stable color identity
    hue: int = 200
    source_path: str = ""
    source_module: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "category": self.category, "description": self.description,
            "capabilities": list(self.capabilities),
            "adapter": self.adapter,
            "accepts": {k: v.to_dict() for k, v in self.fields.items()},
            "route": self.route, "probe": self.probe,
            "requires": list(self.requires),
            "cancellable": self.cancellable,
            "hue": self.hue,
            "source_path": self.source_path,
            "source_module": self.source_module,
        }


def _hue_for(name: str, offset: int = 0) -> int:
    h = offset
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % 360


#: The hub agents, mirrored from fleet.AGENT_PROBES at import time so probe
#: paths can never drift from the live control center. Invoke contracts below
#: were verified against the actual routers (trading_agent.py, scraping_agent.py,
#: agency_agents.py); agents whose real surface is a full workspace keep
#: adapter=None and deep-link there instead of pretending to be runnable here.
def _hub_bots() -> List[BotSpec]:
    from app.agents.sara_ultron_agent.core import fleet

    run_contracts: Dict[str, Dict[str, Any]] = {
        "trading-agent": {
            "adapter": "trading_demo",
            "description": (
                "Trading Agent (backtest engine) — replays generated demo "
                "candles through the real engine. Analysis only: no live "
                "orders, no LLM key required."
            ),
            "capabilities": ["Technical Analysis", "Signal Generation",
                             "Demo Backtest"],
            "fields": {
                "candles": FieldSpec("number", "Demo candles to replay (300-20000)",
                                     True, 2500),
                "threshold": FieldSpec("number", "Signal confidence threshold", False, 0.85),
                "eta": FieldSpec("number", "ETA smoothing factor", False, 0.15),
            },
        },
        "email-automation": {
        "adapter": "email_campaign",
        "description": (
            "Email Automation — creates a personalized email campaign scaffold "
            "(draft mode) from your instructions: name, subject, objective, tone, "
            "context, and sender provider. Returns the campaign id and a link to "
            "the workspace where you upload clients/attachments and start sending. "
            "Nothing is sent by this step; sending happens from the workspace."
        ),
        "capabilities": ["Campaign Creation", "Personalized Outreach",
                         "Attachments", "Dry-run first"],
        "fields": {
            "name": FieldSpec("string", "Campaign name", False, "Sara bot campaign"),
            "subject": FieldSpec("string", "Email subject line", False, ""),
            "objective": FieldSpec("string", "What the email should achieve", False, ""),
            "tone": FieldSpec("string", "professional | casual | urgent", False, "professional"),
            "context": FieldSpec("string", "Background info / product details", False, ""),
            "instructions": FieldSpec("string", "Agent instructions for drafting", False, ""),
            "provider": FieldSpec("string", "smtp | gmail", False, "smtp"),
        },
        "requires": ["Email sender connected (Sender panel on /hub/email-automation)"],
    },
    "ai-web-scraper": {
            "adapter": "scraper",
            "description": (
                "AI Web Scraper — fetches real URLs and extracts structured "
                "data with the configured LLM brain (needs an LLM key)."
            ),
            "capabilities": ["Data Extraction", "DOM Parsing", "Data Structuring"],
            "fields": {
                "urls": FieldSpec("array", "Target URL(s), http(s) only", True),
                "prompt": FieldSpec("string", "What to extract from the pages", True),
                "maxPages": FieldSpec("number", "Max pages in crawl mode", False, 3),
            },
            "requires": ["LLM key (OPENAI_API_KEY / LLM_API_KEY or local Ollama)"],
        },
    }
    bots: List[BotSpec] = []
    for entry in fleet.AGENT_PROBES:
        if entry.get("id") == "llm-gateway":
            continue        # infrastructure, not a bot
        contract = run_contracts.get(entry["id"], {})
        bots.append(BotSpec(
            id=entry["id"],
            name=entry["name"],
            kind="hub",
            category=entry.get("category") or "General",
            description=contract.get("description", "")
            or f"{entry['name']} — live backend agent (status from its real health probe).",
            capabilities=contract.get("capabilities", []),
            adapter=contract.get("adapter"),
            fields=contract.get("fields", {}),
            route=entry.get("route"),
            probe=entry.get("probe"),
            hue=_hue_for(entry["id"], 17),
        ))
    bots.append(BotSpec(
        id="agency-specialists",
        name="Agency Specialists",
        kind="agency",
        category="Business",
        description=(
            "267 markdown prompt agents (18 divisions) executed through the "
            "real /api/agency route and the shared LLM failover chain."
        ),
        capabilities=["Domain Expertise", "Research", "Drafting"],
        adapter="agency_run",
        route="/agency",
        probe="/api/agency/llm/health",
        fields={
            "agency_agent_id": FieldSpec(
                "string", "Specialist id from GET /api/agency/agents", True),
            "message": FieldSpec("string", "Your request for the specialist", True),
        },
        requires=["LLM key (at least one provider in backend/.env)"],
        hue=_hue_for("agency-specialists", 97),
    ))
    bots.append(BotSpec(
        id="sales-agent",
        name="SARA Sales Agent",
        kind="hub",
        category="Sales",
        description=(
            "Autonomous AI sales operation: real OpenStreetMap lead "
            "discovery, transparent qualification scoring, listing drafts "
            "and approval-gated email campaigns — controlled from the "
            "WhatsApp-style Sales Command Center chat."
        ),
        capabilities=["Lead Discovery", "Qualification", "Campaigns",
                      "Command Center"],
        adapter=None,
        route="/hub/sara-sales",
        probe=None,
        hue=_hue_for("sales-agent", 143),
    ))
    return bots


# ─────────────────────────────────────────────────────────────────────────────
# State store (root path + approvals + disables) — small, atomic, ours
# ─────────────────────────────────────────────────────────────────────────────

class BotStateStore:
    """JSON file holding the root path, approvals and disables.

    Owned entirely by this module; agents and discovery can only influence it
    through the explicit approval API, never by writing files.
    """

    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("sara-bots state unreadable — starting from defaults")
        return {"root": "", "approved": {}, "disabled": {}}

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    # -- root ---------------------------------------------------------------
    def root(self) -> Optional[Path]:
        raw = str(self.load().get("root") or "").strip()
        return Path(raw) if raw else None

    def set_root(self, candidate: str) -> Path:
        """Validate + canonicalize a discovery root. Raises ValueError when
        the path is outside the approved scope, missing, or not a directory."""
        if not candidate or not candidate.strip():
            raise ValueError("A root path is required.")
        raw = candidate.strip().strip('"').strip("'")
        if ".." in Path(raw).parts:
            # resolve() would fold it away; the explicit reject keeps intent clear
            raise ValueError("Relative parent segments ('..') are not allowed.")
        resolved = Path(os.path.realpath(raw))
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"'{raw}' is not an existing directory.")
        try:
            resolved.relative_to(APPROVED_SCOPE)
        except ValueError:
            raise ValueError(
                f"'{resolved}' is outside the approved discovery scope "
                f"({APPROVED_SCOPE}). Automatic discovery is restricted to the "
                "platform repository.")
        data = self.load()
        data["root"] = str(resolved)
        self.save(data)
        return resolved

    # -- approvals ----------------------------------------------------------
    def approve(self, bot_id: str) -> None:
        data = self.load()
        data.setdefault("approved", {})[bot_id] = {
            "approved_at": time.time(), "via": "sara-bots-api"}
        data.setdefault("disabled", {}).pop(bot_id, None)
        self.save(data)

    def revoke(self, bot_id: str) -> None:
        data = self.load()
        data.setdefault("approved", {}).pop(bot_id, None)
        self.save(data)

    def disable(self, bot_id: str) -> None:
        data = self.load()
        data.setdefault("disabled", {})[bot_id] = {
            "disabled_at": time.time(), "via": "sara-bots-api"}
        self.save(data)

    def enable(self, bot_id: str) -> None:
        data = self.load()
        data.setdefault("disabled", {}).pop(bot_id, None)
        self.save(data)

    def approved_ids(self) -> set[str]:
        return set(self.load().get("approved", {}))

    def disabled_ids(self) -> set[str]:
        return set(self.load().get("disabled", {}))


# ─────────────────────────────────────────────────────────────────────────────
# AST discovery — parse only, never import
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredAction:
    name: str
    description: str
    parameters: Dict[str, Dict[str, Any]]     # Gemini-style schema subset
    file: str
    requires_session: bool                    # handler needs live desktop ctx
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "parameters": self.parameters, "file": self.file,
            "requires_session": self.requires_session, "note": self.note,
        }


def _safe_within_root(path: Path, root: Path) -> bool:
    """True when path is a real child of root (no symlink escape)."""
    try:
        rp = Path(os.path.realpath(path))
        rp.relative_to(Path(os.path.realpath(root)))
        return True
    except (ValueError, OSError):
        return False


def _literal(node: ast.AST) -> Optional[Any]:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _extract_tool_dict(tree: ast.Module) -> Optional[Dict[Any, Any]]:
    """Pull the module-level ``TOOL = {...}`` dict, per-key.

    Real action files reference the handler by name (``"handler": open_app``),
    which is not a literal — so the dict is read key by key: literal values
    via ``literal_eval``, plain identifiers as their name string. Anything
    more complex (calls, attributes) is reported as ``<unparsable>`` for that
    key rather than failing the whole file.
    """
    for node in tree.body:
        targets: List[ast.AST] = []
        value: Optional[ast.AST] = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or not isinstance(value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "TOOL" for t in targets):
            continue
        extracted: Dict[Any, Any] = {}
        for key_node, val_node in zip(value.keys, value.values):
            key = _literal(key_node)
            if key is None:
                return None        # dynamic key: not a declarative TOOL dict
            literal = _literal(val_node)
            if literal is not None:
                extracted[key] = literal
            elif isinstance(val_node, ast.Name):
                extracted[key] = val_node.id    # handler function reference
            else:
                extracted[key] = "<unparsable>"
        return extracted
    return None


def _handler_needs_session(tree: ast.Module, handler_name: str) -> tuple[bool, str]:
    """Statically decide whether the handler can run headless.

    An action whose handler declares a context kwarg (player/speak/response/
    session_memory) WITHOUT a default cannot run in the headless worker; one
    with defaults (e.g. ``def x(parameters, player=None)``) can.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == handler_name:
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.arg in _CTX_KEYS and arg.annotation is not None:
                    pass  # annotation irrelevant
            params = {a.arg: a for a in list(node.args.args) + list(node.args.kwonlyargs)}
            defaults = list(node.args.defaults) + list(node.args.kw_defaults)
            # defaults align to the tail of the parameter list
            names = list(params)
            offset = len(names) - len(defaults)
            for idx, name in enumerate(names):
                if name in _CTX_KEYS:
                    has_default = idx >= offset and defaults[idx - offset] is not None
                    if not has_default and not node.args.kwarg:
                        return True, (
                            f"handler '{handler_name}' requires live desktop "
                            f"context '{name}' — not runnable headless")
            return False, ""
    return True, f"handler '{handler_name}' not found by static analysis"


def _gemini_schema_to_fields(parameters: Any) -> Dict[str, FieldSpec]:
    """Convert the discovered Gemini-style schema into UI field specs."""
    fields: Dict[str, FieldSpec] = {}
    props = parameters.get("properties") if isinstance(parameters, dict) else None
    required = set()
    if isinstance(parameters, dict) and isinstance(parameters.get("required"), list):
        required = {str(r) for r in parameters["required"]}
    if not isinstance(props, dict):
        return fields
    type_map = {"STRING": "string", "string": "string", "str": "string",
                "NUMBER": "number", "number": "number", "int": "number",
                "INTEGER": "number", "float": "number",
                "BOOLEAN": "boolean", "boolean": "boolean", "BOOL": "boolean",
                "ARRAY": "array", "array": "array", "list": "array"}
    for pname, pdef in props.items():
        pdef = pdef if isinstance(pdef, dict) else {}
        ptype = type_map.get(str(pdef.get("type", "string")).upper(),
                             type_map.get(str(pdef.get("type", "string")), "string"))
        fields[str(pname)] = FieldSpec(
            type=ptype,
            description=str(pdef.get("description", ""))[:300],
            required=str(pname) in required,
        )
    return fields


def discover_local_actions(root: Path) -> tuple[List[DiscoveredAction], List[Dict[str, str]]]:
    """Static discovery of action files under ``root``. NEVER imports code.

    Returns (discovered, problems). Problems are per-file reasons a candidate
    was skipped — surfaced to the UI so a silent miss is debuggable.
    """
    root = Path(os.path.realpath(root))
    discovered: List[DiscoveredAction] = []
    problems: List[Dict[str, str]] = []
    if not root.is_dir():
        return [], [{"file": str(root), "reason": "root is not a directory"}]

    files = sorted(root.glob("*.py"))
    if len(files) > _MAX_FILES:
        files = files[:_MAX_FILES]
        problems.append({"file": "<root>", "reason": f"scan capped at {_MAX_FILES} files"})

    for path in files:
        rel = path.name
        if rel.startswith("_"):
            continue        # private/helper modules are invisible by contract
        if not _safe_within_root(path, root):
            problems.append({"file": rel, "reason": "skipped: resolves outside root"})
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                problems.append({"file": rel, "reason": "skipped: file too large"})
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            problems.append({"file": rel, "reason": f"unparsable: {exc}"})
            continue

        tool = _extract_tool_dict(tree)
        if not isinstance(tool, dict):
            continue        # not an action module — a helper; not a problem

        name = tool.get("name")
        description = tool.get("description")
        handler = tool.get("handler")
        if not isinstance(name, str) or not _SAFE_TOOL_RE.match(name):
            problems.append({"file": rel, "reason": "TOOL['name'] invalid or unsafe"})
            continue
        if not isinstance(description, str) or not description.strip():
            problems.append({"file": rel, "reason": "TOOL['description'] missing"})
            continue
        if not isinstance(handler, str):
            problems.append({"file": rel, "reason": "TOOL['handler'] must name a module function"})
            continue

        needs_session, note = _handler_needs_session(tree, handler)
        discovered.append(DiscoveredAction(
            name=name,
            description=description.strip()[:600],
            parameters=tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {},
            file=str(path),
            requires_session=needs_session,
            note=note,
        ))
    return discovered, problems


def discovered_to_bot(action: DiscoveredAction) -> BotSpec:
    return BotSpec(
        id=f"action.{action.name}",
        name=action.name.replace("_", " ").title(),
        kind="local",
        category="Local Machine",
        description=action.description,
        capabilities=[action.name],
        adapter=None if action.requires_session else "local_action",
        fields=_gemini_schema_to_fields(action.parameters),
        route=None,
        probe=None,
        requires=(["Live desktop session (sara_desktop running)"]
                  if action.requires_session else []),
        cancellable=not action.requires_session,
        hue=_hue_for(action.name, 211),
        source_path=action.file,
        source_module=action.name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The registry façade
# ─────────────────────────────────────────────────────────────────────────────

class SaraBotsRegistry:
    """Thread-safe union of hub bots, the agency bot and approved local bots."""

    def __init__(self, store: Optional[BotStateStore] = None):
        self.store = store or BotStateStore()
        self._lock = threading.RLock()
        self._hub: List[BotSpec] = _hub_bots()
        self._discovered: Dict[str, DiscoveredAction] = {}
        self._problems: List[Dict[str, str]] = []
        self._scanned_root: Optional[Path] = None
        self._scanned_at: float = 0.0

    # -- discovery ----------------------------------------------------------
    def rescan(self, root: Optional[Path] = None) -> Dict[str, Any]:
        """Re-run static discovery. Executes nothing; approvals survive.

        The target is canonicalized and must resolve inside APPROVED_SCOPE —
        the same containment as ``set_root`` — so a hand-edited state file
        can never widen discovery outside the repository."""
        target = root or self.store.root() or DEFAULT_ROOT
        resolved = Path(os.path.realpath(target))
        if not resolved.exists() or not resolved.is_dir():
            return {"ok": False, "error": f"root '{target}' does not exist"}
        try:
            resolved.relative_to(APPROVED_SCOPE)
        except ValueError:
            return {"ok": False,
                    "error": f"root '{resolved}' is outside the approved "
                             f"discovery scope ({APPROVED_SCOPE})"}
        actions, problems = discover_local_actions(resolved)
        with self._lock:
            self._discovered = {a.name: a for a in actions}
            self._problems = problems
            self._scanned_root = resolved
            self._scanned_at = time.time()
        return {
            "ok": True,
            "root": str(self._scanned_root),
            "discovered": len(self._discovered),
            "problems": problems,
        }

    def ensure_scanned(self) -> None:
        with self._lock:
            fresh = (time.time() - self._scanned_at) < 300 and self._scanned_root is not None
        if not fresh:
            target = self.store.root() or DEFAULT_ROOT
            if target.exists():
                self.rescan(target)

    # -- lookups ------------------------------------------------------------
    def hub_bots(self) -> List[BotSpec]:
        return list(self._hub)

    # -- SaraMuse: the OpenMuse-powered primary agent's fleet card ----------
    def _saramuse_pair(self) -> Optional[tuple[BotSpec, Dict[str, Any]]]:
        """Resolve the owner's live 'SaraMuse' custom bot, or None."""
        from app.services.sara_bots.custom import LOCAL_OWNER, get_custom_store
        try:
            bots = get_custom_store().list(owner=LOCAL_OWNER)
        except Exception:  # noqa: BLE001 — a broken store must not break the fleet
            log.exception("sara-bots: SaraMuse lookup failed")
            return None
        live = next(
            (b for b in bots
             if str(b.get("name", "")).strip().lower() == SARAMUSE_NAME.lower()),
            None)
        if not live:
            return None
        spec = BotSpec(
            id=SARAMUSE_ID,
            name=SARAMUSE_NAME,
            kind="hub",
            category="Autonomous",
            description=str(live.get("description") or (
                "OpenMuse-powered autonomous primary agent — browser, terminal, "
                "files, goals, scheduling, memory and notifications.")),
            capabilities=["Autonomous Planning", "Browser", "Terminal & Files",
                          "Goals & Schedules", "Scoped Memory"],
            adapter=None,          # the real engine runs from its workspace
            route="/hub/sara-muse",
            probe=None,
            hue=int(live.get("hue") or 260),
        )
        return spec, live

    def saramuse_bot(self) -> Optional[BotSpec]:
        pair = self._saramuse_pair()
        return pair[0] if pair else None

    def local_bot(self, bot_id: str) -> Optional[BotSpec]:
        if not bot_id.startswith("action."):
            return None
        name = bot_id[len("action."):]
        action = self._discovered.get(name)
        return discovered_to_bot(action) if action else None

    def bot(self, bot_id: str) -> Optional[BotSpec]:
        if bot_id == SARAMUSE_ID:
            return self.saramuse_bot()
        for spec in self._hub:
            if spec.id == bot_id:
                return spec
        return self.local_bot(bot_id)

    def discovered_actions(self) -> List[DiscoveredAction]:
        return list(self._discovered.values())

    def scan_info(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "root": str(self._scanned_root) if self._scanned_root else None,
                "configured_root": str(self.store.root() or ""),
                "default_root": str(DEFAULT_ROOT),
                "approved_scope": str(APPROVED_SCOPE),
                "scanned_at": self._scanned_at,
                "discovered": len(self._discovered),
                "problems": list(self._problems),
            }

    # -- status assembly (honest states only) -------------------------------
    def bot_states(self, fleet_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Merge approvals + real probe results into per-bot state blocks.

        Scans local actions FIRST: on a fresh process the very first caller
        must never see an empty discovered-set, or approved actions would be
        misreported as ``needs_approval`` (and planning with them would be
        refused with a misleading "not in the allowlist" error).
        """
        self.ensure_scanned()
        approved = self.store.approved_ids()
        disabled = self.store.disabled_ids()
        probes = {a["id"]: a for a in (fleet_snapshot or {}).get("agents", [])}

        out: Dict[str, Dict[str, Any]] = {}
        for spec in self._hub:
            state, detail, probe = "unavailable", "", probes.get(spec.id)
            if spec.id in probes:
                status = probe.get("status")
                if status == "online":
                    state, detail = "available", probe.get("detail") or "health probe answered"
                elif status == "degraded":
                    state, detail = "needs_configuration", probe.get("detail") or "degraded"
                elif status == "unreachable":
                    state, detail = "unavailable", probe.get("detail") or "probe failed"
                else:
                    state, detail = "unavailable", "no health route exposed"
            else:
                detail = "not probed yet"
            if spec.id in disabled:
                state, detail = "disabled", "disabled by the owner"
            invocable = bool(spec.adapter) and state == "available"
            out[spec.id] = {
                "state": state, "detail": detail,
                "probe": ({"status": probe.get("status"),
                           "http_status": probe.get("http_status"),
                           "latency_ms": probe.get("latency_ms"),
                           "checked_at": probe.get("checked_at")}
                          if probe else None),
                "approved": spec.id not in disabled,
                "invocable": invocable,
                "why_not": "" if invocable else (
                    "disabled by the owner" if state == "disabled" else
                    ("needs configuration" if state == "needs_configuration" else
                     ("agent unreachable" if state == "unavailable" else
                      "it runs in its own workspace"))),
            }

        # agency bot — liveness = real LLM provider availability
        agency = probes.get("agency-specialists")
        agency_state = "unavailable"
        agency_detail = "LLM health not probed yet"
        if agency and agency.get("status") == "online":
            agency_state = "available"
            agency_detail = agency.get("detail") or "LLM failover chain online"
        elif agency:
            agency_state = "needs_configuration"
            agency_detail = "no LLM provider available — add a key to backend/.env"
        if "agency-specialists" in disabled:
            agency_state, agency_detail = "disabled", "disabled by the owner"
        out["agency-specialists"] = {
            "state": agency_state, "detail": agency_detail,
            "probe": ({"status": agency.get("status"),
                       "http_status": agency.get("http_status"),
                       "latency_ms": agency.get("latency_ms"),
                       "checked_at": agency.get("checked_at")}
                      if agency else None),
            "approved": "agency-specialists" not in disabled,
            "invocable": agency_state == "available",
            "why_not": "" if agency_state == "available" else agency_detail,
        }

        # SaraMuse — state from the live bot record + the real engine, no probe
        pair = self._saramuse_pair()
        if pair:
            spec, live = pair
            if str(live.get("status") or "paused") != "active":
                state, detail = "needs_configuration", (
                    "bot paused — resume it from its workspace")
            else:
                try:
                    from app.services.sara_bots.engine import get_engine
                    eng = get_engine().status()
                except Exception:  # noqa: BLE001 — registry must survive engine errors
                    eng = {}
                if not eng.get("engine_running"):
                    state, detail = "unavailable", (
                        "engine worker not running — start the backend")
                elif not eng.get("llm_ready"):
                    state, detail = "needs_configuration", (
                        "no LLM provider available — add a key to backend/.env")
                else:
                    state, detail = "available", (
                        f"engine live · {eng.get('active', 0)} active task(s) · "
                        f"{len(live.get('tools') or [])} granted powers")
            out[SARAMUSE_ID] = {
                "state": state, "detail": detail, "probe": None,
                "approved": True,
                "invocable": False,
                "why_not": ("it runs in its own workspace"
                            if state == "available" else detail),
            }

        # Sales Agent — always available: its workspace is this platform
        out["sales-agent"] = {
            "state": "available",
            "detail": "command center ready — open its workspace",
            "probe": None,
            "approved": True,
            "invocable": False,
            "why_not": "it runs in its own workspace",
        }

        # local bots — approval-gated, honestly unproven until first real run
        for name in self._discovered:
            bot_id = f"action.{name}"
            if bot_id in disabled:
                state, detail = "disabled", "disabled by the owner"
            elif bot_id not in approved:
                state, detail = "awaiting_approval", (
                    "discovered statically; NOT executed until you approve it")
            elif self._discovered[name].requires_session:
                state, detail = "needs_configuration", self._discovered[name].note
            else:
                state, detail = "ready", "approved — awaiting first real execution"
            out[bot_id] = {
                "state": state, "detail": detail, "probe": None,
                "approved": bot_id in approved and bot_id not in disabled,
                "invocable": state in ("ready",),
                "why_not": "" if state == "ready" else detail,
            }
        return out


#: Process-wide singleton (mirrors the platform's other registries).
_registry: Optional[SaraBotsRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> SaraBotsRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = SaraBotsRegistry()
        return _registry
