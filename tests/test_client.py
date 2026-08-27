"""Tests for the OIF connection: login, dispatch, framing, TLS and backoff."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from custom_components.sigur.api.client import (
    BackoffController,
    Credentials,
    OifConnection,
    SubscribeMode,
    run_with_backoff,
)
from custom_components.sigur.api.commands import SigurApi
from custom_components.sigur.api.errors import (
    SigurAuthError,
    SigurConnectionError,
    SigurPermissionError,
    SigurTimeoutError,
    SigurTlsError,
    SigurUnknownAccessPointError,
    SigurUnsupportedVersionError,
)
from custom_components.sigur.api.parser import ApMode, ApState, Direction, RawEvent
from custom_components.sigur.api.transport import TlsSettings, TransportSettings

from .fake_oif_server import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    FakeAccessPoint,
    FakeBehaviour,
    FakeEvent,
    FakeSigurServer,
    make_events,
    self_signed_context,
)


async def _start(server: FakeSigurServer) -> int:
    """Start ``server`` and register its shutdown."""
    return await server.start()


def _settings(port: int, **kwargs: object) -> TransportSettings:
    """Build transport settings pointing at the fake server."""
    return TransportSettings(host="127.0.0.1", port=port, **kwargs)  # type: ignore[arg-type]


async def _connect(
    server: FakeSigurServer,
    *,
    events: list[RawEvent] | None = None,
    password: str = DEFAULT_PASSWORD,
    ssl_context: object = None,
    command_timeout: float = 5.0,
) -> OifConnection:
    """Start the fake server and return a logged-in connection to it."""
    port = await _start(server)
    connection = OifConnection(
        _settings(port, command_timeout=command_timeout),
        Credentials(DEFAULT_USERNAME, password),
        ssl_context=ssl_context,
        event_callback=(events.append if events is not None else None),
    )
    await connection.connect()
    return connection


@pytest.fixture
async def server() -> FakeSigurServer:
    """A running fake OIF server, torn down after the test."""
    fake = FakeSigurServer()
    yield fake
    await fake.stop()


async def test_login_succeeds_and_is_the_first_request(server: FakeSigurServer) -> None:
    """``LOGIN`` is sent immediately after the TCP connection is opened."""
    connection = await _connect(server)
    try:
        assert connection.connected is True
        assert server.login_count == 1
        assert server.received[0].startswith("LOGIN 1.8 ")
    finally:
        await connection.close()


async def test_login_never_logs_the_password(
    server: FakeSigurServer, caplog: pytest.LogCaptureFixture
) -> None:
    """The password must not reach the debug log."""
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.sigur.api.client")
    connection = await _connect(server)
    try:
        assert DEFAULT_PASSWORD not in caplog.text
        assert "LOGIN <redacted>" in caplog.text
    finally:
        await connection.close()


async def test_bad_credentials_raise_auth_error(server: FakeSigurServer) -> None:
    """Error 11 becomes :class:`SigurAuthError`."""
    with pytest.raises(SigurAuthError) as excinfo:
        await _connect(server, password="wrong")
    assert excinfo.value.code == 11


async def test_oif_disabled_for_user_raises_permission_error() -> None:
    """Error 21 becomes :class:`SigurPermissionError`."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(oif_disabled=True))
    try:
        with pytest.raises(SigurPermissionError) as excinfo:
            await _connect(fake)
        assert excinfo.value.code == 21
    finally:
        await fake.stop()


async def test_unsupported_version_raises_its_own_error() -> None:
    """Error 3 is distinguishable from a plain authentication failure."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(unsupported_version=True))
    try:
        with pytest.raises(SigurUnsupportedVersionError):
            await _connect(fake)
    finally:
        await fake.stop()


async def test_connection_refused_raises_connection_error() -> None:
    """An unreachable server produces a connection error, not a crash."""
    connection = OifConnection(
        TransportSettings(host="127.0.0.1", port=1, connect_timeout=2.0),
        Credentials(DEFAULT_USERNAME, DEFAULT_PASSWORD),
    )
    with pytest.raises(SigurConnectionError):
        await connection.connect()


async def test_discovery_round_trip(server: FakeSigurServer) -> None:
    """Zones, the access point list and per-point info all round-trip."""
    connection = await _connect(server)
    api = SigurApi(connection)
    try:
        zones = await api.get_zones()
        assert [z.name for z in zones] == ["A", "B"]
        ap_ids = await api.get_access_point_ids()
        assert ap_ids == [1, 2]
        info = await api.get_access_point(1)
        assert info.name == "Главный вход"
        assert info.state is ApState.ONLINE_NORMAL
    finally:
        await connection.close()


async def test_unknown_access_point_raises_a_typed_error(
    server: FakeSigurServer,
) -> None:
    """Error 10 is mapped to a dedicated exception."""
    connection = await _connect(server)
    try:
        with pytest.raises(SigurUnknownAccessPointError):
            await SigurApi(connection).get_access_point(99)
    finally:
        await connection.close()


async def test_fragmented_lines_are_reassembled() -> None:
    """A reply split across many TCP segments still parses as one message."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(fragment_lines=True))
    connection = await _connect(fake)
    try:
        info = await SigurApi(connection).get_access_point(1)
        assert info.name == "Главный вход"
    finally:
        await connection.close()
        await fake.stop()


