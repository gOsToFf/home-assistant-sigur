"""Registration of the Sigur sidebar panel.

The panel is a single self-contained ES module served straight from the
integration directory. It deliberately imports nothing: Home Assistant does not
expose bare module specifiers to custom panels, and reaching into its internal
elements to borrow Lit breaks on every frontend refactor.

One panel serves every configured server, so registration is refcounted across
config entries rather than tied to any one of them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_VERSION,
)

_LOGGER = logging.getLogger(__name__)

_MODULE = "sigur-panel.js"
_STATIC_URL = f"/{DOMAIN}_panel/{_MODULE}"
_ELEMENT = "sigur-panel"
#: Key under ``hass.data`` holding how many entries want the panel.
_REFCOUNT = f"{DOMAIN}_panel_refcount"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel, once, for however many entries exist."""
    count = hass.data.get(_REFCOUNT, 0)
    hass.data[_REFCOUNT] = count + 1
    if count:
        return

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                _STATIC_URL, str(Path(__file__).parent / "panel" / _MODULE), False
            )
        ]
    )

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=_ELEMENT,
        # A version query string is what busts the browser cache after an
        # upgrade; without it users keep running the previous panel.
        module_url=f"{_STATIC_URL}?v={PANEL_VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        embed_iframe=False,
    )
    _LOGGER.debug("Registered the Sigur panel at /%s", PANEL_URL_PATH)


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Drop the panel once the last config entry has unloaded."""
    count = hass.data.get(_REFCOUNT, 0) - 1
    hass.data[_REFCOUNT] = max(count, 0)
    if count > 0:
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    _LOGGER.debug("Removed the Sigur panel")
