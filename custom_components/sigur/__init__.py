"""The Sigur OIF integration.

Unofficial Home Assistant integration for the Sigur access control system,
speaking the OIF integration protocol directly over TCP/TLS. Not affiliated
with Sigur or "Промавтоматика".
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import SigurDataUpdateCoordinator
from .runtime import SigurConfigEntry, SigurHub, SigurRuntimeData
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: SigurConfigEntry) -> bool:
    """Set up one Sigur server from a config entry."""
    hub = SigurHub(hass, entry)
    await hub.async_setup()

    coordinator = SigurDataUpdateCoordinator(hass, entry, hub)
    entry.runtime_data = SigurRuntimeData(hub=hub, coordinator=coordinator)

    if hub.options.webhook_enabled:
        from .webhook import WebhookForwarder

        hub.webhook = WebhookForwarder(hass, hub)
        await hub.webhook.async_setup()

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await hub.async_shutdown()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SigurConfigEntry) -> bool:
    """Unload a config entry and stop every task it owns."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.hub.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: SigurConfigEntry) -> None:
    """Reload the entry after its options changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SigurConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removing an access point device that the server no longer reports.

    Devices are never deleted automatically, because a temporary discovery
    failure must not destroy the user's automations; this lets the user do it
    deliberately once an access point is really gone.
    """
    hub = entry.runtime_data.hub
    live_identifiers = {(DOMAIN, entry.entry_id)} | {
        (DOMAIN, f"{entry.entry_id}_ap_{ap_id}")
        for ap_id, state in hub.access_points.items()
        if state.available
    }
    return not device_entry.identifiers & live_identifiers
