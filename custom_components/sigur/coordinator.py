"""Polling coordinator for Sigur access point state."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SigurAuthError, SigurError, SigurPermissionError
from .const import DOMAIN
from .models import AccessPointState

if TYPE_CHECKING:
    from .runtime import SigurConfigEntry, SigurHub

_LOGGER = logging.getLogger(__name__)


class SigurDataUpdateCoordinator(DataUpdateCoordinator[dict[int, AccessPointState]]):
    """Polls ``GETAPINFO`` for every access point of one Sigur server.

    Real-time events patch the same state objects in place and call
    :meth:`async_set_updated_data`, so entities normally update long before the
    next poll; polling is the safety net that catches whatever the event stream
    missed.
    """

    config_entry: SigurConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SigurConfigEntry, hub: SigurHub
    ) -> None:
        """Create the coordinator for ``hub``."""
        self.hub = hub
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({hub.server_name})",
            update_interval=timedelta(seconds=hub.options.scan_interval),
        )

    async def _async_update_data(self) -> dict[int, AccessPointState]:
        """Refresh every access point of this server."""
        try:
            return await self.hub.async_poll()
        except (SigurAuthError, SigurPermissionError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SigurError as err:
            raise UpdateFailed(str(err)) from err
