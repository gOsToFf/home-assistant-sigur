"""Reconnect, resubscribe, history backfill and de-duplication tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest

from custom_components.sigur.const import (
    EVENT_SIGUR,
    OPT_BACKFILL_HOURS,
    OPT_BACKFILL_ON_FIRST_START,
    OPT_ENABLE_BACKFILL,
)

from .conftest import requires_home_assistant
from .fake_oif_server import FakeBehaviour, FakeEvent, FakeSigurServer, make_events
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
    """Add and set up a config entry pointing at ``fake``, with fast backoff."""
    entry = make_entry(fake.port, **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data.hub
    hub.backoff.initial = 0.01
    hub.backoff.maximum = 0.05
    hub.backoff.jitter = lambda: 1.0
    return entry


async def _settle(hass: HomeAssistant, rounds: int = 40) -> None:
    """Let the supervisor, the reader and the event worker catch up."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()


def _capture(hass: HomeAssistant) -> list[Event]:
    """Record every ``sigur_event`` fired on the bus."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_SIGUR, events.append)
    return events


async def test_event_connection_reconnects_and_resubscribes(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A dropped event connection is re-established and re-subscribed."""
    entry = await _setup(hass, server)
    assert server.subscriber_count == 1
    logins_before = server.login_count

    await server.drop_all_connections()
    await _settle(hass)

    assert server.login_count > logins_before
    assert server.subscriber_count == 1
    assert entry.runtime_data.hub.subscribe_mode is not None

    events = _capture(hass)
    await server.push_event(FakeEvent(dt_util.now().replace(tzinfo=None), 4, 1, 6, 2))
    await _settle(hass, rounds=10)
    assert len(events) == 1


