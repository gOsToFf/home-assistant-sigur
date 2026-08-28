"""Diagnostics redaction and content tests."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json

from homeassistant.core import HomeAssistant
import pytest

from custom_components.sigur.const import (
    OPT_ENABLE_PERSONAL_DATA,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_URL,
)
from custom_components.sigur.diagnostics import (
    _mask_host,
    async_get_config_entry_diagnostics,
)

from .conftest import requires_home_assistant
from .fake_oif_server import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    FakeEvent,
    FakeSigurServer,
)
from .helpers import make_entry

pytestmark = requires_home_assistant


@pytest.fixture
async def server() -> FakeSigurServer:
    """A running fake OIF server, torn down after the test."""
    fake = FakeSigurServer()
    await fake.start()
    yield fake
    await fake.stop()


async def _setup(hass: HomeAssistant, fake: FakeSigurServer, **kwargs):  # type: ignore[no-untyped-def]
    """Add and set up a config entry pointing at ``fake``."""
    entry = make_entry(fake.port, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.10.25", "192.***.***.25"),
        ("sigur.corp.example.com", "***.example.com"),
        ("sigur", "si***"),
        ("", ""),
    ],
)
def test_host_masking(host: str, expected: str) -> None:
    """Hosts are masked enough to be unrecognisable but still comparable."""
    assert _mask_host(host) == expected


async def test_diagnostics_never_leak_credentials(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The password, the login and the raw host never appear in diagnostics."""
    entry = await _setup(
        hass,
        server,
        options={
            OPT_WEBHOOK_URL: "https://example.com/hook",
            OPT_WEBHOOK_SECRET: "s3cret-signing-key",
        },
    )
    payload = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(payload, default=str)

    assert DEFAULT_PASSWORD not in dumped
    assert DEFAULT_USERNAME not in dumped
    assert "s3cret-signing-key" not in dumped
    assert "https://example.com/hook" not in dumped
    assert payload["server"]["host"] == "127.***.***.1"


async def test_diagnostics_report_the_connection_state(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Both connections, the subscribe mode and the counters are reported."""
    entry = await _setup(hass, server)
    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["connections"]["command"]["connected"] is True
    assert payload["connections"]["events"]["connected"] is True
    assert payload["server"]["subscribe_mode"] == "CE_WITH_NAMES"
    assert payload["server"]["tls_mode"] == "disabled"
    assert payload["server"]["mutual_tls"] is False
    assert payload["server"]["zone_count"] == 2
    assert payload["server"]["access_point_count"] == 2
    assert payload["connections"]["command"]["command_count"] > 0
    assert payload["coordinator"]["last_update_success"] is True
    assert payload["pipeline"]["dropped_events"] == 0
    assert payload["pipeline"]["webhook_queue_size"] is None


async def test_diagnostics_list_access_points_without_personal_data(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Object ids of the last event stay out unless the option is on."""
    entry = await _setup(hass, server)
    await server.push_event(
        FakeEvent(datetime(2025, 1, 27, 11, 23, 8), 4, 1, 6, 2, object_name="Иванов")
    )
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)
    first = next(ap for ap in payload["access_points"] if ap["id"] == 1)
    assert first["state"] == "ONLINE_NORMAL"
    assert first["open_state"] == "CLOSED"
    assert first["last_event"]["category"] == "pass_registered"
    assert "object_id" not in first["last_event"]
    assert "Иванов" not in json.dumps(payload, default=str)


async def test_diagnostics_include_object_ids_when_opted_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """With personal data enabled the object id is included."""
    entry = await _setup(hass, server, options={OPT_ENABLE_PERSONAL_DATA: True})
    await server.push_event(FakeEvent(datetime(2025, 1, 27, 11, 23, 8), 4, 1, 6, 2))
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    payload = await async_get_config_entry_diagnostics(hass, entry)
    first = next(ap for ap in payload["access_points"] if ap["id"] == 1)
    assert first["last_event"]["object_id"] == 6


async def test_debug_logs_do_not_contain_the_password(
    hass: HomeAssistant, server: FakeSigurServer, caplog: pytest.LogCaptureFixture
) -> None:
    """Even at debug level, the operator password never reaches the log."""
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.sigur")
    await _setup(hass, server)
    await server.push_event(FakeEvent(datetime(2025, 1, 27, 11, 23, 8), 4, 1, 6, 2))
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    assert DEFAULT_PASSWORD not in caplog.text
    assert "29323" not in caplog.text, "the credential number must not be logged"
