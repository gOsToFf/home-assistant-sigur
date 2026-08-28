"""Per-access-point bindings that the protocol itself knows nothing about.

Sigur has no concept of a camera, so anything attaching video to an access
point is the integration's own data. It is kept out of the config entry
options on purpose: an installation with a hundred access points would make
an options form unusable, and these values change independently of how the
integration connects to the server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import BINDINGS_STORAGE_KEY, BINDINGS_STORAGE_VERSION


@dataclass(slots=True)
class AccessPointBinding:
    """What the user attached to one access point."""

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
        return asdict(self)

    @property
    def empty(self) -> bool:
        """Whether nothing is bound, so the entry can be dropped."""
        return not self.camera_entity_id and not self.rtsp_url


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

    async def async_set(
        self,
        ap_id: int,
        *,
        camera_entity_id: str | None = None,
        rtsp_url: str | None = None,
    ) -> AccessPointBinding:
        """Replace the binding for ``ap_id`` and persist it.

        Passing nothing for both fields clears the binding.
        """
        binding = AccessPointBinding(
            camera_entity_id=camera_entity_id or None, rtsp_url=rtsp_url or None
        )
        if binding.empty:
            self._bindings.pop(ap_id, None)
        else:
            self._bindings[ap_id] = binding
        await self.async_save()
        return binding
