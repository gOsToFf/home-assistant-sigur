"""Tests for the sidebar panel: registration, data and camera bindings."""

from __future__ import annotations

from homeassistant.components import frontend
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
import voluptuous as vol

from custom_components.sigur.const import (
    DOMAIN,
    OPT_ENABLE_CONTROL,
    PANEL_DATA_CHANGED,
    PANEL_URL_PATH,
    SERVICE_SET_CAMERA,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant


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


async def test_panel_is_registered(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The sidebar gains a Sigur entry once an entry is loaded."""
    await _setup(hass, server)
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]


async def test_panel_survives_a_second_entry(hass: HomeAssistant) -> None:
    """Two servers share one panel, and it outlives the first unload."""
    office = FakeSigurServer()
    depot = FakeSigurServer()
    await office.start()
    await depot.start()
    try:
        first = await _setup(hass, office, name="Sigur - Офис")
        await _setup(hass, depot, name="Sigur - Склад")
        assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]

        await hass.config_entries.async_unload(first.entry_id)
        await hass.async_block_till_done()
        # The second server still needs it.
        assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    finally:
        await office.stop()
        await depot.stop()


async def test_panel_is_removed_with_the_last_entry(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Unloading the only entry takes the sidebar entry away again."""
    entry = await _setup(hass, server)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]


async def test_panel_data_describes_servers_and_access_points(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """One call gives the panel the whole structure it needs to draw."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "sigur/panel/data"})
    response = await client.receive_json()

    assert response["success"]
    servers = response["result"]["servers"]
    assert len(servers) == 1
    first = servers[0]
    assert first["entry_id"] == entry.entry_id
    assert first["name"] == "Sigur - Офис"
    assert first["connected"] is True
    assert first["control_enabled"] is False
    assert {zone["name"] for zone in first["zones"]} == {"A", "B"}

    points = {ap["id"]: ap for ap in first["access_points"]}
    assert points[1]["name"] == "Главный вход"
    assert points[1]["zone_a_name"] == "A"
    assert points[1]["available"] is True


async def test_panel_data_carries_the_entity_ids(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The panel never has to guess an entity id from a name."""
    await _setup(hass, server, options={OPT_ENABLE_CONTROL: True})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "sigur/panel/data"})
    response = await client.receive_json()

    points = {ap["id"]: ap for ap in response["result"]["servers"][0]["access_points"]}
    entities = points[1]["entities"]
    assert entities["connectivity"] == "binary_sensor.glavnyi_vkhod_link"
    assert entities["door"] == "binary_sensor.glavnyi_vkhod_door"
    assert entities["mode"] == "select.glavnyi_vkhod_mode"
    assert entities["event"] == "event.glavnyi_vkhod_last_event"
    assert entities["allow_pass_in"] == "button.glavnyi_vkhod_allow_entry"


async def test_panel_data_reports_control_being_enabled(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The panel shows read-only chrome unless control is on."""
    await _setup(hass, server, options={OPT_ENABLE_CONTROL: True})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "sigur/panel/data"})
    response = await client.receive_json()
    assert response["result"]["servers"][0]["control_enabled"] is True


async def test_binding_a_camera_round_trips(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """A camera entity and an RTSP URL stick to the access point."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "camera_entity_id": "camera.entrance",
            "rtsp_url": "rtsp://10.0.0.5:554/live",
        }
    )
    response = await client.receive_json()
    assert response["success"]
    assert response["result"] == {
        "camera_entity_id": "camera.entrance",
        "rtsp_url": "rtsp://10.0.0.5:554/live",
        "direction_mode": "both",
    }

    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[1]["camera_entity_id"] == "camera.entrance"
    assert points[1]["rtsp_url"] == "rtsp://10.0.0.5:554/live"
    assert points[2]["camera_entity_id"] is None


async def test_a_binding_survives_a_reload(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """Bindings are persisted, not held only in memory."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 2,
            "camera_entity_id": "camera.turnstile",
        }
    )
    assert (await client.receive_json())["success"]

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[2]["camera_entity_id"] == "camera.turnstile"


async def test_a_binding_can_be_cleared(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """Sending nothing removes the binding rather than storing empty strings."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    for payload in (
        {"camera_entity_id": "camera.entrance"},
        {"camera_entity_id": None, "rtsp_url": None},
    ):
        await client.send_json_auto_id(
            {
                "type": "sigur/panel/set_binding",
                "entry_id": entry.entry_id,
                "access_point_id": 1,
                **payload,
            }
        )
        response = await client.receive_json()
        assert response["success"]
    assert response["result"] == {
        "camera_entity_id": None,
        "rtsp_url": None,
        "direction_mode": "both",
    }


