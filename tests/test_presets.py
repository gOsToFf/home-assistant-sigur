"""Tests for panel presets and the per-user view they produce."""

from __future__ import annotations

from typing import Any

from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
import pytest

from custom_components.sigur.const import EVENT_SIGUR, PRESETS_STORAGE_KEY
from custom_components.sigur.presets import Access, Preset

from .conftest import requires_home_assistant
from .fake_oif_server import FakeAccessPoint, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant


@pytest.fixture
async def server() -> FakeSigurServer:
    """A fake server with two zones: points 1 and 2 in zone 1, point 3 in 5."""
    fake = FakeSigurServer(
        access_points=[
            FakeAccessPoint(1, "Въезд 1", zone_a=1),
            FakeAccessPoint(2, "Въезд 2", zone_a=1),
            FakeAccessPoint(3, "Проходная", zone_a=5),
        ]
    )
    await fake.start()
    yield fake
    await fake.stop()


async def _setup(hass: HomeAssistant, fake: FakeSigurServer):
    entry = make_entry(fake.port)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call(client, payload: dict[str, Any]) -> dict[str, Any]:
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def _save(client, **fields: Any) -> dict[str, Any]:
    response = await _call(client, {"type": "sigur/presets/save", **fields})
    assert response["success"], response
    return response["result"]


def _point_ids(result: dict[str, Any]) -> list[int]:
    return [ap["id"] for server in result["servers"] for ap in server["access_points"]]


# --- the access rule on its own ----------------------------------------------


def test_a_preset_covers_its_points_and_its_zones() -> None:
    """Zone membership follows the zone, so later points join by themselves."""
    preset = Preset(id="p", name="P", access_points=["E:3"], zones=["E:1"])
    assert preset.covers("E", 3, 5)
    assert preset.covers("E", 9, 1)
    assert not preset.covers("E", 4, 5)
    assert not preset.covers("other", 9, 1)


def test_unrestricted_access_allows_everything() -> None:
    """Nobody is limited until a preset names them."""
    assert Access(presets=[], restricting=None).allows("E", 1, None)


# --- administration ----------------------------------------------------------


async def test_an_admin_creates_edits_and_deletes_a_preset(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """The whole life of a preset, as an administrator sees it."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)

    created = await _save(
        client, name="  Парковка ", access_points=[f"{entry.entry_id}:1"]
    )
    assert created["name"] == "Парковка"
    assert created["users"] == []

    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert [p["name"] for p in data["presets"]] == ["Парковка"]
    assert data["restricted"] is False

    renamed = await _save(
        client,
        preset_id=created["id"],
        name="Въезды",
        zones=[f"{entry.entry_id}:1"],
    )
    assert renamed["id"] == created["id"]
    assert renamed["access_points"] == []

    response = await _call(
        client, {"type": "sigur/presets/delete", "preset_id": created["id"]}
    )
    assert response["success"]
    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert data["presets"] == []


async def test_presets_persist(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client, hass_storage
) -> None:
    """A saved preset is written to storage."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass)
    await _save(client, name="Парковка", access_points=[f"{entry.entry_id}:1"])
    await hass.async_block_till_done()

    stored = hass_storage[PRESETS_STORAGE_KEY]["data"]["presets"]
    assert [item["name"] for item in stored] == ["Парковка"]


async def test_stored_presets_are_loaded(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client, hass_storage
) -> None:
    """Presets written by an earlier run are there after a restart."""
    hass_storage[PRESETS_STORAGE_KEY] = {
        "version": 1,
        "key": PRESETS_STORAGE_KEY,
        "data": {"presets": [{"id": "abc", "name": "Склад", "zones": ["x:1"]}]},
    }
    await _setup(hass, server)
    client = await hass_ws_client(hass)

    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert data["presets"] == [
        {
            "id": "abc",
            "name": "Склад",
            "access_points": [],
            "zones": ["x:1"],
            "users": [],
        }
    ]


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"name": "   "}, "invalid_format"),
        ({"name": "X", "preset_id": "missing"}, "not_found"),
        ({"name": "X", "users": ["no-such-user"]}, "invalid_format"),
    ],
)
async def test_a_bad_preset_is_rejected(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    fields: dict[str, Any],
    code: str,
) -> None:
    """A nameless preset, an unknown one, or an unknown user is refused."""
    await _setup(hass, server)
    client = await hass_ws_client(hass)
    response = await _call(client, {"type": "sigur/presets/save", **fields})
    assert response["success"] is False
    assert response["error"]["code"] == code


async def test_deleting_an_unknown_preset_is_rejected(
    hass: HomeAssistant, server: FakeSigurServer, hass_ws_client
) -> None:
    """There is nothing to delete."""
    await _setup(hass, server)
    client = await hass_ws_client(hass)
    response = await _call(
        client, {"type": "sigur/presets/delete", "preset_id": "missing"}
    )
    assert response["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "sigur/presets/save", "name": "X"},
        {"type": "sigur/presets/delete", "preset_id": "x"},
    ],
)
async def test_managing_presets_requires_admin(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
    payload: dict[str, Any],
) -> None:
    """Only an administrator decides who sees what."""
    await _setup(hass, server)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    response = await _call(client, payload)
    assert response["error"]["code"] == "unauthorized"


# --- what a non-admin is handed ----------------------------------------------


