"""Tokenizer and message parsers for the Sigur OIF text protocol.

The protocol is line oriented (CRLF terminated), UTF-8 encoded, and mixes bare
words, decimal integers, ``,`` list separators and double quoted strings.
Inside a quoted string a byte may be written as ``#NN`` where ``NN`` is its
hexadecimal representation, e.g. ``"#D1#8E#D1#80#D0#B8#D1#81#D1#82"`` is the
UTF-8 encoding of ``юрист``.

No Home Assistant import belongs in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
import re
from typing import Final

from .errors import SigurProtocolError, error_from_reply

#: ``"YYYY-MM-DD hh:mm:ss"`` as specified in "Кодирование типов данных".
DATETIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

_HEX_ESCAPE: Final = re.compile(rb"#([0-9A-Fa-f]{2})")
#: Bytes that must never be emitted raw inside a quoted string.
_NEEDS_ESCAPE: Final = frozenset({0x22, 0x23, 0x0D, 0x0A, 0x00})
_WHITESPACE: Final = frozenset({0x20, 0x09, 0x0D, 0x0A})


class TokenType(Enum):
    """Kind of a lexical token."""

    WORD = "word"
    STRING = "string"
    COMMA = "comma"


@dataclass(frozen=True, slots=True)
class Token:
    """A single lexical token of an OIF message."""

    type: TokenType
    value: str

    @property
    def is_comma(self) -> bool:
        """Whether this token is a list separator."""
        return self.type is TokenType.COMMA


def decode_quoted(raw: bytes) -> str:
    """Decode the raw bytes of a quoted string, expanding ``#NN`` escapes."""
    expanded = _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    # Sigur emits UTF-8; tolerate a broken sequence rather than dropping a whole
    # event, and never raise from the tokenizer over a single payload byte.
    return expanded.decode("utf-8", errors="replace")


def quote(value: str) -> str:
    """Encode a Python string as an OIF quoted string.

    Only bytes that would break framing are escaped as ``#NN``; everything
    else is emitted as-is, so the result is still a Python string whose UTF-8
    encoding is exactly what goes on the wire. Every byte needing an escape is
    ASCII, and no byte of a multi-byte UTF-8 sequence is, so escaping can be
    decided per character.
    """
    out = ['"']
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or code in _NEEDS_ESCAPE:
            out.extend(f"#{byte:02X}" for byte in char.encode("utf-8"))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def tokenize(line: str) -> list[Token]:
    """Split an OIF line into tokens.

    Raises:
        SigurProtocolError: if a quoted string is not terminated.

    """
    tokens: list[Token] = []
    raw = line.encode("utf-8", errors="surrogateescape")
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if char in _WHITESPACE:
            index += 1
            continue
        if char == 0x2C:  # ,
            tokens.append(Token(TokenType.COMMA, ","))
            index += 1
            continue
        if char == 0x22:  # "
            end = raw.find(b'"', index + 1)
            if end == -1:
                raise SigurProtocolError("unterminated quoted string")
            tokens.append(Token(TokenType.STRING, decode_quoted(raw[index + 1 : end])))
            index = end + 1
            continue
        end = index
        while (
            end < length
            and raw[end] not in _WHITESPACE
            and raw[end]
            not in (
                0x2C,
                0x22,
            )
        ):
            end += 1
        tokens.append(
            Token(TokenType.WORD, raw[index:end].decode("utf-8", errors="replace"))
        )
        index = end
    return tokens


