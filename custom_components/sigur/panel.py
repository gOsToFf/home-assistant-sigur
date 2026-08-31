"""Registration of the Sigur sidebar panel.

The panel is a single self-contained ES module served straight from the
integration directory. It deliberately imports nothing: Home Assistant does not
expose bare module specifiers to custom panels, and reaching into its internal
elements to borrow Lit breaks on every frontend refactor.

One panel serves every configured server, so registration is refcounted across
config entries rather than tied to any one of them.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom

# Imported from the module that defines it: `homeassistant.components.http`
# re-exports the name at runtime but does not list it in `__all__`, so the
# shorter import is not part of the promised interface.
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ICON, PANEL_TITLE, PANEL_URL_PATH

_LOGGER = logging.getLogger(__name__)

_MODULE = "sigur-panel.js"
_STATIC_URL = f"/{DOMAIN}_panel/{_MODULE}"
_ELEMENT = "sigur-panel"
#: Key under ``hass.data`` holding how many entries want the panel.
_REFCOUNT = f"{DOMAIN}_panel_refcount"
#: Set once the static route exists; it can never be removed again.
_STATIC_REGISTERED = f"{DOMAIN}_panel_static_registered"


def _module_revision(path: Path) -> str:
    """Return a short digest of the panel module, for cache busting."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel, once, for however many entries exist."""
    module_path = Path(__file__).parent / "panel" / _MODULE

    # A static route cannot be removed or replaced once aiohttp has it, so this
    # is done once per Home Assistant run rather than per panel registration -
    # reloading an entry would otherwise raise "route will never be executed".
    if not hass.data.get(_STATIC_REGISTERED):
        hass.data[_STATIC_REGISTERED] = True
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STATIC_URL, str(module_path), False)]
        )

    count = hass.data.get(_REFCOUNT, 0)
    hass.data[_REFCOUNT] = count + 1
    if count:
        return

    # Derive the cache-busting token from the file itself. A hand-maintained
    # constant is forgotten exactly when the panel changes, and the browser
    # then keeps running the previous module.
    revision = await hass.async_add_executor_job(_module_revision, module_path)

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=_ELEMENT,
        module_url=f"{_STATIC_URL}?v={revision}",
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
