#!/usr/bin/env python3
r"""Opt-in, read-only probe against a real Sigur OIF server.

The automated test suite never touches a real installation. This script exists
so that a maintainer can verify the client against their own server, and it is
**read-only by default**: it only issues ``LOGIN``, ``GETZONEINFO``,
``GETAPLIST``, ``GETAPINFO`` and, optionally, a short ``SUBSCRIBE`` listen.

It never sends ``SETAPMODE``, ``ALLOWPASS``, ``SYNCDB*``, ``DEVCONF_*``,
``IP_SETCONF``, ``DELEGATION_*`` or any other command that changes state.

Usage:
    python scripts/probe_real_server.py --host sigur.example.com --username asuUser
    python scripts/probe_real_server.py --host 10.0.0.5 --tls --ca-bundle ca.pem \\
        --username asuUser --listen 30
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
from pathlib import Path
import sys

# Import the protocol package directly rather than through
# `custom_components.sigur`, whose __init__ pulls in Home Assistant. The point
# of the api layer is that it stands on its own, and this script relies on it.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "sigur")
)

from api import (
    DEFAULT_OIF_VERSION,
    DEFAULT_PORT,
    Credentials,
    OifConnection,
    RawEvent,
    SigurApi,
    SigurError,
    TlsSettings,
    TransportSettings,
    create_ssl_context,
    resolve_event_code,
)

_LOGGER = logging.getLogger("sigur.probe")


def _parse_args() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Sigur server host or IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--username", required=True, help="OIF operator login")
    parser.add_argument(
        "--password",
        default=os.environ.get("SIGUR_PASSWORD"),
        help="operator password; prompted for, or read from $SIGUR_PASSWORD",
    )
    parser.add_argument("--oif-version", default=DEFAULT_OIF_VERSION)
    parser.add_argument("--tls", action="store_true", help="connect over TLS")
    parser.add_argument("--ca-bundle", help="path to a custom CA bundle")
    parser.add_argument("--client-cert", help="client certificate for mutual TLS")
    parser.add_argument("--client-key", help="private key for mutual TLS")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="DANGEROUS: skip certificate verification; the OIF 1.8 password "
        "travels in clear text inside the protocol",
    )
    parser.add_argument(
        "--listen",
        type=int,
        default=0,
        metavar="SECONDS",
        help="after the read-only probe, subscribe and print events for this long",
    )
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def _on_event(raw: RawEvent) -> None:
    """Print one event, with its credential number masked."""
    resolved = (
        resolve_event_code(raw.event_code) if raw.event_code is not None else None
    )
    label = resolved.description_en if resolved else (raw.classic_type or "?")
    print(
        f"  {raw.occurred_at:%Y-%m-%d %H:%M:%S}  ap={raw.access_point_id}  "
        f"code={raw.event_code}  {label}  dir={raw.direction}  key={raw.key.masked}"
    )


async def _probe(args: argparse.Namespace) -> int:
    """Run the read-only probe. Returns the process exit code."""
    password = args.password or getpass.getpass("OIF operator password: ")
    tls = TlsSettings(
        enabled=args.tls,
        verify=not args.no_verify,
        ca_bundle=args.ca_bundle,
        client_certificate=args.client_cert,
        client_key=args.client_key,
    )
    if args.tls and args.no_verify:
        print("WARNING: certificate verification is disabled.", file=sys.stderr)

    settings = TransportSettings(host=args.host, port=args.port, tls=tls)
    credentials = Credentials(args.username, password, args.oif_version)
    ssl_context = await asyncio.to_thread(create_ssl_context, tls)

    events: list[RawEvent] = []
    connection = OifConnection(
        settings,
        credentials,
        ssl_context=ssl_context,
        event_callback=lambda raw: (events.append(raw), _on_event(raw))[0],
        name="probe",
    )
    api = SigurApi(connection)

    try:
        print(f"Connecting to {args.host}:{args.port} (TLS: {tls.mode})...")
        await connection.connect()
        print("LOGIN accepted.")

        zones = await api.get_zones()
        print(f"\nZones ({len(zones)}):")
        for zone in zones:
            print(f"  {zone.id:>5}  {zone.name}")

        ap_ids = await api.get_access_point_ids()
        print(f"\nAccess points ({len(ap_ids)}):")
        for ap_id in ap_ids:
            info = await api.get_access_point(ap_id)
            print(
                f"  {info.id:>5}  {info.name!r:<32} "
                f"zones {info.zone_a}->{info.zone_b}  "
                f"{info.state.value:<16} {info.open_state.value}"
            )

        if args.listen > 0:
            mode = await connection.subscribe()
            print(f"\nSubscribed using {mode}. Listening for {args.listen} s:")
            await asyncio.sleep(args.listen)
            await connection.unsubscribe()
            print(f"Received {len(events)} event(s).")
    except SigurError as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        return 1
    finally:
        await connection.close()

    print("\nProbe finished. No state-changing command was sent.")
    return 0


def main() -> int:
    """Entry point."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    return asyncio.run(_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
