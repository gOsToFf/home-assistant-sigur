"""The access point mode select: NORMAL, LOCKED or UNLOCKED.

Sigur's three-position mode does not map onto a two-state lock without losing
information, so ``select`` is the canonical entity for it. A ``lock`` entity is
deliberately not provided.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ApMode, SigurError
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .runtime import SigurConfigEntry

#: Option strings shown in the UI, lowercased for translation keys.
MODE_OPTIONS: list[str] = [mode.value.lower() for mode in ApMode]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a mode select for every known access point."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    @callback
    def _add(ap_ids: list[int]) -> None:
        async_add_entities(SigurModeSelect(coordinator, ap_id) for ap_id in ap_ids)

    _add(list(runtime.hub.access_points))
    entry.async_on_unload(runtime.hub.async_add_new_access_point_listener(_add))


class SigurModeSelect(SigurAccessPointEntity, SelectEntity):
    """Reads and, when control is enabled, sets the access point lock mode."""

    _attr_translation_key = "mode"
    _attr_options = MODE_OPTIONS

    def __init__(self, coordinator: SigurDataUpdateCoordinator, ap_id: int) -> None:
        """Create the mode select for ``ap_id``."""
        super().__init__(coordinator, ap_id, "mode")

    @property
    def current_option(self) -> str | None:
        """The current mode, or ``None`` while the access point is offline."""
        mode = self.ap_state.mode
        return mode.value.lower() if mode else None

    async def async_select_option(self, option: str) -> None:
        """Apply a new mode via ``SETAPMODE``.

        Raises:
            HomeAssistantError: if control is disabled or the server refused.

        """
        try:
            mode = ApMode(option.upper())
        except ValueError as err:
            raise HomeAssistantError(
                f"Unknown Sigur access point mode: {option}"
            ) from err
        try:
            await self.hub.async_set_mode([self._ap_id], mode)
        except SigurError as err:
            raise HomeAssistantError(
                f"Sigur refused to set the mode of access point {self._ap_id}: {err}"
            ) from err
        self.async_write_ha_state()