class TokenStream:
    """Cursor over a token list with protocol-aware accessors."""

    def __init__(self, tokens: list[Token]) -> None:
        """Wrap an already tokenized message."""
        self._tokens = tokens
        self._pos = 0

    def __bool__(self) -> bool:
        """Whether any token is left."""
        return self._pos < len(self._tokens)

    @property
    def remaining(self) -> list[Token]:
        """Tokens that have not been consumed yet."""
        return self._tokens[self._pos :]

    def peek(self) -> Token | None:
        """Return the next token without consuming it."""
        if self._pos >= len(self._tokens):
            return None
        return self._tokens[self._pos]

    def next(self) -> Token:
        """Consume and return the next token."""
        token = self.peek()
        if token is None:
            raise SigurProtocolError("unexpected end of message")
        self._pos += 1
        return token

    def next_value(self) -> str:
        """Consume the next token and return its textual value."""
        token = self.next()
        if token.is_comma:
            raise SigurProtocolError("unexpected ',' in message")
        return token.value

    def next_int(self) -> int:
        """Consume the next token and parse it as a decimal integer."""
        value = self.next_value()
        try:
            return int(value, 10)
        except ValueError as err:
            raise SigurProtocolError(f"expected an integer, got {value!r}") from err

    def next_datetime(self) -> datetime:
        """Consume the next token and parse it as an OIF timestamp."""
        value = self.next_value()
        try:
            return datetime.strptime(value, DATETIME_FORMAT)
        except ValueError as err:
            raise SigurProtocolError(f"invalid timestamp {value!r}") from err

    def expect(self, *keywords: str) -> str:
        """Consume the next token, requiring it to be one of ``keywords``."""
        value = self.next_value()
        if value.upper() not in {kw.upper() for kw in keywords}:
            expected = ", ".join(keywords)
            raise SigurProtocolError(f"expected one of {expected}, got {value!r}")
        return value.upper()

    def skip_comma(self) -> bool:
        """Consume a list separator if the cursor sits on one."""
        token = self.peek()
        if token is not None and token.is_comma:
            self._pos += 1
            return True
        return False


class ApState(StrEnum):
    """Link and lock mode of an access point (``<state>``)."""

    OFFLINE = "OFFLINE"
    ONLINE_NORMAL = "ONLINE_NORMAL"
    ONLINE_LOCKED = "ONLINE_LOCKED"
    ONLINE_UNLOCKED = "ONLINE_UNLOCKED"


class ApOpenState(StrEnum):
    """Physical door position of an access point (``<open-state>``)."""

    UNKNOWN = "UNKNOWN"
    OPENED = "OPENED"
    CLOSED = "CLOSED"


class ApMode(StrEnum):
    """Lock mode accepted by ``SETAPMODE`` (``<mode>``)."""

    NORMAL = "NORMAL"
    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"


class Direction(StrEnum):
    """Textual pass direction used by classic events and ``ALLOWPASS``."""

    IN = "IN"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"


class DirectionCode(int, Enum):
    """Numeric ``<directioncode>`` used by ``EVENT_CE``."""

    NONE = 0
    OUT = 1
    IN = 2
    UNKNOWN = 3

    @property
    def normalized(self) -> str:
        """Direction as published on the Home Assistant event bus."""
        return _DIRECTION_NAMES[self]


_DIRECTION_NAMES: Final[dict[DirectionCode, str]] = {
    DirectionCode.NONE: "none",
    DirectionCode.OUT: "out",
    DirectionCode.IN: "in",
    DirectionCode.UNKNOWN: "unknown",
}

_TEXT_DIRECTION_NAMES: Final[dict[str, str]] = {
    Direction.IN.value: "in",
    Direction.OUT.value: "out",
    Direction.UNKNOWN.value: "unknown",
}


def normalize_direction_code(code: int) -> str:
    """Map a numeric direction code onto ``in``/``out``/``unknown``/``none``."""
    try:
        return DirectionCode(code).normalized
    except ValueError:
        return "unknown"


def normalize_direction_text(text: str) -> str:
    """Map a textual direction onto ``in``/``out``/``unknown``."""
    return _TEXT_DIRECTION_NAMES.get(text.upper(), "unknown")


def direction_to_protocol(direction: str) -> Direction:
    """Map a normalized direction back onto the protocol keyword."""
    try:
        return Direction(direction.upper())
    except ValueError:
        return Direction.UNKNOWN


