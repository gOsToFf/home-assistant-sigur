"""Home Assistant actions exposed by the Sigur integration.

Every action here writes to the access control system, so each one first checks
that the user turned control on for the target config entry. Raw OIF commands
are deliberately not exposed.
"""

from __future__ import annotations

from collections import defaultdict
import logging
from typing import Any, cast

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .api import ANONYMOUS, ApMode, Direction, SigurError
from .bindings import DirectionMode
from .const import (
    ATTR_CAMERA_ENTITY_ID,
    ATTR_CONFIRM_ALL,
    ATTR_DIRECTION,
    ATTR_DIRECTION_MODE,
    ATTR_MODE,
    ATTR_OBJECT_ID,
    ATTR_RTSP_URL,
    DOMAIN,
    SERVICE_ALLOW_PASS,
    SERVICE_REFRESH,
    SERVICE_SET_ACCESS_POINT_MODE,
    SERVICE_SET_CAMERA,
)
from .registry_watch import async_notify_panel
from .runtime import SigurConfigEntry, SigurHub, SigurRuntimeData

_LOGGER = logging.getLogger(__name__)

_TARGET_SCHEMA: dict[Any, Any] = {
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
}

SET_MODE_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Required(ATTR_MODE): vol.In([mode.value.lower() for mode in ApMode]),
        vol.Optional(ATTR_CONFIRM_ALL, default=False): cv.boolean,
    }
)

ALLOW_PASS_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Optional(ATTR_DIRECTION, default="unknown"): vol.In(
            ["in", "out", "unknown"]
        ),
        vol.Optional(ATTR_OBJECT_ID): vol.Any(
            cv.positive_int, vol.All(cv.string, vol.Lower, vol.In([ANONYMOUS.lower()]))
        ),
    }
)

REFRESH_SCHEMA = vol.Schema(_TARGET_SCHEMA)

SET_CAMERA_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Optional(ATTR_CAMERA_ENTITY_ID): vol.Any(
            None, vol.All(cv.string, cv.entity_domain("camera"))
        ),
        vol.Optional(ATTR_RTSP_URL): vol.Any(None, cv.string),
        vol.Optional(ATTR_DIRECTION_MODE): vol.In([m.value for m in DirectionMode]),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Sigur actions once, on the first loaded config entry."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_ACCESS_POINT_MODE):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ACCESS_POINT_MODE,
        _async_set_mode,
        schema=SET_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ALLOW_PASS, _async_allow_pass, schema=ALLOW_PASS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CAMERA, _async_set_camera, schema=SET_CAMERA_SCHEMA
    )


def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> dict[SigurConfigEntry, set[int]]:
    """Map the call's entity/device targets onto ``(entry, ap ids)``.

    Raises:
        ServiceValidationError: if no Sigur access point was targeted.

    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device_ids: set[str] = set(call.data.get(ATTR_DEVICE_ID, []))

    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        registry_entry = entity_registry.async_get(entity_id)
        if registry_entry is None or registry_entry.platform != DOMAIN:
            continue
        if registry_entry.device_id is not None:
            device_ids.add(registry_entry.device_id)

    resolved: dict[SigurConfigEntry, set[int]] = defaultdict(set)
    for device_id in device_ids:
        device = device_registry.async_get(device_id)
        if device is None:
            continue
        for config_entry_id in device.config_entries:
            config_entry = hass.config_entries.async_get_entry(config_entry_id)
            if config_entry is None or config_entry.domain != DOMAIN:
                continue
            for identifier_domain, identifier in device.identifiers:
                if identifier_domain != DOMAIN:
                    continue
                prefix = f"{config_entry.entry_id}_ap_"
                if identifier.startswith(prefix):
                    resolved[cast("SigurConfigEntry", config_entry)].add(
                        int(identifier.removeprefix(prefix))
                    )

    if not resolved:
        raise ServiceValidationError(
            "No Sigur access point was targeted. Pick an access point device or "
            "one of its entities."
        )
    return resolved


def _hub(entry: SigurConfigEntry) -> SigurHub:
    """Return the loaded runtime of ``entry``.

    Raises:
        ServiceValidationError: if the entry is not loaded.

    """
    runtime: SigurRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None:
        raise ServiceValidationError(
            f"The Sigur config entry '{entry.title}' is not loaded."
        )
    return runtime.hub


async def _async_set_mode(call: ServiceCall) -> None:
    """Handle ``sigur.set_access_point_mode``."""
    mode = ApMode(str(call.data[ATTR_MODE]).upper())
    targets = _resolve_targets(call.hass, call)
    for entry, ap_ids in targets.items():
        hub = _hub(entry)
        if call.data.get(ATTR_CONFIRM_ALL) and len(ap_ids) < len(hub.access_points):
            # `SETAPMODE ALL` is never reachable implicitly: the caller has to
            # both target every point and set the confirmation flag.
            raise ServiceValidationError(
                "confirm_all_access_points was set, but not every access point "
                f"of '{hub.server_name}' was targeted."
            )
        try:
            await hub.async_set_mode(sorted(ap_ids), mode)
        except SigurError as err:
            raise HomeAssistantError(
                f"Sigur '{hub.server_name}' refused to set the mode: {err}"
            ) from err


async def _async_allow_pass(call: ServiceCall) -> None:
    """Handle ``sigur.allow_pass``."""
    direction = Direction(str(call.data.get(ATTR_DIRECTION, "unknown")).upper())
    raw_object: Any = call.data.get(ATTR_OBJECT_ID, ANONYMOUS)
    obj: int | str = ANONYMOUS if isinstance(raw_object, str) else int(raw_object)
    targets = _resolve_targets(call.hass, call)
    for entry, ap_ids in targets.items():
        hub = _hub(entry)
        for ap_id in sorted(ap_ids):
            try:
                await hub.async_allow_pass(ap_id, obj, direction)
            except SigurError as err:
                raise HomeAssistantError(
                    f"Sigur '{hub.server_name}' refused the pass through "
                    f"access point {ap_id}: {err}"
                ) from err


async def _async_set_camera(call: ServiceCall) -> None:
    """Handle ``sigur.set_access_point_camera``.

    Attaching a camera is not a write to the access control system, so it is
    not behind the control option; it only records which video belongs to
    which door. Calling it with neither field clears the binding.
    """
    camera = call.data.get(ATTR_CAMERA_ENTITY_ID)
    rtsp = call.data.get(ATTR_RTSP_URL)
    direction_mode = call.data.get(ATTR_DIRECTION_MODE)
    for entry, ap_ids in _resolve_targets(call.hass, call).items():
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        for ap_id in sorted(ap_ids):
            await runtime.bindings.async_set(
                ap_id,
                camera_entity_id=camera,
                rtsp_url=rtsp,
                direction_mode=direction_mode,
            )
        async_notify_panel(call.hass, entry.entry_id)
        if direction_mode is not None:
            # Which pass buttons exist is decided at platform setup.
            call.hass.async_create_task(
                call.hass.config_entries.async_reload(entry.entry_id)
            )


async def _async_refresh(call: ServiceCall) -> None:
    """Handle ``sigur.refresh``: force a poll of the targeted servers."""
    for entry in _resolve_targets(call.hass, call):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is not None:
            await runtime.coordinator.async_request_refresh()
