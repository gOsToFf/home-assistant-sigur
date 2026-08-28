"""Setup, unload and device/entity creation tests for the Sigur integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sigur.const import DOMAIN, OPT_ENABLE_PERSONAL_DATA

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeBehaviour, FakeSigurServer
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


async def test_setup_creates_hub_and_access_point_devices(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """One hub device and one device per access point are registered."""
    entry = await _setup(hass, server)
    assert entry.state is ConfigEntryState.LOADED

    devices = dr.async_get(hass)
    hub = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert hub is not None
    assert hub.name == "Sigur - Офис"

    for ap_id, expected in ((1, "Главный вход"), (2, "Турникет")):
        device = devices.async_get_device(
            identifiers={(DOMAIN, f"{entry.entry_id}_ap_{ap_id}")}
        )
        assert device is not None
        assert device.name == expected
        assert device.via_device_id == hub.id


async def test_setup_creates_the_expected_entities(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Each access point gets its link, door, mode and event entities."""
    entry = await _setup(hass, server)
    registry = er.async_get(hass)
    unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for ap_id in (1, 2):
        for key in ("connectivity", "door", "mode", "event"):
            assert f"{entry.entry_id}_{ap_id}_{key}" in unique_ids


async def test_states_reflect_the_server(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Link, door and mode states come from ``GETAPINFO``."""
    await _setup(hass, server)
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "on"
    assert hass.states.get("binary_sensor.glavnyi_vkhod_door").state == "off"
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "normal"


async def test_offline_access_point_has_no_mode(hass: HomeAssistant) -> None:
    """An offline access point reports no lock mode, not a wrong one."""
    fake = FakeSigurServer(
        access_points=[
            FakeAccessPoint(1, "Ворота", state="OFFLINE", open_state="UNKNOWN")
        ]
    )
    await fake.start()
    try:
        await _setup(hass, fake)
        assert hass.states.get("binary_sensor.vorota_link").state == "off"
        assert hass.states.get("select.vorota_mode").state == "unknown"
        # No door sensor is wired up, so the door entity must not guess.
        assert hass.states.get("binary_sensor.vorota_door").state == "unavailable"
    finally:
        await fake.stop()


async def test_personal_data_sensors_are_opt_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Object id and name sensors only exist once the option is enabled."""
    entry = await _setup(hass, server)
    registry = er.async_get(hass)
    unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert f"{entry.entry_id}_1_last_pass_object_name" not in unique_ids

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    entry2 = await _setup(
        hass, server, name="Sigur - ПДн", options={OPT_ENABLE_PERSONAL_DATA: True}
    )
    unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry2.entry_id)
    }
    assert f"{entry2.entry_id}_1_last_pass_object_name" in unique_ids


async def test_unload_leaves_no_running_tasks(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Unloading cancels every Sigur task and closes both connections."""
    entry = await _setup(hass, server)
    hub = entry.runtime_data.hub

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hub.command_connection is None
    assert hub.event_connection is None
    leftover = [
        task
        for task in asyncio.all_tasks()
        if "sigur" in (task.get_name() or "") and not task.done()
    ]
    assert leftover == []


async def test_setup_retries_when_the_server_is_down(hass: HomeAssistant) -> None:
    """An unreachable server yields SETUP_RETRY rather than a hard failure."""
    entry = make_entry(1)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_bad_credentials_trigger_reauth(hass: HomeAssistant) -> None:
    """Rejected credentials put the entry into the reauth state."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(reject_login=True))
    await fake.start()
    try:
        entry = make_entry(fake.port)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert any(flow["context"].get("source") == "reauth" for flow in flows)
    finally:
        await fake.stop()


async def test_two_servers_with_the_same_access_point_ids(hass: HomeAssistant) -> None:
    """Two Sigur systems can expose access point 1 without colliding."""
    office = FakeSigurServer(access_points=[FakeAccessPoint(1, "Офис - вход")])
    depot = FakeSigurServer(access_points=[FakeAccessPoint(1, "Склад - вход")])
    await office.start()
    await depot.start()
    try:
        entry_a = await _setup(hass, office, name="Sigur - Офис")
        entry_b = await _setup(hass, depot, name="Sigur - Склад")
        assert entry_a.state is ConfigEntryState.LOADED
        assert entry_b.state is ConfigEntryState.LOADED

        devices = dr.async_get(hass)
        device_a = devices.async_get_device(
            identifiers={(DOMAIN, f"{entry_a.entry_id}_ap_1")}
        )
        device_b = devices.async_get_device(
            identifiers={(DOMAIN, f"{entry_b.entry_id}_ap_1")}
        )
        assert device_a is not None and device_b is not None
        assert device_a.id != device_b.id

        registry = er.async_get(hass)
        ids_a = {
            item.unique_id
            for item in er.async_entries_for_config_entry(registry, entry_a.entry_id)
        }
        ids_b = {
            item.unique_id
            for item in er.async_entries_for_config_entry(registry, entry_b.entry_id)
        }
        assert not ids_a & ids_b
        assert hass.states.get("binary_sensor.ofis_vkhod_link") is not None
        assert hass.states.get("binary_sensor.sklad_vkhod_link") is not None
    finally:
        await office.stop()
        await depot.stop()


async def test_new_access_points_are_added_on_the_next_poll(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An access point added on the server shows up without a reload."""
    entry = await _setup(hass, server)
    server.access_points[7] = FakeAccessPoint(7, "Новая дверь")

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    devices = dr.async_get(hass)
    assert (
        devices.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_ap_7")})
        is not None
    )
    assert hass.states.get("binary_sensor.novaia_dver_link") is not None


async def test_removed_access_point_becomes_unavailable_not_deleted(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A vanished access point is marked unavailable, keeping its registry entry."""
    entry = await _setup(hass, server)
    server.access_points.pop(2)

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.turniket_link").state == "unavailable"
    devices = dr.async_get(hass)
    assert (
        devices.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_ap_2")})
        is not None
    )
