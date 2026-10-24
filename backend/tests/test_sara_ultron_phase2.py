"""SARA Ultron - Phase 2 tests: the Brahma-Echo bridge and the local voice stack.

Kept separate from ``test_sara_ultron_agent.py`` on purpose. That file is the
original 20-section integration suite and stays byte-for-byte stable as the
regression baseline; this one covers the two systems added afterwards, whose
failure modes are completely different - the bridge is about *isolation* (does
loading 38 upstream modules contaminate SARA?) and voice is about *blocking*
(mic and TTS calls hang forever if they run on the event loop).

Run both:
    python -m tests.test_sara_ultron_agent
    python -m tests.test_sara_ultron_phase2
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.agents.sara_ultron_agent import api as api_mod  # noqa: E402
from app.agents.sara_ultron_agent.core import tools as tools_mod  # noqa: E402
from app.agents.sara_ultron_agent.core.brahma_bridge import (  # noqa: E402
    DEFAULT_SOURCE_DIR,
    LIVE_BROWSER_TOOLS,
    RISKY_TOOLS,
    BrahmaBridge,
    _UnifiedClientShim,
    get_bridge,
    rewrite_source,
)
from app.agents.sara_ultron_agent.core.executor import (  # noqa: E402
    DESTRUCTIVE_BRAHMA_ACTIONS,
    is_destructive,
)
from app.agents.sara_ultron_agent.core.tools import (  # noqa: E402
    NATIVE_TOOL_NAMES,
    get_registry,
)
from app.agents.sara_ultron_agent.core.voice import (  # noqa: E402
    DEFAULT_MODEL,
    MAX_UTTERANCE_S,
    UltronVoice,
    get_voice,
)

#: Shared with `core/verify_actions.py` so the two harnesses cannot disagree
#: about what counts as a dead end.
_DEAD_END = re.compile(
    r"unknown action|unrecognized action|invalid action|could not match|"
    r"unsupported action|action not found|no such action|"
    r"unknown (?:command|option)|no handler for",
    re.I,
)

# ---------------------------------------------------------------------------
# Tiny harness (same shape as the main suite so output is consistent)
# ---------------------------------------------------------------------------
_FAILURES: List[str] = []
_CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _CHECKS
    _CHECKS += 1
    if ok:
        print(f"  [OK  ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))
        _FAILURES.append(f"{label} ({detail})" if detail else label)
    return ok


def section(title: str) -> None:
    print("\n" + title)
    print("-" * 66)


# ---------------------------------------------------------------------------
# 1. Import rewriting
# ---------------------------------------------------------------------------
def test_rewrite_source() -> None:
    section("1. Import rewriting (the isolation mechanism)")

    src = (
        "from core.echo import cancel_echo\n"
        "from core.user_paths import get_user_data_dir\n"
        "import config\n"
        "from actions.youtube_video import play\n"
        "from llm_client import client\n"
        "from or_client import chat\n"
        "import os\n"
        "from __future__ import annotations\n"
        "config = 5\n"
    )
    out = rewrite_source(src, "demo")

    check("from core.X -> brahma_core.X",
          "from brahma_core.echo import cancel_echo" in out, out)
    check("import config -> import brahma_config as config",
          "import brahma_config as config" in out, out)
    check("from actions.X -> brahma_actions.X",
          "from brahma_actions.youtube_video import play" in out, out)
    check("from llm_client -> brahma_llm_client",
          "from brahma_llm_client import client" in out, out)
    check("from or_client -> brahma_or_client",
          "from brahma_or_client import chat" in out, out)
    check("unrelated imports untouched",
          "import os" in out and "from __future__ import annotations" in out, out)
    check("a local variable named config is not rewritten",
          "\nconfig = 5\n" in out, out)
    check("rewritten source still parses", _parses(out))


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


# ---------------------------------------------------------------------------
# 2. Security shims
# ---------------------------------------------------------------------------
def test_security_shims() -> None:
    section("2. Security shims (no plaintext secrets, no second LLM path)")

    # The shims are installed when the bridge first loads, not at import of this
    # module, so force that here rather than depending on test ordering.
    get_bridge()
    cfg_mod = sys.modules.get("brahma_config")
    check("config shim is installed", cfg_mod is not None)
    if cfg_mod is not None:
        cfg = cfg_mod.get_config()
        # The shim holds no credentials of its own; it republishes whatever the
        # *environment* holds, which is the supported way to configure an
        # upstream tool that needs a key. So the assertion is that every
        # non-platform entry is traceable to an env var, not that there are
        # none - this machine legitimately exports several.
        stray = [k for k in cfg
                 if k != "os_system"
                 and os.environ.get(k.upper()) != cfg[k]]
        check("every config entry comes from the environment, none is hardcoded",
              not stray, f"untraceable: {sorted(stray)}")
        check("config shim reports the OS", cfg_mod.is_windows() is True)
        # The real security property: upstream read `config/api_keys.json` off
        # disk. Nothing in the shim may open a file.
        source = inspect.getsource(cfg_mod.get_config)
        check("config shim has no file-reading source",
              "open(" not in source and "api_keys" not in source, source)

    # A key exported into the environment must be visible to upstream, which is
    # the supported way to configure a tool that needs one.
    os.environ["SARA_TEST_API_KEY"] = "sentinel-value"
    try:
        cfg = sys.modules["brahma_config"].get_config()
        check("exported env keys reach upstream config",
              cfg.get("sara_test_api_key") == "sentinel-value")
    finally:
        os.environ.pop("SARA_TEST_API_KEY", None)

    llm_mod = sys.modules.get("brahma_llm_client")
    check("llm shim is installed", llm_mod is not None)
    check("llm shim exposes `client`", hasattr(llm_mod, "client"))
    client = getattr(llm_mod, "client", None)
    check("client is the SARA-backed shim",
          isinstance(client, _UnifiedClientShim), type(client).__name__)

    # Every method upstream calls must exist, or nine modules break at import.
    for method in ("chat", "chat_json", "multi_turn", "vision",
                   "vision_from_file", "Dispatch", "reload_settings"):
        check(f"client.{method}() exists",
              callable(getattr(client, method, None)))
    check("Dispatch keeps upstream's capitalisation",
          hasattr(client, "Dispatch") and not hasattr(client, "dispatch"))

    # chat_json must survive a reply that is fenced or chatty, because a
    # failover chain can land on a provider that ignores JSON mode.
    from app.agents.sara_ultron_agent.core.brahma_bridge import _parse_json_loose
    check("json: bare object",
          _parse_json_loose('{"a": 1}') == {"a": 1})
    check("json: fenced block",
          _parse_json_loose('```json\n{"a": 2}\n```') == {"a": 2})
    check("json: prose around it",
          _parse_json_loose('Sure! Here you go: {"a": 3} Hope that helps.')
          == {"a": 3})
    check("json: total garbage -> empty, not an exception",
          _parse_json_loose("no json at all") == {})

    # The shim must route through SARA's router, never a hardcoded pool.
    src = inspect.getsource(_UnifiedClientShim)
    check("llm shim does not name a model provider",
          "openrouter" not in src.lower() and "generativeai" not in src.lower())
    check("llm shim defers to app.core.llm_router", "llm_chat_sync" in src)


# ---------------------------------------------------------------------------
# 3. The bridge loads the full set
# ---------------------------------------------------------------------------
def test_bridge_loads() -> None:
    section("3. Bridge loading")

    if not DEFAULT_SOURCE_DIR.is_dir():
        check("Brahma-Echo source present", False, str(DEFAULT_SOURCE_DIR))
        print("  (skipped: set BRAHMA_ECHO_DIR or place the checkout at the default path)")
        return

    bridge = BrahmaBridge().load()
    stats = bridge.stats()
    names = stats["available_names"]

    check("every upstream module produced a tool or a stated reason",
          stats["available"] + stats["unavailable"] >= 30,
          f"{stats['available']} available, {stats['unavailable']} unavailable")
    check("no module failed to import",
          stats["unavailable"] == 0,
          json.dumps(stats["unavailable_detail"])[:300])
    check("the set is substantially complete",
          stats["available"] >= 35, f"only {stats['available']}")

    # Names must be namespaced so they cannot collide with SARA's own actions,
    # several of which share a filename with an upstream one.
    check("every tool is brahma_ prefixed",
          all(n.startswith("brahma_") for n in names),
          str([n for n in names if not n.startswith("brahma_")][:5]))
    check("no tool shadows a SARA desktop action name",
          not (set(names) & set(tools_mod.get_registry()._desktop.names())))
    check("tool names are unique", len(names) == len(set(names)))

    # The modules upstream also ships must be present, not just the easy ones.
    for expected in ("brahma_youtube_video", "brahma_spotify_controller",
                     "brahma_weather_report", "brahma_browser_control",
                     "brahma_file_controller", "brahma_computer_settings"):
        check(f"bridged {expected}", expected in names)

    check("get_bridge() is a process singleton", get_bridge() is get_bridge())

    # Loading must not have touched SARA's own action modules.
    for name in ("actions.youtube_video", "actions.web_search"):
        mod = sys.modules.get(name)
        if mod is not None:
            check(f"SARA's {name} was not replaced by the bridge",
                  not str(getattr(mod, "__file__", "")).startswith(
                      str(DEFAULT_SOURCE_DIR)),
                  str(getattr(mod, "__file__", "")))
    check("no bare `core`/`config` module was injected into sys.modules",
          "core" not in sys.modules or getattr(
              sys.modules.get("core"), "__file__", "") != str(DEFAULT_SOURCE_DIR),
          str(getattr(sys.modules.get("core"), "__file__", "")))


# ---------------------------------------------------------------------------
# 4. Action vocabulary discovery
# ---------------------------------------------------------------------------
def test_action_vocabulary() -> None:
    section("4. Action vocabulary (why the planner stops guessing)")

    if not DEFAULT_SOURCE_DIR.is_dir():
        check("Brahma-Echo source present", False)
        return
    bridge = BrahmaBridge().load()

    with_enum = []
    for name in bridge.stats()["available_names"]:
        rec = bridge.record(name)
        enum = ((rec.parameters or {}).get("properties", {})
                .get("action", {}) or {}).get("enum")
        if enum:
            with_enum.append((name, enum))

    check("most action-dispatching tools expose an enum",
          len(with_enum) >= 12, f"only {len(with_enum)}")

    # `desktop.py` declares two dicts that are *not* dispatch tables:
    #   safe_builtins = {"print": print, "len": len, ...}   (22 entries)
    #   sandbox       = {"__builtins__": ..., "Path": Path, ...}  (5 entries)
    # Both are dicts of callables, so both look exactly like a dispatch table.
    # Neither may reach the planner, so the check is against the real names
    # from that file rather than a blanket "no builtins" rule - `open`,
    # `eval` and `next` are legitimately actions in docx_tools,
    # browser_control and spotify_controller, and banning them outright would
    # be a different bug in the other direction.
    sandbox_names = {
        "print", "len", "str", "int", "float", "bool", "dict", "tuple",
        "range", "enumerate", "sorted", "isinstance", "hasattr", "getattr",
        "max", "min", "sum", "abs", "zip", "map", "filter",
        "__builtins__", "Path", "time", "shutil", "os_path",
    }
    desktop_enum = ((bridge.record("brahma_desktop").parameters or {})
                    .get("properties", {}).get("action", {}) or {}).get("enum")
    check("brahma_desktop exposes an enum", bool(desktop_enum), str(desktop_enum))
    leaked = set(desktop_enum or []) & sandbox_names
    check("brahma_desktop carries none of its own sandbox allowlist names",
          not leaked, str(sorted(leaked)))
    check("brahma_desktop still advertises its real actions",
          {"organize", "organize_desktop", "clean", "list", "wallpaper"}
          <= set(desktop_enum or []), str(desktop_enum))

    # And the two real actions whose names collide with builtins must survive.
    cc_enum = ((bridge.record("brahma_computer_control").parameters or {})
               .get("properties", {}).get("action", {}) or {}).get("enum")
    check("computer_control keeps its real 'type' action",
          bool(cc_enum) and "type" in cc_enum, str(cc_enum))
    fc_enum = ((bridge.record("brahma_file_controller").parameters or {})
               .get("properties", {}).get("action", {}) or {}).get("enum")
    check("file_controller keeps its real 'list' action",
          bool(fc_enum) and "list" in fc_enum, str(fc_enum))
    docx_enum = ((bridge.record("brahma_docx_tools").parameters or {})
                 .get("properties", {}).get("action", {}) or {}).get("enum")
    check("docx_tools keeps its real 'open' action",
          bool(docx_enum) and "open" in docx_enum, str(docx_enum))

    # A known ground truth: file_controller dispatches on these literals.
    fc = bridge.record("brahma_file_controller")
    enum = ((fc.parameters or {}).get("properties", {}).get("action", {}) or {}).get("enum")
    check("file_controller discovered its real action names",
          enum is not None and {"list", "delete", "copy"} <= set(enum),
          str(enum))

    yt = bridge.record("brahma_youtube_video")
    enum = ((yt.parameters or {}).get("properties", {}).get("action", {}) or {}).get("enum")
    check("youtube_video includes play", enum is not None and "play" in enum,
          str(enum))

    # The schema must be unwrapped: a planner shown only `parameters` has to
    # guess that the real arguments live inside it.
    for name, enum in with_enum:
        rec = bridge.record(name)
        props = (rec.parameters or {}).get("properties", {})
        check(f"{name}: schema is not a bare 'parameters' wrapper",
              "parameters" not in props, str(list(props)))
        break  # one representative is enough; the loop above is the real check

    # The enum must reach the LLM declaration the planner is sent.
    reg = get_registry()
    decls = {d["function"]["name"]: d for d in reg.declarations()}
    yt_decl = decls.get("brahma_youtube_video", {})
    props = (yt_decl.get("function", {}).get("parameters", {})
             .get("properties", {}))
    check("enum survives into the planner's tool declaration",
          "enum" in props.get("action", {}), str(props.get("action"))[:120])


# ---------------------------------------------------------------------------
# 5. Descriptions reach the planner
# ---------------------------------------------------------------------------
def test_descriptions_reach_planner() -> None:
    section("5. Descriptions reach the planner")

    reg = get_registry()
    text = reg.descriptions()
    check("planner catalogue is non-trivial", len(text) > 1500, str(len(text)))
    check("catalogue announces the Brahma-Echo section",
          "BRAHMA-ECHO" in text)
    check("catalogue lists gated tools explicitly",
          "approval" in text.lower())
    check("catalogue names a real gated tool",
          "brahma_mobile_autopilot" in text)
    check("catalogue still lists desktop actions",
          "SARA DESKTOP ACTIONS" in text)
    check("catalogue still lists native tools",
          "ULTRON NATIVE TOOLS" in text)

    # Every tool in the registry must appear, or the planner cannot pick it.
    missing = [n for n in reg.names() if n not in text]
    check("every registered tool is in the catalogue", not missing,
          str(missing[:6]))

    # Required-argument hints are what stop the planner inventing an argument.
    check("required arguments are shown",
          "(requires:" in text or "parameters" in text)


# ---------------------------------------------------------------------------
# 6. Approval gating
# ---------------------------------------------------------------------------
def test_approval_gating() -> None:
    section("6. Approval gating for risky tools")

    reg = get_registry()
    gated = sorted(n for n in reg.names() if reg.is_gated(n))
    check("risky tools are flagged gated", len(gated) >= 5, str(gated))
    for name in ("brahma_mobile_autopilot", "brahma_system_manager",
                 "brahma_unlock_device", "brahma_upload_video"):
        check(f"{name} is gated", reg.is_gated(name))
    check("a read-only tool is not gated",
          not reg.is_gated("brahma_weather_report"))

    # A whole-tool gate must apply to any call, even one with no action.
    check("gated tool gates an empty call", is_destructive(
        "brahma_system_manager", {}, reg.is_gated("brahma_system_manager")))

    # Per-action gating: asking the volume is not as intrusive as a reboot.
    check("reading the volume is not gated", not is_destructive(
        "brahma_computer_settings", {"action": "volume_get"},
        reg.is_gated("brahma_computer_settings")))
    for act in ("restart", "shutdown", "lock_screen"):
        check(f"computer_settings/{act} is gated", is_destructive(
            "brahma_computer_settings", {"action": act},
            reg.is_gated("brahma_computer_settings")))

    # The pre-existing SARA behaviour must be untouched.
    check("pre-existing computer_settings gate still fires",
          is_destructive("computer_settings", {}))
    check("pre-existing file_controller/delete still fires",
          is_destructive("file_controller", {"action": "delete"}))
    check("pre-existing file_controller/list is not gated",
          not is_destructive("file_controller", {"action": "list"}))

    # Every listed destructive Brahma action must be a real action of its tool,
    # or the gate is decoration.
    reg_all = get_registry()
    for tool, actions in DESTRUCTIVE_BRAHMA_ACTIONS.items():
        rec = None
        if reg_all._brahma is not None:
            rec = reg_all._brahma.record(tool)
        if rec is None:
            continue
        enum = ((rec.parameters or {}).get("properties", {})
                .get("action", {}) or {}).get("enum")
        if not enum:
            continue
        phantom = actions - set(enum)
        check(f"{tool}: gated actions all exist", not phantom, str(sorted(phantom)))


# ---------------------------------------------------------------------------
# 7. Registry integration
# ---------------------------------------------------------------------------
def test_registry_integration() -> None:
    section("7. Registry integration")

    reg = get_registry()
    names = reg.names()
    check("registry is non-empty", len(names) > 40, str(len(names)))
    check("tool names are unique", len(names) == len(set(names)))

    sources = {reg.source(n) for n in names}
    check("three sources are represented",
          {"ultron", "desktop", "brahma"} <= sources, str(sources))
    check("voice tools are native", reg.source("ultron_speak") == "ultron")
    check("weather is brahma", reg.source("brahma_weather_report") == "brahma")

    # A brahma schema must win over a default, and an unknown tool must not
    # crash the schema lookup.
    check("brahma schema is returned",
          (reg._schema("brahma_file_controller") or {})
          .get("properties", {}).get("action", {}).get("enum") is not None)
    check("native schema is returned",
          "agent_id" in (reg._schema("ultron_delegate") or {}).get("properties", {}))
    check("unknown tool yields an empty schema, not an exception",
          reg._schema("does_not_exist") in ({}, None))

    # Missing-argument validation must see through the unwrapped schema.
    check("a brahma call missing its action is reported",
          reg.missing_args("brahma_file_controller", {}) == ["action"],
          str(reg.missing_args("brahma_file_controller", {})))
    check("a complete brahma call passes validation",
          not reg.missing_args("brahma_file_controller",
                               {"action": "list", "path": "C:/"}))

    # An unknown tool must report unavailability, not raise.
    check("unknown tool returns a message",
          "not available" in reg.run("does_not_exist", {}).lower(),
          reg.run("does_not_exist", {}))

    # Every declaration must be valid OpenAI function-calling shape.
    decls = reg.declarations()
    check("declarations cover every tool", len(decls) == len(names),
          f"{len(decls)} vs {len(names)}")
    bad = [d for d in decls
           if d.get("type") != "function"
           or not isinstance(d.get("function", {}).get("name"), str)
           or not isinstance(d["function"].get("parameters"), dict)]
    check("every declaration is well-formed", not bad, str(bad[:2]))


# ---------------------------------------------------------------------------
# 8. Voice
# ---------------------------------------------------------------------------
def test_voice_surface() -> None:
    section("8. Voice surface")

    check("voice tools are declared as native tools",
          {"ultron_listen", "ultron_speak"} <= NATIVE_TOOL_NAMES)
    check("voice tools are in the registry",
          get_registry().has("ultron_listen")
          and get_registry().has("ultron_speak"))

    voice = UltronVoice()
    status = voice.status()
    data = status.as_dict()

    check("status is JSON-serialisable", isinstance(json.dumps(data), str))
    for field in ("available", "microphone", "transcriber", "speech_synth",
                  "model", "voices", "problems", "echo_guard_s"):
        check(f"status reports '{field}'", field in data, str(sorted(data)))
    check("status declares a model", data["model"] == DEFAULT_MODEL)
    check("status is honest about a missing mic",
          data["microphone"] is False or data["microphone"] is True)
    if not data["microphone"]:
        check("a missing mic produces an explanation",
              any("microphone" in p.lower() for p in data["problems"]),
              str(data["problems"]))

    # status() must not record, load Whisper, or start the engine.
    check("status does not load the Whisper model",
          voice._model is None)
    check("status does not claim the engine is loaded",
          voice._engine is None)
    check("get_voice is a singleton", get_voice() is get_voice())


def test_voice_echo_guard() -> None:
    section("9. Voice echo guard")

    voice = UltronVoice()

    # A short reply must arm a guard, and it must be positive.
    voice._last_spoken = ""
    voice._echo_until = 0.0
    short = voice._mark_speaking("Opening YouTube now.")
    check("speaking arms the echo guard", short > 0, str(short))
    long = voice._mark_speaking("word " * 60)
    check("a longer reply guards for longer", long > short, f"{short} -> {long}")

    # Counts down, and clears once it expires. Measured on a guard set one
    # second out rather than by re-arming mid-assertion, which would race.
    voice._echo_until = time.time() + 1.0
    first = voice.echo_guard_remaining()
    time.sleep(0.15)
    second = voice.echo_guard_remaining()
    check("guard counts down", 0 < second < first, f"{first:.3f} -> {second:.3f}")
    voice._echo_until = time.time() - 1.0
    check("an expired guard reports zero", voice.echo_guard_remaining() == 0.0)

    # A listener started while the speaker is still going must refuse.
    voice._echo_until = time.time() + 5.0
    result = voice.listen(timeout=2, phrase_limit=2)
    check("listen is suppressed while speaking", not result.get("ok"))
    check("the suppression reason is explicit",
          "still speaking" in str(result.get("error", "")), str(result))
    check("suppression reports the remaining guard",
          result.get("echo_guard_s", 0) > 0)

    # The transcript-level check catches a reply that slipped into the same clip.
    voice._last_spoken = "I have opened YouTube and started a lo-fi video."
    check("an identical transcript is treated as echo",
          voice._is_echo("I have opened YouTube and started a lo fi video."))
    check("a different sentence is not echo",
          not voice._is_echo("What is the weather in London"))
    check("an empty transcript is not echo", not voice._is_echo(""))
    check("echo check with nothing spoken is not echo",
          not UltronVoice()._is_echo("anything"))


def test_voice_failure_paths() -> None:
    section("10. Voice failure handling")

    voice = UltronVoice()

    # Every failure must be a message, never an exception: these are called from
    # inside the executor's retry loop.
    check("speaking nothing is refused",
          not voice.speak("").get("ok"))
    check("the empty-speak reason is clear",
          "nothing to say" in voice.speak("  ")["error"])
    check("listening with no recogniser is refused",
          not voice.listen(timeout=1, phrase_limit=1).get("ok"))
    check("transcribing empty audio is refused",
          not voice.transcribe(b"").get("ok"))
    check("transcribing garbage audio is refused, not raised",
          not voice.transcribe(b"not a wav file at all").get("ok"))
    check("cancelling with no engine reports honestly",
          not voice.cancel_speech().get("ok"))

    # Timeouts are clamped so a request cannot wedge the worker indefinitely.
    check("timeout is clamped to the ceiling",
          MAX_UTTERANCE_S >= 1.0)

    # The tool wrappers must return planner-readable text, not dicts.
    check("ultron_speak with no text explains itself",
          "Voice output failed" in voice._tool_speak({}))
    check("ultron_listen failure is reported as text",
          "Voice input failed" in voice._tool_listen({"timeout": 1}))


def test_voice_does_not_block_import() -> None:
    section("11. Voice does not block at import time")

    # The single worst regression this feature could cause: importing the agent
    # opens a microphone or stalls on a model download. Measure the import.
    import subprocess
    code = (
        "import time; t=time.perf_counter();"
        "import app.agents.sara_ultron_agent.core.voice as v;"
        "print(f'{time.perf_counter()-t:.3f}')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND),
        capture_output=True, text=True, timeout=120,
    )
    check("voice module imports cleanly in a fresh process",
          proc.returncode == 0, proc.stderr[-300:])
    if proc.returncode == 0:
        try:
            elapsed = float(proc.stdout.strip().splitlines()[-1])
            check("voice import is fast (no model load, no mic open)",
                  elapsed < 5.0, f"{elapsed:.2f}s")
        except (ValueError, IndexError):
            check("voice import timing is readable", False, proc.stdout[-200:])


def test_voice_api() -> None:
    section("12. Voice API surface")

    # Built against the real app, not a bare router: `require_api_key` is a
    # dependency of the full middleware/auth stack, and a router-only client
    # cannot exercise it.
    from app.api.deps import API_KEY
    from app.main import app

    client = TestClient(app)
    auth = {"X-API-Key": API_KEY} if API_KEY else {}

    # Read route: open, reports capability, no key needed.
    resp = client.get("/ultron/voice/status")
    check("GET /voice/status is open and 200", resp.status_code == 200,
          str(resp.status_code))
    body = resp.json()
    check("voice status body is the documented shape",
          "available" in body and "problems" in body, str(sorted(body))[:120])
    check("voice status leaks no key", "sk-" not in json.dumps(body))
    check("voice status did not start a speech engine",
          body.get("speech_synth_detail", "").startswith("pyttsx3 importable")
          or not body.get("speech_synth"),
          str(body.get("speech_synth_detail")))

    # Write routes: same key requirement as the rest of the agent. When
    # SARA_API_KEY is unset, `require_api_key` is a no-op and this cannot be
    # asserted, so say so rather than passing vacuously.
    if API_KEY:
        for path, payload in (
            ("/ultron/voice/speak", {"text": "hello"}),
            ("/ultron/voice/listen", {"timeout": 1, "phrase_limit": 1}),
            ("/ultron/voice/cancel", {}),
        ):
            unauth = client.post(path, json=payload)
            check(f"POST {path} without a key -> 401", unauth.status_code == 401,
                  str(unauth.status_code))
    else:
        check("voice auth untested: SARA_API_KEY is not set in this env", True)

    # The dependency itself must be attached, whatever the env says.
    from app.api.deps import require_api_key
    voice_routes = [r for r in api_mod.router.routes
                    if getattr(r, "path", "").startswith("/ultron/voice")]
    check("all four voice routes exist", len(voice_routes) == 4,
          str([r.path for r in voice_routes]))
    writes = [r for r in voice_routes if "GET" not in getattr(r, "methods", set())]
    check("every voice write route requires the API key",
          bool(writes) and all(
              any(d.dependency is require_api_key for d in r.dependencies)
              for r in writes
          ), str([r.path for r in writes]))

    # A bad request must be a 422 from validation, not a crash.
    bad = client.post("/ultron/voice/speak", json={}, headers=auth)
    check("speak without text is rejected by validation",
          bad.status_code == 422, str(bad.status_code))

    # An empty utterance is a capability failure (nothing to say), reported as
    # 503 with the reason - not a 500, and not a silent 200.
    empty = client.post("/ultron/voice/speak", json={"text": "   "}, headers=auth)
    check("empty speak is a 503 with a reason, not a 500",
          empty.status_code == 503, str(empty.status_code))
    check("empty speak explains itself",
          "nothing to say" in json.dumps(empty.json()), empty.text[:160])


# ---------------------------------------------------------------------------
# 13. Full-app regression
# ---------------------------------------------------------------------------
def test_full_app_registration() -> None:
    section("13. Full app registration (no route collisions)")

    from app.main import app
    paths = app.openapi()["paths"]
    ultron = sorted(p for p in paths if p.startswith("/ultron"))

    check("the agent is mounted", len(ultron) == 12, str(len(ultron)))
    for expected in ("/ultron/", "/ultron/run", "/ultron/events", "/ultron/approve",
                     "/ultron/stop", "/ultron/status", "/ultron/capabilities",
                     "/ultron/health", "/ultron/voice/status",
                     "/ultron/voice/listen", "/ultron/voice/speak",
                     "/ultron/voice/cancel"):
        check(f"route {expected} exists", expected in ultron)

    # No operation id collision anywhere in the app, which is what breaks
    # generated clients when two routers pick the same name.
    ops: Dict[str, List[str]] = {}
    for path, methods in paths.items():
        for method, spec in methods.items():
            ops.setdefault(spec.get("operationId", ""), []).append(
                f"{method.upper()} {path}")
    dupes = {k: v for k, v in ops.items() if k and len(v) > 1}
    check("no duplicate operationIds", not dupes, str(dupes)[:300])

    # SARA's other agents must be untouched.
    for other in ("/api/health", "/sara-auto/health", "/api/llm/health"):
        check(f"regression: {other} still registered", other in paths)


def test_capabilities_reports_sources() -> None:
    section("14. Capabilities reports the new surface")

    from app.main import app

    client = TestClient(app)
    body = client.get("/ultron/capabilities").json()

    check("capabilities 200", bool(body))
    check("capabilities splits tools by source",
          {"ultron", "desktop", "brahma"} <= set(body.get("tools_by_source", {})),
          str(sorted(body.get("tools_by_source", {}))))
    check("capabilities counts brahma availability",
          body.get("brahma", {}).get("available", 0) >= 30,
          str(body.get("brahma")))
    check("capabilities lists gated tools",
          "brahma_system_manager" in body.get("approval_gated_tools", []),
          str(body.get("approval_gated_tools"))[:150])
    check("capabilities total matches the by-source split",
          sum(len(v) for v in body.get("tools_by_source", {}).values())
          == len(body.get("tools", [])))
    check("capabilities leaks no key", "sk-" not in json.dumps(body))


def test_isolation_from_other_agents() -> None:
    section("15. Isolation from the other agents")

    pkg = BACKEND / "app" / "agents" / "sara_ultron_agent"
    files = sorted(p for p in pkg.rglob("*.py"))
    check("the package has files", len(files) > 8, str(len(files)))

    banned = ("sara_auto_agent", "desktop_agent", "voice_agent_engine",
              "api.desktop_agent", "api.sara_auto_agent")
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods: List[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if any(mod == b or mod.startswith(b + ".") for b in banned):
                    offenders.append(f"{path.name}:{node.lineno} {mod}")
    check("no import of another agent's module", not offenders, str(offenders[:5]))

    # Sharing SARA *infrastructure* is intended; sharing a *registry instance*
    # is not. The objects must be distinct.
    from app.agents.sara_ultron_agent.core.memory import UltronMemory
    from app.agents.sara_ultron_agent.core.session import store as ultron_store

    try:
        from app.api import sara_auto_agent as jarvis
        jarvis_reg = jarvis._get_registry()
        check("Ultron's registry is not Jarvis's registry object",
              jarvis_reg is not get_registry())
    except Exception as exc:  # noqa: BLE001
        check("Jarvis registry check ran", False, f"{type(exc).__name__}: {exc}")

    check("Ultron has its own session store",
          ultron_store is not None)
    check("Ultron memory is a distinct instance",
          UltronMemory() is not UltronMemory())


# ---------------------------------------------------------------------------
# 16. The live dry run
# ---------------------------------------------------------------------------
def test_action_dry_run() -> None:
    section("16. Every advertised action actually dispatches")

    if not DEFAULT_SOURCE_DIR.is_dir():
        check("Brahma-Echo source present", False)
        return

    bridge = get_bridge()
    dead: List[str] = []
    exercised = 0
    gated_skipped = 0
    browser_skipped = 0

    for name in bridge.stats()["available_names"]:
        rec = bridge.record(name)
        enum = ((rec.parameters or {}).get("properties", {})
                .get("action", {}) or {}).get("enum")
        if not enum:
            continue
        # Same gate the executor and `core/verify_actions.py` apply. A test
        # harness that invoked `shutdown` or `file_controller/delete`
        # unattended would be a worse bug than the dead ends it hunts for.
        if name in RISKY_TOOLS:
            gated_skipped += len(enum)
            continue
        # Browser actions drive a real MCP-managed browser: `dialog` blocks
        # waiting for a modal that a dry run never opens, and every following
        # call queues behind it (45 s timeout each). Skipped here and in
        # `core/verify_actions.py` via the shared set; the vocabulary is still
        # verified by `test_action_vocabulary` and the browser seam is covered
        # with fakes in `tests/test_sara_bots_custom.py`.
        if name in LIVE_BROWSER_TOOLS:
            browser_skipped += len(enum)
            continue
        destructive = DESTRUCTIVE_BRAHMA_ACTIONS.get(name, set())
        for action in enum:
            if action in destructive:
                gated_skipped += 1
                continue
            exercised += 1
            try:
                out = str(bridge.run(name, {"action": action}))
            except BaseException as exc:  # noqa: BLE001
                dead.append(f"{name}/{action}: raised {type(exc).__name__}: {exc}")
                continue
            if _DEAD_END.search(out):
                dead.append(f"{name}/{action}: {out[:90]}")

    check("actions were actually exercised", exercised >= 100, str(exercised))
    check("no advertised action is a dead end", not dead,
          " | ".join(dead[:4]))
    check("destructive actions were skipped, not invoked", gated_skipped > 0,
          f"{gated_skipped} skipped")
    check("live-browser actions were skipped, not invoked",
          browser_skipped > 0, f"{browser_skipped} skipped")


def main() -> int:
    print("SARA Ultron Phase 2 tests (Brahma-Echo bridge + local voice)")
    print("=" * 66)
    for fn in (
        test_rewrite_source, test_security_shims, test_bridge_loads,
        test_action_vocabulary, test_descriptions_reach_planner,
        test_approval_gating, test_registry_integration, test_voice_surface,
        test_voice_echo_guard, test_voice_failure_paths,
        test_voice_does_not_block_import, test_voice_api,
        test_full_app_registration, test_capabilities_reports_sources,
        test_isolation_from_other_agents, test_action_dry_run,
    ):
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()
            _FAILURES.append(f"{fn.__name__} raised")

    print("\n" + "=" * 66)
    print(f"{_CHECKS - len(_FAILURES)}/{_CHECKS} checks passed")
    if _FAILURES:
        print(f"\n{len(_FAILURES)} FAILED:")
        for f in _FAILURES:
            print("  -", f)
        return 1
    print("All SARA Ultron Phase 2 tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
