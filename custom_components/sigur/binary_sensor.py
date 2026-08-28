"""Binary sensors for Sigur access points: link state and door position."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ApOpenState
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .runtime import SigurConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors for every known access point."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    @callback
    def _add(ap_ids: list[int]) -> None:
        entities: list[BinarySensorEntity] = []
        for ap_id in ap_ids:
            entities.append(SigurConnectivityBinarySensor(coordinator, ap_id))
            entities.append(SigurDoorBinarySensor(coordinator, ap_id))
        async_add_entities(entities)

    _add(list(runtime.hub.access_points))
    entry.async_on_unload(runtime.hub.async_add_new_access_point_listener(_add))


class SigurConnectivityBinarySensor(SigurAccessPointEntity, BinarySensorEntity):
    """Whether the Sigur server currently has a link to the access point.

    An ``OFFLINE`` access point is reported as ``off``, not as unavailable:
    losing the link to a controller is exactly what this sensor exists to show.
    The entity only becomes unavailable when Sigur stops reporting the access
    point at all.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connectivity"

    def __init__(self, coordinator: SigurDataUpdateCoordinator, ap_id: int) -> None:
        """Create the connectivity sensor for ``ap_id``."""
        super().__init__(coordinator, ap_id, "connectivity")

    @property
    def is_on(self) -> bool | None:
        """``True`` while the access point is online."""
        state = self.ap_state
        if state.info is None:
            return None
        return state.online


class SigurDoorBinarySensor(SigurAccessPointEntity, BinarySensorEntity):
    """Physical open/closed position of the access point.

    Sigur reports ``UNKNOWN`` when no door sensor is wired up; the entity is
    unavailable in that case rather than pretending the door is closed.
    """

    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_translation_key = "door"

    def __init__(self, coordinator: SigurDataUpdateCoordinator, ap_id: int) -> None:
        """Create the door sensor for ``ap_id``."""
        super().__init__(coordinator, ap_id, "door")

    @property
    def available(self) -> bool:
        """Unavailable while the door position is unknown."""
        return super().available and self.ap_state.open_state is not ApOpenState.UNKNOWN

    @property
    def is_on(self) -> bool | None:
        """``True`` while the door is open."""
        open_state = self.ap_state.open_state
        if open_state is ApOpenState.UNKNOWN:
            return None
        return open_state is ApOpenState.OPENED
