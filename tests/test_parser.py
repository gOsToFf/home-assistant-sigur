"""Tests for the OIF tokenizer and message parsers."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.sigur.api.errors import (
    SigurAuthError,
    SigurBusyError,
    SigurCommandError,
    SigurPermissionError,
    SigurProtocolError,
    SigurUnknownAccessPointError,
    SigurUnknownObjectError,
    SigurUnsupportedVersionError,
    error_from_reply,
)
from custom_components.sigur.api.event_codes import (
    EVENT_CODES,
    EventCategory,
    resolve_event_code,
)
from custom_components.sigur.api.parser import (
    ApOpenState,
    ApState,
    TokenStream,
    TokenType,
    decode_quoted,
    parse_apinfo,
    parse_aplist,
    parse_classic_event,
    parse_error,
    parse_event_ce,
    parse_history,
    parse_location,
    parse_objectinfo,
    parse_zoneinfo,
    quote,
    split_reply,
    tokenize,
)


def test_tokenize_splits_words_strings_and_commas() -> None:
    """Bare words, quoted strings and separators become distinct tokens."""
    tokens = tokenize('ZONEINFO ID 1 NAME "A", ID 2 NAME "B"')
    assert [t.type for t in tokens[:4]] == [
        TokenType.WORD,
        TokenType.WORD,
        TokenType.WORD,
        TokenType.WORD,
    ]
    assert tokens[4].type is TokenType.STRING
    assert tokens[5].is_comma


def test_tokenize_keeps_spaces_and_commas_inside_quotes() -> None:
    """A quoted string is opaque to the splitter."""
    tokens = tokenize('APINFO NAME "Главный вход, второй этаж"')
    assert tokens[-1].value == "Главный вход, второй этаж"


def test_decode_quoted_expands_hex_escapes() -> None:
    """``#NN`` escapes decode to the UTF-8 bytes they name."""
    assert decode_quoted(b"#D1#8E#D1#80#D0#B8#D1#81#D1#82") == "юрист"


def test_decode_quoted_mixes_literal_and_escaped_bytes() -> None:
    """Escapes may appear next to plain ASCII."""
    assert decode_quoted(b"A#20B") == "A B"


def test_decode_quoted_survives_broken_utf8() -> None:
    """A malformed byte sequence never raises out of the tokenizer."""
    assert decode_quoted(b"#FF#FE") == "��"


def test_quote_escapes_the_characters_that_break_framing() -> None:
    """Quotes, hashes and control bytes are always escaped on the way out."""
    assert quote('a"b#c') == '"a#22b#23c"'
    assert quote("line\r\n") == '"line#0D#0A"'


def test_quote_round_trips_cyrillic() -> None:
    """Encoding then decoding a Russian string is lossless."""
    assert decode_quoted(quote("Александр")[1:-1].encode("utf-8")) == "Александр"


def test_tokenize_rejects_an_unterminated_string() -> None:
    """A missing closing quote is a protocol error, not a silent truncation."""
    with pytest.raises(SigurProtocolError, match="unterminated"):
        tokenize('APINFO NAME "Главный вход')


def test_token_stream_reports_type_errors() -> None:
    """Reading the wrong shape produces a protocol error with context."""
    stream = TokenStream(tokenize("APINFO ID abc"))
    stream.next_value()
    stream.expect("ID")
    with pytest.raises(SigurProtocolError, match="expected an integer"):
        stream.next_int()


def test_parse_zoneinfo_matches_the_specification_example() -> None:
    """``ZONEINFO ID 1 NAME "A", ID 2 NAME "B"`` parses into two zones."""
    _, stream = split_reply('ZONEINFO ID 1 NAME "A", ID 2 NAME "B"')
    zones = parse_zoneinfo(stream)
    assert [(z.id, z.name) for z in zones] == [(1, "A"), (2, "B")]


def test_parse_aplist_matches_the_specification_example() -> None:
    """``APLIST 1 2 3 4 5 6`` parses into six ids."""
    _, stream = split_reply("APLIST 1 2 3 4 5 6")
    assert parse_aplist(stream) == [1, 2, 3, 4, 5, 6]