async def test_coalesced_lines_are_split_again() -> None:
    """Several messages arriving in one packet are handled independently."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(coalesce=True))
    connection = await _connect(fake)
    api = SigurApi(connection)
    try:
        assert await api.get_access_point_ids() == [1, 2]
        assert (await api.get_access_point(2)).id == 2
    finally:
        await connection.close()
        await fake.stop()


async def test_events_do_not_get_mistaken_for_replies() -> None:
    """An event pushed between commands never lands in a command reply."""
    events: list[RawEvent] = []
    pushed = make_events(3, start=datetime(2025, 1, 27, 11, 23, 8))
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(push_events_after={"GETAPINFO": pushed})
    )
    connection = await _connect(fake, events=events)
    api = SigurApi(connection)
    try:
        await connection.subscribe()
        for _ in range(5):
            info = await api.get_access_point(1)
            assert info.id == 1
        assert len(events) == 15
        assert {e.event_code for e in events} == {4}
        assert connection.stats.unsolicited_line_count == 0
    finally:
        await connection.close()
        await fake.stop()


async def test_subscribe_prefers_ce_with_names(server: FakeSigurServer) -> None:
    """``CE_WITH_NAMES`` is chosen when the server supports it."""
    connection = await _connect(server)
    try:
        assert await connection.subscribe() is SubscribeMode.CE_WITH_NAMES
        assert server.subscriptions == ["CE_WITH_NAMES"]
    finally:
        await connection.close()


async def test_subscribe_falls_back_to_ce() -> None:
    """A server that rejects ``CE_WITH_NAMES`` gets ``CE`` instead."""
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(supported_subscribe_modes={"CE", "CLASSIC"})
    )
    connection = await _connect(fake)
    try:
        assert await connection.subscribe() is SubscribeMode.CE
    finally:
        await connection.close()
        await fake.stop()


async def test_subscribe_falls_back_to_classic() -> None:
    """A server that only knows bare ``SUBSCRIBE`` still works."""
    fake = FakeSigurServer(
        behaviour=FakeBehaviour(supported_subscribe_modes={"CLASSIC"})
    )
    events: list[RawEvent] = []
    connection = await _connect(fake, events=events)
    try:
        assert await connection.subscribe() is SubscribeMode.CLASSIC
        await fake.push_event(FakeEvent(datetime(2025, 1, 27, 11, 23, 8), 4, 1, 6, 2))
        await asyncio.sleep(0.05)
        assert len(events) == 1
        assert events[0].classic_type == "OBJECTPASS"
        assert events[0].direction == "in"
    finally:
        await connection.close()
        await fake.stop()


async def test_pushed_ce_events_reach_the_callback(server: FakeSigurServer) -> None:
    """Real-time ``EVENT_CE`` lines are delivered with names decoded."""
    events: list[RawEvent] = []
    connection = await _connect(server, events=events)
    try:
        await connection.subscribe()
        await server.push_event(
            FakeEvent(
                datetime(2025, 1, 27, 11, 23, 8),
                39,
                3,
                6,
                2,
                object_name="Иванов Иван",
            )
        )
        await asyncio.sleep(0.05)
        assert len(events) == 1
        assert events[0].object_name == "Иванов Иван"
        assert events[0].event_code == 39
    finally:
        await connection.close()


async def test_hex_escaped_names_are_decoded(server: FakeSigurServer) -> None:
    """A name sent as ``#NN`` escapes decodes to the same string."""
    events: list[RawEvent] = []
    connection = await _connect(server, events=events)
    try:
        await connection.subscribe()
        await server.push_raw(
            'EVENT_CE "2025-01-27 11:23:08" 4 1 6 2 W26 249 29323 '
            '"#D1#8E#D1#80#D0#B8#D1#81#D1#82"'
        )
        await asyncio.sleep(0.05)
        assert events[0].object_name == "юрист"
    finally:
        await connection.close()


