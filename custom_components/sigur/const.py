"""Constants for the Sigur OIF integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sigur"

#: Event fired on the Home Assistant bus for every Sigur event.
EVENT_SIGUR: Final = "sigur_event"

MANUFACTURER: Final = "Промавтоматика / Sigur"
HUB_MODEL: Final = "Sigur OIF server"
ACCESS_POINT_MODEL: Final = "Sigur access point"

# --- Config entry data keys -------------------------------------------------

CONF_TLS: Final = "tls"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_CA_BUNDLE: Final = "ca_bundle"
CONF_CLIENT_CERTIFICATE: Final = "client_certificate"
CONF_CLIENT_KEY: Final = "client_key"
CONF_CLIENT_KEY_PASSWORD: Final = "client_key_password"
CONF_OIF_VERSION: Final = "oif_version"

# --- Options keys -----------------------------------------------------------

OPT_SCAN_INTERVAL: Final = "scan_interval"
OPT_ENABLE_CONTROL: Final = "enable_control"
OPT_ENABLE_PERSONAL_DATA: Final = "enable_personal_data"
OPT_RESOLVE_OBJECT_NAMES: Final = "resolve_object_names"
OPT_ENABLE_BACKFILL: Final = "enable_backfill"
OPT_BACKFILL_HOURS: Final = "backfill_hours"
OPT_BACKFILL_ON_FIRST_START: Final = "backfill_on_first_start"
OPT_EVENT_CATEGORIES: Final = "event_categories"
OPT_DEBUG_RAW_EVENTS: Final = "debug_raw_events"
OPT_WEBHOOK_ENABLED: Final = "webhook_enabled"
OPT_WEBHOOK_URL: Final = "webhook_url"
OPT_WEBHOOK_SECRET: Final = "webhook_secret"
OPT_WEBHOOK_TIMEOUT: Final = "webhook_timeout"
OPT_WEBHOOK_CATEGORIES: Final = "webhook_categories"
OPT_WEBHOOK_ALLOW_INSECURE: Final = "webhook_allow_insecure"
OPT_WEBHOOK_INCLUDE_NAMES: Final = "webhook_include_names"

# --- Defaults ---------------------------------------------------------------

DEFAULT_NAME: Final = "Sigur"
DEFAULT_SCAN_INTERVAL: Final = 30
#: Polling faster than this puts avoidable load on the serialised OIF command
#: connection, which has no request pipelining.
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 3600

DEFAULT_BACKFILL_HOURS: Final = 1
MIN_BACKFILL_HOURS: Final = 1
MAX_BACKFILL_HOURS: Final = 24

DEFAULT_WEBHOOK_TIMEOUT: Final = 10
MAX_WEBHOOK_QUEUE: Final = 500
#: How long a resolved object name stays cached before it is fetched again.
OBJECT_NAME_TTL: Final = 900
#: Upper bound on the lazy object-name cache, to bound memory and PII exposure.
OBJECT_NAME_CACHE_SIZE: Final = 256
#: How many event fingerprints are remembered for backfill de-duplication.
DEDUP_WINDOW: Final = 2000

# --- Services ---------------------------------------------------------------

SERVICE_SET_ACCESS_POINT_MODE: Final = "set_access_point_mode"
SERVICE_ALLOW_PASS: Final = "allow_pass"
SERVICE_REFRESH: Final = "refresh"

ATTR_MODE: Final = "mode"
ATTR_DIRECTION: Final = "direction"
ATTR_OBJECT_ID: Final = "object_id"
ATTR_CONFIRM_ALL: Final = "confirm_all_access_points"

# --- Storage ----------------------------------------------------------------

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.state"

#: Per-access-point bindings (camera entity, RTSP URL) live in their own store,
#: because an installation with a hundred points would make an options form
#: unusable and these values change independently of the connection settings.
BINDINGS_STORAGE_VERSION: Final = 1
BINDINGS_STORAGE_KEY: Final = f"{DOMAIN}.bindings"

# --- Sidebar panel ----------------------------------------------------------

PANEL_URL_PATH: Final = "sigur"
PANEL_TITLE: Final = "Sigur"
PANEL_ICON: Final = "mdi:door-sliding"
#: Bumped whenever the panel module changes, to bust the browser cache.
PANEL_VERSION: Final = "0.2.0"

# --- Repairs ----------------------------------------------------------------

ISSUE_OIF_DISABLED: Final = "oif_access_disabled"
ISSUE_INVALID_CERTIFICATE: Final = "invalid_certificate"
ISSUE_UNSUPPORTED_VERSION: Final = "unsupported_version"
ISSUE_SERVER_UNAVAILABLE: Final = "server_unavailable"
#: How long the server must stay unreachable before a repair issue is raised.
UNAVAILABLE_ISSUE_AFTER: Final = 900
