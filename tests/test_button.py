"""Tests for the one-shot pass buttons."""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sigur.const import DOMAIN, OPT_ENABLE_CONTROL

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant

CONTROL_ON = {OPT_ENABLE_CONTROL: True}


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


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press a button entity."""
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def test_no_buttons_while_control_is_disabled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A door-opening button must not exist until the user opts in."""
    entry = await _setup(hass, server)
    registry = er.async_get(hass)
    buttons = [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "button"
    ]
    assert buttons == []


async def test_buttons_appear_once_control_is_enabled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Each access point gets an entry, an exit and a directionless button."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    registry = er.async_get(hass)
    unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "button"
    }
    for ap_id in (1, 2):
        for key in ("allow_pass_in", "allow_pass_out", "allow_pass_unknown"):
            assert f"{entry.entry_id}_{ap_id}_{key}" in unique_ids


async def test_the_directionless_button_is_disabled_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Most access points have a direction, so that button stays out of the way."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    registry = er.async_get(hass)
    entries = {
        item.unique_id: item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert entries[f"{entry.entry_id}_1_allow_pass_in"].disabled_by is None
    assert entries[f"{entry.entry_id}_1_allow_pass_out"].disabled_by is None
    assert (
        entries[f"{entry.entry_id}_1_allow_pass_unknown"].disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )


async def test_pressing_the_entry_button_sends_allowpass_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The entry button authorises one anonymous pass inbound."""
    await _setup(hass, server, options=CONTROL_ON)
    await _press(hass, "button.glavnyi_vkhod_allow_entry")
    assert "ALLOWPASS 1 ANONYMOUS IN" in server.received


async def test_pressing_the_exit_button_sends_allowpass_out(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The exit button authorises one anonymous pass outbound."""
    await _setup(hass, server, options=CONTROL_ON)
    await _press(hass, "button.turniket_allow_exit")
    assert "ALLOWPASS 2 ANONYMOUS OUT" in server.received


async def test_a_press_does_not_change_the_mode(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """One-shot means one-shot: no SETAPMODE is sent, and the mode stands."""
    await _setup(hass, server, options=CONTROL_ON)
    await _press(hass, "button.glavnyi_vkhod_allow_entry")
    assert not [line for line in server.received if line.startswith("SETAPMODE")]
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "normal"


async def test_a_refused_pass_surfaces_as_an_error(hass: HomeAssistant) -> None:
    """An access point the server rejects produces a Home Assistant error."""
    fake = FakeSigurServer(access_points=[FakeAccessPoint(1, "Ворота")])
    await fake.start()
    try:
        await _setup(hass, fake, options=CONTROL_ON)
        # The access point vanishes from the server between setup and the press.
        fake.access_points.clear()
        with pytest.raises(HomeAssistantError) as excinfo:
            await _press(hass, "button.vorota_allow_entry")
        assert excinfo.value.translation_key == "allow_pass_failed"
    finally:
        await fake.stop()


async def test_buttons_are_scoped_to_their_own_server(hass: HomeAssistant) -> None:
    """Pressing a button on one system never opens the other system's point 1."""
    office = FakeSigurServer(access_points=[FakeAccessPoint(1, "Офис")])
    depot = FakeSigurServer(access_points=[FakeAccessPoint(1, "Склад")])
    await office.start()
    await depot.start()
    try:
        await _setup(hass, office, name="Sigur - Офис", options=CONTROL_ON)
        await _setup(hass, depot, name="Sigur - Склад", options=CONTROL_ON)
        await _press(hass, "button.ofis_allow_entry")
        assert [line for line in office.received if line.startswith("ALLOWPASS")]
        assert not [line for line in depot.received if line.startswith("ALLOWPASS")]
    finally:
        await office.stop()
        await depot.stop()


async def test_control_disabled_error_is_translatable(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The refusal carries a translation key, not a hardcoded English string."""
    await _setup(hass, server)
    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", "option": "unlocked"},
            blocking=True,
        )
    assert excinfo.value.translation_domain == "sigur"
    assert excinfo.value.translation_key == "control_disabled"
    assert excinfo.value.translation_placeholders == {"name": "Sigur - Офис"}


async def test_a_one_way_in_access_point_offers_only_entry(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """A point declared entry-only must not offer an exit button.

    OIF never reports directionality, so this is the user's declaration; the
    exit button would simply always be wrong.
    """
    entry = await _setup(hass, server, options=CONTROL_ON)
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

    assert hass.states.get("button.glavnyi_vkhod_allow_entry") is not None
    assert hass.states.get("button.glavnyi_vkhod_allow_exit") is None
    # The other access point is untouched and still bidirectional.
    assert hass.states.get("button.turniket_allow_exit") is not None
    # The withdrawn button leaves no orphan behind in the registry.
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_1_allow_pass_out"
        )
        is None
    )


async def test_a_one_way_out_access_point_offers_only_exit(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The mirror case."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 2,
            "direction_mode": "out",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()

    assert hass.states.get("button.turniket_allow_exit") is not None
    assert hass.states.get("button.turniket_allow_entry") is None


async def test_the_directionless_button_survives_a_one_way_point(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """A door with one reader still needs its directionless button."""
    entry = await _setup(hass, server, options=CONTROL_ON)
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

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_1_allow_pass_unknown"
        )
        is not None
    )


async def test_the_direction_mode_is_visible_on_entities(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """Automations can read the declared mode without the panel."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "direction_mode": "out",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()

    state = hass.states.get("select.glavnyi_vkhod_mode")
    assert state is not None
    assert state.attributes["direction_mode"] == "out"
    assert (
        hass.states.get("select.turniket_mode").attributes["direction_mode"] == "both"
    )
