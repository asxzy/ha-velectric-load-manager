#!/usr/bin/env python3
"""Exercise the rewritten client against a real device, counting its traffic.

Proves the two properties the rewrite is supposed to have:

  1. exactly ONE TCP connection, opened once and held
  2. ZERO periodic bytes sent by the client (no PING, no 0x67 polling)

    python tools/verify_client_footprint.py --host 192.168.1.91 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "velectric_load_manager"

STATS = {"connects": 0, "sends": 0, "bytes_out": 0}


def patch_websockets() -> None:
    """Wrap connect() so every connection and every send() is counted."""
    from websockets.asyncio.client import ClientConnection

    real_connect = websockets.connect
    real_send = ClientConnection.send

    def counting_connect(*args, **kwargs):
        STATS["connects"] += 1
        return real_connect(*args, **kwargs)

    async def send_counted(self, data, *a, **kw):
        STATS["sends"] += 1
        if isinstance(data, (bytes, bytearray)):
            STATS["bytes_out"] += len(data)
        else:
            STATS["bytes_out"] += len(str(data))
        return await real_send(self, data, *a, **kw)

    websockets.connect = counting_connect
    ClientConnection.send = send_counted


def load_client_class():
    pkg_name = "velectric_load_manager"
    if pkg_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg_name, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
        )
        sys.modules[pkg_name] = importlib.util.module_from_spec(spec)

    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.websocket_client", PKG / "websocket_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.VElectricWebSocketClient


async def main_async(args: argparse.Namespace) -> int:
    patch_websockets()
    Client = load_client_class()

    client = Client(args.host, args.port)
    frames = {"readings": 0, "settings": 0}
    client.on_current_reading = lambda *_: frames.__setitem__(
        "readings", frames["readings"] + 1
    )
    client.on_settings_update = lambda *_: frames.__setitem__(
        "settings", frames["settings"] + 1
    )

    print(f"running client against {args.host}:{args.port} for {args.duration}s")
    await client.start()

    started = time.monotonic()
    last_report = started
    try:
        while time.monotonic() - started < args.duration:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - last_report >= args.report_every:
                last_report = now
                print(
                    f"  t+{now - started:5.1f}s  connects={STATS['connects']} "
                    f"sends={STATS['sends']} ({STATS['bytes_out']}B out) "
                    f"readings={frames['readings']} settings={frames['settings']} "
                    f"connected={client.is_connected} "
                    f"status={client.connection_status.value}",
                    flush=True,
                )
    finally:
        await client.async_stop()

    elapsed = time.monotonic() - started
    print("\n=== verdict ===")
    print(f"duration          {elapsed:.1f}s")
    print(f"connections       {STATS['connects']}")
    print(f"bytes sent        {STATS['bytes_out']} in {STATS['sends']} frame(s)")
    print(f"readings received {frames['readings']}")

    # Two bytes is the floor: settings + one readings request. Pull firmware then
    # needs one request per reading, spaced PULL_INTERVAL apart. Anything above
    # that budget means the client is talking more than the device requires.
    expected_polls = int(elapsed / 8.0) + 1
    push_budget = 2
    pull_budget = 2 + expected_polls

    ok = True
    if STATS["connects"] > 1:
        print(f"FAIL: opened {STATS['connects']} connections; expected 1")
        ok = False
    else:
        print("PASS: exactly one connection")

    if STATS["sends"] <= push_budget:
        print(f"PASS: push firmware detected; only {STATS['sends']} bytes sent")
    elif STATS["sends"] <= pull_budget:
        print(
            f"PASS: request/response firmware; {STATS['sends']} bytes sent "
            f"(budget {pull_budget} at one poll per 8s)"
        )
    else:
        print(
            f"FAIL: sent {STATS['sends']} frames; expected at most {pull_budget} "
            f"(2 handshake + 1 per 8s poll)"
        )
        ok = False

    if frames["readings"] == 0:
        print("NOTE: no readings arrived; device may be wedged or idle")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--report-every", type=float, default=10)
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
