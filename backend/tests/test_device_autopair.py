"""Tests for zero-touch device pairing (desktop auto-connect + phone QR).

Three separate defects made "connect the device" fail, and each is pinned here
so it cannot come back:

* the registry was in-memory only, so every backend restart silently unpaired
  every device and the command center went back to demanding a connection;
* the QR encoded ``/ultron/pair/claim`` but nothing served that route, so
  scanning a pairing QR on a phone returned 404;
* the Windows agent imported ``websocket_client`` (the distribution name, not
  the module) and set ping_timeout above ping_interval, so it could never hold
  a socket open - which made the whole desktop path unreachable no matter how
  the device had been paired.

The tests deliberately avoid touching the real user profile: every registry is
constructed with an explicit ``state_path`` under ``tmp_path``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.agents.sara_ultron_agent.core.devices import (
    DEVICE_SCOPES,
    DeviceRegistry,
)

def _find_local_agent() -> Path:
    """Locate ``sara_desktop/local_agent.py`` in either layout.

    In a checkout the tests live at ``<repo>/backend/tests`` so the agent is two
    parents up. In the backend image the contents of ``backend/`` are copied to
    ``/app`` and ``sara_desktop/`` sits beside them, so the same relative walk
    lands outside the tree. Both layouts are real, so try each and skip the
    source-level tests (rather than failing them) where it is genuinely absent.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "sara_desktop" / "local_agent.py",
        here.parents[1] / "sara_desktop" / "local_agent.py",
        here.parents[1] / ".." / "sara_desktop" / "local_agent.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


LOCAL_AGENT = _find_local_agent()

requires_local_agent = pytest.mark.skipif(
    not LOCAL_AGENT.is_file(),
    reason="sara_desktop/local_agent.py is not present in this layout",
)


@pytest.fixture()
def registry(tmp_path: Path) -> DeviceRegistry:
    return DeviceRegistry(state_path=str(tmp_path / "devices.json"))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_pairing_survives_a_restart(tmp_path: Path) -> None:
    """The whole point: a restart must not unpair a device."""
    path = str(tmp_path / "devices.json")
    first = DeviceRegistry(state_path=path)
    offer = first.create_offer(kind="phone", label="Pixel")
    claimed = first.claim(offer["token"], kind="phone", label="Pixel")
    token = claimed["device_token"]

    second = DeviceRegistry(state_path=path)  # simulates the restart
    assert len(second.list()) == 1
    assert second.list()[0]["label"] == "Pixel"
    assert second.authenticate(token) is not None, \
        "a device token issued before the restart must still authenticate"


def test_restored_device_is_never_trusted_as_connected(tmp_path: Path) -> None:
    """A persisted 'connected' flag would be a lie after a restart."""
    path = str(tmp_path / "devices.json")
    first = DeviceRegistry(state_path=path)
    offer = first.create_offer(kind="phone", label="Pixel")
    claimed = first.claim(offer["token"], kind="phone")
    first.set_connected(claimed["device"]["device_id"], True)

    second = DeviceRegistry(state_path=path)
    # ``as_dict`` derives "connected" from the heartbeat age, so the stored flag
    # is what must be checked: a restart must never leave it set.
    assert second.get(claimed["device"]["device_id"]).connected is False


def test_persisted_token_is_hashed_never_plaintext(tmp_path: Path) -> None:
    """The state file must not become a credential store."""
    path = tmp_path / "devices.json"
    reg = DeviceRegistry(state_path=str(path))
    offer = reg.create_offer(kind="phone", label="Pixel")
    token = reg.claim(offer["token"], kind="phone")["device_token"]

    blob = path.read_text(encoding="utf-8")
    assert token not in blob, "the raw device token must never be written to disk"
    stored = json.loads(blob)["devices"][0]["token_hash"]
    assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)


def test_revocation_survives_a_restart(tmp_path: Path) -> None:
    """Revocation is sticky; a restart must not quietly un-revoke a device."""
    path = str(tmp_path / "devices.json")
    first = DeviceRegistry(state_path=path)
    offer = first.create_offer(kind="phone", label="Revoked")
    claimed = first.claim(offer["token"], kind="phone")
    token = claimed["device_token"]
    first.revoke(claimed["device"]["device_id"], "test")

    second = DeviceRegistry(state_path=path)
    assert second.list()[0]["revoked"] is True
    assert second.authenticate(token) is None, (
        "a revoked token must not authenticate again after a restart")


def test_corrupt_state_file_does_not_stop_the_boot(tmp_path: Path) -> None:
    """A bad file costs the user their pairings, not their backend."""
    path = tmp_path / "devices.json"
    path.write_text("{not json at all", encoding="utf-8")
    reg = DeviceRegistry(state_path=str(path))
    assert reg.list() == []
    assert reg.authenticate("anything") is None


