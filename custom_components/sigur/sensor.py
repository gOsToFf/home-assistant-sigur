"""Diagnostic and last-pass sensors for Sigur access points and servers.

Anything that can carry personal data - the object id, the object name, the
masked credential - lives on an entity that is disabled by default and only
becomes useful once the user enables the personal-data option.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity, hub_device_info
from .models import AccessPointState, SigurEvent
from .runtime import SigurConfigEntry, SigurHub


@dataclass(frozen=True, kw_only=True)
class SigurAccessPointSensorDescription(SensorEntityDescription):
    """Describes one access point sensor and how to read its value."""

    value_fn: Callable[[AccessPointState, SigurHub], Any]
    personal_data: bool = False
    """Whether this sensor can expose personal data."""


ACCESS_POINT_SENSORS: tuple[SigurAccessPointSensorDescription, ...] = (
    SigurAccessPointSensorDescription(
        key="last_updated",
        translation_key="last_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state, _hub: state.last_updated,
    ),
    SigurAccessPointSensorDescription(
        key="last_error",
        translation_key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state, _hub: state.last_error,
    ),
    SigurAccessPointSensorDescription(
        key="zone_a",
        translation_key="zone_a",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state, hub: (
            hub.zone_name(state.info.zone_a) or str(state.info.zone_a)
            if state.info
            else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="zone_b",
        translation_key="zone_b",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state, hub: (
            hub.zone_name(state.info.zone_b) or str(state.info.zone_b)
            if state.info
            else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="last_pass_time",
        translation_key="last_pass_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=lambda state, _hub: (
            state.last_event.occurred_at if state.last_event else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="last_pass_direction",
        translation_key="last_pass_direction",
        entity_registry_enabled_default=False,
        value_fn=lambda state, _hub: (
            state.last_event.direction if state.last_event else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="last_event_type",
        translation_key="last_event_type",
        entity_registry_enabled_default=False,
        value_fn=lambda state, _hub: (
            state.last_event.event_type if state.last_event else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="last_pass_object_id",
        translation_key="last_pass_object_id",
        entity_registry_enabled_default=False,
        personal_data=True,
        value_fn=lambda state, _hub: (
            state.last_event.object_id if state.last_event else None
        ),
    ),
    SigurAccessPointSensorDescription(
        key="last_pass_object_name",
        translation_key="last_pass_object_name",
        entity_registry_enabled_default=False,
        personal_data=True,
        value_fn=lambda state, _hub: (
            state.last_event.object_name if state.last_event else None
        ),
    ),
)


@dataclass(frozen=True, kw_only=True)
class SigurHubSensorDescription(SensorEntityDescription):
    """Describes one Sigur server sensor."""

    value_fn: Callable[[SigurHub], Any]


HUB_SENSORS: tuple[SigurHubSensorDescription, ...] = (
    SigurHubSensorDescription(
        key="connection_state",
        translation_key="connection_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: (
            "connected"
            if hub.command_connection and hub.command_connection.connected
            else "disconnected"
        ),
    ),
    SigurHubSensorDescription(
        key="subscribe_mode",
        translation_key="subscribe_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: hub.subscribe_mode.value if hub.subscribe_mode else None,
    ),
    SigurHubSensorDescription(
        key="access_point_count",
        translation_key="access_point_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub: len(hub.access_points),
    ),
    SigurHubSensorDescription(
        key="last_event_at",
        translation_key="last_event_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.last_event_at,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the diagnostic sensors for the server and its access points."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    hub = runtime.hub

    entities: list[SensorEntity] = [
        SigurHubSensor(coordinator, description) for description in HUB_SENSORS
    ]

    @callback
    def _add(ap_ids: list[int]) -> None:
        async_add_entities(
            SigurAccessPointSensor(coordinator, ap_id, description)
            for ap_id in ap_ids
            for description in ACCESS_POINT_SENSORS
            if not description.personal_data or hub.options.enable_personal_data
        )

    async_add_entities(entities)
    _add(list(hub.access_points))
    entry.async_on_unload(hub.async_add_new_access_point_listener(_add))


class SigurAccessPointSensor(SigurAccessPointEntity, SensorEntity):
    """A diagnostic or last-pass value of one access point."""

    entity_description: SigurAccessPointSensorDescription

    def __init__(
        self,
        coordinator: SigurDataUpdateCoordinator,
        ap_id: int,
        description: SigurAccessPointSensorDescription,
    ) -> None:
        """Create the sensor described by ``description``."""
        super().__init__(coordinator, ap_id, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Diagnostics stay available even when the point itself is not."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> Any:
        """Read the value through the description."""
        return self.entity_description.value_fn(self.ap_state, self.hub)

    async def async_added_to_hass(self) -> None:
        """Also refresh when a real-time event updates the last pass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hub.async_add_event_listener(self._handle_sigur_event)
        )

    @callback
    def _handle_sigur_event(self, event: SigurEvent) -> None:
        """Re-render when an event for this access point arrives."""
        if event.access_point_id == self._ap_id:
            self.async_write_ha_state()


class SigurHubSensor(CoordinatorEntity[SigurDataUpdateCoordinator], SensorEntity):
    """A diagnostic value of the Sigur server itself."""

    _attr_has_entity_name = True
    entity_description: SigurHubSensorDescription

    def __init__(
        self,
        coordinator: SigurDataUpdateCoordinator,
        description: SigurHubSensorDescription,
    ) -> None:
        """Create the server-level sensor described by ``description``."""
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_hub_{description.key}"
        self._attr_device_info = hub_device_info(coordinator.hub)

    @property
    def available(self) -> bool:
        """Server diagnostics are readable even while polling fails."""
        return True

    @property
    def native_value(self) -> Any:
        """Read the value through the description."""
        return self.entity_description.value_fn(self.coordinator.hub)
