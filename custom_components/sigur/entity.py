"""Shared entity base classes for the Sigur integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DEFAULT_OIF_VERSION
from .bindings import DirectionMode
from .const import (
    ACCESS_POINT_MODEL,
    CONF_OIF_VERSION,
    DOMAIN,
    HUB_MODEL,
    MANUFACTURER,
)
from .coordinator import SigurDataUpdateCoordinator
from .models import AccessPointState
from .runtime import SigurHub, SigurRuntimeData


def hub_device_identifier(entry_id: str) -> tuple[str, str]:
    """Device registry identifier of the Sigur server itself."""
    return (DOMAIN, entry_id)


def access_point_device_identifier(entry_id: str, ap_id: int) -> tuple[str, str]:
    """Device registry identifier of one access point.

    Scoped by config entry id so that two Sigur servers can expose access
    points with the same numeric id without colliding.
    """
    return (DOMAIN, f"{entry_id}_ap_{ap_id}")


def hub_device_info(hub: SigurHub) -> DeviceInfo:
    """Device entry describing the Sigur server."""
    settings = hub.settings
    return DeviceInfo(
        identifiers={hub_device_identifier(hub.entry.entry_id)},
        name=hub.server_name,
        manufacturer=MANUFACTURER,
        model=HUB_MODEL,
        sw_version=f"OIF {hub.entry.data.get(CONF_OIF_VERSION, DEFAULT_OIF_VERSION)}",
        # The address and TLS mode are the closest thing this "device" has to a
        # hardware revision, and having them on the device page saves a trip to
        # diagnostics when something is misconfigured.
        hw_version=f"{settings.host}:{settings.port} ({settings.tls.mode})",
    )


class SigurAccessPointEntity(CoordinatorEntity[SigurDataUpdateCoordinator]):
    """Base class for every entity that belongs to one access point."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SigurDataUpdateCoordinator, ap_id: int, key: str
    ) -> None:
        """Bind the entity to an access point of this config entry."""
        super().__init__(coordinator)
        self._ap_id = ap_id
        self._key = key
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{ap_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={access_point_device_identifier(entry_id, ap_id)},
            name=self.ap_state.name,
            manufacturer=MANUFACTURER,
            model=ACCESS_POINT_MODEL,
            # Deprecated in favour of `via_device_id` and dropped from the
            # DeviceInfo type in 2026.9, but that key does not exist in any
            # Home Assistant released so far - 2026.8.3 still has only this
            # one, and hacs.json admits 2026.2. Switching now would break
            # every install that exists to satisfy a beta's type stub.
            # Home Assistant keeps accepting it until 2027.8.
            via_device=hub_device_identifier(entry_id),  # type: ignore[typeddict-unknown-key]
        )

    @property
    def hub(self) -> SigurHub:
        """The runtime this entity belongs to."""
        return self.coordinator.hub

    @property
    def _direction_mode(self) -> str:
        """Which pass directions the user declared for this access point."""
        runtime: SigurRuntimeData | None = getattr(
            self.coordinator.config_entry, "runtime_data", None
        )
        if runtime is None:
            return DirectionMode.BOTH.value
        return runtime.bindings.get(self._ap_id).direction_mode.value

    @property
    def ap_state(self) -> AccessPointState:
        """Current state of this access point."""
        return self.hub.access_points.get(self._ap_id) or AccessPointState(
            id=self._ap_id
        )

    @property
    def available(self) -> bool:
        """Whether the entity has usable data."""
        return (
            super().available
            and self.ap_state.available
            and self.ap_state.info is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the access point id and its zones."""
        state = self.ap_state
        attributes: dict[str, Any] = {
            "access_point_id": self._ap_id,
            "direction_mode": self._direction_mode,
        }
        if state.info is not None:
            attributes["zone_a"] = state.info.zone_a
            attributes["zone_b"] = state.info.zone_b
            attributes["zone_a_name"] = self.hub.zone_name(state.info.zone_a)
            attributes["zone_b_name"] = self.hub.zone_name(state.info.zone_b)
        return attributes
