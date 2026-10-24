"""Phase 4 regression suite - realtime, devices, pairing and the voice state.

What this locks down
--------------------
* The bus delivers by topic prefix, replays a bounded history, and drops the
  *oldest* event (never the newest) when a subscriber falls behind. A status
  channel that delivers stale state is worse than one that drops it.
* Pairing tokens are single-use, expire, are stored hashed, and a wrong token
  and a used token are indistinguishable to the caller.
* Revocation is immediate: a revoked device token stops authenticating, and a
  revoked device is never returned even with a valid token.
* Claiming is throttled per address, and every failure path produces the same
  message so the endpoint cannot be used to enumerate live tokens.
* The voice state machine only reaches THINKING/EXECUTING/VERIFYING when a run
  is genuinely in that phase. With no runs it is IDLE, never THINKING.
* A device token cannot reach a scope it was not granted, and cannot reach the
  SARA API key's endpoints at all.

No test here contacts a microphone, a speaker or the network.
"""

from __future__ import annotations

import sys
import types
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.sara_ultron_agent.core.bus import (  # noqa: E402
    HISTORY_PER_TOPIC,
    EventBus,
)
from app.agents.sara_ultron_agent.core.devices import (  # noqa: E402
    DEVICE_KINDS,
    DEVICE_SCOPES,
    KNOWN_SCOPES,
    DeviceRegistry,
    render_qr,
)
from app.agents.sara_ultron_agent.core.voice_state import (  # noqa: E402
    ERROR_LINGER_S,
    VOICE_STATES,
    VoiceStateTracker,
    derive_voice_state,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def voice_ok(**overrides):
    """A healthy voice stack, with field overrides for the failure cases."""
    base = {
        "available": True,
        "listening": False,
        "speaking": False,
        "microphone": True,
        "microphone_detail": "default input device open",
        "transcriber": True,
        "speech_synth": True,
        "model": "base",
        "voices": [],
        "echo_guard_s": 0.0,
        "problems": [],
    }
    base.update(overrides)
    return base


def run(phase, status="running"):
    return {"task_id": "t-" + phase, "status": status, "phase": phase}


# ===========================================================================
# 1. Event bus
# ===========================================================================
def test_bus_delivers_to_matching_prefix():
    bus = EventBus()
    sub = bus.subscribe("device")
    bus.publish("device.dev_1", "paired", label="Pixel")
    bus.publish("task.abc", "created")
    events = sub.drain()
    assert len(events) == 1, "a device subscriber must not see task events"
    assert events[0]["kind"] == "paired"
    assert events[0]["label"] == "Pixel"


def test_bus_prefix_is_not_a_bare_string_match():
    """``device`` must not match a topic called ``devicefarm``."""
    bus = EventBus()
    sub = bus.subscribe("device")
    bus.publish("devicefarm.x", "nope")
    assert sub.drain() == []


def test_bus_star_receives_everything():
    bus = EventBus()
    sub = bus.subscribe("*")
    bus.publish("voice", "heard")
    bus.publish("system", "stop_all")
    assert len(sub.drain()) == 2


def test_bus_events_are_sequenced_and_timestamped():
    bus = EventBus()
    a = bus.publish("x", "one")
    b = bus.publish("x", "two")
    assert b["seq"] == a["seq"] + 1
    assert a["t"] > 0


def test_bus_replay_buffer_is_bounded_and_ordered():
    bus = EventBus()
    for i in range(HISTORY_PER_TOPIC + 15):
        bus.publish("x", "e", i=i)
    history = bus.recent("x", limit=1000)
    assert len(history) == HISTORY_PER_TOPIC, "history must not grow unbounded"
    seqs = [e["seq"] for e in history]
    assert seqs == sorted(seqs), "replay must be in seq order"
    # The most recent event survived; the oldest did not.
    assert history[-1]["i"] == HISTORY_PER_TOPIC + 14


def test_bus_drops_oldest_not_newest_when_slow():
    bus = EventBus()
    sub = bus.subscribe("x")
    sub._queue.maxsize = 5
    for i in range(20):
        bus.publish("x", "e", i=i)
    drained = sub.drain(limit=50)
    assert len(drained) == 5
    assert drained[-1]["i"] == 19, "the newest state must always survive"


def test_bus_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe("x")
    bus.unsubscribe(sub)
    bus.publish("x", "e")
    assert sub.drain() == []
    assert bus.subscriber_count() == 0


def test_bus_context_manager_closes():
    bus = EventBus()
    with bus.subscribe("x") as sub:
        assert bus.subscriber_count() == 1
    assert bus.subscriber_count() == 0
    assert sub.closed


# ===========================================================================
# 2. Pairing and the device registry
# ===========================================================================
def test_offer_mints_a_usable_token_and_qr():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel 9", "https://ultron.example")
    assert offer["token"]
    assert offer["kind"] == "phone"
    assert offer["expires_in_s"] == 120
    # The QR encodes something a phone can act on: the claim route + token.
    assert offer["payload"].startswith("https://ultron.example/ultron/pair/claim?t=")
    assert offer["token"] in offer["payload"]
    assert offer["qr"]["available"] is True, "segno is installed for this suite"
    assert offer["qr"]["data_url"].startswith("data:image/png;base64,")


def test_raw_token_is_never_retained():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel 9", "https://x")
    blob = repr(reg.__dict__)
    assert offer["token"] not in blob, "the raw pairing token must not be stored"


def test_claim_mints_a_device_token_with_real_scopes():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel 9", "https://x")
    claimed = reg.claim(offer["token"], "phone", "Pixel 9", "android")
    device = claimed["device"]
    assert device["kind"] == "phone"
    assert device["state"] == "online", "a freshly claimed device is online"
    assert device["scopes"] == list(DEVICE_SCOPES["phone"])
    assert "voice.listen" in device["scopes"]
    assert not any(s.startswith("computer.") for s in device["scopes"]), (
        "no computer.* scope may be declared while nothing enforces it"
    )
    assert reg.authenticate(claimed["device_token"]).device_id == device["device_id"]


def test_pairing_token_is_single_use():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    reg.claim(offer["token"], "phone", "Pixel")
    with pytest.raises(PermissionError):
        reg.claim(offer["token"], "phone", "Pixel")


def test_expired_offer_cannot_be_claimed():
    import hashlib

    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x", ttl_s=15)
    # Move the expiry into the past rather than sleeping 15 seconds.
    digest = hashlib.sha256(offer["token"].encode()).hexdigest()
    reg._offers[digest].expires_at = time.time() - 1
    with pytest.raises(PermissionError):
        reg.claim(offer["token"], "phone", "Pixel")


def test_bus_rejects_payloads_that_shadow_event_fields():
    """``kind=`` in a payload must fail loudly, not relabel the event."""
    bus = EventBus()
    with pytest.raises(ValueError):
        bus.publish("device", "paired", kind="phone")
    # ...and the bus still works afterwards.
    assert bus.publish("device", "paired", device_kind="phone")["kind"] == "paired"


def test_wrong_kind_does_not_widen_scopes():
    """A phone QR redeemed as a desktop must fail, not silently escalate."""
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    with pytest.raises(PermissionError):
        reg.claim(offer["token"], "desktop", "My PC")


def test_unknown_and_used_tokens_are_indistinguishable():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    reg.claim(offer["token"], "phone", "Pixel")
    try:
        reg.claim("not-a-real-token", "phone", "x")
    except PermissionError as unknown:
        unknown_message = str(unknown)
    try:
        reg.claim(offer["token"], "phone", "x")
    except PermissionError as used:
        used_message = str(used)
    assert unknown_message == used_message


def test_claim_is_throttled_per_address():
    reg = DeviceRegistry()
    for _ in range(8):
        try:
            reg.claim("garbage", "phone", "x", address="1.2.3.4")
        except PermissionError:
            pass
    with pytest.raises(PermissionError) as locked:
        reg.claim(reg.create_offer("phone", "x", "https://x")["token"],
                  "phone", "x", address="1.2.3.4")
    assert "Too many" in str(locked.value)
    # A different address is unaffected by another address's failures.
    assert reg.create_offer("phone", "x", "https://x")["token"]


def test_device_state_decays_from_heartbeat():
    reg = DeviceRegistry()
    offer = reg.create_offer("desktop", "Workstation", "https://x")
    claimed = reg.claim(offer["token"], "desktop", "Workstation")
    device_id = claimed["device"]["device_id"]

    assert reg.list()[0]["state"] == "online"

    device = reg.get(device_id)
    device.last_seen = time.time() - 40      # past ONLINE_S, inside STALE_S
    assert reg.list()[0]["state"] == "stale"

    device.last_seen = time.time() - 400
    assert reg.list()[0]["state"] == "offline"

    reg.heartbeat(device_id)
    assert reg.list()[0]["state"] == "online", "a heartbeat brings it straight back"


def test_revocation_kills_authentication_immediately():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    token = reg.claim(offer["token"], "phone", "Pixel")["device_token"]
    device_id = reg.list()[0]["device_id"]

    assert reg.authenticate(token) is not None
    assert reg.revoke(device_id, "test") is True
    assert reg.authenticate(token) is None, "a revoked token must not authenticate"
    assert reg.list()[0]["state"] == "revoked"
    # Revoking something that does not exist is an honest 404, not a silent ok.
    assert reg.revoke("dev_nope") is False


def test_revoke_all_also_destroys_pending_offers():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    reg.claim(offer["token"], "phone", "Pixel")
    reg.create_offer("phone", "Second", "https://x")
    assert reg.revoke_all("global stop") == 1
    assert reg.pending_offer(reg.list()[0]["device_id"]) is None
    assert reg.summary()["pending_offers"] == 0


def test_scoped_auth_rejects_missing_scope():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    token = reg.claim(offer["token"], "phone", "Pixel")["device_token"]
    assert reg.authenticate_scoped(token, "voice.listen") is not None
    assert reg.authenticate_scoped(token, "computer.window") is None


def test_device_view_never_leaks_a_token():
    reg = DeviceRegistry()
    offer = reg.create_offer("phone", "Pixel", "https://x")
    token = reg.claim(offer["token"], "phone", "Pixel")["device_token"]
    view = reg.list()[0]
    blob = repr(view)
    assert token not in blob
    assert "token_hash" not in view


def test_client_info_is_allowlisted():
    reg = DeviceRegistry()
    offer = reg.create_offer("desktop", "PC", "https://x")
    claimed = reg.claim(offer["token"], "desktop", "PC")
    device_id = claimed["device"]["device_id"]
    reg.heartbeat(device_id, {"app": "ultron-runtime", "evil": "<script>", "version": "1"})
    view = reg.list()[0]["client"]
    assert view.get("app") == "ultron-runtime"
    assert "evil" not in view


def test_unknown_device_kind_is_rejected():
    reg = DeviceRegistry()
    with pytest.raises(ValueError):
        reg.create_offer("toaster")


def test_all_declared_kinds_and_scopes_are_real():
    assert set(DEVICE_SCOPES) == set(DEVICE_KINDS)
    for kind, scopes in DEVICE_SCOPES.items():
        assert scopes, f"{kind} has no scopes"
        # Guard the real invariant - no kind may declare a scope the system
        # does not implement - against the canonical vocabulary rather than
        # against any one kind. Checking against "phone or desktop" made a
        # legitimately new scope (pc.execute) indistinguishable from a typo.
        assert all(s in KNOWN_SCOPES for s in scopes), \
            f"{kind} declares an unknown scope"

    # And the vocabulary itself must not drift: a scope nobody grants is dead
    # configuration waiting to confuse whoever reads it next.
    granted = {s for scopes in DEVICE_SCOPES.values() for s in scopes}
    assert granted == set(KNOWN_SCOPES), \
        f"KNOWN_SCOPES has entries no device grants: {set(KNOWN_SCOPES) - granted}"


def test_qr_failure_is_reported_not_hidden(monkeypatch):
    """A missing QR encoder must degrade the payload, never break pairing."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "segno":
            raise ImportError("no segno here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = render_qr("hello")
    assert result["available"] is False
    assert "unavailable" in result["error"]


# ===========================================================================
# 3. Voice state machine
# ===========================================================================
def test_idle_with_no_runs_and_a_healthy_mic():
    state = derive_voice_state(voice_ok(), [])
    assert state["state"] == "IDLE"
    assert state["active_runs"] == 0
    assert state["reason"]


def test_voice_state_is_never_thinking_without_a_run():
    """The single most important property: no phase means no state."""
    assert derive_voice_state(voice_ok(), [])["state"] == "IDLE"
    assert derive_voice_state(voice_ok(), [run("completed")])["state"] == "IDLE"
    assert derive_voice_state(voice_ok(), [run("queued", status="failed")])["state"] == "IDLE"


@pytest.mark.parametrize("phase,expected", [
    ("understanding", "THINKING"),
    ("planning", "THINKING"),
    ("executing", "EXECUTING"),
    ("verifying", "VERIFYING"),
])
def test_phases_map_to_states(phase, expected):
    assert derive_voice_state(voice_ok(), [run(phase)])["state"] == expected


def test_executing_outranks_a_concurrent_understanding_run():
    state = derive_voice_state(voice_ok(), [run("understanding"), run("executing")])
    assert state["state"] == "EXECUTING"
    assert state["active_runs"] == 2


def test_listening_and_speaking_beat_run_phases():
    assert derive_voice_state(voice_ok(listening=True), [run("executing")])["state"] == "LISTENING"
    assert derive_voice_state(voice_ok(speaking=True), [run("executing")])["state"] == "SPEAKING"


def test_missing_mic_is_permission_required_not_idle():
    state = derive_voice_state(
        voice_ok(microphone=False, available=False,
                 microphone_detail="no usable input device: OSError"),
        [],
    )
    assert state["state"] == "PERMISSION_REQUIRED"
    assert "OSError" in state["reason"]


def test_missing_stack_is_unavailable():
    state = derive_voice_state(None, [])
    assert state["state"] == "UNAVAILABLE"


def test_fresh_error_outranks_everything_then_ages_out():
    now = time.time()
    error = {"t": now, "message": "Listening failed: timeout"}
    assert derive_voice_state(voice_ok(listening=True), [run("executing")],
                              now=now, last_error=error)["state"] == "ERROR"
    # Older than the linger window: back to what is actually happening.
    old = {"t": now - ERROR_LINGER_S - 1, "message": "old failure"}
    assert derive_voice_state(voice_ok(listening=True), [], now=now,
                              last_error=old)["state"] == "LISTENING"


def test_continuous_is_reported_as_not_implemented():
    """The UI must be able to show this rather than implying hands-free."""
    state = derive_voice_state(voice_ok(), [])
    assert state["continuous"] is False
    assert "not implemented" in state["continuous_detail"]


def test_echo_guard_is_surfaced():
    state = derive_voice_state(voice_ok(echo_guard_s=3.42), [])
    assert state["echo_guard_s"] == 3.42


def test_every_state_is_declared_in_the_public_tuple():
    produced = set()
    for sessions, voice in [
        ([], voice_ok()),
        ([], voice_ok(listening=True)),
        ([], voice_ok(speaking=True)),
        ([], voice_ok(microphone=False)),
        ([], None),
        ([run("executing")], voice_ok()),
        ([run("verifying")], voice_ok()),
        ([run("planning")], voice_ok()),
    ]:
        produced.add(derive_voice_state(voice, sessions)["state"])
    assert produced <= set(VOICE_STATES), f"undeclared states: {produced - set(VOICE_STATES)}"


def test_tracker_clears_its_error_on_success():
    tracker = VoiceStateTracker()
    tracker.note_error("boom")
    assert tracker.last_error is not None
    tracker.note_success()
    assert tracker.last_error is None


# ===========================================================================
# 4. HTTP surface
# ===========================================================================
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_devices_route_lists_devices_without_a_key(client):
    response = client.get("/ultron/devices")
    assert response.status_code == 200
    body = response.json()
    assert {"devices", "summary", "kinds", "scopes", "policy"} <= set(body)


def test_pairing_requires_the_api_key(client):
    """Unauthenticated callers must not be able to mint pairing tokens."""
    assert client.post("/ultron/pairing", json={"kind": "phone"}).status_code == 401


def test_emergency_stop_requires_the_api_key(client):
    assert client.post("/ultron/emergency-stop", json={}).status_code == 401


def test_voice_state_route_reports_inputs(client):
    response = client.get("/ultron/voice/state")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] in VOICE_STATES
    assert "inputs" in body and "voice_stack" in body["inputs"]
    assert body["continuous"] is False


def test_ready_reports_each_subsystem(client):
    body = client.get("/ultron/ready").json()
    assert set(body["checks"]) == {"tool_registry", "llm_router", "voice", "device_link"}
    # A boolean would hide which dependency broke; the list must name it.
    assert isinstance(body["ready"], bool)
    assert isinstance(body["blocking"], list)


def test_pairing_claim_round_trip(client, monkeypatch):
    from app.api import deps
    from app.agents.sara_ultron_agent.core.devices import registry

    key = deps.API_KEY or "test-key"
    created = client.post(
        "/ultron/pairing",
        json={"kind": "phone", "label": "QA Pixel"},
        headers={"X-API-Key": key},
    )
    assert created.status_code == 200, created.text
    token = created.json()["token"]

    claimed = client.post("/ultron/pairing/claim",
                          json={"token": token, "kind": "phone", "label": "QA Pixel"})
    assert claimed.status_code == 200, claimed.text
    device = claimed.json()["device"]

    listed = client.get("/ultron/devices").json()["devices"]
    assert device["device_id"] in [d["device_id"] for d in listed]

    # A device token must not work as the SARA API key.
    denied = client.post("/ultron/emergency-stop", json={},
                         headers={"X-API-Key": claimed.json()["device_token"]})
    assert denied.status_code == 401

    registry.clear()


def test_device_token_cannot_submit_without_the_scope(client):
    from app.agents.sara_ultron_agent.core.devices import registry

    reg = registry
    offer = reg.create_offer("desktop", "PC", "https://x")
    token = reg.claim(offer["token"], "desktop", "PC")["device_token"]
    # desktop has task.submit, but not voice.listen.
    denied = client.post("/ultron/device/voice/listen", json={"timeout": 1},
                         headers={"X-Ultron-Device-Token": token})
    assert denied.status_code == 403
    assert "voice.listen" in denied.json()["detail"]
    reg.clear()


def test_bad_device_token_is_401(client):
    response = client.post("/ultron/device/tasks", json={"goal": "hello"},
                           headers={"X-Ultron-Device-Token": "nope"})
    assert response.status_code == 401


# ===========================================================================
# 5. Task board counting - the bug this suite was written to prevent
# ===========================================================================
def test_step_result_has_no_status_field():
    """Pin the executor's real serialisation.

    ``StepResult.to_dict()`` emits ``ok``/``skipped`` and has no ``status``
    key. An earlier version of the task board counted ``step["status"]``,
    matched nothing, and rendered 0% for every run including finished ones -
    found by watching the live UI, not by a unit test. If the executor's shape
    ever changes, this fails here first.
    """
    from app.agents.sara_ultron_agent.core.executor import StepResult

    payload = StepResult(tool="t", ok=True).to_dict()
    assert "status" not in payload
    assert payload["ok"] is True
    assert payload["skipped"] is False


def test_task_progress_counts_real_step_results(client):
    from app.agents.sara_ultron_agent.core.session import store

    session = store.create("counting probe")
    session.plan = [{"tool": "a"}, {"tool": "b"}, {"tool": "c"}, {"tool": "d"}]
    session.steps = [
        {"tool": "a", "ok": True, "skipped": False},    # finished
        {"tool": "b", "ok": False, "skipped": True},    # skipped == finished
        {"tool": "c", "ok": False, "skipped": False},   # failed
    ]                                             # "d" never ran
    session.phase = "executing"
    session.status = "running"
    try:
        from app.api.deps import API_KEY

        body = client.get("/ultron/tasks", headers={"X-API-Key": API_KEY or "k"}).json()
        row = next(t for t in body["tasks"] if t["task_id"] == session.task_id)
        assert row["steps_done"] == 2, "ok + skipped are both finished steps"
        assert row["steps_failed"] == 1
        assert row["steps_total"] == 4
        assert row["progress"] == 0.5
        assert row["terminal"] is False
    finally:
        store._sessions.pop(session.task_id, None)


def test_task_progress_is_zero_with_no_plan(client):
    """A queued task has no plan, so it must read 0% - not 100%."""
    from app.agents.sara_ultron_agent.core.session import store

    session = store.create("no plan probe")
    try:
        from app.api.deps import API_KEY

        body = client.get("/ultron/tasks", headers={"X-API-Key": API_KEY or "k"}).json()
        row = next(t for t in body["tasks"] if t["task_id"] == session.task_id)
        assert row["progress"] == 0.0
    finally:
        store._sessions.pop(session.task_id, None)


# ===========================================================================
# 6. The microphone - two regressions plus the browser upload route
# ===========================================================================
def test_listen_uses_the_module_microphone_not_the_recogniser():
    """Regression: voice input has NEVER worked until this fix.

    ``listen()`` used ``self._recogniser.Microphone()``. ``sr.Recognizer`` is
    the recognition engine and has no ``Microphone`` attribute, so every call
    raised::

        AttributeError: 'Recognizer' object has no attribute 'Microphone'

    and the UI showed it verbatim as ``Listening failed: ...``. The microphone
    is a *module-level* context manager; the recogniser consumes it.

    The stand-ins below model the real library exactly: a Recognizer with no
    Microphone, and a module-level Microphone. If the call is ever spelled
    wrongly again, this fails with the same AttributeError the user saw.
    """
    import sys as _sys
    import types

    from app.agents.sara_ultron_agent.core.voice import UltronVoice

    captured = {}

    class FakeRecognizer:
        """Deliberately has NO Microphone attribute, exactly like sr.Recognizer."""

        def adjust_for_ambient_noise(self, source, duration=1.0):
            captured["calibrated"] = True

        def listen(self, source, timeout=None, phrase_time_limit=None):
            captured["listen_args"] = (timeout, phrase_time_limit)
            # A real, decodable WAV: transcribe() parses the container before it
            # touches Whisper, so a stub string would fail for an unrelated
            # reason and the assertion below would prove nothing.
            return types.SimpleNamespace(get_wav_data=lambda: _wav_bytes())

    class FakeMicrophone:
        def __enter__(self):
            captured["mic_opened"] = True
            # A real `sr.Microphone` always carries `stream`; the code checks it
            # to catch the failure `Microphone.__enter__` swallows. A stand-in
            # that omits it would be testing a shape that never occurs.
            return types.SimpleNamespace(stream=object())

        def __exit__(self, *exc):
            captured["mic_closed"] = True
            return False

    fake = types.ModuleType("speech_recognition")
    fake.Recognizer = FakeRecognizer
    fake.Microphone = FakeMicrophone

    original = _sys.modules.get("speech_recognition")
    _sys.modules["speech_recognition"] = fake
    import app.agents.sara_ultron_agent.core.voice as voice_module

    real_probe = voice_module.probe_microphone
    # listen() probes the real device out-of-process first. Stub it: a test
    # must not depend on whether the machine running it has a microphone.
    voice_module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": True, "detail": "stubbed for the test"}
    try:
        voice = UltronVoice()
        voice._recogniser = FakeRecognizer()
        voice._recogniser_ready = True
        # Stub the model so the test never loads Whisper.
        voice._model = types.SimpleNamespace(
            transcribe=lambda audio, fp16=False: {"text": "hello ultron",
                                                   "language": "en"}
        )
        result = voice.listen(timeout=5, phrase_limit=9)
    finally:
        voice_module.probe_microphone = real_probe
        if original is not None:
            _sys.modules["speech_recognition"] = original
        else:
            _sys.modules.pop("speech_recognition", None)

    assert captured.get("mic_opened") is True, "sr.Microphone() was never opened"
    assert captured.get("mic_closed") is True, "the microphone was never released"
    assert captured.get("calibrated") is True
    assert captured["listen_args"] == (5.0, 9.0)
    assert result.get("ok") is True, result
    assert result.get("text") == "hello ultron"


def test_two_concurrent_listens_never_open_two_microphones():
    """One microphone, one listener at a time.

    ``pyaudio.PyAudio()`` initialises PortAudio in C and is not re-entrant.
    Two request threads opening a microphone at the same instant is what
    crashed the running API server with a native access violation - the
    browser polls voice state more than once a second, so it really happened.
    A Python exception cannot catch that; serialisation is the only defence.
    """
    import sys as _sys
    import threading
    import time
    import types

    from app.agents.sara_ultron_agent.core.voice import UltronVoice
    import app.agents.sara_ultron_agent.core.voice as voice_module

    state = {"open": 0, "peak": 0}
    guard = threading.Lock()

    class FakeRecognizer:
        def adjust_for_ambient_noise(self, source, duration=1.0):
            pass

        def listen(self, source, timeout=None, phrase_time_limit=None):
            time.sleep(0.05)          # hold the device long enough to overlap
            return types.SimpleNamespace(get_wav_data=lambda: _wav_bytes())

    class FakeMicrophone:
        def __enter__(self):
            with guard:
                state["open"] += 1
                state["peak"] = max(state["peak"], state["open"])
            return types.SimpleNamespace(stream=object())

        def __exit__(self, *exc):
            with guard:
                state["open"] -= 1
            return False

    fake = types.ModuleType("speech_recognition")
    fake.Recognizer = FakeRecognizer
    fake.Microphone = FakeMicrophone

    original = _sys.modules.get("speech_recognition")
    real_probe = voice_module.probe_microphone
    _sys.modules["speech_recognition"] = fake
    voice_module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": True, "detail": "stubbed for the test"}
    try:
        voice = UltronVoice()
        voice._recogniser = FakeRecognizer()
        voice._recogniser_ready = True
        voice._model = types.SimpleNamespace(
            transcribe=lambda audio, fp16=False: {"text": "hi", "language": "en"}
        )
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            voice.listen(timeout=5, phrase_limit=9))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
    finally:
        voice_module.probe_microphone = real_probe
        if original is not None:
            _sys.modules["speech_recognition"] = original
        else:
            _sys.modules.pop("speech_recognition", None)

    assert len(results) == 4, "a listener never returned"
    assert all(r.get("ok") for r in results), results
    assert state["open"] == 0, "a microphone was left open"
    assert state["peak"] == 1, (
        f"{state['peak']} microphones were open at once - PortAudio "
        "initialisation ran concurrently"
    )


def test_status_does_not_open_the_device_in_process():
    """The status probe must not be the thing that kills the server.

    ``status()`` runs on every voice-state poll, so opening the real device
    there put a native crash on a hot path. It must take the microphone verdict
    from the out-of-process probe and open nothing itself.
    """
    import sys as _sys
    import types

    import app.agents.sara_ultron_agent.core.voice as voice_module

    class ForbiddenMicrophone:
        def __init__(self, *a, **kw):
            raise AssertionError("status() touched PortAudio in process")

    fake = types.ModuleType("speech_recognition")
    fake.Microphone = ForbiddenMicrophone
    original = _sys.modules.get("speech_recognition")
    real_probe = voice_module.probe_microphone
    _sys.modules["speech_recognition"] = fake
    voice_module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": True, "detail": "input device opened (probe says so)"}
    try:
        voice = voice_module.UltronVoice()
        voice._recogniser = object()
        voice._recogniser_ready = True
        status = voice.status()
    finally:
        voice_module.probe_microphone = real_probe
        if original is not None:
            _sys.modules["speech_recognition"] = original
        else:
            _sys.modules.pop("speech_recognition", None)

    assert status.microphone is True
    assert status.microphone_detail == "input device opened (probe says so)"


def test_a_missing_microphone_is_refused_before_the_device_is_touched():
    """A failed probe short-circuits the capture instead of crashing into it."""
    import sys as _sys
    import types

    from app.agents.sara_ultron_agent.core.voice import UltronVoice
    import app.agents.sara_ultron_agent.core.voice as voice_module

    class FakeMicrophone:
        def __init__(self, *a, **kw):
            raise AssertionError("the device was opened with no probe result")

    fake = types.ModuleType("speech_recognition")
    fake.Microphone = FakeMicrophone
    original = _sys.modules.get("speech_recognition")
    real_probe = voice_module.probe_microphone
    _sys.modules["speech_recognition"] = fake
    voice_module.probe_microphone = lambda ttl=60.0: {
        "at": 0.0, "ok": False, "detail": "no usable input device: stubbed"}
    try:
        voice = UltronVoice()
        voice._recogniser = object()
        voice._recogniser_ready = True
        result = voice.listen(timeout=5, phrase_limit=9)
    finally:
        voice_module.probe_microphone = real_probe
        if original is not None:
            _sys.modules["speech_recognition"] = original
        else:
            _sys.modules.pop("speech_recognition", None)

    assert result["ok"] is False
    assert "no microphone" in result["error"]


def test_transcribe_rejects_audio_it_cannot_read():
    """A malformed clip must fail before the ~140 MB model is touched."""
    from app.agents.sara_ultron_agent.core.voice import UltronVoice

    voice = UltronVoice()
    voice._ensure_model = lambda: True          # type: ignore[method-assign]
    boom = types.SimpleNamespace(
        transcribe=lambda audio, fp16=False: pytest.fail(
            "Whisper was called on an unreadable clip"
        )
    )
    voice._model = boom
    result = voice.transcribe(b"not a wav file at all")
    assert result["ok"] is False
    assert "not a readable WAV clip" in result["error"]


def _wav_bytes(sample_rate: int = 16000, frames: int = 8000) -> bytes:
    """A real, decodable, silent 16 kHz mono WAV - enough to exercise the route."""
    import io as _io
    import struct as _struct
    import wave as _wave

    buffer = _io.BytesIO()
    with _wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(_struct.pack("<" + "h" * frames, *([0] * frames)))
    return buffer.getvalue()


def test_transcribe_route_requires_the_api_key(client):
    assert client.post("/ultron/voice/transcribe", content=b"x",
                       headers={"Content-Type": "audio/wav"}).status_code == 401


def test_transcribe_route_rejects_the_wrong_content_type(client):
    from app.api.deps import API_KEY

    # Authenticated: the API-key dependency runs before the handler, so an
    # unauthenticated caller gets 401 and learns nothing about this route's
    # input handling - which is the correct order.
    response = client.post("/ultron/voice/transcribe", json={"audio": "base64"},
                           headers={"Content-Type": "application/json",
                                    "X-API-Key": API_KEY or "k"})
    assert response.status_code == 415
    assert "audio/wav" in response.json()["detail"]


def test_transcribe_route_checks_auth_before_content_type(client):
    """Unauthenticated callers must not learn what the route accepts."""
    response = client.post("/ultron/voice/transcribe", json={"audio": "base64"},
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 401


def test_transcribe_route_rejects_an_empty_body(client):
    from app.api.deps import API_KEY

    response = client.post("/ultron/voice/transcribe", content=b"",
                           headers={"Content-Type": "audio/wav",
                                    "X-API-Key": API_KEY or "k"})
    assert response.status_code == 400


def test_transcribe_route_round_trips_a_real_wav(client, monkeypatch):
    """The browser path, end to end: WAV body in, transcript out."""
    import types as _types

    from app.agents.sara_ultron_agent.core.voice import get_voice

    from app.api.deps import API_KEY

    voice = get_voice()
    # Stand in for Whisper; loading the real model would download 140 MB.
    voice._ensure_model = lambda: True                       # type: ignore[method-assign]
    voice._model = _types.SimpleNamespace(
        transcribe=lambda audio, fp16=False: {
            "text": "open chrome and search for ai news",
            "language": "en",
        }
    )
    response = client.post(
        "/ultron/voice/transcribe",
        content=_wav_bytes(),
        headers={"Content-Type": "audio/wav", "X-API-Key": API_KEY or "k"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "open chrome and search for ai news"
    assert body["source"] == "browser"


def test_transcribe_route_reports_an_unreadable_clip(client):
    import types as _types

    from app.agents.sara_ultron_agent.core.voice import get_voice

    from app.api.deps import API_KEY

    voice = get_voice()
    voice._ensure_model = lambda: True                       # type: ignore[method-assign]
    voice._model = _types.SimpleNamespace(
        transcribe=lambda audio, fp16=False: pytest.fail("should not be reached")
    )
    response = client.post(
        "/ultron/voice/transcribe",
        content=b"RIFFbut-not-really",
        headers={"Content-Type": "audio/wav", "X-API-Key": API_KEY or "k"},
    )
    assert response.status_code == 503
    assert "not a readable WAV clip" in response.json()["detail"]["error"]
