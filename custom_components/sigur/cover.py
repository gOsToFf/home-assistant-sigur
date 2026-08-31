"""One-shot pass covers, for voice assistants that only know how to open.

A pass through a Sigur access point is a momentary ``ALLOWPASS``, which maps
naturally onto a ``button``. Voice assistants do not speak button: Yandex
Alice, Google and Siri all reach an access point through an *openable* device,
and in Home Assistant that is a ``cover``.

So this entity is a second face on the same command - opening it sends exactly
the ``ALLOWPASS`` the pass button sends. It is off by default, because most
installations do not need two controls for one action, and it follows the
control option for the same reason the buttons do: it opens doors.

There is exactly one cover per access point and it asks for no direction.
Directions are a real distinction for automations, which is why the buttons
keep them, but they are the wrong shape here: "Alice, open entrance 1" carries
no direction, and a cover per direction would only make the assistant ask
which of two identical doors was meant. ``ALLOWPASS ... UNKNOWN`` leaves that
decision to Sigur, which is the one that knows how the point is wired.

Being the only cover, it carries the access point's own name, so an assistant
hears "Въезд 1" rather than "Въезд 1 Проход". It states that name outright
rather than inheriting it from the device through ``has_entity_name``, which
is the usual way and was how 0.2.1 did it. The convention leaves the entity's
own name empty and composes the displayed one at render time; a bridge that
reads the registry instead of the state then finds nothing and hands the
assistant a nameless device, which Yandex labels with the device type. Every
access point arriving as "Открывающее устройство" is not a trade worth making
for convention, so the name is set here and both readings see it.

The state is the real door position reported by OIF, not a guess from the last
command. A pass authorises an opening; whether the door then opened is
something the door sensor knows and this entity should not pretend to.
"""

from __future__ import annotations

from typing import Final

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.cover.const import DOMAIN as COVER_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ANONYMOUS, ApOpenState, Direction, SigurError
from .const import DOMAIN
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .runtime import SigurConfigEntry

#: Unique id suffix of the cover, and of the per-direction covers 0.2.0 shipped
#: before the directionless one replaced all three. The old ones are removed
#: from the registry wherever they are found, so an upgrade does not leave two
#: dead entities on every access point.
COVER_KEY: Final = "pass_cover"
LEGACY_COVER_KEYS: Final = ("pass_cover_in", "pass_cover_out", "pass_cover_unknown")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the pass covers, if the user asked for them."""
    runtime = entry.runtime_data
    hub = runtime.hub
    coordinator = runtime.coordinator
    registry = er.async_get(hass)
    # Changing either option reloads the entry, so the covers appear and
    # disappear as soon as the user decides.
    wanted = hub.options.enable_control and hub.options.enable_pass_covers

    @callback
    def _add(ap_ids: list[int]) -> None:
        for ap_id in ap_ids:
            for key in LEGACY_COVER_KEYS:
                _remove(registry, entry.entry_id, ap_id, key)
            if not wanted:
                # Without this the cover would stay in the registry as an
                # unavailable entity, and an assistant that already knows it
                # would keep offering a control that no longer does anything.
                _remove(registry, entry.entry_id, ap_id, COVER_KEY)
        if wanted:
            async_add_entities(SigurPassCover(coordinator, ap_id) for ap_id in ap_ids)

    _add(list(hub.access_points))
    entry.async_on_unload(hub.async_add_new_access_point_listener(_add))


@callback
def _remove(registry: er.EntityRegistry, entry_id: str, ap_id: int, key: str) -> None:
    """Remove the cover registered for ``key``, if there is one."""
    stale = registry.async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{entry_id}_{ap_id}_{key}"
    )
    if stale is not None:
        registry.async_remove(stale)


class SigurPassCover(SigurAccessPointEntity, CoverEntity):
    """Authorises a single pass, and reports the real door position."""

    _attr_device_class = CoverDeviceClass.DOOR

    # The name is this entity's own rather than the device's, so that it is
    # stored in the entity registry as well as rendered into the state. See
    # the module docstring: voice assistants reach this entity through a
    # bridge, and not every bridge reads the name from the same place.
    _attr_has_entity_name = False

    # Opening is the only thing a pass can do. A Sigur access point closes on
    # its own, and claiming a close command this integration cannot honour
    # would put a dead button in every assistant that reads the features.
    _attr_supported_features = CoverEntityFeature.OPEN

    def __init__(self, coordinator: SigurDataUpdateCoordinator, ap_id: int) -> None:
        """Create the pass cover for ``ap_id``."""
        super().__init__(coordinator, ap_id, COVER_KEY)

    @property
    def name(self) -> str:
        """The access point's own name, and nothing appended to it.

        A property rather than a fixed attribute so that renaming the access
        point in Sigur reaches the entity on the next reload; the registry
        keeps whatever this returned when the entity was first added, which is
        exactly the value a bridge needs to find there.
        """
        return self.ap_state.name

    @property
    def is_closed(self) -> bool | None:
        """Whether the door is shut, or ``None`` when nothing reports it.

        Sigur says ``UNKNOWN`` when no door sensor is wired up. Returning
        ``None`` leaves the entity in an unknown state, which is honest; the
        open command works either way.
        """
        open_state = self.ap_state.open_state
        if open_state is ApOpenState.UNKNOWN:
            return None
        return open_state is not ApOpenState.OPENED

    async def async_open_cover(self, **kwargs: object) -> None:
        """Send ``ALLOWPASS`` for a single anonymous pass.

        Raises:
            HomeAssistantError: if Sigur refused the request.

        """
        try:
            await self.hub.async_allow_pass(self._ap_id, ANONYMOUS, Direction.UNKNOWN)
        except SigurError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="allow_pass_failed",
                translation_placeholders={
                    "ap_id": str(self._ap_id),
                    "error": str(err),
                },
            ) from err