async def test_binding_an_unknown_access_point_is_rejected(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """A point the server never reported cannot be bound."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 4242,
            "camera_entity_id": "camera.entrance",
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_binding_requires_admin(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """Attaching a camera is an administrative change."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "camera_entity_id": "camera.entrance",
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"


async def test_two_servers_appear_separately(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """Access point 1 of each server is listed under its own entry."""
    office = FakeSigurServer(access_points=[FakeAccessPoint(1, "Офис")])
    depot = FakeSigurServer(access_points=[FakeAccessPoint(1, "Склад")])
    await office.start()
    await depot.start()
    try:
        first = await _setup(hass, office, name="Sigur - Офис")
        second = await _setup(hass, depot, name="Sigur - Склад")

        client = await hass_ws_client(hass)
        await client.send_json_auto_id({"type": "sigur/panel/data"})
        servers = (await client.receive_json())["result"]["servers"]

        assert len(servers) == 2
        by_entry = {server["entry_id"]: server for server in servers}
        assert by_entry[first.entry_id]["access_points"][0]["name"] == "Офис"
        assert by_entry[second.entry_id]["access_points"][0]["name"] == "Склад"
        # The same access point number on both, with different entities.
        assert (
            by_entry[first.entry_id]["access_points"][0]["entities"]["mode"]
            != by_entry[second.entry_id]["access_points"][0]["entities"]["mode"]
        )
    finally:
        await office.stop()
        await depot.stop()


async def test_the_service_attaches_a_camera(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """``sigur.set_access_point_camera`` is the scriptable way to bind one."""
    entry = await _setup(hass, server)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CAMERA,
        {
            ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode",
            "camera_entity_id": "camera.entrance",
            "rtsp_url": "rtsp://user:password@10.0.0.5:554/stream",
        },
        blocking=True,
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[1]["camera_entity_id"] == "camera.entrance"
    assert points[1]["rtsp_url"] == "rtsp://user:password@10.0.0.5:554/stream"
    assert entry.entry_id


async def test_the_service_clears_a_binding(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """Calling it with neither field removes the binding."""
    await _setup(hass, server)
    for payload in (
        {"camera_entity_id": "camera.entrance"},
        {},
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CAMERA,
            {ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode", **payload},
            blocking=True,
        )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[1]["camera_entity_id"] is None


async def test_the_service_rejects_a_non_camera_entity(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Only a camera entity may be attached as one."""
    await _setup(hass, server)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CAMERA,
            {
                ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode",
                "camera_entity_id": "light.kitchen",
            },
            blocking=True,
        )


async def test_attaching_a_camera_does_not_need_control_enabled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Recording which camera watches a door opens nothing."""
    await _setup(hass, server)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CAMERA,
        {
            ATTR_ENTITY_ID: "select.glavnyi_vkhod_mode",
            "camera_entity_id": "camera.entrance",
        },
        blocking=True,
    )
    assert not [line for line in server.received if line.startswith("SETAPMODE")]


async def test_a_binding_follows_a_renamed_camera(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """An entity id is not stable, so the binding has to follow a rename.

    This is what broke in the field: the panel kept requesting a picture from
    the old entity id, and Home Assistant rejected every request.
    """
    entry = await _setup(hass, server)
    registry = er.async_get(hass)
    camera = registry.async_get_or_create(
        "camera", "generic", "unique-cam", suggested_object_id="barrier_one"
    )
    assert camera.entity_id == "camera.barrier_one"

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "camera_entity_id": camera.entity_id,
        }
    )
    assert (await client.receive_json())["success"]

    registry.async_update_entity(camera.entity_id, new_entity_id="camera.renamed")
    await hass.async_block_till_done()

    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[1]["camera_entity_id"] == "camera.renamed"


async def test_a_binding_is_dropped_when_the_camera_is_removed(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """A removed camera leaves no dangling reference behind."""
    entry = await _setup(hass, server)
    registry = er.async_get(hass)
    camera = registry.async_get_or_create(
        "camera", "generic", "unique-cam-2", suggested_object_id="barrier_two"
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "camera_entity_id": camera.entity_id,
            "rtsp_url": "rtsp://user:password@10.0.0.5:554/stream",
        }
    )
    assert (await client.receive_json())["success"]

    registry.async_remove(camera.entity_id)
    await hass.async_block_till_done()

    await client.send_json_auto_id({"type": "sigur/panel/data"})
    data = await client.receive_json()
    points = {ap["id"]: ap for ap in data["result"]["servers"][0]["access_points"]}
    assert points[1]["camera_entity_id"] is None
    # The RTSP URL is independent of the entity and survives.
    assert points[1]["rtsp_url"] == "rtsp://user:password@10.0.0.5:554/stream"


async def test_the_panel_is_told_when_its_data_goes_stale(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The panel caches structure, so a change has to announce itself."""
    entry = await _setup(hass, server)
    fired: list[Event] = []
    hass.bus.async_listen(PANEL_DATA_CHANGED, fired.append)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "camera_entity_id": "camera.entrance",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()

    assert [event.data["entry_id"] for event in fired] == [entry.entry_id]
