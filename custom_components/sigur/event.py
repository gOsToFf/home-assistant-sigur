"""Event entities carrying the last Sigur event of each access point."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.event_codes import EventCategory
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .models import SigurEvent
from .runtime import SigurConfigEntry

#: Event types the entity can report. These are the coarse categories, not the
#: ~90 numeric codes, so an automation stays readable; the numeric code is kept
#: in the entity attributes and in the bus event.
EVENT_TYPES: list[str] = [category.value for category in EventCategory]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the event entity for every known access point."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    @callback
    def _add(ap_ids: list[int]) -> None:
        async_add_entities(
            SigurAccessPointEvent(coordinator, ap_id) for ap_id in ap_ids
        )

    _add(list(runtime.hub.access_points))
    entry.async_on_unload(runtime.hub.async_add_new_access_point_listener(_add))


class SigurAccessPointEvent(SigurAccessPointEntity, EventEntity):
    """The most recent Sigur event of one access point."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_translation_key = "access_point_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: SigurDataUpdateCoordinator, ap_id: int) -> None:
        """Create the event entity for ``ap_id``."""
        super().__init__(coordinator, ap_id, "event")

    @property
    def available(self) -> bool:
        """Stay available so events are never lost to a transient poll error."""
        return self.coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        """Subscribe to the hub's normalized event stream."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event: SigurEvent) -> None:
        """Record an event that belongs to this access point."""
        if event.access_point_id != self._ap_id:
            return
        attributes: dict[str, Any] = {
            "event_code": event.event_code,
            "description": event.description,
            "direction": event.direction,
            "direction_code": event.direction_code,
            "key_masked": event.key_masked,
            "occurred_at": event.occurred_at.isoformat(),
        }
        if event.deny_reason is not None:
            attributes["deny_reason"] = event.deny_reason
        if self.hub.options.enable_personal_data:
            attributes["object_id"] = event.object_id
            attributes["object_name"] = event.object_name
        self._trigger_event(event.category.value, attributes)
        self.async_write_ha_state()
