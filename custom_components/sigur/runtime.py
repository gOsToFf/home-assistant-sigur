"""Per-config-entry runtime for the Sigur integration.

Each config entry owns two independent OIF connections to its server:

* a **command connection**, on which requests are serialised behind a lock
  because OIF messages carry no request identifier;
* an **event connection**, which does nothing but ``LOGIN`` + ``SUBSCRIBE`` and
  then reads pushed events forever.

Keeping them apart means a long ``GETHISTORY`` can never delay a pushed event,
and a burst of events can never be mistaken for a command reply.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import (
    ApMode,
    ApOpenState,
    ApState,
    BackoffController,
    Credentials,
    OifConnection,
    RawEvent,
    SigurApi,
    SigurAuthError,
    SigurConnectionError,
    SigurError,
    SigurPermissionError,
    SigurUnknownAccessPointError,
    SubscribeMode,
    TlsSettings,
    TransportSettings,
    ZoneInfo,
    create_ssl_context,
)
from .api.event_codes import EventCategory
from .const import (
    CONF_CA_BUNDLE,
    CONF_CLIENT_CERTIFICATE,
    CONF_CLIENT_KEY,
    CONF_CLIENT_KEY_PASSWORD,
    CONF_OIF_VERSION,
    CONF_TLS,
    CONF_VERIFY_SSL,
    DEDUP_WINDOW,
    DEFAULT_BACKFILL_HOURS,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEBHOOK_TIMEOUT,
    DOMAIN,
    EVENT_SIGUR,
    MAX_BACKFILL_HOURS,
    MIN_SCAN_INTERVAL,
    OBJECT_NAME_CACHE_SIZE,
    OBJECT_NAME_TTL,
    OPT_BACKFILL_HOURS,
    OPT_BACKFILL_ON_FIRST_START,
    OPT_DEBUG_RAW_EVENTS,
    OPT_ENABLE_BACKFILL,
    OPT_ENABLE_CONTROL,
    OPT_ENABLE_PERSONAL_DATA,
    OPT_EVENT_CATEGORIES,
    OPT_RESOLVE_OBJECT_NAMES,
    OPT_SCAN_INTERVAL,
    OPT_WEBHOOK_ALLOW_INSECURE,
    OPT_WEBHOOK_CATEGORIES,
    OPT_WEBHOOK_ENABLED,
    OPT_WEBHOOK_INCLUDE_NAMES,
    OPT_WEBHOOK_SECRET,
    OPT_WEBHOOK_TIMEOUT,
    OPT_WEBHOOK_URL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .models import AccessPointState, SigurEvent, normalize_event

if TYPE_CHECKING:
    from .coordinator import SigurDataUpdateCoordinator
    from .webhook import WebhookForwarder

_LOGGER = logging.getLogger(__name__)

#: Bounded backlog between the event reader and the event processor, so that a
#: burst of events can never grow without limit nor stall the socket reader.
_EVENT_QUEUE_SIZE = 1000

type SigurConfigEntry = ConfigEntry[SigurRuntimeData]


@dataclass(frozen=True, slots=True)
class SigurOptions:
    """The user-facing options of one config entry, with defaults applied."""

    scan_interval: int = DEFAULT_SCAN_INTERVAL
    enable_control: bool = False
    enable_personal_data: bool = False
    resolve_object_names: bool = False
    enable_backfill: bool = False
    backfill_hours: int = DEFAULT_BACKFILL_HOURS
    backfill_on_first_start: bool = False
    event_categories: tuple[str, ...] = ()
    """Categories published on the bus; empty means "everything"."""

    debug_raw_events: bool = False
    webhook_enabled: bool = False
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_timeout: int = DEFAULT_WEBHOOK_TIMEOUT
    webhook_categories: tuple[str, ...] = ()
    webhook_allow_insecure: bool = False
    webhook_include_names: bool = False

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> SigurOptions:
        """Read the options of ``entry``, clamping them to safe bounds."""
        options = entry.options
        return cls(
            scan_interval=max(
                MIN_SCAN_INTERVAL,
                int(options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            ),
            enable_control=bool(options.get(OPT_ENABLE_CONTROL, False)),
            enable_personal_data=bool(options.get(OPT_ENABLE_PERSONAL_DATA, False)),
            resolve_object_names=bool(options.get(OPT_RESOLVE_OBJECT_NAMES, False)),
            enable_backfill=bool(options.get(OPT_ENABLE_BACKFILL, False)),
            backfill_hours=min(
                MAX_BACKFILL_HOURS,
                max(1, int(options.get(OPT_BACKFILL_HOURS, DEFAULT_BACKFILL_HOURS))),
            ),
            backfill_on_first_start=bool(
                options.get(OPT_BACKFILL_ON_FIRST_START, False)
            ),
            event_categories=tuple(options.get(OPT_EVENT_CATEGORIES, ()) or ()),
            debug_raw_events=bool(options.get(OPT_DEBUG_RAW_EVENTS, False)),
            webhook_enabled=bool(options.get(OPT_WEBHOOK_ENABLED, False)),
            webhook_url=options.get(OPT_WEBHOOK_URL) or None,
            webhook_secret=options.get(OPT_WEBHOOK_SECRET) or None,
            webhook_timeout=int(
                options.get(OPT_WEBHOOK_TIMEOUT, DEFAULT_WEBHOOK_TIMEOUT)
            ),
            webhook_categories=tuple(options.get(OPT_WEBHOOK_CATEGORIES, ()) or ()),
            webhook_allow_insecure=bool(options.get(OPT_WEBHOOK_ALLOW_INSECURE, False)),
            webhook_include_names=bool(options.get(OPT_WEBHOOK_INCLUDE_NAMES, False)),
        )

    def publishes(self, category: EventCategory) -> bool:
        """Whether events of ``category`` go onto the Home Assistant bus."""
        return not self.event_categories or category.value in self.event_categories


def build_transport_settings(entry: ConfigEntry) -> TransportSettings:
    """Build the transport settings described by ``entry``."""
    data = entry.data
    return TransportSettings(
        host=data[CONF_HOST],
        port=int(data[CONF_PORT]),
        tls=TlsSettings(
            enabled=bool(data.get(CONF_TLS, False)),
            verify=bool(data.get(CONF_VERIFY_SSL, True)),
            ca_bundle=data.get(CONF_CA_BUNDLE) or None,
            client_certificate=data.get(CONF_CLIENT_CERTIFICATE) or None,
            client_key=data.get(CONF_CLIENT_KEY) or None,
            client_key_password=data.get(CONF_CLIENT_KEY_PASSWORD) or None,
        ),
    )


def build_credentials(entry: ConfigEntry) -> Credentials:
    """Build the OIF credentials described by ``entry``."""
    return Credentials(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        version=entry.data.get(CONF_OIF_VERSION, "1.8"),
    )


@dataclass(slots=True)
class _CachedName:
    """One entry of the lazy object-name cache."""

    name: str | None
    fetched_at: datetime


@dataclass(slots=True)
class SigurRuntimeData:
    """Everything one config entry owns while it is loaded."""

    hub: SigurHub
    coordinator: SigurDataUpdateCoordinator


class SigurHub:
    """Owns the connections, discovery, events and control for one server."""

    def __init__(self, hass: HomeAssistant, entry: SigurConfigEntry) -> None:
        """Prepare the hub; no I/O happens until :meth:`async_setup`."""
        self.hass = hass
        self.entry = entry
        self.options = SigurOptions.from_entry(entry)
        self.server_name: str = entry.data.get(CONF_NAME) or entry.title or DEFAULT_NAME
        self.settings = build_transport_settings(entry)
        self._credentials = build_credentials(entry)

        self.zones: dict[int, ZoneInfo] = {}
        self.access_points: dict[int, AccessPointState] = {}
        self.subscribe_mode: SubscribeMode | None = None
        self.last_event_at: datetime | None = None
        self.unavailable_since: datetime | None = None

        self._ssl_context: Any = None
        self._command: OifConnection | None = None
        self._api: SigurApi | None = None
        self._event_connection: OifConnection | None = None
        self._command_lock = asyncio.Lock()
        self._event_queue: asyncio.Queue[RawEvent] = asyncio.Queue(_EVENT_QUEUE_SIZE)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._shutdown = asyncio.Event()
        self.backoff = BackoffController()
        self._name_cache: OrderedDict[int, _CachedName] = OrderedDict()
        self._fingerprints: deque[tuple[Any, ...]] = deque(maxlen=DEDUP_WINDOW)
        self._fingerprint_set: set[tuple[Any, ...]] = set()
        self._listeners: list[Callable[[SigurEvent], None]] = []
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self._dropped_events = 0
        self._new_ap_listeners: list[Callable[[list[int]], None]] = []
        self.webhook: WebhookForwarder | None = None

    # --- Lifecycle ---------------------------------------------------------

    async def async_setup(self) -> None:
        """Connect, discover and start the event pipeline.

        Raises:
            ConfigEntryAuthFailed: if the operator credentials were rejected.
            ConfigEntryNotReady: if the server is temporarily unreachable.

        """
        stored = await self._store.async_load() or {}
        if (raw_last := stored.get("last_event_at")) is not None:
            self.last_event_at = dt_util.parse_datetime(raw_last)

        self._ssl_context = await self.hass.async_add_executor_job(
            create_ssl_context, self.settings.tls
        )
        await self._async_connect_command()
        await self._async_discover()

        self._start_task(self._event_worker(), "sigur-event-worker")
        self._start_task(self._event_supervisor(), "sigur-event-supervisor")

    async def async_shutdown(self) -> None:
        """Stop every task and close both connections."""
        self._shutdown.set()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.webhook is not None:
            await self.webhook.async_shutdown()
        for connection in (self._event_connection, self._command):
            if connection is not None:
                await connection.close()
        self._command = None
        self._api = None
        self._event_connection = None
        await self._async_persist_state()

    def _start_task(self, coro: Any, name: str) -> None:
        """Track a background task so unload can cancel it."""
        task = self.entry.async_create_background_task(self.hass, coro, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- Command connection ------------------------------------------------

    @property
    def api(self) -> SigurApi:
        """The command API.

        Raises:
            SigurConnectionError: if the command connection is not established.

        """
        if self._api is None:
            raise SigurConnectionError("the Sigur command connection is not ready")
        return self._api

    @property
    def command_connection(self) -> OifConnection | None:
        """The command connection, for diagnostics."""
        return self._command

    @property
    def event_connection(self) -> OifConnection | None:
        """The event connection, for diagnostics."""
        return self._event_connection

    async def _async_connect_command(self) -> None:
        """Open (or reopen) the command connection, mapping OIF failures."""
        async with self._command_lock:
            if self._command is not None and self._command.connected:
                return
            connection = OifConnection(
                self.settings,
                self._credentials,
                ssl_context=self._ssl_context,
                name=f"{self.server_name}/command",
            )
            try:
                await connection.connect()
            except SigurAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SigurPermissionError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SigurError as err:
                raise ConfigEntryNotReady(str(err)) from err
            if self._command is not None:
                connection.stats.reconnect_count = (
                    self._command.stats.reconnect_count + 1
                )
                await self._command.close()
            self._command = connection
            self._api = SigurApi(connection)

    async def async_ensure_command_connection(self) -> None:
        """Reconnect the command connection if it dropped."""
        if self._command is None or not self._command.connected:
            await self._async_connect_command()

    # --- Discovery and polling --------------------------------------------

    async def _async_discover(self) -> None:
        """Fetch zones and the access point list."""
        try:
            zones = await self.api.get_zones()
        except SigurPermissionError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SigurError as err:
            raise ConfigEntryNotReady(str(err)) from err
        self.zones = {zone.id: zone for zone in zones}
        await self._async_refresh_access_point_list()

    async def _async_refresh_access_point_list(self) -> list[int]:
        """Refresh the set of known access points.

        Returns:
            The ids that were not known before. Listeners are notified by
            :meth:`async_poll` only once those points have their ``GETAPINFO``
            data, so the devices they create carry the real name rather than a
            placeholder.

        """
        ap_ids = await self.api.get_access_point_ids()
        added = [ap_id for ap_id in ap_ids if ap_id not in self.access_points]
        for ap_id in added:
            self.access_points[ap_id] = AccessPointState(id=ap_id)
        # Access points that disappeared are marked unavailable rather than
        # deleted: a transient discovery blip must not destroy registry entries.
        for ap_id, state in self.access_points.items():
            if ap_id not in ap_ids:
                state.available = False
                state.last_error = "not returned by GETAPLIST"
        return added

    @callback
    def async_add_new_access_point_listener(
        self, listener: Callable[[list[int]], None]
    ) -> Callable[[], None]:
        """Register a platform hook that creates entities for new points.

        Returns:
            A callable that removes the listener again.

        """
        self._new_ap_listeners.append(listener)

        @callback
        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._new_ap_listeners.remove(listener)

        return _remove

    async def async_poll(self) -> dict[int, AccessPointState]:
        """Refresh every access point, tolerating a per-point failure."""
        await self.async_ensure_command_connection()
        added = await self._async_refresh_access_point_list()
        now = dt_util.utcnow()
        for ap_id, state in self.access_points.items():
            try:
                info = await self.api.get_access_point(ap_id)
            except SigurUnknownAccessPointError as err:
                state.available = False
                state.last_error = str(err)
                continue
            except SigurConnectionError:
                # The whole connection is gone: let the coordinator report it.
                raise
            except SigurError as err:
                state.available = False
                state.last_error = str(err)
                _LOGGER.debug(
                    "Sigur (%s): GETAPINFO %s failed: %s", self.server_name, ap_id, err
                )
                continue
            state.info = info
            state.available = True
            state.last_error = None
            state.last_updated = now
        self._clear_unavailable()
        if added:
            for listener in list(self._new_ap_listeners):
                listener(added)
        return self.access_points

    # --- Event pipeline ----------------------------------------------------

    @callback
    def async_add_event_listener(
        self, listener: Callable[[SigurEvent], None]
    ) -> Callable[[], None]:
        """Subscribe to normalized events; returns the unsubscribe callable."""
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return _remove

    @callback
    def _on_raw_event(self, raw: RawEvent) -> None:
        """Hand a freshly read event to the worker without blocking the reader."""
        try:
            self._event_queue.put_nowait(raw)
        except asyncio.QueueFull:
            self._dropped_events += 1
            _LOGGER.warning(
                "Sigur (%s): the event queue is full, dropping an event "
                "(%d dropped so far)",
                self.server_name,
                self._dropped_events,
            )

    async def _event_worker(self) -> None:
        """Process queued events in order, resolving names when asked to."""
        while True:
            raw = await self._event_queue.get()
            try:
                await self._async_process_event(raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "Sigur (%s): failed to process an event", self.server_name
                )
            finally:
                self._event_queue.task_done()

    async def _async_process_event(self, raw: RawEvent) -> None:
        """Normalize, de-duplicate and publish one event."""
        ap_state = (
            self.access_points.get(raw.access_point_id)
            if raw.access_point_id is not None
            else None
        )
        object_name = raw.object_name
        if (
            object_name is None
            and raw.object_id is not None
            and self.options.resolve_object_names
        ):
            object_name = await self._async_resolve_object_name(raw.object_id)

        event = normalize_event(
            RawEvent(
                occurred_at=raw.occurred_at,
                access_point_id=raw.access_point_id,
                object_id=raw.object_id,
                event_code=raw.event_code,
                classic_type=raw.classic_type,
                deny_reason=raw.deny_reason,
                direction=raw.direction,
                direction_code=raw.direction_code,
                object_name=object_name,
                key=raw.key,
                raw_message=raw.raw_message,
            ),
            entry_id=self.entry.entry_id,
            server_name=self.server_name,
            access_point_name=ap_state.name if ap_state else None,
            include_object_name=self.options.enable_personal_data,
        )

        if not self._remember(event):
            _LOGGER.debug(
                "Sigur (%s): dropping a duplicate event %s",
                self.server_name,
                event.event_type,
            )
            return

        if self.last_event_at is None or event.occurred_at > self.last_event_at:
            self.last_event_at = event.occurred_at

        self._apply_event_to_state(event)

        if ap_state is not None:
            ap_state.last_event = event

        if not self.options.publishes(event.category):
            return

        self.hass.bus.async_fire(
            EVENT_SIGUR,
            event.as_bus_payload(include_raw=self.options.debug_raw_events),
        )
        for listener in list(self._listeners):
            listener(event)
        if self.webhook is not None:
            self.webhook.enqueue(event)

    def _remember(self, event: SigurEvent) -> bool:
        """Record the event fingerprint; ``False`` if it was already seen."""
        fingerprint = event.fingerprint
        if fingerprint in self._fingerprint_set:
            return False
        if len(self._fingerprints) == self._fingerprints.maxlen:
            self._fingerprint_set.discard(self._fingerprints[0])
        self._fingerprints.append(fingerprint)
        self._fingerprint_set.add(fingerprint)
        return True

    @callback
    def _apply_event_to_state(self, event: SigurEvent) -> None:
        """Patch access point state so entities react without waiting for a poll."""
        if event.access_point_id is None:
            return
        state = self.access_points.get(event.access_point_id)
        if state is None or state.info is None:
            return

        changed = False
        if event.category is EventCategory.DOOR_OPENED:
            state.apply_open_state(ApOpenState.OPENED)
            changed = True
        elif event.category is EventCategory.DOOR_CLOSED:
            state.apply_open_state(ApOpenState.CLOSED)
            changed = True
        elif event.category is EventCategory.LINK_LOST:
            state.apply_state(ApState.OFFLINE)
            changed = True
        elif event.category is EventCategory.MODE_CHANGED:
            new_state = _MODE_EVENT_STATES.get(event.event_code or -1)
            if new_state is not None:
                state.apply_state(new_state)
                changed = True

        if changed:
            state.last_updated = dt_util.utcnow()

        if event.category is EventCategory.LINK_RESTORED:
            # The event says the link is back but not which mode it came back
            # in, so ask the server instead of guessing.
            self._start_task(self._async_refresh_one(state.id), "sigur-refresh-ap")
            return

        if changed:
            self._notify_coordinator()

    async def _async_refresh_one(self, ap_id: int) -> None:
        """Re-read one access point after an event that invalidated its state."""
        try:
            await self.async_ensure_command_connection()
            info = await self.api.get_access_point(ap_id)
        except SigurError as err:
            _LOGGER.debug(
                "Sigur (%s): refreshing AP %s failed: %s", self.server_name, ap_id, err
            )
            return
        state = self.access_points.get(ap_id)
        if state is None:
            return
        state.info = info
        state.available = True
        state.last_updated = dt_util.utcnow()
        self._notify_coordinator()

    @callback
    def _notify_coordinator(self) -> None:
        """Push the patched state into the coordinator, if one exists yet."""
        runtime = getattr(self.entry, "runtime_data", None)
        if runtime is None:
            return
        runtime.coordinator.async_set_updated_data(self.access_points)

    async def _async_resolve_object_name(self, object_id: int) -> str | None:
        """Resolve an object name lazily, with a bounded TTL cache."""
        now = dt_util.utcnow()
        cached = self._name_cache.get(object_id)
        if (
            cached is not None
            and (now - cached.fetched_at).total_seconds() < OBJECT_NAME_TTL
        ):
            self._name_cache.move_to_end(object_id)
            return cached.name
        try:
            await self.async_ensure_command_connection()
            info = await self.api.get_object(object_id)
        except SigurError as err:
            _LOGGER.debug(
                "Sigur (%s): resolving object %s failed: %s",
                self.server_name,
                object_id,
                err,
            )
            return cached.name if cached else None
        name = info.display_name if info else None
        self._name_cache[object_id] = _CachedName(name, now)
        self._name_cache.move_to_end(object_id)
        while len(self._name_cache) > OBJECT_NAME_CACHE_SIZE:
            self._name_cache.popitem(last=False)
        return name

    # --- Event connection supervisor --------------------------------------

    async def _event_supervisor(self) -> None:
        """Keep the event connection alive, re-subscribing after every drop."""
        while not self._shutdown.is_set():
            try:
                await self._async_open_event_connection()
            except SigurAuthError as err:
                _LOGGER.error(
                    "Sigur (%s): the event connection was rejected: %s",
                    self.server_name,
                    err,
                )
                self.entry.async_start_reauth(self.hass)
                return
            except SigurError as err:
                self._mark_unavailable(err)
                delay = self.backoff.next_delay()
                _LOGGER.debug(
                    "Sigur (%s): event connection failed (%s); retrying in %.0f s",
                    self.server_name,
                    err,
                    delay,
                )
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(delay):
                        await self._shutdown.wait()
                continue

            self.backoff.reset()
            self._clear_unavailable()
            await self._async_backfill()
            await self._async_wait_for_event_connection_loss()

    async def _async_open_event_connection(self) -> None:
        """Open a dedicated connection and subscribe to real-time events."""
        previous = self._event_connection
        connection = OifConnection(
            self.settings,
            self._credentials,
            ssl_context=self._ssl_context,
            event_callback=self._on_raw_event,
            name=f"{self.server_name}/events",
        )
        await connection.connect()
        self.subscribe_mode = await connection.subscribe()
        if previous is not None:
            connection.stats.reconnect_count = previous.stats.reconnect_count + 1
            await previous.close()
        self._event_connection = connection
        _LOGGER.debug(
            "Sigur (%s): subscribed to events using %s",
            self.server_name,
            self.subscribe_mode,
        )

    async def _async_wait_for_event_connection_loss(self) -> None:
        """Block until the event connection drops or the entry unloads."""
        connection = self._event_connection
        if connection is None:
            return
        waiters = [
            asyncio.ensure_future(self._shutdown.wait()),
            asyncio.ensure_future(connection.wait_disconnected()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            for waiter in waiters:
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

    # --- History backfill --------------------------------------------------

    async def _async_backfill(self) -> None:
        """Replay events missed while the subscription was down.

        The subscription is established *first* and this runs afterwards on the
        command connection, so no event can slip through the gap between the two:
        anything that arrives twice is removed by the fingerprint de-duplication.
        """
        options = self.options
        if not options.enable_backfill:
            return
        if self.last_event_at is None and not options.backfill_on_first_start:
            # A first start would otherwise import history the user never asked
            # for, filling the recorder with old events.
            self.last_event_at = dt_util.now()
            return

        now = dt_util.now()
        window_start = now - timedelta(hours=options.backfill_hours)
        start = max(self.last_event_at or window_start, window_start)
        if start >= now:
            return
        try:
            await self.async_ensure_command_connection()
            events = await self.api.get_history(
                start.replace(tzinfo=None), now.replace(tzinfo=None)
            )
        except SigurError as err:
            _LOGGER.warning(
                "Sigur (%s): the history backfill failed: %s", self.server_name, err
            )
            return
        _LOGGER.debug(
            "Sigur (%s): backfilling %d events since %s",
            self.server_name,
            len(events),
            start.isoformat(),
        )
        for raw in events:
            self._on_raw_event(raw)
        await self._async_persist_state()

    async def _async_persist_state(self) -> None:
        """Persist the last processed event timestamp for the next start."""
        await self._store.async_save(
            {
                "last_event_at": (
                    self.last_event_at.isoformat() if self.last_event_at else None
                )
            }
        )

    # --- Availability and repairs -----------------------------------------

    @callback
    def _mark_unavailable(self, err: Exception) -> None:
        """Remember when the server first became unreachable."""
        if self.unavailable_since is None:
            self.unavailable_since = dt_util.utcnow()
        from .repairs import async_check_connection_issue

        async_check_connection_issue(self.hass, self.entry, err, self.unavailable_since)

    @callback
    def _clear_unavailable(self) -> None:
        """Clear the unavailability marker and any repair issue it raised."""
        if self.unavailable_since is None:
            return
        self.unavailable_since = None
        from .repairs import async_clear_connection_issues

        async_clear_connection_issues(self.hass, self.entry)

    # --- Control -----------------------------------------------------------

    def assert_control_enabled(self) -> None:
        """Fail unless the user opted into write access for this entry.

        Raises:
            ServiceValidationError: when control is disabled, which is the
                default. It is a validation error rather than a plain failure
                because the fix is a setting the user can change, and it is
                raised with a translation key so the message follows the
                Home Assistant language.

        """
        if not self.options.enable_control:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="control_disabled",
                translation_placeholders={"name": self.server_name},
            )

    async def async_set_mode(self, ap_ids: Iterable[int], mode: ApMode) -> None:
        """Apply ``mode`` to ``ap_ids`` and refresh them optimistically."""
        self.assert_control_enabled()
        ids = list(ap_ids)
        unknown = [ap_id for ap_id in ids if ap_id not in self.access_points]
        if unknown:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_access_point",
                translation_placeholders={
                    "ap_id": ", ".join(str(ap_id) for ap_id in unknown)
                },
            )
        await self.async_ensure_command_connection()
        await self.api.set_access_point_mode(mode, ids)
        for ap_id in ids:
            state = self.access_points[ap_id]
            if state.info is not None and state.info.online:
                state.apply_state(_MODE_TO_STATE[mode])
                state.last_updated = dt_util.utcnow()
        self._notify_coordinator()

    async def async_allow_pass(
        self, ap_id: int, obj: int | str, direction: Any
    ) -> None:
        """Authorise a single pass through ``ap_id``."""
        self.assert_control_enabled()
        if ap_id not in self.access_points:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_access_point",
                translation_placeholders={"ap_id": str(ap_id)},
            )
        await self.async_ensure_command_connection()
        await self.api.allow_pass(ap_id, obj, direction)

    # --- Diagnostics -------------------------------------------------------

    @property
    def dropped_event_count(self) -> int:
        """How many events were dropped because the queue was full."""
        return self._dropped_events

    @property
    def event_queue_size(self) -> int:
        """Current depth of the event backlog."""
        return self._event_queue.qsize()

    def zone_name(self, zone_id: int) -> str | None:
        """Human readable name of ``zone_id``, if it is known."""
        zone = self.zones.get(zone_id)
        return zone.name if zone else None


#: ``EVENT_CE`` mode-change codes and the state they imply.
_MODE_EVENT_STATES: dict[int, ApState] = {
    30: ApState.ONLINE_NORMAL,
    31: ApState.ONLINE_LOCKED,
    32: ApState.ONLINE_UNLOCKED,
}

_MODE_TO_STATE: dict[ApMode, ApState] = {
    ApMode.NORMAL: ApState.ONLINE_NORMAL,
    ApMode.LOCKED: ApState.ONLINE_LOCKED,
    ApMode.UNLOCKED: ApState.ONLINE_UNLOCKED,
}