class DenyReason(StrEnum):
    """``<deny-reason>`` of a classic ``DENY`` event."""

    UNKNOWN = "UNKNOWN"
    SYSTEM = "SYSTEM"
    UNKNOWNKEY = "UNKNOWNKEY"
    RULEDENY = "RULEDENY"
    RULEDENYAP = "RULEDENYAP"
    RULEDENYTIME = "RULEDENYTIME"


@dataclass(frozen=True, slots=True)
class CredentialKey:
    """A credential number (``<key>``) in Wiegand-26 or Wiegand-34 form."""

    format: str
    """``UNKNOWN``, ``W26`` or ``W34``."""

    facility: int | None = None
    """Wiegand-26 facility code (0-255)."""

    number: int | None = None
    """Wiegand-26 serial number (0-65535)."""

    hex_value: str | None = None
    """Wiegand-34 value as 8 hexadecimal characters."""

    @property
    def masked(self) -> str:
        """Redacted form, safe for logs, events and diagnostics.

        Keeps just enough to correlate events (the format plus the last two
        digits) while never disclosing a usable credential number.
        """
        if self.format == "W26" and self.number is not None:
            return f"W26 ***{self.number % 100:02d}"
        if self.format == "W34" and self.hex_value:
            return f"W34 ******{self.hex_value[-2:]}"
        return "UNKNOWN"

    @property
    def raw(self) -> str:
        """Unredacted protocol representation, for outbound commands only."""
        if self.format == "W26" and self.facility is not None:
            return f"W26 {self.facility:03d} {self.number or 0:05d}"
        if self.format == "W34" and self.hex_value:
            return f"W34 {self.hex_value}"
        return "UNKNOWN"


def parse_key(stream: TokenStream) -> CredentialKey:
    """Parse a ``<key>`` production from the stream."""
    kind = stream.next_value().upper()
    if kind == "W26":
        facility = stream.next_int()
        number = stream.next_int()
        return CredentialKey("W26", facility=facility, number=number)
    if kind == "W34":
        return CredentialKey("W34", hex_value=stream.next_value().upper())
    return CredentialKey("UNKNOWN")


