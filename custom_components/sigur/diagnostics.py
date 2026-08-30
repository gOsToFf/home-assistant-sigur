"""Diagnostics for the Sigur integration.

Nothing that could identify a person or unlock a door is ever written here:
passwords, credential numbers, names and private keys are redacted, and object
ids only appear when the user explicitly enabled the personal-data option.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLIENT_KEY,
    CONF_CLIENT_KEY_PASSWORD,
    CONF_OIF_VERSION,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_URL,
)
from .runtime import SigurConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_CLIENT_KEY,
    CONF_CLIENT_KEY_PASSWORD,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_URL,
}


def _mask_host(host: str) -> str:
    """Partially mask a host name or address."""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.***.***.{parts[3]}"
    if len(parts) > 2:
        return f"***.{'.'.join(parts[-2:])}"
    return f"{host[:2]}***"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SigurConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one Sigur config entry."""
    runtime = entry.runtime_data
    hub = runtime.hub
    options = hub.options
    settings = hub.settings

    command = hub.command_connection
    events = hub.event_connection

    access_points: list[dict[str, Any]] = []
    for ap_id, state in sorted(hub.access_points.items()):
        entry_data: dict[str, Any] = {
            "id": ap_id,
            "available": state.available,
            "state": state.state.value if state.state else None,
            "open_state": state.open_state.value,
            "zone_a": state.info.zone_a if state.info else None,
            "zone_b": state.info.zone_b if state.info else None,
            "last_updated": (
                state.last_updated.isoformat() if state.last_updated else None
            ),
            "last_error": state.last_error,
        }
        if state.last_event is not None:
            last_event: dict[str, Any] = {
                "occurred_at": state.last_event.occurred_at.isoformat(),
                "category": state.last_event.category.value,
                "event_code": state.last_event.event_code,
                "direction": state.last_event.direction,
            }
            if options.enable_personal_data:
                last_event["object_id"] = state.last_event.object_id
            entry_data["last_event"] = last_event
        access_points.append(entry_data)

    return {
        "entry": {
            "version": entry.version,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "server": {
            "host": _mask_host(settings.host),
            "port": settings.port,
            "oif_version": entry.data.get(CONF_OIF_VERSION),
            "tls_mode": settings.tls.mode,
            "mutual_tls": settings.tls.mutual,
            "subscribe_mode": hub.subscribe_mode.value if hub.subscribe_mode else None,
            "zone_count": len(hub.zones),
            "access_point_count": len(hub.access_points),
            # Both numbers, because "only three access points" reads very
            # differently once you know the server reports forty-seven.
            "discovered_access_point_count": len(hub.discovered_access_points),
            "last_event_at": (
                hub.last_event_at.isoformat() if hub.last_event_at else None
            ),
            "unavailable_since": (
                hub.unavailable_since.isoformat() if hub.unavailable_since else None
            ),
        },
        "connections": {
            "command": {
                "connected": bool(command and command.connected),
                **(command.stats.as_dict() if command else {}),
            },
            "events": {
                "connected": bool(events and events.connected),
                **(events.stats.as_dict() if events else {}),
            },
        },
        "pipeline": {
            "event_queue_size": hub.event_queue_size,
            "dropped_events": hub.dropped_event_count,
            "webhook_queue_size": (
                hub.webhook.queue_size if hub.webhook is not None else None
            ),
            "webhook_failure_count": (
                hub.webhook.failure_count if hub.webhook is not None else None
            ),
        },
        "coordinator": {
            "last_update_success": runtime.coordinator.last_update_success,
            "update_interval": (
                runtime.coordinator.update_interval.total_seconds()
                if runtime.coordinator.update_interval
                else None
            ),
        },
        "access_points": access_points,
    }