def test_parse_aplist_handles_empty() -> None:
    """``APLIST EMPTY`` means no access points, not a parse failure."""
    _, stream = split_reply("APLIST EMPTY")
    assert parse_aplist(stream) == []


def test_parse_apinfo_matches_the_specification_example() -> None:
    """The documented ``APINFO`` example parses field by field."""
    _, stream = split_reply(
        'APINFO ID 1 NAME "Главный вход" ZONEA 1 ZONEB 2 STATE ONLINE_NORMAL CLOSED'
    )
    info = parse_apinfo(stream)
    assert info.id == 1
    assert info.name == "Главный вход"
    assert (info.zone_a, info.zone_b) == (1, 2)
    assert info.state is ApState.ONLINE_NORMAL
    assert info.open_state is ApOpenState.CLOSED
    assert info.online is True
    assert info.mode is not None and info.mode.value == "NORMAL"


def test_parse_apinfo_offline_has_no_mode() -> None:
    """An offline access point exposes no lock mode."""
    _, stream = split_reply(
        'APINFO ID 4 NAME "Ворота" ZONEA 1 ZONEB 2 STATE OFFLINE UNKNOWN'
    )
    info = parse_apinfo(stream)
    assert info.online is False
    assert info.mode is None


def test_parse_apinfo_rejects_an_unknown_state() -> None:
    """An undocumented ``<state>`` is a protocol error."""
    _, stream = split_reply(
        'APINFO ID 4 NAME "Ворота" ZONEA 1 ZONEB 2 STATE ONLINE_FOO CLOSED'
    )
    with pytest.raises(SigurProtocolError, match="unknown access point state"):
        parse_apinfo(stream)


def test_parse_apinfo_tolerates_a_missing_open_state() -> None:
    """A truncated reply degrades to ``UNKNOWN`` rather than failing."""
    _, stream = split_reply(
        'APINFO ID 4 NAME "Ворота" ZONEA 1 ZONEB 2 STATE ONLINE_NORMAL'
    )
    assert parse_apinfo(stream).open_state is ApOpenState.UNKNOWN


def test_parse_objectinfo_matches_the_specification_example() -> None:
    """The mixed ``EMP``/``GUESTBADGE`` example parses into three objects."""
    _, stream = split_reply(
        'OBJECTINFO EMP ID 6 NAME "1" POSITION "2" TABNUMBER "001", '
        'EMP ID 7 NAME "3" POSITION "4" TABNUMBER "002", '
        'GUESTBADGE ID 9 NAME "Пропуск посетителя № 1" TABNUMBER "003"'
    )
    objects = parse_objectinfo(stream)
    assert [(o.kind, o.id) for o in objects] == [
        ("EMP", 6),
        ("EMP", 7),
        ("GUESTBADGE", 9),
    ]
    assert objects[2].name == "Пропуск посетителя № 1"
    assert objects[2].position is None


def test_parse_objectinfo_reads_car_fields() -> None:
    """``CAR`` items expose the plate and the model."""
    _, stream = split_reply(
        'OBJECTINFO CAR ID 21 NUMBER "a123bc12" MODEL "Lada" TABNUMBER "004"'
    )
    car = parse_objectinfo(stream)[0]
    assert (car.car_number, car.car_model) == ("a123bc12", "Lada")
    assert car.display_name == "a123bc12"


def test_parse_location_v2_matches_the_specification_example() -> None:
    """``LOCATION OBJECT 10 ZONE 1 ACTTIME ...`` parses fully."""
    _, stream = split_reply('LOCATION OBJECT 10 ZONE 1 ACTTIME "2006-01-12 12:34:00"')
    location = parse_location(stream)
    assert location.object_id == 10
    assert location.zone_id == 1
    assert location.act_time == datetime(2006, 1, 12, 12, 34)
    assert location.known is True


def test_parse_location_unknown() -> None:
    """An unknown location keeps the object id and reports ``known`` false."""
    _, stream = split_reply("LOCATION OBJECT 10 UNKNOWN")
    location = parse_location(stream)
    assert location.known is False
    assert location.zone_id is None


