"""Event bus, event entity and device trigger tests."""

from __future__ import annotations

import asyncio
from datetime import datetime

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest

from custom_components.sigur.const import (
    DOMAIN,
    EVENT_SIGUR,
    OPT_DEBUG_RAW_EVENTS,
    OPT_ENABLE_PERSONAL_DATA,
    OPT_EVENT_CATEGORIES,
    OPT_RESOLVE_OBJECT_NAMES,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeBehaviour, FakeEvent, FakeSigurServer
from .helpers import make_entry

pytestmark = requires_home_assistant

#: Timestamp used by every pushed event in this module.
WHEN = datetime(2025, 1, 27, 11, 23, 8)


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


def _capture(hass: HomeAssistant) -> list[Event]:
    """Record every ``sigur_event`` fired on the bus."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_SIGUR, events.append)
    return events


async def _push(hass: HomeAssistant, fake: FakeSigurServer, event: FakeEvent) -> None:
    """Push an event and let the pipeline drain."""
    await fake.push_event(event)
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def test_a_pushed_event_reaches_the_bus(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A ``EVENT_CE`` pass is published as a normalized ``sigur_event``."""
    entry = await _setup(hass, server)
    events = _capture(hass)

    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))

    assert len(events) == 1
    data = events[0].data
    assert data["server_entry_id"] == entry.entry_id
    assert data["server_name"] == "Sigur - Офис"
    assert data["event_code"] == 4
    assert data["category"] == "pass_registered"
    assert data["access_point_id"] == 1
    assert data["access_point_name"] == "Главный вход"
    assert data["direction"] == "in"
    assert data["direction_code"] == 2
    assert data["key_masked"] == "W26 ***23"
    assert "raw_message" not in data


async def test_personal_data_is_withheld_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The object name is not published unless the user opted in."""
    await _setup(hass, server)
    events = _capture(hass)
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))
    assert events[0].data["object_name"] is None
    # The credential number is never published in full, opt-in or not.
    assert "29323" not in str(events[0].data)


async def test_personal_data_is_published_when_enabled(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """With the option on, the name from ``CE_WITH_NAMES`` is published."""
    await _setup(hass, server, options={OPT_ENABLE_PERSONAL_DATA: True})
    events = _capture(hass)
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2, object_name="Иванов"))
    assert events[0].data["object_name"] == "Иванов"
    assert events[0].data["object_id"] == 6


async def test_object_names_can_be_resolved_lazily(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An event without a name triggers a single ``GETOBJECTINFO`` lookup."""
    await _setup(
        hass,
        server,
        options={OPT_ENABLE_PERSONAL_DATA: True, OPT_RESOLVE_OBJECT_NAMES: True},
    )
    events = _capture(hass)
    for _ in range(3):
        await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))

    assert events[0].data["object_name"] == "Иванов Иван"
    lookups = [line for line in server.received if line.startswith("GETOBJECTINFO")]
    assert len(lookups) == 1, "the resolved name must be cached"


async def test_object_names_are_not_resolved_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Without the option, no directory lookup is ever issued."""
    await _setup(hass, server, options={OPT_ENABLE_PERSONAL_DATA: True})
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert not [line for line in server.received if line.startswith("GETOBJECTINFO")]


async def test_raw_message_is_opt_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The raw protocol line is only published in debug mode."""
    await _setup(hass, server, options={OPT_DEBUG_RAW_EVENTS: True})
    events = _capture(hass)
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert events[0].data["raw_message"].startswith("EVENT_CE ")


