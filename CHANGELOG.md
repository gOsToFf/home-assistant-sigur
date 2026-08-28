# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.2.0b2] - 2026-08-28

### Added

- A way to actually attach a camera. `0.2.0b1` shipped the storage and the
  websocket command but no interface, which left the feature unreachable
  without a browser console.
  - In the panel, a gear on each access point tile opens an inline editor with
    a camera entity field, completed from the cameras Home Assistant already
    has, and an RTSP field. Administrators only.
  - `sigur.set_access_point_camera` does the same from an automation or in
    bulk. It is not behind the control option, because recording which camera
    watches a door opens nothing; calling it with neither field clears the
    binding.

### Fixed

- The panel failed to load after its cache-busting version changed. The module
  can be evaluated twice in one page session, and the second
  `customElements.define` threw, leaving the sidebar entry blank until a hard
  refresh.

## [0.2.0b1] - 2026-08-28

First beta of the sidebar panel. Betas are only offered to users who turn on
the per-repository **Pre-release** switch in HACS.

### Added

- A **Sigur** entry in the Home Assistant sidebar. It groups every configured
  server's access points by zone and shows link state, door position and lock
  mode on one screen, with a search box and a "problems only" filter for
  installations with a hundred points.
- Control straight from a tile - the lock mode and the one-shot entry and exit
  buttons - shown only while control is enabled for that server; otherwise the
  tile is read-only and says so.
- A live event feed beside the tiles, fed from the `sigur_event` bus event.
- Per-access-point bindings: a Home Assistant camera entity and/or a raw RTSP
  URL can be attached to an access point and are persisted per config entry.
  A bound camera's snapshot appears on the tile. The RTSP URL is stored for
  automations and for a future camera platform; a browser cannot play RTSP
  directly, so it renders nothing on its own yet.
- `sigur/panel/data` and `sigur/panel/set_binding` websocket commands. Setting
  a binding requires an administrator.

### Changed

- The integration now depends on `frontend` and `panel_custom`, which it needs
  to register the panel.

## [0.1.0] - 2026-08-28

First release. Implements the Sigur OIF integration protocol, rev. 27.01.2025
(OIF 1.8, Sigur 1.6.3.14).

### Added

- Async OIF client with TLS, custom CA and mutual TLS support, a safe
  tokenizer for quoted strings and `#NN` escapes, and typed exceptions for all
  29 documented error codes.
- Two connections per Sigur server: a serialised command connection and a
  dedicated event connection, so a long request can never delay or corrupt a
  pushed event.
- Config flow with connection validation, duplicate protection, reauth and
  reconfigure; several Sigur servers can run side by side.
- Automatic discovery of zones and access points; one Home Assistant device per
  access point, linked to the server device.
- `binary_sensor` for link state and door position, `select` for the
  three-position lock mode, `event` for the last event, and diagnostic sensors.
- Real-time events through `SUBSCRIBE CE_WITH_NAMES` with automatic fallback to
  `CE` and to the classic format, published as `sigur_event` with device
  triggers for every event category.
- The complete `EVENT_CE` code table (0-93 plus the `256+N`, `512+N`, `768+N`
  and `1024+N` alarm-panel ranges); unknown codes are published rather than
  dropped.
- Bounded, opt-in `GETHISTORY` backfill after a reconnect, with persistent
  last-event metadata and fingerprint de-duplication.
- Automatic reconnection with exponential backoff and jitter.
- `sigur.set_access_point_mode`, `sigur.allow_pass` and `sigur.refresh`, all
  gated behind an opt-in control option.
- Optional outbound webhook with HMAC-SHA256 signing, a nonce, a bounded queue
  and retries; disabled by default.
- Diagnostics with full redaction of credentials, names and credential numbers,
  and repair issues for persistent problems.
- One-shot pass buttons on every access point: "Allow entry" and "Allow exit"
  send a single `ALLOWPASS` without changing the access point's mode. A third,
  directionless button is available but disabled by default. The buttons only
  exist while control is enabled, so a button that would always refuse is never
  shown.
- Access object ids and names are withheld from entities, bus events,
  diagnostics and the webhook unless the personal-data option is enabled, and
  credential numbers are always masked.
- Russian and English translations, with error messages that follow the Home
  Assistant language.

[Unreleased]: https://github.com/gOsToFf/home-assistant-sigur/compare/v0.2.0b2...HEAD
[0.2.0b2]: https://github.com/gOsToFf/home-assistant-sigur/releases/tag/v0.2.0b2
[0.2.0b1]: https://github.com/gOsToFf/home-assistant-sigur/releases/tag/v0.2.0b1
[0.1.0]: https://github.com/gOsToFf/home-assistant-sigur/releases/tag/v0.1.0
