"""Brahma-Echo action bridge.

Brahma-Echo ships 47 modules under ``actions/`` but only two of them declare the
``TOOL`` dict that SARA's ``core.action_loader`` discovers. The other 45 are
plain helper modules that its own ``main.py`` wires up directly, so simply
pointing the existing loader at that folder would load almost nothing.

This module bridges that gap. It imports each Brahma-Echo module, finds the
callable that takes ``parameters`` (the handler shape SARA already speaks) and
exposes it through the same record interface ``ActionRegistry`` uses, so
``UltronToolRegistry`` can merge it like any other source.

Two problems had to be solved to make that safe, and both are solved by
rewriting the *imports* rather than editing the upstream files:

**Module-name collisions.** Every handler does ``from core.x import y`` and
``from actions.x import y``. SARA's own desktop actions are already registered
in ``sys.modules`` as ``actions.<stem>`` by ``discover_actions``, so a plain
``sys.path`` insertion would silently hand Brahma-Echo *SARA's* modules and
produce baffling ``ImportError``s for functions that genuinely only exist
upstream. Every upstream top-level module is therefore re-exported under a
``brahma_`` prefix and the import statements are rewritten to match.

**Plaintext credentials and hardcoded model pools.** Upstream reads
``config/api_keys.json`` off disk and calls OpenRouter / ``google.generativeai``
directly, with model names baked into the source. ``brahma_config`` and
``brahma_llm_client`` are replaced with adapters over SARA's own
``app.core.llm_router``, so secrets come from the environment and LLM traffic
inherits SARA's failover chain. No upstream file is modified.

Importing a module can have side effects, so every import is attempted in a
guarded block and a failure downgrades that one tool to "unavailable with a
reason" rather than taking the registry down.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Set

log = logging.getLogger("sara.ultron.brahma")

#: Where the upstream checkout lives. Overridable so a deployment can point at
#: its own copy; the D: default is the location this was integrated from.
DEFAULT_SOURCE_DIR = Path(
    os.environ.get("BRAHMA_ECHO_DIR", r"D:\Brahma-Echo-main\Brahma-Echo-main")
)

#: Upstream top-level names -> the SARA-side name they are rewritten to.
_REWRITES = {
    "core": "brahma_core",
    "config": "brahma_config",
    "actions": "brahma_actions",
    "llm_client": "brahma_llm_client",
    "or_client": "brahma_or_client",
    "memory": "brahma_memory",
    "ui": "brahma_ui",
    "plugin_manager": "brahma_plugin_manager",
    "workspace_store": "brahma_workspace_store",
}

#: Upstream names that must never be imported. They are interactive CLIs or
#: pull in an unmaintained YouTube scraper, and importing them would block the
#: backend on stdin.
_BLOCKED = {"test_action", "y"}

#: Explicit entry-point overrides, consulted before the heuristic.
#:
#: The heuristic (first ``parameters``-first function, preferring ``run``)
#: picks a *helper* for a few modules whose real dispatcher upstream wires
#: from ``main.py``/``agent/executor.py``. Calling the helper instead of the
#: dispatcher silently degraded those tools (e.g. ``brahma_youtube_video``
#: only ever played, never summarized). Keys are module stems, values the
#: upstream-wired function name; an unknown name falls back to the
#: heuristic so a future upstream rename cannot break loading.
_ENTRY_OVERRIDES: Dict[str, str] = {
    # Stem              -> upstream-wired entry point
    "brahma_connect":      "connect_execute",            # main.py wires connect_*
    "claude_code_bridge":  "run_developer_mode_request",  # agent/executor.py
    "pdf_tools":           "create_pdf",                 # main.py:54
    "youtube_video":       "youtube_video",              # main.py:48 dispatcher
    "clipboard_processor": "run",                        # main.py:3783
}

#: Modules whose honest action vocabulary cannot be recovered from the
#: AST of their dispatcher (indirect table lookups, parameter-name
#: comparisons). The values are upstream-declared: computer_settings
#: spells its full list in its TOOL declaration (ACTION_MAP keys +
#: _VALUE_ACTIONS); open_app genuinely dispatches on the platform name.
#: When set, this replaces the heuristic output for that module.
_VOCABULARY_OVERRIDES: Dict[str, List[str]] = {
    "computer_settings": ["volume_up", "volume_down", "volume_set", "mute", "unmute", "toggle_mute", "brightness_up", "brightness_down", "sleep_display", "screen_off", "pause_video", "play_pause", "close_app", "close_window", "full_screen", "fullscreen", "minimize", "maximize", "snap_left", "snap_right", "switch_window", "show_desktop", "task_manager", "focus_search", "refresh_page", "reload", "close_tab", "new_tab", "next_tab", "prev_tab", "go_back", "go_forward", "zoom_in", "zoom_out", "zoom_reset", "find_on_page", "scroll_up", "scroll_down", "scroll_top", "scroll_bottom", "page_up", "page_down", "copy", "paste", "cut", "undo", "redo", "select_all", "save", "enter", "escape", "press_key", "type_text", "screenshot", "lock_screen", "open_settings", "file_explorer", "open_run", "dark_mode", "toggle_wifi", "restart", "shutdown"],
    "open_app": ["Windows", "Darwin", "Linux"],
}

#: Context keys SARA's action loader passes to a handler. Mirrors
#: ``core.action_loader._CTX_KEYS`` so handlers written for either runtime work.
CTX_KEYS = ("player", "speak", "response", "session_memory")

#: Every Python builtin name, computed once from the ``builtins`` module.
#:
#: Deliberately *not* derived from the running module's ``__builtins__``: CPython
#: binds that to the builtins module in imported modules but to the module's
#: ``__dict__`` in ``__main__``, so the obvious ``set(dir(__builtins__))`` returns
#: dict *methods* (``clear``, ``keys``, ``update``) when it is the dict form and
#: silently filters nothing. That turned ``desktop.py``'s ``safe_builtins``
#: sandbox allowlist into 22 advertised "actions" for whoever imported the
#: bridge as a library.
BUILTIN_NAMES: Set[str] = frozenset(dir(builtins))  # type: ignore[assignment]

#: Value shapes that indicate a dict literal is a dispatch table rather than
#: arbitrary data. A table of actions maps each name to something callable.
_CALLABLE_LIKE = (ast.Name, ast.Attribute, ast.Lambda, ast.Call)


# --------------------------------------------------------------------------- #
# Shim modules
# --------------------------------------------------------------------------- #
def _synthesise_package(name: str, path: Path) -> ModuleType:
    """Register a package without running its ``__init__``.

    Used when upstream's package initialiser fails or does not exist: the
    submodules still have to be importable, and running a failing initialiser
    would take the whole bridge down for no gain.
    """
    pkg = ModuleType(name)
    pkg.__path__ = [str(path)]  # type: ignore[attr-defined]
    pkg.__package__ = name
    pkg.__file__ = str(path / "__init__.py")
    sys.modules[name] = pkg
    return pkg


def _install_config_shim() -> ModuleType:
    """Env-backed stand-in for Brahma-Echo's ``config``.

    Upstream's ``config.get_config()`` opens ``config/api_keys.json``, a
    plaintext credential file that does not even ship with the project. This
    replacement builds the same dict shape from the environment, so nothing is
    read off disk and a missing key degrades to "not configured" instead of
    raising at import time.
    """
    mod = ModuleType("brahma_config")
    mod.__file__ = "<sara shim: brahma_config>"
    mod.__all__ = ["get_config", "get_os", "is_windows", "is_mac", "is_linux"]

    def get_config() -> Dict[str, Any]:
        # Non-secret platform setting, plus whatever the operator exported.
        cfg: Dict[str, Any] = {"os_system": os.environ.get("BRAHMA_OS", "windows")}
        for key, value in os.environ.items():
            if key.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
                cfg[key.lower()] = value
        return cfg

    def get_os() -> str:
        return str(get_config().get("os_system", "windows")).lower()

    mod.get_config = get_config  # type: ignore[attr-defined]
    mod.get_os = get_os  # type: ignore[attr-defined]
    mod.is_windows = lambda: get_os() == "windows"  # type: ignore[attr-defined]
    mod.is_mac = lambda: get_os() == "mac"  # type: ignore[attr-defined]
    mod.is_linux = lambda: get_os() == "linux"  # type: ignore[attr-defined]
    return mod


class _UnifiedClientShim:
    """Drop-in replacement for Brahma-Echo's ``UnifiedAIClient``.

    Nine action modules do ``from llm_client import client`` and then call
    ``client.chat``/``chat_json``/``vision``/``multi_turn``. Reimplementing that
    surface on top of ``app.core.llm_router`` means the bridged tools use
    SARA's configured providers and failover chain instead of the model list
    hardcoded in upstream's ``llm_client.py``.

    Arguments upstream passes that SARA does not support (``model=``) are
    accepted and ignored rather than raising, because the router owns model
    selection; passing a specific model would defeat failover.
    """

    def __init__(self) -> None:
        self._router = None
        self._provider = "SARA"

    def _llm(self):
        if self._router is None:
            from app.core.llm_router import llm_chat_sync

            self._router = llm_chat_sync
        return self._router

    # -- SARA plumbing --------------------------------------------------
    def reload_settings(self) -> None:  # noqa: D401 - upstream calls this
        """Kept for API parity. SARA reloads its provider chain itself."""
        from app.core.llm_router import reload_chain

        reload_chain()

    def _messages(self, prompt: str, system: str, history) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for turn in history or []:
            if isinstance(turn, dict) and "content" in turn:
                messages.append({"role": str(turn.get("role", "user")),
                                 "content": str(turn["content"])})
        messages.append({"role": "user", "content": str(prompt)})
        return messages

    # -- upstream API ---------------------------------------------------
    def chat(self, prompt: str, system: str = "You are a helpful assistant.",
             history=None, model: Optional[str] = None, max_tokens: int = 4096,
             temperature: float = 0.7) -> str:
        messages = self._messages(prompt, system, history)
        return self._llm()(messages, max_tokens=max_tokens, temperature=temperature)

    def multi_turn(self, messages, model: Optional[str] = None,
                   max_tokens: int = 4096, temperature: float = 0.7) -> str:
        if isinstance(messages, str):
            messages = self._messages(messages, "", None)
        return self._llm()(list(messages), max_tokens=max_tokens,
                          temperature=temperature)

    def chat_json(self, prompt: str, system: str = "Return ONLY valid JSON.",
                  model: Optional[str] = None, max_tokens: int = 4096) -> Dict[str, Any]:
        """JSON is requested in the prompt as well as parsed, because a
        failover chain can land on a provider that ignores response_format."""
        raw = self.chat(
            f"{prompt}\n\nRespond with a single valid JSON value and nothing else.",
            system=system, max_tokens=max_tokens,
        )
        return _parse_json_loose(raw)

    def vision(self, prompt: str, image_b64: str, mime: str = "image/png",
               system: str = "Analyze the image.", model: Optional[str] = None,
               max_tokens: int = 1024) -> str:
        payload = f"data:{mime};base64,{image_b64}"
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": str(prompt)},
                {"type": "image_url", "image_url": {"url": payload}},
            ],
        })
        return self._llm()(messages, max_tokens=max_tokens)

    def vision_from_file(self, prompt: str, image_path: str,
                         system: str = "Analyze the image.",
                         model: Optional[str] = None, max_tokens: int = 1024) -> str:
        import base64

        data = Path(image_path).read_bytes()
        return self.vision(prompt, base64.b64encode(data).decode("ascii"),
                           system=system, max_tokens=max_tokens)

    def Dispatch(self, prompt: str, **kwargs: Any) -> str:  # noqa: N802
        """Upstream's router entry point; SARA's router already dispatches."""
        return self.chat(prompt, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<brahma_llm_client.client backed by SARA llm_router>"


def _parse_json_loose(raw: str) -> Dict[str, Any]:
    """Pull a JSON value out of a reply that may be fenced or chatty."""
    import re as _re

    text = (raw or "").strip()
    fenced = _re.search(r"```(?:json)?\s*(.+?)```", text, _re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0),
                    default=-1)
        if start < 0:
            return {}
        closer = "}" if text[start] == "{" else "]"
        end = text.rfind(closer)
        if end <= start:
            return {}
        try:
            value = json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {"result": value}


def _install_llm_shim(module_name: str) -> ModuleType:
    """Publish a ``brahma_llm_client``/``brahma_or_client`` adapter module."""
    mod = ModuleType(module_name)
    mod.__file__ = f"<sara shim: {module_name}>"
    mod.client = _UnifiedClientShim()  # type: ignore[attr-defined]

    client = mod.client  # type: ignore[attr-defined]
    for name in ("chat", "chat_json", "multi_turn", "vision", "vision_from_file"):
        setattr(mod, name, getattr(client, name))
    mod.available = lambda: True  # type: ignore[attr-defined]
    mod.get_client = lambda: client  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------- #
# Source rewriting
# --------------------------------------------------------------------------- #
_FROM_RE = re.compile(
    r"^(\s*)from\s+(" + "|".join(_REWRITES) + r")(\.\w+)*\s+import\s+",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(
    r"^(\s*)import\s+(" + "|".join(_REWRITES) + r")\s*$", re.MULTILINE
)


def rewrite_source(source: str, module_alias: str = "") -> str:
    """Point every upstream top-level import at its ``brahma_``-prefixed twin.

    ``from core.echo import x``  -> ``from brahma_core.echo import x``
    ``import config``            -> ``import brahma_config as config``

    Only whole-word module names at the head of an import statement are
    touched, so a local variable called ``config`` is left alone.
    """
    def _from_sub(match: re.Match) -> str:
        head, module, rest = match.group(1), match.group(2), match.group(3) or ""
        return f"{head}from {_REWRITES[module]}{rest} import "

    def _import_sub(match: re.Match) -> str:
        head, module = match.group(1), match.group(2)
        return f"{head}import {_REWRITES[module]} as {module}"

    rewritten = _FROM_RE.sub(_from_sub, source)
    return _IMPORT_RE.sub(_import_sub, rewritten)


#: Namespace prefixes this finder owns.
_OWNED = ("brahma_actions.", "brahma_core.")


class _BrahmaFinder:
    """Meta-path finder that loads every ``brahma_*`` module through the rewriter.

    Loading modules by hand is not enough: ``claude_code_bridge`` imports
    ``brahma_dev_agent``, and if that submodule is not already cached, the
    import system reads it straight off disk and its ``from core.x import y``
    line is executed unrewritten and blows up. Registering the namespace here
    means *whoever* triggers the import gets the rewritten module, so load
    order stops mattering.
    """

    #: Guards against a genuinely runaway import graph. Normal modules nest
    #: many levels deep, so this only trips on real recursion.
    MAX_DEPTH = 60

    def __init__(self) -> None:
        self._local = threading.local()

    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        if not fullname.startswith(_OWNED):
            return None
        pkg, _, leaf = fullname.rpartition(".")
        parent = sys.modules.get(pkg)
        search = getattr(parent, "__path__", None)
        if not search:
            return None
        for base in search:
            candidate = Path(base) / f"{leaf}.py"
            if candidate.is_file():
                return importlib.util.spec_from_file_location(
                    fullname, candidate, loader=self
                )
        return None

    def create_module(self, spec: Any) -> Optional[ModuleType]:
        return None  # default module creation

    def exec_module(self, module: ModuleType) -> None:
        depth = self._depth()
        if depth > self.MAX_DEPTH:
            raise ImportError(
                f"import of {module.__name__} nested deeper than "
                f"{self.MAX_DEPTH} levels; aborting a runaway graph"
            )
        path = Path(str(getattr(module, "__file__", "")))
        raw = path.read_text(encoding="utf-8", errors="replace")
        source = rewrite_source(raw, path.stem)
        self._local.depth = depth + 1
        try:
            exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
        except SyntaxError:
            # A file the rewriter cannot parse still deserves a chance.
            exec(compile(raw, str(path), "exec"), module.__dict__)  # noqa: S102
        finally:
            self._local.depth = depth


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class BrahmaRecord:
    """One bridged Brahma-Echo entry point, shaped like an ``ActionRecord``."""

    name: str
    file: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable] = None
    valid: bool = False
    error: str = ""
    gated: bool = False

    @property
    def source(self) -> str:
        return "brahma"


#: Tools that change the machine, other people's data, or the account itself.
#: Answered "yes, include them" - so they ship, but every call stops at the
#: approval gate first and cannot run unattended.
RISKY_TOOLS: Set[str] = {
    # Names verified against the loaded records (load-parity probe).
    # Earlier revisions listed name-drift entries that matched no tool,
    # silently leaving five risky actions ungated.
    "brahma_mobile_autopilot",
    "brahma_upload_video",
    "brahma_system_manager",
    "brahma_unlock_device",
    "brahma_desktop_organizer_mcp",
    "brahma_auto_heal_engine",
    "brahma_google_workspace_mcp",
    "brahma_brahma_connect",
    "brahma_file_processor",
    "brahma_claude_code_bridge",
    "brahma_brahma_dev_agent",
    "brahma_dev_agent",
}

#: Tools whose actions drive a real, persistent browser session through an
#: external MCP process (``npx @playwright/mcp``). They are NOT gated — a user
#: asking Ultron to "open the site and click Login" must work without an
#: approval banner on every step. But they cannot be invoked by the *dry-run*
#: harnesses (``core/verify_actions.py`` and the phase-2 suite): several of
#: their actions block until a browser event that never occurs in a dry run
#: (``dialog`` waits for a modal that no one opened), the MCP request queue
#: then stalls behind that call, and one test turns into tens of minutes of
#: back-to-back 45 s timeouts. Browser behaviour is exercised with fakes in
#: ``tests/test_sara_bots_custom.py`` and the action vocabulary is checked in
#: ``tests/test_sara_ultron_phase2.py::test_action_vocabulary`` instead, so the
#: dead-end scan loses nothing by skipping this one tool.
LIVE_BROWSER_TOOLS: Set[str] = {
    "brahma_browser_control",
}


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
class BrahmaBridge:
    """Loads the Brahma-Echo action set and exposes it as SARA action records."""

    def __init__(self, source_dir: Optional[Path] = None) -> None:
        self.source_dir = Path(source_dir or DEFAULT_SOURCE_DIR)
        self._records: Dict[str, BrahmaRecord] = {}
        self._rejected: List[BrahmaRecord] = []
        self._lock = threading.RLock()
        self._loaded = False
        self._ctx_factory: Optional[Callable[[], Dict[str, Any]]] = None

    # -- context ---------------------------------------------------------
    def set_context_factory(self, factory: Callable[[], Dict[str, Any]]) -> None:
        """Supply the ``player``/``speak``/``response``/``session_memory`` context.

        Brahma-Echo handlers all accept these. Without a factory the bridge
        passes ``None`` for each, which is what SARA's own actions already get.
        """
        self._ctx_factory = factory

    def _context(self) -> Dict[str, Any]:
        if self._ctx_factory is None:
            return {key: None for key in CTX_KEYS}
        try:
            ctx = self._ctx_factory() or {}
        except Exception:  # noqa: BLE001 - a bad factory must not kill a tool
            log.warning("Brahma context factory failed; using empty context",
                        exc_info=True)
            return {key: None for key in CTX_KEYS}
        return {key: ctx.get(key) for key in CTX_KEYS}

    # -- shims -----------------------------------------------------------
    def _install_packages(self) -> None:
        """Publish the upstream packages under their ``brahma_`` names.

        Every directory that upstream treats as a top-level package is
        registered under a ``brahma_``-prefixed name, and the meta-path finder
        is installed so every module inside them is loaded through
        :func:`rewrite_source`.
        """
        packages = {
            "brahma_core": self.source_dir / "core",
            "brahma_actions": self.source_dir / "actions",
            # Upstream's phone/IoT bridge lives in its own top-level package.
            "brahma_connect": self.source_dir / "brahma_connect",
        }

        for name, path in packages.items():
            if not path.is_dir():
                continue
            init = path / "__init__.py"
            try:
                if init.is_file():
                    spec = importlib.util.spec_from_file_location(
                        name, init, submodule_search_locations=[str(path)],
                    )
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        module.__path__ = [str(path)]  # type: ignore[attr-defined]
                        sys.modules[name] = module
                        try:
                            spec.loader.exec_module(module)
                        except BaseException:
                            _synthesise_package(name, path)
                else:
                    _synthesise_package(name, path)
            except BaseException as exc:  # noqa: BLE001
                log.warning("Could not initialise %s: %s", name, exc)
                _synthesise_package(name, path)

        sys.modules.setdefault("brahma_config", _install_config_shim())
        sys.modules.setdefault("brahma_llm_client", _install_llm_shim("brahma_llm_client"))
        sys.modules.setdefault("brahma_or_client", _install_llm_shim("brahma_or_client"))

        if not any(isinstance(f, _BrahmaFinder) for f in sys.meta_path):
            sys.meta_path.insert(0, _BrahmaFinder())

    # -- import ----------------------------------------------------------
    def _load_module(self, path: Path) -> ModuleType:
        """Import one action module through the rewriting finder."""
        cached = sys.modules.get(f"brahma_actions.{path.stem}")
        if isinstance(cached, ModuleType):
            return cached
        return importlib.import_module(f"brahma_actions.{path.stem}")

    @staticmethod
    def _entry_point(module: ModuleType, stem: str = "") -> Optional[Callable]:
        """Find the callable a Brahma-Echo module expects to be driven by.

        Every action exposes exactly one function whose first positional
        parameter is ``parameters`` — that is SARA's handler contract. Modules
        that only publish helpers (monitors, engines) are not actions and are
        skipped rather than guessed at.
        """
        import inspect

        candidates = []
        for attr in vars(module).values():
            if not callable(attr) or inspect.isclass(attr):
                continue
            if getattr(attr, "__module__", None) != module.__name__:
                continue
            try:
                params = list(inspect.signature(attr).parameters)
            except (TypeError, ValueError):
                continue
            if params and params[0] == "parameters":
                candidates.append(attr)

        if not candidates:
            return None
        # An explicit override always wins: upstream wires a specific
        # dispatcher, not whichever parameters-first helper sorts first.
        override = _ENTRY_OVERRIDES.get(stem, "")
        if override:
            for attr in candidates:
                if attr.__name__ == override:
                    return attr
        # Prefer a plain `run` when several match, else the first.
        for attr in candidates:
            if attr.__name__ == "run":
                return attr
        return candidates[0]

    @staticmethod
    def _schema(module: ModuleType, handler: Callable, name: str) -> Dict[str, Any]:
        """Build a Gemini/JSON-schema parameter block for a bridged handler.

        Upstream describes parameters in prose, so the object's real signature
        is the honest source: required arguments become ``required``, and the
        rest are typed from their defaults.
        """
        import inspect

        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            return {"type": "OBJECT", "properties": {}}

        properties: Dict[str, Any] = {}
        required: List[str] = []
        for pname, param in sig.parameters.items():
            if pname in CTX_KEYS or param.kind in (
                inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL
            ):
                continue
            if param.default is inspect.Parameter.empty:
                required.append(pname)
                schema: Dict[str, Any] = {"type": "string"}
            else:
                default = param.default
                if isinstance(default, bool):
                    schema = {"type": "boolean"}
                elif isinstance(default, int):
                    schema = {"type": "integer"}
                elif isinstance(default, float):
                    schema = {"type": "number"}
                elif isinstance(default, (list, tuple)):
                    schema = {"type": "array", "items": {"type": "string"}}
                elif isinstance(default, dict):
                    schema = {"type": "object"}
                else:
                    schema = {"type": "string"}
                if isinstance(default, str) and default:
                    schema["default"] = default
            properties[pname] = schema

        out: Dict[str, Any] = {"type": "OBJECT", "properties": properties}
        if required:
            out["required"] = required
        return out

    @staticmethod
    def _action_vocabulary(path: Path, handler_name: Optional[str] = None,
                           stem: str = "") -> List[str]:
        """Recover the ``action`` values a handler accepts.

        Most Brahma-Echo tools take a single ``parameters`` dict and dispatch on
        ``action``. Without the vocabulary the planner has to guess a name, and
        a guess produces "Unknown action" at run time - a run that looks
        planned, reports nothing useful, and leaves the user told it worked.

        Two dispatch shapes are recognised: a dict whose values are all
        callables, and a comparison against a name called ``action``. Upstream
        uses three spellings of the comparison and all three are matched:
        ``action == "x"``, ``"x" == action``, and ``action in ("x", "y")`` - the
        last is how ``desktop.py`` dispatches ``organize``/``organize_desktop``.

        Scope
        -----
        The entry handler's own body is scanned first, and the module scope is
        *always* unioned in. Both are needed: ``desktop.py`` dispatches in its
        handler body, while ``computer_settings.py`` delegates to a module-level
        table and would otherwise advertise 18 of its ~45 real actions - missing
        ``restart``, ``shutdown`` and ``lock_screen``, which are exactly the
        ones that must be gated.

        Two filters keep the wider scan honest:

        * Builtins are dropped from *table* candidates, never from *comparison*
          candidates. ``desktop.py`` declares a ``safe_builtins`` sandbox
          allowlist that would otherwise become 22 advertised verbs, but
          ``computer_control`` genuinely dispatches ``action == "type"`` and
          ``file_controller`` genuinely dispatches ``action == "list"``.
        * A table is only read when every value is callable, which is what
          separates a dispatch table from ordinary data.

        Over-listing is recoverable (the planner picks a listed name and it
        works); under-listing is what produces the "Unknown action" dead end, so
        the union errs toward completeness and ``core/verify_actions.py``
        exercises every name it advertises.
        """
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            return []

        scope: ast.AST = tree
        if handler_name:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == handler_name:
                    scope = node
                    break

        found: List[str] = []
        from_chain: List[str] = []
        from_dict: List[str] = []

        def add(bucket: List[str], value: Any) -> None:
            if isinstance(value, str) and value and value not in bucket:
                # Action names are identifiers-ish and never dunder/private.
                if value.startswith("_"):
                    return
                if re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", value):
                    bucket.append(value)

        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                keys = [k.value for k in node.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                values = node.value.values
                if (len(keys) >= 3 and len(keys) == len(values)
                        and all(isinstance(v, _CALLABLE_LIKE) for v in values)
                        and not any("__" in k for k in keys)
                        and not any(isinstance(v, ast.Call) for v in values)):
                    # Rejections: dunder keys ("__builtins__") and Call
                    # values mark a sandbox/namespace dict (desktop.py),
                    # not a dispatch table - its keys (Path/shutil/time/
                    # os_path) are imported objects, not actions.
                    for key in keys:
                        add(from_dict, key)
            elif isinstance(node, ast.Compare):
                # The action name can be compared either way round
                # (`action == "x"` or `"x" == action`), so look for it on both
                # sides rather than assuming the left.
                sides = (node.left, *node.comparators)
                if not any(
                    isinstance(o, ast.Name) and o.id in ("action", "_action")
                    for o in sides
                ):
                    continue
                for side in sides:
                    if isinstance(side, ast.Constant) and isinstance(side.value, str):
                        add(from_chain, side.value)
                    elif isinstance(side, (ast.Tuple, ast.List, ast.Set)):
                        # `elif action in ("organize", "organize_desktop"):`
                        for element in side.elts:
                            if isinstance(element, ast.Constant) and isinstance(
                                element.value, str
                            ):
                                add(from_chain, element.value)

        # Union, not "chain wins": a module can dispatch `action == "play"` in
        # one branch and also carry a `{"summarize": ..., "trending": ...}`
        # table for the rest. Preferring the chain silently dropped three real
        # actions from youtube_video.
        #
        # Builtins are filtered from the *table* only, never from the chain.
        # A table is a guess and picks up `desktop.py`'s `safe_builtins`
        # sandbox allowlist; a string compared against `action` is by
        # definition an action.
        found = list(from_chain)
        for value in from_dict:
            if value not in found and value not in BUILTIN_NAMES:
                found.append(value)

        # Always union the module scope: handlers that delegate to a
        # module-level dispatch table (`computer_settings`) would otherwise
        # advertise a fraction of their real actions.
        if scope is not tree:
            for value in BrahmaBridge._action_vocabulary(path, None, stem):
                if value not in found:
                    found.append(value)
        return found

    @classmethod
    def _schema_with_actions(cls, module: ModuleType, handler: Callable,
                             name: str, vocabulary: List[str]) -> Dict[str, Any]:
        """Schema for a handler, with the discovered actions as an enum.

        Brahma-Echo handlers take a single ``parameters`` dict, so describing
        the *wrapper* tells the planner nothing — it would have to guess that
        the real arguments live inside. When ``parameters`` is the only input
        the schema is unwrapped to the dict's own contents, and the discovered
        action list becomes an ``enum``.

        An ``enum`` is what makes this reliable: a provider validating against
        the declared tool schema cannot return an action name the tool does
        not implement, which is the "Unknown action" dead end the planner used
        to walk into on every one of these tools.
        """
        schema = cls._schema(module, handler, name)
        properties: Dict[str, Any] = schema.get("properties", {})

        # Unwrap the single-`parameters` signature into the dict's contents.
        if list(properties) == ["parameters"]:
            properties = {}
        if not vocabulary:
            return schema

        if "action" in properties:
            target = properties["action"]
        elif not properties:
            target = {"type": "string"}
            properties["action"] = target
        else:
            # The handler has named parameters *and* an action vocabulary; the
            # vocabulary still belongs on the dict, so keep the signature.
            properties.setdefault("action", {"type": "string"})

        target["type"] = "string"
        target["enum"] = sorted(vocabulary)
        target["description"] = "What to do. Must be one of the listed values."

        required = [r for r in schema.get("required", []) if r != "parameters"]
        if "action" not in required:
            required.insert(0, "action")
        out: Dict[str, Any] = {"type": "OBJECT", "properties": properties}
        if required:
            out["required"] = required
        return out

    def _describe(self, module: ModuleType, handler: Callable, stem: str,
                  vocabulary: Optional[List[str]] = None) -> str:
        """Tool description: upstream docstring first, prose fallback second."""
        doc = (handler.__doc__ or module.__doc__ or "").strip()
        doc = " ".join(doc.split())
        if not doc or doc.lower().startswith("brahma-echo "):
            doc = ""
        if not doc:
            doc = f"Brahma-Echo '{stem}' action."
        if vocabulary:
            listed = ", ".join(vocabulary)
            doc = f"{doc} Actions: {listed}."
        return doc[:1200]

    # -- public ----------------------------------------------------------
    def load(self) -> "BrahmaBridge":
        with self._lock:
            if self._loaded:
                return self
            if not self.source_dir.is_dir():
                log.warning("Brahma-Echo source not found at %s; bridge is empty",
                            self.source_dir)
                self._loaded = True
                return self

            self._install_packages()
            actions_dir = self.source_dir / "actions"
            for path in sorted(actions_dir.glob("*.py"), key=lambda p: p.name):
                if path.name.startswith("_"):
                    continue
                record = self._load_one(path)
                if record is None:
                    continue
                if record.valid:
                    self._records[record.name] = record
                else:
                    self._rejected.append(record)
            self._loaded = True
            log.info(
                "Brahma bridge: %d tools available, %d unavailable",
                len(self._records), len(self._rejected),
            )
        return self

    def _load_one(self, path: Path) -> Optional[BrahmaRecord]:
        stem = path.stem
        if stem in _BLOCKED:
            return None

        try:
            module = self._load_module(path)
        except BaseException as exc:  # noqa: BLE001 - one bad file, not the set
            return BrahmaRecord(
                name=f"brahma_{stem}", file=path.name, valid=False,
                error=f"import failed: {type(exc).__name__}: {exc}",
            )

        # A module may expose its own TOOL dict (Brahma-Echo plugins do).
        tool = getattr(module, "TOOL", None) or getattr(module, "PLUGIN", None)
        if isinstance(tool, dict) and callable(tool.get("handler")):
            name = f"brahma_{tool.get('name') or stem}"
            handler = tool["handler"]
            vocabulary = _VOCABULARY_OVERRIDES.get(stem) \
                or self._action_vocabulary(
                    path, getattr(handler, "__name__", None), stem
                )
            params = tool.get("parameters")
            if not (isinstance(params, dict) and params.get("type") == "OBJECT"):
                params = self._schema_with_actions(module, handler, name, vocabulary)
            else:
                # Upstream's own schema is authoritative for *shape*, but it
                # never enumerates what `action` accepts. Trusting it verbatim
                # is what left `brahma_file_controller` with 14 real actions
                # and a bare `action: string`, so the planner invented names and
                # every call came back "Unknown action".
                params = self._with_action_enum(params, vocabulary)
            return BrahmaRecord(
                name=name, file=path.name,
                description=str(tool.get("description")
                                or self._describe(module, handler, stem, vocabulary)),
                parameters=params, handler=handler, valid=True,
                gated=name in RISKY_TOOLS,
            )

        handler = self._entry_point(module, stem)
        if handler is None:
            # Not an action: a monitor, engine or capture-only helper. Kept out
            # of the tool surface on purpose — exposing half an object as a tool
            # is how a planner ends up calling something that does nothing.
            return None

        vocabulary = _VOCABULARY_OVERRIDES.get(stem) \
            or self._action_vocabulary(path, handler.__name__, stem)
        name = f"brahma_{stem}"
        return BrahmaRecord(
            name=name, file=path.name,
            description=self._describe(module, handler, stem, vocabulary),
            parameters=self._schema_with_actions(module, handler, name, vocabulary),
            handler=handler, valid=True, gated=name in RISKY_TOOLS,
        )

    @staticmethod
    def _with_action_enum(schema: Dict[str, Any],
                          vocabulary: List[str]) -> Dict[str, Any]:
        """Add the discovered actions as an ``enum`` on an existing schema.

        Idempotent and non-destructive: a schema with no ``action`` property is
        returned untouched, because in that case the handler does not dispatch on
        one and an enum would be a lie the planner acts on.
        """
        if not vocabulary:
            return schema
        props = schema.get("properties")
        if not isinstance(props, dict):
            return schema
        target = props.get("action")
        if not isinstance(target, dict):
            return schema
        enriched = dict(schema)
        enriched["properties"] = {**props, "action": {
            **target,
            "type": "string",
            "enum": sorted(vocabulary),
            "description": (
                f"Action to perform. One of: {', '.join(sorted(vocabulary))}."
            ),
        }}
        return enriched

    # -- registry interface ---------------------------------------------
    def names(self) -> Set[str]:
        with self._lock:
            return set(self._records)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._records

    def record(self, name: str) -> Optional[BrahmaRecord]:
        with self._lock:
            return self._records.get(name)

    def all_records(self) -> List[BrahmaRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.name)

    def rejected(self) -> List[BrahmaRecord]:
        with self._lock:
            return list(self._rejected)

    def is_gated(self, name: str) -> bool:
        record = self.record(name)
        return bool(record and record.gated)

    def run(self, name: str, parameters: Optional[Dict[str, Any]] = None) -> str:
        record = self.record(name)
        if record is None or not record.valid or record.handler is None:
            return f"Action '{name}' is not available."
        import inspect

        kwargs: Dict[str, Any] = {"parameters": parameters or {}}
        try:
            sig = inspect.signature(record.handler)
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            ctx = self._context()
            for key, value in ctx.items():
                if has_var_kw or key in sig.parameters:
                    kwargs[key] = value
            out = record.handler(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Brahma tool %s raised: %s", name, exc, exc_info=True)
            return f"Tool '{name}' failed: {exc}"
        return out if isinstance(out, str) else str(out)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            gated = [r.name for r in self._records.values() if r.gated]
            return {
                "source_dir": str(self.source_dir),
                "available": len(self._records),
                "unavailable": len(self._rejected),
                "gated": sorted(gated),
                "available_names": sorted(self._records),
                "unavailable_detail": [
                    {"file": r.file, "name": r.name, "reason": r.error}
                    for r in self._rejected
                ],
            }


_BRIDGE: Optional[BrahmaBridge] = None
_BRIDGE_LOCK = threading.Lock()


def get_bridge() -> BrahmaBridge:
    """Process-wide bridge, built once."""
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = BrahmaBridge().load()
        return _BRIDGE


def reset_bridge() -> None:
    global _BRIDGE
    with _BRIDGE_LOCK:
        _BRIDGE = None
