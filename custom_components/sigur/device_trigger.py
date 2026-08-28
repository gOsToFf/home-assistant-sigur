"""Device triggers for Sigur access points.

Triggers fire on the ``sigur_event`` bus event, filtered by the access point
device and the coarse event category, so an automation does not have to know
the ~90 numeric ``EVENT_CE`` codes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .api.event_codes import EventCategory
from .const import DOMAIN, EVENT_SIGUR

#: Categories offered as device triggers, in the order shown in the UI.
TRIGGER_TYPES: tuple[str, ...] = (
    EventCategory.PASS_REGISTERED.value,
    EventCategory.ACCESS_GRANTED.value,
    EventCategory.ACCESS_DENIED.value,
    EventCategory.BREAK_IN.value,
    EventCategory.DOOR_OPENED.value,
    EventCategory.DOOR_CLOSED.value,
    EventCategory.DOOR_HELD_OPEN_START.value,
    EventCategory.DOOR_HELD_OPEN_END.value,
    EventCategory.LINK_LOST.value,
    EventCategory.LINK_RESTORED.value,
    EventCategory.MODE_CHANGED.value,
    EventCategory.LOCK_FAULT.value,
    EventCategory.POWER_MAINS.value,
    EventCategory.POWER_BATTERY.value,
    EventCategory.TAMPER.value,
    EventCategory.FIRE_ALARM.value,
    EventCategory.WAITING.value,
    EventCategory.FACE.value,
    EventCategory.TEMPERATURE.value,
    EventCategory.POWER_QUALITY.value,
    EventCategory.ALARM_PANEL.value,
    EventCategory.GATE.value,
    EventCategory.OTHER.value,
    EventCategory.UNKNOWN.value,
)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _access_point_target(hass: HomeAssistant, device_id: str) -> tuple[str, int] | None:
    """Return ``(entry_id, ap_id)`` if ``device_id`` is a Sigur access point."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        entry_id, separator, ap_id = identifier.partition("_ap_")
        if separator and ap_id.isdigit():
            return entry_id, int(ap_id)
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers available for a Sigur access point device."""
    if _access_point_target(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger to the ``sigur_event`` bus event."""
    target = _access_point_target(hass, config[CONF_DEVICE_ID])
    entry_id, ap_id = target if target is not None else ("", -1)
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_SIGUR,
            CONF_EVENT_DATA: {
                "server_entry_id": entry_id,
                "access_point_id": ap_id,
                "category": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