async def test_malformed_event_is_counted_not_fatal(server: FakeSigurServer) -> None:
    """A broken event increments a counter and leaves the session usable."""
    events: list[RawEvent] = []
    connection = await _connect(server, events=events)
    try:
        await connection.subscribe()
        await server.push_raw('EVENT_CE "not-a-timestamp" 4 1 6 2 W26 249 29323')
        await asyncio.sleep(0.05)
        assert events == []
        assert connection.stats.protocol_error_count == 1
        assert (await SigurApi(connection).get_access_point(1)).id == 1
    finally:
        await connection.close()


async def test_unknown_event_code_is_still_delivered(server: FakeSigurServer) -> None:
    """An undocumented numeric code is delivered rather than dropped."""
    events: list[RawEvent] = []
    connection = await _connect(server, events=events)
    try:
        await connection.subscribe()
        await server.push_raw('EVENT_CE "2025-01-27 11:23:08" 4242 1 6 2 UNKNOWN')
        await asyncio.sleep(0.05)
        assert events[0].event_code == 4242
    finally:
        await connection.close()


async def test_command_timeout_is_reported() -> None:
    """A hanging server produces a timeout, not a hung coroutine."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(hang_commands={"GETZONEINFO"}))
    connection = await _connect(fake, command_timeout=0.3)
    try:
        with pytest.raises(SigurTimeoutError):
            await SigurApi(connection).get_zones()
        assert connection.stats.last_error is not None
    finally:
        await connection.close()
        await fake.stop()


async def test_a_late_reply_does_not_corrupt_the_next_command() -> None:
    """A reply that arrives after a timeout is discarded, not mismatched."""
    fake = FakeSigurServer()
    port = await fake.start()
    delayed: list[str] = []

    original = fake._handle_command

    async def slow_first(writer, keyword, line):  # type: ignore[no-untyped-def]
        if keyword == "GETZONEINFO" and not delayed:
            delayed.append(line)
            await asyncio.sleep(0.5)
        await original(writer, keyword, line)

    fake._handle_command = slow_first  # type: ignore[assignment]

    connection = OifConnection(
        _settings(port, command_timeout=0.2),
        Credentials(DEFAULT_USERNAME, DEFAULT_PASSWORD),
    )
    await connection.connect()
    api = SigurApi(connection)
    try:
        with pytest.raises(SigurTimeoutError):
            await api.get_zones()
        await asyncio.sleep(0.5)
        # The stale ZONEINFO is dropped, so APLIST gets its own reply.
        assert await api.get_access_point_ids() == [1, 2]
        assert connection.stats.unsolicited_line_count >= 1
    finally:
        await connection.close()
        await fake.stop()


async def test_server_closing_the_connection_is_reported() -> None:
    """A dropped connection surfaces as a connection error on the next call."""
    fake = FakeSigurServer(behaviour=FakeBehaviour(close_on_commands={"GETZONEINFO"}))
    connection = await _connect(fake)
    try:
        with pytest.raises(SigurConnectionError):
            await SigurApi(connection).get_zones()
    finally:
        await connection.close()
        await fake.stop()


async def test_close_cancels_the_reader_task(server: FakeSigurServer) -> None:
    """No asyncio task survives :meth:`OifConnection.close`."""
    connection = await _connect(server)
    task_names_before = {
        task.get_name()
        for task in asyncio.all_tasks()
        if "sigur-reader" in task.get_name()
    }
    assert task_names_before
    await connection.close()
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if "sigur-reader" in task.get_name() and not task.done()
    ]
    assert connection.connected is False


async def test_history_round_trip() -> None:
    """``GETHISTORY`` returns the events inside the requested window."""
    start = datetime(2025, 1, 27, 11, 0, 0)
    fake = FakeSigurServer(history=make_events(5, start=start))
    connection = await _connect(fake)
    try:
        events = await SigurApi(connection).get_history(
            start, start + timedelta(seconds=2)
        )
        assert len(events) == 3
        assert events[0].occurred_at == start
    finally:
        await connection.close()
        await fake.stop()


async def test_history_empty_window() -> None:
    """A window with no events yields an empty list."""
    start = datetime(2025, 1, 27, 11, 0, 0)
    fake = FakeSigurServer(history=make_events(2, start=start))
    connection = await _connect(fake)
    try:
        events = await SigurApi(connection).get_history(
            start + timedelta(days=1), start + timedelta(days=2)
        )
        assert events == []
    finally:
        await connection.close()
        await fake.stop()


async def test_set_access_point_mode(server: FakeSigurServer) -> None:
    """``SETAPMODE`` reaches the server with the documented syntax."""
    connection = await _connect(server)
    try:
        await SigurApi(connection).set_access_point_mode(ApMode.LOCKED, [1, 2])
        assert "SETAPMODE LOCKED 1 2" in server.received
        assert server.access_points[1].state == "ONLINE_LOCKED"
    finally:
        await connection.close()


async def test_set_access_point_mode_rejects_an_empty_selection(
    server: FakeSigurServer,
) -> None:
    """An empty id list is refused before anything is sent."""
    connection = await _connect(server)
    try:
        with pytest.raises(ValueError, match="at least one access point"):
            await SigurApi(connection).set_access_point_mode(ApMode.NORMAL, [])
    finally:
        await connection.close()


async def test_set_access_point_mode_rejects_an_unknown_selector(
    server: FakeSigurServer,
) -> None:
    """Only the literal ``ALL`` is accepted as a string selector."""
    connection = await _connect(server)
    try:
        with pytest.raises(ValueError, match="invalid access point selector"):
            await SigurApi(connection).set_access_point_mode(
                ApMode.NORMAL, "EVERYTHING"
            )
    finally:
        await connection.close()


async def test_allow_pass_anonymous_and_by_id(server: FakeSigurServer) -> None:
    """``ALLOWPASS`` supports both an object id and ``ANONYMOUS``."""
    connection = await _connect(server)
    api = SigurApi(connection)
    try:
        await api.allow_pass(1, 6, Direction.IN)
        await api.allow_pass(1, "anonymous", Direction.OUT)
        assert "ALLOWPASS 1 6 IN" in server.received
        assert "ALLOWPASS 1 ANONYMOUS OUT" in server.received
    finally:
        await connection.close()


async def test_object_lookup_and_unknown_object(server: FakeSigurServer) -> None:
    """``GETOBJECTINFO OBJECTID`` resolves a name, or ``None`` if unknown."""
    connection = await _connect(server)
    api = SigurApi(connection)
    try:
        obj = await api.get_object(6)
        assert obj is not None and obj.name == "Иванов Иван"
        assert await api.get_object(4242) is None
    finally:
        await connection.close()


async def test_multiple_servers_with_overlapping_ap_ids() -> None:
    """Two independent servers can expose the same access point ids."""
    first = FakeSigurServer(access_points=[FakeAccessPoint(1, "Офис - вход")])
    second = FakeSigurServer(access_points=[FakeAccessPoint(1, "Склад - вход")])
    connection_a = await _connect(first)
    connection_b = await _connect(second)
    try:
        assert (await SigurApi(connection_a).get_access_point(1)).name == "Офис - вход"
        assert (await SigurApi(connection_b).get_access_point(1)).name == "Склад - вход"
    finally:
        await connection_a.close()
        await connection_b.close()
        await first.stop()
        await second.stop()


async def test_tls_connection_with_a_custom_ca() -> None:
    """A TLS server whose CA is trusted connects and logs in."""
    server_context, client_context, _ = self_signed_context()
    fake = FakeSigurServer(ssl_context=server_context)
    connection = await _connect(fake, ssl_context=client_context)
    try:
        assert connection.connected is True
        assert (await SigurApi(connection).get_access_point(1)).id == 1
    finally:
        await connection.close()
        await fake.stop()


async def test_tls_rejects_an_untrusted_certificate() -> None:
    """An unknown CA produces a TLS error rather than a silent downgrade."""
    import ssl

    server_context, _, _ = self_signed_context()
    fake = FakeSigurServer(ssl_context=server_context)
    port = await fake.start()
    connection = OifConnection(
        _settings(port),
        Credentials(DEFAULT_USERNAME, DEFAULT_PASSWORD),
        ssl_context=ssl.create_default_context(),
    )
    try:
        with pytest.raises(SigurTlsError):
            await connection.connect()
    finally:
        await connection.close()
        await fake.stop()


async def test_mutual_tls_requires_the_client_certificate() -> None:
    """With mTLS enabled the client certificate is what lets the session open."""
    import ssl

    server_context, client_context, paths = self_signed_context(
        require_client_cert=True
    )
    fake = FakeSigurServer(ssl_context=server_context)
    port = await fake.start()

    bare = ssl.create_default_context(cafile=paths["ca_bundle"])
    without_cert = OifConnection(
        _settings(port, connect_timeout=3.0),
        Credentials(DEFAULT_USERNAME, DEFAULT_PASSWORD),
        ssl_context=bare,
    )
    try:
        with pytest.raises((SigurTlsError, SigurConnectionError)):
            await without_cert.connect()
        with_cert = OifConnection(
            _settings(port),
            Credentials(DEFAULT_USERNAME, DEFAULT_PASSWORD),
            ssl_context=client_context,
        )
        await with_cert.connect()
        assert with_cert.connected is True
        await with_cert.close()
    finally:
        await without_cert.close()
        await fake.stop()


def test_create_ssl_context_is_none_when_tls_is_off() -> None:
    """No context is built for a plain TCP connection."""
    from custom_components.sigur.api.transport import create_ssl_context

    assert create_ssl_context(TlsSettings(enabled=False)) is None


def test_create_ssl_context_without_verification_is_permissive() -> None:
    """The dangerous advanced option really does disable verification."""
    import ssl

    from custom_components.sigur.api.transport import create_ssl_context

    context = create_ssl_context(TlsSettings(enabled=True, verify=False))
    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE


def test_create_ssl_context_reports_a_bad_ca_bundle() -> None:
    """A missing CA bundle is reported as a TLS error, not an OSError."""
    from custom_components.sigur.api.transport import create_ssl_context

    with pytest.raises(SigurTlsError, match="CA bundle"):
        create_ssl_context(TlsSettings(enabled=True, ca_bundle="/nonexistent/ca.pem"))


def test_backoff_grows_and_is_capped() -> None:
    """Delays grow exponentially, stay jittered and never exceed the maximum."""
    backoff = BackoffController(initial=1.0, maximum=10.0, jitter=lambda: 1.0)
    assert [backoff.next_delay() for _ in range(5)] == [1.0, 2.0, 4.0, 8.0, 10.0]
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_halves_the_delay_at_worst() -> None:
    """Full jitter keeps the delay in ``[0.5x, 1.0x]``."""
    backoff = BackoffController(initial=4.0, jitter=lambda: 0.0)
    assert backoff.next_delay() == 2.0


async def test_run_with_backoff_retries_until_success() -> None:
    """Transient failures are retried; the backoff resets afterwards."""
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SigurConnectionError("nope")

    backoff = BackoffController(initial=0.001, maximum=0.002, jitter=lambda: 0.0)
    await run_with_backoff(flaky, backoff=backoff, should_stop=lambda: False)
    assert attempts == 3
    assert backoff.attempt == 0


async def test_run_with_backoff_does_not_retry_authentication_failures() -> None:
    """Bad credentials propagate immediately so reauth can be triggered."""
    attempts = 0

    async def bad_credentials() -> None:
        nonlocal attempts
        attempts += 1
        raise SigurAuthError(11, "AUTHENTICATION FAILED")

    with pytest.raises(SigurAuthError):
        await run_with_backoff(
            bad_credentials,
            backoff=BackoffController(initial=0.001),
            should_stop=lambda: False,
        )
    assert attempts == 1


async def test_run_with_backoff_stops_when_asked() -> None:
    """A cancelled entry stops retrying immediately."""
    attempts = 0

    async def never_called() -> None:
        nonlocal attempts
        attempts += 1

    await run_with_backoff(
        never_called,
        backoff=BackoffController(initial=0.001),
        should_stop=lambda: True,
    )
    assert attempts == 0


async def test_reconnect_after_the_server_restarts(server: FakeSigurServer) -> None:
    """A dropped session is re-established with a fresh ``LOGIN``."""
    events: list[RawEvent] = []
    connection = await _connect(server, events=events)
    try:
        await connection.subscribe()
        await server.drop_all_connections()
        await asyncio.sleep(0.05)
        await connection.connect()
        await connection.subscribe()
        assert server.login_count == 2
        assert connection.subscribe_mode is SubscribeMode.CE_WITH_NAMES
        await server.push_event(FakeEvent(datetime(2025, 1, 27, 12, 0, 0), 4, 1, 6, 2))
        await asyncio.sleep(0.05)
        assert len(events) == 1
    finally:
        await connection.close()


async def test_all_documented_error_codes_are_mapped(server: FakeSigurServer) -> None:
    """Every ``ERROR n`` the server can send raises a Sigur exception."""
    from custom_components.sigur.api.errors import ERROR_TEXTS, SigurCommandError

    for code, text in ERROR_TEXTS.items():
        fake = FakeSigurServer(
            behaviour=FakeBehaviour(error_on_commands={"GETZONEINFO": (code, text)})
        )
        connection = await _connect(fake)
        try:
            with pytest.raises(SigurCommandError) as excinfo:
                await SigurApi(connection).get_zones()
            assert excinfo.value.code == code
            assert excinfo.value.text == text
        finally:
            await connection.close()
            await fake.stop()
