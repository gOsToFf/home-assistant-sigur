"""Command dispatch and event routing for a single OIF connection.

The specification states that the server only sends data in response to a
client request - with one exception: after a successful ``SUBSCRIBE`` it also
pushes ``EVENT``/``EVENT_CE`` lines asynchronously. Since OIF messages carry no
request identifier, this module keeps a dedicated reader task that classifies
every inbound line as either an asynchronous event or a command reply, and
serialises commands behind a lock. A naive ``send(); readline()`` would
interleave an event into a reply.

No Home Assistant import belongs in this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import logging
import random
from typing import Any, Final

from .errors import (
    SigurAuthError,
    SigurCommandError,
    SigurConnectionError,
    SigurError,
    SigurProtocolError,
    SigurTimeoutError,
)
from .parser import (
    RawEvent,
    parse_classic_event,
    parse_error,
    parse_event_ce,
    quote,
    split_reply,
)
from .transport import (
    DEFAULT_LONG_COMMAND_TIMEOUT,
    DEFAULT_OIF_VERSION,
    OifTransport,
    TransportSettings,
)

_LOGGER = logging.getLogger(__name__)

#: Reply keywords that carry an asynchronous event rather than a command reply.
_EVENT_KEYWORDS: Final = frozenset({"EVENT", "EVENT_CE"})

EventCallback = Callable[[RawEvent], None]


class SubscribeMode(StrEnum):
    """``<subscription-type>`` values accepted by ``SUBSCRIBE``."""

    CE_WITH_NAMES = "CE_WITH_NAMES"
    CE = "CE"
    CLASSIC = "CLASSIC"
    """Bare ``SUBSCRIBE``: the server pushes classic ``EVENT`` lines."""

    @property
    def command(self) -> str:
        """The literal command line for this mode."""
        return "SUBSCRIBE" if self is SubscribeMode.CLASSIC else f"SUBSCRIBE {self}"


#: Preference order for subscription negotiation. ``CE_WITH_NAMES`` gives the
#: most information; older Sigur builds only understand the earlier forms.
SUBSCRIBE_FALLBACK_ORDER: Final[tuple[SubscribeMode, ...]] = (
    SubscribeMode.CE_WITH_NAMES,
    SubscribeMode.CE,
    SubscribeMode.CLASSIC,
)

#: Error codes that mean "this server does not know this SUBSCRIBE variant".
_SUBSCRIBE_FALLBACK_CODES: Final = frozenset({2, 3, 6})


@dataclass(slots=True)
class ConnectionStats:
    """Counters exposed through Home Assistant diagnostics."""

    connect_count: int = 0
    reconnect_count: int = 0
    command_count: int = 0
    event_count: int = 0
    protocol_error_count: int = 0
    unsolicited_line_count: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for diagnostics output."""
        return {
            "connect_count": self.connect_count,
            "reconnect_count": self.reconnect_count,
            "command_count": self.command_count,
            "event_count": self.event_count,
            "protocol_error_count": self.protocol_error_count,
            "unsolicited_line_count": self.unsolicited_line_count,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
            "last_error": self.last_error,
            "last_error_at": (
                self.last_error_at.isoformat() if self.last_error_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class Credentials:
    """OIF operator credentials."""

    username: str
    password: str
    version: str = DEFAULT_OIF_VERSION


class OifConnection:
    """One logical OIF session: transport, ``LOGIN`` and message dispatch."""

    def __init__(
        self,
        settings: TransportSettings,
        credentials: Credentials,
        *,
        ssl_context: Any = None,
        event_callback: EventCallback | None = None,
        name: str = "sigur",
    ) -> None:
        """Prepare a connection; no I/O happens until :meth:`connect`."""
        self._settings = settings
        self._credentials = credentials
        self._ssl_context = ssl_context
        self._event_callback = event_callback
        self._name = name
        self._transport: OifTransport | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._replies: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._command_lock = asyncio.Lock()
        self._disconnected = asyncio.Event()
        self._disconnected.set()
        self._closing = False
        self.stats = ConnectionStats()
        self.subscribe_mode: SubscribeMode | None = None

    @property
    def name(self) -> str:
        """Human readable name of this connection, used in log messages."""
        return self._name

    @property
    def connected(self) -> bool:
        """Whether the session is usable right now."""
        return (
            self._transport is not None
            and self._transport.connected
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def connect(self) -> None:
        """Open the transport, start the reader task and perform ``LOGIN``.

        Raises:
            SigurAuthError: if the operator credentials are rejected.
            SigurConnectionError: if the server cannot be reached.

        """
        await self.close()
        self._closing = False
        self._disconnected.clear()
        self._replies = asyncio.Queue()
        transport = OifTransport(self._settings, ssl_context=self._ssl_context)
        await transport.connect()
        self._transport = transport
        transport.enable_keepalive()
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"sigur-reader-{self._name}"
        )
        try:
            await self._login()
        except SigurError:
            await self.close()
            raise
        self.stats.connect_count += 1

    async def _login(self) -> None:
        """Send ``LOGIN <version> <login> <password>`` and check the reply."""
        credentials = self._credentials
        command = (
            f"LOGIN {credentials.version} "
            f"{quote(credentials.username)} {quote(credentials.password)}"
        )
        reply = await self.execute(command, log_command="LOGIN <redacted>")
        keyword, _ = split_reply(reply)
        if keyword != "OK":
            raise SigurProtocolError(f"unexpected LOGIN reply: {reply!r}")

    async def subscribe(
        self, preferred: SubscribeMode = SubscribeMode.CE_WITH_NAMES
    ) -> SubscribeMode:
        """Subscribe to real-time events, falling back to older variants.

        Returns:
            The mode the server actually accepted.

        Raises:
            SigurCommandError: if no variant was accepted.

        """
        start = SUBSCRIBE_FALLBACK_ORDER.index(preferred)
        last_error: SigurCommandError | None = None
        for mode in SUBSCRIBE_FALLBACK_ORDER[start:]:
            try:
                await self.execute(mode.command)
            except SigurCommandError as err:
                if err.code == 15:  # ALREADY SUBSCRIBED
                    self.subscribe_mode = mode
                    return mode
                if err.code not in _SUBSCRIBE_FALLBACK_CODES:
                    raise
                _LOGGER.info(
                    "Sigur (%s) rejected '%s' with error %s; trying an older "
                    "subscription mode",
                    self._name,
                    mode.command,
                    err.code,
                )
                last_error = err
                continue
            self.subscribe_mode = mode
            _LOGGER.debug("Sigur (%s) subscribed using %s", self._name, mode)
            return mode
        if last_error is None:  # pragma: no cover - the loop always runs once
            raise SigurProtocolError("no SUBSCRIBE variant was attempted")
        raise last_error

    async def unsubscribe(self) -> None:
        """Stop receiving real-time events, ignoring "not subscribed"."""
        try:
            await self.execute("UNSUBSCRIBE")
        except SigurCommandError as err:
            if err.code != 14:  # NOT SUBSCRIBED
                raise
        self.subscribe_mode = None

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        log_command: str | None = None,
    ) -> str:
        """Send one command and return its single reply line.

        Args:
            command: The full command line, already quoted and encoded.
            timeout: Override for the per-command timeout.
            log_command: Redacted form to use in log messages, when ``command``
                contains a secret.

        Raises:
            SigurCommandError: if the server replied with ``ERROR``.
            SigurTimeoutError: if no reply arrived in time.
            SigurConnectionError: if the connection dropped meanwhile.

        """
        lines = await self._execute_lines(
            command, timeout=timeout, log_command=log_command, terminal=None
        )
        return lines[0]

    async def execute_stream(
        self,
        command: str,
        terminal: Callable[[str], bool],
        *,
        timeout: float = DEFAULT_LONG_COMMAND_TIMEOUT,
    ) -> list[str]:
        """Send one command that answers with several lines.

        Args:
            command: The full command line.
            terminal: Predicate that recognises the final line of the reply.
            timeout: Budget for the whole exchange. Long running server side
                operations get a much larger default than normal commands.

        """
        return await self._execute_lines(command, timeout=timeout, terminal=terminal)

    async def _execute_lines(
        self,
        command: str,
        *,
        timeout: float | None,
        terminal: Callable[[str], bool] | None,
        log_command: str | None = None,
    ) -> list[str]:
        """Serialise one request/response exchange on this connection."""
        budget = timeout if timeout is not None else self._settings.command_timeout
        display = log_command or command
        async with self._command_lock:
            if not self.connected:
                raise SigurConnectionError("the OIF connection is not established")
            self._drain_stale_replies(display)
            _LOGGER.debug("Sigur (%s) -> %s", self._name, display)
            await self._require_transport().send_line(command)
            self.stats.command_count += 1
            lines: list[str] = []
            try:
                async with asyncio.timeout(budget):
                    while True:
                        line = await self._next_reply()
                        keyword, stream = split_reply(line)
                        if keyword == "ERROR":
                            raise parse_error(stream, command=display)
                        lines.append(line)
                        if terminal is None or terminal(line):
                            break
            except TimeoutError as err:
                self._record_error(f"timeout waiting for a reply to {display}")
                raise SigurTimeoutError(
                    f"Sigur did not answer {display} within {budget:g} s"
                ) from err
            self.stats.last_success = datetime.now()
            return lines

    async def _next_reply(self) -> str:
        """Await the next reply line, re-raising a reader-side failure."""
        item = await self._replies.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def _drain_stale_replies(self, command: str) -> None:
        """Discard replies left over from a previous, timed-out command."""
        while not self._replies.empty():
            item = self._replies.get_nowait()
            if isinstance(item, BaseException):
                # Preserve a pending connection failure for the next read.
                self._replies.put_nowait(item)
                return
            self.stats.unsolicited_line_count += 1
            _LOGGER.debug(
                "Sigur (%s) discarding a stale reply before %s", self._name, command
            )

    def _require_transport(self) -> OifTransport:
        """Return the live transport or fail loudly."""
        if self._transport is None:
            raise SigurConnectionError("the OIF connection is not established")
        return self._transport

    async def wait_disconnected(self) -> None:
        """Block until this session is no longer usable.

        Lets a supervisor react to a dropped socket the moment the reader
        notices it, instead of discovering it on the next poll.
        """
        await self._disconnected.wait()

    async def _read_loop(self) -> None:
        """Read inbound lines forever, routing events away from replies."""
        transport = self._require_transport()
        try:
            while True:
                line = await transport.read_line()
                if not line.strip():
                    continue
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except SigurError as err:
            if not self._closing:
                self._record_error(str(err))
                _LOGGER.debug("Sigur (%s) reader stopped: %s", self._name, err)
            self._replies.put_nowait(err)
        except Exception as err:
            self._record_error(f"unexpected reader failure: {err}")
            _LOGGER.exception("Sigur (%s) reader failed unexpectedly", self._name)
            self._replies.put_nowait(SigurConnectionError(str(err)))
        finally:
            self._disconnected.set()

    def _dispatch(self, line: str) -> None:
        """Route one inbound line to the event callback or the reply queue."""
        try:
            keyword, stream = split_reply(line)
        except SigurProtocolError as err:
            self.stats.protocol_error_count += 1
            _LOGGER.warning("Sigur (%s) sent an unparsable line: %s", self._name, err)
            return
        if keyword not in _EVENT_KEYWORDS:
            self._replies.put_nowait(line)
            return
        try:
            event = (
                parse_event_ce(stream, line)
                if keyword == "EVENT_CE"
                else parse_classic_event(stream, line)
            )
        except SigurProtocolError as err:
            self.stats.protocol_error_count += 1
            _LOGGER.warning("Sigur (%s) sent a malformed event: %s", self._name, err)
            return
        self.stats.event_count += 1
        self.stats.last_success = datetime.now()
        if self._event_callback is not None:
            self._event_callback(event)

    def _record_error(self, message: str) -> None:
        """Remember the most recent failure for diagnostics."""
        self.stats.last_error = message
        self.stats.last_error_at = datetime.now()

    async def close(self) -> None:
        """Tear the session down and cancel the reader task."""
        self._closing = True
        self._disconnected.set()
        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        self.subscribe_mode = None
        self._replies.put_nowait(SigurConnectionError("the OIF connection was closed"))


@dataclass(slots=True)
class BackoffController:
    """Exponential backoff with full jitter, used between reconnect attempts."""

    initial: float = 2.0
    maximum: float = 300.0
    factor: float = 2.0
    attempt: int = 0
    jitter: Callable[[], float] = random.random
    """Source of randomness, injectable so that tests stay deterministic."""

    def reset(self) -> None:
        """Forget previous failures after a successful connection."""
        self.attempt = 0

    def next_delay(self) -> float:
        """Return the delay before the next attempt, in seconds."""
        delay = min(self.initial * (self.factor**self.attempt), self.maximum)
        self.attempt += 1
        # Full jitter keeps several config entries from reconnecting in lockstep.
        return delay * (0.5 + 0.5 * self.jitter())


async def run_with_backoff(
    connect: Callable[[], Coroutine[Any, Any, None]],
    *,
    backoff: BackoffController,
    should_stop: Callable[[], bool],
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Call ``connect`` until it succeeds, honouring ``backoff``.

    Raises:
        SigurAuthError: immediately, because retrying bad credentials cannot
            help and must instead trigger a Home Assistant reauth flow.

    """
    while not should_stop():
        try:
            await connect()
        except SigurAuthError:
            raise
        except SigurError as err:
            if on_error is not None:
                on_error(err)
            delay = backoff.next_delay()
            _LOGGER.debug("Sigur reconnect failed (%s); retrying in %.1f s", err, delay)
            await asyncio.sleep(delay)
            continue
        backoff.reset()
        return
