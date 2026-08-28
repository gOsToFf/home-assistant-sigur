"""Data models shared between the Sigur runtime, entities and services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .api import AccessPointInfo, ApMode, ApOpenState, ApState, RawEvent
from .api.event_codes import CLASSIC_CATEGORIES, EventCategory, resolve_event_code


@dataclass(slots=True)
class AccessPointState:
    """Everything the integration knows about one access point.

    The coordinator refreshes :attr:`info` by polling ``GETAPINFO``; real-time
    events patch it in place so entities do not have to wait for the next poll.
    """

    id: int
    info: AccessPointInfo | None = None
    available: bool = True
    last_updated: datetime | None = None
    last_error: str | None = None
    last_event: SigurEvent | None = None

    @property
    def name(self) -> str:
        """Access point name, falling back to its id."""
        return self.info.name if self.info else f"AP {self.id}"

    @property
    def state(self) -> ApState | None:
        """Link and lock state, if it is known."""
        return self.info.state if self.info else None

    @property
    def open_state(self) -> ApOpenState:
        """Physical door position, ``UNKNOWN`` if the server did not say."""
        return self.info.open_state if self.info else ApOpenState.UNKNOWN

    @property
    def mode(self) -> ApMode | None:
        """Lock mode, ``None`` while the access point is offline."""
        return self.info.mode if self.info else None

    @property
    def online(self) -> bool:
        """Whether the Sigur server currently has a link to this point."""
        return bool(self.info and self.info.online)

    def apply_state(self, state: ApState) -> None:
        """Patch the link/lock state in place after an event."""
        if self.info is None:
            return
        self.info = AccessPointInfo(
            id=self.info.id,
            name=self.info.name,
            zone_a=self.info.zone_a,
            zone_b=self.info.zone_b,
            state=state,
            open_state=self.info.open_state,
        )

    def apply_open_state(self, open_state: ApOpenState) -> None:
        """Patch the physical door position in place after an event."""
        if self.info is None:
            return
        self.info = AccessPointInfo(
            id=self.info.id,
            name=self.info.name,
            zone_a=self.info.zone_a,
            zone_b=self.info.zone_b,
            state=self.info.state,
            open_state=open_state,
        )


@dataclass(frozen=True, slots=True)
class SigurEvent:
    """A Sigur event, normalized for the Home Assistant event bus.

    Personal data is deliberately optional: ``object_name`` is only populated
    when the user enabled name resolution, and ``key_masked`` never contains a
    complete credential number.
    """

    server_entry_id: str
    server_name: str
    occurred_at: datetime
    event_code: int | None
    event_type: str
    category: EventCategory
    description: str
    access_point_id: int | None
    access_point_name: str | None
    object_id: int | None
    object_name: str | None
    direction_code: int | None
    direction: str
    key_masked: str
    deny_reason: str | None = None
    raw_message: str | None = None

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        """Stable identity used to de-duplicate backfilled events.

        The same event reaches the integration in two different shapes:
        ``SUBSCRIBE CE`` delivers a numeric ``EVENT_CE`` code, while
        ``GETHISTORY`` only ever answers in the classic format, which has no
        numeric code at all. Matching on the code would therefore never
        de-duplicate a backfilled event against its live twin, so the
        fingerprint uses the coarse :class:`EventCategory` both forms agree on,
        together with the fields both carry verbatim. The object name and the
        raw payload are excluded for the same reason.
        """
        return (
            self.occurred_at,
            self.category,
            self.access_point_id,
            self.object_id,
            self.direction,
            self.key_masked,
        )

    def as_bus_payload(self, *, include_raw: bool) -> dict[str, Any]:
        """Render the payload published as the ``sigur_event`` bus event."""
        payload: dict[str, Any] = {
            "server_entry_id": self.server_entry_id,
            "server_name": self.server_name,
            "occurred_at": self.occurred_at.isoformat(),
            "event_code": self.event_code,
            "event_type": self.event_type,
            "category": self.category.value,
            "description": self.description,
            "access_point_id": self.access_point_id,
            "access_point_name": self.access_point_name,
            "object_id": self.object_id,
            "object_name": self.object_name,
            "direction_code": self.direction_code,
            "direction": self.direction,
            "key_masked": self.key_masked,
        }
        if self.deny_reason is not None:
            payload["deny_reason"] = self.deny_reason
        if include_raw and self.raw_message is not None:
            payload["raw_message"] = self.raw_message
        return payload


def normalize_event(
    raw: RawEvent,
    *,
    entry_id: str,
    server_name: str,
    access_point_name: str | None,
    include_object_name: bool,
) -> SigurEvent:
    """Turn a parsed OIF event into the integration's normalized form.

    Classic ``EVENT``/``HISTORY`` records and ``EVENT_CE`` records both end up
    in the same shape, so an automation does not have to care which subscription
    mode the server negotiated.
    """
    if raw.event_code is not None:
        resolved = resolve_event_code(raw.event_code)
        category = resolved.category
        event_type = resolved.event_type
        description = resolved.description_en
    else:
        classic = raw.classic_type or "unknown"
        category = CLASSIC_CATEGORIES.get(classic, EventCategory.UNKNOWN)
        event_type = category.value
        description = classic

    return SigurEvent(
        server_entry_id=entry_id,
        server_name=server_name,
        occurred_at=dt_util.as_local(raw.occurred_at.replace(tzinfo=None))
        if raw.occurred_at.tzinfo is None
        else raw.occurred_at,
        event_code=raw.event_code,
        event_type=event_type,
        category=category,
        description=description,
        access_point_id=raw.access_point_id,
        access_point_name=access_point_name,
        object_id=raw.object_id,
        object_name=raw.object_name if include_object_name else None,
        direction_code=raw.direction_code,
        direction=raw.direction,
        key_masked=raw.key.masked,
        deny_reason=raw.deny_reason,
        raw_message=raw.raw_message or None,
    )
