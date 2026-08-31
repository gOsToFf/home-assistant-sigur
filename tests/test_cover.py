"""Tests for the one-shot pass covers."""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sigur.const import (
    DOMAIN,
    OPT_ENABLE_CONTROL,
    OPT_ENABLE_PASS_COVERS,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant

CONTROL_ON = {OPT_ENABLE_CONTROL: True}
COVERS_ON = {OPT_ENABLE_CONTROL: True, OPT_ENABLE_PASS_COVERS: True}


@pytest.fixture
async def server() -> FakeSigurServer:
    """A running fake OIF server, torn down after the test."""
    fake = FakeSigurServer()
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


async def _open(hass: HomeAssistant, entity_id: str) -> None:
    """Open a cover entity."""
    await hass.services.async_call(
        "cover", "open_cover", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


def _covers(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Unique ids of every cover this entry registered."""
    registry = er.async_get(hass)
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry_id)
        if item.domain == "cover"
    }


async def test_no_covers_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Two controls for one action is not what most installations want."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    assert _covers(hass, entry.entry_id) == set()


async def test_no_covers_while_control_is_disabled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Asking for covers must not grant write access on its own."""
    entry = await _setup(hass, server, options={OPT_ENABLE_PASS_COVERS: True})
    assert _covers(hass, entry.entry_id) == set()


async def test_covers_appear_once_both_options_are_on(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Each access point gets an entry, an exit and a directionless cover."""
    entry = await _setup(hass, server, options=COVERS_ON)
    unique_ids = _covers(hass, entry.entry_id)
    for ap_id in (1, 2):
        for key in ("pass_cover_in", "pass_cover_out", "pass_cover_unknown"):
            assert f"{entry.entry_id}_{ap_id}_{key}" in unique_ids


async def test_the_directionless_cover_is_disabled_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """As with the matching button, most access points have a direction."""
    entry = await _setup(hass, server, options=COVERS_ON)
    registry = er.async_get(hass)
    entries = {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entries[f"{entry.entry_id}_1_pass_cover_in"].disabled_by is None
    assert (
        entries[f"{entry.entry_id}_1_pass_cover_unknown"].disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )


async def test_opening_the_entry_cover_sends_allowpass_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Opening is exactly the ALLOWPASS its button sends, nothing more."""
    await _setup(hass, server, options=COVERS_ON)
    await _open(hass, "cover.glavnyi_vkhod_entry")
    assert "ALLOWPASS 1 ANONYMOUS IN" in server.received
    assert not [line for line in server.received if line.startswith("SETAPMODE")]


async def test_opening_the_exit_cover_sends_allowpass_out(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The mirror case."""
    await _setup(hass, server, options=COVERS_ON)
    await _open(hass, "cover.turniket_exit")
    assert "ALLOWPASS 2 ANONYMOUS OUT" in server.received


async def test_the_cover_reports_the_real_door_position(hass: HomeAssistant) -> None:
    """A pass authorises an opening; the door sensor says whether it opened."""
    fake = FakeSigurServer(
        access_points=[
            FakeAccessPoint(1, "Ворота", open_state="OPENED"),
            FakeAccessPoint(2, "Калитка", open_state="CLOSED"),
        ]
    )
    await fake.start()
    try:
        await _setup(hass, fake, options=COVERS_ON)
        assert hass.states.get("cover.vorota_entry").state == "open"
        assert hass.states.get("cover.kalitka_entry").state == "closed"
    finally:
        await fake.stop()


async def test_an_unknown_door_position_stays_unknown(hass: HomeAssistant) -> None:
    """Sigur says UNKNOWN when no door sensor is wired up; do not invent one."""
    fake = FakeSigurServer(
        access_points=[FakeAccessPoint(1, "Шлагбаум", open_state="UNKNOWN")]
    )
    await fake.start()
    try:
        await _setup(hass, fake, options=COVERS_ON)
        assert hass.states.get("cover.shlagbaum_entry").state == "unknown"
    finally:
        await fake.stop()


async def test_a_one_way_access_point_offers_only_its_direction(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The covers follow the declared direction, exactly as the buttons do."""
    entry = await _setup(hass, server, options=COVERS_ON)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "direction_mode": "in",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()

    assert hass.states.get("cover.glavnyi_vkhod_entry") is not None
    assert hass.states.get("cover.glavnyi_vkhod_exit") is None
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "cover", DOMAIN, f"{entry.entry_id}_1_pass_cover_out"
        )
        is None
    )


async def test_turning_the_option_off_removes_the_covers(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An assistant that already knows the cover must stop being offered it."""
    entry = await _setup(hass, server, options=COVERS_ON)
    assert _covers(hass, entry.entry_id)

    hass.config_entries.async_update_entry(entry, options=CONTROL_ON)
    await hass.async_block_till_done()

    assert _covers(hass, entry.entry_id) == set()
    assert hass.states.get("cover.glavnyi_vkhod_entry") is None


async def test_a_refused_pass_surfaces_as_an_error(hass: HomeAssistant) -> None:
    """An access point the server rejects produces a Home Assistant error."""
    fake = FakeSigurServer(access_points=[FakeAccessPoint(1, "Ворота")])
    await fake.start()
    try:
        await _setup(hass, fake, options=COVERS_ON)
        fake.access_points.clear()
        with pytest.raises(HomeAssistantError) as excinfo:
            await _open(hass, "cover.vorota_entry")
        assert excinfo.value.translation_key == "allow_pass_failed"
    finally:
        await fake.stop()
