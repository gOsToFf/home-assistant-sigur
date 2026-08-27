"""The ``EVENT_CE`` event code table of the Sigur OIF protocol.

Transcribed verbatim from appendix 6.2 "Типы событий" of "Протокол интеграции
OIF" (rev. 27.01.2025, OIF 1.8 / Sigur 1.6.3.14): codes 0-93 plus the extended
alarm-panel ranges ``256+N``, ``512+N``, ``768+N`` and ``1024+N``.

Every code is additionally tagged with a coarse :class:`EventCategory` so that
automations and device triggers can subscribe to a class of events instead of
enumerating dozens of numeric codes. Categories are an integration-level
concept, not part of the protocol.

No Home Assistant import belongs in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Base offsets of the extended alarm-panel ("ОПС") event ranges.
OPS_SIGUR_BASE: Final = 256
OPS_BOLID_BASE: Final = 512
OPS_RUBEZH_SECURITY_BASE: Final = 768
OPS_RUBEZH_FIRE_BASE: Final = 1024
#: Width of one extended range; ``N`` is the panel specific sub-code.
OPS_RANGE_SIZE: Final = 256


class EventCategory(StrEnum):
    """Coarse grouping of event codes, used by device triggers."""

    PASS_REGISTERED = "pass_registered"
    """A pass/drive-through was registered."""

    ACCESS_GRANTED = "access_granted"
    """Access was granted, no pass registered yet."""

    ACCESS_DENIED = "access_denied"
    """Access was denied for any reason."""

    BREAK_IN = "break_in"
    """A break-in was registered."""

    DOOR_OPENED = "door_opened"
    """The door moved to the open position."""

    DOOR_CLOSED = "door_closed"
    """The door moved to the closed position."""

    DOOR_HELD_OPEN_START = "door_held_open_start"
    """The door has been held open for too long."""

    DOOR_HELD_OPEN_END = "door_held_open_end"
    """The door is no longer being held open."""

    LINK_LOST = "link_lost"
    """The link to the access point was lost."""

    LINK_RESTORED = "link_restored"
    """The link to the access point was restored."""

    MODE_CHANGED = "mode_changed"
    """The lock mode of the access point was changed."""

    LOCK_FAULT = "lock_fault"
    """The lock hardware reported an inconsistent state."""

    POWER_MAINS = "power_mains"
    """The controller switched to (or recovered) mains power."""

    POWER_BATTERY = "power_battery"
    """The controller switched to battery power."""

    TAMPER = "tamper"
    """The controller enclosure was opened or closed."""

    FIRE_ALARM = "fire_alarm"
    """A fire alarm started or ended."""

    WAITING = "waiting"
    """The controller is waiting for an additional factor or confirmation."""

    FACE = "face"
    """A face recognition or face mask result."""

    TEMPERATURE = "temperature"
    """A temperature measurement result."""

    POWER_QUALITY = "power_quality"
    """A supply voltage measurement result."""

    ALARM_PANEL = "alarm_panel"
    """An extended alarm panel ("ОПС") event."""

    GATE = "gate"
    """A gate/barrier opened or closed."""

    OTHER = "other"
    """A documented event that does not fit any other category."""

    UNKNOWN = "unknown"
    """A code that is not present in this table."""


@dataclass(frozen=True, slots=True)
class EventDefinition:
    """One row of the ``EVENT_CE`` code table."""

    code: int
    category: EventCategory
    description_ru: str
    description_en: str

    @property
    def slug(self) -> str:
        """Stable machine readable identifier for this code."""
        return f"code_{self.code}"


def _d(
    code: int, category: EventCategory, ru: str, en: str
) -> tuple[int, EventDefinition]:
    """Build one table entry."""
    return code, EventDefinition(code, category, ru, en)


#: Documented ``EVENT_CE`` codes 0-93. Codes marked "(не используется)" in the
#: specification are omitted deliberately: the server should never send them,
#: and if it does they are reported as unknown rather than silently mislabelled.
EVENT_CODES: Final[dict[int, EventDefinition]] = dict(
    (
        _d(1, EventCategory.BREAK_IN, "Зарегистрирован взлом", "Break-in registered"),
        _d(
            2,
            EventCategory.PASS_REGISTERED,
            "Зарегистрирован проход в разблокированном режиме",
            "Pass registered in unlocked mode",
        ),
        _d(
            3,
            EventCategory.PASS_REGISTERED,
            "Зарегистрирован проход, санкционированный с кнопки",
            "Pass registered, authorised from a button",
        ),
        _d(
            4,
            EventCategory.PASS_REGISTERED,
            "Зарегистрирован проход",
            "Pass registered",
        ),
        _d(
            5,
            EventCategory.PASS_REGISTERED,
            "Зарегистрирован проход при открытой двери",
            "Pass registered while the door was open",
        ),
        _d(
            6,
            EventCategory.PASS_REGISTERED,
            "Зарегистрирован проезд по путевому листу",
            "Drive-through registered against a waybill",
        ),
        _d(
            8,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Введен неверный PIN-код",
            "Access denied: wrong PIN code",
        ),
        _d(
            9,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Контроллер не готов",
            "Access denied: controller not ready",
        ),
        _d(
            10,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Неизвестный код пропуска",
            "Access denied: unknown credential",
        ),
        _d(
            11,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Режим не позволяет проход",
            "Access denied: the access rule does not allow the pass",
        ),
        _d(
            12,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Нет допуска на точку доступа",
            "Access denied: no permission for this access point",
        ),
        _d(
            13,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Нет допуска в это время",
            "Access denied: no permission at this time",
        ),
        _d(
            14,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Повторный проход",
            "Access denied: anti-passback violation",
        ),
        _d(
            15,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Срок действия ключа истек",
            "Access denied: the credential has expired",
        ),
        _d(
            16,
            EventCategory.FIRE_ALARM,
            "Пожарная тревога! Произведена аварийная разблокировка",
            "Fire alarm: emergency unlock performed",
        ),
        _d(
            17,
            EventCategory.FIRE_ALARM,
            "Пожарная тревога завершена",
            "Fire alarm ended",
        ),
        _d(18, EventCategory.TAMPER, "Корпус контроллера открыт", "Enclosure opened"),
        _d(19, EventCategory.TAMPER, "Корпус контроллера закрыт", "Enclosure closed"),
        _d(
            20,
            EventCategory.LINK_LOST,
            "Связь с точкой доступа потеряна",
            "Link to the access point lost",
        ),
        _d(
            21,
            EventCategory.LINK_RESTORED,
            "Связь с точкой доступа восстановлена",
            "Link to the access point restored",
        ),
        _d(22, EventCategory.GATE, "Закрытие ворот", "Gate closing"),
        _d(23, EventCategory.GATE, "Открытие ворот", "Gate opening"),
        _d(24, EventCategory.ACCESS_GRANTED, "Доступ разрешен", "Access granted"),
        _d(
            25,
            EventCategory.DOOR_HELD_OPEN_START,
            "Удержание двери в открытом состоянии начато",
            "Door held open: started",
        ),
        _d(
            28,
            EventCategory.POWER_MAINS,
            "Переход на работу от сети (восстановление питания)",
            "Switched to mains power (power restored)",
        ),
        _d(
            29,
            EventCategory.POWER_BATTERY,
            "Переход на работу от аккумулятора (потеря сетевого питания)",
            "Switched to battery power (mains power lost)",
        ),
        _d(
            30,
            EventCategory.MODE_CHANGED,
            "Установка режима точки доступа «Нормальный»",
            "Access point mode set to normal",
        ),
        _d(
            31,
            EventCategory.MODE_CHANGED,
            "Установка режима точки доступа «Заблокировано»",
            "Access point mode set to locked",
        ),
        _d(
            32,
            EventCategory.MODE_CHANGED,
            "Установка режима точки доступа «Разблокировано»",
            "Access point mode set to unlocked",
        ),
        _d(
            36,
            EventCategory.DOOR_CLOSED,
            "Переход точки доступа в состояние «закрыта» (закрытие двери)",
            "Access point closed (door closed)",
        ),
        _d(
            37,
            EventCategory.DOOR_OPENED,
            "Переход точки доступа в состояние «открыта» (открытие двери)",
            "Access point opened (door opened)",
        ),
        _d(
            38,
            EventCategory.DOOR_HELD_OPEN_END,
            "Удержание двери в открытом состоянии закончено",
            "Door held open: ended",
        ),
        _d(
            39,
            EventCategory.WAITING,
            "Начало ожидания санкции охраны",
            "Waiting for a guard decision: started",
        ),
        _d(
            40,
            EventCategory.WAITING,
            "Окончание ожидания санкции охраны",
            "Waiting for a guard decision: ended",
        ),
        _d(41, EventCategory.OTHER, "Проход не совершён", "Pass was not completed"),
        _d(42, EventCategory.WAITING, "Ожидание сопровождающего", "Waiting for escort"),
        _d(
            43,
            EventCategory.WAITING,
            "Ожидание ввода PIN-кода",
            "Waiting for a PIN code",
        ),
        _d(
            44, EventCategory.WAITING, "Ожидание алкотеста", "Waiting for a breath test"
        ),
        _d(
            45,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Использован основной считыватель "
            "(ожидается дополнительный)",
            "Access denied: primary reader used, secondary expected",
        ),
        _d(
            46,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Использован дополнительный считыватель "
            "(ожидается основной)",
            "Access denied: secondary reader used, primary expected",
        ),
        _d(
            47,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Режимы пересеклись недопустимым способом",
            "Access denied: access rules overlap in a forbidden way",
        ),
        _d(
            48,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Точка доступа заблокирована",
            "Access denied: the access point is locked",
        ),
        _d(
            49,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Удерживается кнопка блокировки",
            "Access denied: the lock button is held down",
        ),
        _d(
            50,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Другая дверь шлюза сейчас открыта",
            "Access denied: the other mantrap door is open",
        ),
        _d(
            51,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Превышение числа лиц в зоне",
            "Access denied: too many people in the zone",
        ),
        _d(
            52,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Охранник отказал в доступе",
            "Access denied: the guard refused access",
        ),
        _d(
            53,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Недопустимое опьянение",
            "Access denied: unacceptable intoxication level",
        ),
        _d(
            54,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Контроллер не готов (код 17)",
            "Access denied: controller not ready (code 17)",
        ),
        _d(
            55,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Не дождались результата алкотестирования",
            "Access denied: breath test result not received in time",
        ),
        _d(
            56,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Не дождались сопровождающего",
            "Access denied: escort did not arrive in time",
        ),
        _d(
            57,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Не дождались санкции охраны",
            "Access denied: guard decision not received in time",
        ),
        _d(
            58,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Не дождались второго объекта",
            "Access denied: the second person did not arrive in time",
        ),
        _d(
            59,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Предыдущий проход не завершен (код 22)",
            "Access denied: the previous pass is not complete (code 22)",
        ),
        _d(
            60,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Предыдущий проход не завершен",
            "Access denied: the previous pass is not complete",
        ),
        _d(
            61,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Невозможно списать стоимость выбранной позиции",
            "Access denied: unable to charge the selected item",
        ),
        _d(
            62,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Не было распознавания гос. номера",
            "Access denied: no licence plate was recognised",
        ),
        _d(
            63,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Активно специальное ограничение",
            "Access denied: a special restriction is active",
        ),
        _d(
            64,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Есть несданные предметы",
            "Access denied: items have not been returned",
        ),
        _d(
            65,
            EventCategory.LOCK_FAULT,
            "Неисправность замка. Датчик Холла не активен, когда должен быть активен",
            "Lock fault: Hall sensor inactive when it should be active",
        ),
        _d(
            66,
            EventCategory.LOCK_FAULT,
            "Неисправность замка. Датчик Холла активен, когда должен быть не активен",
            "Lock fault: Hall sensor active when it should be inactive",
        ),
        _d(
            67,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Лицо не опознано",
            "Access denied: face not recognised",
        ),
        _d(68, EventCategory.FACE, "Лицо не опознано", "Face not recognised"),
        _d(
            69,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Попытка подбора кода (постановление №969)",
            "Access denied: code brute-force attempt (decree No. 969)",
        ),
        _d(
            70,
            EventCategory.WAITING,
            "Ждём сопровождающего, идентифицированный не может выступить в этой роли",
            "Waiting for escort: the identified person cannot act as one",
        ),
        _d(
            71,
            EventCategory.ACCESS_DENIED,
            "Доступ по гос. номеру запрещен согласно режиму",
            "Licence plate access denied by the access rule",
        ),
        _d(
            72,
            EventCategory.OTHER,
            "Не удалось получить ответ от внешней системы",
            "No reply received from the external system",
        ),
        _d(
            73,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен внешней системой",
            "Access denied by the external system",
        ),
        _d(74, EventCategory.FACE, "Лицо распознано", "Face recognised"),
        _d(75, EventCategory.WAITING, "Ожидание лица", "Waiting for a face"),
        _d(
            76,
            EventCategory.POWER_QUALITY,
            "Напряжение питания в норме",
            "Supply voltage is normal",
        ),
        _d(
            77,
            EventCategory.POWER_QUALITY,
            "Напряжение питания ниже нормы",
            "Supply voltage is below normal",
        ),
        _d(
            78,
            EventCategory.POWER_QUALITY,
            "Напряжение питания выше нормы",
            "Supply voltage is above normal",
        ),
        _d(
            79,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Нет связи с сервером",
            "Access denied: no connection to the server",
        ),
        _d(
            80,
            EventCategory.WAITING,
            "Ожидание измерения температуры",
            "Waiting for a temperature measurement",
        ),
        _d(
            81,
            EventCategory.TEMPERATURE,
            "Превышен порог предупреждения по температуре",
            "Temperature warning threshold exceeded",
        ),
        _d(
            82,
            EventCategory.TEMPERATURE,
            "Превышен порог тревоги по температуре",
            "Temperature alarm threshold exceeded",
        ),
        _d(
            83,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Проверка температуры не пройдена",
            "Access denied: temperature check failed",
        ),
        _d(
            84,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Верификация не пройдена",
            "Access denied: verification failed",
        ),
        _d(
            85,
            EventCategory.TEMPERATURE,
            "Температура в норме",
            "Temperature is normal",
        ),
        _d(
            86,
            EventCategory.OTHER,
            "Идентифицирован сопровождающий",
            "Escort identified",
        ),
        _d(
            87,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Отсутствует лицевая маска",
            "Access denied: face mask missing",
        ),
        _d(88, EventCategory.FACE, "Лицевая маска отсутствует", "Face mask is missing"),
        _d(
            89,
            EventCategory.FACE,
            "Успешная проверка лицевой маски",
            "Face mask check passed",
        ),
        _d(
            90,
            EventCategory.FACE,
            "Начата проверка наличия лицевой маски",
            "Face mask check started",
        ),
        _d(
            91,
            EventCategory.TEMPERATURE,
            "Результат измерения температуры отсутствует",
            "No temperature measurement result",
        ),
        _d(
            92,
            EventCategory.ACCESS_DENIED,
            "Доступ запрещен. Срок доступа не начался или уже закончился",
            "Access denied: the access period has not started or has ended",
        ),
        _d(
            93,
            EventCategory.WAITING,
            "Ожидание второго объекта для доступа",
            "Waiting for the second person for two-person access",
        ),
    )
)


@dataclass(frozen=True, slots=True)
class OpsRange:
    """One extended alarm-panel ("ОПС") event range."""

    base: int
    panel: str
    description_ru: str
    description_en: str
    sub_codes: dict[int, tuple[str, str]]
    """``N`` values documented for this panel, if any."""


#: ``256+N`` - "ОПС события «Sigur»".
_OPS_SIGUR_SUBCODES: Final[dict[int, tuple[str, str]]] = {
    0: ("Состояние зоны неизвестно", "Zone state unknown"),
    1: ("Зона снята с охраны", "Zone disarmed"),
    2: ("Зона взята на охрану", "Zone armed"),
    3: ("Тревога", "Alarm"),
}

#: ``768+N`` - "ОПС события «Рубеж», охранные".
_OPS_RUBEZH_SECURITY_SUBCODES: Final[dict[int, tuple[str, str]]] = {
    0: ("Не на охране", "Disarmed"),
    1: ("Тревога", "Alarm"),
    2: ("Задержка по входу/выходу", "Entry/exit delay"),
    4: ("Неудачная постановка (неисправность)", "Arming failed (fault)"),
    16: ("Потеря связи", "Link lost"),
    32: ("На охране", "Armed"),
    64: ("Неправильная конфигурация", "Invalid configuration"),
    255: ("Неизвестно", "Unknown"),
}

#: ``1024+N`` - "ОПС события «Рубеж», пожарные".
_OPS_RUBEZH_FIRE_SUBCODES: Final[dict[int, tuple[str, str]]] = {
    0: ("Дежурный режим", "Standby"),
    1: ("Пожар", "Fire"),
    2: ("Внимание", "Attention"),
    4: ("Неисправность", "Fault"),
    16: ("Обход", "Bypassed"),
    32: ("Потеря связи", "Link lost"),
    64: ("Неправильная конфигурация", "Invalid configuration"),
    255: ("Неизвестно", "Unknown"),
}

#: The four documented extended ranges, highest base first so that lookup can
#: simply take the first matching range.
OPS_RANGES: Final[tuple[OpsRange, ...]] = (
    OpsRange(
        OPS_RUBEZH_FIRE_BASE,
        "rubezh_fire",
        "ОПС события «Рубеж», пожарные",
        "Rubezh fire alarm panel event",
        _OPS_RUBEZH_FIRE_SUBCODES,
    ),
    OpsRange(
        OPS_RUBEZH_SECURITY_BASE,
        "rubezh_security",
        "ОПС события «Рубеж», охранные",
        "Rubezh security alarm panel event",
        _OPS_RUBEZH_SECURITY_SUBCODES,
    ),
    OpsRange(
        OPS_BOLID_BASE,
        "bolid",
        "ОПС события «Болид»",
        "Bolid alarm panel event",
        {},
    ),
    OpsRange(
        OPS_SIGUR_BASE,
        "sigur",
        "ОПС события «Sigur»",
        "Sigur alarm panel event",
        _OPS_SIGUR_SUBCODES,
    ),
)


@dataclass(frozen=True, slots=True)
class ResolvedEvent:
    """Result of looking a numeric event code up in the table."""

    code: int
    category: EventCategory
    event_type: str
    """Stable slug published on the Home Assistant event bus."""

    description_ru: str
    description_en: str
    ops_panel: str | None = None
    ops_sub_code: int | None = None

    @property
    def known(self) -> bool:
        """Whether the code is documented by the specification."""
        return self.category is not EventCategory.UNKNOWN


def _resolve_ops(code: int) -> ResolvedEvent | None:
    """Resolve a code inside one of the extended alarm-panel ranges."""
    for ops in OPS_RANGES:
        if code < ops.base or code >= ops.base + OPS_RANGE_SIZE:
            continue
        sub = code - ops.base
        sub_ru, sub_en = ops.sub_codes.get(sub, (f"N={sub}", f"N={sub}"))
        return ResolvedEvent(
            code=code,
            category=EventCategory.ALARM_PANEL,
            event_type=f"ops_{ops.panel}_{sub}",
            description_ru=f"{ops.description_ru}: {sub_ru}",
            description_en=f"{ops.description_en}: {sub_en}",
            ops_panel=ops.panel,
            ops_sub_code=sub,
        )
    return None


def resolve_event_code(code: int) -> ResolvedEvent:
    """Resolve an ``EVENT_CE`` numeric code into a typed description.

    An undocumented code never raises: it comes back as
    :attr:`EventCategory.UNKNOWN` with the numeric value preserved, so a new
    firmware release cannot break event handling.
    """
    definition = EVENT_CODES.get(code)
    if definition is not None:
        return ResolvedEvent(
            code=code,
            category=definition.category,
            event_type=definition.category.value,
            description_ru=definition.description_ru,
            description_en=definition.description_en,
        )
    if (ops := _resolve_ops(code)) is not None:
        return ops
    return ResolvedEvent(
        code=code,
        category=EventCategory.UNKNOWN,
        event_type=EventCategory.UNKNOWN.value,
        description_ru=f"Неизвестное событие (код {code})",
        description_en=f"Unknown event (code {code})",
    )


#: Categories a user can subscribe to from a device trigger or filter on in the
#: options flow. ``UNKNOWN`` is included so that new firmware events remain
#: reachable from automations.
TRIGGER_CATEGORIES: Final[tuple[EventCategory, ...]] = tuple(EventCategory)


#: Maps classic (non-``CE``) event keywords onto the same categories, so that
#: a server which only supports ``SUBSCRIBE``/``GETHISTORY`` produces events
#: indistinguishable from ``EVENT_CE`` ones for automation purposes.
CLASSIC_CATEGORIES: Final[dict[str, EventCategory]] = {
    "OBJECTPASS": EventCategory.PASS_REGISTERED,
    "FREEPASS": EventCategory.PASS_REGISTERED,
    "MANUALPASS": EventCategory.PASS_REGISTERED,
    "OPENDOOR": EventCategory.PASS_REGISTERED,
    "BREAKINGPASS": EventCategory.BREAK_IN,
    "DENY": EventCategory.ACCESS_DENIED,
}
