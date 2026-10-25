"""Helpers for the Home Assistant half of the Sigur test suite."""

from __future__ import annotations

from typing import Any

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.helpers import device_registry as dr

from custom_components.sigur.const import (
    CONF_OIF_VERSION,
    CONF_TLS,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .fake_oif_server import DEFAULT_PASSWORD, DEFAULT_USERNAME


def entry_data(
    port: int, *, name: str = "Sigur - Офис", **overrides: Any
) -> dict[str, Any]:
    """Config entry data pointing at a fake server on ``port``."""
    return {
        CONF_NAME: name,
        CONF_HOST: "127.0.0.1",
        CONF_PORT: port,
        CONF_USERNAME: DEFAULT_USERNAME,
        CONF_PASSWORD: DEFAULT_PASSWORD,
        CONF_OIF_VERSION: "1.8",
        CONF_TLS: False,
        CONF_VERIFY_SSL: True,
        **overrides,
    }


def make_entry(
    port: int, *, name: str = "Sigur - Офис", options: dict[str, Any] | None = None
):  # type: ignore[no-untyped-def]
    """Build a ``MockConfigEntry`` for a fake server on ``port``."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        data=entry_data(port, name=name),
        options=options or {},
        unique_id=f"127.0.0.1:{port}",
    )


def device_by_identifier(
    registry: dr.DeviceRegistry, identifier: tuple[str, str], entry_id: str
) -> dr.DeviceEntry | None:
    """Look a device up by one identifier, on old and new Home Assistant alike.

    Newer releases no longer treat identifiers as unique across config entries,
    so the lookup takes the entry as well and the old ``async_get_device``
    raises. Releases before that only have the old call.
    """
    lookup = getattr(registry, "async_get_device_by_identifier", None)
    if lookup is not None:
        return lookup(identifier, entry_id)  # type: ignore[no-any-return]
    return registry.async_get_device(identifiers={identifier})
