# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - unreleased

### Added

- A **Sigur** entry in the Home Assistant sidebar. It groups every configured
  server's access points by zone and shows link state, door position and lock
  mode on one screen, with a search box and a "problems only" filter for
  installations with a hundred points.
- Control straight from a tile - the lock mode and the one-shot entry and exit
  buttons - shown only while control is enabled for that server; otherwise the
  tile is read-only and says so.
- A live event feed beside the tiles, fed from the `sigur_event` bus event.
- A camera can be attached to an access point, from a gear on its tile
  (administrators only) or with the `sigur.set_access_point_camera` action for
  automations and bulk setup. Attaching one does not require the control
  option, because recording which camera watches a door opens nothing.
  - A Home Assistant `camera.*` entity is what puts a picture on the tile; the
    stream stays the responsibility of whichever integration provides it.
  - A raw RTSP URL can be stored alongside or instead, for automations and for
    a future camera platform. A browser cannot play RTSP directly, so on its
    own it displays nothing.
  - The binding follows its entity: renaming a bound camera rewrites it, and
    removing the camera clears it while keeping any RTSP URL.
- The camera frame refreshes the moment an event arrives for that access
  point, which is when it is worth looking at, and every 30 seconds for tiles
  that are on screen. Home Assistant only rotates the token inside
  `entity_picture` every five minutes, so the picture would otherwise be up to
  that stale; refreshing only visible tiles keeps a hundred access points from
  meaning a hundred JPEG fetches per tick.
- A pass direction per access point: both ways, entry only or exit only. OIF
  reports the zone on each side but never whether the point is one-way, so it
  is declared by the user rather than guessed from a name - a wrong guess on a
  control that opens a barrier is not a small thing. A one-way point only
  offers the button for its direction, and the withdrawn button is removed
  from the registry instead of lingering as an unavailable entity. The
  directionless button survives either way, for doors with a single reader.
  The mode is exposed as a `direction_mode` attribute for automations.
- `sigur/panel/data` and `sigur/panel/set_binding` websocket commands.
- A choice of which access points become devices at all. An **Access points**
  step in the options lists everything the server reports, including points
  already excluded, and an unselected point gets no device, no entities and no
  `GETAPINFO` - on a hundred point system that is the difference between a
  hundred commands per scan interval and a handful. Selecting all of them
  stores no filter, so "all" keeps meaning "follow the server" and a point
  added in Sigur next month still appears by itself. Deselecting a point
  deletes its device and everything on it; a point that merely dropped out of
  `GETAPLIST` is left alone, because that is what a temporary discovery
  failure looks like.
- An optional `cover` per pass direction, for voice assistants. A one-shot pass
  is a `button`, but Alice, Google and Siri all reach an access point through
  an *openable* device, and in Home Assistant that is a `cover`; opening one
  sends exactly the `ALLOWPASS` its button sends. Off by default - most
  installations do not need two controls for one action - and it follows the
  control option for the same reason the buttons do. The state is the real
  door position reported by OIF rather than a guess from the last command, and
  only opening is offered, because a Sigur access point closes on its own.

### Changed

- The integration now depends on `frontend` and `panel_custom`, which it needs
  in order to register the panel.
- The panel's cache-busting token is derived from the module's own contents. A
  hand-maintained constant is forgotten exactly when the panel changes, and
  the browser then keeps running the previous module.
- The panel's static route is registered once per Home Assistant run rather
  than per panel registration; reloading a config entry raised "route will
  never be executed" because an aiohttp route cannot be replaced.

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

[Unreleased]: https://github.com/gOsToFf/home-assistant-sigur/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gOsToFf/home-assistant-sigur/releases/tag/v0.1.0
