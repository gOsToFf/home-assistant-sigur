# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-08-27

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
- Russian and English translations.

[Unreleased]: https://github.com/gOsToFf/home-assistant-sigur/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gOsToFf/home-assistant-sigur/releases/tag/v0.1.0
