"""Config and options flow for the Sigur OIF integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .api import (
    DEFAULT_OIF_VERSION,
    DEFAULT_PORT,
    AccessPointInfo,
    Credentials,
    OifConnection,
    SigurApi,
    SigurAuthError,
    SigurConnectionError,
    SigurError,
    SigurPermissionError,
    SigurTimeoutError,
    SigurTlsError,
    SigurUnsupportedVersionError,
    TlsSettings,
    TransportSettings,
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
    DEFAULT_BACKFILL_HOURS,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEBHOOK_TIMEOUT,
    DOMAIN,
    MAX_BACKFILL_HOURS,
    MAX_SCAN_INTERVAL,
    MIN_BACKFILL_HOURS,
    MIN_SCAN_INTERVAL,
    OPT_ACCESS_POINTS,
    OPT_BACKFILL_HOURS,
    OPT_BACKFILL_ON_FIRST_START,
    OPT_DEBUG_RAW_EVENTS,
    OPT_ENABLE_BACKFILL,
    OPT_ENABLE_CONTROL,
    OPT_ENABLE_PASS_COVERS,
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
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_OIF_VERSION, default=DEFAULT_OIF_VERSION): str,
        vol.Required(CONF_TLS, default=False): bool,
        vol.Required(CONF_VERIFY_SSL, default=True): bool,
        vol.Optional(CONF_CA_BUNDLE): str,
        vol.Optional(CONF_CLIENT_CERTIFICATE): str,
        vol.Optional(CONF_CLIENT_KEY): str,
        vol.Optional(CONF_CLIENT_KEY_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    """Coerce the raw form values into their stored representation."""
    data = dict(user_input)
    data[CONF_PORT] = int(data[CONF_PORT])
    data[CONF_HOST] = str(data[CONF_HOST]).strip()
    for optional in (
        CONF_CA_BUNDLE,
        CONF_CLIENT_CERTIFICATE,
        CONF_CLIENT_KEY,
        CONF_CLIENT_KEY_PASSWORD,
    ):
        if not data.get(optional):
            data.pop(optional, None)
    return data


def _tls_settings(data: Mapping[str, Any]) -> TlsSettings:
    """Build TLS settings from raw config entry data."""
    return TlsSettings(
        enabled=bool(data.get(CONF_TLS, False)),
        verify=bool(data.get(CONF_VERIFY_SSL, True)),
        ca_bundle=data.get(CONF_CA_BUNDLE) or None,
        client_certificate=data.get(CONF_CLIENT_CERTIFICATE) or None,
        client_key=data.get(CONF_CLIENT_KEY) or None,
        client_key_password=data.get(CONF_CLIENT_KEY_PASSWORD) or None,
    )


class SigurConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding, re-authenticating and reconfiguring a Sigur server."""

    VERSION = 1

    async def _async_validate(self, data: Mapping[str, Any]) -> str | None:
        """Connect, log in and run a read-only probe.

        Returns:
            ``None`` on success, otherwise the error key for the form.

        """
        settings = TransportSettings(
            host=data[CONF_HOST], port=int(data[CONF_PORT]), tls=_tls_settings(data)
        )
        credentials = Credentials(
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            version=data.get(CONF_OIF_VERSION, DEFAULT_OIF_VERSION),
        )
        try:
            ssl_context = await self.hass.async_add_executor_job(
                create_ssl_context, settings.tls
            )
        except SigurTlsError:
            return "invalid_certificate"

        connection = OifConnection(
            settings, credentials, ssl_context=ssl_context, name="sigur/config-flow"
        )
        try:
            await connection.connect()
            # A read-only probe: it proves the operator really has the
            # "Доступ по протоколу OIF (Интеграции)" right, not just a password.
            await SigurApi(connection).get_zones()
        except SigurTlsError:
            return "invalid_certificate"
        except SigurUnsupportedVersionError:
            return "unsupported_version"
        except SigurPermissionError as err:
            return "oif_disabled" if err.code == 21 else "permission_denied"
        except SigurAuthError:
            return "invalid_auth"
        except (SigurConnectionError, SigurTimeoutError):
            return "cannot_connect"
        except SigurError:
            _LOGGER.exception("Unexpected Sigur error while validating the connection")
            return "unknown"
        finally:
            await connection.close()
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial configuration form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            await self.async_set_unique_id(
                f"{data[CONF_HOST].lower()}:{data[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            error = await self._async_validate(data)
            if error is None:
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for fresh credentials and validate them."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            error = await self._async_validate(data)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or TLS settings of an existing server.

        The unique id is re-derived from the new host and port, so the entity
        and device registries survive an address change untouched.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize({**entry.data, **user_input})
            unique_id = f"{data[CONF_HOST].lower()}:{data[CONF_PORT]}"
            if any(
                other.entry_id != entry.entry_id and other.unique_id == unique_id
                for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            error = await self._async_validate(data)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data=data, unique_id=unique_id
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA, {**entry.data, **(user_input or {})}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SigurOptionsFlow:
        """Return the options flow for this integration."""
        return SigurOptionsFlow()


def _category_selector() -> selector.SelectSelector:
    """Multi-select over the event categories a Sigur system can report."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[category.value for category in EventCategory],
            multiple=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="event_category",
        )
    )


class SigurOptionsFlow(OptionsFlow):
    """Lets the user tune polling, control, privacy, backfill and webhooks."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "access_points", "events", "webhook"],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Polling interval, control and personal data."""
        if user_input is not None:
            return self._save(
                {**user_input, OPT_SCAN_INTERVAL: int(user_input[OPT_SCAN_INTERVAL])}
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_SCAN_INTERVAL,
                    default=options.get(OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    OPT_ENABLE_CONTROL,
                    default=options.get(OPT_ENABLE_CONTROL, False),
                ): bool,
                vol.Required(
                    OPT_ENABLE_PASS_COVERS,
                    default=options.get(OPT_ENABLE_PASS_COVERS, False),
                ): bool,
                vol.Required(
                    OPT_ENABLE_PERSONAL_DATA,
                    default=options.get(OPT_ENABLE_PERSONAL_DATA, False),
                ): bool,
                vol.Required(
                    OPT_RESOLVE_OBJECT_NAMES,
                    default=options.get(OPT_RESOLVE_OBJECT_NAMES, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="general", data_schema=schema)

    async def async_step_access_points(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which access points get devices and entities.

        A Sigur system with a hundred doors would otherwise put a hundred
        devices into Home Assistant whether or not the user cares about them,
        and poll every one of them forever.
        """
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return self.async_abort(reason="entry_not_loaded")

        try:
            points = await runtime.hub.async_list_all_access_points()
        except SigurError:
            _LOGGER.exception("Could not list the Sigur access points")
            return self.async_abort(reason="cannot_list_access_points")

        available = {point.id for point in points}
        if user_input is not None:
            chosen = {int(value) for value in user_input.get(OPT_ACCESS_POINTS, [])}
            if not chosen:
                return self._access_points_form(
                    points, user_input, {OPT_ACCESS_POINTS: "no_access_points"}
                )
            # Picking every point means "follow the server", so an access point
            # added in Sigur next month still shows up on its own. Storing the
            # full list instead would silently freeze the set as it is today.
            stored = [] if chosen == available else sorted(str(i) for i in chosen)
            return self._save({OPT_ACCESS_POINTS: stored})

        selected = self.config_entry.options.get(OPT_ACCESS_POINTS) or [
            str(ap_id) for ap_id in sorted(available)
        ]
        return self._access_points_form(points, {OPT_ACCESS_POINTS: selected}, {})

    @callback
    def _access_points_form(
        self,
        points: list[AccessPointInfo],
        values: Mapping[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult:
        """Render the access point picker over ``points``."""
        options = [
            selector.SelectOptionDict(
                value=str(point.id), label=f"{point.name} (#{point.id})"
            )
            for point in sorted(points, key=lambda point: (point.name, point.id))
        ]
        schema = vol.Schema(
            {
                vol.Required(OPT_ACCESS_POINTS): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="access_points",
            data_schema=self.add_suggested_values_to_schema(schema, values),
            errors=errors,
            description_placeholders={"count": str(len(points))},
        )

    async def async_step_events(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which events are published, and how history is recovered."""
        if user_input is not None:
            return self._save(
                {**user_input, OPT_BACKFILL_HOURS: int(user_input[OPT_BACKFILL_HOURS])}
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_EVENT_CATEGORIES,
                    default=list(options.get(OPT_EVENT_CATEGORIES, [])),
                ): _category_selector(),
                vol.Required(
                    OPT_ENABLE_BACKFILL,
                    default=options.get(OPT_ENABLE_BACKFILL, False),
                ): bool,
                vol.Required(
                    OPT_BACKFILL_HOURS,
                    default=options.get(OPT_BACKFILL_HOURS, DEFAULT_BACKFILL_HOURS),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_BACKFILL_HOURS,
                        max=MAX_BACKFILL_HOURS,
                        step=1,
                        unit_of_measurement="h",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    OPT_BACKFILL_ON_FIRST_START,
                    default=options.get(OPT_BACKFILL_ON_FIRST_START, False),
                ): bool,
                vol.Required(
                    OPT_DEBUG_RAW_EVENTS,
                    default=options.get(OPT_DEBUG_RAW_EVENTS, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="events", data_schema=schema)

    async def async_step_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional outbound webhook, disabled by default."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = (user_input.get(OPT_WEBHOOK_URL) or "").strip()
            enabled = bool(user_input.get(OPT_WEBHOOK_ENABLED))
            allow_insecure = bool(user_input.get(OPT_WEBHOOK_ALLOW_INSECURE))
            if enabled and not url:
                errors[OPT_WEBHOOK_URL] = "webhook_url_required"
            elif enabled and not url.startswith("https://") and not allow_insecure:
                errors[OPT_WEBHOOK_URL] = "webhook_https_required"
            elif enabled and not (user_input.get(OPT_WEBHOOK_SECRET) or "").strip():
                errors[OPT_WEBHOOK_SECRET] = "webhook_secret_required"
            if not errors:
                return self._save(
                    {
                        **user_input,
                        OPT_WEBHOOK_URL: url,
                        OPT_WEBHOOK_TIMEOUT: int(user_input[OPT_WEBHOOK_TIMEOUT]),
                    }
                )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_WEBHOOK_ENABLED,
                    default=options.get(OPT_WEBHOOK_ENABLED, False),
                ): bool,
                vol.Optional(
                    OPT_WEBHOOK_URL, default=options.get(OPT_WEBHOOK_URL, "")
                ): str,
                vol.Optional(
                    OPT_WEBHOOK_SECRET, default=options.get(OPT_WEBHOOK_SECRET, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    OPT_WEBHOOK_TIMEOUT,
                    default=options.get(OPT_WEBHOOK_TIMEOUT, DEFAULT_WEBHOOK_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=120, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    OPT_WEBHOOK_CATEGORIES,
                    default=list(options.get(OPT_WEBHOOK_CATEGORIES, [])),
                ): _category_selector(),
                vol.Required(
                    OPT_WEBHOOK_INCLUDE_NAMES,
                    default=options.get(OPT_WEBHOOK_INCLUDE_NAMES, False),
                ): bool,
                vol.Required(
                    OPT_WEBHOOK_ALLOW_INSECURE,
                    default=options.get(OPT_WEBHOOK_ALLOW_INSECURE, False),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="webhook", data_schema=schema, errors=errors
        )

    @callback
    def _save(self, updates: dict[str, Any]) -> ConfigFlowResult:
        """Merge ``updates`` into the stored options and finish the flow."""
        return self.async_create_entry(data={**self.config_entry.options, **updates})
