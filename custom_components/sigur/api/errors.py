"""Exceptions for the Sigur OIF protocol layer.

This module is intentionally free of any Home Assistant import so that the
protocol layer can be extracted into a standalone library later on.

Error codes and texts are taken verbatim from the appendix "Сообщения об
ошибках" of the "Протокол интеграции OIF" specification (rev. 27.01.2025,
OIF 1.8 / Sigur 1.6.3.14).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class OifErrorCode(IntEnum):
    """Numeric error codes returned by ``ERROR <code> <text>`` replies."""

    UNABLE_TO_CONNECT_TO_DB = 1
    UNKNOWN_COMMAND = 2
    UNSUPPORTED_INTERFACE_VERSION = 3
    NOT_LOGGED_IN = 4
    GENERIC_SQL_ERROR = 5
    SYNTAX_ERROR = 6
    UNKNOWN_OBJECT = 7
    INTERNAL_ERROR = 8
    CONCURRENT_TRANSACTION_IS_IN_PROGRESS = 9
    UNKNOWN_ACCESS_POINT = 10
    AUTHENTICATION_FAILED = 11
    DELEGATION_IS_DISABLED = 12
    DELEGATION_IS_NOT_ACTIVE = 13
    NOT_SUBSCRIBED = 14
    ALREADY_SUBSCRIBED = 15
    SPECIFIED_KEY_ALREADY_IN_USE = 16
    SPECIFIED_RULE_DOESNT_EXIST = 17
    SPECIFIED_KEY_DOESNT_EXIST = 18
    UNKNOWN_VIDEO_CHANNEL = 19
    UNKNOWN_ALARM_LINE = 20
    OIF_ACCESS_IS_DISABLED_FOR_THIS_USER = 21
    VIDEO_IS_NOT_AVAILABLE = 22
    STREAM_IS_NOT_OPENED = 23
    FACE_RECOGNITION_IS_OFF = 24
    ACCESS_POLICY_ERROR = 25
    TIMED_OUT = 26
    SOCKET_BIND_FAILED = 27
    UNKNOWN_ERROR = 28
    EXTMEM_ERROR = 29


#: Canonical English error texts as documented in the specification.
ERROR_TEXTS: Final[dict[int, str]] = {
    OifErrorCode.UNABLE_TO_CONNECT_TO_DB: "UNABLE TO CONNECT TO DB",
    OifErrorCode.UNKNOWN_COMMAND: "UNKNOWN COMMAND",
    OifErrorCode.UNSUPPORTED_INTERFACE_VERSION: "UNSUPPORTED INTERFACE VERSION",
    OifErrorCode.NOT_LOGGED_IN: "NOT LOGGED IN",
    OifErrorCode.GENERIC_SQL_ERROR: "GENERIC SQL ERROR",
    OifErrorCode.SYNTAX_ERROR: "SYNTAX ERROR",
    OifErrorCode.UNKNOWN_OBJECT: "UNKNOWN OBJECT",
    OifErrorCode.INTERNAL_ERROR: "INTERNAL ERROR",
    OifErrorCode.CONCURRENT_TRANSACTION_IS_IN_PROGRESS: (
        "CONCURRENT TRANSACTION IS IN PROGRESS"
    ),
    OifErrorCode.UNKNOWN_ACCESS_POINT: "UNKNOWN ACCESS POINT",
    OifErrorCode.AUTHENTICATION_FAILED: "AUTHENTICATION FAILED",
    OifErrorCode.DELEGATION_IS_DISABLED: "DELEGATION IS DISABLED",
    OifErrorCode.DELEGATION_IS_NOT_ACTIVE: "DELEGATION IS NOT ACTIVE",
    OifErrorCode.NOT_SUBSCRIBED: "NOT SUBSCRIBED",
    OifErrorCode.ALREADY_SUBSCRIBED: "ALREADY SUBSCRIBED",
    OifErrorCode.SPECIFIED_KEY_ALREADY_IN_USE: "SPECIFIED KEY ALREADY IN USE",
    OifErrorCode.SPECIFIED_RULE_DOESNT_EXIST: "SPECIFIED RULE DOESN'T EXIST",
    OifErrorCode.SPECIFIED_KEY_DOESNT_EXIST: "SPECIFIED KEY DOESN'T EXIST",
    OifErrorCode.UNKNOWN_VIDEO_CHANNEL: "UNKNOWN VIDEO CHANNEL",
    OifErrorCode.UNKNOWN_ALARM_LINE: "UNKNOWN ALARM LINE",
    OifErrorCode.OIF_ACCESS_IS_DISABLED_FOR_THIS_USER: (
        "OIF ACCESS IS DISABLED FOR THIS USER"
    ),
    OifErrorCode.VIDEO_IS_NOT_AVAILABLE: "VIDEO IS NOT AVAILABLE",
    OifErrorCode.STREAM_IS_NOT_OPENED: "STREAM IS NOT OPENED",
    OifErrorCode.FACE_RECOGNITION_IS_OFF: "FACE RECOGNITION IS OFF",
    OifErrorCode.ACCESS_POLICY_ERROR: "ACCESS POLICY ERROR",
    OifErrorCode.TIMED_OUT: "TIMED OUT",
    OifErrorCode.SOCKET_BIND_FAILED: "SOCKET BIND FAILED",
    OifErrorCode.UNKNOWN_ERROR: "UNKNOWN ERROR",
    OifErrorCode.EXTMEM_ERROR: "EXTMEM ERROR",
}


class SigurError(Exception):
    """Base class for every error raised by the OIF client."""


class SigurConnectionError(SigurError):
    """The OIF server is unreachable, or the connection dropped."""


class SigurTlsError(SigurConnectionError):
    """The TLS handshake or certificate validation failed."""


class SigurTimeoutError(SigurError):
    """A command or a read timed out."""


class SigurProtocolError(SigurError):
    """A malformed or unexpected message was received."""


class SigurCommandError(SigurError):
    """The server replied with ``ERROR <code> <text>``."""

    def __init__(self, code: int, text: str, *, command: str | None = None) -> None:
        """Initialise from the raw error reply."""
        self.code = code
        self.text = text
        self.command = command
        try:
            self.error_code: OifErrorCode | None = OifErrorCode(code)
        except ValueError:
            self.error_code = None
        suffix = f" (command: {command})" if command else ""
        super().__init__(f"OIF error {code}: {text}{suffix}")


class SigurAuthError(SigurCommandError):
    """Authentication failed (error 11) or OIF access is denied (error 21)."""


class SigurPermissionError(SigurCommandError):
    """The operator account is not allowed to use the requested feature."""


class SigurUnsupportedVersionError(SigurCommandError):
    """The server does not support the requested interface version (error 3)."""


class SigurUnknownObjectError(SigurCommandError):
    """The requested object-id is unknown to the system (error 7)."""


class SigurUnknownAccessPointError(SigurCommandError):
    """The requested ap-id is unknown to the system (error 10)."""


class SigurBusyError(SigurCommandError):
    """A concurrent transaction is in progress (error 9)."""


class SigurServerError(SigurCommandError):
    """The server hit an internal/database problem (errors 1, 5, 8, 29)."""


#: Maps documented error codes onto the exception class used to report them.
_ERROR_CLASSES: Final[dict[int, type[SigurCommandError]]] = {
    OifErrorCode.UNABLE_TO_CONNECT_TO_DB: SigurServerError,
    OifErrorCode.UNSUPPORTED_INTERFACE_VERSION: SigurUnsupportedVersionError,
    OifErrorCode.NOT_LOGGED_IN: SigurAuthError,
    OifErrorCode.GENERIC_SQL_ERROR: SigurServerError,
    OifErrorCode.UNKNOWN_OBJECT: SigurUnknownObjectError,
    OifErrorCode.INTERNAL_ERROR: SigurServerError,
    OifErrorCode.CONCURRENT_TRANSACTION_IS_IN_PROGRESS: SigurBusyError,
    OifErrorCode.UNKNOWN_ACCESS_POINT: SigurUnknownAccessPointError,
    OifErrorCode.AUTHENTICATION_FAILED: SigurAuthError,
    OifErrorCode.DELEGATION_IS_DISABLED: SigurPermissionError,
    OifErrorCode.FACE_RECOGNITION_IS_OFF: SigurPermissionError,
    OifErrorCode.OIF_ACCESS_IS_DISABLED_FOR_THIS_USER: SigurPermissionError,
    OifErrorCode.EXTMEM_ERROR: SigurServerError,
}


def error_from_reply(
    code: int, text: str, *, command: str | None = None
) -> SigurCommandError:
    """Build the most specific exception for an ``ERROR`` reply."""
    cls = _ERROR_CLASSES.get(code, SigurCommandError)
    return cls(code, text, command=command)
