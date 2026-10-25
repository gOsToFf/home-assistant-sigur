"""Websocket commands backing the Sigur sidebar panel.

The panel could read most of this from the entity states it already has, but
not the parts that make the view readable: which access points belong to which
zone, which server they came from, and which entity is which. Rather than have
the frontend rebuild that from the entity registry, the backend hands it over
in one shot; live values then come from the states the panel already receives.

What a user is handed depends on the presets assigned to them, and that is
decided here rather than in the browser: data a user is not meant to see is
never sent. Events reach the panel through a subscription of its own for the
same reason, and because Home Assistant does not let a non-admin subscribe to
an integration's bus events at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.components.websocket_api.messages import event_message
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .bindings import DirectionMode
from .compat import vol
from .const import DOMAIN, EVENT_SIGUR, PANEL_DATA_CHANGED
from .models import AccessPointState
from .presets import Access, async_get_presets
from .registry_watch import async_notify_panel
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
    async_register_command(hass, ws_panel_subscribe)
    async_register_command(hass, ws_set_binding)
    async_register_command(hass, ws_save_preset)
    async_register_command(hass, ws_delete_preset)


def _zone_of(state: AccessPointState | None) -> int | None:
    """The zone on the A side of an access point, which presets go by."""
    return state.info.zone_a if state is not None and state.info else None


@callback
def _event_allowed(
    hass: HomeAssistant, access: Access, data: Mapping[str, Any]
) -> bool:
    """Whether a `sigur_event` payload is for an access point the user may see.

    An event with no access point - the server itself lost its link - is shown
    to anyone who may see at least one point of that server.
    """
    if not access.restricted:
        return True
    entry_id = data.get("server_entry_id")
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    runtime = getattr(entry, "runtime_data", None) if entry else None
    if entry_id is None or runtime is None:
        return False
    points = runtime.hub.access_points
    ap_id = data.get("access_point_id")
    if ap_id is None:
        return any(
            access.allows(entry_id, point_id, _zone_of(state))
            for point_id, state in points.items()
        )
    return access.allows(entry_id, ap_id, _zone_of(points.get(ap_id)))


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
    """Describe the servers, zones and access points this user may see."""
    store = await async_get_presets(hass)
    user = connection.user
    access = store.access_for(user)

    servers: list[dict[str, Any]] = []
    for entry in _loaded_entries(hass):
        runtime = entry.runtime_data
        hub = runtime.hub
        entities = _entity_map(hass, entry)
        bindings = runtime.bindings

        access_points: list[dict[str, Any]] = []
        for ap_id, state in sorted(hub.access_points.items()):
            if not access.allows(entry.entry_id, ap_id, _zone_of(state)):
                continue
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
                    # Stream URLs usually carry the camera's login and
                    # password, and only the admin editor needs them.
                    "rtsp_url": binding.rtsp_url if user.is_admin else None,
                    "direction_mode": binding.direction_mode.value,
                }
            )

        if access.restricted and not access_points:
            # Not even the server's name is the user's business then.
            continue

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

    connection.send_result(
        msg["id"],
        {
            "servers": servers,
            "presets": [
                preset.as_dict() if user.is_admin else preset.as_public_dict()
                for preset in access.presets
            ],
            "restricted": access.restricted,
        },
    )


@websocket_command({vol.Required("type"): "sigur/panel/subscribe"})
@async_response
async def ws_panel_subscribe(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream the events this user may see, and say when the data changed.

    Access is worked out again for every event rather than once: presets can
    be edited while the panel is open, and the change applies at once.
    """
    store = await async_get_presets(hass)
    user = connection.user

    @callback
    def forward_event(event: Event) -> None:
        if _event_allowed(hass, store.access_for(user), event.data):
            connection.send_message(
                event_message(msg["id"], {"type": "event", "event": event.data})
            )

    @callback
    def forward_change(event: Event) -> None:
        connection.send_message(event_message(msg["id"], {"type": "changed"}))

    unsubscribers = [
        hass.bus.async_listen(EVENT_SIGUR, forward_event),
        hass.bus.async_listen(PANEL_DATA_CHANGED, forward_change),
    ]

    @callback
    def unsubscribe() -> None:
        for unsubscriber in unsubscribers:
            unsubscriber()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])


@websocket_command(
    {
        vol.Required("type"): "sigur/panel/set_binding",
        vol.Required("entry_id"): str,
        vol.Required("access_point_id"): int,
        vol.Optional("camera_entity_id"): vol.Any(str, None),
        vol.Optional("rtsp_url"): vol.Any(str, None),
        vol.Optional("direction_mode"): vol.In([mode.value for mode in DirectionMode]),
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

    previous = runtime.bindings.get(ap_id).direction_mode
    binding = await runtime.bindings.async_set(
        ap_id,
        camera_entity_id=msg.get("camera_entity_id"),
        rtsp_url=msg.get("rtsp_url"),
        direction_mode=msg.get("direction_mode"),
    )
    async_notify_panel(hass, entry.entry_id)
    connection.send_result(msg["id"], binding.as_dict())

    if binding.direction_mode is not previous:
        # Which pass buttons exist is decided when the platform is set up, so
        # the entry has to be reloaded for the change to take effect.
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))


#: Keys look like `entry_id:number`; anything else would match nothing anyway.
_KEY = vol.Match(r"^[^:]+:\d+$")


@websocket_command(
    {
        vol.Required("type"): "sigur/presets/save",
        vol.Optional("preset_id"): vol.Any(str, None),
        vol.Required("name"): str,
        vol.Optional("access_points", default=list): [_KEY],
        vol.Optional("zones", default=list): [_KEY],
        vol.Optional("users", default=list): [str],
    }
)
@require_admin
@async_response
async def ws_save_preset(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a preset, or replace an existing one."""
    store = await async_get_presets(hass)
    name = msg["name"].strip()
    if not name:
        connection.send_error(msg["id"], "invalid_format", "A preset needs a name")
        return

    preset_id = msg.get("preset_id")
    if preset_id and store.get(preset_id) is None:
        connection.send_error(msg["id"], "not_found", f"Unknown preset {preset_id}")
        return

    for user_id in msg["users"]:
        if await hass.auth.async_get_user(user_id) is None:
            connection.send_error(
                msg["id"], "invalid_format", f"Unknown user {user_id}"
            )
            return

    preset = await store.async_save(
        preset_id=preset_id,
        name=name,
        access_points=msg["access_points"],
        zones=msg["zones"],
        users=msg["users"],
    )
    async_notify_panel(hass)
    connection.send_result(msg["id"], preset.as_dict())


@websocket_command(
    {
        vol.Required("type"): "sigur/presets/delete",
        vol.Required("preset_id"): str,
    }
)
@require_admin
@async_response
async def ws_delete_preset(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete a preset."""
    store = await async_get_presets(hass)
    if not await store.async_delete(msg["preset_id"]):
        connection.send_error(
            msg["id"], "not_found", f"Unknown preset {msg['preset_id']}"
        )
        return
    async_notify_panel(hass)
    connection.send_result(msg["id"])