def test_parse_event_ce_matches_the_specification_example() -> None:
    """The documented ``CE_WITH_NAMES`` example parses field by field."""
    line = 'EVENT_CE "2025-01-27 11:23:08" 39 3 6 2 W26 249 29323 "Иванов Иван"'
    _, stream = split_reply(line)
    event = parse_event_ce(stream, line)
    assert event.occurred_at == datetime(2025, 1, 27, 11, 23, 8)
    assert event.event_code == 39
    assert event.access_point_id == 3
    assert event.object_id == 6
    assert event.direction_code == 2
    assert event.direction == "in"
    assert event.object_name == "Иванов Иван"
    assert event.key.format == "W26"


def test_parse_event_ce_without_a_name() -> None:
    """Plain ``CE`` mode omits the trailing object name."""
    line = 'EVENT_CE "2025-01-27 11:23:08" 4 1 6 1 W34 DEADBEEF'
    _, stream = split_reply(line)
    event = parse_event_ce(stream, line)
    assert event.object_name is None
    assert event.direction == "out"
    assert event.key.hex_value == "DEADBEEF"


def test_parse_event_ce_unknown_key() -> None:
    """``UNKNOWN`` is a valid ``<key>`` and yields a redacted placeholder."""
    line = 'EVENT_CE "2025-01-27 11:23:08" 1 1 0 0 UNKNOWN'
    _, stream = split_reply(line)
    event = parse_event_ce(stream, line)
    assert event.key.masked == "UNKNOWN"
    assert event.object_id is None
    assert event.direction == "none"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('EVENT_CE "2025-01-27 11:23:08" 4 1 6 2 W26 249 29323', "W26 ***23"),
        ('EVENT_CE "2025-01-27 11:23:08" 4 1 6 2 W34 DEADBEEF', "W34 ******EF"),
    ],
)
def test_credential_keys_are_masked(line: str, expected: str) -> None:
    """A credential number is never exposed in full."""
    _, stream = split_reply(line)
    assert parse_event_ce(stream, line).key.masked == expected


def test_parse_history_matches_the_specification_example() -> None:
    """The documented two-event ``HISTORY`` reply parses into two records."""
    line = (
        'HISTORY "2006-01-12 12:34:00" OBJECTPASS 1 1 IN W26 123 12345, '
        '"2006-01-12 12:34:12" OBJECTPASS 1 1 OUT W26 123 12345'
    )
    _, stream = split_reply(line)
    events = parse_history(stream, line)
    assert len(events) == 2
    assert [e.direction for e in events] == ["in", "out"]
    assert events[0].classic_type == "OBJECTPASS"


def test_parse_history_empty() -> None:
    """A ``HISTORY`` reply with no events yields an empty list."""
    _, stream = split_reply("HISTORY")
    assert parse_history(stream, "HISTORY") == []


@pytest.mark.parametrize(
    ("body", "kind", "object_id"),
    [
        ("BREAKINGPASS 1 IN", "BREAKINGPASS", None),
        ("FREEPASS 2 OUT", "FREEPASS", None),
        ("MANUALPASS 3 UNKNOWN", "MANUALPASS", None),
        ("OPENDOOR 1 7 IN W26 001 00002", "OPENDOOR", 7),
    ],
)
def test_parse_classic_pass_variants(
    body: str, kind: str, object_id: int | None
) -> None:
    """Each documented ``<pass-event-description>`` shape parses correctly."""
    line = f'"2006-01-12 12:34:00" {body}'
    event = parse_classic_event(TokenStream(tokenize(line)), line)
    assert event.classic_type == kind
    assert event.object_id == object_id


def test_parse_classic_deny_keeps_the_reason() -> None:
    """A ``DENY`` event keeps its documented ``<deny-reason>``."""
    line = '"2006-01-12 12:34:00" DENY 1 5 IN RULEDENYTIME W26 123 12345'
    event = parse_classic_event(TokenStream(tokenize(line)), line)
    assert event.classic_type == "DENY"
    assert event.deny_reason == "RULEDENYTIME"
    assert event.object_id == 5


