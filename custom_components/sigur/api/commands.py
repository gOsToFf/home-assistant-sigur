"""Typed OIF commands built on top of :class:`~.client.OifConnection`.

Only commands that the integration actually needs are implemented. The
remaining OIF requests (``SYNCDB3``, ``FACE_SYNC``, ``BS_SYNC``, ``DEVCONF_*``,
``IP_SETCONF``, ``DELEGATION_*``, ``LPREVENT``, ``EXTFACEDETECT``) are dangerous,
long running or highly specialised; they are deliberately left out and are
documented as future work. Adding one means adding a method here - the
transport and dispatch layers already support multi-line replies.

No Home Assistant import belongs in this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
import logging
from typing import Final

from .client import OifConnection
from .errors import SigurProtocolError, SigurUnknownObjectError
from .parser import (
    DATETIME_FORMAT,
    AccessPointInfo,
    ApMode,
    Direction,
    ObjectInfo,
    ObjectLocation,
    RawEvent,
    ZoneInfo,
    parse_apinfo,
    parse_aplist,
    parse_history,
    parse_location,
    parse_objectinfo,
    parse_zoneinfo,
    quote,
    split_reply,
)
from .transport import DEFAULT_LONG_COMMAND_TIMEOUT

_LOGGER = logging.getLogger(__name__)

#: Sentinel accepted by ``ALLOWPASS`` in place of an ``<obj-id>``.
ANONYMOUS: Final = "ANONYMOUS"
#: Sentinel accepted by ``SETAPMODE`` in place of an ``<ap-id-list>``.
ALL_ACCESS_POINTS: Final = "ALL"


def format_datetime(value: datetime) -> str:
    """Render a timestamp as the quoted OIF ``"YYYY-MM-DD hh:mm:ss"`` form."""
    return quote(value.strftime(DATETIME_FORMAT))


class SigurApi:
    """The OIF commands this integration issues, as typed methods."""

    def __init__(self, connection: OifConnection) -> None:
        """Bind the API to an established connection."""
        self._connection = connection

    @property
    def connection(self) -> OifConnection:
        """The underlying connection, for lifecycle management."""
        return self._connection

    async def _expect(self, command: str, keyword: str) -> tuple[str, object]:
        """Run ``command`` and require the reply to start with ``keyword``."""
        reply = await self._connection.execute(command)
        actual, stream = split_reply(reply)
        if actual != keyword:
            raise SigurProtocolError(
                f"expected a {keyword} reply to {command!r}, got {actual!r}"
            )
        return reply, stream

    async def get_zones(self) -> list[ZoneInfo]:
        """Fetch every access zone via ``GETZONEINFO``."""
        _, stream = await self._expect("GETZONEINFO", "ZONEINFO")
        return parse_zoneinfo(stream)  # type: ignore[arg-type]

    async def get_access_point_ids(self) -> list[int]:
        """Fetch the list of access point ids via ``GETAPLIST``."""
        _, stream = await self._expect("GETAPLIST", "APLIST")
        return parse_aplist(stream)  # type: ignore[arg-type]

    async def get_access_point(self, ap_id: int) -> AccessPointInfo:
        """Fetch one access point's name, zones and state via ``GETAPINFO``."""
        _, stream = await self._expect(f"GETAPINFO {ap_id:d}", "APINFO")
        return parse_apinfo(stream)  # type: ignore[arg-type]

    async def get_object(self, object_id: int) -> ObjectInfo | None:
        """Fetch one access object via ``GETOBJECTINFO OBJECTID``.

        Returns:
            The object, or ``None`` if the server does not know this id.

        """
        try:
            _, stream = await self._expect(
                f"GETOBJECTINFO OBJECTID {object_id:d}", "OBJECTINFO"
            )
        except SigurUnknownObjectError:
            return None
        objects = parse_objectinfo(stream)  # type: ignore[arg-type]
        return objects[0] if objects else None

    async def get_all_objects(self) -> list[ObjectInfo]:
        """Fetch every access object via ``GETOBJECTINFO ALL``.

        This returns personal data for the whole directory and can be a very
        large reply, so the integration only calls it behind an explicit,
        off-by-default option.
        """
        _, stream = await self._expect("GETOBJECTINFO ALL", "OBJECTINFO")
        return parse_objectinfo(stream)  # type: ignore[arg-type]

    async def get_location(self, object_id: int) -> ObjectLocation:
        """Fetch an object's current zone via ``GETLOCATION2``."""
        _, stream = await self._expect(f"GETLOCATION2 {object_id:d}", "LOCATION")
        return parse_location(stream)  # type: ignore[arg-type]

    async def get_history(
        self, start: datetime, end: datetime, *, timeout: float | None = None
    ) -> list[RawEvent]:
        """Fetch events in ``[start, end]`` via ``GETHISTORY``.

        The caller is responsible for keeping the window bounded; the protocol
        happily accepts a request spanning years.
        """
        command = (
            f"GETHISTORY FROM {format_datetime(start)} TILL {format_datetime(end)}"
        )
        reply = await self._connection.execute(
            command, timeout=timeout or DEFAULT_LONG_COMMAND_TIMEOUT
        )
        keyword, stream = split_reply(reply)
        if keyword != "HISTORY":
            raise SigurProtocolError(f"expected a HISTORY reply, got {keyword!r}")
        return parse_history(stream, reply)

    async def set_access_point_mode(
        self, mode: ApMode, ap_ids: Iterable[int] | str
    ) -> None:
        """Set the lock mode of one or more access points via ``SETAPMODE``.

        Args:
            mode: ``NORMAL``, ``LOCKED`` or ``UNLOCKED``.
            ap_ids: Explicit access point ids, or :data:`ALL_ACCESS_POINTS`.
                The integration never passes ``ALL`` from a public action
                without a separate, explicit confirmation parameter.

        """
        if isinstance(ap_ids, str):
            if ap_ids.upper() != ALL_ACCESS_POINTS:
                raise ValueError(f"invalid access point selector {ap_ids!r}")
            target = ALL_ACCESS_POINTS
        else:
            ids = [int(ap_id) for ap_id in ap_ids]
            if not ids:
                raise ValueError("SETAPMODE needs at least one access point")
            target = " ".join(f"{ap_id:d}" for ap_id in ids)
        await self._require_ok(f"SETAPMODE {mode.value} {target}")

    async def allow_pass(
        self, ap_id: int, obj: int | str, direction: Direction
    ) -> None:
        """Authorise a single pass via ``ALLOWPASS``.

        Args:
            ap_id: The access point to open.
            obj: An access object id, or :data:`ANONYMOUS`.
            direction: ``IN``, ``OUT`` or ``UNKNOWN``.

        """
        if isinstance(obj, str):
            if obj.upper() != ANONYMOUS:
                raise ValueError(f"invalid ALLOWPASS object {obj!r}")
            target = ANONYMOUS
        else:
            target = f"{int(obj):d}"
        await self._require_ok(f"ALLOWPASS {ap_id:d} {target} {direction.value}")

    async def _require_ok(self, command: str) -> None:
        """Run a command whose only successful reply is ``OK``."""
        reply = await self._connection.execute(command)
        keyword, _ = split_reply(reply)
        if keyword != "OK":
            raise SigurProtocolError(f"expected OK for {command!r}, got {reply!r}")

    async def quit(self) -> None:
        """Politely end the session with ``QUIT``.

        The server may close the socket without answering, which is fine: the
        caller closes the transport afterwards either way.
        """
        try:
            await self._connection.execute("QUIT", timeout=2.0)
        except Exception as err:
            _LOGGER.debug("QUIT was not acknowledged: %s", err)
