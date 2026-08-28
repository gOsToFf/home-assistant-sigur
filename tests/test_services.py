"""Tests for the Sigur actions and for control being off by default."""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.sigur.const import (
    ATTR_CONFIRM_ALL,
    ATTR_DIRECTION,
    ATTR_MODE,
    ATTR_OBJECT_ID,
    DOMAIN,
    OPT_ENABLE_CONTROL,
    SERVICE_ALLOW_PASS,
    SERVICE_REFRESH,
    SERVICE_SET_ACCESS_POINT_MODE,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeSigurServer
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


async def _setup(hass: HomeAssistant, fake: FakeSigurServer, **kwargs):  # type: ignore[no-untyped-def]
    """Add and set up a config entry pointing at ``fake``."""
    entry = make_entry(fake.port, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_control_is_disabled_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Setting a mode fails with a clear error until control is enabled."""
    await _setup(hass, server)
    with pytest.raises(HomeAssistantError, match="disabled"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ACCESS_POINT_MODE,
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", ATTR_MODE: "locked"},
            blocking=True,
        )
    assert not [line for line in server.received if line.startswith("SETAPMODE")]


async def test_the_select_entity_also_refuses_while_control_is_off(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The select is read-only until control is enabled."""
    await _setup(hass, server)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", "option": "locked"},
            blocking=True,
        )
    assert not [line for line in server.received if line.startswith("SETAPMODE")]


async def test_set_mode_sends_setapmode(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """With control enabled the action reaches the server and updates state."""
    await _setup(hass, server, options=CONTROL_ON)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ACCESS_POINT_MODE,
        {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", ATTR_MODE: "locked"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert "SETAPMODE LOCKED 1" in server.received
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "locked"


async def test_select_option_sends_setapmode(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The select entity is a first-class way of changing the mode."""
    await _setup(hass, server, options=CONTROL_ON)
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.turniket_mode", "option": "unlocked"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert "SETAPMODE UNLOCKED 2" in server.received
    assert hass.states.get("select.turniket_mode").state == "unlocked"


async def test_set_mode_targets_a_device(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Targeting the access point device works as well as targeting an entity."""
    entry = await _setup(hass, server, options=CONTROL_ON)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_ap_2")}
    )
    assert device is not None
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ACCESS_POINT_MODE,
        {"device_id": device.id, ATTR_MODE: "normal"},
        blocking=True,
    )
    assert "SETAPMODE NORMAL 2" in server.received


async def test_set_mode_without_a_target_is_rejected(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A call with no Sigur target fails validation instead of doing nothing."""
    await _setup(hass, server, options=CONTROL_ON)
    with pytest.raises(ServiceValidationError, match="No Sigur access point"):
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_ACCESS_POINT_MODE, {ATTR_MODE: "locked"}, blocking=True
        )


async def test_setapmode_all_is_never_reachable_implicitly(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """``SETAPMODE ALL`` is never sent: ids are always listed explicitly."""
    await _setup(hass, server, options=CONTROL_ON)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ACCESS_POINT_MODE,
        {
            ATTR_ENTITY_ID: [
                "select.glavnyi_vkhod_mode",
                "select.turniket_mode",
            ],
            ATTR_MODE: "locked",
            ATTR_CONFIRM_ALL: True,
        },
        blocking=True,
    )
    assert "SETAPMODE LOCKED 1 2" in server.received
    assert not [line for line in server.received if line.endswith(" ALL")]


async def test_confirm_all_requires_every_access_point(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Claiming "all" while targeting a subset is refused."""
    await _setup(hass, server, options=CONTROL_ON)
    with pytest.raises(ServiceValidationError, match="not every access point"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ACCESS_POINT_MODE,
            {
                ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode",
                ATTR_MODE: "locked",
                ATTR_CONFIRM_ALL: True,
            },
            blocking=True,
        )


async def test_allow_pass_is_disabled_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Authorising a pass needs control to be enabled first."""
    await _setup(hass, server)
    with pytest.raises(HomeAssistantError, match="disabled"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ALLOW_PASS,
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode"},
            blocking=True,
        )
    assert not [line for line in server.received if line.startswith("ALLOWPASS")]


async def test_allow_pass_anonymous(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Without an object id the pass is anonymous."""
    await _setup(hass, server, options=CONTROL_ON)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ALLOW_PASS,
        {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", ATTR_DIRECTION: "in"},
        blocking=True,
    )
    assert "ALLOWPASS 1 ANONYMOUS IN" in server.received


async def test_allow_pass_with_an_object_id(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A numeric object id is passed through to ``ALLOWPASS``."""
    await _setup(hass, server, options=CONTROL_ON)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ALLOW_PASS,
        {
            ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode",
            ATTR_DIRECTION: "out",
            ATTR_OBJECT_ID: 6,
        },
        blocking=True,
    )
    assert "ALLOWPASS 1 6 OUT" in server.received


async def test_allow_pass_reports_an_unknown_object(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Error 7 from the server surfaces as a Home Assistant error."""
    await _setup(hass, server, options=CONTROL_ON)
    with pytest.raises(HomeAssistantError, match="refused"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ALLOW_PASS,
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", ATTR_OBJECT_ID: 4242},
            blocking=True,
        )


async def test_refresh_polls_the_server(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """``sigur.refresh`` triggers an immediate poll."""
    await _setup(hass, server)
    before = len([line for line in server.received if line.startswith("GETAPINFO")])
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REFRESH,
        {ATTR_ENTITY_ID: "binary_sensor.glavnyi_vkhod_link"},
        blocking=True,
    )
    await hass.async_block_till_done()
    after = len([line for line in server.received if line.startswith("GETAPINFO")])
    assert after > before


async def test_actions_are_scoped_to_their_own_server(hass: HomeAssistant) -> None:
    """A call against one system never touches another one's access point 1."""
    office = FakeSigurServer()
    depot = FakeSigurServer()
    await office.start()
    await depot.start()
    try:
        await _setup(hass, office, name="Sigur - Офис", options=CONTROL_ON)
        await _setup(hass, depot, name="Sigur - Склад", options=CONTROL_ON)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ACCESS_POINT_MODE,
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", ATTR_MODE: "locked"},
            blocking=True,
        )
        office_calls = [c for c in office.received if c.startswith("SETAPMODE")]
        depot_calls = [c for c in depot.received if c.startswith("SETAPMODE")]
        assert len(office_calls) + len(depot_calls) == 1
    finally:
        await office.stop()
        await depot.stop()