async def test_a_user_in_no_preset_sees_everything(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
    hass_admin_user: User,
) -> None:
    """Without an assignment nothing changes; shared views are offered.

    A preset assigned to somebody else is not offered: it is that user's.
    """
    entry = await _setup(hass, server)
    admin = await hass_ws_client(hass)
    await _save(admin, name="Общий", access_points=[f"{entry.entry_id}:1"])
    await _save(
        admin,
        name="Чужой",
        access_points=[f"{entry.entry_id}:2"],
        users=[hass_admin_user.id],
    )

    client = await hass_ws_client(hass, hass_read_only_access_token)
    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert _point_ids(data) == [1, 2, 3]
    assert data["restricted"] is False
    assert [p["name"] for p in data["presets"]] == ["Общий"]
    assert "users" not in data["presets"][0]


async def test_an_assigned_user_sees_only_their_presets(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
    hass_read_only_user: User,
) -> None:
    """The union of the user's presets: a point, plus a whole zone."""
    entry = await _setup(hass, server)
    admin = await hass_ws_client(hass)
    await _save(admin, name="Общий", access_points=[f"{entry.entry_id}:2"])
    await _save(
        admin,
        name="Проходная",
        zones=[f"{entry.entry_id}:5"],
        users=[hass_read_only_user.id],
    )
    await _save(
        admin,
        name="Въезд 1",
        access_points=[f"{entry.entry_id}:1"],
        users=[hass_read_only_user.id],
    )

    client = await hass_ws_client(hass, hass_read_only_access_token)
    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert _point_ids(data) == [1, 3]
    assert data["restricted"] is True
    assert [p["name"] for p in data["presets"]] == ["Въезд 1", "Проходная"]

    # The administrator still sees everything.
    data = (await _call(admin, {"type": "sigur/panel/data"}))["result"]
    assert _point_ids(data) == [1, 2, 3]


async def test_a_server_with_nothing_allowed_is_left_out(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    hass_read_only_user: User,
) -> None:
    """Not even the name of a server the user has nothing on is sent."""
    office = FakeSigurServer(access_points=[FakeAccessPoint(1, "Офис")])
    depot = FakeSigurServer(access_points=[FakeAccessPoint(1, "Склад")])
    await office.start()
    await depot.start()
    try:
        first = await _setup(hass, office)
        await _setup(hass, depot)
        admin = await hass_ws_client(hass)
        await _save(
            admin,
            name="Офис",
            access_points=[f"{first.entry_id}:1"],
            users=[hass_read_only_user.id],
        )

        client = await hass_ws_client(hass, hass_read_only_access_token)
        data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
        assert [s["entry_id"] for s in data["servers"]] == [first.entry_id]
    finally:
        await office.stop()
        await depot.stop()


async def test_stream_urls_are_for_admins_only(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """An RTSP URL usually carries a password; only the admin editor needs it."""
    entry = await _setup(hass, server)
    admin = await hass_ws_client(hass)
    response = await _call(
        admin,
        {
            "type": "sigur/panel/set_binding",
            "entry_id": entry.entry_id,
            "access_point_id": 1,
            "rtsp_url": "rtsp://user:secret@10.0.0.5/stream",
        },
    )
    assert response["success"]

    def first_url(data: dict[str, Any]) -> str | None:
        return data["servers"][0]["access_points"][0]["rtsp_url"]

    data = (await _call(admin, {"type": "sigur/panel/data"}))["result"]
    assert first_url(data) == "rtsp://user:secret@10.0.0.5/stream"

    client = await hass_ws_client(hass, hass_read_only_access_token)
    data = (await _call(client, {"type": "sigur/panel/data"}))["result"]
    assert first_url(data) is None


# --- the event stream --------------------------------------------------------


async def test_the_stream_carries_only_allowed_events(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
    hass_read_only_user: User,
) -> None:
    """A restricted user hears about their points, and about their server."""
    entry = await _setup(hass, server)
    admin = await hass_ws_client(hass)
    await _save(
        admin,
        name="Въезд 1",
        access_points=[f"{entry.entry_id}:1"],
        users=[hass_read_only_user.id],
    )

    client = await hass_ws_client(hass, hass_read_only_access_token)
    response = await _call(client, {"type": "sigur/panel/subscribe"})
    assert response["success"]
    subscription = response["id"]

    for ap_id in (2, 1, None):
        hass.bus.async_fire(
            EVENT_SIGUR,
            {"server_entry_id": entry.entry_id, "access_point_id": ap_id},
        )
    hass.bus.async_fire(
        EVENT_SIGUR, {"server_entry_id": "elsewhere", "access_point_id": 1}
    )
    await hass.async_block_till_done()

    first = await client.receive_json()
    second = await client.receive_json()
    assert first["id"] == subscription
    assert first["event"] == {
        "type": "event",
        "event": {"server_entry_id": entry.entry_id, "access_point_id": 1},
    }
    assert second["event"]["event"]["access_point_id"] is None


async def test_a_non_admin_can_subscribe_and_hears_everything_unassigned(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """Home Assistant refuses a non-admin `sigur_event`; this stream does not."""
    entry = await _setup(hass, server)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _call(client, {"type": "sigur/panel/subscribe"}))["success"]

    hass.bus.async_fire(
        EVENT_SIGUR, {"server_entry_id": entry.entry_id, "access_point_id": 2}
    )
    await hass.async_block_till_done()
    message = await client.receive_json()
    assert message["event"]["event"]["access_point_id"] == 2


async def test_the_stream_says_when_presets_change(
    hass: HomeAssistant,
    server: FakeSigurServer,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """An open panel learns that it has to reload."""
    await _setup(hass, server)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _call(client, {"type": "sigur/panel/subscribe"}))["success"]

    admin = await hass_ws_client(hass)
    await _save(admin, name="Новый")
    await hass.async_block_till_done()

    message = await client.receive_json()
    assert message["event"] == {"type": "changed"}
