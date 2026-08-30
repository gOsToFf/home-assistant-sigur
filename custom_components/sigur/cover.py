"""One-shot pass covers, for voice assistants that only know how to open.

A pass through a Sigur access point is a momentary ``ALLOWPASS``, which maps
naturally onto a ``button``. Voice assistants do not speak button: Yandex
Alice, Google and Siri all reach an access point through an *openable* device,
and in Home Assistant that is a ``cover``.

So these entities are a second face on the same command - opening one sends
exactly the ``ALLOWPASS`` its button sends. They are off by default, because
most installations do not need two controls for one action, and they follow
the control option for the same reason the buttons do: they open doors.

The state is the real door position reported by OIF, not a guess from the last
command. A pass authorises an opening; whether the door then opened is
something the door sensor knows and this entity should not pretend to.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.components.cover.const import DOMAIN as COVER_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ANONYMOUS, ApOpenState, Direction, SigurError
from .bindings import DirectionMode
from .const import DOMAIN
from .coordinator import SigurDataUpdateCoordinator
from .entity import SigurAccessPointEntity
from .runtime import SigurConfigEntry


@dataclass(frozen=True, kw_only=True)
class SigurPassCoverDescription(CoverEntityDescription):
    """Describes one one-shot pass cover."""

    direction: Direction

    @property
    def offered_direction(self) -> str | None:
        """The direction this cover asks for, or ``None`` if unspecified."""
        if self.direction is Direction.IN:
            return "in"
        if self.direction is Direction.OUT:
            return "out"
        return None


PASS_COVERS: tuple[SigurPassCoverDescription, ...] = (
    SigurPassCoverDescription(
        key="pass_cover_in",
        translation_key="pass_cover_in",
        device_class=CoverDeviceClass.DOOR,
        direction=Direction.IN,
    ),
    SigurPassCoverDescription(
        key="pass_cover_out",
        translation_key="pass_cover_out",
        device_class=CoverDeviceClass.DOOR,
        direction=Direction.OUT,
    ),
    SigurPassCoverDescription(
        key="pass_cover_unknown",
        translation_key="pass_cover_unknown",
        device_class=CoverDeviceClass.DOOR,
        direction=Direction.UNKNOWN,
        # As with the matching button: a door with a single reader has no
        # meaningful direction, but most access points do.
        entity_registry_enabled_default=False,
    ),
)


def _offers(mode: DirectionMode, description: SigurPassCoverDescription) -> bool:
    """Whether ``mode`` offers the pass this cover asks for."""
    direction = description.offered_direction
    return direction is None or mode.allows(direction)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SigurConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the pass covers, if the user asked for them."""
    runtime = entry.runtime_data
    hub = runtime.hub
    # Changing either option reloads the entry, so the covers appear and
    # disappear as soon as the user decides.
    if not hub.options.enable_control or not hub.options.enable_pass_covers:
        _async_purge(hass, entry, list(hub.access_points))
        return

    coordinator = runtime.coordinator
    registry = er.async_get(hass)

    @callback
    def _add(ap_ids: list[int]) -> None:
        wanted: list[SigurPassCover] = []
        for ap_id in ap_ids:
            mode = runtime.bindings.get(ap_id).direction_mode
            for description in PASS_COVERS:
                if _offers(mode, description):
                    wanted.append(SigurPassCover(coordinator, ap_id, description))
                    continue
                # Switching a point to one-way leaves the opposite cover in the
                # registry, where it would linger as an unavailable entity.
                _remove_stale(registry, entry.entry_id, ap_id, description.key)
        async_add_entities(wanted)

    _add(list(hub.access_points))
    entry.async_on_unload(hub.async_add_new_access_point_listener(_add))


@callback
def _async_purge(
    hass: HomeAssistant, entry: SigurConfigEntry, ap_ids: list[int]
) -> None:
    """Drop every pass cover of this entry after the option was turned off.

    Without this the covers would stay in the registry as unavailable
    entities, and a voice assistant that already knows them would keep
    offering a control that no longer does anything.
    """
    registry = er.async_get(hass)
    for ap_id in ap_ids:
        for description in PASS_COVERS:
            _remove_stale(registry, entry.entry_id, ap_id, description.key)


@callback
def _remove_stale(
    registry: er.EntityRegistry, entry_id: str, ap_id: int, key: str
) -> None:
    """Remove the cover for ``key`` if it is still registered."""
    unique_id = f"{entry_id}_{ap_id}_{key}"
    stale = registry.async_get_entity_id(COVER_DOMAIN, DOMAIN, unique_id)
    if stale is not None:
        registry.async_remove(stale)


class SigurPassCover(SigurAccessPointEntity, CoverEntity):
    """Authorises a single pass, and reports the real door position."""

    entity_description: SigurPassCoverDescription

    # Opening is the only thing a pass can do. A Sigur access point closes on
    # its own, and claiming a close command this integration cannot honour
    # would put a dead button in every assistant that reads the features.
    _attr_supported_features = CoverEntityFeature.OPEN

    def __init__(
        self,
        coordinator: SigurDataUpdateCoordinator,
        ap_id: int,
        description: SigurPassCoverDescription,
    ) -> None:
        """Create the cover described by ``description``."""
        super().__init__(coordinator, ap_id, description.key)
        self.entity_description = description

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