@dataclass(frozen=True, slots=True)
class ZoneInfo:
    """One ``<zone-info-item>`` of a ``ZONEINFO`` reply."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class AccessPointInfo:
    """An ``APINFO`` reply for a single access point."""

    id: int
    name: str
    zone_a: int
    zone_b: int
    state: ApState
    open_state: ApOpenState

    @property
    def online(self) -> bool:
        """Whether the server currently has a link to the access point."""
        return self.state is not ApState.OFFLINE

    @property
    def mode(self) -> ApMode | None:
        """Lock mode, or ``None`` while the access point is offline."""
        return _STATE_TO_MODE.get(self.state)


_STATE_TO_MODE: Final[dict[ApState, ApMode]] = {
    ApState.ONLINE_NORMAL: ApMode.NORMAL,
    ApState.ONLINE_LOCKED: ApMode.LOCKED,
    ApState.ONLINE_UNLOCKED: ApMode.UNLOCKED,
}

#: Inverse of :data:`_STATE_TO_MODE`, used for optimistic state updates.
MODE_TO_STATE: Final[dict[ApMode, ApState]] = {
    mode: state for state, mode in _STATE_TO_MODE.items()
}


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """One ``<object-info-item>`` of an ``OBJECTINFO`` reply."""

    id: int
    kind: str
    """``EMP``, ``GUEST``, ``GUESTBADGE`` or ``CAR`` as reported by the server."""

    name: str | None = None
    position: str | None = None
    tab_number: str | None = None
    car_number: str | None = None
    car_model: str | None = None

    @property
    def display_name(self) -> str | None:
        """Best human readable label for this object."""
        return self.name or self.car_number


@dataclass(frozen=True, slots=True)
class ObjectLocation:
    """A ``LOCATION`` reply."""

    object_id: int | None
    zone_id: int | None
    act_time: datetime | None

    @property
    def known(self) -> bool:
        """Whether the server knows where the object currently is."""
        return self.zone_id is not None


@dataclass(slots=True)
class RawEvent:
    """A parsed ``EVENT``/``EVENT_CE``/``HISTORY`` record.

    ``event_code`` is populated for ``EVENT_CE`` messages; classic events carry
    ``classic_type`` instead. Both forms are normalized further up the stack.
    """

    occurred_at: datetime
    access_point_id: int | None = None
    object_id: int | None = None
    event_code: int | None = None
    classic_type: str | None = None
    deny_reason: str | None = None
    direction: str = "unknown"
    direction_code: int | None = None
    object_name: str | None = None
    key: CredentialKey = field(default_factory=lambda: CredentialKey("UNKNOWN"))
    raw_message: str = ""


def parse_zoneinfo(stream: TokenStream) -> list[ZoneInfo]:
    """Parse the body of a ``ZONEINFO`` reply."""
    zones: list[ZoneInfo] = []
    while stream:
        stream.expect("ID")
        zone_id = stream.next_int()
        stream.expect("NAME")
        zones.append(ZoneInfo(zone_id, stream.next_value()))
        if not stream.skip_comma():
            break
    return zones


def parse_aplist(stream: TokenStream) -> list[int]:
    """Parse the body of an ``APLIST`` reply."""
    first = stream.peek()
    if first is not None and first.value.upper() == "EMPTY":
        return []
    ap_ids: list[int] = []
    while stream:
        stream.skip_comma()
        if not stream:
            break
        ap_ids.append(stream.next_int())
    return ap_ids


def parse_apinfo(stream: TokenStream) -> AccessPointInfo:
    """Parse the body of an ``APINFO`` reply."""
    stream.expect("ID")
    ap_id = stream.next_int()
    stream.expect("NAME")
    name = stream.next_value()
    stream.expect("ZONEA")
    zone_a = stream.next_int()
    stream.expect("ZONEB")
    zone_b = stream.next_int()
    stream.expect("STATE")
    state_raw = stream.next_value().upper()
    try:
        state = ApState(state_raw)
    except ValueError as err:
        raise SigurProtocolError(f"unknown access point state {state_raw!r}") from err
    open_raw = stream.next_value().upper() if stream else ApOpenState.UNKNOWN.value
    try:
        open_state = ApOpenState(open_raw)
    except ValueError:
        open_state = ApOpenState.UNKNOWN
    return AccessPointInfo(ap_id, name, zone_a, zone_b, state, open_state)


def parse_objectinfo(stream: TokenStream) -> list[ObjectInfo]:
    """Parse the body of an ``OBJECTINFO`` reply."""
    objects: list[ObjectInfo] = []
    while stream:
        kind = stream.next_value().upper()
        stream.expect("ID")
        object_id = stream.next_int()
        fields: dict[str, str] = {}
        while stream:
            token = stream.peek()
            if token is None or token.is_comma:
                break
            key = stream.next_value().upper()
            if not stream:
                break
            fields[key] = stream.next_value()
        objects.append(
            ObjectInfo(
                id=object_id,
                kind=kind,
                name=fields.get("NAME"),
                position=fields.get("POSITION"),
                tab_number=fields.get("TABNUMBER"),
                car_number=fields.get("NUMBER"),
                car_model=fields.get("MODEL"),
            )
        )
        if not stream.skip_comma():
            break
    return objects


def parse_location(stream: TokenStream) -> ObjectLocation:
    """Parse the body of a ``LOCATION`` reply (both v1 and v2 forms)."""
    object_id: int | None = None
    token = stream.peek()
    if token is not None and token.value.upper() == "OBJECT":
        stream.next()
        object_id = stream.next_int()
    marker = stream.next_value().upper()
    if marker == "UNKNOWN":
        return ObjectLocation(object_id, None, None)
    if marker != "ZONE":
        raise SigurProtocolError(f"unexpected LOCATION payload {marker!r}")
    zone_id = stream.next_int()
    stream.expect("ACTTIME")
    return ObjectLocation(object_id, zone_id, stream.next_datetime())


#: Classic ``<pass-event-description>`` variants and whether each carries an
#: ``<object-id>`` and a ``<key>``, per the GETHISTORY BNF.
_CLASSIC_PASS_SHAPES: Final[dict[str, tuple[bool, bool]]] = {
    "OBJECTPASS": (True, True),
    "BREAKINGPASS": (False, False),
    "FREEPASS": (False, False),
    "MANUALPASS": (False, False),
    "OPENDOOR": (True, True),
}


def parse_classic_event(stream: TokenStream, raw_message: str) -> RawEvent:
    """Parse one ``<event>`` of a ``HISTORY`` reply or a classic ``EVENT``."""
    occurred_at = stream.next_datetime()
    kind = stream.next_value().upper()
    if kind == "DENY":
        ap_id = stream.next_int()
        object_id = stream.next_int()
        direction = stream.next_value().upper()
        reason = stream.next_value().upper()
        key = parse_key(stream) if stream else CredentialKey("UNKNOWN")
        return RawEvent(
            occurred_at=occurred_at,
            access_point_id=ap_id,
            object_id=object_id or None,
            classic_type=kind,
            deny_reason=reason,
            direction=normalize_direction_text(direction),
            key=key,
            raw_message=raw_message,
        )

    shape = _CLASSIC_PASS_SHAPES.get(kind)
    if shape is None:
        # Unknown classic event kind: keep the timestamp and the raw payload
        # instead of failing the whole batch.
        return RawEvent(
            occurred_at=occurred_at, classic_type=kind, raw_message=raw_message
        )

    has_object, has_key = shape
    pass_ap_id = stream.next_int()
    pass_object_id = stream.next_int() if has_object else None
    direction = stream.next_value().upper() if stream else Direction.UNKNOWN.value
    key = parse_key(stream) if has_key and stream else CredentialKey("UNKNOWN")
    return RawEvent(
        occurred_at=occurred_at,
        access_point_id=pass_ap_id,
        object_id=pass_object_id or None,
        classic_type=kind,
        direction=normalize_direction_text(direction),
        key=key,
        raw_message=raw_message,
    )


def parse_history(stream: TokenStream, raw_message: str) -> list[RawEvent]:
    """Parse the body of a ``HISTORY`` reply."""
    events: list[RawEvent] = []
    while stream:
        events.append(parse_classic_event(stream, raw_message))
        if not stream.skip_comma():
            break
    return events


def parse_event_ce(stream: TokenStream, raw_message: str) -> RawEvent:
    """Parse the body of an ``EVENT_CE`` message (``CE``/``CE_WITH_NAMES``)."""
    occurred_at = stream.next_datetime()
    event_code = stream.next_int()
    ap_id = stream.next_int()
    object_id = stream.next_int()
    direction_code = stream.next_int()
    key = parse_key(stream) if stream else CredentialKey("UNKNOWN")
    object_name: str | None = None
    token = stream.peek()
    if token is not None and token.type is TokenType.STRING:
        object_name = stream.next_value()
    return RawEvent(
        occurred_at=occurred_at,
        access_point_id=ap_id,
        object_id=object_id or None,
        event_code=event_code,
        direction=normalize_direction_code(direction_code),
        direction_code=direction_code,
        object_name=object_name,
        key=key,
        raw_message=raw_message,
    )


def parse_error(stream: TokenStream, *, command: str | None = None) -> Exception:
    """Build an exception from the body of an ``ERROR`` reply."""
    code = stream.next_int()
    text = " ".join(token.value for token in stream.remaining).strip()
    return error_from_reply(code, text, command=command)


def split_reply(line: str) -> tuple[str, TokenStream]:
    """Split a reply line into its uppercased keyword and remaining tokens."""
    tokens = tokenize(line)
    if not tokens:
        raise SigurProtocolError("empty message")
    stream = TokenStream(tokens)
    return stream.next_value().upper(), stream
