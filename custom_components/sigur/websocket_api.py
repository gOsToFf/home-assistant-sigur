"""Websocket commands backing the Sigur sidebar panel.

The panel could read most of this from the entity states it already has, but
not the parts that make the view readable: which access points belong to which
zone, which server they came from, and which entity is which. Rather than have
the frontend rebuild that from the entity registry, the backend hands it over
in one shot; live values then come from the states the panel already receives.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .const import DOMAIN
from .runtime import SigurConfigEntry

#: Suffixes of the per-access-point entities the panel drives, mapped onto the
#: key the frontend uses. Kept here so the panel never has to guess an
#: entity_id from a name.
_ENTITY_KEYS: dict[str, str] = {
    "connectivity": "connectivity",
    "door": "door",
    "mode": "mode",
    "event": "event",
    "allow_pass_in": "allow_pass_in",
    "allow_pass_out": "allow_pass_out",
    "allow_pass_unknown": "allow_pass_unknown",
}


#: Guards against re-registering the commands for every config entry.
_REGISTERED = f"{DOMAIN}_websocket_registered"


@callback
def async_setup_websocket_api(hass: HomeAssistant) -> None:
    """Register the panel's websocket commands once."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    async_register_command(hass, ws_panel_data)
    async_register_command(hass, ws_set_binding)


def _loaded_entries(hass: HomeAssistant) -> list[SigurConfigEntry]:
    """Return every loaded Sigur config entry."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]


@callback
def _entity_map(
    hass: HomeAssistant, entry: SigurConfigEntry
) -> dict[int, dict[str, str]]:
    """Map each access point id onto its entity ids, by role."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    result: dict[int, dict[str, str]] = {}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = item.unique_id
        if not unique_id.startswith(prefix):
            continue
        remainder = unique_id.removeprefix(prefix)
        ap_id_text, separator, key = remainder.partition("_")
        if not separator or not ap_id_text.isdigit():
            continue
        role = _ENTITY_KEYS.get(key)
        if role is None:
            continue
        result.setdefault(int(ap_id_text), {})[role] = item.entity_id
    return result


@websocket_command({vol.Required("type"): "sigur/panel/data"})
@async_response
async def ws_panel_data(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Describe every configured Sigur server, zone and access point."""
    servers: list[dict[str, Any]] = []
    for entry in _loaded_entries(hass):
        runtime = entry.runtime_data
        hub = runtime.hub
        entities = _entity_map(hass, entry)
        bindings = runtime.bindings

        access_points: list[dict[str, Any]] = []
        for ap_id, state in sorted(hub.access_points.items()):
            binding = bindings.get(ap_id)
            access_points.append(
                {
                    "id": ap_id,
                    "name": state.name,
                    "available": state.available,
                    "zone_a": state.info.zone_a if state.info else None,
                    "zone_b": state.info.zone_b if state.info else None,
                    "zone_a_name": (
                        hub.zone_name(state.info.zone_a) if state.info else None
                    ),
                    "zone_b_name": (
                        hub.zone_name(state.info.zone_b) if state.info else None
                    ),
                    "entities": entities.get(ap_id, {}),
                    "camera_entity_id": binding.camera_entity_id,
                    "rtsp_url": binding.rtsp_url,
                }
            )

        servers.append(
            {
                "entry_id": entry.entry_id,
                "name": hub.server_name,
                "control_enabled": hub.options.enable_control,
                "personal_data_enabled": hub.options.enable_personal_data,
                "connected": bool(
                    hub.command_connection and hub.command_connection.connected
                ),
                "subscribe_mode": (
                    hub.subscribe_mode.value if hub.subscribe_mode else None
                ),
                "zones": [
                    {"id": zone.id, "name": zone.name}
                    for zone in sorted(hub.zones.values(), key=lambda z: z.id)
                ],
                "access_points": access_points,
            }
        )

    connection.send_result(msg["id"], {"servers": servers})


@websocket_command(
    {
        vol.Required("type"): "sigur/panel/set_binding",
        vol.Required("entry_id"): str,
        vol.Required("access_point_id"): int,
        vol.Optional("camera_entity_id"): vol.Any(str, None),
        vol.Optional("rtsp_url"): vol.Any(str, None),
    }
)
@require_admin
@async_response
async def ws_set_binding(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Attach a camera entity and/or an RTSP URL to one access point."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if (
        entry is None
        or entry.domain != DOMAIN
        or getattr(entry, "runtime_data", None) is None
    ):
        connection.send_error(
            msg["id"], "not_found", f"Unknown Sigur entry {msg['entry_id']}"
        )
        return

    runtime = entry.runtime_data
    ap_id = msg["access_point_id"]
    if ap_id not in runtime.hub.access_points:
        connection.send_error(msg["id"], "not_found", f"Unknown access point {ap_id}")
        return

    binding = await runtime.bindings.async_set(
        ap_id,
        camera_entity_id=msg.get("camera_entity_id"),
        rtsp_url=msg.get("rtsp_url"),
    )
    connection.send_result(msg["id"], binding.as_dict())
