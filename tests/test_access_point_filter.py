"""Tests for choosing which access points become devices."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sigur.const import DOMAIN, OPT_ACCESS_POINTS

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant


@pytest.fixture
async def server() -> FakeSigurServer:
    """Three access points, so a filter has something to leave out."""
    fake = FakeSigurServer(
        access_points=[
            FakeAccessPoint(1, "Главный вход"),
            FakeAccessPoint(2, "Турникет"),
            FakeAccessPoint(3, "Ворота"),
        ]
    )
    await fake.start()
    yield fake
    await fake.stop()


async def _setup(hass: HomeAssistant, fake: FakeSigurServer, **kwargs):
    """Add and set up a config entry pointing at ``fake``."""
    entry = make_entry(fake.port, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _access_point_devices(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Identifier suffixes of the access point devices of ``entry_id``."""
    registry = dr.async_get(hass)
    return {
        identifier.removeprefix(f"{entry_id}_ap_")
        for device in dr.async_entries_for_config_entry(registry, entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and identifier.startswith(f"{entry_id}_ap_")
    }


async def test_every_access_point_is_exposed_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """No filter means what it always meant: follow the server."""
    entry = await _setup(hass, server)
    assert _access_point_devices(hass, entry.entry_id) == {"1", "2", "3"}


async def test_only_the_selected_access_points_get_devices(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A hundred-door system should not force a hundred devices on the user."""
    entry = await _setup(hass, server, options={OPT_ACCESS_POINTS: ["1", "3"]})
    assert _access_point_devices(hass, entry.entry_id) == {"1", "3"}
    assert hass.states.get("select.glavnyi_vkhod_mode") is not None
    assert hass.states.get("select.turniket_mode") is None


async def test_an_excluded_access_point_is_never_polled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The filter is what makes a large installation cheap, not just tidy."""
    await _setup(hass, server, options={OPT_ACCESS_POINTS: ["1"]})
    assert "GETAPINFO 1" in server.received
    assert "GETAPINFO 2" not in server.received
    assert "GETAPINFO 3" not in server.received


async def test_deselecting_a_point_deletes_its_device_and_entities(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Otherwise the point lingers as an unavailable device forever."""
    entry = await _setup(hass, server)
    assert _access_point_devices(hass, entry.entry_id) == {"1", "2", "3"}

    hass.config_entries.async_update_entry(entry, options={OPT_ACCESS_POINTS: ["1"]})
    await hass.async_block_till_done()

    assert _access_point_devices(hass, entry.entry_id) == {"1"}
    registry = er.async_get(hass)
    remaining = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert not [uid for uid in remaining if uid.startswith(f"{entry.entry_id}_2_")]


async def test_a_point_missing_from_the_server_keeps_its_device(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A discovery blip must not destroy the user's automations.

    Only a deliberate deselection removes anything; this is the case the
    purge has to leave alone.
    """
    entry = await _setup(hass, server, options={OPT_ACCESS_POINTS: ["1", "2", "3"]})
    del server.access_points[2]

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert "2" in _access_point_devices(hass, entry.entry_id)


async def _open_access_points_step(hass: HomeAssistant, entry) -> dict:  # type: ignore[no-untyped-def]
    """Open the options flow and pick the access point step."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "access_points"}
    )


async def test_the_form_lists_excluded_points_too(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A point cannot be selected back if the form does not offer it."""
    entry = await _setup(hass, server, options={OPT_ACCESS_POINTS: ["1"]})
    result = await _open_access_points_step(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"count": "3"}
    selector = result["data_schema"].schema[OPT_ACCESS_POINTS]
    values = {option["value"] for option in selector.config["options"]}
    assert values == {"1", "2", "3"}


async def test_selecting_every_point_stores_no_filter(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Selecting all must keep meaning "follow the server"."""
    entry = await _setup(hass, server, options={OPT_ACCESS_POINTS: ["1"]})
    result = await _open_access_points_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ACCESS_POINTS: ["1", "2", "3"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ACCESS_POINTS] == []


async def test_a_subset_is_stored_as_chosen(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The ordinary case: keep two of the three."""
    entry = await _setup(hass, server)
    result = await _open_access_points_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ACCESS_POINTS: ["3", "1"]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_ACCESS_POINTS] == ["1", "3"]
    assert _access_point_devices(hass, entry.entry_id) == {"1", "3"}


async def test_selecting_nothing_is_refused(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An empty selection would silently read back as "everything"."""
    entry = await _setup(hass, server)
    result = await _open_access_points_step(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_ACCESS_POINTS: []}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {OPT_ACCESS_POINTS: "no_access_points"}
