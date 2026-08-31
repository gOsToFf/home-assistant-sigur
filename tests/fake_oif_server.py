"""An asyncio fake Sigur OIF server used by the test suite.

The fake speaks enough of "Протокол интеграции OIF" 1.8 to exercise the client
end to end, and can be told to misbehave on purpose: fragment lines across TCP
segments, coalesce several messages into one packet, inject asynchronous events
between replies, return any documented ``ERROR`` code, hang, or drop the
connection.

Nothing here talks to a real Sigur installation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import ssl
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

DEFAULT_USERNAME: Final = "asuUser"
DEFAULT_PASSWORD: Final = "passpass"
DATETIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"


@dataclass
class FakeAccessPoint:
    """State of one access point exposed by the fake server."""

    id: int
    name: str
    zone_a: int = 1
    zone_b: int = 2
    state: str = "ONLINE_NORMAL"
    open_state: str = "CLOSED"

    def render(self) -> str:
        """Render this access point as an ``APINFO`` reply."""
        return (
            f'APINFO ID {self.id} NAME "{self.name}" ZONEA {self.zone_a} '
            f"ZONEB {self.zone_b} STATE {self.state} {self.open_state}"
        )


@dataclass
class FakeZone:
    """One access zone exposed by the fake server."""

    id: int
    name: str


@dataclass
class FakeEvent:
    """A stored event, replayed by ``GETHISTORY`` and by the pusher."""

    occurred_at: datetime
    code: int
    ap_id: int
    object_id: int
    direction_code: int
    key: str = "W26 249 29323"
    object_name: str | None = None
    classic: str | None = None
    """Rendering used for classic ``EVENT``/``HISTORY`` replies."""

    def render_ce(self, *, with_names: bool) -> str:
        """Render as an ``EVENT_CE`` line."""
        stamp = self.occurred_at.strftime(DATETIME_FORMAT)
        line = (
            f'EVENT_CE "{stamp}" {self.code} {self.ap_id} {self.object_id} '
            f"{self.direction_code} {self.key}"
        )
        if with_names and self.object_name is not None:
            line += f' "{self.object_name}"'
        return line

    def render_classic(self) -> str:
        """Render the ``<event>`` body used by classic replies."""
        stamp = self.occurred_at.strftime(DATETIME_FORMAT)
        if self.classic is not None:
            return f'"{stamp}" {self.classic}'
        direction = {0: "UNKNOWN", 1: "OUT", 2: "IN", 3: "UNKNOWN"}[self.direction_code]
        return (
            f'"{stamp}" OBJECTPASS {self.ap_id} {self.object_id} {direction} {self.key}'
        )


@dataclass
class FakeBehaviour:
    """Knobs that make the fake server misbehave in a controlled way."""

    fragment_lines: bool = False
    """Write each reply one byte at a time, splitting it across segments."""

    coalesce: bool = False
    """Buffer replies and flush several lines in a single packet."""

    hang_commands: set[str] = field(default_factory=set)
    """Command keywords that never receive an answer."""

    close_on_commands: set[str] = field(default_factory=set)
    """Command keywords that make the server drop the connection."""

    error_on_commands: dict[str, tuple[int, str]] = field(default_factory=dict)
    """Command keyword -> ``(code, text)`` forced ``ERROR`` reply."""

    reject_login: bool = False
    """Answer ``LOGIN`` with ``ERROR 11 AUTHENTICATION FAILED``."""

    oif_disabled: bool = False
    """Answer ``LOGIN`` with ``ERROR 21 OIF ACCESS IS DISABLED FOR THIS USER``."""

    unsupported_version: bool = False
    """Answer ``LOGIN`` with ``ERROR 3 UNSUPPORTED INTERFACE VERSION``."""

    supported_subscribe_modes: set[str] = field(
        default_factory=lambda: {"CE_WITH_NAMES", "CE", "CLASSIC"}
    )
    """Subscription variants this server understands."""

    push_events_after: dict[str, list[FakeEvent]] = field(default_factory=dict)
    """Command keyword -> events pushed right before its reply."""

    require_client_certificate: bool = False
    """Whether the TLS listener demands a client certificate (mTLS)."""


class FakeSigurServer:
    """A minimal, deliberately fallible OIF server."""

    def __init__(
        self,
        *,
        zones: Iterable[FakeZone] | None = None,
        access_points: Iterable[FakeAccessPoint] | None = None,
        history: Iterable[FakeEvent] | None = None,
        behaviour: FakeBehaviour | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Configure the fake before it starts listening."""
        self.zones = (
            list(zones) if zones is not None else [FakeZone(1, "A"), FakeZone(2, "B")]
        )
        self.access_points = {
            ap.id: ap
            for ap in (
                access_points
                if access_points is not None
                else [
                    FakeAccessPoint(1, "Главный вход"),
                    FakeAccessPoint(2, "Турникет"),
                ]
            )
        }
        self.history = list(history) if history is not None else []
        self.behaviour = behaviour or FakeBehaviour()
        self.username = username
        self.password = password
        self._ssl_context = ssl_context
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.received: list[str] = []
        """Every command line the server has seen, in order."""

        self.connection_count = 0
        self.login_count = 0
        self.subscriptions: list[str] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._subscribed: dict[asyncio.StreamWriter, str] = {}
        self.on_command: Callable[[str], None] | None = None
        """Hook invoked for every received command, before it is handled."""

        self._client_tasks: set[asyncio.Task[None]] = set()
        self._previous_exception_handler: Any = None

    async def start(self, host: str = "127.0.0.1") -> int:
        """Start listening on an ephemeral port and return it."""
        loop = asyncio.get_running_loop()
        self._previous_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._on_loop_exception)
        self._server = await asyncio.start_server(
            self._handle_client, host, 0, ssl=self._ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Close every connection and stop listening."""
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()
        self._subscribed.clear()
        # A connection parked in `hang_commands` is asleep for an hour and will
        # not notice its writer closing, so it has to be cancelled outright.
        # Otherwise it outlives the test and the harness reports it as leaked.
        tasks = list(self._client_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # Bounded on purpose: tearing down a test double must never be the
            # thing that hangs a test run, however wedged a connection got.
            await asyncio.wait(tasks, timeout=5)
        self._client_tasks.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(5):
                    await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().set_exception_handler(
                self._previous_exception_handler
            )
        self._previous_exception_handler = None

    async def drop_all_connections(self) -> None:
        """Simulate a server restart by dropping live sessions."""
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        self._writers.clear()
        self._subscribed.clear()

    async def push_event(self, event: FakeEvent) -> None:
        """Push one event to every subscribed connection."""
        for writer, mode in list(self._subscribed.items()):
            line = (
                event.render_classic()
                if mode == "CLASSIC"
                else event.render_ce(with_names=mode == "CE_WITH_NAMES")
            )
            if mode == "CLASSIC":
                line = f"EVENT {line}"
            await self._write_line(writer, line)

    async def push_raw(self, raw: str) -> None:
        """Push a raw line to every subscribed connection, verbatim."""
        for writer in list(self._subscribed):
            await self._write_line(writer, raw)

    @property
    def subscriber_count(self) -> int:
        """How many connections currently hold a subscription."""
        return len(self._subscribed)

    def _on_loop_exception(self, loop: Any, context: dict[str, Any]) -> None:
        """Swallow the noise a deliberately broken connection makes.

        The TLS tests reject the server certificate or withhold a client one on
        purpose, so the handshake fails - on the server side that surfaces from
        asyncio's SSL layer, before any handler runs, and the test harness
        counts an unhandled loop exception against the test. The failure is the
        scenario, not a fault, so it is dropped here; everything else is passed
        on to whoever was handling exceptions before.
        """
        exception = context.get("exception")
        if isinstance(exception, ssl.SSLError | ConnectionResetError | BrokenPipeError):
            return
        if self._previous_exception_handler is not None:
            self._previous_exception_handler(loop, context)
            return
        loop.default_exception_handler(context)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one client connection until it goes away."""
        self.connection_count += 1
        self._writers.add(writer)
        if (task := asyncio.current_task()) is not None:
            self._client_tasks.add(task)
        logged_in = False
        try:
            while True:
                raw = await reader.readuntil(b"\n")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                self.received.append(line)
                if self.on_command is not None:
                    self.on_command(line)
                keyword = line.split(" ", 1)[0].upper()

                if keyword in self.behaviour.close_on_commands:
                    writer.close()
                    return
                if keyword in self.behaviour.hang_commands:
                    await asyncio.sleep(3600)
                    continue

                for event in self.behaviour.push_events_after.get(keyword, ()):
                    mode = self._subscribed.get(writer, "CE_WITH_NAMES")
                    await self._write_line(
                        writer, event.render_ce(with_names=mode == "CE_WITH_NAMES")
                    )

                if (
                    forced := self.behaviour.error_on_commands.get(keyword)
                ) is not None:
                    await self._write_line(writer, f"ERROR {forced[0]} {forced[1]}")
                    continue

                if keyword == "LOGIN":
                    logged_in = await self._handle_login(writer, line)
                    continue
                if keyword in ("QUIT", "EXIT"):
                    writer.close()
                    return
                if not logged_in:
                    await self._write_line(writer, "ERROR 4 NOT LOGGED IN")
                    continue
                await self._handle_command(writer, keyword, line)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Fake Sigur server failed while serving a client")
        finally:
            if (task := asyncio.current_task()) is not None:
                self._client_tasks.discard(task)
            self._writers.discard(writer)
            self._subscribed.pop(writer, None)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle_login(self, writer: asyncio.StreamWriter, line: str) -> bool:
        """Validate a ``LOGIN`` line and answer it."""
        behaviour = self.behaviour
        if behaviour.unsupported_version:
            await self._write_line(writer, "ERROR 3 UNSUPPORTED INTERFACE VERSION")
            return False
        if behaviour.oif_disabled:
            await self._write_line(
                writer, "ERROR 21 OIF ACCESS IS DISABLED FOR THIS USER"
            )
            return False
        parts = _split_quoted(line)
        if behaviour.reject_login or len(parts) < 4:
            await self._write_line(writer, "ERROR 11 AUTHENTICATION FAILED")
            return False
        if parts[2] != self.username or parts[3] != self.password:
            await self._write_line(writer, "ERROR 11 AUTHENTICATION FAILED")
            return False
        self.login_count += 1
        await self._write_line(writer, "OK")
        return True

    async def _handle_command(
        self, writer: asyncio.StreamWriter, keyword: str, line: str
    ) -> None:
        """Answer one authenticated command."""
        parts = _split_quoted(line)
        if keyword == "GETZONEINFO":
            body = ", ".join(f'ID {z.id} NAME "{z.name}"' for z in self.zones)
            await self._write_line(writer, f"ZONEINFO {body}")
            return
        if keyword == "GETAPLIST":
            if not self.access_points:
                await self._write_line(writer, "APLIST EMPTY")
                return
            ids = " ".join(str(ap_id) for ap_id in sorted(self.access_points))
            await self._write_line(writer, f"APLIST {ids}")
            return
        if keyword == "GETAPINFO":
            ap = self.access_points.get(int(parts[1]))
            if ap is None:
                await self._write_line(writer, "ERROR 10 UNKNOWN ACCESS POINT")
                return
            await self._write_line(writer, ap.render())
            return
        if keyword == "SUBSCRIBE":
            await self._handle_subscribe(writer, parts)
            return
        if keyword == "UNSUBSCRIBE":
            if self._subscribed.pop(writer, None) is None:
                await self._write_line(writer, "ERROR 14 NOT SUBSCRIBED")
                return
            await self._write_line(writer, "OK")
            return
        if keyword == "GETHISTORY":
            await self._handle_history(writer, parts)
            return
        if keyword == "SETAPMODE":
            await self._handle_setapmode(writer, parts)
            return
        if keyword == "ALLOWPASS":
            await self._handle_allowpass(writer, parts)
            return
        if keyword == "GETOBJECTINFO":
            await self._handle_objectinfo(writer, parts)
            return
        if keyword == "GETLOCATION2":
            await self._write_line(
                writer,
                f'LOCATION OBJECT {int(parts[1])} ZONE 1 ACTTIME "2025-01-27 11:23:08"',
            )
            return
        await self._write_line(writer, "ERROR 2 UNKNOWN COMMAND")

    async def _handle_subscribe(
        self, writer: asyncio.StreamWriter, parts: list[str]
    ) -> None:
        """Handle ``SUBSCRIBE [<subscription-type>]``."""
        requested = parts[1].upper() if len(parts) > 1 else "CLASSIC"
        if requested not in self.behaviour.supported_subscribe_modes:
            await self._write_line(writer, "ERROR 2 UNKNOWN COMMAND")
            return
        if writer in self._subscribed:
            await self._write_line(writer, "ERROR 15 ALREADY SUBSCRIBED")
            return
        self._subscribed[writer] = requested
        self.subscriptions.append(requested)
        await self._write_line(writer, "OK")

    async def _handle_history(
        self, writer: asyncio.StreamWriter, parts: list[str]
    ) -> None:
        """Handle ``GETHISTORY FROM <t> TILL <t>``."""
        start = datetime.strptime(parts[2], DATETIME_FORMAT)
        end = datetime.strptime(parts[4], DATETIME_FORMAT)
        selected = [e for e in self.history if start <= e.occurred_at <= end]
        if not selected:
            await self._write_line(writer, "HISTORY")
            return
        body = ", ".join(event.render_classic() for event in selected)
        await self._write_line(writer, f"HISTORY {body}")

    async def _handle_setapmode(
        self, writer: asyncio.StreamWriter, parts: list[str]
    ) -> None:
        """Handle ``SETAPMODE <mode> <ap-list>``."""
        mode = parts[1].upper()
        if mode not in ("NORMAL", "LOCKED", "UNLOCKED"):
            await self._write_line(writer, "ERROR 6 SYNTAX ERROR")
            return
        targets = parts[2:]
        if targets and targets[0].upper() == "ALL":
            selected = list(self.access_points.values())
        else:
            selected = []
            for token in targets:
                ap = self.access_points.get(int(token))
                if ap is None:
                    await self._write_line(writer, "ERROR 10 UNKNOWN ACCESS POINT")
                    return
                selected.append(ap)
        for ap in selected:
            ap.state = f"ONLINE_{mode}"
        await self._write_line(writer, "OK")

    async def _handle_allowpass(
        self, writer: asyncio.StreamWriter, parts: list[str]
    ) -> None:
        """Handle ``ALLOWPASS <ap-id> <obj> <direction>``."""
        ap = self.access_points.get(int(parts[1]))
        if ap is None:
            await self._write_line(writer, "ERROR 10 UNKNOWN ACCESS POINT")
            return
        if parts[2].upper() != "ANONYMOUS" and int(parts[2]) not in _KNOWN_OBJECTS:
            await self._write_line(writer, "ERROR 7 UNKNOWN OBJECT")
            return
        await self._write_line(writer, "OK")

    async def _handle_objectinfo(
        self, writer: asyncio.StreamWriter, parts: list[str]
    ) -> None:
        """Handle ``GETOBJECTINFO ALL|OBJECTID <id>``."""
        if parts[1].upper() == "ALL":
            body = ", ".join(
                f'EMP ID {oid} NAME "{name}" POSITION "юрист" TABNUMBER "{oid:03d}"'
                for oid, name in _KNOWN_OBJECTS.items()
            )
            await self._write_line(writer, f"OBJECTINFO {body}")
            return
        object_id = int(parts[2])
        name = _KNOWN_OBJECTS.get(object_id)
        if name is None:
            await self._write_line(writer, "ERROR 7 UNKNOWN OBJECT")
            return
        await self._write_line(
            writer,
            f'OBJECTINFO EMP ID {object_id} NAME "{name}" POSITION "юрист" '
            f'TABNUMBER "{object_id:03d}"',
        )

    async def _write_line(self, writer: asyncio.StreamWriter, line: str) -> None:
        """Write one line, honouring the fragmentation/coalescing knobs."""
        if writer.is_closing():
            return
        payload = line.encode("utf-8") + b"\r\n"
        try:
            if self.behaviour.fragment_lines:
                for index in range(len(payload)):
                    writer.write(payload[index : index + 1])
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(payload)
                if not self.behaviour.coalesce:
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def flush(self) -> None:
        """Flush buffered output when :attr:`FakeBehaviour.coalesce` is set."""
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                await writer.drain()


#: Access objects the fake knows about, used by ``GETOBJECTINFO``/``ALLOWPASS``.
_KNOWN_OBJECTS: Final[dict[int, str]] = {
    6: "Иванов Иван",
    10: "Мария Кузнецова",
}


def _split_quoted(line: str) -> list[str]:
    """Split a command line into tokens, honouring quotes and ``#NN`` escapes."""
    tokens: list[str] = []
    raw = line.encode("utf-8")
    index = 0
    while index < len(raw):
        char = raw[index]
        if char in (0x20, 0x09):
            index += 1
            continue
        if char == 0x22:
            end = raw.find(b'"', index + 1)
            if end == -1:
                end = len(raw)
            tokens.append(_decode_escapes(raw[index + 1 : end]))
            index = end + 1
            continue
        end = index
        while end < len(raw) and raw[end] not in (0x20, 0x09, 0x22):
            end += 1
        tokens.append(raw[index:end].decode("utf-8", errors="replace"))
        index = end
    return tokens


def _decode_escapes(raw: bytes) -> str:
    """Expand ``#NN`` escapes inside a quoted string."""
    out = bytearray()
    index = 0
    while index < len(raw):
        if raw[index : index + 1] == b"#" and index + 2 < len(raw):
            try:
                out.append(int(raw[index + 1 : index + 3], 16))
            except ValueError:
                out.append(raw[index])
                index += 1
                continue
            index += 3
            continue
        out.append(raw[index])
        index += 1
    return out.decode("utf-8", errors="replace")


def make_events(
    count: int, *, start: datetime, ap_id: int = 1, code: int = 4
) -> list[FakeEvent]:
    """Build ``count`` consecutive events one second apart."""
    return [
        FakeEvent(
            occurred_at=start + timedelta(seconds=index),
            code=code,
            ap_id=ap_id,
            object_id=6,
            direction_code=2,
            object_name="Иванов Иван",
        )
        for index in range(count)
    ]


def self_signed_context(
    common_name: str = "localhost", *, require_client_cert: bool = False
) -> tuple[ssl.SSLContext, ssl.SSLContext, dict[str, Any]]:
    """Create matching server/client TLS contexts backed by a throwaway CA.

    Returns:
        ``(server_context, client_context, paths)`` where ``paths`` holds the
        on-disk locations of the generated CA bundle, client certificate and
        client key, for tests that configure the integration by file path.

    """
    import ipaddress
    from pathlib import Path
    import tempfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tmp = Path(tempfile.mkdtemp(prefix="sigur-tls-"))

    def _issue(
        name: str, *, ca: bool, issuer: Any = None, issuer_key: Any = None
    ) -> tuple[Any, Any]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer or subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now() - timedelta(days=1))
            .not_valid_after(datetime.now() + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=ca, path_length=None), critical=True
            )
        )
        builder = builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        if ca:
            # OpenSSL 3 requires a CA certificate to declare certificate signing.
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        if not ca:
            # OpenSSL 3 rejects a chain whose leaf has no authority key id.
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    issuer_key.public_key()
                ),
                critical=False,
            ).add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName(name),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        x509.IPAddress(ipaddress.ip_address("::1")),
                    ]
                ),
                critical=False,
            )
        certificate = builder.sign(issuer_key or key, hashes.SHA256())
        return certificate, key

    ca_cert, ca_key = _issue("sigur-test-ca", ca=True)
    server_cert, server_key = _issue(
        common_name, ca=False, issuer=ca_cert.subject, issuer_key=ca_key
    )
    client_cert, client_key = _issue(
        "sigur-test-client", ca=False, issuer=ca_cert.subject, issuer_key=ca_key
    )

    def _write(name: str, data: bytes) -> str:
        path = tmp / name
        path.write_bytes(data)
        return str(path)

    def _pem_cert(cert: Any) -> bytes:
        return cert.public_bytes(serialization.Encoding.PEM)

    def _pem_key(key: Any) -> bytes:
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    paths = {
        "ca_bundle": _write("ca.pem", _pem_cert(ca_cert)),
        "server_cert": _write("server.pem", _pem_cert(server_cert)),
        "server_key": _write("server.key", _pem_key(server_key)),
        "client_cert": _write("client.pem", _pem_cert(client_cert)),
        "client_key": _write("client.key", _pem_key(client_key)),
    }

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(paths["server_cert"], paths["server_key"])
    if require_client_cert:
        server_context.verify_mode = ssl.CERT_REQUIRED
        server_context.load_verify_locations(paths["ca_bundle"])

    client_context = ssl.create_default_context(cafile=paths["ca_bundle"])
    if require_client_cert:
        client_context.load_cert_chain(paths["client_cert"], paths["client_key"])

    return server_context, client_context, paths
