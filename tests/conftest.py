"""Shared pytest fixtures for the Sigur integration tests.

The suite is split in two halves:

* protocol tests, which only import ``custom_components.sigur.api`` and run on
  any platform, including Windows;
* Home Assistant tests, which need ``pytest-homeassistant-custom-component``
  and therefore a Linux host, because Home Assistant core imports ``fcntl``.

The Home Assistant half is skipped automatically when the harness is missing,
so the protocol half stays runnable everywhere.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # pragma: no cover - import guard, exercised by the platform, not a test
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    HA_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows / bare protocol runs
    MockConfigEntry = None  # type: ignore[assignment]
    HA_AVAILABLE = False

requires_home_assistant = pytest.mark.skipif(
    not HA_AVAILABLE,
    reason="pytest-homeassistant-custom-component is unavailable on this platform",
)


@pytest.fixture(autouse=True)
def _allow_loopback_sockets() -> Generator[None]:
    """Let the tests talk to the in-process fake OIF server.

    ``pytest-homeassistant-custom-component`` blocks every socket by default so
    that a test can never reach a real service. The fake Sigur server is a
    loopback listener started inside the test process, so sockets have to be
    re-enabled; nothing here ever leaves the machine.
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield
    pytest_socket.socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)


if HA_AVAILABLE:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(
        enable_custom_integrations: Any,
    ) -> Generator[None]:
        """Load ``custom_components/sigur`` in every Home Assistant test."""
        yield
