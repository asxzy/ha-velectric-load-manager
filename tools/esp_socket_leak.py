#!/usr/bin/env python3
"""Reproduce the "abandoned socket" leak on an ESP web server.

The Home Assistant integration sends byte 0x67 over /ws every 2 s, and when the
connection drops it *replaces* the socket object instead of closing it
(see websocket_client.py: `_message_handler` sets _connected=False but never
closes the socket, and `disconnect()` early-returns because _connected is
already False). The old TCP connection is never closed from the client side.

On an ESP the TCP stack has a small, fixed pool of PCBs (~5 by default for
LWIP in many Arduino builds). Leaked sockets therefore translate directly into
"the device accepts TCP but can no longer answer HTTP".

This tool proves or disproves that with a staged ramp. It NEVER opens more than
--max-leaks connections, waits --settle between steps, and probes HTTP health
after every step so you can stop at the first sign of trouble.

Two modes:

  leak    open a socket, complete the WS handshake, then abandon it WITHOUT a
          close frame (leaking it exactly like the integration does)
  churn   open + close properly, to separate "leak" from "connect churn"

Usage:
    # stage the ramp; stop manually with Ctrl-C at any time
    python tools/esp_socket_leak.py --host 192.168.1.91 --mode leak --max-leaks 8

    # control: proper closes, same rate -- should NOT wedge the device
    python tools/esp_socket_leak.py --host 192.168.1.91 --mode churn --max-leaks 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esp_probe import http_get  # noqa: E402


async def http_healthy(
    host: str, port: int, path: str, timeout: float
) -> tuple[bool, str]:
    ok, lat, status, body, detail = await http_get(host, port, path, timeout)
    healthy = bool(ok and status == 200 and body)
    return (
        healthy,
        f"{'ok' if healthy else 'DEAD'} {lat * 1000:.0f}ms {detail or status}",
    )


class Leaked:
    """A socket we deliberately do not close."""

    def __init__(self) -> None:
        self.writer: asyncio.StreamWriter | None = None

    async def open_abandoned(
        self, host: str, port: int, path: str, timeout: float
    ) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except (asyncio.TimeoutError, OSError):
            return False
        try:
            writer.write(
                (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
                ).encode()
            )
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=timeout
            )
        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
            writer.close()
            return False

        if b"101" not in head.split(b"\r\n", 1)[0]:
            writer.close()
            return False

        # send the same request byte the integration sends, then go silent
        mask = b"\x00\x00\x00\x00"
        writer.write(bytes([0x82, 0x81]) + mask + bytes([103]))
        try:
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except (asyncio.TimeoutError, OSError):
            pass

        # IMPORTANT: keep the writer referenced so asyncio does not close it,
        # and never call close() -- this is the abandonment
        self.writer = writer
        return True

    def drop_hard(self) -> None:
        """Release our only handle without a close frame."""
        if self.writer is not None:
            try:
                self.writer.transport.abort()
            except Exception:
                pass
            self.writer = None


async def main_async(args: argparse.Namespace) -> int:
    print(f"socket-leak test on {args.host}:{args.port}{args.path} mode={args.mode}")
    print(
        f"max_leaks={args.max_leaks} settle={args.settle}s "
        f"http_timeout={args.http_timeout}s"
    )

    healthy, msg = await http_healthy(
        args.host, args.port, args.path, args.http_timeout
    )
    print(f"baseline HTTP: {msg}")
    if not healthy:
        print("device is already unhealthy; let it rest and retry later")
        return 2

    held: list[Leaked] = []
    results: list[dict] = []
    verdict = "inconclusive"

    try:
        for step in range(1, args.max_leaks + 1):
            leaked = Leaked()
            ok = await leaked.open_abandoned(
                args.host, args.port, args.path, args.timeout
            )
            if ok:
                if args.mode == "churn":
                    leaked.drop_hard()
                else:
                    held.append(leaked)
            print(
                f"[{time.strftime('%H:%M:%S')}] step {step}/{args.max_leaks}: "
                f"socket {'opened+abandoned' if ok else 'FAILED'} "
                f"(held={len(held)})",
                flush=True,
            )

            await asyncio.sleep(args.settle)
            healthy, msg = await http_healthy(
                args.host, args.port, args.path, args.http_timeout
            )
            print(f"    HTTP after step: {msg}", flush=True)
            results.append(
                {"step": step, "held": len(held), "healthy": healthy, "detail": msg}
            )
            if not healthy:
                verdict = (
                    "LEAK CONFIRMED"
                    if args.mode == "leak"
                    else "churn also kills it (not a pure leak)"
                )
                print(
                    f"    -> {verdict}: HTTP died with {len(held)} "
                    f"abandoned socket(s) held open"
                )
                break
        else:
            verdict = "no failure within the tested range"
            print(f"\n-> {verdict}: survived {args.max_leaks} {args.mode} cycles")
    finally:
        # release everything, even on Ctrl-C, so the device can recover
        for leaked in held:
            leaked.drop_hard()
        if held:
            print(f"released {len(held)} abandoned socket(s)")

    if args.recover:
        print("watching device recovery (HTTP 1/3s) ...")
        deadline = time.monotonic() + args.recover
        while time.monotonic() < deadline:
            healthy, msg = await http_healthy(
                args.host, args.port, args.path, args.http_timeout
            )
            print(f"  {msg}", flush=True)
            if healthy:
                print("  recovered without a restart")
                break
            await asyncio.sleep(3.0)
        else:
            print("  still dead: the chip needs a power cycle / reboot")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"mode": args.mode, "verdict": verdict, "steps": results}, indent=2
            ),
            encoding="utf-8",
        )
        print(f"json written to {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--path", default="/ws")
    p.add_argument("--mode", choices=("leak", "churn"), default="leak")
    p.add_argument("--max-leaks", type=int, default=8)
    p.add_argument(
        "--settle",
        type=float,
        default=6.0,
        help="seconds to wait before the health probe after each step",
    )
    p.add_argument("--timeout", type=float, default=6.0)
    p.add_argument("--http-timeout", type=float, default=6.0)
    p.add_argument("--recover", type=float, default=60.0)
    p.add_argument("--json")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