def test_forged_token_hash_is_refused_on_load(tmp_path: Path) -> None:
    """A hand-edited state file must not be able to authorise itself."""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({
        "version": 1,
        "devices": [{
            "device_id": "dev_evil", "kind": "pc_agent", "label": "evil",
            "platform": "", "token_hash": "not-a-real-sha256",
            "scopes": list(DEVICE_SCOPES["pc_agent"]),
            "created": 0, "last_seen": 0,
        }],
    }), encoding="utf-8")
    reg = DeviceRegistry(state_path=str(path))
    assert reg.list() == [], "a non-sha256 token_hash must not be loaded"


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    reg = DeviceRegistry(state_path=str(tmp_path / "devices.json"))
    for i in range(5):
        offer = reg.create_offer(kind="phone", label=f"P{i}")
        reg.claim(offer["token"], kind="phone")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".devices-")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# ---------------------------------------------------------------------------
# Auto-pairing
# ---------------------------------------------------------------------------
def test_auto_pair_creates_an_executor_scoped_device(registry: DeviceRegistry) -> None:
    result = registry.auto_pair(kind="pc_agent", label="SARA-PC")
    assert result["reused"] is False
    assert result["device_token"]
    assert "pc.execute" in result["device"]["scopes"], (
        "an auto-paired desktop agent must be able to execute, or the whole "
        "point is lost")


def test_auto_pair_is_idempotent_and_rotates_the_token(registry: DeviceRegistry) -> None:
    """Re-running must reuse the device, not stack up duplicates."""
    first = registry.auto_pair(kind="pc_agent", label="SARA-PC")
    second = registry.auto_pair(kind="pc_agent", label="SARA-PC")

    assert len(registry.list()) == 1, "auto-pair must not duplicate devices"
    assert second["reused"] is True
    assert second["rotated"] is True
    assert second["device_token"], (
        "a reused device must still receive a usable token, otherwise a lost "
        "cache becomes a permanent lockout")
    assert second["device_token"] != first["device_token"], (
        "the token should rotate so the new one is the only valid one")
    assert registry.authenticate(first["device_token"]) is None, (
        "the superseded token must stop working")
    assert registry.authenticate(second["device_token"]) is not None


def test_auto_pair_refuses_to_rotate_a_live_session(registry: DeviceRegistry) -> None:
    """Knocking a connected agent offline would be worse than doing nothing."""
    result = registry.auto_pair(kind="pc_agent", label="SARA-PC")
    registry.set_connected(result["device"]["device_id"], True)

    again = registry.auto_pair(kind="pc_agent", label="SARA-PC")
    assert again["device_token"] == ""
    assert again["rotated"] is False
    assert again["reused"] is True


def test_auto_pair_rejects_an_unknown_kind(registry: DeviceRegistry) -> None:
    with pytest.raises(ValueError):
        registry.auto_pair(kind="smart-fridge")


def test_auto_pair_survives_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "devices.json")
    first = DeviceRegistry(state_path=path)
    token = first.auto_pair(kind="pc_agent", label="SARA-PC")["device_token"]

    second = DeviceRegistry(state_path=path)
    assert second.authenticate(token) is not None, (
        "an auto-paired device must still be connected after a restart, or the "
        "user is asked to pair their own machine again")


# ---------------------------------------------------------------------------
# The auto-pair network guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.5.5.5", True),
    ("::1", True),
    ("localhost", True),
    ("10.1.2.3", True),
    ("172.17.0.1", True),      # the Docker bridge gateway
    ("192.168.1.20", True),
    ("169.254.10.10", True),
    ("::ffff:127.0.0.1", True),
    ("8.8.8.8", False),
    ("203.0.113.9", False),    # documentation block - not a LAN
    ("192.0.2.5", False),      # documentation block - not a LAN
    ("::ffff:8.8.8.8", False),
    ("", False),
    ("not-an-ip", False),
    ("999.999.999.999", False),
])
def test_local_address_guard(host: str, expected: bool) -> None:
    from app.agents.sara_ultron_agent.api import _is_local_address
    assert _is_local_address(host) is expected


