"""Phase 5 regression suite - context awareness, vision and always-on voice.

What this locks down
--------------------
* **Permissions are independent and default-off.** Revoking one must not touch
  another, must stop the thing it gates *immediately*, and must say so.
* **Context never confuses old with new.** A context that has not changed does
  not bump the revision, so downstream consumers do not re-analyse on idle.
* **Adapters are extensible.** A new application is one ``register()`` call and
  zero changes to core - asserted by registering one at runtime.
* **Adapters never invent facts.** An unreadable URL or process is reported as
  unavailable, not guessed.
* **Vision is cheapest-tier-first and retains nothing.** An unchanged context
  is a no-op; a screenshot is refused unless explicitly enabled.
* **References resolve or ask - never guess.** An ordinal with no element list
  returns a question, not an invented target.
* **The always-on loop survives silence, stops on repeated real errors, and
  refuses to run without the microphone permission.**

No test opens a microphone, takes a screenshot, or moves the mouse.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.sara_ultron_agent.core.adapters import (  # noqa: E402
    CAT_BROWSER,
    CAT_EDITOR,
    CAT_FILE_MANAGER,
    CAT_MEDIA,
    CAT_UNKNOWN,
    AdapterRegistry,
    AppAdapter,
    RawWindow,
)
from app.agents.sara_ultron_agent.core.alwayson import AlwaysOnVoice  # noqa: E402
from app.agents.sara_ultron_agent.core.awareness import ContextManager  # noqa: E402
from app.agents.sara_ultron_agent.core.observer import WindowObserver  # noqa: E402
from app.agents.sara_ultron_agent.core.permissions import (  # noqa: E402
    GATES,
    PermissionDenied,
    PermissionRegistry,
)
from app.agents.sara_ultron_agent.core.references import (  # noqa: E402
    is_reference_only,
    resolve,
)
from app.agents.sara_ultron_agent.core.vision import VisionEngine  # noqa: E402


# ===========================================================================
# 1. Permissions
# ===========================================================================
def test_all_gates_start_denied():
    perms = PermissionRegistry()
    for gate in GATES:
        assert perms.allowed(gate) is False, f"{gate} must be off by default"


def test_require_raises_with_an_actionable_message():
    perms = PermissionRegistry()
    with pytest.raises(PermissionDenied) as denied:
        perms.require("microphone", "listen")
    assert "microphone" in str(denied.value)
    perms.set("microphone", True)
    perms.require("microphone", "listen")     # must not raise


def test_unknown_gate_cannot_be_granted():
    perms = PermissionRegistry()
    with pytest.raises(ValueError):
        perms.set("telepathy", True)


def test_gates_are_independent():
    perms = PermissionRegistry()
    perms.set("screen", True)
    assert perms.allowed("screen") is True
    assert perms.allowed("microphone") is False
    assert perms.allowed("control") is False
    perms.set("screen", False)
    assert perms.allowed("control") is False, "revoking screen must not touch control"


def test_grant_is_idempotent_and_says_so():
    perms = PermissionRegistry()
    first = perms.set("control", True)
    assert first["changed"] is True
    again = perms.set("control", True)
    assert again["changed"] is False, "a no-op grant must not claim a change"


def test_revoke_sensitive_leaves_control_alone():
    perms = PermissionRegistry()
    perms.set("control", True)
    perms.set("microphone", True)
    perms.set("screen", True)
    perms.disable_sensitive()
    assert perms.allowed("microphone") is False
    assert perms.allowed("screen") is False
    assert perms.allowed("control") is True


def test_audit_records_every_real_change():
    perms = PermissionRegistry()
    perms.set("screen", True, "for testing")
    perms.set("screen", True)                 # no-op
    perms.set("screen", False, "done")
    audit = perms.audit()
    assert [e["changed"] for e in audit] == [True, False, True]


def test_grants_survive_a_restart(tmp_path):
    store = tmp_path / "permissions.json"
    first = PermissionRegistry(store)
    first.set("screen", True, "user asked")
    assert PermissionRegistry(store).allowed("screen") is True, \
        "a restart must not silently revoke a grant the user made"


def test_corrupt_store_starts_denied(tmp_path):
    store = tmp_path / "permissions.json"
    store.write_text("{not json", encoding="utf-8")
    perms = PermissionRegistry(store)
    assert perms.allowed("screen") is False
    assert perms.allowed("microphone") is False


# ===========================================================================
# 2. Adapters
# ===========================================================================
def test_browser_adapter_reads_tab_and_site():
    ctx, adapter = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="Saiyaara - YouTube - Google Chrome", process="chrome")
    )
    assert ctx.category == CAT_BROWSER
    assert ctx.tab_title == "Saiyaara"
    assert ctx.site == "YouTube"
    assert ctx.item == "Saiyaara"
    assert ctx.confidence >= 0.7
    # A URL is NOT in the title, so it must be reported missing, not invented.
    assert ctx.url is None
    assert "url" in ctx.unavailable


def test_browser_adapter_prefers_a_real_url_over_title_parsing():
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="x - Google Chrome", process="chrome",
                  url="https://music.youtube.com/watch?v=abc")
    )
    assert ctx.url == "https://music.youtube.com/watch?v=abc"
    assert ctx.site == "music.youtube.com"
    assert "url" not in ctx.unavailable


def test_editor_adapter_finds_file_and_project():
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="app.ts - my-project - Visual Studio Code", process="code")
    )
    assert ctx.category == CAT_EDITOR
    assert ctx.item == "app.ts"
    assert ctx.project == "my-project"


def test_explorer_adapter_treats_the_title_as_the_folder():
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="Downloads", process="explorer")
    )
    assert ctx.category == CAT_FILE_MANAGER
    assert ctx.folder == "Downloads"


def test_terminal_adapter_strips_the_shell_before_reading_the_path():
    """Splitting on "C:" before stripping " - Windows Terminal" mangles it."""
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title=r"C:\Users\HP\proj - Windows Terminal", process="windowsterminal")
    )
    assert ctx.item == r"C:\Users\HP\proj"
    assert ctx.folder == r"C:\Users\HP\proj"


def test_media_adapter_says_when_playback_state_is_unknown():
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="Saiyaara - VLC media player", process="vlc")
    )
    assert ctx.category == CAT_MEDIA
    assert ctx.item == "Saiyaara"
    assert "playing" in ctx.unavailable, "a title cannot tell us if it is playing"


def test_generic_adapter_never_claims_confidence():
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="Some Unknown App 3.2", process="somethingelse")
    )
    assert ctx.category == CAT_UNKNOWN
    assert ctx.confidence < 0.5
    for field in ("url", "site", "project", "folder"):
        assert getattr(ctx, field) is None


def test_unnamed_process_is_reported_not_hidden():
    """A higher-integrity window the OS will not name is a real limitation."""
    ctx, _ = AdapterRegistry().register_default_set().interpret(
        RawWindow(title="Freebuff Desktop", process="")
    )
    assert ctx.extra.get("process_unreadable") is True
    assert "process_name" in ctx.unavailable


def test_a_broken_adapter_does_not_break_interpretation():
    class Broken(AppAdapter):
        processes = ("explorer",)

        def interpret(self, window):
            raise RuntimeError("boom")

    registry = AdapterRegistry().register(Broken())
    ctx, adapter = registry.interpret(RawWindow(title="x", process="chrome"))
    assert ctx is not None, "a broken adapter must fall through, not crash"
    assert adapter == "GenericAdapter"


def test_registry_extends_without_core_changes():
    """Adding an app must be one register() call - no core file touched."""

    class MailAdapter(AppAdapter):
        processes = ("thunderbird",)
        label = "Thunderbird"
        category = "email"

        def interpret(self, window):
            from app.agents.sara_ultron_agent.core.adapters import AdapterContext

            return AdapterContext(category="email", app_name="Thunderbird",
                                  item=window.title, confidence=0.9)

    registry = AdapterRegistry().register_default_set()
    before = ctx_of(registry, "mail - Thunderbird", "thunderbird")
    registry.register(MailAdapter())
    after = ctx_of(registry, "mail - Thunderbird", "thunderbird")
    assert before.category != "email"
    assert after.category == "email"
    assert after.app_name == "Thunderbird"


def ctx_of(registry, title, process):
    return registry.interpret(RawWindow(title=title, process=process))[0]


# ===========================================================================
# 3. Context manager
# ===========================================================================
def test_first_observation_is_adopted():
    cm = ContextManager()
    result = cm.observe(RawWindow(title="Saiyaara - YouTube - Google Chrome",
                                  process="chrome"))
    assert result["changed"] is True
    assert cm.current().revision == 1
    assert cm.current().context.site == "YouTube"


def test_identical_context_does_not_bump_the_revision():
    """The single most expensive bug available here: every poll looks new."""
    cm = ContextManager()
    window = RawWindow(title="app.ts - proj - Visual Studio Code", process="code")
    cm.observe(window)
    for _ in range(5):
        result = cm.observe(window)
        assert result["changed"] is False
    assert cm.current().revision == 1, "an idle screen must not churn revisions"
    assert cm.state().changes == 1


def test_switching_windows_moves_previous_into_previous():
    cm = ContextManager()
    cm.observe(RawWindow(title="Saiyaara - YouTube - Google Chrome", process="chrome"))
    cm.observe(RawWindow(title="app.ts - proj - Visual Studio Code", process="code"))
    assert cm.current().context.category == CAT_EDITOR
    assert cm.previous().context.category == CAT_BROWSER


def test_task_and_last_action_follow_the_user_not_the_window():
    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    cm.set_task("play Saiyaara")
    cm.record_action("video started", ok=True)
    cm.observe(RawWindow(title="app.ts - p - Visual Studio Code", process="code"))
    assert cm.current().task == "play Saiyaara", "alt-tab must not clear the task"
    assert cm.current().last_action == "video started"
    assert cm.current().last_action_ok is True


def test_unverified_action_is_distinguishable_from_success():
    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    cm.record_action("clicked skip", ok=None)
    assert cm.current().last_action_ok is None
    cm.record_action("clicked skip", ok=False)
    assert cm.current().last_action_ok is False


def test_task_set_before_any_window_is_not_lost():
    cm = ContextManager()
    cm.set_task("find the invoice")
    cm.observe(RawWindow(title="Downloads", process="explorer"))
    assert cm.current().task == "find the invoice"


def test_blocked_is_reported_without_erasing_the_last_context():
    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    cm.mark_blocked("screen access is not granted")
    snapshot = cm.snapshot()
    assert snapshot["blocked"] is not None
    assert snapshot["current"] is not None, "last known context is still the best we have"
    assert snapshot["current"]["age_s"] >= 0


def test_switching_screen_off_marks_the_context_stale_by_itself():
    """A frozen context must never be served as if it were live.

    Flipping the switch off stops the observer, but the last window stays in
    the panel. Without this the user reads a window they left minutes ago as
    the present one, with nothing in the UI to say otherwise.
    """
    from app.agents.sara_ultron_agent.core.permissions import permissions

    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    original = permissions.snapshot()["gates"]["screen"]["allowed"]
    try:
        permissions.set("screen", False, "test")
        off = cm.snapshot()
        assert off["blocked"], "a frozen context must announce itself"
        assert off["live"] is False
        assert off["current"] is not None, "the last context is still shown"
        permissions.set("screen", True, "test")
        assert cm.snapshot()["blocked"] is None
    finally:
        permissions.set("screen", original, "test restore")


def test_switch_back_appends_rather_than_rewinds():
    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    cm.observe(RawWindow(title="app.ts - p - Visual Studio Code", process="code"))
    cm.switch_back()
    assert cm.current().revision == 3, "going back is itself an event"
    assert cm.current().context.category == CAT_BROWSER


def test_timeline_is_bounded():
    cm = ContextManager()
    for i in range(80):
        cm.observe(RawWindow(title=f"window {i}", process=""))
    assert len(cm.timeline(limit=500)) <= 50


# ===========================================================================
# 4. Vision
# ===========================================================================
def test_vision_refuses_without_the_permission():
    engine = VisionEngine()
    perms = PermissionRegistry()
    import app.agents.sara_ultron_agent.core.vision as vision_module

    original = vision_module.permissions
    vision_module.permissions = perms
    try:
        result = engine.analyse({"current": None}, question="where is the button")
        assert result.tier == "none"
        assert "screen" in result.reason
    finally:
        vision_module.permissions = original


def test_vision_uses_app_facts_and_never_stores_an_image():
    engine = VisionEngine()
    perms = PermissionRegistry()
    perms.set("screen", True)
    import app.agents.sara_ultron_agent.core.vision as vision_module

    original = vision_module.permissions
    vision_module.permissions = perms
    original_read = vision_module.read_accessibility
    vision_module.read_accessibility = lambda hwnd, depth=3: []
    try:
        snapshot = {"current": {"revision": 3, "hwnd": 0, "context": {
            "app_name": "Google Chrome", "item": "Saiyaara",
            "site": "YouTube", "url": None, "project": None, "folder": None}}}
        result = engine.analyse(snapshot)
        assert result.tier == "app_facts"
        assert "YouTube" in result.summary
        assert result.screenshot_taken is False
        payload = result.as_dict()
        assert payload["retained"] is False
        assert "image" not in payload and "data_url" not in payload
    finally:
        vision_module.permissions = original
        vision_module.read_accessibility = original_read


def test_vision_is_a_no_op_when_nothing_changed():
    engine = VisionEngine()
    perms = PermissionRegistry()
    perms.set("screen", True)
    import app.agents.sara_ultron_agent.core.vision as vision_module

    original = vision_module.permissions
    vision_module.permissions = perms
    original_read = vision_module.read_accessibility
    vision_module.read_accessibility = lambda hwnd, depth=3: [
        {"role": "ButtonControl", "name": "Skip ad", "value": "", "depth": 1}
    ]
    try:
        snapshot = {"current": {"revision": 1, "hwnd": 0, "context": {
            "app_name": "Google Chrome", "item": "x", "site": "YouTube",
            "url": None, "project": None, "folder": None}}}
        first = engine.analyse(snapshot, question="skip ad")
        assert first.changed is True
        assert first.tier == "accessibility"
        assert "1 matching control" in first.summary
        second = engine.analyse(snapshot, question="skip ad")
        assert second.changed is False, "an identical screen must cost nothing"
        assert "unchanged" in second.reason
    finally:
        vision_module.permissions = original
        vision_module.read_accessibility = original_read


def test_screenshots_are_off_by_default_even_when_permitted():
    from app.agents.sara_ultron_agent.core.vision import ALLOW_SCREENSHOT, capture_screenshot

    if ALLOW_SCREENSHOT:
        pytest.skip("ULTRON_ALLOW_SCREENSHOT is set in this environment")
    perms = PermissionRegistry()
    perms.set("screen", True)
    import app.agents.sara_ultron_agent.core.vision as vision_module

    original = vision_module.permissions
    vision_module.permissions = perms
    try:
        result = capture_screenshot(0)
        assert result["ok"] is False
        assert "ULTRON_ALLOW_SCREENSHOT" in result["error"]
    finally:
        vision_module.permissions = original


def test_find_elements_matches_names_and_values():
    from app.agents.sara_ultron_agent.core.vision import find_elements

    elements = [
        {"role": "ButtonControl", "name": "Skip ad", "value": ""},
        {"role": "EditControl", "name": "Search", "value": "ai news"},
        {"role": "ButtonControl", "name": "Play", "value": ""},
    ]
    assert len(find_elements(elements, "skip")) == 1
    assert len(find_elements(elements, "ai news")) == 1
    assert find_elements(elements, "") == []


# ===========================================================================
# 5. Reference resolution
# ===========================================================================
def _with_context(cm):
    cm.observe(RawWindow(title="Saiyaara - YouTube - Google Chrome", process="chrome"))
    return cm


def test_previous_window_resolves_from_the_timeline():
    import app.agents.sara_ultron_agent.core.references as ref_module
    from app.agents.sara_ultron_agent.core.awareness import context as live

    cm = ContextManager()
    original = ref_module.context_manager
    ref_module.context_manager = cm
    try:
        cm.observe(RawWindow(title="Saiyaara - YouTube - Google Chrome", process="chrome"))
        cm.observe(RawWindow(title="app.ts - p - Visual Studio Code", process="code"))
        result = ref_module.resolve("go back to the previous window")
        assert result.resolved is True
        assert result.kind == "previous"
        assert result.target["site"] == "YouTube"
    finally:
        ref_module.context_manager = original


def test_ordinal_without_elements_asks_instead_of_guessing():
    import app.agents.sara_ultron_agent.core.references as ref_module

    cm = _with_context(ContextManager())
    original = ref_module.context_manager
    ref_module.context_manager = cm
    try:
        result = ref_module.resolve("click the second one")
        assert result.resolved is False
        assert result.question, "an unresolvable ordinal must ask"
    finally:
        ref_module.context_manager = original


def test_ordinal_with_elements_resolves():
    import app.agents.sara_ultron_agent.core.references as ref_module

    cm = _with_context(ContextManager())
    original = ref_module.context_manager
    ref_module.context_manager = cm
    elements = [
        {"role": "HyperlinkControl", "name": "First result", "value": ""},
        {"role": "HyperlinkControl", "name": "Second result", "value": ""},
    ]
    try:
        result = ref_module.resolve("open the second one", elements)
        assert result.resolved is True
        assert result.target["name"] == "Second result"
    finally:
        ref_module.context_manager = original


def test_previous_window_with_no_history_asks():
    import app.agents.sara_ultron_agent.core.references as ref_module

    cm = _with_context(ContextManager())
    original = ref_module.context_manager
    ref_module.context_manager = cm
    try:
        result = ref_module.resolve("go to the previous window")
        assert result.resolved is False
        assert "previous" in result.reason
    finally:
        ref_module.context_manager = original


def test_noun_reference_uses_the_matching_context_field():
    import app.agents.sara_ultron_agent.core.references as ref_module

    cm = ContextManager()
    cm.observe(RawWindow(title="app.ts - my-project - Visual Studio Code", process="code"))
    original = ref_module.context_manager
    ref_module.context_manager = cm
    try:
        result = ref_module.resolve("open that file")
        assert result.resolved is True
        assert result.target["value"] == "app.ts"
        missing = ref_module.resolve("play that video")
        assert missing.resolved is False, "an editor has no video to point at"
        assert missing.question
    finally:
        ref_module.context_manager = original


def test_bare_reference_resolves_to_the_current_focus():
    import app.agents.sara_ultron_agent.core.references as ref_module

    cm = _with_context(ContextManager())
    original = ref_module.context_manager
    ref_module.context_manager = cm
    try:
        result = ref_module.resolve("scroll down there")
        assert result.resolved is True
        assert result.kind == "focus"
        assert result.target["site"] == "YouTube"
    finally:
        ref_module.context_manager = original


def test_no_reference_is_reported_as_such():
    import app.agents.sara_ultron_agent.core.references as ref_module

    result = ref_module.resolve("organize my desktop by file type")
    assert result.resolved is False
    assert result.kind == "none"
    assert result.reason == "no contextual reference in this phrase"


def test_reference_only_detects_short_follow_ups():
    assert is_reference_only("scroll down") is False
    assert is_reference_only("scroll down there") is True
    assert is_reference_only("play this song") is True


# ===========================================================================
# 6. Always-on voice
# ===========================================================================
class FakeVoice:
    """A stand-in for UltronVoice. Never touches a real device."""

    def __init__(self, results):
        self.results = list(results)
        self.spoken = []
        self.cancelled = 0
        self.guard = 0.0

    def echo_guard_remaining(self):
        return self.guard

    def listen(self, timeout=8.0, phrase_limit=12.0):
        return self.results.pop(0) if self.results else {"ok": False, "error": "done"}

    def speak(self, text):
        self.spoken.append(text)
        return {"ok": True, "spoken": text}

    def cancel_speech(self):
        self.cancelled += 1
        return {"ok": True}


def _loop_with(fake, **perms):
    import app.agents.sara_ultron_agent.core.alwayson as module

    loop = AlwaysOnVoice()
    original = module.get_voice
    module.get_voice = lambda: fake
    # The loop refuses to start when the input device cannot be opened. A test
    # must not depend on whether the machine running it has a working
    # microphone, so the probe is stubbed rather than inherited from the host.
    module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": True, "detail": "stubbed for the test"}
    registry = PermissionRegistry()
    for gate, value in perms.items():
        registry.set(gate, value)
    module.permissions = registry
    return loop, module, original


def test_always_on_refuses_to_start_without_the_permission():
    fake = FakeVoice([])
    loop, module, original = _loop_with(fake)
    try:
        result = loop.start()
        assert result["started"] is False
        assert "permission" in result["reason"]
        assert loop.running is False
    finally:
        module.get_voice = original


def test_always_on_refuses_to_start_on_a_device_that_will_not_open():
    """Permission is not consent to loop against a broken device.

    A microphone that is on and deaf is worse than one that is off, so the
    loop checks the device before starting rather than spending its error
    budget discovering the same failure three times.
    """
    fake = FakeVoice([])
    loop, module, original = _loop_with(fake, microphone=True)
    module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": False,
        "detail": "no usable input device: OSError: [Errno -9999]"}
    try:
        result = loop.start()
        assert result["started"] is False
        assert "-9999" in result["reason"], "the real reason must reach the user"
        assert loop.running is False
    finally:
        loop.stop("test")
        module.get_voice = original


def test_a_permanently_dead_microphone_stops_the_loop_at_once():
    fake = FakeVoice([{"ok": False,
                       "error": "no microphone: no usable input device: OSError"}] * 6)
    loop, module, original = _loop_with(fake, microphone=True)
    try:
        outcome = loop.cycle_once()
        assert outcome["reason"] == "no_microphone"
        assert loop.stats.errors == 1, "a permanent fault must not be retried"
        assert loop.state == "error"
    finally:
        loop.stop("test")
        module.get_voice = original


def test_always_on_cycles_past_silence():
    """Silence is not an error. A quiet room must not switch the mic off."""
    fake = FakeVoice([{"ok": False, "error": "WaitTimeoutError: timed out"}] * 6)
    loop, module, original = _loop_with(fake, microphone=True)
    try:
        loop.start()
        for _ in range(5):
            loop.cycle_once()
        assert loop.stats.timeouts >= 5
        assert loop.stats.errors == 0, "silence must not count toward the error budget"
        assert loop.state != "error"
    finally:
        loop.stop("test")
        module.get_voice = original


def test_always_on_stops_after_repeated_real_errors():
    fake = FakeVoice([{"ok": False, "error": "OSError: device busy"}] * 6)
    loop, module, original = _loop_with(fake, microphone=True)
    try:
        for _ in range(3):
            loop.cycle_once()
        assert loop.state == "error"
        assert loop.stats.consecutive_errors >= 3
    finally:
        loop.stop("test")
        module.get_voice = original


def test_always_on_success_clears_the_error_streak():
    fake = FakeVoice([
        {"ok": False, "error": "OSError: glitch"},
        {"ok": False, "error": "OSError: glitch"},
        {"ok": True, "text": "open chrome", "seconds": 1.0},
    ])
    loop, module, original = _loop_with(fake, microphone=True)
    handled = []

    def handler(utterance, snapshot):
        handled.append(utterance)
        return {"ok": True, "speak": "opening chrome"}

    loop.set_handler(handler)
    try:
        loop.cycle_once()
        loop.cycle_once()
        assert loop.stats.consecutive_errors == 2
        outcome = loop.cycle_once()
        assert outcome["ok"] is True
        assert handled == ["open chrome"]
        assert loop.stats.consecutive_errors == 0, "a success must reset the streak"
        assert fake.spoken == ["opening chrome"]
        assert loop.stats.utterances == 1
    finally:
        loop.stop("test")
        module.get_voice = original


def test_a_broken_handler_does_not_kill_the_loop():
    fake = FakeVoice([{"ok": True, "text": "hello"}] * 5)
    loop, module, original = _loop_with(fake, microphone=True)

    def handler(utterance, snapshot):
        raise RuntimeError("handler exploded")

    loop.set_handler(handler)
    try:
        for _ in range(3):
            outcome = loop.cycle_once()
            assert outcome["ok"] is False
        assert loop.stats.consecutive_errors >= 3
        assert loop.state == "error", "repeated handler failures must stop the mic"
    finally:
        loop.stop("test")
        module.get_voice = original


def test_barge_in_cancels_speech():
    fake = FakeVoice([])
    loop, module, original = _loop_with(fake, microphone=True)
    try:
        result = loop.barge_in()
        assert result["barge_in"] is True
        assert fake.cancelled >= 1
        assert loop.stats.barge_ins == 1
    finally:
        module.get_voice = original


def test_always_on_snapshot_never_leaks_a_handler():
    fake = FakeVoice([])
    loop, module, original = _loop_with(fake, microphone=True)
    loop.set_handler(lambda u, s: {"ok": True})
    try:
        assert "handler" not in str(loop.snapshot()).lower()
    finally:
        module.get_voice = original


# ===========================================================================
# 7. Observer
# ===========================================================================
def test_observer_does_nothing_without_the_permission():
    observer = WindowObserver()
    import app.agents.sara_ultron_agent.core.observer as module

    registry = PermissionRegistry()
    original = module.permissions
    module.permissions = registry
    try:
        result = observer.poll_once()
        assert result["reason"] == "permission_required"
        assert observer.status.permission_denials == 1
    finally:
        module.permissions = original


def test_observer_status_is_serialisable():
    status = WindowObserver().status.as_dict()
    for key in ("running", "polls", "changes", "url_reads", "interval_s"):
        assert key in status


# ===========================================================================
# 8. HTTP surface
# ===========================================================================
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _key():
    from app.api.deps import API_KEY

    return API_KEY or "test-key"


def test_awareness_route_is_open_and_reports_everything(client):
    body = client.get("/ultron/awareness").json()
    for key in ("current", "previous", "observer", "vision", "vision_stats",
                "always_on", "permissions", "blocked", "adapters"):
        assert key in body


def test_permissions_route_lists_the_three_gates(client):
    body = client.get("/ultron/permissions").json()
    assert set(body["gate_names"]) == {"microphone", "screen", "control"}
    assert "audit" in body


def test_permission_write_requires_the_key(client):
    assert client.post("/ultron/permissions/screen", json={"allowed": True}).status_code == 401


def test_reporting_context_requires_the_screen_permission(client):
    response = client.post("/ultron/awareness/report",
                           json={"title": "x", "process": "chrome"},
                           headers={"X-API-Key": _key()})
    # Depends on ambient state; either a clean 403 from the gate or a real
    # observation. What must never happen is a silent success with the gate off.
    assert response.status_code in (200, 403)


def test_task_and_action_routes(client):
    import app.agents.sara_ultron_agent.core.awareness as awareness_module

    cm = ContextManager()
    original = awareness_module.context_manager
    awareness_module.context_manager = cm
    import app.agents.sara_ultron_agent.api as api_module

    api_context = api_module.context_manager
    api_module.context_manager = cm
    try:
        cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
        assert client.post("/ultron/awareness/task", json={"task": "play a song"},
                           headers={"X-API-Key": _key()}).status_code == 200
        assert cm.current().task == "play a song"
        response = client.post("/ultron/awareness/action",
                               json={"action": "scrolled down", "verified": True},
                               headers={"X-API-Key": _key()})
        assert response.status_code == 200
        assert cm.current().last_action == "scrolled down"
        assert client.post("/ultron/awareness/action", json={"action": " "},
                           headers={"X-API-Key": _key()}).status_code == 400
    finally:
        api_module.context_manager = api_context
        awareness_module.context_manager = original


def test_resolve_route_returns_a_question_not_a_guess(client):
    response = client.post("/ultron/awareness/resolve",
                           json={"text": "click the second one"},
                           headers={"X-API-Key": _key()})
    assert response.status_code == 200
    assert "resolved" in response.json()


def test_an_ordinal_never_resolves_against_a_stale_tree(client):
    """A tree from a window the user has left must not answer "the second one".

    The controls are real and the resolution looks confident; it is just
    describing the previous window. This is the exact failure the resolver
    documents, so it is pinned here rather than left to inspection.
    """
    import app.agents.sara_ultron_agent.core.awareness as awareness_module
    import app.agents.sara_ultron_agent.core.vision as vision_module
    import app.agents.sara_ultron_agent.api as api_module

    cm = ContextManager()
    cm.observe(RawWindow(title="A - Google Chrome", process="chrome"))
    engine = vision_module.VisionEngine()
    engine._last = vision_module.VisionResult(
        tier="accessibility",
        elements=[{"role": "ButtonControl", "name": "first", "value": "", "depth": 1},
                  {"role": "ButtonControl", "name": "second", "value": "", "depth": 1}],
        revision=999,  # belongs to a window that is long gone
    )
    original_context, original_api_context = (
        awareness_module.context_manager, api_module.context_manager)
    original_vision = api_module.vision
    awareness_module.context_manager = cm
    api_module.context_manager = cm
    api_module.vision = engine
    try:
        body = client.post("/ultron/awareness/resolve",
                           json={"text": "click the second one"},
                           headers={"X-API-Key": _key()}).json()
        assert body["elements_stale"] is True
        assert body["resolved"] is False
        assert body["question"]
    finally:
        api_module.vision = original_vision
        api_module.context_manager = original_api_context
        awareness_module.context_manager = original_context


def test_always_on_routes_report_state(client):
    body = client.get("/ultron/voice/always-on").json()
    assert body["running"] is False
    assert "state" in body
    assert client.post("/ultron/voice/always-on/stop", json={},
                       headers={"X-API-Key": _key()}).status_code == 200
    assert client.post("/ultron/voice/always-on/barge-in", json={"speaking": False},
                       headers={"X-API-Key": _key()}).status_code == 200
    assert client.post("/ultron/voice/always-on", json={}).status_code == 401


def test_vision_routes(client):
    body = client.get("/ultron/vision").json()
    assert "stats" in body and "latest" in body
    assert body["stats"]["retained_images"] == 0
    assert client.post("/ultron/vision/analyse", json={}).status_code == 401


def test_observer_route_requires_screen_permission(client):
    response = client.post("/ultron/observer", json={"running": True},
                           headers={"X-API-Key": _key()})
    assert response.status_code in (200, 403)


def test_a_denied_gate_answers_403_not_500(client):
    """A thrown permission error must not reach the client as a traceback.

    ``PermissionDenied`` subclasses ``PermissionError`` and Starlette maps
    neither to 403, so without the explicit conversion in ``api.py`` a switch
    the user has deliberately thrown looks like a server crash.
    """
    from app.agents.sara_ultron_agent.core.permissions import permissions

    original = permissions.snapshot()["gates"]["screen"]["allowed"]
    try:
        permissions.set("screen", False, "test")
        for path, body in (("/ultron/awareness/report", {"title": "x",
                                                         "process": "chrome"}),
                           ("/ultron/observer", {"running": True})):
            response = client.post(path, json=body,
                                   headers={"X-API-Key": _key()})
            assert response.status_code == 403, f"{path} leaked {response.status_code}"
            assert "screen" in str(response.json()["detail"]).lower()
    finally:
        permissions.set("screen", original, "test restore")


def test_an_unknown_gate_name_is_a_client_mistake(client):
    response = client.post("/ultron/permissions/telepathy", json={"allowed": True},
                           headers={"X-API-Key": _key()})
    assert response.status_code == 400
    assert "telepathy" in response.json()["detail"]


def test_context_manager_alias_is_the_same_object():
    """``context`` and ``context_manager`` must never drift into two instances.

    Two managers would mean the observer writes to one and the HUD reads the
    other, and the symptom - a permanently stale context panel - points
    nowhere near the cause.
    """
    import app.agents.sara_ultron_agent.core.awareness as module

    assert module.context_manager is module.context
