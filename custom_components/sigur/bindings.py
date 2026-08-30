"""Per-access-point settings that the protocol itself knows nothing about.

OIF describes an access point's geometry - the zone on each side - but never
says whether people may pass in both directions or only one, and it has no
concept of a camera at all. Both are the integration's own data.

They are kept out of the config entry options on purpose: an installation with
a hundred access points would make an options form unusable, and these values
change independently of how the integration connects to the server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import BINDINGS_STORAGE_KEY, BINDINGS_STORAGE_VERSION


class DirectionMode(StrEnum):
    """Which way people may pass through an access point.

    OIF does not report this, so it is declared by the user. Guessing it from
    an access point's name would be wrong often enough to matter on controls
    that open doors.
    """

    BOTH = "both"
    """Bidirectional: entry and exit are both offered."""

    IN = "in"
    """One way in only."""

    OUT = "out"
    """One way out only."""

    def allows(self, direction: str) -> bool:
        """Whether a pass in ``direction`` ("in"/"out") is offered."""
        return self is DirectionMode.BOTH or self.value == direction


@dataclass(slots=True)
class AccessPointBinding:
    """What the user declared about one access point."""

    direction_mode: DirectionMode = DirectionMode.BOTH
    """Which pass directions this access point offers."""

    camera_entity_id: str | None = None
    """An existing Home Assistant camera entity to show for this point.

    Preferred, because the stream is then handled by whichever integration
    provides the camera - generic, ONVIF, Frigate - rather than by this one.
    """

    rtsp_url: str | None = None
    """A raw RTSP stream URL.

    Stored for automations and for a future camera platform. A browser cannot
    play RTSP directly, so this alone does not render video anywhere; that
    needs a camera entity, which is what :attr:`camera_entity_id` points at.
    """

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage and for the websocket API."""
        data = asdict(self)
        data["direction_mode"] = self.direction_mode.value
        return data

    @property
    def empty(self) -> bool:
        """Whether nothing was declared, so the entry can be dropped."""
        return (
            not self.camera_entity_id
            and not self.rtsp_url
            and self.direction_mode is DirectionMode.BOTH
        )


class BindingStore:
    """Persists :class:`AccessPointBinding` values for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the store for ``entry_id``; nothing is read until loaded."""
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, BINDINGS_STORAGE_VERSION, f"{BINDINGS_STORAGE_KEY}.{entry_id}"
        )
        self._bindings: dict[int, AccessPointBinding] = {}

    async def async_load(self) -> None:
        """Read the stored bindings."""
        stored = await self._store.async_load() or {}
        self._bindings = {
            int(ap_id): AccessPointBinding(
                # Files written before direction modes existed simply have no
                # key, and bidirectional is what they behaved as.
                direction_mode=_parse_direction(value.get("direction_mode")),
                camera_entity_id=value.get("camera_entity_id") or None,
                rtsp_url=value.get("rtsp_url") or None,
            )
            for ap_id, value in stored.items()
        }

    async def async_save(self) -> None:
        """Write the bindings back."""
        await self._store.async_save(
            {str(ap_id): b.as_dict() for ap_id, b in self._bindings.items()}
        )

    @callback
    def get(self, ap_id: int) -> AccessPointBinding:
        """Return the binding for ``ap_id``, empty if there is none."""
        return self._bindings.get(ap_id, AccessPointBinding())

    @callback
    def all(self) -> dict[int, AccessPointBinding]:
        """Return every non-empty binding."""
        return dict(self._bindings)

    @callback
    def access_points_for_entity(self, entity_id: str) -> list[int]:
        """Return the access points bound to ``entity_id``."""
        return [
            ap_id
            for ap_id, binding in self._bindings.items()
            if binding.camera_entity_id == entity_id
        ]

    async def async_rename_entity(self, old_entity_id: str, new_entity_id: str) -> bool:
        """Follow a camera entity that was renamed.

        An entity id is not a stable identifier - the user may change it at any
        time - so a binding that stored one has to be rewritten, or the panel
        keeps pointing at an entity that no longer exists.

        Returns:
            Whether any binding changed.

        """
        affected = self.access_points_for_entity(old_entity_id)
        for ap_id in affected:
            self._bindings[ap_id].camera_entity_id = new_entity_id
        if affected:
            await self.async_save()
        return bool(affected)

    async def async_forget_entity(self, entity_id: str) -> bool:
        """Drop bindings to a camera entity that was removed.

        Returns:
            Whether any binding changed.

        """
        affected = self.access_points_for_entity(entity_id)
        for ap_id in affected:
            binding = self._bindings[ap_id]
            binding.camera_entity_id = None
            if binding.empty:
                del self._bindings[ap_id]
        if affected:
            await self.async_save()
        return bool(affected)

    async def async_set(
        self,
        ap_id: int,
        *,
        camera_entity_id: str | None = None,
        rtsp_url: str | None = None,
        direction_mode: DirectionMode | str | None = None,
    ) -> AccessPointBinding:
        """Replace the settings for ``ap_id`` and persist them.

        Passing nothing at all restores the defaults, which drops the entry.
        """
        binding = AccessPointBinding(
            direction_mode=_parse_direction(direction_mode),
            camera_entity_id=camera_entity_id or None,
            rtsp_url=rtsp_url or None,
        )
        if binding.empty:
            self._bindings.pop(ap_id, None)
        else:
            self._bindings[ap_id] = binding
        await self.async_save()
        return binding


def _parse_direction(value: object) -> DirectionMode:
    """Read a stored or supplied direction mode, defaulting to bidirectional."""
    if isinstance(value, DirectionMode):
        return value
    try:
        return DirectionMode(str(value))
    except ValueError:
        return DirectionMode.BOTH