# ---------------------------------------------------------------------------
# The QR redeem page
# ---------------------------------------------------------------------------
def _pair_client():
    """A TestClient over the real router.

    The router must be mounted on an app rather than used bare: this FastAPI
    version raises "fastapi_middleware_astack not found in request scope" when a
    router's dependencies are resolved without one.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.agents.sara_ultron_agent import api as api_module

    app = FastAPI()
    app.include_router(api_module.router)
    return TestClient(app), api_module


def test_pair_page_is_served_for_a_live_offer(tmp_path: Path) -> None:
    """The QR pointed at a route with no handler: scanning returned 404."""
    client, api_module = _pair_client()

    offer = api_module.device_registry.create_offer(kind="phone", label="Pixel")
    response = client.get("/ultron/pair/claim", params={"t": offer["token"]})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'STATE = "ready"' in response.text


def test_pair_page_rejects_an_unknown_token() -> None:
    client, _ = _pair_client()
    response = client.get("/ultron/pair/claim", params={"t": "definitely-not-a-token"})
    assert response.status_code == 410


def test_pair_page_requires_a_token() -> None:
    client, _ = _pair_client()
    assert client.get("/ultron/pair/claim").status_code == 400


def test_qr_payload_points_at_a_route_that_exists() -> None:
    """The defect in one assertion: the QR encoded a path with no handler."""
    client, api_module = _pair_client()

    offer = api_module.device_registry.create_offer(kind="phone", label="Pixel",
                                                    backend_url="http://x")
    assert offer["payload"] == f"http://x/ultron/pair/claim?t={offer['token']}"
    response = client.get("/ultron/pair/claim", params={"t": offer["token"]})
    assert response.status_code == 200, \
        "the URL a QR encodes must resolve, or phone pairing cannot work"


def test_pair_page_never_renders_unfiltered_input() -> None:
    """Markup in a label must not reach the page able to execute anything."""
    from app.agents.sara_ultron_agent.api import _render_pair_page

    html = _render_pair_page("ready", token="abc-123",
                             detail='<img src=x onerror="alert(1)">')
    # The sentinel reduces input to a plain alphabet, so no angle bracket,
    # quote or equals sign from the input can survive into the document.
    body = html.split('<p class="dim">')[1].split("</p>")[0]
    assert not set(body) & set("<>\"'="), f"markup survived into the page: {body!r}"
    assert "<script>alert" not in html
    assert "@@DETAIL@@" not in html and "@@TOKEN@@" not in html and "@@STATE@@" not in html


def test_pair_page_token_is_sanitised_too() -> None:
    """The token goes into a JS string literal, so it must be inert there too."""
    from app.agents.sara_ultron_agent.api import _render_pair_page

    html = _render_pair_page("ready", token='"; alert(1); //', detail="d")
    assert 'alert(1); //' not in html.split("var TOKEN = ")[1].split("\n")[0]


def test_pair_page_has_no_str_format_brace_bug() -> None:
    """Regression: the template contains CSS/JS braces and once used
    ``str.format``, which raised KeyError('color-scheme') and 500'd the page."""
    from app.agents.sara_ultron_agent.api import _render_pair_page

    html = _render_pair_page("ready", token="t", detail="d")
    assert ":root{color-scheme:dark}" in html, "the CSS block must survive intact"


# ---------------------------------------------------------------------------
# The Windows agent: the two bugs that made it unable to connect at all
# ---------------------------------------------------------------------------
def _agent_source() -> str:
    return LOCAL_AGENT.read_text(encoding="utf-8")


@requires_local_agent
def test_agent_imports_the_module_name_not_the_distribution_name() -> None:
    """``websocket-client`` installs a module called ``websocket``.

    Importing ``websocket_client`` raised ImportError even when the package was
    installed, so the agent printed 'install websocket-client' - which the user
    had done - and exited.
    """
    source = _agent_source()
    assert "from websocket_client import" not in source, (
        "websocket_client is the distribution name, not the module")
    assert "from websocket import WebSocketApp" in source


@requires_local_agent
def test_agent_ping_timeout_is_below_ping_interval() -> None:
    """websocket-client refuses to run unless ping_timeout < ping_interval.

    These were 30s and 15s, so the agent raised WebSocketException the moment
    it opened a socket and never stayed connected.
    """
    source = _agent_source()
    tree = ast.parse(source)
    assigned: dict[str, float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        assigned[target.id] = float(node.value.value)
                    except (TypeError, ValueError):
                        pass
    assert "PING_TIMEOUT_S" in assigned, "the timeout must be a named constant"
    assert assigned["PING_TIMEOUT_S"] < assigned["HEARTBEAT_S"], (
        "ping_timeout must be strictly below ping_interval")


@requires_local_agent
def test_agent_source_is_valid_python() -> None:
    ast.parse(_agent_source())


# ---------------------------------------------------------------------------
# The agent's token cache
# ---------------------------------------------------------------------------
def _load_agent(tmp_path: Path, monkeypatch):
    import importlib.util

    monkeypatch.setenv("SARA_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("la_under_test", LOCAL_AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_local_agent
def test_token_cache_round_trip(tmp_path: Path, monkeypatch) -> None:
    module = _load_agent(tmp_path, monkeypatch)
    assert module._read_cached_token() == ""
    module._cache_token("tok-abc-123")
    assert module._read_cached_token() == "tok-abc-123"


@requires_local_agent
def test_token_cache_tolerates_a_missing_directory(tmp_path: Path, monkeypatch) -> None:
    """It must not raise - a cache problem is not a reason to refuse to run."""
    module = _load_agent(tmp_path, monkeypatch)
    nested = tmp_path / "does" / "not" / "exist"
    monkeypatch.setenv("SARA_STATE_DIR", str(nested))
    module._cache_token("tok")
    assert module._read_cached_token() == "tok"


@requires_local_agent
def test_explicit_token_file_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SARA_STATE_DIR", str(tmp_path / "ignored"))
    module = _load_agent(tmp_path, monkeypatch)
    explicit = tmp_path / "explicit.token"
    monkeypatch.setenv("SARA_TOKEN_FILE", str(explicit))
    assert module._token_cache_path() == str(explicit)