def test_parse_classic_unknown_kind_is_preserved() -> None:
    """An undocumented classic event keeps its timestamp and raw payload."""
    line = '"2006-01-12 12:34:00" SOMETHINGNEW 1 2 3'
    event = parse_classic_event(TokenStream(tokenize(line)), line)
    assert event.classic_type == "SOMETHINGNEW"
    assert event.raw_message == line


def test_parse_error_maps_documented_codes() -> None:
    """An ``ERROR`` reply becomes the most specific exception available."""
    _, stream = split_reply("ERROR 11 AUTHENTICATION FAILED")
    err = parse_error(stream, command="LOGIN")
    assert isinstance(err, SigurAuthError)
    assert err.code == 11
    assert err.text == "AUTHENTICATION FAILED"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (3, SigurUnsupportedVersionError),
        (4, SigurAuthError),
        (7, SigurUnknownObjectError),
        (9, SigurBusyError),
        (10, SigurUnknownAccessPointError),
        (11, SigurAuthError),
        (21, SigurPermissionError),
        (28, SigurCommandError),
    ],
)
def test_error_classes_cover_the_documented_range(
    code: int, expected: type[Exception]
) -> None:
    """Every mapped code produces the right exception class."""
    assert isinstance(error_from_reply(code, "text"), expected)


def test_every_documented_error_code_is_known() -> None:
    """Codes 1-29 all resolve to a documented enum member."""
    from custom_components.sigur.api.errors import ERROR_TEXTS, OifErrorCode

    assert sorted(ERROR_TEXTS) == list(range(1, 30))
    assert len(list(OifErrorCode)) == 29


def test_undocumented_error_code_still_raises_a_usable_exception() -> None:
    """A future error code degrades to the generic class, keeping the text."""
    err = error_from_reply(999, "SOMETHING NEW")
    assert isinstance(err, SigurCommandError)
    assert err.error_code is None
    assert "999" in str(err)


def test_split_reply_rejects_an_empty_line() -> None:
    """An empty message is a protocol error."""
    with pytest.raises(SigurProtocolError, match="empty message"):
        split_reply("")


def test_event_code_table_covers_the_documented_range() -> None:
    """Every documented code 0-93 is present, and the unused ones are absent."""
    unused = {0, 7, 26, 27, 33, 34, 35}
    assert set(EVENT_CODES) == set(range(1, 94)) - unused


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (1, EventCategory.BREAK_IN),
        (4, EventCategory.PASS_REGISTERED),
        (10, EventCategory.ACCESS_DENIED),
        (20, EventCategory.LINK_LOST),
        (21, EventCategory.LINK_RESTORED),
        (24, EventCategory.ACCESS_GRANTED),
        (25, EventCategory.DOOR_HELD_OPEN_START),
        (28, EventCategory.POWER_MAINS),
        (29, EventCategory.POWER_BATTERY),
        (31, EventCategory.MODE_CHANGED),
        (36, EventCategory.DOOR_CLOSED),
        (37, EventCategory.DOOR_OPENED),
        (38, EventCategory.DOOR_HELD_OPEN_END),
        (65, EventCategory.LOCK_FAULT),
        (93, EventCategory.WAITING),
    ],
)
def test_event_categories_match_the_specification(
    code: int, category: EventCategory
) -> None:
    """Codes required by the device triggers map to the right category."""
    assert resolve_event_code(code).category is category


@pytest.mark.parametrize(
    ("code", "event_type"),
    [
        (256, "ops_sigur_0"),
        (259, "ops_sigur_3"),
        (512, "ops_bolid_0"),
        (768, "ops_rubezh_security_0"),
        (800, "ops_rubezh_security_32"),
        (1024, "ops_rubezh_fire_0"),
        (1025, "ops_rubezh_fire_1"),
    ],
)
def test_extended_alarm_panel_ranges(code: int, event_type: str) -> None:
    """``256+N``/``512+N``/``768+N``/``1024+N`` resolve to panel sub-codes."""
    resolved = resolve_event_code(code)
    assert resolved.category is EventCategory.ALARM_PANEL
    assert resolved.event_type == event_type
    assert resolved.ops_sub_code == code % 256


