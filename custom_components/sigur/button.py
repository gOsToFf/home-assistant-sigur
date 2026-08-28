"""One-shot pass buttons for Sigur access points.

Pressing a button sends a single ``ALLOWPASS``: it authorises one pass and
does not change the access point's mode. Because these buttons open doors,
they only exist while the user has enabled control for the config entry -
there is no state for them to show, so creating them read-only would be
pointless as well as misleading.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ANONYMOUS, Direction, SigurError
from .const import DOMAIN
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .runtime import SigurConfigEntry


@dataclass(frozen=True, kw_only=True)
class SigurPassButtonDescription(ButtonEntityDescription):
    """Describes one one-shot pass button."""

    direction: Direction


PASS_BUTTONS: tuple[SigurPassButtonDescription, ...] = (
    SigurPassButtonDescription(
        key="allow_pass_in",
        translation_key="allow_pass_in",
        direction=Direction.IN,
    ),
    SigurPassButtonDescription(
        key="allow_pass_out",
        translation_key="allow_pass_out",
        direction=Direction.OUT,
    ),
    SigurPassButtonDescription(
        key="allow_pass_unknown",
        translation_key="allow_pass_unknown",
        direction=Direction.UNKNOWN,
        # A door with a single reader has no meaningful direction, but most
        # access points do, so this one stays out of the way until asked for.
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the pass buttons, but only when control is enabled."""
    runtime = entry.runtime_data
    hub = runtime.hub
    if not hub.options.enable_control:
        # Toggling the option reloads the entry, so the buttons appear as soon
        # as the user opts in.
        return

    coordinator = runtime.coordinator

    @callback
    def _add(ap_ids: list[int]) -> None:
        async_add_entities(
            SigurAllowPassButton(coordinator, ap_id, description)
            for ap_id in ap_ids
            for description in PASS_BUTTONS
        )

    _add(list(hub.access_points))
    entry.async_on_unload(hub.async_add_new_access_point_listener(_add))


class SigurAllowPassButton(SigurAccessPointEntity, ButtonEntity):
    """Authorises a single pass through one access point."""

    entity_description: SigurPassButtonDescription

    def __init__(
        self,
        coordinator: SigurDataUpdateCoordinator,
        ap_id: int,
        description: SigurPassButtonDescription,
    ) -> None:
        """Create the button described by ``description``."""
        super().__init__(coordinator, ap_id, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Send ``ALLOWPASS`` for a single anonymous pass.

        Raises:
            HomeAssistantError: if Sigur refused the request.

        """
        try:
            await self.hub.async_allow_pass(
                self._ap_id, ANONYMOUS, self.entity_description.direction
            )
        except SigurError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="allow_pass_failed",
                translation_placeholders={
                    "ap_id": str(self._ap_id),
                    "error": str(err),
                },
            ) from err
