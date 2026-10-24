"""Test-suite isolation for state that would otherwise touch the real machine.

``core.devices`` now persists paired devices to disk, and the process-wide
``registry`` singleton resolves its path at import time. Without this, running
the test suite would load the developer's real ``~/.sara/devices.json``,
mutate it (several tests revoke everything), and leave their actual phones and
desktops unpaired. A test run must never be able to unpair a real device.

The environment variable is set at conftest import time - before pytest
collects any test module, and therefore before ``core.devices`` is first
imported - because the module reads it once, at import, not per instance.

A second machine-level hazard is handled here too: pytest's default temp root
``%TEMP%/pytest-of-<user>`` can end up owned by an inaccessible SID (seen on
this machine: ``PermissionError: [WinError 5] ... pytest-of-HP`` on *every*
test, because the autouse fixture below needs ``tmp_path``). When that root is
not usable we transparently fall back to a project-local ``.pytest_tmp`` so the
suite still runs without administrator rights.
"""
from __future__ import annotations

import atexit
import getpass
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_STATE_DIR = tempfile.mkdtemp(prefix="sara-test-devices-")
os.environ["SARA_DEVICE_STATE_PATH"] = os.path.join(_STATE_DIR, "devices.json")


def pytest_configure(config) -> None:
    """Pick a usable base temp directory before any tmp_path is requested.

    The default ``%TEMP%/pytest-of-<user>`` root is checked once; only when it
    is unwritable (the WinError 5 case) does the project-local directory take
    over. An explicit ``--basetemp`` on the command line always wins.
    """
    if getattr(config.option, "basetemp", None):
        return
    try:
        root = os.path.join(
            tempfile.gettempdir(), f"pytest-of-{getpass.getuser()}")
        os.makedirs(root, exist_ok=True)
        list(os.scandir(root))          # raises on the inaccessible directory
    except Exception:
        local = Path(__file__).resolve().parent.parent / ".pytest_tmp"
        local.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(local)


@atexit.register
def _cleanup_state_dir() -> None:
    shutil.rmtree(_STATE_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_device_state(tmp_path, monkeypatch):
    """Give every test its own device-registry file.

    ``DeviceRegistry()`` with no argument now resolves a shared on-disk path, so
    without this a test would load whatever the previous test paired, and
    assertions like ``list()[0]`` would address the wrong device. Patching the
    module attribute works because ``__init__`` looks ``STATE_PATH`` up at call
    time rather than capturing it at import.
    """
    from app.agents.sara_ultron_agent.core import devices

    monkeypatch.setattr(devices, "STATE_PATH", str(tmp_path / "devices.json"))
    yield