def test_unknown_event_code_never_raises() -> None:
    """An undocumented code is reported as unknown, keeping its number."""
    resolved = resolve_event_code(5000)
    assert resolved.category is EventCategory.UNKNOWN
    assert resolved.known is False
    assert resolved.code == 5000
    assert "5000" in resolved.description_en


@pytest.mark.parametrize("code", [0, 7, 26, 27, 33, 34, 35])
def test_codes_marked_unused_resolve_to_unknown(code: int) -> None:
    """Codes the specification marks "(не используется)" are not invented."""
    assert resolve_event_code(code).category is EventCategory.UNKNOWN


# --- Shapes that occur in the field ---------------------------------------
#
# The specification's examples do not cover everything a deployed system
# answers with. These cases use invented names, but the shapes are real, and
# each one would have broken a stricter parser.


def test_zone_id_zero_is_a_real_zone() -> None:
    """Zone 0 exists, so zone ids must not be assumed positive."""
    _, stream = split_reply(
        'ZONEINFO ID 0 NAME "Улица", ID 2 NAME "Территория", ID 11 NAME "Парковка"'
    )
    zones = parse_zoneinfo(stream)
    assert [z.id for z in zones] == [0, 2, 11]
    assert zones[0].name == "Улица"


def test_aplist_order_is_preserved_not_sorted() -> None:
    """``APLIST`` is not ordered, and the parser must not reorder it."""
    _, stream = split_reply("APLIST 1 12 2 3 7 5 4")
    assert parse_aplist(stream) == [1, 12, 2, 3, 7, 5, 4]


def test_apinfo_accepts_zone_zero_as_the_exit_zone() -> None:
    """``ZONEB 0`` is what an outward-facing access point reports."""
    _, stream = split_reply(
        'APINFO ID 1 NAME "Ворота" ZONEA 5 ZONEB 0 STATE ONLINE_NORMAL CLOSED'
    )
    info = parse_apinfo(stream)
    assert (info.zone_a, info.zone_b) == (5, 0)
    assert info.name == "Ворота"


@pytest.mark.parametrize(
    ("reply", "state", "open_state"),
    [
        (
            'APINFO ID 1 NAME "Шлагбаум выезд" ZONEA 0 ZONEB 5 '
            "STATE ONLINE_UNLOCKED OPENED",
            ApState.ONLINE_UNLOCKED,
            ApOpenState.OPENED,
        ),
        (
            'APINFO ID 3 NAME "Шлагбаум въезд" ZONEA 5 ZONEB 0 '
            "STATE ONLINE_LOCKED CLOSED",
            ApState.ONLINE_LOCKED,
            ApOpenState.CLOSED,
        ),
        (
            'APINFO ID 18 NAME "Выезд 2" ZONEA 3 ZONEB 0 STATE OFFLINE UNKNOWN',
            ApState.OFFLINE,
            ApOpenState.UNKNOWN,
        ),
    ],
)
def test_apinfo_state_combinations(
    reply: str, state: ApState, open_state: ApOpenState
) -> None:
    """Every ``<state>``/``<open-state>`` pair a controller can report."""
    _, stream = split_reply(reply)
    info = parse_apinfo(stream)
    assert info.state is state
    assert info.open_state is open_state


def test_names_may_arrive_as_raw_utf8_not_escaped() -> None:
    """``#NN`` escaping is optional: Cyrillic may be sent verbatim."""
    _, stream = split_reply(
        'APINFO ID 25 NAME "Проходная " ZONEA 5 ZONEB 0 STATE ONLINE_NORMAL CLOSED'
    )
    # A trailing space inside the quotes is significant and must survive.
    assert parse_apinfo(stream).name == "Проходная "


def test_location_unknown_v2_shape() -> None:
    """``LOCATION OBJECT <id> UNKNOWN`` is the reply for an unplaced object."""
    _, stream = split_reply("LOCATION OBJECT 1 UNKNOWN")
    location = parse_location(stream)
    assert location.object_id == 1
    assert location.known is False
