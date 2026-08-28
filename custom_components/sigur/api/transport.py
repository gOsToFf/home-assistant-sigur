r"""Line framing and TLS handling for the Sigur OIF TCP transport.

OIF is a plain text protocol over TCP: every message is a single line ending
with ``\\r\\n``, encoded as UTF-8. TLS is supported by Sigur 1.6.0.1 and newer
and is the recommended mode, because OIF 1.8 sends the operator password in
clear text inside the protocol.

No Home Assistant import belongs in this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
import ssl
from typing import Final

from .errors import (
    SigurConnectionError,
    SigurProtocolError,
    SigurTimeoutError,
    SigurTlsError,
)

_LOGGER = logging.getLogger(__name__)

#: Default OIF TCP port ("Порты интеграций СКУД").
DEFAULT_PORT: Final = 3312
#: Interface version described by the specification this client implements.
DEFAULT_OIF_VERSION: Final = "1.8"

#: Upper bound on a single protocol line. ``HISTORY`` and ``OBJECTINFO ALL``
#: replies are the only realistically large ones; anything beyond this is
#: treated as a protocol violation rather than buffered indefinitely.
DEFAULT_LINE_LIMIT: Final = 4 * 1024 * 1024

DEFAULT_CONNECT_TIMEOUT: Final = 10.0
DEFAULT_COMMAND_TIMEOUT: Final = 20.0
#: Long running server side operations (``SYNCDB3``, ``IP_DISCOVER``) get their
#: own, much larger budget so they never share the normal command timeout.
DEFAULT_LONG_COMMAND_TIMEOUT: Final = 300.0

_LINE_TERMINATOR: Final = b"\r\n"


class TlsMode(str):
    """Marker type for the three supported transport security modes."""


TLS_DISABLED: Final = "disabled"
TLS_VERIFIED: Final = "verified"
TLS_INSECURE: Final = "insecure"


@dataclass(frozen=True, slots=True)
class TlsSettings:
    """TLS configuration for one OIF connection."""

    enabled: bool = False
    verify: bool = True
    """Validate the server certificate chain and hostname."""

    ca_bundle: str | None = None
    """Path to a custom CA bundle used to validate the Sigur server."""

    client_certificate: str | None = None
    """Client certificate path, for servers with mutual authentication."""

    client_key: str | None = None
    """Private key matching :attr:`client_certificate`."""

    client_key_password: str | None = None
    """Passphrase of :attr:`client_key`, if it is encrypted."""

    @property
    def mode(self) -> str:
        """Coarse mode label used in diagnostics and the config flow."""
        if not self.enabled:
            return TLS_DISABLED
        return TLS_VERIFIED if self.verify else TLS_INSECURE

    @property
    def mutual(self) -> bool:
        """Whether a client certificate is configured."""
        return bool(self.client_certificate)


def create_ssl_context(settings: TlsSettings) -> ssl.SSLContext | None:
    """Build the :class:`ssl.SSLContext` for ``settings``.

    This performs blocking file I/O (reading CA bundles and key material) and
    must therefore be called from an executor thread, never from the event
    loop.

    Returns:
        The context, or ``None`` when TLS is disabled.

    Raises:
        SigurTlsError: if the supplied certificate material cannot be loaded.

    """
    if not settings.enabled:
        return None
    try:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=settings.ca_bundle or None
        )
    except OSError as err:
        raise SigurTlsError(f"cannot load the CA bundle: {err}") from err

    if not settings.verify:
        # Explicitly opt-in, dangerous advanced option: the password travels in
        # clear text inside OIF 1.8, so an unverified peer can capture it.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _LOGGER.warning(
            "TLS certificate verification is disabled for this Sigur "
            "connection; the OIF password can be intercepted"
        )

    if settings.client_certificate:
        try:
            context.load_cert_chain(
                settings.client_certificate,
                keyfile=settings.client_key or None,
                password=settings.client_key_password or None,
            )
        except (OSError, ssl.SSLError) as err:
            raise SigurTlsError(f"cannot load the client certificate: {err}") from err
    return context


@dataclass(frozen=True, slots=True)
class TransportSettings:
    """Everything needed to open one OIF TCP connection."""

    host: str
    port: int = DEFAULT_PORT
    tls: TlsSettings = TlsSettings()
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    line_limit: int = DEFAULT_LINE_LIMIT


class OifTransport:
    """A single framed OIF TCP/TLS connection.

    The transport only deals with bytes and line framing; it knows nothing
    about commands, replies or events.
    """

    def __init__(
        self, settings: TransportSettings, *, ssl_context: ssl.SSLContext | None = None
    ) -> None:
        """Store the connection settings and a prepared SSL context."""
        self._settings = settings
        self._ssl_context = ssl_context
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Whether the socket is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def peer_certificate_ok(self) -> bool:
        """Whether the peer presented a certificate that was validated."""
        if self._writer is None:
            return False
        ssl_object = self._writer.get_extra_info("ssl_object")
        return bool(ssl_object and ssl_object.getpeercert())

    async def connect(self) -> None:
        """Open the TCP (and, if configured, TLS) connection.

        Raises:
            SigurTlsError: on a TLS handshake or certificate failure.
            SigurConnectionError: if the server cannot be reached.
            SigurTimeoutError: if the connection attempt times out.

        """
        settings = self._settings
        try:
            async with asyncio.timeout(settings.connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(
                    settings.host,
                    settings.port,
                    ssl=self._ssl_context,
                    limit=settings.line_limit,
                    server_hostname=(
                        settings.host if self._ssl_context is not None else None
                    ),
                )
        except ssl.SSLCertVerificationError as err:
            raise SigurTlsError(
                f"the Sigur server certificate was rejected: {err}"
            ) from err
        except ssl.SSLError as err:
            raise SigurTlsError(f"TLS handshake with Sigur failed: {err}") from err
        except TimeoutError as err:
            raise SigurTimeoutError(
                f"timed out connecting to {settings.host}:{settings.port}"
            ) from err
        except OSError as err:
            raise SigurConnectionError(
                f"cannot connect to {settings.host}:{settings.port}: {err}"
            ) from err

    def enable_keepalive(self) -> None:
        """Turn on TCP keepalive so a silent connection is noticed eventually."""
        if self._writer is None:
            return
        sock = self._writer.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError as err:
            _LOGGER.debug("Cannot enable TCP keepalive: %s", err)

    async def send_line(self, line: str) -> None:
        """Write one CRLF terminated protocol line.

        Raises:
            SigurConnectionError: if the connection is closed or the write
                fails.

        """
        writer = self._writer
        if writer is None or writer.is_closing():
            raise SigurConnectionError("the OIF connection is closed")
        payload = line.encode("utf-8") + _LINE_TERMINATOR
        if len(payload) > self._settings.line_limit:
            raise SigurProtocolError("outgoing OIF line exceeds the size limit")
        async with self._write_lock:
            try:
                writer.write(payload)
                await writer.drain()
            except OSError as err:
                raise SigurConnectionError(f"writing to Sigur failed: {err}") from err

    async def read_line(self) -> str:
        r"""Read one protocol line, without its terminator.

        Tolerates a bare ``\\n`` terminator, which some Sigur builds emit.

        Raises:
            SigurConnectionError: if the peer closed the connection.
            SigurProtocolError: if a line exceeds the configured limit.

        """
        reader = self._reader
        if reader is None:
            raise SigurConnectionError("the OIF connection is closed")
        try:
            raw = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as err:
            if err.partial:
                _LOGGER.debug("Discarding %d trailing bytes at EOF", len(err.partial))
            raise SigurConnectionError(
                "the Sigur server closed the connection"
            ) from err
        except asyncio.LimitOverrunError as err:
            raise SigurProtocolError(
                f"an OIF line exceeded the {self._settings.line_limit} byte limit"
            ) from err
        except (OSError, ssl.SSLError) as err:
            raise SigurConnectionError(f"reading from Sigur failed: {err}") from err
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def close(self) -> None:
        """Close the connection, ignoring errors from an already dead socket."""
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, ssl.SSLError) as err:
            _LOGGER.debug("Ignoring error while closing the OIF connection: %s", err)
