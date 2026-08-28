"""Repair issues raised for persistent Sigur problems.

Only conditions a user has to act on get an issue: a wrong operator right, an
expired certificate, an unsupported protocol version, or a server that has been
unreachable long enough that it is no longer a blip.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .api import SigurPermissionError, SigurTlsError, SigurUnsupportedVersionError
from .const import (
    DOMAIN,
    ISSUE_INVALID_CERTIFICATE,
    ISSUE_OIF_DISABLED,
    ISSUE_SERVER_UNAVAILABLE,
    ISSUE_UNSUPPORTED_VERSION,
    UNAVAILABLE_ISSUE_AFTER,
)

#: Every issue id this module can raise, for bulk clearing.
_ALL_ISSUES = (
    ISSUE_OIF_DISABLED,
    ISSUE_INVALID_CERTIFICATE,
    ISSUE_UNSUPPORTED_VERSION,
    ISSUE_SERVER_UNAVAILABLE,
)


def _issue_id(entry: ConfigEntry, kind: str) -> str:
    """Scope an issue id to one config entry."""
    return f"{kind}_{entry.entry_id}"


@callback
def async_check_connection_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    err: Exception,
    unavailable_since: datetime,
) -> None:
    """Raise the repair issue that matches ``err``, if any."""
    if isinstance(err, SigurPermissionError):
        _create(hass, entry, ISSUE_OIF_DISABLED)
        return
    if isinstance(err, SigurTlsError):
        _create(hass, entry, ISSUE_INVALID_CERTIFICATE)
        return
    if isinstance(err, SigurUnsupportedVersionError):
        _create(hass, entry, ISSUE_UNSUPPORTED_VERSION)
        return
    outage = (dt_util.utcnow() - unavailable_since).total_seconds()
    if outage >= UNAVAILABLE_ISSUE_AFTER:
        _create(hass, entry, ISSUE_SERVER_UNAVAILABLE)


@callback
def _create(hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
    """Create one repair issue for ``entry``."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry, kind),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR
        if kind != ISSUE_SERVER_UNAVAILABLE
        else ir.IssueSeverity.WARNING,
        translation_key=kind,
        translation_placeholders={"name": entry.title},
    )


@callback
def async_clear_connection_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove every issue raised for ``entry``."""
    for kind in _ALL_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))
