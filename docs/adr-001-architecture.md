# ADR 001 — Architecture of the Sigur OIF integration

Status: accepted · Date: 2026-08-27

Source of truth: **«Протокол интеграции OIF», редакция от 27.01.2025** (OIF 1.8,
Sigur 1.6.3.14).

## Context

OIF is a line-oriented text protocol over TCP. Every message is one `\r\n`
terminated UTF-8 line. There is **no request identifier** anywhere in the
protocol. The server answers only when asked — with one exception: after a
successful `SUBSCRIBE` it also pushes `EVENT` / `EVENT_CE` lines asynchronously
on the same connection.

That single exception is what shapes the whole design: a naive
`send(); readline()` client will eventually read an event where it expected a
reply, and mis-parse both.

## Decisions

### 1. Two TCP connections per config entry

Each config entry opens two independent OIF sessions to its server:

| Connection | Purpose | Concurrency |
|---|---|---|
| **Command** | `GETZONEINFO`, `GETAPLIST`, `GETAPINFO`, `GETHISTORY`, `GETOBJECTINFO`, `SETAPMODE`, `ALLOWPASS` | serialised behind an `asyncio.Lock`, one command in flight |
| **Event** | `LOGIN` + `SUBSCRIBE CE_WITH_NAMES`, then read forever | never sends anything else |

Rationale:

* a long `GETHISTORY` on the command connection cannot delay a pushed event;
* the event connection never has a pending reply, so every line it reads is
  unambiguously an event;
* the command connection still routes any stray `EVENT*` line to the event
  dispatcher rather than treating it as a reply, so a server that pushes events
  on both connections cannot corrupt a response.

Both connections still run one shared reader task each, which classifies each
inbound line before it reaches the command queue. Replies left over from a
timed-out command are drained and counted (`unsolicited_line_count`) instead of
being handed to the next command.

### 2. Unique IDs are scoped by config entry

Two Sigur servers routinely use the same access point numbers. Identifiers are
therefore:

* config entry unique id — `"<host>:<port>"` (prevents adding the same server
  twice, allows any number of different ones);
* hub device — `(sigur, <entry_id>)`;
* access point device — `(sigur, "<entry_id>_ap_<ap_id>")`;
* entity unique id — `"<entry_id>_<ap_id>_<key>"`.

Reconfiguring host/port re-derives the entry unique id but keeps the entry, so
the device and entity registries survive an address change untouched.

### 3. `select`, not `lock`, is the canonical mode entity

`SETAPMODE` has three positions — `NORMAL`, `LOCKED`, `UNLOCKED` — which do not
collapse into a two-state lock without losing information. `NORMAL` means "the
controller decides according to its access rules", which is neither locked nor
unlocked. A `select` entity is therefore canonical; no `lock` entity is
provided.

### 4. Event codes live in one typed table

Appendix 6.2 of the specification (codes 0–93 plus the `256+N`, `512+N`,
`768+N`, `1024+N` alarm-panel ranges) is transcribed verbatim into
`api/event_codes.py`. Codes the specification marks `(не используется)` are
deliberately **absent** so they resolve as unknown rather than being invented.

Each code additionally carries a coarse `EventCategory` — an integration-level
concept, not part of the protocol — so automations and device triggers
subscribe to "access denied" instead of enumerating twenty numeric codes.

An undocumented code never raises: it is published with `category: unknown`,
its numeric value preserved.

### 5. De-duplication keys on the category, not the event code

`GETHISTORY` **only ever answers in the classic format**, which has no numeric
event code at all; `SUBSCRIBE CE` delivers numeric `EVENT_CE` codes. The same
physical event therefore arrives in two different shapes. Matching on the code
would mean a backfilled event never de-duplicates against its live twin.

The fingerprint is `(occurred_at, category, access_point_id, object_id,
direction, key_masked)` — the fields both representations carry identically,
plus the coarse category that both map onto. The object name and the raw
payload are excluded for the same reason.

### 6. Backfill is bounded, opt-in, and runs after the subscription

Order of operations after a reconnect:

1. `LOGIN` + `SUBSCRIBE` on the event connection — the live stream is restored
   *first*, so the gap can only ever shrink;
2. `GETHISTORY` over a bounded window on the command connection;
3. both streams flow through the same fingerprint filter.

The window is `max(last_processed_event, now - backfill_hours)`, clamped to
1–24 hours. `last_event_at` is persisted per entry through
`homeassistant.helpers.storage.Store`. On a first start nothing is imported
unless the user opts in a second time, so a fresh install does not fill the
recorder with old history. The integration keeps **no** long-term event
database of its own — that is the recorder's job.

### 7. Everything that writes, and everything personal, is off by default

* `enable_control` gates `SETAPMODE` and `ALLOWPASS`, the mode `select` and
  both actions. Default: off.
* `enable_personal_data` gates names and object ids in entities, events and
  diagnostics. Default: off.
* `resolve_object_names` gates lazy `GETOBJECTINFO OBJECTID` lookups behind a
  TTL + LRU cache. Default: off. `GETOBJECTINFO ALL` is never called.
* Credential numbers are masked at the parser boundary (`W26 ***23`) and the
  full number never leaves `CredentialKey.raw`, which only outbound commands
  use.
* `SETAPMODE ALL` is unreachable from the public action: ids are always listed
  explicitly, and claiming "all" requires both targeting every point and
  setting `confirm_all_access_points`.

### 8. Advanced OIF commands are deliberately out of scope

`SYNCDB3`, `FACE_SYNC`, `BS_SYNC`, `DEVCONF_*`, `IP_SETCONF`, `DELEGATION_*`,
`LPREVENT` and `EXTFACEDETECT` are dangerous, long-running or highly
specialised. They are not exposed. The transport already supports multi-line
streamed replies (`OifConnection.execute_stream`), so adding one later means
adding a method to `api/commands.py` — no protocol rework.

`IP_DISCOVER` is a plausible second-wave diagnostic action; note that the
search is performed by the Sigur server as a broadcast inside its own subnet
and does not apply to E510, E2 or E4.

### 9. The event bus is the primary outbound interface

`sigur_event` on the Home Assistant bus, plus device triggers, plus actions.
A user forwards events outward with a normal automation and `rest_command` or
MQTT. The built-in signed outbound webhook exists for deployments that need a
direct feed; it is off by default, requires HTTPS (unless the destination is a
private address and the user confirms), signs every delivery with HMAC-SHA256
over `timestamp.nonce.body`, retries a bounded number of times, and drops the
oldest events rather than growing its queue. No inbound webhook is implemented:
the authenticated Home Assistant API and actions already cover that, without a
second credential to protect.

### 10. The protocol layer imports nothing from Home Assistant

`custom_components/sigur/api/` is a self-contained async OIF client. It can be
lifted into its own package later without touching protocol code. That is also
why its tests run on any platform, including Windows, where Home Assistant core
itself cannot be imported.

## Consequences

* Two TCP sessions per configured server instead of one. Acceptable: the
  protocol has no multiplexing to share.
* Backfilled events have `event_code: null` and only a category. Documented in
  the README so automations key on `category`, not on `event_code`.
* Polling remains as a safety net (default 30 s, floor 5 s) even though events
  update entities immediately, because the protocol offers no way to confirm
  that a subscription is still healthy other than asking.