async def test_command_connection_reconnects_on_the_next_poll(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A dropped command connection is reopened by the coordinator."""
    entry = await _setup(hass, server)
    await server.drop_all_connections()
    await _settle(hass, rounds=10)

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.coordinator.last_update_success
    assert hass.states.get("binary_sensor.glavnyi_vkhod_link").state == "on"


async def test_backfill_is_off_by_default(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """No ``GETHISTORY`` is issued unless the user enabled backfill."""
    await _setup(hass, server)
    await server.drop_all_connections()
    await _settle(hass)
    assert not [line for line in server.received if line.startswith("GETHISTORY")]


async def test_backfill_does_not_import_history_on_a_first_start(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """A fresh install does not replay old events into the recorder."""
    server.history = make_events(5, start=dt_util.now().replace(tzinfo=None))
    await _setup(hass, server, options={OPT_ENABLE_BACKFILL: True})
    await _settle(hass, rounds=10)
    assert not [line for line in server.received if line.startswith("GETHISTORY")]


async def test_backfill_on_first_start_is_opt_in(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """With the extra option on, the first start does replay history."""
    now = dt_util.now().replace(tzinfo=None)
    server.history = make_events(3, start=now - timedelta(minutes=5))
    events = _capture(hass)
    await _setup(
        hass,
        server,
        options={
            OPT_ENABLE_BACKFILL: True,
            OPT_BACKFILL_ON_FIRST_START: True,
            OPT_BACKFILL_HOURS: 1,
        },
    )
    await _settle(hass, rounds=20)
    assert [line for line in server.received if line.startswith("GETHISTORY")]
    assert len(events) == 3


async def test_backfill_recovers_events_missed_while_offline(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """Events that happened during an outage are replayed after reconnect."""
    now = dt_util.now().replace(tzinfo=None)
    entry = await _setup(
        hass,
        server,
        options={OPT_ENABLE_BACKFILL: True, OPT_BACKFILL_HOURS: 1},
    )
    hub = entry.runtime_data.hub
    # Pretend the integration has already seen an event, so the first-start
    # guard does not apply.
    hub.last_event_at = dt_util.as_local(now - timedelta(minutes=10))

    events = _capture(hass)
    server.history = make_events(4, start=now - timedelta(minutes=5))

    await server.drop_all_connections()
    await _settle(hass)

    assert [line for line in server.received if line.startswith("GETHISTORY")]
    assert len(events) == 4
    # GETHISTORY only answers in the classic format, so a backfilled event
    # carries no numeric EVENT_CE code - only the normalized category.
    assert {event.data["event_code"] for event in events} == {None}
    assert {event.data["category"] for event in events} == {"pass_registered"}


async def test_backfill_deduplicates_against_live_events(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """An event delivered both live and by history is published once."""
    now = dt_util.now().replace(tzinfo=None)
    entry = await _setup(
        hass,
        server,
        options={OPT_ENABLE_BACKFILL: True, OPT_BACKFILL_HOURS: 1},
    )
    hub = entry.runtime_data.hub
    hub.last_event_at = dt_util.as_local(now - timedelta(minutes=10))

    events = _capture(hass)
    live = make_events(2, start=now - timedelta(minutes=2))
    for event in live:
        await server.push_event(event)
    await _settle(hass, rounds=10)
    assert len(events) == 2

    # The same two events are also in history when the connection comes back.
    server.history = live
    await server.drop_all_connections()
    await _settle(hass)

    assert [line for line in server.received if line.startswith("GETHISTORY")]
    assert len(events) == 2, "the replayed events must be de-duplicated"


async def test_backfill_window_is_bounded(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The requested history window never exceeds the configured maximum."""
    now = dt_util.now().replace(tzinfo=None)
    entry = await _setup(
        hass,
        server,
        options={OPT_ENABLE_BACKFILL: True, OPT_BACKFILL_HOURS: 2},
    )
    entry.runtime_data.hub.last_event_at = dt_util.as_local(now - timedelta(days=30))

    await server.drop_all_connections()
    await _settle(hass)

    requests = [line for line in server.received if line.startswith("GETHISTORY")]
    assert requests
    start = datetime.strptime(requests[-1].split('"')[1], "%Y-%m-%d %H:%M:%S")
    assert now - start <= timedelta(hours=2, minutes=1)


async def test_last_event_timestamp_survives_a_restart(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The last processed event time is persisted and reloaded."""
    now = dt_util.now().replace(tzinfo=None)
    entry = await _setup(
        hass, server, options={OPT_ENABLE_BACKFILL: True, OPT_BACKFILL_HOURS: 1}
    )
    hub = entry.runtime_data.hub
    hub.last_event_at = dt_util.as_local(now - timedelta(minutes=3))
    await server.push_event(FakeEvent(now, 4, 1, 6, 2))
    await _settle(hass, rounds=10)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.hub.last_event_at is not None
    # OIF timestamps have one-second resolution.
    assert entry.runtime_data.hub.last_event_at.replace(tzinfo=None) >= now.replace(
        microsecond=0
    )


async def test_a_failed_backfill_does_not_break_the_subscription(
    hass: HomeAssistant,
) -> None:
    """If ``GETHISTORY`` errors, live events keep flowing."""
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(error_on_commands={"GETHISTORY": (8, "INTERNAL ERROR")})
    )
    await fake.start()
    try:
        now = dt_util.now().replace(tzinfo=None)
        entry = await _setup(
            hass, fake, options={OPT_ENABLE_BACKFILL: True, OPT_BACKFILL_HOURS: 1}
        )
        entry.runtime_data.hub.last_event_at = dt_util.as_local(
            now - timedelta(minutes=5)
        )
        await fake.drop_all_connections()
        await _settle(hass)

        events = _capture(hass)
        await fake.push_event(FakeEvent(now, 4, 1, 6, 2))
        await _settle(hass, rounds=10)
        assert len(events) == 1
    finally:
        await fake.stop()


async def test_reconnect_counter_is_exposed(
    hass: HomeAssistant, server: FakeSigurServer
) -> None:
    """The reconnect count is carried across event connections."""
    entry = await _setup(hass, server)
    await server.drop_all_connections()
    await _settle(hass)
    assert entry.runtime_data.hub.event_connection.stats.reconnect_count >= 1