async def test_event_categories_can_be_filtered(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Only the selected categories reach the bus."""
    await _setup(hass, server, options={OPT_EVENT_CATEGORIES: ["break_in"]})
    events = _capture(hass)
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))  # pass_registered
    assert events == []
    await _push(hass, server, FakeEvent(WHEN, 1, 1, 0, 0))  # break_in
    assert len(events) == 1
    assert events[0].data["category"] == "break_in"


async def test_unknown_event_codes_are_published_not_dropped(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A future numeric code is published as ``unknown`` with its number."""
    await _setup(hass, server)
    events = _capture(hass)
    await server.push_raw('EVENT_CE "2025-01-27 11:23:08" 4242 1 6 2 UNKNOWN')
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["event_code"] == 4242
    assert events[0].data["category"] == "unknown"
    assert events[0].data["event_type"] == "unknown"


async def test_alarm_panel_codes_are_published_with_their_sub_code(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An extended ``768+N`` code resolves to the Rubezh security range."""
    await _setup(hass, server)
    events = _capture(hass)
    await _push(hass, server, FakeEvent(WHEN, 769, 1, 0, 0))
    assert events[0].data["category"] == "alarm_panel"
    assert events[0].data["event_type"] == "ops_rubezh_security_1"


async def test_a_malformed_event_does_not_break_the_stream(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A broken line is counted and the next good event still arrives."""
    entry = await _setup(hass, server)
    events = _capture(hass)
    await server.push_raw('EVENT_CE "broken" 4 1 6 2 W26 249 29323')
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    assert len(events) == 1
    assert entry.runtime_data.hub.event_connection.stats.protocol_error_count == 1


async def test_door_events_update_the_binary_sensor_immediately(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Door open/close events patch the state without waiting for a poll."""
    await _setup(hass, server)
    assert hass.states.get("binary_sensor.glavnyi_vkhod_door").state == "off"

    await _push(hass, server, FakeEvent(WHEN, 37, 1, 0, 0))  # door opened
    assert hass.states.get("binary_sensor.glavnyi_vkhod_door").state == "on"

    await _push(hass, server, FakeEvent(WHEN, 36, 1, 0, 0))  # door closed
    assert hass.states.get("binary_sensor.glavnyi_vkhod_door").state == "off"


async def test_mode_change_events_update_the_select(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A mode-change event moves the select without a poll."""
    await _setup(hass, server)
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "normal"
    await _push(hass, server, FakeEvent(WHEN, 31, 1, 0, 0))  # locked
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "locked"


async def test_link_lost_event_marks_the_point_offline(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Losing the link is reflected immediately by the connectivity sensor."""
    await _setup(hass, server)
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "on"
    await _push(hass, server, FakeEvent(WHEN, 20, 1, 0, 0))
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "off"


async def test_link_restored_event_re_reads_the_access_point(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A restore event asks the server for the real mode instead of guessing."""
    await _setup(hass, server)
    await _push(hass, server, FakeEvent(WHEN, 20, 1, 0, 0))
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "off"

    server.access_points[1].state = "ONLINE_UNLOCKED"
    await _push(hass, server, FakeEvent(WHEN, 21, 1, 0, 0))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "on"
    assert hass.states.get("select.glavnyi_vkhod_mode").state == "unlocked"


async def test_event_entity_records_the_last_event(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The event entity carries the category and the numeric code."""
    await _setup(hass, server)
    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))
    state = hass.states.get("event.glavnyi_vkhod_last_event")
    assert state is not None
    assert state.attributes["event_type"] == "pass_registered"
    assert state.attributes["event_code"] == 4
    assert state.attributes["key_masked"] == "W26 ***23"
    assert "object_name" not in state.attributes


async def test_event_entity_only_sees_its_own_access_point(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An event for AP 2 does not touch the entity of AP 1."""
    await _setup(hass, server)
    await _push(hass, server, FakeEvent(WHEN, 4, 2, 6, 2))
    assert hass.states.get("event.glavnyi_vkhod_last_event").state == "unknown"
    assert hass.states.get("event.turniket_last_event").state != "unknown"


async def test_classic_subscription_events_are_normalized(hass: HomeAssistant) -> None:
    """A server that only supports bare ``SUBSCRIBE`` produces the same shape."""
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(supported_subscribe_modes={"CLASSIC"})
    )
    await fake.start()
    try:
        await _setup(hass, fake)
        events = _capture(hass)
        await _push(hass, fake, FakeEvent(WHEN, 4, 1, 6, 2))
        assert len(events) == 1
        assert events[0].data["category"] == "pass_registered"
        assert events[0].data["direction"] == "in"
        assert events[0].data["event_code"] is None
    finally:
        await fake.stop()


async def test_device_triggers_are_offered_for_access_points(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Every category is available as a device trigger on an access point."""
    from homeassistant.components.device_automation import (
        DeviceAutomationType,
        async_get_device_automations,
    )

    assert await async_setup_component(hass, "device_automation", {})
    entry = await _setup(hass, server)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_ap_1")}
    )
    assert device is not None

    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, [device.id]
    )
    types = {trigger["type"] for trigger in triggers[device.id]}
    for expected in (
        "pass_registered",
        "access_denied",
        "break_in",
        "door_opened",
        "door_closed",
        "door_held_open_start",
        "door_held_open_end",
        "link_lost",
        "link_restored",
        "mode_changed",
        "lock_fault",
        "power_mains",
        "power_battery",
        "unknown",
    ):
        assert expected in types


async def test_device_trigger_fires_an_automation(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A device trigger only fires for its own access point and category."""
    assert await async_setup_component(hass, "device_automation", {})
    entry = await _setup(hass, server)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_ap_1")}
    )
    assert device is not None

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device.id,
                        "type": "break_in",
                    },
                    "action": {
                        "event": "sigur_test_fired",
                        "event_data": {
                            "id": "{{ trigger.event.data.access_point_id }}"
                        },
                    },
                }
            ]
        },
    )
    fired: list[Event] = []
    hass.bus.async_listen("sigur_test_fired", fired.append)

    await _push(hass, server, FakeEvent(WHEN, 4, 1, 6, 2))  # wrong category
    assert fired == []

    await _push(hass, server, FakeEvent(WHEN, 1, 2, 0, 0))  # wrong access point
    assert fired == []

    await _push(hass, server, FakeEvent(WHEN, 1, 1, 0, 0))
    await hass.async_block_till_done()
    assert len(fired) == 1
    assert fired[0].data["id"] == 1
