"""Keeps camera bindings pointing at the right entity.

An entity id is not a stable identifier: a user may rename `camera.old` to
`camera.new` at any moment, and every stored reference to the old id silently
stops resolving. Bindings therefore follow the entity registry rather than
trusting the id they were given.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PANEL_DATA_CHANGED

_LOGGER = logging.getLogger(__name__)


@callback
def async_watch_entity_registry(hass: HomeAssistant, entry_id: str) -> CALLBACK_TYPE:
    """Follow renames and removals of bound camera entities.

    Returns:
        A callable that stops watching.

    """

    async def _handle(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """React to one entity registry change."""
        data = event.data
        action = data["action"]
        entity_id = data["entity_id"]
        if action not in ("update", "remove"):
            return

        entry = hass.config_entries.async_get_entry(entry_id)
        runtime = getattr(entry, "runtime_data", None) if entry else None
        if runtime is None:
            return
        bindings = runtime.bindings

        if action == "remove":
            changed = await bindings.async_forget_entity(entity_id)
            if changed:
                _LOGGER.debug(
                    "Sigur: %s was removed, dropping the bindings to it", entity_id
                )
        else:
            # `entity_id` already holds the new id; the old one is in changes.
            # Only the "update" variant carries `changes`, and only a rename
            # puts `entity_id` in it.
            changes: dict[str, Any] = data.get("changes", {})  # type: ignore[assignment]
            old_entity_id = changes.get("entity_id")
            if not old_entity_id:
                return
            changed = await bindings.async_rename_entity(old_entity_id, entity_id)
            if changed:
                _LOGGER.debug(
                    "Sigur: following %s -> %s in the camera bindings",
                    old_entity_id,
                    entity_id,
                )

        if changed:
            # The panel caches the structure it was given, so tell it to reload
            # rather than leave it pointing at an entity that no longer exists.
            hass.bus.async_fire(PANEL_DATA_CHANGED, {"entry_id": entry_id})

    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _handle)


@callback
def async_notify_panel(hass: HomeAssistant, entry_id: str) -> None:
    """Tell any open panel that its cached structure is out of date."""
    hass.bus.async_fire(PANEL_DATA_CHANGED, {"entry_id": entry_id})


__all__ = ["DOMAIN", "async_notify_panel", "async_watch_entity_registry"]
